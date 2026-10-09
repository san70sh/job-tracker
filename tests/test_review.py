"""Which fields of a job need a look, and why."""
from jobtracker.extract.job import NOT_FOUND, NOT_SURE, REVIEW_FIELDS, review_reasons

FULL = {"role": "Backend Engineer", "company": "Acme", "key_responsibilities": "- build", "requirements": "- java"}
STRONG = {f: ("rules", 0.9) for f in REVIEW_FIELDS}


def test_nothing_to_review_when_every_field_is_present_and_read_with_confidence():
    assert review_reasons(FULL, STRONG, 0.8) == {}


def test_an_empty_field_is_not_found_and_a_weak_one_is_not_sure():
    values = {**FULL, "requirements": None, "key_responsibilities": "  "}
    assert review_reasons(values, STRONG, 0.8) == {"key_responsibilities": NOT_FOUND, "requirements": NOT_FOUND}
    assert review_reasons(FULL, {**STRONG, "company": ("rules", 0.55)}, 0.8) == {"company": NOT_SURE}


def test_a_field_you_wrote_is_settled_unless_you_emptied_it():
    weak_but_edited = {**STRONG, "requirements": ("manual", 1.0), "role": ("manual", 1.0)}
    assert review_reasons(FULL, {**weak_but_edited, "company": ("rules", 0.5)}, 0.8) == {"company": NOT_SURE}
    assert review_reasons({**FULL, "role": ""}, weak_but_edited, 0.8) == {"role": NOT_FOUND}


def test_a_job_without_provenance_is_taken_as_fine_unless_a_field_is_empty():
    assert review_reasons(FULL, {}, 0.8) == {}
    assert review_reasons({**FULL, "requirements": ""}, {}, 0.8) == {"requirements": NOT_FOUND}


def test_location_and_the_soft_fields_never_tag_a_job():
    assert review_reasons({**FULL, "location": None, "level": None}, {**STRONG, "location": ("rules", 0.1)}, 0.8) == {}
