import pytest
from app.knowledge import Candidate
from app.resume_builder import generate_role_tailored_summary, build_safe_cs_summary, build_resume_content


def test_hybrid_summary_blends_kb_intro_with_jd_alignment():
    # User's authentic intro in Knowledge Base
    kb_bio = "Computer Science student at FAST National University passionate about software engineering and machine learning"
    candidate = Candidate(
        name="Haseeb Ur Rahman",
        email="haseeb@example.com",
        summary=kb_bio,
        skills=["Python", "FastAPI", "React", "PyTorch", "ChromaDB", "PostgreSQL", "RAG"],
    )

    # 1. AI/Agentic Role
    summary_ai = generate_role_tailored_summary(
        job_title="AI Engineer",
        jd_text="Seeking AI Engineer to build RAG pipelines, agentic workflows, and FastAPI LLM services.",
        candidate=candidate,
    )
    # Must contain the candidate's authentic intro from KB
    assert "Computer Science student at FAST National University" in summary_ai
    # Must contain role-specific technical specialization
    assert any(k in summary_ai for k in ["Agentic AI", "Retrieval-Augmented Generation", "RAG", "ChromaDB", "LLM"])
    # Must not contain exaggerated hallucinated senior titles
    assert "Solutions Architect" not in summary_ai
    assert "10+ years" not in summary_ai

    # 2. Frontend Role
    summary_fe = generate_role_tailored_summary(
        job_title="Frontend Developer",
        jd_text="React developer with Tailwind CSS and responsive UI expertise.",
        candidate=candidate,
    )
    assert "Computer Science student at FAST National University" in summary_fe
    assert any(k in summary_fe for k in ["React", "Tailwind", "Full-Stack & Frontend", "web applications"])

    # 3. Backend Role
    summary_be = generate_role_tailored_summary(
        job_title="Backend Python Engineer",
        jd_text="Python FastAPI backend developer building scalable microservices and databases.",
        candidate=candidate,
    )
    assert "Computer Science student at FAST National University" in summary_be
    assert any(k in summary_be for k in ["FastAPI", "Python", "REST API", "database", "Backend"])


def test_custom_kb_intro_update_propagates_to_built_resume():
    # Candidate updates their bio in KB to something specific
    custom_intro = "Final-year Computer Science undergraduate at FAST-NUCES building scalable backend systems"
    candidate = Candidate(
        name="Haseeb Ur Rahman",
        email="haseeb@example.com",
        summary=custom_intro,
        skills=["Python", "FastAPI", "Docker", "PostgreSQL"],
    )

    built = build_resume_content(
        candidate=candidate,
        jd_text="FastAPI backend engineer with Postgres and cloud experience.",
        job_title="Backend Engineer",
    )

    # The HTML summary must start with the candidate's custom intro from KB
    assert "Final-year Computer Science undergraduate at FAST-NUCES" in built.html_content
    assert "FastAPI" in built.html_content
