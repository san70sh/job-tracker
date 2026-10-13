"""Editing a watched board, and previewing what its filters would keep. The rules live here; SQL is in repo.py."""
from __future__ import annotations

from typing import Any

import psycopg

from . import board_options, experience, filters, repo, watcher
from .http import Fetcher
from .models import EXP_DEFERRED, EXP_READ, EXP_UNSTATED

SAMPLE = 5  # titles shown in a preview
FILTER_FIELDS = ("title_include", "title_exclude", "location_include")
EXPERIENCE_FIELDS = ("experience_min", "experience_max")
MAX_YEARS = 60  # a typo guard, not a rule about careers


def clean_range(low: int | None, high: int | None) -> tuple[int | None, int | None]:
    """The years you want, either end optional; raises ValueError for a negative, absurd or back-to-front range."""
    for years in (low, high):
        if years is not None and not 0 <= years <= MAX_YEARS:
            raise ValueError(f"experience must be between 0 and {MAX_YEARS} years")
    if low is not None and high is not None and low > high:
        raise ValueError("the minimum experience cannot be above the maximum")
    return low, high


def clean_terms(terms: list[str]) -> list[str]:
    """Trimmed, de-duplicated (any case), in the order given; raises ValueError on an empty or invalid term."""
    seen: dict[str, str] = {}
    for t in terms:
        seen.setdefault(t.strip().lower(), t.strip())
    out = list(seen.values())
    filters.validate(out)
    return out


def update(c: psycopg.Connection, board_id: int, changes: dict[str, Any]) -> list[str]:
    """Apply an edit. Returns the names of the fields that really changed. Raises KeyError for an unknown board and
    ValueError for a bad value. Renaming the company renames it on every saved job (see repo.rename_company)."""
    rows = repo.list_boards(c, board_id=board_id)
    if not rows:
        raise KeyError(board_id)
    row, columns, changed = rows[0], {}, []
    for field in FILTER_FIELDS:
        if field in changes:
            columns[field] = clean_terms(changes[field])
    if any(k in changes for k in EXPERIENCE_FIELDS):  # either end can change on its own: the range is checked whole
        lo, hi = clean_range(*(changes.get(k, row[k]) for k in EXPERIENCE_FIELDS))
        columns.update({"experience_min": lo, "experience_max": hi})
    if "poll_interval_minutes" in changes:
        if changes["poll_interval_minutes"] not in board_options.allowed_minutes():
            raise ValueError("choose one of the listed check intervals")
        columns["poll_interval_minutes"] = changes["poll_interval_minutes"]
    if "enabled" in changes:
        columns["enabled"] = bool(changes["enabled"])
    columns = {k: v for k, v in columns.items() if v != row[k]}
    changed += list(columns)
    name = (changes.get("company") or "").strip()
    if "company" in changes:
        if not name:
            raise ValueError("company cannot be empty")
        if name != row["company"]:
            repo.rename_company(c, str(row["company_id"]), name)
            changed.append("company")
    repo.update_board(c, board_id, columns)
    if any(f in columns for f in (*FILTER_FIELDS, *EXPERIENCE_FIELDS)):
        repo.refilter_pending(c, board_id)
    return changed


async def preview(row: dict, f: Fetcher | None = None) -> dict[str, Any]:
    """What a board would keep right now with the filters in `row`, without saving anything."""
    own = f is None
    f = f or Fetcher()
    try:
        listed, effective = await watcher.fetch_listing(row, f)
        max_age = board_options.max_age_days()
        matching = [p for p in listed if filters.passes(p.title, p.location, effective["title_include"], effective["title_exclude"],
                                                         effective["location_include"])]
        kept = [p for p in matching if filters.is_recent(p.posted_at, max_age)]  # what the other filters would announce
        years = None
        if experience.wanted(effective):  # open what remains (up to the per-check limit) to see which postings fit the years
            states = await experience.annotate(effective, kept, f, known=set())
            kept = [p for p in kept if experience.fits(effective, p)]
            still_read = sum(1 for p in kept if p.experience_state == EXP_READ)
            years = {"read": states[EXP_READ] + states[EXP_UNSTATED], "not_stated": states[EXP_UNSTATED],
                     "outside": states[EXP_READ] - still_read, "not_read": states[EXP_DEFERRED]}
    finally:
        if own:
            await f.aclose()
    return {"listed": len(listed), "matched": len(matching), "recent": len(kept), "max_age_days": max_age, "experience": years,
            "complete": getattr(listed, "complete", True), "narrowed_at_source": effective is not row,
            "sample": [{"title": p.title, "location": p.location} for p in kept[:SAMPLE]]}
