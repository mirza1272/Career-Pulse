"""Integration API tests for resume templates endpoints in CareerPulse."""

import pytest
from fastapi.testclient import TestClient
from app.main import create_app
from app.models import User, Application
from app.db import get_session
from app.auth import hash_password, create_session_token


@pytest.fixture
def client():
    app = create_app()
    return TestClient(app)


@pytest.fixture
def auth_cookie(client):
    with get_session() as s:
        user = s.get(User, 1)
        if not user:
            user = User(
                id=1,
                email="test_template_user@example.com",
                password_hash=hash_password("Password123!"),
                name="Test Template User",
                smtp_verified=True,
                smtp_username="test_template_user@example.com",
                smtp_password_encrypted="dummy_enc_pwd",
                selected_template_id="apex_modern",
            )
            s.add(user)
        else:
            user.smtp_verified = True
            user.smtp_username = "test_template_user@example.com"
            user.smtp_password_encrypted = "dummy_enc_pwd"
        s.commit()
    token = create_session_token(1, "test_template_user@example.com")
    return {"careerpulse_auth": token}


def test_templates_gallery_route(client, auth_cookie):
    resp = client.get("/templates", cookies=auth_cookie)
    assert resp.status_code == 200
    assert "Resume Templates Gallery" in resp.text
    assert "Apex Modern" in resp.text
    assert "Oxford Editorial" in resp.text
    assert "Silicon Compact" in resp.text
    assert "Monarch Executive" in resp.text
    assert "Nova Minimalist" in resp.text


def test_template_preview_endpoint(client, auth_cookie):
    for tid in ["apex_modern", "oxford_editorial", "silicon_compact", "monarch_executive", "nova_minimalist"]:
        resp = client.get(f"/templates/preview/{tid}", cookies=auth_cookie)
        assert resp.status_code == 200
        assert "<html" in resp.text
        assert "class=\"resume\"" in resp.text or "resume-a4" in resp.text or "entry" in resp.text


def test_template_select_json_endpoint(client, auth_cookie):
    resp = client.post(
        "/templates/select",
        json={"template_id": "oxford_editorial"},
        cookies=auth_cookie,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("success") is True
    assert data.get("selected_template_id") == "oxford_editorial"


def test_template_sample_pdf_endpoint(client, auth_cookie):
    resp = client.get("/templates/sample-pdf/silicon_compact", cookies=auth_cookie)
    assert resp.status_code == 200
    assert resp.headers.get("content-type") == "application/pdf"
    assert resp.content.startswith(b"%PDF-")


def test_application_change_template_endpoint(client, auth_cookie):
    with get_session() as s:
        app_row = s.get(Application, 999)
        if not app_row:
            app_row = Application(
                id=999,
                user_id=1,
                email="hiring@techcorp.internal",
                job_title="Senior Python Architect",
                company="TechCorp",
                jd_text="Python FastAPI Distributed Systems",
                template_id="apex_modern",
            )
            s.add(app_row)
            s.commit()

    resp = client.post(
        "/application/999/change-template",
        data={"template_id": "monarch_executive"},
        cookies=auth_cookie,
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    assert "template_changed=monarch_executive" in resp.headers.get("location", "")

    # Verify DB update
    with get_session() as s:
        updated = s.get(Application, 999)
        assert updated.template_id == "monarch_executive"
