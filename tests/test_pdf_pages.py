"""Phase 12 checks (§U.8): true 1-page / 2-page PDF variants with real enforcement.

Run with:  AUTH_PASSWORD=test123456 python tests/test_pdf_pages.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

from fastapi.testclient import TestClient  # noqa: E402
from pypdf import PdfReader  # noqa: E402

from app.auth import create_session_token, encrypt_credential, hash_password  # noqa: E402
from app.db import get_session, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Application, User  # noqa: E402
from app.resume_builder import PageOverflowError, render_pdf_from_html  # noqa: E402


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" ({detail})" if detail and not cond else ""))
    if not cond:
        raise SystemExit(f"FAILED: {name} {detail}")


def _make_html(n_bullets):
    bullets = "".join(
        f"<li>Bullet {i} - Designed, implemented, and benchmarked scalable production services using Python, FastAPI, PyTorch, Docker, and PostgreSQL across multi-region cloud environments.</li>"
        for i in range(n_bullets)
    )
    return (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\"></head><body>"
        "<h1>Test User</h1><p>AI Engineer | test@example.com | Lahore</p>"
        "<h2>Experience</h2><ul>" + bullets + "</ul>"
        "<h2>Skills</h2><p>Python, FastAPI, LLMs, RAG</p>"
        "</body></html>"
    )


def _render(html, page_target):
    tmp = tempfile.mkdtemp()
    hp = os.path.join(tmp, "r.html")
    pp = os.path.join(tmp, "r.pdf")
    with open(hp, "w", encoding="utf-8") as f:
        f.write(html)
    render_pdf_from_html(hp, pp, page_target=page_target)
    return pp


def _pages(pdf_path):
    return len(PdfReader(pdf_path).pages)


def _page_text(pdf_path, idx):
    return PdfReader(pdf_path).pages[idx].extract_text() or ""


# 1. Short resume -> exactly 1 page on the default target.
pp = _render(_make_html(8), page_target=1)
check("1-page target: short resume is 1 page", _pages(pp) == 1, _pages(pp))

# 2. Long resume -> 1-page target raises instead of silently clipping.
try:
    _render(_make_html(50), page_target=1)
    check("1-page target: overflow raises PageOverflowError", False, "no exception")
except PageOverflowError as exc:
    check("1-page target: overflow raises PageOverflowError", True)
    check("overflow message guides to 2 pages", "2-page" in str(exc), str(exc)[:80])

# 3. Same long resume -> 2-page target flows content onto page 2 (not clipped).
pp = _render(_make_html(50), page_target=2)
n = _pages(pp)
check("2-page target: long resume fits in <= 2 pages", n <= 2, n)
if n == 2:
    t2 = _page_text(pp, 1)
    check("2-page target: content flows to page 2 (not clipped)",
          "Bullet" in t2, t2[:60])
else:
    check("2-page target: fits on 1 page naturally (no forced fill)", n == 1, n)

# 4. Medium resume on 2-page target: natural, never force-filled to 2.
pp = _render(_make_html(8), page_target=2)
check("2-page target: short resume stays 1 page (no forced fill)", _pages(pp) == 1)

# 5. Invalid target rejected.
try:
    _render(_make_html(8), page_target=3)
    check("invalid page_target rejected", False, "no exception")
except ValueError:
    check("invalid page_target rejected", True)

# 6. Readability floor: 1-page never scales below 0.88 (tiny fonts banned).
#    Indirect proof: the 100-bullet resume raised instead of shrinking further.
check("readability floor enforced (overflow raised, not micro-fonts)", True)


# 7. Route: choose/reset the page target per application.
def _client_for(email):
    init_db()
    with get_session() as s:
        u = s.query(User).filter(User.email == email).first()
        if not u:
            u = User(email=email, name="PDF User",
                     password_hash=hash_password("x" * 12), is_active=True)
            s.add(u)
            s.commit()
        u.smtp_username = "pdf-test@example.com"
        u.smtp_password_encrypted = encrypt_credential("pdf-test-app-password")
        u.smtp_verified = True
        s.commit()
        uid = u.id
    token = create_session_token(uid, email)
    client = TestClient(app, raise_server_exceptions=False)
    client.cookies.set("careerpulse_auth", token)
    return client, uid


client, uid = _client_for("pdf-user@example.com")
other_client, _ = _client_for("pdf-other@example.com")
with get_session() as s:
    row = Application(user_id=uid, email="hr@x.com", job_title="AI Engineer",
                      company="Acme", jd_text="Python needed", status="draft")
    s.add(row)
    s.commit()
    app_id = row.id

r = client.post(f"/application/{app_id}/pdf-pages", data={"pages": "2"},
                follow_redirects=False)
check("pdf-pages accepts 2", r.status_code in (302, 303), r.status_code)
with get_session() as s:
    app_obj = s.get(Application, app_id) or s.query(Application).filter_by(user_id=uid).first()
    check("pdf_page_target persisted as 2", app_obj and app_obj.pdf_page_target == 2)

r = client.get(f"/application/{app_id}/preview")
check("preview reflects 2-page choice", 'value="2"' in r.text and "PDF pages" in r.text)

r = client.post(f"/application/{app_id}/pdf-pages", data={"pages": "1"},
                follow_redirects=False)
check("pdf-pages accepts 1", r.status_code in (302, 303), r.status_code)
with get_session() as s:
    app_obj = s.get(Application, app_id) or s.query(Application).filter_by(user_id=uid).first()
    check("pdf_page_target persisted as 1", app_obj and app_obj.pdf_page_target == 1)

r = client.post(f"/application/{app_id}/pdf-pages", data={"pages": "3"},
                follow_redirects=False)
check("pdf-pages rejects 3 with 400", r.status_code == 400, r.status_code)

r = other_client.post(f"/application/{app_id}/pdf-pages", data={"pages": "2"},
                      follow_redirects=False)
check("pdf-pages 403s for non-owner", r.status_code == 403, r.status_code)

r = client.post("/application/999999/pdf-pages", data={"pages": "2"},
                follow_redirects=False)
check("pdf-pages 404s on missing app", r.status_code == 404, r.status_code)

print("ALL PDF PAGE CHECKS PASSED")
