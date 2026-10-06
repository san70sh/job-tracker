// The "check what was found" form, shared by the Home page (pasted text) and the extension's pop-up window.
// Needs the helpers from base.html: $, esc, api, toast.
window.JobPreview = (() => {
  const FIELDS = [
    ["company", "Company", "text", true], ["role", "Job title", "text", true], ["location", "Location", "text"],
    ["work_mode", "Work mode", "mode"], ["level", "Level", "text"], ["experience_required", "Experience", "text"],
    ["team_domain", "Team / Domain", "text"], ["job_ref", "Job ID", "text"], ["job_link", "Job link", "text"],
    ["salary_min_lpa", "Salary min (LPA)", "number"], ["salary_max_lpa", "Salary max (LPA)", "number"],
  ];
  // a required field that is empty, or a detected company/title/location the rules were not sure about
  const isWeak = (key, f, required) =>
    (required && !f.value) || (!!f.value && f.confidence < 0.6 && ["company", "role", "location"].includes(key));

  function inputFor([key, label, type, required], f) {
    const v = f.value ?? "", weak = isWeak(key, f, required), cls = weak ? "weak" : "";
    const hint = required && !v ? "Not found, type it" : (weak ? "Please check this one" : "");
    const ctl = type === "mode"
      ? `<select data-key="${key}" class="${cls}">${["", "Onsite", "Hybrid", "Remote"].map(o => `<option ${o === v ? "selected" : ""}>${o}</option>`).join("")}</select>`
      : `<input data-key="${key}" class="${cls}" type="${type === "number" ? "number" : "text"}" ${type === "number" ? 'step="0.1" min="0"' : ""} value="${esc(v)}">`;
    return `<div><label>${label}${required ? " *" : ""}</label>${ctl}${hint ? `<div class="hint">${hint}</div>` : ""}</div>`;
  }

  // Where the job was read from, and the way to switch to the other source.
  function originNote(o, ctx) {
    if (o.source === "company_site")
      return `<div class="origin">Read from the company site (${esc(o.ats)}) · <a href="${esc(o.url)}" target="_blank" rel="noopener">open it</a>
        · <button class="link" data-act="source" data-prefer="page">Use the LinkedIn text instead</button></div>`;
    if (!ctx.siteLink) return "";
    return ctx.prefer === "page"
      ? `<div class="origin">Read from the page text, as you asked. · <button class="link" data-act="source" data-prefer="site">Use the company site</button></div>`
      : `<div class="origin">The company link was not used: ${esc(o.reason || "it could not be read")}. Read from the page text.
         · <a href="${esc(o.url)}" target="_blank" rel="noopener">open the link</a></div>`;
  }

  // box: the element to draw in. ctx: {text, link, hints, siteLink, prefer, onSaved(result), onCancel()}
  async function open(box, ctx) {
    ctx.prefer = ctx.prefer || "site";
    if (ctx.siteLink && ctx.prefer !== "page") { box.hidden = false; box.innerHTML = '<div class="muted">Reading the company site…</div>'; }
    const draft = await api("/api/jobs/parse", {method: "POST", body: {text: ctx.text, link: ctx.link || null, hints: ctx.hints || null,
                                                                          site_link: ctx.siteLink || null, prefer: ctx.prefer}});
    const f = draft.fields, techs = Object.entries(draft.technologies || {});
    const weak = FIELDS.filter(([k, , , req]) => isWeak(k, f[k], req)).length, dup = draft.duplicate;
    box.innerHTML = `
      <h2>Check what was found <span class="muted">· ${weak ? weak + " field" + (weak === 1 ? "" : "s") + " need you" : "nothing needs you"}</span></h2>
      ${originNote(draft.origin, ctx)}
      ${dup ? `<div class="notice" style="margin:0 0 12px">Already tracked: <a href="/jobs/${dup.id}" target="_blank">${esc(dup.role)}</a>. Saving is switched off.</div>` : ""}
      <div class="grid">${FIELDS.map(spec => inputFor(spec, f[spec[0]])).join("")}</div>
      <div class="sum">
        <div><span>Technologies</span><span>${techs.length ? techs.map(([n, k]) => esc(n) + (k === "nice_to_have" ? " (nice)" : "")).join(", ") : "none found"}</span></div>
        <div><span>Responsibilities</span><span>${draft.counts.responsibilities} item${draft.counts.responsibilities === 1 ? "" : "s"}</span></div>
        <div><span>Requirements</span><span>${draft.counts.required} required, ${draft.counts.nice_to_have} good to have</span></div>
      </div>
      <div class="actions">
        <button class="ghost" data-act="cancel">Cancel</button>
        <button class="ghost" data-act="applied" ${dup ? "disabled" : ""}>Save as applied</button>
        <button data-act="later" ${dup ? "disabled" : ""}>Save for later</button>
      </div>`;
    box.hidden = false;
    for (const el of box.querySelectorAll("[data-key]")) el.addEventListener("input", () => el.classList.remove("weak"));
    box.querySelector('[data-act="cancel"]').onclick = () => (ctx.onCancel ? ctx.onCancel() : (box.hidden = true));
    const swap = box.querySelector('[data-act="source"]');
    if (swap) swap.onclick = async () => { ctx.prefer = swap.dataset.prefer; try { await open(box, ctx); } catch (err) { toast(esc(err.message), "err"); } };
    box.querySelector('[data-act="later"]').onclick = () => save(box, ctx, false);
    box.querySelector('[data-act="applied"]').onclick = () => save(box, ctx, true);
    return draft;
  }

  async function save(box, ctx, applied) {
    const fields = {};
    for (const el of box.querySelectorAll("[data-key]")) {
      const k = el.dataset.key, v = el.value.trim();
      fields[k] = k.startsWith("salary_") ? (v === "" ? null : Number(v)) : v;
    }
    for (const k of ["company", "role"]) {
      if (!fields[k]) {
        const el = box.querySelector(`[data-key=${k}]`); el.classList.add("weak"); el.focus();
        toast(`Enter the ${k === "role" ? "job title" : "company"} first.`, "err"); return;
      }
    }
    const btns = box.querySelectorAll("button"); btns.forEach(b => (b.disabled = true));
    try {
      const r = await api("/api/jobs/manual", {method: "POST", body: {text: ctx.text, link: ctx.link || null, hints: ctx.hints || null, site_link: ctx.siteLink || null, prefer: ctx.prefer || "site", fields, applied}});
      if (r.status === "duplicate") { toast(`Already tracked: <a href="/jobs/${r.job_id}">${esc(r.message)}</a>`); btns.forEach(b => (b.disabled = false)); return; }
      ctx.onSaved ? ctx.onSaved(r) : (location.href = "/jobs/" + r.job_id);
    } catch (err) { toast(esc(err.message), "err"); btns.forEach(b => (b.disabled = false)); }
  }

  return {open};
})();
