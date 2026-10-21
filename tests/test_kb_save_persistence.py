"""KB success must mean acknowledged cloud persistence, including email test mode."""
import json
import uuid
from datetime import datetime

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from app import config
import app.main as main
from app.auth import create_session_token, hash_password
from app.db import get_session
from app.models import User
from radar import supabase_client


class ProfileCloud:
    """Plain client so production code cannot special-case Mock objects."""
    is_configured = True

    def __init__(self):
        self.rows = {}
        self.calls = []
        self.failure = None

    def upsert_user(self, payload):
        self.calls.append(dict(payload))
        if self.failure == "exception":
            raise RuntimeError("Private upstream failure details")
        if self.failure == "empty":
            return None
        if self.failure == "wrong_profile":
            return {**payload, "knowledge_base_json": "{}"}
        if self.failure == "wrong_user":
            return {**payload, "id": payload["id"] + 1}
        self.rows[payload["id"]] = dict(payload)
        return dict(payload)

    def get_user_by_id(self, user_id):
        return self.rows.get(user_id)


@pytest.fixture
def profile(monkeypatch):
    cloud = ProfileCloud()
    monkeypatch.setattr(main, "get_supabase_client", lambda: cloud)
    monkeypatch.setattr(supabase_client, "SupabaseClient", lambda: cloud)
    monkeypatch.setattr(config, "TEST_MODE", True)
    monkeypatch.setattr(config, "is_serverless", False)
    email = f"kb-save-{uuid.uuid4().hex}@example.com"
    original = {"name": "Original Name", "email": email, "skills": ["Python"],
                "experience": [], "projects": []}
    with get_session() as session:
        user = User(email=email, name="Original Name", password_hash=hash_password("test-only"),
                    knowledge_base_json=json.dumps(original))
        session.add(user)
        session.commit()
        user_id = user.id
    client = TestClient(main.app)
    client.cookies.set("careerpulse_auth", create_session_token(user_id, email))
    form = {
        "name": "Updated Name", "email": email, "skills_csv": "Python, Ethical Hacking",
        "experience_json": json.dumps([{"employer": "Probe Company", "title": "Engineer"}]),
        "projects_json": json.dumps([{"name": "Probe Project", "description": "Saved project"}]),
        "education_json": "[]", "certifications_json": "[]",
    }
    yield client, cloud, user_id, form, original
    client.close()


@pytest.mark.parametrize("test_mode", [True, False])
def test_kb_saves_to_cloud_in_both_email_modes_and_survives_cache_loss(profile, monkeypatch, test_mode):
    client, cloud, user_id, form, original = profile
    monkeypatch.setattr(config, "TEST_MODE", test_mode)
    response = client.post("/knowledge-base", data=form, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/knowledge-base?saved=1"
    assert len(cloud.calls) == 1
    remote = cloud.rows[user_id]
    saved = json.loads(remote["knowledge_base_json"])
    assert saved["skills"] == ["Python", "Ethical Hacking"]
    assert saved["experience"][0]["employer"] == "Probe Company"
    assert saved["projects"][0]["name"] == "Probe Project"
    with get_session() as session:
        user = session.get(User, user_id)
        assert json.loads(user.knowledge_base_json) == saved
        assert user.updated_at.replace(tzinfo=None) == datetime.fromisoformat(remote["updated_at"]).replace(tzinfo=None)
        user.knowledge_base_json = "{}"
        session.commit()
    fresh = client.get("/knowledge-base?saved=1")
    assert fresh.status_code == 200
    assert "saved to Supabase successfully" in fresh.text
    soup = BeautifulSoup(fresh.text, "html.parser")
    assert "Ethical Hacking" in soup.select_one('textarea[name="skills_csv"]').get_text()
    assert config.TEST_MODE is test_mode


@pytest.mark.parametrize("failure", ["empty", "exception", "wrong_profile", "wrong_user"])
def test_cloud_failure_keeps_submitted_form_without_false_success_or_local_commit(profile, failure):
    client, cloud, user_id, form, original = profile
    cloud.failure = failure
    response = client.post("/knowledge-base", data=form, follow_redirects=False)
    assert response.status_code == 503
    assert "location" not in response.headers
    assert "could not be saved to Supabase" in response.text
    assert "saved to Supabase successfully" not in response.text
    assert "Private upstream failure details" not in response.text
    soup = BeautifulSoup(response.text, "html.parser")
    assert soup.select_one('input[name="name"]')["value"] == "Updated Name"
    assert "Ethical Hacking" in soup.select_one('textarea[name="skills_csv"]').get_text()
    assert "Probe Company" in response.text and "Probe Project" in response.text
    with get_session() as session:
        user = session.get(User, user_id)
        assert user.name == "Original Name"
        assert json.loads(user.knowledge_base_json) == original
    assert not cloud.rows


def test_retry_after_failure_persists_the_same_form(profile):
    client, cloud, user_id, form, original = profile
    cloud.failure = "empty"
    assert client.post("/knowledge-base", data=form).status_code == 503
    cloud.failure = None
    assert client.post("/knowledge-base", data=form).status_code == 200
    assert "Ethical Hacking" in json.loads(cloud.rows[user_id]["knowledge_base_json"])["skills"]


@pytest.mark.parametrize("serverless", [True, False])
def test_missing_cloud_configuration_is_never_reported_as_cloud_success(profile, monkeypatch, serverless):
    client, cloud, user_id, form, original = profile
    cloud.is_configured = False
    monkeypatch.setattr(config, "is_serverless", serverless)
    response = client.post("/knowledge-base", data=form)
    assert response.status_code == (503 if serverless else 200)
    assert "saved to Supabase successfully" not in response.text
    assert not cloud.calls
    if not serverless:
        assert "Saved for this local session only" in response.text
    with get_session() as session:
        saved = json.loads(session.get(User, user_id).knowledge_base_json)
        if serverless:
            assert saved == original
        else:
            assert "Ethical Hacking" in saved["skills"]
