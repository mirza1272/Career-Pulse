"""Role detection + best-fit role selection in a single LLM call.

A job posting can advertise several openings ("we are hiring X, Y and Z").
detect_and_select_role extracts the distinct roles AND picks the single
best-fit role for the candidate's KB profile in ONE structured LLM call:

    structured input : JD text + candidate summary + key skills
    structured output: {"roles": [...], "best_role": "<exact>", "reason": "<1 sentence>"}

Resolution never blocks the intake flow:
    user_pick set -> used as-is (no LLM call at all)
    0 roles       -> job title fallback
    1 role        -> that role
    N roles       -> best_role auto-selected from the same call

Deterministic rule-based fallback when the LLM is unreachable.
"""
from __future__ import annotations

import hashlib
import logging
import re

logger = logging.getLogger("careerpulse.roles")

# Resolution methods (how the role was resolved)
M_AUTO_NONE = "auto_none"          # no roles found; fall back to job title
M_AUTO_SINGLE = "auto_single"      # exactly one role detected
M_USER_PROVIDED = "user_provided"  # user typed a role not among detected
M_USER_PICK = "user_pick"          # user picked one of the detected roles
M_AI_PICK = "ai_pick"              # AI selected the best fit in one LLM call

_MAX_ROLES = 6


def jd_hash(jd_text: str) -> str:
    """Stable fingerprint of the JD text a detection ran on."""
    return hashlib.sha256((jd_text or "").strip().encode("utf-8")).hexdigest()[:16]


class RoleResolution:
    """Outcome of detect_and_select_role. Always resolved — never blocks."""

    def __init__(
        self,
        roles: list[str] | None = None,
        role: str = "",
        method: str = "",
        reason: str = "",
    ) -> None:
        self.roles = roles or []
        self.role = role
        self.method = method
        self.reason = reason

    @property
    def resolved(self) -> bool:
        return True

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return (
            f"RoleResolution(role={self.role!r}, "
            f"method={self.method}, roles={self.roles})"
        )


def _clean_role(raw: str) -> str:
    cleaned = re.sub(r"\s+", " ", (raw or "")).strip(" -–—•*:\t")
    cleaned = re.sub(r"^(?:we are |we're |looking for |hiring |open role:? |position:? )\s*",
                     "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip().strip(".")


def _dedupe(roles: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for r in roles:
        key = r.casefold()
        if r and key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _rule_based_detect(jd_text: str, job_title: str = "") -> list[str]:
    """Deterministic fallback when the LLM is unreachable.

    Catches the common multi-role shapes: a header line mentioning
    hiring/positions/openings/roles followed by bullet lines, or an inline
    comma/and-separated role list.
    """
    text = (jd_text or "").strip()
    if not text:
        return [job_title.strip()] if job_title.strip() else []

    roles: list[str] = []
    lines = [ln.strip() for ln in text.splitlines()]
    header_hit = False
    for i, ln in enumerate(lines):
        low = ln.lower()
        if re.search(r"\b(hiring|openings?|positions?|vacanc\w+|roles?)\b", low) and (
            low.endswith(":") or "following" in low or i + 1 < len(lines)
        ):
            # Collect following bullet/numbered lines as candidate roles.
            for nxt in lines[i + 1 : i + 12]:
                m = re.match(r"^(?:[-•*–—]|\d+[.)])\s*(.+)$", nxt)
                if not m:
                    if nxt and len(nxt) < 80 and not nxt.endswith((".", "!", "?")):
                        cand = _clean_role(nxt)
                        if 2 < len(cand) < 80:
                            roles.append(cand)
                    continue
                cand = _clean_role(m.group(1))
                # Bullet lines are usually short titles; skip sentence-like lines.
                if 2 < len(cand) < 80 and len(cand.split()) <= 8:
                    roles.append(cand)
                elif roles:
                    break
            if roles:
                header_hit = True
                break
        # Inline shape: "Open roles: A, B and C"
        m = re.match(r"(?i)^(?:open\s+)?(?:roles?|positions?)\s*:\s*(.+)$", ln)
        if m and not header_hit:
            parts = re.split(r"\s*(?:,|;|\band\b|&)\s*", m.group(1))
            for p in parts:
                cand = _clean_role(p)
                if 2 < len(cand) < 60:
                    roles.append(cand)

    roles = _dedupe(roles)[:_MAX_ROLES]
    if not roles and job_title.strip():
        return [job_title.strip()]
    return roles


def _profile_bits(candidate) -> str:
    """Compact candidate profile for the structured LLM input."""
    bits = str(getattr(candidate, "summary", "") or "").strip()
    skills = [str(s) for s in (getattr(candidate, "skills", "") or [])][:20]
    if skills:
        bits += ("\n" if bits else "") + "Key skills: " + ", ".join(skills)
    return bits


def _match_pick(pick: str, roles: list[str]) -> str:
    """Fuzzy-match a pick against detected roles (case-insensitive)."""
    norm = pick.strip().casefold()
    for r in roles:
        if r.casefold() == norm:
            return r
    for r in roles:  # substring containment either way
        rl = r.casefold()
        if norm in rl or rl in norm:
            return r
    return ""


def _fallback_resolution(jd_text: str, job_title: str) -> RoleResolution:
    """Deterministic rule-based resolution when the LLM is unreachable."""
    roles = _rule_based_detect((jd_text or "").strip(), job_title)
    role = roles[0] if roles else ""
    logger.info("role resolved via rule-based fallback: %r from %s", role, roles)
    return RoleResolution(
        roles=roles,
        role=role,
        method=M_AUTO_SINGLE if len(roles) == 1 else M_AUTO_NONE,
        reason="rule-based fallback",
    )


def detect_and_select_role(
    jd_text: str,
    job_title: str = "",
    user_pick: str = "",
    candidate=None,
) -> RoleResolution:
    """Detect roles AND pick the best-fit role in ONE structured LLM call.

    Never raises and never blocks: user picks are used as-is (no LLM call),
    and a deterministic rule-based fallback covers LLM outages.
    """
    text = (jd_text or "").strip()

    pick = (user_pick or "").strip()
    if pick:
        detected = _rule_based_detect(text, job_title)
        match = _match_pick(pick, detected)
        return RoleResolution(
            roles=detected,
            role=match or pick,
            method=M_USER_PICK if match else M_USER_PROVIDED,
            reason="matched a detected role" if match else "typed by the user; not among detected roles",
        )

    if not text:
        title = job_title.strip()
        return RoleResolution(
            roles=[title] if title else [],
            role=title,
            method=M_AUTO_SINGLE if title else M_AUTO_NONE,
            reason="empty posting; using job title" if title else "empty posting",
        )

    # Fast path: very short pastes are almost always a single role.
    if len(text) < 120:
        return _fallback_resolution(text, job_title)

    profile = _profile_bits(candidate)
    try:
        from app.llm import call_llm_json

        data = call_llm_json(
            system_prompt=(
                "You analyze a job posting and pick the best-fit role for a candidate. "
                "Return ONLY a JSON object: "
                "{\"roles\": [\"role 1\", \"role 2\"], "
                "\"best_role\": \"<one of the roles, copied verbatim>\", "
                "\"reason\": \"<one sentence>\"}."
            ),
            user_prompt=(
                "JOB POSTING:\n" + text[:6000] + "\n\n"
                "CANDIDATE PROFILE:\n" + (profile or "(no profile)") + "\n\n"
                "Rules:\n"
                "- \"roles\": every DISTINCT role/position the posting hires for. "
                "Only roles actually mentioned. Never invent roles.\n"
                "- Merge seniority variants of the same role family "
                "(\"Senior Python Developer\" + \"Junior Python Developer\" -> \"Python Developer\").\n"
                "- Use the exact title wording from the posting.\n"
                "- Single-role posting -> exactly one entry. No clear role -> [].\n"
                "- At most 6 entries.\n"
                "- \"best_role\": the ONE role from \"roles\" that best fits the "
                "candidate profile (copy its string verbatim). With exactly one "
                "role, \"best_role\" must be that role.\n"
                "- \"reason\": one sentence why it fits."
            ),
            temperature=0.1,
            timeout=30.0,
        )
        if data:
            raw = data.get("roles") or []
            roles = _dedupe([_clean_role(str(r)) for r in raw if str(r).strip()])
            roles = [r for r in roles if r][:_MAX_ROLES]
            if roles:
                best = str(data.get("best_role") or "").strip()
                match = _match_pick(best, roles) if best else ""
                role = match or roles[0]
                reason = str(data.get("reason") or "").strip()
                method = M_AUTO_SINGLE if len(roles) == 1 else M_AI_PICK
                logger.info("role resolved in one LLM call via %s: %r from %s", method, role, roles)
                return RoleResolution(
                    roles=roles,
                    role=role,
                    method=method,
                    reason=reason
                    or ("single role detected" if len(roles) == 1 else "best match for the candidate profile"),
                )
            title = job_title.strip()
            return RoleResolution(
                roles=[title] if title else [],
                role=title,
                method=M_AUTO_SINGLE if title else M_AUTO_NONE,
                reason="LLM found no clear role; using job title" if title else "LLM found no clear role",
            )
    except Exception as exc:  # fail soft -> deterministic fallback
        logger.warning("detect_and_select_role LLM failed, using rule-based fallback: %s", exc)

    return _fallback_resolution(text, job_title)
