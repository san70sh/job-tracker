"""A board's experience filter.

A listing does not say how many years a job asks for; the posting does. So a check opens the new postings that passed
every other filter and reads the years there (requirements first, then the whole text: extract.job.experience_of).
A posting that states nothing, or that cannot be opened, is kept: it cannot be shown to be out of range.
"""
from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Mapping

from . import board_options, filters, repo
from .adapters import registry
from .adapters.base import READ_ERRORS, Target
from .extract.job import experience_of
from .http import Fetcher
from .models import EXP_DEFERRED, EXP_READ, EXP_UNSTATED, ListedPosting

log = logging.getLogger("jobtracker.experience")


def wanted(board: Mapping) -> bool:
    """Does this board filter on experience (either end of the range is enough)?"""
    return board.get("experience_min") is not None or board.get("experience_max") is not None


def fits(board: Mapping, p: ListedPosting) -> bool:
    """False only for a posting that stated years outside the board's range."""
    return filters.experience_fits(p.experience_min, p.experience_max, board.get("experience_min"), board.get("experience_max"))


async def _read(board: Mapping, p: ListedPosting, f: Fetcher) -> None:
    """Open one posting and note the years it asks for."""
    adapter = registry.adapters()[board["ats"]]
    try:
        posting = await adapter.fetch(Target(repo.board_ref(board), p.ats_posting_id, p.url), f)
    except READ_ERRORS as e:
        log.warning("could not read the experience of %s (%s); keeping it", p.url, e)
        p.experience_state = EXP_UNSTATED
        return
    exp = experience_of(posting)
    p.experience_min, p.experience_max = (exp.min_years, exp.max_years) if exp else (None, None)
    p.experience_state = EXP_READ if exp else EXP_UNSTATED


async def annotate(board: Mapping, listed: list[ListedPosting], f: Fetcher, known: set[str]) -> Counter:
    """Read the experience of the postings about to be judged: the new ones (not in `known`) that pass the title,
    place and age filters. One check reads at most experience_checks_per_poll; the rest are marked EXP_DEFERRED and
    judged at a later check. Fills the postings in place; returns how many ended in each state."""
    max_age, limit = board_options.max_age_days(), board_options.experience_checks_per_poll()
    todo = [p for p in listed if p.ats_posting_id not in known and filters.board_keeps(board, p, max_age)]
    for i, p in enumerate(todo):
        if i >= limit:
            p.experience_state = EXP_DEFERRED
        else:
            await _read(board, p, f)
    return Counter(p.experience_state for p in todo)
