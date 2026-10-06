"""Controlled vocabulary (technologies + aliases + level rules) from config/vocab.json."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache

from ..config import ROOT

VOCAB_PATH = ROOT / "config" / "vocab.json"


@dataclass
class Tech:
    name: str
    category: str
    patterns: list[re.Pattern[str]] = field(default_factory=list)


@dataclass
class LevelRule:
    regex: re.Pattern[str]
    level: str
    priority: int


@dataclass
class Vocab:
    techs: list[Tech]
    level_rules: list[LevelRule]
    raw: dict


def _compile_tech(t: dict) -> Tech:
    pats: list[re.Pattern[str]] = []
    if t.get("match_name", True):
        flags = 0 if t.get("name_case_sensitive") else re.I
        name = t["name"]
        # \b does not work next to '.', '#', '+' — use look-arounds for names that start/end with symbols.
        pats.append(re.compile(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", flags))
    for a in t.get("aliases", []):
        flags = 0 if a.get("case_sensitive") else re.I
        pat = a["pattern"] if a.get("regex") else re.escape(a["pattern"])
        if not a.get("regex"):
            pat = rf"(?<![A-Za-z0-9]){pat}(?![A-Za-z0-9])"
        else:
            pat = rf"(?:{pat})"
        pats.append(re.compile(pat, flags))
    return Tech(t["name"], t["category"], pats)


@lru_cache
def load_vocab() -> Vocab:
    raw = json.loads(VOCAB_PATH.read_text(encoding="utf-8"))
    techs = [_compile_tech(t) for t in raw["technologies"]]
    rules = sorted(
        (LevelRule(re.compile(r["regex"]), r["level"], r["priority"]) for r in raw["level_rules"]),
        key=lambda r: r.priority,
    )
    return Vocab(techs, rules, raw)
