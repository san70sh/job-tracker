"""Reading the job from the company's own page: what counts as a careers position page, and how it is fetched."""
import asyncio

import pytest

from jobtracker import pipeline
from jobtracker.http import FetchError, PostingGone
from jobtracker.models import Posting

LONG = ("<h3>Responsibilities</h3><ul><li>Design and build backend services in Java and Spring Boot for payments.</li>"
        "<li>Own Kafka based event pipelines and keep them reliable around the clock.</li></ul>"
        "<h3>Requirements</h3><ul><li>3-5 years of experience in backend development with relational databases.</li>"
        "<li>Strong problem solving skills and clear written communication with product teams.</li></ul>")


def posting(ats="html", url="https://careers.acme.com/jobs/12345", title="Backend Engineer", html=LONG, raw=None):
    return Posting(ats=ats, url=url, title=title, company="Acme", description_html=html, raw=raw or {"html_len": 9000})


MARKETING = "<p>" + "Reach millions of customers by selling on our marketplace. " * 10 + "</p>"
ONE_SECTION = "<h3>Responsibilities</h3><ul><li>" + "Build and run services every day. " * 12 + "</li></ul>"


@pytest.mark.parametrize("p,how,url,ok", [
    (posting(ats="greenhouse", raw={}), "greenhouse", "https://boards.greenhouse.io/acme/jobs/1", True),   # a recognised ATS
    (posting(raw={"jsonld": {"@type": "JobPosting"}}), "html", "https://acme.com/x/1", True),                 # declares itself a job
    (posting(), "html", "https://careers.acme.com/jobs/12345", True),                                         # careers path + job sections
    (posting(), "html", "https://www.noon.com/en-ae/seller/p-504574", False),                                 # a seller portal address
    (posting(html=MARKETING), "html", "https://careers.acme.com/jobs/1", False),                              # no job sections
    (posting(html=ONE_SECTION), "html", "https://careers.acme.com/jobs/1", False),                            # one section only
    (posting(), "greenhouse->html (NeedsFallback)", "https://www.acme.com/blog/post-1", False),               # sections, not a careers address
    (posting(html="<p>Short.</p>"), "html", "https://careers.acme.com/jobs/1", False),                        # no description
    (posting(title=""), "html", "https://careers.acme.com/jobs/1", False),                                    # no title
])
def test_careers_page_check(p, how, url, ok):
    assert (pipeline.careers_page_problem(p, how, url) is None) is ok


@pytest.fixture(autouse=True)
def fresh_cache():
    pipeline._site_cache.clear()


def fetch_with(monkeypatch, result):
    calls = []

    async def fake(url, f):
        calls.append(url)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(pipeline, "fetch_posting", fake)
    return calls


def test_job_boards_are_never_fetched_as_company_sites(monkeypatch):
    calls = fetch_with(monkeypatch, AssertionError("must not fetch"))
    for url in ("https://www.linkedin.com/jobs/view/1/", "https://in.indeed.com/viewjob?jk=1", "ftp://x/y", "not a link"):
        got, _, why = asyncio.run(pipeline.fetch_site_posting(url))
        assert got is None and why
    assert calls == []


def test_fetch_failures_become_a_reason_not_an_error(monkeypatch):
    for exc, word in ((PostingGone("404"), "gone"), (FetchError("500 boom"), "could not read"), (asyncio.TimeoutError(), "too long")):
        pipeline._site_cache.clear()
        fetch_with(monkeypatch, exc)
        got, _, why = asyncio.run(pipeline.fetch_site_posting("https://careers.acme.com/jobs/1"))
        assert got is None and word in why


def test_a_good_page_is_returned_once_and_cached(monkeypatch):
    calls = fetch_with(monkeypatch, (posting(), "html"))
    first = asyncio.run(pipeline.fetch_site_posting("https://careers.acme.com/jobs/12345"))
    second = asyncio.run(pipeline.fetch_site_posting("https://careers.acme.com/jobs/12345"))
    assert first[0].title == "Backend Engineer" and second[0] is first[0] and len(calls) == 1


def test_a_wrong_page_is_rejected_and_remembered(monkeypatch):
    url = "https://www.noon.com/en-ae/seller/p-504574?link_source=share_btn"
    calls = fetch_with(monkeypatch, (posting(html=MARKETING, url=url), "html"))
    got, _, why = asyncio.run(pipeline.fetch_site_posting(url))
    assert got is None and "careers position" in why
    asyncio.run(pipeline.fetch_site_posting(url))
    assert len(calls) == 1
