from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, date, datetime
from urllib.parse import parse_qs, urlsplit

from ..http import Fetcher
from ..models import BoardRef, ListedPosting, Posting


class Unsupported(Exception):
    """Adapter cannot do this operation (e.g. listing a classic iCIMS board)."""


class NeedsFallback(Exception):
    """The structured source could not be used; the caller should degrade to the generic parser."""


@dataclass
class Target:
    """A single posting identified within a board."""

    board: BoardRef
    posting_id: str
    url: str
    html: str | None = None  # page already fetched during fingerprinting; reused by the generic parser


_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


class Adapter(ABC):
    ats: str

    def posting_id_from_url(self, url: str) -> str | None:
        """Posting id from a vanity-domain URL whose board is configured in hosts.json."""
        p = urlsplit(url)
        qs = parse_qs(p.query)
        for key in ("gh_jid", "jid", "id", "job", "jobId", "reqId"):
            if key in qs:
                return qs[key][0]
        for seg in reversed([s for s in p.path.split("/") if s]):
            m = _UUID.search(seg) or re.match(r"^(\d{5,})", seg)
            if m:
                return m.group(0) if _UUID.search(seg) else m.group(1)
        return None

    @abstractmethod
    def identify(self, url: str) -> Target | None:
        """Recognise `url` by host/path alone (no network). None = not mine."""

    @abstractmethod
    async def fetch(self, target: Target, f: Fetcher) -> Posting: ...

    async def list_postings(self, board: BoardRef, f: Fetcher) -> list[ListedPosting]:
        raise Unsupported(f"{self.ats} does not support board listing")

    async def resolve_location_filter(self, board: BoardRef, terms: list[str], f: Fetcher) -> tuple[BoardRef, bool]:
        """Narrow the listing to `terms` at the source, for systems that can filter by place. Returns the board to
        list and whether the terms were applied there. Default: no, the posting's own place text is filtered later."""
        return board, False

    def board_from_url(self, url: str) -> BoardRef | None:
        """Board-level URL (e.g. https://boards.greenhouse.io/stripe) -> BoardRef, for the watcher UI."""
        t = self.identify(url)
        return t.board if t else None

    def recognise(self, html: str, page_url: str) -> BoardRef | None:
        """Spot this system from a page's own content, for company domains that front it (the address says nothing).
        Only for pages that ARE this system's front end; a mere link to it belongs to address recognition. Default: no."""
        return None

    def host_entry(self, board: BoardRef) -> dict:
        """The config/hosts.json entry that would resolve this board's company domain later; what discovery remembers.
        Address-only settings (the filters in a pasted URL) are left out, they belong to one address, not the domain."""
        return {"ats": self.ats, "slug": board.slug, "company": board.company,
                "config": {k: v for k, v in board.config.items() if k != "facets"}}


def parse_date(value: str | int | float | None) -> date | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            ts = float(value)
            if ts > 1e11:  # milliseconds
                ts /= 1000
            return datetime.fromtimestamp(ts, UTC).date()
        s = str(value).strip()
        if len(s) >= 10 and s[4] == "-":
            return date.fromisoformat(s[:10])
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except (ValueError, OverflowError, OSError):
        return None
