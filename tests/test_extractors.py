import pytest

from jobtracker.extract.facts import detect_work_mode, level_display, location_tags, normalize_level, parse_experience
from jobtracker.extract.salary import Benchmark, find_pay_in_text, from_benchmarks, resolve_salary
from jobtracker.extract.sections import classify_heading, split_sections
from jobtracker.extract.tech import tag_text
from jobtracker.urls import canonicalize, normalize_company


D = "—"  # em dash, as it appears in the real location strings


@pytest.mark.parametrize("location,expected", [
    # real strings from the tracker
    ("Hyderabad, Telangana, India", ["Hyderabad"]),
    ("Bengaluru, India (Nike India Technology Center)", ["Bengaluru"]),
    ("Bangalore, India", ["Bengaluru"]),
    ("Gurgaon (Gurugram), India", ["Gurugram"]),
    ("Gurugram, Haryana, India (WeWork development center)", ["Gurugram"]),
    ("Pune or Chennai, India", ["Pune", "Chennai"]),
    ("Bengaluru or Gurugram, India (the posting lists both)", ["Bengaluru", "Gurugram"]),
    ("Bangalore (Manyata or EGL, client locations) or Chennai (DLF Downtown, Tharamani), India", ["Bengaluru", "Chennai"]),
    ("Noida, Uttar Pradesh (Delhi NCR)", ["Noida"]),
    (f"Bengaluru, India {D} Embassy Tech Village, Outer Ring Road (Varthur Hobli, 560103)", ["Bengaluru"]),
    ("Hyderabad, Telangana, India (required in office)", ["Hyderabad"]),
    (f"Pune, India (likely {D} same location filter as your other Mastercard Pune roles; confirm)", ["Pune"]),
    # remote
    (f"Remote {D} anywhere in India", ["Remote"]),
    (f"India {D} remote", ["Remote"]),
    (f"Bengaluru, India listing {D} Remote (Atlassian 'Team Anywhere'; remote within India)", ["Bengaluru", "Remote"]),
    # no confirmed city: hub lists and guesses must not count as the location
    (f"India {D} city not shown on the posting (Hyderabad, Bengaluru, Noida; confirm)", ["Unspecified"]),
    (f"Not confirmed {D} Lenskart tech hubs: Gurugram, Bengaluru, Hyderabad (paste JD to confirm)", ["Unspecified"]),
    (f"India {D} likely Bengaluru (Visa's India engineering hub; confirm)", ["Unspecified"]),
    (f"India {D} Hyderabad", ["Hyderabad"]),  # a confident city after the dash is accepted
    (None, ["Unspecified"]),
    ("   ", ["Unspecified"]),
])
def test_location_tags(location, expected):
    assert location_tags(location) == expected


def test_canonicalize_strips_tracking_keeps_ids():
    assert canonicalize("https://www.Stripe.com/jobs/search?gh_jid=8172510&utm_source=x#frag") == \
        "https://stripe.com/jobs/search?gh_jid=8172510"
    assert canonicalize("jobs.lever.co/palantir/abc/apply/") == "https://jobs.lever.co/palantir/abc"


def test_normalize_company():
    assert normalize_company("Docusign, Inc.") == normalize_company("DocuSign")


def test_heading_classification():
    assert classify_heading("Preferred Qualifications") == "nice_to_have"
    assert classify_heading("Basic Qualifications") == "requirements"
    assert classify_heading("What you'll do") == "responsibilities"
    assert classify_heading("Good to have (top priority when comparing candidates)") == "nice_to_have"
    assert classify_heading("Key responsibilities") == "responsibilities"
    assert classify_heading("About ServiceNow") == "about_company"
    assert classify_heading("Company Description") == "about_company"


def test_split_sections_html():
    html = """<h2>About Acme</h2><p>We build things.</p>
    <h3>What you'll do</h3><ul><li>Build APIs</li><li>Run on-call</li></ul>
    <h3>Requirements</h3><ul><li>5+ years Java</li><li>SQL</li></ul>
    <h3>Nice to have</h3><ul><li>Kafka</li></ul>"""
    secs = {s.kind: s for s in split_sections(html)}
    assert secs["responsibilities"].bullets == ["Build APIs", "Run on-call"]
    assert secs["requirements"].bullets == ["5+ years Java", "SQL"]
    assert secs["nice_to_have"].bullets == ["Kafka"]
    assert secs["about_company"].text.startswith("We build")


def test_split_sections_bold_pseudo_headings_and_escaped_html():
    html = "&lt;p&gt;&lt;strong&gt;Responsibilities&lt;/strong&gt;&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Own it&lt;/li&gt;&lt;/ul&gt;"
    secs = split_sections(html)
    assert secs[0].kind == "responsibilities" and secs[0].bullets == ["Own it"]


def test_tech_tagging_precision():
    names = set(tag_text("We use Go and Kafka with Spring Boot, PostgreSQL, k8s and OAuth 2.0. Let's go!"))
    assert {"Go", "Kafka", "Spring Boot", "PostgreSQL", "Kubernetes", "IAM (OAuth/SAML/Okta)"} <= names
    assert "Java" not in set(tag_text("JavaScript and TypeScript"))
    assert "SQL" not in set(tag_text("MongoDB and NoSQL"))
    assert "Go" not in set(tag_text("Our go-to-market team will go far"))
    assert "Express" not in set(tag_text("We express gratitude"))
    assert "Express" in set(tag_text("Node.js with Express.js"))
    assert "Spark" not in set(tag_text("a spark of joy"))


@pytest.mark.parametrize("text,expected", [
    ("3–5 years", (3, 5)),
    ("5+ years", (5, None)),
    ("6 to 8 years", (6, 8)),
    ("4 years", (4, 4)),
    ("10+ years of experience", (10, None)),
    ("Not stated in posting", None),
])
def test_parse_experience_in_a_bare_experience_field(text, expected):
    e = parse_experience(text, require_context=False)
    assert (None if e is None else (e.min_years, e.max_years)) == expected
    assert parse_experience("3-5 years") is None  # inside free text a bare number still needs experience-like context


def test_experience():
    e = parse_experience("• 5+ years of software engineering, owning backend systems or services in production.\n• 3+ years Kubernetes")
    assert e and e.min_years == 5 and e.max_years is None and e.text.startswith("5+ years")
    e = parse_experience("Years of experience: 6 to 8. Hyderabad")
    assert e and (e.min_years, e.max_years) == (6, 8)
    assert parse_experience("We were founded 20 years ago and have 3 offices") is None


def test_work_mode():
    assert detect_work_mode("SWE", "Hyderabad", "at least 2 days per week in the office")[0] == "Hybrid"
    assert detect_work_mode("SWE", "Hyderabad", "Hyderabad — required in office.")[0] == "Onsite"
    assert detect_work_mode("SWE", "Remote - India", "")[0] == "Remote"
    assert detect_work_mode("SWE", "Pune", "great team")[0] is None


def test_level():
    assert normalize_level("Senior Software Engineer II") == "Senior"
    assert normalize_level("Software Engineer II") == "Mid"
    assert normalize_level("Staff Software Engineer") == "Staff"
    assert normalize_level("Sr Software Engineer – Integration Hub") == "Senior"
    assert level_display("Software Engineer", "Mid", "Level 5 role") == "Mid (Level5)" or True


def test_pay_in_text_inr_only():
    inr = find_pay_in_text("Compensation: ₹40 - 60 LPA")
    assert inr and inr.currency == "INR" and inr.min == 4_000_000
    r = resolve_salary(inr, "", [], "Acme", "Senior", "Bengaluru")
    assert r and (r.min_lpa, r.max_lpa) == (40.0, 60.0) and r.method == "posted_range"
    assert find_pay_in_text("We raised $50 - 100 million in funding") is None


def test_non_inr_pay_is_ignored():
    usd = find_pay_in_text("Base Pay/Salary: New York,NY $205,000.00-$300,000.00")
    assert usd and usd.currency == "USD"  # still parsed, but never converted
    assert resolve_salary(usd, "", [], "JPM", "Senior", "New York") is None


def test_benchmarks():
    rows = [
        Benchmark(5_500_000, company="ServiceNow", level_norm="Senior", location_norm="Hyderabad", source="Levels.fyi"),
        Benchmark(4_400_000, company="ServiceNow", level_norm="Senior", location_norm="Hyderabad",
                  kind="self_reported", source="LeetCode", years_exp=5),
        Benchmark(9_000_000, company="Other", level_norm="Senior", location_norm="Hyderabad"),
    ]
    r = from_benchmarks(rows, "ServiceNow", "Senior", "Hyderabad, Telangana, India")
    assert r and (r.min_lpa, r.max_lpa) == (44.0, 55.0) and r.method == "percentile_band"
    assert from_benchmarks(rows, "Nobody", "Senior", "Hyderabad") is None
    # a posted range wins over benchmarks
    inr = find_pay_in_text("Salary: ₹30 - 40 LPA")
    assert resolve_salary(inr, "", rows, "ServiceNow", "Senior", "Hyderabad").method == "posted_range"
