"""Phase 7 checks: ATS recalibration - every point traceable to evidence.
Phase 13: fairness update — a JD naming no technical requirements marks
required/preferred coverage N/A (excluded) instead of 0; relevance is
normalized to the 0-60 scale over applicable components."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

from app.ats import evaluate_parsing_safety, evaluate_relevance, score_resume  # noqa: E402

JD = "Seeking a Backend Developer with Python, FastAPI, Docker and Kubernetes experience."

BASE_RESUME = """Jane Doe
Email: jane@example.com Phone: +1 555 123 4567
Summary
Backend developer with 3 years of experience.
Experience
2022 - 2024 Backend Developer at Acme, Python and FastAPI.
Education
BSc Computer Science 2018 - 2022
Skills
Python, FastAPI, SQL
Projects
- API service with Python
- Data pipeline with SQL
- Portfolio site
"""


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" ({detail})" if detail and not cond else ""))
    if not cond:
        raise SystemExit(f"FAILED: {name} {detail}")


def rel(resume, jd=JD, title="Backend Developer", skills=None):
    score, matched, missing, _ = evaluate_relevance(
        resume_text=resume, jd_text=jd, job_title=title,
        attested_candidate_skills=skills if skills is not None else ["Python", "FastAPI", "SQL", "Go"],
    )
    return score, matched, missing


# 1. Phase 13: a JD naming no technical requirements cannot fail the resume
# on required/preferred coverage — those components are N/A (excluded from
# the denominator), not scored 0. Friendly, but not inflated to near-perfect.
s, matched, missing = rel(BASE_RESUME, jd="Looking for a pastry chef with croissant lamination skills.",
                          skills=["Python", "FastAPI"])
check("no-requirement JD -> required/preferred N/A (not zeroed)", matched == [] and 40.0 < s < 58.0, f"score={s}")

# 1b. But a JD that DOES name requirements the candidate lacks is a genuine
# miss and still scores low.
s2, _, _ = rel(BASE_RESUME, jd="Seeking a Kubernetes and Terraform expert.", skills=["Python", "FastAPI"])
check("unmet JD requirements still score low", s2 < 30, f"score={s2}")

# 2. Required coverage is proportional, not granted.
s_full, m_full, _ = rel(BASE_RESUME)  # Python, FastAPI hit
s_none, _, _ = rel(BASE_RESUME.replace("Python", "Cobol").replace("FastAPI", "Soap"),
                   skills=["Python", "FastAPI"])
check("coverage scales with evidence", s_full > s_none + 10, f"full={s_full} none={s_none}")

# 3. Layout: table artifacts cost points vs clean text.
clean = BASE_RESUME
messy = BASE_RESUME.replace("Skills\n", "Skills\n| a | b |\n|---|---|\n") + "\tindented\twith\ttabs\n"
sc, _ = evaluate_parsing_safety(clean)
sm, wm = evaluate_parsing_safety(messy)
check("layout artifacts penalized", sm < sc, f"clean={sc} messy={sm}")
check("artifact warning emitted", any("table" in w.lower() or "column" in w.lower() for w in wm))

# 4. Layout bullets: bulleted resume outscores unbulleted.
nobullets = BASE_RESUME.replace("- ", "")
sb, _ = evaluate_parsing_safety(BASE_RESUME)
sn, _ = evaluate_parsing_safety(nobullets)
check("bullets earn layout points", sb > sn, f"bullets={sb} none={sn}")

# 5. Preferred keywords: shared extra tech terms earn points.
jd_extra = JD + " We also use Redis and Terraform."
r_with = BASE_RESUME + "Tools\nRedis, Terraform\n"
r_without = BASE_RESUME
sw, _, _ = rel(r_with, jd=jd_extra)
so, _, _ = rel(r_without, jd=jd_extra)
check("preferred keywords earned, not granted", sw > so, f"with={sw} without={so}")

# 6. Title: no match -> no title points (old code granted 4.0). Costs ~8 raw
# points vs a match (threshold adjusted for Phase 13 normalization).
st, _, _ = rel(BASE_RESUME, title="Pastry Chef")
st2, _, _ = rel(BASE_RESUME, title="Backend Developer")
check("title mismatch earns no title points", st2 - st >= 7.0, f"mismatch={st} match={st2}")
check("title match still rewarded", st2 > st, f"match={st2} mismatch={st}")

# 7. Experience/education: each section earned separately.
no_edu = BASE_RESUME.replace("Education\nBSc Computer Science 2018 - 2022\n", "")
se, _, _ = rel(BASE_RESUME)
sn2, _, _ = rel(no_edu)
check("missing education costs ~4 pts", 3.0 <= (se - sn2) <= 5.0, f"with={se} without={sn2}")

# 8. Determinism.
a = score_resume(BASE_RESUME, JD, "Backend Developer", ["Python", "FastAPI", "SQL"])
b = score_resume(BASE_RESUME, JD, "Backend Developer", ["Python", "FastAPI", "SQL"])
check("scoring deterministic", a.ats_readiness_score == b.ats_readiness_score)
check("score bounded 0-100", 0.0 <= a.ats_readiness_score <= 100.0)

# 9. Score components add up transparently.
check("total == safety + relevance",
      abs(a.ats_readiness_score - (a.parsing_safety_score + a.relevance_score)) < 0.15,
      f"{a.ats_readiness_score} vs {a.parsing_safety_score}+{a.relevance_score}")

print("ALL RECALIBRATION CHECKS PASSED")
