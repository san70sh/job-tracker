"""iCIMS career portals (classic, HTML): {tenant}.icims.com.

A job page carries JSON-LD (read by the generic parser). The board is a search page, /jobs/search?ss=1&in_iframe=1,
whose rows hold the id, title, place and category; it pages with ?pr=N (from 0) and filters with the page's own
search* parameters (searchKeyword, searchLocation, searchCategory...), which a pasted address keeps.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from .. import board_options
from ..http import Fetcher
from ..models import BoardRef, ListedBatch, ListedPosting, Posting
from .base import Adapter, Target
from .generic import parse_html

_JOB_PATH = re.compile(r"^/jobs/(\d+)(?:/|$)")
_BOARD_PATHS = ("", "/jobs", "/jobs/search", "/jobs/intro")  # the portal's home and search pages (not one job)
_PAGE_OF = re.compile(r"Page\s+(\d+)\s+of\s+(\d+)", re.I)
# iCIMS' own infrastructure also lives under icims.com (assets, the CDN copies of a tenant); not a portal
SERVICE_HOSTS = {"cdn01", "cdn02", "cdn03", "www", "static", "login", "api", "i"}
_COUNTRY_CODE_PLACE = re.compile(r"^([A-Z]{2})-(?:([A-Z0-9]{1,4})-)?(.+)$")  # IN-KA-Bengaluru, US-Remote


def is_portal_host(host: str) -> bool:
    return host.endswith(".icims.com") and not host.endswith(".i.icims.com") and host.split(".")[0] not in SERVICE_HOSTS


def _company_of(host: str) -> str:
    """Portals are named {region}careers-{company} (indiacareers-docusign) or careers-{company}: the company is the last part."""
    return host.split(".")[0].split("-")[-1].title()


def place(raw: str | None) -> str | None:
    """iCIMS writes places as COUNTRY-STATE-City ("IN-KA-Bengaluru"); say them as a person would ("Bengaluru, KA, India")."""
    names = board_options.country_names()
    out = []
    for part in re.split(r"\s*[|;]\s*", re.sub(r"\s+", " ", raw or "").strip()):
        m = _COUNTRY_CODE_PLACE.match(part)
        out.append(", ".join(x for x in (m.group(3), m.group(2), names.get(m.group(1), m.group(1))) if x) if m else part)
    return "; ".join(p for p in out if p) or None


def _text(node) -> str:
    return re.sub(r"\s+", " ", node.text(strip=True)) if node else ""


def parse_list(html: str, host: str) -> list[ListedPosting]:
    """The job rows of one search page."""
    out: list[ListedPosting] = []
    for row in HTMLParser(html).css(".iCIMS_JobsTable .row"):
        a = row.css_first(".title a[href]")
        m = _JOB_PATH.match(urlsplit(a.attributes.get("href") or "").path) if a else None
        title = _text(a.css_first("h3")) or (a.attributes.get("title") or "").split(" - ", 1)[-1] if a else ""
        if not (m and title):
            continue
        fields = {_text(t.css_first("dt")).lower(): _text(t.css_first("dd")) for t in row.css(".iCIMS_JobHeaderTag")}
        # the place's label is an icon plus a screen-reader "Job Locations"; the category's is the visible word "Category"
        where = next((v for k, v in fields.items() if "location" in k), None)
        out.append(ListedPosting(
            ats_posting_id=m.group(1), url=f"https://{host}{urlsplit(a.attributes['href']).path}", title=title,
            location=place(where), department=fields.get("category") or None))
    return out


class IcimsClassic(Adapter):
    ats = "icims"

    def identify(self, url: str) -> Target | None:
        p = urlsplit(url)
        host = (p.hostname or "").lower()
        m = _JOB_PATH.match(p.path)
        if is_portal_host(host) and m:
            return Target(BoardRef("icims", host, _company_of(host), {"host": host}), m.group(1), url)
        return None

    def board_from_url(self, url: str) -> BoardRef | None:
        p = urlsplit(url)
        host = (p.hostname or "").lower()
        if not is_portal_host(host) or p.path.rstrip("/").lower() not in _BOARD_PATHS:
            return None
        config: dict = {"host": host}
        if facets := {k: v[0] for k, v in parse_qs(p.query).items() if k.startswith("search") and v and v[0]}:
            config["facets"] = facets  # what the portal's own search form put in the address
        return BoardRef("icims", host, _company_of(host), config)

    async def fetch(self, target: Target, f: Fetcher) -> Posting:
        sep = "&" if "?" in target.url else "?"
        html = await f.get_text(f"{target.url}{sep}in_iframe=1")
        post = parse_html(html, target.url, "icims", target.board.company)
        post.ats_posting_id = post.ats_posting_id or target.posting_id
        post.job_ref = post.job_ref or target.posting_id
        return post

    async def list_postings(self, board: BoardRef, f: Fetcher) -> list[ListedPosting]:
        host, out, seen = board.config["host"], [], set()
        max_pages, done = int(board.config.get("max_pages", 10)), False
        for pr in range(max_pages):
            html = await f.get_text(f"https://{host}/jobs/search", params={"ss": 1, "in_iframe": 1, **board.config.get("facets", {}), "pr": pr})
            rows = [r for r in parse_list(html, host) if r.ats_posting_id not in seen]
            seen.update(r.ats_posting_id for r in rows)
            out += rows
            pages = _PAGE_OF.search(html)
            if not rows or (pages and pr + 1 >= int(pages.group(2))):
                done = True  # the last page, or a page with nothing new
                break
        return ListedBatch(out, complete=done)
