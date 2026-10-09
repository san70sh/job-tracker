"""Database-backed behaviour: discovery lifecycle, purge safety, Notion outbox, review flag.

Connects with the DATABASE_URL from .env (same server, user and password as the app), creates a throwaway database
named `jobtracker_test` next to the real one, runs the tests there, and drops it afterwards. The real database is
never touched. The role needs the CREATEDB permission. Skipped if the server cannot be reached.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

from jobtracker import db, migrate, notion_sync, repo
from jobtracker.config import get_settings
from jobtracker.models import ListedBatch, ListedPosting

TEST_DB = "jobtracker_test"  # fixed on purpose: this is the only database the tests are allowed to drop


def _conninfo(dbname: str) -> str:
    """The app's connection settings, pointed at another database on the same server."""
    return make_conninfo(get_settings().database_url, dbname=dbname)


@pytest.fixture(scope="module")
def database():
    configured = conninfo_to_dict(get_settings().database_url).get("dbname")
    if configured == TEST_DB:
        pytest.fail(f"DATABASE_URL points at {TEST_DB}, which these tests drop and recreate. Point it at the real database.")
    try:
        admin = psycopg.connect(_conninfo("postgres"), autocommit=True, connect_timeout=5)
    except psycopg.OperationalError as e:
        pytest.skip(f"cannot reach the database server with DATABASE_URL from .env: {str(e).splitlines()[0]}")
    try:
        admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        admin.execute(f"CREATE DATABASE {TEST_DB}")
    except psycopg.errors.InsufficientPrivilege:
        pytest.fail("the database role needs CREATEDB to run these tests: ALTER ROLE <your role> CREATEDB;")
    finally:
        admin.close()
    c = psycopg.connect(_conninfo(TEST_DB), row_factory=dict_row)
    try:
        migrate.run(c)  # the same path a real install takes: baseline, then every migration
        repo.seed_vocab(c)
        yield c
    finally:
        c.close()
        cleanup = psycopg.connect(_conninfo("postgres"), autocommit=True, connect_timeout=5)
        try:
            cleanup.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        finally:
            cleanup.close()


@pytest.fixture
def c(database):
    """Clean tables before every test."""
    database.rollback()
    database.execute("TRUNCATE jobs, companies, watched_boards, notion_outbox CASCADE")
    database.commit()
    return database


def board(c, **kw):
    from jobtracker.models import BoardRef

    bid, _ = repo.add_board(c, "Acme", BoardRef("greenhouse", "acme"), f"https://boards.greenhouse.io/acme?{kw.pop('tag', 'x')}", **kw)
    return next(b for b in repo.list_boards(c) if b["id"] == bid)


def lp(i, title="Backend Engineer"):
    return ListedPosting(ats_posting_id=str(i), url=f"https://boards.greenhouse.io/acme/jobs/{i}", title=title, location="Bengaluru")


def states(c):
    with c.cursor() as cur:
        cur.execute("SELECT ats_posting_id, state FROM discovered_postings ORDER BY ats_posting_id")
        return {r["ats_posting_id"]: r["state"] for r in cur.fetchall()}


def make_job(c, status="Applied", url="https://x.example/jobs/1", page_id=None, ref="R1", company="Acme"):
    jid = repo.import_job(c, {
        "notion_page_id": page_id or f"import-{url}", "role": "Engineer", "company": company, "status": status,
        "date_applied": date(2026, 10, 1), "job_ref": ref, "job_link": url, "level": None, "location": "Pune",
        "work_mode": None, "experience_required": None, "team_domain": None, "key_responsibilities": "• a",
        "requirements": "• b", "notes": None, "salary_min_lpa": None, "salary_max_lpa": None, "salary_details": None,
        "salary_sources": None, "technologies": ["Java"],
    }, url)
    with c.cursor() as cur:
        cur.execute("UPDATE jobs SET notion_page_id = %s WHERE id = %s", (page_id, jid))
        cur.execute("DELETE FROM notion_outbox")
    c.commit()
    return jid


# ───────────────────────────── discovery lifecycle ─────────────────────────────
def test_first_check_is_silent_then_new_postings_surface(c):
    b = board(c)
    assert repo.record_poll(c, b, ListedBatch([lp(1), lp(2)])) == []
    assert states(c) == {"1": "baseline", "2": "baseline"}
    b = next(x for x in repo.list_boards(c))
    new = repo.record_poll(c, b, ListedBatch([lp(1), lp(2), lp(3)]))
    assert [n["title"] for n in new] == ["Backend Engineer"] and states(c)["3"] == "new"
    assert repo.record_poll(c, b, ListedBatch([lp(1), lp(2), lp(3)])) == []  # not announced twice


def test_filters_make_matching_postings_new_immediately_and_others_silent(c):
    b = board(c, title_include=["backend"], title_exclude=["intern"])
    new = repo.record_poll(c, b, ListedBatch([lp(1, "Backend Engineer"), lp(2, "Backend Intern"), lp(3, "Designer")]))
    assert [n["id"] for n in new] and states(c) == {"1": "new", "2": "filtered", "3": "filtered"}


def test_postings_older_than_the_age_limit_are_not_announced(c):
    from datetime import timedelta

    from jobtracker import board_options

    limit, today = board_options.max_age_days(), date.today()
    b = board(c, title_include=["backend"])
    dated = lambda i, days: lp(i).model_copy(update={"posted_at": None if days is None else today - timedelta(days=days)})
    new = repo.record_poll(c, b, ListedBatch([dated(1, 0), dated(2, limit), dated(3, limit + 1), dated(4, None)]))
    assert sorted(n["url"][-1] for n in new) == ["1", "2", "4"]             # the limit day itself counts; no date = kept
    assert states(c) == {"1": "new", "2": "new", "3": "filtered", "4": "new"}


def test_editing_filters_never_brings_back_a_posting_past_the_age_limit(c):
    from datetime import timedelta

    from jobtracker import board_options, boards

    old = date.today() - timedelta(days=board_options.max_age_days() + 5)
    bid, _ = _gh(c, "Acme", "acme", title_include=["backend"])
    repo.record_poll(c, _row(c, bid), ListedBatch([lp(1).model_copy(update={"posted_at": old})]))
    assert states(c) == {"1": "filtered"}
    boards.update(c, bid, {"title_include": ["backend", "engineer"]})      # still stale: stays out of the Inbox
    assert states(c) == {"1": "filtered"}


def test_closing_only_from_a_complete_listing(c):
    b = board(c)
    repo.record_poll(c, b, ListedBatch([lp(1), lp(2)]))
    b = repo.list_boards(c)[0]
    repo.record_poll(c, b, ListedBatch([lp(1)], complete=False))  # page cap hit: absence proves nothing
    assert states(c) == {"1": "baseline", "2": "baseline"}
    repo.record_poll(c, b, ListedBatch([lp(1)], complete=True))
    assert states(c) == {"1": "baseline", "2": "closed"}
    repo.record_poll(c, b, ListedBatch([]))  # empty = probably an API hiccup, must not close everything
    assert states(c)["1"] == "baseline"


def test_purge_removes_added_and_closed_but_keeps_not_interested_until_it_closes(c):
    b = board(c)
    repo.record_poll(c, b, ListedBatch([lp(1)]))
    b = repo.list_boards(c)[0]
    repo.record_poll(c, b, ListedBatch([lp(1), lp(2), lp(3), lp(4)]))
    with c.cursor() as cur:
        cur.execute("SELECT id, ats_posting_id FROM discovered_postings")
        ids = {r["ats_posting_id"]: str(r["id"]) for r in cur.fetchall()}
    repo.set_discovered_state(c, ids["2"], "added")
    repo.set_discovered_state(c, ids["3"], "not_interested")
    # 4 stays 'new'
    assert repo.purge_discovered(c) == {"added": 1, "closed": 0}
    assert states(c) == {"1": "baseline", "3": "not_interested", "4": "new"}
    # the rejected posting is still open, so a poll must not resurrect it as new
    assert repo.record_poll(c, b, ListedBatch([lp(1), lp(3), lp(4)])) == []
    assert states(c)["3"] == "not_interested"
    # once it leaves the board it closes and the next purge removes it
    repo.record_poll(c, b, ListedBatch([lp(1), lp(4)]))
    assert repo.purge_discovered(c) == {"added": 0, "closed": 1} and "3" not in states(c)


def test_tracked_postings_are_not_resurfaced_after_their_row_is_purged(c):
    b = board(c)
    repo.record_poll(c, b, ListedBatch([lp(1)]))
    b = repo.list_boards(c)[0]
    make_job(c, url="https://boards.greenhouse.io/acme/jobs/9")
    assert repo.record_poll(c, b, ListedBatch([lp(1), lp(9)])) == [] and "9" not in states(c)


def test_pending_summary_counts_overdue(c):
    b = board(c)
    repo.record_poll(c, b, ListedBatch([lp(1)]))
    b = repo.list_boards(c)[0]
    repo.record_poll(c, b, ListedBatch([lp(1), lp(2), lp(3)]))
    with c.cursor() as cur:
        cur.execute("UPDATE discovered_postings SET first_seen_at = now() - interval '5 days' WHERE ats_posting_id = '2'")
        cur.execute("UPDATE discovered_postings SET state = 'seen' WHERE ats_posting_id = '2'")
    c.commit()
    s = repo.pending_summary(c, overdue_days=2)
    assert (s["new"], s["seen"], s["pending"], s["overdue"], s["oldest_days"]) == (1, 1, 2, 1, 5)
    assert [d["ats_posting_id"] if "ats_posting_id" in d else d["title"] for d in repo.list_discovered(c)][0]  # oldest first
    assert repo.list_discovered(c)[0]["age_days"] == 5


# ───────────────────────────── review flag ─────────────────────────────
def test_needs_review_clears_when_required_fields_are_filled(c):
    jid = make_job(c, status="Applied")
    with c.cursor() as cur:
        cur.execute("UPDATE jobs SET needs_review = true, requirements = NULL WHERE id = %s", (jid,))
    c.commit()
    repo.update_job(c, jid, notes="just a note")
    assert repo.get_job(c, jid)["needs_review"] is True  # requirements still empty
    repo.update_job(c, jid, requirements="• Java")
    assert repo.get_job(c, jid)["needs_review"] is False


def test_a_flagged_job_names_the_fields_to_check_and_each_clears_when_you_settle_it(c):
    jid = make_job(c, status="Applied")
    with c.cursor() as cur:
        cur.execute("UPDATE jobs SET needs_review = true, requirements = NULL WHERE id = %s", (jid,))
        cur.execute("""INSERT INTO field_provenance (job_id, field, method, confidence) VALUES (%s,'key_responsibilities','rules',0.55)
                       ON CONFLICT (job_id, field) DO UPDATE SET method='rules', confidence=0.55""", (jid,))
    c.commit()
    review = {r["field"]: r for r in repo.get_job(c, jid)["review"]}
    assert {f: r["reason"] for f, r in review.items()} == {"key_responsibilities": "not_sure", "requirements": "not_found"}
    assert (review["key_responsibilities"]["method"], review["key_responsibilities"]["confidence"]) == ("rules", 0.55)
    repo.update_job(c, jid, notes="an unrelated edit")  # does not settle either field
    assert repo.get_job(c, jid)["needs_review"] is True
    repo.update_job(c, jid, requirements="• Java")      # one settled: the other is still listed
    job = repo.get_job(c, jid)
    assert job["needs_review"] is True and [r["field"] for r in job["review"]] == ["key_responsibilities"]
    repo.update_job(c, jid, key_responsibilities="• Build services")
    job = repo.get_job(c, jid)
    assert job["needs_review"] is False and job["review"] == []


def test_marking_a_job_reviewed_is_not_undone_by_a_later_edit(c):
    jid = make_job(c, status="Applied")
    with c.cursor() as cur:
        cur.execute("UPDATE jobs SET needs_review = true WHERE id = %s", (jid,))
        cur.execute("""INSERT INTO field_provenance (job_id, field, method, confidence) VALUES (%s,'requirements','rules',0.5)
                       ON CONFLICT (job_id, field) DO UPDATE SET method='rules', confidence=0.5""", (jid,))
    c.commit()
    assert [r["field"] for r in repo.get_job(c, jid)["review"]] == ["requirements"]
    repo.mark_reviewed(c, jid)                         # "it is fine as it is"
    repo.update_job(c, jid, notes="later note")
    job = repo.get_job(c, jid)
    assert job["needs_review"] is False and job["review"] == []


# ───────────────────────────── notion outbox ─────────────────────────────
def outbox(c):
    with c.cursor() as cur:
        cur.execute("SELECT op, status, attempts, notion_page_id FROM notion_outbox ORDER BY id")
        return cur.fetchall()


def test_edits_coalesce_into_one_pending_push_and_only_for_applied_jobs(c):
    jid = make_job(c)
    repo.update_job(c, jid, notes="a")
    repo.update_job(c, jid, notes="b")
    repo.set_status(c, jid, "Interviewing")
    assert [(r["op"], r["status"]) for r in outbox(c)] == [("upsert", "pending")]
    inbox = repo.import_job(c, {**{k: None for k in ("level", "work_mode", "experience_required", "team_domain", "notes",
        "salary_min_lpa", "salary_max_lpa", "salary_details", "salary_sources", "key_responsibilities", "requirements")},
        "notion_page_id": "p2", "role": "X", "company": "Acme", "status": None, "date_applied": None, "job_ref": "R2",
        "job_link": "https://x.example/2", "location": None, "technologies": []}, "https://x.example/2")
    with c.cursor() as cur:
        cur.execute("DELETE FROM notion_outbox")
    c.commit()
    repo.update_job(c, inbox, notes="not applied yet")
    assert outbox(c) == []  # Notion only mirrors applied jobs


def test_deleting_a_job_queues_the_page_for_archiving(c):
    jid = make_job(c, page_id="page-xyz")
    repo.delete_job(c, jid)
    assert [(r["op"], r["notion_page_id"]) for r in outbox(c)] == [("archive", "page-xyz")]


def test_failed_pushes_back_off_then_park_as_failed(c):
    jid = make_job(c)
    repo.enqueue_notion(c, jid)
    oid = repo.due_outbox(c)[0]["id"]
    for _ in range(8):
        repo.outbox_fail(c, oid, "boom", max_attempts=8)
    row = outbox(c)[0]
    assert (row["status"], row["attempts"]) == ("failed", 8)
    assert repo.due_outbox(c) == []
    st = repo.outbox_status(c)
    assert st["failed"] == 1 and st["last_error"] == "boom"
    assert repo.retry_failed_outbox(c) == 1 and len(repo.due_outbox(c)) == 1


class FakeNotion:
    def __init__(self, fail_first=0):
        self.created, self.updated, self.trashed, self._fail = [], [], [], fail_first
        outer = self

        class Pages:
            def create(self, **kw):
                if outer._fail:
                    outer._fail -= 1
                    err = RuntimeError("rate limited")
                    err.status = 429  # type: ignore[attr-defined]
                    err.headers = {"Retry-After": "0"}  # type: ignore[attr-defined]
                    raise err
                outer.created.append(kw)
                return {"id": "page-new"}

            def update(self, **kw):
                (outer.trashed if kw.get("in_trash") else outer.updated).append(kw)

        self.pages = Pages()


@pytest.fixture
def notion(monkeypatch, c):
    @contextmanager
    def conn():
        yield c

    monkeypatch.setattr(db, "conn", conn)
    monkeypatch.setattr(notion_sync, "MIN_INTERVAL", 0)
    monkeypatch.setattr(notion_sync.time, "sleep", lambda s: None)
    monkeypatch.setattr(notion_sync, "configured", lambda: True)
    fake = FakeNotion()
    monkeypatch.setattr(notion_sync, "_client", lambda: (fake, "ds-1"))
    return fake


def test_outbox_delivers_creates_then_skips_unchanged_then_updates(c, notion):
    jid = make_job(c, page_id=None)
    repo.enqueue_notion(c, jid)
    assert notion_sync.process_outbox() == {"delivered": 1, "failed": 0}
    assert len(notion.created) == 1 and notion.created[0]["parent"]["data_source_id"] == "ds-1"
    assert repo.get_job(c, jid)["notion_page_id"] == "page-new"
    repo.enqueue_notion(c, jid)
    notion_sync.process_outbox()
    assert notion.updated == []  # identical content: nothing sent
    repo.update_job(c, jid, notes="changed")
    notion_sync.process_outbox()
    assert len(notion.updated) == 1 and notion.updated[0]["page_id"] == "page-new"


def test_outbox_retries_a_429_and_archives_deleted_jobs(c, notion):
    notion._fail = 2
    jid = make_job(c, page_id=None)
    repo.enqueue_notion(c, jid)
    assert notion_sync.process_outbox() == {"delivered": 1, "failed": 0}  # two 429s absorbed inside call()
    repo.delete_job(c, jid)
    assert notion_sync.process_outbox() == {"delivered": 1, "failed": 0}
    assert notion.trashed and notion.trashed[0]["page_id"] == "page-new"


# ───────────────────────────── notion payloads and drift ─────────────────────────────
def test_rich_text_is_chunked_under_the_2000_char_limit():
    parts = notion_sync.rich_text("x" * 4500)
    assert [len(p["text"]["content"]) for p in parts] == [1900, 1900, 700]
    assert notion_sync.rich_text(None) == []


def test_diff_job_finds_hand_edits_in_notion(c):
    jid = make_job(c)
    job = repo.get_job(c, jid)
    in_notion = {f: job.get(f) for f in notion_sync._COMPARE}
    in_notion["technologies"] = ["Java"]
    assert notion_sync.diff_job(job, in_notion) == []
    in_notion["status"], in_notion["notes"] = "Offer", "edited in notion"
    assert set(notion_sync.diff_job(job, in_notion)) == {"status", "notes"}


def provenance(c, job_id):
    with c.cursor() as cur:
        cur.execute("SELECT field, method FROM field_provenance WHERE job_id = %s", (job_id,))
        return {r["field"]: r["method"] for r in cur.fetchall()}


def company_names(c):
    with c.cursor() as cur:
        cur.execute("SELECT name FROM companies ORDER BY name")
        return [r["name"] for r in cur.fetchall()]


def test_update_only_writes_what_actually_changed(c):
    jid = make_job(c)
    # a whole form submitted with nothing changed: no write, no 'manual' stamp, no Notion push
    assert repo.update_job(c, jid, notes=None, team_domain=None, location="Pune", salary_min_lpa=None) == []
    assert provenance(c, jid) == {} and outbox(c) == []
    assert repo.update_job(c, jid, notes="x", location="Pune") == ["notes"]
    assert provenance(c, jid) == {"notes": "manual"}
    assert [(r["op"], r["status"]) for r in outbox(c)] == [("upsert", "pending")]
    repo.update_job(c, jid, salary_min_lpa=48)
    assert repo.update_job(c, jid, salary_min_lpa=48.0) == []  # 48 and Decimal('48.00') are the same value


def test_company_can_be_renamed_and_the_unused_old_one_is_removed(c):
    jid = make_job(c, company="Rbs")
    assert repo.update_job(c, jid, company="NatWest Group") == ["company"]
    assert repo.get_job(c, jid)["company"] == "NatWest Group"
    assert company_names(c) == ["NatWest Group"]
    assert provenance(c, jid) == {"company": "manual"}
    assert len(outbox(c)) == 1  # the new name goes to Notion
    with pytest.raises(ValueError):
        repo.update_job(c, jid, company="   ")


def test_old_company_is_kept_while_another_job_uses_it(c):
    a = make_job(c, url="https://x.example/jobs/a", ref="A1", company="Rbs")
    make_job(c, url="https://x.example/jobs/b", ref="B1", company="Rbs")
    repo.update_job(c, a, company="NatWest Group")
    assert company_names(c) == ["NatWest Group", "Rbs"]


def test_renaming_into_a_company_that_already_has_that_job_id_is_refused(c):
    make_job(c, url="https://x.example/jobs/a", ref="R1", company="Acme")
    b = make_job(c, url="https://x.example/jobs/b", ref="R1", company="Beta")
    with pytest.raises(psycopg.errors.UniqueViolation):
        repo.update_job(c, b, company="Acme")
    c.rollback()
    assert repo.get_job(c, b)["company"] == "Beta" and "Beta" in company_names(c)


def test_backfill_fills_numeric_experience_from_the_text(c):
    a = make_job(c, url="https://x.example/jobs/a", ref="A1")
    b = make_job(c, url="https://x.example/jobs/b", ref="B1")
    c.execute("UPDATE jobs SET experience_required = %s WHERE id = %s", ("3–5 years", a))
    # a guess about other postings must not become this job's requirement
    c.execute("UPDATE jobs SET experience_required = %s WHERE id = %s", ("Not stated (AVP typically ~8–12 yrs)", b))
    c.commit()
    dry = repo.backfill_experience(c, dry_run=True)
    assert [(r["min"], r["max"]) for r in dry] == [(3.0, 5.0)]
    assert repo.get_job(c, a)["experience_min_years"] is None  # a dry run writes nothing
    repo.backfill_experience(c)
    job = repo.get_job(c, a)
    assert (float(job["experience_min_years"]), float(job["experience_max_years"])) == (3.0, 5.0)
    assert provenance(c, a)["experience_min_years"] == "rules"
    assert repo.get_job(c, b)["experience_min_years"] is None
    assert repo.backfill_experience(c) == []  # nothing left to do


def test_backfill_team_fills_blanks_only_and_never_overwrites(c):
    a = make_job(c, url="https://x.example/jobs/a", ref="A1")  # blank team, text names one
    b = make_job(c, url="https://x.example/jobs/b", ref="B1")  # already has a team
    n = make_job(c, url="https://x.example/jobs/n", ref="N1")  # blank, nothing to infer
    c.execute("UPDATE jobs SET team_domain = 'Claude wrote this' WHERE id = %s", (b,))
    for jid, body in ((a, "Join the Ledger Platform team."), (b, "Join the Other Team team."), (n, "We build things.")):
        c.execute("INSERT INTO job_sections (job_id, kind, heading, body_md, position) VALUES (%s,'role','The role',%s,0)", (jid, body))
    c.commit()
    dry = repo.backfill_team(c, dry_run=True)
    assert [r["value"] for r in dry] == ["Ledger Platform"]
    assert repo.get_job(c, a)["team_domain"] is None  # a dry run writes nothing
    repo.backfill_team(c)
    assert repo.get_job(c, a)["team_domain"] == "Ledger Platform"
    assert provenance(c, a)["team_domain"] == "rules"
    assert repo.get_job(c, b)["team_domain"] == "Claude wrote this"
    assert repo.get_job(c, n)["team_domain"] is None
    assert repo.backfill_team(c) == []


PASTE = """Software Engineer II
Acme Payments
Bengaluru, Karnataka, India (Hybrid) · 2 weeks ago · Over 100 applicants
About the job
Join the Ledger Platform team and help us scale settlement systems.
Responsibilities
- Design and build backend services in Java and Spring Boot
- Own Kafka based event pipelines
Requirements
- 3-5 years of experience in backend development
- Experience with PostgreSQL
https://www.linkedin.com/jobs/view/4472684826/?trackingId=abc
"""


class NoLLM:
    available = False


@pytest.fixture
def pasted(monkeypatch, c):
    @contextmanager
    def conn():
        yield c

    monkeypatch.setattr(db, "conn", conn)
    return c


def _fields(**kw):
    return {"company": "Acme Payments", "role": "Software Engineer II", **kw}


def test_pasted_text_is_saved_with_provenance_and_dedupes(pasted):
    import asyncio

    from jobtracker import pipeline

    d = asyncio.run(pipeline.draft_from_text(PASTE))
    assert d["fields"]["company"]["value"] == "Acme Payments" and d["fields"]["job_ref"]["value"] == "4472684826"
    assert d["duplicate"] is None and d["counts"]["required"] == 2

    res = asyncio.run(pipeline.ingest_text(PASTE, _fields(level="Mid-level", team_domain="Ledger Platform — Payments"), llm=NoLLM()))
    assert res.status == "created"
    job = repo.get_job(pasted, res.job_id)
    assert job["company"] == "Acme Payments" and job["ats"] == "manual" and job["stage"] == "Inbox"
    assert job["canonical_url"] == "https://linkedin.com/jobs/view/4472684826"
    assert job["job_ref"] == "4472684826" and job["work_mode"] == "Hybrid" and "Java" in {t["name"] for t in job["technologies"]}
    prov = provenance(pasted, res.job_id)
    assert prov["role"] == "manual" and prov["company"] == "manual"          # confirmed in the preview
    assert prov["level"] == "manual"                                        # the value was changed
    assert prov["work_mode"] == "rules"                                     # left as the rules found it
    assert prov["experience_required"] == "rules"

    # the same link again, in another form, and the same posting text again, are both caught
    again = asyncio.run(pipeline.ingest_text(PASTE.replace("view/4472684826/?trackingId=abc", "view/some-title-4472684826"), _fields(), llm=NoLLM()))
    assert again.status == "duplicate" and again.job_id == res.job_id
    assert asyncio.run(pipeline.draft_from_text(PASTE))["duplicate"]["id"] == res.job_id


CARD = """Senior Software Engineer
Acme Payments
Pune, Maharashtra, India · 2 days ago · Over 100 applicants
On-site
Full-time
About the job
A short LinkedIn blurb about the role, much poorer than the page on the company site.
Responsibilities
- Something vague
"""
SITE_URL = "https://boards.greenhouse.io/acme/jobs/9001"
LI_LINK = "https://www.linkedin.com/jobs/view/4476129432/"
HINTS = {"title": "Senior Software Engineer", "company": "Acme Payments", "confidence": 0.9, "job_ref": "4476129432"}


def _site_posting():
    from jobtracker.models import Posting

    html = ("<h3>Responsibilities</h3><ul><li>Design and build backend services in Java and Spring Boot for payments.</li>"
            "<li>Own Kafka based event pipelines and keep them reliable around the clock.</li></ul>"
            "<h3>Requirements</h3><ul><li>5-8 years of experience in backend development with PostgreSQL.</li>"
            "<li>Strong problem solving skills and clear communication with product teams.</li></ul>")
    return Posting(ats="greenhouse", url=SITE_URL, title="Backend Engineer II", company="Acme Greenhouse Board",
                   ats_posting_id="9001", job_ref="REQ-9001", location="Pune", department="Payments Platform",
                   description_html=html, raw={"id": 9001})


def _serve(monkeypatch, posting, how):
    from jobtracker import pipeline

    async def fake(url, f):
        return posting, how

    monkeypatch.setattr(pipeline, "fetch_posting", fake)
    pipeline._site_cache.clear()
    return pipeline


def test_company_site_is_the_primary_source_when_it_is_a_careers_page(pasted, monkeypatch):
    import asyncio

    pipeline = _serve(monkeypatch, _site_posting(), "greenhouse")
    d = asyncio.run(pipeline.draft_from_text(CARD, LI_LINK, HINTS, SITE_URL))
    assert d["origin"]["source"] == "company_site" and d["origin"]["ats"] == "greenhouse"
    f = d["fields"]
    assert f["role"]["value"] == "Backend Engineer II"                       # the company's title, even though it differs
    assert f["company"]["value"] == "Acme Payments"                          # the board's cleaner company name
    assert f["job_ref"]["value"] == "REQ-9001" and f["job_link"]["value"] == SITE_URL
    assert f["work_mode"]["value"] == "Onsite"                               # the site states none: the card's chip fills it
    assert f["team_domain"]["value"] == "Payments Platform" and d["counts"]["required"] == 2
    assert f["experience_required"]["value"].startswith("5-8 years")         # from the company's text, not LinkedIn's

    # "Use the LinkedIn text instead"
    d2 = asyncio.run(pipeline.draft_from_text(CARD, LI_LINK, HINTS, SITE_URL, prefer="page"))
    assert d2["origin"]["source"] == "page" and d2["fields"]["role"]["value"] == "Senior Software Engineer"
    assert d2["fields"]["job_ref"]["value"] == "4476129432" and d2["fields"]["job_link"]["value"] == LI_LINK

    res = asyncio.run(pipeline.ingest_text(CARD, {"company": "Acme Payments", "role": "Backend Engineer II"}, link=LI_LINK,
                                           hints=HINTS, site_link=SITE_URL, llm=NoLLM()))
    assert res.status == "created"
    job = repo.get_job(pasted, res.job_id)
    assert job["job_link"] == SITE_URL and job["canonical_url"] == SITE_URL and job["ats"] == "greenhouse"
    assert job["team_domain"] == "Payments Platform" and job["work_mode"] == "Onsite"
    assert provenance(pasted, res.job_id)["experience_required"] == "rules"
    # capturing the same job again from LinkedIn is caught through the company link
    again = asyncio.run(pipeline.ingest_text(CARD, {"company": "Acme Payments", "role": "x"}, link=LI_LINK, hints=HINTS,
                                             site_link=SITE_URL, llm=NoLLM()))
    assert again.status == "duplicate" and again.job_id == res.job_id


def test_a_link_that_is_not_a_careers_page_falls_back_to_the_board_text(pasted, monkeypatch):
    import asyncio

    from jobtracker.models import Posting

    seller = Posting(ats="html", url="https://www.noon.com/en-ae/seller/p-504574", title="Sell on noon", company="Noon",
                     description_html="<p>" + "Reach millions of customers by selling on our marketplace. " * 10 + "</p>",
                     raw={"html_len": 5000})
    pipeline = _serve(monkeypatch, seller, "html")
    d = asyncio.run(pipeline.draft_from_text(CARD, LI_LINK, HINTS, "https://www.noon.com/en-ae/seller/p-504574?link_source=share_btn"))
    assert d["origin"]["source"] == "page" and "careers position" in d["origin"]["reason"]
    assert d["fields"]["role"]["value"] == "Senior Software Engineer" and d["fields"]["job_link"]["value"] == LI_LINK


def _workday_job(pasted):
    """A Workday job saved the way the app saves one, then made stale and edited the way real data gets."""
    import asyncio
    import json
    from pathlib import Path

    from jobtracker import pipeline
    from jobtracker.adapters.workday import Workday
    from jobtracker.models import BoardRef

    raw = json.loads((Path(__file__).parent / "fixtures" / "workday_detail.json").read_text(encoding="utf-8"))
    url = "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/Israel-Yokneam/Software-Engineer--SPE_JR2015623"
    board = BoardRef("workday", "nvidia", "NVIDIA", {"host": "nvidia.wd5.myworkdayjobs.com", "site": "NVIDIAExternalCareerSite"})
    posting = Workday.to_posting(raw, board, url)
    res = asyncio.run(pipeline.store_posting(posting, canonical=url, snapshot=json.dumps({"how": "workday", "raw": raw}), llm=NoLLM()))
    assert res.status == "created"
    jid = res.job_id
    fresh = repo.get_job(pasted, jid)
    pasted.execute("UPDATE jobs SET key_responsibilities = '• thin', experience_required = NULL, requirements = 'my own edit' WHERE id = %s", (jid,))
    pasted.execute("UPDATE field_provenance SET method = 'manual', confidence = 1 WHERE job_id = %s AND field = 'requirements'", (jid,))
    pasted.execute("DELETE FROM job_sections WHERE job_id = %s", (jid,))
    pasted.execute("DELETE FROM job_technologies WHERE job_id = %s", (jid,))
    pasted.commit()
    return jid, fresh


def test_reparse_refreshes_machine_made_text_and_spares_what_you_edited(pasted):
    from jobtracker import reparse

    jid, fresh = _workday_job(pasted)
    make_job(pasted, url="https://x.example/imported", ref="IMP")  # imported from Notion: no snapshot

    dry = {r["id"]: r for r in reparse.reparse_jobs(pasted, dry_run=True)}
    assert dry[jid]["status"] == "would update"
    assert any(ch.startswith("key_responsibilities") for ch in dry[jid]["changes"])
    assert not any(ch.startswith("requirements") for ch in dry[jid]["changes"])   # you edited it: left alone
    assert [r["reason"] for r in dry.values() if r["id"] != jid] == ["no saved snapshot"]
    assert repo.get_job(pasted, jid)["key_responsibilities"] == "• thin"          # a dry run writes nothing

    done = {r["id"]: r for r in reparse.reparse_jobs(pasted, dry_run=False)}
    assert done[jid]["status"] == "updated"
    job = repo.get_job(pasted, jid)
    assert job["key_responsibilities"] == fresh["key_responsibilities"] and len(job["key_responsibilities"]) > 20
    assert job["experience_required"] == fresh["experience_required"] and job["experience_required"]
    assert job["requirements"] == "my own edit" and provenance(pasted, jid)["requirements"] == "manual"
    assert provenance(pasted, jid)["key_responsibilities"] in ("rules", "ats_api")
    assert len(job["sections"]) >= 3 and {t["name"] for t in job["technologies"]} == {t["name"] for t in fresh["technologies"]}
    assert [r["status"] for r in reparse.reparse_jobs(pasted, dry_run=True) if r["id"] == jid] == ["unchanged"]  # nothing left to do


def test_reparse_rebuilds_a_pasted_job_from_its_saved_text(pasted):
    import asyncio

    from jobtracker import pipeline, reparse

    res = asyncio.run(pipeline.ingest_text(PASTE, _fields(), llm=NoLLM()))
    jid = res.job_id
    before = repo.get_job(pasted, jid)["key_responsibilities"]
    assert before
    pasted.execute("UPDATE jobs SET key_responsibilities = '• thin' WHERE id = %s", (jid,))
    pasted.execute("DELETE FROM job_sections WHERE job_id = %s", (jid,))
    pasted.commit()
    out = [r for r in reparse.reparse_jobs(pasted, dry_run=False, job_id=jid)]
    assert [r["status"] for r in out] == ["updated"]
    after = repo.get_job(pasted, jid)
    assert after["key_responsibilities"] == before and after["role"] == "Software Engineer II" and after["company"] == "Acme Payments"


def test_pasted_text_without_a_link_dedupes_on_the_text(pasted):
    import asyncio

    from jobtracker import pipeline

    text = "Backend Engineer\nInitech\nChennai, India\nResponsibilities\n- Write services in Go\nRequirements\n- 2 years of Go"
    first = asyncio.run(pipeline.ingest_text(text, _fields(company="Initech", role="Backend Engineer"), llm=NoLLM()))
    assert first.status == "created" and repo.get_job(pasted, first.job_id)["job_link"] == ""
    second = asyncio.run(pipeline.ingest_text(text, _fields(company="Initech", role="Backend Engineer"), llm=NoLLM()))
    assert second.status == "duplicate"


def test_pasted_text_needs_company_and_title(pasted):
    import asyncio

    from jobtracker import pipeline

    assert asyncio.run(pipeline.ingest_text(PASTE, {"company": "", "role": "X"}, llm=NoLLM())).status == "failed"
    assert asyncio.run(pipeline.ingest_text(PASTE, {"company": "X", "role": ""}, llm=NoLLM())).status == "failed"


def test_job_list_carries_what_the_board_filters_on(c):
    make_job(c)
    row = repo.list_jobs(c)[0]
    assert row["job_ref"] == "R1"  # the board searches by job ID
    assert row["company"] == "Acme" and row["location"] == "Pune" and row["date_applied"] == date(2026, 10, 1)


def test_avature_boards_and_jobs_can_be_stored(c):
    """A fresh install knows 'avature' (schema.sql), and so does an old database once its migration has run."""
    from jobtracker.models import BoardRef

    bid, _ = repo.add_board(c, "Delta", BoardRef("avature", "dth", None, {"host": "dth.avature.net", "base": "https://dth.avature.net/en_US/careers"}),
                         "https://dth.avature.net/en_US/careers/SearchJobs")
    board = next(b for b in repo.list_boards(c) if b["id"] == bid)
    ref = repo.board_ref(board)
    assert board["ats"] == "avature" and ref.config["base"] == "https://dth.avature.net/en_US/careers"
    assert repo._ats("avature") == "avature"


def test_the_avature_migration_is_safe_to_run_on_an_up_to_date_database(c):
    from pathlib import Path

    sql = (Path(__file__).resolve().parent.parent / "db" / "migrations" / "001_avature_ats_type.sql").read_text(encoding="utf-8")
    c.commit()  # ALTER TYPE ... ADD VALUE needs its own transaction
    c.execute(sql)
    c.execute(sql)
    c.commit()
    assert c.execute("SELECT 'avature'::ats_type::text AS v").fetchone()["v"] == "avature"


# ───────────────────────────── watched boards: identity, editing, status ─────────────────────────────
def _gh(c, company="Acme", slug="acme", url=None, **kw):
    from jobtracker.models import BoardRef

    return repo.add_board(c, company, BoardRef("greenhouse", slug), url or f"https://boards.greenhouse.io/{slug}", **kw)


def _row(c, bid):
    return repo.list_boards(c, board_id=bid)[0]


def test_the_same_board_reached_by_another_address_is_not_added_twice(c):
    first, created = _gh(c, title_include=["java"])
    again, created_again = _gh(c, url="https://job-boards.greenhouse.io/Acme", title_include=["python"])  # other host, other case
    assert created and not created_again and again == first
    assert _row(c, first)["title_include"] == ["java"]                  # the existing board is returned untouched
    other, created_other = _gh(c, "Other", "other")
    assert created_other and other != first


def test_a_workday_tenant_with_two_sites_is_two_boards(c):
    from jobtracker.models import BoardRef

    a, _ = repo.add_board(c, "T", BoardRef("workday", "t", None, {"host": "t.wd1.myworkdayjobs.com", "site": "External"}), "https://t.wd1.myworkdayjobs.com/External")
    b, made = repo.add_board(c, "T", BoardRef("workday", "t", None, {"host": "t.wd1.myworkdayjobs.com", "site": "Campus"}), "https://t.wd1.myworkdayjobs.com/Campus")
    assert made and a != b


def test_a_new_board_gets_the_databases_own_interval(c):
    default = repo.default_poll_minutes(c)
    bid, _ = _gh(c)
    assert _row(c, bid)["poll_interval_minutes"] == default == 180
    other, _ = _gh(c, "Two", "two", interval_minutes=60)
    assert _row(c, other)["poll_interval_minutes"] == 60


def test_the_first_good_check_is_the_silent_baseline_even_after_a_failed_attempt(c):
    bid, _ = _gh(c)
    repo.record_poll_failure(c, bid, "boom")                              # an attempt that failed first
    row = _row(c, bid)
    assert row["last_error"] == "boom" and row["last_polled_at"] and row["last_success_at"] is None
    assert repo.list_boards(c, only_due=True) == []                       # a failed board waits its interval, no 15-minute retry
    repo.record_poll(c, row, ListedBatch([lp(1), lp(2)]))
    assert states(c) == {"1": "baseline", "2": "baseline"}                # still treated as the first check: nothing floods the popup
    ok = _row(c, bid)
    assert ok["last_error"] is None and ok["last_success_at"] and (ok["last_listed"], ok["last_new"]) == (2, 0)
    new = repo.record_poll(c, ok, ListedBatch([lp(1), lp(2), lp(3)]))
    assert [n["title"] for n in new] == ["Backend Engineer"] and _row(c, bid)["last_new"] == 1


def test_editing_filters_moves_waiting_postings_in_and_out_of_the_inbox(c):
    from jobtracker import boards

    bid, _ = _gh(c, "Acme", "acme")
    repo.record_poll(c, _row(c, bid), ListedBatch([lp(1, "Backend Engineer")]))
    repo.record_poll(c, _row(c, bid), ListedBatch([lp(1, "Backend Engineer"), lp(2, "Backend Engineer"), lp(3, "Sales Manager")]))
    assert states(c) == {"1": "baseline", "2": "new", "3": "new"}
    assert boards.update(c, bid, {"title_exclude": ["manager", "Manager"]}) == ["title_exclude"]   # de-duplicated, one change
    assert states(c)["3"] == "filtered" and states(c)["2"] == "new" and states(c)["1"] == "baseline"
    boards.update(c, bid, {"title_exclude": []})
    assert states(c)["3"] == "seen"                                        # back in the Inbox, quietly


def test_board_edits_are_validated(c):
    from jobtracker import boards

    bid, _ = _gh(c)
    with pytest.raises(ValueError, match="valid pattern"):
        boards.update(c, bid, {"title_include": ["/(oops/"]})
    with pytest.raises(ValueError, match="interval"):
        boards.update(c, bid, {"poll_interval_minutes": 7})
    with pytest.raises(ValueError, match="company"):
        boards.update(c, bid, {"company": "  "})
    with pytest.raises(KeyError):
        boards.update(c, 999999, {"enabled": False})
    assert boards.update(c, bid, {"enabled": False, "poll_interval_minutes": 60}) == ["poll_interval_minutes", "enabled"]
    assert repo.list_boards(c, only_due=True) == []                        # paused boards are never due


def test_renaming_a_boards_company_renames_it_on_its_jobs_and_queues_notion(c):
    from jobtracker import boards

    bid, _ = _gh(c, "Dth", "dth")
    jid = make_job(c, company="Dth", url="https://x.example/dth/1", ref="D1")
    c.execute("UPDATE companies SET name = 'Dth' WHERE normalized_name = 'dth'")
    c.commit()
    assert _row(c, bid)["company_jobs"] == 1
    assert boards.update(c, bid, {"company": "Delta Global Technology Hub"}) == ["company"]
    assert _row(c, bid)["company"] == repo.get_job(c, jid)["company"] == "Delta Global Technology Hub"
    assert len(repo.due_outbox(c)) == 1                                    # the applied job's Notion page will follow


def test_renaming_to_an_existing_company_merges_the_two(c):
    from jobtracker import boards

    bid, _ = _gh(c, "Dth", "dth")
    keep = make_job(c, company="Delta", url="https://x.example/d/1", ref="K1")
    move = make_job(c, company="Dth", url="https://x.example/d/2", ref="M1")
    boards.update(c, bid, {"company": "Delta"})
    assert repo.get_job(c, move)["company"] == repo.get_job(c, keep)["company"] == "Delta"
    assert _row(c, bid)["company"] == "Delta" and len(company_names(c)) == 1


def test_a_job_added_through_its_careers_page_is_saved_under_the_workday_address(pasted, monkeypatch):
    """The careers page and the Workday board lead to one job: same saved address, so adding it again is a duplicate."""
    import asyncio

    from jobtracker import pipeline
    from jobtracker.models import Posting

    wd = "https://barclays.wd3.myworkdayjobs.com/External_Career_Site_Barclays/job/Chennai-DLF-IT-Park/WCR-analyst_JR-0000121275"
    html = "<h3>Responsibilities</h3><ul><li>Review alerts and escalate cases for the team every day.</li></ul>"

    async def fake(url, f):
        return Posting(ats="workday", url=wd, title="WCR analyst", company="Barclays", job_ref="JR-0000121275", description_html=html,
                       via=url, raw={"jobPostingInfo": {}}), "workday"

    monkeypatch.setattr(pipeline, "fetch_posting", fake)
    first = asyncio.run(pipeline.ingest("https://search.jobs.barclays/job/chennai/wcr-analyst/13015/101587812368", llm=NoLLM()))
    assert first.status == "created"
    job = repo.get_job(pasted, first.job_id)
    assert job["job_link"] == wd and job["canonical_url"] == wd and job["ats"] == "workday"
    assert job["via_url"] == "https://search.jobs.barclays/job/chennai/wcr-analyst/13015/101587812368"   # kept for the "careers page" link
    other_page = asyncio.run(pipeline.ingest("https://search.jobs.barclays/job/chennai/wcr-analyst-again/13015/999", llm=NoLLM()))
    assert other_page.status == "duplicate" and other_page.job_id == first.job_id


# ───────────────────────────── the boards API ─────────────────────────────
@pytest.fixture
def api(pasted):
    from fastapi.testclient import TestClient

    from jobtracker.web import app as webapp

    return TestClient(webapp.app)


def test_meta_serves_what_the_pages_would_otherwise_hardcode(api):
    m = api.get("/api/meta").json()
    assert m["inbox"]["name"] == repo.INBOX and [s["name"] for s in m["statuses"]] == repo.STATUSES
    assert {s["slug"] for s in m["statuses"]} >= {"oa-assessment", "recruiter-screen"}          # derived from the name, one function
    assert m["default_poll_minutes"] == 180 and 180 in {i["minutes"] for i in m["poll_intervals"]}
    assert m["stale_after_hours"] and m["ats_colours"]["workday"]


def test_a_board_can_be_added_edited_previewed_and_checked(api, monkeypatch):
    from jobtracker import boards, watcher

    r = api.post("/api/boards", json={"url": "https://boards.greenhouse.io/acme", "title_include": ["Backend", "backend"], "location_include": ["Pune"]})
    assert r.status_code == 200 and r.json()["created"] and r.json()["company"] == "Acme"
    bid = r.json()["id"]
    again = api.post("/api/boards", json={"url": "https://job-boards.greenhouse.io/ACME"}).json()
    assert again["created"] is False and again["id"] == bid                                      # the same board, found by its identity
    row = next(b for b in api.get("/api/boards").json() if b["id"] == bid)
    assert row["title_include"] == ["Backend"] and row["poll_interval_minutes"] == 180 and row["last_error"] is None   # de-duplicated, default interval

    assert api.patch(f"/api/boards/{bid}", json={"title_exclude": ["intern"], "enabled": False}).json()["changed"] == ["title_exclude", "enabled"]
    assert api.patch(f"/api/boards/{bid}", json={"poll_interval_minutes": 7}).status_code == 422
    assert api.patch(f"/api/boards/{bid}", json={"title_include": ["/(bad/"]}).status_code == 422
    assert api.patch("/api/boards/999999", json={"enabled": True}).status_code == 404
    assert api.post("/api/boards", json={"url": "https://example.com/nope"}).status_code == 422

    async def fake_listing(row, f):  # a listing without the network
        return ListedBatch([lp(1, "Backend Engineer"), lp(2, "Sales Manager")]), row

    monkeypatch.setattr(watcher, "fetch_listing", fake_listing)
    pv = api.post("/api/boards/preview", json={"board_id": bid, "title_include": ["backend"]}).json()
    assert (pv["listed"], pv["matched"], pv["narrowed_at_source"]) == (2, 1, False) and pv["sample"][0]["title"] == "Backend Engineer"
    new = api.post("/api/boards/preview", json={"url": "https://boards.greenhouse.io/other", "title_exclude": ["manager"]}).json()
    assert new["matched"] == 1
    assert api.post("/api/boards/preview", json={}).status_code == 422
    assert api.post(f"/api/boards/{bid}/poll").json() == {"new": 0}                              # first good check: silent baseline
    assert next(b for b in api.get("/api/boards").json() if b["id"] == bid)["last_listed"] == 2
    assert api.delete(f"/api/boards/{bid}").json() == {"ok": True}


def test_suggestions_come_from_applied_jobs_and_are_empty_without_any(api, pasted):
    empty = api.get("/api/boards/suggestions").json()
    assert all(v == {"top": [], "more": []} for v in empty.values())
    make_job(pasted, url="https://x.example/s/1", ref="S1")
    got = api.get("/api/boards/suggestions").json()
    assert [o["value"] for o in got["location_include"]["top"]] == ["Pune"]                     # make_job's location
