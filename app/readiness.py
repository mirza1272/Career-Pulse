"""Knowledge-base readiness gating.

``kb_completeness(candidate)`` reports one of three levels:

- ``blocked``  — below the minimum: resume generation is refused, and the
  ``missing`` checklist names every gap to fix.
- ``minimum``  — generation unlocked: name, contact, summary, >=1 education,
  >=5 skills, >=2 projects.
- ``strong``   — the full 87+ optimization loop target: minimum plus >=5
  projects, >=10 skills, >=1 experience, complete contact + education.

Nothing is ever invented: the gate only measures what the KB contains.
"""

from __future__ import annotations

from dataclasses import dataclass, field

MIN_SKILLS = 5
MIN_PROJECTS = 2
STRONG_SKILLS = 10
STRONG_PROJECTS = 5


@dataclass
class ReadinessReport:
    level: str  # "blocked" | "minimum" | "strong"
    missing: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return self.level in ("minimum", "strong")


def kb_completeness(candidate) -> ReadinessReport:
    """Assess a Candidate's KB against the minimum/strong thresholds."""
    missing: list[str] = []

    name = (getattr(candidate, "name", "") or "").strip()
    if not name or name.lower() == "applicant":
        missing.append("Add your full name")

    email = (getattr(candidate, "email", "") or "").strip()
    phone = (getattr(candidate, "phone", "") or "").strip()
    location = (getattr(candidate, "location", "") or "").strip()
    if not email and not phone:
        missing.append("Add an email address or phone number")

    if not (getattr(candidate, "summary", "") or "").strip():
        missing.append("Write a professional summary")

    education = getattr(candidate, "education", None) or []
    if len(education) < 1:
        missing.append("Add at least 1 education entry")

    skills = getattr(candidate, "skills", None) or []
    n_skills = len([s for s in skills if str(s).strip()])
    if n_skills < MIN_SKILLS:
        missing.append(f"Add at least {MIN_SKILLS} skills (have {n_skills})")

    projects = getattr(candidate, "projects", None) or []
    n_projects = len(projects)
    if n_projects < MIN_PROJECTS:
        missing.append(f"Add at least {MIN_PROJECTS} projects (have {n_projects})")

    if missing:
        return ReadinessReport(level="blocked", missing=missing)

    # Minimum satisfied — measure the gap to "strong".
    strong_missing: list[str] = []
    if n_projects < STRONG_PROJECTS:
        strong_missing.append(
            f"Add {STRONG_PROJECTS - n_projects} more project(s) to reach "
            f"{STRONG_PROJECTS} (strong profile)"
        )
    if n_skills < STRONG_SKILLS:
        strong_missing.append(
            f"Add {STRONG_SKILLS - n_skills} more skill(s) to reach "
            f"{STRONG_SKILLS} (strong profile)"
        )
    experience = getattr(candidate, "experience", None) or []
    if len(experience) < 1:
        strong_missing.append("Add at least 1 work experience entry (strong profile)")
    if not (email and phone and location):
        strong_missing.append("Complete your contact info: email, phone and location (strong profile)")

    if strong_missing:
        return ReadinessReport(level="minimum", missing=strong_missing)
    return ReadinessReport(level="strong", missing=[])


def format_blocked_message(report: ReadinessReport) -> str:
    """Single human-readable message for the blocked case."""
    lines = ["Your knowledge-base profile is not ready for resume generation."] + [
        f"- {item}" for item in report.missing
    ]
    lines.append("Complete these in the Knowledge Base, then try again.")
    return "\n".join(lines)
