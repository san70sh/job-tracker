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


def page_settings() -> dict[str, Any]:
    """What the Company boards page needs besides the intervals."""
    return {"stale_after_hours": load()["stale_after_hours"], "ats_colours": load()["ats_colours"]}


def suggestion_settings() -> dict[str, Any]:
    return load()["suggestions"]
