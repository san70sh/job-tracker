"""Notion as a READ-ONLY mirror of the app.

The app is the only writer. Every change is queued in `notion_outbox` in the same database transaction as the
change itself, then delivered here with throttling and retries. Edits made inside Notion are never read back;
`reconcile` finds them and `--fix` overwrites them with the app's values.

Needs an internal integration token shared with the database:
  NOTION_TOKEN=secret_...   NOTION_DATA_SOURCE_ID=<id of the 'Job Tracker' data source>

Notion API limits this module is built around (developers.notion.com/reference/request-limits):
  * ~3 requests/second average per integration on non-Enterprise plans (180/min), HTTP 429 + Retry-After above it
  * 2000 characters per rich-text object, 100 elements per array, 1000 blocks / 500 KB per request
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import time
from datetime import date
from typing import Any

from . import db, repo
from .config import get_settings
from .urls import canonicalize

log = logging.getLogger("jobtracker.notion")
CHUNK = 1900  # stay under the 2000-character rich-text limit
MIN_INTERVAL = 0.4  # seconds between calls = 2.5 requests/second, under the 3/second average
MAX_RETRIES = 5


class NotionNotConfigured(Exception):
    pass


# ───────────────────────────── payload builders (pure, unit-tested) ─────────────────────────────
def rich_text(s: str | None) -> list[dict]:
    if not s:
        return []
    parts = [s[i:i + CHUNK] for i in range(0, len(s), CHUNK)]
    return [{"type": "text", "text": {"content": p}} for p in parts[:100]]


def build_properties(job: dict, technologies: list[str]) -> dict[str, Any]:
    return {
        "Role": {"title": rich_text(job["role"]) or [{"type": "text", "text": {"content": "(untitled)"}}]},
        "Company": {"rich_text": rich_text(job["company"])},
        "Job ID": {"rich_text": rich_text(job.get("job_ref"))},
        "Job Link": {"url": job.get("job_link") or None},
        "Level": {"rich_text": rich_text(job.get("level"))},
        "Location": {"rich_text": rich_text(job.get("location"))},
        "Experience Required": {"rich_text": rich_text(job.get("experience_required"))},
        "Team / Domain": {"rich_text": rich_text(job.get("team_domain"))},
        "Key Responsibilities": {"rich_text": rich_text(job.get("key_responsibilities"))},
        "Requirements": {"rich_text": rich_text(job.get("requirements"))},
        "Notes": {"rich_text": rich_text(job.get("notes"))},
        "Salary Details": {"rich_text": rich_text(job.get("salary_details"))},
        "Salary Sources": {"rich_text": rich_text(job.get("salary_sources"))},
        "Salary Min (LPA)": {"number": float(job["salary_min_lpa"]) if job.get("salary_min_lpa") is not None else None},
        "Salary Max (LPA)": {"number": float(job["salary_max_lpa"]) if job.get("salary_max_lpa") is not None else None},
        "Key Technologies": {"multi_select": [{"name": n.replace(",", " ")} for n in technologies]},
        "Work Mode": {"select": {"name": job["work_mode"]} if job.get("work_mode") else None},
        "Status": {"select": {"name": job["status"]} if job.get("status") else None},
        "Date Applied": {"date": {"start": job["date_applied"].isoformat()} if job.get("date_applied") else None},
    }


def build_children(sections: list[dict]) -> list[dict]:
    """Page body: one heading per section, bullets for lists, paragraphs for prose (first 100 blocks)."""
    titles = {"about_company": "About", "role": "The role", "responsibilities": "Key responsibilities",
              "requirements": "Qualifications", "nice_to_have": "Good to have", "benefits": "Benefits",
              "logistics": "Logistics"}
    blocks: list[dict] = []
    for s in sections:
        heading = titles.get(s["kind"], s["heading"])
        blocks.append({"object": "block", "type": "heading_2", "heading_2": {"rich_text": rich_text(heading[:200])}})
        for line in (s["body_md"] or "").split("\n"):
            line = line.strip()
            if not line:
                continue
            if line.startswith("- "):
                blocks.append({"object": "block", "type": "bulleted_list_item",
                               "bulleted_list_item": {"rich_text": rich_text(line[2:])}})
            else:
                blocks.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich_text(line)}})
    return blocks[:100]


def content_hash(props: dict, children: list[dict]) -> str:
    return hashlib.sha256(json.dumps([props, children], sort_keys=True, default=str).encode()).hexdigest()


# ───────────────────────────── throttled, retrying API calls ─────────────────────────────
_last_call = 0.0


def _throttle() -> None:
    global _last_call
    wait = _last_call + MIN_INTERVAL - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()


def _retry_after(err: Exception) -> float | None:
    headers = getattr(err, "headers", None) or {}
    try:
        return float(headers.get("Retry-After") or headers.get("retry-after"))
    except (TypeError, ValueError):
        return None


def call(fn, /, **kwargs):
    """Run one Notion API call: spaced out, and on 429/5xx wait (Retry-After if given) and retry with jitter."""
    for attempt in range(MAX_RETRIES):
        _throttle()
        try:
            return fn(**kwargs)
        except Exception as e:  # noqa: BLE001
            status = getattr(e, "status", None)
            if status == 429 or (isinstance(status, int) and status >= 500):
                delay = _retry_after(e) or min(30.0, 2.0 ** attempt)
                delay += random.uniform(0, 0.5)
                log.warning("notion %s, retrying in %.1fs (attempt %d/%d)", status, delay, attempt + 1, MAX_RETRIES)
                time.sleep(delay)
                continue
            raise
    raise RuntimeError(f"notion still failing after {MAX_RETRIES} attempts")


def _client():
    s = get_settings()
    if not (s.notion_token and s.notion_data_source_id):
        raise NotionNotConfigured("set NOTION_TOKEN and NOTION_DATA_SOURCE_ID in .env")
    from notion_client import Client

    return Client(auth=s.notion_token), s.notion_data_source_id


def configured() -> bool:
    s = get_settings()
    return bool(s.notion_token and s.notion_data_source_id)


# ───────────────────────────── push ─────────────────────────────
def push_job(job_id: str, client: Any = None, data_source_id: str | None = None) -> str | None:
    """Create or update the Notion page for one job. Returns the page id (None if the job is not applied-to)."""
    if client is None:
        client, data_source_id = _client()
    with db.conn() as c:
        job = repo.get_job(c, job_id)
        if job is None or job["stage"] != "Applied":
            return None
        props = build_properties(job, [t["name"] for t in job["technologies"]])
        children = build_children(job["sections"])
        h = content_hash(props, children)
        with c.cursor() as cur:
            cur.execute("SELECT last_local_hash FROM notion_sync WHERE job_id = %s", (job_id,))
            prev = cur.fetchone()
        page_id = job.get("notion_page_id")
        if page_id and prev and prev["last_local_hash"] == h:
            return page_id  # nothing changed since the last successful push
        if page_id:
            call(client.pages.update, page_id=page_id, properties=props)  # body is written once, at creation
        else:
            page = call(client.pages.create, parent={"type": "data_source_id", "data_source_id": data_source_id},
                        properties=props, children=children)
            page_id = page["id"]
        with c.cursor() as cur:
            cur.execute("UPDATE jobs SET notion_page_id = %s WHERE id = %s", (page_id, job_id))
            cur.execute(
                """INSERT INTO notion_sync (job_id, notion_page_id, last_pushed_at, last_local_hash)
                   VALUES (%s,%s,now(),%s) ON CONFLICT (job_id) DO UPDATE
                   SET notion_page_id = EXCLUDED.notion_page_id, last_pushed_at = now(), last_local_hash = EXCLUDED.last_local_hash""",
                (job_id, page_id, h))
        c.commit()
    return page_id


def archive_page(page_id: str, client: Any = None) -> None:
    """Move a page to Notion's trash (recoverable there for 30 days)."""
    if client is None:
        client, _ = _client()
    call(client.pages.update, page_id=page_id, in_trash=True)


def process_outbox(limit: int = 50) -> dict:
    """Deliver due outbox rows. Safe to call repeatedly; a failure only delays that row (with backoff)."""
    if not configured():
        return {"skipped": "notion not configured"}
    client, ds = _client()
    with db.conn() as c:
        rows = repo.due_outbox(c, limit)
    done = failed = 0
    for r in rows:
        try:
            if r["op"] == "upsert":
                push_job(str(r["job_id"]), client, ds)
            else:
                archive_page(r["notion_page_id"], client)
            with db.conn() as c:
                repo.outbox_done(c, r["id"])
            done += 1
        except Exception as e:  # noqa: BLE001
            log.error("notion outbox %s (%s) failed: %s", r["id"], r["op"], e)
            with db.conn() as c:
                repo.outbox_fail(c, r["id"], f"{type(e).__name__}: {e}")
            failed += 1
    return {"delivered": done, "failed": failed}


def push_all(include_inbox: bool = False) -> str:
    """Queue every applied job for a push (used for the first full sync) and deliver the queue."""
    with db.conn() as c, c.cursor() as cur:
        cur.execute("SELECT id FROM jobs WHERE (%s OR stage = 'Applied')", (include_inbox,))
        ids = [str(r["id"]) for r in cur.fetchall()]
    with db.conn() as c:
        for jid in ids:
            repo.enqueue_notion(c, jid)
    total = {"delivered": 0, "failed": 0}
    while True:
        res = process_outbox(200)
        if "skipped" in res:
            return res["skipped"]
        total["delivered"] += res["delivered"]
        total["failed"] += res["failed"]
        if not res["delivered"] and not res["failed"]:
            break
        if res["failed"]:
            break  # failed rows are backed off; do not spin
    return f"queued {len(ids)}, delivered {total['delivered']}, failed {total['failed']} (failures retry automatically)"


# ───────────────────────────── reading Notion (import + reconcile) ─────────────────────────────
def _plain(prop: dict | None) -> Any:
    if not prop:
        return None
    t = prop.get("type")
    items = prop.get(t) if t else None
    if t in ("title", "rich_text"):
        return "".join(x.get("plain_text", "") for x in items or []) or None
    if t in ("url", "number"):
        return items
    if t == "select":
        return items["name"] if items else None
    if t == "date":
        return items["start"] if items else None
    if t == "multi_select":
        return [x["name"] for x in items or []]
    return None


def map_page(page: dict) -> dict:
    pr = page["properties"]
    g = lambda name: _plain(pr.get(name))  # noqa: E731
    return {
        "notion_page_id": page["id"], "in_trash": page.get("in_trash") or page.get("archived") or False,
        "role": g("Role"), "company": g("Company"), "status": g("Status"),
        "date_applied": date.fromisoformat(g("Date Applied")[:10]) if g("Date Applied") else None,
        "job_ref": g("Job ID"), "job_link": g("Job Link"), "level": g("Level"), "location": g("Location"),
        "work_mode": g("Work Mode"), "experience_required": g("Experience Required"), "team_domain": g("Team / Domain"),
        "key_responsibilities": g("Key Responsibilities"), "requirements": g("Requirements"), "notes": g("Notes"),
        "salary_min_lpa": g("Salary Min (LPA)"), "salary_max_lpa": g("Salary Max (LPA)"),
        "salary_details": g("Salary Details"), "salary_sources": g("Salary Sources"),
        "technologies": g("Key Technologies") or [],
    }


def fetch_all_pages(client: Any, data_source_id: str) -> list[dict]:
    pages: list[dict] = []
    cursor = None
    while True:
        kw: dict[str, Any] = {"data_source_id": data_source_id, "page_size": 100}
        if cursor:
            kw["start_cursor"] = cursor
        res = call(client.data_sources.query, **kw)
        pages += res["results"]
        if not res.get("has_more"):
            return pages
        cursor = res["next_cursor"]


def import_from_notion(dry_run: bool = False) -> str:
    """One-time migration of existing Notion rows into the app (existing rows are skipped, never updated)."""
    client, ds = _client()
    rows = [map_page(p) for p in fetch_all_pages(client, ds)]
    if dry_run:
        return f"would import {len(rows)} row(s)"
    created = skipped = 0
    with db.conn() as c:
        for r in rows:
            canonical = canonicalize(r["job_link"]) if r["job_link"] else f"notion://{r['notion_page_id']}"
            if repo.find_job_by_url(c, canonical):
                skipped += 1
                continue
            repo.import_job(c, r, canonical)
            created += 1
    return f"imported {created}, skipped {skipped} already present"


_COMPARE = ["role", "company", "status", "date_applied", "job_ref", "job_link", "level", "location", "work_mode",
            "experience_required", "team_domain", "key_responsibilities", "requirements", "notes", "salary_min_lpa",
            "salary_max_lpa", "salary_details", "salary_sources"]


def _norm(v: Any) -> Any:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)) or hasattr(v, "quantize"):
        return round(float(v), 2)
    if isinstance(v, str):
        return " ".join(v.split())
    return v


def diff_job(app_job: dict, notion_row: dict) -> list[str]:
    """Names of fields whose value in Notion differs from the app (the app is the source of truth)."""
    bad = [f for f in _COMPARE if _norm(app_job.get(f)) != _norm(notion_row.get(f))]
    if sorted(t["name"].replace(",", " ") for t in app_job["technologies"]) != sorted(notion_row["technologies"]):
        bad.append("technologies")
    return bad


def reconcile(fix: bool = False) -> dict:
    """Compare Notion with the app. Reports (and with fix=True repairs) drift, missing pages and duplicates.

    * drift      a field in Notion was edited by hand            -> re-push, overwriting Notion
    * missing    applied job has no page (or its page vanished)  -> re-create
    * orphans    Notion pages the app does not know about        -> reported only, never deleted
    * duplicates several Notion pages for one Job Link           -> reported only
    """
    client, ds = _client()
    pages = {p["id"]: map_page(p) for p in fetch_all_pages(client, ds)}
    pages = {k: v for k, v in pages.items() if not v["in_trash"]}
    report: dict[str, Any] = {"notion_pages": len(pages), "drift": [], "missing": [], "orphans": [], "duplicates": []}

    with db.conn() as c:
        with c.cursor() as cur:
            cur.execute("SELECT id, notion_page_id FROM jobs WHERE stage = 'Applied'")
            jobs = cur.fetchall()
        linked: set[str] = set()
        for j in jobs:
            job = repo.get_job(c, str(j["id"]))
            page_id = j["notion_page_id"]
            if not page_id or page_id not in pages:
                report["missing"].append(job["role"])
                if fix:
                    with c.cursor() as cur:
                        cur.execute("UPDATE jobs SET notion_page_id = NULL WHERE id = %s", (j["id"],))
                        cur.execute("DELETE FROM notion_sync WHERE job_id = %s", (j["id"],))
                    c.commit()
                    repo.enqueue_notion(c, str(j["id"]))
                continue
            linked.add(page_id)
            bad = diff_job(job, pages[page_id])
            if bad:
                report["drift"].append({"job": job["role"], "fields": bad})
                if fix:
                    with c.cursor() as cur:
                        cur.execute("DELETE FROM notion_sync WHERE job_id = %s", (j["id"],))  # forget the hash -> forces a push
                        cur.execute("INSERT INTO notion_sync (job_id, notion_page_id) VALUES (%s,%s)", (j["id"], page_id))
                    c.commit()
                    repo.enqueue_notion(c, str(j["id"]))
    report["orphans"] = [p["role"] or p["notion_page_id"] for pid, p in pages.items() if pid not in linked]
    by_link: dict[str, list[str]] = {}
    for p in pages.values():
        if p["job_link"]:
            by_link.setdefault(canonicalize(p["job_link"]), []).append(p["role"] or p["notion_page_id"])
    report["duplicates"] = [v for v in by_link.values() if len(v) > 1]
    if fix:
        report["delivery"] = process_outbox(500)
    return report
