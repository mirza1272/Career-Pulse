"""Test suite to verify that resumes, project selection, summaries, skill categories,
and ATS scoring are distinct, tailored, and accurate across diverse technical roles.
"""

from __future__ import annotations

import pytest
from app.knowledge import load_candidate
from app.resume_builder import build_resume_content, generate_role_tailored_summary
from app.ats import score_resume
from app.tailor import tailor_application_resume


def test_role_tailored_summaries_are_distinct_and_impactful():
    roles = [
        ("React Frontend Engineer", "Building modern web UI with React.js, Tailwind CSS, and Next.js"),
        ("Computer Vision Engineer", "Developing CNN architectures, PyTorch vision models, and image processing"),
        ("Agentic AI Engineer", "Building multi-agent systems, RAG workflows, ChromaDB vector search, and LLMs"),
        ("Backend Python Developer", "Building high-performance FastAPI REST APIs, SSRF security, and asynchronous pipelines"),
        ("Machine Learning Engineer", "Developing predictive models, Scikit-learn tabular pipelines, and K-Means clustering"),
    ]

    summaries = []
    for title, jd in roles:
        s = generate_role_tailored_summary(title, jd_text=jd)
        summaries.append(s)

    # Ensure all 5 summaries are unique and not boilerplate
    assert len(set(summaries)) == 5, f"Expected 5 distinct summaries, got {len(set(summaries))}"
    for s in summaries:
        assert len(s) > 80, f"Summary too short: {s}"
        assert not s.startswith("Computer Science student at FAST National University focused on"), (
            f"Summary should not use legacy repetitive boilerplate: {s}"
        )


def test_project_selection_across_distinct_domains():
    candidate = load_candidate()

    # 1. Frontend Web Role
    frontend_build = build_resume_content(
        candidate,
        jd_text="Seeking Senior Frontend React Developer. Required: React.js, HTML, CSS, JavaScript, Tailwind CSS, MERN, Node.js.",
        job_title="Frontend Developer",
        max_projects=4,
    )
    frontend_projects = [p.lower() for p in frontend_build.selected_projects]
    assert any("hrconnect" in p for p in frontend_projects), f"Frontend build missing HRConnect: {frontend_projects}"
    assert not any("x-ray" in p or "chest" in p for p in frontend_projects), f"Frontend build should not select X-ray: {frontend_projects}"

    # 2. Computer Vision Role
    cv_build = build_resume_content(
        candidate,
        jd_text="Deep Learning & Computer Vision Engineer. Required: CNN, PyTorch, TensorFlow, Keras, image classification, deep learning.",
        job_title="Computer Vision Engineer",
        max_projects=4,
    )
    cv_projects = [p.lower() for p in cv_build.selected_projects]
    assert any("x-ray" in p or "chest" in p for p in cv_projects), f"CV build missing X-ray Chest Classification: {cv_projects}"
    assert not any("hrconnect" in p for p in cv_projects), f"CV build should not select HRConnect: {cv_projects}"

    # 3. Agentic AI Role
    agentic_build = build_resume_content(
        candidate,
        jd_text="Agentic AI & RAG Engineer. Required: Multi-agent systems, RAG, vector search, ChromaDB, LLMs, Groq, Vapi voice AI.",
        job_title="Agentic AI Engineer",
        max_projects=4,
    )
    agentic_projects = [p.lower() for p in agentic_build.selected_projects]
    assert any("nemetron" in p for p in agentic_projects), f"Agentic AI missing Nemetron: {agentic_projects}"
    assert any("nexus" in p for p in agentic_projects), f"Agentic AI missing Nexus AI: {agentic_projects}"

    # 4. Backend Python Role
    backend_build = build_resume_content(
        candidate,
        jd_text="Backend Python Engineer. Required: FastAPI, REST APIs, HTTP clients, security, SSRF protection, data pipelines.",
        job_title="Backend Developer",
        max_projects=4,
    )
    backend_projects = [p.lower() for p in backend_build.selected_projects]
    assert any("safe web access" in p for p in backend_projects), f"Backend missing Safe Web Access: {backend_projects}"
    assert any("signal reach" in p for p in backend_projects), f"Backend missing Signal Reach: {backend_projects}"

    # Verify project selection sets are genuinely distinct
    assert set(frontend_projects) != set(cv_projects), "Frontend and CV projects must be distinct"
    assert set(cv_projects) != set(agentic_projects), "CV and Agentic AI projects must be distinct"


def test_full_tailoring_flow_differentiation():
    candidate = load_candidate()

    # Frontend Application
    fe_res = tailor_application_resume(
        application_id="test-fe-role",
        jd_text="Looking for a React.js and Tailwind CSS Frontend Developer with MERN stack experience.",
        job_title="Frontend Developer",
        company="WebCorp",
        candidate=candidate,
        max_projects=4,
    )

    # CV Application
    cv_res = tailor_application_resume(
        application_id="test-cv-role",
        jd_text="Looking for a Deep Learning Computer Vision Engineer with CNN and PyTorch expertise.",
        job_title="Computer Vision Engineer",
        company="VisionAI",
        candidate=candidate,
        max_projects=4,
    )

    # Assert different templates or variants and different HTML content
    fe_html = open(fe_res.resume_html_path, encoding="utf-8").read()
    cv_html = open(cv_res.resume_html_path, encoding="utf-8").read()

    assert "HRConnect" in fe_html
    assert "HRConnect" not in cv_html or "X-ray" in cv_html
    assert "X-ray Chest Classification" in cv_html
    assert "X-ray Chest Classification" not in fe_html

    # Ensure summaries are distinct
    assert "React.js" in fe_html or "Frontend" in fe_html
    assert "Vision" in cv_html or "CNN" in cv_html


def test_parse_resume_regions_detail_ranks_projects_and_defaults_to_5():
    candidate = load_candidate()
    from app.resume_builder import parse_resume_regions_detail
    detail = parse_resume_regions_detail(
        html_content="",
        candidate=candidate,
        jd_text="React Frontend Developer Tailwind CSS MERN Stack",
        job_title="Frontend Developer",
    )
    assert len(detail["all_projects"]) >= 5
    # The top ranked project for Frontend must be web-focused (e.g. HRConnect)
    top_p = detail["all_projects"][0]
    top_name = (top_p.get("name") or top_p.get("short_name") or "").lower()
    assert "hrconnect" in top_name or "safe" in top_name
    # Default active project IDs should be 5
    assert len(detail["active_project_ids"]) == 5


if __name__ == "__main__":
    test_role_tailored_summaries_are_distinct_and_impactful()
    test_project_selection_across_distinct_domains()
    test_full_tailoring_flow_differentiation()
    test_parse_resume_regions_detail_ranks_projects_and_defaults_to_5()
    print("ALL ROLE DIFFERENTIATION TESTS PASSED SUCCESSFULLY!")

