import os
import pytest
from app.knowledge import load_candidate
from app.resume_builder import (
    render_resume,
    build_resume_sections,
    detect_section_order,
    reorder_html_sections,
    rebuild_resume_from_custom_edits,
    DEFAULT_SECTION_ORDER,
)
from app.tailor import tailor_application_resume, interpret_user_custom_instructions


def test_detect_and_reorder_html_sections():
    candidate = load_candidate()
    sections = build_resume_sections(candidate=candidate, jd_text="Python AI Engineer", job_title="AI Engineer", max_projects=5)
    
    # Render with default order
    default_html = render_resume(sections, section_order=DEFAULT_SECTION_ORDER)
    detected = detect_section_order(default_html)
    assert detected == DEFAULT_SECTION_ORDER

    # Reorder HTML directly: Experience first, Skills second, Projects third, Education fourth
    custom_order = ["summary", "experience", "skills", "projects", "education", "certifications"]
    reordered_html = reorder_html_sections(default_html, custom_order)
    new_detected = detect_section_order(reordered_html)
    assert new_detected == custom_order

    # Check that headings appear in the new order in the HTML string
    pos_exp = reordered_html.find("Employment History")
    pos_skills = reordered_html.find("Skills")
    pos_proj = reordered_html.find("Projects")
    pos_edu = reordered_html.find("Education")

    assert pos_exp != -1 and pos_skills != -1 and pos_proj != -1 and pos_edu != -1
    assert pos_exp < pos_skills < pos_proj < pos_edu


def test_rebuild_from_custom_edits_with_section_order():
    candidate = load_candidate()
    sections = build_resume_sections(candidate=candidate, jd_text="Full Stack Engineer", job_title="Full Stack Engineer", max_projects=4)
    base_html = render_resume(sections)

    custom_order = ["summary", "skills", "projects", "experience", "education", "certifications"]
    
    rebuilt_res = rebuild_resume_from_custom_edits(
        base_html=base_html,
        candidate=candidate,
        selected_categories=["Programming Languages", "Frameworks & Libraries"],
        selected_project_ids=[p.get("id") or p.get("name") for p in candidate.projects[:4]],
        section_order=custom_order,
    )

    detected = detect_section_order(rebuilt_res)
    assert detected == custom_order

    raw_html = rebuilt_res.html_content
    pos_skills = raw_html.find("Skills")
    pos_proj = raw_html.find("Projects")
    pos_exp = raw_html.find("Employment History")
    pos_edu = raw_html.find("Education")

    assert pos_skills < pos_proj < pos_exp < pos_edu


def test_tailor_application_resume_with_custom_section_order():
    candidate = load_candidate()
    jd_text = """
    Software Engineer
    Requirements:
    - Experience in Python, Django, React
    - Great team player and project leader
    """
    
    custom_order = ["summary", "projects", "skills", "experience", "education", "certifications"]
    
    res = tailor_application_resume(
        application_id=999,
        jd_text=jd_text,
        job_title="Software Engineer",
        company="Startup Co",
        candidate=candidate,
        section_order=custom_order,
        max_projects=4,
    )

    with open(res.resume_html_path, "r", encoding="utf-8") as f:
        html = f.read()

    detected = detect_section_order(html)
    assert detected == custom_order
    assert os.path.exists(res.resume_pdf_path)


def test_interpret_user_custom_instructions_detects_order():
    candidate = load_candidate()
    jd = "Software Engineer at TechCorp"
    
    # 1. Experience first
    parsed1 = interpret_user_custom_instructions(
        "Put work experience first before skills and projects",
        candidate=candidate,
        jd_text=jd,
    )
    assert parsed1.get("section_order") is not None
    assert parsed1["section_order"][0] == "summary"
    assert parsed1["section_order"][1] == "experience"

    # 2. Skills first
    parsed2 = interpret_user_custom_instructions(
        "Make skills first right after profile summary, then projects",
        candidate=candidate,
        jd_text=jd,
    )
    assert parsed2.get("section_order") is not None
    assert parsed2["section_order"][0] == "summary"
    assert parsed2["section_order"][1] == "skills"

    # 3. Projects first
    parsed3 = interpret_user_custom_instructions(
        "Projects first layout, then experience then skills",
        candidate=candidate,
        jd_text=jd,
    )
    assert parsed3.get("section_order") is not None
    assert parsed3["section_order"][0] == "summary"
    assert parsed3["section_order"][1] == "projects"
