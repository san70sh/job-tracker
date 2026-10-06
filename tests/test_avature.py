"""Avature career sites: recognising their addresses, listing a board over its paged search, reading one posting."""
import json

import httpx
import pytest
import respx

from jobtracker.adapters import registry
from jobtracker.adapters.avature import Avature, parse_list
from jobtracker.extract.job import extract
from jobtracker.http import Fetcher

BASE = "https://dth.avature.net/en_US/careers"


@pytest.fixture
async def fetcher():
    async with Fetcher(delay=0) as f:
        yield f


def row(job_id, title, place="India, Bangalore.", slug="Some-Slug"):
    """One search result, laid out the way Avature's list does, share buttons included (they link to the job as well)."""
    url = f"{BASE}/JobDetail/{slug}/{job_id}?jobId={job_id}"
    return f"""<li class="list__item"><div class="list__item__text">
      <div class="list__item__text__title"><a href="{url}"> {title} </a></div>
      <div class="list__item__text__subtitle"><span> {place} </span><span> Ref #{job_id} </span></div></div>
      <div class="list__item__actions"><a class="button--link" href="{BASE}/ApplicationMethods?jobId={job_id}">Apply</a>
      <div class="social-share"><a href="http://twitter.com/intent/tweet?url={BASE}/JobDetail/{slug}/{job_id}">Tweet</a></div></div></li>"""


def page(rows, legend):
    return f"""<html><body><span class="pagination__legend">{legend}</span><ul class="list list--jobs">{''.join(rows)}</ul></body></html>"""


PAGES = {
    "0": page([row(101, "AI Software Architect", slug="AI-Software-Architect"), row(102, "Data Engineer", "India, Pune."),
               row(103, "Lead Android", "")], "1-3 of 5 results"),
    "3": page([row(104, "QA Analyst"), row(105, "Site Reliability Engineer")], "4-5 of 5 results"),
}


def serve_list(pages):
    seen = []

    def handler(request):
        seen.append(request.url.params.get("jobOffset"))
        return httpx.Response(200, text=pages.get(request.url.params.get("jobOffset") or "0", page([], "0 results")))

    respx.get(url__regex=r"https://dth\.avature\.net/en_US/careers/SearchJobs/.*").mock(side_effect=handler)
    return seen


# ───────────────────────────── addresses ─────────────────────────────
@pytest.mark.parametrize("url", [
    f"{BASE}/SearchJobs", f"{BASE}/SearchJobs/", f"{BASE}/SearchJobs/?jobOffset=20", f"{BASE}", f"{BASE}/",
    f"{BASE}/JobDetail/AI-ML-Engineer/27677?jobId=27677",
])
def test_every_kind_of_avature_address_maps_to_the_same_board(url):
    ad, board = registry.board_from_url(url)
    assert ad.ats == "avature" and board.slug == "dth" and board.config == {"host": "dth.avature.net", "base": BASE}


def test_a_job_address_resolves_to_its_id_without_the_network():
    r = registry.resolve_static(f"{BASE}/JobDetail/AI-ML-Engineer-VAA-G6/27677?jobId=27677")
    assert r.adapter.ats == "avature" and r.target.posting_id == "27677" and r.target.board.config["base"] == BASE


@pytest.mark.parametrize("url", ["https://boards.greenhouse.io/acme", "https://www.avature.com/en/careers/SearchJobs",
                                 "https://example.com/en_US/careers/SearchJobs", "https://dth.avature.net/en_US/careers/Other/1"])
def test_other_addresses_are_not_avature(url):
    ad = registry.board_from_url(url)
    assert ad is None or ad[0].ats != "avature"


def test_a_company_domain_running_avature_is_added_in_hosts_json(monkeypatch):
    hosts = {"careers.example.com": {"ats": "avature", "company": "Example", "config": {"base": "https://careers.example.com/en_US/careers"}}}
    monkeypatch.setattr(registry, "load_hosts", lambda: hosts)
    job = "https://careers.example.com/en_US/careers/JobDetail/Engineer/555"
    r = registry.resolve_static(job)
    assert r.adapter.ats == "avature" and r.target.posting_id == "555"
    assert r.target.board.company == "Example" and r.target.board.config["base"] == "https://careers.example.com/en_US/careers"
    ad, board = registry.board_from_url("https://careers.example.com/en_US/careers/SearchJobs")
    assert ad.ats == "avature" and board.company == "Example" and board.config["base"] == "https://careers.example.com/en_US/careers"


# ───────────────────────────── listing ─────────────────────────────
def test_a_result_row_gives_id_title_and_place_and_ignores_share_buttons():
    rows = parse_list(PAGES["0"], f"{BASE}/SearchJobs/")
    assert [(r.ats_posting_id, r.title, r.location) for r in rows] == [
        ("101", "AI Software Architect", "India, Bangalore"), ("102", "Data Engineer", "India, Pune"), ("103", "Lead Android", None)]
    assert rows[0].url == f"{BASE}/JobDetail/AI-Software-Architect/101"  # one address per job: no ?jobId= tracking


@respx.mock
async def test_the_whole_board_is_read_across_pages(fetcher):
    seen = serve_list(PAGES)
    ad, board = registry.board_from_url(f"{BASE}/SearchJobs")
    batch = await ad.list_postings(board, fetcher)
    assert [p.ats_posting_id for p in batch] == ["101", "102", "103", "104", "105"]
    assert batch.complete is True and seen == [None, "3"]  # the second page starts after the first page's 3 rows


@respx.mock
async def test_a_board_larger_than_the_page_cap_is_reported_incomplete(fetcher):
    serve_list(PAGES)
    ad, board = registry.board_from_url(f"{BASE}/SearchJobs")
    board.config["max_pages"] = 1
    batch = await ad.list_postings(board, fetcher)
    assert len(batch) == 3 and batch.complete is False  # so postings missing from it are never treated as closed


@respx.mock
async def test_an_empty_board_is_complete_and_empty(fetcher):
    serve_list({"0": page([], "0 results")})
    ad, board = registry.board_from_url(f"{BASE}/SearchJobs")
    batch = await ad.list_postings(board, fetcher)
    assert list(batch) == [] and batch.complete is True


# ───────────────────────────── one posting ─────────────────────────────
JOB_PAGE = """<html><head><meta property="og:title" content="Senior AI ML Engineer"></head><body><main>
<div class="description-ajax" data-list=27677></div>
<script type="application/ld+json">%s</script></main></body></html>""" % json.dumps({
    "@context": "http://schema.org", "@type": "JobPosting", "title": "Senior AI ML Engineer", "datePosted": "2026-09-23",
    "hiringOrganization": {"@type": "Organization", "name": "Delta Global Technology Hub"},
    "jobLocation": {"@type": "Place", "address": {"addressLocality": "", "addressRegion": None, "addressCountry": None}},
    "description": ("<p><b>Responsibilities</b></p><ul><li>Build machine learning services in Python for flight operations.</li>"
                    "<li>Own model deployment on AWS and keep it reliable.</li></ul><p><b>Requirements</b></p>"
                    "<ul><li>5+ years of experience in software engineering with strong SQL skills.</li><li>Good communication.</li></ul>")})


@respx.mock
async def test_a_posting_is_read_from_its_structured_data(fetcher):
    r = registry.resolve_static(f"{BASE}/JobDetail/AI-ML-Engineer-VAA-G6/27677?jobId=27677")
    respx.get(f"{BASE}/JobDetail/AI-ML-Engineer-VAA-G6/27677", params={"jobId": "27677"}).mock(return_value=httpx.Response(200, text=JOB_PAGE))
    p = await r.adapter.fetch(r.target, fetcher)
    assert p.ats == "avature" and p.title == "Senior AI ML Engineer" and p.company == "Delta Global Technology Hub"
    assert p.ats_posting_id == "27677" and p.job_ref == "27677" and str(p.posted_at) == "2026-09-23"
    assert p.url == f"{BASE}/JobDetail/AI-ML-Engineer-VAA-G6/27677"  # without the ?jobId= tracking part
    ex = extract(p)
    assert {"responsibilities", "requirements"} <= {s.kind for s in ex.sections}
    assert {"Python", "AWS", "SQL"} <= set(ex.technologies) and ex.get("experience_min_years") == 5
