"""URL -> stored job. Fetch is async; every database touch is a short sync call run in a thread."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from . import PARSER_VERSION, db, repo
from .adapters import registry
from .adapters.base import NeedsFallback, Target
from .adapters.generic import parse_html
from .extract.facts import find_req_id, parse_experience
from .extract.job import apply_salary, extract, missing_fields
from .extract.pasted import Parsed, canonical_key, parse_pasted
from .extract.sections import bullets_of, split_sections
from .extract.text import blocks_to_text, html_to_blocks
from .http import FetchError, Fetcher, PostingGone
from .watcher import BOARD_ERRORS
from .llm import LLMFallback
from .models import Posting
from .urls import canonicalize

log = logging.getLogger("jobtracker.pipeline")


@dataclass
class IngestResult:
    status: str  # created | duplicate | failed
    message: str = ""
    job_id: str | None = None
    needs_review: bool = False
    missing: list[str] | None = None
    ats: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__


def _db(fn, *a, **kw):
    def run():
        with db.conn() as c:
            return fn(c, *a, **kw)
    return asyncio.to_thread(run)


async def fetch_posting(url: str, f: Fetcher) -> tuple[Posting, str]:
    """Resolve the adapter and fetch. Falls back to the generic parser if a structured source fails.
    Returns (posting, how) where `how` notes any degradation."""
    resolved = await registry.resolve(url, f)
    how = resolved.adapter.ats
    if how == "html" and resolved.target.html and (inner := registry.delegate_from_html(resolved.target.html, url)):
        try:  # the careers page only displays the job: read it from the system behind it (structured, and the same address the watcher finds)
            posting = await inner.adapter.fetch(inner.target, f)
            posting.via = url
            return posting, inner.adapter.ats
        except (PostingGone, NeedsFallback, *BOARD_ERRORS) as e:
            log.warning("could not follow %s to %s (%s); reading the page itself", url, inner.target.url, e)
    try:
        return await resolved.adapter.fetch(resolved.target, f), how
    except PostingGone:
        raise
    except (NeedsFallback, FetchError, KeyError, ValueError, TypeError, IndexError) as e:
        if resolved.adapter.ats == "html":
            raise
        log.warning("%s adapter failed for %s (%s); degrading to generic parser", resolved.adapter.ats, url, e)
        html = resolved.target.html or await f.get_text(url)
        return parse_html(html, url, "html", resolved.target.board.company), f"{how}->html ({type(e).__name__})"


_WALL_HOSTS = ("linkedin.com",)
_WALL_TITLE = re.compile(r"sign in|log in|login|authwall|access denied|just a moment|captcha|verify you are human", re.I)
NEEDS_TEXT = ("needs a login or blocks automated access. Open the job in your own browser, copy the page text "
              "and paste it here instead.")


async def ingest(url: str, *, fetcher: Fetcher | None = None, llm: LLMFallback | None = None,
                 stage: str = "Inbox") -> IngestResult:
    own = fetcher is None
    f = fetcher or Fetcher()
    try:
        canonical = canonicalize(url)
        dup = await _db(repo.find_job_by_url, canonical)
        if dup:
            return IngestResult("duplicate", f"already tracked: {dup['role']}", str(dup["id"]))
        host = (urlsplit(canonical).hostname or "").lower()
        if any(host == h or host.endswith("." + h) for h in _WALL_HOSTS):
            return IngestResult("needs_text", f"{host} {NEEDS_TEXT}")

        try:
            posting, how = await fetch_posting(url, f)
        except PostingGone:
            return IngestResult("failed", "posting is gone (404/410) — it may have been closed")
        except FetchError as e:
            if re.match(r"(401|403|999)\b", str(e)):
                return IngestResult("needs_text", f"{host} {NEEDS_TEXT}")
            return IngestResult("failed", f"could not fetch: {e}")
        if posting.ats in ("html", "icims") and not posting.description_html and _WALL_TITLE.search(posting.title or ""):
            return IngestResult("needs_text", f"{host} {NEEDS_TEXT}")
        if not posting.title:
            return IngestResult("failed", "no job title found on the page (is this a job posting URL?)", ats=posting.ats)
        if posting.via:  # saved under the address of the system behind the page, so both routes to a job find the same one
            canonical = canonicalize(posting.url)
            if dup := await _db(repo.find_job_by_url, canonical):
                return IngestResult("duplicate", f"already tracked: {dup['role']}", str(dup["id"]))
        snapshot = json.dumps({"how": how, "via": posting.via, "raw": posting.raw}, default=str, ensure_ascii=False)
        return await store_posting(posting, canonical=canonical, snapshot=snapshot, stage=stage, llm=llm)
    finally:
        if own:
            await f.aclose()


async def store_posting(posting: Posting, *, canonical: str, snapshot: str, stage: str = "Inbox",
                        llm: LLMFallback | None = None, overrides: dict[str, Any] | None = None) -> IngestResult:
    """Posting -> extracted fields -> stored job. `overrides` are values you typed or confirmed: they win over
    every extractor and are recorded as manual."""
    ex = extract(posting)
    description = blocks_to_text(html_to_blocks(posting.description_html)) if posting.description_html else ""

    # salary (INR only): posted range -> cached benchmarks
    bench = await _db(repo.load_benchmarks)
    salary = apply_salary(ex, bench)
    for name, value in (overrides or {}).items():
        ex.set(name, value, "manual", 1.0)
        if name == "experience_required":
            e = parse_experience(str(value), require_context=False)
            if e:
                ex.set("experience_min_years", e.min_years, "manual", 1.0)
                ex.set("experience_max_years", e.max_years, "manual", 1.0)
        if name in ("salary_min_lpa", "salary_max_lpa"):
            ex.set("salary_details", "Entered by you.", "manual", 1.0)
    company = ex.get("company") or "Unknown"

    # LLM only for what rules could not fill
    missing = missing_fields(ex)
    llm_ids: dict[str, str] = {}
    if missing:
        llm = llm or LLMFallback(
            cache_get=lambda t, h: _sync(repo.llm_cache_get, t, h),
            cache_put=lambda **kw: _sync(repo.llm_cache_put, **kw),
        )
        if llm.available:
            llm_ids = await asyncio.to_thread(llm.fill, ex, missing, description)
            missing = missing_fields(ex)
    needs_review = any(m in missing for m in ("role", "company", "key_responsibilities", "requirements"))

    def persist(c):
        cid = repo.upsert_company(c, company, posting.ats if posting.ats not in ("html", "icims", "manual") else None)
        ref_dup = repo.find_job_by_ref(c, cid, ex.get("job_ref"))
        if ref_dup:
            return IngestResult("duplicate", f"same requisition already tracked: {ref_dup['role']}", str(ref_dup["id"]))
        jid = repo.save_job(c, ex, canonical=canonical, company_id=cid, snapshot_body=snapshot,
                            parser_version=PARSER_VERSION, salary=salary, needs_review=needs_review, stage=stage,
                            llm_call_ids=llm_ids)
        return IngestResult("created", f"{ex.get('role')} @ {company}", jid, needs_review, missing, posting.ats)

    return await _db(persist)


# ───────────────────────────── pasted text and captured pages ─────────────────────────────
_PLAIN = ("work_mode", "level", "experience_required", "team_domain", "salary_min_lpa", "salary_max_lpa")


def _same(a: Any, b: Any) -> bool:
    if a in (None, "") and b in (None, ""):
        return True
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return str(a).strip() == str(b).strip()


# A "company site" link only counts if it leads to one careers position. These are the checks that stop a seller
# portal, a blog post or a product page from being read as the job.
_STRUCTURED = {"greenhouse", "lever", "ashby", "smartrecruiters", "workday", "oracle", "eightfold", "amazon", "jibe", "avature"}
_CAREERS_PATH = re.compile(
    r"/(careers?|jobs?|positions?|openings?|vacanc(?:y|ies)|opportunit(?:y|ies)|requisitions?|job-?details?|job-?postings?|apply)(?:[/_.-]|$)", re.I)
_BOARD_HOSTS = ("linkedin.com", "indeed.", "naukri.", "glassdoor.", "monster.", "foundit.", "instahyre.", "cutshort.")
_SITE_TTL = 30 * 60
_site_cache: dict[str, tuple[float, Posting | None, str, str | None]] = {}


def careers_page_problem(posting: Posting, how: str, url: str) -> str | None:
    """None when the fetched page is a careers position page; otherwise the reason it is not."""
    text = blocks_to_text(html_to_blocks(posting.description_html)) if posting.description_html else ""
    if not (posting.title or "").strip():
        return "the page has no job title"
    if len(text) < 300:
        return "the page has no job description"
    if posting.ats in _STRUCTURED and how == posting.ats:
        return None  # a recognised ATS only matches addresses of single postings
    if isinstance(posting.raw, dict) and "jsonld" in posting.raw:
        return None  # the page declares itself a JobPosting
    kinds = {s.kind for s in split_sections(posting.description_html)} & {"responsibilities", "requirements", "nice_to_have"}
    if _CAREERS_PATH.search(urlsplit(url).path) and len(kinds) >= 2:
        return None
    return "the link does not look like a careers position page"


async def fetch_site_posting(url: str, fetcher: Fetcher | None = None) -> tuple[Posting | None, str, str | None]:
    """Read the company's own posting. Returns (posting, how, None), or (None, "", why it was not used)."""
    host = (urlsplit(url).hostname or "").lower()
    if urlsplit(url).scheme not in ("http", "https") or not host:
        return None, "", "the link is not a web address"
    if any(h in host for h in _BOARD_HOSTS):
        return None, "", "the link is another job board, not the company's site"
    now = time.time()
    hit = _site_cache.get(url)
    if hit and now - hit[0] < _SITE_TTL:
        return hit[1], hit[2], hit[3]
    own = fetcher is None
    f = fetcher or Fetcher()
    try:
        try:
            posting, how = await asyncio.wait_for(fetch_posting(url, f), timeout=25)
            problem = careers_page_problem(posting, how, url)
            result = (None, "", problem) if problem else (posting, how, None)
        except PostingGone:
            result = (None, "", "the company's posting is gone (404/410)")
        except asyncio.TimeoutError:
            result = (None, "", "the company site took too long to answer")
        except (FetchError, KeyError, ValueError, TypeError, IndexError) as e:
            result = (None, "", f"could not read the company site ({str(e)[:80]})")
    finally:
        if own:
            await f.aclose()
    _site_cache[url] = (now, *result)
    return result


def _with_site(site: Posting, page: Parsed, site_link: str) -> Posting:
    """The company's posting is the base; what only the job board's page knows fills the gaps."""
    p = site.model_copy(deep=True)
    p.url = site_link
    if page.company.value:  # the board's company name is usually cleaner than one derived from a host name
        p.company = page.company.value
    if not any(ch.isalnum() for ch in (p.location or "")):  # empty, or just punctuation: no place was given
        p.location = page.posting.location
    p.work_mode = p.work_mode or page.posting.work_mode
    text = blocks_to_text(html_to_blocks(p.description_html)) if p.description_html else ""
    if not (p.job_ref or p.ats_posting_id or find_req_id(text)):
        p.job_ref = page.posting.job_ref  # the board's own id, only when the company site gives none
    return p


def _apply_confirmed(p: Posting, o: dict[str, Any]) -> None:
    """Values confirmed in the preview, applied to a posting that came from the company site."""
    if o.get("company"):
        p.company = o["company"]
    if o.get("role"):
        p.title = o["role"]
    for key, attr in (("location", "location"), ("job_ref", "job_ref"), ("job_link", "url")):
        if key in o:
            setattr(p, attr, o[key] or (None if attr != "url" else ""))
    if o.get("work_mode") in ("Onsite", "Hybrid", "Remote"):
        p.work_mode = o["work_mode"]


async def _source(text: str, link: str | None, hints: dict | None, site_link: str | None, prefer: str,
                  confirmed: dict[str, Any] | None = None) -> tuple[Posting, Parsed, dict[str, Any]]:
    """Choose what the job is read from: the company's own page when there is a usable link to it, else the
    text from the job board. Returns the posting, the parse of the board's text, and where it came from."""
    page = parse_pasted(text, link=link, hints=hints, overrides=confirmed)
    origin: dict[str, Any] = {"source": "page", "ats": None, "url": None, "reason": None}
    if site_link and prefer != "page":
        site, how, reason = await fetch_site_posting(site_link)
        if site:
            posting = _with_site(site, page, site_link)
            if confirmed:
                _apply_confirmed(posting, confirmed)
            return posting, page, {"source": "company_site", "ats": site.ats, "url": site_link, "reason": None}
        origin["reason"] = reason
        origin["url"] = site_link
    elif site_link:
        origin["url"] = site_link
    return page.posting, page, origin


async def draft_from_text(text: str, link: str | None = None, hints: dict | None = None, site_link: str | None = None,
                          prefer: str = "site") -> dict[str, Any]:
    """What the preview shows: every field found, with how sure each one is and where the posting was read from."""
    posting, page, origin = await _source(text, link, hints, site_link, prefer)
    ex = extract(posting)
    apply_salary(ex, await _db(repo.load_benchmarks))
    from_text = origin["source"] == "page"  # rules guessed company/title/location from text: show how sure they were
    conf = ({"company": page.company.confidence, "role": page.title.confidence, "location": page.location.confidence}
            if from_text else {"company": page.company.confidence or 0.8})

    def fv(name: str) -> dict[str, Any]:
        f = ex.fields.get(name)
        return {"value": f.value if f else None, "confidence": round(conf.get(name, f.confidence if f else 0.0), 2),
                "method": f.method if f else None, "evidence": f.evidence if f else None}

    fields = {n: fv(n) for n in ("company", "role", "location", "work_mode", "level", "experience_required",
                                  "team_domain", "job_ref", "job_link", "salary_min_lpa", "salary_max_lpa")}
    fields["company"]["value"] = posting.company
    fields["role"]["value"] = posting.title or None
    fields["location"]["value"] = posting.location
    dup = await _db(repo.find_job_by_url, canonical_key(posting.url or None, text))
    if not dup and fields["company"]["value"] and fields["job_ref"]["value"]:
        dup = await _db(repo.find_job_by_company_ref, fields["company"]["value"], fields["job_ref"]["value"])

    def count(kind: str) -> int:
        return sum(len(s.bullets or bullets_of(s.text)) for s in ex.sections if s.kind == kind)

    return {
        "fields": fields,
        "technologies": ex.technologies,
        "counts": {"responsibilities": count("responsibilities"), "required": count("requirements"),
                   "nice_to_have": count("nice_to_have")},
        "duplicate": {"id": str(dup["id"]), "role": dup["role"]} if dup else None,
        "origin": origin,
    }


async def ingest_text(text: str, fields: dict[str, Any], *, link: str | None = None, hints: dict | None = None,
                      site_link: str | None = None, prefer: str = "site", stage: str = "Inbox",
                      llm: LLMFallback | None = None) -> IngestResult:
    """Save a captured page or pasted text with the field values confirmed in the preview. Company and title are
    always recorded as manual (you confirmed them); other fields only where you changed what was found."""
    clean = {k: (v.strip() if isinstance(v, str) else v) for k, v in fields.items()}
    company, role = clean.get("company"), clean.get("role")
    if not company or not role:
        return IngestResult("failed", "company and job title are required")
    sent = {k: clean[k] for k in ("location", "job_ref", "job_link", "work_mode") if k in clean}  # absent = keep what was found
    posting, _page, origin = await _source(text, link, hints, site_link, prefer, confirmed={"company": company, "role": role, **sent})
    canonical = canonical_key(posting.url or None, text)
    dup = await _db(repo.find_job_by_url, canonical)
    if dup:
        return IngestResult("duplicate", f"already tracked: {dup['role']}", str(dup["id"]))

    base_posting, base_page, _ = await _source(text, link, hints, site_link, prefer)
    base = extract(base_posting)
    apply_salary(base, await _db(repo.load_benchmarks))
    overrides: dict[str, Any] = {"company": company, "role": role}
    for name in _PLAIN:
        v = clean.get(name)
        if v in (None, "") or _same(v, base.get(name)):
            continue
        overrides[name] = float(v) if name.startswith("salary_") else v
    snapshot = json.dumps({"how": origin["source"], "raw": {
        "text": text, "link": posting.url or None, "hints": hints, "origin": origin,
        "site": posting.raw if origin["source"] == "company_site" else None}}, default=str, ensure_ascii=False)
    return await store_posting(posting, canonical=canonical, snapshot=snapshot, stage=stage, llm=llm, overrides=overrides)


def _sync(fn, *a, **kw):
    with db.conn() as c:
        return fn(c, *a, **kw)
