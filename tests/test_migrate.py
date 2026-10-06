"""The migration runner: baseline on an empty database, pending files once each, edits to applied files reported."""
import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from jobtracker import migrate
from jobtracker.config import get_settings

SCRATCH = "jobtracker_migrate_test"  # its own throwaway database, so the shared test database is left alone


@pytest.fixture
def empty_db():
    base = get_settings().database_url
    try:
        admin = psycopg.connect(make_conninfo(base, dbname="postgres"), autocommit=True, connect_timeout=5)
    except psycopg.OperationalError as e:
        pytest.skip(f"cannot reach the database server: {str(e).splitlines()[0]}")
    admin.execute(f"DROP DATABASE IF EXISTS {SCRATCH} WITH (FORCE)")
    admin.execute(f"CREATE DATABASE {SCRATCH}")
    c = psycopg.connect(make_conninfo(base, dbname=SCRATCH), row_factory=dict_row)
    try:
        yield c
    finally:
        c.close()
        admin.execute(f"DROP DATABASE IF EXISTS {SCRATCH} WITH (FORCE)")
        admin.close()


def test_an_empty_database_gets_the_baseline_then_every_migration(empty_db):
    done = migrate.run(empty_db)
    assert done[0] == migrate.BASELINE_NAME and done[1:] == [p.name for p in migrate.files()]
    cols = {r["column_name"] for r in empty_db.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'watched_boards'")}
    assert {"last_success_at", "last_error", "last_listed", "last_new"} <= cols     # 002 ran
    assert empty_db.execute("SELECT 'avature'::ats_type::text AS v").fetchone()["v"] == "avature"


def test_running_again_does_nothing(empty_db):
    migrate.run(empty_db)
    assert migrate.run(empty_db) == []
    st = migrate.status(empty_db)
    assert st["pending"] == [] and st["changed"] == []


def test_an_existing_database_without_the_tracking_table_catches_up(empty_db):
    """A database made before the runner existed: tables present, no schema_migrations. Every file is safe to apply."""
    migrate.run(empty_db)
    empty_db.execute("DROP TABLE schema_migrations")
    empty_db.commit()
    done = migrate.run(empty_db)
    assert migrate.BASELINE_NAME not in done and done == [p.name for p in migrate.files()]


def test_a_migration_edited_after_applying_is_reported(empty_db, tmp_path, monkeypatch):
    (tmp_path / "003_x.sql").write_text("ALTER TABLE watched_boards ADD COLUMN IF NOT EXISTS x int;")
    monkeypatch.setattr(migrate, "MIGRATIONS", tmp_path)
    migrate.run(empty_db)
    (tmp_path / "003_x.sql").write_text("ALTER TABLE watched_boards ADD COLUMN IF NOT EXISTS y int;")
    assert migrate.status(empty_db)["changed"] == ["003_x.sql"]


def test_a_failing_migration_leaves_nothing_half_applied(empty_db, tmp_path, monkeypatch):
    migrate.run(empty_db)
    (tmp_path / "003_bad.sql").write_text("ALTER TABLE watched_boards ADD COLUMN ok_col int; SELECT nope FROM nowhere;")
    monkeypatch.setattr(migrate, "MIGRATIONS", tmp_path)
    with pytest.raises(psycopg.errors.UndefinedTable):
        migrate.run(empty_db)
    empty_db.rollback()
    cols = {r["column_name"] for r in empty_db.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'watched_boards'")}
    assert "ok_col" not in cols and migrate.status(empty_db)["pending"] == ["003_bad.sql"]
