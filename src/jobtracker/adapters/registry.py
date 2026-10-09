"""Pick the adapter + target for a URL: hosts.json (and the domains discovery learned) -> URL patterns -> generic.
What a page itself gives away is in discovery.py."""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
import re
from urllib.parse import urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from ..config import ROOT
from ..http import Fetcher
from ..models import BoardRef
from .amazon import AmazonJobs
from .ashby import Ashby
from .avature import Avature
from .base import Adapter, Target
from .eightfold import Eightfold
from .generic import Generic
from .icims import IcimsClassic
from .greenhouse import Greenhouse
from .jibe import Jibe
from .lever import Lever
from .oracle import OracleHCM
from .smartrecruiters import SmartRecruiters
from .workday import Workday

HOSTS_PATH = ROOT / "config" / "hosts.json"


@lru_cache
def adapters() -> dict[str, Adapter]:
    items: list[Adapter] = [Greenhouse(), Lever(), Ashby(), SmartRecruiters(), Workday(), OracleHCM(), Eightfold(), AmazonJobs(), Jibe(), Avature(), IcimsClassic(), Generic()]
    return {a.ats: a for a in items}


@lru_cache
def _file_hosts() -> dict[str, dict]:
    if not HOSTS_PATH.exists():
        return {}
    return {k.lower(): v for k, v in json.loads(HOSTS_PATH.read_text(encoding="utf-8")).items() if not k.startswith("_")}


_learned: dict[str, dict] = {}  # domains discovery worked out (stored in the database); the file wins over them


def load_hosts() -> dict[str, dict]:
    return {**_learned, **_file_hosts()}


def set_learned(entries: dict[str, dict]) -> None:
    _learned.clear()
    _learned.update({k.lower(): v for k, v in entries.items()})


def learn(host: str, entry: dict) -> None:
    _learned[host.lower()] = entry


@dataclass
class Resolved:
    adapter: Adapter
    target: Target


def _apply_entry(target: Target, entry: dict) -> Target:
    b = target.board
    if entry.get("company"):
        b.company = entry["company"]
    if entry.get("slug") and not b.slug:
        b.slug = entry["slug"]
    b.config = {**b.config, **entry.get("config", {})}
    return target


def resolve_static(url: str) -> Resolved | None:
    """No-network resolution: hosts.json (and learned domains), then URL patterns."""
    host = (urlsplit(url).hostname or "").lower()
    entry = load_hosts().get(host)
    reg = adapters()
    if entry:
        ad = reg[entry["ats"]]
        t = ad.identify(url)
        if t is None:
            pid = ad.posting_id_from_url(url)
            if pid:
                cfg = dict(entry.get("config", {}))
                slug = entry.get("slug") or cfg.get("host") or host
                t = Target(BoardRef(entry["ats"], slug, entry.get("company"), cfg), pid, url)
        if t is not None:
            return Resolved(ad, _apply_entry(t, entry))
    for name, ad in reg.items():
        if name == "html":
            continue
        t = ad.identify(url)
        if t is not None:
            return Resolved(ad, t)
    return None


async def resolve(url: str, f: Fetcher) -> Resolved:
    r = resolve_static(url)
    if r:
        return r
    html = None
    try:
        html = await f.get_text(url)
    except Exception:
        html = None
    return Resolved(adapters()["html"], Target(BoardRef("html"), "", url, html))


def delegate_from_html(html: str, page_url: str) -> Resolved | None:
    """Careers sites that only display jobs (Radancy TalentBrew, Phenom...) link their Apply button to the system that
    really holds the job. Return that job if there is exactly one such link: "similar jobs" links are not Apply links."""
    found: dict[str, Resolved] = {}
    for a in HTMLParser(html).css("a[href]"):
        href = urljoin(page_url, a.attributes.get("href") or "")
        if not (re.search(r"\bapply\b", (a.text(strip=True) or "").lower()) or href.rstrip("/").lower().endswith("/apply")):
            continue
        r = resolve_static(href)
        if r and r.adapter.ats != "html" and r.target.posting_id and r.target.url != page_url:
            found[r.target.url] = r
    return next(iter(found.values())) if len(found) == 1 else None


def board_from_url(url: str) -> tuple[Adapter, BoardRef] | None:
    """Board-level URL (for the watcher UI) -> (adapter, BoardRef)."""
    host = (urlsplit(url).hostname or "").lower()
    entry = load_hosts().get(host)
    for name, ad in adapters().items():
        if name == "html":
            continue
        b = ad.board_from_url(url)
        if b:
            if entry:
                _apply_entry(Target(b, "", url), entry)
            return ad, b
    if entry and entry["ats"] in ("jibe", "smartrecruiters", "greenhouse", "eightfold", "avature"):
        ad = adapters()[entry["ats"]]
        cfg = dict(entry.get("config", {}))
        if entry["ats"] == "jibe":
            return ad, Jibe.board_for_host(host, entry.get("company"))
        return ad, BoardRef(entry["ats"], entry.get("slug") or host, entry.get("company"), {"host": host, **cfg})
    return None
