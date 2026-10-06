"""Rule-based small facts: experience, work mode, level, requisition id."""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..models import WorkMode
from .vocab import Vocab, load_vocab

# ───────────────────────────── experience ─────────────────────────────
_RANGE = re.compile(r"(\d{1,2})\s*(?:\+|plus)?\s*(?:-|–|—|to)\s*(\d{1,2})\s*\+?\s*(?:years?|yrs?)", re.I)
_LABELLED_RANGE = re.compile(r"(?:years of experience|experience)\s*(?:required)?[:\-]\s*(\d{1,2})\s*(?:\+|plus)?\s*(?:-|–|—|to)\s*(\d{1,2})", re.I)
_SINGLE = re.compile(
    r"(?:(?:at least|minimum(?: of)?|min\.?|over|more than)\s+)?(\d{1,2})\s*(\+|plus)?\s*(?:years?|yrs?)(?:'|’)?\s*(?:of\s+)?",
    re.I,
)
_EXP_CONTEXT = re.compile(r"experience|engineer|develop|software|industry|backend|back-end|building|designing|working|professional", re.I)


@dataclass
class Experience:
    text: str  # sentence snippet, e.g. "5+ years software engineering, owning backend systems in production"
    min_years: float
    max_years: float | None


def parse_experience(text: str, require_context: bool = True) -> Experience | None:
    """Pick the headline experience requirement: first year-count that sits in an experience-ish context.

    `require_context=False` is for text already known to be an experience field ("3-5 years"), where there is
    no surrounding sentence to confirm it."""
    candidates: list[tuple[int, re.Match[str], bool]] = []
    for m in _RANGE.finditer(text):
        candidates.append((m.start(), m, True))
    for m in _LABELLED_RANGE.finditer(text):  # "Years of experience: 6 to 8"
        candidates.append((m.start(), m, True))
    for m in _SINGLE.finditer(text):
        if any(abs(m.start() - s) < 4 for s, _, _ in candidates):  # already covered by a range
            continue
        candidates.append((m.start(), m, False))
    candidates.sort(key=lambda c: c[0])
    for _, m, is_range in candidates:
        window = text[max(0, m.start() - 60): m.end() + 90]
        if require_context and not _EXP_CONTEXT.search(window):
            continue
        # ignore things like "10 years of age" / company age
        if re.search(r"years?\s+(old|ago|of age)", window, re.I) or re.search(r"(founded|for over|for more than)\s+\d+\s+years", window, re.I):
            continue
        snippet = _snippet(text, m.start())
        if is_range:
            return Experience(snippet, float(m.group(1)), float(m.group(2)))
        n = float(m.group(1))
        plus = bool(m.group(2)) or bool(re.search(r"at least|minimum|min\.|over|more than", m.group(0), re.I))
        return Experience(snippet, n, None if plus else n)
    return None


def _snippet(text: str, pos: int) -> str:
    start = max(text.rfind("\n", 0, pos), text.rfind(". ", 0, pos))
    start = 0 if start < 0 else start + 1
    end_candidates = [i for i in (text.find("\n", pos), text.find(". ", pos)) if i >= 0]
    end = min(end_candidates) if end_candidates else len(text)
    s = re.sub(r"^[•\s]+", "", text[start:end]).strip()
    return s[:160]


def format_experience(e: Experience) -> str:
    if e.max_years is None:
        head = f"{e.min_years:g}+ years"
    elif e.max_years == e.min_years:
        head = f"{e.min_years:g} years"
    else:
        head = f"{e.min_years:g} to {e.max_years:g} years"
    return head


# ───────────────────────────── work mode ─────────────────────────────
_HYBRID = re.compile(
    r"\bhybrid\b|\b\d\s*(?:-\s*\d\s*)?days?\s*(?:a|per|each|/)\s*week[^.\n]{0,60}(?:office|on-?site|in person)|"
    r"(?:office|on-?site|in person)[^.\n]{0,60}\b\d\s*(?:-\s*\d\s*)?days?\s*(?:a|per|each|/)\s*week|"
    r"\b(?:two|three|2|3)\s+days[^.\n]{0,40}(?:office|on-?site)", re.I)
_ONSITE = re.compile(
    r"required in office|in[- ]office|on-?site|work from (?:the )?office|office[- ]based|"
    r"(?:must|required to) (?:work|be) (?:in|from) (?:the )?office|in[- ]person", re.I)
_REMOTE = re.compile(
    r"fully remote|100% remote|remote[- ]first|work from (?:home|anywhere)|\(remote\)|[-–—,]\s*remote\b|\bremote\s*(?:[-–—(,]|$)|"
    r"this is a remote|remote (?:position|role|job|opportunity)|location:\s*remote", re.I)

_ATS_MODE = {
    "remote": "Remote", "telecommute": "Remote", "fully remote": "Remote", "work from home": "Remote",
    "hybrid": "Hybrid",
    "onsite": "Onsite", "on-site": "Onsite", "on site": "Onsite", "office": "Onsite", "in office": "Onsite",
}


def map_ats_mode(value: str | None) -> WorkMode | None:
    if not value:
        return None
    return _ATS_MODE.get(value.strip().lower())  # type: ignore[return-value]


def detect_work_mode(title: str, location: str | None, text: str) -> tuple[WorkMode | None, str | None]:
    head = f"{title} {location or ''}"
    if _REMOTE.search(head) or re.search(r"\bremote\b", location or "", re.I):
        if not _HYBRID.search(text):
            return "Remote", "location mentions remote"
    m = _HYBRID.search(text)
    if m:
        return "Hybrid", m.group(0)
    m = _ONSITE.search(text)
    if m:
        return "Onsite", m.group(0)
    m = _REMOTE.search(text)
    if m:
        return "Remote", m.group(0)
    return None, None


# ───────────────────────────── level ─────────────────────────────
_LEVEL_TOKEN = re.compile(r"\b(IC\s?\d{1,2}|L\d{1,2}|SDE[- ]?\d|E\d{1,2}|Level\s?\d{1,2}|Grade\s?\d{1,2}|P\d{1,2}|T\d{1,2}|MTS\s?\d?|Band\s?\w{1,2})\b")


def normalize_level(title: str, vocab: Vocab | None = None) -> str | None:
    vocab = vocab or load_vocab()
    for rule in vocab.level_rules:
        if rule.regex.search(title):
            return rule.level
    return None


def level_display(title: str, level_norm: str | None, text: str = "") -> str | None:
    """Normalised ladder plus any explicit company level token found near the top of the posting."""
    token = None
    for hay in (title, text[:700]):
        m = _LEVEL_TOKEN.search(hay)
        if m:
            token = re.sub(r"\s+", "", m.group(1)) if m.group(1)[0] in "IL" else m.group(1)
            break
    if level_norm and token:
        return f"{level_norm} ({token})"
    return level_norm or token


_REQ_ID = re.compile(
    r"(?:job|req(?:uisition)?|requisition|position|posting)\s*(?:id|number|no\.?|#)\s*[:#]?\s*([A-Z]{0,4}[-_]?\d[\w-]{2,20})", re.I)


def find_req_id(text: str) -> str | None:
    m = _REQ_ID.search(text)
    return m.group(1) if m else None


# ───────────────────────────── location tags (for the board filter) ─────────────────────────────
_PAREN = re.compile(r"\([^)]*\)")
_DASH = re.compile(r"\s[—–-]\s")  # spaced em dash, en dash or hyphen: "India - city not shown ..."
_REMOTE = re.compile(r"\bremote\b|work from home|\bwfh\b", re.I)
# Words that mean "this part is guessing / explaining", so cities mentioned there are not the posting's location.
_UNSURE = re.compile(r"confirm|likely|not (?:shown|stated|confirmed)|city not|hubs?\b|unclear|tbd|multiple", re.I)
_city_patterns: list[tuple[str, re.Pattern[str]]] | None = None


def _cities_in(text: str, vocab: Vocab) -> list[str]:
    global _city_patterns
    if _city_patterns is None or vocab is not load_vocab():
        table = vocab.raw.get("cities", {})
        _city_patterns = [(name, re.compile(r"\b(?:" + "|".join(re.escape(a) for a in aliases) + r")\b", re.I))
                          for name, aliases in table.items()]
    hits = [(m.start(), name) for name, rx in _city_patterns if (m := rx.search(text))]
    return [name for _, name in sorted(hits)]

# the two tags that are not a place: used by the board filter and by the filter suggestions
TAG_REMOTE = "Remote"
TAG_UNSPECIFIED = "Unspecified"


def location_tags(location: str | None, vocab: Vocab | None = None) -> list[str]:
    """Clean filter tags from a free-text location: canonical city names, plus 'Remote'. 'Unspecified' when the
    text names no confirmed city (for example "India - city not shown on the posting (Pune, Chennai; confirm)").

    Parenthetical remarks are ignored, and only the part before a spaced dash is trusted for cities; the part after
    it is used only when it does not sound uncertain."""
    vocab = vocab or load_vocab()
    if not location or not location.strip():
        return [TAG_UNSPECIFIED]
    text = _PAREN.sub(" ", location)
    parts = _DASH.split(text, maxsplit=1)
    head, tail = parts[0], (parts[1] if len(parts) > 1 else "")
    tags = _cities_in(head, vocab)
    if not tags and tail and not _UNSURE.search(tail):
        tags = _cities_in(tail, vocab)
    if _REMOTE.search(text):
        tags.append(TAG_REMOTE)
    return tags or [TAG_UNSPECIFIED]
