"""Rebuilding a posting from the response saved with a job (the basis of the reparse command)."""
import json
from pathlib import Path

import pytest

from jobtracker import reparse
from jobtracker.adapters.greenhouse import Greenhouse
from jobtracker.adapters.smartrecruiters import SmartRecruiters
from jobtracker.adapters.workday import Workday
from jobtracker.models import BoardRef

FX = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FX / name).read_text(encoding="utf-8"))


NVIDIA = "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/Israel-Yokneam/Software-Engineer--SPE_JR2015623"


@pytest.mark.parametrize("ats,fixture,url,direct", [
    ("workday", "workday_detail.json", NVIDIA,
     lambda raw, url: Workday.to_posting(raw, BoardRef("workday", "nvidia", "NVIDIA", {"host": "nvidia.wd5.myworkdayjobs.com", "site": "NVIDIAExternalCareerSite"}), url)),
    ("greenhouse", "greenhouse_detail.json", "https://boards.greenhouse.io/stripe/jobs/8172510",
     lambda raw, url: Greenhouse.to_posting(raw, "stripe", None)),
    ("smartrecruiters", "smartrecruiters_detail.json", "https://jobs.smartrecruiters.com/ServiceNow/744000153446319-sr-software-engineer",
     lambda raw, url: SmartRecruiters.to_posting(raw, BoardRef("smartrecruiters", "ServiceNow"))),
])
def test_a_saved_response_rebuilds_the_same_posting(ats, fixture, url, direct):
    raw = load(fixture)
    rebuilt, why = reparse._from_adapter(ats, raw, url)
    expected = direct(raw, url)
    assert rebuilt is not None, why
    assert (rebuilt.title, rebuilt.description_html, rebuilt.job_ref) == (expected.title, expected.description_html, expected.job_ref)


@pytest.mark.parametrize("ats,raw,why", [
    ("html", {"html_len": 9000}, "HTML was not saved"),      # generic pages keep only a summary
    ("icims", {"jsonld": {}}, "HTML was not saved"),
    ("workday", None, "no saved response"),
    ("workday", {"unexpected": True}, "did not fit"),
])
def test_what_cannot_be_rebuilt_says_why(ats, raw, why):
    got, reason = reparse._from_adapter(ats, raw, NVIDIA)
    assert got is None and why in reason


def test_an_address_that_no_longer_maps_to_a_board_is_skipped():
    got, reason = reparse._from_adapter("workday", {"jobPostingInfo": {}}, "https://example.com/careers/1")
    assert got is None and "no longer maps" in reason
