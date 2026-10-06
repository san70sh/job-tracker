"""Re-run extraction on jobs already stored, from the response saved with each job (no refetch).

Why: when the extractors improve (for example the loose-text fix for Workday), jobs saved earlier keep their old,
thinner text. This rebuilds the posting from its saved snapshot, extracts again, and updates only what the machine
produced. Anything you edited yourself (provenance `manual`) is never touched. Jobs imported from Notion have no
snapshot and are skipped.
"""
from __future__ import annotations

import json
from typing import Any

from . import repo
from .adapters import registry
from .extract.job import extract, missing_fields
from .extract.pasted import parse_pasted
from .models import ExtractedJob, Posting

# fields the text produces (title, company, location, level and salary are left alone)
TEXT_FIELDS = ("key_responsibilities", "requirements", "experience_required", "team_domain")


def rebuild_posting(job: dict, snap: dict) -> tuple[Posting | None, str]:
    """The Posting this job was first built from, or (None, why it cannot be rebuilt)."""
    from .pipeline import _apply_confirmed, _with_site  # lazy: pipeline imports a lot

    how, raw = str(snap.get("how") or ""), snap.get("raw")
    if not isinstance(raw, dict) and how not in ("manual", "page", "company_site"):
        return None, "no saved response"
    confirmed = {"company": job["company"], "role": job["role"]}

    if how in ("manual", "page", "company_site"):
        text = (raw or {}).get("text")
        if not text:
            return None, "no saved text"
        page = parse_pasted(text, link=raw.get("link"), hints=raw.get("hints"), overrides=confirmed)
        if how != "company_site":
            return page.posting, ""
        site_url = (raw.get("origin") or {}).get("url") or job["job_link"]
        site, why = _from_adapter(job["ats"], raw.get("site"), site_url)
        if not site:
            return None, f"company page not rebuildable ({why})"
        posting = _with_site(site, page, site_url)
        _apply_confirmed(posting, confirmed)
        return posting, ""

    return _from_adapter(how.split("->")[0].strip(), raw, job["job_link"])


def _from_adapter(ats: str, raw: Any, url: str) -> tuple[Posting | None, str]:
    if not isinstance(raw, dict):
        return None, "no saved response"
    if ats in ("html", "icims", "manual"):
        return None, "the page HTML was not saved"  # only a summary of generic pages is kept
    resolved = registry.resolve_static(url)
    if resolved is None:
        return None, "the address no longer maps to a known board"
    adapter, board = resolved.adapter, resolved.target.board
    try:
        if ats == "workday":
            return adapter.to_posting(raw, board, url), ""
        if ats == "greenhouse":
            return adapter.to_posting(raw, board.slug, board.company), ""
        if ats == "amazon":
            return adapter.to_posting(raw), ""
        if ats in ("lever", "ashby", "smartrecruiters", "eightfold", "jibe", "oracle"):
            return adapter.to_posting(raw, board), ""
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        return None, f"saved response did not fit the {ats} reader ({type(e).__name__})"
    return None, f"no rebuild for {ats}"


def _sections_of(ex: ExtractedJob) -> list[tuple[str, str, str]]:
    return [(s.kind, s.heading, "\n".join(f"- {b}" for b in s.bullets) if s.bullets else s.text) for s in ex.sections]


def plan_one(job: dict, ex: ExtractedJob) -> dict[str, Any]:
    """What would change for this job: only machine-made values that differ, never ones you edited."""
    manual = {f for f, m in job["provenance"].items() if m == "manual"}
    updates: dict[str, Any] = {}
    for f in TEXT_FIELDS:
        new, old = ex.get(f), job.get(f)
        if f in manual or (new or None) == (old or None) or new is None:
            continue
        updates[f] = new
        if f == "experience_required":
            updates["experience_min_years"], updates["experience_max_years"] = ex.get("experience_min_years"), ex.get("experience_max_years")
    sections = _sections_of(ex)
    techs = dict(ex.technologies)
    return {
        "updates": updates,
        "sections": sections if sections != job["sections"] else None,
        "technologies": techs if techs != job["technologies"] else None,
    }


def reparse_jobs(c, *, dry_run: bool = True, job_id: str | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for job in repo.reparse_candidates(c, job_id):
        item = {"id": job["id"], "role": job["role"], "company": job["company"], "ats": job["ats"], "status": "unchanged",
                "reason": "", "changes": []}
        out.append(item)
        try:
            snap = json.loads(job["snapshot"]) if job["snapshot"] else None
        except json.JSONDecodeError:
            snap = None
        if not snap:
            item["status"], item["reason"] = "skipped", "no saved snapshot"
            continue
        posting, why = rebuild_posting(job, snap)
        if posting is None:
            item["status"], item["reason"] = "skipped", why
            continue
        ex = extract(posting)
        plan = plan_one(job, ex)
        for f, v in plan["updates"].items():
            if f in TEXT_FIELDS:
                item["changes"].append(f"{f}: {len(job.get(f) or '')} -> {len(str(v))} chars")
        if plan["sections"] is not None:
            item["changes"].append(f"sections: {len(job['sections'])} -> {len(plan['sections'])}")
        if plan["technologies"] is not None:
            old, new = set(job["technologies"]), set(plan["technologies"])
            item["changes"].append(f"technologies: +{sorted(new - old)} -{sorted(old - new)}")
        if not item["changes"]:
            continue
        item["status"] = "would update" if dry_run else "updated"
        if not dry_run:
            clear_review = job["needs_review"] and not any(
                m in missing_fields(ex) for m in ("role", "company", "key_responsibilities", "requirements"))
            repo.apply_reparse(c, job["id"], plan, ex, clear_review=clear_review)
    return out
