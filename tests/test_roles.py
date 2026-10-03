"""Phase 4 checks: role detection + the role-selection state machine."""
import html
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

from fastapi.testclient import TestClient  # noqa: E402

from app import config  # noqa: E402
from app.auth import create_session_token, encrypt_credential  # noqa: E402
from app.db import get_session, init_db  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import Application, User  # noqa: E402
from app.roles import (  # noqa: E402
    AI_SELECTS_ROLE,
    JD_PARSED,
    ROLES_DETECTED,
    ROLE_RESOLVED,
    RoleResolution,
    ai_select_role,
    detect_roles,
    jd_hash,
    resolve_role,
)

MULTI_JD = """Join our growing team! We are hiring for the following positions:
- Software Engineer
- Data Scientist
- DevOps Engineer

All roles are remote-friendly. Apply with your resume today."""

SINGLE_JD = """We are looking for a Senior AI Engineer to join our ML platform team.
You will build RAG pipelines with Python, LangChain and Qdrant."""


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" ({detail})" if detail and not cond else ""))
    if not cond:
        raise SystemExit(f"FAILED: {name} {detail}")


def run_tests():
    # --- unit: state machine (roles injected, no LLM involved) ---
    r = resolve_role("x", roles=[])
    check("no roles -> auto_none/RESOLVED", r.resolved and r.method == "auto_none" and r.role == "")

    r = resolve_role("x", roles=["Data Scientist"])
    check("one role -> auto_single/RESOLVED", r.resolved and r.method == "auto_single" and r.role == "Data Scientist")

    r = resolve_role("x", roles=["Software Engineer", "Data Scientist"], user_pick="data scientist")
    check("user pick (fuzzy) -> user_pick", r.resolved and r.method == "user_pick" and r.role == "Data Scientist")

    r = resolve_role("x", roles=["Software Engineer", "Data Scientist"], user_pick="ML Engineer")
    check("user typed other -> user_provided", r.resolved and r.method == "user_provided" and r.role == "ML Engineer")

    r = resolve_role("x", roles=["Software Engineer", "Data Scientist"], skip_ai=True)
    check("skip -> AI_SELECTS_ROLE -> ai_pick", r.resolved and r.method == "ai_pick" and r.role in ("Software Engineer", "Data Scientist"), f"got {r!r}")

    r = resolve_role("x", roles=["Software Engineer", "Data Scientist"])
    check("multi-role, no decision -> gate trips", (not r.resolved) and r.state == ROLES_DETECTED and len(r.roles) == 2)

    check("RoleResolution defaults to JD_PARSED", RoleResolution().state == JD_PARSED)
    check("AI_SELECTS_ROLE state constant exists", AI_SELECTS_ROLE == "AI_SELECTS_ROLE")

    # --- unit: detector (rule-based fallback; no Groq keys in this env) ---
    roles = detect_roles(MULTI_JD)
    check("multi-role JD -> 3 roles", len(roles) == 3, f"got {roles}")
    check("role names kept verbatim", "Data Scientist" in roles and "DevOps Engineer" in roles, f"got {roles}")

    roles = detect_roles(SINGLE_JD, job_title="AI Engineer")
    check("single-role JD -> 1 role", len(roles) == 1, f"got {roles}")

    roles = detect_roles("", job_title="AI Engineer")
    check("empty JD + job title -> title", roles == ["AI Engineer"], f"got {roles}")

    check("jd_hash stable", jd_hash("abc") == jd_hash("abc") and jd_hash("abc") != jd_hash("abd"))

    role, reason = ai_select_role(MULTI_JD, ["Software Engineer", "Data Scientist"])
    check("ai_select_role fallback -> first role", role == "Software Engineer" and reason, f"got {(role, reason)}")

    print("ALL ROLE CHECKS PASSED")


if __name__ == "__main__":
    run_tests()

