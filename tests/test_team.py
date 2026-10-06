import pytest

from jobtracker.extract.job import extract
from jobtracker.extract.team import domain_hint, infer_team
from jobtracker.models import Posting


def team(title, text="", company="Acme"):
    g = infer_team(title, company, [], text)
    return g.value if g else None


@pytest.mark.parametrize("title,text,expected", [
    # explicit label beats everything
    ("Software Engineer", "About the role\nTeam: Audit & Logging\nYou will build.", "Audit & Logging"),
    ("Software Engineer", "Department: Merchant Onboarding\n", "Merchant Onboarding"),
    # sentence naming the team
    ("Software Engineer II", "Join the Ledger Platform team and help us scale.", "Ledger Platform"),
    ("Software Engineer", "As part of our Developer Experience group you will own tooling.", "Developer Experience"),
    ("Backend Engineer", "The Ads Infrastructure team is looking for an engineer.", "Ads Infrastructure"),
    # title suffix
    ("Software Engineer - Trust Engineering", "", "Trust Engineering"),
    ("Senior Software Engineer (Audit & Logging)", "", "Audit & Logging"),
    ("Software Engineer, Marketplace Data", "", "Marketplace Data"),
])
def test_team_found(title, text, expected):
    assert team(title, text) == expected


@pytest.mark.parametrize("title,text", [
    ("Software Engineer", "Join our team of talented engineers!"),          # generic
    ("Software Engineer", "Join the Engineering team."),                     # generic after stop words
    ("Software Engineer", "Join the Acme team and grow."),                   # the company itself
    ("Software Engineer - Java", ""),                                        # technology, not a team
    ("Software Engineer (Remote)", ""),
    ("Software Engineer - Bengaluru", ""),                                   # city
    ("Software Engineer (3-5 yrs)", ""),                                     # years
    ("Senior Software Engineer - Backend", ""),                              # discipline
    ("Software Engineer, ITC", ""),                                          # bare acronym: site or team, unknowable
    ("MongoDB hiring Senior Software Engineer in Pune, Maharashtra, India | LinkedIn", ""),  # job-board page title
    ("Rakuten - Careers", ""),
    ("Software Engineer", ""),
    ("Software Engineer", "you will work with the Platform team on ..." + "x" * 1600 + " join the Hidden Gem team"),  # deep in text
])
def test_team_not_guessed(title, text):
    assert team(title, text) is None


def test_two_sources_agreeing_raise_confidence():
    g = infer_team("Software Engineer - Ledger Platform", "Acme", [], "Join the Ledger Platform team today.")
    assert g and g.value == "Ledger Platform" and g.confidence == 0.75
    assert g.evidence.startswith("sentence+title")


def test_label_confidence_and_evidence():
    g = infer_team("Software Engineer", "Acme", [], "Team: Trust Engineering")
    assert g.confidence == 0.85 and "Team: Trust Engineering" in g.evidence


def test_domain_hint_is_appended_not_standalone():
    text = "Join the Ledger Platform team. You will build payment settlement, UPI checkout and wallet payouts."
    assert team("Software Engineer", text) == "Ledger Platform — Payments"
    # the same text with no team name gives nothing, even though the domain is obvious
    assert team("Software Engineer", "You will build payment settlement and UPI checkout services.") is None


def test_domain_hint_needs_a_clear_lead():
    assert domain_hint("Software Engineer", "payment and security work") is None  # one point each
    assert domain_hint("Payments Engineer", "") is None                             # a title keyword alone is 3 of the 4 needed
    assert domain_hint("Payments Engineer", "UPI settlement") == "Payments"         # title 3 + body 2


def test_hint_not_repeated_when_name_already_says_it():
    assert team("Software Engineer - Payments Platform", "UPI checkout settlement payments") == "Payments Platform"


def test_ats_department_wins_and_inference_only_fills_blanks():
    p = Posting(ats="greenhouse", url="https://x/1", title="Software Engineer - Trust Engineering", company="Acme",
                department="Payments", description_html="<p>Team: Something Else</p>")
    ex = extract(p)
    assert ex.get("team_domain") == "Payments" and ex.fields["team_domain"].method == "ats_api"
    p.department = None
    ex = extract(p)
    assert ex.get("team_domain") == "Something Else"
    assert ex.fields["team_domain"].method == "rules" and ex.fields["team_domain"].confidence >= 0.85
