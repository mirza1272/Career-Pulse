"""Phase 5 checks: JD<->KB gap report (matched/missing skills, project relevance)."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

from app.matching import (  # noqa: E402
    MatchReport,
    extract_required_skills,
    match_jd_to_kb,
)

JD = """We are hiring a Backend Developer.
Requirements: Python, FastAPI, Kubernetes and Docker experience.
Nice to have: Terraform."""


class FakeCandidate:
    def __init__(self):
        self.skills = ["Python", "FastAPI", "React"]
        self.projects = [
            {"name": "API service", "subtitle": "", "bullet": "Built REST API with Python and FastAPI",
             "skills": ["Python", "FastAPI"], "link": ""},
            {"name": "Portfolio site", "subtitle": "", "bullet": "Personal site in React",
             "skills": ["React"], "link": ""},
        ]
        self.summary = "Backend developer"


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" ({detail})" if detail and not cond else ""))
    if not cond:
        raise SystemExit(f"FAILED: {name} {detail}")


# required-skill extraction (keyword fallback; no Groq keys in this env)
req = extract_required_skills(JD)
check("extracts JD tech terms", "Python" in req and "Kubernetes" in req and "Docker" in req, f"got {req}")
check("no duplicates, capped", len(req) == len(set(req)) and len(req) <= 20, f"got {req}")
check("empty JD -> no required skills", extract_required_skills("") == [])

# gap report
rep = match_jd_to_kb(JD, role="Backend Developer", candidate=FakeCandidate())
check("report carries role", rep.role == "Backend Developer")
check("matched = JD req ∩ KB", "Python" in rep.matched_skills and "FastAPI" in rep.matched_skills,
      f"got {rep.matched_skills}")
check("missing = JD req − KB", "Kubernetes" in rep.missing_skills and "Docker" in rep.missing_skills,
      f"got {rep.missing_skills}")
check("no invented matched skills", all(s in FakeCandidate().skills for s in rep.matched_skills))
check("coverage = matched/required",
      rep.coverage == round(len(rep.matched_skills) / len(rep.required_skills), 2),
      f"got {rep.coverage}")
check("project relevance ranked", rep.project_matches[0].name == "API service"
      and rep.project_matches[0].relevance > rep.project_matches[1].relevance,
      f"got {[(p.name, p.relevance) for p in rep.project_matches]}")
check("project shows its matched skills", "Python" in rep.project_matches[0].matched_skills)

focus = rep.optimizer_focus
check("optimizer_focus names gaps + strengths",
      "Kubernetes" in focus and "Python" in focus and "invent" in focus, f"got {focus!r}")

d = rep.to_dict()
check("to_dict shape", set(d) >= {"role", "required_skills", "matched_skills", "missing_skills",
                                  "coverage", "optimizer_focus", "project_matches"}, f"got {set(d)}")

# robustness
rep2 = match_jd_to_kb(JD, candidate=None)
check("candidate=None never raises", isinstance(rep2, MatchReport))
rep3 = match_jd_to_kb("", candidate=FakeCandidate())
check("empty JD -> full coverage, no crash", rep3.coverage == 1.0 and rep3.missing_skills == [])

print("ALL MATCHING CHECKS PASSED")
