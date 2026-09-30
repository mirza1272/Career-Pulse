"""Update19 (2026-09-24) verification: audit-triage fixes.

Run: /home/hatch/workspace/.venv-careerpulse/bin/python tests/test_update19.py
Covers: HTML sanitizer, attestation leak fix, canonical display names,
link URL scheme allowlist, taxonomy cleanup, optimizer_brief seam,
OCR key hygiene.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

failures = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


from app.knowledge import Candidate  # noqa: E402
from app.resume_builder import (  # noqa: E402
    CANDIDATE_SKILL_CATEGORIES,
    canonical_display_name,
    generate_tailored_skills_html,
    sanitize_resume_html,
    skills_csv_to_html,
    update_links_html,
)

# --- 1. Sanitizer: stored-XSS defense --------------------------------------
xss = '<script>alert(1)</script><p onclick="evil()">Hi <strong>there</strong></p><a href="javascript:alert(2)">x</a>'
clean = sanitize_resume_html(xss)
check("sanitizer strips <script>", "<script" not in clean and "alert(1)" not in clean, clean)
check("sanitizer strips event handlers", "onclick" not in clean, clean)
check("sanitizer strips javascript: URLs", "javascript:" not in clean, clean)
check("sanitizer keeps formatting", "<p>" in clean and "<strong>there</strong>" in clean, clean)

safe = '<p>Line one</p><ul><li>Item <em>one</em></li></ul><a href="https://example.com">ok</a>'
clean2 = sanitize_resume_html(safe)
check("sanitizer keeps safe markup", "<ul>" in clean2 and 'href="https://example.com"' in clean2, clean2)

check("sanitizer escapes bare text", sanitize_resume_html("a < b & c") == "a &lt; b &amp; c")
check("sanitizer empty-safe", sanitize_resume_html("") == "" and sanitize_resume_html(None) == "")

# --- 2. Attestation leak: substring clause removed --------------------------
cand = Candidate(
    name="T", email="t@t.com",
    skills=["Python", "GitHub", "Scikit-learn", "JavaScript", "React"],
)
html = generate_tailored_skills_html(cand, jd_text="need Git, ANN, Python, JavaScript, React, JS, HTML, CSS, Node.js, FastAPI, RAG, LangChain")
check("GitHub does NOT attest Git", ">Git<" not in html and ">Git," not in html, html[:200])
check("Scikit-learn does NOT attest ANN", "ANN" not in html, html[:200])
check("JS attests JavaScript (alias)", "JavaScript" in html, html[:200])
check("Python attested", "Python" in html)

# --- 3. Canonical display names ---------------------------------------------
check("ML -> Machine Learning", canonical_display_name("ML") == "Machine Learning")
check("js -> JavaScript", canonical_display_name("js") == "JavaScript")
check("Node -> Node.js", canonical_display_name("Node") == "Node.js")
check("html -> HTML", canonical_display_name("html") == "HTML")
check("unknown stays as typed", canonical_display_name("Quantum++") == "Quantum++")

csv_html = skills_csv_to_html("ML, Node, Js, Quantum++")
check("csv: ML canonicalized", "<li>Machine Learning</li>" in csv_html, csv_html)
check("csv: Node canonicalized", "<li>Node.js</li>" in csv_html, csv_html)
check("csv: unknown kept", "<li>Quantum++</li>" in csv_html, csv_html)
check("csv: no raw phrasing", "<li>ML</li>" not in csv_html and "<li>Js</li>" not in csv_html)

# --- 4. Link URL scheme allowlist -------------------------------------------
base_links = '<a href="https://old.com">Portfolio</a>'
out = update_links_html(base_links, {"Evil": "javascript:alert(1)", "Good": "https://new.com"})
check("javascript: URL refused", "javascript:" not in out, out)
check("https URL accepted", "https://new.com" in out, out)

# --- 5. Taxonomy cleanup -----------------------------------------------------
all_tax = [s for skills in CANDIDATE_SKILL_CATEGORIES.values() for s in skills]
check("no 'HTTP Clients' in taxonomy", "HTTP Clients" not in all_tax)
check("no 'SSRF Protection' in taxonomy", "SSRF Protection" not in all_tax)

# --- 6. optimizer_brief seam --------------------------------------------------
import inspect  # noqa: E402

from app import tailor  # noqa: E402

sig = inspect.signature(tailor.tailor_application_resume)
check("tailor accepts optimizer_brief", "optimizer_brief" in sig.parameters)
ref_sig = inspect.signature(tailor._ask_llm_for_refinement)
check("refinement accepts optimizer_brief", "optimizer_brief" in ref_sig.parameters)

# --- 7. OCR key hygiene (source-level, code lines only — not comments) ---------
ocr_code = [ln for ln in open(os.path.join(ROOT, "app/ocr.py")).read().splitlines()
            if ln.strip() and not ln.strip().startswith("#")]
check("no helloworld key in ocr.py code",
      not any('"helloworld"' in ln or "'helloworld'" in ln for ln in ocr_code))
check("api_keys uses only configured key",
      any("api_keys = [config.OCR_SPACE_API_KEY]" in ln for ln in ocr_code))
cfg_src = open(os.path.join(ROOT, "app/config.py")).read()
cfg_code = [ln for ln in cfg_src.splitlines() if ln.strip() and not ln.strip().startswith("#")]
bad_defaults = [ln for ln in cfg_code
                if 'os.environ.get("OCR_SPACE_API_KEY"' in ln and ', "")' not in ln]
check("no hardcoded OCR key default", not bad_defaults, bad_defaults[:1])

# --- 8. main.py seams (source-level) ------------------------------------------
main_src = open(os.path.join(ROOT, "app/main.py")).read()
check("new_submit is sync def", re.search(r"\n    def new_submit\(", main_src) is not None)
check("new_submit has no await", "await jd_image.read()" not in main_src)
check("radar status allowlist enforced", "RADAR_STATUS_ALLOWLIST" in main_src and 'status_code=400, detail="Invalid status value."' in main_src)
check("recreate syncs to supabase", main_src.count("sync_app_to_supabase(row, s)") >= 2)
check("CSP on /resume.html", "Content-Security-Policy" in main_src)
check("atomic write helper used", main_src.count("_atomic_write_text(") >= 6)
check("hydrate TTL guard", "_CLOUD_HYDRATION_TTL" in main_src)
check("logout is POST-only", '@app.get("/logout")' not in main_src)

base_src = open(os.path.join(ROOT, "templates/base.html")).read()
check("escapeHtml helper in base.html", "window.escapeHtml = function" in base_src)
check("confirmDelete escapes description", "safeDescription" in base_src)

app_html_src = open(os.path.join(ROOT, "templates/application.html")).read()
check("dispatch modal escapes recipient/subject",
      "safeRecipient" in app_html_src and "safeSubject" in app_html_src)
check("email prompt value escaped", "escHtml(recipientEmail" in app_html_src)
check("no raw recipientEmail interpolation",
      "${recipientEmail}" not in app_html_src)
check("no raw emailSubject interpolation",
      "${emailSubject}" not in app_html_src)
check("skill pills escape custom names", "safeSkillName" in app_html_src)

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("ALL UPDATE19 CHECKS PASSED")
