import json
import pytest
from app.knowledge import load_candidate
from app.tailor import tailor_application_resume, interpret_user_custom_instructions
from app.resume_builder import extract_regions


JD_SAMPLE = """
Senior AI / Software Engineer
TechCorp
Requirements:
- Strong Python, FastAPI, Docker
- Experience with RAG, LLMs, LangChain
- Experience with React, Node.js, C++
- Databases: PostgreSQL, MongoDB
"""


def test_custom_focus_remove_skills_and_categories():
    candidate = load_candidate()
    prompt = "The skills section is too long, remove Web and C++ from it"
    
    # Pass existing manual locks as if user had edited before
    dummy_locks = {
        "SKILLS": "<li><strong>Frontend & Web</strong> — React.js, HTML, CSS</li><li><strong>Programming Languages</strong> — Python, C++</li>"
    }

    res = tailor_application_resume(
        application_id=101,
        jd_text=JD_SAMPLE,
        job_title="Senior AI Engineer",
        company="TechCorp",
        custom_focus=prompt,
        candidate=candidate,
        locked_regions=dummy_locks,
    )

    with open(res.resume_html_path, "r", encoding="utf-8") as f:
        html = f.read()

    regions = extract_regions(html)
    skills_html = regions.get("SKILLS", "")

    # C++ must not be in skills HTML
    assert "C++" not in skills_html
    # Frontend & Web category must not be in skills HTML
    assert "Frontend & Web" not in skills_html


def test_custom_focus_summary_highlight():
    candidate = load_candidate()
    prompt = "Highlight Agentic AI and RAG in my summary and make it 2 concise lines"

    # Pass an old manual lock on summary
    dummy_locks = {
        "SUMMARY": "Old generic summary before recreate."
    }

    res = tailor_application_resume(
        application_id=102,
        jd_text=JD_SAMPLE,
        job_title="Senior AI Engineer",
        company="TechCorp",
        custom_focus=prompt,
        candidate=candidate,
        locked_regions=dummy_locks,
    )

    with open(res.resume_html_path, "r", encoding="utf-8") as f:
        html = f.read()

    regions = extract_regions(html)
    summary_html = regions.get("SUMMARY", "")

    assert "Old generic summary" not in summary_html
    assert any(k in summary_html.lower() for k in ["agentic", "rag", "ai", "llm"])


def test_custom_focus_project_filtering():
    candidate = load_candidate()
    prompt = "Only keep AI and Machine Learning projects, remove HRConnect and Income Classification"

    res = tailor_application_resume(
        application_id=103,
        jd_text=JD_SAMPLE,
        job_title="Senior AI Engineer",
        company="TechCorp",
        custom_focus=prompt,
        candidate=candidate,
    )

    with open(res.resume_html_path, "r", encoding="utf-8") as f:
        html = f.read()

    regions = extract_regions(html)
    projects_html = regions.get("PROJECTS", "")

    assert "hrconnect" not in projects_html.lower()
    assert "income classification" not in projects_html.lower()


def test_custom_focus_add_skills_and_focus():
    candidate = load_candidate()
    prompt = "Add Docker and Kubernetes to skills, make summary focus on Senior AI Engineer"

    res = tailor_application_resume(
        application_id=104,
        jd_text=JD_SAMPLE,
        job_title="Senior AI Engineer",
        company="TechCorp",
        custom_focus=prompt,
        candidate=candidate,
    )

    with open(res.resume_html_path, "r", encoding="utf-8") as f:
        html = f.read()

    regions = extract_regions(html)
    skills_html = regions.get("SKILLS", "")
    summary_html = regions.get("SUMMARY", "")

    # Docker and Kubernetes should be present in skills
    assert "Docker" in skills_html
    assert "Kubernetes" in skills_html
    assert any(k in summary_html.lower() for k in ["ai", "engineer", "python", "developer"])


def test_custom_focus_roman_urdu():
    candidate = load_candidate()
    prompt = "web or c++ nikal do skills mn sy"

    res = tailor_application_resume(
        application_id=105,
        jd_text=JD_SAMPLE,
        job_title="Senior AI Engineer",
        company="TechCorp",
        custom_focus=prompt,
        candidate=candidate,
    )

    with open(res.resume_html_path, "r", encoding="utf-8") as f:
        html = f.read()

    regions = extract_regions(html)
    skills_html = regions.get("SKILLS", "")

    assert "C++" not in skills_html
    assert "Frontend & Web" not in skills_html
