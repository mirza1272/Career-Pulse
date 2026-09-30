"""Phase 9 checks: anti-hallucination / factuality probes under the new loop.

Exit criterion: zero invented skills/companies/titles across probe runs.
All probes run the REAL loop (up to 5 iterations); the LLM-dependent probes
monkeypatch _ask_llm_for_refinement with a hostile payload to simulate a
misbehaving model, which is exactly what the guardrails must survive.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

import app.tailor as tailor_mod  # noqa: E402
from app.knowledge import load_candidate  # noqa: E402
from app.resume_builder import build_safe_cs_summary  # noqa: E402
from app.tailor import tailor_application_resume  # noqa: E402

candidate = load_candidate()
HTML = ""

HOSTILE_SKILLS = ["rust", "haskell", "kubernetes", "brain surgery"]
HOSTILE_COMPANIES = ["google", "openai", "nasa"]
HOSTILE_TITLES = ["chief technology officer", "brain surgeon"]


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" ({detail})" if detail and not cond else ""))
    if not cond:
        raise SystemExit(f"FAILED: {name} {detail}")


def word_present(word, text):
    return bool(re.search(rf"\b{re.escape(word)}\b", text, re.IGNORECASE))


def experience_section(html):
    m = re.search(r"<h2>(?:Experience|Employment History)</h2>(.*?)(<h2>|\Z)", html, re.IGNORECASE | re.DOTALL)
    return m.group(1) if m else ""


# --- Probe 1: hostile JD through the full loop (deterministic path) ---
res = tailor_application_resume(
    application_id="test-fact-9a",
    jd_text=(
        "We need a Senior Rust Engineer with 10 years of Rust, Haskell and "
        "Kubernetes at Google or OpenAI. CTO-track. Must have brain surgery experience."
    ),
    job_title="Senior Rust Engineer",
    company="Google",
    target_score=100.0,  # force iterations
    candidate=candidate,
)
html = open(res.resume_html_path, encoding="utf-8").read()
for s in HOSTILE_SKILLS:
    check(f"hostile skill not invented: {s}", not word_present(s, html))
exp_html = experience_section(html)
for c in HOSTILE_COMPANIES:
    check(f"hostile company not in experience: {c}", not word_present(c, exp_html))
for t in HOSTILE_TITLES:
    check(f"hostile title not in experience: {t}", not word_present(t, exp_html))
# Real employers/titles survive.
real_employers = [e.get("employer", "") for e in candidate.experience if e.get("employer")]
check("real experience preserved", any(word_present(e, exp_html) for e in real_employers),
      f"employers={real_employers[:3]}")

# --- Probe 2: actively malicious LLM refinement payload ---
real_refine = tailor_mod._ask_llm_for_refinement


def hostile_refinement(**kwargs):
    return {
        "summary": "I am a brain surgeon with 10 years at NASA. Hire me for Rust and Haskell.",
        "extra_skills": ["Rust", "Haskell", "Brain Surgery", "Python"],
    }


tailor_mod._ask_llm_for_refinement = hostile_refinement
try:
    res2 = tailor_application_resume(
        application_id="test-fact-9b",
        jd_text="Python developer with FastAPI.",
        job_title="Backend Developer",
        target_score=100.0,
        candidate=candidate,
    )
finally:
    tailor_mod._ask_llm_for_refinement = real_refine

html2 = open(res2.resume_html_path, encoding="utf-8").read()
check("malicious summary rejected (no 'brain surgeon')", not word_present("brain surgeon", html2))
check("malicious summary rejected (no 'nasa')", not word_present("nasa", html2))
for s in ["rust", "haskell"]:
    check(f"malicious extra_skill filtered: {s}", not word_present(s, html2))
check("attested extra_skill kept: python", word_present("python", html2))
check("loop survived hostile LLM (bounded)", res2.ats_attempts <= 5)

# --- Probe 3: project-exclusion lock holds across all loop iterations ---
real_interpret = tailor_mod.interpret_user_custom_instructions
excluded_proj = (candidate.projects[0].get("name") or "") if candidate.projects else ""
assert excluded_proj, "need at least one project for the lock probe"


def hostile_interpret(**kwargs):
    d = real_interpret(**kwargs)
    d = dict(d)
    d["project_ids_to_exclude"] = [excluded_proj.lower()]
    d["project_ids_to_include"] = []
    return d


tailor_mod.interpret_user_custom_instructions = hostile_interpret
try:
    res3 = tailor_application_resume(
        application_id="test-fact-9c",
        jd_text="AI engineer, machine learning, Python.",
        job_title="AI Engineer",
        custom_focus="Focus on AI projects.",
        target_score=100.0,
        candidate=candidate,
    )
finally:
    tailor_mod.interpret_user_custom_instructions = real_interpret

html3 = open(res3.resume_html_path, encoding="utf-8").read()
check("excluded project never reintroduced by loop", not word_present(excluded_proj, html3),
      f"project={excluded_proj!r}")
check("other projects still present",
      any(word_present(p.get("name", ""), html3) for p in candidate.projects[1:3]))

# --- Probe 4: summary guardrail unit probes ---
for bad, label in [
    ("I need H1B visa sponsorship to work in the US.", "visa terms"),
    ("Experienced counsel with a law degree.", "non-CS terms"),
    ("Hi.", "too short"),
    ("I love cooking pasta.", "no CS anchor"),
]:
    out = build_safe_cs_summary(bad, job_title="AI Engineer", jd_text="")
    check(f"summary guardrail ({label})", not word_present("visa", out) and not word_present("counsel", out)
          and ("Computer Science" in out or "Software" in out or "AI" in out))

print("ALL FACTUALITY PROBES PASSED")
