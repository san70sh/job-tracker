"""Capture small real responses from each ATS into tests/fixtures (run manually, needs internet).

    uv run python scripts/capture_fixtures.py

Lists are trimmed to a few postings so fixtures stay small. Re-run when an adapter breaks to see
what the live API looks like now.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx

FX = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
FX.mkdir(parents=True, exist_ok=True)
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Accept": "application/json"}


def save(name: str, data) -> None:
    (FX / name).write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"{name:32} {(FX / name).stat().st_size:>9,} bytes")


def main() -> None:
    with httpx.Client(headers=UA, timeout=40, follow_redirects=True) as c:
        # Greenhouse ---------------------------------------------------------------
        d = c.get("https://boards-api.greenhouse.io/v1/boards/stripe/jobs?content=true").json()
        d["jobs"] = d["jobs"][:3]
        save("greenhouse_list.json", d)
        jid = d["jobs"][0]["id"]
        save("greenhouse_detail.json", c.get(f"https://boards-api.greenhouse.io/v1/boards/stripe/jobs/{jid}").json())

        # Lever --------------------------------------------------------------------
        save("lever_list.json", c.get("https://api.lever.co/v0/postings/palantir?mode=json&limit=3").json())

        # Ashby --------------------------------------------------------------------
        d = c.get("https://api.ashbyhq.com/posting-api/job-board/ashby?includeCompensation=true").json()
        d["jobs"] = d["jobs"][:3]
        save("ashby_list.json", d)

        # SmartRecruiters ----------------------------------------------------------
        d = c.get("https://api.smartrecruiters.com/v1/companies/ServiceNow/postings?limit=3").json()
        save("smartrecruiters_list.json", d)
        pid = d["content"][0]["id"]
        save("smartrecruiters_detail.json", c.get(f"https://api.smartrecruiters.com/v1/companies/ServiceNow/postings/{pid}").json())

        # Workday ------------------------------------------------------------------
        base = "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite"
        d = c.post(f"{base}/jobs", json={"appliedFacets": {}, "limit": 3, "offset": 0, "searchText": "software engineer"}).json()
        d.pop("facets", None)
        save("workday_list.json", d)
        ep = d["jobPostings"][0]["externalPath"]
        save("workday_detail.json", c.get(f"{base}{ep}").json())

        # Oracle Cloud HCM (JPMC) --------------------------------------------------
        host = "https://jpmc.fa.oraclecloud.com/hcmRestApi/resources/latest"
        d = c.get(
            f"{host}/recruitingCEJobRequisitions",
            params={
                "onlyData": "true",
                "expand": "requisitionList.secondaryLocations",
                "finder": "findReqs;siteNumber=CX_1001,limit=3,sortBy=POSTING_DATES_DESC",
            },
        ).json()
        save("oracle_list.json", d)
        rid = d["items"][0]["requisitionList"][0]["Id"]
        r = c.get(
            f"{host}/recruitingCEJobRequisitionDetails",
            params={"expand": "all", "onlyData": "true", "finder": f'ById;Id="{rid}",siteNumber=CX_1001'},
        )
        print("oracle detail status", r.status_code)
        save("oracle_detail.json", r.json())

        # Eightfold (PayPal) -------------------------------------------------------
        host = "https://paypal.eightfold.ai/api/pcsx"
        d = c.get(f"{host}/search", params={"domain": "paypal.com", "query": "software", "start": 0, "num": 3}).json()
        save("eightfold_list.json", d)
        pos = d["data"]["positions"][0]["id"]
        r = c.get(f"{host}/position_details", params={"position_id": pos, "domain": "paypal.com", "hl": "en"})
        print("eightfold detail status", r.status_code)
        save("eightfold_detail.json", r.json())

        # iCIMS Jibe (DocuSign) ----------------------------------------------------
        save("jibe_list.json", c.get("https://careers.docusign.com/api/jobs?limit=3&page=1").json())


if __name__ == "__main__":
    main()
