"""Portal discovery: working out the job system behind a careers page that its address does not identify."""
import httpx
import pytest
import respx

from jobtracker import pipeline, portals
from jobtracker.adapters import discovery, registry
from jobtracker.http import Fetcher

ORACLE = "egug.fa.us2.oraclecloud.com"
API = f"https://{ORACLE}/hcmRestApi/resources/latest"
PAGE = "https://careers.newco.example/en/sites/CX_9/jobs"
# what an Oracle candidate-site page gives away about itself
ORACLE_PAGE = f'<html><head><link rel="icon" href="https://{ORACLE}/favicon?siteNumber=CX_9"></head><body><div id="app"></div></body></html>'
LINKS = "".join(f'<a href="/x{i}">x</a>' for i in range(6))  # a page with ordinary links, so it is not taken for a JavaScript-only page


@pytest.fixture
async def fetcher():
    async with Fetcher(delay=0) as f:
        yield f


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    """No hosts.json entries, nothing learned, and nothing written to the database."""
    monkeypatch.setattr(registry, "_file_hosts", lambda: {})
    registry.set_learned({})
    monkeypatch.setattr(portals, "_loaded", True)
    saved = []
    monkeypatch.setattr(portals, "_save", lambda host, entry, evidence: saved.append((host, entry, evidence)))
    yield saved
    registry.set_learned({})


def page(url, html, status=200):
    respx.get(url).mock(return_value=httpx.Response(status, text=html, headers={"content-type": "text/html"}))


@respx.mock
async def test_a_company_domain_in_front_of_oracle_is_recognised_from_its_page(fetcher):
    page(PAGE, ORACLE_PAGE)
    found = await discovery.discover(PAGE + "?selectedLocationsFacet=123", fetcher)
    b = found.board
    assert found.adapter.ats == "oracle" and found.from_page and found.host == "careers.newco.example"
    assert (b.slug, b.config["site"], b.config["public_host"], b.config["facets"]) == (ORACLE, "CX_9", "careers.newco.example",
                                                                                      {"selectedLocationsFacet": "123"})
    assert found.adapter.host_entry(b) == {"ats": "oracle", "company": None, "config": {"host": ORACLE}}  # no site, no filters


@respx.mock
async def test_find_board_verifies_the_system_then_remembers_the_domain(fetcher, isolated):
    page(PAGE, ORACLE_PAGE)
    respx.get(f"{API}/recruitingCEJobRequisitions").mock(return_value=httpx.Response(200, json={"items": [{"requisitionList": [], "TotalJobsCount": 0}]}))
    found = await portals.find_board(PAGE, fetcher)
    assert found.adapter.ats == "oracle" and isolated and isolated[0][0] == "careers.newco.example"  # an empty job list still counts
    assert registry.load_hosts()["careers.newco.example"]["config"] == {"host": ORACLE}
    # the second time the domain is known: no page is opened (respx would fail on an unmocked request)
    assert (await portals.find_board("https://careers.newco.example/en/sites/CX_9/jobs", fetcher)).how == portals.BY_ADDRESS


@respx.mock
async def test_a_found_system_that_cannot_list_jobs_is_an_error_and_is_not_remembered(fetcher, isolated):
    page(PAGE, ORACLE_PAGE)
    respx.get(f"{API}/recruitingCEJobRequisitions").mock(return_value=httpx.Response(500, text="boom"))
    with pytest.raises(portals.NotFound, match="oracle board .* job list could not be read"):
        await portals.find_board(PAGE, fetcher)
    assert not isolated and "careers.newco.example" not in registry.load_hosts()


@respx.mock
async def test_a_greenhouse_embed_on_a_job_page_one_click_from_the_listing(fetcher):
    page("https://careers.newco.example/positions/", f'<a href="/positions/42/">Engineer</a>{LINKS}')
    page("https://careers.newco.example/positions/42/",
         '<script src="//boards.greenhouse.io/embed/job_board/js?for=newco"></script>')
    found = await discovery.discover("https://careers.newco.example/positions/", fetcher)
    assert (found.adapter.ats, found.board.slug, found.from_page) == ("greenhouse", "newco", True)
    assert "job page" in found.how


@respx.mock
async def test_a_link_to_one_systems_board_is_enough_for_adding_a_board(fetcher):
    page("https://jobs.newco.example/search", '<a href="https://newco.wd3.myworkdayjobs.com/en-US/External_Site">Jobs</a>' + LINKS)
    found = await discovery.discover("https://jobs.newco.example/search", fetcher)
    assert (found.adapter.ats, found.board.slug, found.board.config["site"]) == ("workday", "newco", "External_Site")
    assert not found.from_page  # a link says nothing about what the company's own pages run on, so it is not remembered


@respx.mock
async def test_the_apply_link_of_a_job_page_is_followed_when_the_listing_says_nothing(fetcher):
    page("https://jobs.newco.example/search", f'<a href="/job/pune/engineer/777">Engineer</a>{LINKS}')
    page("https://jobs.newco.example/job/pune/engineer/777",
         '<a class="apply" href="https://newco.wd3.myworkdayjobs.com/External_Site/job/Pune/Engineer_R1234">Apply now</a>')
    found = await discovery.discover("https://jobs.newco.example/search", fetcher)
    assert (found.adapter.ats, found.board.slug) == ("workday", "newco") and "Apply link" in found.how


@respx.mock
async def test_two_different_systems_on_one_page_are_a_conflict_not_a_guess(fetcher):
    page("https://jobs.newco.example/", '<a href="https://jobs.lever.co/newco">a</a><a href="https://boards.greenhouse.io/newco">b</a>' + LINKS)
    with pytest.raises(portals.NotFound, match="More than one job system.*lever 'newco'.*greenhouse 'newco'"):
        await discovery.discover("https://jobs.newco.example/", fetcher)


@respx.mock
async def test_a_page_that_builds_itself_with_javascript_says_so(fetcher):
    page("https://jobs.newco.example/", '<div id="root"></div><script src="/app.js"></script>')
    with pytest.raises(portals.NotFound, match="JavaScript.*config/hosts.json"):
        await discovery.discover("https://jobs.newco.example/", fetcher)


@respx.mock
async def test_an_unreachable_page_is_an_error_naming_the_problem(fetcher):
    page("https://jobs.newco.example/", "denied", status=403)
    with pytest.raises(portals.NotFound, match=r"refused the request \(HTTP 403.*bot protection.*Apply") as e:
        await discovery.discover("https://jobs.newco.example/", fetcher)
    assert "denied" not in str(e.value)  # the site's own reply is not echoed


@respx.mock
async def test_other_failures_to_open_a_page_are_named_briefly(fetcher):
    page("https://jobs.newco.example/", "x" * 500, status=500)
    with pytest.raises(portals.NotFound, match="could not be opened") as e:
        await discovery.discover("https://jobs.newco.example/", fetcher)
    assert len(str(e.value)) < 260


@respx.mock
async def test_a_redirect_to_a_known_board_address_is_followed(fetcher):
    respx.get("https://jobs.newco.example/").mock(return_value=httpx.Response(302, headers={"location": "https://job-boards.greenhouse.io/newco"}))
    page("https://job-boards.greenhouse.io/newco", "<html></html>")
    found = await discovery.discover("https://jobs.newco.example/", fetcher)
    assert (found.adapter.ats, found.board.slug) == ("greenhouse", "newco") and "redirects" in found.how


@respx.mock
async def test_a_pasted_job_on_an_unknown_oracle_domain_is_read_through_oracle_and_remembered(fetcher, isolated, fx):
    job_url = "https://careers.newco.example/en/sites/CX_9/job/210711544"
    page(job_url, ORACLE_PAGE)
    respx.get(f"{API}/recruitingCEJobRequisitionDetails").mock(return_value=httpx.Response(200, json=fx("oracle_detail.json")))
    posting, how = await pipeline.fetch_posting(job_url, fetcher)
    assert how == "oracle" and posting.title.startswith("J.P. Morgan")
    assert isolated and isolated[0][0] == "careers.newco.example" and isolated[0][1]["config"] == {"host": ORACLE}


@respx.mock
async def test_a_failed_read_is_not_remembered_and_the_page_is_parsed_as_before(fetcher, isolated):
    job_url = "https://careers.newco.example/en/sites/CX_9/job/1"
    page(job_url, ORACLE_PAGE + "<h1>Backend Engineer</h1>")
    respx.get(f"{API}/recruitingCEJobRequisitionDetails").mock(return_value=httpx.Response(200, json={"items": []}))
    posting, how = await pipeline.fetch_posting(job_url, fetcher)
    assert how == "html" and not isolated  # Oracle had no such job: nothing learned, the page itself was read


# ───────────────────────────── Eightfold tenants that only allow the older listing interface ─────────────────────────────
HSBC = "https://portal.careers.hsbc.com"
HSBC_PAGE = ('<html><link href="https://static.vscdn.net/fonts/css/eightfold-font-base.css"><script>var s="https://hsbc.eightfold.ai/x";'
             'Sentry.init({dsn:"https://k@vs-errors.eightfold.ai/10"}); var u="/api/pcsx/search?domain=hsbc.com";</script></html>' + LINKS)


def older(i):
    return {"id": 5000 + i, "name": f"Java Developer {i}", "location": "Pune, India", "locations": ["Pune, India"], "department": None,
            "business_unit": "Technology", "t_create": 1790860093, "canonicalPositionUrl": f"{HSBC}/careers/job/{5000 + i}"}


def test_eightfolds_own_service_hosts_are_not_employers():
    assert registry.board_from_url("https://vs-errors.eightfold.ai/10") is None
    assert registry.board_from_url("https://paypal.eightfold.ai/careers")[1].config["domain"] == "paypal.com"


@respx.mock
async def test_a_tenant_without_the_newer_interface_is_listed_through_the_older_one_with_the_address_filters(fetcher, isolated):
    page(HSBC + "/careers?location=India&skill=Java", HSBC_PAGE)
    refused = respx.get(f"{HSBC}/api/pcsx/search").mock(return_value=httpx.Response(403, json={"message": "PCSX is not enabled for this user."}))

    def v2(request):
        start = int(request.url.params["start"])
        return httpx.Response(200, json={"count": 12, "positions": [older(i) for i in range(start, min(start + 10, 12))]})

    listing = respx.get(f"{HSBC}/api/apply/v2/jobs").mock(side_effect=v2)
    found = await portals.find_board(HSBC + "/careers?location=India&skill=Java", fetcher)  # the check lists one page
    b = found.board
    assert (found.adapter.ats, b.company, b.config["host"], b.config["domain"]) == ("eightfold", "Hsbc", "portal.careers.hsbc.com", "hsbc.com")
    assert b.config["facets"] == {"location": "India", "skills": "Java"}  # the page says "skill", the older interface "skills"
    assert listing.calls[0].request.url.params["skills"] == "Java" and listing.calls[0].request.url.params["location"] == "India"
    assert isolated[0][1]["config"] == {"host": "portal.careers.hsbc.com", "domain": "hsbc.com"}  # remembered without the filters
    # a full listing pages through the older interface and does not retry the refused one on every page
    refused.reset(); listing.reset()
    jobs = await found.adapter.list_postings(b, fetcher)
    assert len(jobs) == 12 and jobs.complete and refused.call_count == 1 and listing.call_count == 2
    assert jobs[0].url == f"{HSBC}/careers/job/5000" and str(jobs[0].posted_at) == "2026-10-01" and jobs[0].department == "Technology"


@respx.mock
async def test_the_newer_interface_gets_only_the_filters_it_honours(fetcher):
    ad, b = registry.board_from_url("https://paypal.eightfold.ai/careers?location=India&skill=Java")
    route = respx.get("https://paypal.eightfold.ai/api/pcsx/search").mock(
        return_value=httpx.Response(200, json={"data": {"positions": [], "count": 0}}))
    await ad.list_postings(b, fetcher)
    sent = route.calls[0].request.url.params
    assert sent["location"] == "India" and "skills" not in sent  # that interface ignores skills, so a skill filter is not pretended
