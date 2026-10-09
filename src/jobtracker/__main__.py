"""CLI:  uv run python -m jobtracker <command>"""
from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import sys
from datetime import date


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="jobtracker")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("seed", help="load the technology vocabulary into the database")
    p = sub.add_parser("ingest", help="fetch + extract + store one job URL")
    p.add_argument("url")
    p.add_argument("--dry-run", action="store_true", help="print the extracted fields, store nothing")
    p = sub.add_parser("serve", help="run the web app")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p = sub.add_parser("poll", help="poll watched boards now")
    p.add_argument("--all", action="store_true", help="ignore the per-board interval")
    p = sub.add_parser("add-board", help="watch a company board")
    p.add_argument("url", help="board or any job URL on it")
    p.add_argument("--company")
    p.add_argument("--include", action="append", default=[], help="title words to keep, or /regex/ (repeatable)")
    p.add_argument("--exclude", action="append", default=[], help="title words to drop, or /regex/ (repeatable)")
    p.add_argument("--location", action="append", default=[], help="place to keep, or /regex/ (repeatable)")
    p = sub.add_parser("bench-import", help="import salary benchmarks from CSV")
    p.add_argument("csv")
    p = sub.add_parser("notion-push", help="queue every applied job for Notion and deliver the queue")
    p.add_argument("--include-inbox", action="store_true", help="also mirror jobs not applied to yet")
    p = sub.add_parser("notion-reconcile", help="compare Notion with the app; --fix overwrites Notion edits and re-creates missing pages")
    p.add_argument("--fix", action="store_true")
    sub.add_parser("notion-retry", help="retry pushes that gave up after repeated failures")
    sub.add_parser("purge", help="purge discovered postings that were added or have closed")
    p = sub.add_parser("backfill-experience", help="fill the numeric experience years from the experience text (for imported jobs)")
    p.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    p = sub.add_parser("backfill-team", help="infer a blank team/domain from the stored title and text (never overwrites)")
    p.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    p = sub.add_parser("notion-import", help="import existing rows from the Notion Job Tracker")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("reparse", help="re-run extraction on saved jobs from their stored snapshot (no refetch); never touches fields you edited")
    p.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    p.add_argument("--job", help="only this job id")
    p = sub.add_parser("migrate", help="bring the database up to date (baseline on an empty one, then pending migrations)")
    p.add_argument("--status", action="store_true", help="only report what is applied and pending")
    sub.add_parser("capture-token", help="create (or show) the token the browser extension uses; stored in .env")

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        return COMMANDS[args.cmd](args) or 0
    except Exception as e:  # noqa: BLE001
        msg = friendly_error(e)
        if msg is None:
            raise  # a real bug: keep the traceback
        print(f"error: {msg}", file=sys.stderr)
        return 2


def friendly_error(e: Exception) -> str | None:
    """One readable line for the failures a user can fix (settings, connection, access). None = unexpected."""
    from . import notion_sync

    if isinstance(e, notion_sync.NotionNotConfigured):
        return ("Notion is not configured. Set NOTION_TOKEN and NOTION_DATA_SOURCE_ID in .env "
                "(see docs/walkthrough.md, section 3.6).")
    try:
        import psycopg
        import psycopg_pool

        if isinstance(e, (psycopg.OperationalError, psycopg_pool.PoolTimeout)):
            first = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
            return ("could not connect to the database. Check that the PostgreSQL service is running and that "
                    f"DATABASE_URL in .env is correct (host, port, user, password). Detail: {first}")
    except ImportError:
        pass
    try:
        from notion_client.errors import APIResponseError

        if isinstance(e, APIResponseError):
            return (f"Notion refused the request (HTTP {getattr(e, 'status', '?')}, {getattr(e, 'code', 'error')}). "
                    "Check NOTION_TOKEN and that the database is shared with your connection "
                    "(database ... menu > Connections).")
    except ImportError:
        pass
    return None


def cmd_seed(_a) -> int:
    from . import db, repo

    with db.conn() as c:
        print(f"seeded {repo.seed_vocab(c)} technologies")
    return 0


def cmd_ingest(a) -> int:
    from . import pipeline

    if a.dry_run:
        return asyncio.run(_dry_run(a.url))
    res = asyncio.run(pipeline.ingest(a.url))
    print(res.status, "-", res.message, f"(id={res.job_id}, needs_review={res.needs_review}, missing={res.missing})")
    return 0 if res.status != "failed" else 1


async def _dry_run(url: str) -> int:
    from .extract.job import apply_salary, extract, missing_fields
    from .http import Fetcher
    from .pipeline import fetch_posting

    async with Fetcher() as f:
        posting, how = await fetch_posting(url, f)
    ex = extract(posting)
    apply_salary(ex, [])
    print(f"adapter: {how}")
    for name, fv in ex.fields.items():
        v = str(fv.value).replace("\n", " | ")
        print(f"  {name:22} [{fv.method}:{fv.confidence:.2f}] {v[:150]}")
    print("  technologies:", ex.technologies)
    print("  sections:", [(s.kind, len(s.bullets)) for s in ex.sections])
    print("  still missing:", missing_fields(ex))
    return 0


def cmd_serve(a) -> int:
    import uvicorn

    uvicorn.run("jobtracker.web.app:app", host=a.host, port=a.port, log_level="info")
    return 0


def cmd_poll(a) -> int:
    from . import watcher

    n = asyncio.run(watcher.poll_due(force=a.all))
    print(f"{n} new posting(s)")
    return 0


def cmd_add_board(a) -> int:
    from . import boards, db, portals, repo
    from .http import Fetcher

    async def find():
        async with Fetcher() as f:
            return await portals.find_board(a.url, f)

    try:
        found = asyncio.run(find())
    except portals.NotFound as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    adapter, board = found.adapter, found.board
    company = a.company or board.company or (board.slug or "").title()
    try:
        include, exclude, places = (boards.clean_terms(t) for t in (a.include, a.exclude, a.location))
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    with db.conn() as c:
        bid, created = repo.add_board(c, company, board, a.url, title_include=include, title_exclude=exclude, location_include=places)
    print(f"watching {company} ({adapter.ats}, board id {bid})" if created else f"already watching it (board id {bid}); edit it on the Company boards page")
    return 0


def cmd_bench_import(a) -> int:
    """CSV columns (all INR per year): company,level,location,total_annual,kind,source,source_url,years_exp"""
    from . import db, repo

    n = 0
    with db.conn() as c, open(a.csv, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            repo.add_benchmark(
                c, row["company"], row.get("level") or None, row.get("location") or None, float(row["total_annual"]),
                row.get("kind") or "aggregate_avg", row.get("source") or "manual",
                row.get("source_url") or None, float(row["years_exp"]) if row.get("years_exp") else None,
            )
            n += 1
    print(f"imported {n} benchmark row(s)")
    return 0


def cmd_notion_push(a) -> int:
    from . import notion_sync

    print(notion_sync.push_all(include_inbox=a.include_inbox))
    return 0


def cmd_notion_reconcile(a) -> int:
    import json

    from . import notion_sync

    print(json.dumps(notion_sync.reconcile(fix=a.fix), indent=2, default=str))
    return 0


def cmd_notion_retry(_a) -> int:
    from . import db, notion_sync, repo

    with db.conn() as c:
        n = repo.retry_failed_outbox(c)
    print(f"requeued {n}; delivery:", notion_sync.process_outbox())
    return 0


def cmd_backfill_experience(a) -> int:
    from . import db, repo

    with db.conn() as c:
        rows = repo.backfill_experience(c, dry_run=a.dry_run)
    verb = "would set" if a.dry_run else "set"
    for r in rows:
        hi = "+" if r["max"] is None else (f"-{r['max']:g}" if r["max"] != r["min"] else "")
        print(f"  {r['min']:g}{hi:<4} years  <-  {r['text'][:70]!r}   [{r['role'][:40]}]")
    print(f"{verb} numeric experience on {len(rows)} job(s)")
    return 0


def cmd_backfill_team(a) -> int:
    from . import db, repo

    with db.conn() as c:
        rows = repo.backfill_team(c, dry_run=a.dry_run)
    verb = "would set" if a.dry_run else "set"
    for r in rows:
        print(f"  {r['value'][:48]!r:<52} {r['confidence']:.2f}  <-  {r['evidence'][:70]}   [{r['role'][:40]}]")
    print(f"{verb} the team on {len(rows)} job(s)")
    return 0


def cmd_reparse(a) -> int:
    from . import db, reparse

    with db.conn() as c:
        rows = reparse.reparse_jobs(c, dry_run=a.dry_run, job_id=a.job)
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        if r["status"] in ("would update", "updated"):
            print(f"{r['status'].upper():<13} {r['role'][:46]!r} @ {r['company'][:22]}  [{r['ats']}]")
            for ch in r["changes"]:
                print(f"    {ch}")
        elif r["status"] == "skipped" and r["reason"] != "no saved snapshot":
            print(f"SKIPPED       {r['role'][:46]!r} @ {r['company'][:22]}: {r['reason']}")
    no_snap = sum(1 for r in rows if r["reason"] == "no saved snapshot")
    print(f"\n{len(rows)} job(s): " + ", ".join(f"{n} {k}" for k, n in sorted(counts.items())) +
          (f" ({no_snap} without a saved snapshot, e.g. imported from Notion)" if no_snap else ""))
    return 0


def cmd_migrate(a) -> int:
    from . import db, migrate

    with db.conn() as c:
        if a.status:
            st = migrate.status(c)
            print(f"applied: {len(st['applied'])} | pending: {st['pending'] or 'none'} | edited after applying: {st['changed'] or 'none'}")
            return 1 if st["pending"] else 0
        done = migrate.run(c)
        st = migrate.status(c)
    print("applied now: " + (", ".join(done) if done else "nothing, already up to date"))
    if st["changed"]:
        print(f"warning: edited after being applied (never edit an applied migration; add a new one): {', '.join(st['changed'])}")
    return 0


def cmd_capture_token(_a) -> int:
    import secrets

    from .config import ROOT, get_settings

    existing = get_settings().capture_token
    if existing:
        print(f"CAPTURE_TOKEN is already set in .env. Paste this into the extension's options:\n\n{existing}\n")
        return 0
    token = secrets.token_urlsafe(32)
    env = ROOT / ".env"
    prefix = b"" if not env.exists() or env.read_bytes().endswith(b"\n") or env.stat().st_size == 0 else b"\n"
    with env.open("ab") as fh:  # append only: never rewrite the other secrets in the file
        fh.write(prefix + f"CAPTURE_TOKEN={token}\n".encode("ascii"))
    print(f"Added CAPTURE_TOKEN to {env}. Restart the app, then paste this into the extension's options:\n\n{token}\n")
    return 0


def cmd_purge(_a) -> int:
    from . import db, repo

    with db.conn() as c:
        print(repo.purge_discovered(c))
    return 0


def cmd_notion_import(a) -> int:
    from . import notion_sync

    print(notion_sync.import_from_notion(dry_run=a.dry_run))
    return 0


COMMANDS = {
    "seed": cmd_seed, "ingest": cmd_ingest, "serve": cmd_serve, "poll": cmd_poll, "add-board": cmd_add_board,
    "bench-import": cmd_bench_import,
    "notion-push": cmd_notion_push, "notion-import": cmd_notion_import,
    "notion-reconcile": cmd_notion_reconcile, "notion-retry": cmd_notion_retry, "purge": cmd_purge,
    "backfill-experience": cmd_backfill_experience, "backfill-team": cmd_backfill_team,
    "capture-token": cmd_capture_token, "migrate": cmd_migrate, "reparse": cmd_reparse,
}

if __name__ == "__main__":
    sys.exit(main())
