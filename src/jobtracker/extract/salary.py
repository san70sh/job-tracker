"""Deterministic salary resolution (all values in INR lakhs per annum, LPA). India-only scope.

Order: 1) a pay range printed in the posting  2) cached benchmark rows  3) nothing (left blank, flagged).
A posted range in any currency other than INR is ignored (no FX conversion: the app only tracks India roles).
No scraping here: benchmarks come from `salary_benchmarks`, which the user fills by CSV import.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from ..models import PayRange

LAKH = 100_000
HOURS_PER_YEAR = 2080
SYMBOLS = {"$": "USD", "£": "GBP", "€": "EUR", "₹": "INR"}


@dataclass
class Benchmark:
    total_annual: float  # INR per year
    company: str | None = None
    level_norm: str | None = None
    location_norm: str | None = None
    kind: str = "aggregate_avg"
    source: str = "manual"
    source_url: str | None = None
    observed_at: date | None = None
    years_exp: float | None = None
    base: float | None = None
    stock_annual: float | None = None
    bonus_annual: float | None = None
    id: str | None = None


@dataclass
class SalaryResult:
    min_lpa: float
    max_lpa: float
    method: str  # posted_range | percentile_band
    explanation: str
    sources: list[str] = field(default_factory=list)
    benchmark_ids: list[str] = field(default_factory=list)


# ───────────────────────────── posted ranges in free text ─────────────────────────────
_NUM = r"(\d{1,3}(?:,\d{2,3})*(?:\.\d+)?|\d+(?:\.\d+)?)"
_CUR = r"(\$|£|€|₹|USD|GBP|EUR|INR|Rs\.?)"
_RANGE = re.compile(
    rf"{_CUR}\s?{_NUM}\s*(k|K|lpa|LPA|lakhs?|lacs?|L)?\s*(?:-|–|—|to)\s*{_CUR}?\s?{_NUM}\s*(k|K|lpa|LPA|lakhs?|lacs?|L)?",
)
_PAY_WORDS = re.compile(r"salary|pay|compensation|base|range|ctc|remuneration|per (?:year|annum|hour)|annual", re.I)


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def _cur(tok: str) -> str:
    tok = tok.strip(". ").upper()
    return SYMBOLS.get(tok, "INR" if tok in ("RS", "INR") else tok)


def find_pay_in_text(text: str) -> PayRange | None:
    for m in _RANGE.finditer(text):
        cur = _cur(m.group(1))
        lo, hi = _num(m.group(2)), _num(m.group(5))
        unit1, unit2 = (m.group(3) or "").lower(), (m.group(6) or "").lower()
        unit = unit2 or unit1
        window = text[max(0, m.start() - 120): m.end() + 120]
        if not _PAY_WORDS.search(window):
            continue
        if unit in ("lpa", "lakh", "lakhs", "lac", "lacs", "l"):
            lo, hi, cur = lo * LAKH, hi * LAKH, "INR"
        elif unit == "k":
            lo, hi = lo * 1000, hi * 1000
        interval = "year"
        if re.search(r"per hour|/\s?hr|hourly|an hour", window, re.I) or hi < 500:
            interval = "hour"
        elif re.search(r"per month|monthly|/\s?mo\b", window, re.I):
            interval = "month"
        if interval == "year" and hi < 10_000:  # not a plausible annual figure
            continue
        if lo > hi:
            continue
        note = re.sub(r"\s+", " ", text[max(0, m.start() - 40): m.start()]).strip(" :-–—") or None
        return PayRange(min=lo, max=hi, currency=cur, interval=interval, note=note)  # type: ignore[arg-type]
    return None


# ───────────────────────────── conversion ─────────────────────────────
def to_annual(amount: float, interval: str) -> float:
    return {"year": amount, "month": amount * 12, "hour": amount * HOURS_PER_YEAR}[interval]


def from_pay_range(pay: PayRange, location: str | None = None) -> SalaryResult | None:
    if pay.currency != "INR":
        return None
    lo = round(to_annual(pay.min, pay.interval) / LAKH, 1)
    hi = round(to_annual(pay.max, pay.interval) / LAKH, 1)
    where = f" for {pay.note}" if pay.note else (f" for {location}" if location else "")
    return SalaryResult(lo, hi, "posted_range", f"Pay range published in the posting{where}.", sources=["Job posting"])


# ───────────────────────────── benchmark lookup ─────────────────────────────
def from_benchmarks(
    rows: list[Benchmark],
    company: str | None,
    level_norm: str | None,
    location: str | None,
) -> SalaryResult | None:
    def norm(s: str | None) -> str:
        return (s or "").strip().lower()

    loc = norm(location)
    matched = [
        b for b in rows
        if norm(b.company) == norm(company)
        and (not b.level_norm or norm(b.level_norm) == norm(level_norm))
        and (not b.location_norm or norm(b.location_norm) in loc or norm(b.location_norm) == "india" and "india" in loc)
    ]
    if not matched:
        return None
    totals = [(b.total_annual / LAKH, b) for b in matched]
    vals = [v for v, _ in totals]
    lo, hi = round(min(vals), 1), round(max(vals), 1)
    parts = []
    for v, b in totals:
        loc_s = f", {b.location_norm}" if b.location_norm else ""
        yrs = f", {b.years_exp:g} yrs" if b.years_exp else ""
        parts.append(f"{b.source} {b.kind.replace('_', ' ')}{loc_s}{yrs}: ≈ ₹{v:.0f}L")
    explanation = (
        f"Total comp, INR, {location or 'region n/a'}. Band is min–max of {len(totals)} benchmark(s): " + "; ".join(parts) + "."
    )
    sources = sorted({f"{b.source}: {b.source_url}" if b.source_url else b.source for _, b in totals})
    return SalaryResult(lo, hi, "percentile_band", explanation, sources, [b.id for _, b in totals if b.id])


def resolve_salary(
    pay: PayRange | None,
    text: str,
    benchmarks: list[Benchmark],
    company: str | None,
    level_norm: str | None,
    location: str | None,
) -> SalaryResult | None:
    pay = pay or find_pay_in_text(text)
    if pay:
        r = from_pay_range(pay, location)
        if r:
            return r
    return from_benchmarks(benchmarks, company, level_norm, location)
