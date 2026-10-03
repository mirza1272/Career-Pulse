"""Test Suite for Privacy Policy, Terms of Service, and Follow-Up Automation Rules.

Verifies:
1. /privacy and /terms are publicly accessible without authentication.
2. Login screen renders valid clickable links to /privacy and /terms.
3. Follow-up email dispatch rules:
   - Max 2 follow-up emails total.
   - Unopened email (open_count == 0): eligible for 1 follow-up max; subsequent attempts are blocked.
   - Opened email (open_count > 0): eligible for up to 2 follow-ups; 3rd attempt is blocked.
   - Replied or bounced applications immediately terminate the follow-up sequence.
4. Batch thread sync detects replies and bounces.
"""

import datetime as dt
import json
import uuid
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
import pytest

from app import config, gmail
from app.auth import create_session_token, encrypt_credential, hash_password
from app.db import get_session, init_db
from app.main import app
from app.models import Application, EmailActivity, User
from app.outbound import compose_followup_message, dispatch_followup_for_application


@pytest.fixture(autouse=True)
def setup_test_env():
    config.SESSION_SECRET = "test-session-secret-for-followup-unit-tests"
    config.TEST_MODE = True
    config.TEST_RECIPIENT = "test-recipient@example.com"
    init_db()


def _create_user(email: str, name: str = "Test Candidate") -> User:
    with get_session() as s:
        u = s.query(User).filter(User.email == email).first()
        if not u:
            u = User(
                email=email,
                name=name,
                password_hash=hash_password("SecurePass123!"),
                is_active=True,
                smtp_host="smtp.gmail.com",
                smtp_port=587,
                smtp_username=email,
                smtp_password_encrypted=encrypt_credential("mock-password"),
                smtp_verified=True,
            )
            s.add(u)
            s.commit()
        return u


# =============================================================================
# 1. LEGAL & POLICY PAGES VERIFICATION
# =============================================================================
def test_privacy_and_terms_pages_public_access():
    """Verify /privacy and /terms load without requiring login (critical for Google OAuth verification)."""
    client = TestClient(app)

    # Privacy Policy check
    r_privacy = client.get("/privacy")
    assert r_privacy.status_code == 200
    assert "Privacy Policy" in r_privacy.text
    assert "Google API Services User Data Policy" in r_privacy.text
    assert "Limited Use" in r_privacy.text

    # Terms of Service check
    r_terms = client.get("/terms")
    assert r_terms.status_code == 200
    assert "Terms of Service" in r_terms.text
    assert "Acceptable Use Policy" in r_terms.text


def test_login_page_links_to_privacy_and_terms():
    """Verify /login screen has clickable links to /privacy and /terms."""
    client = TestClient(app)
    r_login = client.get("/login")
    assert r_login.status_code == 200
    assert 'href="/privacy"' in r_login.text
    assert 'href="/terms"' in r_login.text


# =============================================================================
# 2. FOLLOW-UP POLICY: UNOPENED EMAIL (1 FOLLOW-UP MAX)
# =============================================================================
@patch("smtplib.SMTP")
def test_followup_policy_for_unopened_email(mock_smtp_cls):
    """If candidate's email was never opened (open_count == 0), allow max 1 follow-up then close sequence."""
    instance = mock_smtp_cls.return_value.__enter__.return_value
    instance.login.return_value = (235, b"2.7.0 Accepted")

    user = _create_user(f"unopened_{uuid.uuid4().hex[:6]}@example.com")
    with get_session() as s:
        app_row = Application(
            user_id=user.id,
            email="recruiter_unopened@company.com",
            job_title="Backend Engineer",
            company="CloudScale",
            drafted_email="Original application email",
            subject="Application for Backend Engineer",
            status="sent",
            open_count=0,
            opened_at=None,
            followup_count=0,
            followup_due_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1),
        )
        s.add(app_row)
        s.commit()
        app_id = app_row.id

    # 1. Dispatch 1st follow-up (Should SUCCEED)
    with get_session() as s:
        res1 = dispatch_followup_for_application(app_id, user.id, s)
        assert res1["success"] is True
        assert res1["followup_count"] == 1
        # Because it was unopened, next_due should be None (sequence closed)
        assert res1["next_due"] is None

    with get_session() as s:
        a = s.get(Application, app_id)
        assert a.followup_count == 1
        assert a.followup_due_at is None
        assert a.followup_sent_at is not None

    # 2. Attempting 2nd follow-up for unopened email MUST BE BLOCKED
    with get_session() as s:
        res2 = dispatch_followup_for_application(app_id, user.id, s)
        assert res2["success"] is False
        assert "Unopened email limit" in res2["error"] or "limit reached" in res2["error"]


# =============================================================================
# 3. FOLLOW-UP POLICY: OPENED EMAIL (2 FOLLOW-UPS MAX)
# =============================================================================
@patch("smtplib.SMTP")
def test_followup_policy_for_opened_email(mock_smtp_cls):
    """If recruiter opened email (open_count > 0), allow 2 follow-ups total, then close."""
    instance = mock_smtp_cls.return_value.__enter__.return_value
    instance.login.return_value = (235, b"2.7.0 Accepted")

    user = _create_user(f"opened_{uuid.uuid4().hex[:6]}@example.com")
    with get_session() as s:
        app_row = Application(
            user_id=user.id,
            email="recruiter_opened@company.com",
            job_title="Senior AI Architect",
            company="InnovateAI",
            drafted_email="Original application email",
            subject="Application for Senior AI Architect",
            status="sent",
            open_count=2,
            opened_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1),
            followup_count=0,
            followup_due_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2),
        )
        s.add(app_row)
        s.commit()
        app_id = app_row.id

    # 1. Dispatch 1st follow-up (Should SUCCEED and schedule 2nd follow-up)
    with get_session() as s:
        res1 = dispatch_followup_for_application(app_id, user.id, s)
        assert res1["success"] is True
        assert res1["followup_count"] == 1
        assert res1["next_due"] is not None  # Scheduled for follow-up #2

    with get_session() as s:
        a = s.get(Application, app_id)
        assert a.followup_count == 1
        assert a.followup_due_at is not None

    # 2. Dispatch 2nd follow-up (Should SUCCEED and close sequence)
    with get_session() as s:
        res2 = dispatch_followup_for_application(app_id, user.id, s)
        assert res2["success"] is True
        assert res2["followup_count"] == 2
        assert res2["next_due"] is None  # Sequence closed after 2 follow-ups

    with get_session() as s:
        a = s.get(Application, app_id)
        assert a.followup_count == 2
        assert a.followup_due_at is None

    # 3. Attempting 3rd follow-up MUST BE BLOCKED
    with get_session() as s:
        res3 = dispatch_followup_for_application(app_id, user.id, s)
        assert res3["success"] is False
        assert "Maximum follow-up limit" in res3["error"]


# =============================================================================
# 4. FOLLOW-UP BLOCKED ON REPLY OR BOUNCE
# =============================================================================
def test_followup_blocked_if_replied_or_bounced():
    """Follow-up must be immediately stopped if employer replied or email bounced."""
    user = _create_user(f"replied_{uuid.uuid4().hex[:6]}@example.com")
    with get_session() as s:
        app_replied = Application(
            user_id=user.id,
            email="recruiter@tech.com",
            job_title="ML Engineer",
            company="Tech Corp",
            status="replied",
            replied_at=dt.datetime.now(dt.timezone.utc),
            followup_count=0,
        )
        app_bounced = Application(
            user_id=user.id,
            email="invalid@tech.com",
            job_title="ML Engineer",
            company="Tech Corp",
            status="bounced",
            bounced_at=dt.datetime.now(dt.timezone.utc),
            followup_count=0,
        )
        s.add_all([app_replied, app_bounced])
        s.commit()
        replied_id = app_replied.id
        bounced_id = app_bounced.id

    with get_session() as s:
        res_rep = dispatch_followup_for_application(replied_id, user.id, s)
        assert res_rep["success"] is False
        assert "replied" in res_rep["error"].lower()

        res_bnc = dispatch_followup_for_application(bounced_id, user.id, s)
        assert res_bnc["success"] is False
        assert "bounced" in res_bnc["error"].lower() or "failed" in res_bnc["error"].lower()


# =============================================================================
# 5. GMAIL THREAD BATCH SYNCHRONIZATION
# =============================================================================
@patch("app.gmail.httpx.Client")
def test_gmail_thread_batch_sync(mock_client_cls):
    """Test sync_user_gmail_threads scans threads and records replies/bounces."""
    mock_client = MagicMock()
    mock_client_cls.return_value.__enter__.return_value = mock_client

    user = _create_user(f"sync_batch_{uuid.uuid4().hex[:6]}@example.com")
    with get_session() as s:
        u = s.get(User, user.id)
        u.gmail_connected = True
        u.gmail_email = "candidate@gmail.com"
        u.gmail_access_token_encrypted = encrypt_credential("ya29.mock")
        u.gmail_refresh_token_encrypted = encrypt_credential("1//mock")
        u.gmail_token_expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)

        app_sent = Application(
            user_id=u.id,
            email="hiring@scale.com",
            job_title="Lead Engineer",
            company="Scale Inc",
            status="sent",
            gmail_message_id="sent_msg_99",
            gmail_thread_id="thread_batch_99",
            followup_due_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=3),
        )
        s.add(app_sent)
        s.commit()
        app_id = app_sent.id

    # Mock response with recruiter reply
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "messages": [
            {"id": "sent_msg_99", "snippet": "Original application"},
            {
                "id": "reply_msg_100",
                "snippet": "We received your application and would like to talk next week.",
                "payload": {
                    "headers": [
                        {"name": "From", "value": "recruiter@scale.com"},
                        {"name": "Subject", "value": "Re: Application for Lead Engineer"},
                    ]
                },
            },
        ]
    }
    mock_client.get.return_value = mock_resp

    with get_session() as s:
        u = s.get(User, user.id)
        sync_res = gmail.sync_user_gmail_threads(u, s)
        assert sync_res["synced"] >= 1
        assert sync_res["replies"] == 1

        updated_app = s.get(Application, app_id)
        assert updated_app.status == "replied"
        assert updated_app.replied_at is not None
        assert updated_app.followup_due_at is None  # Followup disabled on reply
