"""Portal discovery: which supported job system sits behind a careers page we cannot recognise by its address.

Evidence, strongest first. Nothing is guessed or probed: only what the page itself says.
  1. where the address redirects to
  2. the page is the system's own front end (each adapter's `recognise` hook: Oracle, Jibe, a Greenhouse embed)
  3. the page's scripts, frames and stylesheets point at a system's board address
  4. the page's links do
  5. one job page the page links to (careers sites like Radancy or Phenom link Apply to the real system)
The same address recognisers the add-board form already uses read the web addresses found in 3-5.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from ..http import FetchError, Fetcher, PostingGone
from ..models import IDENTITY_KEYS, BoardRef
from . import registry
from .base import Adapter

MAX_URLS = 400  # per page; a careers page with more links than this is not pointing at one system
BLOCKED_STATUSES = ("401", "403", "429", "999")  # how sites answer a program they do not want (bot protection, login walls)
MIN_LINKS_PAGE = 5  # fewer links than this: the page most likely builds itself with JavaScript
_URL_IN_TEXT = re.compile(r"https?://[^\s\"'<>\\)\]}]+", re.I)
_JOB_LINK = re.compile(r"/(?:jobs?|positions?|openings?|requisitions?)/[^/?#]+", re.I)  # looks like a link to one job


class NotFound(Exception):
    """No supported job system could be identified; the message says what was looked at and what to do."""


@dataclass
class Found:
    adapter: Adapter
    board: BoardRef
    how: str  # shown to the user: what gave it away
    from_page: bool = False  # the page itself is this system's front end, so the domain is worth remembering
    host: str = ""  # the domain of the page that was read


def _systems() -> dict[str, Adapter]:
    return {n: a for n, a in registry.adapters().items() if n != "html"}  # "html" accepts any address


def _key(board: BoardRef) -> tuple:
    return (board.ats, (board.slug or "").lower(), *(board.config.get(k) for k in IDENTITY_KEYS))


def boards_at(urls: list[str]) -> list[tuple[Adapter, BoardRef]]:
    """Every distinct board that these web addresses are the address of."""
    seen: dict[tuple, tuple[Adapter, BoardRef]] = {}
    for u in dict.fromkeys(urls[:MAX_URLS]):
        for ad in _systems().values():
            if b := ad.board_from_url(u):
                seen.setdefault(_key(b), (ad, b))
                break
    return list(seen.values())


def recognise_page(html: str, url: str) -> list[tuple[Adapter, BoardRef]]:
    """Systems whose own front end this page is (each adapter's `recognise` hook)."""
    seen: dict[tuple, tuple[Adapter, BoardRef]] = {}
    for ad in _systems().values():
        if b := ad.recognise(html, url):
            seen.setdefault(_key(b), (ad, b))
    return list(seen.values())


def _web_addresses(html: str, page_url: str) -> tuple[list[str], list[str]]:
    """(embedded, linked): addresses in script/frame/stylesheet tags and script text, and addresses of links."""
    tree = HTMLParser(html)
    embedded: list[str] = []
    for n in tree.css("script, iframe, link"):
        embedded += [urljoin(page_url, v) for attr in ("src", "href") if (v := n.attributes.get(attr))]
        if n.tag == "script":
            embedded += _URL_IN_TEXT.findall((n.text() or "").replace("\\/", "/"))  # inline configuration
    linked = [urljoin(page_url, a.attributes["href"]) for a in tree.css("a[href]")]
    return embedded, linked


def _label(found: list[tuple[Adapter, BoardRef]]) -> str:
    return ", ".join(f"{a.ats} '{b.slug}'" for a, b in found)


def _read(html: str, page_url: str, *, with_links: bool) -> tuple[Found | None, list[str]]:
    """Evidence in one page's content, strongest first: (the answer, if exactly one system; else what conflicted).
    `with_links` is off for a job page: its links are other jobs, but its own content and Apply link still count."""
    host = (urlsplit(page_url).hostname or "").lower()
    embedded, linked = _web_addresses(html, page_url)
    steps = [("the page is the system's own front end", recognise_page(html, page_url), True),
             ("the page's scripts and frames point to it", boards_at(embedded), False)]
    if with_links:
        steps.append(("a link on the page points to it", boards_at(linked), False))
    conflicts: list[str] = []
    for how, found, from_page in steps:
        if len(found) == 1:
            return Found(found[0][0], found[0][1], how, from_page, host), conflicts
        if found:
            conflicts.append(f"{how} ({_label(found)})")
    return None, conflicts


def _job_link(html: str, page_url: str) -> str | None:
    """A link on the same site that looks like one job: where careers sites show the Apply link of the real system."""
    host, here = (urlsplit(page_url).hostname or "").lower(), urlsplit(page_url).path
    for a in HTMLParser(html).css("a[href]"):
        u = urlsplit(urljoin(page_url, a.attributes["href"]))
        if (u.hostname or "").lower() == host and u.path != here and _JOB_LINK.search(u.path):
            return u._replace(fragment="").geturl()
    return None


def _open_error(e: Exception) -> str:
    """Why the page could not be read, in words (not the raw reply, which for bot protection is a page of markup)."""
    status = str(e).split(" ", 1)[0]
    if status in BLOCKED_STATUSES:
        return (f"The site refused the request (HTTP {status}, usually bot protection or a login), so its page could not be read. "
                "Open the careers page in your browser, click Apply on any job and paste the address that opens "
                "(it usually names the job system, for example boards.greenhouse.io/company), or add the host to config/hosts.json.")
    return f"The page could not be opened ({str(e)[:160]})."


async def discover(url: str, f: Fetcher) -> Found:
    """Open the page and work out its job system. Raises NotFound, saying what was looked at, when nothing fits."""
    try:
        r = await f.request("GET", url)
    except (FetchError, PostingGone) as e:
        raise NotFound(_open_error(e)) from e
    html = r.text if "html" in r.headers.get("content-type", "html") else ""
    page_url = str(r.url)

    for hop in (*r.history[1:], r):  # where each redirect led
        if (b := registry.board_from_url(str(hop.url))) and str(hop.url) != url:
            return Found(b[0], b[1], f"the address redirects to {hop.url}")

    found, conflicts = _read(html, page_url, with_links=True) if html else (None, [])
    if found:
        return found

    if not conflicts and html and (job := _job_link(html, page_url)):  # one hop: a job page shows the system's own embed or Apply link
        try:
            job_html = await f.get_text(job)
        except (FetchError, PostingGone):
            job_html = ""
        if job_html:
            found, conflicts = _read(job_html, job, with_links=False)
            if found:
                found.how += f", on a job page it links to ({job})"
                return found
            if not conflicts and (inner := registry.delegate_from_html(job_html, job)):
                return Found(inner.adapter, inner.target.board, f"the Apply link on a job page it links to ({job})")

    if conflicts:
        raise NotFound("More than one job system is mentioned on this page, so none was picked: " + "; ".join(conflicts)
                       + ". Paste the board address of the one you want.")
    looked = "the address, its redirects, the page's own content, scripts, frames and links" + (", and one job page it links to" if html else "")
    js = " The page has almost no links, so it probably builds its job list with JavaScript, which this check cannot run." \
        if html and len(HTMLParser(html).css("a[href]")) < MIN_LINKS_PAGE else ""
    raise NotFound(f"No supported job system was found at {urlsplit(page_url).hostname} (looked at {looked}).{js} "
                   "If you know which system it is, add the host to config/hosts.json.")
