"""Text that sits loose between tags (Workday writes whole descriptions this way) must not be dropped."""
import pytest

from jobtracker.extract.job import extract
from jobtracker.extract.sections import classify_heading, looks_like_heading, split_sections
from jobtracker.extract.text import html_to_blocks, text_coverage
from jobtracker.models import Posting

T = "•\t"  # Workday types its bullets as a bullet character and a tab
# The shape of a real Workday description whose body is bare text and <br>, with wrapped boilerplate around it.
LOOSE = (
    "<p><span><b>Our Purpose</b></span></p><p></p><p><i>Acme powers payments for people in 200+ countries.</i></p><p></p>"
    "<p><b>Title and Summary</b></p><h3></h3><p></p>Software Engineer II<h3></h3><p style=\"text-align:inherit\"></p>"
    "Who is Acme?<br>Acme is a global technology company in the payments industry.<br>Our culture is built on trust.<br><br>"
    "Overview<br>The Payment Networks team is looking for a Software Developer to build resilient services.<br><br>"
    "Key Responsibilities<br>"
    + "".join(f"{T}{b}<br>" for b in (
        "Develop and maintain high-quality scalable applications", "Build backend services using Java (17+) and Spring Boot",
        "Contribute to microservices based applications", "Implement data persistence with RDBMS and NoSQL such as Cassandra",
        "Participate in Agile ceremonies and commit to the team goals")) +
    " <br>All About You<br>"
    + "".join(f"{T}{b}<br>" for b in (
        "Strong foundation in backend development", "Good understanding of Java and Spring Boot", "Experience working on real-time systems",
        "Familiarity with CI/CD practices and tools")) +
    "<h3></h3><p style=\"text-align:inherit\"></p><p style=\"text-align:left\"><b>Corporate Security Responsibility</b></p>"
    "<p style=\"text-align:left\"><br />All activities involving access to Acme assets come with an inherent risk.</p>"
    "<ul><li><p style=\"text-align:left\">Abide by the security policies and practices;</p></li>"
    "<li><p style=\"text-align:left\">Report any suspected security violation or breach.</p></li></ul>"
    "<p style=\"text-align:inherit\"></p><br /><p></p><br />&#xa;&#xa;"
)


def test_loose_lines_become_headings_bullets_and_paragraphs():
    blocks = [(b.kind, b.text) for b in html_to_blocks(LOOSE)]
    assert ("heading", "Key Responsibilities") in blocks and ("heading", "All About You") in blocks
    assert ("li", "Build backend services using Java (17+) and Spring Boot") in blocks
    assert sum(1 for k, _ in blocks if k == "li") == 5 + 4 + 2
    assert ("p", "Software Engineer II") in blocks
    assert not any("#xa" in t or t.strip() == "" for _, t in blocks)  # entity leftovers are dropped


def test_nothing_is_lost_any_more():
    assert text_coverage(LOOSE) >= 0.97


def test_sections_and_fields_from_loose_text():
    kinds = {s.kind: s for s in split_sections(LOOSE)}
    assert len(kinds["responsibilities"].bullets) == 5 and len(kinds["requirements"].bullets) == 4
    posting = Posting(ats="workday", url="https://acme.wd1.myworkdayjobs.com/x/job/Pune/SE-II_R-1", title="Software Engineer II",
                      company="Acme", description_html=LOOSE)
    ex = extract(posting)
    assert "Build backend services" in ex.get("key_responsibilities") and "Strong foundation" in ex.get("requirements")
    assert {"Java", "Spring Boot", "Cassandra", "Microservices"} <= set(ex.technologies)
    # the security boilerplate is footer material, not a responsibility or a requirement
    assert "Abide by" not in ex.get("key_responsibilities") and "Abide by" not in ex.get("requirements")


def test_wrapped_html_is_read_exactly_as_before():
    html = ("<h3>Responsibilities</h3><ul><li>Build services</li><li>Own pipelines</li></ul>"
            "<p>A plain paragraph.</p><div><p>Nested paragraph</p><ul><li>Item <b>bold</b> end</li></ul></div>"
            "<p><strong>Requirements:</strong></p><ul><li>3+ years</li></ul>")
    assert [(b.kind, b.text) for b in html_to_blocks(html)] == [
        ("heading", "Responsibilities"), ("li", "Build services"), ("li", "Own pipelines"), ("p", "A plain paragraph."),
        ("p", "Nested paragraph"), ("li", "Item bold end"), ("heading", "Requirements"), ("li", "3+ years")]


def test_page_furniture_between_tags_is_dropped():
    html = "<div><p>Real description paragraph.</p>Show more<br>Apply Now<br>Browse More Jobs<br>&#xa;&#xa;Second real line</div>"
    assert [b.text for b in html_to_blocks(html)] == ["Real description paragraph.", "Second real line"]


@pytest.mark.parametrize("heading,kind", [
    ("Corporate Security Responsibility", "logistics"),
    ("Overview", "role"),
    ("Who is Acme?", "about_company"),
    ("Company", "logistics"), ("Job Title", "logistics"), ("Job Posting Closing Date", "logistics"),
    ("Key Responsibilities", "responsibilities"), ("All About You", "requirements"),
    ("Responsibilities", "responsibilities"),  # the footer rule must not swallow the normal heading
])
def test_heading_kinds(heading, kind):
    assert classify_heading(heading) == kind


def test_the_heading_guess_is_shared_and_cautious():
    assert looks_like_heading("Key Responsibilities") and looks_like_heading("WHAT YOU WILL DO") and looks_like_heading("Qualifications:")
    assert not looks_like_heading("Experience with Java")          # a requirement line, not the heading "Experience"
    assert not looks_like_heading("Build backend services.")       # a sentence
    assert not looks_like_heading("• Responsibilities")       # a bullet


# ───────────────────────────── Airbnb-style headings, written in a company's own voice ─────────────────────────────
@pytest.mark.parametrize("heading,kind", [
    ("The Community You Will Join", "role"), ("The Difference You Will Make", "role"), ("The Difference You Will Make:", "role"),
    ("A Typical Day", "responsibilities"), ("A day in the life", "responsibilities"),
    ("Your Expertise", "requirements"), ("Technical expertise", "requirements"),
    ("Preferred expertise", "nice_to_have"),                                  # the nice-to-have rule still comes first
    ("Our Commitment To Inclusion & Belonging", "logistics"), ("How We'll Take Care of You", "benefits"),
    # headings with "you will" that ARE duties keep working
    ("What you will do", "responsibilities"), ("In this role, you will", "responsibilities"), ("You will", "responsibilities"),
])
def test_company_voice_headings(heading, kind):
    assert classify_heading(heading) == kind


AIRBNB_SHAPE = (
    "<p>Acme was born when two friends opened their home to travellers, and has grown to millions of hosts.</p>"
    "<h2>The Community You Will Join</h2><p>Our Supply Team is growing and we want you to be part of it.</p>"
    "<h2>The Difference You Will Make</h2><p>You will own the retention and growth of long-tail hosts in a region.</p>"
    "<h2>A Typical Day</h2><ul><li>Manage a book of business of host entrepreneurs and keep them engaged.</li>"
    "<li>Run one-on-one onboarding conversations with pricing and listing advice.</li>"
    "<li>Analyse performance data to find trends and act on opportunities.</li></ul>"
    "<h2>Your Expertise</h2><ul><li>3+ years of experience in sales, account management or partner support.</li>"
    "<li>Proficiency in CRM tools, particularly Salesforce.</li><li>Excellent communication and presentation skills.</li></ul>"
    "<h2>Our Commitment To Inclusion &amp; Belonging</h2><p>Acme is committed to working with the broadest talent pool possible.</p>"
    "<h2>How We'll Take Care of You</h2><p>The actual base pay depends on many factors.</p>"
)


def test_a_posting_in_a_companys_own_voice_gets_its_requirements_and_duties():
    ex = extract(Posting(ats="greenhouse", url="https://careers.acme.com/positions/1", title="Account Manager", company="Acme",
                         description_html=AIRBNB_SHAPE))
    kinds = [s.kind for s in ex.sections]
    assert kinds.count("requirements") == 1 and kinds.count("responsibilities") == 1
    resp, req = ex.get("key_responsibilities"), ex.get("requirements")
    assert "Manage a book of business" in resp and "Our Supply Team is growing" not in resp  # the intro is not a duty
    assert "3+ years of experience" in req and "CRM tools" in req and "broadest talent pool" not in req
    assert ex.fields["requirements"].confidence >= 0.8 and ex.get("experience_min_years") == 3
