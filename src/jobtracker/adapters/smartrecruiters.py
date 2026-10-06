"""SmartRecruiters (P1): api.smartrecruiters.com/v1/companies/{id}/postings[/{postingId}]."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from ..http import Fetcher
from ..models import BoardRef, ListedBatch, ListedPosting, Posting
from .base import Adapter, Target, parse_date

API = "https://api.smartrecruiters.com/v1/companies"
_ID = re.compile(r"^(\d{6,})")


class SmartRecruiters(Adapter):
    ats = "smartrecruiters"

    def identify(self, url: str) -> Target | None:
        p = urlsplit(url)
        host = (p.hostname or "").lower()
        segs = [s for s in p.path.split("/") if s]
        if host in ("jobs.smartrecruiters.com", "careers.smartrecruiters.com") and len(segs) >= 2:
            m = _ID.match(segs[1])
            if m:
                return Target(BoardRef("smartrecruiters", segs[0]), m.group(1), url)
        if host == "api.smartrecruiters.com" and len(segs) >= 5 and segs[2] == "companies":
            return Target(BoardRef("smartrecruiters", segs[3]), segs[5] if len(segs) > 5 else "", url)
        return None

    def board_from_url(self, url: str) -> BoardRef | None:
        p = urlsplit(url)
        segs = [s for s in p.path.split("/") if s]
        if (p.hostname or "").lower() in ("jobs.smartrecruiters.com", "careers.smartrecruiters.com") and segs:
            return BoardRef("smartrecruiters", segs[0])
        return None

    async def fetch(self, target: Target, f: Fetcher) -> Posting:
        d = await f.get_json(f"{API}/{target.board.slug}/postings/{target.posting_id}")
        return self.to_posting(d, target.board)

    @staticmethod
    def to_posting(d: dict, board: BoardRef) -> Posting:
        loc = d.get("location") or {}
        mode = "Remote" if loc.get("remote") else "Hybrid" if loc.get("hybrid") else None
        full = loc.get("fullLocation") or ""
        # the API sometimes sends just ", " when it has no place; that is no location
        full_loc = (full if any(ch.isalnum() for ch in full) else "") or ", ".join(
            x for x in (loc.get("city"), loc.get("region"), (loc.get("country") or "").upper()) if x) or None
        full_loc = re.sub(r",\s*,", ",", full_loc or "") or None
        sections = (d.get("jobAd") or {}).get("sections") or {}
        order = ["companyDescription", "jobDescription", "qualifications", "additionalInformation"]
        html = "".join(
            f"<h2>{sections[k].get('title') or k}</h2>{sections[k].get('text', '')}"
            for k in order if k in sections and sections[k].get("text")
        )
        return Posting(
            ats="smartrecruiters",
            url=d.get("postingUrl") or d.get("applyUrl", ""),
            title=d["name"],
            ats_posting_id=str(d["id"]),
            company=(d.get("company") or {}).get("name") or board.company,
            job_ref=d.get("refNumber"),
            location=full_loc,
            work_mode=mode,  # type: ignore[arg-type]
            department=(d.get("department") or {}).get("label") or (d.get("function") or {}).get("label"),
            posted_at=parse_date(d.get("releasedDate")),
            description_html=html,
            raw=d,
        )

    async def list_postings(self, board: BoardRef, f: Fetcher) -> list[ListedPosting]:
        out: list[ListedPosting] = []
        offset, limit, total = 0, 100, 0
        max_pages = int(board.config.get("max_pages", 10))
        for _ in range(max_pages):
            d = await f.get_json(f"{API}/{board.slug}/postings", params={"limit": limit, "offset": offset})
            for j in d.get("content", []):
                loc = j.get("location") or {}
                out.append(ListedPosting(
                    ats_posting_id=str(j["id"]),
                    url=f"https://jobs.smartrecruiters.com/{board.slug}/{j['id']}",
                    title=j["name"],
                    location=loc.get("fullLocation"),
                    posted_at=parse_date(j.get("releasedDate")),
                    department=(j.get("department") or {}).get("label"),
                ))
            offset += limit
            total = d.get("totalFound", 0)
            if offset >= total:
                break
        return ListedBatch(out, complete=len(out) >= total)
