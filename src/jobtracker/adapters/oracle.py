"""Oracle Cloud HCM Candidate Experience (P2, undocumented): /hcmRestApi/resources/latest/recruitingCE*."""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

from ..extract.facts import map_ats_mode
from ..extract.salary import find_pay_in_text
from ..http import Fetcher, PostingGone
from ..models import BoardRef, ListedBatch, ListedPosting, Posting, Section
from .base import Adapter, Target, parse_date

_PATH = re.compile(r"/hcmUI/CandidateExperience/(?P<lang>[^/]+)/sites/(?P<site>[^/]+)(?:/job/(?P<id>[^/?#]+))?", re.I)
# the same pages on a company's own domain (careers.example.com/en/sites/CX_1/jobs or .../job/123)
_OWN_PATH = re.compile(r"^/(?P<lang>[a-z]{2}(?:-[A-Za-z]{2})?)/sites/(?P<site>[^/]+)(?:/job/(?P<id>[^/?#]+)|/jobs)?/?$", re.I)
# what an Oracle-powered page gives away about itself
_ORACLE_HOST = re.compile(r"([\w-]+\.fa\.[\w.-]*oraclecloud\.com)", re.I)
_SITE_NUMBER = re.compile(r"siteNumber=([\w-]+)", re.I)
# filters the search page keeps in its address (?selectedLocationsFacet=ID&selectedFlexFieldsFacets="Field|Value")
_FACET_PARAM = re.compile(r"^selected\w*Facets?$", re.I)


def _own_domain_entry(host: str) -> dict | None:
    """hosts.json entry of a company domain that fronts an Oracle site (its `config.host` is the Oracle host)."""
    from .registry import load_hosts  # imported here: registry imports this module

    e = load_hosts().get(host)
    return e if e and e.get("ats") == "oracle" and (e.get("config") or {}).get("host") else None


def _address_facets(query: str) -> dict[str, str]:
    """The search filters in a pasted page address, as the API's finder parameters."""
    return {k: v[0] for k, v in parse_qs(query).items() if _FACET_PARAM.match(k) and v and v[0]}


class OracleHCM(Adapter):
    ats = "oracle"

    def _parse(self, url: str) -> tuple[BoardRef, str | None] | None:
        p = urlsplit(url)
        host = (p.hostname or "").lower()
        own = _own_domain_entry(host)
        if own:
            m, api_host, public = _OWN_PATH.match(p.path), own["config"]["host"], host
        elif ".oraclecloud.com" in host and ".fa." in host:
            m, api_host, public = _PATH.search(p.path), host, None
        else:
            return None
        if not m:
            return None
        site = m.group("site")
        # siteNumber is what the REST API wants; for most tenants it equals the site name in the URL (CX_1001).
        config = {"host": api_host, "site": site, "siteNumber": site, "lang": m.group("lang")}
        if public:
            config["public_host"] = public  # postings are linked on the company's domain, as its own pages are
        if facets := _address_facets(p.query):
            config["facets"] = facets
        return BoardRef("oracle", api_host, config=config), m.group("id")

    def identify(self, url: str) -> Target | None:
        r = self._parse(url)
        if r and r[1]:
            return Target(r[0], r[1], url)
        return None

    def board_from_url(self, url: str) -> BoardRef | None:
        r = self._parse(url)
        return r[0] if r else None

    def recognise(self, html: str, page_url: str) -> BoardRef | None:
        """A company page that is Oracle's candidate site: the page itself names the Oracle host and the site number."""
        api, site = _ORACLE_HOST.search(html), _SITE_NUMBER.search(html)
        if not (api and site):
            return None
        p = urlsplit(page_url)
        host, oracle_host, m = (p.hostname or "").lower(), api.group(1).lower(), _OWN_PATH.match(p.path)
        config = {"host": oracle_host, "site": site.group(1), "siteNumber": site.group(1), "lang": m.group("lang") if m else "en"}
        if m and host != oracle_host:
            config["public_host"] = host  # its own pages follow the /lang/sites/SITE/job/ID pattern, so postings are linked that way
        if facets := _address_facets(p.query):
            config["facets"] = facets
        return BoardRef("oracle", oracle_host, config=config)

    def host_entry(self, board: BoardRef) -> dict:
        """Only the Oracle host: the site comes from each address, so one domain can serve several sites."""
        return {"ats": self.ats, "company": board.company, "config": {"host": board.config["host"]}}

    @staticmethod
    def _api(board: BoardRef) -> str:
        return f"https://{board.config['host']}/hcmRestApi/resources/latest"

    @staticmethod
    def _public_url(board: BoardRef, job_id: str) -> str:
        c = board.config
        if c.get("public_host"):
            return f"https://{c['public_host']}/{c.get('lang', 'en')}/sites/{c['site']}/job/{job_id}"
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
        facets = "".join(f",{k}={v}" for k, v in board.config.get("facets", {}).items())
        for page in range(max_pages):
            d = await f.get_json(
                f"{self._api(board)}/recruitingCEJobRequisitions",
                params={"onlyData": "true", "expand": "requisitionList.secondaryLocations",
                        "finder": f"findReqs;siteNumber={board.config['siteNumber']},limit={limit},offset={page * limit},"
                                  f"sortBy=POSTING_DATES_DESC{facets}"},
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
