"""The browser-extension endpoints: token handling and the short-lived hand-off. No database involved."""
import time

import pytest
from fastapi.testclient import TestClient

from jobtracker import config
from jobtracker.web import app as webapp

TEXT = "About the role. We build payment services in Java and Kafka for merchants across India. " * 2


class Settings:
    def __init__(self, token):
        self.capture_token = token


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(webapp, "get_settings", lambda: Settings("s3cret-token"))
    webapp._captures.clear()
    return TestClient(webapp.app)


def post(client, token="s3cret-token", **body):
    payload = {"url": "https://www.linkedin.com/jobs/view/1234567890/", "text": TEXT, "source": "linkedin",
               "hints": {"title": "Backend Engineer", "company": "Acme", "confidence": 0.9}, **body}
    return client.post("/api/capture", json=payload, headers={"X-Capture-Token": token} if token else {})


def test_capture_needs_the_right_token(client):
    assert post(client, token=None).status_code == 401
    assert post(client, token="wrong").status_code == 401
    assert post(client).status_code == 200


def test_capture_is_off_until_a_token_is_configured(client, monkeypatch):
    monkeypatch.setattr(webapp, "get_settings", lambda: Settings(None))
    r = post(client, token="anything")
    assert r.status_code == 503 and "capture-token" in r.json()["detail"]


def test_capture_hands_over_text_link_and_hints(client):
    cid = post(client, top="Acme\nBackend Engineer\nPune, India \u00b7 2 days ago").json()["id"]
    got = client.get(f"/api/captures/{cid}").json()
    assert got["link"].endswith("/jobs/view/1234567890/") and got["hints"]["company"] == "Acme"
    assert got["text"].startswith("Acme\nBackend Engineer\nPune, India") and TEXT.strip() in got["text"]
    assert client.get(f"/capture/{cid}").status_code == 200  # the pop-up page itself


def test_unknown_or_expired_capture_is_a_404(client, monkeypatch):
    assert client.get("/api/captures/nope").status_code == 404
    cid = post(client).json()["id"]
    real = time.time
    monkeypatch.setattr(webapp.time, "time", lambda: real() + webapp.CAPTURE_TTL + 5)
    assert client.get(f"/api/captures/{cid}").status_code == 404


def test_too_short_a_page_is_refused(client):
    assert post(client, text="Sign in").status_code == 422


def test_capture_token_command_appends_without_touching_other_lines(tmp_path, monkeypatch, capsys):
    from jobtracker import __main__ as cli

    env = tmp_path / ".env"
    env.write_bytes(b"DATABASE_URL=postgresql://x:y@localhost/db")  # no trailing newline, as editors often leave it
    state = {"token": None}
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "get_settings", lambda: Settings(state["token"]))
    assert cli.cmd_capture_token(None) == 0
    lines = env.read_text().splitlines()
    assert lines[0] == "DATABASE_URL=postgresql://x:y@localhost/db" and lines[1].startswith("CAPTURE_TOKEN=")
    token = lines[1].split("=", 1)[1]
    assert len(token) >= 40 and token in capsys.readouterr().out
    state["token"] = token  # a second run shows the same token and writes nothing
    cli.cmd_capture_token(None)
    assert env.read_text().count("CAPTURE_TOKEN=") == 1


def test_company_site_link_is_the_job_link_and_the_boards_id_is_kept(client):
    cid = post(client, apply_url="https://careers.acme.com/jobs/senior-engineer-77").json()["id"]
    got = client.get(f"/api/captures/{cid}").json()
    assert got["link"] == "https://careers.acme.com/jobs/senior-engineer-77"   # 1. the company's own posting
    assert got["hints"]["job_ref"] == "1234567890"                             #    the Job ID stays LinkedIn's


def test_without_a_company_link_the_linkedin_page_is_the_job_link(client):
    cid = post(client).json()["id"]
    got = client.get(f"/api/captures/{cid}").json()
    assert got["link"] == "https://www.linkedin.com/jobs/view/1234567890/"     # 2. fall back to this job on LinkedIn
