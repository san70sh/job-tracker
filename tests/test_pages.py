"""Every page renders, and the shared pieces (colours, job viewer, scripts) are wired in. No database needed."""
import pytest
from fastapi.testclient import TestClient

from jobtracker.web import app as webapp

client = TestClient(webapp.app)
JOB = "0af8f3fd-1c21-4b25-975a-f20bd824284c"


@pytest.mark.parametrize("path", ["/", "/pipeline", "/inbox", "/boards", f"/jobs/{JOB}"])
def test_pages_render_with_the_shared_colours_and_job_viewer(path):
    r = client.get(path)
    assert r.status_code == 200
    assert '/static/jobview.js' in r.text and 'data-st="applied"' in r.text      # palette rules and the viewer are in the layout
    assert "/api/meta" in r.text and "JT_STATUSES" not in r.text            # statuses come from the server's meta call, not a pasted list


def test_the_full_job_page_does_not_open_itself_as_a_modal():
    assert "window.NO_JOB_MODAL = true" in client.get(f"/jobs/{JOB}").text
    assert "NO_JOB_MODAL = true" not in client.get("/pipeline").text


def test_the_capture_pop_up_keeps_its_links_plain():
    assert "window.NO_JOB_MODAL = true" in client.get("/capture/abc").text


@pytest.mark.parametrize("path", ["/static/jobview.js", "/static/preview.js", "/static/modal.js", "/static/chips.js"])
def test_scripts_are_served(path):
    r = client.get(path)
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]


def test_each_script_owns_its_part_of_the_modal():
    """The dialog shell is shared (modal.js); only job-specific behaviour is in jobview.js."""
    modal, view = (client.get(f"/static/{n}.js").text for n in ("modal", "jobview"))
    for needed in ('role="dialog"', "aria-modal", "Escape", "Tab", "beforeClose", "overflow"):
        assert needed in modal, needed
    for needed in ("popstate", "ArrowRight", "nextflag", "Discard your unsaved changes", "Modal("):
        assert needed in view, needed
    for moved in ('role="dialog"', "downOnBackdrop"):
        assert moved not in view, moved                                      # not copied back into the viewer
