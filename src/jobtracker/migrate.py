"""Database migrations: the baseline schema on an empty database, then every pending db/migrations/*.sql in name order.

Forward-only. Each file runs once, in its own transaction, and is recorded with a checksum, so a file edited after it
was applied is reported instead of silently ignored. `schema.sql` is the frozen baseline: every later change lives in
a migration file only, so there is one place per change.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import psycopg

from .config import ROOT

BASELINE = ROOT / "db" / "schema.sql"
MIGRATIONS = ROOT / "db" / "migrations"
BASELINE_NAME = "000_baseline"

_TABLE = """CREATE TABLE IF NOT EXISTS schema_migrations (
  name text PRIMARY KEY, checksum text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())"""


def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_text(encoding="utf-8").replace("\r\n", "\n").encode()).hexdigest()


def files() -> list[Path]:
    return sorted(MIGRATIONS.glob("*.sql"), key=lambda p: p.name)


def status(c: psycopg.Connection) -> dict[str, list[str]]:
    """applied: recorded files; pending: files not yet applied; changed: applied files edited since."""
    c.execute(_TABLE)
    c.commit()
    done = {r["name"]: r["checksum"] for r in c.execute("SELECT name, checksum FROM schema_migrations").fetchall()}
    return {
        "applied": sorted(done),
        "pending": [p.name for p in files() if p.name not in done],
        "changed": [p.name for p in files() if p.name in done and done[p.name] != _checksum(p)],
    }


def _record(c: psycopg.Connection, name: str, path: Path) -> None:
    c.execute("INSERT INTO schema_migrations (name, checksum) VALUES (%s, %s)", (name, _checksum(path)))


def run(c: psycopg.Connection) -> list[str]:
    """Bring the database up to date. Returns the names applied this time (empty when already current)."""
    c.execute(_TABLE)
    c.commit()
    applied: list[str] = []
    if c.execute("SELECT to_regclass('public.jobs') IS NULL AS empty").fetchone()["empty"]:
        with c.transaction():  # an empty database starts from the baseline
            c.execute(BASELINE.read_text(encoding="utf-8"))
            _record(c, BASELINE_NAME, BASELINE)
        applied.append(BASELINE_NAME)
    for name in status(c)["pending"]:
        path = MIGRATIONS / name
        with c.transaction():
            c.execute(path.read_text(encoding="utf-8"))
            _record(c, name, path)
        applied.append(name)
    c.commit()
    return applied
