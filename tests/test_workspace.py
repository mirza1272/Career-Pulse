"""Workspace & Dashboard route checks.

Run with:  AUTH_PASSWORD=test123456 python tests/test_workspace.py
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
            u = User(email=email, name="WS User",
                     password_hash=hash_password("x" * 12), is_active=True)
            s.add(u)
            s.commit()
        u.smtp_username = "ws-test@example.com"
        u.smtp_password_encrypted = encrypt_credential("ws-test-app-password")
        u.smtp_verified = True
        s.commit()
        uid = u.id
        # Fresh fixture rows for this user.
        s.query(Application).filter(Application.user_id == uid).delete()
        s.add_all([
            Application(user_id=uid, email="a@x.com", job_title="Draft Role",
                        company="Acme", jd_text="x", status="draft"),
            Application(user_id=uid, email="b@x.com", job_title="Legacy Pending Role",
                        company="Acme", jd_text="x", status="pending"),
            Application(user_id=uid, email="c@x.com", job_title="Sent Role",
                        company="Acme", jd_text="x", status="sent"),
            Application(user_id=uid, email="d@x.com", job_title="Failed Role",
                        company="Acme", jd_text="x", status="failed"),
        ])
        s.commit()
    token = create_session_token(uid, email)
    client = TestClient(app, raise_server_exceptions=False)
    client.cookies.set("careerpulse_auth", token)
    return client


client = _client_for("ws-user@example.com")

# 1. Check Executive Dashboard on /
r_dash = client.get("/")
check("executive dashboard renders on /", r_dash.status_code == 200, r_dash.status_code)
check("heading is Executive Overview", "Executive Overview" in r_dash.text)
check("dashboard has performance cards", "Discovered Postings" in r_dash.text and "ATS Match Index" in r_dash.text)

# 2. Check Workspace Queue on /workspace
r = client.get("/workspace")
check("workspace renders on /workspace", r.status_code == 200, r.status_code)
check("heading is Workspace", "<h1>Workspace</h1>" in r.text)
check("filter cards present", "/workspace?status=draft" in r.text and "/workspace?status=sent" in r.text)
for title in ("Draft Role", "Legacy Pending Role", "Sent Role", "Failed Role"):
    check(f"all view shows '{title}'", title in r.text)
check("status badges shown", "Pending Approval" in r.text or "Draft" in r.text)

r = client.get("/workspace", params={"status": "sent"})
check("sent filter shows sent only",
      "Sent Role" in r.text and "Draft Role" not in r.text
      and "Failed Role" not in r.text)

r = client.get("/workspace", params={"status": "draft"})
check("draft filter includes legacy 'pending' rows",
      "Draft Role" in r.text and "Legacy Pending Role" in r.text
      and "Sent Role" not in r.text)

r = client.get("/workspace", params={"status": "bogus"})
check("invalid status falls back to all",
      "Draft Role" in r.text and "Sent Role" in r.text)

r = client.get("/workspace", params={"status": "approved"})
check("empty filter shows friendly empty state",
      "No approved applications" in r.text and "Show all" in r.text)

print("ALL WORKSPACE & DASHBOARD CHECKS PASSED")
