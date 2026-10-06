"""Board watcher: poll watched company boards, record new postings, notify the UI."""
from __future__ import annotations

import asyncio
import logging

from . import db, repo
from .adapters import registry
from .adapters.base import Unsupported
from .events import bus
from .http import FetchError, Fetcher
from .models import ListedPosting

log = logging.getLogger("jobtracker.watcher")

# what a listing can fail with that is the board's fault (as opposed to a bug here)
BOARD_ERRORS = (FetchError, KeyError, ValueError, TypeError)


async def fetch_listing(row: dict, f: Fetcher) -> tuple[list[ListedPosting], dict]:
    """List a board's postings, asking the system to filter by place when it can. Returns the listing and the board
    row to filter it with: when the place filter was applied at the source it is dropped from the row, because the
    posting's own place text (Workday says "2 Locations") would wrongly reject what the source already matched."""
    adapter = registry.adapters()[row["ats"]]
    board, at_source = await adapter.resolve_location_filter(repo.board_ref(row), row["location_include"], f)
    listed = await adapter.list_postings(board, f)
    return listed, ({**row, "location_include": []} if at_source else row)


def _record(fn, *args):
    with db.conn() as c:
        return fn(c, *args)


async def poll_board(row: dict, f: Fetcher) -> list[dict]:
    try:
        listed, effective = await fetch_listing(row, f)
    except (Unsupported, *BOARD_ERRORS) as e:
        log.error("board %s (%s) poll failed: %s", row["id"], row["company"], e)
        await asyncio.to_thread(_record, repo.record_poll_failure, row["id"], str(e))
        if not isinstance(e, Unsupported):
            bus.publish("poll_error", board=row["company"], error=str(e))
        return []

    new = await asyncio.to_thread(_record, repo.record_poll, effective, listed)
    log.info("polled %s: %d listed, %d new", row["company"], len(listed), len(new))
    if new:
        total = await asyncio.to_thread(_record, repo.count_new_discovered)
        bus.publish("new_postings", count=len(new), total_new=total, postings=new[:5], company=row["company"])
    return new


async def poll_due(f: Fetcher | None = None, force: bool = False) -> int:
    own = f is None
    f = f or Fetcher()
    try:
        rows = await asyncio.to_thread(_record, repo.list_boards, not force)
        n = 0
        for row in rows:
            n += len(await poll_board(row, f))
        return n
    finally:
        if own:
            await f.aclose()


async def poll_one(board_id: int) -> list[dict]:
    """Check one board now, whatever its interval or whether it is paused."""
    rows = await asyncio.to_thread(_record, lambda c, i: repo.list_boards(c, board_id=i), board_id)
    if not rows:
        raise KeyError(board_id)
    async with Fetcher() as f:
        return await poll_board(rows[0], f)
