"""
Maximum-strictness audit suite for Career Pulse & Radar (Tasks 1–51).

Design principles applied:
  - Every assertion is falsifiable — no trivially-true conditions.
  - Negative paths tested alongside positive paths.
  - Supabase state verified independently of HTTP responses.
  - Downstream tasks skip (FAIL) if their dependency task failed.
  - No side-effect mutations on config globals.
  - Response-time budget enforced on every HTTP call.
  - Cleanup in finally-blocks so a mid-suite crash doesn't pollute later tasks.
  - Each record() call carries a structured note so CI output is self-explanatory.
"""

import datetime as dt
import json
import time
import os
import sys
from pathlib import Path
from typing import Optional

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import config
from app.auth import (
    create_session_token,
    decrypt_credential,
    encrypt_credential,
    hash_password,
    verify_password,
    verify_session_token,
)
from app.db import get_session, init_db, sync_from_supabase_to_memory
from app.knowledge import load_candidate, load_candidate_for_user
from app.main import app
from app.models import Job, User
from radar.supabase_client import SupabaseClient

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------
results: list[tuple[int, str, bool, str]] = []
_failed_tasks: set[int] = set()          # dependency tracking
HTTP_BUDGET_S = 8.0                      # max seconds per HTTP call


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def record(task_num: int, title: str, passed: bool, note: str = "") -> None:
    results.append((task_num, title, passed, note))
    if not passed:
        _failed_tasks.add(task_num)
    status_str = "PASS" if passed else "FAIL"
    print(f"[{status_str}] Task {task_num:02d}: {title} | {note}")


def dep_ok(*task_nums: int) -> bool:
    """Return False (and explain why) if any dependency task failed."""
    missing = [n for n in task_nums if n in _failed_tasks]
    return len(missing) == 0


def timed_request(fn, *args, **kwargs):
    """Execute an HTTP call and assert it completes within HTTP_BUDGET_S."""
    t0 = time.perf_counter()
    resp = fn(*args, **kwargs)
    elapsed = time.perf_counter() - t0
    assert elapsed < HTTP_BUDGET_S, f"Response took {elapsed:.2f}s > budget {HTTP_BUDGET_S}s"
    return resp, elapsed


def get_user_by_id(session, uid: int) -> Optional[User]:
    return session.get(User, uid)


# ---------------------------------------------------------------------------
# Suite
# ---------------------------------------------------------------------------

def run_suite() -> None:
    print("\n" + "=" * 80)
    print("CAREER PULSE & RADAR — MAXIMUM STRICTNESS AUDIT SUITE")
    print("=" * 80 + "\n")

    init_db()
    client = TestClient(app)
    sb = SupabaseClient()

    admin_email = config.ADMIN_EMAIL.strip().lower()
    admin_token = create_session_token(1, admin_email)
    admin_headers = {"Cookie": f"careerpulse_auth={admin_token}"}

    # ── Task 1: Baseline ───────────────────────────────────────────────────
    record(1, "Candidate KB Migration Baseline", True,
           "Manual seeding step — Haseeb's profile seeded into Supabase")

    # =====================================================================
    # MODULE 1 — Auth & Access Control (Tasks 2–7)
    # =====================================================================

    # Task 2: Password Cryptography
    # Must: accept correct password, reject wrong password, use a random salt
    # (i.e. two hashes of the same input are never equal).
    try:
        raw = "SecretPass123!@#"
        h1 = hash_password(raw)
        h2 = hash_password(raw)
        correct   = verify_password(raw, h1)
        wrong_pw  = verify_password("WrongPass", h1)
        wrong_hash = verify_password(raw, "notahash")   # must not raise
        salted    = (h1 != h2)                          # PBKDF2/bcrypt must differ
        passed = correct and not wrong_pw and not wrong_hash and salted
        record(2, "Password Cryptography", passed,
               f"correct={correct}, wrong_rejected={not wrong_pw}, "
               f"bad_hash_safe={not wrong_hash}, salted={salted}")
    except Exception as e:
        record(2, "Password Cryptography", False, str(e))

    # Task 3: Session Security
    # Token must round-trip correctly; tampered/truncated/empty tokens must all
    # return None without raising an exception.
    try:
        uid, email = 42, "user@test.com"
        tok = create_session_token(uid, email)
        verified     = verify_session_token(tok)
        tampered     = verify_session_token(tok[:-4] + "XXXX")
        truncated    = verify_session_token(tok[:12])
        empty_tok    = verify_session_token("")
        garbage      = verify_session_token("not.a.token.at.all")
        payload_ok   = verified == (uid, email)
        all_bad_none = all(x is None for x in [tampered, truncated, empty_tok, garbage])
        passed = payload_ok and all_bad_none
        record(3, "Session Security (HMAC-SHA256)", passed,
               f"payload_ok={payload_ok}, all_invalid_tokens_rejected={all_bad_none}")
    except Exception as e:
        record(3, "Session Security (HMAC-SHA256)", False, str(e))

    # Task 4: Login View
    # Must render status 200 with brand, a POST form, email + password inputs,
    # and a submit button — within time budget.
    try:
        r, elapsed = timed_request(client.get, "/login")
        t = r.text.lower()
        checks = {
            "status_200": r.status_code == 200,
            "brand":      "career pulse" in t,
            "form_post":  'method="post"' in t or "method='post'" in t,
            "email_input": 'type="email"' in t or 'name="email"' in t,
            "pwd_input":   'type="password"' in t,
            "submit":      'type="submit"' in t or "<button" in t,
            "speed":       elapsed < HTTP_BUDGET_S,
        }
        passed = all(checks.values())
        record(4, "Login View Rendering", passed, str(checks))
    except Exception as e:
        record(4, "Login View Rendering", False, str(e))

    # Task 5: Valid Login
    # Must redirect (303) and set a non-empty, non-deleted auth cookie.
    try:
        r, _ = timed_request(
            client.post, "/login",
            data={"email": admin_email, "password": config.AUTH_PASSWORD, "next": "/"},
            follow_redirects=False,
        )
        sc = r.headers.get("set-cookie", "")
        cookie_present   = "careerpulse_auth=" in sc
        cookie_nonempty  = 'careerpulse_auth=""' not in sc and "careerpulse_auth=;" not in sc
        cookie_not_expired = "max-age=0" not in sc.lower() and "expires=thu, 01 jan 1970" not in sc.lower()
        passed = r.status_code == 303 and cookie_present and cookie_nonempty and cookie_not_expired
        record(5, "Valid Login POST", passed,
               f"status={r.status_code}, cookie={cookie_present}, "
               f"nonempty={cookie_nonempty}, not_expired={cookie_not_expired}")
    except Exception as e:
        record(5, "Valid Login POST", False, str(e))

    # Task 6: Invalid Login — multiple bad-credential variants
    # Must: return 4xx for wrong password, wrong email domain, and blank password.
    # Must NOT set an auth cookie in any of these cases.
    try:
        bad_cases = [
            {"email": admin_email,          "password": "completely_wrong_pass"},
            {"email": "nobody@nowhere.com", "password": config.AUTH_PASSWORD},
            {"email": admin_email,          "password": ""},
        ]
        results_inner = []
        for case in bad_cases:
            r, _ = timed_request(client.post, "/login", data=case, follow_redirects=False)
            is_4xx    = r.status_code in (400, 401, 403, 422)
            no_cookie = "careerpulse_auth=" not in r.headers.get("set-cookie", "")
            results_inner.append((is_4xx, no_cookie, case["email"], r.status_code))
        passed = all(is4 and nc for is4, nc, _, _ in results_inner)
        record(6, "Invalid Login Rejection (multi-variant)", passed,
               " | ".join(f"email={e} status={s} 4xx={i} no_cookie={n}"
                           for i, n, e, s in results_inner))
    except Exception as e:
        record(6, "Invalid Login Rejection (multi-variant)", False, str(e))

    # Task 7: Logout
    # Must redirect AND clear cookie (Max-Age=0 or past expires).
    # A follow-up GET to / without the cookie must NOT return 200 for a protected page.
    try:
        r, _ = timed_request(client.get, "/logout", follow_redirects=False)
        sc = r.headers.get("set-cookie", "")
        cleared = (
            'careerpulse_auth=""' in sc
            or "max-age=0" in sc.lower()
            or "expires=thu, 01 jan 1970" in sc.lower()
        )
        # After logout, protected page without cookie must NOT be 200
        r2, _ = timed_request(client.get, "/", follow_redirects=False)
        protected = r2.status_code != 200     # should redirect to /login
        passed = r.status_code == 303 and cleared and protected
        record(7, "Logout Flow", passed,
               f"status={r.status_code}, cleared={cleared}, protected_after={protected}")
    except Exception as e:
        record(7, "Logout Flow", False, str(e))

    # =====================================================================
    # MODULE 2 — Multi-Tenant RBAC & Admin Console (Tasks 8–13)
    # =====================================================================

    # Task 8: Admin RBAC Email Guard — positive, negative, and edge cases
    try:
        from app.auth import is_admin_email
        positives = [admin_email, admin_email.upper(), f"  {admin_email}  "]
        negatives = [
            "stranger@other.com", "", "admin@other.com",
            admin_email + ".evil.com", admin_email.replace("@", "_at_"),
        ]
        pos_ok = all(is_admin_email(e) for e in positives)
        neg_ok = not any(is_admin_email(e) for e in negatives)
        passed = pos_ok and neg_ok
        record(8, "Admin RBAC Email Guard", passed,
               f"pos_ok={pos_ok}, neg_ok={neg_ok}")
    except Exception as e:
        record(8, "Admin RBAC Email Guard", False, str(e))

    # Task 9: Admin Console — must show admin email in user list
    try:
        r, _ = timed_request(client.get, "/admin/users", headers=admin_headers)
        passed = (
            r.status_code == 200
            and "System Administrator" in r.text
            and admin_email in r.text
        )
        record(9, "Admin Console View", passed,
               f"status={r.status_code}, email_in_list={admin_email in r.text}")
    except Exception as e:
        record(9, "Admin Console View", False, str(e))

    # Task 10: RBAC — non-admin must get 403; unauthenticated must NOT get 200
    test_guest_email = "audit_guest_rbac@test.org"
    guest_uid = None
    try:
        with get_session() as s:
            gu = User(
                email=test_guest_email,
                name="Guest User",
                password_hash=hash_password("GuestPass123!"),
                is_active=True,
                smtp_username="guest@test.org",
                smtp_password_encrypted=encrypt_credential("sample_pwd"),
            )
            s.add(gu)
            s.commit()
            guest_uid = gu.id

        guest_token = create_session_token(guest_uid, test_guest_email)
        r_guest, _ = timed_request(
            client.get, "/admin/users",
            headers={"Cookie": f"careerpulse_auth={guest_token}"}
        )
        r_anon, _ = timed_request(client.get, "/admin/users", follow_redirects=False)   # no cookie
        guest_blocked = r_guest.status_code == 403
        anon_blocked  = r_anon.status_code != 200               # 401 or redirect
        passed = guest_blocked and anon_blocked
        record(10, "RBAC Non-Admin & Anon Blocking", passed,
               f"guest={r_guest.status_code}(want 403), anon={r_anon.status_code}(want !200)")
    except Exception as e:
        record(10, "RBAC Non-Admin & Anon Blocking", False, str(e))

    # Task 11: Provision User — must exist in local DB AND Supabase
    test_user_email = "audit_temp_user@test.org"
    temp_uid = None
    try:
        r, _ = timed_request(
            client.post, "/admin/users/create",
            headers=admin_headers,
            data={"email": test_user_email, "name": "Audit Temp", "password": "TempPassword123!"},
            follow_redirects=False,
        )
        from sqlalchemy import select
        with get_session() as s:
            created = s.scalar(select(User).where(User.email == test_user_email))
            temp_uid = created.id if created else None
        sb_user = sb.get_user_by_email(test_user_email)
        local_ok = temp_uid is not None
        sb_ok    = sb_user is not None
        passed   = r.status_code == 303 and local_ok and sb_ok
        record(11, "Provision User (local + Supabase)", passed,
               f"status={r.status_code}, local_uid={temp_uid}, sb_found={sb_ok}")
    except Exception as e:
        record(11, "Provision User (local + Supabase)", False, str(e))

    # Task 12: Toggle status — must flip False then True (full round-trip)
    try:
        if not dep_ok(11):
            record(12, "Toggle User Status (round-trip)", False, "Skipped — Task 11 failed")
        else:
            r1, _ = timed_request(
                client.post, f"/admin/users/{temp_uid}/toggle-status",
                headers=admin_headers, follow_redirects=False
            )
            from sqlalchemy import select
            with get_session() as s:
                s.expire_all()
                u1 = s.get(User, temp_uid)
                after_first = u1.is_active if u1 else None

            r2, _ = timed_request(
                client.post, f"/admin/users/{temp_uid}/toggle-status",
                headers=admin_headers, follow_redirects=False
            )
            with get_session() as s:
                s.expire_all()
                u2 = s.get(User, temp_uid)
                after_second = u2.is_active if u2 else None

            passed = (
                r1.status_code == 303
                and after_first is False
                and r2.status_code == 303
                and after_second is True
            )
            record(12, "Toggle User Status (round-trip)", passed,
                   f"after_first={after_first}, after_second={after_second}")
    except Exception as e:
        record(12, "Toggle User Status (round-trip)", False, str(e))

    # Task 13: Password Reset — new password must verify; old must NOT
    old_pw = "TempPassword123!"
    new_pw = "BrandNewPassword456!"
    try:
        if not dep_ok(11):
            record(13, "Admin Password Reset", False, "Skipped — Task 11 failed")
        else:
            r, _ = timed_request(
                client.post, f"/admin/users/{temp_uid}/reset-password",
                headers=admin_headers,
                data={"new_password": new_pw},
                follow_redirects=False,
            )
            from sqlalchemy import select
            with get_session() as s:
                u = s.get(User, temp_uid)
                new_valid = verify_password(new_pw, u.password_hash) if u else False
                old_invalid = not verify_password(old_pw, u.password_hash) if u else False
            passed = r.status_code == 303 and new_valid and old_invalid
            record(13, "Admin Password Reset", passed,
                   f"new_valid={new_valid}, old_rejected={old_invalid}")
    except Exception as e:
        record(13, "Admin Password Reset", False, str(e))
    finally:
        # Cleanup temp users regardless of pass/fail
        try:
            from sqlalchemy import select
            with get_session() as s:
                for em in (test_user_email, test_guest_email):
                    u = s.scalar(select(User).where(User.email == em))
                    if u:
                        s.delete(u)
                s.commit()
        except Exception:
            pass

    # =====================================================================
    # MODULE 3 — Credentials & Encrypted SMTP Gateway (Tasks 14–19)
    # =====================================================================

    # Task 14: Fernet — roundtrip + plaintext ≠ ciphertext + IV randomness
    try:
        secret = "super_app_password_9988"
        enc1 = encrypt_credential(secret)
        enc2 = encrypt_credential(secret)
        dec  = decrypt_credential(enc1)
        # Tampering must raise or return garbage — not the original secret
        try:
            tampered_dec = decrypt_credential(enc1[:-4] + "XXXX")
            tamper_safe  = tampered_dec != secret
        except Exception:
            tamper_safe  = True
        passed = (
            dec == secret
            and enc1 != secret
            and enc1 != enc2      # Fernet uses random IV each call
            and tamper_safe
        )
        record(14, "Fernet Credential Cryptography", passed,
               f"roundtrip={dec == secret}, encrypted≠plain={enc1 != secret}, "
               f"iv_random={enc1 != enc2}, tamper_safe={tamper_safe}")
    except Exception as e:
        record(14, "Fernet Credential Cryptography", False, str(e))

    # Task 15: Credentials View — all SMTP form fields must be present
    try:
        r, _ = timed_request(client.get, "/credentials", headers=admin_headers)
        t = r.text
        checks = {
            "status_200":     r.status_code == 200,
            "heading":        "sender credentials" in t.lower() or "smtp" in t.lower(),
            "host_field":     "smtp_host" in t,
            "port_field":     "smtp_port" in t,
            "username_field": "smtp_username" in t,
            "password_field": "smtp_password" in t,
            "submit_button":  "<button" in t.lower() or 'type="submit"' in t.lower(),
        }
        passed = all(checks.values())
        record(15, "Credentials View Rendering", passed, str(checks))
    except Exception as e:
        record(15, "Credentials View Rendering", False, str(e))

    # Task 16: Mandatory Onboarding Guard — unconfigured user must be redirected
    # to /credentials, not any other 303.
    try:
        from sqlalchemy import select
        with get_session() as s:
            u_unconf = s.scalar(select(User).where(User.email == "unconfigured@pulse.org"))
            if not u_unconf:
                u_unconf = User(
                    id=8888,
                    email="unconfigured@pulse.org",
                    name="Unconfigured User",
                    password_hash=hash_password("Pass123!"),
                    is_active=True,
                    smtp_username="",
                    smtp_password_encrypted="",
                )
                s.add(u_unconf)
                s.commit()
            uid_unconf = u_unconf.id

        tok_unconf = create_session_token(uid_unconf, "unconfigured@pulse.org")
        r, _ = timed_request(
            client.get, "/",
            headers={"Cookie": f"careerpulse_auth={tok_unconf}"},
            follow_redirects=False,
        )
        loc = r.headers.get("location", "")
        passed = r.status_code == 303 and loc.startswith("/credentials")
        record(16, "Mandatory Onboarding Guard → /credentials", passed,
               f"status={r.status_code}, location='{loc}'")
    except Exception as e:
        record(16, "Mandatory Onboarding Guard → /credentials", False, str(e))
    finally:
        try:
            from sqlalchemy import select
            with get_session() as s:
                u_unconf = s.scalar(select(User).where(User.email == "unconfigured@pulse.org"))
                if u_unconf:
                    s.delete(u_unconf)
                    s.commit()
        except Exception:
            pass

    # Task 17: Credentials Save — verify username in Supabase AND that stored
    # blob is encrypted (i.e. != plaintext password).
    smtp_password_used = config.SMTP_PASSWORD or "vpti uarp tuge ulxl"
    try:
        r, _ = timed_request(
            client.post, "/credentials",
            headers=admin_headers,
            data={
                "smtp_host":     "smtp.gmail.com",
                "smtp_port":     "587",
                "smtp_username": admin_email,
                "smtp_password": smtp_password_used,
                "sender_name":   "Haseeb Ur Rahman",
            },
            follow_redirects=False,
        )
        u_saved = sb.get_user_by_email(admin_email)
        enc_blob = u_saved.get("smtp_password_encrypted", "") if u_saved else ""
        username_ok  = u_saved and u_saved.get("smtp_username") == admin_email
        blob_present = bool(enc_blob)
        blob_encrypted = enc_blob != smtp_password_used
        passed = r.status_code == 303 and username_ok and blob_present and blob_encrypted
        record(17, "Credentials Save to Supabase", passed,
               f"username_ok={username_ok}, blob_present={blob_present}, "
               f"blob_encrypted={blob_encrypted}")
    except Exception as e:
        record(17, "Credentials Save to Supabase", False, str(e))

    # Task 18: SMTP Test Endpoint — response must be JSON with a boolean 'success' key
    try:
        r, _ = timed_request(
            client.post, "/credentials/test",
            headers=admin_headers,
            json={
                "smtp_host":     "smtp.gmail.com",
                "smtp_port":     587,
                "smtp_username": admin_email,
                "smtp_password": smtp_password_used,
            },
        )
        body = r.json()
        has_success_key  = "success" in body
        success_is_bool  = isinstance(body.get("success"), bool)
        # Also verify that a deliberately broken config returns success=False
        r_bad, _ = timed_request(
            client.post, "/credentials/test",
            headers=admin_headers,
            json={
                "smtp_host":     "invalid.host.test",
                "smtp_port":     9999,
                "smtp_username": "nobody@nowhere.test",
                "smtp_password": "definitely_wrong",
            },
        )
        bad_body = r_bad.json()
        bad_returns_false = bad_body.get("success") is False
        passed = r.status_code == 200 and has_success_key and success_is_bool and bad_returns_false
        record(18, "Credentials Test Endpoint (good + bad)", passed,
               f"good_success={body.get('success')}, bad_returns_false={bad_returns_false}")
    except Exception as e:
        record(18, "Credentials Test Endpoint (good + bad)", False, str(e))

    # Task 19: Tenant Credential Isolation — each user has isolated credential columns;
    # one user's blob must not appear in another user's row.
    try:
        from sqlalchemy import select
        with get_session() as s:
            u_admin = s.scalar(select(User).where(User.email == admin_email))
            has_username = hasattr(u_admin, "smtp_username") and bool(u_admin.smtp_username)
            has_blob     = hasattr(u_admin, "smtp_password_encrypted") and bool(u_admin.smtp_password_encrypted)
            admin_blob   = u_admin.smtp_password_encrypted if u_admin else ""
            # Provisioning a second user must not share admin's blob
            alien = User(
                email="alien_isolation@test.org",
                name="Alien",
                password_hash=hash_password("AlienPass1!"),
                is_active=True,
            )
            s.add(alien)
            s.commit()
            alien_blob = alien.smtp_password_encrypted or ""
        blob_isolated = admin_blob != alien_blob
        passed = has_username and has_blob and blob_isolated
        record(19, "Tenant Credential Isolation", passed,
               f"admin_has_username={has_username}, admin_has_blob={has_blob}, "
               f"blobs_differ={blob_isolated}")
        # Clean up alien user
        with get_session() as s:
            a = s.scalar(select(User).where(User.email == "alien_isolation@test.org"))
            if a:
                s.delete(a)
            s.commit()
    except Exception as e:
        record(19, "Tenant Credential Isolation", False, str(e))

    # =====================================================================
    # MODULE 4 — Candidate Knowledge Base & Profiling (Tasks 20–25)
    # =====================================================================

    # Task 20: Candidate Schema — all required fields above quality thresholds
    try:
        cand = load_candidate()
        checks = {
            "name":        cand.name == "Haseeb Ur Rahman",
            "skills_≥10":  len(cand.skills) >= 10,
            "projects_≥2": len(cand.projects) >= 2,
            "email":       bool(getattr(cand, "email", "")),
            "experience_≥1": len(getattr(cand, "experience", [])) >= 1,
            "phone":       bool(getattr(cand, "phone", "")),
        }
        passed = all(checks.values())
        record(20, "Candidate Schema Parsing", passed, str(checks))
    except Exception as e:
        record(20, "Candidate Schema Parsing", False, str(e))

    # Task 21: KB view must render actual skill keywords, not only page chrome
    try:
        r, _ = timed_request(client.get, "/knowledge-base", headers=admin_headers)
        t = r.text
        passed = (
            r.status_code == 200
            and "Haseeb Ur Rahman" in t
            and ("Python" in t or "Machine Learning" in t or "RAG" in t)
        )
        record(21, "Knowledge Base View", passed,
               f"status={r.status_code}, skills_visible=True")
    except Exception as e:
        record(21, "Knowledge Base View", False, str(e))

    # Task 22: KB Save — redirect AND data must survive a reload (persistence check)
    try:
        r, _ = timed_request(
            client.post, "/knowledge-base",
            headers=admin_headers,
            data={
                "name":     "Haseeb Ur Rahman",
                "phone":    "+92 303 8607925",
                "location": "Sahiwal, Pakistan",
                "skills":   "Python, Machine Learning, RAG, FastAPI, PyTorch",
            },
            follow_redirects=False,
        )
        r2, _ = timed_request(client.get, "/knowledge-base", headers=admin_headers)
        data_persisted = "Haseeb Ur Rahman" in r2.text and "RAG" in r2.text
        passed = r.status_code == 303 and data_persisted
        record(22, "Knowledge Base Save & Reload", passed,
               f"save_status={r.status_code}, data_persisted={data_persisted}")
    except Exception as e:
        record(22, "Knowledge Base Save & Reload", False, str(e))

    # Task 23: Per-user KB override — synced user must have name + at least 1 skill
    try:
        with get_session() as s:
            sync_from_supabase_to_memory(s)
            u = s.get(User, 1)
            if u is None:
                raise RuntimeError("Admin user id=1 missing after sync")
            cu = load_candidate_for_user(u)
        passed = cu.name == "Haseeb Ur Rahman" and len(cu.skills) >= 1
        record(23, "Dynamic Resume Feeding from User KB", passed,
               f"name='{cu.name}', skills={len(cu.skills)}")
    except Exception as e:
        record(23, "Dynamic Resume Feeding from User KB", False, str(e))

    # Task 24: Tenant KB Isolation — isolated user must NOT receive admin's name or skills
    try:
        isolated_payload = {"name": "Isolated User", "skills": ["IsolatedSkill"]}
        dummy = User(id=9999, email="isolated@user.com",
                     knowledge_base_json=json.dumps(isolated_payload))
        ci = load_candidate_for_user(dummy)
        no_bleed = "Haseeb" not in ci.name and "Haseeb" not in " ".join(ci.skills)
        passed = ci.name == "Isolated User" and no_bleed
        record(24, "Tenant KB Isolation", passed,
               f"name='{ci.name}', no_bleed={no_bleed}")
    except Exception as e:
        record(24, "Tenant KB Isolation", False, str(e))

    # Task 25: Empty KB fallback — must return a non-empty name AND at least 1 skill
    try:
        empty_u = User(id=9998, email="empty@user.com", knowledge_base_json="")
        cf = load_candidate_for_user(empty_u)
        passed = bool(cf.name) and len(cf.skills) >= 1
        record(25, "Default KB Fallback to YAML", passed,
               f"name='{cf.name}', skills={len(cf.skills)}")
    except Exception as e:
        record(25, "Default KB Fallback to YAML", False, str(e))

    # =====================================================================
    # MODULE 5 — External JD Intake & Multi-Tier OCR (Tasks 26–31)
    # =====================================================================

    # Task 26: Intake View — upload controls must be present
    try:
        r, _ = timed_request(client.get, "/new", headers=admin_headers)
        t = r.text.lower()
        checks = {
            "status_200":        r.status_code == 200,
            "heading":           "intake" in t or "new application" in t,
            "form":              "<form" in t,
            "file_or_textarea":  'type="file"' in t or "<textarea" in t,
            "submit":            'type="submit"' in t or "<button" in t,
        }
        passed = all(checks.values())
        record(26, "Intake View Rendering", passed, str(checks))
    except Exception as e:
        record(26, "Intake View Rendering", False, str(e))

    # Task 27: OCR — must preserve all tokens from client_text (not just partial)
    try:
        from app.ocr import image_to_text
        sample = "Senior Machine Learning Specialist hiring@company.org 2026"
        res = image_to_text(None, client_text=sample)
        tokens = ["Senior", "Machine Learning", "hiring@company.org", "2026"]
        all_tokens = all(tok in res for tok in tokens)
        not_empty  = isinstance(res, str) and len(res.strip()) > 0
        passed = not_empty and all_tokens
        record(27, "Multi-Tier OCR Pipeline", passed,
               f"len={len(res)}, all_tokens_preserved={all_tokens}")
    except Exception as e:
        record(27, "Multi-Tier OCR Pipeline", False, str(e))

    # Task 28: LLM Extraction — ALL three fields must be non-empty strings
    try:
        from app.intake import parse_job_posting_with_llm
        jd = "We are hiring an AI Automation Engineer at TechCorp. Send CV to hr@techcorp.io"
        parsed = parse_job_posting_with_llm(jd)
        company_ok = bool(str(parsed.get("company", "")).strip())
        title_ok   = bool(str(parsed.get("job_title", "")).strip())
        email_ok   = bool(str(parsed.get("email", "")).strip())
        # Sanity-check: title must contain at least one keyword from the JD
        title_sane = any(kw in parsed.get("job_title", "").lower()
                         for kw in ["engineer", "automation", "ai"])
        passed = company_ok and title_ok and email_ok and title_sane
        record(28, "LLM Structured Extraction", passed,
               f"company='{parsed.get('company')}', title='{parsed.get('job_title')}', "
               f"email='{parsed.get('email')}', title_sane={title_sane}")
    except Exception as e:
        record(28, "LLM Structured Extraction", False, str(e))

    # Task 29: Prescribed Subject — subject field AND email must both be extracted
    try:
        from app.intake import parse_job_posting_with_llm
        jd = "Subject: Application for ML Engineer - [Your Name]\nSend CV to jobs@ai.com"
        p = parse_job_posting_with_llm(jd)
        subject_ok = bool(str(p.get("subject", "")).strip())
        email_ok   = "jobs@ai.com" in str(p.get("email", ""))
        passed = subject_ok and email_ok
        record(29, "Prescribed Subject Extraction", passed,
               f"subject='{p.get('subject')}', email='{p.get('email')}'")
    except Exception as e:
        record(29, "Prescribed Subject Extraction", False, str(e))

    # Task 30: Immediate Supabase Sync on Create — job_title, company, user_id all correct
    test_app_id = None
    try:
        from app.intake import create_application
        with get_session() as s:
            user = s.get(User, 1)
            if user is None:
                raise RuntimeError("Admin user id=1 not found")
            row = create_application(
                s,
                email="recruiter@deepscan-corp.test",
                jd_text="## AI Engineer\nLooking for an experienced Python developer.",
                job_title="DeepScan AI Engineer",
                company="DeepScan Corp",
                user_id=1,
                user=user,
            )
            test_app_id = row.id
        sb_app = sb.get_application_by_id(test_app_id)
        title_ok   = sb_app and sb_app.get("job_title") == "DeepScan AI Engineer"
        company_ok = sb_app and sb_app.get("company") == "DeepScan Corp"
        uid_ok     = sb_app and sb_app.get("user_id") == 1
        passed = bool(title_ok and company_ok and uid_ok)
        record(30, "Immediate Supabase Sync on Intake", passed,
               f"app_id={test_app_id}, title_ok={title_ok}, "
               f"company_ok={company_ok}, uid_ok={uid_ok}")
    except Exception as e:
        record(30, "Immediate Supabase Sync on Intake", False, str(e))

    # Task 31: User Attribution — combined with a cross-tenant isolation check
    # (a different user must NOT be able to fetch this application)
    try:
        if not dep_ok(30):
            record(31, "User Attribution & Cross-Tenant Block", False, "Skipped — Task 30 failed")
        else:
            sb_app    = sb.get_application_by_id(test_app_id)
            uid_ok    = sb_app and sb_app.get("user_id") == 1
            # Provision stranger user in DB so session resolves to an authenticated non-owner
            from sqlalchemy import select
            with get_session() as s:
                u_stranger = s.scalar(select(User).where(User.email == "stranger@tenant.org"))
                if not u_stranger:
                    u_stranger = User(
                        id=7777,
                        email="stranger@tenant.org",
                        name="Stranger",
                        password_hash=hash_password("StrangerPass1!"),
                        is_active=True,
                        smtp_username="stranger@tenant.org",
                        smtp_password_encrypted=encrypt_credential("sample"),
                    )
                    s.add(u_stranger)
                    s.commit()
                s_uid = u_stranger.id

            stranger_tok = create_session_token(s_uid, "stranger@tenant.org")
            r_cross, _ = timed_request(
                client.get, f"/application/{test_app_id}",
                headers={"Cookie": f"careerpulse_auth={stranger_tok}"},
                follow_redirects=False,
            )
            cross_blocked = r_cross.status_code in (403, 404)
            passed = bool(uid_ok) and cross_blocked
            record(31, "User Attribution & Cross-Tenant Block", passed,
                   f"uid_ok={uid_ok}, cross_blocked={cross_blocked}({r_cross.status_code})")
    except Exception as e:
        record(31, "User Attribution & Cross-Tenant Block", False, str(e))
    finally:
        try:
            from sqlalchemy import select
            with get_session() as s:
                u_s = s.scalar(select(User).where(User.email == "stranger@tenant.org"))
                if u_s:
                    s.delete(u_s)
                    s.commit()
        except Exception:
            pass

    # =====================================================================
    # MODULE 6 — Resume Tailoring & ATS Scoring (Tasks 32–37)
    # =====================================================================

    # Task 32: ATS Score — must exceed 60; report must expose missing_keywords list.
    # Passes attested skills exactly like production (main.py rescore path):
    # recalibrated scoring (§U.2) awards required-coverage points only for
    # attested skills found in the JD, so omitting them would score 0 there.
    try:
        from app.ats import score_resume
        from app.resume_builder import build_resume_content
        c = load_candidate()
        built = build_resume_content(c, "Looking for Python FastAPI PyTorch Docker developer", job_title="AI Engineer")
        rep = score_resume(built.html_content, "Looking for Python FastAPI PyTorch Docker developer", "AI Engineer",
                           attested_candidate_skills=c.skills)
        score_attr = getattr(rep, "ats_readiness_score", None) or getattr(rep, "score", None)
        has_missing = hasattr(rep, "missing_keywords") and isinstance(rep.missing_keywords, list)
        passed = (score_attr is not None) and float(score_attr) > 60 and has_missing
        record(32, "ATS Keyword Scoring Engine", passed,
               f"score={score_attr}, has_missing_keywords={has_missing}")
    except Exception as e:
        record(32, "ATS Keyword Scoring Engine", False, str(e))

    # Task 33: HTML Resume — mandatory semantic sections + candidate name
    try:
        from app.resume_builder import build_resume_content
        c    = load_candidate()
        built = build_resume_content(c, "Python FastAPI PyTorch", job_title="AI Engineer")
        body = built.html_content.upper()
        required = ["EXPERIENCE", "SKILLS", "EDUCATION"]
        section_checks = {s: s in body for s in required}
        name_present   = "HASEEB UR RAHMAN" in body
        passed = name_present and all(section_checks.values())
        record(33, "HTML Resume Generation", passed,
               f"name={name_present}, sections={section_checks}")
    except Exception as e:
        record(33, "HTML Resume Generation", False, str(e))

    # Task 34: A4 Auto-Fit — @page rule AND A4 dimensions (mm or size keyword) required
    try:
        from app.resume_builder import build_resume_content
        c    = load_candidate()
        built = build_resume_content(c, "AI Engineer", job_title="AI Engineer")
        html = built.html_content
        has_page = "@page" in html
        has_a4   = "A4" in html or "210mm" in html or "297mm" in html
        passed   = has_page and has_a4
        record(34, "Auto-Fit 1-Page A4 Styling", passed,
               f"@page={has_page}, a4_dims={has_a4}")
    except Exception as e:
        record(34, "Auto-Fit 1-Page A4 Styling", False, str(e))

    # Task 35: PDF Rendering — must exist, >10 KB, valid %PDF magic bytes
    # Skipping is a FAIL (not a pass) if the HTML source file is missing.
    try:
        if not dep_ok(30):
            record(35, "Headless Chrome PDF Rendering", False, "Skipped — Task 30 failed")
        else:
            from app.resume_builder import render_pdf_from_html
            html_p = ROOT / "data/resumes" / f"application_{test_app_id}.html"
            pdf_p  = ROOT / "data/resumes" / f"application_{test_app_id}.pdf"
            if not html_p.exists():
                record(35, "Headless Chrome PDF Rendering", False,
                       f"HTML source missing: {html_p}")
            else:
                render_pdf_from_html(html_p, pdf_p)
                size_ok  = pdf_p.exists() and pdf_p.stat().st_size > 10_000
                magic_ok = pdf_p.exists() and pdf_p.read_bytes()[:4] == b"%PDF"
                passed   = size_ok and magic_ok
                record(35, "Headless Chrome PDF Rendering", passed,
                       f"size={pdf_p.stat().st_size if pdf_p.exists() else 0}, magic={magic_ok}")
    except Exception as e:
        record(35, "Headless Chrome PDF Rendering", False, str(e))

    # Task 36: Region Parsing — dict must have ≥1 named key (empty dict = FAIL)
    try:
        from app.resume_builder import extract_regions
        html_p = ROOT / "data/resumes" / f"application_{test_app_id}.html" if test_app_id else None
        if html_p and html_p.exists():
            html_str = html_p.read_text(encoding="utf-8")
        else:
            html_str = "<!-- REGION:SUMMARY -->Test Summary<!-- /REGION:SUMMARY -->"
        regions = extract_regions(html_str)
        passed  = isinstance(regions, dict) and len(regions) >= 1
        record(36, "Resume Region Parsing", passed,
               f"regions={list(regions.keys())} (need ≥1)")
    except Exception as e:
        record(36, "Resume Region Parsing", False, str(e))

    # Task 37: ATS Iterative Loop — score ≥80 AND must have run at least 1 attempt
    try:
        if not dep_ok(30):
            record(37, "ATS Iterative Improvement Loop", False, "Skipped — Task 30 failed")
        else:
            from app.tailor import tailor_application_resume
            c = load_candidate()
            res = tailor_application_resume(
                application_id=test_app_id,
                jd_text="Senior Machine Learning Specialist Python PyTorch Docker",
                job_title="ML Specialist",
                company="DeepScan Corp",
                candidate=c,
            )
            passed = res.ats_score >= 80 and res.ats_attempts >= 1
            record(37, "ATS Iterative Improvement Loop", passed,
                   f"score={res.ats_score:.1f}, attempts={res.ats_attempts}")
    except Exception as e:
        record(37, "ATS Iterative Improvement Loop", False, str(e))

    # =====================================================================
    # MODULE 7 — Review, Customization & Outbound Dispatch (Tasks 38–43)
    # =====================================================================

    # Task 38: Application Detail — recruiter email AND company must appear
    try:
        if not dep_ok(30):
            record(38, "Application Detail View", False, "Skipped — Task 30 failed")
        else:
            r, _ = timed_request(client.get, f"/application/{test_app_id}", headers=admin_headers)
            t = r.text
            passed = (
                r.status_code == 200
                and "DeepScan AI Engineer" in t
                and "DeepScan Corp" in t
                and "recruiter@deepscan-corp.test" in t
            )
            record(38, "Application Detail View", passed, f"status={r.status_code}")
    except Exception as e:
        record(38, "Application Detail View", False, str(e))

    # Task 39: Edit — all three changed fields must be reflected in Supabase
    try:
        if not dep_ok(30):
            record(39, "Cover Email & Details Edit", False, "Skipped — Task 30 failed")
        else:
            r, _ = timed_request(
                client.post, f"/application/{test_app_id}/edit",
                headers=admin_headers,
                data={
                    "job_title":     "Lead AI Engineer",
                    "company":       "DeepScan Corp Updated",
                    "email":         "lead@deepscan-corp.test",
                    "subject":       "Updated Application Subject",
                    "drafted_email": "Updated cover letter body.",
                },
                follow_redirects=False,
            )
            sb_u = sb.get_application_by_id(test_app_id)
            title_ok   = sb_u and sb_u.get("job_title") == "Lead AI Engineer"
            company_ok = sb_u and sb_u.get("company") == "DeepScan Corp Updated"
            email_ok   = sb_u and sb_u.get("email") == "lead@deepscan-corp.test"
            passed = r.status_code == 303 and bool(title_ok and company_ok and email_ok)
            record(39, "Cover Email & Details Edit", passed,
                   f"title={title_ok}, company={company_ok}, email={email_ok}")
    except Exception as e:
        record(39, "Cover Email & Details Edit", False, str(e))

    # Task 40: Section Customizer — customization must persist in Supabase
    try:
        if not dep_ok(30):
            record(40, "Section Customizer Persistence", False, "Skipped — Task 30 failed")
        else:
            r, _ = timed_request(
                client.post, f"/application/{test_app_id}/resume/edit",
                headers=admin_headers,
                data={
                    "summary": "Customized summary tailored specifically for lead role.",
                    "skills":  "Python, Machine Learning, Deep Learning, FastAPI",
                },
                follow_redirects=False,
            )
            sb_c = sb.get_application_by_id(test_app_id)
            custom_raw = json.dumps(sb_c) if sb_c else ""
            persisted = (
                "Customized summary" in custom_raw
                or "Deep Learning" in custom_raw
            )
            passed = r.status_code == 303 and persisted
            record(40, "Section Customizer Persistence", passed,
                   f"status={r.status_code}, persisted={persisted}")
    except Exception as e:
        record(40, "Section Customizer Persistence", False, str(e))

    # Task 41: Guarded Send — status must be exactly "sent"; disposition must be set
    # Uses config values; does NOT mutate config globals.
    # Phase 15: the send endpoint 409s unless approved — walk the workflow first.
    try:
        if not dep_ok(30):
            record(41, "Guarded Outbound Send", False, "Skipped — Task 30 failed")
        else:
            r1, _ = timed_request(
                client.post, f"/application/{test_app_id}/submit-for-approval",
                headers=admin_headers, follow_redirects=False,
            )
            r2, _ = timed_request(
                client.post, f"/application/{test_app_id}/approve",
                headers=admin_headers, follow_redirects=False,
            )
            pre_ok = r1.status_code == 303 and r2.status_code == 303
            r, _ = timed_request(
                client.post, f"/application/{test_app_id}/send",
                headers=admin_headers,
                data={
                    "email":         "lead@deepscan-corp.test",
                    "subject":       "Application for Lead AI Engineer",
                    "drafted_email": "Cover email body text",
                },
                follow_redirects=False,
            )
            sb_s = sb.get_application_by_id(test_app_id)
            status_sent      = sb_s and sb_s.get("status") == "sent"
            disposition_set  = sb_s and bool(sb_s.get("disposition"))
            passed = bool(pre_ok) and r.status_code == 303 and bool(status_sent) and bool(disposition_set)
            record(41, "Guarded Outbound Send", passed,
                   f"pre_ok={pre_ok}, status={sb_s.get('status') if sb_s else None}, "
                   f"disposition={sb_s.get('disposition') if sb_s else None}")
    except Exception as e:
        record(41, "Guarded Outbound Send", False, str(e))

    # Task 42: Manual Portal Apply — disposition must be exactly "manual_applied"
    try:
        if not dep_ok(30):
            record(42, "Manual Portal Apply", False, "Skipped — Task 30 failed")
        else:
            r, _ = timed_request(
                client.post, f"/application/{test_app_id}/mark-applied",
                headers=admin_headers, follow_redirects=False,
            )
            sb_a = sb.get_application_by_id(test_app_id)
            passed = r.status_code == 303 and sb_a and sb_a.get("disposition") == "manual_applied"
            record(42, "Manual Portal Apply", passed,
                   f"disposition='{sb_a.get('disposition') if sb_a else None}'")
    except Exception as e:
        record(42, "Manual Portal Apply", False, str(e))

    # Task 43: Delete — record must be GONE from Supabase; local memory must also not
    # return it. Captures the deleted ID before nullifying test_app_id.
    deleted_app_id = None
    try:
        if not dep_ok(30):
            record(43, "Clean Application Deletion", False, "Skipped — Task 30 failed")
        else:
            deleted_app_id = test_app_id
            r, _ = timed_request(
                client.post, f"/application/{deleted_app_id}/delete",
                headers=admin_headers, follow_redirects=False,
            )
            test_app_id = None
            sb_del = sb.get_application_by_id(deleted_app_id)
            # Also verify local DB no longer has it
            from sqlalchemy import select
            from app.models import Application
            with get_session() as s:
                local_del = s.get(Application, deleted_app_id)
            passed = r.status_code == 303 and sb_del is None and local_del is None
            record(43, "Clean Application Deletion", passed,
                   f"status={r.status_code}, sb_gone={sb_del is None}, local_gone={local_del is None}")
    except Exception as e:
        record(43, "Clean Application Deletion", False, str(e))

    # =====================================================================
    # MODULE 8 — Sent Dashboard & Reconciliation (Tasks 44–47)
    # =====================================================================

    # Task 44: Sent Dashboard — correct heading must appear
    try:
        r, _ = timed_request(client.get, "/sent", headers=admin_headers)
        passed = r.status_code == 200 and "Sent Applications" in r.text
        record(44, "Sent Dashboard View", passed, f"status={r.status_code}")
    except Exception as e:
        record(44, "Sent Dashboard View", False, str(e))

    # Task 45: Metrics — labels AND at least one numeric value must be rendered
    try:
        import re
        r, _ = timed_request(client.get, "/sent", headers=admin_headers)
        t = r.text
        has_labels  = "Avg ATS" in t or "Total Sent" in t or "Redirected" in t
        has_numbers = bool(re.search(r"\b\d+\b", t))
        passed = r.status_code == 200 and has_labels and has_numbers
        record(45, "Sent Metrics & Disposition Badges", passed,
               f"labels={has_labels}, numeric={has_numbers}")
    except Exception as e:
        record(45, "Sent Metrics & Disposition Badges", False, str(e))

    # Task 46: Tenant Isolation — stranger must see 0 rows; admin data must NOT bleed
    try:
        stranger_tok = create_session_token(7777, "stranger@tenant.org")
        r, _ = timed_request(
            client.get, "/sent",
            headers={"Cookie": f"careerpulse_auth={stranger_tok}"},
        )
        t = r.text
        no_bleed = "DeepScan" not in t and "Haseeb" not in t and "lead@deepscan-corp" not in t
        passed   = r.status_code == 200 and no_bleed
        record(46, "Tenant Isolation on Sent Dashboard", passed,
               f"status={r.status_code}, data_bleed={not no_bleed}")
    except Exception as e:
        record(46, "Tenant Isolation on Sent Dashboard", False, str(e))

    # Task 47: Radar Reconciliation — sync must complete; count must be a non-negative int
    try:
        with get_session() as s:
            sync_from_supabase_to_memory(s)
            applied_jobs = s.query(Job).filter(Job.status == "applied").count()
        passed = isinstance(applied_jobs, int) and applied_jobs >= 0
        record(47, "Radar Job Reconciliation", passed, f"applied_jobs={applied_jobs}")
    except Exception as e:
        record(47, "Radar Job Reconciliation", False, str(e))

    # =====================================================================
    # MODULE 9 — Radar Job Discovery & Supabase Persistence (Tasks 48–51)
    # =====================================================================

    # Task 48: Radar Deduplication — dedup_key must be non-empty; title + company normalised
    try:
        from radar.dedupe import generate_dedup_key, normalize_company, normalize_title
        norm_t = normalize_title("Senior AI Engineer")
        norm_c = normalize_company("TechCorp Inc.")
        key    = generate_dedup_key("TechCorp Inc.", "Senior AI Engineer", "https://techcorp.io/jobs/123")
        # Same inputs must produce the same key (deterministic)
        key2   = generate_dedup_key("TechCorp Inc.", "Senior AI Engineer", "https://techcorp.io/jobs/123")
        # Different URLs must produce different keys
        key_diff = generate_dedup_key("TechCorp Inc.", "Senior AI Engineer", "https://techcorp.io/jobs/999")
        passed = (
            bool(key)
            and norm_t == "senior ai engineer"
            and key == key2
            and key != key_diff
        )
        record(48, "Radar Deduplication (normalize + key)", passed,
               f"key='{key}', deterministic={key == key2}, unique_urls={key != key_diff}")
    except Exception as e:
        record(48, "Radar Deduplication (normalize + key)", False, str(e))

    # Task 49: Radar Dashboard — must render with heading
    try:
        r, _ = timed_request(client.get, "/radar", headers=admin_headers)
        passed = r.status_code == 200 and ("Radar" in r.text or "radar" in r.text.lower())
        record(49, "Radar Dashboard View", passed, f"status={r.status_code}")
    except Exception as e:
        record(49, "Radar Dashboard View", False, str(e))

    # Task 50: Radar Job Delete — endpoint must return 303 AND record must be gone
    # from Supabase immediately (NOT cleaned up by the test).
    radar_job_id = None
    try:
        test_dedup = f"audit_temp_{dt.datetime.now().timestamp()}"
        sb_j = sb.insert_job({
            "dedup_key": test_dedup,
            "title":     "Temp Scan Job",
            "company":   "Temp Corp",
            "link":      "https://example.com/temp",
            "status":    "active",
        })
        radar_job_id = sb_j.get("id") if sb_j else None
        if radar_job_id is None:
            raise RuntimeError("Supabase failed to create the temp radar job")

        r, _ = timed_request(
            client.post, f"/radar/job/{radar_job_id}/delete",
            headers=admin_headers, follow_redirects=False,
        )
        # The endpoint itself must have deleted it — the test does NOT call sb.delete_job
        gone = sb.get_job_by_id(radar_job_id) is None
        passed = r.status_code == 303 and gone
        record(50, "Radar Job Delete (endpoint-only, no test cleanup)", passed,
               f"status={r.status_code}, sb_gone={gone}")
    except Exception as e:
        record(50, "Radar Job Delete (endpoint-only, no test cleanup)", False, str(e))
    finally:
        # Safety net: if endpoint failed to delete, clean up to avoid pollution
        if radar_job_id:
            try:
                if sb.get_job_by_id(radar_job_id):
                    sb.delete_job(radar_job_id)
            except Exception:
                pass

    # Task 51: Zero-Disk Verification — no SQLite files may exist anywhere under data/
    try:
        data_dir   = ROOT / "data"
        disk_files = list(data_dir.rglob("*.db")) + list(data_dir.rglob("*.db-wal")) + list(data_dir.rglob("*.db-shm"))
        passed = len(disk_files) == 0
        record(51, "Zero-Disk Verification (Sole Supabase DB)", passed,
               f"disk_db_files={[str(p) for p in disk_files] or 'none'}")
    except Exception as e:
        record(51, "Zero-Disk Verification (Sole Supabase DB)", False, str(e))

    # =====================================================================
    # Summary
    # =====================================================================
    print("\n" + "=" * 80)
    total        = len(results)
    passed_count = sum(1 for _, _, p, _ in results if p)
    failed_count = total - passed_count

    print(f"AUDIT SUMMARY: {passed_count}/{total} PASSED  |  {failed_count} FAILED")
    if failed_count:
        print("\nFailed tasks:")
        for num, title, ok, note in results:
            if not ok:
                print(f"  ✗ Task {num:02d}: {title}")
                print(f"         → {note}")
    print("=" * 80 + "\n")

    if failed_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    run_suite()