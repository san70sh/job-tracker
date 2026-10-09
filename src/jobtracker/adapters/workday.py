"""Workday (P2, undocumented): POST /wday/cxs/{tenant}/{site}/jobs and GET /wday/cxs/{tenant}/{site}{externalPath}.

tenant, wd-number and site come from the careers URL; they are never guessed.
"""
from __future__ import annotations

import re
from dataclasses import replace
from datetime import date, timedelta
from urllib.parse import parse_qs, urlsplit, urlunsplit

from .. import filters
from ..extract.facts import map_ats_mode
from ..http import FetchError, Fetcher, PostingGone
from ..models import BoardRef, ListedBatch, ListedPosting, Posting
from .base import Adapter, Target, parse_date

_LOCALE = re.compile(r"^[a-z]{2}(?:-[A-Z]{2})?$")


# Workday's listing only says "Posted Today", "Posted Yesterday", "Posted 3 Days Ago" or "Posted 30+ Days Ago"
_DAYS_AGO = re.compile(r"(\d+)\+?\s*days?\s+ago", re.I)
_DAY_WORDS = {"today": 0, "yesterday": 1}


def posted_on(text: str | None, today: date | None = None) -> date | None:
    """The date behind Workday's 'Posted ...' text (for '30+ Days Ago', 30 days back: at least that old, so old either way)."""
    today = today or date.today()
    low = (text or "").lower()
    m = _DAYS_AGO.search(low)
    if m:
        return today - timedelta(days=int(m.group(1)))
    return next((today - timedelta(days=n) for word, n in _DAY_WORDS.items() if word in low), None)


# address parameters that are not Workday filters ("q" is the search box; the rest is tracking)
_NOT_FILTERS = {"q", "redirect", "source", "sourceid"}


def _address_filters(query: str) -> dict:
    """The filters Workday keeps in its page address (?locations=ID&jobFamilyGroup=ID&q=text) as an API request."""
    qs = {k: v for k, v in parse_qs(query).items() if k.lower() not in _NOT_FILTERS and not k.lower().startswith("utm_")}
    out: dict = {"facets": qs} if qs else {}
    if parse_qs(query).get("q"):
        out["search_text"] = parse_qs(query)["q"][0]
    return out


def _place_group(facets: list[dict]) -> tuple[str | None, list[dict]]:
    """(parameter, [{id, descriptor}]) of the tenant's place filter: the group whose name mentions location.
    Tenants nest it differently (Barclays: locationMainGroup > locations), so the tree is searched, not indexed."""
    for group in facets:
        values = group.get("values") or []
        leaves = [v for v in values if v.get("id")]
        if leaves and "location" in str(group.get("facetParameter", "")).lower():
            return group["facetParameter"], leaves
        found = _place_group([v for v in values if v.get("values")])
        if found[0]:
            return found
    return None, []


def _job_segments(rest: list[str]) -> list[str]:
    """['job', place, 'Title_ID', 'apply', ...] -> ['job', place, 'Title_ID']. Workday's own API knows only the job
    page, not its /apply form, so an apply link (what LinkedIn's "Go to company site" gives) must be cut back to it."""
    cut = next((i for i, s in enumerate(rest) if i >= 2 and s.lower() == "apply"), len(rest))
    return rest[:cut]


class Workday(Adapter):
    ats = "workday"

    @staticmethod
    def _board(host: str, segs: list[str]) -> tuple[BoardRef, list[str]] | None:
        if not host.endswith(".myworkdayjobs.com"):
            return None
        tenant = host.split(".")[0]
        rest = segs[:]
        if rest and _LOCALE.match(rest[0]):
            rest = rest[1:]
        if not rest:
            return None
        site = rest[0]
        return BoardRef("workday", tenant, config={"host": host, "site": site}), rest[1:]

    def identify(self, url: str) -> Target | None:
        p = urlsplit(url)
        segs = [s for s in p.path.split("/") if s]
        b = self._board((p.hostname or "").lower(), segs)
        if not b:
            return None
        board, rest = b
        if rest and rest[0] == "job" and len(rest) >= 2:
            rest = _job_segments(rest)
            m = re.search(r"_([A-Za-z0-9-]+)$", rest[-1])
            clean = urlunsplit((p.scheme, p.netloc, "/" + "/".join(segs[: len(segs) - (len(b[1]) - len(rest))]), "", ""))
            return Target(board, m.group(1) if m else rest[-1], clean)
        return None

    def board_from_url(self, url: str) -> BoardRef | None:
        p = urlsplit(url)
        b = self._board((p.hostname or "").lower(), [s for s in p.path.split("/") if s])
        if not b:
            return None
        b[0].config.update(_address_filters(p.query))  # a filtered address watches that filtered view
        return b[0]

    async def resolve_location_filter(self, board: BoardRef, terms: list[str], f: Fetcher) -> tuple[BoardRef, bool]:
        """Ask only for the tenant's offices whose name matches the terms. Offices are looked up on every check,
        so a new matching office is picked up without editing the board. Nothing matching leaves the listing as is."""
        if not terms:
            return board, False
        cfg = board.config
        d = await f.post_json(self._base(board) + "/jobs", {"appliedFacets": cfg.get("facets", {}), "limit": 1, "offset": 0,
                                                              "searchText": cfg.get("search_text", "")})
        param, offices = _place_group(d.get("facets") or [])
        ids = [o["id"] for o in offices if filters.any_match(o.get("descriptor"), terms, location=True)]
        if not param or not ids:
            return board, False
        return replace(board, config={**cfg, "facets": {**cfg.get("facets", {}), param: ids}}), True

    @staticmethod
    def _base(board: BoardRef) -> str:
        return f"https://{board.config['host']}/wday/cxs/{board.slug}/{board.config['site']}"

    async def fetch(self, target: Target, f: Fetcher) -> Posting:
        p = urlsplit(target.url)
        segs = [s for s in p.path.split("/") if s]
        if segs and _LOCALE.match(segs[0]):
            segs = segs[1:]
        i = segs.index("job")
        external_path = "/" + "/".join(_job_segments(segs[i:]))
        try:
            d = await f.get_json(self._base(target.board) + external_path)
        except FetchError as e:
            # Workday answers a closed/removed posting with 403 errorCode S22 ("permission denied"), not 404.
            if "403" in str(e) and "S22" in str(e):
                raise PostingGone(f"workday posting closed: {target.url}") from e
            raise
        return self.to_posting(d, target.board, target.url)

    @staticmethod
    def to_posting(d: dict, board: BoardRef, url: str) -> Posting:
        info = d["jobPostingInfo"]
        return Posting(
            ats="workday",
            url=info.get("externalUrl") or url,
            title=info["title"],
            ats_posting_id=info.get("jobPostingId"),
            company=board.company or board.slug.replace("-", " ").title(),
            job_ref=info.get("jobReqId"),
            location=info.get("location"),
            work_mode=map_ats_mode(info.get("remoteType")),
            posted_at=parse_date(info.get("startDate")),
            description_html=info.get("jobDescription"),
            raw=d,
        )

    async def list_postings(self, board: BoardRef, f: Fetcher) -> list[ListedPosting]:
        out: list[ListedPosting] = []
        limit, total, skipped = 20, 0, 0
        max_pages = int(board.config.get("max_pages", 10))
        search = board.config.get("search_text", "")
        for page in range(max_pages):
            d = await f.post_json(
                self._base(board) + "/jobs",
                {"appliedFacets": board.config.get("facets", {}), "limit": limit, "offset": page * limit, "searchText": search},
            )
            for j in d.get("jobPostings", []):
                if not j.get("title") or not j.get("externalPath"):
                    skipped += 1  # Workday sometimes lists a bare requisition number: no title or page, so not a posting
                    continue
                ext = j["externalPath"]
                req = (j.get("bulletFields") or [None])[0] or ext.rsplit("_", 1)[-1]
                out.append(ListedPosting(
                    ats_posting_id=req,
                    url=f"https://{board.config['host']}/{board.config['site']}{ext}",
                    title=j["title"],
                    location=j.get("locationsText"),
                    posted_at=posted_on(j.get("postedOn")),
                ))
            total = d.get("total", 0)
            if (page + 1) * limit >= total or not d.get("jobPostings"):
                break
        return ListedBatch(out, complete=len(out) + skipped >= total)  # skipped ones are in Workday's total too
