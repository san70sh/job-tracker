// Job details. One renderer, two homes: a modal over whatever page you are on (JobView.open), and the full page at
// /jobs/{id} (JobView.page). Needs base.html's helpers ($, esc, api, toast, metaReady, statusPill, modePill, stSlug, colOf)
// and modal.js. It talks to the server only through the JSON API.
window.JobView = (() => {
  const EDIT = [["role", "Role"], ["company", "Company"], ["level", "Level"], ["location", "Location"], ["job_ref", "Job ID"],
                ["experience_required", "Experience required"], ["team_domain", "Team / Domain"]];
  // fields that can tag a job for review (the server decides which, see review_reasons), and what to say about each reason
  const REVIEW_LABEL = {role: "Role", company: "Company", key_responsibilities: "Key responsibilities", requirements: "Requirements"};
  const REVIEW_WHY = {not_found: "not found", not_sure: "not sure it is right"};
  let ROOT = null, ID = null, CUR = {}, MODE = "page", NAV = [];
  let modal = null, fromPop = false;  // fromPop: the close came from the Back button, so the history is already right

  // ───────── content ─────────
  function secHtml(s) {
    const body = s.body_md.split("\n").filter(Boolean);
    const isList = body.length && body.every(l => l.startsWith("- "));
    return `<div class="sec"><h4>${esc(s.heading)} <span class="muted">· ${s.kind.replace("_", " ")}</span></h4>` +
      (isList ? `<ul>${body.map(l => `<li>${esc(l.slice(2))}</li>`).join("")}</ul>` : `<p>${esc(s.body_md)}</p>`) + `</div>`;
  }
  function view(j) {
    const llm = j.provenance.filter(p => p.method === "llm").map(p => p.field);
    const rv = Object.fromEntries((j.review || []).map(r => [r.field, r]));  // field -> why it needs a look
    const why = k => rv[k] ? ` <span class="why">${REVIEW_WHY[rv[k].reason]}</span>` : "";
    const cls = k => rv[k] ? ' class="review"' : "";
    const pos = NAV.findIndex(n => n.id === ID);
    const inModal = MODE === "modal", many = inModal && NAV.length > 1 && pos >= 0, flagged = inModal && NAV.some(n => n.review && n.id !== ID);
    const opts = ["", ...META.statuses.map(s => s.name)].map(o => `<option ${o === (j.status || "") ? "selected" : ""}>${esc(o)}</option>`).join("");
    const tools = inModal ? `<div class="jv-tools">
        ${many ? `<button class="ghost" data-act="prev" title="Previous job (←)" aria-label="Previous job">‹</button><span class="muted">${pos + 1} / ${NAV.length}</span>
                  <button class="ghost" data-act="next" title="Next job (→)" aria-label="Next job">›</button>` : ""}
        ${flagged ? '<button class="ghost" data-act="nextflag" title="Jump to the next job that needs a check">Next to review ›</button>' : ""}
        <button class="ghost jv-x" data-act="close" title="Close (Esc)" aria-label="Close">✕</button></div>` : "";
    return `<div class="jv ${MODE}">
      <div class="jv-head"><div class="jv-title"><h2>${esc(j.role)}</h2>
        <div class="muted">${esc(j.company)}${j.job_link ? ` · <a href="${esc(j.job_link)}" target="_blank" rel="noopener">posting</a>` : ""}${j.via_url ? ` · <a href="${esc(j.via_url)}" target="_blank" rel="noopener">careers page</a>` : ""} · ${j.ats === "manual" ? "added from pasted text" : esc(j.ats)}${j.closed_at ? " · closed " + j.closed_at : ""}</div>
        <div class="jv-pills">${statusPill(colOf(j))} ${modePill(j.work_mode)}</div></div>${tools}</div>
      <div class="jv-scroll">
        ${j.needs_review ? `<div class="jv-review"><span>${j.review.length
            ? "Check " + j.review.map(r => `<b>${REVIEW_LABEL[r.field]}</b> (${REVIEW_WHY[r.reason]})`).join(", ") + ". Fix what is wrong and save, or mark it reviewed if it is fine."
            : "Marked for a check. Mark it reviewed once you have looked."}</span><button class="ghost" data-act="reviewed">Mark reviewed</button></div>` : ""}
        ${llm.length ? `<p class="muted">Filled by the LLM fallback: ${llm.map(esc).join(", ")}</p>` : ""}
        <div class="layout"><div class="stack">
          <div class="card">
            <div class="two">${EDIT.map(([k, l]) => `<div><label>${l}${why(k)}</label><input type="text" data-f="${k}"${cls(k)} value="${esc(j[k])}"></div>`).join("")}
              <div><label>Work mode</label><select data-f="work_mode"><option value=""></option>${["Onsite", "Hybrid", "Remote"].map(o => `<option ${o === j.work_mode ? "selected" : ""}>${o}</option>`).join("")}</select></div>
              <div><label>Date applied</label><input type="date" data-f="date_applied" value="${j.date_applied || ""}"></div>
              <div><label>Salary min (LPA)</label><input type="number" step="0.1" data-f="salary_min_lpa" value="${j.salary_min_lpa ?? ""}"></div>
              <div><label>Salary max (LPA)</label><input type="number" step="0.1" data-f="salary_max_lpa" value="${j.salary_max_lpa ?? ""}"></div></div>
            <label>Salary details</label><textarea data-f="salary_details" style="min-height:60px">${esc(j.salary_details)}</textarea>
            <label>Notes</label><textarea data-f="notes">${esc(j.notes)}</textarea>
            <label>Key responsibilities${why("key_responsibilities")}</label><textarea data-f="key_responsibilities"${cls("key_responsibilities")}>${esc(j.key_responsibilities)}</textarea>
            <label>Requirements${why("requirements")}</label><textarea data-f="requirements"${cls("requirements")}>${esc(j.requirements)}</textarea>
          </div>
          <div class="card"><h3 style="margin:0">Posting</h3>${j.sections.map(secHtml).join("") || '<p class="muted">No sections stored.</p>'}</div>
        </div><div class="stack">
          <div class="card"><label style="margin-top:0">Status</label>
            <select data-role="status">${opts}</select>
            <p><button data-act="status">Update status</button></p>
            <div class="hist">${j.events.map(e => `<div><span class="dot" data-st="${stSlug(e.to_status)}"></span><span>${new Date(e.occurred_at).toLocaleDateString()} · ${esc(e.from_status || "—")} → ${esc(e.to_status)} <i>(${esc(e.source)})</i></span></div>`).join("") || '<span class="muted">Not applied yet.</span>'}</div>
          </div>
          <div class="card"><label style="margin-top:0">Technologies</label>
            ${j.technologies.map(t => `<span class="tag ${t.kind === "nice_to_have" ? "nice" : ""}" title="${t.kind}">${esc(t.name)}</span>`).join("") || '<span class="muted">none detected</span>'}</div>
          <div class="card"><label style="margin-top:0">Where each field came from</label>
            <div class="prov">${j.provenance.map(p => `<span class="m-${p.method}" title="confidence ${p.confidence}">${esc(p.field)}·${esc(p.method)}</span>`).join("")}</div></div>
        </div></div>
      </div>
      <div class="jv-foot"><span class="jv-dirty" hidden>Unsaved changes</span><span class="grow"></span>
        <button class="ghost" data-act="notion" title="Notion is a read-only mirror: the app overwrites it">Sync to Notion now</button>
        <button class="danger" data-act="delete">Delete</button><button data-act="save">Save</button></div></div>`;
  }

  // ───────── editing ─────────
  // Compare a form value and a stored value like for like: empty = null, numbers as numbers, line endings unified.
  function norm(k, v) {
    if (v === null || v === undefined || v === "") return null;
    if (k.startsWith("salary_m")) return Number(v);
    return String(v).replace(/\r\n/g, "\n");
  }
  function changes() {  // only the fields you actually changed, so untouched fields are never recorded as "edited by hand"
    const body = {};
    for (const el of ROOT.querySelectorAll("[data-f]")) {
      const k = el.dataset.f, now = norm(k, el.value);
      if (now !== norm(k, CUR[k])) body[k] = now;
    }
    return body;
  }
  const dirty = () => !!ROOT && !!ROOT.querySelector("[data-f]") && Object.keys(changes()).length > 0;
  const showDirty = () => { const d = ROOT && ROOT.querySelector(".jv-dirty"); if (d) d.hidden = !dirty(); };
  const outside = () => { if (MODE === "modal" && typeof window.reload === "function") window.reload(); };  // refresh the page behind

  async function load() {
    let j;
    try { j = await api(`/api/jobs/${ID}`); } catch { ROOT.innerHTML = '<div class="jm-loading muted">Not found.</div>'; return; }
    CUR = j;
    const scroll = ROOT.querySelector(".jv-scroll"), top = scroll ? scroll.scrollTop : 0;
    ROOT.innerHTML = view(j);
    const s = ROOT.querySelector(".jv-scroll"); if (s) s.scrollTop = top;
  }

  async function act(name, el) {
    try {
      if (name === "close") return modal.close();
      if (name === "prev" || name === "next") return step(name === "next" ? 1 : -1);
      if (name === "nextflag") return nextFlagged();
      if (name === "save") {
        const body = changes();
        if (!Object.keys(body).length) return toast("Nothing changed");
        if ("company" in body && !body.company) return toast("Company cannot be empty", "err");
        await api(`/api/jobs/${ID}`, {method: "PATCH", body});
        toast("Saved"); await load(); outside();
      } else if (name === "status") {
        const s = ROOT.querySelector("[data-role=status]").value; if (!s) return;
        await api(`/api/jobs/${ID}/status`, {method: "POST", body: {status: s}});
        toast("Status updated"); await load(); outside();
      } else if (name === "notion") {
        const r = await api(`/api/jobs/${ID}/notion`, {method: "POST"});
        toast(r.failed ? "Queued; Notion push failed, will retry" : "Pushed to Notion");
      } else if (name === "reviewed") {
        await api(`/api/jobs/${ID}/reviewed`, {method: "POST"}); toast("Marked reviewed"); await load(); outside();
      } else if (name === "delete") {
        if (!confirm("Delete this job from the tracker?")) return;
        await api(`/api/jobs/${ID}`, {method: "DELETE"});
        if (MODE === "modal") { modal.close(true); toast("Job deleted"); outside(); } else location.href = "/pipeline";
      }
    } catch (e) { toast(esc(e.message), "err"); }
  }
  function bind() {
    ROOT.onclick = e => { const b = e.target.closest("[data-act]"); if (b) act(b.dataset.act, b); };
    ROOT.oninput = ROOT.onchange = showDirty;
  }

  // ───────── moving between jobs (modal) ─────────
  async function loadNav() {
    let list = null;
    try { list = typeof window.jobNav === "function" ? window.jobNav() : null; } catch { list = null; }
    if (!list || !list.length) {
      try { list = (await api("/api/jobs")).map(j => ({id: j.id, review: j.needs_review})); } catch { list = []; }
    }
    NAV = list;
  }
  async function go(id) {
    if (dirty() && !confirm("Discard your unsaved changes?")) return;
    history.replaceState({jv: id}, "", "/jobs/" + id);
    ID = id; await load(); modal.panel.focus();
  }
  function step(d) {
    const i = NAV.findIndex(n => n.id === ID);
    if (i >= 0 && NAV[i + d]) return go(NAV[i + d].id);
  }
  function nextFlagged() {
    const i = NAV.findIndex(n => n.id === ID), after = NAV.slice(i + 1).find(n => n.review), first = NAV.find(n => n.review && n.id !== ID);
    const target = after || first;
    if (target) return go(target.id);
  }

  // ───────── the modal: modal.js is the shell; this adds what is specific to jobs ─────────
  const confirmDiscard = () => !dirty() || confirm("Discard your unsaved changes?");
  function arrows(e) {  // ← → step through the jobs, unless you are typing in a field
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName) || e.target.isContentEditable;
    if (typing || e.altKey || e.ctrlKey || e.metaKey || !["ArrowLeft", "ArrowRight"].includes(e.key)) return false;
    e.preventDefault(); step(e.key === "ArrowRight" ? 1 : -1);
    return true;
  }
  function ensure() {
    if (modal) return;
    modal = Modal({
      label: "Job details", beforeClose: confirmDiscard, onKey: arrows,
      onClose: () => {
        ROOT = null;
        if (!fromPop && history.state && history.state.jv) history.back();  // closed in the page: undo the address change
        fromPop = false;
      },
    });
    window.addEventListener("popstate", () => {  // the browser's Back button closes the modal, Forward reopens it
      const st = history.state;
      if (modal.isOpen() && !(st && st.jv)) {
        if (!confirmDiscard()) { history.pushState({jv: ID}, "", "/jobs/" + ID); return; }
        fromPop = true; modal.close(true);
      } else if (!modal.isOpen() && st && st.jv) open(st.jv, false);
    });
  }

  async function open(id, push = true) {
    await metaReady; ensure();
    MODE = "modal"; ID = id;
    if (!modal.isOpen()) {
      if (push) history.pushState({jv: id}, "", "/jobs/" + id);  // so a refresh or a shared link opens the same job as a page
      ROOT = modal.body; ROOT.innerHTML = '<div class="jm-loading muted">Loading…</div>';
      modal.open();
    } else history.replaceState({jv: id}, "", "/jobs/" + id);
    ROOT = modal.body; bind();
    await loadNav();
    await load();
  }

  async function page(root, id) {
    await metaReady;
    MODE = "page"; ROOT = root; ID = id; NAV = [];
    bind(); await load();
  }

  return {open, page, isOpen: () => !!modal && modal.isOpen(), reviewLabel: f => REVIEW_LABEL[f] || f};
})();
