"""Multi-tenant User Isolation, Knowledge Base Sync, and Account Switching Tests."""

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
from app.intake import create_application
from app.knowledge import load_candidate_for_user
from app.main import app
from app.models import Application, User


def test_multi_tenant_isolation_and_kb_sync():
    """Verify that multiple users have completely isolated Knowledge Bases and Applications."""
    init_db()

    user1_email = "user1_iso@test.org"
    user2_email = "user2_iso@test.org"

    u1_initial_kb = {
        "name": "User One",
        "email": user1_email,
        "phone": "+1 555 111 2222",
        "location": "New York, USA",
        "summary": "Senior Python and AI Engineer",
        "links": {"linkedin": "https://linkedin.com/in/userone", "github": "https://github.com/userone", "portfolio": ""},
        "skills": ["Python", "FastAPI", "PyTorch", "RAG", "SQL", "Docker"],
        "projects": [
            {"name": "AI Platform", "description": "Platform built with Python and FastAPI", "skills": ["Python", "FastAPI"]},
            {"name": "Vector RAG Search", "description": "High performance vector search pipeline", "skills": ["Python", "RAG"]},
        ],
        "experience": [{"employer": "AI Labs", "title": "Senior AI Engineer", "start": "2024", "end": "Present", "skills_used": ["Python", "FastAPI"]}],
        "education": [{"institution": "MIT", "degree": "BS CS", "field": "CS", "start": "2020", "end": "2024"}],
        "certifications": [],
    }

    u2_initial_kb = {
        "name": "User Two",
        "email": user2_email,
        "phone": "+44 20 7946 0991",
        "location": "London, UK",
        "summary": "Rust and Systems Developer",
        "links": {"linkedin": "https://linkedin.com/in/usertwo", "github": "https://github.com/usertwo", "portfolio": ""},
        "skills": ["Rust", "Actix-Web", "WebAssembly", "C++", "gRPC", "Tokio"],
        "projects": [
            {"name": "High-Speed Engine", "description": "High performance Rust networking engine", "skills": ["Rust", "Actix-Web"]},
            {"name": "Async Network Layer", "description": "Low latency socket layer", "skills": ["Rust", "Tokio"]},
        ],
        "experience": [{"employer": "Systems Corp", "title": "Systems Engineer", "start": "2023", "end": "Present", "skills_used": ["Rust"]}],
        "education": [{"institution": "Oxford", "degree": "MEng", "field": "Software", "start": "2019", "end": "2023"}],
        "certifications": [],
    }

    # 1. Setup User 1 and User 2 in database
    with get_session() as s:
        u1 = s.query(User).filter(User.email == user1_email).first()
        if not u1:
            u1 = User(
                email=user1_email,
                name="User One",
                password_hash=hash_password("password123"),
                is_active=True,
                smtp_username="user1@example.com",
                smtp_password_encrypted=encrypt_credential("app-pass-1"),
                smtp_verified=True,
                knowledge_base_json=json.dumps(u1_initial_kb),
            )
            s.add(u1)
        else:
            u1.name = "User One"
            u1.knowledge_base_json = json.dumps(u1_initial_kb)

        u2 = s.query(User).filter(User.email == user2_email).first()
        if not u2:
            u2 = User(
                email=user2_email,
                name="User Two",
                password_hash=hash_password("password456"),
                is_active=True,
                smtp_username="user2@example.com",
                smtp_password_encrypted=encrypt_credential("app-pass-2"),
                smtp_verified=True,
                knowledge_base_json=json.dumps(u2_initial_kb),
            )
            s.add(u2)
        else:
            u2.name = "User Two"
            u2.knowledge_base_json = json.dumps(u2_initial_kb)

        s.commit()
        u1_id, u2_id = u1.id, u2.id

    client = TestClient(app, follow_redirects=False)

    # 2. Test User 1 session
    token1 = create_session_token(u1_id, user1_email)
    client.cookies.set("careerpulse_auth", token1)

    r1_kb = client.get("/knowledge-base")
    assert r1_kb.status_code == 200
    assert "User One" in r1_kb.text
    assert "Python, FastAPI, PyTorch" in r1_kb.text
    assert "Rust" not in r1_kb.text

    # User 1 loads candidate model
    with get_session() as s:
        db_u1 = s.get(User, u1_id)
        c1 = load_candidate_for_user(db_u1)
        assert c1.name == "User One"
        assert "Python" in c1.skills
        assert "Rust" not in c1.skills
        assert len(c1.projects) == 2 and c1.projects[0]["name"] == "AI Platform"

    # User 1 creates an application
    with get_session() as s:
        app1 = create_application(
            s,
            email="jobs@target-ai.test",
            jd_text="Looking for a Python and FastAPI engineer to build AI pipelines.",
            job_title="AI Engineer",
            company="Target AI",
            user_id=u1_id,
            user=db_u1,
        )
        app1_id = app1.id

    # User 1 sees app1 in workspace
    r1_ws = client.get("/workspace")
    assert r1_ws.status_code == 200
    assert "Target AI" in r1_ws.text

    # 3. Switch account to User 2
    token2 = create_session_token(u2_id, user2_email)
    client.cookies.set("careerpulse_auth", token2)

    r2_kb = client.get("/knowledge-base")
    assert r2_kb.status_code == 200
    assert "User Two" in r2_kb.text
    assert "Rust, Actix-Web, WebAssembly" in r2_kb.text
    assert "User One" not in r2_kb.text
    assert "user1_iso@test.org" not in r2_kb.text
    assert "AI Labs" not in r2_kb.text
    assert "AI Platform" not in r2_kb.text

    # User 2 loads candidate model
    with get_session() as s:
        db_u2 = s.get(User, u2_id)
        c2 = load_candidate_for_user(db_u2)
        assert c2.name == "User Two"
        assert "Rust" in c2.skills
        assert "Python" not in c2.skills
        assert len(c2.projects) == 2 and c2.projects[0]["name"] == "High-Speed Engine"

    # User 2 workspace does NOT contain User 1's applications
    r2_ws = client.get("/workspace")
    assert r2_ws.status_code == 200
    assert "Target AI" not in r2_ws.text

    # User 2 attempts to view User 1's application directly -> 403 Forbidden
    r2_forbidden = client.get(f"/application/{app1_id}")
    assert r2_forbidden.status_code == 403

    # 4. User 2 updates their Knowledge Base
    post_data = {
        "name": "User Two Updated",
        "email": user2_email,
        "phone": "+44 20 7946 0991",
        "location": "Cambridge, UK",
        "summary": "Staff Systems & Distributed Systems Architect",
        "skills_csv": "Rust, Tokio, Distributed Systems, WebAssembly",
        "experience_json": json.dumps([{
            "employer": "Cloud Systems UK",
            "title": "Staff Architect",
            "start": "2024",
            "end": "Present",
            "bullets": ["Architected distributed storage"],
            "skills_used": ["Rust", "Tokio"]
        }]),
        "projects_json": json.dumps([{
            "name": "Distributed Storage Engine",
            "subtitle": "Rust Engine",
            "bullet": "Fast async distributed storage engine",
            "skills": ["Rust", "Tokio"]
        }]),
        "education_json": json.dumps([{"institution": "Cambridge", "degree": "PhD", "field": "Distributed Systems", "start": "2021", "end": "2024"}]),
        "certifications_json": json.dumps([]),
    }
    r2_save = client.post("/knowledge-base", data=post_data)
    assert r2_save.status_code in (200, 303)

    # 5. Switch back to User 1
    client.cookies.set("careerpulse_auth", token1)
    r1_kb_after = client.get("/knowledge-base")
    assert r1_kb_after.status_code == 200
    assert "User One" in r1_kb_after.text
    assert "Python, FastAPI, PyTorch" in r1_kb_after.text
    assert "User Two Updated" not in r1_kb_after.text
    assert "Distributed Storage Engine" not in r1_kb_after.text

    print("PASS multi-tenant isolation and knowledge base sync test!")


if __name__ == "__main__":
    test_multi_tenant_isolation_and_kb_sync()
