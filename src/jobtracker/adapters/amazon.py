"""Amazon Jobs (P2, undocumented): https://www.amazon.jobs/en/search.json

Not in the original ATS list but it is ~12% of the applications in the Notion tracker. The search endpoint
accepts a requisition id as `base_query` and returns the full posting with basic/preferred qualifications
already separated.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from urllib.parse import urlsplit

from ..http import Fetcher, PostingGone
from ..models import BoardRef, ListedBatch, ListedPosting, Posting, Section
from .base import Adapter, Target
from ..extract.sections import bullets_of
from ..extract.text import html_to_blocks

API = "https://www.amazon.jobs/en/search.json"


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(re.sub(r"\s+", " ", s.strip()), "%B %d, %Y").date()
    except ValueError:
        return None


def _bullets(html: str | None) -> list[str]:
    if not html:
        return []
    html = re.sub(r"<br\s*/?>", "\n", html)
    out: list[str] = []
    for b in html_to_blocks(html):
        out.extend(x.strip(" -•\t") for x in b.text.split("\n") if x.strip(" -•\t"))
    return out


class AmazonJobs(Adapter):
    ats = "amazon"

    def identify(self, url: str) -> Target | None:
        p = urlsplit(url)
        host = (p.hostname or "").lower()
        m = re.match(r"^/(?:[a-z]{2}(?:-[a-z]{2})?/)?jobs/(\d+)", p.path)
        if host in ("www.amazon.jobs", "amazon.jobs") and m:
            return Target(BoardRef("amazon", "amazon.jobs", "Amazon"), m.group(1), url)
        return None

    def board_from_url(self, url: str) -> BoardRef | None:
        host = (urlsplit(url).hostname or "").lower()
        if host in ("www.amazon.jobs", "amazon.jobs"):
            return BoardRef("amazon", "amazon.jobs", "Amazon", {"country": "IND", "query": "software development engineer"})
        return None

    async def fetch(self, target: Target, f: Fetcher) -> Posting:
        d = await f.get_json(API, params={"base_query": target.posting_id, "result_limit": 5})
        for j in d.get("jobs", []):
            if str(j.get("id_icims")) == target.posting_id:
                return self.to_posting(j)
        raise PostingGone(f"amazon job {target.posting_id} not found (closed?)")

    @staticmethod
    def to_posting(j: dict) -> Posting:
        sections: list[Section] = []
        desc = _bullets(j.get("description"))
        if desc:
            sections.append(Section(kind="responsibilities", heading="Description", bullets=desc, text="\n".join(desc)))
        basic, pref = _bullets(j.get("basic_qualifications")), _bullets(j.get("preferred_qualifications"))
        if basic:
            sections.append(Section(kind="requirements", heading="Basic qualifications", bullets=basic, text="\n".join(basic)))
        if pref:
            sections.append(Section(kind="nice_to_have", heading="Preferred qualifications", bullets=pref, text="\n".join(pref)))
        team = j.get("team") if isinstance(j.get("team"), dict) else {}
        return Posting(
            ats="amazon",
            url=f"https://www.amazon.jobs{j['job_path']}",
            title=j["title"].strip(),
            ats_posting_id=str(j["id_icims"]),
            company="Amazon",
            job_ref=str(j["id_icims"]),
            location=j.get("normalized_location") or j.get("location"),
            department=j.get("job_category"),
            posted_at=_parse_date(j.get("posted_date")),
            description_html=None,
            sections=sections,
            raw=j,
        )

    async def list_postings(self, board: BoardRef, f: Fetcher) -> list[ListedPosting]:
        out: list[ListedPosting] = []
        size, hits = 50, 0
        for page in range(int(board.config.get("max_pages", 4))):
            d = await f.get_json(API, params={
                "base_query": board.config.get("query", ""), "result_limit": size, "offset": page * size,
                "sort": "recent", "country": board.config.get("country", "IND"),
            })
            jobs = d.get("jobs", [])
            for j in jobs:
                out.append(ListedPosting(
                    ats_posting_id=str(j["id_icims"]), url=f"https://www.amazon.jobs{j['job_path']}", title=j["title"].strip(),
                    location=j.get("normalized_location"), posted_at=_parse_date(j.get("posted_date")), department=j.get("job_category"),
                ))
            hits = d.get("hits", 0)
            if len(jobs) < size or (page + 1) * size >= hits:
                break
        return ListedBatch(out, complete=len(out) >= hits)
