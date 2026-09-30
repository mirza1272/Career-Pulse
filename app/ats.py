"""ATS Analyzer — Parsing Safety (0 to 40) + Job Relevance (0 to 60).

Total ATS Readiness Score: 0 to 100.
Fully deterministic and offline. Uses local 384-d token/trigram hashing for semantic
similarity without external API calls or latency.

Phase 7 recalibration (§U.2): every point is now traceable to evidence.
Removed: layout 16/16 flat grant, preferred-keyword 5.0/6 grant,
required-keyword 22.0/26 baseline on zero JD hits, title 4.0 baseline,
experience 4.0 fallback. Old-vs-new documented in CHANGELOG.md.

Phase 13 recalibration: two fairness fixes, no free points —
(1) the relevance components only sum to 54, so the score is normalized to
the 0–60 scale over *applicable* components (a 60/54 factor); every point is
still earned from evidence, the scale just stops pretending 60 raw points
exist. (2) A JD that names no technical requirements (e.g. "hiring this")
cannot fail the resume on required/preferred coverage: components with no JD
signal are marked not-applicable and excluded from the denominator instead
of scored 0. A JD that DOES name requirements the candidate lacks still
scores 0 there (genuine miss).
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.matching import TECH_VOCAB, phrase_in_text
from app.skill_aliases import all_phrasings, phrasing_in_text, skill_matches

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b")
PHONE_RE = re.compile(
    r"\b(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b|\b\+92[-.\s]?\d{3}[-.\s]?\d{7}\b"
)
DATE_RE = re.compile(
    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|\d{4})\b", re.IGNORECASE
)

STANDARD_HEADINGS = [
    re.compile(r"\b(?:Summary|Profile|Professional Summary|About Me)\b", re.IGNORECASE),
    re.compile(r"\b(?:Experience|Work Experience|Employment History|Employment)\b", re.IGNORECASE),
    re.compile(r"\b(?:Education|Academic Background)\b", re.IGNORECASE),
    re.compile(r"\b(?:Skills|Technical Skills|Core Competencies)\b", re.IGNORECASE),
    re.compile(r"\b(?:Projects|Key Projects)\b", re.IGNORECASE),
]


class LocalEmbeddingEngine:
    """Deterministic offline embedding engine.

    Produces 384-dimensional normalized vector embeddings with zero external API calls.
    """

    def __init__(self, dimension: int = 384) -> None:
        self.dimension = dimension

    def _tokenize(self, text: str) -> list[str]:
        if not text:
            return []
        cleaned = re.sub(r"[^\w\s]", " ", text.lower())
        tokens = [t for t in cleaned.split() if len(t) > 1]
        trigrams: list[str] = []
        for t in tokens:
            if len(t) >= 3:
                for i in range(len(t) - 2):
                    trigrams.append(t[i : i + 3])
        return tokens + trigrams

    def embed_text(self, text: str) -> list[float]:
        tokens = self._tokenize(text)
        if not tokens:
            return [0.0] * self.dimension

        vec = [0.0] * self.dimension
        for token in tokens:
            h = int(hashlib.md5(token.encode("utf-8")).hexdigest(), 16)
            idx = h % self.dimension
            sign = 1.0 if ((h >> 8) & 1) == 1 else -1.0
            vec[idx] += sign

        sum_sq = sum(x * x for x in vec)
        norm = math.sqrt(sum_sq)
        if norm > 1e-6:
            return [x / norm for x in vec]
        return vec

    @staticmethod
    def cosine_similarity(vec_a: Sequence[float], vec_b: Sequence[float]) -> float:
        if not vec_a or not vec_b:
            return 0.0
        dot = sum(a * b for a, b in zip(vec_a, vec_b))
        norm_a = math.sqrt(sum(a * a for a in vec_a))
        norm_b = math.sqrt(sum(b * b for b in vec_b))
        if norm_a < 1e-6 or norm_b < 1e-6:
            return 0.0
        val = dot / (norm_a * norm_b)
        return float(max(-1.0, min(1.0, val)))


@dataclass
class ATSScore:
    ats_readiness_score: float = 0.0
    parsing_safety_score: float = 0.0
    relevance_score: float = 0.0
    matched_skills: list[str] = field(default_factory=list)
    missing_attested_skills: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def missing_keywords(self) -> list[str]:
        """Alias for missing_attested_skills."""
        return self.missing_attested_skills


def evaluate_parsing_safety(text_content: str) -> tuple[float, list[str]]:
    """Evaluate document-intrinsic parsing safety (0 to 40 points)."""
    warnings: list[str] = []
    if len(text_content.strip()) < 50:
        warnings.append("Text content is too short for ATS parser.")
        return 0.0, warnings

    # 1. Headings (max 8)
    headings_matched = sum(1 for pat in STANDARD_HEADINGS if pat.search(text_content))
    headings_score = round(8.0 * (headings_matched / len(STANDARD_HEADINGS)), 2)
    if headings_matched < 3:
        warnings.append("Standard section headings (Profile, Experience, Education, Skills, Projects) missing.")

    # 2. Contact block (max 8)
    has_email = bool(EMAIL_RE.search(text_content))
    has_phone = bool(PHONE_RE.search(text_content))
    if has_email and has_phone:
        contact_score = 8.0
    elif has_email or has_phone:
        contact_score = 4.0
        warnings.append("Contact block missing either phone number or email address.")
    else:
        contact_score = 0.0
        warnings.append("Contact information (email, phone) could not be parsed.")

    # 3. Date consistency (max 8) — earned per date found; nothing granted.
    date_matches = DATE_RE.findall(text_content)
    date_score = round(min(8.0, 4.0 * len(date_matches)), 2)
    if len(date_matches) < 2:
        warnings.append("Few or no dates found; work/education history may look undated.")

    # 4. Clean single-column layout (max 16) — Phase 7 recalibration: previously
    #    a flat 16.0 grant. Now 4 pts per positive, measurable evidence check.
    layout_score = 0.0
    if not re.search(r"[|\t]| {4,}", text_content):
        layout_score += 4.0  # no table/column artifacts
    else:
        warnings.append("Possible table/column artifacts detected (pipes, tabs, wide spacing).")
    bullet_lines = sum(1 for ln in text_content.splitlines() if re.match(r"\s*[-*•–]", ln))
    if bullet_lines >= 3:
        layout_score += 4.0  # consistent bullet usage
    if headings_matched >= 3:
        layout_score += 4.0  # standard section structure
    if 200 <= len(text_content) <= 12000:
        layout_score += 4.0  # sane, non-truncated content length

    total_safety = round(min(40.0, headings_score + contact_score + date_score + layout_score), 2)
    return total_safety, warnings


def extract_keywords_from_jd(jd_text: str, candidate_skills: list[str]) -> list[str]:
    """Find which candidate-attested skills or notable tech keywords appear in the JD.

    Alias-aware: if the JD says "object-oriented programming" and the KB has
    "OOP", the KB skill is returned (it IS attested, just phrased differently).
    """
    jd_lower = jd_text.lower()
    found: list[str] = []
    for skill in candidate_skills:
        # Direct phrasing OR any known alias phrasing in the JD (hyphen-tolerant)
        phrasings = {skill.lower()} | set(all_phrasings(skill))
        if any(phrasing_in_text(p, jd_lower) for p in phrasings):
            found.append(skill)
    return found


def evaluate_relevance(
    resume_text: str,
    jd_text: str,
    job_title: str = "",
    attested_candidate_skills: list[str] | None = None,
    embedding_engine: LocalEmbeddingEngine | None = None,
) -> tuple[float, list[str], list[str], list[str]]:
    """Evaluate job-relative keyword and semantic relevance (0 to 60 points).

    Phase 13: the score is normalized to the 0–60 scale over the components
    the JD actually gives signal for. Components with no JD signal are
    not-applicable (excluded from the denominator), never 0.
    """
    engine = embedding_engine or LocalEmbeddingEngine(dimension=384)
    attested = attested_candidate_skills or []
    suggestions: list[str] = []
    jd_lower = jd_text.lower()
    resume_lower = resume_text.lower()

    # --- Which components does this JD give signal for? ---
    # target_skills: candidate-attested skills the JD asks for (alias-aware).
    target_skills = extract_keywords_from_jd(jd_text, attested)
    required_lower = {s.casefold() for s in target_skills}
    # Any technical terms the JD names (candidate skill or not).
    jd_tech_terms = [t for t in TECH_VOCAB if phrase_in_text(t, jd_lower)]
    # "Preferred" signal = JD tech terms beyond the required-skill set.
    pref_signal_terms = [t for t in jd_tech_terms if t.casefold() not in required_lower]

    # The five components sum to 54 raw points; N/A components are excluded.
    applicable = 54.0
    # Required coverage is N/A only when the JD names NO technical
    # requirements at all. If the JD names requirements the candidate lacks,
    # that is a genuine miss (0), not N/A.
    required_applicable = bool(target_skills) or bool(jd_tech_terms)
    preferred_applicable = bool(pref_signal_terms)
    if not required_applicable:
        applicable -= 26.0
    if not preferred_applicable:
        applicable -= 6.0

    # 1. Required Keyword Coverage (max 26)
    matched: list[str] = []
    missing_attested: list[str] = []

    for sk in target_skills:
        # Alias-aware: "object-oriented programming" counts as present if the
        # resume says "OOP", and vice versa — same skill, different phrasing.
        phrasings = {sk.lower()} | set(all_phrasings(sk))
        if any(phrasing_in_text(p, resume_lower) for p in phrasings):
            matched.append(sk)
        else:
            missing_attested.append(sk)
            suggestions.append(f"Emphasize attested skill '{sk}' which was mentioned in the job description.")

    if not required_applicable:
        req_score = 0.0  # not-applicable: excluded from the denominator below
    elif target_skills:
        req_score = round(26.0 * (len(matched) / len(target_skills)), 2)
    else:
        # JD names technical requirements but none are attested candidate
        # skills -> genuine miss, 0 points.
        req_score = 0.0

    # 2. Preferred / Additional Keyword Match (max 6): 1.5 pts per distinct
    # tech term present in BOTH the JD and the resume (excluding required
    # skills, already counted — no double count). N/A when the JD has no
    # tech terms beyond the required set.
    if not preferred_applicable:
        pref_score = 0.0  # not-applicable: excluded from the denominator below
    else:
        pref_hits = [t for t in pref_signal_terms if phrase_in_text(t, resume_lower)]
        pref_score = round(min(6.0, 1.5 * len(pref_hits)), 2)

    # 3. Title Alignment (max 8) — Phase 7 recalibration: 4.0 baseline removed.
    title_score = 0.0
    if job_title:
        title_words = [w for w in re.sub(r"[^\w\s]", "", job_title.lower()).split() if len(w) > 3]
        matched_words = sum(1 for w in title_words if w in resume_lower)
        if title_words and matched_words >= len(title_words) * 0.5:
            title_score = 8.0
        elif title_words and matched_words > 0:
            title_score = 6.0
            suggestions.append(f"Align summary headline more closely with target role '{job_title}'.")
        else:
            suggestions.append(f"Align summary profile directly with the target role '{job_title}'.")

    # 4. Experience & Education Alignment (max 8) — Phase 7 recalibration:
    #    was 8.0/4.0 (4.0 fallback). Now 4 pts per section actually present.
    exp_score = 0.0
    if "experience" in resume_lower or "employment" in resume_lower:
        exp_score += 4.0
    else:
        suggestions.append("No experience section detected in the resume text.")
    if re.search(r"\beducation\b", resume_lower):
        exp_score += 4.0
    else:
        suggestions.append("No education section detected in the resume text.")

    # 5. Semantic Cosine Relevance (max 6)
    v_resume = engine.embed_text(resume_text[:1200])
    v_jd = engine.embed_text(f"{job_title} {jd_text[:1000]}")
    sim = max(0.0, engine.cosine_similarity(v_resume, v_jd))
    sem_score = round(6.0 * sim, 2)

    # Phase 13: normalize earned points to the 0–60 scale over applicable
    # components (raw max is 54, and N/A components are excluded). Every
    # point is still earned from evidence — the scale just matches the
    # documented 0–60 range.
    earned = req_score + pref_score + title_score + exp_score + sem_score
    if applicable > 0:
        total_relevance = round(max(0.0, min(60.0, 60.0 * earned / applicable)), 2)
    else:
        total_relevance = 0.0

    if not suggestions:
        matched_unique = list(dict.fromkeys(matched))
        if matched_unique:
            suggestions.append(f"Strong keyword match for {job_title or 'this role'}! Key matching skills: {', '.join(matched_unique[:5])}.")
            suggestions.append("Ensure your tailored cover letter highlights your top relevant projects and impact metrics.")
        else:
            suggestions.append(f"Ensure your resume summary clearly mentions '{job_title or 'your core role'}' and relevant technical stack.")
            suggestions.append("Check the JD requirements and add any missing verified skills in your Knowledge Base.")

    return total_relevance, matched, missing_attested, suggestions


def check_candidate_87_eligibility(candidate: object | None) -> tuple[bool, str]:
    """Check if the candidate meets the baseline qualifications for 87+ ATS optimization:
    - At least 1 experience entry (len(candidate.experience) >= 1)
    - At least 5 projects (len(candidate.projects) >= 5)
    - At least 5 skills (len(candidate.skills) >= 5)

    Returns (is_eligible, explanatory_note). Retargeted 85 -> 87 in Phase 8 (§U.1).
    """
    if not candidate:
        return False, "Knowledge Base profile is empty. 87+ optimization requires at least 1 work experience, 5 projects, and 5-8 skills."

    exp_count = len(getattr(candidate, "experience", []) or [])
    proj_count = len(getattr(candidate, "projects", []) or [])
    skill_count = len(getattr(candidate, "skills", []) or [])

    reasons: list[str] = []
    if exp_count < 1:
        reasons.append("0 work experience entries (min. 1 required)")
    if proj_count < 5:
        reasons.append(f"{proj_count}/5 projects (min. 5 required)")
    if skill_count < 5:
        reasons.append(f"{skill_count}/5 skills (min. 5-8 required)")

    if reasons:
        note = (
            "ATS score reflects available facts: 87+ optimization cap is inactive because your Knowledge Base is missing: "
            + "; ".join(reasons)
            + ". Add these in your Knowledge Base tab to unlock full 87+ ATS optimization."
        )
        return False, note

    return True, ""


def score_resume(
    resume_text: str,
    jd_text: str,
    job_title: str = "",
    attested_candidate_skills: list[str] | None = None,
    candidate: object | None = None,
) -> ATSScore:
    """Run complete ATS Readiness Analysis: Parsing Safety (0-40) + Job Relevance (0-60)."""
    safety_score, warnings = evaluate_parsing_safety(resume_text)
    relevance_score, matched, missing_attested, suggestions = evaluate_relevance(
        resume_text=resume_text,
        jd_text=jd_text,
        job_title=job_title,
        attested_candidate_skills=attested_candidate_skills,
    )
    total = round(max(0.0, min(100.0, safety_score + relevance_score)), 1)
    
    note = ""
    if candidate is not None:
        eligible, cand_note = check_candidate_87_eligibility(candidate)
        if not eligible:
            note = cand_note

    return ATSScore(
        ats_readiness_score=total,
        parsing_safety_score=safety_score,
        relevance_score=relevance_score,
        matched_skills=matched,
        missing_attested_skills=missing_attested,
        suggestions=suggestions,
        warnings=warnings,
        note=note,
    )
