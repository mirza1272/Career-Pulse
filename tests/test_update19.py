"""Update19 (2026-09-24) verification: audit-triage fixes.

Covers: HTML sanitizer, attestation leak fix, canonical display names,
link URL scheme allowlist, taxonomy cleanup, optimizer_brief seam,
OCR key hygiene.
"""
import inspect
import os
import re
import sys

import pytest

from app import tailor
from app.knowledge import Candidate
from app.resume_builder import (
    CANDIDATE_SKILL_CATEGORIES,
    canonical_display_name,
    generate_tailored_skills_html,
    sanitize_resume_html,
    skills_csv_to_html,
    update_links_html,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_update19_sanitizer():
    # --- 1. Sanitizer: stored-XSS defense --------------------------------------
    xss = '<script>alert(1)</script><p onclick="evil()">Hi <strong>there</strong></p><a href="javascript:alert(2)">x</a>'
    clean = sanitize_resume_html(xss)
    assert "<script" not in clean and "alert(1)" not in clean
    assert "onclick" not in clean
    assert "javascript:" not in clean
    assert "<p>" in clean and "<strong>there</strong>" in clean

    safe = '<p>Line one</p><ul><li>Item <em>one</em></li></ul><a href="https://example.com">ok</a>'
    clean2 = sanitize_resume_html(safe)
    assert "<ul>" in clean2 and 'href="https://example.com"' in clean2
    assert sanitize_resume_html("a < b & c") == "a &lt; b &amp; c"
    assert sanitize_resume_html("") == "" and sanitize_resume_html(None) == ""


def test_update19_attestation():
    # --- 2. Attestation leak: substring clause removed --------------------------
    cand = Candidate(
        name="T",
        email="t@t.com",
        skills=["Python", "GitHub", "Scikit-learn", "JavaScript", "React"],
    )
    html = generate_tailored_skills_html(
        cand,
        jd_text="need Git, ANN, Python, JavaScript, React, JS, HTML, CSS, Node.js, FastAPI, RAG, LangChain",
    )
    assert ">Git<" not in html and ">Git," not in html
    assert "ANN" not in html
    assert "JavaScript" in html
    assert "Python" in html


def test_update19_canonical_names():
    # --- 3. Canonical display names ---------------------------------------------
    assert canonical_display_name("ML") == "Machine Learning"
    assert canonical_display_name("js") == "JavaScript"
    assert canonical_display_name("Node") == "Node.js"
    assert canonical_display_name("html") == "HTML"
    assert canonical_display_name("Quantum++") == "Quantum++"

    csv_html = skills_csv_to_html("ML, Node, Js, Quantum++")
    assert "<li>Machine Learning</li>" in csv_html
    assert "<li>Node.js</li>" in csv_html
    assert "<li>Quantum++</li>" in csv_html
    assert "<li>ML</li>" not in csv_html and "<li>Js</li>" not in csv_html


def test_update19_links_and_seams():
    # --- 4. Link URL scheme allowlist -------------------------------------------
    base_links = '<a href="https://old.com">Portfolio</a>'
    out = update_links_html(base_links, {"Evil": "javascript:alert(1)", "Good": "https://new.com"})
    assert "javascript:" not in out
    assert "https://new.com" in out

    # --- 6. optimizer_brief seam --------------------------------------------------
    sig = inspect.signature(tailor.tailor_application_resume)
    assert "optimizer_brief" in sig.parameters
    ref_sig = inspect.signature(tailor._ask_llm_for_refinement)
    assert "optimizer_brief" in ref_sig.parameters

    # --- 7. OCR key hygiene (source-level, code lines only — not comments) ---------
    ocr_code = [
        ln
        for ln in open(os.path.join(ROOT, "app/ocr.py")).read().splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    assert not any('"helloworld"' in ln or "'helloworld'" in ln for ln in ocr_code)
    assert any("api_keys = [config.OCR_SPACE_API_KEY]" in ln for ln in ocr_code)

    cfg_src = open(os.path.join(ROOT, "app/config.py")).read()
    cfg_code = [ln for ln in cfg_src.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    bad_defaults = [
        ln for ln in cfg_code if 'os.environ.get("OCR_SPACE_API_KEY"' in ln and ', "")' not in ln
    ]
    assert not bad_defaults

    # --- 8. main.py seams (source-level) ------------------------------------------
    main_src = open(os.path.join(ROOT, "app/main.py")).read()
    assert re.search(r"\n    def new_submit\(", main_src) is not None
    assert "await jd_image.read()" not in main_src
    assert "RADAR_STATUS_ALLOWLIST" in main_src
    assert main_src.count("sync_app_to_supabase(row, s)") >= 2
    assert "Content-Security-Policy" in main_src
    assert main_src.count("_atomic_write_text(") >= 6
    assert "_CLOUD_HYDRATION_TTL" in main_src
    assert '@app.get("/logout")' not in main_src

    base_src = open(os.path.join(ROOT, "templates/base.html")).read()
    assert "window.escapeHtml = function" in base_src
    assert "safeDescription" in base_src

    app_html_src = open(os.path.join(ROOT, "templates/application.html")).read()
    assert "safeRecipient" in app_html_src and "safeSubject" in app_html_src
    assert "escHtml(recipientEmail" in app_html_src
    assert "${recipientEmail}" not in app_html_src
    assert "${emailSubject}" not in app_html_src
    assert "safeSkillName" in app_html_src
