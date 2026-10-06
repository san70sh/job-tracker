"""Posting -> ExtractedJob: every deterministic extractor, wired together."""
from __future__ import annotations

from ..config import get_settings
from ..models import ExtractedJob, Posting, Section
from .facts import (
    detect_work_mode,
    find_req_id,
    format_experience,
    level_display,
    normalize_level,
    parse_experience,
)
from .salary import Benchmark, SalaryResult, resolve_salary
from .sections import bullets_of, split_sections
from .team import infer_team
from .tech import tag_technologies
from .text import blocks_to_text, html_to_blocks
from .vocab import Vocab, load_vocab

# Fields the pipeline expects; anything missing after extraction is a candidate for the LLM fallback.
REQUIRED_FIELDS = ("role", "company", "location", "key_responsibilities", "requirements")
FALLBACK_FIELDS = REQUIRED_FIELDS + ("work_mode", "level", "experience_required", "team_domain")


def build_sections(posting: Posting) -> list[Section]:
    sections = split_sections(posting.description_html) if posting.description_html else []
    for ex in posting.sections or []:  # explicit sections from the ATS replace heuristic ones of the same kind
        if ex.kind in ("responsibilities", "requirements", "nice_to_have"):
            sections = [s for s in sections if s.kind != ex.kind]
        sections.append(ex)
    order = {"about_company": 0, "role": 1, "responsibilities": 2, "requirements": 3, "nice_to_have": 4,
             "benefits": 5, "logistics": 6, "other": 7}
    return sorted(sections, key=lambda s: order.get(s.kind, 9))


def _join(sections: list[Section], kinds: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    for s in sections:
        if s.kind in kinds:
            out.extend(s.bullets or bullets_of(s.text))
    return [b for b in out if b]


def format_requirements(required: list[str], nice: list[str]) -> str:
    parts = []
    if required:
        parts.append("REQUIRED\n" + "\n".join(f"• {b}" for b in required))
    if nice:
        parts.append("GOOD TO HAVE\n" + "\n".join(f"• {b}" for b in nice))
    return "\n\n".join(parts)


def extract(posting: Posting, vocab: Vocab | None = None) -> ExtractedJob:
    vocab = vocab or load_vocab()
    ex = ExtractedJob(posting=posting)
    sections = build_sections(posting)
    ex.sections = sections
    full_text = blocks_to_text(html_to_blocks(posting.description_html)) if posting.description_html else ""

    manual = posting.ats == "manual"  # pasted text: values were detected by rules and confirmed by you
    src = "rules" if manual else ("ats_api" if posting.ats not in ("html", "icims") else "jsonld")
    ex.set("role", posting.title.strip(), src, 0.8 if manual else 0.99)
    ex.set("company", posting.company, src, (0.8 if manual else 0.95) if posting.company else 0)
    ex.set("job_link", posting.url, "manual" if manual else src, 1.0)
    ex.set("job_ref", posting.job_ref or find_req_id(full_text) or posting.ats_posting_id, src, 0.9)
    ex.set("location", posting.location, src, 0.8 if manual else 0.95)
    ex.set("team_domain", posting.department, src, 0.8)
    if "team_domain" not in ex.fields:  # no department from the ATS: look in the title and the text
        guess = infer_team(posting.title, posting.company, sections, full_text, vocab)
        if guess:
            ex.set("team_domain", guess.value, guess.method, guess.confidence, guess.evidence)
    if posting.posted_at:
        ex.set("posted_at", posting.posted_at, src, 0.95)

    # work mode: explicit ATS value beats text rules
    if posting.work_mode:
        ex.set("work_mode", posting.work_mode, src, 0.95)
    else:
        mode, evidence = detect_work_mode(posting.title, posting.location, full_text)
        ex.set("work_mode", mode, "rules", 0.7, evidence)

    # level
    norm = normalize_level(posting.title, vocab)
    ex.set("level_normalized", norm, "rules", 0.7)
    ex.set("level", level_display(posting.title, norm, full_text), "rules", 0.7)

    # responsibilities / requirements
    resp = _join(sections, ("responsibilities",))
    conf = 0.85
    if not resp:  # no explicit heading: use bullets from the overview/unclassified text as a weaker guess
        resp = _join(sections, ("role", "other"))[:12]
        conf = 0.45
    ex.set("key_responsibilities", "\n".join(f"• {b}" for b in resp), "rules", conf)
    required = _join(sections, ("requirements",))
    nice = _join(sections, ("nice_to_have",))
    ex.set("requirements", format_requirements(required, nice), "rules", 0.85 if required else 0.4)

    # experience: look in requirements first, then everywhere
    exp = parse_experience("\n".join(required)) or parse_experience(full_text)
    if exp:
        ex.set("experience_required", exp.text, "rules", 0.8)
        ex.set("experience_min_years", exp.min_years, "rules", 0.8)
        ex.set("experience_max_years", exp.max_years, "rules", 0.8)
        ex.set("experience_label", format_experience(exp), "rules", 0.8)

    ex.technologies = tag_technologies(sections, vocab)
    ex.pay = posting.pay
    return ex


def apply_salary(
    ex: ExtractedJob,
    benchmarks: list[Benchmark],
) -> SalaryResult | None:
    text = blocks_to_text(html_to_blocks(ex.posting.description_html)) if ex.posting.description_html else ""
    res = resolve_salary(ex.pay, text, benchmarks, ex.get("company"), ex.get("level_normalized"), ex.get("location"))
    if res:
        ex.set("salary_min_lpa", res.min_lpa, "rules", 0.9 if res.method == "posted_range" else 0.6)
        ex.set("salary_max_lpa", res.max_lpa, "rules", 0.9 if res.method == "posted_range" else 0.6)
        ex.set("salary_details", res.explanation, "rules", 0.9)
        ex.set("salary_sources", "\n".join(res.sources), "rules", 0.9)
    return res


def missing_fields(ex: ExtractedJob, threshold: float | None = None) -> list[str]:
    """Fields the LLM fallback should fill. A call is only worth making when a HARD field is missing or weak;
    soft fields (team, work mode, level, experience) ride along in that call but never trigger one alone,
    since many postings genuinely do not state them."""
    t = threshold if threshold is not None else get_settings().confidence_threshold
    hard = [n for n in REQUIRED_FIELDS if (fv := ex.fields.get(n)) is None or fv.confidence < t]
    if not hard:
        return []
    soft = [n for n in FALLBACK_FIELDS if n not in REQUIRED_FIELDS and n not in ex.fields]
    return hard + soft


def render_body(ex: ExtractedJob, company_about: str | None = None) -> str:
    """Markdown for the Notion page body / detail page."""
    headings = {
        "about_company": "About", "role": "The role", "responsibilities": "Key responsibilities",
        "requirements": "Qualifications", "nice_to_have": "Good to have", "benefits": "Benefits",
        "logistics": "Logistics", "other": "Details",
    }
    lines: list[str] = []
    if company_about and not any(s.kind == "about_company" for s in ex.sections):
        lines += [f"## About {ex.get('company', 'the company')}", company_about, ""]
    for s in ex.sections:
        lines.append(f"## {s.heading if s.kind == 'other' else headings[s.kind]}")
        if s.bullets:
            lines += [f"- {b}" for b in s.bullets]
        else:
            lines.append(s.text)
        lines.append("")
    return "\n".join(lines).strip() + "\n"
