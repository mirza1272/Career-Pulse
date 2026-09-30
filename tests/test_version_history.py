"""Phase 16 checks (FR-R-03): version history list + restore with locks winning.

- detail page lists versions (newest first, current marked)
- restore re-applies current manual-edit locks via replace_regions()
  (locks win over restored AI content)
- restore records a new version (undoable), recomputes ATS, resets
  approval to ready
- 404 on missing version, 403 for non-owner

Run with:  AUTH_PASSWORD=test123456 python tests/test_version_history.py
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

from fastapi.testclient import TestClient  # noqa: E402

from app.auth import create_session_token, encrypt_credential, hash_password  # noqa: E402
from app.db import get_session, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Application, ResumeVersion, User  # noqa: E402
from app.resume_builder import RESUMES_OUTPUT_DIR, extract_regions  # noqa: E402


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" ({detail})" if detail and not cond else ""))
    if not cond:
        raise SystemExit(f"FAILED: {name} {detail}")


V1_HTML = """<html><body>
<!--REGION:SUMMARY--><p>AI-generated summary v1.</p><!--END:SUMMARY-->
<!--REGION:SKILLS--><p>Python, FastAPI</p><!--END:SKILLS-->
</body></html>"""
V2_HTML = """<html><body>
<!--REGION:SUMMARY--><p>AI-generated summary v2, rewritten.</p><!--END:SUMMARY-->
<!--REGION:SKILLS--><p>Python, FastAPI, LLMs</p><!--END:SKILLS-->
</body></html>"""
LOCKED_SUMMARY = "<p>MY HAND-WRITTEN SUMMARY — never wipe this.</p>"


def _client_for(email):
    init_db()
    with get_session() as s:
        u = s.query(User).filter(User.email == email).first()
        if not u:
            u = User(email=email, name="Ver User",
                     password_hash=hash_password("x" * 12), is_active=True)
            s.add(u)
            s.commit()
        u.smtp_username = "ver-test@example.com"
        u.smtp_password_encrypted = encrypt_credential("ver-test-app-password")
        u.smtp_verified = True
        s.commit()
        uid = u.id
        s.query(Application).filter(Application.user_id == uid).delete()
        s.commit()
    token = create_session_token(uid, email)
    client = TestClient(app, raise_server_exceptions=False)
    client.cookies.set("careerpulse_auth", token)
    return client, uid


def _seed(uid, status="approved"):
    RESUMES_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with get_session() as s:
        row = Application(user_id=uid, email="hr@x.com", job_title="Ver Role",
                          company="Acme", jd_text="Python and FastAPI needed.",
                          status=status,
                          resume_locks_json=json.dumps({"SUMMARY": LOCKED_SUMMARY}))
        s.add(row)
        s.flush()
        app_id = row.id
        for i, html in ((1, V1_HTML), (2, V2_HTML)):
            p = RESUMES_OUTPUT_DIR / f"application_{app_id}_v{i}.html"
            p.write_text(html, encoding="utf-8")
            s.add(ResumeVersion(application_id=app_id, version_no=i,
                                role="Ver Role", jd_text="Python",
                                resume_path=str(p), ats_score=80.0 + i,
                                iterations=1, score_history_json="[]"))
        # Current file = v2 content (as if v2 is live).
        (RESUMES_OUTPUT_DIR / f"application_{app_id}.html").write_text(V2_HTML, encoding="utf-8")
        s.commit()
        return app_id


client, uid = _client_for("ver-user@example.com")
other_client, _ = _client_for("ver-other@example.com")
app_id = _seed(uid)


def _version_count(aid):
    with get_session() as s:
        return s.query(ResumeVersion).filter_by(application_id=aid).count()


# 1. List UI.
r = client.get(f"/application/{app_id}")
check("detail renders", r.status_code == 200, r.status_code)
check("version history section present", "Version History" in r.text)
check("v1 and v2 listed", "v1" in r.text and "v2" in r.text)
check("current version marked", ">current<" in r.text)
check("restore buttons present", f"/resume/restore/1" in r.text)
check("no restore button on current version",
      f"/resume/restore/2" not in r.text)

# 2. Restore v1: locks win over restored AI content.
n_before = _version_count(app_id)
r = client.post(f"/application/{app_id}/resume/restore/1", follow_redirects=False)
check("restore redirects", r.status_code in (302, 303), r.status_code)
check("restore banner param", "restored=v1" in r.headers.get("location", ""),
      r.headers.get("location"))

cur_html = (RESUMES_OUTPUT_DIR / f"application_{app_id}.html").read_text(encoding="utf-8")
regions = extract_regions(cur_html)
check("restored base is v1 (SKILLS from v1)",
      "LLMs" not in regions.get("SKILLS", ""), regions.get("SKILLS", "")[:60])
check("manual-edit lock wins over restored AI content (SUMMARY)",
      "MY HAND-WRITTEN SUMMARY" in regions.get("SUMMARY", ""),
      regions.get("SUMMARY", "")[:60])

# 3. Restore bookkeeping.
check("restore recorded as a new version", _version_count(app_id) == n_before + 1,
      _version_count(app_id))
with get_session() as s:
    row = s.get(Application, app_id)
    check("ATS score recomputed", (row.ats_score or 0) > 0, row.ats_score)
    check("approval reset to ready after restore", row.status == "ready", row.status)
    check("PDF re-rendered", (RESUMES_OUTPUT_DIR / f"application_{app_id}.pdf").exists())

# 4. Errors.
r = client.post(f"/application/{app_id}/resume/restore/99", follow_redirects=False)
check("restore 404s on missing version", r.status_code == 404, r.status_code)
r = other_client.post(f"/application/{app_id}/resume/restore/1", follow_redirects=False)
check("restore 403s for non-owner", r.status_code == 403, r.status_code)

print("ALL VERSION HISTORY CHECKS PASSED")
