"""Tests for Monarch Executive template rendering, section preservation, and ResumeVersion persistence."""

import json
from pathlib import Path
import pytest

from app.db import get_session, init_db, record_resume_version, sync_from_supabase_to_memory
from app.knowledge import load_candidate
from app.models import Application, Base, ResumeVersion, User
from app.resume_builder import (
    build_resume_content,
    detect_section_order,
    extract_regions,
    reorder_html_sections,
    switch_resume_template,
)


def test_monarch_executive_reordering_preserves_all_sections():
    """Verify that reordering sections in monarch_executive does NOT wipe out content."""
    cand = load_candidate()
    built = build_resume_content(cand, "Looking for Python AI Engineer with FastAPI and RAG experience", job_title="AI Engineer", variant="monarch_executive")
    html = built.html_content

    assert "Haseeb Ur Rahman" in html
    assert "<!--REGION:SUMMARY-->" in html
    assert "<!--REGION:EXPERIENCE-->" in html
    assert "<!--REGION:SKILLS-->" in html
    assert "<!--REGION:PROJECTS-->" in html
    assert "<!--REGION:EDUCATION-->" in html

    # Test reordering
    target_order = ["skills", "projects", "experience", "education", "summary"]
    reordered = reorder_html_sections(html, target_order)

    # All regions must still exist in reordered output
    reordered_regions = extract_regions(reordered)
    assert reordered_regions.get("SUMMARY"), "Summary was lost during monarch reordering"
    assert reordered_regions.get("EXPERIENCE"), "Experience was lost during monarch reordering"
    assert reordered_regions.get("SKILLS"), "Skills were lost during monarch reordering"
    assert reordered_regions.get("PROJECTS"), "Projects were lost during monarch reordering"
    assert reordered_regions.get("EDUCATION"), "Education was lost during monarch reordering"


def test_switch_template_to_monarch_executive():
    """Verify switching template from apex_modern to monarch_executive keeps all content."""
    cand = load_candidate()
    apex_built = build_resume_content(cand, "Python and FastAPI developer", job_title="Backend Engineer", variant="apex_modern")
    
    monarch_html = switch_resume_template(apex_built.html_content, "monarch_executive", candidate=cand)
    assert "data-template=\"monarch_executive\"" in monarch_html or "Monarch" in monarch_html or "Amiri" in monarch_html
    assert "<!--REGION:SUMMARY-->" in monarch_html
    assert "<!--REGION:SKILLS-->" in monarch_html
    assert "<!--REGION:PROJECTS-->" in monarch_html

    monarch_regions = extract_regions(monarch_html)
    assert len(monarch_regions.get("SUMMARY", "")) > 20
    assert len(monarch_regions.get("SKILLS", "")) > 20


def test_resume_versions_hydration_from_disk(tmp_path, monkeypatch):
    """Verify sync_from_supabase_to_memory hydrates resume versions from disk."""
    import app.tailor
    monkeypatch.setattr(app.tailor, "RESUMES_OUTPUT_DIR", tmp_path)

    from app.db import _engine
    Base.metadata.create_all(_engine)
    app_id = 999
    # Create sample application files on disk
    v1_html = tmp_path / f"application_{app_id}_v1.html"
    v1_html.write_text("<html><body><!--REGION:SUMMARY-->V1<!--END:SUMMARY--></body></html>", encoding="utf-8")
    v2_html = tmp_path / f"application_{app_id}_v2.html"
    v2_html.write_text("<html><body><!--REGION:SUMMARY-->V2<!--END:SUMMARY--></body></html>", encoding="utf-8")
    main_html = tmp_path / f"application_{app_id}.html"
    main_html.write_text("<html><body><!--REGION:SUMMARY-->Current<!--END:SUMMARY--></body></html>", encoding="utf-8")

    with get_session() as s:
        # Create an Application in DB
        app_obj = Application(
            id=app_id,
            job_title="Full Stack Engineer",
            company="Test Corp",
            email="jobs@test.com",
            jd_text="Python React",
            resume_path=str(main_html),
            status="draft",
        )
        s.add(app_obj)
        s.commit()

        # Run sync_from_supabase_to_memory
        sync_from_supabase_to_memory(s)

        # Versions must now be hydrated in DB
        versions = s.query(ResumeVersion).filter_by(application_id=app_id).all()
        assert len(versions) >= 2
        ver_nos = [v.version_no for v in versions]
        assert 1 in ver_nos
        assert 2 in ver_nos
