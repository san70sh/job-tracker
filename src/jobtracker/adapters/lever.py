"""Lever (P1): api.lever.co/v0/postings/{company}[/{id}]."""
from __future__ import annotations

from urllib.parse import urlsplit

from ..http import Fetcher
from ..models import BoardRef, ListedPosting, PayRange, Posting
from .base import Adapter, Target, parse_date
from ..extract.facts import map_ats_mode


def _api(host_eu: bool) -> str:
    return "https://api.eu.lever.co/v0/postings" if host_eu else "https://api.lever.co/v0/postings"


class Lever(Adapter):
    ats = "lever"

    def identify(self, url: str) -> Target | None:
        p = urlsplit(url)
        host = (p.hostname or "").lower()
        if host not in ("jobs.lever.co", "jobs.eu.lever.co"):
            return None
        segs = [s for s in p.path.split("/") if s]
        if not segs:
            return None
        board = BoardRef("lever", segs[0], config={"eu": host.endswith(".eu.lever.co")})
        if len(segs) >= 2:
            return Target(board, segs[1], url)
        return None

    def board_from_url(self, url: str) -> BoardRef | None:
        p = urlsplit(url)
        host = (p.hostname or "").lower()
        segs = [s for s in p.path.split("/") if s]
        if host in ("jobs.lever.co", "jobs.eu.lever.co") and segs:
            return BoardRef("lever", segs[0], config={"eu": host.endswith(".eu.lever.co")})
        return None

    async def fetch(self, target: Target, f: Fetcher) -> Posting:
        eu = bool(target.board.config.get("eu"))
        d = await f.get_json(f"{_api(eu)}/{target.board.slug}/{target.posting_id}")
        return self.to_posting(d, target.board)

    @staticmethod
    def to_posting(d: dict, board: BoardRef) -> Posting:
        cats = d.get("categories") or {}
        # `description` is the full intro+body; fall back to opening + body when it is absent
        html = d.get("description") or ((d.get("opening") or "") + (d.get("descriptionBody") or ""))
        for lst in d.get("lists") or []:
            html += f"<h3>{lst.get('text', '')}</h3><ul>{lst.get('content', '')}</ul>"
        if d.get("additional"):
            html += f"<h3>Additional information</h3>{d['additional']}"
        sr = d.get("salaryRange")
        pay = None
        if sr and sr.get("min") and sr.get("max"):
            interval = {"per-year-salary": "year", "per-month-salary": "month", "per-hour-wage": "hour"}.get(sr.get("interval", ""), "year")
            pay = PayRange(min=sr["min"], max=sr["max"], currency=sr.get("currency", "USD"), interval=interval)  # type: ignore[arg-type]
        return Posting(
            ats="lever",
            url=d.get("hostedUrl", ""),
            title=d["text"],
            ats_posting_id=d["id"],
            company=board.company or (board.slug or "").replace("-", " ").title(),
            location=cats.get("location"),
            work_mode=map_ats_mode(d.get("workplaceType")),
            department=cats.get("team") or cats.get("department"),
            posted_at=parse_date(d.get("createdAt")),
            description_html=html,
            pay=pay,
            raw=d,
        )

    async def list_postings(self, board: BoardRef, f: Fetcher) -> list[ListedPosting]:
        eu = bool(board.config.get("eu"))
        data = await f.get_json(f"{_api(eu)}/{board.slug}", params={"mode": "json"})
        return [
            ListedPosting(
                ats_posting_id=j["id"], url=j["hostedUrl"], title=j["text"],
                location=(j.get("categories") or {}).get("location"),
                posted_at=parse_date(j.get("createdAt")),
                department=(j.get("categories") or {}).get("team"),
            )
            for j in data
        ]
