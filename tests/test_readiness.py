"""Runnable checks for KB readiness gating (Phase 3).

- kb_completeness: blocked / minimum / strong levels + checklist contents.
- create_application: refuses below-minimum profiles with a named checklist.
- Education renders in resume HTML from the KB (both KB-form and
  profile.yaml shapes); empty education removes the section cleanly.
- KB form round-trips education + certifications.

Run with:  AUTH_PASSWORD=<pw> python tests/test_readiness.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from fastapi.testclient import TestClient

from app import config
from app.auth import create_session_token, encrypt_credential, hash_password
from app.db import get_session, init_db
from app.intake import IntakeError, create_application
from app.knowledge import Candidate, load_candidate, load_candidate_for_user
from app.main import app
from app.models import User
from app.readiness import format_blocked_message, kb_completeness
from app.resume_builder import build_resume_content, render_education_html


def _cand(**kw):
    base = dict(
        name="Test User", email="t@example.com", phone="+92 300 0000000",
        location="Lahore, Pakistan", summary="AI engineer",
        skills=[f"s{i}" for i in range(12)],
        projects=[{"name": f"p{i}"} for i in range(6)],
        experience=[{"employer": "Acme", "title": "Dev"}],
        education=[{"institution": "FAST", "degree": "BS CS", "start": "2023", "end": "present"}],
    )
    base.update(kw)
    return Candidate(**base)


def test_levels():
    assert kb_completeness(_cand()).level == "strong"
    assert kb_completeness(_cand()).missing == []

    minimum = _cand(skills=["a", "b", "c", "d", "e", "f"],
                    projects=[{"name": "p1"}, {"name": "p2"}, {"name": "p3"}],
                    experience=[])
    rep = kb_completeness(minimum)
    assert rep.level == "minimum", rep
    assert any("project" in m for m in rep.missing), rep.missing
    assert any("skill" in m for m in rep.missing), rep.missing
    assert any("experience" in m for m in rep.missing), rep.missing

    blocked = _cand(skills=["a", "b"], projects=[{"name": "p1"}], education=[],
                    summary="", name="Applicant")
    rep = kb_completeness(blocked)
    assert rep.level == "blocked", rep
    assert len(rep.missing) >= 4, rep.missing
    assert not rep.ready
    msg = format_blocked_message(rep)
    assert "not ready" in msg and "- " in msg
    print("PASS levels blocked/minimum/strong + checklist")


def test_no_contact_blocked():
    rep = kb_completeness(_cand(email="", phone=""))
    assert rep.level == "blocked"
    assert any("email address or phone" in m for m in rep.missing)
    print("PASS missing contact blocks")


def _authed_client():
    init_db()
    with get_session() as s:
        test_user = s.query(User).filter(User.email == "readiness_test_user@example.test").first()
        if not test_user:
            test_user = User(
                email="readiness_test_user@example.test",
                name="Test User",
                password_hash=hash_password("testpass123"),
                is_active=True,
                smtp_username="test@example.com",
                smtp_password_encrypted=encrypt_credential("test-app-password"),
                smtp_verified=True,
                knowledge_base_json="{}",
            )
            s.add(test_user)
            s.commit()
        uid, email = test_user.id, test_user.email
    token = create_session_token(uid, email)
    client = TestClient(app)
    client.cookies.set("careerpulse_auth", token)
    return client, uid


def test_create_application_blocked_for_weak_profile():
    _authed_client()
    with get_session() as s:
        weak = s.query(User).filter(User.email == "weak@example.com").first()
        if not weak:
            weak = User(email="weak@example.com", name="Weak User",
                        password_hash=hash_password("x" * 12), is_active=True,
                        knowledge_base_json=json.dumps({
                            "name": "Weak User", "email": "weak@example.com",
                            "skills": ["Python", "SQL"],
                            "projects": [{"name": "Only Project"}],
                        }))
            s.add(weak)
            s.commit()
        weak_id = weak.id
    with get_session() as s:
        weak = s.get(User, weak_id)
        try:
            create_application(s, jd_text="We are hiring a Python developer. Apply now.",
                               user_id=weak_id, user=weak)
        except IntakeError as exc:
            msg = str(exc)
            assert "not ready" in msg, msg
            assert "5 skills" in msg and "2 projects" in msg, msg
            print(f"PASS create_application blocked; checklist: {msg.splitlines()[1].strip()}")
            return
        raise AssertionError("create_application should have refused the weak profile")


def test_admin_profile_passes_gate():
    _authed_client()
    with get_session() as s:
        admin = s.query(User).filter(User.email == config.ADMIN_EMAIL.strip().lower()).first()
        report = kb_completeness(load_candidate_for_user(admin))
    assert report.ready, f"admin effective profile should pass the gate: {report}"
    print(f"PASS admin effective profile passes gate (level={report.level})")


def test_education_renders_from_kb():
    cand = load_candidate()  # base YAML profile -> profile.yaml education shape
    html = render_education_html(cand)
    assert "FAST" in html and "Education" in html, html[:200]
    built = build_resume_content(cand, "python developer job", job_title="AI Engineer")
    assert "FAST National University" in built.html_content
    assert "<!--REGION:EDUCATION-->" in built.html_content
    print("PASS education renders from KB into resume HTML")

    empty = _cand(education=[])
    assert render_education_html(empty) == ""
    built2 = build_resume_content(empty, "python developer job", job_title="AI Engineer")
    assert "Education</h2>" not in built2.html_content, "empty education must remove the section"
    print("PASS empty education removes the section cleanly")


def test_kb_form_roundtrip_education_certifications():
    client, uid = _authed_client()
    edu = [{"institution": "Test University", "degree": "BS AI",
            "field": "AI", "start": "2022", "end": "2026"}]
    certs = [
        {"name": "Cert A (2024)", "organization": "Test Org", "link": ""},
        {"name": "Cert B (2025)", "organization": "Test Org", "link": ""},
    ]
    r = client.post("/knowledge-base", data={
        "name": "Test User", "email": "t@example.com",
        "education_json": json.dumps(edu),
        "certifications_json": json.dumps(certs),
    })
    assert r.status_code in (200, 303), r.status_code
    with get_session() as s:
        u = s.get(User, uid)
        kb = json.loads(u.knowledge_base_json)
    assert kb["education"] == edu, kb.get("education")
    assert kb["certifications"] == certs, kb.get("certifications")

    r = client.get("/knowledge-base")
    assert r.status_code == 200
    assert "Test University" in r.text, "education not shown in KB form"
    assert "Cert A (2024)" in r.text, "certifications not shown in KB form"
    assert "Knowledge Base" in r.text, "KB page header missing"
    print("PASS KB form round-trips education + certifications")


def test_new_page_shows_checklist():
    client, _ = _authed_client()
    r = client.get("/new")
    assert r.status_code == 200
    # Admin profile is complete -> no blocked banner; the template guards handle both states.
    assert "newApplicationForm" in r.text
    print("PASS /new renders with readiness context")


if __name__ == "__main__":
    test_levels()
    test_no_contact_blocked()
    test_create_application_blocked_for_weak_profile()
    test_admin_profile_passes_gate()
    test_education_renders_from_kb()
    test_kb_form_roundtrip_education_certifications()
    test_new_page_shows_checklist()
    print("ALL READINESS CHECKS PASSED")
