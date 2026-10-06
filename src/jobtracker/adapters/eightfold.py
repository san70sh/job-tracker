"""Eightfold (P2, undocumented): /api/pcsx/search and /api/pcsx/position_details.

`domain` is the employer's email domain Eightfold keys on (paypal.com, microsoft.com). For
`{x}.eightfold.ai` hosts it defaults to `{x}.com`; override via config/hosts.json.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from ..extract.facts import map_ats_mode
from ..http import Fetcher, PostingGone
from ..models import BoardRef, ListedBatch, ListedPosting, Posting
from .base import Adapter, Target, parse_date

_JOB_PATH = re.compile(r"/careers/job/(\d+)")


def default_domain(host: str) -> str:
    if host.endswith(".eightfold.ai"):
        return host.split(".")[0] + ".com"
    parts = host.split(".")
    return ".".join(parts[-2:])


class Eightfold(Adapter):
    ats = "eightfold"

    def _board(self, url: str) -> BoardRef | None:
        host = (urlsplit(url).hostname or "").lower()
        if not host.endswith(".eightfold.ai"):
            return None
        return BoardRef("eightfold", host, config={"host": host, "domain": default_domain(host)})

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

    async def list_postings(self, board: BoardRef, f: Fetcher) -> list[ListedPosting]:
        out: list[ListedPosting] = []
        num, total = 10, 0
        max_pages = int(board.config.get("max_pages", 10))
        for page in range(max_pages):
            d = await f.get_json(
                f"{self._api(board)}/search",
                params={"domain": board.config["domain"], "query": board.config.get("query", ""),
                        "start": page * num, "num": num, "sort_by": "timestamp"},
            )
            data = d.get("data") or {}
            positions = data.get("positions") or []
            for j in positions:
                out.append(ListedPosting(
                    ats_posting_id=str(j["id"]),
                    url=f"https://{board.config['host']}{j.get('positionUrl') or '/careers/job/' + str(j['id'])}",
                    title=j["name"],
                    location="; ".join(j.get("locations") or []) or None,
                    posted_at=parse_date(j.get("postedTs")),
                    department=j.get("department"),
                ))
            total = data.get("count", 0)
            if not positions or (page + 1) * num >= total:
                break
        return ListedBatch(out, complete=len(out) >= total)
