"""Tests for Radar Provider Selection (Tavily, Apify MCP) and Optional Credentials Management."""

import datetime as dt
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import pytest
from starlette.testclient import TestClient

from app.auth import create_session_token, decrypt_credential, encrypt_credential, hash_password
from app.db import get_session
from app.main import app
from app.models import User
from radar.apify_mcp import ApifySearchProvider, test_apify_connection as check_apify_conn
from radar.credentials import (
    check_limited_search_cooldown,
    get_masked_provider_credentials,
    get_user_custom_keys,
    get_user_provider_credentials,
    record_limited_search,
    save_user_provider_credentials,
)
from radar.extractor import ExtractedJob
from radar.pipeline import IngestionStats
from radar.search import TavilySearchProvider, execute_search, test_tavily_connection as check_tavily_conn


def test_provider_credentials_storage_encryption_and_masking():
    """Verify AES-256 GCM encryption, retrieval, and UI masking for provider API keys."""
    with get_session() as s:
        user = s.query(User).filter_by(email="cred_test_user@example.com").first()
        if not user:
            user = User(
                email="cred_test_user@example.com",
                password_hash=hash_password("testpass123"),
                name="Credential Tester",
                is_active=True,
                smtp_verified=True,
            )
            s.add(user)
            s.commit()
            s.refresh(user)
        user_id = user.id

    # 1. Save provider keys
    test_keys = {
        "apify_api_key": "apify_api_SECRET_KEY_987654321",
        "tavily_api_key": "tvly-SECRET_TAVILY_KEY_123456",
        "firecrawl_api_key": "fc-SECRET_FIRECRAWL_KEY_ABC",
        "serpapi_api_key": "SECRET_SERPAPI_KEY_XYZ",
    }
    ok, msg = save_user_provider_credentials(user_id, test_keys)
    assert ok

    # 2. Retrieve decrypted keys
    with get_session() as s:
        u = s.get(User, user_id)
        assert u is not None
        creds = get_user_provider_credentials(u)
        assert creds["apify_api_key"] == "apify_api_SECRET_KEY_987654321"
        assert creds["tavily_api_key"] == "tvly-SECRET_TAVILY_KEY_123456"
        assert creds["firecrawl_api_key"] == "fc-SECRET_FIRECRAWL_KEY_ABC"
        assert creds["serpapi_api_key"] == "SECRET_SERPAPI_KEY_XYZ"

        # Verify ciphertext in knowledge_base_json is not plain text
        kb = json.loads(u.knowledge_base_json)
        enc_apify = kb["provider_credentials"]["apify_api_key_encrypted"]
        assert "SECRET_KEY" not in enc_apify

        # 3. Verify masking for UI (never exposing plain keys)
        masked = get_masked_provider_credentials(u)
        assert masked["apify"]["configured"] is True
        assert "••••••••" in masked["apify"]["masked_key"]
        assert "SECRET" not in masked["apify"]["masked_key"]
        assert masked["tavily"]["configured"] is True
        assert "••••••••" in masked["tavily"]["masked_key"]


def test_limited_search_mode_and_2hour_cooldown_enforcement():
    """Verify that without user credentials, searches are limited to max 3 jobs and enforce 2-hour cooldown."""
    with get_session() as s:
        user = s.query(User).filter_by(email="cooldown_user@example.com").first()
        if not user:
            user = User(
                email="cooldown_user@example.com",
                password_hash=hash_password("testpass123"),
                name="Cooldown Tester",
                is_active=True,
                smtp_verified=True,
                knowledge_base_json="{}",
            )
            s.add(user)
            s.commit()
            s.refresh(user)
        else:
            user.knowledge_base_json = "{}"
            s.commit()
        user_id = user.id

    # 1. First search without user credentials -> Allowed in limited_free mode
    with get_session() as s:
        u = s.get(User, user_id)
        can_search, rem, mode = check_limited_search_cooldown(u, provider="tavily")
        assert can_search is True
        assert rem == 0
        assert mode == "limited_free"

        # Record search timestamp
        record_limited_search(u, provider="tavily")

    # 2. Immediate second search without credentials -> Blocked by 2-hour cooldown
    with get_session() as s:
        u = s.get(User, user_id)
        can_search2, rem2, mode2 = check_limited_search_cooldown(u, provider="tavily")
        assert can_search2 is False
        assert rem2 > 7000  # ~7200 seconds remaining
        assert mode2 == "limited_free"

    # 3. Add user custom Tavily key -> Cooldown bypassed immediately
    save_user_provider_credentials(user_id, {"tavily_api_key": "tvly-CUSTOM_KEY_123"})
    with get_session() as s:
        u = s.get(User, user_id)
        can_search3, rem3, mode3 = check_limited_search_cooldown(u, provider="tavily")
        assert can_search3 is True
        assert mode3 == "user_credential"


def test_apify_mcp_normalization_and_provider_routing():
    """Verify that ApifySearchProvider correctly normalizes items into Radar ExtractedJob schema."""
    provider = ApifySearchProvider(api_key="mock_test_key")

    mock_raw_item = {
        "title": "Associate AI Engineer - Scale AI",
        "url": "https://jobs.lever.co/scale/sample-job-123",
        "description": "We are hiring an Associate AI Engineer to build agentic workflows and fine-tune models. Apply at jobs@scale.com",
        "location": "Lahore, Pakistan (Remote)",
        "company": "Scale AI",
        "postedAt": "1 day ago",
    }

    job = provider._normalize_apify_item(mock_raw_item, default_location="Pakistan")
    assert job is not None
    assert isinstance(job, ExtractedJob)
    assert "AI Engineer" in job.title
    assert "Scale AI" in job.company
    assert job.link == "https://jobs.lever.co/scale/sample-job-123"
    assert job.source == "apify_mcp"
    assert job.has_email is True
    assert job.email == "jobs@scale.com"


def test_provider_connection_checks():
    """Verify provider token connection check functions handle empty keys safely."""
    ok1, msg1 = check_apify_conn("")
    assert not ok1
    assert "empty" in msg1.lower() or "required" in msg1.lower()

    ok2, msg2 = check_tavily_conn("")
    assert not ok2
    assert "empty" in msg2.lower() or "required" in msg2.lower()


def test_credentials_api_endpoints_and_provider_testing():
    """Verify HTTP endpoints for saving and testing provider credentials."""
    client = TestClient(app)

    with get_session() as s:
        user = s.query(User).filter_by(email="api_endpoint_user@example.com").first()
        if not user:
            user = User(
                email="api_endpoint_user@example.com",
                password_hash=hash_password("testpass123"),
                name="API Tester",
                is_active=True,
                smtp_verified=True,
                smtp_username="api_endpoint_user@example.com",
                smtp_password_encrypted=encrypt_credential("pwd123"),
            )
            s.add(user)
            s.commit()
            s.refresh(user)
        user_id = user.id

    token = create_session_token(user_id, "api_endpoint_user@example.com")
    client.cookies.set("careerpulse_auth", token)

    # 1. Test POST /credentials/providers
    res = client.post(
        "/credentials/providers",
        data={
            "apify_api_key": "apify_api_NEW_TEST_KEY",
            "tavily_api_key": "tvly-NEW_TEST_KEY",
        },
        headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
    )
    assert res.status_code == 200
    assert res.json().get("success") is True

    # 2. Test POST /credentials/test-provider with empty key
    res_test_empty = client.post(
        "/credentials/test-provider",
        json={"provider": "apify", "api_key": ""},
    )
    assert res_test_empty.status_code == 200

    # 3. Test GET /credentials renders provider cards
    res_page = client.get("/credentials")
    assert res_page.status_code == 200
    assert "Job Search Provider Credentials" in res_page.text
    assert "Apify MCP" in res_page.text or "Apify API Key" in res_page.text
    assert "Tavily" in res_page.text

    # 4. Test Unified POST /credentials saving both SMTP and provider keys simultaneously
    from unittest.mock import patch
    with patch("smtplib.SMTP") as mock_smtp:
        mock_instance = mock_smtp.return_value.__enter__.return_value
        mock_instance.login.return_value = True
        res_unified = client.post(
            "/credentials",
            data={
                "sender_name": "Unified Tester",
                "smtp_username": "api_endpoint_user@example.com",
                "smtp_password": "testapppassword12",
                "smtp_host": "smtp.gmail.com",
                "smtp_port": 587,
                "apify_api_key": "apify_api_UNIFIED_KEY",
                "tavily_api_key": "tvly-UNIFIED_KEY",
                "firecrawl_api_key": "fc-UNIFIED_KEY",
            },
            headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
        )
        assert res_unified.status_code == 200
        assert res_unified.json().get("success") is True

    # Verify provider keys were saved into user record
    with get_session() as s:
        u = s.get(User, user_id)
        from radar.credentials import get_user_custom_keys
        custom_keys = get_user_custom_keys(u)
        assert custom_keys["apify_api_key"] == "apify_api_UNIFIED_KEY"
        assert custom_keys["tavily_api_key"] == "tvly-UNIFIED_KEY"
        assert custom_keys["firecrawl_api_key"] == "fc-UNIFIED_KEY"


def test_radar_scan_provider_dropdown_flow():
    """Verify that /radar/scan receives provider selection and executes properly."""
    client = TestClient(app)

    with get_session() as s:
        user = s.query(User).filter_by(email="radar_flow_user@example.com").first()
        if not user:
            user = User(
                email="radar_flow_user@example.com",
                password_hash=hash_password("testpass123"),
                name="Radar Flow Tester",
                is_active=True,
                smtp_verified=True,
                smtp_username="radar_flow_user@example.com",
                smtp_password_encrypted=encrypt_credential("pwd123"),
                knowledge_base_json=json.dumps({"provider_credentials": {"tavily_api_key_encrypted": encrypt_credential("tvly-test")}}),
            )
            s.add(user)
            s.commit()
            s.refresh(user)
        user_id = user.id

    token = create_session_token(user_id, "radar_flow_user@example.com")
    client.cookies.set("careerpulse_auth", token)

    # 1. Test GET /radar includes provider dropdown and status metadata
    res_radar = client.get("/radar")
    assert res_radar.status_code == 200
    assert "Job Search Provider" in res_radar.text
    assert "providerSelect" in res_radar.text
    assert "Apify" in res_radar.text
    assert "Firecrawl" in res_radar.text
    assert "Tavily" in res_radar.text

    # 2. Test POST /radar/scan with provider=apify_mcp
    mock_res = IngestionStats(
        total_found=1,
        valid_count=1,
        duplicates_skipped=0,
        stored_jobs=1,
        pushed_to_career_pulse=1,
        errors=[],
        message="Scan completed successfully",
    )

    with patch("app.main.run_pipeline", return_value=mock_res) as mock_pipe:
        res_scan = client.post(
            "/radar/scan",
            data={
                "provider": "apify_mcp",
                "role": "Junior Full-Stack Developer",
                "location": "pakistan_lahore",
                "platform": "all",
                "limit": "3",
            },
            follow_redirects=False,
        )
        assert res_scan.status_code == 303
        loc = res_scan.headers.get("location", "")
        assert "scanned=1" in loc
        # Check that run_pipeline received provider="apify_mcp"
        assert mock_pipe.call_count == 1
        _, kwargs = mock_pipe.call_args
        assert kwargs.get("provider") == "apify_mcp"


def test_apify_3_key_round_robin_rotation(monkeypatch):
    """Verify that optional users cycle through 3 system Apify API keys in round-robin sequence."""
    from radar.credentials import (
        get_next_system_apify_key,
        get_system_apify_keys,
        get_user_provider_credentials,
        reset_apify_key_rotation,
    )

    # Configure 3 distinct Apify keys in environment
    monkeypatch.setenv("APIFY_API_KEY_1", "apify_token_ALPHA_111")
    monkeypatch.setenv("APIFY_API_KEY_2", "apify_token_BETA_222")
    monkeypatch.setenv("APIFY_API_KEY_3", "apify_token_GAMMA_333")
    monkeypatch.delenv("APIFY_API_KEY", raising=False)
    monkeypatch.delenv("APIFY_TOKEN", raising=False)

    reset_apify_key_rotation()

    pool = get_system_apify_keys()
    assert len(pool) == 3
    assert pool == ["apify_token_ALPHA_111", "apify_token_BETA_222", "apify_token_GAMMA_333"]

    # Test round-robin sequential cycling
    assert get_next_system_apify_key() == "apify_token_ALPHA_111"  # 1st call -> Key 1
    assert get_next_system_apify_key() == "apify_token_BETA_222"   # 2nd call -> Key 2
    assert get_next_system_apify_key() == "apify_token_GAMMA_333"  # 3rd call -> Key 3
    assert get_next_system_apify_key() == "apify_token_ALPHA_111"  # 4th call -> Key 1 (cycled)
    assert get_next_system_apify_key() == "apify_token_BETA_222"   # 5th call -> Key 2

    # Test resolution for optional user (no custom key)
    reset_apify_key_rotation()
    mock_optional_user = User(
        email="optional_user@example.com",
        password_hash=hash_password("pwd123"),
        name="Optional User",
        knowledge_base_json="{}",
    )

    creds_call_1 = get_user_provider_credentials(mock_optional_user)
    assert creds_call_1["apify_api_key"] == "apify_token_ALPHA_111"

    creds_call_2 = get_user_provider_credentials(mock_optional_user)
    assert creds_call_2["apify_api_key"] == "apify_token_BETA_222"

    creds_call_3 = get_user_provider_credentials(mock_optional_user)
    assert creds_call_3["apify_api_key"] == "apify_token_GAMMA_333"

    # Test user with custom key (must bypass system round-robin pool)
    mock_custom_user = User(
        email="custom_key_user@example.com",
        password_hash=hash_password("pwd123"),
        name="Custom Key User",
        knowledge_base_json=json.dumps({
            "provider_credentials": {
                "apify_api_key_encrypted": encrypt_credential("my_personal_custom_apify_key")
            }
        }),
    )

    custom_creds = get_user_provider_credentials(mock_custom_user)
    assert custom_creds["apify_api_key"] == "my_personal_custom_apify_key"


def test_job_freshness_rules_enforcement():
    """Verify strict freshness rules:
    1. Due date / deadline in future -> Accepted.
    2. Due date / deadline in past -> Rejected.
    3. No deadline, posting date <= 3 weeks (21 days) -> Accepted.
    4. No deadline, posting date > 3 weeks (e.g. 4 weeks, 2 months) -> Rejected.
    5. Neither deadline nor posting date mentioned -> Rejected.
    """
    from radar.validator import validate_apply_window, validate_job_freshness

    now = dt.datetime(2026, 10, 1, 12, 0, 0, tzinfo=dt.timezone.utc)

    # Case 1: Deadline in the future (Oct 15, 2026) -> ACCEPTED
    job_future_deadline = ExtractedJob(
        title="AI Engineer",
        company="TechCorp",
        location="Remote",
        link="https://jobs.lever.co/techcorp/ai-eng-1",
        jd_text="We are hiring an AI Engineer. Apply before October 15, 2026.",
        email="jobs@techcorp.com",
        has_email=True,
        deadline=dt.datetime(2026, 10, 15, 23, 59, 59, tzinfo=dt.timezone.utc),
        posted_at=None,
    )
    v1 = validate_job_freshness(job_future_deadline, now=now)
    assert v1.valid is True
    assert "deadline is valid" in v1.reason

    # Case 2: Deadline in the past (Sep 15, 2026) -> REJECTED
    job_past_deadline = ExtractedJob(
        title="AI Engineer",
        company="OldCorp",
        location="Remote",
        link="https://jobs.lever.co/oldcorp/ai-eng-2",
        jd_text="Application closed on September 15, 2026.",
        email="jobs@oldcorp.com",
        has_email=True,
        deadline=dt.datetime(2026, 9, 15, 0, 0, 0, tzinfo=dt.timezone.utc),
        posted_at=None,
    )
    v2 = validate_job_freshness(job_past_deadline, now=now)
    assert v2.valid is False
    assert "deadline has passed" in v2.reason

    # Case 3: No deadline, posting date within 3 weeks (posted 5 days ago / published Sep 26) -> ACCEPTED
    job_fresh_posted = ExtractedJob(
        title="Backend Engineer",
        company="FreshCorp",
        location="Remote",
        link="https://jobs.lever.co/freshcorp/backend-1",
        jd_text="Looking for a Python developer. Great benefits.",
        email="apply@freshcorp.com",
        has_email=True,
        deadline=None,
        posted_at="5 days ago",
        published_at=dt.datetime(2026, 9, 26, 10, 0, 0, tzinfo=dt.timezone.utc),
    )
    v3 = validate_job_freshness(job_fresh_posted, now=now)
    assert v3.valid is True
    assert "within 3 weeks" in v3.reason or "verified fresh" in v3.reason

    # Case 4: No deadline, posting date > 3 weeks old (published Aug 20, 2026 / 5 weeks ago) -> REJECTED
    job_stale_posted = ExtractedJob(
        title="Backend Engineer",
        company="StaleCorp",
        location="Remote",
        link="https://jobs.lever.co/stalecorp/backend-2",
        jd_text="Looking for a developer.",
        email="apply@stalecorp.com",
        has_email=True,
        deadline=None,
        posted_at="5 weeks ago",
        published_at=dt.datetime(2026, 8, 20, 10, 0, 0, tzinfo=dt.timezone.utc),
    )
    v4 = validate_job_freshness(job_stale_posted, now=now)
    assert v4.valid is False
    assert "> 3 weeks" in v4.reason

    # Case 5: Neither deadline nor posting date mentioned -> REJECTED (avoid unverified stale vacancy)
    job_no_date = ExtractedJob(
        title="Full Stack Developer",
        company="UnknownDateCorp",
        location="Remote",
        link="https://apply.workable.com/unknown/job-123",
        jd_text="We are looking for a full stack dev with React and Python experience.",
        email="apply@unknown.com",
        has_email=True,
        deadline=None,
        posted_at=None,
        published_at=None,
    )
    v5 = validate_job_freshness(job_no_date, now=now)
    assert v5.valid is False
    assert "No verified posting date or deadline detected" in v5.reason


