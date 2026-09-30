"""Phase 10 checks (§U.13): full-coverage resume editor + inviolable manual-edit locks."""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

from app.knowledge import load_candidate  # noqa: E402
from app.resume_builder import (  # noqa: E402
    TEMPLATES_DIR,
    build_resume_content,
    certifications_text_to_html,
    extract_regions,
    parse_resume_regions_detail,
    rebuild_resume_from_custom_edits,
    render_certifications_html,
    update_links_html,
)
from app.tailor import tailor_application_resume  # noqa: E402

candidate = load_candidate()
BASE_HTML = (TEMPLATES_DIR / "se_al.html").read_text(encoding="utf-8")


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" ({detail})" if detail and not cond else ""))
    if not cond:
        raise SystemExit(f"FAILED: {name} {detail}")


# 1. Education edit lands in the EDUCATION region; others untouched.
built = rebuild_resume_from_custom_edits(
    base_html=BASE_HTML,
    education_html="<h2>Education</h2><p>LOCKED-EDU-123</p>",
    candidate=candidate,
)
regs = extract_regions(built.html_content)
check("education edit applied", "LOCKED-EDU-123" in regs.get("EDUCATION", ""))
check("education edit leaves summary intact", regs.get("SUMMARY", "").strip() != "")

# 2. Certifications: plain text -> HTML -> CERTIFICATIONS region.
certs_html = certifications_text_to_html("AWS ML Specialty (2025)\nTensorFlow Developer")
check("certs text->html has header", "<h2>Certifications</h2>" in certs_html)
check("certs text->html escapes", "<script>" not in certifications_text_to_html("<script>x</script>"))
built = rebuild_resume_from_custom_edits(
    base_html=BASE_HTML, certifications_html=certs_html, candidate=candidate
)
regs = extract_regions(built.html_content)
check("certifications region applied", "AWS ML Specialty (2025)" in regs.get("CERTIFICATIONS", ""))

# 3. Empty certifications -> section fully removed (no header) on the render path.
check("empty certs render to empty", render_certifications_html(candidate) == "")
built2 = build_resume_content(candidate, jd_text="AI engineer", job_title="AI Engineer")
check("no Certifications header when KB has none",
      "<h2>Certifications</h2>" not in built2.html_content)

# 4. update_links_html: replace existing href, append new label.
links = '<span class="item"><a href="https://old.example.com" target="_blank">LinkedIn</a></span>'
upd = update_links_html(links, {"LinkedIn": "https://linkedin.com/in/newuser", "Website": "https://newsite.dev"})
check("links href replaced", "https://linkedin.com/in/newuser" in upd and "old.example.com" not in upd)
check("new label appended", "Website" in upd and "https://newsite.dev" in upd)
check("links helper ignores empties", update_links_html(links, {"LinkedIn": ""}) == links)

# 5. Links edit via rebuild lands in LINKS region.
built = rebuild_resume_from_custom_edits(
    base_html=BASE_HTML,
    links_html='<span class="item"><a href="https://x.dev">LOCKED-LINK-9</a></span>',
    candidate=candidate,
)
regs = extract_regions(built.html_content)
check("links region applied", "LOCKED-LINK-9" in regs.get("LINKS", ""))

# 6. Per-project bullet edit swaps the bullet text.
pid = candidate.projects[0].get("id") or candidate.projects[0].get("name")
new_bullet = "LOCKED-PROJECT-BULLET-456 built with FastAPI and Qdrant."
built = rebuild_resume_from_custom_edits(
    base_html=BASE_HTML,
    project_bullets={pid: new_bullet},
    candidate=candidate,
)
regs = extract_regions(built.html_content)
check("project bullet swapped", "LOCKED-PROJECT-BULLET-456" in regs.get("PROJECTS", ""))

# 7. Per-experience bullet edit swaps the bullet list.
emp = candidate.experience[0].get("employer")
built = rebuild_resume_from_custom_edits(
    base_html=BASE_HTML,
    experience_bullets={emp: ["LOCKED-EXP-BULLET-789 did X.", "Second locked line."]},
    candidate=candidate,
)
regs = extract_regions(built.html_content)
check("experience bullets swapped", "LOCKED-EXP-BULLET-789" in regs.get("EXPERIENCE", ""))

# 8. extra_regions: generic "other sections" replacement.
built = rebuild_resume_from_custom_edits(
    base_html=BASE_HTML,
    extra_regions={"SUMMARY": "LOCKED-EXTRA-000 summary override."},
    candidate=candidate,
)
regs = extract_regions(built.html_content)
check("extra_regions generic replacement", "LOCKED-EXTRA-000" in regs.get("SUMMARY", ""))

# 9. parse_resume_regions_detail exposes the new editor fields.
detail = parse_resume_regions_detail(built2.html_content, candidate)
for key in ("education_html", "certifications_html", "links_html",
            "education_text", "certifications_text", "link_urls",
            "project_bullets", "experience_bullets"):
    check(f"resume_detail has {key}", key in detail)
check("link_urls parsed", "linkedin" in detail["link_urls"], str(detail["link_urls"].keys()))

# 10. Lock round-trip: manual edits survive a full recreate (tailor loop).
locks = {
    # NOTE: the lock summary is CS-anchored on purpose — the Phase 9 summary
    # guardrail (higher precedence than locks) still sanitizes summaries with
    # zero CS content, even user-locked ones. See check 10b below.
    "SUMMARY": "INVIOABLE-SUMMARY-ZZZ: AI Engineer specializing in LLM applications, RAG pipelines and FastAPI backends.",
    "EDUCATION": "<h2>Education</h2><p>INVIOABLE-EDU-ZZZ</p>",
    "CERTIFICATIONS": "<h2>Certifications</h2><ul><li>INVIOABLE-CERT-ZZZ</li></ul>",
}
res = tailor_application_resume(
    application_id="test-editor-10",
    jd_text="We need a Python Backend Developer with FastAPI, Docker and SQL. Build REST APIs.",
    job_title="Backend Developer",
    candidate=candidate,
    locked_regions=locks,
)
final = res.resume_html_path
html = open(final, encoding="utf-8").read()
check("locked summary survives recreate", "INVIOABLE-SUMMARY-ZZZ" in html)
check("locked education survives recreate", "INVIOABLE-EDU-ZZZ" in html)
check("locked certifications survive recreate", "INVIOABLE-CERT-ZZZ" in html)
check("loop still ran with locks", res.ats_attempts >= 1, f"attempts={res.ats_attempts}")

# 10b. Guardrail precedence: a non-CS lock summary is still sanitized by the
# Phase 9 CS-anchor guardrail (guardrails > locks > AI optimizer).
res_bad = tailor_application_resume(
    application_id="test-editor-10b",
    jd_text="We need a Python Backend Developer with FastAPI, Docker and SQL. Build REST APIs.",
    job_title="Backend Developer",
    candidate=candidate,
    locked_regions={"SUMMARY": "INVIOABLE-BAD — hand-written with no technical content at all."},
    max_attempts=1,
)
html_bad = open(res_bad.resume_html_path, encoding="utf-8").read()
check("non-CS lock summary sanitized by guardrail",
      "INVIOABLE-BAD" not in html_bad and "Computer Science" in html_bad)

# 11. Locks are JSON-serializable (they persist in resume_locks_json).
json.dumps(locks)
check("locks JSON-serializable", True)

# 12. build_resume_content honors education/certifications/links overrides directly.
built3 = build_resume_content(
    candidate, jd_text="AI engineer", job_title="AI Engineer",
    education_override_html="<p>DIRECT-EDU-OVERRIDE</p>",
    certifications_override_html="<h2>Certifications</h2><ul><li>DIRECT-CERT-OVERRIDE</li></ul>",
    links_override_html='<a href="https://d.example">DIRECT-LINK-OVERRIDE</a>',
)
check("direct education override", "DIRECT-EDU-OVERRIDE" in built3.html_content)
check("direct certifications override", "DIRECT-CERT-OVERRIDE" in built3.html_content)
check("direct links override", "DIRECT-LINK-OVERRIDE" in built3.html_content)

print("All resume-editor (Phase 10) checks passed.")
