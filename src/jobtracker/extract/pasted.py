"""Pasted posting text (LinkedIn, Indeed, an email, a PDF copy) -> a Posting the normal extractors can read.

The ATS adapters get title, company and location as separate fields; pasted text has none, so they are detected here.
Each detection carries a confidence: below 0.6 the preview highlights it for you to confirm. Headings are guessed
(plain text has no markup), the text is turned into escaped HTML, and from there `extract()` does the rest.
"""
from __future__ import annotations

import hashlib
import html as htmllib
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

from ..models import Posting, WorkMode
from ..urls import canonicalize
from .sections import looks_like_heading
from .vocab import Vocab, load_vocab

WEAK = 0.6  # below this the preview asks you to check the value

_SEP = re.compile(r"\s*[·•|]\s*")
_BULLET = re.compile(r"^\s*(?:[-•*▪●◦·–]|\d+[.)])\s+")
_URL = re.compile(r"https?://[^\s<>\"')]+", re.I)
_AGE = re.compile(r"\b\d+\s*(?:minute|hour|day|week|month)s?\s+ago\b|\b(?:reposted|posted)\b", re.I)
_APPLICANTS = re.compile(r"\b(?:over\s+)?\d[\d,]*\+?\s+(?:applicants?|clicks?)\b|early applicant", re.I)
_ROLE_WORD = re.compile(
    r"\b(?:engineer|developer|sde|sdet|architect|manager|lead|scientist|analyst|consultant|programmer|devops|sre|"
    r"administrator|designer|specialist|intern|director|head of|principal|staff)\b", re.I)
_MODE = re.compile(r"\(\s*(on-?site|hybrid|remote)\s*\)", re.I)
_NOISE = re.compile(
    r"^(show (more|less)|see (more|less)|report( this)? job|easy apply|apply( now)?|save|saved|promoted( by hirer)?|"
    r"responses managed off linkedin|meet the hiring team|see how you compare.*|get ai-powered.*|try premium.*|"
    r"reactivate premium.*|set alert|share|actively (reviewing|recruiting).*|matches your job preferences.*|"
    r"job description|\d+ (connections?|school alumni).*)$", re.I)
# A whole LinkedIn page (not just the description) has page chrome around the posting. "About the job" marks where
# the posting starts; these lines mark where it ends. Only used when that marker is present, because headings such
# as "About the company" are legitimate inside a normal description.
_ABOUT_JOB = re.compile(r"^about the job$", re.I)
_END_OF_POSTING = re.compile(
    r"^(set alert for similar jobs|put your best foot forward.*|see how you compare.*|exclusive job seeker insights.*|"
    r"show premium insights|meet the hiring team|people you can reach out to|applicants for this job)$", re.I)
_CHIP = re.compile(r"^(on-?site|hybrid|remote|full-time|part-time|contract|temporary|internship|volunteer)$", re.I)
_LABELS = {
    "company": re.compile(r"^(?:company|employer|organi[sz]ation|hiring company)\s*[:\-–]\s*(.{2,60})$", re.I),
    "title": re.compile(r"^(?:job title|title|position|role|designation)\s*[:\-–]\s*(.{3,100})$", re.I),
    "location": re.compile(r"^(?:work |job )?location\s*[:\-–]\s*(.{2,80})$", re.I),
}
_IS_HIRING = re.compile(r"^([A-Z][\w&.,' -]{1,50}?)\s+is\s+(?:hiring|looking for|seeking|recruiting)\b")
_ABOUT = re.compile(r"^About\s+(?!the\b|us\b|this\b|you\b|our\b|your\b|these\b|them\b)([A-Z][\w&.' -]{1,40})$")
_BAD_COMPANY = re.compile(r"^(?:our|the|a|an|us|this|you|your|team|we)\b", re.I)


@dataclass
class Detected:
    value: str | None = None
    confidence: float = 0.0
    how: str = ""


@dataclass
class Parsed:
    posting: Posting
    company: Detected = field(default_factory=Detected)
    title: Detected = field(default_factory=Detected)
    location: Detected = field(default_factory=Detected)
    link: str | None = None
    warnings: list[str] = field(default_factory=list)


# ───────────────────────────── links and keys ─────────────────────────────
def find_link(text: str) -> str | None:
    """A job link inside pasted text. Only links that look like one: a policy PDF or a privacy page in the footer of a
    posting must not become the job's link."""
    for u in (u.rstrip(".,;:!?") for u in _URL.findall(text)):
        if re.search(r"linkedin\.com/jobs|indeed\.|naukri\.|glassdoor\.|/jobs?/|/careers?/|/positions?/", u, re.I):
            return u
    return None


def trim_page(lines: list[str]) -> list[str]:
    """Reduce a whole job-board page to the posting: the header card, then the text from "About the job" up to the
    insights and suggestions that follow it. Lines are left alone when the page marker is absent."""
    ab = next((i for i, ln in enumerate(lines) if _ABOUT_JOB.match(ln)), None)
    if ab is None:
        return lines
    head, body = lines[:ab], lines[ab + 1:]
    hdr = next((i for i, ln in enumerate(head) if _SEP.search(ln) and (_AGE.search(ln) or _APPLICANTS.search(ln))), None)
    if hdr is not None:  # keep the card up to its "place · age · applicants" line, and the On-site / Full-time chips after it
        head = head[:hdr + 1] + [ln for ln in head[hdr + 1:] if _CHIP.match(ln)]
    end = next((i for i, ln in enumerate(body) if _END_OF_POSTING.match(ln)), len(body))
    return head + body[:end]


def link_job_ref(url: str | None) -> str | None:
    """LinkedIn and Indeed put their own job id in the link."""
    if not url:
        return None
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    q = parse_qs(p.query)
    if "linkedin.com" in host:
        m = re.search(r"/jobs/view/(?:[^/?]*-)?(\d{6,})", p.path)
        if m:
            return m.group(1)
        if q.get("currentJobId"):
            return q["currentJobId"][0]
    if "indeed." in host and (q.get("jk") or q.get("vjk")):
        return (q.get("jk") or q["vjk"])[0]
    return None


def canonical_key(link: str | None, text: str) -> str:
    """The duplicate key: the link when there is one (LinkedIn views reduced to the job id), else a hash of the text."""
    if link:
        ref = link_job_ref(link)
        if ref and "linkedin.com" in link.lower():
            return f"https://linkedin.com/jobs/view/{ref}"
        return canonicalize(link)
    body = re.sub(r"\s+", " ", text).strip().lower()[:600]
    return "manual://" + hashlib.sha1(body.encode("utf-8", "replace")).hexdigest()[:20]


# ───────────────────────────── detection ─────────────────────────────
def _city_pattern(vocab: Vocab) -> re.Pattern[str]:
    words = [a for aliases in vocab.raw.get("cities", {}).values() for a in aliases] + ["india", "remote", "karnataka", "maharashtra", "telangana", "tamil nadu", "haryana", "uttar pradesh"]
    return re.compile(r"\b(?:" + "|".join(re.escape(w) for w in words) + r")\b", re.I)


# Postings authored with adjacent bold elements arrive with the line breaks missing:
#   "Backend" + "Team: Consumer Product" + "Location: India"  ->  "BackendTeam: Consumer ProductLocation: India"
_GLUED_LABEL = re.compile(
    r"(?<=[a-z0-9)\]])(?=(?:Team|Location|Department|Business Unit|Job Location|Work Location|Employment Type|"
    r"Reports To|Job ID|Requisition ID|Req ID)\s*:)")
_HEADING_PHRASES = (
    r"Company Introduction|About the role|About the job|About the team|About us|About you|Job Description|"
    r"Key Responsibilities|Responsibilities|What you(?:’|')ll do|What you(?:’|')ll need|"
    r"What you(?:’|')ll bring|Requirements|Qualifications|Good to have|Nice to have|Level expectations|"
    r"Who will excel\??|Benefits|Perks"
)
_GLUED_HEADING = re.compile(rf"((?i:{_HEADING_PHRASES}))(?=[A-Z][a-z])")


def unglue(text: str) -> str:
    """Put back the line breaks a copy or a page read dropped, but only where nothing else is possible:
    a known label stuck to the word before it, or a known heading stuck to the sentence after it."""
    text = _GLUED_LABEL.sub("\n", text)
    return _GLUED_HEADING.sub(lambda m: m.group(1) + "\n", text)


def _clean_lines(text: str) -> list[str]:
    out = []
    for ln in unglue(text).replace("\r", "").split("\n"):
        ln = re.sub(r"[ \t ​]+", " ", ln).strip()
        if not ln or _NOISE.match(ln):
            continue
        if all(_NOISE.match(p) or _APPLICANTS.search(p) for p in _SEP.split(ln) if p.strip()):  # "Promoted by hirer · Actively reviewing applicants"
            continue
        out.append(ln)
    return out


def _looks_like_name(s: str) -> bool:
    s = s.strip()
    return (1 < len(s) <= 60 and s[0].isalnum() and len(s.split()) <= 7 and not _BAD_COMPANY.match(s)
            and not _ROLE_WORD.search(s) and not s.endswith((".", ":")))


def detect(lines: list[str], vocab: Vocab) -> tuple[Detected, Detected, Detected, set[int], str | None]:
    """Returns company, title, location, indexes of lines used up by the header, and a work-mode hint."""
    cities = _city_pattern(vocab)
    company, title, location = Detected(), Detected(), Detected()
    used: set[int] = set()
    mode: str | None = None

    # 1. explicit labels
    for i, ln in enumerate(lines[:60]):
        for name, rx in _LABELS.items():
            m = rx.match(ln)
            if m:
                det = {"company": company, "title": title, "location": location}[name]
                if not det.value:
                    det.value, det.confidence, det.how = m.group(1).strip(), 0.9, "label"
                    used.add(i)

    # 2. the job-board header: "Company · Bengaluru, Karnataka, India (Hybrid) · 2 weeks ago · 45 applicants"
    head_i = next((i for i, ln in enumerate(lines[:25]) if _SEP.search(ln) and (_AGE.search(ln) or _APPLICANTS.search(ln)
                                                                              or (cities.search(ln) and len(ln) < 160))), None)
    if head_i is not None:
        parts = [p for p in _SEP.split(lines[head_i]) if p.strip()]
        used.add(head_i)
        for p in parts:
            if _AGE.search(p) or _APPLICANTS.search(p):
                continue
            mm = _MODE.search(p)
            if mm:
                mode = {"hybrid": "Hybrid", "remote": "Remote"}.get(mm.group(1).lower(), "Onsite")
            clean = _MODE.sub("", p).strip(" ,")
            if cities.search(clean) and not location.value:
                location.value, location.confidence, location.how = clean, 0.85, "header"
            elif not company.value and _looks_like_name(clean):
                company.value, company.confidence, company.how = clean, 0.8, "header"
        above = [i for i in range(max(0, head_i - 3), head_i) if i not in used]
        if not title.value and above:
            roley = [i for i in above if _ROLE_WORD.search(lines[i]) and len(lines[i]) <= 110]
            if roley:
                j = roley[-1]
                title.value, title.confidence, title.how = lines[j], 0.8, "above header"
                used.add(j)
            else:
                j = above[-1]
                title.value, title.confidence, title.how = lines[j], 0.5, "line above header"
                used.add(j)
        if not company.value:
            rest = [i for i in above if i not in used and _looks_like_name(lines[i])]
            if rest:
                j = rest[0]
                company.value, company.confidence, company.how = lines[j], 0.7, "line above title"
                used.add(j)

    # 3. no header: first role-looking line is the title
    if not title.value:
        for i, ln in enumerate(lines[:8]):
            if _ROLE_WORD.search(ln) and len(ln) <= 110 and not ln.endswith("."):
                title.value, title.confidence, title.how = ln, 0.6, "first role-like line"
                used.add(i)
                break

    # 4. company from the prose
    if not company.value:
        for i, ln in enumerate(lines[:40]):
            m = _IS_HIRING.match(ln) or _ABOUT.match(ln)
            if m and _looks_like_name(m.group(1)):
                company.value, company.confidence, company.how = m.group(1).strip(), 0.5, "prose"
                break
    if not company.value and title.value:  # a short name-like line next to the title
        ti = next((i for i in used if lines[i] == title.value), None)
        if ti is not None:
            for i in (ti - 1, ti + 1):
                if 0 <= i < len(lines) and i not in used and _looks_like_name(lines[i]) and len(lines[i].split()) <= 4:
                    company.value, company.confidence, company.how = lines[i], 0.4, "line next to title"
                    used.add(i)
                    break

    # 5. location without a header
    if not location.value:
        for i, ln in enumerate(lines[:20]):
            if i not in used and len(ln) <= 80 and cities.search(ln) and not _ROLE_WORD.search(ln) and not ln.endswith("."):
                location.value, location.confidence, location.how = _MODE.sub("", ln).strip(" ,"), 0.6, "city line"
                break
    return company, title, location, used, mode


# ───────────────────────────── text -> html ─────────────────────────────
def to_html(lines: list[str], vocab: Vocab) -> str:
    out: list[str] = []
    in_list = False
    for ln in lines:
        if _BULLET.match(ln):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{htmllib.escape(_BULLET.sub('', ln))}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if looks_like_heading(ln, vocab):
            out.append(f"<h3>{htmllib.escape(ln.rstrip(':'))}</h3>")
        else:
            out.append(f"<p>{htmllib.escape(ln)}</p>")
    if in_list:
        out.append("</ul>")
    return "".join(out)


# ───────────────────────────── entry point ─────────────────────────────
def parse_pasted(text: str, *, link: str | None = None, overrides: dict | None = None,
                 hints: dict | None = None, vocab: Vocab | None = None) -> Parsed:
    """`overrides` (company, role, location, job_link, job_ref) are the values you confirmed in the preview.
    `hints` (title, company, location, confidence) come from the browser extension, which read them from the page
    itself; they beat what is guessed from the text."""
    vocab = vocab or load_vocab()
    overrides = overrides or {}
    hints = hints or {}
    lines = trim_page(_clean_lines(text))
    company, title, location, used, mode = detect(lines, vocab)
    for i, ln in enumerate(lines[:14]):  # the On-site / Hybrid / Remote chip of a job card states the work mode outright
        if _CHIP.match(ln):
            used.add(i)
            low = ln.lower().replace("-", "")
            if low in ("onsite", "hybrid", "remote") and not mode:
                mode = {"hybrid": "Hybrid", "remote": "Remote"}.get(low, "Onsite")
    hint_conf = float(hints.get("confidence", 0.9))
    for name, det in (("title", title), ("company", company), ("location", location)):
        value = str(hints.get(name) or "").strip()
        if not value:
            continue
        mm = _MODE.search(value) if name == "location" else None
        if mm:
            mode = {"hybrid": "Hybrid", "remote": "Remote"}.get(mm.group(1).lower(), "Onsite")
            value = _MODE.sub("", value).strip(" ,")
        det.value, det.confidence, det.how = value, hint_conf, "page"
    found_link = link or find_link(text)  # a link you were given (the page's own address) beats one found in the text
    body = [ln for i, ln in enumerate(lines) if i not in used and ln != found_link]
    # a lone link line is not part of the description
    body = [ln for ln in body if not (_URL.fullmatch(ln))]

    final_link = overrides.get("job_link", found_link) or None
    job_ref = overrides.get("job_ref") or hints.get("job_ref") or link_job_ref(final_link)
    warnings: list[str] = []
    if not lines:
        warnings.append("nothing to read")
    posting = Posting(
        ats="manual", url=final_link or "",
        title=(overrides.get("role") or title.value or "").strip(),
        company=(overrides.get("company") or company.value or None),
        location=overrides.get("location", location.value) or None,
        job_ref=job_ref, work_mode=_mode_of(overrides.get("work_mode"), mode),
        description_html=to_html(body, vocab), raw={"text": text},
    )
    return Parsed(posting, company, title, location, final_link, warnings)


def _mode_of(override: str | None, header_mode: str | None) -> WorkMode | None:
    v = override or header_mode
    return v if v in ("Onsite", "Hybrid", "Remote") else None  # type: ignore[return-value]
