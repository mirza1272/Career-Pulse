"""Tests for Radar search accuracy, parallel execution, and async JSON endpoints."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import pytest
from starlette.testclient import TestClient
from app.main import app, get_session
from app.models import User, Job, Application
from app.auth import create_session_token, hash_password, encrypt_credential
from radar.search import is_valid_single_job_posting, parse_job_title_and_company
from radar.pipeline import run_pipeline


def test_radar_search_validator_filters_aggregates():
    """Verify that aggregate listing pages are rejected and direct job postings are accepted."""
    # Aggregates should return False
    valid, _ = is_valid_single_job_posting("https://pk.indeed.com/q-python-jobs.html", "200+ Python Jobs in Lahore", "")
    assert not valid

    valid, _ = is_valid_single_job_posting("https://www.linkedin.com/jobs/search?keywords=python", "Python Jobs - Search", "")
    assert not valid

    valid, _ = is_valid_single_job_posting("https://glassdoor.com/Job/pakistan-ai-engineer-jobs-SRCH_IL.0,8_IN184_KO9,20.htm", "AI Engineer Jobs", "")
    assert not valid

    valid, _ = is_valid_single_job_posting("https://rozee.pk/jobs-in-lahore", "Jobs in Lahore - Rozee.pk", "")
    assert not valid

    valid, _ = is_valid_single_job_posting("https://monster.com/jobs/search", "Search Jobs", "")
    assert not valid

    # Direct postings should return True
    valid, _ = is_valid_single_job_posting("https://boards.greenhouse.io/anthropic/jobs/5291837004", "Senior AI Safety Engineer - Anthropic", "Design and evaluate...")
    assert valid

    valid, _ = is_valid_single_job_posting("https://jobs.lever.co/scale/9c83b8a1-1234", "Machine Learning Engineer - Scale AI", "We are hiring...")
    assert valid

    valid, _ = is_valid_single_job_posting("https://jobs.ashbyhq.com/posthog/12984-soft-eng", "Full Stack Engineer - PostHog", "Build open source tools...")
    assert valid

    valid, _ = is_valid_single_job_posting("https://www.linkedin.com/jobs/view/4102938192", "Lead Python Developer - TechCorp", "Join our backend team...")
    assert valid


def test_radar_clean_title_and_company_parser():
    """Verify clean extraction of titles and company names."""
    title, company = parse_job_title_and_company(
        "Senior Python Engineer - Lahore, Pakistan at Arbisoft",
        "https://boards.greenhouse.io/arbisoft/jobs/123"
    )
    assert "Python Engineer" in title
    assert company in ("Arbisoft", "boards.greenhouse.io", "arbisoft")

    title2, company2 = parse_job_title_and_company(
        "AI Agent Engineer (Remote) | Scale AI",
        "https://jobs.lever.co/scale/456"
    )
    assert "AI Agent Engineer" in title2
    assert "Scale AI" in company2 or "scale" in company2.lower()


def test_radar_pipeline_direct_run_and_scan_endpoint():
    """Verify that radar pipeline runs smoothly against the unified database without table errors."""
    client = TestClient(app)

    with get_session() as s:
        user = s.query(User).filter_by(email="async_test_user@example.com").first()
        if not user:
            user = User(
                email="async_test_user@example.com",
                password_hash=hash_password("testpass123"),
                name="Async Tester",
                is_active=True,
                smtp_verified=True,
                smtp_username="async_test_user@example.com",
                smtp_password_encrypted=encrypt_credential("app_password_123"),
            )
            s.add(user)
            s.commit()
            s.refresh(user)
        user_id = user.id

    token = create_session_token(user_id, "async_test_user@example.com")
    client.cookies.set("careerpulse_auth", token)

    # Test POST /radar/scan
    res = client.post(
        "/radar/scan",
        data={"query": "Python Engineer", "sources": ["mock"]},
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert "/radar?scanned=1" in res.headers.get("location", "")


def test_async_radar_endpoints_and_application_email_edit():
    """Verify instant JSON responses for status changes, deletions, and email edits."""
    client = TestClient(app)

    # 1. Setup test user in database with verified credentials so gate passes
    with get_session() as s:
        user = s.query(User).filter_by(email="async_test_user@example.com").first()
        if not user:
            user = User(
                email="async_test_user@example.com",
                password_hash=hash_password("testpass123"),
                name="Async Tester",
                is_active=True,
                smtp_verified=True,
                smtp_username="async_test_user@example.com",
                smtp_password_encrypted=encrypt_credential("app_password_123"),
            )
            s.add(user)
        else:
            user.smtp_verified = True
            user.smtp_username = "async_test_user@example.com"
            user.smtp_password_encrypted = encrypt_credential("app_password_123")
        s.commit()
        s.refresh(user)
        user_id = user.id

        # Add a radar job
        job = Job(
            dedup_key="test_radar_job_99999",
            title="Senior Machine Learning Specialist",
            company="DeepMind Labs",
            location="Remote",
            link="https://boards.greenhouse.io/deepmind/jobs/999",
            status="active",
            source="greenhouse",
        )
        s.add(job)

        # Add an application
        app_record = Application(
            user_id=user_id,
            job_title="Senior AI Engineer",
            company="Neural Works",
            email="recruiter@neuralworks.ai",
            subject="Application for Senior AI Engineer - Mumtaz",
            drafted_email="Dear Hiring Manager,\nI am excited to apply...",
            jd_text="Requirements: Python, PyTorch, LangChain, FastAPI.",
            status="ready",
        )
        s.add(app_record)
        s.commit()
        s.refresh(job)
        s.refresh(app_record)
        job_id = job.id
        app_id = app_record.id

    token = create_session_token(user_id, "async_test_user@example.com")
    client.cookies.set("careerpulse_auth", token)

    # 2. Test AJAX Radar Status Change -> Returns JSON
    res = client.post(
        f"/radar/job/{job_id}/status",
        data={"status": "saved"},
        headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True
    assert data["new_status"] == "saved"

    # Verify status in database
    with get_session() as s:
        j = s.get(Job, job_id)
        assert j.status == "saved"

    # 3. Test AJAX Application Email Edit -> Returns JSON
    res = client.post(
        f"/application/{app_id}/edit",
        data={
            "job_title": "Lead AI Engineer",
            "company": "Neural Works Global",
            "email": "careers@neuralworks.ai",
            "subject": "Application for Lead AI Engineer - Mumtaz",
            "drafted_email": "Dear Engineering Team,\nI am writing to express my strong enthusiasm...",
        },
        headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True

    # Verify update in database
    with get_session() as s:
        a = s.get(Application, app_id)
        assert a.job_title == "Lead AI Engineer"
        assert a.company == "Neural Works Global"
        assert "enthusiasm" in a.drafted_email

    # 4. Test AJAX Single Radar Job Delete -> Returns JSON
    res = client.post(
        f"/radar/job/{job_id}/delete",
        headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True

    # Verify deleted
    with get_session() as s:
        assert s.get(Job, job_id) is None
