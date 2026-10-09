import asyncio
import logging
import time

import pytest

from jobtracker.web import diagnostics


@pytest.fixture
def diag_log(tmp_path, monkeypatch):
    """Point the diagnostics file at a temp folder and make the stall limit short, then undo the handler."""
    monkeypatch.setattr(diagnostics, "LOG_FILE", tmp_path / "logs" / "d.log")
    monkeypatch.setattr("jobtracker.web.diagnostics.get_settings", lambda: type("S", (), {"loop_stall_seconds": 0.3, "slow_request_seconds": 0.2})())
    yield tmp_path / "logs" / "d.log"
    for h in list(diagnostics.log.handlers):
        diagnostics.log.removeHandler(h)
        h.close()


def _blocks_the_loop():
    time.sleep(0.8)  # what a stuck handler looks like to the event loop


async def test_a_stuck_event_loop_is_logged_with_the_code_that_blocked_it(diag_log):
    task = diagnostics.start()
    await asyncio.sleep(0.3)
    _blocks_the_loop()
    await asyncio.sleep(0.3)
    task.cancel()
    text = diag_log.read_text(encoding="utf-8")
    assert "event loop stuck" in text and "_blocks_the_loop" in text


async def test_a_slow_background_job_is_listed_while_it_runs_and_logged_after(diag_log, caplog):
    @diagnostics.tracked("demo job")
    async def job():
        assert "demo job" in diagnostics._context()
        await asyncio.sleep(0.3)

    with caplog.at_level(logging.WARNING, logger="jobtracker.diagnostics"):
        await job()
    assert "background job demo job took" in caplog.text and not diagnostics._running


async def test_slow_requests_are_logged_and_the_popup_stream_is_not(diag_log, caplog):
    async def app(scope, receive, send):
        await asyncio.sleep(0.4)
        await send({"type": "http.response.start", "status": 200, "headers": []})

    wrapped = diagnostics.SlowRequests(app, slow_seconds=0.2)

    async def nothing():
        return {"type": "http.request"}

    async def drop(_):
        pass

    with caplog.at_level(logging.WARNING, logger="jobtracker.diagnostics"):
        await wrapped({"type": "http", "method": "GET", "path": "/api/jobs"}, nothing, drop)
        await wrapped({"type": "http", "method": "GET", "path": "/events"}, nothing, drop)
    assert caplog.text.count("slow request") == 1 and "/api/jobs" in caplog.text
