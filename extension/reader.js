// Runs inside the job page you are looking at. It only reads; it sends nothing anywhere itself.
// Order of trust: the page's own job data (JSON-LD), then a site-specific reader (LinkedIn, Indeed), then a generic
// guess. If you had selected text on the page, that selection is used as the description instead.
// importScripts()'d by background.js, and injected with chrome.scripting.executeScript({func: readPage}).
function readPage() {
  const MAX = 60000;
  const clean = s => String(s || "").replace(/ /g, " ").replace(/[ \t]+\n/g, "\n").replace(/\n{3,}/g, "\n\n").trim();
  const textOf = el => (el ? clean(el.innerText || el.textContent) : "");
  const pick = sels => { for (const s of sels) { const el = document.querySelector(s); if (el && textOf(el)) return el; } return null; };
  const meta = name => (document.querySelector(`meta[property="${name}"], meta[name="${name}"]`) || {}).content || "";

  // HTML from structured data -> text, parsed in an inert document (nothing in it can run or load)
  const htmlToText = html => {
    const doc = new DOMParser().parseFromString(
      String(html).replace(/<\s*br\s*\/?>/gi, "\n").replace(/<\/(p|div|h[1-6]|ul|ol|tr)>/gi, "\n")
        .replace(/<li[^>]*>/gi, "\n- ").replace(/<\/li>/gi, ""), "text/html");
    return clean(doc.body ? doc.body.textContent : "");
  };

  const out = {url: location.href, text: "", top: "", hints: {}, source: "generic"};
  const host = location.hostname.replace(/^www\./, "");

  // 1. structured data most career sites publish for search engines
  try {
    for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
      let data; try { data = JSON.parse(s.textContent); } catch { continue; }
      const jp = [].concat(data || [], (data && data["@graph"]) || [])
        .find(x => x && [].concat(x["@type"] || []).includes("JobPosting"));
      if (!jp) continue;
      const org = jp.hiringOrganization;
      const loc = [].concat(jp.jobLocation || [])[0];
      const a = (loc && loc.address) || {};
      const part = v => (v && typeof v === "object" ? v.name : v) || "";
      const country = v => (part(v) === "IN" ? "India" : part(v));
      let where = [part(a.addressLocality), part(a.addressRegion), country(a.addressCountry)].filter(Boolean).join(", ");
      if (jp.jobLocationType === "TELECOMMUTE") where = (where ? where + " " : "") + "(Remote)";
      out.hints = {title: jp.title || "", company: (org && (org.name || org)) || "", location: where, confidence: 0.95};
      out.text = htmlToText(jp.description || "");
      out.source = "jsonld";
      break;
    }
  } catch (e) { /* fall through to the other readers */ }

  // 2. LinkedIn
  if (/(^|\.)linkedin\.com$/.test(host)) {
    const title = textOf(pick([".job-details-jobs-unified-top-card__job-title h1", ".job-details-jobs-unified-top-card__job-title",
                               ".jobs-unified-top-card__job-title", "h1"]));
    let company = textOf(pick([".job-details-jobs-unified-top-card__company-name", ".jobs-unified-top-card__company-name"]));
    // the tab title of a single-job page reads "Title | Company | LinkedIn"; a search page's does not describe one job
    const tab = document.title.replace(/^\(\d+\)\s*/, "").split(" | ");
    if (/\/jobs\/view\//.test(location.pathname) && tab.length >= 3 && tab[tab.length - 1] === "LinkedIn") {
      if (!company) company = tab[tab.length - 2];
      if (!title) out.hints.title = tab[0];
    }
    // The page address, reduced to the job: a search page with the job open on the right has the id in its query.
    const idm = location.pathname.match(/\/jobs\/view\/(?:[^/?]*-)?(\d{6,})/) || location.search.match(/currentJobId=(\d+)/);
    if (idm) out.url = `https://www.linkedin.com/jobs/view/${idm[1]}/`;
    // The company's own posting: the "Go to company site" link (under Application status) or the external Apply link.
    // LinkedIn wraps it in its own redirect, so unwrap the address it points to; one that stays on LinkedIn is no use.
    const unwrap = href => {
      try {
        const u = new URL(href, location.href), target = u.searchParams.get("url") || u.searchParams.get("redirect");
        const final = target ? new URL(target) : u;
        // only the job board's own pages are no use; a company's careers.linkedin.com is a real company site
        return /^(www\.|[a-z]{2}\.)?linkedin\.com$/.test(final.hostname) ? "" : final.href;
      } catch (e) { return ""; }
    };
    // Only links that say what they are count: "Go to company site", or an "Apply" button that leaves LinkedIn. LinkedIn
    // wraps EVERY outbound link on the page in the same redirect (including ones written into the description), so the
    // wrapper alone proves nothing. Links inside the description itself are never the apply link.
    const descEl = pick([".jobs-description__content", "#job-details", ".jobs-description-content__text", ".jobs-box__html-content"]);
    const desc = descEl || pick(["article", "main"]);
    let companySite = "", applyButton = "";
    for (const a of document.querySelectorAll("a[href]")) {
      if (descEl && descEl.contains(a)) continue;
      const label = (a.innerText || "").trim(), aria = a.getAttribute("aria-label") || "";
      if (!companySite && (/^go to company (web)?site$/i.test(label) || /go to company (web)?site/i.test(aria))) companySite = unwrap(a.href);
      else if (!applyButton && /^apply( now| on company (web)?site)?$/i.test(label) && /externalApply|\/safety\/go/i.test(a.href)) applyButton = unwrap(a.href);
    }
    if (companySite || applyButton) out.apply_url = companySite || applyButton;
    // The card above the description holds "Place · 2 weeks ago · 45 applicants". Class names change, so look for the
    // smallest block that has a "·" line and the title, in this order: any "top-card" element, then walking up from the title.
    const looksLikeCard = t => /[·•]/.test(t) && t.length < 900 && (!title || t.includes(title));
    const cards = [...document.querySelectorAll('[class*="top-card"]')].map(textOf).filter(looksLikeCard).sort((a, b) => a.length - b.length);
    if (cards.length) out.top = cards[0];
    else {
      let el = document.querySelector("h1");
      for (let i = 0; el && i < 7 && !out.top; i++, el = el.parentElement) { const t = textOf(el); if (looksLikeCard(t)) out.top = t; }
    }
    out.hints = {...out.hints, title: title || out.hints.title || "", company: company || out.hints.company || "", confidence: 0.9};
    if (desc) out.text = textOf(desc);
    out.source = "linkedin";
  }

  // 3. Indeed
  if (/(^|\.)indeed\.[a-z.]+$/.test(host)) {
    const title = textOf(pick(['h1[data-testid="jobsearch-JobInfoHeader-title"]', "h1.jobsearch-JobInfoHeader-title", "h1"])).replace(/\s*-\s*job post$/i, "");
    const company = textOf(pick(['[data-testid="inlineHeader-companyName"]', '[data-company-name="true"]']));
    const where = textOf(pick(['[data-testid="inlineHeader-companyLocation"]', '[data-testid="job-location"]']));
    const desc = pick(["#jobDescriptionText"]);
    out.hints = {...out.hints, title: title || out.hints.title || "", company: company || out.hints.company || "",
                 location: where || out.hints.location || "", confidence: 0.9};
    if (desc) out.text = textOf(desc);
    out.source = "indeed";
  }

  // 4. anything else: page title and the main content, flagged as a weak guess
  if (!out.text) {
    const main = pick(["main", "article", '[role="main"]']) || document.body;
    out.text = textOf(main);
  }
  if (!out.hints.title) {
    const h1 = textOf(document.querySelector("h1")) || meta("og:title");
    if (h1) out.hints = {...out.hints, title: h1, confidence: out.hints.confidence || 0.5};
  }
  if (!out.hints.company && meta("og:site_name")) out.hints = {...out.hints, company: meta("og:site_name"), confidence: out.hints.confidence || 0.5};

  // what you selected on the page wins: it is the one way to say "this is the posting" on any site
  const sel = clean(String(window.getSelection() || ""));
  if (sel.length >= 200) { out.text = sel; out.source += "+selection"; }

  out.text = out.text.slice(0, MAX);
  return out;
}
