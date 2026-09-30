"""Phase 6 checks: generation seams - content/render split, template registry."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

from app.knowledge import load_candidate  # noqa: E402
from app.resume_builder import (  # noqa: E402
    ResumeContent,
    build_resume_content,
    build_resume_sections,
    extract_regions,
    render_resume,
    resolve_template,
)

JD = "We seek a Machine Learning Engineer with Python, TensorFlow and CNN experience."


def test_generation_seams():
    candidate = load_candidate()

    # template registry seam
    tid, info = resolve_template("oxford_editorial")
    assert tid == "oxford_editorial" and info["template"] == "oxford_editorial.html"
    
    tid, info = resolve_template("nope")
    assert tid == "apex_modern" and info["template"] == "apex_modern.html"
    
    tid, _ = resolve_template(None)
    assert tid == "apex_modern"

    # content half
    content = build_resume_sections(candidate, JD, job_title="ML Engineer")
    assert isinstance(content, ResumeContent)
    assert all([content.summary_html, content.skills_html, content.projects_html, content.education_html, content.header_title])
    assert content.template_id in ("apex_modern", "oxford_editorial", "silicon_compact", "monarch_executive", "nova_minimalist", "se_al", "se_am", "se_fd")
    assert len(content.selected_projects) > 0

    # render half
    html = render_resume(content, candidate)
    assert "<html" in html.lower() and len(html) > 5000
    assert content.header_title in html and "Haseeb Ur Rahman" in html
    assert render_resume(content, candidate) == render_resume(content, candidate)

    # orchestrator == manual composition
    built = build_resume_content(
        candidate,
        JD,
        job_title="ML Engineer",
        summary_override="Deterministic summary for ML Engineer",
        skills_override_html="<li><strong>Skills</strong> — Python, PyTorch</li>",
        projects_override_html="<div class='proj'>Project Alpha</div>",
        max_projects=1,
    )
    manual_sections = build_resume_sections(
        candidate,
        JD,
        job_title="ML Engineer",
        summary_override="Deterministic summary for ML Engineer",
        skills_override_html="<li><strong>Skills</strong> — Python, PyTorch</li>",
        projects_override_html="<div class='proj'>Project Alpha</div>",
        max_projects=1,
    )
    manual_html = render_resume(manual_sections, candidate)
    assert extract_regions(built.html_content) == extract_regions(manual_html)
    assert len(built.resume_text) > 1000 and "Haseeb" in built.resume_text

    # unknown variant override falls back cleanly
    built2 = build_resume_content(candidate, JD, job_title="ML Engineer", variant="nope")
    assert built2.variant == "apex_modern" and len(built2.html_content) > 5000

    # explicit variant honored
    built3 = build_resume_content(candidate, JD, job_title="ML Engineer", variant="monarch_executive")
    assert built3.variant == "monarch_executive"

