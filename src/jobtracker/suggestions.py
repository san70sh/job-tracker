"""Filter suggestions for the board form, drawn from the jobs you applied to. Pure functions: rows in, lists out."""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

from .board_options import suggestion_settings
from .extract.facts import TAG_REMOTE, TAG_UNSPECIFIED, location_tags

_WORD = re.compile(r"[a-z][a-z+#.]*")
_BRACKETS = re.compile(r"\(.*?\)")


def _words(title: str, stop: set[str]) -> list[str]:
    return [w for w in _WORD.findall(_BRACKETS.sub(" ", title.lower())) if w not in stop and len(w) > 1]


def _locations(rows: list[dict]) -> Counter:
    """Places you applied to. Remote and unknown are not places a board can filter on, so they are left out."""
    skip = {TAG_REMOTE, TAG_UNSPECIFIED}
    return Counter(t for r in rows for t in location_tags(r.get("location")) if t not in skip)


def _title_terms(rows: list[dict], cfg: dict[str, Any]) -> list[tuple[str, int]]:
    """Words and two-word phrases found in some of your titles but not nearly all (the word "engineer" would match
    everything and filter nothing). Most common first; a term overlapping a better one is dropped."""
    stop = set(cfg["stop_words"])
    docs = [_words(r["role"], stop) for r in rows]
    freq: Counter = Counter()
    for w in docs:
        freq.update(set(w) | {" ".join(p) for p in zip(w, w[1:])})
    n = len(docs)
    kept: list[tuple[str, int]] = []
    for term, df in freq.most_common():
        if df < cfg["min_titles"] or df / n > cfg["max_share"]:
            continue
        if any(term in k or k in term for k, _ in kept):
            continue
        kept.append((term, df))
    return kept


def _excluded(rows: list[dict], cfg: dict[str, Any]) -> list[str]:
    """Words that never appear in a title you applied to: what you apply for shows what to leave out."""
    seen = {w for r in rows for w in _words(r["role"], set())}
    return [w for w in cfg["exclude_candidates"] if w not in seen]


def _split(items: list, top: int, more: int) -> dict[str, list]:
    return {"top": items[:top], "more": items[top:top + more]}


def suggest(rows: list[dict]) -> dict[str, dict[str, list]]:
    """{field: {top: [...], more: [...]}}. Empty lists when there is nothing to learn from (the form then takes any text)."""
    cfg = suggestion_settings()
    top, more = cfg["count"], cfg["more"]
    if not rows:
        return {k: _split([], top, more) for k in ("title_include", "title_exclude", "location_include")}
    as_option = lambda pairs: [{"value": v, "count": n} for v, n in pairs]  # noqa: E731
    return {
        "title_include": _split(as_option(_title_terms(rows, cfg)), top, more),
        "title_exclude": _split([{"value": w, "count": None} for w in _excluded(rows, cfg)], top, more),
        "location_include": _split(as_option(_locations(rows).most_common()), top, more),
    }
