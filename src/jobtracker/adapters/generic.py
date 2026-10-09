"""P4 generic fallback: JSON-LD JobPosting, else best-effort main-content HTML.

Also used by iCIMS (icims.py), whose job pages carry JSON-LD.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from ..extract.facts import map_ats_mode
from ..http import Fetcher
from ..models import BoardRef, PayRange, Posting
from .base import Adapter, Target, parse_date


def _jsonld_blocks(tree: HTMLParser) -> list[dict]:
    out: list[dict] = []
    for s in tree.css('script[type="application/ld+json"]'):
        raw = (s.text() or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            try:  # some sites embed raw newlines in strings
                data = json.loads(re.sub(r"[\x00-\x1f]+", " ", raw))
            except json.JSONDecodeError:
                continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                if "@graph" in node:
                    stack.extend(node["@graph"] if isinstance(node["@graph"], list) else [node["@graph"]])
                out.append(node)
    return out


def _is_job(node: dict) -> bool:
    t = node.get("@type")
    return t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t)


def _text(v) -> str | None:
    if isinstance(v, dict):
        return v.get("name") or v.get("value") or None
    if isinstance(v, list):
        return _text(v[0]) if v else None
    return str(v) if v not in (None, "") else None


def _location(job: dict) -> str | None:
    locs = job.get("jobLocation")
    if isinstance(locs, dict):
        locs = [locs]
    parts: list[str] = []
    for loc in locs or []:
        addr = (loc or {}).get("address") or {}
        if isinstance(addr, str):
            parts.append(addr)
            continue
        bits = [addr.get("addressLocality"), addr.get("addressRegion"), _text(addr.get("addressCountry"))]
        s = ", ".join(str(b) for b in bits if b)
        if s:
            parts.append(s)
    return "; ".join(dict.fromkeys(parts)) or None


def _pay(job: dict) -> PayRange | None:
    bs = job.get("baseSalary")
    if not isinstance(bs, dict):
        return None
    val = bs.get("value") if isinstance(bs.get("value"), dict) else bs
    lo, hi = val.get("minValue"), val.get("maxValue")
    if lo is None and hi is None and val.get("value") is not None:
        lo = hi = val.get("value")
    try:
        lo, hi = float(lo), float(hi if hi is not None else lo)
    except (TypeError, ValueError):
        return None
    if hi <= 0:
        return None
    unit = str(val.get("unitText") or "YEAR").upper()
    interval = {"YEAR": "year", "MONTH": "month", "HOUR": "hour"}.get(unit, "year")
    return PayRange(min=lo, max=hi, currency=bs.get("currency") or val.get("currency") or "USD", interval=interval)  # type: ignore[arg-type]


def parse_html(html: str, url: str, ats: str = "html", company_hint: str | None = None) -> Posting:
    tree = HTMLParser(html)
    job = next((n for n in _jsonld_blocks(tree) if _is_job(n)), None)
    if job:
        org = job.get("hiringOrganization")
        ident = job.get("identifier")
        ref = _text(ident) if ident else None
        mode = "Remote" if str(job.get("jobLocationType", "")).upper() == "TELECOMMUTE" else None
        return Posting(
            ats=ats, url=url,
            title=_text(job.get("title")) or "",
            ats_posting_id=ref,
            company=company_hint or _text(org),
            job_ref=ref,
            location=_location(job),
            work_mode=mode,  # type: ignore[arg-type]
            department=_text(job.get("industry")) or _text(job.get("occupationalCategory")),
            posted_at=parse_date(job.get("datePosted")),
            description_html=job.get("description"),
            pay=_pay(job),
            raw={"jsonld": job},
        )
    # No structured data: best-effort.
    title = None
    for sel in ('meta[property="og:title"]', 'meta[name="twitter:title"]'):
        m = tree.css_first(sel)
        if m and m.attributes.get("content"):
            title = m.attributes["content"]
            break
    if not title:
        h1 = tree.css_first("h1")
        title = h1.text(strip=True) if h1 else (tree.css_first("title").text(strip=True) if tree.css_first("title") else "")
    site = tree.css_first('meta[property="og:site_name"]')
    body = _main_html(tree)
    return Posting(
        ats=ats, url=url, title=title or "",
        company=company_hint or (site.attributes.get("content") if site else None) or (urlsplit(url).hostname or "").removeprefix("www.").split(".")[0].title(),
        description_html=body,
        raw={"html_len": len(html)},
    )


def _main_html(tree: HTMLParser) -> str | None:
    for bad in tree.css("script, style, nav, header, footer, noscript, form, aside"):
        bad.decompose()
    best, best_len = None, 0
    for sel in ("main", "article", '[role="main"]', "#content", ".job-description", '[class*="description"]', '[class*="job"]', "body"):
        for n in tree.css(sel):
            ln = len(n.text(deep=True))
            if ln > best_len and ln > 400:
                best, best_len = n, ln
        if best is not None and sel != "body" and best_len > 1200:
            break
    return best.html if best is not None else None


class Generic(Adapter):
    ats = "html"

    def identify(self, url: str) -> Target | None:
        return Target(BoardRef("html"), "", url)  # never matches by itself; registry uses it as last resort

    async def fetch(self, target: Target, f: Fetcher) -> Posting:
        html = await f.get_text(target.url)
        return parse_html(html, target.url, "html", target.board.company)
