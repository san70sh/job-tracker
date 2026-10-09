from __future__ import annotations

import asyncio
import hmac
import logging
import secrets
import threading
import time
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any

import psycopg
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from .. import board_options, boards, db, migrate, notion_sync, pipeline, portals, repo, suggestions, watcher
from ..config import get_settings
from ..events import bus
from ..http import Fetcher
from ..extract.facts import location_tags
from . import diagnostics
from ..extract.pasted import link_job_ref

log = logging.getLogger("jobtracker.web")
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _with(fn, *a, **kw):
    with db.conn() as c:
        return fn(c, *a, **kw)


# ───────────────────────────── background jobs ─────────────────────────────
@diagnostics.tracked("board poll")
async def job_poll() -> None:
    try:
        await watcher.poll_due()
    except Exception:  # noqa: BLE001
        log.exception("board poll failed")


@diagnostics.tracked("notion push")
async def job_notion() -> None:
    """Deliver queued Notion pushes (retries and backoff are handled by the outbox)."""
    if not notion_sync.configured():
        return
    try:
        await asyncio.to_thread(notion_sync.process_outbox)
    except Exception:  # noqa: BLE001
        log.exception("notion outbox run failed")


@diagnostics.tracked("purge")
async def job_purge() -> None:
    """Daily: drop discovered postings that were added or have closed."""
    res = await asyncio.to_thread(lambda: _with(repo.purge_discovered))
    if res["added"] or res["closed"]:
        log.info("purged discovered postings: %s", res)


@diagnostics.tracked("reminder")
async def job_reminder() -> None:
    """Daily nudge for postings that have been waiting for a decision longer than REMINDER_AFTER_DAYS."""
    s = await asyncio.to_thread(lambda: _with(repo.pending_summary, get_settings().reminder_after_days))
    if s["overdue"]:
        bus.publish("action_needed", **s)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    with db.conn() as c:  # a database behind the code is reported, not fixed silently
        pending = migrate.status(c)["pending"]
    if pending:
        log.warning("database migrations pending: %s. Run scripts/db.ps1 apply (or: python -m jobtracker migrate)", ", ".join(pending))
    sched = AsyncIOScheduler()
    sched.add_job(job_poll, "interval", minutes=15, id="poll")
    sched.add_job(job_notion, "interval", minutes=1, id="notion")
    sched.add_job(job_purge, "cron", hour=3, minute=30, id="purge")
    sched.add_job(job_reminder, "cron", hour=9, minute=0, id="reminder")
    sched.start()
    heartbeat = diagnostics.start()
    loop = asyncio.get_running_loop()
    loop.call_later(20, lambda: asyncio.ensure_future(job_poll()))
    loop.call_later(30, lambda: asyncio.ensure_future(job_purge()))
    loop.call_later(10, lambda: asyncio.ensure_future(job_reminder()))
    try:
        yield
    finally:
        heartbeat.cancel()
        sched.shutdown(wait=False)
        db.pool().close()


app = FastAPI(title="Job Tracker", lifespan=lifespan)
app.add_middleware(diagnostics.SlowRequests, slow_seconds=get_settings().slow_request_seconds)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")


# ───────────────────────────── pages ─────────────────────────────
def page(request: Request, name: str, **ctx: Any) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, name, ctx)


@app.get("/", response_class=HTMLResponse)
def home_page(request: Request):
    return page(request, "home.html")


@app.get("/capture/{capture_id}", response_class=HTMLResponse)
def capture_page(request: Request, capture_id: str):
    """The small pop-up window the browser extension opens."""
    return page(request, "capture.html", capture_id=capture_id)


@app.get("/pipeline", response_class=HTMLResponse)
def board_page(request: Request):
    return page(request, "board.html")


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_page(request: Request, job_id: str):
    return page(request, "job.html", job_id=job_id)


@app.get("/inbox", response_class=HTMLResponse)
def inbox_page(request: Request):
    return page(request, "inbox.html")


@app.get("/boards", response_class=HTMLResponse)
def boards_page(request: Request):
    return page(request, "boards.html")


# ───────────────────────────── events (SSE) ─────────────────────────────
@app.get("/events")
async def events(request: Request):
    q = bus.subscribe()

    async def gen():
        try:
            yield "retry: 3000\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=20)
                    yield f"data: {msg}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


# ───────────────────────────── jobs API ─────────────────────────────
class NewJob(BaseModel):
    url: str
    applied: bool = False  # true = also mark as applied today


@app.post("/api/jobs")
async def add_job(body: NewJob):
    res = await pipeline.ingest(body.url.strip())
    if res.status == "created" and body.applied and res.job_id:
        await asyncio.to_thread(lambda: _with(repo.mark_applied, res.job_id))
    if res.status == "created":
        bus.publish("job_added", id=res.job_id, title=res.message, needs_review=res.needs_review)
    code = 200 if res.status not in ("failed", "needs_text") else 422
    return JSONResponse(res.as_dict(), status_code=code)


class PastedText(BaseModel):
    text: str
    link: str | None = None  # the link that needed a login, when the text is the fallback for it
    hints: dict[str, Any] | None = None  # title / company / location read from the page by the extension
    site_link: str | None = None  # the company's own posting, when the page linked to it
    prefer: str = "site"  # "page" = read the job from the pasted/captured text even if there is a company link


class PastedJob(PastedText):
    fields: dict[str, Any]  # the preview, as confirmed
    applied: bool = False


@app.post("/api/jobs/parse")
async def parse_text(body: PastedText):
    """Preview for pasted posting text. Nothing is saved."""
    if len(body.text.strip()) < 40:
        raise HTTPException(422, "That's too short to be a job posting. Paste the full text.")
    return await pipeline.draft_from_text(body.text, body.link, body.hints, body.site_link, body.prefer)


@app.post("/api/jobs/manual")
async def add_pasted_job(body: PastedJob):
    res = await pipeline.ingest_text(body.text, body.fields, link=body.link, hints=body.hints,
                                     site_link=body.site_link, prefer=body.prefer)
    if res.status == "created" and body.applied and res.job_id:
        await asyncio.to_thread(lambda: _with(repo.mark_applied, res.job_id))
    if res.status == "created":
        bus.publish("job_added", id=res.job_id, title=res.message, needs_review=res.needs_review)
    return JSONResponse(res.as_dict(), status_code=200 if res.status != "failed" else 422)


# ───────────────────────────── browser extension capture ─────────────────────────────
CAPTURE_TTL = 30 * 60  # a captured page waits this long for you to review it
_captures: dict[str, tuple[float, dict[str, Any]]] = {}
_captures_lock = threading.Lock()


class Capture(BaseModel):
    url: str = ""  # the address of the page the extension read
    apply_url: str | None = None  # the "Go to company site" link on that page, when it has one
    text: str  # the description as read from the page (or the text you had selected)
    top: str = ""  # the text of the job's header card, when the site has one
    hints: dict[str, Any] | None = None
    source: str = "generic"


def _check_token(given: str | None) -> None:
    want = get_settings().capture_token
    if not want:
        raise HTTPException(503, "Capture is off: run `python -m jobtracker capture-token`, then restart the app.")
    if not given or not hmac.compare_digest(given.encode(), want.encode()):
        raise HTTPException(401, "Wrong capture token. Copy it again from the app's .env into the extension options.")


@app.post("/api/capture")
def api_capture(body: Capture, x_capture_token: str | None = Header(default=None)):
    """Called by the extension only (token required). Parks the page until the review window loads it."""
    _check_token(x_capture_token)
    if len(body.text.strip()) < 40:
        raise HTTPException(422, "The page text was too short to be a job posting. Select the description and try again.")
    now = time.time()
    cid = secrets.token_urlsafe(16)
    text = "\n".join([body.top.strip(), body.text]) if body.top.strip() else body.text
    with _captures_lock:
        for k in [k for k, (t, _) in _captures.items() if now - t > CAPTURE_TTL]:
            del _captures[k]
        if len(_captures) >= 50:
            del _captures[min(_captures, key=lambda k: _captures[k][0])]
        # The job's link is the company's own posting when the page links to it, else the job-board page itself.
        # The board's own job id (LinkedIn, Indeed) still comes from the page address.
        hints = dict(body.hints or {})
        if ref := link_job_ref(body.url):
            hints["job_ref"] = ref
        _captures[cid] = (now, {"text": text, "link": body.apply_url or body.url or None, "hints": hints or None,
                                "site_link": body.apply_url or None, "source": body.source})
    return {"id": cid, "path": f"/capture/{cid}"}


@app.get("/api/captures/{capture_id}")
def api_get_capture(capture_id: str):
    with _captures_lock:
        item = _captures.get(capture_id)
    if not item or time.time() - item[0] > CAPTURE_TTL:
        raise HTTPException(404, "This capture has expired. Click the extension button on the job page again.")
    return item[1]


@app.get("/api/jobs")
def api_jobs(stage: str | None = None):
    jobs = _with(repo.list_jobs, stage)
    for j in jobs:
        j["cities"] = location_tags(j["location"])  # clean tags for the board's location filter
    return jobs


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str):
    job = _with(repo.get_job, job_id)
    if not job:
        raise HTTPException(404)
    return job


class JobPatch(BaseModel):
    role: str | None = None
    company: str | None = None
    job_ref: str | None = None
    level: str | None = None
    location: str | None = None
    work_mode: str | None = None
    experience_required: str | None = None
    team_domain: str | None = None
    key_responsibilities: str | None = None
    requirements: str | None = None
    notes: str | None = None
    salary_min_lpa: float | None = None
    salary_max_lpa: float | None = None
    salary_details: str | None = None
    date_applied: date | None = None


@app.patch("/api/jobs/{job_id}")
def patch_job(job_id: str, body: JobPatch):
    fields = body.model_dump(exclude_unset=True)
    if fields.get("work_mode") == "":
        fields["work_mode"] = None
    try:
        changed = _with(repo.update_job, job_id, **fields)  # also queues the Notion push in the same transaction
    except ValueError as e:
        raise HTTPException(400, str(e))
    except KeyError:
        raise HTTPException(404)
    except psycopg.errors.UniqueViolation:
        raise HTTPException(409, "Another job with the same Job ID already exists for that company.")
    return {"ok": True, "changed": changed}


@app.post("/api/jobs/{job_id}/reviewed")
def mark_reviewed(job_id: str):
    _with(repo.mark_reviewed, job_id)
    return {"ok": True}


class StatusChange(BaseModel):
    status: str
    note: str | None = None


@app.post("/api/jobs/{job_id}/status")
def change_status(job_id: str, body: StatusChange):
    try:
        _with(repo.set_status, job_id, body.status, note=body.note)
    except repo.IllegalTransition as e:
        raise HTTPException(409, str(e))
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e))
    bus.publish("status_changed", id=job_id, status=body.status)
    return {"ok": True}


@app.delete("/api/jobs/{job_id}")
def remove_job(job_id: str):
    _with(repo.delete_job, job_id)  # queues the Notion page for archiving
    return {"ok": True}


@app.get("/api/funnel")
def api_funnel():
    return _with(repo.funnel)


# ───────────────────────────── notion (read-only mirror) ─────────────────────────────
@app.get("/api/notion/status")
def notion_status():
    st = _with(repo.outbox_status)
    return {"configured": notion_sync.configured(), **st}


@app.post("/api/jobs/{job_id}/notion")
async def push_to_notion(job_id: str):
    if not notion_sync.configured():
        raise HTTPException(400, "set NOTION_TOKEN and NOTION_DATA_SOURCE_ID in .env")
    await asyncio.to_thread(lambda: _with(repo.enqueue_notion, job_id))
    res = await asyncio.to_thread(notion_sync.process_outbox)
    return res


@app.post("/api/notion/retry")
async def notion_retry():
    n = await asyncio.to_thread(lambda: _with(repo.retry_failed_outbox))
    res = await asyncio.to_thread(notion_sync.process_outbox) if notion_sync.configured() else {}
    return {"requeued": n, **res}


# ───────────────────────────── discovery API ─────────────────────────────
@app.get("/api/discovered")
def api_discovered():
    """Postings still awaiting a decision (new + seen), oldest first."""
    return _with(repo.list_discovered)


@app.get("/api/discovered/count")
def api_discovered_count():
    return {"new": _with(repo.count_new_discovered)}


@app.get("/api/discovered/summary")
def api_discovered_summary():
    return _with(repo.pending_summary, get_settings().reminder_after_days)


@app.post("/api/discovered/{disc_id}/add")
async def add_discovered(disc_id: str):
    row = await asyncio.to_thread(lambda: _with(repo.set_discovered_state, disc_id, "added"))
    if not row:
        raise HTTPException(404)
    res = await pipeline.ingest(row["url"])
    if res.job_id:
        await asyncio.to_thread(lambda: _with(repo.set_discovered_state, disc_id, "added", res.job_id))
    elif res.status == "failed":
        await asyncio.to_thread(lambda: _with(repo.set_discovered_state, disc_id, "new"))
    return JSONResponse(res.as_dict(), status_code=200 if res.status != "failed" else 422)


@app.post("/api/discovered/{disc_id}/not-interested")
def not_interested(disc_id: str):
    if not _with(repo.set_discovered_state, disc_id, "not_interested"):
        raise HTTPException(404)
    return {"ok": True}


@app.post("/api/discovered/seen-all")
def seen_all():
    _with(repo.mark_all_seen)
    return {"ok": True}


# ───────────────────────────── boards API ─────────────────────────────
class NewBoard(BaseModel):
    url: str
    company: str | None = None
    title_include: list[str] = []
    title_exclude: list[str] = []
    location_include: list[str] = []
    poll_interval_minutes: int | None = None


class BoardPatch(BaseModel):
    company: str | None = None
    title_include: list[str] | None = None
    title_exclude: list[str] | None = None
    location_include: list[str] | None = None
    poll_interval_minutes: int | None = None
    enabled: bool | None = None


class BoardPreview(BaseModel):
    board_id: int | None = None  # an existing board, with the filters below tried in place of its saved ones
    url: str | None = None  # or a board not added yet
    title_include: list[str] = []
    title_exclude: list[str] = []
    location_include: list[str] = []


BOARD_FIELDS = ("id", "ats", "board_url", "title_include", "title_exclude", "location_include", "poll_interval_minutes", "enabled",
                "last_polled_at", "last_success_at", "last_error", "last_listed", "last_new", "new_count", "company_jobs", "company")


async def _new_board(url: str) -> portals.Found:
    """The board an address stands for (by its address, else by what the page says), or a 422 saying what was tried."""
    try:
        async with Fetcher() as f:
            return await portals.find_board(url.strip(), f)
    except portals.NotFound as e:
        raise HTTPException(422, str(e))


async def _board_row(url: str, filters_in: dict) -> dict:
    """A board that is not saved yet, shaped like a watched_boards row so the same code can list it."""
    found = await _new_board(url)
    return {"ats": found.adapter.ats, "slug": found.board.slug, "company": found.board.company, "config": found.board.config, **filters_in}


@app.get("/api/meta")
def api_meta():
    """What the pages need to know that lives on the server, so no list is repeated in JavaScript."""
    return {
        "inbox": {"name": repo.INBOX, "slug": repo.status_slug(repo.INBOX), "label": repo.INBOX_LABEL},
        "statuses": [{"name": s, "slug": repo.status_slug(s)} for s in repo.STATUSES],
        "poll_intervals": board_options.poll_intervals(),
        "default_poll_minutes": _with(repo.default_poll_minutes),
        **board_options.page_settings(),
    }


@app.get("/api/boards")
def api_boards():
    return [{k: r[k] for k in BOARD_FIELDS} for r in _with(repo.list_boards)]


@app.get("/api/boards/suggestions")
def api_board_suggestions():
    """Options for the filter fields, learned from the jobs you applied to (empty lists when there are none)."""
    return suggestions.suggest(_with(repo.applied_jobs))


@app.post("/api/boards")
async def add_board(body: NewBoard):
    found = await _new_board(body.url)
    adapter, board = found.adapter, found.board
    try:
        include, exclude, places = (boards.clean_terms(t) for t in (body.title_include, body.title_exclude, body.location_include))
    except ValueError as e:
        raise HTTPException(422, str(e))
    company = (body.company or "").strip() or board.company or (board.slug or "").replace("-", " ").title()
    try:
        bid, created = await asyncio.to_thread(lambda: _with(
            repo.add_board, company, board, body.url.strip(), title_include=include, title_exclude=exclude,
            location_include=places, interval_minutes=body.poll_interval_minutes))
    except psycopg.errors.InvalidTextRepresentation:  # the database predates this job system: it needs its migration
        raise HTTPException(503, f"The database does not know {adapter.ats} yet. Run scripts/db.ps1 apply, then try again.")
    return {"id": bid, "company": company, "ats": adapter.ats, "created": created,
            "found_by": None if found.how == portals.BY_ADDRESS else found.how}  # set only when the page had to be read


@app.patch("/api/boards/{board_id}")
def patch_board(board_id: int, body: BoardPatch):
    try:
        changed = _with(boards.update, board_id, body.model_dump(exclude_unset=True))
    except KeyError:
        raise HTTPException(404)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except psycopg.errors.UniqueViolation:  # merging into an existing company clashed on a Job ID
        raise HTTPException(409, "Another company with that name already has a job with the same Job ID.")
    return {"ok": True, "changed": changed}


@app.delete("/api/boards/{board_id}")
def remove_board(board_id: int):
    _with(repo.delete_board, board_id)
    return {"ok": True}


@app.post("/api/boards/preview")
async def preview_board(body: BoardPreview):
    """What these filters would keep on the board right now. Nothing is saved."""
    try:
        filters_in = {k: boards.clean_terms(getattr(body, k)) for k in boards.FILTER_FIELDS}
    except ValueError as e:
        raise HTTPException(422, str(e))
    if body.board_id is not None:
        rows = await asyncio.to_thread(lambda: _with(repo.list_boards, board_id=body.board_id))
        if not rows:
            raise HTTPException(404)
        row = {**rows[0], **filters_in}
    elif body.url:
        row = await _board_row(body.url, filters_in)
    else:
        raise HTTPException(422, "Give a board_id or a url.")
    try:
        return await boards.preview(row)
    except watcher.BOARD_ERRORS as e:
        raise HTTPException(502, f"Could not read the board: {e}")


@app.post("/api/boards/poll")
async def poll_now():
    return {"new": await watcher.poll_due(force=True)}


@app.post("/api/boards/{board_id}/poll")
async def poll_board_now(board_id: int):
    try:
        return {"new": len(await watcher.poll_one(board_id))}
    except KeyError:
        raise HTTPException(404)
