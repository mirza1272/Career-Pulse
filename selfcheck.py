"""One runnable check for Phases 1-4.

Proves:
- Phase 1: Shared schema + queue round-trip.
- Phase 2: JD intake (text & image OCR) -> LLM email draft -> pending application, via intake & API.
- Phase 3: Precision resume tailoring (knowledge base + variant selection), ATS scoring (safety + relevance),
  automated improvement loop, headless Chrome A4 PDF render, and resume delivery endpoints.
- Phase 4: Review & edit application, guarded Approve & Send (SMTP with test-mode safety redirect),
  manual portal apply path, and Sent Applications dashboard.

Runs fully offline: with no LLM_API_KEY it uses deterministic fallbacks.
With TEST_MODE=1, outbound sends are safely redirected to your test inbox.

    .venv/bin/python selfcheck.py
"""

from __future__ import annotations

from pathlib import Path
from app import config
from app.db import get_session, init_db
from app.intake import create_application
from app.models import Application
from app.knowledge import load_candidate
from app.ats import score_resume
from app.resume_builder import build_resume_content, render_pdf_from_html
from app.outbound import OutboundEmailGuard


def main() -> None:
    init_db()

    # Satisfy the mandatory credentials gate for the test user (mirrors a
    # real user completing /credentials before using engine features).
    from app.auth import encrypt_credential
    from app.models import User

    with get_session() as s:
        u = s.get(User, 1)
        if u is not None and not (u.smtp_username and u.smtp_password_encrypted):
            u.smtp_username = "selfcheck@example.com"
            u.smtp_password_encrypted = encrypt_credential("selfcheck-app-password")
            u.smtp_verified = True
            s.commit()

    # --- Phase 4 Unit Checks: Outbound Safety Guard ---
    guard = OutboundEmailGuard()
    if config.TEST_MODE:
        resolved_recp, is_redirected = guard.resolve_recipient("recruiter@target-company.com")
        assert is_redirected, "Expected test-mode redirection to be True"
        assert resolved_recp == config.TEST_RECIPIENT, f"Expected {config.TEST_RECIPIENT}, got {resolved_recp}"
    else:
        resolved_recp, is_redirected = guard.resolve_recipient("recruiter@target-company.com")
        assert not is_redirected, "Expected live mode (no redirection)"
        assert resolved_recp == "recruiter@target-company.com"

    # Verify real send is forbidden when ALLOW_REAL_EMAIL=0
    prev_tm = config.TEST_MODE
    prev_re = config.ALLOW_REAL_EMAIL
    try:
        config.TEST_MODE = False
        config.ALLOW_REAL_EMAIL = False
        allowed, reason = guard.real_send_allowed()
        assert not allowed, "Expected real send to be disallowed without ALLOW_REAL_EMAIL=1"
        assert "CAREERPULSE_ALLOW_REAL_EMAIL" in reason
    finally:
        config.TEST_MODE = prev_tm
        config.ALLOW_REAL_EMAIL = prev_re

    # --- Phase 3 Unit Checks: Candidate Knowledge & ATS Engine ---
    candidate = load_candidate()
    assert candidate.name == "Haseeb Ur Rahman", f"Unexpected candidate name: {candidate.name}"
    assert len(candidate.skills) > 5, "Candidate skills not loaded"
    assert len(candidate.projects) > 0, "Candidate project library empty"

    # Test ATS Scorer directly
    test_resume = (
        "Haseeb Ur Rahman\nEmail: mirzahaseeb0566@gmail.com Phone: +92 303 8607925\n"
        "Summary\nComputer Science graduate passionate about Machine Learning and AI.\n"
        "Experience\nDec 2025 - Mar 2026\nML Engineer at CodeCelix with Python, TensorFlow.\n"
        "Education\nFAST National University Bachelor in Computer Science 2023 - present\n"
        "Skills\nPython, TensorFlow, PyTorch, Deep Learning, Machine Learning\n"
        "Projects\nRecommendation System using PyTorch and clustering."
    )
    test_score = score_resume(
        resume_text=test_resume,
        jd_text="Seeking Machine Learning Engineer with Python, TensorFlow, and PyTorch experience.",
        job_title="ML Engineer",
        attested_candidate_skills=candidate.skills,
    )
    assert test_score.ats_readiness_score >= 80.0, f"Baseline ATS score too low: {test_score.ats_readiness_score}"
    assert test_score.parsing_safety_score >= 30.0, "Parsing safety score failed"
    assert "python" in [s.lower() for s in test_score.matched_skills], "Python was not matched"

    # Test Precision Resume Builder & Chrome PDF Generator
    built = build_resume_content(
        candidate,
        jd_text="Backend Developer role, Python, SQL, REST APIs.",
        job_title="Backend Developer",
    )
    assert "Haseeb Ur Rahman" in built.html_content
    assert "Nunito" in built.html_content
    assert len(built.selected_projects) > 0

    tmp_html = Path("/tmp/selfcheck_resume.html")
    tmp_pdf = Path("/tmp/selfcheck_resume.pdf")
    tmp_html.write_text(built.html_content, encoding="utf-8")
    render_pdf_from_html(tmp_html, tmp_pdf)
    assert tmp_pdf.exists() and tmp_pdf.stat().st_size > 5000, "PDF rendering failed or produced empty file"
    tmp_html.unlink(missing_ok=True)
    tmp_pdf.unlink(missing_ok=True)

    # --- Phase 2 + Phase 3: Full End-to-End Application Intake ---
    with get_session() as s:
        row = create_application(
            s,
            email="hiring@acme.test",
            jd_text="We seek a Machine Learning Engineer with Python, TensorFlow and CNN experience.",
            job_title="ML Engineer",
            company="Acme",
            link="https://acme.test/job/1",
            user_id=1,
        )
        rid = row.id
        assert row.status in ("ready", "pending", "draft")
        assert row.drafted_email, "email was not drafted"
        assert "Haseeb" in row.drafted_email or len(row.drafted_email) > 60

        # Phase 3 asserts on created row
        assert row.ats_score and row.ats_score >= 70.0, f"Expected ATS score >= 70, got {row.ats_score}"
        assert row.ats_attempts >= 1, "Expected at least 1 ATS attempt"
        assert row.resume_path and Path(row.resume_path).exists(), f"Resume PDF not found on disk: {row.resume_path}"

    # --- Phase 1: it shows up in the queue ---
    with get_session() as s:
        pending = s.query(Application).filter(Application.status.in_(["ready", "pending", "draft"])).all()
        assert any(a.id == rid for a in pending)

    # --- App + API & Resume Delivery Respond ---
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app, follow_redirects=False)
    assert client.get("/health").json() == {"status": "ok"}
    # Verify unauthenticated access redirects to /login
    unauth = client.get("/")
    assert unauth.status_code == 303
    assert "/login" in unauth.headers["location"]

    # Authenticate client with valid session token
    from app.auth import create_session_token
    token = create_session_token(1, config.AUTH_EMAIL)
    client.cookies.set("careerpulse_auth", token)
    client.follow_redirects = True
    assert client.get("/").status_code == 200

    # Test application detail and resume endpoints
    detail_res = client.get(f"/application/{rid}")
    assert detail_res.status_code == 200
    assert "ATS Score" in detail_res.text
    assert "Review &amp; Edit Application" in detail_res.text or "Review & Edit" in detail_res.text

    pdf_res = client.get(f"/application/{rid}/resume.pdf")
    assert pdf_res.status_code == 200
    assert pdf_res.headers["content-type"] == "application/pdf"
    assert len(pdf_res.content) > 5000

    html_res = client.get(f"/application/{rid}/resume.html")
    assert html_res.status_code == 200
    assert "Haseeb Ur Rahman" in html_res.text

    # --- Phase 4: Review & Edit Application ---
    edit_res = client.post(
        f"/application/{rid}/edit",
        data={
            "job_title": "Lead ML Engineer",
            "company": "Acme AI Corp",
            "email": "lead-hiring@acme.test",
            "subject": "Application for Lead ML Engineer — Haseeb Ur Rahman",
            "drafted_email": "Edited cover letter text with specialized ML skills.",
        },
        follow_redirects=False,
    )
    assert edit_res.status_code == 303, f"Expected redirect, got {edit_res.status_code}"

    with get_session() as s:
        edited_row = s.get(Application, rid)
        assert edited_row is not None
        assert edited_row.job_title == "Lead ML Engineer"
        assert edited_row.company == "Acme AI Corp"
        assert edited_row.email == "lead-hiring@acme.test"
        assert "Edited cover letter" in edited_row.drafted_email

    # --- Phase 4: Guarded Approve & Send (Test Mode Redirect) ---
    from unittest.mock import patch
    from app.outbound import OutboundResult

    with patch(
        "app.main.outbound_guard.send",
        return_value=OutboundResult(
            disposition="redirected",
            recipient="haseeb.rahman0566@gmail.com",
            original_recipient="lead-hiring@acme.test",
            message_id="mock-msg-001",
        ),
    ):
        send_res = client.post(f"/application/{rid}/send", follow_redirects=False)
        assert send_res.status_code == 303

    with get_session() as s:
        sent_row = s.get(Application, rid)
        assert sent_row is not None
        assert sent_row.status == "sent", f"Expected status 'sent', got {sent_row.status}"
        assert sent_row.disposition == "redirected"
        assert sent_row.sent_at is not None

    # --- Phase 4: Manual Web-Portal Application (Mark as Applied) ---
    # Create manual job via Radar API
    api = client.post(
        "/api/applications",
        json={
            "email": "",  # Web portal job, no email
            "jd_text": "Full-Stack Engineer role, Next.js, FastAPI.",
            "job_title": "Full-Stack Engineer",
            "company": "WebCorp",
            "link": "https://webcorp.test/careers/apply",
        },
    )
    assert api.status_code == 200, api.text
    api_app_id = api.json()["id"]

    # Mark as applied
    applied_res = client.post(f"/application/{api_app_id}/mark-applied", follow_redirects=False)
    assert applied_res.status_code == 303

    with get_session() as s:
        manual_row = s.get(Application, api_app_id)
        assert manual_row is not None
        assert manual_row.status == "sent"
        assert manual_row.disposition == "manual_applied"

    # --- Phase 4: Sent Applications Dashboard ---
    sent_dash = client.get("/sent")
    assert sent_dash.status_code == 200
    assert "Sent Applications" in sent_dash.text
    assert "Lead ML Engineer" in sent_dash.text
    assert "Full-Stack Engineer" in sent_dash.text

    # Clean up test rows
    with get_session() as s:
        s.query(Application).filter(
            Application.job_title.in_(["ML Engineer", "Lead ML Engineer", "Backend Engineer", "Full-Stack Engineer"])
        ).delete(synchronize_session=False)

    # --- Phase 4 Advanced: Resume Customization, Live Edit, Custom Upload, & 1-Page Guarantee ---
    import subprocess
    import io

    # Create a fresh application to thoroughly test resume customization
    with get_session() as s:
        custom_app = create_application(
            s,
            email="hiring@techcorp.test",
            jd_text="Senior Machine Learning Engineer with strong PyTorch, TensorFlow, CNN, NLP, and system design expertise.",
            job_title="Senior ML Engineer",
            company="TechCorp AI",
            link="https://techcorp.test/jobs/ml-senior",
        )
        ca_id = custom_app.id
        initial_pdf = Path(custom_app.resume_path)
        assert initial_pdf.exists()

    # 1. Verify default project count is 4 and strictly 1 page
    pinfo = subprocess.run(["pdfinfo", str(initial_pdf)], capture_output=True, text=True, check=True)
    assert "Pages:           1" in pinfo.stdout, f"Initial resume PDF overflowed page 1:\n{pinfo.stdout}"
    with get_session() as s:
        row = s.get(Application, ca_id)
        html_file = Path(row.resume_path).with_suffix(".html")
        assert html_file.exists()
        html_txt = html_file.read_text(encoding="utf-8")
        assert html_txt.count('<div class="proj">') >= 4, f"Expected at least 4 projects, found: {html_txt.count('<div class=\"proj\">')}"

    # 2. Test Recreate Resume with 5 Projects & Custom Focus
    recreate_res = client.post(
        f"/application/{ca_id}/resume/recreate",
        data={
            "project_count": "5",
            "variant": "se_am",
            "custom_focus": "Highlight Deep Learning and CNN models with production deployment.",
        },
        follow_redirects=False,
    )
    assert recreate_res.status_code == 303, f"Expected 303 redirect, got {recreate_res.status_code}"
    with get_session() as s:
        row = s.get(Application, ca_id)
        p5_html = Path(row.resume_path).with_suffix(".html").read_text(encoding="utf-8")
        assert p5_html.count('<div class="proj">') == 5, f"Expected exactly 5 projects, found: {p5_html.count('<div class=\"proj\">')}"
        # Verify 5 projects still fit on strictly 1 page
        pinfo5 = subprocess.run(["pdfinfo", str(row.resume_path)], capture_output=True, text=True, check=True)
        assert "Pages:           1" in pinfo5.stdout, f"5-project resume PDF overflowed page 1:\n{pinfo5.stdout}"

    # 3. Test Live Edit Resume Content (Summary & Skills CSV)
    edit_resume_res = client.post(
        f"/application/{ca_id}/resume/edit",
        data={
            "summary": "Custom edited summary showcasing top-tier AI engineering leadership.",
            "skills": "Python, PyTorch, TensorFlow, Docker, Kubernetes, MLOps, LangChain, FastAPI",
            "experience": "",
            "projects": "",
        },
        follow_redirects=False,
    )
    assert edit_resume_res.status_code == 303, f"Expected 303 redirect, got {edit_resume_res.status_code}"
    with get_session() as s:
        row = s.get(Application, ca_id)
        edited_html = Path(row.resume_path).with_suffix(".html").read_text(encoding="utf-8")
        assert "Custom edited summary showcasing top-tier AI engineering leadership." in edited_html
        assert "<li>Docker</li>" in edited_html
        assert "<li>MLOps</li>" in edited_html
        assert row.ats_score is not None and row.ats_score > 0
        pinfo_edit = subprocess.run(["pdfinfo", str(row.resume_path)], capture_output=True, text=True, check=True)
        assert "Pages:           1" in pinfo_edit.stdout, f"Edited resume PDF overflowed page 1:\n{pinfo_edit.stdout}"

    # 4. Test Custom PDF Upload
    # Generate a dummy valid PDF bytes
    dummy_pdf_bytes = b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n3 0 obj<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R/Resources<<>>>>endobj\nxref\n0 4\n0000000000 65535 f \n0000000009 00000 n \n0000000052 00000 n \n0000000101 00000 n \ntrailer<</Size 4/Root 1 0 R>>\nstartxref\n178\n%%EOF\n"
    upload_res = client.post(
        f"/application/{ca_id}/resume/upload",
        files={"custom_resume": ("my_custom_cv.pdf", io.BytesIO(dummy_pdf_bytes), "application/pdf")},
        follow_redirects=False,
    )
    assert upload_res.status_code == 303, f"Expected 303 redirect, got {upload_res.status_code}"
    with get_session() as s:
        row = s.get(Application, ca_id)
        assert "custom" in row.resume_path
        assert Path(row.resume_path).exists()
        assert Path(row.resume_path).read_bytes() == dummy_pdf_bytes

    # Verify custom PDF is now delivered by /resume.pdf
    served_pdf = client.get(f"/application/{ca_id}/resume.pdf")
    assert served_pdf.status_code == 200
    assert served_pdf.content == dummy_pdf_bytes

    # Verify custom PDF is displayed with badge on application view
    detail_custom = client.get(f"/application/{ca_id}")
    assert detail_custom.status_code == 200
    assert "Custom uploaded PDF is currently attached" in detail_custom.text

    # Clean up custom test app
    with get_session() as s:
        s.query(Application).filter(Application.id == ca_id).delete(synchronize_session=False)

    print("Phase 1-4 self-check PASSED: schema + intake + email draft + resume builder + ATS loop + review/edit + guarded send + manual apply + sent dashboard OK")


if __name__ == "__main__":
    main()
