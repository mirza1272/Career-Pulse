"""Generated skill suggestions must never become new candidate claims."""
import html
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.knowledge import Candidate, load_candidate_for_user
from app.resume_builder import canonical_display_name, generate_tailored_skills_html


def displayed_skills(markup):
    return [
        html.unescape(skill.strip())
        for row in re.findall(r"</strong> — (.*?)</li>", markup)
        for skill in row.split(",")
    ]


@pytest.mark.parametrize("variant", [
    "apex_modern", "oxford_editorial", "silicon_compact", "monarch_executive",
    "nova_minimalist", "se_al", "se_am", "se_fd",
])
def test_suggestions_cannot_add_skills_or_expand_kb_labels(variant):
    candidate = Candidate(skills=["python", "machine learning", "git", "fastapi"])
    original = list(candidate.skills)
    output = generate_tailored_skills_html(
        candidate,
        "Need Python, Machine Learning Fundamentals, OpenAI, Kubernetes, AWS",
        variant=variant,
        extra_target_skills=[
            "Machine Learning Fundamentals", "OpenAI", "Kubernetes", "AWS",
            "Python", "GitHub",
        ],
    )
    skills = displayed_skills(output)
    assert set(skills) == {"Python", "Machine Learning", "Git", "FastAPI"}
    assert candidate.skills == original


def test_empty_kb_stays_empty_despite_jd_and_suggestions():
    assert generate_tailored_skills_html(
        Candidate(skills=[]), "Python AWS Docker",
        extra_target_skills=["Python", "AWS", "Docker"],
    ) == ""


@pytest.mark.parametrize("stored", ["{}", "", '{"skills": []}'])
def test_empty_admin_kb_does_not_import_yaml_skills(stored):
    from app import config

    user = SimpleNamespace(id=1, email=config.ADMIN_EMAIL, name="Account", knowledge_base_json=stored)
    candidate = load_candidate_for_user(user)
    assert candidate.skills == []
    assert candidate.experience == []
    assert candidate.projects == []


def test_priorities_only_reorder_existing_skills_and_respect_exclusions():
    candidate = Candidate(skills=["python", "js", "sql"])
    output = generate_tailored_skills_html(
        candidate, "", extra_target_skills=["JavaScript", "Docker"],
        max_skills_per_cat=1,
    )
    assert displayed_skills(output) == ["JavaScript"]
    excluded = generate_tailored_skills_html(
        candidate, "", extra_target_skills=["Python", "Docker"],
        excluded_skills=["python"],
    )
    assert set(displayed_skills(excluded)) == {"JavaScript", "SQL"}


@pytest.mark.parametrize("typed,display", [
    ("python", "Python"), ("PYTHON", "Python"), ("fastapi", "FastAPI"),
    ("pytorch", "PyTorch"), ("tensorflow", "TensorFlow"),
    ("javascript", "JavaScript"), ("sql", "SQL"), ("mongodb", "MongoDB"),
    ("mlops", "MLOps"), ("graphql", "GraphQL"), ("grpc", "gRPC"),
    ("ci/cd", "CI/CD"), ("workflow automation", "Workflow Automation"),
    ("data validation", "Data Validation"), ("containerization", "Containerization"),
    ("data visualization", "Data Visualization"), ("classification", "Classification"),
    ("custom tooling", "Custom Tooling"),
])
def test_formal_capitalization_does_not_modify_kb(typed, display):
    candidate = Candidate(skills=[typed])
    assert canonical_display_name(typed) == display
    assert displayed_skills(generate_tailored_skills_html(candidate, "")) == [display]
    assert candidate.skills == [typed]


def test_tailoring_and_pdf_keep_only_kb_skills(tmp_path, monkeypatch):
    from pypdf import PdfReader
    from app import tailor
    from app.resume_builder import extract_regions

    candidate = Candidate(
        name="Skill Source Test", email="skills@example.test",
        summary="Software engineer building reliable services.",
        skills=["python", "machine learning", "data validation", "git"],
    )
    monkeypatch.setattr(tailor, "RESUMES_OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(tailor.config, "LLM_API_KEY", "mock-only")
    monkeypatch.setattr(tailor, "_analyze_jd_and_skills_with_llm", lambda **kw: {
        "priority_skills": ["Machine Learning Fundamentals", "GitHub", "Pydantic", "Python"],
    })
    monkeypatch.setattr(tailor, "_ask_llm_for_refinement", lambda **kw: {
        "summary": candidate.summary, "extra_skills": ["Docker", "Kubernetes"],
    })
    result = tailor.tailor_application_resume(
        application_id="skills-regression", candidate=candidate,
        jd_text="Python engineer with Pydantic, GitHub, Docker and Kubernetes",
        job_title="Software Engineer", target_score=100, max_attempts=2,
    )
    skills_html = extract_regions(Path(result.resume_html_path).read_text())["SKILLS"]
    assert set(displayed_skills(skills_html)) == {
        "Python", "Machine Learning", "Data Validation", "Git",
    }
    assert result.resume_pdf_path
    pdf_text = "\n".join(page.extract_text() for page in PdfReader(result.resume_pdf_path).pages)
    pdf_skills = pdf_text.split("SKILLS", 1)[1].split("PROJECTS", 1)[0]
    for invented in ("Machine Learning Fundamentals", "GitHub", "Pydantic", "Docker", "Kubernetes"):
        assert invented not in pdf_skills
    assert "Python" in pdf_skills and "Data Validation" in pdf_skills
