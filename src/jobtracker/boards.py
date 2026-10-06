"""Editing a watched board, and previewing what its filters would keep. The rules live here; SQL is in repo.py."""
from __future__ import annotations

from typing import Any

import psycopg

from . import board_options, filters, repo, watcher
from .http import Fetcher

SAMPLE = 5  # titles shown in a preview
FILTER_FIELDS = ("title_include", "title_exclude", "location_include")


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
    if any(f in columns for f in FILTER_FIELDS):
        repo.refilter_pending(c, board_id)
    return changed


async def preview(row: dict, f: Fetcher | None = None) -> dict[str, Any]:
    """What a board would keep right now with the filters in `row`, without saving anything."""
    own = f is None
    f = f or Fetcher()
    try:
        listed, effective = await watcher.fetch_listing(row, f)
    finally:
        if own:
            await f.aclose()
    kept = [p for p in listed if filters.passes(p.title, p.location, effective["title_include"], effective["title_exclude"],
                                                 effective["location_include"])]
    return {"listed": len(listed), "matched": len(kept), "complete": getattr(listed, "complete", True),
            "narrowed_at_source": effective is not row, "sample": [{"title": p.title, "location": p.location} for p in kept[:SAMPLE]]}
