"""The experience filter: the overlap rule, and reading the years of only the postings that need it."""
from datetime import date, timedelta

import pytest

from jobtracker import board_options, experience, filters
from jobtracker.adapters import registry
from jobtracker.http import FetchError
from jobtracker.models import EXP_DEFERRED, EXP_READ, EXP_UNSTATED, ListedPosting, Posting


@pytest.mark.parametrize("found, want, fits", [
    ((2, 4), (3, 6), True),           # overlaps at 3-4
    ((5, None), (3, 6), True),        # "5+" starts inside the range
    ((8, 12), (3, 6), False),         # asks for more than you want
    ((0, 2), (3, 6), False),          # asks for less
    ((6, 6), (3, 6), True),           # the edge counts
    ((7, None), (3, 6), False),       # "7+" starts above the range
    ((2, None), (3, 6), True),        # "2+" includes the range
    ((5, 8), (None, 4), False),       # only a maximum wanted: nothing below 5 is asked
    ((2, 8), (None, 4), True),
    ((5, None), (6, None), True),     # only a minimum wanted: "5+" reaches it
    ((3, 5), (6, None), False),
    ((None, None), (3, 6), True),     # nothing stated: cannot be shown to be out of range
])
def test_the_range_a_posting_asks_for_must_overlap_the_range_you_want(found, want, fits):
    assert filters.experience_fits(*found, *want) is fits


BOARD = {"ats": "greenhouse", "slug": "acme", "company": "Acme", "config": {}, "title_include": [], "title_exclude": [],
         "location_include": [], "experience_min": 3, "experience_max": 6}


def listed(i, title="Backend Engineer", posted=None):
    return ListedPosting(ats_posting_id=str(i), url=f"https://boards.greenhouse.io/acme/jobs/{i}", title=title,
                         location="Pune", posted_at=posted or date.today())


class FakeAdapter:
    """Stands in for a job system: each posting id maps to the description to return, or an error to raise."""
    ats = "greenhouse"

    def __init__(self, pages):
        self.pages, self.opened = pages, []

    async def fetch(self, target, f):
        self.opened.append(target.posting_id)
        page = self.pages[target.posting_id]
        if isinstance(page, Exception):
            raise page
        return Posting(ats="greenhouse", url=target.url, title="x", description_html=page)


def req(text):
    return f"<h3>Requirements</h3><ul><li>{text}</li></ul>"


@pytest.fixture
def adapter(monkeypatch):
    holder = {}
    monkeypatch.setattr(registry, "adapters", lambda: {"greenhouse": holder["a"]})
    return lambda pages: holder.setdefault("a", FakeAdapter(pages))


async def test_the_years_come_from_the_requirements_or_a_labelled_line(adapter):
    a = adapter({"1": req("3 to 5 years of experience in backend development"), "2": req("8+ years of experience building services"),
                 "3": "<p>Experience: 4-6 years</p>", "4": req("Strong Java skills")})
    posts = [listed(i) for i in (1, 2, 3, 4)]
    states = await experience.annotate(BOARD, posts, None, known=set())
    assert [(p.experience_min, p.experience_max, p.experience_state) for p in posts] == [
        (3.0, 5.0, EXP_READ), (8.0, None, EXP_READ), (4.0, 6.0, EXP_READ), (None, None, EXP_UNSTATED)]
    assert states == {EXP_READ: 3, EXP_UNSTATED: 1}
    assert [experience.fits(BOARD, p) for p in posts] == [True, False, True, True]  # "8+" is out; "not stated" stays in


async def test_only_new_postings_that_pass_the_other_filters_are_opened(adapter):
    old = date.today() - timedelta(days=board_options.max_age_days() + 3)
    a = adapter({"1": req("5 years of experience"), "4": req("2 years of experience")})
    posts = [listed(1), listed(2), listed(3, title="Sales Manager"), listed(4), listed(5, posted=old)]
    board = {**BOARD, "title_include": ["engineer"]}
    await experience.annotate(board, posts, None, known={"2"})  # 2 is already stored; 3 fails the title filter; 5 is too old
    assert sorted(a.opened) == ["1", "4"]
    assert [p.experience_state for p in posts] == [EXP_READ, None, None, EXP_READ, None]


async def test_a_check_opens_only_so_many_and_defers_the_rest(adapter, monkeypatch):
    monkeypatch.setattr(board_options, "experience_checks_per_poll", lambda: 2)
    a = adapter({str(i): req("4 years of experience") for i in range(1, 6)})
    posts = [listed(i) for i in range(1, 6)]
    states = await experience.annotate(BOARD, posts, None, known=set())
    assert len(a.opened) == 2 and states[EXP_DEFERRED] == 3
    assert [p.experience_state for p in posts] == [EXP_READ, EXP_READ, EXP_DEFERRED, EXP_DEFERRED, EXP_DEFERRED]


async def test_a_posting_that_cannot_be_opened_is_kept_not_lost(adapter):
    adapter({"1": FetchError("503 GET https://x"), "2": KeyError("title")})
    posts = [listed(1), listed(2)]
    await experience.annotate(BOARD, posts, None, known=set())
    assert all(p.experience_state == EXP_UNSTATED and experience.fits(BOARD, p) for p in posts)


def test_a_board_filters_on_experience_when_either_end_is_set():
    assert not experience.wanted({"experience_min": None, "experience_max": None})
    assert experience.wanted({"experience_min": 3, "experience_max": None}) and experience.wanted({"experience_min": None, "experience_max": 6})
