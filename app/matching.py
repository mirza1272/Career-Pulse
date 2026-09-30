"""JD <-> KB matching: the gap report shown before generation (Phase 5).

`match_jd_to_kb` compares a job description against the candidate's knowledge
base and returns a MatchReport: which JD-required skills the candidate has
(matched), which are missing, and how relevant each KB project is. The /new UI
renders this report on a review screen so the user sees the gaps BEFORE any
resume is generated; the report also feeds the optimizer via
`MatchReport.optimizer_focus` (passed to the tailor step as custom_focus).
"""
from __future__ import annotations

import logging
import re

from app.skill_aliases import skill_matches

logger = logging.getLogger("careerpulse.matching")

# Curated tech vocabulary for the deterministic fallback path (used only when
# the LLM is unreachable). Keyword spotting, not a capability claim.
# Also reused by app.ats for evidence-based preferred-keyword scoring.
TECH_VOCAB = [
    "python", "java", "javascript", "typescript", "c++", "c#", "go", "rust",
    "sql", "nosql", "postgres", "postgresql", "mysql", "mongodb", "redis",
    "chromadb", "falkordb", "supabase", "pydantic", "react", "next.js",
    "node.js", "node", "express", "fastapi", "django", "flask", "tensorflow",
    "pytorch", "keras", "scikit-learn", "sklearn", "langchain", "llamaindex",
    "openai", "huggingface", "transformers", "rag", "llm", "llms", "nlp",
    "computer vision", "cnn", "ann", "deep learning", "machine learning",
    "mlops", "docker", "kubernetes", "aws", "azure", "gcp", "git", "ci/cd",
    "linux", "rest", "rest api", "apis", "graphql", "kafka", "spark",
    "airflow", "pandas", "numpy", "matplotlib", "seaborn", "qdrant", "pinecone",
    "weaviate", "elasticsearch", "neo4j", "langgraph", "autogen", "crewai",
    "vapi", "voice ai", "groq", "gemini", "n8n", "chainlit", "streamlit", "gradio",
    "mcp", "fastmcp", "vector search", "knowledge graphs", "data pipelines",
    "data preprocessing", "predictive modeling", "security", "ssrf protection",
    "http clients", "automation", "optimization", "tailwind css",
]


def _keep_case(token: str) -> str:
    """Display-case a skill: keep symbol/digit tokens (c++, node.js) as-is, title-case the rest."""
    if re.search(r"[+#./]|\d", token):
        return token
    return token.title()


def _norm(skill: str) -> str:
    return re.sub(r"\s+", " ", (skill or "").strip().casefold())


def phrase_in_text(phrase: str, text_lower: str) -> bool:
    """Token-aware containment that also handles symbols like c++/c#."""
    p = phrase.strip().casefold()
    if not p:
        return False
    if re.fullmatch(r"[a-z0-9+#.]+", p):
        return re.search(rf"(?<![a-z0-9+#.]){re.escape(p)}(?![a-z0-9+#.])", text_lower) is not None
    return re.search(rf"\b{re.escape(p)}\b", text_lower) is not None


class ProjectMatch:
    def __init__(self, name: str, relevance: float, matched_skills: list[str]) -> None:
        self.name = name
        self.relevance = relevance  # 0..1
        self.matched_skills = matched_skills

    def to_dict(self) -> dict:
        return {"name": self.name, "relevance": round(self.relevance, 2),
                "matched_skills": self.matched_skills}


class MatchReport:
    """Gap report for one (JD, role, candidate) triple."""

    def __init__(
        self,
        role: str = "",
        required_skills: list[str] | None = None,
        matched_skills: list[str] | None = None,
        missing_skills: list[str] | None = None,
        project_matches: list[ProjectMatch] | None = None,
    ) -> None:
        self.role = role
        self.required_skills = required_skills or []
        self.matched_skills = matched_skills or []
        self.missing_skills = missing_skills or []
        self.project_matches = project_matches or []

    @property
    def coverage(self) -> float:
        if not self.required_skills:
            return 1.0
        return round(len(self.matched_skills) / len(self.required_skills), 2)

    @property
    def optimizer_focus(self) -> str:
        """One-paragraph brief for the tailor/optimizer step."""
        bits = []
        if self.role:
            bits.append(f"Target role: {self.role}.")
        if self.missing_skills:
            bits.append("JD requires these skills the candidate lacks; do NOT invent them, "
                        f"but avoid spotlighting the gap: {', '.join(self.missing_skills[:8])}.")
        if self.matched_skills:
            bits.append(f"Emphasize these matched skills: {', '.join(self.matched_skills[:10])}.")
        top = [p.name for p in self.project_matches[:3] if p.relevance > 0]
        if top:
            bits.append(f"Most relevant projects for this role: {', '.join(top)}.")
        return " ".join(bits)

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "required_skills": self.required_skills,
            "matched_skills": self.matched_skills,
            "missing_skills": self.missing_skills,
            "coverage": self.coverage,
            "optimizer_focus": self.optimizer_focus,
            "project_matches": [p.to_dict() for p in self.project_matches],
        }


def extract_required_skills(jd_text: str, role: str = "") -> list[str]:
    """Skills the JD asks for. LLM structured JSON first, keyword scan fallback."""
    text = (jd_text or "").strip()
    if not text:
        return []

    try:
        from app.llm import call_llm_json

        role_hint = f" for the '{role}' position" if role else ""
        data = call_llm_json(
            system_prompt=(
                "You extract required technical skills from a job posting. "
                "Return ONLY a JSON object: {\"required_skills\": [\"skill 1\", \"skill 2\"]}."
            ),
            user_prompt=(
                f"List the technical skills, tools, frameworks and technologies this job "
                f"posting requires{role_hint}.\n"
                "Rules:\n"
                "- Only items explicitly mentioned or clearly required by the posting. Never invent.\n"
                "- Use concise canonical names (\"Python\", not \"strong Python programming skills\").\n"
                "- Deduplicate. At most 20 entries.\n\n"
                f"POSTING:\n{text[:6000]}"
            ),
            temperature=0.1,
            timeout=25.0,
        )
        if data:
            skills = [_norm(str(s)) for s in (data.get("required_skills") or []) if str(s).strip()]
            skills = [s for s in dict.fromkeys(skills) if s][:20]
            if skills:
                # Title-case for display, keeping symbols intact.
                return [_keep_case(s) for s in skills]
    except Exception as exc:
        logger.warning("extract_required_skills LLM failed, using keyword fallback: %s", exc)

    jd_lower = text.lower()
    found = [t for t in TECH_VOCAB if phrase_in_text(t, jd_lower)]
    return [_keep_case(t) for t in found[:20]]


def _project_tokens(project: dict) -> set[str]:
    tokens: set[str] = set()
    for skill in project.get("skills") or []:
        tokens.add(_norm(str(skill)))
    blob = " ".join(str(project.get(k) or "") for k in ("name", "subtitle", "bullet")).lower()
    for word in re.findall(r"[a-z][a-z0-9+#.]*", blob):
        tokens.add(word)
    return tokens


def match_jd_to_kb(jd_text: str, role: str = "", candidate=None) -> MatchReport:
    """Build the gap report for a (JD, role, candidate) triple. Never raises."""
    try:
        skills = list(getattr(candidate, "skills", "") or [])
        projects = list(getattr(candidate, "projects", "") or [])

        required = extract_required_skills(jd_text, role)
        norm_have = {_norm(s): s for s in skills}

        matched, missing = [], []
        for req in required:
            key = _norm(req)
            if key in norm_have:
                matched.append(norm_have[key])
            else:
                # Alias-aware second chance: "Object-Oriented Programming" (JD)
                # vs "OOP" (KB), "AI Agents" (JD) vs "Agentic AI" (KB).
                hit = next((orig for k, orig in norm_have.items()
                            if k and skill_matches(k, key)), "")
                if not hit:
                    # Legacy substring-level third chance ("Next.js" vs "nextjs").
                    hit = next((orig for k, orig in norm_have.items()
                                if k and (k in key or key in k)), "")
                (matched if hit else missing).append(hit or req)

        project_matches: list[ProjectMatch] = []
        req_norm = {_norm(r) for r in required}
        for proj in projects:
            name = str(proj.get("name") or "Untitled project")
            tokens = _project_tokens(proj)
            hit_norm = {r for r in req_norm if r in tokens or any(r in t or t in r for t in tokens if len(t) > 2)}
            relevance = (len(hit_norm) / len(req_norm)) if req_norm else 0.0
            display = sorted({s for s in skills if _norm(s) in hit_norm})
            project_matches.append(ProjectMatch(name, round(relevance, 2), display))
        project_matches.sort(key=lambda p: p.relevance, reverse=True)

        return MatchReport(role=role, required_skills=required,
                           matched_skills=matched, missing_skills=missing,
                           project_matches=project_matches[:6])
    except Exception as exc:  # never break intake on matching
        logger.warning("match_jd_to_kb failed: %s", exc)
        return MatchReport(role=role)
