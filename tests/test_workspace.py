"""Workspace & Dashboard route checks.
"""
import os
import sys

from fastapi.testclient import TestClient

from app.auth import create_session_token, encrypt_credential, hash_password
from app.db import get_session, init_db
from app.main import app
from app.models import Application, User

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _client_for(email):
    init_db()
    with get_session() as s:
        u = s.query(User).filter(User.email == email).first()
        if not u:
            u = User(
                email=email,
                name="WS User",
                password_hash=hash_password("x" * 12),
                is_active=True,
            )
            s.add(u)
            s.commit()
        u.smtp_username = "ws-test@example.com"
        u.smtp_password_encrypted = encrypt_credential("ws-test-app-password")
        u.smtp_verified = True
        s.commit()
        uid = u.id
        # Fresh fixture rows for this user.
        s.query(Application).filter(Application.user_id == uid).delete()
        s.add_all(
            [
                Application(
                    user_id=uid,
                    email="a@x.com",
                    job_title="Draft Role",
                    company="Acme",
                    jd_text="x",
                    status="draft",
                ),
                Application(
                    user_id=uid,
                    email="b@x.com",
                    job_title="Legacy Pending Role",
                    company="Acme",
                    jd_text="x",
                    status="pending",
                ),
                Application(
                    user_id=uid,
                    email="c@x.com",
                    job_title="Sent Role",
                    company="Acme",
                    jd_text="x",
                    status="sent",
                ),
                Application(
                    user_id=uid,
                    email="d@x.com",
                    job_title="Failed Role",
                    company="Acme",
                    jd_text="x",
                    status="failed",
                ),
            ]
        )
        s.commit()
    token = create_session_token(uid, email)
    client = TestClient(app, raise_server_exceptions=False)
    client.cookies.set("careerpulse_auth", token)
    return client


def test_workspace_and_dashboard_routes():
    client = _client_for("ws-user@example.com")

    # 1. Check Executive Dashboard on /
    r_dash = client.get("/")
    assert r_dash.status_code == 200
    assert "Executive Overview" in r_dash.text
    assert "Discovered Postings" in r_dash.text and "ATS Match Index" in r_dash.text

    # 2. Check Workspace Queue on /workspace
    r = client.get("/workspace")
    assert r.status_code == 200
    assert "<h1>Workspace</h1>" in r.text
    assert "/workspace?status=draft" in r.text and "/workspace?status=sent" in r.text
    for title in ("Draft Role", "Legacy Pending Role", "Sent Role", "Failed Role"):
        assert title in r.text
    assert "Pending Approval" in r.text or "Draft" in r.text

    r = client.get("/workspace", params={"status": "sent"})
    assert (
        "Sent Role" in r.text
        and "Draft Role" not in r.text
        and "Failed Role" not in r.text
    )

    r = client.get("/workspace", params={"status": "draft"})
    assert (
        "Draft Role" in r.text
        and "Legacy Pending Role" in r.text
        and "Sent Role" not in r.text
    )

    r = client.get("/workspace", params={"status": "bogus"})
    assert "Draft Role" in r.text and "Sent Role" in r.text

    r = client.get("/workspace", params={"status": "approved"})
    assert "No approved applications" in r.text and "Show all" in r.text
