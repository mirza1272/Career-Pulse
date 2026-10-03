"""Runnable checks for resume PDF parsing (Phase 2: app/resume_parse.py + import route).

- extract_pdf_text: real PDF (data/resumes/application_1.pdf), rejects non-PDF/empty.
- structure_resume_text: mocked LLM -> normalised KB shape; LLM failure -> None.
- POST /knowledge-base/import: prefilled review page; never saves by itself.

Run with:  AUTH_PASSWORD=<pw> python tests/test_resume_parse.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from fastapi.testclient import TestClient

from app import config
from app.auth import create_session_token
from app.db import get_session, init_db
from app.main import app
from app.models import User
import app.llm as llm_module
from app.resume_parse import extract_docx_text, extract_pdf_text, extract_resume_text, structure_resume_text

SAMPLE_PDF = ROOT / "data" / "resumes" / "application_1.pdf"


def test_extract_real_pdf():
    assert SAMPLE_PDF.exists(), f"sample PDF missing: {SAMPLE_PDF}"
    text = extract_pdf_text(SAMPLE_PDF.read_bytes())
    assert len(text) >= 20, f"too little text extracted: {len(text)}"
    print(f"PASS extract real PDF ({len(text)} chars)")


def test_extract_rejects_bad_input():
    for bad, label in [(b"", "empty"), (b"%PDF-1.4\n%junk with no text objects", "unreadable")]:
        try:
            extract_pdf_text(bad)
        except RuntimeError as exc:
            print(f"PASS reject {label}: {exc}")
        else:
            raise AssertionError(f"{label} input should raise RuntimeError")


def test_extract_resume_text_multiformat():
    # Plain text / markdown
    txt_bytes = b"Haseeb Ur Rahman\nAI Engineer\nSkills: Python, FastAPI, PyTorch, Docker, RAG\n" + b"Experience: ML Dev at Tech Co\n" * 5
    txt = extract_resume_text(txt_bytes, "resume.txt")
    assert "Haseeb Ur Rahman" in txt
    assert "FastAPI" in txt
    print("PASS extract_resume_text for plain text / markdown")


def test_structure_normalises():
    orig = llm_module.call_llm_json
    llm_module.call_llm_json = lambda *a, **k: {
        "name": "Jane Doe", "email": "jane@example.com", "junk_field": 123,
        "links": {"linkedin": "https://linkedin.com/in/jane", "github": ""},
        "skills": ["Python", "", 42, "FastAPI"],
        "experience": [{"employer": "Acme", "title": "Dev"}],
        "projects": [{"name": "P1"}],
    }
    try:
        out = structure_resume_text("Jane Doe\nPython developer...\n" + "x" * 100)
    finally:
        llm_module.call_llm_json = orig
    assert out["name"] == "Jane Doe"
    assert out["links"] == {"linkedin": "https://linkedin.com/in/jane", "github": "", "portfolio": ""}
    assert "Python" in out["skills"] and "FastAPI" in out["skills"]
    assert any(e["employer"] == "Acme" for e in out["experience"])
    assert "junk_field" not in out
    print("PASS structure normalises to KB shape")


def test_structure_llm_failure_uses_heuristic_fallback():
    orig = llm_module.call_llm_json
    llm_module.call_llm_json = lambda *a, **k: None
    sample_text = (
        "Haseeb Ur Rahman\n"
        "haseeb@example.com | +92 300 1234567 | Lahore, Pakistan\n"
        "https://linkedin.com/in/haseeb-ur-rehman https://github.com/haseeb\n\n"
        "Professional Summary\n"
        "AI and Machine Learning Engineer specializing in Agentic AI, RAG, and FastAPI.\n\n"
        "Technical Skills\n"
        "Python, FastAPI, PyTorch, TensorFlow, Docker, PostgreSQL, ChromaDB, RAG, LLMs\n\n"
        "Work Experience\n"
        "AI Engineer | Contour Software | 2023 - Present\n"
        "• Built RAG pipelines with ChromaDB and FastAPI\n"
        "• Deployed PyTorch models on Docker\n\n"
        "Education\n"
        "FAST National University of Computer and Emerging Sciences\n"
        "Bachelor of Science in Computer Science | 2021 - 2025\n"
    )
    try:
        out = structure_resume_text(sample_text)
    finally:
        llm_module.call_llm_json = orig
    assert out is not None, "Heuristic fallback should never return None for valid resume text"
    assert out["name"] == "Haseeb Ur Rahman"
    assert out["email"] == "haseeb@example.com"
    assert "FastAPI" in out["skills"]
    assert "PyTorch" in out["skills"]
    assert len(out["experience"]) >= 1
    assert "FAST" in out["education"][0]["institution"]
    print("PASS heuristic fallback populates all fields when LLM fails")


def _authed_client():
    init_db()
    with get_session() as s:
        admin = s.query(User).filter(User.email == config.ADMIN_EMAIL.strip().lower()).first()
        assert admin, "admin user missing (set AUTH_PASSWORD so bootstrap runs)"
        uid, email = admin.id, admin.email
    token = create_session_token(uid, email)
    client = TestClient(app)
    client.cookies.set("careerpulse_auth", token)
    return client, uid


def test_import_route_prefills_review():
    client, uid = _authed_client()
    orig = llm_module.call_llm_json
    llm_module.call_llm_json = lambda *a, **k: {
        "name": "Parsed Person", "email": "parsed@example.com", "phone": "",
        "location": "Lahore, Pakistan", "summary": "AI engineer",
        "links": {"linkedin": "", "github": "https://github.com/pp", "portfolio": ""},
        "skills": ["Python", "LLMs"], "experience": [], "projects": [],
    }
    try:
        with open(SAMPLE_PDF, "rb") as f:
            r = client.post("/knowledge-base/import",
                            files={"resume_pdf": ("resume.pdf", f, "application/pdf")})
    finally:
        llm_module.call_llm_json = orig
    assert r.status_code == 200, r.status_code
    assert "Parsed Person" in r.text, "parsed name not prefilled in review form"
    assert "Resume parsed" in r.text, "review banner missing"
    # The import must NOT have saved anything by itself.
    with get_session() as s:
        u = s.get(User, uid)
        kb = (u.knowledge_base_json or "")
        assert "Parsed Person" not in kb, "import route saved KB data without user review!"
    print("PASS import route prefills review form and saves nothing")


def test_project_subtitles_and_live_links_extraction():
    sample_text = (
        "Haseeb Ur Rahman Full-Stack Developer\n"
        "+92 303 8607925 • mirzahaseeb0566@gmail.com • LinkedIn • GitHub • Portfolio\n\n"
        "SKILLS\n"
        "• Backend, Cloud & APIs — FastAPI, Node.js, Express.js\n"
        "• Programming Languages — Python, JavaScript, SQL\n\n"
        "PROJECTS\n"
        "HRConnect, ATS System\n"
        "• Built a MERN-based recruitment system for managing job postings across multiple\n"
        "branches and admin panels.\n"
        "Signal Reach, Agentic AI Lead Generation Platform\n"
        "• Developed an Agentic AI lead generation platform that automates prospect research.\n"
        "Safe Web Access, Secure Web Fetching & AI Data Ingestion Package\n"
        "• Developed and published a Python package for secure web access in AI agents.\n"
    )
    sample_links = [
        "tel:+923038607925",
        "mailto:mirzahaseeb0566@gmail.com",
        "https://www.linkedin.com/in/haseeb-ur-rahman-8b152234b",
        "https://github.com/mirza1272",
        "https://mirzahaseeb.me/",
        "https://hrconnect-ats.vercel.app/",
        "https://github.com/mirza1272/Signal-Reach",
        "https://pypi.org/project/safe-web-access/",
    ]

    out = structure_resume_text(sample_text, sample_links)
    assert out["name"] == "Haseeb Ur Rahman"
    assert out["email"] == "mirzahaseeb0566@gmail.com"
    assert out["links"]["linkedin"] == "https://www.linkedin.com/in/haseeb-ur-rahman-8b152234b"
    assert out["links"]["github"] == "https://github.com/mirza1272"
    assert out["links"]["portfolio"] == "https://mirzahaseeb.me/"

    assert len(out["projects"]) == 3
    p1 = out["projects"][0]
    assert p1["name"] == "HRConnect"
    assert p1["subtitle"] == "ATS System"
    assert p1["link"] == "https://hrconnect-ats.vercel.app/"

    p2 = out["projects"][1]
    assert p2["name"] == "Signal Reach"
    assert p2["subtitle"] == "Agentic AI Lead Generation Platform"
    assert p2["link"] == "https://github.com/mirza1272/Signal-Reach"

    p3 = out["projects"][2]
    assert p3["name"] == "Safe Web Access"
    assert p3["subtitle"] == "Secure Web Fetching & AI Data Ingestion Package"
    assert p3["link"] == "https://pypi.org/project/safe-web-access/"
    print("PASS project subtitles and live links extracted accurately")


if __name__ == "__main__":
    test_extract_real_pdf()
    test_extract_rejects_bad_input()
    test_extract_resume_text_multiformat()
    test_structure_normalises()
    test_structure_llm_failure_uses_heuristic_fallback()
    test_project_subtitles_and_live_links_extraction()
    test_import_route_prefills_review()
    print("ALL RESUME PARSE CHECKS PASSED")
