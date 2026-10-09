"""Lag diagnostics. Quiet while the app is healthy; writes logs/diagnostics.log when it is not.

  * a stall of the event loop (nothing else can be served meanwhile): the stack of the stuck thread is logged
    while it is still stuck, so the log names the code that blocked it
  * a slow request, and a slow background job, each with what else was running and the database pool's state
"""
from __future__ import annotations

import asyncio
import functools
import logging
import sys
import threading
import time
import traceback
from logging.handlers import RotatingFileHandler

from .. import db
from ..config import ROOT, get_settings

log = logging.getLogger("jobtracker.diagnostics")
LOG_FILE = ROOT / "logs" / "diagnostics.log"
HEARTBEAT = 0.1  # seconds between loop heartbeats; a stall is a gap much longer than this
STACK_LINES = 14  # how much of the stuck stack to log

_running: dict[object, tuple[str, float]] = {}  # background jobs running now: token -> (name, started)


def _context() -> str:
    """What was going on: running background jobs and the database connection pool."""
    now = time.monotonic()
    jobs = ", ".join(f"{n} ({now - t:.1f}s)" for n, t in _running.values()) or "none"
    try:
        s = db.pool().get_stats()
        pool = f"{s.get('pool_size', 0) - s.get('pool_available', 0)}/{s.get('pool_max', 0)} in use, {s.get('requests_waiting', 0)} waiting"
    except Exception:  # noqa: BLE001
        pool = "unknown"
    return f"background jobs: {jobs}; db pool: {pool}"


def tracked(name: str):
    """Decorator for a background job: lists it while it runs and logs it when it is slow."""
    def wrap(fn):
        @functools.wraps(fn)
        async def run(*a, **kw):
            token = object()
            _running[token] = (name, time.monotonic())
            try:
                return await fn(*a, **kw)
            finally:
                took = time.monotonic() - _running.pop(token)[1]
                if took > get_settings().slow_request_seconds:
                    log.warning("background job %s took %.1fs", name, took)
        return run
    return wrap


class SlowRequests:
    """ASGI middleware: logs requests that take long to start answering. The popup stream (/events) is never 'slow'."""

    def __init__(self, app, slow_seconds: float):
        self.app, self.slow = app, slow_seconds

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] == "/events":
            return await self.app(scope, receive, send)
        start, answered = time.perf_counter(), False

        async def timed_send(message):
            nonlocal answered
            if message["type"] == "http.response.start" and not answered:
                answered = True
                took = time.perf_counter() - start
                if took > self.slow:
                    log.warning("slow request: %s %s took %.1fs to answer (%s); %s", scope["method"], scope["path"], took,
                                message.get("status"), _context())
            await send(message)

        await self.app(scope, receive, timed_send)


class _Watchdog(threading.Thread):
    """Runs in its own thread, so it still works while the event loop is stuck: logs the loop thread's stack."""

    def __init__(self, loop_thread: int, stall: float):
        super().__init__(daemon=True, name="lag-watchdog")
        self.loop_thread, self.stall, self.beat, self.reported = loop_thread, stall, time.monotonic(), False

    def run(self):
        while True:
            time.sleep(HEARTBEAT)
            late = time.monotonic() - self.beat
            if late <= self.stall:
                self.reported = False
            elif not self.reported:
                self.reported = True
                frame = sys._current_frames().get(self.loop_thread)
                stack = "".join(traceback.format_stack(frame)[-STACK_LINES:]) if frame else "(no stack)"
                log.warning("event loop stuck for over %.1fs; %s\nstuck at:\n%s", self.stall, _context(), stack)


async def _heartbeat(dog: _Watchdog) -> None:
    while True:
        dog.beat = time.monotonic()
        await asyncio.sleep(HEARTBEAT)


def _file_handler() -> None:
    if any(isinstance(h, RotatingFileHandler) for h in log.handlers):
        return
    LOG_FILE.parent.mkdir(exist_ok=True)
    h = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    log.addHandler(h)


def start() -> asyncio.Task:
    """Call once from inside the running loop (the app's startup). Returns the heartbeat task, to cancel at shutdown."""
    _file_handler()
    dog = _Watchdog(threading.get_ident(), get_settings().loop_stall_seconds)
    dog.start()
    return asyncio.create_task(_heartbeat(dog))
