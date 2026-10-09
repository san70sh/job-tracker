"""Options for the Company boards page, read from config/boards.json (no lists hardcoded in code or in JavaScript)."""
from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from .config import ROOT

PATH = ROOT / "config" / "boards.json"


@lru_cache
def load() -> dict[str, Any]:
    return json.loads(PATH.read_text(encoding="utf-8"))


def poll_intervals() -> list[dict[str, Any]]:
    return load()["poll_intervals"]


def allowed_minutes() -> set[int]:
    return {i["minutes"] for i in poll_intervals()}


def country_names() -> dict[str, str]:
    """ISO country code -> name, for systems that write places as codes (iCIMS: IN-KA-Bengaluru)."""
    return load()["country_names"]


def max_age_days() -> int:
    """How old a posting may be and still be announced."""
    return load()["max_age_days"]


def page_settings() -> dict[str, Any]:
    """What the Company boards page needs besides the intervals."""
    return {"stale_after_hours": load()["stale_after_hours"], "ats_colours": load()["ats_colours"],
            "max_age_days": max_age_days()}


def suggestion_settings() -> dict[str, Any]:
    return load()["suggestions"]
