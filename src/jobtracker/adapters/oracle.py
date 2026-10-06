"""Oracle Cloud HCM Candidate Experience (P2, undocumented): /hcmRestApi/resources/latest/recruitingCE*."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from ..extract.facts import map_ats_mode
from ..extract.salary import find_pay_in_text
from ..http import Fetcher, PostingGone
from ..models import BoardRef, ListedBatch, ListedPosting, Posting, Section
from .base import Adapter, Target, parse_date

_PATH = re.compile(r"/hcmUI/CandidateExperience/(?P<lang>[^/]+)/sites/(?P<site>[^/]+)(?:/job/(?P<id>[^/?#]+))?", re.I)


class OracleHCM(Adapter):
    ats = "oracle"

    def _parse(self, url: str) -> tuple[BoardRef, str | None] | None:
        p = urlsplit(url)
        host = (p.hostname or "").lower()
        if ".oraclecloud.com" not in host or ".fa." not in host:
            return None
        m = _PATH.search(p.path)
        if not m:
            return None
        site = m.group("site")
        # siteNumber is what the REST API wants; for most tenants it equals the site name in the URL (CX_1001).
        return BoardRef("oracle", host, config={"host": host, "site": site, "siteNumber": site, "lang": m.group("lang")}), m.group("id")

    def identify(self, url: str) -> Target | None:
        r = self._parse(url)
        if r and r[1]:
            return Target(r[0], r[1], url)
        return None

    def board_from_url(self, url: str) -> BoardRef | None:
        r = self._parse(url)
        return r[0] if r else None

    @staticmethod
    def _api(board: BoardRef) -> str:
        return f"https://{board.config['host']}/hcmRestApi/resources/latest"

    @staticmethod
    def _public_url(board: BoardRef, job_id: str) -> str:
        c = board.config
        return f"https://{c['host']}/hcmUI/CandidateExperience/{c.get('lang', 'en')}/sites/{c['site']}/job/{job_id}"

    async def fetch(self, target: Target, f: Fetcher) -> Posting:
        b = target.board
        d = await f.get_json(
            f"{self._api(b)}/recruitingCEJobRequisitionDetails",
            params={"expand": "all", "onlyData": "true",
                    "finder": f'ById;Id="{target.posting_id}",siteNumber={b.config["siteNumber"]}'},
        )
        items = d.get("items") or []
        if not items:
            raise PostingGone(f"oracle requisition {target.posting_id} not found")
        return self.to_posting(items[0], b)

    def to_posting(self, j: dict, board: BoardRef) -> Posting:
        sections: list[Section] = []
        desc = j.get("ExternalDescriptionStr") or ""
        resp = j.get("ExternalResponsibilitiesStr") or ""
        qual = j.get("ExternalQualificationsStr") or ""
        html = desc
        if resp:
            html += f"<h2>Responsibilities</h2>{resp}"
        if qual:
            html += f"<h2>Qualifications</h2>{qual}"
        pay = None
        for ff in j.get("requisitionFlexFields") or []:
            if re.search(r"pay|salary|compensation", ff.get("Prompt", ""), re.I) and ff.get("Value"):
                pay = find_pay_in_text(f"Base pay salary: {ff['Value']}")
                if pay:
                    break
        locs = [j.get("PrimaryLocation")] + [s.get("Name") for s in (j.get("secondaryLocations") or []) if s.get("Name")]
        return Posting(
            ats="oracle",
            url=self._public_url(board, j["Id"]),
            title=j["Title"],
            ats_posting_id=str(j["Id"]),
            company=board.company,
            job_ref=str(j["Id"]),
            location="; ".join(dict.fromkeys(x for x in locs if x)) or None,
            work_mode=map_ats_mode(j.get("WorkplaceType") or j.get("WorkplaceTypeCode")),
            department=j.get("Category") or j.get("JobFamily") or j.get("BusinessUnit"),
            posted_at=parse_date(j.get("ExternalPostedStartDate") or j.get("PostedDate")),
            description_html=html,
            sections=sections or None,
            pay=pay,
            raw=j,
        )

    async def list_postings(self, board: BoardRef, f: Fetcher) -> list[ListedPosting]:
        out: list[ListedPosting] = []
        limit, total = 25, 0
        max_pages = int(board.config.get("max_pages", 8))
        for page in range(max_pages):
            d = await f.get_json(
                f"{self._api(board)}/recruitingCEJobRequisitions",
                params={"onlyData": "true", "expand": "requisitionList.secondaryLocations",
                        "finder": f"findReqs;siteNumber={board.config['siteNumber']},limit={limit},offset={page * limit},"
                                  f"sortBy=POSTING_DATES_DESC"},
            )
            head = (d.get("items") or [{}])[0]
            reqs = head.get("requisitionList") or []
            for j in reqs:
                out.append(ListedPosting(
                    ats_posting_id=str(j["Id"]),
                    url=self._public_url(board, j["Id"]),
                    title=j["Title"],
                    location=j.get("PrimaryLocation"),
                    posted_at=parse_date(j.get("PostedDate")),
                    department=j.get("JobFamily"),
                ))
            total = head.get("TotalJobsCount", 0)
            if not reqs or (page + 1) * limit >= total:
                break
        return ListedBatch(out, complete=len(out) >= total)
