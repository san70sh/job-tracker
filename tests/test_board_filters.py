"""Board filter terms and the suggestions behind the filter fields."""
import pytest

from jobtracker import filters, suggestions


@pytest.mark.parametrize("term,text,hit", [
    ("java", "Senior Java Developer", True),
    ("JAVA", "senior java developer", True),                   # any case
    ("java", "JavaScript Engineer", False),                    # whole words only
    ("software engineer", "Senior Software Engineer II", True),  # a phrase
    ("c++", "C++ Developer", True),                            # symbols are literal, not regex
    ("/java|kotlin/", "Kotlin Backend Engineer", True),        # /regex/ is the advanced form
    ("/^staff/", "Senior Staff Engineer", False),
])
def test_title_terms(term, text, hit):
    assert bool(filters.any_match(text, [term])) is hit


@pytest.mark.parametrize("term,place,hit", [
    ("Bengaluru", "Bangalore, India", True),                   # a known city also matches its other spellings
    ("Bangalore", "Bengaluru, Maruthi Onyx", True),
    ("Pune", "Pune, Gera Commerzone SEZ", True),
    ("Pune", "Punekar Nagar", False),
    ("Gurugram", "Gurgaon", True),
    ("Reading", "Reading, UK", True),                          # not a known city: plain word
])
def test_location_terms_know_city_spellings(term, place, hit):
    assert filters.any_match(place, [term], location=True) is hit


def test_passes_combines_the_three_filters():
    f = lambda t, p, i=(), e=(), l=(): filters.passes(t, p, list(i), list(e), list(l))  # noqa: E731
    assert f("Anything", None)                                              # no filters keeps everything
    assert f("Backend Engineer", "Pune", i=["backend"], l=["Pune"])
    assert not f("Frontend Engineer", "Pune", i=["backend"])               # no title term matches
    assert not f("Backend Intern", "Pune", i=["backend"], e=["intern"])    # an exclusion wins
    assert not f("Backend Engineer", "Leeds", i=["backend"], l=["Pune"])   # wrong place
    assert not f("Backend Engineer", None, l=["Pune"])                     # unknown place cannot match a place filter


def test_validate_names_the_bad_term():
    filters.validate(["java", "/a|b/"])
    with pytest.raises(ValueError, match="empty"):
        filters.validate(["java", "  "])
    with pytest.raises(ValueError, match="not a valid pattern"):
        filters.validate(["/(unclosed/"])


ROLES = (["Software Engineer II"] * 5 + ["Senior Software Engineer - Java"] * 3 + ["Backend Developer"] * 2
         + ["Platform Engineer"] + ["Software Development Engineer"])
PLACES = ["Bengaluru"] * 5 + ["Pune, India"] * 3 + ["Remote"] * 2 + [None] + ["Bangalore"]
ROWS = [{"role": r, "location": loc} for r, loc in zip(ROLES, PLACES)]


def test_title_suggestions_skip_words_that_match_nearly_everything():
    s = suggestions.suggest(ROWS)["title_include"]
    values = [o["value"] for o in s["top"] + s["more"]]
    assert values[0] == "software engineer"                            # common (8 of 12) but not universal
    assert "engineer" not in values and "software" not in values       # 10 and 9 of 12: they would filter almost nothing
    assert not ({"senior", "senior software"} <= set(values))          # overlapping terms: only the better one is kept
    assert "backend" in values or "backend developer" in values


def test_exclusions_come_from_words_you_never_applied_with():
    s = suggestions.suggest(ROWS)["title_exclude"]
    values = [o["value"] for o in s["top"] + s["more"]]
    assert "intern" in values and "director" in values
    assert "platform" not in values and "developer" not in values       # words that are in your titles are never suggested


def test_location_suggestions_are_places_only_most_common_first():
    s = suggestions.suggest(ROWS)["location_include"]
    names = [o["value"] for o in s["top"] + s["more"]]
    assert names[0] == "Bengaluru" and set(names) == {"Bengaluru", "Pune"}   # no Remote, no Unspecified
    assert s["top"][0]["count"] == 6                                    # 'Bangalore' counted with Bengaluru (5 + 1)


def test_top_is_five_and_the_rest_is_offered_as_more():
    rows = [{"role": f"Role {c}", "location": c} for c in ["Bengaluru", "Pune", "Hyderabad", "Chennai", "Mumbai", "Noida", "Gurugram"] for _ in range(2)]
    s = suggestions.suggest(rows)["location_include"]
    assert len(s["top"]) == 5 and len(s["more"]) == 2


def test_nothing_applied_gives_empty_lists_so_the_form_takes_any_text():
    s = suggestions.suggest([])
    assert all(v == {"top": [], "more": []} for v in s.values())
