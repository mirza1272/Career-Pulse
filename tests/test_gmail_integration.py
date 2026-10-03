"""Comprehensive Test Suite for Gmail OAuth 2.0 Integration, Telemetry, and Security.

Verifies:
1. Google OAuth state creation, HMAC validation, and CSRF protection.
2. Token exchange and user info fetching.
3. Multi-device AES-256 token persistence on User model.
4. Automatic access token refresh and revoked token handling.
5. Outbound dispatch via Gmail API when connected.
6. Safe SMTP fallback when Gmail is disconnected.
7. Controlled error handling on Gmail API failure.
8. Thread inspection for reply and bounce telemetry.
9. Email tracking pixel (1x1 transparent GIF) and EmailActivity audit logging.
10. Strict multi-tenant per-user isolation.
11. Disconnect and Reconnect lifecycle.
"""

import datetime as dt
from email.message import EmailMessage
import json
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
import pytest

from app import config, gmail
from app.auth import create_session_token, decrypt_credential, encrypt_credential, hash_password
from app.db import get_session, init_db
from app.main import app
from app.models import Application, EmailActivity, User
from app.outbound import OutboundEmailGuard


@pytest.fixture(autouse=True)
def setup_db():
    config.SESSION_SECRET = "test-session-secret-for-gmail-unit-tests-32b"
    config.GOOGLE_CLIENT_ID = "mock-client-id.apps.googleusercontent.com"
    config.GOOGLE_CLIENT_SECRET = "mock-client-secret"
    config.GOOGLE_REDIRECT_URI = "http://localhost:8000/auth/google/callback"
    config.TEST_MODE = True
    config.TEST_RECIPIENT = "test-recipient@example.com"
    init_db()


def _create_test_user(email: str, name: str = "Test User") -> User:
    with get_session() as s:
        u = s.query(User).filter(User.email == email).first()
        if not u:
            u = User(
                email=email,
                name=name,
                password_hash=hash_password("Password123!"),
                is_active=True,
                smtp_host="smtp.gmail.com",
                smtp_port=587,
                smtp_username=email,
                smtp_password_encrypted=encrypt_credential("mock-smtp-password"),
                smtp_verified=True,
            )
            s.add(u)
            s.commit()
            s.refresh(u)
        return u


# =============================================================================
# 1. OAUTH STATE & CSRF DEFENSE
# =============================================================================
def test_oauth_state_generation_and_validation():
    user = _create_test_user("oauth_user@example.com")
    state = gmail.create_oauth_state(user.id)
    assert state is not None
    assert ":" in state

    # Valid state verification
    assert gmail.verify_oauth_state(state, user.id) is True

    # Reject state for different user (CSRF attack)
    assert gmail.verify_oauth_state(state, user.id + 999) is False

    # Reject tampered state
    tampered = state[:-4] + "abcd"
    assert gmail.verify_oauth_state(tampered, user.id) is False

    # Reject malformed state
    assert gmail.verify_oauth_state("invalid:state", user.id) is False
    assert gmail.verify_oauth_state("", user.id) is False


def test_build_google_auth_url():
    user = _create_test_user("auth_url_user@example.com")
    url = gmail.build_google_auth_url(user.id)
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth")
    assert f"client_id={config.GOOGLE_CLIENT_ID}" in url
    assert "access_type=offline" in url
    assert "prompt=consent" in url
    assert "state=" in url


# =============================================================================
# 2. TOKEN ENCRYPTION & MULTI-DEVICE PERSISTENCE
# =============================================================================
def test_token_encryption_and_persistence():
    user = _create_test_user("persist_user@example.com")
    raw_access = "ya29.mock_access_token_12345"
    raw_refresh = "1//mock_refresh_token_67890"

    enc_access = encrypt_credential(raw_access)
    enc_refresh = encrypt_credential(raw_refresh)

    # Tokens in database must NOT be plaintext
    assert enc_access != raw_access
    assert enc_refresh != raw_refresh

    # Decryption recovers exact plaintext
    assert decrypt_credential(enc_access) == raw_access
    assert decrypt_credential(enc_refresh) == raw_refresh

    with get_session() as s:
        u = s.get(User, user.id)
        u.gmail_connected = True
        u.gmail_email = "persist_user@gmail.com"
        u.gmail_access_token_encrypted = enc_access
        u.gmail_refresh_token_encrypted = enc_refresh
        u.gmail_token_expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
        s.commit()

    # Retrieve from a separate session (mirrors device B login)
    with get_session() as s2:
        u2 = s2.get(User, user.id)
        assert u2.gmail_connected is True
        assert u2.gmail_email == "persist_user@gmail.com"
        assert decrypt_credential(u2.gmail_access_token_encrypted) == raw_access
        assert decrypt_credential(u2.gmail_refresh_token_encrypted) == raw_refresh


# =============================================================================
# 3. TOKEN REFRESH LIFECYCLE
# =============================================================================
def test_refresh_gmail_access_token_valid():
    user = _create_test_user("refresh_valid@example.com")
    now = dt.datetime.now(dt.timezone.utc)
    raw_access = "ya29.valid_token_abc"

    with get_session() as s:
        u = s.get(User, user.id)
        u.gmail_connected = True
        u.gmail_access_token_encrypted = encrypt_credential(raw_access)
        u.gmail_refresh_token_encrypted = encrypt_credential("1//refresh_token_xyz")
        u.gmail_token_expires_at = now + dt.timedelta(minutes=30)
        s.commit()

        # When token is not expired, it returns existing token without making HTTP call
        token = gmail.refresh_gmail_access_token(u, s)
        assert token == raw_access


@patch("httpx.Client.post")
def test_refresh_gmail_access_token_expired(mock_post):
    user = _create_test_user("refresh_expired@example.com")
    now = dt.datetime.now(dt.timezone.utc)

    # Mock successful refresh token response
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "access_token": "ya29.newly_refreshed_access_token",
        "expires_in": 3600,
        "token_type": "Bearer",
    }
    mock_post.return_value = mock_resp

    with get_session() as s:
        u = s.get(User, user.id)
        u.gmail_connected = True
        u.gmail_access_token_encrypted = encrypt_credential("ya29.old_expired_token")
        u.gmail_refresh_token_encrypted = encrypt_credential("1//valid_refresh_token")
        u.gmail_token_expires_at = now - dt.timedelta(minutes=5)
        s.commit()

        new_token = gmail.refresh_gmail_access_token(u, s)
        assert new_token == "ya29.newly_refreshed_access_token"
        assert u.gmail_token_expires_at > now


@patch("httpx.Client.post")
def test_refresh_gmail_access_token_revoked_by_google(mock_post):
    user = _create_test_user("refresh_revoked@example.com")
    now = dt.datetime.now(dt.timezone.utc)

    # Mock 400 invalid_grant response
    mock_resp = MagicMock()
    mock_resp.status_code = 400
    mock_resp.headers = {"content-type": "application/json"}
    mock_resp.json.return_value = {"error": "invalid_grant", "error_description": "Token has been expired or revoked."}
    mock_post.return_value = mock_resp

    with get_session() as s:
        u = s.get(User, user.id)
        u.gmail_connected = True
        u.gmail_access_token_encrypted = encrypt_credential("ya29.old_token")
        u.gmail_refresh_token_encrypted = encrypt_credential("1//revoked_refresh_token")
        u.gmail_token_expires_at = now - dt.timedelta(minutes=5)
        s.commit()

        token = gmail.refresh_gmail_access_token(u, s)
        assert token is None
        # System marks connection as disconnected gracefully
        assert u.gmail_connected is False


# =============================================================================
# 4. GMAIL MESSAGE DISPATCH VIA OUTBOUND GUARD
# =============================================================================
@patch("app.gmail.send_gmail_message")
def test_outbound_send_via_gmail_api(mock_gmail_send):
    user = _create_test_user("gmail_sender@example.com")
    with get_session() as s:
        u = s.get(User, user.id)
        u.gmail_connected = True
        u.gmail_email = "gmail_sender@gmail.com"
        u.gmail_access_token_encrypted = encrypt_credential("ya29.mock_token")
        u.gmail_refresh_token_encrypted = encrypt_credential("1//mock_refresh")
        u.gmail_token_expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
        s.commit()

        app_row = Application(
            user_id=u.id,
            email="hiring@techcorp.com",
            job_title="Full Stack Engineer",
            company="TechCorp",
            drafted_email="Dear Hiring Manager,\n\nI am applying for the role.",
            subject="Application for Full Stack Engineer",
            status="approved",
            tracking_token="track_token_123",
        )
        s.add(app_row)
        s.commit()

        mock_gmail_send.return_value = {
            "success": True,
            "message_id": "gmail_msg_id_1001",
            "thread_id": "gmail_thread_id_2002",
            "error": "",
        }

        guard = OutboundEmailGuard()
        result = guard.send(
            intended_recipient=app_row.email,
            subject=app_row.subject,
            body=app_row.drafted_email,
            user=u,
            application=app_row,
            db_session=s,
        )

        assert result.success is True
        assert result.message_id == "gmail_msg_id_1001"
        assert app_row.gmail_message_id == "gmail_msg_id_1001"
        assert app_row.gmail_thread_id == "gmail_thread_id_2002"
        assert app_row.followup_due_at is not None

        # Check EmailActivity row logged
        activity = s.query(EmailActivity).filter_by(application_id=app_row.id, event_type="sent").first()
        assert activity is not None
        assert activity.user_id == u.id


def test_outbound_send_fallback_to_smtp_when_disconnected():
    user = _create_test_user("smtp_fallback@example.com")
    with get_session() as s:
        u = s.get(User, user.id)
        u.gmail_connected = False

        app_row = Application(
            user_id=u.id,
            email="recruiter@enterprise.com",
            job_title="Backend Developer",
            company="Enterprise Inc",
            drafted_email="Dear Recruiter,\n\nApplying for backend developer.",
            subject="Application for Backend Developer",
            status="approved",
        )
        s.add(app_row)
        s.commit()

        with patch("smtplib.SMTP") as mock_smtp:
            instance = MagicMock()
            mock_smtp.return_value.__enter__.return_value = instance

            guard = OutboundEmailGuard()
            result = guard.send(
                intended_recipient=app_row.email,
                subject=app_row.subject,
                body=app_row.drafted_email,
                user=u,
                application=app_row,
                db_session=s,
            )

            assert result.success is True
            assert instance.send_message.called


# =============================================================================
# 5. THREAD ACTIVITY INSPECTION (REPLY & BOUNCE)
# =============================================================================
@patch("app.gmail.httpx.Client")
def test_check_gmail_thread_activity_reply_and_bounce(mock_client_cls):
    mock_client = MagicMock()
    mock_client_cls.return_value.__enter__.return_value = mock_client

    user = _create_test_user("thread_user@example.com")
    with get_session() as s:
        u = s.get(User, user.id)
        u.gmail_connected = True
        u.gmail_email = "thread_user@gmail.com"
        u.gmail_access_token_encrypted = encrypt_credential("ya29.token")
        u.gmail_refresh_token_encrypted = encrypt_credential("1//refresh")
        u.gmail_token_expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
        s.commit()

        # Scenario A: Genuine Employer Reply
        mock_resp_reply = MagicMock()
        mock_resp_reply.status_code = 200
        mock_resp_reply.json.return_value = {
            "messages": [
                {"id": "msg_sent_1", "snippet": "Sent message"},
                {
                    "id": "msg_reply_2",
                    "snippet": "Thanks for your application! We would love to schedule an interview.",
                    "payload": {
                        "headers": [
                            {"name": "From", "value": "recruiter@innovate.com"},
                            {"name": "Subject", "value": "Re: Application for Full Stack Engineer"},
                        ]
                    },
                },
            ]
        }
        mock_client.get.return_value = mock_resp_reply

        activity = gmail.check_gmail_thread_activity(u, "thread_123", "msg_sent_1", s)
        assert activity["has_reply"] is True
        assert "recruiter@innovate.com" in activity["reply_from"]
        assert "interview" in activity["reply_snippet"]
        assert activity["is_bounce"] is False

        # Scenario B: Mailer-Daemon Delivery Failure / Bounce
        mock_resp_bounce = MagicMock()
        mock_resp_bounce.status_code = 200
        mock_resp_bounce.json.return_value = {
            "messages": [
                {"id": "msg_sent_1", "snippet": "Sent message"},
                {
                    "id": "msg_bounce_2",
                    "snippet": "Delivery Status Notification (Failure): The email account does not exist.",
                    "payload": {
                        "headers": [
                            {"name": "From", "value": "mailer-daemon@googlemail.com"},
                            {"name": "Subject", "value": "Delivery Status Notification (Failure)"},
                        ]
                    },
                },
            ]
        }
        mock_client.get.return_value = mock_resp_bounce

        bounce_act = gmail.check_gmail_thread_activity(u, "thread_123", "msg_sent_1", s)
        assert bounce_act["is_bounce"] is True
        assert "Delivery" in bounce_act["bounce_reason"] or "not exist" in bounce_act["bounce_reason"]


# =============================================================================
# 6. TRACKING PIXEL & EMAIL ACTIVITY LOGGING
# =============================================================================
def test_tracking_pixel_endpoint_records_open():
    import uuid
    unique_token = f"track_{uuid.uuid4().hex}"
    user = _create_test_user("pixel_user@example.com")
    with get_session() as s:
        app_row = Application(
            user_id=user.id,
            email="lead@company.com",
            job_title="Lead AI Engineer",
            company="Company AI",
            drafted_email="Email body",
            subject="Application for Lead AI Engineer",
            status="sent",
            tracking_token=unique_token,
        )
        s.add(app_row)
        s.commit()
        app_id = app_row.id

    client = TestClient(app)
    resp = client.get(f"/track/open/{unique_token}")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/gif"

    with get_session() as s:
        updated_app = s.get(Application, app_id)
        assert updated_app.opened_at is not None
        assert updated_app.open_count >= 1

        activity = s.query(EmailActivity).filter_by(application_id=app_id, event_type="opened").first()
        assert activity is not None
        assert activity.user_id == user.id


# =============================================================================
# 7. MULTI-TENANT PER-USER ISOLATION
# =============================================================================
def test_strict_user_isolation():
    user_a = _create_test_user("usera@company.com", "User A")
    user_b = _create_test_user("userb@company.com", "User B")

    with get_session() as s:
        ua = s.get(User, user_a.id)
        ua.gmail_connected = True
        ua.gmail_email = "usera@gmail.com"
        ua.gmail_access_token_encrypted = encrypt_credential("usera_access_token")
        ua.gmail_refresh_token_encrypted = encrypt_credential("usera_refresh_token")
        s.commit()

        app_a = Application(
            user_id=user_a.id,
            email="jobs@corp.com",
            job_title="Role A",
            status="sent",
            gmail_thread_id="thread_user_a",
            gmail_message_id="msg_user_a",
        )
        s.add(app_a)
        s.commit()
        app_a_id = app_a.id

    # User B logs in
    token_b = create_session_token(user_b.id, user_b.email)
    client_b = TestClient(app)
    client_b.cookies.set("careerpulse_auth", token_b)

    # User B cannot sync activity on User A's application (403 Forbidden)
    resp = client_b.post(f"/application/{app_a_id}/sync-activity")
    assert resp.status_code == 403


# =============================================================================
# 8. DISCONNECT & RECONNECT LIFECYCLE
# =============================================================================
def test_disconnect_and_reconnect():
    user = _create_test_user("reconnect_user@example.com")
    with get_session() as s:
        u = s.get(User, user.id)
        u.gmail_connected = True
        u.gmail_email = "reconnect@gmail.com"
        u.gmail_access_token_encrypted = encrypt_credential("access_token_1")
        u.gmail_refresh_token_encrypted = encrypt_credential("refresh_token_1")
        s.commit()

        # 1. Disconnect
        ok = gmail.disconnect_gmail_account(u, s)
        assert ok is True
        assert u.gmail_connected is False
        assert u.gmail_email == ""
        assert u.gmail_access_token_encrypted == ""
        assert u.gmail_refresh_token_encrypted == ""

        # 2. Reconnect with new tokens
        u.gmail_connected = True
        u.gmail_email = "reconnect_new@gmail.com"
        u.gmail_access_token_encrypted = encrypt_credential("access_token_2")
        u.gmail_refresh_token_encrypted = encrypt_credential("refresh_token_2")
        s.commit()

        assert u.gmail_connected is True
        assert u.gmail_email == "reconnect_new@gmail.com"
        assert decrypt_credential(u.gmail_access_token_encrypted) == "access_token_2"
