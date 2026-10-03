"""Comprehensive verification of Supabase Cloud synchronization fidelity for Gmail and Telemetry data."""

import datetime as dt
import json
import pytest
from unittest.mock import MagicMock, patch

from app.auth import encrypt_credential, decrypt_credential
from app.db import get_session, init_db, sync_from_supabase_to_memory
from app.main import sync_app_to_supabase, sync_user_to_supabase
from app.models import Application, EmailActivity, User
from radar.supabase_client import SupabaseClient


def test_supabase_client_email_activity_methods():
    """Verify SupabaseClient handles email activity methods gracefully."""
    client = SupabaseClient(base_url="https://fake.supabase.co", service_key="fake-key")
    assert client.is_configured is True

    # Test insert_email_activity with mock
    with patch("httpx.Client.post") as mock_post:
        mock_post.return_value = MagicMock(
            status_code=201,
            json=lambda: [{"id": 42, "event_type": "sent", "recipient": "recruiter@techcorp.com"}],
        )
        res = client.insert_email_activity({
            "user_id": 1,
            "application_id": 10,
            "event_type": "sent",
            "recipient": "recruiter@techcorp.com",
            "subject": "Application for Staff Engineer",
            "details_json": json.dumps({"provider": "gmail_oauth"}),
        })
        assert res is not None
        assert res["id"] == 42
        assert res["event_type"] == "sent"

    # Test fetch_all_email_activities with mock
    with patch("httpx.Client.get") as mock_get:
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: [
                {
                    "id": 42,
                    "user_id": 1,
                    "application_id": 10,
                    "event_type": "sent",
                    "recipient": "recruiter@techcorp.com",
                    "subject": "Application for Staff Engineer",
                    "details_json": "{}",
                    "created_at": "2026-10-03T12:00:00+00:00",
                }
            ],
        )
        activities = client.fetch_all_email_activities(user_id=1)
        assert len(activities) == 1
        assert activities[0]["recipient"] == "recruiter@techcorp.com"


def test_supabase_sync_app_and_user_helpers():
    """Verify that sync_app_to_supabase and sync_user_to_supabase pass all new Gmail/telemetry columns."""
    init_db()
    
    mock_sb = MagicMock()
    mock_sb.is_configured = True
    mock_sb.upsert_application.return_value = {"id": 101}
    mock_sb.upsert_user.return_value = {"id": 202}

    with patch("radar.db.get_supabase_client", return_value=mock_sb), \
         patch("app.main.get_supabase_client", return_value=mock_sb), \
         patch("app.config.TEST_MODE", False):

        # 1. Test user sync with Gmail credentials
        test_user = User(
            id=202,
            email="developer_sync@realcompany.com",
            name="Developer Sync",
            gmail_connected=True,
            gmail_email="developer_sync@gmail.com",
            gmail_access_token_encrypted=encrypt_credential("fake_access_token"),
            gmail_refresh_token_encrypted=encrypt_credential("fake_refresh_token"),
            gmail_token_expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1),
            gmail_token_scopes="openid email https://www.googleapis.com/auth/gmail.send",
            gmail_connected_at=dt.datetime.now(dt.timezone.utc),
            selected_template_id="oxford_editorial",
        )
        sync_user_to_supabase(test_user)
        assert mock_sb.upsert_user.called
        user_call_dict = mock_sb.upsert_user.call_args[0][0]
        assert user_call_dict["gmail_connected"] is True
        assert user_call_dict["gmail_email"] == "developer_sync@gmail.com"
        assert user_call_dict["selected_template_id"] == "oxford_editorial"
        assert user_call_dict["gmail_token_scopes"] != ""

        # 2. Test application sync with Telemetry & Gmail Thread fields
        now = dt.datetime.now(dt.timezone.utc)
        test_app = Application(
            id=101,
            user_id=202,
            email="recruiter@ai.corp",
            job_title="Lead AI Engineer",
            company="AI Corp",
            status="sent",
            gmail_message_id="msg_18fa392810a",
            gmail_thread_id="th_18fa392810a",
            tracking_token="trk_unique_sync_token_123",
            opened_at=now,
            open_count=3,
            bounced_at=None,
            bounce_reason="",
            replied_at=now,
            reply_snippet="Thanks for applying! We'd love to schedule an interview.",
            followup_due_at=now + dt.timedelta(days=3),
            followup_sent_at=None,
        )
        sync_app_to_supabase(test_app)
        assert mock_sb.upsert_application.called
        app_call_dict = mock_sb.upsert_application.call_args[0][0]
        assert app_call_dict["gmail_message_id"] == "msg_18fa392810a"
        assert app_call_dict["gmail_thread_id"] == "th_18fa392810a"
        assert app_call_dict["tracking_token"] == "trk_unique_sync_token_123"
        assert app_call_dict["open_count"] == 3
        assert app_call_dict["reply_snippet"].startswith("Thanks for applying")


def test_supabase_cloud_to_memory_hydration():
    """Verify sync_from_supabase_to_memory properly maps cloud records into SQLAlchemy models."""
    init_db()

    mock_sb_instance = MagicMock()
    mock_sb_instance.is_configured = True
    mock_sb_instance.fetch_all_users.return_value = [
        {
            "id": 8803,
            "email": "cloud_hydrated_test@sample.org",
            "name": "Cloud User",
            "password_hash": "hash123",
            "is_active": True,
            "gmail_connected": True,
            "gmail_email": "cloud_hydrated_test@gmail.com",
            "gmail_access_token_encrypted": encrypt_credential("hydrated_token"),
            "gmail_refresh_token_encrypted": encrypt_credential("hydrated_refresh"),
            "gmail_token_expires_at": "2026-10-03T15:00:00+00:00",
            "gmail_token_scopes": "openid https://www.googleapis.com/auth/gmail.send",
            "gmail_connected_at": "2026-10-03T12:00:00+00:00",
            "selected_template_id": "monarch_clean",
        }
    ]
    mock_sb_instance.fetch_all_jobs.return_value = []
    mock_sb_instance.fetch_all_applications.return_value = [
        {
            "id": 8804,
            "user_id": 8803,
            "email": "hiring@innovate.io",
            "job_title": "Founding Engineer",
            "company": "Innovate IO",
            "status": "sent",
            "gmail_message_id": "msg_cloud_8804",
            "gmail_thread_id": "th_cloud_8804",
            "tracking_token": "trk_cloud_8804",
            "opened_at": "2026-10-03T13:00:00+00:00",
            "open_count": 2,
            "bounced_at": None,
            "bounce_reason": "",
            "replied_at": "2026-10-03T14:30:00+00:00",
            "reply_snippet": "Let's speak tomorrow at 2pm.",
            "followup_due_at": "2026-10-06T12:00:00+00:00",
            "followup_sent_at": None,
        }
    ]
    mock_sb_instance.fetch_all_resume_versions.return_value = []
    mock_sb_instance.fetch_all_email_activities.return_value = [
        {
            "id": 8805,
            "user_id": 8803,
            "application_id": 8804,
            "event_type": "opened",
            "recipient": "hiring@innovate.io",
            "subject": "Application for Founding Engineer",
            "details_json": '{"open_count": 2}',
            "created_at": "2026-10-03T13:00:00+00:00",
        }
    ]

    with patch("radar.supabase_client.SupabaseClient", return_value=mock_sb_instance):
        with get_session() as s:
            sync_from_supabase_to_memory(s)

        with get_session() as s:
            hydrated_user = s.get(User, 8803)
            assert hydrated_user is not None
            assert hydrated_user.gmail_connected is True
            assert hydrated_user.gmail_email == "cloud_hydrated_test@gmail.com"
            assert decrypt_credential(hydrated_user.gmail_access_token_encrypted) == "hydrated_token"
            assert hydrated_user.selected_template_id == "monarch_clean"

            hydrated_app = s.get(Application, 8804)
            assert hydrated_app is not None
            assert hydrated_app.gmail_message_id == "msg_cloud_8804"
            assert hydrated_app.open_count == 2
            assert hydrated_app.reply_snippet == "Let's speak tomorrow at 2pm."

            hydrated_activity = s.get(EmailActivity, 8805)
            assert hydrated_activity is not None
            assert hydrated_activity.event_type == "opened"
            assert hydrated_activity.recipient == "hiring@innovate.io"
