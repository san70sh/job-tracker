"""Eightfold (P2, undocumented): /api/pcsx/search and /api/pcsx/position_details.

`domain` is the employer's email domain Eightfold keys on (paypal.com, microsoft.com). For
`{x}.eightfold.ai` hosts it defaults to `{x}.com`; override via config/hosts.json.

Tenants differ in which listing interface they allow: most the newer /api/pcsx/search, some (HSBC) only the older
/api/apply/v2/jobs and answer the newer one with "PCSX is not enabled". The list tries the newer, then the older.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

from ..extract.facts import map_ats_mode
from ..http import FetchError, Fetcher, PostingGone
from ..models import BoardRef, ListedBatch, ListedPosting, Posting
from .base import Adapter, Target, parse_date

_JOB_PATH = re.compile(r"/careers/job/(\d+)")
# Eightfold's own services also live under eightfold.ai (error reporting, assets); they are not employers
SERVICE_HOSTS = {"vs-errors", "static", "cdn", "www", "app", "api", "login", "status"}
# what the search page keeps in its address, as each listing interface names it (the page says "skill", the API "skills")
_ADDRESS_FILTERS = {"location": "location", "skill": "skills", "skills": "skills", "query": "query"}
_PCSX_FILTERS = ("location", "query")  # the newer interface ignores "skills" (checked on a live tenant), so it is not sent
_PCSX_OFF = "PCSX is not enabled"
# an Eightfold-built page names itself and the employer's domain
_FRONT_END = re.compile(r"hide_eightfold_branding|eightfold-font-base|[\w-]+\.eightfold\.ai", re.I)
_DOMAIN_IN_PAGE = re.compile(r"[?&;]domain=([a-z0-9-]+(?:\.[a-z0-9-]+)+)", re.I)


def _address_filters(query: str) -> dict[str, str]:
    qs = parse_qs(query)
    return {api: qs[name][0] for name, api in _ADDRESS_FILTERS.items() if qs.get(name) and qs[name][0]}


def _own_domain_entry(host: str) -> dict | None:
    """hosts.json entry (or a learned one) of a company domain that serves Eightfold's pages."""
    from .registry import load_hosts  # imported here: registry imports this module

    e = load_hosts().get(host)
    return e if e and e.get("ats") == "eightfold" else None


def default_domain(host: str) -> str:
    if host.endswith(".eightfold.ai"):
        return host.split(".")[0] + ".com"
    parts = host.split(".")
    return ".".join(parts[-2:])


def _from_older(p: dict) -> dict:
    """A position of the older interface, in the newer one's shape (only what the listing uses)."""
    return {"id": p["id"], "name": p["name"], "locations": p.get("locations") or ([p["location"]] if p.get("location") else []),
            "postedTs": p.get("t_create"), "department": p.get("department") or p.get("business_unit"),
            "positionUrl": urlsplit(p.get("canonicalPositionUrl") or "").path or None}


class Eightfold(Adapter):
    ats = "eightfold"

    def _board(self, url: str) -> BoardRef | None:
        p = urlsplit(url)
        host = (p.hostname or "").lower()
        own = _own_domain_entry(host)
        if own:
            domain = (own.get("config") or {}).get("domain") or default_domain(host)
        elif host.endswith(".eightfold.ai") and host.split(".")[0] not in SERVICE_HOSTS:
            domain = default_domain(host)
        else:
            return None
        config = {"host": host, "domain": domain}
        if facets := _address_filters(p.query):
            config["facets"] = facets
        return BoardRef("eightfold", host, config=config)

    def recognise(self, html: str, page_url: str) -> BoardRef | None:
        """A company page built by Eightfold: it names Eightfold and the employer's domain. Its own host serves the data."""
        host = (urlsplit(page_url).hostname or "").lower()
        domain = _DOMAIN_IN_PAGE.search(html)
        if not (domain and _FRONT_END.search(html)):
            return None
        config = {"host": host, "domain": domain.group(1).lower()}
        if facets := _address_filters(urlsplit(page_url).query):
            config["facets"] = facets
        return BoardRef("eightfold", host, company=config["domain"].split(".")[0].title(), config=config)  # hsbc.com -> Hsbc

    def identify(self, url: str) -> Target | None:
        board = self._board(url)
        m = _JOB_PATH.search(urlsplit(url).path)
        if board and m:
            return Target(board, m.group(1), url)
        return None

    def board_from_url(self, url: str) -> BoardRef | None:
        return self._board(url)

    @staticmethod
    def _api(board: BoardRef) -> str:
        return f"https://{board.config['host']}/api/pcsx"

    async def fetch(self, target: Target, f: Fetcher) -> Posting:
        b = target.board
        d = await f.get_json(
            f"{self._api(b)}/position_details",
            params={"position_id": target.posting_id, "domain": b.config["domain"], "hl": "en"},
        )
        data = d.get("data") or {}
        if not data.get("id"):
            raise PostingGone(f"eightfold position {target.posting_id} not found")
        return self.to_posting(data, b)

    @staticmethod
    def to_posting(j: dict, board: BoardRef) -> Posting:
        locs = j.get("locations") or ([j["location"]] if j.get("location") else [])
        return Posting(
            ats="eightfold",
            url=j.get("publicUrl") or f"https://{board.config['host']}{j.get('positionUrl', '')}",
            title=j["name"],
            ats_posting_id=str(j["id"]),
            company=board.company or board.config["domain"].split(".")[0].title(),
            job_ref=j.get("displayJobId") or j.get("atsJobId"),
            location="; ".join(locs) or None,
            work_mode=map_ats_mode(j.get("workLocationOption")),
            department=j.get("department"),
            posted_at=parse_date(j.get("postedTs")),
            description_html=j.get("jobDescription"),
            raw=j,
        )

    async def _search(self, board: BoardRef, f: Fetcher, start: int, num: int, older: bool) -> tuple[list[dict], int, bool]:
        """One page of the listing: (positions in the newer interface's shape, total, whether the older interface was used)."""
        facets = board.config.get("facets", {})
        if not older:
            wanted = {k: v for k, v in facets.items() if k in _PCSX_FILTERS}
            try:
                d = await f.get_json(f"{self._api(board)}/search", params={
                    "domain": board.config["domain"], "query": board.config.get("query", ""), "start": start, "num": num,
                    "sort_by": "timestamp", **wanted})
                data = d.get("data") or {}
                return data.get("positions") or [], data.get("count", 0), False
            except FetchError as e:
                if _PCSX_OFF not in str(e):
                    raise
        d = await f.get_json(f"https://{board.config['host']}/api/apply/v2/jobs",
                             params={"domain": board.config["domain"], "start": start, "num": num, **facets})
        return [_from_older(p) for p in d.get("positions") or []], d.get("count", 0), True

    async def list_postings(self, board: BoardRef, f: Fetcher) -> list[ListedPosting]:
        out: list[ListedPosting] = []
        num, total, older = 10, 0, False
        max_pages = int(board.config.get("max_pages", 10))
        for page in range(max_pages):
            positions, total, older = await self._search(board, f, page * num, num, older)  # once refused, stay on the older one
            for j in positions:
                out.append(ListedPosting(
                    ats_posting_id=str(j["id"]),
                    url=f"https://{board.config['host']}{j.get('positionUrl') or '/careers/job/' + str(j['id'])}",
                    title=j["name"],
                    location="; ".join(j.get("locations") or []) or None,
                    posted_at=parse_date(j.get("postedTs")),
                    department=j.get("department"),
                ))
            if not positions or (page + 1) * num >= total:
                break
        return ListedBatch(out, complete=len(out) >= total)
