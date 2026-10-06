"""Command-line error handling: fixable problems print one readable line, real bugs keep their traceback."""
from __future__ import annotations

import psycopg
import pytest

from jobtracker import __main__ as cli
from jobtracker import notion_sync


def test_missing_notion_settings_is_a_friendly_error(monkeypatch, capsys):
    class NoNotion:
        notion_token = None
        notion_data_source_id = None

    monkeypatch.setattr(notion_sync, "get_settings", lambda: NoNotion())
    code = cli.main(["notion-import", "--dry-run"])
    err = capsys.readouterr().err
    assert code == 2
    assert err.startswith("error: Notion is not configured")
    assert "NOTION_TOKEN" in err and "Traceback" not in err


def test_database_connection_failure_is_a_friendly_error(monkeypatch, capsys):
    def boom(_args):
        raise psycopg.OperationalError('connection failed: password authentication failed for user "jobtracker"')

    monkeypatch.setitem(cli.COMMANDS, "purge", boom)
    assert cli.main(["purge"]) == 2
    err = capsys.readouterr().err
    assert "could not connect to the database" in err and "DATABASE_URL" in err and "Traceback" not in err


def test_unexpected_errors_are_not_swallowed(monkeypatch):
    def bug(_args):
        raise ZeroDivisionError("a real bug")

    monkeypatch.setitem(cli.COMMANDS, "purge", bug)
    with pytest.raises(ZeroDivisionError):
        cli.main(["purge"])
