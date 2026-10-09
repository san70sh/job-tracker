import httpx
import pytest
import respx

from jobtracker.adapters import registry
from jobtracker.adapters.ashby import Ashby
from jobtracker.adapters.eightfold import Eightfold
from jobtracker.adapters.greenhouse import Greenhouse
from jobtracker.adapters.jibe import Jibe, split_qualifications
from jobtracker.adapters.lever import Lever
from jobtracker.adapters.oracle import OracleHCM
from jobtracker.adapters.smartrecruiters import SmartRecruiters
from jobtracker.adapters.workday import Workday
from jobtracker.extract.job import extract
from jobtracker.http import Fetcher
from jobtracker.models import BoardRef


# ───────────────────────────── URL recognition (no network) ─────────────────────────────
@pytest.mark.parametrize("url,ats,slug,pid", [
    ("https://boards.greenhouse.io/stripe/jobs/8172510", "greenhouse", "stripe", "8172510"),
    ("https://job-boards.greenhouse.io/stripe/jobs/8172510?gh_src=x", "greenhouse", "stripe", "8172510"),
    ("https://stripe.com/jobs/search?gh_jid=8172510", "greenhouse", "stripe", "8172510"),  # via hosts.json
    ("https://jobs.lever.co/palantir/6ed76ce8-4156-4b60-b120-403538bd66cd", "lever", "palantir", "6ed76ce8-4156-4b60-b120-403538bd66cd"),
    ("https://jobs.ashbyhq.com/ashby/7458d4e9-da2e-47bd-98cb-adfda43d42b2/application", "ashby", "ashby", "7458d4e9-da2e-47bd-98cb-adfda43d42b2"),
    ("https://jobs.smartrecruiters.com/ServiceNow/744000153446319-sr-software-engineer", "smartrecruiters", "ServiceNow", "744000153446319"),
    ("https://careers.servicenow.com/jobs/744000153446319/sr-software-engineer/", "smartrecruiters", "ServiceNow", "744000153446319"),
    ("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/Israel-Yokneam/Software-Engineer--SPE_JR2015623", "workday", "nvidia", "JR2015623"),
    ("https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/210711544", "oracle", "jpmc.fa.oraclecloud.com", "210711544"),
    ("https://paypal.eightfold.ai/careers/job/274919097057", "eightfold", "paypal.eightfold.ai", "274919097057"),
    ("https://apply.careers.microsoft.com/careers/job/1970393556953612?domain=microsoft.com", "eightfold", "apply.careers.microsoft.com", "1970393556953612"),
    ("https://careers.docusign.com/jobs/30419?lang=en-us", "jibe", "careers.docusign.com", "30419"),
    ("https://careers-apac-atlassian.icims.com/jobs/27371/senior-software-engineer/job", "icims", "careers-apac-atlassian.icims.com", "27371"),
])
def test_resolve_static(url, ats, slug, pid):
    r = registry.resolve_static(url)
    assert r is not None, url
    assert r.target.board.ats == ats
    assert r.target.board.slug == slug
    assert r.target.posting_id == pid


def test_resolve_static_company_overrides():
    r = registry.resolve_static("https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/1")
    assert r and r.target.board.company == "JPMorgan Chase"
    r = registry.resolve_static("https://apply.careers.microsoft.com/careers/job/1970393556953612")
    assert r and r.target.board.config["domain"] == "microsoft.com" and r.target.board.company == "Microsoft"


def test_rbs_workday_jobs_are_named_natwest_group():
    r = registry.resolve_static("https://rbs.wd3.myworkdayjobs.com/RBS/job/Bengaluru/WoW-Software-Engineer--AVP_R-00286132")
    assert r and r.target.board.ats == "workday" and r.target.board.slug == "rbs"
    assert r.target.board.company == "NatWest Group" and r.target.posting_id == "R-00286132"


def test_unknown_url_has_no_static_resolution():
    assert registry.resolve_static("https://example.com/careers/engineer") is None


# ───────────────────────────── posting mapping from real responses ─────────────────────────────
def test_greenhouse_posting(fx):
    p = Greenhouse.to_posting(fx("greenhouse_detail.json"), "stripe")
    assert p.ats == "greenhouse" and p.company == "Stripe" and p.ats_posting_id
    assert p.job_ref is None  # "See Opening ID" is rejected as a requisition id
    assert p.description_html and "&lt;" in p.description_html  # escaped upstream; text layer unescapes
    ex = extract(p)
    assert ex.get("responsibilities") is None  # field name sanity
    assert ex.get("key_responsibilities") and ex.get("requirements")


def test_lever_posting(fx):
    p = Lever.to_posting(fx("lever_list.json")[0], BoardRef("lever", "palantir"))
    assert p.company == "Palantir" and p.work_mode == "Hybrid"
    kinds = {s.kind for s in extract(p).sections}
    assert {"responsibilities", "requirements", "nice_to_have"} <= kinds  # via heading rules + positional fallback


def test_ashby_posting_pay(fx):
    j = next(j for j in fx("ashby_list.json")["jobs"] if j.get("compensation", {}).get("compensationTiers"))
    p = Ashby.to_posting(j, BoardRef("ashby", "ashby"))
    assert p.pay and p.pay.currency == "EUR" and (p.pay.min, p.pay.max) == (110000, 185000)
    assert p.work_mode == "Remote"


def test_smartrecruiters_posting(fx):
    p = SmartRecruiters.to_posting(fx("smartrecruiters_detail.json"), BoardRef("smartrecruiters", "ServiceNow"))
    ex = extract(p)
    assert p.job_ref == "JB0072427" and ex.get("company") == "ServiceNow"
    assert ex.get("experience_min_years") == 10
    assert any(s.kind == "requirements" for s in ex.sections)


def test_workday_posting_mojibake_and_sections(fx):
    b = BoardRef("workday", "nvidia", config={"host": "nvidia.wd5.myworkdayjobs.com", "site": "NVIDIAExternalCareerSite"})
    ex = extract(Workday.to_posting(fx("workday_detail.json"), b, "https://x"))
    assert "â€" not in ex.get("experience_required", "")
    kinds = [s.kind for s in ex.sections]
    assert "responsibilities" in kinds and "requirements" in kinds and "nice_to_have" in kinds
    assert ex.get("job_ref") == "JR2015623"


def test_oracle_posting_pay_from_flex_field(fx):
    b = BoardRef("oracle", "jpmc.fa.oraclecloud.com", company="JPMorgan Chase",
                 config={"host": "jpmc.fa.oraclecloud.com", "site": "CX_1001", "siteNumber": "CX_1001", "lang": "en"})
    p = OracleHCM().to_posting(fx("oracle_detail.json")["items"][0], b)
    assert p.pay and p.pay.currency == "USD" and p.pay.min == 205000
    assert p.url.endswith("/sites/CX_1001/job/210711544")


def test_eightfold_posting(fx):
    b = BoardRef("eightfold", "paypal.eightfold.ai", company="PayPal", config={"host": "paypal.eightfold.ai", "domain": "paypal.com"})
    p = Eightfold.to_posting(fx("eightfold_detail.json")["data"], b)
    ex = extract(p)
    assert p.job_ref == "R0136054" and p.work_mode == "Onsite" and p.posted_at is not None
    assert {"Kubernetes", "Python"} <= set(ex.technologies)


def test_jibe_posting_splits_qualifications(fx):
    data = fx("jibe_list.json")["jobs"][0]["data"]
    p = Jibe.to_posting(data, Jibe.board_for_host("careers.docusign.com", "DocuSign"))
    ex = extract(p)
    kinds = {s.kind: s for s in ex.sections}
    assert "nice_to_have" in kinds and "requirements" in kinds
    assert not any(b.endswith("Sr.") for b in kinds["responsibilities"].bullets)  # 'Sr.' must not split sentences
    req, nice = split_qualifications("Basic BS/BA degree 5+ years sales Preferred 7+ years quota-carrying")
    assert req.startswith("BS/BA") and nice.startswith("7+ years")


# ───────────────────────────── listing + fetching over (mocked) HTTP ─────────────────────────────
@pytest.fixture
async def fetcher():
    f = Fetcher(delay=0)
    yield f
    await f.aclose()


@respx.mock
async def test_greenhouse_list(fetcher, fx):
    respx.get("https://boards-api.greenhouse.io/v1/boards/stripe/jobs").mock(return_value=httpx.Response(200, json=fx("greenhouse_list.json")))
    jobs = await Greenhouse().list_postings(BoardRef("greenhouse", "stripe"), fetcher)
    assert len(jobs) == 3 and jobs[0].ats_posting_id and jobs[0].title


@respx.mock
async def test_smartrecruiters_list_paginates(fetcher, fx):
    page = fx("smartrecruiters_list.json")
    page["totalFound"] = 3
    respx.get("https://api.smartrecruiters.com/v1/companies/ServiceNow/postings").mock(return_value=httpx.Response(200, json=page))
    jobs = await SmartRecruiters().list_postings(BoardRef("smartrecruiters", "ServiceNow"), fetcher)
    assert len(jobs) == len(page["content"]) and jobs[0].url.startswith("https://jobs.smartrecruiters.com/ServiceNow/")


def test_workday_reads_its_posted_text_as_a_date():
    from datetime import date

    from jobtracker.adapters.workday import posted_on

    today = date(2026, 10, 6)
    assert posted_on("Posted Today", today) == today
    assert posted_on("Posted Yesterday", today) == date(2026, 10, 5)
    assert posted_on("Posted 2 Days Ago", today) == date(2026, 10, 4)
    assert posted_on("Posted 30+ Days Ago", today) == date(2026, 9, 6)
    assert posted_on(None, today) is None and posted_on("Open until filled", today) is None


@respx.mock
async def test_workday_skips_a_list_entry_with_no_title_and_still_counts_the_list_as_complete(fetcher):
    b = BoardRef("workday", "acme", config={"host": "acme.wd1.myworkdayjobs.com", "site": "Ext"})
    page = {"total": 2, "jobPostings": [
        {"bulletFields": ["JR-0000132739"]},  # a bare requisition number, as Workday sometimes lists
        {"title": "Backend Engineer", "externalPath": "/job/Pune/Backend-Engineer_JR1", "bulletFields": ["JR1"], "locationsText": "Pune"}]}
    respx.post("https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/Ext/jobs").mock(return_value=httpx.Response(200, json=page))
    jobs = await Workday().list_postings(b, fetcher)
    assert [j.ats_posting_id for j in jobs] == ["JR1"] and jobs.complete  # complete: postings missing later are still seen as closed


@respx.mock
async def test_workday_list_and_detail(fetcher, fx):
    b = BoardRef("workday", "nvidia", config={"host": "nvidia.wd5.myworkdayjobs.com", "site": "NVIDIAExternalCareerSite"})
    lst = fx("workday_list.json")
    lst["total"] = 3
    respx.post("https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/jobs").mock(return_value=httpx.Response(200, json=lst))
    jobs = await Workday().list_postings(b, fetcher)
    assert jobs and jobs[0].ats_posting_id.startswith("JR") and all(j.posted_at for j in jobs)  # fixture rows all say "N Days Ago"

    url = "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/Israel-Yokneam/Software-Engineer--SPE_JR2015623"
    respx.get("https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/job/Israel-Yokneam/Software-Engineer--SPE_JR2015623").mock(
        return_value=httpx.Response(200, json=fx("workday_detail.json")))
    r = registry.resolve_static(url)
    post = await r.adapter.fetch(r.target, fetcher)
    assert post.job_ref == "JR2015623" and post.company == "NVIDIA"


@respx.mock
async def test_oracle_list_and_detail(fetcher, fx):
    b = BoardRef("oracle", "jpmc.fa.oraclecloud.com", company="JPMorgan Chase",
                 config={"host": "jpmc.fa.oraclecloud.com", "site": "CX_1001", "siteNumber": "CX_1001", "lang": "en"})
    route = respx.get("https://jpmc.fa.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions").mock(
        return_value=httpx.Response(200, json=fx("oracle_list.json")))
    jobs = await OracleHCM().list_postings(b, fetcher)
    from urllib.parse import unquote
    assert jobs and "findReqs;siteNumber=CX_1001" in unquote(str(route.calls[0].request.url))
    respx.get("https://jpmc.fa.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails").mock(
        return_value=httpx.Response(200, json=fx("oracle_detail.json")))
    r = registry.resolve_static("https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/210711544")
    post = await r.adapter.fetch(r.target, fetcher)
    assert post.title.startswith("J.P. Morgan")


AMEX_SEARCH = ("https://careers.americanexpress.com/en/sites/CX_1/jobs?intlink=us-amex-career-en-us-search&lastSelectedFacet=AttributeChar6"
               "&selectedFlexFieldsFacets=%22AttributeChar6%7CTechnology%22&selectedLocationsFacet=300000000228786")


@respx.mock
async def test_oracle_site_on_a_company_domain_keeps_its_filters_and_links(fetcher, fx):
    ad, b = registry.board_from_url(AMEX_SEARCH)
    assert ad.ats == "oracle" and b.company == "American Express" and b.slug == "egug.fa.us2.oraclecloud.com"  # the API lives on Oracle's host
    assert b.config["public_host"] == "careers.americanexpress.com" and b.config["site"] == "CX_1"
    assert b.config["facets"] == {"selectedFlexFieldsFacets": '"AttributeChar6|Technology"', "selectedLocationsFacet": "300000000228786"}
    route = respx.get("https://egug.fa.us2.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions").mock(
        return_value=httpx.Response(200, json=fx("oracle_list.json")))
    jobs = await ad.list_postings(b, fetcher)
    from urllib.parse import unquote
    sent = unquote(str(route.calls[0].request.url))
    assert "selectedLocationsFacet=300000000228786" in sent and 'selectedFlexFieldsFacets="AttributeChar6|Technology"' in sent
    assert jobs and all(j.url.startswith("https://careers.americanexpress.com/en/sites/CX_1/job/") for j in jobs)
    # a job pasted from the company site resolves to the same job (same link), read through Oracle's host
    r = registry.resolve_static(jobs[0].url)
    assert r.adapter.ats == "oracle" and r.target.posting_id == jobs[0].ats_posting_id and r.target.board.config["host"] == "egug.fa.us2.oraclecloud.com"


def test_a_company_domain_not_in_hosts_json_is_not_taken_for_oracle():
    assert registry.board_from_url("https://careers.example.com/en/sites/CX_1/jobs") is None


@respx.mock
async def test_eightfold_list_and_detail(fetcher, fx):
    b = BoardRef("eightfold", "paypal.eightfold.ai", company="PayPal", config={"host": "paypal.eightfold.ai", "domain": "paypal.com"})
    respx.get("https://paypal.eightfold.ai/api/pcsx/search").mock(return_value=httpx.Response(200, json=fx("eightfold_list.json")))
    jobs = await Eightfold().list_postings(b, fetcher)
    assert jobs and jobs[0].url.startswith("https://paypal.eightfold.ai/careers/job/")


@respx.mock
async def test_jibe_fetch_by_keyword(fetcher, fx):
    page = fx("jibe_list.json")
    want = page["jobs"][0]["data"]["slug"]
    respx.get("https://careers.docusign.com/api/jobs").mock(return_value=httpx.Response(200, json=page))
    r = registry.resolve_static(f"https://careers.docusign.com/jobs/{want}")
    post = await r.adapter.fetch(r.target, fetcher)
    assert post.job_ref == want and post.company == "DocuSign"


@respx.mock
async def test_posting_gone_on_404(fetcher):
    from jobtracker.http import PostingGone
    respx.get("https://api.lever.co/v0/postings/x/y").mock(return_value=httpx.Response(404))
    r = registry.resolve_static("https://jobs.lever.co/x/y")
    with pytest.raises(PostingGone):
        await r.adapter.fetch(r.target, fetcher)


def test_smartrecruiters_empty_location_is_none_not_a_comma():
    from jobtracker.adapters.smartrecruiters import SmartRecruiters
    from jobtracker.models import BoardRef

    d = {"id": "1", "name": "Engineer", "location": {"fullLocation": ", "}, "company": {"name": "Acme"}}
    p = SmartRecruiters.to_posting(d, BoardRef("smartrecruiters", "acme"))
    assert p.location is None
    d["location"] = {"fullLocation": ", ", "city": "Pune", "country": "in"}
    assert SmartRecruiters.to_posting(d, BoardRef("smartrecruiters", "acme")).location == "Pune, IN"


JOB = "https://barclays.wd3.myworkdayjobs.com/External_Career_Site_Barclays/job/Chennai-DLF-IT-Park/WCR-analyst_JR-0000121275"


@pytest.mark.parametrize("suffix", ["", "/apply", "/apply/applyManually", "/apply/autofillWithResume", "/Apply", "?source=x"])
def test_workday_apply_links_resolve_to_the_job_page(suffix):
    """LinkedIn's 'Go to company site' gives .../apply; Workday's API only knows the job page."""
    r = registry.resolve_static(JOB + suffix)
    assert r.adapter.ats == "workday" and r.target.posting_id == "JR-0000121275"
    assert r.target.url == JOB  # cut back to the job page, no apply segment or query


def test_a_job_called_apply_is_not_cut():
    # only a segment AFTER the title counts as the apply page
    r = registry.resolve_static("https://x.wd1.myworkdayjobs.com/Site/job/Pune/apply_R-5")
    assert r.target.posting_id == "R-5" and r.target.url.endswith("/job/Pune/apply_R-5")


@respx.mock
async def test_workday_fetch_of_an_apply_link_asks_for_the_job_page(fetcher, fx):
    route = respx.get("https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/job/Israel-Yokneam/Software-Engineer--SPE_JR2015623").mock(
        return_value=httpx.Response(200, json=fx("workday_detail.json")))
    r = registry.resolve_static("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/Israel-Yokneam/Software-Engineer--SPE_JR2015623/apply/applyManually")
    post = await r.adapter.fetch(r.target, fetcher)
    assert route.called and post.job_ref == "JR2015623"


# ───────────────────────────── Workday: filtering by place at the source ─────────────────────────────
import json as _json  # noqa: E402

from jobtracker import filters, watcher  # noqa: E402
from jobtracker.adapters import workday as wd  # noqa: E402

# the shape of a real tenant's filter tree (Barclays nests its offices under locationMainGroup > locations)
FACETS = [
    {"facetParameter": "timeType", "values": [{"id": "t1", "descriptor": "Full time", "count": 3}]},
    {"facetParameter": "locationMainGroup", "values": [{"facetParameter": "locations", "descriptor": "Locations", "values": [
        {"id": "o1", "descriptor": "Pune, Gera Commerzone SEZ", "count": 208},
        {"id": "o2", "descriptor": "Bengaluru, Maruthi Onyx", "count": 10},
        {"id": "o3", "descriptor": "New York, 745 7th Ave", "count": 9}]}]},
]
BARCLAYS = "https://barclays.wd3.myworkdayjobs.com/External_Career_Site_Barclays"
API = "https://barclays.wd3.myworkdayjobs.com/wday/cxs/barclays/External_Career_Site_Barclays/jobs"


def test_the_place_filter_is_found_wherever_the_tenant_nests_it():
    assert wd._place_group(FACETS)[0] == "locations" and len(wd._place_group(FACETS)[1]) == 3
    assert wd._place_group(FACETS[:1]) == (None, [])
    flat = [{"facetParameter": "Location_Country", "values": [{"id": "c1", "descriptor": "India", "count": 1}]}]
    assert wd._place_group(flat)[0] == "Location_Country"


def test_filters_in_a_pasted_workday_address_are_read():
    b = registry.board_from_url(BARCLAYS + "?locations=o1&locations=o2&timeType=t1&q=java&utm_source=x")[1]
    assert b.config["facets"] == {"locations": ["o1", "o2"], "timeType": ["t1"]} and b.config["search_text"] == "java"
    assert "facets" not in registry.board_from_url(BARCLAYS)[1].config


def _serve_workday(listing_location="2 Locations"):
    seen = []

    def handler(request):
        body = _json.loads(request.content)
        seen.append(body)
        if body["limit"] == 1:
            return httpx.Response(200, json={"total": 227, "jobPostings": [], "facets": FACETS})
        post = {"title": "Engineer", "externalPath": "/job/x/Engineer_JR-1", "locationsText": listing_location, "bulletFields": ["JR-1"]}
        return httpx.Response(200, json={"total": 1, "jobPostings": [post], "facets": []})

    respx.post(API).mock(side_effect=handler)
    return seen


@respx.mock
async def test_only_the_offices_matching_the_terms_are_asked_for(fetcher):
    seen = _serve_workday()
    ad, board = registry.board_from_url(BARCLAYS)
    narrowed, at_source = await ad.resolve_location_filter(board, ["Pune", "Bangalore"], fetcher)   # Bangalore = Bengaluru
    assert at_source and narrowed.config["facets"] == {"locations": ["o1", "o2"]}
    assert board.config.get("facets") is None                                                          # the saved board is not changed
    assert seen[0]["limit"] == 1


@respx.mock
async def test_nothing_matching_or_no_terms_leaves_the_listing_alone(fetcher):
    seen = _serve_workday()
    ad, board = registry.board_from_url(BARCLAYS)
    assert await ad.resolve_location_filter(board, ["Atlantis"], fetcher) == (board, False)
    assert await ad.resolve_location_filter(board, [], fetcher) == (board, False)
    assert len(seen) == 1                                                                              # no terms: no request at all


@respx.mock
async def test_a_multi_office_posting_survives_when_the_source_already_matched_it(fetcher):
    """Workday lists a role in two offices as '2 Locations'; filtering that text afterwards would wrongly drop it."""
    _serve_workday("2 Locations")
    row = {"ats": "workday", "slug": "barclays", "company": "Barclays", "config": {"host": "barclays.wd3.myworkdayjobs.com", "site": "External_Career_Site_Barclays"},
           "title_include": [], "title_exclude": [], "location_include": ["Pune"]}
    listed, effective = await watcher.fetch_listing(row, fetcher)
    assert effective["location_include"] == [] and len(listed) == 1
    assert filters.passes(listed[0].title, listed[0].location, effective["title_include"], effective["title_exclude"], effective["location_include"])
    assert not filters.passes(listed[0].title, listed[0].location, [], [], ["Pune"])                  # the text alone could not have matched


@respx.mock
async def test_a_system_that_cannot_filter_by_place_keeps_the_text_filter(fetcher):
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(return_value=httpx.Response(200, json={"jobs": [
        {"id": 1, "title": "Dev", "absolute_url": "https://x/1", "location": {"name": "Pune"}}]}))
    row = {"ats": "greenhouse", "slug": "acme", "company": "Acme", "config": {}, "title_include": [], "title_exclude": [], "location_include": ["Pune"]}
    listed, effective = await watcher.fetch_listing(row, fetcher)
    assert effective is row and effective["location_include"] == ["Pune"]


# ───────────────────────────── a careers page that only displays the job ─────────────────────────────
from jobtracker import pipeline as _pipeline  # noqa: E402

CAREERS = "https://search.jobs.barclays/job/chennai/wcr-analyst/13015/101587812368"
WD_JOB = "https://barclays.wd3.myworkdayjobs.com/External_Career_Site_Barclays/job/Chennai-DLF-IT-Park/WCR-analyst_JR-0000121275"
PAGE = f"""<html><body><h1>WCR analyst</h1><a href="/job/chennai/other/13015/9">Apply for similar jobs near you</a>
  <a href="mailto:hr@x.com">Apply by email</a><a class="cta" href="{WD_JOB}/apply">Apply now</a></body></html>"""


def test_the_apply_button_leads_to_the_system_behind_the_page():
    r = registry.delegate_from_html(PAGE, CAREERS)
    assert r.adapter.ats == "workday" and r.target.posting_id == "JR-0000121275" and r.target.url == WD_JOB


@pytest.mark.parametrize("html", [
    "<html><body><a href='/similar/1'>Similar jobs</a></body></html>",                                   # no apply link
    f"<a href='{WD_JOB}'>See the posting on our other site</a>",                                          # a link, but not an Apply link
    f"<a href='{WD_JOB}/apply'>Apply</a><a href='{WD_JOB.replace('WCR', 'OTHER')}/apply'>Apply</a>",     # two different jobs: ambiguous
    "<a href='https://example.com/careers/apply'>Apply</a>",                                              # a system the app does not read
    f"<a href='{CAREERS}'>Apply</a>",                                                                     # the page itself
])
def test_no_hand_off_when_the_page_does_not_point_clearly_to_one_job(html):
    assert registry.delegate_from_html(html, CAREERS) is None


@respx.mock
async def test_a_careers_page_is_read_from_the_workday_job_it_points_to(fetcher, fx):
    respx.get(CAREERS).mock(return_value=httpx.Response(200, text=PAGE))
    api = respx.get("https://barclays.wd3.myworkdayjobs.com/wday/cxs/barclays/External_Career_Site_Barclays/job/Chennai-DLF-IT-Park/WCR-analyst_JR-0000121275").mock(
        return_value=httpx.Response(200, json=fx("workday_detail.json")))
    post, how = await _pipeline.fetch_posting(CAREERS, fetcher)
    assert api.called and how == "workday" and post.ats == "workday" and post.via == CAREERS


@respx.mock
async def test_if_the_system_behind_the_page_fails_the_page_itself_is_read(fetcher):
    respx.get(CAREERS).mock(return_value=httpx.Response(200, text=PAGE))
    respx.get("https://barclays.wd3.myworkdayjobs.com/wday/cxs/barclays/External_Career_Site_Barclays/job/Chennai-DLF-IT-Park/WCR-analyst_JR-0000121275").mock(
        return_value=httpx.Response(404))
    post, how = await _pipeline.fetch_posting(CAREERS, fetcher)
    assert how == "html" and post.title == "WCR analyst" and post.via is None
