"""iCIMS Jibe careers sites (P3, JSON): https://{host}/api/jobs?page=N&limit=M.

Jibe has no single-posting endpoint, so fetch() filters the list by keyword and falls back to paging.
The list payload carries explicit `responsibilities` and `qualifications`, which beat heading-splitting.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from ..extract.facts import map_ats_mode
from ..extract.sections import bullets_of
from ..http import Fetcher, PostingGone
from ..models import BoardRef, ListedBatch, ListedPosting, Posting, Section
from .base import Adapter, NeedsFallback, Target, parse_date

_NICE_SPLIT = re.compile(r"\b(Preferred(?: Qualifications)?|Nice to have|Bonus)\b[:\s]*", re.I)
_BASIC = re.compile(r"^\s*(Basic(?: Qualifications)?|Required|Minimum)\b[:\s]*", re.I)


def split_qualifications(q: str) -> tuple[str, str]:
    parts = _NICE_SPLIT.split(q, maxsplit=1)
    req = _BASIC.sub("", parts[0]).strip()
    nice = parts[2].strip() if len(parts) >= 3 else ""
    return req, nice


class Jibe(Adapter):
    ats = "jibe"

    def identify(self, url: str) -> Target | None:
        return None  # custom domains only: resolved via hosts.json or HTML fingerprint

    @staticmethod
    def board_for_host(host: str, company: str | None = None) -> BoardRef:
        return BoardRef("jibe", host, company=company, config={"host": host})

    @staticmethod
    def posting_id_from_url(url: str) -> str | None:
        m = re.search(r"/jobs/([^/?#]+)", urlsplit(url).path)
        return m.group(1) if m else None

    async def _page(self, board: BoardRef, f: Fetcher, **params) -> dict:
        return await f.get_json(f"https://{board.config['host']}/api/jobs", params=params)

    async def fetch(self, target: Target, f: Fetcher) -> Posting:
        b = target.board
        want = target.posting_id
        d = await self._page(b, f, keywords=want, limit=10, page=1)
        for j in d.get("jobs", []):
            data = j["data"]
            if want in (str(data.get("slug")), str(data.get("req_id"))):
                return self.to_posting(data, b)
        # keyword search missed: page through everything (a few hundred postings at most)
        page, seen = 1, 0
        while True:
            d = await self._page(b, f, limit=100, page=page)
            jobs = d.get("jobs", [])
            for j in jobs:
                if want in (str(j["data"].get("slug")), str(j["data"].get("req_id"))):
                    return self.to_posting(j["data"], b)
            seen += len(jobs)
            if not jobs or seen >= d.get("totalCount", 0):
                break
            page += 1
        raise PostingGone(f"jibe posting {want} not on {b.config['host']}")

    @staticmethod
    def to_posting(data: dict, board: BoardRef) -> Posting:
        sections: list[Section] = []
        resp, qual = data.get("responsibilities") or "", data.get("qualifications") or ""
        if resp:
            sections.append(Section(kind="responsibilities", heading="Responsibilities", bullets=bullets_of(resp), text=resp))
        if qual:
            req, nice = split_qualifications(qual)
            if req:
                sections.append(Section(kind="requirements", heading="Qualifications", bullets=bullets_of(req), text=req))
            if nice:
                sections.append(Section(kind="nice_to_have", heading="Preferred", bullets=bullets_of(nice), text=nice))
        tags = [str(t) for k in ("tags1", "tags2", "tags3") for t in (data.get(k) or [])]
        mode = next((m for t in tags if (m := map_ats_mode(t))), None)
        cats = data.get("categories") or []
        slug = str(data.get("slug") or data.get("req_id"))
        return Posting(
            ats="jibe",
            url=f"https://{board.config['host']}/jobs/{slug}",
            title=data["title"],
            ats_posting_id=slug,
            company=board.company,
            job_ref=str(data.get("req_id") or slug),
            location=data.get("full_location") or data.get("location_name"),
            work_mode=mode,
            department=(cats[0].get("name") if cats else None),
            posted_at=parse_date(data.get("posted_date")),
            description_html=data.get("description"),
            sections=sections or None,
            raw=data,
        )

    async def list_postings(self, board: BoardRef, f: Fetcher) -> list[ListedPosting]:
        out: list[ListedPosting] = []
        max_pages = int(board.config.get("max_pages", 10))
        total = 0
        for page in range(1, max_pages + 1):
            d = await self._page(board, f, limit=100, page=page)
            jobs = d.get("jobs", [])
            for j in jobs:
                data = j["data"]
                slug = str(data.get("slug") or data.get("req_id"))
                cats = data.get("categories") or []
                out.append(ListedPosting(
                    ats_posting_id=slug, url=f"https://{board.config['host']}/jobs/{slug}", title=data["title"],
                    location=data.get("full_location"), posted_at=parse_date(data.get("posted_date")),
                    department=cats[0].get("name") if cats else None,
                ))
            total = d.get("totalCount", 0)
            if not jobs or len(out) >= total:
                break
        return ListedBatch(out, complete=len(out) >= total)


def looks_like_jibe(html: str) -> bool:
    return "data-jibe-search-version" in html or "jibeapply" in html
