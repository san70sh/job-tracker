"""Split a description into typed sections (responsibilities / requirements / nice-to-have / ...)."""
from __future__ import annotations

import re

from ..models import Section, SectionKind
from .tech import tag_text
from .text import _BULLET, Block, html_to_blocks
from .vocab import Vocab

# Order matters: the first matching rule wins, so specific ("preferred qualifications") precede general.
_RULES: list[tuple[SectionKind, re.Pattern[str]]] = [
    # footer material that must never read as a requirement ("Suggested Skills" is a job board's own tag list)
    ("logistics", re.compile(r"suggested skills|^additional information$|disability|e-verify|data privacy|equal (employment )?opportunity|"
                             r"corporate security|inclusion|belonging|"
                             # "Label:" lines that open or close many postings; the value under them is data, not text
                             r"^(company|job title|position title|closing date|job posting (closing|end|start) date|posted( on| date)?|"
                             r"requisition (id|number)|time type|worker type|job family|business unit)\s*:?$", re.I)),
    # intro headings written in a company's own voice that contain "you will" without being duties ("The Community You Will Join")
    ("role", re.compile(r"(community|team) you(?:'ll| will) (?:be )?join|difference you(?:'ll| will) make", re.I)),
    ("nice_to_have", re.compile(
        r"nice[- ]to[- ]have|good[- ]to[- ]have|preferred|bonus|desired|a plus|added plus|extra credit|"
        r"tie[- ]?breaker|great if you|even better|ideal(ly)?|what (would|will) (make|set) you stand out|"
        r"it'?s a plus|is a plus|plus points?|what we value|stand out|ways to stand", re.I)),
    ("benefits", re.compile(r"benefits|perks|what we offer|total rewards|why (join|work)|compensation|pay (range|transparency)|salary|take care of you", re.I)),
    ("requirements", re.compile(
        r"requirements?|qualifications?|what you'?ll bring|what you bring|what we'?re looking for|who you are|"
        r"you have|you'?ll need|must[- ]have|minimum|basic qualifications|skills|experience|about you|"
        r"technical skills|what you need|you should have|to be successful|your background|looking for|"
        r"what we (require|need)|need to see|tech(nology)? stack|must have|you bring|your (talent|profile|skills)|all about you|"
        r"what (you'?ll|you will) need|essential|expertise", re.I)),
    ("responsibilities", re.compile(
        r"responsibilit|what you'?ll (do|be doing)|what you will (do|be doing)|day[- ]to[- ]day|typical day|day in the life|duties|"
        r"in this role|your impact|how you'?ll (make|have)|you will|you'?ll (own|build|work|lead)|"
        r"key (areas|accountabilities|deliverables)|the work|what the job involves|your mission|what you'?ll own", re.I)),
    ("role", re.compile(r"^about the (role|job|position|opportunity)\b", re.I)),  # before the generic "about X" rule
    ("about_company", re.compile(
        r"about (us|the company|the team|our company)|about [A-Z][\w&. -]{1,40}$|who we are|^who is\b|company (overview|description|introduction)|"
        r"our (mission|story|company|culture|values)|overview of|life at|why [A-Z]", re.I)),
    ("role", re.compile(r"about the role|the role|role overview|position (summary|overview)|job (summary|overview|description)|summary|"
                        r"the opportunity|your role|the position|^overview$", re.I)),
    ("logistics", re.compile(r"location|logistics|work (arrangement|mode|model|environment)|equal opportunity|eeo|diversity|accommodation|"
                             r"additional information|how to apply|hiring process|travel|privacy|disclaimer|job (details|id)|employment type", re.I)),
]


def classify_heading(heading: str) -> SectionKind:
    h = heading.strip().replace("’", "'").replace("‘", "'")
    for kind, rx in _RULES:
        if rx.search(h):
            return kind
    return "other"


def looks_like_heading(line: str, vocab: Vocab | None = None) -> bool:
    """Is this plain line (no markup to tell us) a section heading? Used for pasted text and for loose text between
    HTML tags. Cautious on purpose: a heading is a short line with no sentence punctuation that either ends in a
    colon, is ALL CAPS, or is a known heading phrase."""
    t = line.strip()
    if _BULLET.match(t) or len(t) > 70 or t.endswith((".", ";", ",")):
        return False
    if t.endswith(":"):
        return True
    words = t.split()
    if t.isupper() and len(t) >= 4 and len(words) <= 6 and re.search(r"[A-Z]{3}", t):
        return True
    if len(words) <= 5 and classify_heading(t) != "other":
        # "Experience with Java" is a requirement line, not the heading "Experience"
        return not (re.search(r"\d|\byears?\b", t, re.I) or tag_text(t, vocab))
    return False


def split_sections(html_or_text: str) -> list[Section]:
    """Heading-driven split. Blocks before the first recognised heading become a 'role' section."""
    blocks = html_to_blocks(html_or_text)
    return sections_from_blocks(blocks)


def sections_from_blocks(blocks: list[Block]) -> list[Section]:
    sections: list[Section] = []
    current: Section | None = None
    pending: list[str] = []

    def flush() -> None:
        nonlocal current, pending
        if current is not None:
            current.text = "\n".join(f"• {b}" if b in current.bullets else b for b in pending) if pending else current.text
        current, pending = None, []

    for b in blocks:
        if b.kind == "heading":
            kind = classify_heading(b.text)
            # consecutive headings of the same kind (e.g. "Requirements" then "Required skills") merge
            if current is not None and current.kind == kind and not current.bullets and not pending:
                current.heading += f" / {b.text}"
                continue
            flush()
            current = Section(kind=kind, heading=b.text)
            sections.append(current)
            continue
        if current is None:
            current = Section(kind="role", heading="Overview")
            sections.append(current)
        pending.append(b.text)
        if b.kind == "li":
            current.bullets.append(b.text)
    flush()
    for s in sections:
        if not s.text:
            s.text = "\n".join(s.bullets)
    sections = [s for s in sections if s.text.strip() or s.bullets]
    _positional_fallback(sections)
    return sections


def _positional_fallback(sections: list[Section]) -> None:
    """Many postings title the duties list with the job name ("Backend Engineer (Platform)").
    If nothing was classified as responsibilities, the first bulleted unclassified section that
    comes after the overview and before requirements is the duties list."""
    if any(s.kind == "responsibilities" for s in sections):
        return
    seen_role = False
    for s in sections:
        if s.kind in ("role", "about_company"):
            seen_role = True
        elif s.kind in ("requirements", "nice_to_have"):
            return
        elif s.kind == "other" and seen_role and len(s.bullets) >= 3:
            s.kind = "responsibilities"
            return


def bullets_of(section_text: str) -> list[str]:
    """Items of a section: real bullets if present, else sentences."""
    lines = [ln.strip() for ln in section_text.split("\n") if ln.strip()]
    items = [re.sub(r"^•\s*", "", ln) for ln in lines if ln.startswith("•")]
    if items:
        return items
    if len(lines) > 1:
        return lines
    return split_sentences(section_text)


_ABBREV = {"sr", "jr", "inc", "ltd", "co", "corp", "vs", "etc", "e.g", "i.e", "dr", "mr", "ms", "st", "no", "approx", "u.s", "b.s", "m.s", "ph.d"}


def split_sentences(text: str) -> list[str]:
    """Sentence split that does not break on 'Sr.', 'e.g.', 'Inc.' etc."""
    out: list[str] = []
    start = 0
    for m in re.finditer(r"[.;!?]\s+(?=[A-Z0-9])", text):
        before = text[start:m.start()]
        last = re.findall(r"[\w.]+$", before)
        if last and last[0].lower().rstrip(".") in _ABBREV and text[m.start()] == ".":
            continue
        out.append(text[start:m.end()].strip())
        start = m.end()
    tail = text[start:].strip()
    if tail:
        out.append(tail)
    return [s for s in out if s]
