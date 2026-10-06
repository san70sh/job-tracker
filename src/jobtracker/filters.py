"""Board filter terms. One reading of a term, used by the watcher, the preview and the Workday office filter.

A term is plain words (matched as whole words, any case); wrap it in slashes for a regular expression: /java|kotlin/.
A location term that names a known city also matches that city's other spellings (Bengaluru ~ Bangalore).
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from functools import lru_cache

from .extract.vocab import load_vocab

_WORD_EDGE = ("(?<![A-Za-z0-9])", "(?![A-Za-z0-9])")


def is_regex(term: str) -> bool:
    t = term.strip()
    return len(t) > 2 and t.startswith("/") and t.endswith("/")


def _spellings(term: str) -> list[str]:
    """[term], or every spelling of the city the term names."""
    low = term.strip().lower()
    for canonical, aliases in load_vocab().raw.get("cities", {}).items():
        if low == canonical.lower() or low in aliases:
            return [canonical, *aliases]
    return [term.strip()]


@lru_cache(maxsize=512)
def compile_term(term: str, location: bool = False) -> re.Pattern[str]:
    t = term.strip()
    if is_regex(t):
        return re.compile(t[1:-1], re.I)
    words = _spellings(t) if location else [t]
    return re.compile(_WORD_EDGE[0] + "(?:" + "|".join(re.escape(w) for w in words) + ")" + _WORD_EDGE[1], re.I)


def validate(terms: Iterable[str]) -> None:
    """Raise ValueError naming the first term that is empty or not a valid expression."""
    for t in terms:
        if not t.strip():
            raise ValueError("a filter cannot be empty")
        try:
            compile_term(t)
        except re.error as e:
            raise ValueError(f"{t!r} is not a valid pattern: {e}") from e


def any_match(text: str | None, terms: Iterable[str], location: bool = False) -> bool:
    return any(compile_term(t, location).search(text or "") for t in terms)


def passes(title: str, place: str | None, include: list[str], exclude: list[str], locations: list[str]) -> bool:
    """A posting is kept when it matches some title term (if any are set), no exclusion, and some location term."""
    if include and not any_match(title, include):
        return False
    if exclude and any_match(title, exclude):
        return False
    return not locations or any_match(place, locations, location=True)
