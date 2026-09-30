"""Resume Tailoring & ATS Optimization Loop.

Orchestrates:
1. Baseline resume assembly.
2. ATS Scoring (Parsing Safety + Relevance, fully evidence-based since Phase 7).
3. Automated bounded improvement loop (Phase 8):
   - Targets ATS score >= 87 (or max 5 iterations / plateau stop).
   - Always keeps the best valid version; iterations chain off the best build.
   - User custom instructions are locked constraints, never a loop-killer.
   - Per-attempt score history persisted as a resume version (FR-R-03).
   - Strict truthfulness: only permitted to emphasize skills/projects from candidate profile.
4. Renders final A4 PDF to `data/resumes/application_{app_id}.pdf`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app import config
from app.ats import ATSScore, score_resume
from app.knowledge import Candidate, load_candidate
from app.llm import _post
from app.skill_aliases import skill_matches
from app.resume_builder import (
    RESUMES_OUTPUT_DIR,
    BuiltResume,
    build_resume_content,
    build_safe_cs_summary,
    generate_role_tailored_summary,
    generate_tailored_skills_html,
    render_pdf_from_html,
    render_projects_html,
    resolve_template,
    sanitize_cs_role_summary,
    select_projects,
)

logger = logging.getLogger(__name__)


@dataclass
class TailorResult:
    resume_pdf_path: str
    resume_html_path: str
    ats_score: float
    ats_attempts: int
    variant: str
    ats_report: ATSScore
    # Phase 8: loop transparency + version persistence (FR-R-03).
    iterations: list[dict] = field(default_factory=list)  # per attempt: attempt/score/delta/gaps_closed/via/note
    kb_snapshot_hash: str = ""
    target_reached: bool = False
    stop_reason: str = ""  # target_reached | plateau | max_iterations
    # Actual pages in the generated PDF (auto: 1 page when it fits, 2 when
    # the content needs it).
    pdf_actual_pages: int = 0


def _analyze_jd_and_skills_with_llm(
    candidate: Candidate,
    job_title: str,
    company: str,
    jd_text: str,
    optimizer_brief: str = "",
) -> dict | None:
    """Stage 1: In-depth LLM analysis of JD requirements and candidate skill mapping."""
    if not config.LLM_API_KEY:
        return None

    try:
        from app.llm import call_llm_json
        nonce = uuid.uuid4().hex[:8]

        projects_catalog = [
            {
                "id": p.get("id") or (p.get("short_name") or p.get("name") or "").lower().replace(" ", "_"),
                "name": p.get("name") or p.get("short_name"),
                "skills": p.get("skills", []),
                "bullet": (p.get("bullet") or "")[:150],
            }
            for p in (candidate.projects or [])
        ]

        candidate_bio = (candidate.summary or "").strip() or "Computer Science student at FAST National University in Pakistan"
        system_prompt = (
            "You are an elite ATS resume strategist and technical alignment engine. "
            "Your objective is to maximize the candidate's ATS Readiness Score for this specific job description.\n"
            "STRICT 2-PART SUMMARY ARCHITECTURE:\n"
            "1. Part 1 (~60%): Grounded in candidate's authentic background & education foundation directly from their Knowledge Base:\n"
            f"   KB Base Intro: \"{candidate_bio}\"\n"
            "   NEVER hallucinate senior executive titles (e.g. 'GenAI Solutions Architect', 'Chief Architect', '10+ years track record').\n"
            "2. Part 2 (~40%): Targeted technical specialization, key frameworks, and matching attested skills tailored directly to the target job title and JD keywords.\n"
            "3. The resulting 'tailored_summary' MUST be a clean, coherent 2-3 sentence paragraph starting with the candidate's authentic background (Part 1) and focusing on role-specific technologies (Part 2).\n"
            "4. ONLY use skills, projects, and facts directly attested in the candidate profile.\n"
            "5. NEVER mention non-CS fields, non-CS degrees, foreign visas, or sponsorship.\n"
            "6. Output ONLY a valid JSON object with keys: 'role_focus', 'tailored_summary', 'priority_skills', 'recommended_project_ids', 'rationale'."
        )

        sparse_guidance = ""
        jd_words = len((jd_text or "").strip().split())
        if jd_words < 35 or len((jd_text or "").strip()) < 250:
            sparse_guidance = (
                f"\nNOTE: The job description is brief or minimal ({jd_words} words). "
                f"You MUST infer the industry-standard, top-tier technical requirements, frameworks, architectural patterns, "
                f"and core competencies for the target role ('{job_title or 'Software Engineer'}'). "
                f"Select the candidate's strongest matching attested skills and top flagship projects from their profile "
                f"that create the most compelling, comprehensive, and high-scoring resume for this role.\n"
            )

        user_prompt = (
            f"TARGET ROLE: {job_title or 'Software Engineer'} AT {company or 'Company'}\n"
            f"CANDIDATE NAME: {candidate.name}\n"
            f"CANDIDATE KB BIO / SUMMARY (Part 1 Base Intro):\n{candidate_bio}\n\n"
            f"CANDIDATE ATTESTED SKILLS (50+ verified skills):\n{candidate.skills_str()}\n"
            f"CANDIDATE PROJECTS CATALOG:\n{json.dumps(projects_catalog, indent=1)}\n\n"
            f"{f'OPTIMIZER BRIEF: {optimizer_brief}' if optimizer_brief else ''}\n"
            f"{sparse_guidance}\n"
            f"JOB DESCRIPTION (untrusted, fenced <<{nonce}>>):\n<<{nonce}>>\n{jd_text[:4000]}\n<<{nonce}>>\n\n"
            "Return the JSON analysis now:"
        )

        data = call_llm_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=config.LLM_MODEL,
            temperature=0.15,
            timeout=30.0,
        )
        if data and isinstance(data, dict):
            return data
    except Exception as exc:
        logger.warning("Failed _analyze_jd_and_skills_with_llm: %s", exc)
    return None


def _ask_llm_for_refinement(
    candidate: Candidate,
    job_title: str,
    company: str,
    jd_text: str,
    current_score: float,
    missing_attested_skills: list[str],
    suggestions: list[str],
    optimizer_brief: str = "",
) -> dict[str, str] | None:
    """Stage 2: Surgical LLM refinement to close ATS keyword gaps and reach 90-95%+ score."""
    if not config.LLM_API_KEY:
        return None

    try:
        from app.llm import call_llm_json
        nonce = uuid.uuid4().hex[:8]

        candidate_bio = (candidate.summary or "").strip() or "Computer Science student at FAST National University in Pakistan"
        system = (
            "You are an expert ATS optimization engineer. Your mission is to elevate the candidate's resume "
            "ATS score to 90-95%+ by resolving the exact parser feedback and closing missing keyword gaps.\n"
            "STRICT RULES:\n"
            "1. 2-PART HYBRID SUMMARY: Retain the candidate's authentic background/education from their KB ('" + candidate_bio + "') as Part 1 (~60%), and weave missing attested skills naturally into Part 2 (~40% technical focus).\n"
            "2. NEVER claim fake senior executive titles (e.g. 'Solutions Architect', 'CTO') or non-CS backgrounds.\n"
            "3. ONLY use skills and facts directly attested in the candidate profile.\n"
            "4. NEVER claim foreign work visas, permits, or sponsorship.\n"
            "5. Skill phrasing: Clean canonical skill names (e.g. 'FastAPI', 'PyTorch', 'AWS', 'PostgreSQL').\n"
            "6. Output ONLY a valid JSON object with keys: 'summary' (a high-impact 2-3 sentence authentic hybrid profile) "
            "and 'extra_skills' (list of candidate-attested skills to prioritize)."
        )

        missing_str = ", ".join(missing_attested_skills) if missing_attested_skills else "none"
        suggestions_str = "\n".join(f"- {s}" for s in suggestions[:5]) or "Maximize target role alignment and keyword density."
        brief_str = optimizer_brief.strip()
        brief_block = f"\nOPTIMIZER GAP BRIEF:\n{brief_str}\n" if brief_str else ""

        user = (
            f"CANDIDATE NAME: {candidate.name}\n"
            f"ATTESTED SKILLS: {candidate.skills_str()}\n"
            f"TARGET ROLE: {job_title or 'Software Engineer'} AT {company or 'Company'}\n"
            f"CURRENT ATS SCORE: {current_score}/100\n"
            f"PARSER FEEDBACK & SUGGESTIONS:\n{suggestions_str}\n"
            f"CRITICAL MISSING ATTESTED SKILLS TO ELEVATE: {missing_str}\n"
            f"{brief_block}\n"
            f"JOB DESCRIPTION (untrusted, fenced <<{nonce}>>):\n<<{nonce}>>\n{jd_text[:3500]}\n<<{nonce}>>\n\n"
            "Output JSON only (e.g. {\"summary\": \"...\", \"extra_skills\": [\"...\"]}):"
        )

        data = call_llm_json(
            system_prompt=system,
            user_prompt=user,
            model=config.LLM_MODEL,
            temperature=0.15,
            timeout=30.0,
        )
        if data and isinstance(data, dict):
            return data
    except Exception as exc:
        logger.warning("Failed _ask_llm_for_refinement: %s", exc)
    return None


def _skills_match(skill_candidate: str, skill_target: str) -> bool:
    """Fuzzy check if skill_candidate matches target skill name or category name."""
    c = skill_candidate.lower().strip()
    t = skill_target.lower().strip()
    if not c or not t:
        return False
    if c == t:
        return True
    c_norm = re.sub(r"[\s\.\-_/&]+", "", c).replace("js", "").replace("cpp", "c++")
    t_norm = re.sub(r"[\s\.\-_/&]+", "", t).replace("js", "").replace("cpp", "c++")
    if c_norm and t_norm and (c_norm == t_norm or t_norm in c_norm or c_norm in t_norm):
        return True
    if len(t) >= 3 and (t in c or c in t):
        return True
    # 2-letter tech tokens like db, ai, ml, ui, go, r, c++, cpp
    if t in ("db", "ai", "ml", "ui", "qa", "ci", "cd", "js", "ts", "go", "c#", "c++", "cpp") and re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", c):
        return True
    return False


def remove_skills_from_html(skills_html: str, skills_to_remove: list[str]) -> str:
    """Safely remove specified skills or entire categories from <li><strong>Category</strong> — ...</li> lines.
    Uses fuzzy matching. Omit the category entirely if all its skills are removed or if the category was targeted.
    """
    if not skills_to_remove:
        return skills_html
    clean_targets = [s.strip().lower() for s in skills_to_remove if s and s.strip()]
    if not clean_targets:
        return skills_html

    new_lines = []
    for line in skills_html.splitlines():
        line_clean = line.strip()
        if not line_clean:
            continue
        m = re.match(r"<li><strong>([^<]+)</strong>\s*—\s*(.*?)</li>", line_clean)
        if m:
            cat_name = m.group(1).strip()
            skills_part = m.group(2).strip()

            # If the entire category was targeted for removal (e.g. 'full-stack', 'web', 'databases', 'programming')
            if any(_skills_match(cat_name, target) for target in clean_targets):
                continue

            skills = [s.strip() for s in skills_part.split(",") if s.strip()]
            filtered = [
                s for s in skills
                if not any(_skills_match(s, target) for target in clean_targets)
            ]
            if filtered:
                new_lines.append(f"    <li><strong>{cat_name}</strong> — {', '.join(filtered)}</li>")
        else:
            new_lines.append(line)
    return "\n".join(new_lines)


def _deterministic_extract_custom_instructions(user_prompt: str, candidate: Candidate) -> dict:
    """Deterministic fallback to extract skill/project additions and removals from user prompt."""
    prompt_lower = user_prompt.lower()
    remove_keywords = [
        "remove", "delete", "hata", "hatao", "nikal", "nikalo", "drop", "exclude", "mat",
        "na dalo", "bina", "chhor", "kam kro", "kam karo", "shorten", "reduce", "cut", "omit", "without"
    ]
    add_keywords = [
        "add", "include", "shamil", "rakho", "rkho", "daal", "plus", "with", "insert",
        "put", "bring", "highlight", "emphasize", "focus", "dalo", "bnao"
    ]

    detected_skills_remove = set()
    detected_skills_add = set()
    detected_projs_exclude = set()
    detected_projs_include = set()
    detected_cats_exclude = set()
    detected_cats_include = set()
    summary_draft = None

    # Category aliases
    cat_aliases = {
        "Frontend & Web": ["frontend", "web", "react", "html", "css", "tailwind", "ui", "full-stack", "fullstack"],
        "Databases & Storage": ["database", "databases", "db", "storage", "postgres", "mongodb", "sql", "redis", "chromadb"],
        "Backend, Cloud & APIs": ["backend", "cloud", "api", "apis", "fastapi", "docker", "aws", "azure", "ci/cd", "devops"],
        "AI & Machine Learning": ["machine learning", "deep learning", "ai & ml", "computer vision", "pytorch", "tensorflow", "scikit"],
        "Agentic AI & GenAI": ["agentic", "genai", "gen ai", "rag", "llm", "llms", "agents", "langchain", "vapi", "fastmcp"],
        "Programming Languages": ["programming", "languages", "coding", "python", "c++", "cpp", "javascript", "typescript"],
    }

    for cat_name, aliases in cat_aliases.items():
        for alias in aliases:
            if re.search(rf"\b{re.escape(alias)}\b", prompt_lower):
                idx = prompt_lower.find(alias)
                start_ctx = max(0, idx - 40)
                end_ctx = min(len(prompt_lower), idx + len(alias) + 40)
                snippet = prompt_lower[start_ctx:end_ctx]
                if any(rw in snippet for rw in remove_keywords):
                    detected_cats_exclude.add(cat_name)
                elif any(aw in snippet for aw in add_keywords):
                    detected_cats_include.add(cat_name)
                break

    for s in candidate.skills:
        s_clean = s.lower().replace(".js", "").replace("-", " ").strip()
        pattern = rf"(?<![a-z0-9]){re.escape(s.lower())}(?![a-z0-9])|(?<![a-z0-9]){re.escape(s_clean)}(?![a-z0-9])"
        match = re.search(pattern, prompt_lower)
        if match:
            start_ctx = max(0, match.start() - 40)
            end_ctx = min(len(prompt_lower), match.end() + 40)
            snippet = prompt_lower[start_ctx:end_ctx]
            if any(rw in snippet for rw in remove_keywords):
                detected_skills_remove.add(s)
            elif any(aw in snippet for aw in add_keywords):
                detected_skills_add.add(s)
            elif any(rw in prompt_lower for rw in remove_keywords) and not any(aw in prompt_lower for aw in add_keywords):
                detected_skills_remove.add(s)

    # Extract direct phrases: "add Docker and Kubernetes to skills"
    add_match = re.search(r"(?:add|include|shamil|dalo|rakho)\s+([a-zA-Z0-9\+\#\.\,\s\-]+?)\s+(?:to|in|into|skills|categories|mn|main)", prompt_lower)
    if add_match:
        items = re.split(r"[,&]|\band\b|\baur\b|\bor\b", add_match.group(1))
        for item in items:
            it = item.strip()
            if it and len(it) >= 2 and it not in ("the", "my", "our", "to", "in", "skills", "section"):
                detected_skills_add.add(it.title() if it.islower() else it)

    # Extract direct phrases: "remove web and c++ from skills"
    rem_match = re.search(r"(?:remove|delete|hata|hatao|nikal|nikalo|drop|exclude)\s+([a-zA-Z0-9\+\#\.\,\s\-]+?)\s+(?:from|in|out|skills|categories|mn|main|sy|se)", prompt_lower)
    if rem_match:
        items = re.split(r"[,&]|\band\b|\baur\b|\bor\b", rem_match.group(1))
        for item in items:
            it = item.strip()
            if it and len(it) >= 2 and it not in ("the", "my", "our", "from", "skills", "section"):
                detected_skills_remove.add(it.title() if it.islower() else it)

    for p in candidate.projects:
        p_name = (p.get("name") or "").lower()
        p_short = (p.get("short_name") or "").lower()
        p_id = (p.get("id") or "").lower()
        for term in [p_name, p_short, p_id]:
            if term and len(term) >= 3 and term in prompt_lower:
                idx = prompt_lower.find(term)
                start_ctx = max(0, idx - 40)
                end_ctx = min(len(prompt_lower), idx + len(term) + 40)
                snippet = prompt_lower[start_ctx:end_ctx]
                if any(rw in snippet for rw in remove_keywords):
                    detected_projs_exclude.add(p_id or p_name)
                elif any(aw in snippet for aw in add_keywords):
                    detected_projs_include.add(p_id or p_name)
                elif any(rw in prompt_lower for rw in remove_keywords) and not any(aw in prompt_lower for aw in add_keywords):
                    detected_projs_exclude.add(p_id or p_name)
                break

    # Section order intent detection
    detected_section_order = None
    if any(k in prompt_lower for k in ["experience first", "experience pehle", "work experience first", "jobs first", "employment first"]):
        detected_section_order = ["summary", "experience", "skills", "projects", "education", "certifications"]
    elif any(k in prompt_lower for k in ["projects first", "projects pehle", "project first"]):
        detected_section_order = ["summary", "projects", "skills", "experience", "education", "certifications"]
    elif any(k in prompt_lower for k in ["skills first", "skills pehle", "skills at top"]):
        detected_section_order = ["summary", "skills", "experience", "projects", "education", "certifications"]
    elif any(k in prompt_lower for k in ["education first", "education pehle", "education at top"]):
        detected_section_order = ["summary", "education", "experience", "skills", "projects", "certifications"]

    return {
        "project_ids_to_include": list(detected_projs_include),
        "project_ids_to_exclude": list(detected_projs_exclude),
        "categories_to_include": list(detected_cats_include),
        "categories_to_exclude": list(detected_cats_exclude),
        "skills_to_add": list(detected_skills_add),
        "skills_to_remove": list(detected_skills_remove),
        "max_skills_per_category": 4 if any(k in prompt_lower for k in ["choti", "shorten", "compact", "kam", "reduce", "short"]) else None,
        "summary_draft": summary_draft,
        "section_order": detected_section_order,
    }


def interpret_user_custom_instructions(
    user_prompt: str,
    candidate: Candidate,
    jd_text: str,
    job_title: str = "",
    company: str = "",
) -> dict:
    """Use Groq LLM with deterministic regex fallback to interpret candidate's custom instructions
    (to add/remove specific projects, skills, or modify summary).
    Never raises.
    """
    if not user_prompt or len(user_prompt.strip()) < 3:
        return {}

    deterministic = _deterministic_extract_custom_instructions(user_prompt, candidate)

    if not config.LLM_API_KEY:
        return deterministic

    try:
        from app.llm import call_llm_json
        from app.resume_builder import CANDIDATE_SKILL_CATEGORIES
        nonce = uuid.uuid4().hex[:8]

        projects_catalog = [
            {
                "id": p.get("id") or (p.get("short_name") or p.get("name") or "").lower().replace(" ", "_"),
                "name": p.get("name"),
                "short_name": p.get("short_name"),
                "subtitle": p.get("subtitle"),
                "skills": p.get("skills", []),
            }
            for p in candidate.projects
        ]

        avail_categories = list(CANDIDATE_SKILL_CATEGORIES.keys())

        system_prompt = (
            "You are an expert AI resume customizer. The candidate has provided natural language instructions "
            "(in English, Roman Urdu, or casual developer phrasing) to customize their resume for a job.\n"
            "Instructions can include: modifying the summary, adding/removing skills, including/excluding projects, "
            "shortening sections, reordering sections, focusing on specific domains (e.g. AI, Backend, Web, Data).\n\n"
            "Analyze the candidate's instructions carefully and extract:\n"
            "1. 'project_ids_to_include': list of project names/IDs they explicitly want added or prioritized (from candidate projects catalog).\n"
            "2. 'project_ids_to_exclude': list of project names/IDs they explicitly want removed.\n"
            "3. 'categories_to_include': list of exact category names to keep, or [] if not specified.\n"
            "4. 'categories_to_exclude': list of exact category names to completely remove, or [] if not specified.\n"
            "5. 'skills_to_add': list of individual skills they explicitly want added or highlighted.\n"
            "6. 'skills_to_remove': list of individual skills or keywords they want removed.\n"
            "7. 'max_skills_per_category': integer (e.g. 4 or 5) if candidate asked to shorten/compact the skills section, or null.\n"
            "8. 'summary_draft': A tailored, polished 2-sentence CS/AI profile summary incorporating their requested focus, length, or highlights (or null if no summary changes were requested).\n"
            "9. 'section_order': list of section keys (from ['summary', 'experience', 'education', 'skills', 'projects', 'certifications']) in requested sequence if candidate asked to change order, else null.\n\n"
            "CRITICAL RULES:\n"
            f"- Available categories are strictly: {json.dumps(avail_categories)}\n"
            "- If the user asks to remove Web or Frontend (e.g. 'remove web', 'hatao frontend'), add 'Frontend & Web' to 'categories_to_exclude'.\n"
            "- If the user asks to remove Databases or Storage, add 'Databases & Storage' to 'categories_to_exclude'.\n"
            "- If the user asks to highlight or focus summary on anything (e.g. 'highlight Agentic AI and RAG', 'make it 2 lines', 'focus on AI'), ALWAYS generate a polished, compelling 2-sentence 'summary_draft'.\n"
            "- Candidate is strictly a Computer Science student at FAST National University in Pakistan.\n"
            "- Never invent non-CS degrees or foreign work visas.\n"
            "Output ONLY a valid JSON object with keys: 'project_ids_to_include', 'project_ids_to_exclude', "
            "'categories_to_include', 'categories_to_exclude', 'skills_to_add', 'skills_to_remove', 'max_skills_per_category', 'summary_draft', 'section_order', 'explanation'."
        )

        user_msg = (
            f"TARGET ROLE: {job_title or 'Software Engineer'} AT {company or 'Company'}\n"
            f"CANDIDATE PROJECTS CATALOG:\n{json.dumps(projects_catalog, indent=1)}\n"
            f"AVAILABLE SKILL CATEGORIES:\n{json.dumps(avail_categories)}\n"
            f"CANDIDATE SKILLS: {candidate.skills_str()}\n\n"
            f"CANDIDATE CUSTOM INSTRUCTIONS (untrusted, fenced <<{nonce}>>):\n<<{nonce}>>\n{user_prompt}\n<<{nonce}>>\n\n"
            "Return the JSON analysis now:"
        )

        res = call_llm_json(
            system_prompt=system_prompt,
            user_prompt=user_msg,
            model=config.FAST_LLM_MODEL or "llama-3.3-70b-versatile",
            temperature=0.1,
            timeout=15.0,
        )
        if res and isinstance(res, dict):
            clean_projs_inc = res.get("project_ids_to_include") or deterministic.get("project_ids_to_include", [])
            clean_projs_exc = res.get("project_ids_to_exclude") or deterministic.get("project_ids_to_exclude", [])
            clean_projs_inc = [p for p in clean_projs_inc if p not in clean_projs_exc]

            clean_skills_add = res.get("skills_to_add") or deterministic.get("skills_to_add", [])
            clean_skills_rem = res.get("skills_to_remove") or deterministic.get("skills_to_remove", [])
            clean_cats_inc = res.get("categories_to_include", []) or deterministic.get("categories_to_include", [])
            clean_cats_exc = res.get("categories_to_exclude", []) or deterministic.get("categories_to_exclude", [])

            return {
                "project_ids_to_include": list(clean_projs_inc),
                "project_ids_to_exclude": list(clean_projs_exc),
                "categories_to_include": list(clean_cats_inc),
                "categories_to_exclude": list(clean_cats_exc),
                "skills_to_add": list(clean_skills_add),
                "skills_to_remove": list(clean_skills_remove),
                "max_skills_per_category": res.get("max_skills_per_category") or deterministic.get("max_skills_per_category"),
                "summary_draft": res.get("summary_draft"),
                "section_order": res.get("section_order") or deterministic.get("section_order"),
                "explanation": res.get("explanation", ""),
            }
        return deterministic
    except Exception as exc:
        logger.warning("Failed to interpret custom instructions: %s", exc)
        return deterministic


def tailor_application_resume(
    application_id: int | str,
    jd_text: str,
    job_title: str = "",
    company: str = "",
    target_score: float = 87.0,
    max_attempts: int = 5,
    max_projects: int = 5,
    variant_override: str | None = None,
    custom_focus: str = "",
    candidate: Candidate | None = None,
    user: object | None = None,
    locked_regions: dict[str, str] | None = None,
    optimizer_brief: str = "",
    section_order: list[str] | str | None = None,
) -> TailorResult:
    """Tailor + optimize an application's resume.

    locked_regions ({REGION_NAME: final region HTML}, Phase 10 §U.13) are the
    user's manual edits from the resume editor. They are INVOLABLE: they win
    over every AI-generated value on every loop iteration, so recreating a
    resume never wipes what the user typed.

    optimizer_brief is the machine-generated gap-analysis brief (from the
    match step). It steers the refinement prompt directly — no extra LLM
    interpretation call. custom_focus is reserved for genuine free-typed
    user instructions (e.g. from the recreate form).
    """
    if candidate is None:
        candidate = load_candidate(user)

    from app.ats import check_candidate_87_eligibility
    is_eligible, eligibility_note = check_candidate_87_eligibility(candidate)
    if is_eligible:
        target_score = max(target_score, 87.0)
    else:
        # User not eligible for 87+ optimization cap; do not force LLM to push ungrounded content
        target_score = min(target_score, 75.0)

    # Phase 10 (§U.13): manual-edit locks — inviolable on every loop iteration.
    _locks = locked_regions or {}
    _lock = lambda name: _locks.get(name) or None
    lock_summary = _lock("SUMMARY")
    lock_skills = _lock("SKILLS")
    lock_projects = _lock("PROJECTS")
    lock_experience = _lock("EXPERIENCE")
    lock_education = _lock("EDUCATION")
    lock_certifications = _lock("CERTIFICATIONS")
    lock_links = _lock("LINKS")

    # Phase 8 (FR-R-03): KB snapshot hash so a version stays reproducible after KB edits.
    try:
        kb_snapshot_hash = hashlib.sha256(
            json.dumps(asdict(candidate), sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
    except Exception:
        kb_snapshot_hash = ""

    try:
        RESUMES_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    eff_variant = resolve_template(variant_override or (getattr(user, "selected_template_id", None) if user else None) or "apex_modern")[0]

    clean_role = sanitize_cs_role_summary(job_title, variant=eff_variant)

    # --- Phase 0: Interpret Custom AI Instructions if provided ---
    # (only genuine user-typed instructions reach this path — the
    # machine-generated optimizer brief steers refinement directly below).
    instructions_data = {}
    if custom_focus:
        instructions_data = interpret_user_custom_instructions(
            user_prompt=custom_focus,
            candidate=candidate,
            jd_text=jd_text,
            job_title=job_title,
            company=company,
        )

    # 1. Summary handling & Stage 1 LLM analysis
    llm_plan = None
    if not custom_focus and config.LLM_API_KEY:
        llm_plan = _analyze_jd_and_skills_with_llm(
            candidate=candidate,
            job_title=job_title,
            company=company,
            jd_text=jd_text,
            optimizer_brief=optimizer_brief,
        )

    initial_summary = None
    if instructions_data.get("summary_draft"):
        initial_summary = build_safe_cs_summary(
            instructions_data["summary_draft"].strip(),
            job_title=job_title,
            variant=eff_variant,
            jd_text=jd_text,
            candidate=candidate,
        )
    elif llm_plan and llm_plan.get("tailored_summary"):
        initial_summary = build_safe_cs_summary(
            llm_plan["tailored_summary"].strip(),
            job_title=job_title,
            variant=eff_variant,
            jd_text=jd_text,
            candidate=candidate,
        )
    else:
        initial_summary = generate_role_tailored_summary(
            job_title,
            jd_text=jd_text,
            variant=eff_variant,
            candidate=candidate,
        )

    # Priority skills & recommended projects from Stage 1 LLM plan
    llm_priority_skills = []
    llm_recommended_projects = []
    if llm_plan:
        if isinstance(llm_plan.get("priority_skills"), list):
            candidate_skills_set = {s.lower() for s in candidate.skills}
            llm_priority_skills = [
                s for s in llm_plan["priority_skills"]
                if isinstance(s, str) and (s.lower() in candidate_skills_set or any(skill_matches(s, cs) for cs in candidate_skills_set))
            ]
        if isinstance(llm_plan.get("recommended_project_ids"), list):
            llm_recommended_projects = [str(p).strip().lower() for p in llm_plan["recommended_project_ids"] if str(p).strip()]

    # 2. Projects override handling (custom additions & removals)
    include_ids = [str(x).strip().lower() for x in instructions_data.get("project_ids_to_include", []) if str(x).strip()]
    exclude_ids = [str(x).strip().lower() for x in instructions_data.get("project_ids_to_exclude", []) if str(x).strip()]

    custom_projects_html = None
    if include_ids or exclude_ids:
        all_p = candidate.projects
        filtered_p = []
        for p in all_p:
            p_id = (p.get("id") or "").lower()
            p_name = (p.get("name") or "").lower()
            p_short = (p.get("short_name") or "").lower()
            if any(ex in p_id or ex in p_name or ex in p_short for ex in exclude_ids):
                continue
            filtered_p.append(p)

        forced_includes = []
        remainder = []
        for p in filtered_p:
            p_id = (p.get("id") or "").lower()
            p_name = (p.get("name") or "").lower()
            p_short = (p.get("short_name") or "").lower()
            if any(inc in p_id or inc in p_name or inc in p_short for inc in include_ids):
                forced_includes.append(p)
            else:
                remainder.append(p)

        needed = max_projects - len(forced_includes)
        if needed > 0 and remainder:
            temp_cand = Candidate(
                name=candidate.name,
                email=candidate.email,
                phone=candidate.phone,
                skills=candidate.skills,
                projects=remainder,
                experience=candidate.experience,
            )
            v_domain = resolve_template(eff_variant)[1].get("domain", "all")
            picked_remainder = select_projects(
                temp_cand,
                variant_domain=v_domain,
                jd_text=jd_text,
                job_title=job_title,
                max_projects=needed,
                recommended_project_ids=llm_recommended_projects,
                use_llm=False,
            )
            selected_custom_projects = (forced_includes + picked_remainder)[:max_projects]
        else:
            selected_custom_projects = forced_includes[:max_projects]

        custom_projects_html = render_projects_html(selected_custom_projects)

    # 3. Skills override handling (custom additions & removals)
    skills_to_add = [str(s).strip() for s in instructions_data.get("skills_to_add", []) if str(s).strip()]
    skills_to_remove = [str(s).strip().lower() for s in instructions_data.get("skills_to_remove", []) if str(s).strip()]
    cats_to_include = instructions_data.get("categories_to_include")
    cats_to_exclude = instructions_data.get("categories_to_exclude")
    max_skills_cat = instructions_data.get("max_skills_per_category") or 6

    custom_skills_html = None
    if skills_to_add or skills_to_remove or cats_to_include or cats_to_exclude:
        base_skills = generate_tailored_skills_html(
            candidate=candidate,
            jd_text=jd_text,
            job_title=job_title,
            variant=eff_variant,
            extra_target_skills=skills_to_add + llm_priority_skills,
            allowed_categories=cats_to_include,
            excluded_categories=cats_to_exclude,
            excluded_skills=skills_to_remove,
            max_skills_per_cat=max_skills_cat,
        )
        if skills_to_remove or cats_to_exclude:
            all_removals = skills_to_remove + (cats_to_exclude or [])
            custom_skills_html = remove_skills_from_html(base_skills, all_removals)
        else:
            custom_skills_html = base_skills
    else:
        # Default: full dynamic categorization prioritizing JD matches and LLM priorities
        custom_skills_html = generate_tailored_skills_html(
            candidate=candidate,
            jd_text=jd_text,
            job_title=job_title,
            variant=eff_variant,
            extra_target_skills=llm_priority_skills,
            max_skills_per_cat=6,
        )

    # Determine precedence: User AI instructions override conflicting locks
    has_custom_summary = bool(instructions_data.get("summary_draft"))
    has_custom_skills = bool(skills_to_add or skills_to_remove or cats_to_include or cats_to_exclude)
    has_custom_projects = bool(include_ids or exclude_ids)
    eff_section_order = section_order or instructions_data.get("section_order")

    eff_summary_override = initial_summary if has_custom_summary else (lock_summary or initial_summary)
    eff_skills_override = custom_skills_html if has_custom_skills else (lock_skills or custom_skills_html)
    eff_projects_override = custom_projects_html if has_custom_projects else (lock_projects or custom_projects_html)

    # --- Attempt 1: Tailored resume with user customizations ---
    best_built = build_resume_content(
        candidate,
        jd_text,
        job_title=job_title,
        variant=eff_variant,
        summary_override=eff_summary_override,
        skills_override_html=eff_skills_override,
        projects_override_html=eff_projects_override,
        experience_override_html=lock_experience,
        education_override_html=lock_education,
        certifications_override_html=lock_certifications,
        links_override_html=lock_links,
        max_projects=max_projects,
        recommended_project_ids=llm_recommended_projects,
        section_order=eff_section_order,
    )
    best_score = score_resume(
        best_built.resume_text,
        jd_text,
        job_title=job_title,
        attested_candidate_skills=candidate.skills,
        candidate=candidate,
    )

    # --- Phase 8: Bounded optimization loop ---
    history: list[dict] = [
        {
            "attempt": 1,
            "score": round(best_score.ats_readiness_score, 2),
            "delta": 0.0,
            "gaps_closed": [],
            "via": "initial",
            "note": "Initial tailored build (custom instructions applied).",
        }
    ]
    refinement_cap = min(max_attempts - 1, 4)
    plateau_streak = 0
    stop_reason = "target_reached" if best_score.ats_readiness_score >= target_score else ""

    attempt = 1
    while not stop_reason and attempt <= refinement_cap and plateau_streak < 2:
        attempt += 1
        prev_best = best_score.ats_readiness_score
        prev_missing = set(best_score.missing_attested_skills)

        # Request LLM refinement (chained off the BEST build, never a worse one)
        refinement = _ask_llm_for_refinement(
            candidate=candidate,
            job_title=job_title,
            company=company,
            jd_text=jd_text,
            current_score=prev_best,
            missing_attested_skills=best_score.missing_attested_skills,
            suggestions=best_score.suggestions,
            optimizer_brief=optimizer_brief,
        )

        if refinement and "summary" in refinement:
            candidate_skills_set = {s.lower() for s in candidate.skills}
            new_summary = (
                initial_summary
                if has_custom_summary
                else build_safe_cs_summary(
                    refinement["summary"].strip(),
                    job_title=job_title,
                    variant=best_built.variant,
                    jd_text=jd_text,
                    candidate=candidate,
                )
            )

            # Emphasize missing attested skills, keeping user custom additions
            extra_skills = [
                s for s in refinement.get("extra_skills", [])
                if isinstance(s, str) and s.lower() in candidate_skills_set
            ]
            all_target_skills = list(set(best_score.matched_skills + extra_skills + best_score.missing_attested_skills + skills_to_add + llm_priority_skills))
            new_skills_html = generate_tailored_skills_html(
                candidate=candidate,
                jd_text=jd_text,
                job_title=job_title,
                variant=best_built.variant,
                extra_target_skills=all_target_skills,
                allowed_categories=cats_to_include,
                excluded_categories=cats_to_exclude,
                excluded_skills=skills_to_remove,
                max_skills_per_cat=max_skills_cat,
            )

            if skills_to_remove or cats_to_exclude:
                all_removals = skills_to_remove + (cats_to_exclude or [])
                new_skills_html = remove_skills_from_html(new_skills_html, all_removals)

            loop_summary_override = initial_summary if has_custom_summary else (lock_summary or new_summary)
            loop_skills_override = new_skills_html if has_custom_skills else (lock_skills or new_skills_html)
            loop_projects_override = custom_projects_html if has_custom_projects else (lock_projects or custom_projects_html)

            # Re-build resume with improved summary and skills
            new_built = build_resume_content(
                candidate=candidate,
                jd_text=jd_text,
                job_title=job_title,
                variant=best_built.variant,
                summary_override=loop_summary_override,
                skills_override_html=loop_skills_override,
                projects_override_html=loop_projects_override,
                experience_override_html=lock_experience,
                education_override_html=lock_education,
                certifications_override_html=lock_certifications,
                links_override_html=lock_links,
                max_projects=max_projects,
                recommended_project_ids=llm_recommended_projects,
                section_order=eff_section_order,
            )
        else:
            # Deterministic role-tailored refinement fallback
            fallback_summary = (
                initial_summary
                if has_custom_summary
                else generate_role_tailored_summary(
                    job_title,
                    jd_text=jd_text,
                    variant=best_built.variant,
                    candidate=candidate,
                )
            )
            fallback_skills_html = generate_tailored_skills_html(
                candidate=candidate,
                jd_text=jd_text,
                job_title=job_title,
                variant=best_built.variant,
                extra_target_skills=list(set(best_score.matched_skills + best_score.missing_attested_skills + skills_to_add + llm_priority_skills)),
                allowed_categories=cats_to_include,
                excluded_categories=cats_to_exclude,
                excluded_skills=skills_to_remove,
                max_skills_per_cat=max_skills_cat,
            )
            if skills_to_remove or cats_to_exclude:
                all_removals = skills_to_remove + (cats_to_exclude or [])
                fallback_skills_html = remove_skills_from_html(fallback_skills_html, all_removals)

            loop_summary_override = initial_summary if has_custom_summary else (lock_summary or fallback_summary)
            loop_skills_override = fallback_skills_html if has_custom_skills else (lock_skills or fallback_skills_html)
            loop_projects_override = custom_projects_html if has_custom_projects else (lock_projects or custom_projects_html)

            new_built = build_resume_content(
                candidate=candidate,
                jd_text=jd_text,
                job_title=job_title,
                variant=best_built.variant,
                summary_override=loop_summary_override,
                skills_override_html=loop_skills_override,
                projects_override_html=loop_projects_override,
                experience_override_html=lock_experience,
                education_override_html=lock_education,
                certifications_override_html=lock_certifications,
                links_override_html=lock_links,
                max_projects=max_projects,
                recommended_project_ids=llm_recommended_projects,
                section_order=eff_section_order,
            )

        new_score = score_resume(
            new_built.resume_text,
            jd_text,
            job_title=job_title,
            attested_candidate_skills=candidate.skills,
            candidate=candidate,
        )

        improvement = round(new_score.ats_readiness_score - prev_best, 2)
        gaps_closed = sorted(prev_missing - set(new_score.missing_attested_skills))
        improved = new_score.ats_readiness_score > prev_best
        if improved:
            best_built = new_built
            best_score = new_score
        history.append(
            {
                "attempt": attempt,
                "score": round(new_score.ats_readiness_score, 2),
                "delta": improvement,
                "gaps_closed": gaps_closed,
                "via": "llm" if refinement else "deterministic",
                "note": "New best version." if improved else "No improvement; best version kept.",
            }
        )
        if new_score.ats_readiness_score >= target_score:
            stop_reason = "target_reached"
        elif improvement >= 1.0:
            plateau_streak = 0
        else:
            plateau_streak += 1
            if plateau_streak >= 2:
                stop_reason = "plateau"
    if not stop_reason:
        stop_reason = "max_iterations"

    # --- Render final artifacts ---
    html_filename = f"application_{application_id}.html"
    pdf_filename = f"application_{application_id}.pdf"
    html_path = RESUMES_OUTPUT_DIR / html_filename
    pdf_path = RESUMES_OUTPUT_DIR / pdf_filename

    html_path.write_text(best_built.html_content, encoding="utf-8")

    try:
        render_pdf_from_html(html_path, pdf_path)
    except Exception as exc:
        logger.error(f"Failed to render PDF for application {application_id}: {exc}")
        # PDF failed, keep path empty or log error

    # Honesty check: count actual PDF pages (auto 1→2 when content needs it).
    pdf_actual_pages = 0
    if pdf_path.exists():
        try:
            from app.resume_builder import count_pdf_pages
            pdf_actual_pages = count_pdf_pages(pdf_path)
        except Exception:
            pdf_actual_pages = 0

    return TailorResult(
        resume_pdf_path=str(pdf_path) if pdf_path.exists() else "",
        resume_html_path=str(html_path),
        ats_score=best_score.ats_readiness_score,
        ats_attempts=attempt,  # total iterations run (best version kept is max(history))
        variant=best_built.variant,
        ats_report=best_score,
        iterations=history,
        kb_snapshot_hash=kb_snapshot_hash,
        target_reached=best_score.ats_readiness_score >= target_score,
        stop_reason=stop_reason,
        pdf_actual_pages=pdf_actual_pages,
    )
