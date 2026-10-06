"""All SQL lives here. Functions take an open connection and are synchronous."""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from . import filters
from .extract.salary import Benchmark, SalaryResult
from .extract.vocab import load_vocab
from .models import IDENTITY_KEYS, BoardRef, ExtractedJob, ListedPosting
from .urls import normalize_company

STATUSES = ["Applied", "OA / Assessment", "Recruiter Screen", "Interviewing", "Offer", "Rejected", "Withdrawn", "Ghosted"]
INBOX = "Inbox"  # the pipeline column for jobs not applied to yet
INBOX_LABEL = "Not applied"  # what a job's status pill says while it is there


def status_slug(name: str) -> str:
    """'OA / Assessment' -> 'oa-assessment': the name the stylesheet and the pages use for a status's colour."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


# ───────────────────────────── vocabulary ─────────────────────────────
def seed_vocab(c: psycopg.Connection) -> int:
    vocab = load_vocab()
    with c.cursor() as cur:
        for t in vocab.raw["technologies"]:
            cur.execute(
                "INSERT INTO technologies (name, category) VALUES (%s, %s) "
                "ON CONFLICT (name) DO UPDATE SET category = EXCLUDED.category",
                (t["name"], t["category"]),
            )
    c.commit()
    return len(vocab.raw["technologies"])


# ───────────────────────────── companies ─────────────────────────────
def upsert_company(c: psycopg.Connection, name: str, ats: str | None = None, slug: str | None = None,
                   config: dict | None = None) -> str:
    norm = normalize_company(name) or name.lower()
    with c.cursor() as cur:
        cur.execute(
            """INSERT INTO companies (name, normalized_name, ats, ats_slug, ats_config)
               VALUES (%s, %s, %s::ats_type, %s, %s)
               ON CONFLICT (normalized_name) DO UPDATE SET
                 ats = COALESCE(companies.ats, EXCLUDED.ats),
                 ats_slug = COALESCE(companies.ats_slug, EXCLUDED.ats_slug),
                 ats_config = CASE WHEN companies.ats_config = '{}'::jsonb THEN EXCLUDED.ats_config ELSE companies.ats_config END
               RETURNING id""",
            (name, norm, ats, slug, Jsonb(config or {})),
        )
        return str(cur.fetchone()["id"])


# ───────────────────────────── lookups ─────────────────────────────
def find_job_by_url(c: psycopg.Connection, canonical: str) -> dict | None:
    with c.cursor() as cur:
        cur.execute("SELECT id, role, status, stage FROM jobs WHERE canonical_url = %s", (canonical,))
        return cur.fetchone()


def find_job_by_ref(c: psycopg.Connection, company_id: str, job_ref: str | None) -> dict | None:
    if not job_ref:
        return None
    with c.cursor() as cur:
        cur.execute("SELECT id, role, status, stage FROM jobs WHERE company_id = %s AND job_ref = %s", (company_id, job_ref))
        return cur.fetchone()


def find_job_by_company_ref(c: psycopg.Connection, company: str, job_ref: str) -> dict | None:
    """Same requisition at the same company, by the company's name as typed (before its id is known)."""
    with c.cursor() as cur:
        cur.execute(
            "SELECT j.id, j.role FROM jobs j JOIN companies co ON co.id = j.company_id "
            "WHERE co.normalized_name = %s AND j.job_ref = %s",
            (normalize_company(company) or company.lower(), job_ref))
        return cur.fetchone()


def load_benchmarks(c: psycopg.Connection) -> list[Benchmark]:
    with c.cursor() as cur:
        cur.execute(
            """SELECT b.*, co.name AS company_name FROM salary_benchmarks b
               LEFT JOIN companies co ON co.id = b.company_id"""
        )
        return [
            Benchmark(
                id=str(r["id"]), company=r["company_name"], level_norm=r["level_norm"], location_norm=r["location_norm"],
                total_annual=float(r["total_annual"]), kind=r["kind"], source=r["source"],
                source_url=r["source_url"], observed_at=r["observed_at"],
                years_exp=float(r["years_exp"]) if r["years_exp"] is not None else None,
            )
            for r in cur.fetchall()
        ]


def add_benchmark(c: psycopg.Connection, company: str, level_norm: str | None, location_norm: str | None,
                  total_annual: float, kind: str = "aggregate_avg", source: str = "manual",
                  source_url: str | None = None, years_exp: float | None = None, observed_at: date | None = None) -> None:
    cid = upsert_company(c, company)
    with c.cursor() as cur:
        cur.execute(
            """INSERT INTO salary_benchmarks (company_id, level_norm, location_norm, total_annual, years_exp,
                                              kind, source, source_url, observed_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,COALESCE(%s, current_date))""",
            (cid, level_norm, location_norm, total_annual, years_exp, kind, source, source_url, observed_at),
        )
    c.commit()


# ───────────────────────────── jobs ─────────────────────────────
JOB_COLUMNS = {
    "role", "job_ref", "job_link", "level", "location", "work_mode", "experience_required", "team_domain",
    "key_responsibilities", "requirements", "notes", "salary_min_lpa", "salary_max_lpa", "salary_details",
    "salary_sources", "date_applied",
}


def save_job(
    c: psycopg.Connection,
    ex: ExtractedJob,
    *,
    canonical: str,
    company_id: str,
    snapshot_body: str,
    parser_version: str,
    salary: SalaryResult | None = None,
    needs_review: bool = False,
    stage: str = "Inbox",
    llm_call_ids: dict[str, str] | None = None,
) -> str:
    p = ex.posting
    summary = {k: {"method": fv.method, "confidence": round(fv.confidence, 2)} for k, fv in ex.fields.items()}
    with c.cursor() as cur:
        cur.execute(
            """INSERT INTO jobs (role, company_id, job_ref, job_link, level, level_normalized, location, work_mode,
                   experience_required, experience_min_years, experience_max_years, team_domain,
                   key_responsibilities, requirements, salary_min_lpa, salary_max_lpa, salary_details, salary_sources,
                   stage, canonical_url, ats, ats_posting_id, posted_at, extraction_summary, needs_review)
               VALUES (%(role)s, %(company_id)s, %(job_ref)s, %(job_link)s, %(level)s, %(level_normalized)s, %(location)s,
                   %(work_mode)s::work_mode, %(experience_required)s, %(exp_min)s, %(exp_max)s, %(team_domain)s,
                   %(key_responsibilities)s, %(requirements)s, %(smin)s, %(smax)s, %(sdetails)s, %(ssources)s,
                   %(stage)s::job_stage, %(canonical)s, %(ats)s::ats_type, %(ats_posting_id)s, %(posted_at)s,
                   %(summary)s, %(needs_review)s)
               RETURNING id""",
            {
                "role": ex.get("role") or p.title, "company_id": company_id,
                "job_ref": ex.get("job_ref"), "job_link": ex.get("job_link") or p.url,
                "level": ex.get("level"), "level_normalized": ex.get("level_normalized"),
                "location": ex.get("location"), "work_mode": ex.get("work_mode"),
                "experience_required": ex.get("experience_required"),
                "exp_min": ex.get("experience_min_years"), "exp_max": ex.get("experience_max_years"),
                "team_domain": ex.get("team_domain"),
                "key_responsibilities": ex.get("key_responsibilities"), "requirements": ex.get("requirements"),
                "smin": ex.get("salary_min_lpa"), "smax": ex.get("salary_max_lpa"),
                "sdetails": ex.get("salary_details"), "ssources": ex.get("salary_sources"),
                "stage": stage, "canonical": canonical, "ats": _ats(p.ats), "ats_posting_id": p.ats_posting_id,
                "posted_at": p.posted_at, "summary": Jsonb(summary), "needs_review": needs_review,
            },
        )
        job_id = str(cur.fetchone()["id"])

        cur.execute(
            "INSERT INTO job_snapshots (job_id, body, content_hash, parser_version, content_type) VALUES (%s,%s,%s,%s,%s) "
            "ON CONFLICT (job_id, content_hash) DO NOTHING",
            (job_id, snapshot_body, _hash(snapshot_body), parser_version, "application/json"),
        )
        for i, s in enumerate(ex.sections):
            cur.execute(
                "INSERT INTO job_sections (job_id, kind, heading, body_md, position) VALUES (%s,%s::section_kind,%s,%s,%s)",
                (job_id, s.kind, s.heading, "\n".join(f"- {b}" for b in s.bullets) if s.bullets else s.text, i),
            )
        if ex.technologies:
            cur.execute("SELECT id, name FROM technologies WHERE name = ANY(%s)", (list(ex.technologies),))
            ids = {r["name"]: r["id"] for r in cur.fetchall()}
            for name, kind in ex.technologies.items():
                if name in ids:
                    cur.execute(
                        "INSERT INTO job_technologies (job_id, tech_id, kind) VALUES (%s,%s,%s::tech_kind) ON CONFLICT DO NOTHING",
                        (job_id, ids[name], kind),
                    )
        for field, fv in ex.fields.items():
            cur.execute(
                """INSERT INTO field_provenance (job_id, field, method, confidence, evidence, llm_call_id)
                   VALUES (%s,%s,%s::extract_method,%s,%s,%s)
                   ON CONFLICT (job_id, field) DO UPDATE SET method = EXCLUDED.method, confidence = EXCLUDED.confidence""",
                (job_id, field, _method(fv.method), min(float(fv.confidence), 9.99), (fv.evidence or "")[:300] or None,
                 (llm_call_ids or {}).get(field)),
            )
        if salary:
            cur.execute(
                "INSERT INTO salary_estimates (job_id, benchmark_id, min_lpa, max_lpa, method, explanation) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (job_id, salary.benchmark_ids[0] if salary.benchmark_ids else None, salary.min_lpa, salary.max_lpa,
                 salary.method, salary.explanation),
            )
    c.commit()
    return job_id


def import_job(c: psycopg.Connection, r: dict, canonical: str) -> str:
    """Insert a row that came from the Notion Job Tracker (already applied-to, fields are as Claude wrote them)."""
    company_id = upsert_company(c, r["company"] or "Unknown")
    status = r["status"] if r["status"] in STATUSES else None
    with c.cursor() as cur:
        cur.execute(
            """INSERT INTO jobs (notion_page_id, role, company_id, status, stage, date_applied, job_ref, job_link, level,
                   location, work_mode, experience_required, team_domain, key_responsibilities, requirements, notes,
                   salary_min_lpa, salary_max_lpa, salary_details, salary_sources, canonical_url, ats, last_activity_at)
               VALUES (%(notion_page_id)s, %(role)s, %(company_id)s, %(status)s::job_status, %(stage)s::job_stage, %(date_applied)s::date,
                   %(job_ref)s, %(job_link)s, %(level)s, %(location)s, %(work_mode)s::work_mode, %(experience_required)s,
                   %(team_domain)s, %(key_responsibilities)s, %(requirements)s, %(notes)s, %(salary_min_lpa)s,
                   %(salary_max_lpa)s, %(salary_details)s, %(salary_sources)s, %(canonical)s, 'manual',
                   COALESCE(%(date_applied)s::date::timestamptz, now()))
               RETURNING id""",
            {**r, "company_id": company_id, "status": status, "stage": "Applied" if status else "Inbox",
             "canonical": canonical, "job_link": r["job_link"] or "", "role": r["role"] or "(untitled)"},
        )
        jid = str(cur.fetchone()["id"])
        if status:
            cur.execute("INSERT INTO status_events (job_id, from_status, to_status, source, occurred_at) "
                        "VALUES (%s, NULL, %s::job_status, 'notion_import', COALESCE(%s::date::timestamptz, now()))",
                        (jid, status, r["date_applied"]))
        if r["technologies"]:
            cur.execute("SELECT id, name FROM technologies WHERE name = ANY(%s)", (r["technologies"],))
            for t in cur.fetchall():
                cur.execute("INSERT INTO job_technologies (job_id, tech_id, kind) VALUES (%s,%s,'required') ON CONFLICT DO NOTHING",
                            (jid, t["id"]))
        cur.execute("INSERT INTO notion_sync (job_id, notion_page_id) VALUES (%s,%s)",
                    (jid, r["notion_page_id"]))
    c.commit()
    return jid


def _ats(name: str) -> str:
    return name if name in {"greenhouse", "lever", "ashby", "smartrecruiters", "workday", "oracle", "eightfold", "amazon", "jibe", "avature", "icims", "html", "manual"} else "html"


def _method(m: str) -> str:
    return m if m in {"ats_api", "jsonld", "rules", "llm", "manual"} else "rules"


def _hash(s: str) -> str:
    import hashlib

    return hashlib.sha256(s.encode("utf-8", "replace")).hexdigest()


EDITABLE = JOB_COLUMNS | {"company"}  # company lives in its own table; editing it re-points the job


def _same(a: Any, b: Any) -> bool:
    """Equality that treats 48 and Decimal('48.00') as the same, and None only as equal to None."""
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, (int, float, Decimal)) and isinstance(b, (int, float, Decimal)):
        return abs(float(a) - float(b)) < 1e-9
    return a == b


def update_job(c: psycopg.Connection, job_id: str, **fields: Any) -> list[str]:
    """Apply user edits. Only fields whose value really changes are written, marked as manual, and queued for
    Notion, so submitting a whole form does not stamp untouched fields as edited. Returns the changed field names."""
    bad = set(fields) - EDITABLE
    if bad:
        raise ValueError(f"not editable: {sorted(bad)}")
    if "company" in fields:
        fields["company"] = (fields["company"] or "").strip()
        if not fields["company"]:
            raise ValueError("company cannot be empty")
    if not fields:
        return []
    with c.cursor() as cur:
        cur.execute("SELECT j.*, co.name AS company FROM jobs j JOIN companies co ON co.id = j.company_id "
                    "WHERE j.id = %s FOR UPDATE OF j", (job_id,))
        current = cur.fetchone()
        if current is None:
            raise KeyError(job_id)
        changed = {k: v for k, v in fields.items() if not _same(current[k], v)}
        if not changed:
            return []
        columns = {k: v for k, v in changed.items() if k != "company"}
        old_company_id = current["company_id"]
        if "company" in changed:
            columns["company_id"] = upsert_company(c, changed["company"])
        sets = ", ".join(f"{k} = %({k})s" + ("::work_mode" if k == "work_mode" else "") for k in columns)
        cur.execute(f"UPDATE jobs SET {sets}, last_activity_at = now() WHERE id = %(id)s", {**columns, "id": job_id})
        for k in changed:
            cur.execute(
                """INSERT INTO field_provenance (job_id, field, method, confidence) VALUES (%s,%s,'manual',1.0)
                   ON CONFLICT (job_id, field) DO UPDATE SET method='manual', confidence=1.0, updated_at=now()""",
                (job_id, k),
            )
        if "company" in changed and columns["company_id"] != old_company_id:  # tidy up a company nothing refers to any more
            cur.execute(
                """DELETE FROM companies WHERE id = %s
                   AND NOT EXISTS (SELECT 1 FROM jobs WHERE company_id = companies.id)
                   AND NOT EXISTS (SELECT 1 FROM watched_boards WHERE company_id = companies.id)
                   AND NOT EXISTS (SELECT 1 FROM salary_benchmarks WHERE company_id = companies.id)""", (old_company_id,))
        # the review flag clears itself once every hard field has content
        cur.execute(
            """UPDATE jobs SET needs_review = NOT (role <> '' AND COALESCE(key_responsibilities,'') <> ''
                                                  AND COALESCE(requirements,'') <> '') WHERE id = %s""", (job_id,))
        _enqueue_notion(cur, job_id)
    c.commit()
    return list(changed)


def backfill_experience(c: psycopg.Connection, dry_run: bool = False) -> list[dict]:
    """Fill experience_min/max_years for rows that have the text but not the numbers (the Notion import copied only
    the text). Only reads experience_required; leaves rows whose text names no number."""
    import re

    from .extract.facts import parse_experience

    # "Not stated (AVP typically ~8-12 yrs)" is a guess about other postings, not this posting's requirement.
    not_stated = re.compile(r"^\s*(not\s+(stated|specified|shown|mentioned|confirmed|given)|n/?a\b|unknown|tbd)", re.I)
    out: list[dict] = []
    with c.cursor() as cur:
        cur.execute("SELECT id, role, experience_required FROM jobs "
                    "WHERE experience_min_years IS NULL AND COALESCE(experience_required, '') <> '' ORDER BY created_at")
        for r in cur.fetchall():
            if not_stated.match(r["experience_required"]):
                continue
            e = parse_experience(r["experience_required"], require_context=False)
            if e is None:
                continue
            out.append({"id": str(r["id"]), "role": r["role"], "text": r["experience_required"], "min": e.min_years, "max": e.max_years})
            if dry_run:
                continue
            cur.execute("UPDATE jobs SET experience_min_years = %s, experience_max_years = %s WHERE id = %s", (e.min_years, e.max_years, r["id"]))
            for field in ("experience_min_years", "experience_max_years"):
                cur.execute(
                    """INSERT INTO field_provenance (job_id, field, method, confidence) VALUES (%s,%s,'rules',0.70)
                       ON CONFLICT (job_id, field) DO NOTHING""", (r["id"], field))
    if not dry_run:
        c.commit()
    return out


def backfill_team(c: psycopg.Connection, dry_run: bool = False) -> list[dict]:
    """Infer a blank team_domain from the stored title and sections (no refetch). Jobs imported from Notion have no
    sections and keep whatever Claude wrote; only jobs added by the app can gain a value. Never overwrites."""
    from .extract.team import infer_team
    from .models import Section

    out: list[dict] = []
    with c.cursor() as cur:
        cur.execute("SELECT j.id, j.role, co.name AS company FROM jobs j JOIN companies co ON co.id = j.company_id "
                    "WHERE COALESCE(j.team_domain, '') = '' ORDER BY j.created_at")
        for r in cur.fetchall():
            cur.execute("SELECT kind::text AS kind, heading, body_md FROM job_sections WHERE job_id = %s ORDER BY position", (r["id"],))
            secs = [Section(kind=s["kind"], heading=s["heading"], text=s["body_md"]) for s in cur.fetchall()]
            g = infer_team(r["role"], r["company"], secs, "\n".join(s.text for s in secs))
            if not g:
                continue
            out.append({"id": str(r["id"]), "role": r["role"], "value": g.value, "confidence": g.confidence, "evidence": g.evidence})
            if dry_run:
                continue
            cur.execute("UPDATE jobs SET team_domain = %s WHERE id = %s", (g.value, r["id"]))
            cur.execute(
                """INSERT INTO field_provenance (job_id, field, method, confidence, evidence)
                   VALUES (%s,'team_domain','rules',%s,%s)
                   ON CONFLICT (job_id, field) DO UPDATE SET method='rules', confidence=EXCLUDED.confidence,
                                                             evidence=EXCLUDED.evidence, updated_at=now()""",
                (r["id"], g.confidence, g.evidence[:300]))
            _enqueue_notion(cur, str(r["id"]))
    if not dry_run:
        c.commit()
    return out


def reparse_candidates(c: psycopg.Connection, job_id: str | None = None) -> list[dict]:
    """Every job (or one) with what reparse needs: its saved snapshot, current text fields, provenance, sections, tags."""
    with c.cursor() as cur:
        cur.execute(
            """SELECT j.id::text AS id, j.role, co.name AS company, j.ats::text AS ats, j.job_link, j.stage::text AS stage,
                      j.needs_review, j.key_responsibilities, j.requirements, j.experience_required, j.team_domain,
                      (SELECT s.body FROM job_snapshots s WHERE s.job_id = j.id ORDER BY s.fetched_at DESC LIMIT 1) AS snapshot
               FROM jobs j JOIN companies co ON co.id = j.company_id
               WHERE (%(id)s::uuid IS NULL OR j.id = %(id)s::uuid) ORDER BY j.created_at""", {"id": job_id})
        jobs = cur.fetchall()
        for j in jobs:
            cur.execute("SELECT field, method::text AS method FROM field_provenance WHERE job_id = %s", (j["id"],))
            j["provenance"] = {r["field"]: r["method"] for r in cur.fetchall()}
            cur.execute("SELECT kind::text AS kind, heading, body_md FROM job_sections WHERE job_id = %s ORDER BY position", (j["id"],))
            j["sections"] = [(r["kind"], r["heading"], r["body_md"]) for r in cur.fetchall()]
            cur.execute("SELECT t.name, jt.kind::text AS kind FROM job_technologies jt JOIN technologies t ON t.id = jt.tech_id "
                        "WHERE jt.job_id = %s", (j["id"],))
            j["technologies"] = {r["name"]: r["kind"] for r in cur.fetchall()}
    return jobs


_REPARSE_COLUMNS = {"key_responsibilities", "requirements", "experience_required", "experience_min_years",
                    "experience_max_years", "team_domain"}


def apply_reparse(c: psycopg.Connection, job_id: str, plan: dict, ex: ExtractedJob, clear_review: bool = False) -> None:
    """Write the result of a re-extraction: changed text fields (with provenance), sections and technologies."""
    with c.cursor() as cur:
        for col, value in plan["updates"].items():
            assert col in _REPARSE_COLUMNS, col
            cur.execute(f"UPDATE jobs SET {col} = %s WHERE id = %s", (value, job_id))
            fv = ex.fields.get(col)
            if fv:
                cur.execute(
                    """INSERT INTO field_provenance (job_id, field, method, confidence, evidence)
                       VALUES (%s,%s,%s::extract_method,%s,%s)
                       ON CONFLICT (job_id, field) DO UPDATE SET method = EXCLUDED.method, confidence = EXCLUDED.confidence,
                                                                 evidence = EXCLUDED.evidence, updated_at = now()""",
                    (job_id, col, _method(fv.method), min(float(fv.confidence), 9.99), (fv.evidence or "")[:300] or None))
        if plan["sections"] is not None:
            cur.execute("DELETE FROM job_sections WHERE job_id = %s", (job_id,))
            for i, (kind, heading, body) in enumerate(plan["sections"]):
                cur.execute("INSERT INTO job_sections (job_id, kind, heading, body_md, position) VALUES (%s,%s::section_kind,%s,%s,%s)",
                            (job_id, kind, heading, body, i))
        if plan["technologies"] is not None:
            cur.execute("DELETE FROM job_technologies WHERE job_id = %s", (job_id,))
            if plan["technologies"]:
                cur.execute("SELECT id, name FROM technologies WHERE name = ANY(%s)", (list(plan["technologies"]),))
                ids = {r["name"]: r["id"] for r in cur.fetchall()}
                for name, kind in plan["technologies"].items():
                    if name in ids:
                        cur.execute("INSERT INTO job_technologies (job_id, tech_id, kind) VALUES (%s,%s,%s::tech_kind) ON CONFLICT DO NOTHING",
                                    (job_id, ids[name], kind))
        if clear_review:
            cur.execute("UPDATE jobs SET needs_review = false WHERE id = %s", (job_id,))
        _enqueue_notion(cur, job_id)
    c.commit()


def mark_reviewed(c: psycopg.Connection, job_id: str) -> None:
    with c.cursor() as cur:
        cur.execute("UPDATE jobs SET needs_review = false WHERE id = %s", (job_id,))
    c.commit()


def delete_job(c: psycopg.Connection, job_id: str) -> None:
    with c.cursor() as cur:
        cur.execute("SELECT notion_page_id FROM jobs WHERE id = %s", (job_id,))
        row = cur.fetchone()
        # a posting that was added and then deleted goes back to being undecided
        cur.execute("UPDATE discovered_postings SET state = 'seen', job_id = NULL WHERE job_id = %s", (job_id,))
        cur.execute("DELETE FROM jobs WHERE id = %s", (job_id,))
        if row and row["notion_page_id"]:  # the job row is gone, so the outbox carries the page id itself
            cur.execute("INSERT INTO notion_outbox (op, notion_page_id) VALUES ('archive', %s)", (row["notion_page_id"],))
    c.commit()


# ─────────────────────────────── notion outbox ───────────────────────────────
def _enqueue_notion(cur, job_id: str) -> None:
    """Queue a push of this job. Called inside the same transaction as the change, so it cannot be lost.
    Only jobs that have been applied to are mirrored. Repeated edits collapse into one pending push."""
    cur.execute(
        """INSERT INTO notion_outbox (job_id, op)
           SELECT id, 'upsert' FROM jobs WHERE id = %s AND stage = 'Applied'
           ON CONFLICT (job_id) WHERE op = 'upsert' AND status = 'pending'
           DO UPDATE SET next_attempt_at = now(), attempts = 0""",
        (job_id,),
    )


def enqueue_notion(c: psycopg.Connection, job_id: str) -> None:
    with c.cursor() as cur:
        _enqueue_notion(cur, job_id)
    c.commit()


def due_outbox(c: psycopg.Connection, limit: int = 50) -> list[dict]:
    with c.cursor() as cur:
        cur.execute("SELECT id, job_id, notion_page_id, op, attempts FROM notion_outbox "
                    "WHERE status = 'pending' AND next_attempt_at <= now() ORDER BY id LIMIT %s", (limit,))
        return cur.fetchall()


def outbox_done(c: psycopg.Connection, outbox_id: int) -> None:
    with c.cursor() as cur:
        cur.execute("UPDATE notion_outbox SET status = 'done', done_at = now(), last_error = NULL WHERE id = %s", (outbox_id,))
    c.commit()


def outbox_fail(c: psycopg.Connection, outbox_id: int, error: str, max_attempts: int = 8) -> None:
    """Back off 2, 4, 8 ... minutes (capped at 60). After max_attempts the row is parked as 'failed' and shown in the UI."""
    with c.cursor() as cur:
        cur.execute(
            """UPDATE notion_outbox SET attempts = attempts + 1, last_error = %s,
                   status = CASE WHEN attempts + 1 >= %s THEN 'failed' ELSE 'pending' END,
                   next_attempt_at = now() + make_interval(mins => LEAST(60, power(2, attempts + 1)::int))
               WHERE id = %s""",
            (error[:500], max_attempts, outbox_id),
        )
    c.commit()


def outbox_status(c: psycopg.Connection) -> dict:
    with c.cursor() as cur:
        cur.execute("SELECT status, count(*) AS n FROM notion_outbox WHERE status <> 'done' GROUP BY status")
        counts = {r["status"]: r["n"] for r in cur.fetchall()}
        cur.execute("SELECT last_error FROM notion_outbox WHERE status <> 'done' AND last_error IS NOT NULL ORDER BY id DESC LIMIT 1")
        err = cur.fetchone()
        cur.execute("DELETE FROM notion_outbox WHERE status = 'done' AND done_at < now() - interval '7 days'")
    c.commit()
    return {"pending": counts.get("pending", 0), "failed": counts.get("failed", 0), "last_error": err["last_error"] if err else None}


def retry_failed_outbox(c: psycopg.Connection) -> int:
    with c.cursor() as cur:
        cur.execute("UPDATE notion_outbox SET status = 'pending', attempts = 0, next_attempt_at = now() WHERE status = 'failed'")
        n = cur.rowcount
    c.commit()
    return n


# ───────────────────────────── status machine ─────────────────────────────
class IllegalTransition(Exception):
    pass


def set_status(c: psycopg.Connection, job_id: str, to_status: str, *, source: str = "user", note: str | None = None,
               when: date | None = None) -> None:
    if to_status not in STATUSES:
        raise ValueError(f"unknown status {to_status!r}")
    with c.cursor() as cur:
        cur.execute("SELECT status, stage, date_applied FROM jobs WHERE id = %s FOR UPDATE", (job_id,))
        row = cur.fetchone()
        if row is None:
            raise KeyError(job_id)
        frm = row["status"]
        if frm == to_status:
            return
        cur.execute(
            "SELECT 1 FROM status_transitions WHERE to_status = %s::job_status AND "
            "(from_status IS NOT DISTINCT FROM %s::job_status)",
            (to_status, frm),
        )
        if cur.fetchone() is None:
            raise IllegalTransition(f"{frm or 'not applied'} -> {to_status} is not allowed")
        cur.execute(
            """UPDATE jobs SET status = %s::job_status, stage = 'Applied',
                   date_applied = COALESCE(date_applied, %s), last_activity_at = now() WHERE id = %s""",
            (to_status, when or date.today(), job_id),
        )
        cur.execute(
            "INSERT INTO status_events (job_id, from_status, to_status, source, note) VALUES (%s,%s,%s,%s,%s)",
            (job_id, frm, to_status, source, note),
        )
        _enqueue_notion(cur, job_id)
    c.commit()


def mark_applied(c: psycopg.Connection, job_id: str, when: date | None = None) -> None:
    set_status(c, job_id, "Applied", source="user", when=when)
    if when:
        with c.cursor() as cur:
            cur.execute("UPDATE jobs SET date_applied = %s WHERE id = %s", (when, job_id))
            _enqueue_notion(cur, job_id)
        c.commit()


# ───────────────────────────── reads for UI / sync ─────────────────────────────
def list_jobs(c: psycopg.Connection, stage: str | None = None) -> list[dict]:
    q = """SELECT j.id, j.role, co.name AS company, j.job_ref, j.status, j.stage, j.level, j.location, j.work_mode,
                  j.salary_min_lpa, j.salary_max_lpa, j.date_applied, j.job_link, j.needs_review, j.created_at,
                  COALESCE((SELECT array_agg(t.name ORDER BY t.name) FROM job_technologies jt
                            JOIN technologies t ON t.id = jt.tech_id WHERE jt.job_id = j.id), '{}') AS technologies
           FROM jobs j JOIN companies co ON co.id = j.company_id"""
    args: list[Any] = []
    if stage:
        q += " WHERE j.stage = %s::job_stage"
        args.append(stage)
    q += " ORDER BY COALESCE(j.date_applied, j.created_at::date) DESC, j.created_at DESC"
    with c.cursor() as cur:
        cur.execute(q, args)
        return cur.fetchall()


def get_job(c: psycopg.Connection, job_id: str) -> dict | None:
    with c.cursor() as cur:
        cur.execute(
            """SELECT j.*, co.name AS company FROM jobs j JOIN companies co ON co.id = j.company_id WHERE j.id = %s""",
            (job_id,),
        )
        job = cur.fetchone()
        if not job:
            return None
        cur.execute(
            """SELECT t.name, jt.kind FROM job_technologies jt JOIN technologies t ON t.id = jt.tech_id
               WHERE jt.job_id = %s ORDER BY jt.kind DESC, t.name""", (job_id,))
        job["technologies"] = cur.fetchall()
        cur.execute("SELECT kind, heading, body_md FROM job_sections WHERE job_id = %s ORDER BY position", (job_id,))
        job["sections"] = cur.fetchall()
        cur.execute("SELECT from_status, to_status, occurred_at, source, note FROM status_events WHERE job_id = %s ORDER BY occurred_at", (job_id,))
        job["events"] = cur.fetchall()
        cur.execute("SELECT body::jsonb ->> 'via' AS via FROM job_snapshots WHERE job_id = %s ORDER BY fetched_at DESC LIMIT 1", (job_id,))
        snap = cur.fetchone()
        job["via_url"] = snap["via"] if snap else None
        cur.execute("SELECT field, method, confidence FROM field_provenance WHERE job_id = %s ORDER BY field", (job_id,))
        job["provenance"] = cur.fetchall()
        return job


def job_snapshot(c: psycopg.Connection, job_id: str) -> str | None:
    with c.cursor() as cur:
        cur.execute("SELECT body FROM job_snapshots WHERE job_id = %s ORDER BY fetched_at DESC LIMIT 1", (job_id,))
        r = cur.fetchone()
        return r["body"] if r else None


def funnel(c: psycopg.Connection) -> list[dict]:
    with c.cursor() as cur:
        cur.execute("SELECT status::text AS status, jobs FROM v_funnel")
        return cur.fetchall()


# ───────────────────────────── watched boards + discovery ─────────────────────────────
BOARD_EDITABLE = ("title_include", "title_exclude", "location_include", "poll_interval_minutes", "enabled")
COMPANY_TABLES = ("jobs", "watched_boards", "salary_benchmarks")  # tables whose company_id points at companies


def _identity_sql() -> str:
    """The part of a board's config that tells boards of one system apart (the keys are IDENTITY_KEYS)."""
    return "coalesce(" + ", ".join(f"config->>'{k}'" for k in IDENTITY_KEYS) + ", '')"


def find_board(c: psycopg.Connection, board: BoardRef) -> dict | None:
    ats, slug, qualifier = board.identity()
    with c.cursor() as cur:
        cur.execute(f"SELECT id, board_url FROM watched_boards WHERE ats = %s::ats_type "
                    f"AND lower(coalesce(slug, '')) = %s AND {_identity_sql()} = %s", (ats, slug, qualifier))
        return cur.fetchone()


def add_board(c: psycopg.Connection, company: str, board: BoardRef, board_url: str, *, title_include: list[str] | None = None,
              title_exclude: list[str] | None = None, location_include: list[str] | None = None,
              interval_minutes: int | None = None) -> tuple[int, bool]:
    """Returns (board id, created). A board that is already watched is returned untouched, never overwritten."""
    if existing := find_board(c, board):
        return existing["id"], False
    values: dict[str, Any] = {
        "company_id": upsert_company(c, company, board.ats, board.slug, board.config), "ats": board.ats, "board_url": board_url,
        "slug": board.slug, "config": Jsonb(board.config), "title_include": title_include or [],
        "title_exclude": title_exclude or [], "location_include": location_include or []}
    if interval_minutes is not None:  # otherwise the column's own default applies: it is the only place that number lives
        values["poll_interval_minutes"] = interval_minutes
    marks = ", ".join("%s::ats_type" if k == "ats" else "%s" for k in values)
    with c.cursor() as cur:
        cur.execute(f"INSERT INTO watched_boards ({', '.join(values)}) VALUES ({marks}) RETURNING id", list(values.values()))
        bid = cur.fetchone()["id"]
    c.commit()
    return bid, True


def default_poll_minutes(c: psycopg.Connection) -> int:
    """The database's own default for a new board's check interval."""
    with c.cursor() as cur:
        cur.execute("SELECT column_default FROM information_schema.columns "
                    "WHERE table_name = 'watched_boards' AND column_name = 'poll_interval_minutes'")
        return int(cur.fetchone()["column_default"])


def list_boards(c: psycopg.Connection, only_due: bool = False, board_id: int | None = None) -> list[dict]:
    where = []
    if only_due:  # last_polled_at is the last attempt, so a failing board waits its interval like any other
        where.append("b.enabled AND (b.last_polled_at IS NULL OR b.last_polled_at < now() - make_interval(mins => b.poll_interval_minutes))")
    if board_id is not None:
        where.append("b.id = %(id)s")
    with c.cursor() as cur:
        cur.execute(f"""SELECT b.*, co.name AS company,
                          (SELECT count(*) FROM discovered_postings d WHERE d.board_id = b.id AND d.state = 'new') AS new_count,
                          (SELECT count(*) FROM jobs j WHERE j.company_id = b.company_id) AS company_jobs
                   FROM watched_boards b JOIN companies co ON co.id = b.company_id
                   {'WHERE ' + ' AND '.join(where) if where else ''} ORDER BY co.name""", {"id": board_id})
        return cur.fetchall()


def board_ref(row: dict) -> BoardRef:
    return BoardRef(row["ats"], row["slug"], row["company"], dict(row["config"] or {}))


def delete_board(c: psycopg.Connection, board_id: int) -> None:
    with c.cursor() as cur:
        cur.execute("DELETE FROM watched_boards WHERE id = %s", (board_id,))
    c.commit()


def update_board(c: psycopg.Connection, board_id: int, changes: dict[str, Any]) -> None:
    """Write the editable columns (BOARD_EDITABLE); validation is the caller's job (see boards.py)."""
    bad = set(changes) - set(BOARD_EDITABLE)
    if bad:
        raise ValueError(f"not editable: {', '.join(sorted(bad))}")
    if changes:
        with c.cursor() as cur:
            cur.execute(f"UPDATE watched_boards SET {', '.join(f'{k} = %s' for k in changes)} WHERE id = %s", [*changes.values(), board_id])
    c.commit()


def rename_company(c: psycopg.Connection, company_id: str, new_name: str) -> int:
    """Rename a company everywhere. If another company already has that name this one is merged into it.
    Returns how many saved jobs carry the name; their Notion pages are queued to follow."""
    norm = normalize_company(new_name) or new_name.lower()
    with c.cursor() as cur:
        cur.execute("SELECT id FROM companies WHERE normalized_name = %s AND id <> %s", (norm, company_id))
        other = cur.fetchone()
        target = str(other["id"]) if other else company_id
        if other:
            for table in COMPANY_TABLES:
                cur.execute(f"UPDATE {table} SET company_id = %s WHERE company_id = %s", (target, company_id))
            cur.execute("DELETE FROM companies WHERE id = %s", (company_id,))
        else:
            cur.execute("UPDATE companies SET name = %s, normalized_name = %s WHERE id = %s", (new_name, norm, company_id))
        cur.execute("SELECT id FROM jobs WHERE company_id = %s", (target,))
        job_ids = [str(r["id"]) for r in cur.fetchall()]
        for jid in job_ids:
            _enqueue_notion(cur, jid)
    c.commit()
    return len(job_ids)


def refilter_pending(c: psycopg.Connection, board_id: int) -> dict[str, int]:
    """After a filter edit: postings waiting for a decision that no longer match leave the Inbox ('filtered'); ones
    that now match come back as 'seen' (visible, no popup). Silent baseline rows are left alone."""
    moved = {"hidden": 0, "restored": 0}
    with c.cursor() as cur:
        cur.execute("SELECT title_include, title_exclude, location_include FROM watched_boards WHERE id = %s", (board_id,))
        f = cur.fetchone()
        cur.execute("SELECT id, title, location, state FROM discovered_postings WHERE board_id = %s AND state IN ('new', 'seen', 'filtered')", (board_id,))
        for r in cur.fetchall():
            ok = filters.passes(r["title"], r["location"], f["title_include"], f["title_exclude"], f["location_include"])
            if ok and r["state"] == "filtered":
                cur.execute("UPDATE discovered_postings SET state = 'seen' WHERE id = %s", (r["id"],))
                moved["restored"] += 1
            elif not ok and r["state"] != "filtered":
                cur.execute("UPDATE discovered_postings SET state = 'filtered' WHERE id = %s", (r["id"],))
                moved["hidden"] += 1
    c.commit()
    return moved


def applied_jobs(c: psycopg.Connection) -> list[dict]:
    """Title and place of every job you applied to: what the board filter suggestions learn from."""
    with c.cursor() as cur:
        cur.execute("SELECT role, location FROM jobs WHERE stage = 'Applied'")
        return cur.fetchall()


def record_poll(c: psycopg.Connection, board: dict, listed: list[ListedPosting]) -> list[dict]:
    """Store a poll result. Returns the postings that are new (state 'new').

    - First good check of an unfiltered board: everything already open is 'baseline' (silent), so a 400-job board
      does not flood the popup. With a title filter, matching postings are 'new' straight away.
    - Postings failing the board's filters are stored as 'filtered' (silent) so they are not re-evaluated.
    - Postings already tracked as jobs are skipped (this is what lets 'added' rows be purged safely).
    - Postings that disappear are marked 'closed', but only when the listing was complete (no page cap hit).
    """
    from .urls import canonicalize

    complete = getattr(listed, "complete", True)
    first = board["last_success_at"] is None  # not last_polled_at: a failed first attempt must not end the baseline
    baseline = first and not board["title_include"]
    new: list[dict] = []
    seen_ids = [p.ats_posting_id for p in listed]
    with c.cursor() as cur:
        for p in listed:
            cur.execute("SELECT id FROM discovered_postings WHERE board_id = %s AND ats_posting_id = %s", (board["id"], p.ats_posting_id))
            existing = cur.fetchone()
            if existing:
                cur.execute("UPDATE discovered_postings SET last_seen_at = now(), "
                            "state = CASE WHEN state = 'closed' THEN 'seen' ELSE state END WHERE id = %s", (existing["id"],))
                continue
            canon = canonicalize(p.url)
            cur.execute("SELECT 1 FROM jobs WHERE canonical_url = %s", (canon,))
            if cur.fetchone():
                continue
            ok = filters.passes(p.title, p.location, board["title_include"], board["title_exclude"], board["location_include"])
            state = "filtered" if not ok else ("baseline" if baseline else "new")
            cur.execute(
                """INSERT INTO discovered_postings (board_id, ats_posting_id, url, canonical_url, title, location, posted_at, state)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (board["id"], p.ats_posting_id, p.url, canon, p.title, p.location, p.posted_at, state),
            )
            if state == "new":
                new.append({"id": str(cur.fetchone()["id"]), "title": p.title, "location": p.location, "url": p.url,
                            "company": board["company"]})
        if listed and complete:  # an empty or truncated list says nothing about what has closed
            cur.execute(
                "UPDATE discovered_postings SET state = 'closed' WHERE board_id = %s AND state <> 'closed' "
                "AND NOT (ats_posting_id = ANY(%s))", (board["id"], seen_ids))
        cur.execute("UPDATE watched_boards SET last_polled_at = now(), last_success_at = now(), last_error = NULL, "
                    "last_listed = %s, last_new = %s WHERE id = %s", (len(listed), len(new), board["id"]))
    c.commit()
    return new


def record_poll_failure(c: psycopg.Connection, board_id: int, error: str) -> None:
    """A failed check still counts as an attempt, so the board waits its interval instead of retrying every tick."""
    with c.cursor() as cur:
        cur.execute("UPDATE watched_boards SET last_polled_at = now(), last_error = %s WHERE id = %s", (error[:500], board_id))
    c.commit()


PENDING_STATES = ("new", "seen")


def list_discovered(c: psycopg.Connection, states: tuple[str, ...] = PENDING_STATES, limit: int = 300) -> list[dict]:
    """Postings awaiting a decision, oldest first so the backlog gets cleared in order."""
    with c.cursor() as cur:
        cur.execute(
            """SELECT d.id, d.title, d.location, d.url, d.posted_at, d.first_seen_at, d.state, co.name AS company,
                      EXTRACT(day FROM now() - d.first_seen_at)::int AS age_days
               FROM discovered_postings d JOIN watched_boards b ON b.id = d.board_id JOIN companies co ON co.id = b.company_id
               WHERE d.state = ANY(%s) ORDER BY d.first_seen_at ASC, d.posted_at DESC NULLS LAST LIMIT %s""", (list(states), limit))
        return cur.fetchall()


def count_new_discovered(c: psycopg.Connection) -> int:
    with c.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM discovered_postings WHERE state = 'new'")
        return cur.fetchone()["n"]


def pending_summary(c: psycopg.Connection, overdue_days: int) -> dict:
    """What the reminder banner shows: how many postings still need a decision and how stale the oldest is."""
    with c.cursor() as cur:
        cur.execute(
            """SELECT count(*) FILTER (WHERE state = 'new') AS new,
                      count(*) FILTER (WHERE state = 'seen') AS seen,
                      count(*) FILTER (WHERE first_seen_at < now() - make_interval(days => %s)) AS overdue,
                      EXTRACT(day FROM now() - min(first_seen_at))::int AS oldest_days
               FROM discovered_postings WHERE state = ANY(%s)""", (overdue_days, list(PENDING_STATES)))
        r = cur.fetchone()
    return {"new": r["new"], "seen": r["seen"], "pending": r["new"] + r["seen"], "overdue": r["overdue"],
            "oldest_days": r["oldest_days"], "overdue_after_days": overdue_days}


def set_discovered_state(c: psycopg.Connection, disc_id: str, state: str, job_id: str | None = None) -> dict | None:
    if state not in ("new", "seen", "added", "not_interested"):
        raise ValueError(f"cannot set state {state!r} by hand")
    with c.cursor() as cur:
        cur.execute("UPDATE discovered_postings SET state = %s, job_id = COALESCE(%s, job_id) WHERE id = %s "
                    "RETURNING id, url, title", (state, job_id, disc_id))
        row = cur.fetchone()
    c.commit()
    return row


def mark_all_seen(c: psycopg.Connection) -> None:
    with c.cursor() as cur:
        cur.execute("UPDATE discovered_postings SET state = 'seen' WHERE state = 'new'")
    c.commit()


def purge_discovered(c: psycopg.Connection) -> dict:
    """Scheduled clean-up. 'added' rows are safe to drop (record_poll skips anything already tracked as a job);
    'closed' rows are gone from the board. 'not_interested' rows are NOT dropped here: while the posting is still
    open, deleting them would make the next poll surface it as new again. They are removed once they close."""
    with c.cursor() as cur:
        cur.execute("DELETE FROM discovered_postings WHERE state IN ('added','closed') RETURNING state")
        rows = cur.fetchall()
    c.commit()
    return {"added": sum(1 for r in rows if r["state"] == "added"), "closed": sum(1 for r in rows if r["state"] == "closed")}


# ───────────────────────────── llm cache ─────────────────────────────
def llm_cache_get(c: psycopg.Connection, task: str, input_hash: str) -> dict | None:
    with c.cursor() as cur:
        cur.execute("SELECT id, output FROM llm_calls WHERE task = %s AND input_hash = %s", (task, input_hash))
        return cur.fetchone()


def llm_cache_put(c: psycopg.Connection, *, task: str, input_hash: str, model: str, prompt_version: str, output: Any,
                  input_tokens: int | None, output_tokens: int | None, job_id: str | None = None) -> str:
    with c.cursor() as cur:
        cur.execute(
            """INSERT INTO llm_calls (job_id, task, input_hash, model, prompt_version, output, input_tokens, output_tokens)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (task, input_hash) DO UPDATE SET output = EXCLUDED.output RETURNING id""",
            (job_id, task, input_hash, model, prompt_version, Jsonb(output), input_tokens, output_tokens),
        )
        rid = str(cur.fetchone()["id"])
    c.commit()
    return rid


def json_default(o: Any) -> Any:
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    raise TypeError(type(o))


def dumps(o: Any) -> str:
    return json.dumps(o, default=json_default, ensure_ascii=False)
