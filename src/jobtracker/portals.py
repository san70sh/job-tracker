"""Careers sites the app has not been told about: find the job system behind one, check it, and remember it.

adapters/discovery.py reads a page; this module adds the database (what was learned earlier) and the check that
a found system really lists jobs, so an error is returned only when nothing usable was found.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from urllib.parse import urlsplit

import psycopg

from . import db, repo
from .adapters import discovery, registry
from .adapters.base import Target, Unsupported
from .adapters.discovery import Found, NotFound
from .http import FetchError, Fetcher, PostingGone

__all__ = ["Found", "NotFound", "ensure_loaded", "find_board", "resolve_from_page", "remember"]
log = logging.getLogger("jobtracker.portals")
_loaded = False
BY_ADDRESS = "its address"  # `how` of a board that needed no looking at the page


def load_learned() -> None:
    """Read the domains learned earlier into the registry. A database that predates the table just has none yet."""
    global _loaded
    try:
        with db.conn() as c:
            registry.set_learned(repo.load_portal_hosts(c))
    except psycopg.Error as e:
        log.warning("could not read the learned career domains (%s); run scripts/db.ps1 apply if migrations are pending", e)
    _loaded = True


async def ensure_loaded() -> None:
    if not _loaded:
        await asyncio.to_thread(load_learned)


async def remember(host: str, entry: dict, evidence: str) -> None:
    """Keep a worked-out domain, unless config/hosts.json already says something about it (the file wins)."""
    if host.lower() in registry.load_hosts():
        return
    registry.learn(host, entry)
    try:
        await asyncio.to_thread(lambda: _save(host, entry, evidence))
    except psycopg.Error as e:
        log.warning("could not store the career domain %s (%s)", host, e)
    else:
        log.info("learned that %s runs on %s (%s)", host, entry["ats"], evidence)


def _save(host: str, entry: dict, evidence: str) -> None:
    with db.conn() as c:
        repo.save_portal_host(c, host, entry, evidence)


async def _verify(found: Found, f: Fetcher) -> None:
    """The found system must list jobs for this board (an empty list is fine). One page is enough to tell."""
    probe = replace(found.board, config={**found.board.config, "max_pages": 1})
    name = f"{found.adapter.ats} board '{found.board.slug}'"
    try:
        await found.adapter.list_postings(probe, f)
    except Unsupported:
        raise NotFound(f"Found {name} ({found.how}), but watching boards of that system is not supported yet.") from None
    except (FetchError, PostingGone, KeyError, ValueError, TypeError) as e:
        raise NotFound(f"Found {name} ({found.how}), but its job list could not be read ({e}).") from e


async def find_board(url: str, f: Fetcher) -> Found:
    """The board an address stands for: by its address if known, else by what the page says. Raises NotFound."""
    await ensure_loaded()
    if known := registry.board_from_url(url):
        return Found(known[0], known[1], BY_ADDRESS)
    found = await discovery.discover(url, f)
    await _verify(found, f)
    if found.from_page:
        await remember(found.host, found.adapter.host_entry(found.board), found.how)
    return found


def resolve_from_page(url: str, html: str) -> tuple[registry.Resolved, str, dict] | None:
    """A pasted job page that is itself a supported system's front end -> (how to read it, its host, what to remember).
    Reading the job is the check: the caller remembers the domain only after that worked."""
    pages = discovery.recognise_page(html, url)
    if len(pages) != 1:
        return None
    adapter, board = pages[0]
    posting_id = adapter.posting_id_from_url(url)
    if not posting_id:
        return None  # a listing or search page, not one job
    host = (urlsplit(url).hostname or "").lower()
    return registry.Resolved(adapter, Target(board, posting_id, url)), host, adapter.host_entry(board)
