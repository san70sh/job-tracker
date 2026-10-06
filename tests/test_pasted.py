import pytest

from jobtracker.extract.job import extract
from jobtracker.extract.pasted import canonical_key, find_link, link_job_ref, parse_pasted

LINKEDIN = """Software Engineer II
Acme Payments
Bengaluru, Karnataka, India (Hybrid) · 2 weeks ago · Over 100 applicants
Promoted by hirer · Actively reviewing applicants
Easy Apply
Save
About the job
Acme Payments builds UPI and checkout infrastructure used by millions of merchants.
Join the Ledger Platform team and help us scale settlement systems.

Responsibilities
Design and build backend services in Java and Spring Boot
Own Kafka based event pipelines
Mentor junior engineers

Requirements
3-5 years of experience in backend development
Experience with PostgreSQL and microservices

Nice to have
Exposure to Kubernetes

Show more
https://www.linkedin.com/jobs/view/4472684826/?trackingId=abc
"""

LABELLED = """Company: Globex India
Job Title: Senior Backend Engineer
Location: Pune, Maharashtra

ABOUT THE ROLE
We are looking for a Senior Backend Engineer to join our Trust Engineering team.

WHAT YOU'LL DO:
- Build REST APIs in Python
- Run services on AWS

QUALIFICATIONS:
- 6+ years of experience
- Strong SQL
"""

COMPANY_FIRST = """Globex India
Staff Software Engineer, Platform
Hyderabad, Telangana, India · 3 days ago · 25 applicants
About the job
Build the platform."""

NO_HEADER = """Backend Developer
Initech is hiring a Backend Developer for its Chennai office.
Responsibilities
- Write services
Requirements
- 2 years of Java"""


def kinds(ex):
    return {s.kind for s in ex.sections}


def test_linkedin_header_gives_company_title_location_mode_and_id():
    p = parse_pasted(LINKEDIN)
    assert (p.title.value, p.title.confidence) == ("Software Engineer II", 0.8)
    assert p.company.value == "Acme Payments" and p.company.confidence >= 0.6
    assert p.location.value == "Bengaluru, Karnataka, India"
    assert p.posting.work_mode == "Hybrid"
    assert p.posting.job_ref == "4472684826" and "linkedin.com/jobs/view/4472684826" in p.link
    ex = extract(p.posting)
    assert {"responsibilities", "requirements", "nice_to_have"} <= kinds(ex)
    assert {"Java", "Kafka", "PostgreSQL"} <= set(ex.technologies) and ex.technologies["Kubernetes"] == "nice_to_have"
    assert ex.get("team_domain", "").startswith("Ledger Platform")
    assert ex.get("experience_required").startswith("3-5 years")


def test_site_noise_and_header_lines_are_not_in_the_description():
    body = parse_pasted(LINKEDIN).posting.description_html
    for junk in ("Easy Apply", "Show more", "Promoted by hirer", "applicants", "linkedin.com"):
        assert junk not in body


def test_labels_win_and_all_caps_or_colon_lines_become_headings():
    p = parse_pasted(LABELLED)
    assert (p.company.value, p.title.value, p.location.value) == ("Globex India", "Senior Backend Engineer", "Pune, Maharashtra")
    assert p.company.how == "label" and p.company.confidence == 0.9
    ex = extract(p.posting)
    assert {"role", "responsibilities", "requirements"} <= kinds(ex)  # "ABOUT THE ROLE" is the role, not company boilerplate
    assert ex.get("team_domain") == "Trust Engineering"


def test_company_line_above_the_title():
    p = parse_pasted(COMPANY_FIRST)
    assert p.title.value == "Staff Software Engineer, Platform"
    assert p.company.value == "Globex India"


def test_no_header_company_from_prose_is_weak():
    p = parse_pasted(NO_HEADER)
    assert p.title.value == "Backend Developer"
    assert p.company.value == "Initech" and p.company.confidence < 0.6


def test_nothing_recognisable_leaves_company_and_title_blank():
    p = parse_pasted("we need somebody good with computers, apply soon, thanks a lot for reading this text here")
    assert p.company.value is None and p.title.value is None


def test_typed_values_replace_detected_ones():
    p = parse_pasted(LINKEDIN, overrides={"company": "Acme Pay", "role": "SDE 2", "location": "Pune", "job_ref": "X9"})
    assert (p.posting.company, p.posting.title, p.posting.location, p.posting.job_ref) == ("Acme Pay", "SDE 2", "Pune", "X9")


def test_pasted_text_with_angle_brackets_stays_text():
    p = parse_pasted("Backend Engineer\nAcme\nResponsibilities\n- keep latency <5ms and use <b>care</b>")
    assert "<b>" not in p.posting.description_html and "&lt;5ms" in p.posting.description_html


@pytest.mark.parametrize("url,ref", [
    ("https://www.linkedin.com/jobs/view/4472684826/?trackingId=1", "4472684826"),
    ("https://in.linkedin.com/jobs/view/senior-engineer-at-acme-4472684826", "4472684826"),
    ("https://www.linkedin.com/jobs/search/?currentJobId=99887766&keywords=x", "99887766"),
    ("https://in.indeed.com/viewjob?jk=abc123def", "abc123def"),
    ("https://boards.greenhouse.io/acme/jobs/123", None),
])
def test_job_id_from_link(url, ref):
    assert link_job_ref(url) == ref


def test_find_link_prefers_a_job_link_and_strips_punctuation():
    assert find_link("see https://example.com/about and https://www.linkedin.com/jobs/view/1234567/.") == \
        "https://www.linkedin.com/jobs/view/1234567/"
    assert find_link("no links here") is None


def test_duplicate_keys():
    a = canonical_key("https://www.linkedin.com/jobs/view/4472684826/?trackingId=1", "x")
    b = canonical_key("https://in.linkedin.com/jobs/view/some-title-4472684826", "y")
    assert a == b == "https://linkedin.com/jobs/view/4472684826"
    t1 = canonical_key(None, "Backend Engineer at   Acme.  Build things.")
    assert t1.startswith("manual://") and t1 == canonical_key(None, "backend engineer at acme. build things.")
    assert t1 != canonical_key(None, "A different posting entirely")


def test_page_hints_beat_text_guesses_and_are_trusted():
    glued = "About the job\nSoftware Development Engineer 2/3 \u2013 BackendTeam: Consumer ProductLocation: India (Gurgaon or Bangalore)\nResponsibilities\n- Build services in Go"
    p = parse_pasted(glued, hints={"title": "Software Development Engineer 2/3 \u2013 Backend", "company": "noon",
                                   "location": "Gurugram, India (Hybrid)", "confidence": 0.9})
    assert (p.title.value, p.title.confidence, p.title.how) == ("Software Development Engineer 2/3 \u2013 Backend", 0.9, "page")
    assert p.company.value == "noon" and p.location.value == "Gurugram, India"
    assert p.posting.title == "Software Development Engineer 2/3 \u2013 Backend" and p.posting.work_mode == "Hybrid"


def test_indeed_view_job_id():
    assert link_job_ref("https://in.indeed.com/jobs?q=java&vjk=abc123") == "abc123"


NOON = (
    "About the job\n"
    "Software Development Engineer 2/3 \u2013 BackendTeam: Consumer ProductLocation: India (Gurgaon or Bangalore) \n"
    "Company IntroductionWe're noon, the Middle East's homegrown e-commerce company, built to champion the region.\n"
    "About the roleConsumer Product teams own the systems behind what millions of customers see and do on noon every day.\n"
    "What you\u2019ll do\n"
    "Design, build and operate high-traffic backend services and the APIs (REST and gRPC) that power the app.\n"
    "Build event-driven workflows and pipelines that react to orders, payments and deliveries.\n"
    "What you\u2019ll need\n"
    "Strong proficiency in at least one language such as Python, Go or Java.\n"
    "Experience with relational databases such as MySQL, PostgreSQL or Spanner.\n"
)


def test_glued_labels_and_headings_are_split_back_apart():
    from jobtracker.extract.pasted import unglue

    out = unglue(NOON).split("\n")
    assert "Software Development Engineer 2/3 \u2013 Backend" in out
    assert "Team: Consumer Product" in out and "Location: India (Gurgaon or Bangalore) " in out
    assert "Company Introduction" in out and "About the role" in out
    assert unglue("Ask the Team: lead") == "Ask the Team: lead"                      # a space before it: not glued
    assert unglue("Responsibilities include") == "Responsibilities include"          # lower case after: a normal sentence


def test_noon_paste_gives_title_team_and_location():
    p = parse_pasted(NOON)
    assert p.title.value == "Software Development Engineer 2/3 \u2013 Backend"
    assert p.location.value == "India (Gurgaon or Bangalore)" and p.location.confidence == 0.9
    ex = extract(p.posting)
    assert ex.get("team_domain") == "Consumer Product"
    assert {"about_company", "role", "responsibilities", "requirements"} <= {s.kind for s in ex.sections}
    assert "Location:" not in p.posting.description_html  # used as the location, so not repeated in the body


# The structure of a real whole-page capture of a LinkedIn job (shortened): card, application status, the posting, then insights.
PAGE = "\n".join([
    "LinkedIn", "", "Senior Software Engineer", "", "Bengaluru, Karnataka, India \u00b7 17 hours ago \u00b7 Over 100 applicants", "",
    "Promoted by hirer \u00b7 No response insights available yet", "", "On-site", "Full-time",
    "Take the next step in your job search", "Practice an interview", "Application status", "Application submitted", "4 hours ago",
    "Go to company site", "People you can reach out to", "Vinayak and others in your network", "Show all",
    "About the job", "",
    "LinkedIn is the world's largest professional network, built to create economic opportunity for every member.", "",
    "Job Description", "This role will be based in Bangalore, India.", "",
    "Responsibilities", "Scale distributed applications and write code.", "Produce high quality software that is unit tested.", "",
    "Qualifications", "Basic Qualifications", "BA/BS Degree in Computer Science or related technical discipline.",
    "4+ years experience programming experience in Python or Go.", "Preferred Qualifications", "6+ years of relevant work experience.", "",
    "India Disability Policy",
    "Details: https://legal.linkedin.com/content/dam/legal/Policy_India_EqualOppPWD_9-12-2023.pdf", "",
    "Set alert for similar jobs", "Senior Software Engineer, Bengaluru, Karnataka, India", "Off",
    "Put your best foot forward with your application", "Hire a resume writer",
    "Applicants for this job", "851", "Applicants in the past day", "57% Entry level people applied for this job",
    "Hiring & headcount", "23,486", "Total employees", "About the company", "LinkedIn", "34,460,064 followers",
])
LI_URL = "https://www.linkedin.com/jobs/view/4476129432/"


def test_whole_linkedin_page_is_cut_down_to_the_posting():
    p = parse_pasted(PAGE, link=LI_URL)
    body = p.posting.description_html
    for chrome in ("Application status", "People you can reach out to", "Take the next step", "851", "followers",
                   "Hire a resume writer", "Put your best foot forward", "Practice an interview", "Full-time"):
        assert chrome not in body, chrome
    assert "4+ years experience" in body and "Basic Qualifications" in body and "Scale distributed" in body
    assert (p.title.value, p.company.value, p.location.value) == ("Senior Software Engineer", "LinkedIn", "Bengaluru, Karnataka, India")
    assert p.posting.work_mode == "Onsite"  # from the On-site chip on the card
    ex = extract(p.posting)
    assert {"responsibilities", "requirements"} <= {s.kind for s in ex.sections}
    assert ex.get("experience_required").startswith("4+ years")


def test_the_pages_own_link_beats_a_url_inside_the_text():
    p = parse_pasted(PAGE, link=LI_URL)
    assert p.link == LI_URL and p.posting.url == LI_URL
    assert p.posting.job_ref == "4476129432"
    # with no link given, a policy PDF in the footer is not mistaken for the job link
    assert parse_pasted(PAGE).link is None


def test_company_site_link_and_the_boards_own_id_can_differ():
    company_link = "https://careers.linkedin.com/jobs/senior-software-engineer-123"
    p = parse_pasted(PAGE, link=company_link, hints={"job_ref": "4476129432"})
    assert p.posting.url == company_link and p.posting.job_ref == "4476129432"


def test_find_link_ignores_links_that_are_not_jobs():
    assert find_link("Details: https://legal.linkedin.com/content/dam/legal/Policy.pdf") is None


def test_text_without_the_page_marker_is_not_trimmed():
    # "About the company" and friends are normal headings in a company's own description
    t = "Backend Engineer\nAcme\nAbout the company\nWe build things.\nResponsibilities\n- Write code\nPeople you can reach out to\nSomeone"
    assert "People you can reach out to" in parse_pasted(t).posting.description_html


def test_legal_footer_and_suggested_skills_are_not_requirements():
    p = parse_pasted(PAGE.replace("Set alert for similar jobs", "Suggested Skills\nData Structures & Algorithms\nPython\nSet alert for similar jobs"), link=LI_URL)
    req = extract(p.posting).get("requirements")
    assert "4+ years experience" in req
    for junk in ("Data Structures & Algorithms", "equal employment", "Disability", "legal.linkedin.com"):
        assert junk not in req, junk
