import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from app.main import create_app
from app.db import get_session, init_db, set_user_selected_template
from app.models import User, Application
from app.auth import hash_password, create_session_token
from app.resume_builder import RESUMES_OUTPUT_DIR


@pytest.fixture
def client():
    init_db()
    app = create_app()
    return TestClient(app)


def test_user_default_template_applied_on_recreate(client):
    with get_session() as s:
        user = s.query(User).filter_by(email="tmpl_default@example.com").first()
        if not user:
            user = User(
                email="tmpl_default@example.com",
                password_hash=hash_password("Pass123!"),
                name="Template Default User",
                smtp_verified=True,
                smtp_username="tmpl_default@example.com",
                smtp_password_encrypted="dummy_enc_pwd",
                selected_template_id="oxford_editorial",
            )
            s.add(user)
            s.commit()
            s.refresh(user)
        else:
            user.smtp_verified = True
            user.smtp_username = "tmpl_default@example.com"
            user.smtp_password_encrypted = "dummy_enc_pwd"
            user.selected_template_id = "oxford_editorial"
            s.commit()
            s.refresh(user)

        app_record = Application(
            user_id=user.id,
            email="jobs@oxfordtech.example.com",
            company="Oxford Tech",
            job_title="Software Architect",
            jd_text="Python FastAPI distributed systems cloud architect.",
            status="ready",
            template_id="oxford_editorial",
        )
        s.add(app_record)
        s.commit()
        s.refresh(app_record)
        app_id = app_record.id
        user_id = user.id

    token = create_session_token(user_id, "tmpl_default@example.com")
    client.cookies.set("careerpulse_auth", token)

    # Recreate without specifying variant form parameter (auto)
    resp = client.post(
        f"/application/{app_id}/resume/recreate",
        data={"project_count": "5"},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    # Verify that application record and HTML have Oxford Editorial
    with get_session() as s:
        updated = s.get(Application, app_id)
        assert updated.template_id == "oxford_editorial"

    html_file = RESUMES_OUTPUT_DIR / f"application_{app_id}.html"
    assert html_file.exists()
    assert "oxford_editorial" in html_file.read_text(encoding="utf-8")

    # Now set default to silicon_compact
    set_user_selected_template(user_id, "silicon_compact")
    
    # Recreate explicitly passing silicon_compact from recreate form
    resp2 = client.post(
        f"/application/{app_id}/resume/recreate",
        data={"project_count": "5", "variant": "silicon_compact"},
        follow_redirects=True,
    )
    assert resp2.status_code == 200

    with get_session() as s:
        updated2 = s.get(Application, app_id)
        assert updated2.template_id == "silicon_compact"

    html_file = RESUMES_OUTPUT_DIR / f"application_{app_id}.html"
    assert html_file.exists()
    content = html_file.read_text(encoding="utf-8")
    assert "silicon_compact" in content


def test_change_template_and_email_dispatch_attachment(client):
    with get_session() as s:
        user = s.query(User).filter_by(email="tmpl_email@example.com").first()
        if not user:
            user = User(
                email="tmpl_email@example.com",
                password_hash=hash_password("Pass123!"),
                name="Template Email User",
                smtp_verified=True,
                smtp_username="tmpl_email@example.com",
                smtp_password_encrypted="dummy_enc_pwd",
                selected_template_id="monarch_executive",
            )
            s.add(user)
            s.commit()
            s.refresh(user)
        else:
            user.smtp_verified = True
            user.smtp_username = "tmpl_email@example.com"
            user.smtp_password_encrypted = "dummy_enc_pwd"
            user.selected_template_id = "monarch_executive"
            s.commit()
            s.refresh(user)

        app_record = Application(
            user_id=user.id,
            company="Monarch Corp",
            job_title="Lead AI Engineer",
            jd_text="Senior Machine Learning Engineer with Python PyTorch and LLMs.",
            email="hiring@monarchcorp.example.com",
            status="ready",
            template_id="monarch_executive",
        )
        s.add(app_record)
        s.commit()
        s.refresh(app_record)
        app_id = app_record.id
        user_id = user.id

    token = create_session_token(user_id, "tmpl_email@example.com")
    client.cookies.set("careerpulse_auth", token)

    # Change template to nova_minimalist via dropdown route
    resp = client.post(
        f"/application/{app_id}/change-template",
        data={"template_id": "nova_minimalist"},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    with get_session() as s:
        updated = s.get(Application, app_id)
        assert updated.template_id == "nova_minimalist"

    html_file = RESUMES_OUTPUT_DIR / f"application_{app_id}.html"
    assert html_file.exists()
    assert "nova_minimalist" in html_file.read_text(encoding="utf-8")

    with patch("app.main.outbound_guard.send") as mock_send:
        mock_send.return_value = MagicMock(success=True, disposition="sent", recipient="hiring@monarchcorp.example.com")

        send_resp = client.post(
            f"/application/{app_id}/send",
            data={
                "email": "hiring@monarchcorp.example.com",
                "subject": "Application for Lead AI Engineer",
                "drafted_email": "Dear Hiring Team,\n\nPlease find attached my resume.",
            },
            follow_redirects=False,
        )
        assert send_resp.status_code == 303
        assert "sent=1" in send_resp.headers["location"]

        assert mock_send.called
        kwargs = mock_send.call_args.kwargs
        assert kwargs["intended_recipient"] == "hiring@monarchcorp.example.com"
        assert kwargs["resume_pdf_path"] is not None
        pdf_path = Path(kwargs["resume_pdf_path"])
        assert pdf_path.exists()
        assert f"application_{app_id}.pdf" in str(pdf_path)


def test_resume_html_cache_control_headers(client):
    with get_session() as s:
        user = s.query(User).filter_by(email="tmpl_cache@example.com").first()
        if not user:
            user = User(
                email="tmpl_cache@example.com",
                password_hash=hash_password("Pass123!"),
                name="Template Cache User",
                smtp_verified=True,
                smtp_username="tmpl_cache@example.com",
                smtp_password_encrypted="dummy_enc_pwd",
                selected_template_id="oxford_editorial",
            )
            s.add(user)
            s.commit()
            s.refresh(user)
        else:
            user.smtp_verified = True
            user.smtp_username = "tmpl_cache@example.com"
            user.smtp_password_encrypted = "dummy_enc_pwd"
            user.selected_template_id = "oxford_editorial"
            s.commit()
            s.refresh(user)

        app_record = Application(
            user_id=user.id,
            email="jobs@editoriallabs.example.com",
            company="Editorial Labs",
            job_title="Backend Developer",
            jd_text="Python FastAPI Postgres",
            status="ready",
            template_id="oxford_editorial",
        )
        s.add(app_record)
        s.commit()
        s.refresh(app_record)
        app_id = app_record.id
        user_id = user.id

    token = create_session_token(user_id, "tmpl_cache@example.com")
    client.cookies.set("careerpulse_auth", token)

    resp = client.get(f"/application/{app_id}/resume.html")
    assert resp.status_code == 200
    assert "no-cache" in resp.headers.get("cache-control", "")
    assert "no-store" in resp.headers.get("cache-control", "")
