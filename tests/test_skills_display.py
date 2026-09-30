"""Skills display and education regression checks (2026-09-23):
- No "JD Term (KB name)" parenthetical labels on the resume.
- No skill repeated across categories.
- JD phrasing never leaks into labels: KB "HTML" + JD "HTML5" -> "HTML".
- Education renders cleanly matching template layout (entry + left/right).
"""
import os
import re
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

from app.knowledge import Candidate  # noqa: E402
from app.resume_builder import generate_tailored_skills_html, render_education_html  # noqa: E402
from app.skill_aliases import canonical_skill, skill_matches  # noqa: E402


def test_skills_display():
    KB_SKILLS = [
        "Machine Learning", "Deep Learning", "PyTorch", "Agentic AI", "RAG",
        "Node.js", "Tailwind CSS", "JavaScript", "Express.js", "CSS", "HTML",
        "Python", "SQL", "Git", "MongoDB",
    ]
    JD = ("We need ML engineers. Node and JS required. HTML5, CSS3. "
          "Machine Learning, Node.js, JavaScript experience preferred.")

    cand = Candidate(skills=KB_SKILLS)
    html = generate_tailored_skills_html(cand, JD, job_title="AI Engineer", variant="se_al")
    labels = re.findall(r"<li><strong>(.*?)</strong> — (.*?)</li>", html)
    all_skills = [s.strip() for _, sk in labels for s in sk.split(",") if s.strip()]

    assert not any("(" in s or ")" in s for s in all_skills), f"got parenthetical: {all_skills}"
    canons = [canonical_skill(s) for s in all_skills]
    dups = [c for c, n in Counter(canons).items() if n > 1]
    assert not dups, f"duplicates found: {dups}"
    assert "Node.js" in all_skills and "Node" not in all_skills, f"{all_skills}"
    assert "Machine Learning" in all_skills, f"{all_skills}"
    assert "HTML" in all_skills and "HTML5" not in all_skills, f"{all_skills}"
    assert "CSS" in all_skills and "CSS3" not in all_skills, f"{all_skills}"
    assert all_skills.count("JavaScript") == 1, f"{all_skills}"

    # Alias-aware scoring still works behind the scenes
    assert skill_matches("HTML", "HTML5")
    assert skill_matches("CSS", "CSS3")


def test_education_format():
    cand2 = Candidate(education=[{
        "degree": "Bachelor's in Computer Science", "start": "2023", "end": "",
        "status": "in_progress",
        "institution": "National University of Computer and Emerging Sciences",
        "location": "Pakistan",
    }])
    edu = render_education_html(cand2)
    assert '<div class="entry">' in edu
    assert '<div class="left"><strong>' in edu
    assert '<div class="right">2023 – present</div>' in edu
    assert "Bachelor" in edu and "National University" in edu
    assert '<div class="sub-right">Pakistan</div>' in edu


if __name__ == "__main__":
    test_skills_display()
    test_education_format()
    print("ALL SKILLS-DISPLAY CHECKS PASSED")
