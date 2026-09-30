"""Candidate knowledge base loader (multi-tenant per-user JSON + fallback to YAML profile).

Provides structured candidate facts for LLM email drafting, resume tailoring, and ATS scoring.
Enforces a Zero-Force policy: if a user leaves experience, projects, or summary empty,
the candidate model cleanly contains empty collections with zero hallucinations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
import json
from pathlib import Path
import re
from typing import TYPE_CHECKING

import yaml

from app import config

if TYPE_CHECKING:
    from app.models import User

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "config/candidate/profile.yaml"
PROJECTS = ROOT / "config/candidate/knowledge/projects.yaml"


@dataclass
class Candidate:
    name: str = "Applicant"
    email: str = ""
    phone: str = ""
    location: str = ""
    summary: str = ""
    links: dict[str, str] = field(default_factory=dict)
    skills: list[str] = field(default_factory=list)
    projects: list[dict] = field(default_factory=list)  # {name, subtitle, bullet, skills, link}
    experience: list[dict] = field(default_factory=list)
    education: list[dict] = field(default_factory=list)  # {institution, degree, field, start, end}
    certifications: list[dict] = field(default_factory=list)  # {name, organization, link}

    def skills_str(self) -> str:
        return ", ".join(self.skills)


@lru_cache(maxsize=1)
def _cached_candidate() -> Candidate:
    prof = yaml.safe_load(PROFILE.read_text(encoding="utf-8")) if PROFILE.exists() else {}
    prof = prof or {}
    ident = prof.get("identity", {})
    skills_map = prof.get("skills", {})

    # 1. Base skills from profile categories
    skills_set: list[str] = (
        list(skills_map.get("expert", []))
        + list(skills_map.get("proficient", []))
        + list(skills_map.get("familiar", []))
    )

    # 2. Add skills from experience blocks
    for exp in prof.get("experience", []):
        for sk in exp.get("skills_used", []):
            if sk and sk.strip() and sk.strip().lower() not in [s.lower() for s in skills_set]:
                skills_set.append(sk.strip())

    # 3. Load projects from projects.yaml
    projects: list[dict] = []
    if PROJECTS.exists():
        raw = yaml.safe_load(PROJECTS.read_text(encoding="utf-8")) or {}
        projects = raw.get("projects", []) if isinstance(raw, dict) else []

    # 4. Merge projects from profile.yaml
    prof_projects = prof.get("projects", []) if isinstance(prof, dict) else []
    existing_keys = {(p.get("name") or p.get("short_name") or "").strip().lower() for p in projects}

    prof_map = {p.get("name", "").strip().lower(): p for p in prof_projects if isinstance(p, dict)}
    for p in projects:
        p_key = (p.get("name") or p.get("short_name") or "").strip().lower()
        if p_key in prof_map:
            prof_p = prof_map[p_key]
            if prof_p.get("link"):
                p["link"] = prof_p["link"]

    for p in prof_projects:
        p_key = p.get("name", "").strip().lower()
        if p_key and p_key not in existing_keys:
            projects.append({
                "id": re.sub(r"[^\w]+", "_", p_key),
                "name": p.get("name", ""),
                "short_name": p.get("name", ""),
                "subtitle": p.get("subtitle", ""),
                "bullet": p.get("description", ""),
                "skills": p.get("skills", []),
                "link": p.get("link", ""),
                "domains": ["ai_ml", "full_stack"],
                "priority": 99,
            })
            existing_keys.add(p_key)

    # 5. Add skills from all loaded projects
    for p in projects:
        for sk in p.get("skills", []):
            if sk and sk.strip() and sk.strip().lower() not in [s.lower() for s in skills_set]:
                skills_set.append(sk.strip())

    loc_obj = ident.get("location", {})
    location_str = ""
    if isinstance(loc_obj, dict):
        parts = [loc_obj.get("city", ""), loc_obj.get("country", "")]
        location_str = ", ".join([p for p in parts if p])
    elif isinstance(loc_obj, str):
        location_str = loc_obj

    return Candidate(
        name=ident.get("name", "Applicant"),
        email=ident.get("email", ""),
        phone=ident.get("phone", ""),
        location=location_str,
        summary=prof.get("summary", ""),
        links=ident.get("links", {}),
        skills=skills_set,
        projects=projects,
        experience=prof.get("experience", []),
        education=[e for e in (prof.get("education", []) or []) if isinstance(e, dict)],
        certifications=[],
    )


def _normalize_certification(raw: object) -> dict:
    """Normalize one certification to {name, organization, link}.

    Accepts the structured dict form and legacy plain-string entries
    (stored before the name/organization/link format existed).
    """
    if isinstance(raw, dict):
        return {
            "name": str(raw.get("name") or "").strip(),
            "organization": str(raw.get("organization") or "").strip(),
            "link": str(raw.get("link") or "").strip(),
        }
    if isinstance(raw, str) and raw.strip():
        return {"name": raw.strip(), "organization": "", "link": ""}
    return {"name": "", "organization": "", "link": ""}


def load_candidate_for_user(user: User | None) -> Candidate:
    """Load Candidate profile for a specific User.

    Respects zero-force policy: if sections are omitted or empty, they stay cleanly empty
    and never get polluted by hardcoded YAML files or other users' data.
    """
    if not user:
        return _cached_candidate()

    is_admin = bool(user.email and user.email.strip().lower() == config.ADMIN_EMAIL.strip().lower())

    if user.knowledge_base_json and user.knowledge_base_json.strip() not in ("", "{}"):
        try:
            data = json.loads(user.knowledge_base_json)
            if isinstance(data, dict):
                ident = data.get("identity", {}) if isinstance(data.get("identity"), dict) else {}
                name = data.get("name") or ident.get("name") or user.name or (user.email.split("@")[0] if user.email else "Applicant")
                email = data.get("email") or ident.get("email") or user.email
                phone = data.get("phone") or ident.get("phone") or ""
                location = data.get("location") or ident.get("location") or ""
                summary = data.get("summary") or ""
                links = data.get("links") or ident.get("links") or {}
                if not isinstance(links, dict):
                    links = {}

                # Normalise skills into list of strings
                raw_skills = data.get("skills", [])
                skills: list[str] = []
                if isinstance(raw_skills, list):
                    for s in raw_skills:
                        if isinstance(s, str) and s.strip():
                            skills.append(s.strip())
                elif isinstance(raw_skills, dict):
                    for cat_skills in raw_skills.values():
                        if isinstance(cat_skills, list):
                            for s in cat_skills:
                                if isinstance(s, str) and s.strip():
                                    skills.append(s.strip())

                # Normalise projects
                raw_projects = data.get("projects", [])
                projects: list[dict] = []
                if isinstance(raw_projects, list):
                    for p in raw_projects:
                        if isinstance(p, dict) and p.get("name"):
                            projects.append(p)

                # Normalise experience
                raw_exp = data.get("experience", [])
                experience: list[dict] = []
                if isinstance(raw_exp, list):
                    for exp in raw_exp:
                        if isinstance(exp, dict) and (exp.get("employer") or exp.get("title") or exp.get("company")):
                            if not exp.get("employer") and exp.get("company"):
                                exp["employer"] = exp["company"]
                            experience.append(exp)

                raw_edu = data.get("education")
                education = [e for e in raw_edu if isinstance(e, dict)] if isinstance(raw_edu, list) else []

                raw_certs = data.get("certifications")
                certifications = []
                if isinstance(raw_certs, list):
                    certifications = [_normalize_certification(c) for c in raw_certs]
                    certifications = [c for c in certifications if c.get("name")]

                return Candidate(
                    name=name,
                    email=email,
                    phone=phone,
                    location=location,
                    summary=summary,
                    links=links,
                    skills=skills,
                    projects=projects,
                    experience=experience,
                    education=education,
                    certifications=certifications,
                )
        except Exception as exc:
            print(f"[Knowledge Notice] Failed parsing knowledge_base_json for user {user.id}: {exc}")

    # Fallback when knowledge_base_json is empty:
    # Only the administrator gets initial baseline bootstrap from profile.yaml
    if is_admin:
        base = _cached_candidate()
        return Candidate(
            name=user.name or base.name or "Haseeb Ur Rahman",
            email=user.email or base.email,
            phone=base.phone,
            location=base.location,
            summary=base.summary,
            links=base.links,
            skills=list(base.skills),
            projects=list(base.projects),
            experience=list(base.experience),
            education=list(base.education),
            certifications=list(base.certifications),
        )

    # For non-admin user with no KB saved yet, return a clean empty profile under their own identity
    return Candidate(
        name=user.name or (user.email.split("@")[0] if user.email else "Applicant"),
        email=user.email or "",
        phone="",
        location="",
        summary="",
        links={},
        skills=[],
        projects=[],
        experience=[],
        education=[],
        certifications=[],
    )


def load_candidate(user: User | None = None, reload: bool = False) -> Candidate:
    """Load candidate profile. If user is provided, load user's profile, else default cached."""
    if reload:
        _cached_candidate.cache_clear()
    if user is not None:
        return load_candidate_for_user(user)
    return _cached_candidate()
