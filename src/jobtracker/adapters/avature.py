"""Avature career sites (P4): server-rendered pages on *.avature.net, no public JSON API.

  list:   {base}/SearchJobs/?jobOffset=N        10 results a page, "1-10 of 117 results"; each row links to /JobDetail/{slug}/{id}
  detail: {base}/JobDetail/{slug}/{id}          carries a schema.org JobPosting block with the full description
  (an RSS feed exists at {base}/SearchJobs/feed/ but it holds only the latest 20, so it cannot be the listing)

`base` is the site root, for example https://dth.avature.net/en_US/careers. A company's own domain that runs Avature is
added in config/hosts.json as {"ats": "avature", "config": {"base": "https://careers.example.com/en_US/careers"}}.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from ..http import Fetcher
from ..models import BoardRef, ListedBatch, ListedPosting, Posting
from .base import Adapter, Target
from .generic import parse_html

_JOB = re.compile(r"^(?P<base>.*?)/JobDetail/(?P<slug>[^/]+)/(?P<id>\d+)/?$")
_SEARCH = re.compile(r"^(?P<base>.*?)/SearchJobs(?:/.*)?$", re.I)
_ROOT = re.compile(r"^(?P<base>/[a-z]{2}_[A-Z]{2}/[^/]+)/?$")  # /en_US/careers
_TOTAL = re.compile(r"of\s+([\d,]+)\s+results", re.I)
_REF = re.compile(r"^Ref\s*#", re.I)
MAX_PAGES = 40  # 400 postings; a board bigger than that is reported as incomplete


def _is_avature_host(host: str) -> bool:
    return host == "avature.net" or host.endswith(".avature.net")


class Avature(Adapter):
    ats = "avature"

    def identify(self, url: str) -> Target | None:
        p = urlsplit(url)
        host = (p.hostname or "").lower()
        m = _JOB.match(p.path)
        if m and _is_avature_host(host):
            board = BoardRef("avature", host.split(".")[0], None, {"host": host, "base": f"https://{host}{m['base']}"})
            return Target(board, m["id"], url)
        return None

    def posting_id_from_url(self, url: str) -> str | None:
        m = _JOB.match(urlsplit(url).path)
        return m["id"] if m else None

    def board_from_url(self, url: str) -> BoardRef | None:
        p = urlsplit(url)
        host = (p.hostname or "").lower()
        if not _is_avature_host(host):
            return None
        m = _JOB.match(p.path) or _SEARCH.match(p.path) or _ROOT.match(p.path)
        if not m:
            return None
        return BoardRef("avature", host.split(".")[0], None, {"host": host, "base": f"https://{host}{m['base']}"})

    # ───────────────────────────── one posting ─────────────────────────────
    async def fetch(self, target: Target, f: Fetcher) -> Posting:
        html = await f.get_text(target.url)
        post = parse_html(html, target.url, "avature", target.board.company)
        post.url = _clean_job_url(target.url)
        post.ats_posting_id = post.ats_posting_id or target.posting_id
        post.job_ref = post.job_ref or target.posting_id  # Avature's "Ref #" is the same number as the id in the address
        return post

    # ───────────────────────────── the board ─────────────────────────────
    async def list_postings(self, board: BoardRef, f: Fetcher) -> list[ListedPosting]:
        base = str(board.config.get("base") or "").rstrip("/")
        if not base:
            raise ValueError("avature board has no base address")
        first = f"{base}/SearchJobs/"
        out: list[ListedPosting] = []
        seen: set[str] = set()
        total: int | None = None
        per_page = 0
        complete = False
        for page in range(int(board.config.get("max_pages", MAX_PAGES))):
            html = await f.get_text(first, params={"jobOffset": page * per_page} if page else None)
            rows = parse_list(html, first)
            if page == 0:
                m = _TOTAL.search(html)
                total = int(m.group(1).replace(",", "")) if m else None
                per_page = len(rows)
            fresh = [r for r in rows if r.ats_posting_id not in seen]
            seen.update(r.ats_posting_id for r in fresh)
            out.extend(fresh)
            if not fresh or (total is not None and len(out) >= total) or per_page == 0:
                complete = True  # the end of the list: nothing new, or everything counted
                break
        return ListedBatch(out, complete=complete)


def _clean_job_url(url: str) -> str:
    """The address without ?jobId=... tracking, so the same job always has one URL."""
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}{p.path.rstrip('/')}"


def parse_list(html: str, page_url: str) -> list[ListedPosting]:
    """The job rows of one search page: title, place and id. Share-button links in the same rows are ignored."""
    tree = HTMLParser(html)
    out: list[ListedPosting] = []
    for item in tree.css("li.list__item"):
        a = item.css_first(".list__item__text__title a[href]") or next(
            (x for x in item.css("a[href]") if _JOB.match(urlsplit(x.attributes.get("href") or "").path)), None)
        if a is None:
            continue
        href = urljoin(page_url, a.attributes.get("href") or "")
        m = _JOB.match(urlsplit(href).path)
        title = (a.text(strip=True) or "").strip()
        if not m or not title:
            continue
        spans = [s.text(strip=True) for s in item.css(".list__item__text__subtitle span")]
        place = next((s.strip().rstrip(".") for s in spans if s and not _REF.match(s)), None)
        out.append(ListedPosting(ats_posting_id=m["id"], url=_clean_job_url(href), title=title, location=place or None))
    return out
