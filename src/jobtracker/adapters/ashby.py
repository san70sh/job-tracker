"""Ashby (P1): api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true.

Ashby has no single-posting endpoint, so fetch() reads the board listing (which already carries full
descriptions and pay) and picks the posting by id.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from ..extract.facts import map_ats_mode
from ..http import Fetcher, PostingGone
from ..models import BoardRef, ListedPosting, PayRange, Posting
from .base import Adapter, Target, parse_date

API = "https://api.ashbyhq.com/posting-api/job-board"


class Ashby(Adapter):
    ats = "ashby"

    def identify(self, url: str) -> Target | None:
        p = urlsplit(url)
        if (p.hostname or "").lower() != "jobs.ashbyhq.com":
            return None
        segs = [s for s in p.path.split("/") if s]
        if len(segs) >= 2:
            return Target(BoardRef("ashby", segs[0]), segs[1], url)
        return None

    def board_from_url(self, url: str) -> BoardRef | None:
        p = urlsplit(url)
        segs = [s for s in p.path.split("/") if s]
        if (p.hostname or "").lower() == "jobs.ashbyhq.com" and segs:
            return BoardRef("ashby", segs[0])
        return None

    async def _board(self, slug: str, f: Fetcher) -> list[dict]:
        return (await f.get_json(f"{API}/{slug}", params={"includeCompensation": "true"})).get("jobs", [])

    async def fetch(self, target: Target, f: Fetcher) -> Posting:
        for j in await self._board(target.board.slug or "", f):
            if j["id"] == target.posting_id:
                return self.to_posting(j, target.board)
        raise PostingGone(f"ashby posting {target.posting_id} not on board {target.board.slug}")

    @staticmethod
    def to_posting(j: dict, board: BoardRef) -> Posting:
        pay = None
        for tier in (j.get("compensation") or {}).get("compensationTiers") or []:
            for c in tier.get("components") or []:
                if c.get("compensationType") == "Salary" and c.get("minValue") and c.get("maxValue"):
                    interval = {"1 YEAR": "year", "1 MONTH": "month", "1 HOUR": "hour"}.get(c.get("interval", ""), "year")
                    pay = PayRange(min=c["minValue"], max=c["maxValue"], currency=c.get("currencyCode") or "USD",
                                   interval=interval, note=tier.get("title"))  # type: ignore[arg-type]
                    break
            if pay:
                break
        return Posting(
            ats="ashby",
            url=j.get("jobUrl", ""),
            title=j["title"],
            ats_posting_id=j["id"],
            company=board.company or (board.slug or "").replace("-", " ").title(),
            location=j.get("location"),
            work_mode=map_ats_mode(j.get("workplaceType")) or ("Remote" if j.get("isRemote") else None),
            department=j.get("team") or j.get("department"),
            posted_at=parse_date(j.get("publishedAt")),
            description_html=j.get("descriptionHtml"),
            pay=pay,
            raw=j,
        )

    async def list_postings(self, board: BoardRef, f: Fetcher) -> list[ListedPosting]:
        return [
            ListedPosting(ats_posting_id=j["id"], url=j["jobUrl"], title=j["title"], location=j.get("location"),
                          posted_at=parse_date(j.get("publishedAt")), department=j.get("department"))
            for j in await self._board(board.slug or "", f) if j.get("isListed", True)
        ]
