"""Score the deterministic pipeline against the rows Claude already filled in Notion.

    uv run python scripts/golden.py                 # all rows with a Job Link (needs internet, no DB)
    uv run python scripts/golden.py --limit 20

Reads tests/fixtures/notion_golden.json (export of the Job Tracker rows). Writes reports/golden.json.
Many postings will have closed since Claude read them; those show up as fetch failures, not parser errors.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from jobtracker.extract.facts import parse_experience  # noqa: E402
from jobtracker.extract.job import extract, missing_fields  # noqa: E402
from jobtracker.extract.team import _content_words as team_words  # noqa: E402
from jobtracker.extract.text import text_coverage  # noqa: E402
from jobtracker.http import Fetcher, PostingGone  # noqa: E402
from jobtracker.pipeline import fetch_posting  # noqa: E402
from jobtracker.urls import normalize_company  # noqa: E402


def norm_loc(s: str | None) -> set[str]:
    toks = re.split(r"[^a-z]+", (s or "").lower())
    stop = {"india", "united", "states", "of", "america", "the", "and", "hybrid", "remote", "onsite", "in", "office", "karnataka", "telangana", "maharashtra", "haryana", "tamil", "nadu", "uttar", "pradesh", "ka", "tg", "mh", "hr", "ts", "in", "ind", "us", "usa", "ca", "ny", "wa", "tx"}
    return {t for t in toks if len(t) > 2 and t not in stop}


def _content_words(s: str) -> set[str]:
    return team_words(s)


def cmp_company(ours: str | None, theirs: str | None) -> bool:
    a, b = normalize_company(ours or ""), normalize_company(theirs or "")
    return bool(a and b and (a in b or b in a or set(a.split()) & set(b.split())))


async def run_one(row: dict, f: Fetcher, sem: asyncio.Semaphore) -> dict:
    url = row.get("Job Link")
    out: dict = {"company": row["Company"], "role": row["Role"], "url": url}
    if not url:
        out["error"] = "no link"
        return out
    async with sem:
        try:
            posting, how = await fetch_posting(url, f)
        except PostingGone:
            out["error"] = "gone"
            return out
        except Exception as e:  # noqa: BLE001
            out["error"] = f"{type(e).__name__}: {str(e)[:90]}"
            return out
    out["adapter"] = how
    out["had_department"] = bool(posting.department)
    out["coverage"] = text_coverage(posting.description_html) if posting.description_html else None
    posting.department = None  # measure the text/title inference on every row, not only where the ATS is silent
    ex = extract(posting)
    fv = ex.fields.get("team_domain")
    theirs_team = row.get("Team / Domain") or ""
    ours_team = fv.value if fv else None
    shared = _content_words(ours_team or "") & _content_words(theirs_team) if ours_team else set()
    out["team"] = {"ours": ours_team, "notion": theirs_team, "confidence": fv.confidence if fv else None,
                   "evidence": fv.evidence if fv else None, "agree": bool(shared) if ours_team else None}
    out["missing"] = missing_fields(ex)
    out["title_ok"] = bool(posting.title)
    theirs_tech = set(json.loads(row["Key Technologies"])) if row.get("Key Technologies") else set()
    ours_tech = set(ex.technologies)
    inter = ours_tech & theirs_tech
    out["tech"] = {"ours": sorted(ours_tech), "notion": sorted(theirs_tech),
                   "recall": len(inter) / len(theirs_tech) if theirs_tech else None,
                   "precision": len(inter) / len(ours_tech) if ours_tech else None}
    out["company_ok"] = cmp_company(ex.get("company"), row["Company"])
    jid = (row.get("Job ID") or "")
    ours_ref = str(ex.get("job_ref") or "")
    out["job_id_ok"] = bool(ours_ref) and (ours_ref.lower() in jid.lower() or any(t in jid for t in re.findall(r"[A-Za-z0-9-]{5,}", ours_ref)))
    a, b = norm_loc(ex.get("location")), norm_loc(row.get("Location"))
    out["location_ok"] = bool(a & b) if (a and b) else None
    wm_theirs, wm_ours = row.get("Work Mode"), ex.get("work_mode")
    out["work_mode"] = {"ours": wm_ours, "notion": wm_theirs, "ok": (wm_ours == wm_theirs) if (wm_ours and wm_theirs) else None}
    e_theirs = parse_experience(row.get("Experience Required") or "")
    e_ours_min = ex.get("experience_min_years")
    out["experience"] = {"ours": e_ours_min, "notion": e_theirs.min_years if e_theirs else None,
                         "ok": (e_ours_min == e_theirs.min_years) if (e_theirs and e_ours_min is not None) else None}
    out["has_resp"] = bool(ex.get("key_responsibilities")) and ex.fields["key_responsibilities"].confidence >= 0.8
    out["has_req"] = bool(ex.get("requirements")) and ex.fields["requirements"].confidence >= 0.8
    return out


def pct(n: int, d: int) -> str:
    return f"{100 * n / d:.0f}% ({n}/{d})" if d else "n/a"


async def main(limit: int | None) -> None:
    rows = json.loads((ROOT / "tests" / "fixtures" / "notion_golden.json").read_text(encoding="utf-8"))
    if limit:
        rows = rows[:limit]
    sem = asyncio.Semaphore(6)
    async with Fetcher() as f:
        results = await asyncio.gather(*(run_one(r, f, sem) for r in rows))
    (ROOT / "reports").mkdir(exist_ok=True)
    (ROOT / "reports" / "golden.json").write_text(json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8")

    ok = [r for r in results if "error" not in r]
    print(f"\nrows: {len(results)}   fetched+parsed: {len(ok)}   failed: {len(results) - len(ok)}")
    print("failures:", dict(Counter(r["error"].split(":")[0] for r in results if "error" in r)))
    print("\nadapter used (parsed rows):")
    for k, v in Counter(r["adapter"] for r in ok).most_common():
        print(f"  {k:32} {v}")
    print("\nfield agreement with what Claude wrote in Notion:")
    print("  title found       ", pct(sum(r["title_ok"] for r in ok), len(ok)))
    print("  company           ", pct(sum(r["company_ok"] for r in ok), len(ok)))
    print("  job id            ", pct(sum(r["job_id_ok"] for r in ok), len(ok)))
    loc = [r for r in ok if r["location_ok"] is not None]
    print("  location          ", pct(sum(r["location_ok"] for r in loc), len(loc)))
    wm = [r for r in ok if r["work_mode"]["ok"] is not None]
    print("  work mode (both set)", pct(sum(r["work_mode"]["ok"] for r in wm), len(wm)), "| we left it blank on", sum(1 for r in ok if not r["work_mode"]["ours"]), "of", len(ok))
    ex_ = [r for r in ok if r["experience"]["ok"] is not None]
    print("  experience (min yrs)", pct(sum(r["experience"]["ok"] for r in ex_), len(ex_)))
    t = [r["tech"] for r in ok if r["tech"]["recall"] is not None]
    if t:
        print(f"  tech recall (avg)  {100 * sum(x['recall'] for x in t) / len(t):.0f}%   precision (avg) "
              f"{100 * sum(x['precision'] for x in t if x['precision'] is not None) / max(1, sum(1 for x in t if x['precision'] is not None)):.0f}%")
    filled = [r for r in ok if r["team"]["ours"]]
    agree = [r for r in filled if r["team"]["agree"]]
    print("  team inferred      ", pct(len(filled), len(ok)), "| agrees with Notion (shared word):", pct(len(agree), len(filled)))
    for r in filled:
        if not r["team"]["agree"]:
            print(f"      DISAGREE  {r['role'][:48]!r}: ours={r['team']['ours']!r}  notion={r['team']['notion'][:70]!r}")
    cov = [r for r in ok if r["coverage"] is not None]
    if cov:
        print("  text coverage (words kept / words visible)", f"avg {100 * sum(r['coverage'] for r in cov) / len(cov):.0f}%")
        for r in sorted(cov, key=lambda r: r["coverage"])[:5]:
            if r["coverage"] < 0.85:
                print(f"      LOW COVERAGE {r['coverage']:.0%}  {r['adapter'][:14]:<14} {r['role'][:60]!r}")
    print("  responsibilities extracted w/ confidence>=0.8:", pct(sum(r["has_resp"] for r in ok), len(ok)))
    print("  requirements extracted w/ confidence>=0.8:    ", pct(sum(r["has_req"] for r in ok), len(ok)))
    need_llm = sum(1 for r in ok if r["missing"])
    print(f"\nwould still need the LLM fallback for some field: {pct(need_llm, len(ok))}")
    miss = Counter(m for r in ok for m in r["missing"])
    print("  most common missing fields:", dict(miss.most_common(6)))

    by_adapter: dict[str, list] = defaultdict(list)
    for r in ok:
        by_adapter[r["adapter"].split("->")[0]].append(r)
    print("\nper adapter: tech recall / has_resp / has_req / needs-LLM")
    for k, rs in sorted(by_adapter.items(), key=lambda kv: -len(kv[1])):
        rec = [r["tech"]["recall"] for r in rs if r["tech"]["recall"] is not None]
        print(f"  {k:16} n={len(rs):3}  recall {100 * sum(rec) / len(rec) if rec else 0:3.0f}%  "
              f"resp {pct(sum(r['has_resp'] for r in rs), len(rs)):>10}  req {pct(sum(r['has_req'] for r in rs), len(rs)):>10}  "
              f"llm {pct(sum(1 for r in rs if r['missing']), len(rs)):>10}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    asyncio.run(main(ap.parse_args().limit))
