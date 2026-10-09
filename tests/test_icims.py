"""iCIMS portals: recognising their addresses, listing a board from its search page, reading places written as codes."""
import httpx
import pytest
import respx

from jobtracker.adapters import discovery, registry
from jobtracker.adapters.icims import IcimsClassic, parse_list, place
from jobtracker.http import Fetcher

HOST = "indiacareers-docusign.icims.com"


@pytest.fixture
async def fetcher():
    async with Fetcher(delay=0) as f:
        yield f


def row(job_id, title, where="IN-KA-Bengaluru", category="Engineering"):
    """One result row, laid out the way iCIMS does (the job's page address, the place and the category as header fields)."""
    return f"""<div class="row"><div class="col-xs-6 header left"><span class="sr-only field-label">Job ID</span><span>2026-{job_id}</span></div>
    <div class="col-xs-12 title"><a href="https://{HOST}/jobs/{job_id}/{title.lower().replace(' ', '-')}/job?in_iframe=1" class="iCIMS_Anchor"
      title="{job_id} - {title}"><span class="sr-only field-label">External Title</span><h3>
      {title}</h3></a></div>
    <div class="col-xs-12 additionalFields"><dl class="iCIMS_JobHeaderGroup">
      <div class="iCIMS_JobHeaderTag"><dt class="iCIMS_JobHeaderField"><span class="sr-only field-label">Job Locations</span></dt><dd class="iCIMS_JobHeaderData"><span>
        {where}</span></dd></div>
      <div class="iCIMS_JobHeaderTag"><dt class="iCIMS_JobHeaderField">Category</dt><dd class="iCIMS_JobHeaderData"><span>{category}</span></dd></div>
    </dl></div></div>"""


def page(*rows, of="Page 1 of 1"):
    return f'<html><body><div>{of}</div><ul class="iCIMS_JobsTable">{"".join(rows)}</ul></body></html>'


def test_places_written_as_codes_are_said_the_way_a_person_would():
    assert place("IN-KA-Bengaluru") == "Bengaluru, KA, India"
    assert place("US-Remote") == "Remote, United States"
    assert place("IN-KA-Bengaluru | GB-LND-London") == "Bengaluru, KA, India; London, LND, United Kingdom"
    assert place("ZZ-AB-Somewhere") == "Somewhere, AB, ZZ"  # an unknown code stays as it is
    assert place("Bengaluru, India") == "Bengaluru, India" and place(None) is None


def test_a_search_page_gives_id_title_place_and_category():
    jobs = parse_list(page(row(30475, "Executive Assistant"), row(30404, "Software Engineer")), HOST)
    assert [(j.ats_posting_id, j.title, j.location, j.department) for j in jobs] == [
        ("30475", "Executive Assistant", "Bengaluru, KA, India", "Engineering"), ("30404", "Software Engineer", "Bengaluru, KA, India", "Engineering")]
    assert jobs[0].url == f"https://{HOST}/jobs/30475/executive-assistant/job"  # the page's own address, without ?in_iframe=1


def test_portal_addresses_are_boards_and_job_addresses_are_jobs():
    ad = IcimsClassic()
    b = ad.board_from_url(f"https://{HOST}/jobs/search?ss=1&searchKeyword=java&searchLocation=-12827-&in_iframe=1")
    assert (b.slug, b.company, b.config["facets"]) == (HOST, "Docusign", {"searchKeyword": "java", "searchLocation": "-12827-"})  # in_iframe is not a filter
    assert ad.board_from_url(f"https://{HOST}/").config == {"host": HOST}
    assert ad.board_from_url(f"https://{HOST}/jobs/30475/software-engineer/job") is None  # that is a job
    assert ad.identify(f"https://{HOST}/jobs/30475/software-engineer/job").posting_id == "30475"
    # iCIMS' own assets and tenant CDN copies are not portals
    assert ad.board_from_url("https://cdn02.icims.com/") is None and ad.board_from_url("https://c-12844-20240424-careers-docusign-com.i.icims.com/") is None


def test_a_jibe_widget_on_an_icims_portal_does_not_make_it_a_jibe_site():
    html = '<div data-jibe-search-version="1" class="jibeapply"></div>'
    assert not discovery.recognise_page(html, f"https://{HOST}/")  # the case that said "jibe board ... /api/jobs 404"
    assert [a.ats for a, _ in discovery.recognise_page(html, "https://careers.docusign.com/jobs")] == ["jibe"]  # a real Jibe site still is


@respx.mock
async def test_the_list_pages_through_the_search_and_keeps_the_address_filters(fetcher):
    ad, b = registry.board_from_url(f"https://{HOST}/jobs/search?ss=1&searchKeyword=engineer")
    route = respx.get(f"https://{HOST}/jobs/search")
    route.side_effect = lambda r: httpx.Response(200, text=(
        page(row(1, "Software Engineer"), row(2, "Data Engineer"), of="Page 1 of 2") if r.url.params["pr"] == "0"
        else page(row(3, "Backend Engineer"), of="Page 2 of 2")))
    jobs = await ad.list_postings(b, fetcher)
    assert [j.ats_posting_id for j in jobs] == ["1", "2", "3"] and jobs.complete and route.call_count == 2
    sent = route.calls[0].request.url.params
    assert sent["searchKeyword"] == "engineer" and sent["in_iframe"] == "1"


@respx.mock
async def test_an_empty_portal_is_a_valid_empty_list(fetcher):
    ad, b = registry.board_from_url(f"https://{HOST}/")
    respx.get(f"https://{HOST}/jobs/search").mock(return_value=httpx.Response(200, text=page(of="Page 1 of 1")))
    jobs = await ad.list_postings(b, fetcher)
    assert list(jobs) == [] and jobs.complete
