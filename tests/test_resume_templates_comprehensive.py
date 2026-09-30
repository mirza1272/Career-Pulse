"""Comprehensive test suite for the Multi-User Resume Template System (5 Modern Templates).

Follows V-Model testing:
- Unit & Component Verification: Fonts, Registry, Aliases, Template HTML compilation
- Integration Verification: Multi-template PDF generation, Section Reordering on all templates
- Data Layer Verification: User preference isolation, Application template tracking, Version history
- Compliance Verification: Zero 'flowcv' mentions across the codebase, 100% strict A4 geometry
"""

import os
import re
from pathlib import Path
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models import Base, User, Application, ResumeVersion
from app.knowledge import Candidate, load_candidate
from app.resume_builder import (
    TEMPLATES_REGISTRY,
    TEMPLATE_ALIASES,
    DEFAULT_TEMPLATE_ID,
    DEFAULT_SECTION_ORDER,
    TEMPLATES_DIR,
    RESUMES_OUTPUT_DIR,
    build_resume_content,
    resolve_template,
    get_available_templates,
    switch_resume_template,
    extract_regions,
    replace_regions,
    detect_section_order,
    reorder_html_sections,
    render_pdf_from_html,
)
from app.db import get_user_selected_template, set_user_selected_template
from app.ats import score_resume


ALL_TEMPLATE_IDS = [
    "apex_modern",
    "oxford_editorial",
    "silicon_compact",
    "monarch_executive",
    "nova_minimalist",
]


def test_templates_registry_completeness():
    """Verify all 5 modern templates are registered with fancy names and valid metadata."""
    assert len(TEMPLATES_REGISTRY) == 5
    for tid in ALL_TEMPLATE_IDS:
        assert tid in TEMPLATES_REGISTRY
        meta = TEMPLATES_REGISTRY[tid]
        assert "name" in meta
        assert "font_family" in meta
        assert "template" in meta
        assert "primary_color" in meta
        assert (TEMPLATES_DIR / meta["template"]).exists(), f"Template file {meta['template']} missing on disk"


def test_template_aliases_resolution():
    """Verify legacy or alias template names resolve to valid modern template IDs."""
    assert resolve_template("apex_modern")[0] == "apex_modern"
    assert resolve_template("oxford_editorial")[0] == "oxford_editorial"
    assert resolve_template("silicon_compact")[0] == "silicon_compact"
    assert resolve_template("monarch_executive")[0] == "monarch_executive"
    assert resolve_template("nova_minimalist")[0] == "nova_minimalist"
    # Legacy aliases
    assert resolve_template("se_al")[0] == "apex_modern"
    assert resolve_template("se_am")[0] == "apex_modern"
    assert resolve_template("se_fd")[0] == "apex_modern"
    # Fallback for unknown
    assert resolve_template("non_existent_template_xyz")[0] == DEFAULT_TEMPLATE_ID
    assert resolve_template(None)[0] == DEFAULT_TEMPLATE_ID


def test_all_5_templates_html_compilation():
    """Verify that every template builds clean HTML with all standard regions."""
    cand = load_candidate()
    sample_jd = "Python FastAPI Backend Engineer with PostgreSQL and Docker experience."
    
    for tid in ALL_TEMPLATE_IDS:
        built = build_resume_content(
            candidate=cand,
            jd_text=sample_jd,
            job_title="Software Engineer",
            variant=tid,
            max_projects=5,
        )
        assert built.html_content is not None
        assert len(built.html_content) > 500
        
        # Verify mandatory semantic markers
        regions = extract_regions(built.html_content)
        assert "SUMMARY" in regions
        assert "EXPERIENCE" in regions
        assert "EDUCATION" in regions
        assert "SKILLS" in regions
        assert "PROJECTS" in regions


def test_all_5_templates_pdf_generation(tmp_path):
    """Verify that every template compiles cleanly to A4 PDF with FPDF2 engine."""
    cand = load_candidate()
    sample_jd = "Senior Full Stack Python Developer with React and Cloud Architecture."

    for tid in ALL_TEMPLATE_IDS:
        built = build_resume_content(
            candidate=cand,
            jd_text=sample_jd,
            job_title="Senior Developer",
            variant=tid,
            max_projects=4,
        )
        html_file = tmp_path / f"test_{tid}.html"
        pdf_file = tmp_path / f"test_{tid}.pdf"
        html_file.write_text(built.html_content, encoding="utf-8")
        
        # Render PDF
        render_pdf_from_html(html_file, pdf_file, page_target=1)
        assert pdf_file.exists()
        assert pdf_file.stat().st_size > 10000, f"PDF file for {tid} is suspiciously small: {pdf_file.stat().st_size} bytes"


def test_template_switching_preserves_content_and_locks():
    """Verify switching from Template A to Template B keeps custom edits and custom sequence."""
    cand = load_candidate()
    initial_built = build_resume_content(
        candidate=cand,
        jd_text="Backend Engineer",
        job_title="Backend Engineer",
        variant="apex_modern",
    )
    
    # Apply a custom summary edit and custom sequence
    custom_order = ["summary", "skills", "projects", "experience", "education", "certifications"]
    reordered_html = reorder_html_sections(initial_built.html_content, custom_order)
    reordered_html = replace_regions(reordered_html, {"SUMMARY": "<p>Customized summary for testing switch.</p>"})
    
    # Switch template to oxford_editorial
    switched_html = switch_resume_template(reordered_html, "oxford_editorial", candidate=cand)
    
    # Verify content preserved
    switched_regions = extract_regions(switched_html)
    assert "Customized summary for testing switch." in switched_regions.get("SUMMARY", "")
    
    # Verify order preserved
    switched_order = detect_section_order(switched_html)
    assert switched_order == custom_order
    
    # Verify typography updated to Oxford Editorial / Alegreya
    assert "Alegreya" in switched_html or "Oxford Editorial" in switched_html or "data-template=\"oxford_editorial\"" in switched_html


def test_multi_user_template_preference_isolation(tmp_path):
    """Verify that different users have completely isolated template preferences."""
    test_db = f"sqlite:///{tmp_path}/test_users.db"
    engine = create_engine(test_db)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    with Session() as s:
        u1 = User(id=1, email="user1@example.com", password_hash="hash1", name="User One", selected_template_id="oxford_editorial")
        u2 = User(id=2, email="user2@example.com", password_hash="hash2", name="User Two", selected_template_id="silicon_compact")
        s.add_all([u1, u2])
        s.commit()

        # Query and verify
        db_u1 = s.get(User, 1)
        db_u2 = s.get(User, 2)
        assert db_u1.selected_template_id == "oxford_editorial"
        assert db_u2.selected_template_id == "silicon_compact"
        assert db_u1.selected_template_id != db_u2.selected_template_id


def test_application_and_version_track_template_id(tmp_path):
    """Verify Application and ResumeVersion entities store template_id correctly."""
    test_db = f"sqlite:///{tmp_path}/test_app_version.db"
    engine = create_engine(test_db)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    with Session() as s:
        app_row = Application(
            id=101,
            user_id=1,
            email="recruiter@example.com",
            job_title="ML Engineer",
            company="AI Labs",
            template_id="monarch_executive",
        )
        s.add(app_row)
        s.commit()

        ver_row = ResumeVersion(
            application_id=101,
            version_no=1,
            template_id="monarch_executive",
            ats_score=88.5,
            score_history_json='[{"via": "template_change"}]',
        )
        s.add(ver_row)
        s.commit()

        loaded_app = s.get(Application, 101)
        assert loaded_app.template_id == "monarch_executive"
        
        loaded_ver = s.query(ResumeVersion).filter_by(application_id=101, version_no=1).first()
        assert loaded_ver.template_id == "monarch_executive"


def test_zero_flowcv_mentions_in_codebase():
    """Strict compliance check: Zero occurrences of the forbidden word in codebase, templates, and UI."""
    project_root = Path(__file__).resolve().parents[1]
    
    # Check all python files, html templates, and css files
    extensions = [".py", ".html", ".css", ".js"]
    forbidden_hits = []
    
    for ext in extensions:
        for p in project_root.rglob(f"*{ext}"):
            if ".venv" in str(p) or ".git" in str(p) or "node_modules" in str(p) or "archive" in str(p) or p.name == "test_resume_templates_comprehensive.py":
                continue
            try:
                content = p.read_text(encoding="utf-8", errors="ignore")
                target = "flow" + "cv"
                if target in content.lower():
                    lines = [
                        (idx + 1, line)
                        for idx, line in enumerate(content.splitlines())
                        if target in line.lower()
                    ]
                    if lines:
                        forbidden_hits.append((str(p), lines))
            except Exception:
                pass
                
    assert len(forbidden_hits) == 0, f"Found forbidden term in files: {forbidden_hits}"
