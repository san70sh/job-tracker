"""Greenhouse (P1): boards-api.greenhouse.io/v1/boards/{token}/jobs[/{id}]."""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

from ..http import Fetcher
from ..models import BoardRef, ListedPosting, PayRange, Posting
from .base import Adapter, NeedsFallback, Target, parse_date

API = "https://boards-api.greenhouse.io/v1/boards"
HOSTS = {"boards.greenhouse.io", "job-boards.greenhouse.io", "job-boards.eu.greenhouse.io", "boards.eu.greenhouse.io"}
_TOKEN_IN_PAGE = re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board(?:/js)?\?for=|embed/job_app\?for=)?([a-z0-9_-]+)", re.I)


class Greenhouse(Adapter):
    ats = "greenhouse"

    def identify(self, url: str) -> Target | None:
        p = urlsplit(url)
        host = (p.hostname or "").lower()
        segs = [s for s in p.path.split("/") if s]
        if host in HOSTS and segs:
            token = segs[0]
            if len(segs) >= 3 and segs[1] == "jobs":
                return Target(BoardRef("greenhouse", token), segs[2], url)
            # embedded: /embed/job_app?for=stripe&token=123
            qs = parse_qs(p.query)
            if segs[0] == "embed" and "for" in qs and "token" in qs:
                return Target(BoardRef("greenhouse", qs["for"][0]), qs["token"][0], url)
            return None
        # Vanity domain with ?gh_jid=...: token unknown here; resolved by hosts.json or page fingerprint.
        qs = parse_qs(p.query)
        if "gh_jid" in qs:
            return Target(BoardRef("greenhouse", None), qs["gh_jid"][0], url)
        return None

    def board_from_url(self, url: str) -> BoardRef | None:
        p = urlsplit(url)
        segs = [s for s in p.path.split("/") if s]
        if (p.hostname or "").lower() in HOSTS and segs:
            return BoardRef("greenhouse", segs[0])
        return None

    async def fetch(self, target: Target, f: Fetcher) -> Posting:
        token = target.board.slug
        if not token:
            html = await f.get_text(target.url)
            m = _TOKEN_IN_PAGE.search(html)
            if not m or m.group(1).lower() in ("embed", "jobs"):
                raise NeedsFallback("greenhouse token not found on vanity page")
            token = m.group(1)
        d = await f.get_json(f"{API}/{token}/jobs/{target.posting_id}", params={"pay_transparency": "true"})
        return self.to_posting(d, token, target.board.company)

    @staticmethod
    def to_posting(d: dict, token: str, company_hint: str | None = None) -> Posting:
        req = d.get("requisition_id")
        if req and not re.fullmatch(r"[\w./-]{3,40}", str(req)):  # "See Opening ID" etc.
            req = None
        pay = None
        ranges = d.get("pay_input_ranges") or []
        if ranges:
            r = ranges[0]
            if r.get("min_cents") and r.get("max_cents"):
                pay = PayRange(min=r["min_cents"] / 100, max=r["max_cents"] / 100, currency=r.get("currency_type", "USD"),
                               note=r.get("title"))
        depts = d.get("departments") or []
        return Posting(
            ats="greenhouse",
            url=d.get("absolute_url") or "",
            title=d["title"],
            ats_posting_id=str(d["id"]),
            company=d.get("company_name") or company_hint or token.title(),
            job_ref=req,
            location=(d.get("location") or {}).get("name"),
            department=depts[0]["name"] if depts else None,
            posted_at=parse_date(d.get("first_published") or d.get("updated_at")),
            description_html=d.get("content"),
            pay=pay,
            raw=d,
        )

    async def list_postings(self, board: BoardRef, f: Fetcher) -> list[ListedPosting]:
        d = await f.get_json(f"{API}/{board.slug}/jobs")
        return [
            ListedPosting(
                ats_posting_id=str(j["id"]), url=j["absolute_url"], title=j["title"],
                location=(j.get("location") or {}).get("name"),
                posted_at=parse_date(j.get("first_published") or j.get("updated_at")),
            )
            for j in d.get("jobs", [])
        ]
