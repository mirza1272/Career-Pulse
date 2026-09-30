import pytest
from app.db import get_session, init_db
from app.intake import create_application, parse_job_posting_with_llm
from app.knowledge import _cached_candidate
from app.models import Application
from app.resume_builder import select_projects, generate_tailored_skills_html, generate_role_tailored_summary
from app.tailor import tailor_application_resume


def test_raw_jd_exact_preservation():
    """Verify that user-uploaded JD text is preserved character-for-character without LLM rewriting."""
    init_db()
    raw_user_input = "We are looking for an AI Engineer with Python and FastAPI skills.\nContact us at careers@techcorp.com\nLocation: Remote"
    with get_session() as s:
        app = create_application(
            session=s,
            jd_text=raw_user_input,
            company="Tech Corp",
            job_title="AI Engineer",
        )
        assert app.jd_text == raw_user_input.strip()
        assert "## Role Overview" not in app.jd_text
        assert "## Key Responsibilities" not in app.jd_text
        assert "## Requirements & Qualifications" not in app.jd_text


def test_parse_job_posting_does_not_rewrite_jd_text():
    """Verify parse_job_posting_with_llm returns the clean input text as jd_text."""
    raw_text = "Urgent: Full Stack Web Developer needed with React and Node.js. Email: jobs@innovate.pk"
    parsed = parse_job_posting_with_llm(raw_text)
    assert parsed["jd_text"] == raw_text
    assert parsed["email"] == "jobs@innovate.pk"


def test_sparse_jd_ai_role_project_and_skills_selection():
    """Verify that a 1-line sparse JD for AI Engineer produces top AI projects and skills."""
    cand = _cached_candidate()
    sparse_jd = "AI Engineer position at Systems Limited."

    selected = select_projects(
        candidate=cand,
        variant_domain="all",
        jd_text=sparse_jd,
        job_title="AI Engineer",
        max_projects=5,
        use_llm=False,
    )
    names = [p.get("name") or p.get("short_name") for p in selected]
    assert any("Nemetron" in n or "Nexus" in n or "CivicHeat" in n for n in names)
    assert len(selected) == 5

    skills_html = generate_tailored_skills_html(
        candidate=cand,
        jd_text=sparse_jd,
        job_title="AI Engineer",
    )
    assert "AI" in skills_html or "Machine Learning" in skills_html or "Agentic" in skills_html
    assert "Python" in skills_html or "PyTorch" in skills_html or "FastAPI" in skills_html


def test_sparse_jd_frontend_role_selection():
    """Verify that a sparse JD for Frontend Developer selects React/Web projects and Frontend skills."""
    cand = _cached_candidate()
    sparse_jd = "Frontend Developer needed."

    selected = select_projects(
        candidate=cand,
        variant_domain="all",
        jd_text=sparse_jd,
        job_title="Frontend Developer",
        max_projects=5,
        use_llm=False,
    )
    names = [p.get("name") or p.get("short_name") for p in selected]
    assert any("HRConnect" in n for n in names)

    skills_html = generate_tailored_skills_html(
        candidate=cand,
        jd_text=sparse_jd,
        job_title="Frontend Developer",
    )
    assert "React" in skills_html or "JavaScript" in skills_html or "Frontend" in skills_html


def test_sparse_jd_general_software_engineer():
    """Verify that a sparse general Software Engineer role produces balanced top-tier resume content."""
    cand = _cached_candidate()
    sparse_jd = "Software Engineer - Fresh Graduate"

    summary = generate_role_tailored_summary(job_title="Software Engineer", jd_text=sparse_jd)
    assert "FAST National University" in summary
    assert "Software Engineer" in summary

    skills_html = generate_tailored_skills_html(
        candidate=cand,
        jd_text=sparse_jd,
        job_title="Software Engineer",
    )
    assert "Python" in skills_html or "C++" in skills_html
