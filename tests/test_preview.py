"""Phase 11 checks: read-only preview step (resume as-sent + ATS breakdown + gap report).

Run with:  AUTH_PASSWORD=test123456 python tests/test_preview.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

from fastapi.testclient import TestClient  # noqa: E402

from app.auth import create_session_token, encrypt_credential, hash_password  # noqa: E402
from app.db import get_session, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Application, User  # noqa: E402


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" ({detail})" if detail and not cond else ""))
    if not cond:
        raise SystemExit(f"FAILED: {name} {detail}")


def _client_for(email):
    init_db()
    with get_session() as s:
        u = s.query(User).filter(User.email == email).first()
        if not u:
            u = User(email=email, name="Preview User",
                     password_hash=hash_password("x" * 12), is_active=True)
            s.add(u)
            s.commit()
        # Satisfy the mandatory credentials gate (mirrors a real user
        # completing /credentials before using engine features).
        u.smtp_username = "preview-test@example.com"
        u.smtp_password_encrypted = encrypt_credential("preview-test-app-password")
        u.smtp_verified = True
        s.commit()
        uid = u.id
    token = create_session_token(uid, email)
    client = TestClient(app, raise_server_exceptions=False)
    client.cookies.set("careerpulse_auth", token)
    return client, uid


def _make_app(uid, **kw):
    base = dict(
        user_id=uid,
        email="hr@example.com",
        job_title="AI Engineer",
        company="Acme",
        jd_text="We need Python, FastAPI, and LLM experience. Build RAG systems with vector databases.",
        subject="Application for AI Engineer",
        drafted_email="Dear Hiring Manager,\n\nBody here.\n\nBest regards,\nPreview User",
        # Phase 15: ready (tailoring complete) — only ready rows can be
        # submitted for approval.
        status="ready",
        ats_score=88.0,
        ats_attempts=2,
    )
    base.update(kw)
    with get_session() as s:
        row = Application(**base)
        s.add(row)
        s.commit()
        return row.id


client, uid = _client_for("preview-user@example.com")
other_client, _ = _client_for("preview-other@example.com")
app_id = _make_app(uid)

# 1. Preview renders with all three sections.
r = client.get(f"/application/{app_id}/preview")
check("preview renders 200", r.status_code == 200, r.status_code)
body = r.text
check("preview shows resume-as-sent embed", f"/application/{app_id}/resume.pdf" in body)
check("preview shows ATS breakdown", "ATS breakdown" in body)
check("preview shows gap report", "Gap report" in body)
check("preview shows parsing safety component", "Parsing safety" in body)
check("preview shows relevance component", "Job relevance" in body)

# 2. Preview is read-only: no resume/email edit controls.
low = body.lower()
check("preview has no textarea editors", "<textarea" not in low)
check("preview has no drafted_email edit field", 'name="drafted_email"' not in low)
check("preview has no contenteditable", "contenteditable" not in low)

# 3. Preview offers the next workflow step, not editing.
check("preview has submit-for-approval action", "submit-for-approval" in body)
check("preview links back to editor", f'href="/application/{app_id}"' in body)

# 4. 404 for missing application.
r = client.get("/application/999999/preview")
check("preview 404s on missing app", r.status_code == 404, r.status_code)

# 5. Ownership enforced.
r = other_client.get(f"/application/{app_id}/preview")
check("preview 403s for non-owner", r.status_code == 403, r.status_code)

# 6. Submit for approval: ready -> pending_approval (Phase 15: draft 409s).
r = client.post(f"/application/{app_id}/submit-for-approval", follow_redirects=False)
check("submit-for-approval redirects", r.status_code in (302, 303), r.status_code)
with get_session() as s:
    check("status is pending_approval", s.get(Application, app_id).status == "pending_approval")

# 7. Preview reflects the new status (no resubmit button anymore).
r = client.get(f"/application/{app_id}/preview")
check("preview shows awaiting-approval state", "Awaiting your approval" in r.text)

# 8. Terminal states reject resubmission.
with get_session() as s:
    row = s.get(Application, app_id)
    row.status = "approved"
    s.commit()
r = client.post(f"/application/{app_id}/submit-for-approval", follow_redirects=False)
check("submit-for-approval 409s from approved", r.status_code == 409, r.status_code)
with get_session() as s:
    row = s.get(Application, app_id)
    row.status = "sent"
    s.commit()
r = client.post(f"/application/{app_id}/submit-for-approval", follow_redirects=False)
check("submit-for-approval 409s from sent", r.status_code == 409, r.status_code)

# 9. Submit requires ownership too.
r = other_client.post(f"/application/{app_id}/submit-for-approval", follow_redirects=False)
check("submit-for-approval 403s for non-owner", r.status_code == 403, r.status_code)

print("ALL PREVIEW CHECKS PASSED")
