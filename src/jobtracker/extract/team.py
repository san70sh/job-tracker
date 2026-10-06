"""Team / domain for postings whose ATS gives no department field.

Precision first: a wrong team is worse than a blank one, so nothing below 0.6 is returned. Sources, best first:
  1. an explicit label in the text            "Team: Audit & Logging"            0.85
  2. a sentence that names the team           "Join the Audit & Logging team"    0.60
  3. a title suffix that is not a qualifier   "Software Engineer - Audit & Logging" 0.60
Two sources agreeing lift the result to 0.75. A domain taken from keywords (Payments, Security...) is appended
as a hint ("Audit & Logging - Security") but is never returned on its own.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..models import Section
from .vocab import Vocab, load_vocab

MIN_CONFIDENCE = 0.6

_CAP = r"[A-Z][\w&.'+/-]*"
_NAME = rf"{_CAP}(?:\s+(?:and|of|for|&|{_CAP})){{0,4}}"
_UNIT = r"(?i:team|group|organi[sz]ation|department|division)"

_LABEL = re.compile(
    r"^\s*(?:team|department|business unit|group|organi[sz]ation|division)\s*:\s*(.{3,60}?)\s*$", re.I | re.M)
_SENTENCES = (
    re.compile(rf"(?i:join(?:ing)?|part of|within|in)\s+(?:the|our)\s+({_NAME})\s+{_UNIT}\b"),
    re.compile(rf"\bThe\s+({_NAME})\s+{_UNIT}\s+(?i:is|are|builds|owns|works|helps|develops|designs|focuses|at)\b"),
)

# words that say nothing about which team it is
_GENERIC = set("""engineering software technology tech product products development our the a an this new growing global
amazing talented core hiring recruiting it company awesome great fast dynamic world class best brilliant
innovative passionate diverse small large wider broader entire whole leadership management senior staff
""".split())
# title words that qualify the role rather than name a team
_QUALIFIERS = set("""senior sr staff principal lead junior jr intern trainee associate graduate fresher avp vp svp director
manager head ii iii iv remote hybrid onsite contract contractor permanent temporary india apac emea global
backend frontend fullstack full stack mobile web java python engineer engineers developer developers sde sdet
programmer architect consultant analyst scientist specialist administrator tester qa
careers career jobs job linkedin indeed naukri hiring opening apply
karnataka haryana maharashtra telangana tamil nadu kerala gujarat rajasthan punjab uttar pradesh bengal odisha
andhra goa bihar jharkhand assam chandigarh
""".split())
# a browser-tab title of a job-board page ("X hiring Y in Pune | LinkedIn"), not a posting title
_PAGE_TITLE = re.compile(r"\bhiring\b|\|\s*(?:linkedin|indeed|naukri)|\bjobs? (?:at|in)\b", re.I)


_SUBJECT_TAGS = {"domain", "observability", "security", "data"}


@dataclass
class TeamGuess:
    value: str
    method: str
    confidence: float
    evidence: str


def _words(s: str) -> list[str]:
    return [w for w in re.findall(r"[a-z][a-z0-9+#]*", s.lower())]


def _content_words(s: str) -> set[str]:
    return {w for w in _words(s) if w not in _GENERIC and len(w) > 2}


def _clean(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip(" -–—:,.;|")


def _plausible_name(name: str, company: str | None) -> bool:
    if not name or len(name) > 45 or not name[0].isupper():
        return False
    words = _words(name)
    if not words or len(words) > 6 or not _content_words(name):
        return False
    if company:  # "the Acme team" at Acme names the whole company
        c = _content_words(company)
        if c and _content_words(name) <= c:
            return False
    return True


def _from_label(text: str, company: str | None) -> tuple[str, str] | None:
    for m in _LABEL.finditer(text):
        name = _clean(m.group(1))
        if _plausible_name(name, company):
            return name, m.group(0).strip()
    return None


def _from_sentence(text: str, company: str | None) -> tuple[str, str] | None:
    head = text[:1500]  # intro paragraphs; deep in the text the team is usually a different one ("you will work with the X team")
    for pat in _SENTENCES:
        for m in pat.finditer(head):
            name = _clean(m.group(1))
            if _plausible_name(name, company):
                return name, m.group(0).strip()
    return None


def _is_qualifier(cand: str, vocab: Vocab) -> bool:
    if re.search(r"\d", cand):  # years, grades, "L5", "(3-5 yrs)"
        return True
    words = _words(cand)
    if not words or any(w in _QUALIFIERS for w in words):
        return True
    low = cand.lower()
    for aliases in vocab.raw.get("cities", {}).values():
        if any(a in low for a in aliases):
            return True
    # a language or tool is a qualifier; subject-matter tags (Payments, Observability, Security) can name a team
    return any(p.search(cand) for t in vocab.techs if t.category not in _SUBJECT_TAGS for p in t.patterns)


def _from_title(title: str, company: str | None, vocab: Vocab) -> tuple[str, str] | None:
    if _PAGE_TITLE.search(title):
        return None
    parens = re.findall(r"\(([^)]+)\)", title)
    rest = re.sub(r"\([^)]*\)", " ", title)
    parts = re.split(r"\s[–—-]\s|\s*\|\s*|,\s+", rest)[1:]
    for cand in [_clean(c) for c in parens + parts]:
        if re.fullmatch(r"[A-Z]{2,4}", cand):  # a bare acronym ("ITC", "CAM") may be a site or entity, not a team
            continue
        if _plausible_name(cand, company) and not _is_qualifier(cand, vocab):
            return cand, title
    return None


def domain_hint(title: str, text: str, vocab: Vocab | None = None) -> str | None:
    """Best-scoring domain from vocab.json "domains". A keyword in the title counts 3, each distinct keyword in
    the opening text counts 1; needs 4 points (a title hit plus one more, or four in the text) and a clear lead."""
    vocab = vocab or load_vocab()
    scores: dict[str, int] = {}
    for domain, words in vocab.raw.get("domains", {}).items():
        pats = [re.compile(rf"(?<![A-Za-z0-9]){re.escape(w)}(?![A-Za-z0-9])", re.I) for w in words]
        s = sum(3 for p in pats if p.search(title)) + sum(1 for p in pats if p.search(text))
        if s:
            scores[domain] = s
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    if not ranked or ranked[0][1] < 4:
        return None
    if len(ranked) > 1 and ranked[0][1] - ranked[1][1] < 2:
        return None
    return ranked[0][0]


def _intro_text(sections: list[Section], full_text: str) -> str:
    """Text to mine for hints: everything except company boilerplate, benefits and logistics."""
    keep = [s.text for s in sections if s.kind not in ("about_company", "benefits", "logistics") and s.text]
    return "\n".join(keep) if keep else full_text


def infer_team(title: str, company: str | None, sections: list[Section], full_text: str,
               vocab: Vocab | None = None) -> TeamGuess | None:
    vocab = vocab or load_vocab()
    found: list[tuple[str, str, float, str]] = []  # (name, source, confidence, evidence)
    if hit := _from_label(full_text, company):
        found.append((hit[0], "label", 0.85, hit[1]))
    if hit := _from_sentence(full_text, company):
        found.append((hit[0], "sentence", 0.6, hit[1]))
    if hit := _from_title(title, company, vocab):
        found.append((hit[0], "title", 0.6, hit[1]))
    if not found:
        return None
    best = max(found, key=lambda f: f[2])
    name, source, conf, evidence = best
    agree = [f[1] for f in found if f is not best and _content_words(f[0]) & _content_words(name)]
    if agree:
        conf = max(conf, 0.75)
        source = "+".join([source, *agree])
    value = name
    domain = domain_hint(title, _intro_text(sections, full_text)[:2500], vocab)
    if domain and not (_content_words(domain) & _content_words(name)):
        value = f"{name} — {domain}"
    return TeamGuess(value, "rules", conf, f"{source}: {evidence[:120]}")
