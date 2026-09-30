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

# --- integration: /new UI gate ---
app = create_app()


def _authed_client():
    init_db()
    with get_session() as s:
        admin = s.query(User).filter(User.email == config.ADMIN_EMAIL.strip().lower()).first()
        assert admin, "admin user missing (set AUTH_PASSWORD so bootstrap runs)"
        if not (admin.smtp_username and admin.smtp_password_encrypted):
            admin.smtp_username = "test@example.com"
            admin.smtp_password_encrypted = encrypt_credential("test-app-password")
            admin.smtp_verified = True
            s.commit()
        uid, email = admin.id, admin.email
        before = s.query(Application).count()
    token = create_session_token(uid, email)
    client = TestClient(app)
    client.cookies.set("careerpulse_auth", token)
    return client, before


client, before = _authed_client()
r = client.post("/new", data={"jd_text": MULTI_JD, "email": "hr@acme.test"})
check("multi-role POST /new shows picker (no generation)", r.status_code == 200 and "Multiple roles detected" in r.text, f"status={r.status_code}")
with get_session() as s:
    check("picker step creates no application", s.query(Application).count() == before)

# Extract hidden fields from the picker HTML.
import re as _re
m_hash = _re.search(r'name="role_jd_hash" value="([^"]+)"', r.text)
m_roles = _re.search(r"name=\"detected_roles_json\" value='([^']+)'", r.text)
check("picker carries jd hash + detected roles", bool(m_hash and m_roles))
detected = json.loads(html.unescape(m_roles.group(1)))
check("picker lists all detected roles", set(detected) == {"Software Engineer", "Data Scientist", "DevOps Engineer"}, f"got {detected}")
for role_name in detected:
    check(f"picker shows {role_name}", role_name in r.text)

# Second submit: explicit pick -> gap-report review screen (Phase 5), still no generation.
r2 = client.post(
    "/new",
    data={
        "jd_text": MULTI_JD,
        "email": "hr@acme.test",
        "role_decision": "pick:Data Scientist",
        "role_jd_hash": m_hash.group(1),
        "detected_roles_json": html.unescape(m_roles.group(1)),
    },
    follow_redirects=False,
)
check("pick submit -> review screen", r2.status_code == 200 and "how this job matches your profile" in r2.text, f"status={r2.status_code}")
check("review shows resolved role", "Data Scientist" in r2.text)
check("review shows matched + gaps", "Matched strengths" in r2.text and "Gaps" in r2.text)
with get_session() as s:
    check("review step creates no application", s.query(Application).count() == before)

# Extract the confirmed text from the review screen, then generate.
m_conf = _re.search(r'name="confirmed_text" value="([^"]*)"', r2.text)
check("review carries confirmed text", bool(m_conf))
confirmed = html.unescape(m_conf.group(1)).replace("&#10;", "\n")
r2b = client.post(
    "/new",
    data={
        "stage": "review",
        "review_action": "generate",
        "email": "hr@acme.test",
        "job_title": "",
        "target_role": "Data Scientist",
        "company": "",
        "link": "",
        "confirmed_text": confirmed,
    },
    follow_redirects=False,
)
check("generate submit -> redirect to application", r2b.status_code == 303 and "/application/" in r2b.headers.get("location", ""), f"status={r2b.status_code}")
with get_session() as s:
    row = s.query(Application).order_by(Application.id.desc()).first()
    check("resolved role stored as job_title", row is not None and row.job_title == "Data Scientist", f"got {row.job_title if row else None}")

# Skip path: "let AI choose" -> review screen, then generate.
client2, _ = _authed_client()
r3 = client2.post("/new", data={"jd_text": MULTI_JD, "email": "hr@acme.test"})
m_hash3 = _re.search(r'name="role_jd_hash" value="([^"]+)"', r3.text)
m_roles3 = _re.search(r"name=\"detected_roles_json\" value='([^']+)'", r3.text)
r4 = client2.post(
    "/new",
    data={
        "jd_text": MULTI_JD,
        "email": "hr@acme.test",
        "role_decision": "ai",
        "role_jd_hash": m_hash3.group(1),
        "detected_roles_json": html.unescape(m_roles3.group(1)),
    },
    follow_redirects=False,
)
check("AI-skip submit -> review screen", r4.status_code == 200 and "how this job matches your profile" in r4.text, f"status={r4.status_code}")
m_conf4 = _re.search(r'name="confirmed_text" value="([^"]*)"', r4.text)
r4b = client2.post(
    "/new",
    data={
        "stage": "review",
        "review_action": "generate",
        "email": "hr@acme.test",
        "target_role": "Software Engineer",
        "confirmed_text": html.unescape(m_conf4.group(1)).replace("&#10;", "\n"),
    },
    follow_redirects=False,
)
check("AI-skip generate -> redirect to application", r4b.status_code == 303, f"status={r4b.status_code}")
with get_session() as s:
    row = s.query(Application).order_by(Application.id.desc()).first()
    check("AI pick stored as job_title", row.job_title in ("Software Engineer", "Data Scientist", "DevOps Engineer"), f"got {row.job_title}")

# Single-role JD: straight to the review screen (gaps visible before generation).
r5 = client.post("/new", data={"jd_text": SINGLE_JD, "email": "hr@acme.test"}, follow_redirects=False)
check("single-role JD -> review screen, no picker", r5.status_code == 200 and "how this job matches your profile" in r5.text and "Multiple roles detected" not in r5.text, f"status={r5.status_code}")

# Edit action: back to the form with values preserved.
m_conf5 = _re.search(r'name="confirmed_text" value="([^"]*)"', r5.text)
r6 = client.post(
    "/new",
    data={"stage": "review", "review_action": "edit", "jd_text": SINGLE_JD, "email": "hr@acme.test"},
    follow_redirects=False,
)
check("edit action returns to form", r6.status_code == 200 and "how this job matches your profile" not in r6.text and "jd_text_input" in r6.text, f"status={r6.status_code}")

print("ALL ROLE CHECKS PASSED")
