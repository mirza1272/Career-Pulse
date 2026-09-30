"""Career Pulse — FastAPI app (GUI + API).

Phase 1: skeleton + Pending Approvals.
Phase 2: JD intake (text + image OCR) + LLM application email.
Phase 3: Precision resume tailoring + ATS score loop + PDF generation.
Phase 4: Review/edit + guarded Approve & Send (SMTP) + manual jobs + Sent dashboard.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
import re
from typing import Any

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import sys
from app import config
from app.auth import (
    authenticate_user,
    change_user_password,
    create_session_token,
    decrypt_credential,
    encrypt_credential,
    get_current_user,
    hash_password,
    is_authenticated,
    verify_password,
    verify_session_token,
)
from app.db import (
    get_session,
    init_db,
    sync_from_supabase_to_memory,
    get_user_selected_template,
    set_user_selected_template,
)
from app.intake import IntakeError, create_application, resolve_intake_text
from app.knowledge import load_candidate, load_candidate_for_user
from app.llm import regenerate_application_email
from app.models import Application, Job, ResumeVersion, User
from app.outbound import OutboundEmailGuard
from app.readiness import format_blocked_message, kb_completeness
from app.matching import match_jd_to_kb
from app.roles import detect_and_select_role
from app.resume_parse import extract_pdf_text, extract_resume_text, extract_resume_text_and_links, structure_resume_text

# Radar integration (Unified Monorepo)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
try:
    from radar.pipeline import run_pipeline
    from radar.expiry import run_expiry_check
    from radar.db import get_supabase_client
    from radar.credentials import (
        get_masked_provider_credentials,
        get_user_provider_credentials,
        save_user_provider_credentials,
        check_limited_search_cooldown,
        record_limited_search,
    )
except Exception:
    run_pipeline = None
    run_expiry_check = None
    get_supabase_client = None
    get_masked_provider_credentials = None
    get_user_provider_credentials = None
    save_user_provider_credentials = None
    check_limited_search_cooldown = None
    record_limited_search = None
from app.resume_builder import (
    DEFAULT_SECTION_ORDER,
    DEFAULT_TEMPLATE_ID,
    RESUMES_OUTPUT_DIR,
    TEMPLATES_DIR,
    TEMPLATES_REGISTRY,
    PageOverflowError,
    build_resume_content,
    detect_section_order,
    extract_regions,
    get_available_templates,
    parse_resume_regions_detail,
    rebuild_resume_from_custom_edits,
    render_pdf_from_html,
    replace_regions,
    resolve_template,
    skills_html_to_csv,
    switch_resume_template,
    update_links_html,
    certifications_text_to_html,
)
from app.ats import score_resume
from app.tailor import tailor_application_resume

ROOT = Path(__file__).resolve().parents[1]
templates = Jinja2Templates(directory=str(ROOT / "templates"))
templates.env.globals["config"] = config
outbound_guard = OutboundEmailGuard()
logger = logging.getLogger("careerpulse.main")

# Upload / intake guards (audit fixes).
MAX_JD_IMAGE_BYTES = 10 * 1024 * 1024      # 10 MB screenshot cap
MAX_JD_TEXT_CHARS = 100_000                 # pasted-JD length cap
MAX_CUSTOM_PDF_BYTES = 10 * 1024 * 1024     # 10 MB custom-resume upload cap

# Statuses the radar status endpoint accepts (arbitrary strings rejected).
RADAR_STATUS_ALLOWLIST = frozenset({"active", "expired", "applied", "saved", "rejected"})


def _atomic_write_text(path: Path, content: str) -> None:
    """Write a text file atomically (temp + os.replace) so a crash or an
    overlapping request never leaves a half-written resume on disk."""
    import os
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    """Atomic variant for binary files (custom resume PDFs)."""
    import os
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# Cloud re-hydration is expensive (full PostgREST pull of all tables). Page
# views used to run it on EVERY request; now it runs at most once per minute
# per process. Local writes push to the cloud immediately, so reads-after-
# write never need a fresh pull — this only catches changes made elsewhere.
_LAST_CLOUD_HYDRATION: float = 0.0
_CLOUD_HYDRATION_TTL = 60.0


def _maybe_hydrate_from_cloud(s) -> None:
    import time

    global _LAST_CLOUD_HYDRATION
    now = time.monotonic()
    if now - _LAST_CLOUD_HYDRATION >= _CLOUD_HYDRATION_TTL:
        sync_from_supabase_to_memory(s)
        _LAST_CLOUD_HYDRATION = now


def _owns_application(user, row) -> bool:
    """Deny-by-default ownership check for application rows.

    A NULL ``user_id`` does NOT mean "ownerless and accessible" — it means
    ownership cannot be established, so access is denied. The admin (by
    ADMIN_EMAIL) may access any row.
    """
    if user is None or row is None:
        return False
    if user.email.strip().lower() == config.ADMIN_EMAIL.strip().lower():
        return True
    return bool(row.user_id) and row.user_id == user.id


def _reset_approval_to_ready(row) -> None:
    """Phase 15: any content change invalidates a prior review.

    If the application was awaiting approval or already approved, the
    edited content has not been reviewed — back to 'ready' so it must
    pass submit → approve again. Drafts and sent rows are untouched.
    """
    if (row.status or "") in ("pending_approval", "approved"):
        row.status = "ready"


def _html_to_text(html: str) -> str:
    """Extract plain text from resume HTML for deterministic ATS re-scoring.

    Phase 11: the preview recomputes the score from the live resume file
    (which includes the user's manual-edit locks), so the breakdown shown
    always matches the resume being previewed.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
    return soup.get_text(separator=" ", strip=True)


def _dedup_skills(skills: list | None) -> list:
    """Remove duplicate skill names (case-insensitive), preserving order.

    The matchers can list the same skill once per JD requirement it hits
    (e.g. 'SQL' for 'SQL', 'Database', 'basic SQL'), which confuses the UI.
    """
    seen: set[str] = set()
    out: list = []
    for s in skills or []:
        key = str(s).strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(s)
    return out


def sync_app_to_supabase(row: Application, session: Any = None) -> None:
    """Helper to push an application record into Supabase PostgREST cloud."""
    if get_supabase_client:
        try:
            sb = get_supabase_client()
            if sb.is_configured:
                cloud_res = sb.upsert_application(
                    {
                        "id": row.id,
                        "user_id": row.user_id,
                        "job_id": row.job_id,
                        "email": row.email or "portal-application@careerpulse.internal",
                        "job_title": row.job_title or "",
                        "company": row.company or "",
                        "link": row.link or "",
                        "jd_text": (row.jd_text or "")[:4000],
                        "drafted_email": (row.drafted_email or "")[:4000],
                        "subject": row.subject or "",
                        "resume_path": row.resume_path or "",
                        "ats_score": float(row.ats_score or 0.0),
                        "ats_attempts": int(row.ats_attempts or 0),
                        "ats_note": row.ats_note or "",
                        "pdf_page_target": int(row.pdf_page_target or 1),
                        "template_id": row.template_id or "apex_modern",
                        "resume_locks_json": row.resume_locks_json or "{}",
                        "status": row.status or "draft",
                        "disposition": row.disposition or "",
                        "sent_at": row.sent_at.isoformat() if row.sent_at else None,
                        "created_at": row.created_at.isoformat() if row.created_at else None,
                        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                    },
                    session=session,
                )
                if cloud_res and cloud_res.get("id") and session:
                    row.id = int(cloud_res["id"])
                    try:
                        session.commit()
                    except Exception:
                        pass
        except Exception as err:
            logger.warning(f"Supabase app sync warning: {err}")


def bg_sync_job_status(job_id: int, status: str) -> None:
    """Non-blocking background helper to sync job status changes to Supabase Cloud."""
    if get_supabase_client:
        try:
            sb = get_supabase_client()
            if sb and sb.is_configured:
                sb.update_job_status(job_id=job_id, status=status)
        except Exception as exc:
            logger.warning(f"Background Supabase job status sync warning: {exc}")


def bg_delete_job(job_id: int, dedup_key: str | None = None) -> None:
    """Non-blocking background helper to sync job deletion to Supabase Cloud."""
    if get_supabase_client:
        try:
            sb = get_supabase_client()
            if sb and sb.is_configured:
                sb.delete_job(job_id=job_id, dedup_key=dedup_key)
        except Exception as exc:
            logger.warning(f"Background Supabase job deletion sync warning: {exc}")


def bg_sync_app(app_data: dict[str, Any]) -> None:
    """Non-blocking background helper to sync application records to Supabase Cloud."""
    if get_supabase_client:
        try:
            sb = get_supabase_client()
            if sb and sb.is_configured:
                sb.upsert_application(app_data)
        except Exception as exc:
            logger.warning(f"Background Supabase app sync warning: {exc}")


def sync_user_to_supabase(user: User) -> None:
    """Helper to push a user record into Supabase PostgREST cloud."""
    import os
    if not user or not user.email:
        return
    # Skip test accounts and test-suite runs from modifying cloud Supabase
    if (
        user.email.endswith(("@test.org", "@example.test", "@example.com", "@acme.test"))
        or (config.TEST_MODE and ("pytest" in sys.modules or os.environ.get("PYTEST_CURRENT_TEST")))
    ):
        return
    if get_supabase_client:
        try:
            sb = get_supabase_client()
            if sb.is_configured:
                sb.upsert_user(
                    {
                        "id": user.id,
                        "email": user.email,
                        "password_hash": user.password_hash,
                        "name": user.name,
                        "is_active": user.is_active,
                        "smtp_host": user.smtp_host,
                        "smtp_port": user.smtp_port,
                        "smtp_username": user.smtp_username,
                        "smtp_password_encrypted": user.smtp_password_encrypted,
                        "sender_name": user.sender_name,
                        "smtp_verified": user.smtp_verified,
                        "knowledge_base_json": user.knowledge_base_json,
                        "created_at": user.created_at.isoformat() if user.created_at else None,
                        "updated_at": user.updated_at.isoformat() if user.updated_at else None,
                    }
                )
        except Exception as err:
            logger.warning(f"Supabase user sync warning: {err}")


class ApplicationIn(BaseModel):
    email: str
    jd_text: str = ""
    job_title: str = ""
    target_role: str = ""
    company: str = ""
    link: str = ""
    job_id: int | None = None


def create_app() -> FastAPI:
    app = FastAPI(title="Career Pulse")
    try:
        init_db()
    except Exception as exc:
        print(f"[DB Init Notice] {exc}")

    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception) -> HTMLResponse:
        import traceback
        tb = traceback.format_exc()
        # Traceback is logged server-side only — never rendered to the client.
        print(f"[Unhandled Exception] {tb}")
        logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
        return HTMLResponse(
            """
            <div style="font-family:system-ui,sans-serif; padding:32px; background:#0f1620; color:#e7edf3; min-height:100vh;">
                <h2 style="color:#f87171; margin-bottom:12px;">Something went wrong</h2>
                <p style="color:#94a3b8; font-size:14px; margin-bottom:18px;">An unexpected error occurred. The incident has been logged.</p>
                <div style="margin-top:20px;"><a href="/" style="color:#38bdf8; text-decoration:none; font-weight:600;">← Return to Home</a></div>
            </div>
            """,
            status_code=500,
        )

    @app.get("/health")
    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> Response:
        """Serve a clean inline SVG icon as favicon to prevent 404 logs."""
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
            '<rect width="32" height="32" rx="8" fill="#0e1015"/>'
            '<circle cx="16" cy="16" r="6" fill="#10b981"/>'
            '<circle cx="16" cy="16" r="11" fill="none" stroke="#10b981" stroke-width="2" stroke-opacity="0.5"/>'
            '</svg>'
        )
        return Response(content=svg, media_type="image/svg+xml")

    # ---- API: Radar (Project 1) pushes email-jobs here -------------------
    @app.post("/api/applications")
    def api_create(request: Request, body: ApplicationIn) -> dict[str, object]:
        user = getattr(request.state, "current_user", None)
        if user is None:
            raise HTTPException(status_code=401, detail="Unauthorized. Please log in.")
        try:
            with get_session() as s:
                row = create_application(
                    s,
                    email=body.email,
                    jd_text=body.jd_text,
                    job_title=body.job_title,
                    target_role=body.target_role,
                    company=body.company,
                    link=body.link,
                    job_id=body.job_id,
                    user_id=user.id,
                    user=user,
                )
                return {"id": row.id, "status": row.status, "ats_score": row.ats_score}
        except IntakeError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        config.ensure_fresh_config()
        path = request.url.path
        if (
            path in ("/login", "/logout", "/health", "/api/health")
            or path.startswith("/assets/")
            or path == "/favicon.ico"
        ):
            return await call_next(request)

        user = get_current_user(request)
        if not user:
            if path.startswith("/api/"):
                return JSONResponse({"error": "Unauthorized. Please log in."}, status_code=401)
            next_url = request.url.path
            if request.url.query:
                next_url += f"?{request.url.query}"
            return RedirectResponse(f"/login?next={next_url}", status_code=303)

        request.state.current_user = user

        # Mandatory Credentials Gate:
        # If user has not verified SMTP credentials, block access to all engine features
        has_credentials = bool(user.smtp_verified and user.smtp_username and user.smtp_password_encrypted)
        exempt_gate_prefixes = (
            "/credentials",
            "/knowledge-base",
            "/change-password",
            "/logout",
            "/health",
            "/api/health",
        )
        if not has_credentials and not any(path.startswith(p) for p in exempt_gate_prefixes):
            if path.startswith("/api/"):
                return JSONResponse({"error": "Please configure and verify your sender credentials first."}, status_code=403)
            return RedirectResponse("/credentials?setup_required=1", status_code=303)

        return await call_next(request)

    # ---- AUTHENTICATION --------------------------------------------------
    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, next: str = "/", error: str = "", logged_out: str = "") -> HTMLResponse:
        user = get_current_user(request)
        if user:
            # Validate the redirect target the same way login_submit does —
            # an unvalidated `next` here is an open redirect for logged-in users.
            target = (next or "/").strip()
            if not target.startswith("/") or target.startswith("//"):
                target = "/"
            return RedirectResponse(target, status_code=303)
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "title": "Career Pulse — Sign In",
                "next_url": next,
                "error": error,
                "logged_out": bool(logged_out),
            },
        )

    @app.post("/login")
    async def login_submit(
        request: Request,
        email: str = Form(""),
        password: str = Form(""),
        next: str = Form("/"),
    ) -> Response:
        user = authenticate_user(email, password)

        if user:
            if not user.is_active:
                return RedirectResponse(f"/login?error=Account is deactivated. Contact administrator.&next={next}", status_code=303)

            session_token = create_session_token(user.id, user.email)
            target_url = next.strip() or "/"
            if not target_url.startswith("/") or target_url.startswith("//"):
                target_url = "/"
            resp = RedirectResponse(target_url, status_code=303)
            resp.set_cookie(
                key="careerpulse_auth",
                value=session_token,
                max_age=7 * 86400,
                httponly=True,
                samesite="lax",
                secure=request.url.scheme == "https",
            )
            return resp

        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "prefill_email": email,
                "next_url": next,
                "error": "Invalid email or password",
                "logged_out": False,
            },
            status_code=401,
        )

    # POST-only: a state-changing action must not be triggerable by a plain
    # link/image load (logout CSRF).
    @app.post("/logout")
    def logout() -> RedirectResponse:
        resp = RedirectResponse("/login?logged_out=1", status_code=303)
        resp.delete_cookie("careerpulse_auth")
        return resp

    @app.post("/change-password")
    async def change_password_route(
        request: Request,
        current_password: str = Form(""),
        new_password: str = Form(""),
        confirm_password: str = Form(""),
    ) -> JSONResponse:
        user = getattr(request.state, "current_user", None)
        if not user:
            return JSONResponse({"success": False, "message": "Unauthorized."}, status_code=401)

        if new_password != confirm_password:
            return JSONResponse({"success": False, "message": "New passwords do not match."})

        if len(new_password) < 6:
            return JSONResponse({"success": False, "message": "New password must be at least 6 characters."})

        ok, msg = change_user_password(user.id, current_password, new_password)
        return JSONResponse({"success": ok, "message": msg})

    # ---- CREDENTIALS GATEWAY ---------------------------------------------
    @app.get("/credentials", response_class=HTMLResponse)
    def credentials_page(
        request: Request, setup_required: str = "", saved: str = "", saved_providers: str = "", error: str = ""
    ) -> HTMLResponse:
        user = getattr(request.state, "current_user", None)
        provider_creds = get_masked_provider_credentials(user) if get_masked_provider_credentials else {}
        return templates.TemplateResponse(
            request=request,
            name="credentials.html",
            context={
                "title": "Career Pulse — Sender Credentials",
                "current_user": user,
                "setup_required": bool(setup_required),
                "saved": bool(saved),
                "saved_providers": bool(saved_providers),
                "provider_creds": provider_creds,
                "error": error.strip(),
            },
        )

    @app.post("/credentials")
    async def credentials_submit(
        request: Request,
        sender_name: str = Form(""),
        smtp_username: str = Form(""),
        smtp_password: str = Form(""),
        smtp_host: str = Form("smtp.gmail.com"),
        smtp_port: int = Form(587),
        apify_api_key: str = Form(""),
        tavily_api_key: str = Form(""),
        firecrawl_api_key: str = Form(""),
        serpapi_api_key: str = Form(""),
        clear_apify: str = Form(""),
        clear_tavily: str = Form(""),
        clear_firecrawl: str = Form(""),
        clear_serpapi: str = Form(""),
    ) -> Response:
        user = getattr(request.state, "current_user", None)
        if not user:
            return RedirectResponse("/login", status_code=303)

        clean_sender = sender_name.strip() or user.name or "Career Pulse"
        clean_user = smtp_username.strip()
        clean_host = (smtp_host.strip() or "smtp.gmail.com").lower()
        try:
            clean_port = int(smtp_port) if smtp_port else 587
        except (ValueError, TypeError):
            clean_port = 587

        raw_pwd = smtp_password.strip()
        if not raw_pwd and user and user.smtp_password_encrypted:
            raw_pwd = decrypt_credential(user.smtp_password_encrypted)

        clean_pwd = raw_pwd.replace(" ", "") if "gmail" in clean_host else raw_pwd

        if not clean_user or not clean_pwd:
            err_msg = "Sender email address and 16-character App Password are required."
            accept = request.headers.get("accept", "")
            if "application/json" in accept or request.headers.get("x-requested-with") == "XMLHttpRequest":
                return JSONResponse({"success": False, "message": err_msg}, status_code=400)
            provider_creds = get_masked_provider_credentials(user) if get_masked_provider_credentials else {}
            return templates.TemplateResponse(
                request=request,
                name="credentials.html",
                context={
                    "title": "Career Pulse — Sender Credentials",
                    "current_user": user,
                    "error": err_msg,
                    "setup_required": True,
                    "provider_creds": provider_creds,
                },
                status_code=400,
            )

        # ACTIVE SMTP TLS HANDSHAKE & AUTHENTICATION TEST
        import smtplib
        handshake_ok = False
        handshake_err = ""
        try:
            with smtplib.SMTP(clean_host, clean_port, timeout=10) as server:
                server.ehlo()
                if clean_port == 587 or "gmail" in clean_host:
                    server.starttls()
                    server.ehlo()
                server.login(clean_user, clean_pwd)
                handshake_ok = True
        except smtplib.SMTPAuthenticationError:
            handshake_err = "Authentication Failed: Username or App Password rejected by mail server. Please make sure you are using a 16-character Google App Password (not your regular account password)."
        except Exception as exc:
            handshake_err = f"SMTP Connection to {clean_host}:{clean_port} failed: {exc}"

        if not handshake_ok:
            logger.warning("SMTP Handshake failed for user %s: %s", user.email, handshake_err)
            accept = request.headers.get("accept", "")
            if "application/json" in accept or request.headers.get("x-requested-with") == "XMLHttpRequest":
                return JSONResponse({"success": False, "message": handshake_err}, status_code=400)
            provider_creds = get_masked_provider_credentials(user) if get_masked_provider_credentials else {}
            return templates.TemplateResponse(
                request=request,
                name="credentials.html",
                context={
                    "title": "Career Pulse — Sender Credentials",
                    "current_user": user,
                    "error": handshake_err,
                    "setup_required": True,
                    "provider_creds": provider_creds,
                },
                status_code=400,
            )

        # Handshake succeeded: save and encrypt SMTP
        with get_session() as s:
            u = s.get(User, user.id)
            if u:
                u.sender_name = clean_sender
                u.smtp_username = clean_user
                u.smtp_host = clean_host
                u.smtp_port = clean_port
                u.smtp_password_encrypted = encrypt_credential(clean_pwd)
                u.smtp_verified = True
                u.updated_at = dt.datetime.now(dt.timezone.utc)
                s.commit()
                sync_user_to_supabase(u)

        # Also save any provider keys submitted in the same form
        if save_user_provider_credentials:
            provider_keys = {
                "apify_api_key": apify_api_key.strip(),
                "tavily_api_key": tavily_api_key.strip(),
                "firecrawl_api_key": firecrawl_api_key.strip(),
                "serpapi_api_key": serpapi_api_key.strip(),
                "clear_apify_api_key": bool(clear_apify),
                "clear_tavily_api_key": bool(clear_tavily),
                "clear_firecrawl_api_key": bool(clear_firecrawl),
                "clear_serpapi_api_key": bool(clear_serpapi),
            }
            if any(provider_keys.values()):
                save_user_provider_credentials(user.id, provider_keys)

        accept = request.headers.get("accept", "")
        if "application/json" in accept or request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JSONResponse({"success": True, "message": "All credentials verified and saved successfully!"})

        return RedirectResponse("/credentials?saved=1", status_code=303)

    @app.post("/credentials/test")
    async def credentials_test(request: Request) -> JSONResponse:
        import smtplib
        user = getattr(request.state, "current_user", None)
        try:
            body_data = await request.json()
        except Exception:
            body_data = {}
        host = str(body_data.get("smtp_host") or (user.smtp_host if user else "smtp.gmail.com")).strip()
        try:
            port = int(body_data.get("smtp_port") or (user.smtp_port if user else 587))
        except (ValueError, TypeError):
            port = 587
        username = str(body_data.get("smtp_username") or (user.smtp_username if user else "")).strip()
        raw_pwd = str(body_data.get("smtp_password") or "").strip()
        if not raw_pwd and user and user.smtp_password_encrypted:
            raw_pwd = decrypt_credential(user.smtp_password_encrypted)

        clean_pwd = raw_pwd.replace(" ", "") if "gmail" in host.lower() else raw_pwd
        if not username or not clean_pwd:
            return JSONResponse({"success": False, "message": "Username and App Password are required for testing."})

        try:
            with smtplib.SMTP(host, port, timeout=8) as server:
                server.starttls()
                server.login(username, clean_pwd)
            return JSONResponse({"success": True, "message": f"{host}:{port} as {username}"})
        except Exception as exc:
            return JSONResponse({"success": False, "message": str(exc)})

    @app.post("/credentials/send-test")
    async def credentials_send_test(request: Request) -> JSONResponse:
        """Send a live test email to verify end-to-end delivery."""
        import smtplib
        from email.message import EmailMessage
        user = getattr(request.state, "current_user", None)
        if not user:
            return JSONResponse({"success": False, "message": "Unauthorized"}, status_code=401)

        try:
            body_data = await request.json()
        except Exception:
            body_data = {}

        target_email = str(body_data.get("recipient_email") or user.smtp_username or user.email).strip()
        host = user.smtp_host or "smtp.gmail.com"
        port = user.smtp_port or 587
        username = user.smtp_username
        enc_pwd = user.smtp_password_encrypted
        raw_pwd = decrypt_credential(enc_pwd) if enc_pwd else ""
        clean_pwd = raw_pwd.replace(" ", "") if "gmail" in host.lower() else raw_pwd

        if not username or not clean_pwd:
            return JSONResponse({"success": False, "message": "SMTP credentials not configured."})

        try:
            msg = EmailMessage()
            msg["Subject"] = "✓ Career Pulse SMTP Test: Handshake Verified"
            msg["From"] = f"{user.sender_name or user.name or 'Career Pulse'} <{username}>"
            msg["To"] = target_email
            now_str = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            msg.set_content(
                f"Hello {user.sender_name or user.name or 'there'},\n\n"
                f"This is a confirmation test from Career Pulse.\n"
                f"Your SMTP dispatch server ({host}:{port}) is active and authenticated.\n\n"
                f"Timestamp: {now_str}\n\n"
                f"— Career Pulse Automated Handshake Verification"
            )
            with smtplib.SMTP(host, port, timeout=12) as server:
                server.starttls()
                server.login(username, clean_pwd)
                server.send_message(msg)
            return JSONResponse({"success": True, "message": f"Test email transmitted successfully to {target_email}!"})
        except Exception as exc:
            return JSONResponse({"success": False, "message": f"SMTP sending error: {exc}"})

    @app.post("/credentials/providers")
    async def credentials_providers_submit(
        request: Request,
        apify_api_key: str = Form(""),
        tavily_api_key: str = Form(""),
        firecrawl_api_key: str = Form(""),
        serpapi_api_key: str = Form(""),
        clear_apify: str = Form(""),
        clear_tavily: str = Form(""),
        clear_firecrawl: str = Form(""),
        clear_serpapi: str = Form(""),
    ) -> Response:
        """Save optional job-search provider API credentials securely with AES-256 encryption."""
        user = getattr(request.state, "current_user", None)
        if not user:
            return RedirectResponse("/login", status_code=303)

        if save_user_provider_credentials:
            keys = {
                "apify_api_key": apify_api_key.strip(),
                "tavily_api_key": tavily_api_key.strip(),
                "firecrawl_api_key": firecrawl_api_key.strip(),
                "serpapi_api_key": serpapi_api_key.strip(),
                "clear_apify_api_key": bool(clear_apify),
                "clear_tavily_api_key": bool(clear_tavily),
                "clear_firecrawl_api_key": bool(clear_firecrawl),
                "clear_serpapi_api_key": bool(clear_serpapi),
            }
            ok, msg = save_user_provider_credentials(user.id, keys)
        else:
            ok, msg = True, "Credentials received."

        accept = request.headers.get("accept", "")
        if "application/json" in accept or request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JSONResponse({"success": ok, "message": msg})

        return RedirectResponse("/credentials?saved_providers=1", status_code=303)

    @app.post("/credentials/test-provider")
    async def credentials_test_provider(request: Request) -> JSONResponse:
        """Test optional search provider API credentials."""
        user = getattr(request.state, "current_user", None)
        try:
            body = await request.json()
        except Exception:
            body = {}
        provider = str(body.get("provider", "")).strip().lower()
        raw_key = str(body.get("api_key", "")).strip()

        # If key is empty, check user's saved key
        if not raw_key and user and get_user_provider_credentials:
            user_creds = get_user_provider_credentials(user)
            if provider in ("apify", "apify_mcp"):
                raw_key = user_creds.get("apify_api_key", "")
            elif provider == "tavily":
                raw_key = user_creds.get("tavily_api_key", "")
            elif provider == "firecrawl":
                raw_key = user_creds.get("firecrawl_api_key", "")
            elif provider == "serpapi":
                raw_key = user_creds.get("serpapi_api_key", "")

        if not raw_key:
            return JSONResponse({"success": False, "message": f"{provider.title()} API key is empty. Enter a key or save one first."})

        if provider in ("apify", "apify_mcp"):
            from radar.apify_mcp import test_apify_connection
            ok, msg = test_apify_connection(raw_key)
            return JSONResponse({"success": ok, "message": msg})
        elif provider == "tavily":
            from radar.search import test_tavily_connection
            ok, msg = test_tavily_connection(raw_key)
            return JSONResponse({"success": ok, "message": msg})
        elif provider == "firecrawl":
            from radar.search import test_firecrawl_connection
            ok, msg = test_firecrawl_connection(raw_key)
            return JSONResponse({"success": ok, "message": msg})
        elif provider == "serpapi":
            from radar.search import test_serpapi_connection
            ok, msg = test_serpapi_connection(raw_key)
            return JSONResponse({"success": ok, "message": msg})
        else:
            return JSONResponse({"success": False, "message": f"Unsupported provider: {provider}"})

    # ---- SYSTEM ADMINISTRATOR CONSOLE (ADMIN ONLY) ------------------------
    @app.get("/admin/users", response_class=HTMLResponse)
    def admin_users_page(request: Request, created: str = "", updated: str = "", password_reset: str = "", error: str = "") -> HTMLResponse:
        from sqlalchemy import select
        user = getattr(request.state, "current_user", None)
        if not user or user.email.strip().lower() != config.ADMIN_EMAIL.strip().lower():
            raise HTTPException(status_code=403, detail="Forbidden. System administrator access required.")

        with get_session() as s:
            all_users = s.scalars(select(User).order_by(User.id.asc())).all()

        return templates.TemplateResponse(
            request=request,
            name="admin_users.html",
            context={
                "title": "Career Pulse — User Management",
                "current_user": user,
                "users": all_users,
                "created": bool(created),
                "updated": bool(updated),
                "password_reset": bool(password_reset),
                "error": error,
            },
        )

    @app.post("/admin/users/create")
    @app.post("/admin/users/add")
    def admin_create_user(
        request: Request,
        name: str = Form(""),
        email: str = Form(""),
        password: str = Form(""),
    ) -> RedirectResponse:
        from sqlalchemy import select
        user = getattr(request.state, "current_user", None)
        if not user or user.email.strip().lower() != config.ADMIN_EMAIL.strip().lower():
            raise HTTPException(status_code=403, detail="Forbidden.")

        clean_email = email.strip().lower()
        if not clean_email or "@" not in clean_email:
            return RedirectResponse("/admin/users?error=Please provide a valid email address", status_code=303)

        if len(password) < 6:
            return RedirectResponse("/admin/users?error=Password must be at least 6 characters", status_code=303)

        with get_session() as s:
            existing = s.scalar(select(User).where(User.email == clean_email))
            if existing:
                return RedirectResponse("/admin/users?error=A user with this email already exists", status_code=303)

            new_user = User(
                email=clean_email,
                name=name.strip() or clean_email.split("@")[0],
                password_hash=hash_password(password),
                is_active=True,
                smtp_host="smtp.gmail.com",
                smtp_port=587,
                smtp_username="",
                smtp_password_encrypted="",
                sender_name=name.strip() or clean_email.split("@")[0],
                knowledge_base_json=json.dumps({"name": name.strip(), "email": clean_email, "skills": [], "projects": [], "experience": []}),
            )
            s.add(new_user)
            s.commit()
            sync_user_to_supabase(new_user)

        return RedirectResponse("/admin/users?created=1", status_code=303)

    @app.post("/admin/users/{user_id}/toggle-status")
    @app.post("/admin/users/{user_id}/toggle")
    def admin_toggle_user_status(request: Request, user_id: int) -> RedirectResponse:
        user = getattr(request.state, "current_user", None)
        if not user or user.email.strip().lower() != config.ADMIN_EMAIL.strip().lower():
            raise HTTPException(status_code=403, detail="Forbidden.")

        with get_session() as s:
            target = s.get(User, user_id)
            if not target:
                raise HTTPException(status_code=404, detail="User not found")
            if target.email.strip().lower() == config.ADMIN_EMAIL.strip().lower():
                return RedirectResponse("/admin/users?error=Cannot deactivate administrator account", status_code=303)

            target.is_active = not target.is_active
            s.commit()
            sync_user_to_supabase(target)

        return RedirectResponse("/admin/users?updated=1", status_code=303)

    @app.post("/admin/users/{user_id}/reset-password")
    def admin_reset_user_password(request: Request, user_id: int, new_password: str = Form("")) -> RedirectResponse:
        user = getattr(request.state, "current_user", None)
        if not user or user.email.strip().lower() != config.ADMIN_EMAIL.strip().lower():
            raise HTTPException(status_code=403, detail="Forbidden.")

        if len(new_password) < 6:
            return RedirectResponse("/admin/users?error=New password must be at least 6 characters", status_code=303)

        with get_session() as s:
            target = s.get(User, user_id)
            if not target:
                raise HTTPException(status_code=404, detail="User not found")

            target.password_hash = hash_password(new_password)
            s.commit()
            sync_user_to_supabase(target)

        return RedirectResponse("/admin/users?password_reset=1", status_code=303)

    # ---- PER-USER KNOWLEDGE BASE PROFILE ---------------------------------
    def _readiness_ctx(user):
        """Readiness checklist context for templates (based on the saved KB)."""
        try:
            report = kb_completeness(load_candidate_for_user(user))
            return {"level": report.level, "missing": report.missing, "ready": report.ready}
        except Exception:
            return {"level": "blocked", "missing": [], "ready": False}

    @app.get("/knowledge-base", response_class=HTMLResponse)
    def knowledge_base_page(request: Request, saved: str = "") -> HTMLResponse:
        user = getattr(request.state, "current_user", None)
        kb_data = {}
        if user and user.knowledge_base_json and user.knowledge_base_json.strip() not in ("", "{}"):
            try:
                kb_data = json.loads(user.knowledge_base_json)
            except Exception:
                kb_data = {}

        # If user KB is empty in memory, check Supabase on-demand
        if not kb_data and user and get_supabase_client:
            try:
                sb = get_supabase_client()
                if sb.is_configured:
                    cloud_u = sb.get_user_by_id(user.id) or sb.get_user_by_email(user.email)
                    if cloud_u and cloud_u.get("knowledge_base_json") and cloud_u.get("knowledge_base_json").strip() not in ("", "{}"):
                        kb_data = json.loads(cloud_u.get("knowledge_base_json"))
                        with get_session() as s:
                            u_db = s.get(User, user.id)
                            if u_db:
                                u_db.knowledge_base_json = cloud_u.get("knowledge_base_json")
                                s.commit()
            except Exception:
                pass

        if not kb_data and user and user.email.strip().lower() == config.ADMIN_EMAIL.strip().lower():
            c = load_candidate()
            kb_data = {
                "name": c.name,
                "email": c.email or user.email,
                "phone": c.phone,
                "location": c.location,
                "summary": c.summary,
                "links": c.links,
                "skills": c.skills,
                "projects": c.projects,
                "experience": c.experience,
                "education": c.education,
                "certifications": c.certifications,
            }

        return templates.TemplateResponse(
            request=request,
            name="knowledge_base.html",
            context={
                "title": "Career Pulse — Knowledge Base Profile",
                "current_user": user,
                "kb_data": kb_data,
                "saved": bool(saved),
                "readiness": _readiness_ctx(user),
            },
        )

    @app.post("/knowledge-base")
    def knowledge_base_submit(
        request: Request,
        name: str = Form(""),
        email: str = Form(""),
        phone: str = Form(""),
        location: str = Form(""),
        linkedin: str = Form(""),
        github: str = Form(""),
        portfolio: str = Form(""),
        summary: str = Form(""),
        skills_csv: str = Form(""),
        skills: str = Form(""),
        experience_json: str = Form("[]"),
        projects_json: str = Form("[]"),
        education_json: str = Form("[]"),
        certifications_json: str = Form("[]"),
    ) -> RedirectResponse:
        user = getattr(request.state, "current_user", None)
        if not user:
            return RedirectResponse("/login", status_code=303)

        raw_skills_text = skills_csv or skills or ""
        submitted_skills = [s.strip() for s in raw_skills_text.split(",") if s.strip()]

        try:
            exps = json.loads(experience_json) if experience_json and experience_json.strip() != "[]" else []
        except Exception:
            exps = []

        try:
            projs = json.loads(projects_json) if projects_json and projects_json.strip() != "[]" else []
        except Exception:
            projs = []

        try:
            edu = json.loads(education_json) if education_json and education_json.strip() != "[]" else []
            edu = [e for e in edu if isinstance(e, dict)]
        except Exception:
            edu = []

        try:
            raw_certs = json.loads(certifications_json) if certifications_json and certifications_json.strip() != "[]" else []
        except Exception:
            raw_certs = []
        certs = []
        for c in raw_certs if isinstance(raw_certs, list) else []:
            if isinstance(c, dict) and str(c.get("name") or "").strip():
                certs.append({
                    "name": str(c.get("name") or "").strip(),
                    "organization": str(c.get("organization") or "").strip(),
                    "link": str(c.get("link") or "").strip(),
                })
            elif isinstance(c, str) and c.strip():
                # Legacy plain-string entry
                certs.append({"name": c.strip(), "organization": "", "link": ""})

        # Preserve existing user-specific data to prevent unintended data loss
        existing_data = {}
        if user.knowledge_base_json and user.knowledge_base_json.strip() not in ("", "{}"):
            try:
                existing_data = json.loads(user.knowledge_base_json)
            except Exception:
                existing_data = {}

        # Respect user's submitted fields without dummy YAML fallback pollution
        final_skills = submitted_skills if submitted_skills is not None else existing_data.get("skills", [])
        final_exps = exps if exps is not None else existing_data.get("experience", [])
        final_projs = projs if projs is not None else existing_data.get("projects", [])
        final_edu = edu if edu is not None else existing_data.get("education", [])
        final_certs = certs if certs is not None else existing_data.get("certifications", [])

        payload = {
            "name": name.strip() or user.name or existing_data.get("name") or (user.email.split("@")[0] if user.email else "Applicant"),
            "email": email.strip() or user.email or existing_data.get("email") or "",
            "phone": phone.strip() or existing_data.get("phone") or "",
            "location": location.strip() or existing_data.get("location") or "",
            "summary": summary.strip() or existing_data.get("summary") or "",
            "links": {
                "linkedin": linkedin.strip() or existing_data.get("links", {}).get("linkedin", ""),
                "github": github.strip() or existing_data.get("links", {}).get("github", ""),
                "portfolio": portfolio.strip() or existing_data.get("links", {}).get("portfolio", ""),
            },
            "skills": final_skills,
            "experience": final_exps,
            "projects": final_projs,
            "education": final_edu,
            "certifications": final_certs,
        }

        with get_session() as s:
            u = s.get(User, user.id)
            if u:
                u.name = payload["name"]
                u.knowledge_base_json = json.dumps(payload, ensure_ascii=False)
                s.commit()
                sync_user_to_supabase(u)

        return RedirectResponse("/knowledge-base?saved=1", status_code=303)

    @app.post("/knowledge-base/import")
    async def knowledge_base_import(
        request: Request, resume_pdf: UploadFile = File(...)
    ) -> HTMLResponse:
        """Resume PDF upload -> pypdf text -> LLM structurer -> review form.

        Never saves by itself: the parsed fields only prefill the
        knowledge-base form below, and the user reviews/corrects before
        saving through the normal /knowledge-base POST.
        """
        user = getattr(request.state, "current_user", None)
        if not user:
            return RedirectResponse("/login", status_code=303)

        base_ctx = {
            "title": "Career Pulse — Knowledge Base Profile",
            "current_user": user,
            "saved": False,
            "readiness": _readiness_ctx(user),
        }
        try:
            file_bytes = await resume_pdf.read()
            raw_text, extracted_links = extract_resume_text_and_links(file_bytes, filename=resume_pdf.filename or "")
        except RuntimeError as exc:
            return templates.TemplateResponse(
                request=request,
                name="knowledge_base.html",
                context={**base_ctx, "kb_data": {}, "import_error": str(exc)},
            )

        structured = structure_resume_text(raw_text, extracted_links)
        return templates.TemplateResponse(
            request=request,
            name="knowledge_base.html",
            context={**base_ctx, "kb_data": structured, "imported": True},
        )

    # ---- GUI -------------------------------------------------------------
    # ---- GUI -------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    @app.get("/dashboard", response_class=HTMLResponse)
    def executive_dashboard(request: Request) -> HTMLResponse:
        """Executive Overview Dashboard: aggregate metrics, pipeline funnel, recent radar postings, and activity feeds."""
        user = getattr(request.state, "current_user", None)
        if user is None:
            return RedirectResponse("/login", status_code=303)

        from sqlalchemy import func as sa_func

        with get_session() as s:
            _maybe_hydrate_from_cloud(s)

            # 1. Job Radar Metrics
            total_jobs = s.query(sa_func.count(Job.id)).scalar() or 0
            active_jobs = s.query(sa_func.count(Job.id)).filter(Job.status == "active").scalar() or 0
            saved_jobs = s.query(sa_func.count(Job.id)).filter(Job.status == "saved").scalar() or 0

            # 2. Application Queue Metrics for current user
            total_apps = s.query(sa_func.count(Application.id)).filter(Application.user_id == user.id).scalar() or 0
            sent_apps = s.query(sa_func.count(Application.id)).filter(Application.user_id == user.id, Application.status == "sent").scalar() or 0
            draft_apps = s.query(sa_func.count(Application.id)).filter(Application.user_id == user.id, Application.status.in_(["draft", "pending"])).scalar() or 0
            ready_apps = s.query(sa_func.count(Application.id)).filter(Application.user_id == user.id, Application.status == "ready").scalar() or 0
            pending_approval_apps = s.query(sa_func.count(Application.id)).filter(Application.user_id == user.id, Application.status == "pending_approval").scalar() or 0
            approved_apps = s.query(sa_func.count(Application.id)).filter(Application.user_id == user.id, Application.status == "approved").scalar() or 0
            in_review_apps = draft_apps + ready_apps + pending_approval_apps + approved_apps

            dispatch_rate = round((sent_apps / total_apps * 100), 1) if total_apps > 0 else 0.0

            # 3. ATS Match Scoring Analytics
            avg_ats_score = s.query(sa_func.avg(Application.ats_score)).filter(
                Application.user_id == user.id,
                Application.ats_score > 0,
            ).scalar() or 0.0

            high_ats_count = s.query(sa_func.count(Application.id)).filter(
                Application.user_id == user.id,
                Application.ats_score >= 80,
            ).scalar() or 0
            medium_ats_count = s.query(sa_func.count(Application.id)).filter(
                Application.user_id == user.id,
                Application.ats_score >= 60,
                Application.ats_score < 80,
            ).scalar() or 0
            low_ats_count = s.query(sa_func.count(Application.id)).filter(
                Application.user_id == user.id,
                Application.ats_score > 0,
                Application.ats_score < 60,
            ).scalar() or 0

            scored_apps_count = high_ats_count + medium_ats_count + low_ats_count
            high_ats_pct = round((high_ats_count / scored_apps_count * 100), 1) if scored_apps_count > 0 else 0.0
            medium_ats_pct = round((medium_ats_count / scored_apps_count * 100), 1) if scored_apps_count > 0 else 0.0
            low_ats_pct = round((low_ats_count / scored_apps_count * 100), 1) if scored_apps_count > 0 else 0.0

            # 4. Recent Data Streams
            recent_jobs = s.query(Job).order_by(Job.created_at.desc()).limit(6).all()
            recent_apps = s.query(Application).filter(Application.user_id == user.id).order_by(Application.created_at.desc()).limit(6).all()

            # 5. Candidate Knowledge Base Summary
            candidate = load_candidate_for_user(user) if user else load_candidate()
            skills_count = len(candidate.skills) if candidate and candidate.skills else 0
            projects_count = len(candidate.projects) if candidate and candidate.projects else 0
            experiences_count = len(candidate.experience) if candidate and candidate.experience else 0

        # System Integration Indicators
        supabase_online = False
        try:
            from radar.db import get_supabase_client
            sb = get_supabase_client()
            supabase_online = sb.is_configured
        except Exception:
            pass

        smtp_ready = bool(user.smtp_verified and user.smtp_username and user.smtp_password_encrypted)
        
        # Profile Completeness Calculation
        profile_strength = 20
        if candidate and candidate.name:
            profile_strength += 15
        if skills_count > 0:
            profile_strength += 25
        if experiences_count > 0:
            profile_strength += 20
        if projects_count > 0:
            profile_strength += 10
        if smtp_ready:
            profile_strength += 10
        profile_strength = min(100, profile_strength)

        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={
                "title": "Career Pulse — Executive Dashboard",
                "current_user": user,
                "total_jobs": total_jobs,
                "active_jobs": active_jobs,
                "saved_jobs": saved_jobs,
                "total_apps": total_apps,
                "sent_apps": sent_apps,
                "draft_apps": draft_apps,
                "ready_apps": ready_apps,
                "pending_approval_apps": pending_approval_apps,
                "approved_apps": approved_apps,
                "in_review_apps": in_review_apps,
                "dispatch_rate": dispatch_rate,
                "avg_ats_score": round(float(avg_ats_score), 1) if avg_ats_score else 0.0,
                "high_ats_count": high_ats_count,
                "medium_ats_count": medium_ats_count,
                "low_ats_count": low_ats_count,
                "high_ats_pct": high_ats_pct,
                "medium_ats_pct": medium_ats_pct,
                "low_ats_pct": low_ats_pct,
                "scored_apps_count": scored_apps_count,
                "profile_strength": profile_strength,
                "recent_jobs": recent_jobs,
                "recent_apps": recent_apps,
                "candidate": candidate,
                "skills_count": skills_count,
                "projects_count": projects_count,
                "experiences_count": experiences_count,
                "supabase_online": supabase_online,
                "smtp_ready": smtp_ready,
            },
        )

    @app.get("/workspace", response_class=HTMLResponse)
    @app.get("/approvals", response_class=HTMLResponse)
    @app.get("/queue", response_class=HTMLResponse)
    def approvals(request: Request, status: str = "all") -> HTMLResponse:
        """Phase 14: workspace — all applications with a status filter.

        Statuses: draft -> ready -> pending_approval -> approved -> sent
        (Phase 15 workflow) plus failed. Legacy 'pending' rows (pre-MIGRATION C)
        are counted/shown under 'draft'.
        """
        user = getattr(request.state, "current_user", None)
        if user is None:
            # Fail closed: the workspace must never render unscoped data.
            return RedirectResponse("/login", status_code=303)
        with get_session() as s:
            _maybe_hydrate_from_cloud(s)
            base_q = s.query(Application).filter(Application.user_id == user.id)

            def _scoped(st: str):
                # Phase 14: legacy 'pending' === 'draft' until MIGRATION C runs.
                if st == "draft":
                    return base_q.filter(Application.status.in_(["draft", "pending"]))
                return base_q.filter(Application.status == st)

            order = ("draft", "ready", "pending_approval", "approved", "sent", "failed")
            # One GROUP BY query instead of 8 separate COUNT queries.
            from sqlalchemy import func as _sa_func

            counts = {st: 0 for st in order}
            for st_val, c in (
                s.query(Application.status, _sa_func.count(Application.id))
                .filter(Application.user_id == user.id)
                .group_by(Application.status)
                .all()
            ):
                key = "draft" if st_val in ("draft", "pending") else st_val
                if key in counts:
                    counts[key] += c
            counts["all"] = sum(counts.values())

            active = status if status in counts else "all"
            q = base_q if active == "all" else _scoped(active)
            applications = q.order_by(Application.created_at.desc()).all()
        return templates.TemplateResponse(
            request=request,
            name="approvals.html",
            context={
                "applications": applications,
                "counts": counts,
                "active_status": active,
                "status_order": order,
                "current_user": user,
                "title": "Career Pulse — Workspace",
            },
        )

    @app.get("/new", response_class=HTMLResponse)
    def new_form(request: Request) -> HTMLResponse:
        user = getattr(request.state, "current_user", None)
        return templates.TemplateResponse(
            request=request, name="new.html", context={"title": "Career Pulse — New Application", "current_user": user, "readiness": _readiness_ctx(user)}
        )

    @app.post("/new")
    # NOTE: plain `def` (not async) on purpose — this route does blocking
    # OCR/file/DB work, and an async route would stall the single worker.
    def new_submit(
        request: Request,
        email: str = Form(""),
        jd_text: str = Form(""),
        job_title: str = Form(""),
        target_role: str = Form(""),
        company: str = Form(""),
        link: str = Form(""),
        client_ocr_text: str = Form(""),
        jd_image: UploadFile | None = File(None),
    ) -> HTMLResponse:
        user = getattr(request.state, "current_user", None)

        def _new_ctx(extra: dict | None = None) -> dict:
            ctx = {
                "title": "Career Pulse — New Application",
                "current_user": user,
                "readiness": _readiness_ctx(user),
                "email": email,
                "jd_text": jd_text,
                "job_title": job_title,
                "target_role": target_role,
                "company": company,
                "link": link,
            }
            if extra:
                ctx.update(extra)
            return ctx

        def _error_page(message: str, status: int) -> HTMLResponse:
            return templates.TemplateResponse(
                request=request,
                name="new.html",
                context=_new_ctx({"error": message}),
                status_code=status,
            )

        # --- Upload guard: size cap + image MIME check, before reading. ---
        image_bytes: bytes | None = None
        mime = "image/png"
        if jd_image and jd_image.filename:
            try:
                jd_image.file.seek(0, 2)
                upload_size = jd_image.file.tell()
                jd_image.file.seek(0)
            except Exception:
                upload_size = 0
            if upload_size > MAX_JD_IMAGE_BYTES:
                return _error_page(
                    "The screenshot is too large (max 10 MB). Please use a smaller image.", 413
                )
            content_type = (jd_image.content_type or "").lower()
            if not (content_type.startswith("image/") or content_type == "application/octet-stream"):
                return _error_page(
                    "Please upload an image file (PNG/JPG) for the JD screenshot.", 400
                )
            image_bytes = jd_image.file.read() or None
            mime = content_type if content_type.startswith("image/") else "image/png"

        # --- Text length guard: never let a giant paste blow up the pipeline. ---
        if len(jd_text or "") > MAX_JD_TEXT_CHARS or len(client_ocr_text or "") > MAX_JD_TEXT_CHARS:
            return _error_page(
                "The job description text is too long. Please shorten it and try again.", 413
            )

        try:
            candidate = load_candidate(user)

            # Readiness gate FIRST: don't burn OCR + LLM calls when the
            # knowledge base isn't ready to generate a resume.
            readiness = kb_completeness(candidate)
            if not readiness.ready:
                raise IntakeError(format_blocked_message(readiness))

            raw_text = resolve_intake_text(jd_text, image_bytes, mime, client_ocr_text)

            # Role detection + best-fit selection in ONE structured LLM call in
            # the background (structured input: JD + KB profile; structured
            # output: roles + best_role). A typed target_role wins as-is.
            # Never blocks; no picker UI, no review screen.
            resolution = detect_and_select_role(
                raw_text, user_pick=target_role, job_title=job_title, candidate=candidate
            )
            target_role = resolution.role or target_role

            # Gap report for the optimizer (computed fresh server-side).
            match_report = match_jd_to_kb(raw_text, role=target_role, candidate=candidate)
            with get_session() as s:
                row = create_application(
                    s,
                    email=email,
                    jd_text=raw_text,
                    job_title=job_title,
                    target_role=target_role,
                    company=company,
                    link=link,
                    user_id=user.id if user else None,
                    user=user,
                    pre_resolved_text=raw_text,
                    match_report=match_report,
                )
                new_id = row.id
            return RedirectResponse(url=f"/application/{new_id}", status_code=303)
        except IntakeError as e:
            return templates.TemplateResponse(
                request=request,
                name="new.html",
                context=_new_ctx({"error": str(e)}),
                status_code=400,
            )

    @app.get("/application/{app_id}", response_class=HTMLResponse)
    def application_detail(request: Request, app_id: int) -> HTMLResponse:
        user = getattr(request.state, "current_user", None)
        with get_session() as s:
            row = s.get(Application, app_id)
            if row is None:
                raise HTTPException(status_code=404, detail="Application not found")
            if not _owns_application(user, row):
                raise HTTPException(status_code=403, detail="Forbidden. You do not own this application.")

            app_user = s.get(User, row.user_id) if row.user_id else user
            candidate = load_candidate_for_user(app_user) if app_user else load_candidate(user)
            default_subject = row.subject or (f"Application for {row.job_title} — {row.company}" if row.company and row.job_title else f"Application for {row.job_title or 'Open Role'}")

            # Extract current resume regions; generate on-the-fly if not on disk
            html_file = RESUMES_OUTPUT_DIR / f"application_{app_id}.html"
            if not html_file.exists():
                tmpl_id = row.template_id or getattr(app_user, "selected_template_id", None) or "apex_modern"
                built = build_resume_content(candidate, row.jd_text or "", job_title=row.job_title or "", variant=tmpl_id)
                try:
                    _atomic_write_text(html_file, built.html_content)
                except OSError:
                    pass
            resume_html = html_file.read_text(encoding="utf-8") if html_file.exists() else ""
            extracted = extract_regions(resume_html) if resume_html else {}
            resume_regions = {
                "summary": extracted.get("SUMMARY", ""),
                "skills_csv": skills_html_to_csv(extracted.get("SKILLS", "")),
                "skills_html": extracted.get("SKILLS", ""),
                "experience": extracted.get("EXPERIENCE", ""),
                "projects": extracted.get("PROJECTS", ""),
                "education": extracted.get("EDUCATION", ""),
                "certifications": extracted.get("CERTIFICATIONS", ""),
                "links": extracted.get("LINKS", ""),
            }
            resume_detail = parse_resume_regions_detail(resume_html, candidate, jd_text=row.jd_text or "", job_title=row.job_title or "")
            detected_order = detect_section_order(resume_html) if resume_html else DEFAULT_SECTION_ORDER

            # Phase 16: version history (FR-R-03) for the detail page.
            versions = (
                s.query(ResumeVersion)
                .filter_by(application_id=app_id)
                .order_by(ResumeVersion.version_no.desc())
                .all()
            )
            if not versions:
                # Dynamically hydrate versions from disk for this application if memory table is fresh
                try:
                    for vfile in RESUMES_OUTPUT_DIR.glob(f"application_{app_id}_v*.html"):
                        m = re.match(rf"^application_{app_id}_v(\d+)\.html$", vfile.name)
                        if m:
                            vno = int(m.group(1))
                            s.add(ResumeVersion(
                                application_id=app_id,
                                version_no=vno,
                                role=row.job_title or "",
                                jd_text=row.jd_text or "",
                                kb_snapshot_hash="",
                                resume_path=str(vfile),
                                pdf_path=str(RESUMES_OUTPUT_DIR / f"application_{app_id}.pdf"),
                                ats_score=row.ats_score or 0.0,
                                iterations=row.ats_attempts or 1,
                                score_history_json="[]",
                                template_id=row.template_id or "apex_modern",
                                created_at=row.created_at or dt.datetime.now(dt.timezone.utc),
                                updated_at=row.updated_at or dt.datetime.now(dt.timezone.utc),
                            ))
                    if html_file.exists():
                        v1_file = RESUMES_OUTPUT_DIR / f"application_{app_id}_v1.html"
                        if not v1_file.exists():
                            try:
                                import shutil
                                shutil.copy2(html_file, v1_file)
                            except Exception:
                                v1_file = html_file
                        existing_v1 = s.query(ResumeVersion).filter_by(application_id=app_id, version_no=1).first()
                        if not existing_v1:
                            s.add(ResumeVersion(
                                application_id=app_id,
                                version_no=1,
                                role=row.job_title or "",
                                jd_text=row.jd_text or "",
                                kb_snapshot_hash="",
                                resume_path=str(v1_file),
                                pdf_path=str(RESUMES_OUTPUT_DIR / f"application_{app_id}.pdf"),
                                ats_score=row.ats_score or 0.0,
                                iterations=row.ats_attempts or 1,
                                score_history_json="[]",
                                template_id=row.template_id or "apex_modern",
                                created_at=row.created_at or dt.datetime.now(dt.timezone.utc),
                                updated_at=row.updated_at or dt.datetime.now(dt.timezone.utc),
                            ))
                    s.commit()
                    versions = (
                        s.query(ResumeVersion)
                        .filter_by(application_id=app_id)
                        .order_by(ResumeVersion.version_no.desc())
                        .all()
                    )
                except Exception as hydr_exc:
                    logger.debug(f"Detail page version hydration notice: {hydr_exc}")

            # ATS breakdown + gap report (merged from preview page).
            ats_breakdown = None
            if resume_html:
                ats_breakdown = score_resume(
                    _html_to_text(resume_html),
                    row.jd_text or "",
                    job_title=row.job_title or "",
                    attested_candidate_skills=candidate.skills if candidate else None,
                    candidate=candidate,
                )
            gap_report = None
            if row.jd_text:
                gap_report = match_jd_to_kb(row.jd_text, role=row.job_title or "", candidate=candidate)

            # Deduplicate skill lists for a clean UI (matchers repeat a skill
            # once per JD requirement it hits).
            if ats_breakdown is not None:
                ats_breakdown.matched_skills = _dedup_skills(ats_breakdown.matched_skills)
                ats_breakdown.missing_attested_skills = _dedup_skills(ats_breakdown.missing_attested_skills)
            gap_dict = gap_report.to_dict() if gap_report else None
            if gap_dict:
                gap_dict["matched_skills"] = _dedup_skills(gap_dict.get("matched_skills"))
                gap_dict["missing_skills"] = _dedup_skills(gap_dict.get("missing_skills"))

            return templates.TemplateResponse(
                request=request,
                name="application.html",
                context={
                    "a": row,
                    "current_user": user,
                    "default_subject": default_subject,
                    "versions": versions,
                    "ats": ats_breakdown,
                    "gap": gap_dict,
                    "resume_regions": resume_regions,
                    "resume_detail": resume_detail,
                    "current_section_order": detected_order,
                    "current_section_order_str": ",".join(detected_order),
                    "available_templates": get_available_templates(),
                    "active_template_id": row.template_id or "apex_modern",
                    "test_mode": config.is_test_mode(),
                    "test_recipient": config.get_test_recipient(),
                    "allow_real_email": config.is_allow_real_email(),
                    "title": f"Career Pulse — {row.job_title or 'Application'}",
                },
            )

    @app.get("/application/{app_id}/preview", response_class=HTMLResponse)
    def application_preview(request: Request, app_id: int, submitted: str = "", pages: str = "", error: str = "") -> HTMLResponse:
        """Phase 11: read-only preview step between editing and approval/send.

        Shows the resume exactly as it will be sent (the PDF renderer's own
        output, embedded), the ATS score with its traceable breakdown
        (recomputed deterministically from the live resume file), and the
        JD↔KB gap report summary. No edits happen here — editing stays in
        the Phase-10 editor on the detail page.
        """
        user = getattr(request.state, "current_user", None)
        with get_session() as s:
            row = s.get(Application, app_id)
            if row is None:
                raise HTTPException(status_code=404, detail="Application not found")
            if not _owns_application(user, row):
                raise HTTPException(status_code=403, detail="Forbidden. You do not own this application.")

            app_user = s.get(User, row.user_id) if row.user_id else user
            candidate = load_candidate_for_user(app_user) if app_user else load_candidate(user)

            # Live resume HTML — the exact file the PDF renderer consumes.
            html_path = RESUMES_OUTPUT_DIR / f"application_{app_id}.html"
            if not html_path.exists():
                tmpl_id = row.template_id or getattr(app_user, "selected_template_id", None) or "apex_modern"
                built = build_resume_content(candidate, row.jd_text or "", job_title=row.job_title or "", variant=tmpl_id)
                try:
                    _atomic_write_text(html_path, built.html_content)
                except OSError:
                    pass
            resume_html = html_path.read_text(encoding="utf-8") if html_path.exists() else ""

            ats_breakdown = None
            if resume_html:
                ats_breakdown = score_resume(
                    _html_to_text(resume_html),
                    row.jd_text or "",
                    job_title=row.job_title or "",
                    attested_candidate_skills=candidate.skills if candidate else None,
                    candidate=candidate,
                )

            gap_report = None
            if row.jd_text:
                gap_report = match_jd_to_kb(row.jd_text, role=row.job_title or "", candidate=candidate)

            # Deduplicate skill lists for a clean UI (matchers repeat a skill
            # once per JD requirement it hits).
            if ats_breakdown is not None:
                ats_breakdown.matched_skills = _dedup_skills(ats_breakdown.matched_skills)
                ats_breakdown.missing_attested_skills = _dedup_skills(ats_breakdown.missing_attested_skills)
            gap_dict = gap_report.to_dict() if gap_report else None
            if gap_dict:
                gap_dict["matched_skills"] = _dedup_skills(gap_dict.get("matched_skills"))
                gap_dict["missing_skills"] = _dedup_skills(gap_dict.get("missing_skills"))

            return templates.TemplateResponse(
                request=request,
                name="preview.html",
                context={
                    "a": row,
                    "current_user": user,
                    "ats": ats_breakdown,
                    "gap": gap_dict,
                    "submitted": submitted,
                    "pages": pages,
                    "error": error,
                    "title": f"Career Pulse — Preview: {row.job_title or 'Application'}",
                },
            )

    @app.post("/application/{app_id}/submit-for-approval")
    def application_submit_for_approval(request: Request, app_id: int) -> RedirectResponse:
        """Phase 15: ready → pending_approval. The content is finalized and
        tailored; the user now asks for an explicit approval decision.

        Legacy 'pending' rows are ready-equivalent (tailored under the old
        pipeline) and accepted during the MIGRATION C transition window.
        """
        user = getattr(request.state, "current_user", None)
        with get_session() as s:
            row = s.get(Application, app_id)
            if row is None:
                raise HTTPException(status_code=404, detail="Application not found")
            if not _owns_application(user, row):
                raise HTTPException(status_code=403, detail="Forbidden.")
            if (row.status or "draft") not in ("ready", "pending", "pending_approval", "approved", "draft"):
                raise HTTPException(
                    status_code=409,
                    detail=f"Cannot submit for approval from status '{row.status}'.",
                )
            row.status = "pending_approval"
            row.updated_at = dt.datetime.now(dt.timezone.utc)
            s.commit()
            sync_app_to_supabase(row, s)
        return RedirectResponse(url=f"/application/{app_id}?submitted=1", status_code=303)

    @app.post("/application/{app_id}/approve")
    def application_approve(request: Request, app_id: int) -> RedirectResponse:
        """Explicit human approval."""
        user = getattr(request.state, "current_user", None)
        with get_session() as s:
            row = s.get(Application, app_id)
            if row is None:
                raise HTTPException(status_code=404, detail="Application not found")
            if not _owns_application(user, row):
                raise HTTPException(status_code=403, detail="Forbidden.")
            row.status = "approved"
            row.updated_at = dt.datetime.now(dt.timezone.utc)
            s.commit()
            sync_app_to_supabase(row, s)
        return RedirectResponse(url=f"/application/{app_id}?approved=1", status_code=303)

    @app.post("/application/{app_id}/edit")
    async def application_edit(
        request: Request,
        app_id: int,
        background_tasks: BackgroundTasks,
        email: str = Form(""),
        job_title: str = Form(""),
        company: str = Form(""),
        subject: str = Form(""),
        drafted_email: str = Form(""),
    ) -> Response:
        if not (job_title or subject or drafted_email):
            try:
                b_json = await request.json()
                email = str(b_json.get("email", email)).strip()
                job_title = str(b_json.get("job_title", job_title)).strip()
                company = str(b_json.get("company", company)).strip()
                subject = str(b_json.get("subject", subject)).strip()
                drafted_email = str(b_json.get("drafted_email", drafted_email)).strip()
            except Exception:
                pass

        user = getattr(request.state, "current_user", None)
        with get_session() as s:
            row = s.get(Application, app_id)
            if row is None:
                raise HTTPException(status_code=404, detail="Application not found")
            if not _owns_application(user, row):
                raise HTTPException(status_code=403, detail="Forbidden.")
            row.email = email.strip()
            row.job_title = job_title.strip()
            row.company = company.strip()
            row.subject = subject.strip()
            row.drafted_email = drafted_email.strip()
            # Phase 15: the email body changed — prior approval no longer valid.
            _reset_approval_to_ready(row)
            row.updated_at = dt.datetime.now(dt.timezone.utc)
            s.commit()
            
            app_dict = {
                "id": row.id,
                "user_id": row.user_id,
                "job_id": row.job_id,
                "email": row.email,
                "job_title": row.job_title,
                "company": row.company,
                "link": row.link,
                "jd_text": (row.jd_text or "")[:4000],
                "drafted_email": (row.drafted_email or "")[:4000],
                "subject": row.subject,
                "status": row.status,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }
            background_tasks.add_task(bg_sync_app, app_dict)

        if (
            request.headers.get("x-requested-with") == "XMLHttpRequest"
            or "application/json" in request.headers.get("accept", "")
            or "application/json" in request.headers.get("content-type", "")
        ):
            return JSONResponse({
                "success": True,
                "app_id": app_id,
                "message": "Application cover email saved successfully.",
            })

        return RedirectResponse(url=f"/application/{app_id}?saved=1", status_code=303)

    @app.post("/application/{app_id}/email/regenerate")
    async def application_email_regenerate(
        request: Request,
        app_id: int,
        background_tasks: BackgroundTasks,
        instruction: str = Form(""),
    ) -> Response:
        """Regenerate the application email from a plain-language instruction.

        The instruction is sent to the LLM along with the
        current email draft; the regenerated email replaces drafted_email.
        Never raises — on LLM failure the draft is left unchanged.
        """
        if not instruction:
            try:
                b_json = await request.json()
                instruction = str(b_json.get("instruction", "")).strip()
            except Exception:
                pass

        user = getattr(request.state, "current_user", None)
        regenerated_text = ""
        with get_session() as s:
            row = s.get(Application, app_id)
            if row is None:
                raise HTTPException(status_code=404, detail="Application not found")
            if not _owns_application(user, row):
                raise HTTPException(status_code=403, detail="Forbidden.")
            instruction = (instruction or "").strip()
            if instruction:
                app_user = s.get(User, row.user_id) if row.user_id else user
                candidate = load_candidate_for_user(app_user) if app_user else load_candidate(user)
                regenerated = regenerate_application_email(
                    candidate,
                    row.drafted_email or "",
                    instruction,
                    jd_text=row.jd_text or "",
                    company=row.company or "",
                    role=row.job_title or "",
                )
                if regenerated and regenerated.strip():
                    regenerated_text = regenerated.strip()
                    row.drafted_email = regenerated_text
                    # Phase 15: the email body changed — prior approval no longer valid.
                    _reset_approval_to_ready(row)
                    row.updated_at = dt.datetime.now(dt.timezone.utc)
                    s.commit()
                    app_dict = {
                        "id": row.id,
                        "user_id": row.user_id,
                        "job_id": row.job_id,
                        "email": row.email,
                        "job_title": row.job_title,
                        "company": row.company,
                        "link": row.link,
                        "jd_text": (row.jd_text or "")[:4000],
                        "drafted_email": (row.drafted_email or "")[:4000],
                        "subject": row.subject,
                        "status": row.status,
                        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                    }
                    background_tasks.add_task(bg_sync_app, app_dict)
                else:
                    regenerated_text = row.drafted_email or ""

        if (
            request.headers.get("x-requested-with") == "XMLHttpRequest"
            or "application/json" in request.headers.get("accept", "")
            or "application/json" in request.headers.get("content-type", "")
        ):
            return JSONResponse({
                "success": True,
                "app_id": app_id,
                "drafted_email": regenerated_text,
                "message": "Cover email regenerated successfully.",
            })

        return RedirectResponse(url=f"/application/{app_id}?email_regenerated=1", status_code=303)

    @app.post("/application/{app_id}/send")
    def application_send(
        request: Request,
        app_id: int,
        email: str = Form(""),
        subject: str = Form(""),
        drafted_email: str = Form(""),
    ) -> RedirectResponse:
        user = getattr(request.state, "current_user", None)
        with get_session() as s:
            row = s.get(Application, app_id)
            if row is None:
                raise HTTPException(status_code=404, detail="Application not found")
            if not _owns_application(user, row):
                raise HTTPException(status_code=403, detail="Forbidden.")

            # User approved & dispatched via modal action
            if (row.status or "") != "sent":
                row.status = "approved"

            app_user = s.get(User, row.user_id) if row.user_id else user
            effective_user = app_user or user

            # If user provided email/subject/drafted_email in the submission, update them
            if email and email.strip():
                row.email = email.strip()
            if subject and subject.strip():
                row.subject = subject.strip()
            if drafted_email and drafted_email.strip():
                row.drafted_email = drafted_email.strip()

            target_email = (row.email or "").strip()
            if not target_email or "internal" in target_email or "@" not in target_email or "." not in target_email:
                return RedirectResponse(
                    url=f"/application/{app_id}?error=Please provide a valid recipient email address before dispatching.",
                    status_code=303,
                )
            # Always ensure fresh, complete resume HTML & PDF are rendered before sending
            html_path = RESUMES_OUTPUT_DIR / f"application_{app_id}.html"
            if not html_path.exists():
                candidate = load_candidate_for_user(effective_user) if effective_user else load_candidate()
                tmpl_id = row.template_id or getattr(effective_user, "selected_template_id", None) or "apex_modern"
                built = build_resume_content(candidate, row.jd_text or "", job_title=row.job_title or "", variant=tmpl_id)
                try:
                    _atomic_write_text(html_path, built.html_content)
                except OSError:
                    pass
            pdf_target = RESUMES_OUTPUT_DIR / f"application_{app_id}.pdf"
            try:
                if not pdf_target.exists() or (html_path.exists() and pdf_target.stat().st_mtime < html_path.stat().st_mtime):
                    render_pdf_from_html(html_path, pdf_target)
                row.resume_path = str(pdf_target)
                s.commit()
            except PageOverflowError as exc:
                # Real page-count enforcement — never attach a clipped PDF.
                # Page handling is automatic; the user must trim content.
                return RedirectResponse(
                    url=f"/application/{app_id}?error={exc}",
                    status_code=303,
                )
            except Exception:
                pass

            subject = row.subject or (f"Application for {row.job_title} — {row.company}" if row.company and row.job_title else f"Application for {row.job_title or 'Open Role'}")
            result = outbound_guard.send(
                intended_recipient=row.email,
                subject=subject,
                body=row.drafted_email,
                resume_pdf_path=str(pdf_target) if pdf_target.exists() else None,
                user=effective_user,
            )
            if result.success:
                row.status = "sent"
                row.disposition = result.disposition
                row.sent_at = dt.datetime.now(dt.timezone.utc)
                row.updated_at = dt.datetime.now(dt.timezone.utc)
                # If linked to a radar job, mark job as applied in SQLite & Supabase
                if row.job_id:
                    job = s.get(Job, row.job_id)
                    if job:
                        job.status = "applied"
                        if get_supabase_client:
                            sb = get_supabase_client()
                            if sb.is_configured:
                                sb.update_job_status(job_id=job.id, status="applied", session=s)
                s.commit()
                sync_app_to_supabase(row, s)
                return RedirectResponse(
                    url=f"/application/{app_id}?sent=1&disp={result.disposition}&target={result.recipient}",
                    status_code=303,
                )
            return RedirectResponse(
                url=f"/application/{app_id}?error={result.reason}",
                status_code=303,
            )

    @app.post("/application/{app_id}/mark-applied")
    def application_mark_applied(request: Request, app_id: int) -> RedirectResponse:
        """User applied manually through an external portal (e.g. LinkedIn, company website)."""
        user = getattr(request.state, "current_user", None)
        with get_session() as s:
            row = s.get(Application, app_id)
            if row is None:
                raise HTTPException(status_code=404, detail="Application not found")
            if not _owns_application(user, row):
                raise HTTPException(status_code=403, detail="Forbidden.")
            row.status = "sent"
            row.disposition = "manual_applied"
            row.sent_at = dt.datetime.now(dt.timezone.utc)
            row.updated_at = dt.datetime.now(dt.timezone.utc)
            # If linked to a radar job, mark job as applied in SQLite & Supabase
            if row.job_id:
                job = s.get(Job, row.job_id)
                if job:
                    job.status = "applied"
                    if get_supabase_client:
                        sb = get_supabase_client()
                        if sb.is_configured:
                            sb.update_job_status(job_id=job.id, status="applied", session=s)
            s.commit()
            sync_app_to_supabase(row, s)
        return RedirectResponse(url=f"/application/{app_id}?applied=1", status_code=303)

    @app.get("/sent", response_class=HTMLResponse)
    def sent_dashboard(request: Request) -> HTMLResponse:
        user = getattr(request.state, "current_user", None)
        if user is None:
            return RedirectResponse("/login", status_code=303)
        with get_session() as s:
            _maybe_hydrate_from_cloud(s)
            # Auto-reconciliation: ensure any job marked as 'applied' has a corresponding Application row.
            # Audit fix: a job that already has an application from ANY user is
            # skipped — creating a second row would be a phantom "sent" entry
            # under the wrong account (Job rows are shared/global).
            applied_jobs = s.query(Job).filter(Job.status == "applied").all()
            applied_ids = [j.id for j in applied_jobs]
            covered_ids = (
                {r[0] for r in s.query(Application.job_id).filter(Application.job_id.in_(applied_ids)).all()}
                if applied_ids
                else set()
            )
            reconciled_any = False
            for j in applied_jobs:
                if j.id in covered_ids:
                    continue
                new_app = Application(
                    user_id=user.id,
                    job_id=j.id,
                    email=j.email or "portal-application@careerpulse.internal",
                    job_title=j.title or "",
                    company=j.company or "",
                    link=j.link or "",
                    jd_text=j.jd_text or "",
                    drafted_email=f"Applied via external career portal for {j.title} at {j.company}.",
                    subject=f"Application for {j.title} — {j.company}",
                    status="sent",
                    disposition="manual_applied",
                    # Phase 8 (§U.2): no resume was generated for a manual portal
                    # apply, so there is no evidence for a score; 0.0 keeps it
                    # out of the sent-metrics average instead of faking 85.
                    ats_score=0.0,
                    ats_attempts=0,
                    sent_at=dt.datetime.now(dt.timezone.utc),
                    created_at=j.created_at or dt.datetime.now(dt.timezone.utc),
                    updated_at=dt.datetime.now(dt.timezone.utc),
                )
                s.add(new_app)
                s.flush()
                sync_app_to_supabase(new_app, s)
                reconciled_any = True
            if reconciled_any:
                s.commit()

            q = (
                s.query(Application)
                .filter(Application.status.in_(["sent", "approved"]))
                .filter(Application.user_id == user.id)
            )
            sent_items = q.order_by(Application.updated_at.desc()).all()
            counts = {
                "redirected": sum(1 for a in sent_items if a.disposition == "redirected"),
                "manual": sum(1 for a in sent_items if a.disposition == "manual_applied"),
                "live": sum(1 for a in sent_items if a.disposition == "sent"),
            }
            ats_vals = [a.ats_score for a in sent_items if a.ats_score and a.ats_score > 0]
            avg_ats = f"{(sum(ats_vals)/len(ats_vals)):.0f}" if ats_vals else "—"

        synced = request.query_params.get("synced")
        deleted = request.query_params.get("deleted")

        return templates.TemplateResponse(
            request=request,
            name="sent.html",
            context={
                "sent_items": sent_items,
                "counts": counts,
                "avg_ats": avg_ats,
                "current_user": user,
                "title": "Career Pulse — Sent Applications",
                "synced": synced,
                "deleted": deleted,
            },
        )

    @app.post("/sent/sync-cloud")
    def sent_sync_cloud() -> RedirectResponse:
        """Push all local applications to Supabase Cloud and sync status."""
        pushed = 0
        if get_supabase_client:
            sb = get_supabase_client()
            with get_session() as s:
                pushed = sb.sync_applications_to_cloud(s)
        return RedirectResponse(url=f"/sent?synced=1&pushed={pushed}", status_code=303)

    # ---- Radar Dashboard & Discovery Endpoints -----------------------------
    @app.get("/radar", response_class=HTMLResponse)
    def radar_dashboard(request: Request) -> HTMLResponse:
        scanned = request.query_params.get("scanned")
        count = request.query_params.get("count")
        expired = request.query_params.get("expired")
        verified = request.query_params.get("verified")
        updated = request.query_params.get("updated")
        new_status = request.query_params.get("status_val", "")
        deleted = request.query_params.get("deleted")
        synced = request.query_params.get("synced")
        status_filter = request.query_params.get("status", "active")
        error_notice = request.query_params.get("error_notice", "")
        prov_param = request.query_params.get("provider", "tavily")

        qinfo = request.query_params.get("qinfo", "")
        message = ""
        if error_notice:
            message = error_notice
        elif scanned:
            cnt = int(count or 0)
            prefix = f"Scan complete ({prov_param.upper()}). Ran {qinfo} AI structured queries. " if qinfo else f"Scan complete ({prov_param.upper()}). "
            if cnt > 0:
                message = f"{prefix}Discovered and stored {cnt} fresh job postings."
            else:
                message = f"{prefix}0 new active jobs stored (duplicate or already saved)."
        elif synced:
            cnt = int(count or 0)
            message = f"Cloud Sync Complete: Pulled {cnt} fresh job postings from Supabase into local database." if cnt > 0 else "Cloud Sync Complete: Local database is already up to date with Supabase."
        elif verified:
            message = f"Live link check complete: Tested all URLs and auto-expired {count or 0} closed postings."
        elif expired:
            message = f"Freshness run complete: Marked {count or 0} stale jobs as expired."
        elif updated:
            status_text = f" to '{new_status}'" if new_status else ""
            message = f"Job status successfully updated{status_text}."
        elif deleted:
            message = "Item removed from database."

        is_cloud, sb_msg = False, "Supabase client not loaded"
        if get_supabase_client:
            supabase = get_supabase_client()
            is_cloud, sb_msg = supabase.check_connection()

        user = getattr(request.state, "current_user", None)
        user_creds = get_user_provider_credentials(user) if get_user_provider_credentials else {}

        # Resolve provider statuses & cooldowns for UI
        tavily_ok = bool(user_creds.get("tavily_api_key"))
        apify_ok = bool(user_creds.get("apify_api_key"))
        firecrawl_ok = bool(user_creds.get("firecrawl_api_key"))
        serpapi_ok = bool(user_creds.get("serpapi_api_key"))

        tavily_can, tavily_rem, tavily_mode = check_limited_search_cooldown(user, "tavily") if check_limited_search_cooldown else (True, 0, "limited_free")
        apify_can, apify_rem, apify_mode = check_limited_search_cooldown(user, "apify_mcp") if check_limited_search_cooldown else (True, 0, "limited_free")
        firecrawl_can, firecrawl_rem, firecrawl_mode = check_limited_search_cooldown(user, "firecrawl") if check_limited_search_cooldown else (True, 0, "limited_free")
        serpapi_can, serpapi_rem, serpapi_mode = check_limited_search_cooldown(user, "google_jobs") if check_limited_search_cooldown else (True, 0, "limited_free")

        provider_statuses = {
            "apify_mcp": {
                "name": "Apify (Online / Live MCP)",
                "configured": apify_ok,
                "is_user_key": apify_ok,
                "can_search": apify_can,
                "remaining_secs": apify_rem,
                "status_label": "Ready (Custom Key)" if apify_ok else ("Ready (Online MCP Key Pool)" if apify_can else f"Cooldown Active ({max(1, apify_rem//60)}m remaining)"),
            },
            "firecrawl": {
                "name": "Firecrawl (Deep Web Scraper)",
                "configured": firecrawl_ok,
                "is_user_key": firecrawl_ok,
                "can_search": firecrawl_can,
                "remaining_secs": firecrawl_rem,
                "status_label": "Ready (Custom Key)" if firecrawl_ok else ("Ready (System Scraper)" if firecrawl_can else f"Cooldown Active ({max(1, firecrawl_rem//60)}m remaining)"),
            },
            "tavily": {
                "name": "Tavily (Real-Time Search)",
                "configured": tavily_ok,
                "is_user_key": tavily_ok,
                "can_search": tavily_can,
                "remaining_secs": tavily_rem,
                "status_label": "Ready (Custom Key)" if tavily_ok else ("Ready (Limited Free Mode: 3 jobs max)" if tavily_can else f"Cooldown Active ({max(1, tavily_rem//60)}m remaining)"),
            },
            "google_jobs": {
                "name": "Google Jobs / SerpAPI",
                "configured": serpapi_ok,
                "is_user_key": serpapi_ok,
                "can_search": serpapi_can,
                "remaining_secs": serpapi_rem,
                "status_label": "Ready (Custom Key)" if serpapi_ok else ("Ready (SerpAPI Engine)" if serpapi_can else f"Cooldown Active ({max(1, serpapi_rem//60)}m remaining)"),
            },
            "all": {
                "name": "All Providers (Multi-Engine)",
                "configured": True,
                "is_user_key": False,
                "can_search": True,
                "remaining_secs": 0,
                "status_label": "Multi-Engine Parallel Mode",
            }
        }

        with get_session() as s:
            sync_from_supabase_to_memory(s)
            # Auto-expire postings in database older than 48 hours
            now_utc = dt.datetime.now(dt.timezone.utc)
            cutoff_48h = now_utc - dt.timedelta(hours=48)
            stale_active = s.query(Job).filter(
                Job.status == "active",
                ((Job.created_at < cutoff_48h) | (Job.expires_at.is_not(None) & (Job.expires_at < now_utc)))
            ).all()
            for sj in stale_active:
                sj.status = "expired"
                if get_supabase_client:
                    supabase = get_supabase_client()
                    if supabase.is_configured:
                        supabase.update_job_status(sj.id, "expired")
            if stale_active:
                s.commit()

            total_jobs = s.query(Job).count()
            active_jobs = s.query(Job).filter(Job.status == "active").count()
            expired_jobs = s.query(Job).filter(Job.status == "expired").count()
            applied_jobs = s.query(Job).filter(Job.status == "applied").count()
            saved_jobs = s.query(Job).filter(Job.status == "saved").count()
            email_jobs = s.query(Job).filter(Job.has_email.is_(True)).count()
            portal_jobs = total_jobs - email_jobs

            query = s.query(Job)
            if status_filter in ("active", "expired", "applied", "saved", "rejected"):
                query = query.filter(Job.status == status_filter)

            jobs_list = query.order_by(Job.created_at.desc()).limit(100).all()

        return templates.TemplateResponse(
            request=request,
            name="radar.html",
            context={
                "jobs": jobs_list,
                "current_user": user,
                "status_filter": status_filter,
                "provider_statuses": provider_statuses,
                "counts": {
                    "total": total_jobs,
                    "active": active_jobs,
                    "expired": expired_jobs,
                    "applied": applied_jobs,
                    "saved": saved_jobs,
                    "email": email_jobs,
                    "portal": portal_jobs,
                },
                "cloud_online": is_cloud,
                "cloud_msg": sb_msg,
                "message": message,
                "title": "Career Pulse — Radar Discovered Jobs",
            },
        )

    @app.post("/radar/scan")
    def radar_scan_trigger(
        request: Request,
        provider: str = Form("tavily"),
        query: str = Form(""),
        role: str = Form("Junior Developer (AI / Full-Stack / ASE)"),
        custom_role: str = Form(""),
        experience: str = Form("junior"),
        location: str = Form("worldwide_remote_or_pakistan_onsite"),
        custom_location: str = Form(""),
        include_remote: str = Form("true"),
        platform: str = Form("all"),
        limit: int = Form(5),
    ) -> RedirectResponse:
        import urllib.parse
        user = getattr(request.state, "current_user", None)
        user_creds = get_user_provider_credentials(user) if get_user_provider_credentials else {}

        clean_provider = (provider or "apify_mcp").strip().lower()
        if clean_provider not in ("apify_mcp", "apify", "firecrawl", "firecrawl_scraper", "tavily", "google_jobs", "all"):
            clean_provider = "apify_mcp"

        # Server-side Cooldown & Limit Enforcement for Limited Mode
        if check_limited_search_cooldown:
            can_search, remaining_secs, mode = check_limited_search_cooldown(user, clean_provider)
            if not can_search:
                rem_mins = max(1, remaining_secs // 60)
                rem_h = rem_mins // 60
                rem_m = rem_mins % 60
                time_str = f"{rem_h}h {rem_m}m" if rem_h > 0 else f"{rem_mins} minutes"
                err_msg = f"Limited search cooldown active. Next search available in {time_str}. Add your own {clean_provider.upper()} API key in Credentials for unlimited searches."
                return RedirectResponse(
                    url=f"/radar?error_notice={urllib.parse.quote(err_msg)}&status=active",
                    status_code=303,
                )

        effective_limit = limit
        if check_limited_search_cooldown:
            _, _, mode = check_limited_search_cooldown(user, clean_provider)
            if mode == "limited_free":
                effective_limit = min(limit, 3)
                if record_limited_search:
                    record_limited_search(user, clean_provider)

        stored_count = 0
        queries_cnt = 0
        rem_bool = include_remote.lower() in ("true", "1", "yes", "on")
        if run_pipeline:
            stats = run_pipeline(
                query=query.strip() or None,
                role=role,
                custom_role=custom_role,
                experience_level=experience,
                location=location,
                custom_location=custom_location,
                include_remote=rem_bool,
                platform=platform,
                limit=effective_limit,
                push_email_jobs=True,
                provider=clean_provider,
                user_creds=user_creds,
            )
            stored_count = stats.stored_jobs
            queries_cnt = len(stats.queries_run)
        return RedirectResponse(url=f"/radar?scanned=1&count={stored_count}&qinfo={queries_cnt}&provider={clean_provider}&status=active", status_code=303)

    @app.get("/api/location/resolve")
    def api_location_resolve(lat: float = 0.0, lon: float = 0.0) -> JSONResponse:
        """Reverse geocode user coordinates and map to radar location options."""
        city = "Worldwide"
        country = "Global"
        loc_val = "worldwide_remote_or_pakistan_onsite"
        display = "Worldwide Remote & Pakistan Onsite"

        # Check if within Pakistan bounding box (~23.6-37.1 N, ~60.9-77.8 E)
        if 23.5 <= lat <= 37.5 and 60.5 <= lon <= 78.0:
            country = "Pakistan"
            # Lahore area (~31.5, ~74.3)
            if 31.0 <= lat <= 32.2 and 73.8 <= lon <= 74.8:
                city = "Lahore"
                loc_val = "pakistan_lahore"
                display = "Lahore, Pakistan (Onsite & Remote)"
            # Islamabad / Rawalpindi area (~33.6, ~73.0)
            elif 33.0 <= lat <= 34.2 and 72.5 <= lon <= 73.6:
                city = "Islamabad"
                loc_val = "pakistan_islamabad"
                display = "Islamabad, Pakistan (Onsite & Remote)"
            # Faisalabad area (~31.4, ~73.0)
            elif 31.0 <= lat <= 31.8 and 72.5 <= lon <= 73.6:
                city = "Faisalabad"
                loc_val = "pakistan_faisalabad"
                display = "Faisalabad, Pakistan (Onsite & Remote)"
            else:
                city = "Pakistan"
                loc_val = "pakistan_all"
                display = "All Pakistan (Onsite & Remote)"
        elif lat != 0.0 or lon != 0.0:
            loc_val = "worldwide_remote"
            display = f"Coordinates ({lat:.2f}, {lon:.2f}) - Worldwide Remote"

        return JSONResponse({
            "city": city,
            "country": country,
            "location_value": loc_val,
            "display": display,
            "include_remote": True,
        })

    @app.get("/api/location/detect")
    def api_location_detect() -> JSONResponse:
        """Lightweight location detection fallback endpoint."""
        return JSONResponse({
            "country": "Pakistan",
            "city": "Lahore",
            "location_value": "pakistan_lahore",
            "display": "Lahore, Pakistan (Remote & Onsite)",
            "include_remote": True,
        })

    @app.post("/radar/sync-cloud")
    def radar_sync_cloud_trigger() -> RedirectResponse:
        """Pull all fresh and archived job postings from Supabase Cloud and push all applications."""
        imported_count = 0
        pushed_apps = 0
        if get_supabase_client:
            sb = get_supabase_client()
            with get_session() as s:
                imported_count = sb.sync_cloud_to_local(s)
                pushed_apps = sb.sync_applications_to_cloud(s)
        return RedirectResponse(url=f"/radar?synced=1&count={imported_count}&apps={pushed_apps}", status_code=303)

    @app.post("/radar/expire")
    def radar_expire_trigger() -> RedirectResponse:
        expired_count = 0
        if run_expiry_check:
            expired_count = run_expiry_check(check_live_urls=False)
        return RedirectResponse(url=f"/radar?expired=1&count={expired_count}", status_code=303)

    @app.post("/radar/verify-freshness")
    def radar_verify_freshness_trigger() -> RedirectResponse:
        """Scan all active job links live to verify if pages are still open, auto-expiring dead links."""
        expired_count = 0
        if run_expiry_check:
            expired_count = run_expiry_check(check_live_urls=True)
        return RedirectResponse(url=f"/radar?verified=1&count={expired_count}", status_code=303)

    @app.post("/radar/job/{job_id}/status")
    async def radar_job_update_status(
        request: Request,
        job_id: int,
        background_tasks: BackgroundTasks,
        new_status: str = Form(default=""),
        status: str = Form(default=""),
    ) -> Response:
        """Update job status (active, expired, applied, saved, rejected) in SQLite & Supabase asynchronously."""
        final_status = (new_status or status).strip()
        if not final_status:
            try:
                b_json = await request.json()
                final_status = str(b_json.get("new_status") or b_json.get("status") or "").strip()
            except Exception:
                pass

        status_clean = final_status.strip().lower()
        if status_clean not in RADAR_STATUS_ALLOWLIST:
            raise HTTPException(status_code=400, detail=f"Invalid status value: '{final_status}'. Must be one of: {list(RADAR_STATUS_ALLOWLIST)}")
        user = getattr(request.state, "current_user", None)
        user_id = user.id if user else None
        with get_session() as s:
            job = s.get(Job, job_id)
            if not job:
                raise HTTPException(status_code=404, detail="Job not found")
            job.status = status_clean

            app_to_sync_dict = None
            if status_clean == "applied":
                existing_app = s.query(Application).filter(
                    Application.job_id == job.id,
                    Application.user_id == user_id,
                ).first()
                if existing_app:
                    existing_app.status = "sent"
                    existing_app.disposition = "manual_applied"
                    existing_app.sent_at = existing_app.sent_at or dt.datetime.now(dt.timezone.utc)
                    existing_app.updated_at = dt.datetime.now(dt.timezone.utc)
                    app_to_sync_dict = {
                        "id": existing_app.id,
                        "user_id": existing_app.user_id,
                        "job_id": existing_app.job_id,
                        "email": existing_app.email,
                        "job_title": existing_app.job_title,
                        "company": existing_app.company,
                        "link": existing_app.link,
                        "jd_text": (existing_app.jd_text or "")[:4000],
                        "drafted_email": (existing_app.drafted_email or "")[:4000],
                        "subject": existing_app.subject,
                        "status": existing_app.status,
                        "disposition": existing_app.disposition,
                        "ats_score": float(existing_app.ats_score or 0.0),
                        "sent_at": existing_app.sent_at.isoformat() if existing_app.sent_at else None,
                        "updated_at": existing_app.updated_at.isoformat() if existing_app.updated_at else None,
                    }
                else:
                    new_app = Application(
                        user_id=user_id,
                        job_id=job.id,
                        email=job.email or "portal-application@careerpulse.internal",
                        job_title=job.title or "",
                        company=job.company or "",
                        link=job.link or "",
                        jd_text=job.jd_text or "",
                        drafted_email=f"Applied via external career portal for {job.title} at {job.company}.",
                        subject=f"Application for {job.title} — {job.company}",
                        status="sent",
                        disposition="manual_applied",
                        ats_score=0.0,
                        ats_attempts=0,
                        sent_at=dt.datetime.now(dt.timezone.utc),
                        created_at=job.created_at or dt.datetime.now(dt.timezone.utc),
                        updated_at=dt.datetime.now(dt.timezone.utc),
                    )
                    s.add(new_app)
                    s.flush()
                    app_to_sync_dict = {
                        "id": new_app.id,
                        "user_id": new_app.user_id,
                        "job_id": new_app.job_id,
                        "email": new_app.email,
                        "job_title": new_app.job_title,
                        "company": new_app.company,
                        "link": new_app.link,
                        "jd_text": (new_app.jd_text or "")[:4000],
                        "drafted_email": (new_app.drafted_email or "")[:4000],
                        "subject": new_app.subject,
                        "status": new_app.status,
                        "disposition": new_app.disposition,
                        "ats_score": float(new_app.ats_score or 0.0),
                        "sent_at": new_app.sent_at.isoformat() if new_app.sent_at else None,
                        "updated_at": new_app.updated_at.isoformat() if new_app.updated_at else None,
                    }

            s.commit()

            # Schedule non-blocking cloud synchronization
            background_tasks.add_task(bg_sync_job_status, job_id, status_clean)
            if app_to_sync_dict:
                background_tasks.add_task(bg_sync_app, app_to_sync_dict)

        if (
            request.headers.get("x-requested-with") == "XMLHttpRequest"
            or "application/json" in request.headers.get("accept", "")
            or "application/json" in request.headers.get("content-type", "")
        ):
            return JSONResponse({
                "success": True,
                "job_id": job_id,
                "new_status": status_clean,
                "message": f"Status updated to {status_clean.capitalize()}",
            })

        from urllib.parse import quote as _urlquote
        return RedirectResponse(url=f"/radar?updated=1&status_val={_urlquote(status_clean)}", status_code=303)

    @app.post("/radar/job/{job_id}/delete")
    def radar_job_delete(request: Request, job_id: int, background_tasks: BackgroundTasks) -> Response:
        """Delete a job from Radar and Supabase."""
        with get_session() as s:
            job = s.get(Job, job_id)
            key = job.dedup_key if job else None
            if job:
                s.delete(job)
                s.commit()
            if key or job_id:
                background_tasks.add_task(bg_delete_job, job_id, key)

        if (
            request.headers.get("x-requested-with") == "XMLHttpRequest"
            or "application/json" in request.headers.get("accept", "")
        ):
            return JSONResponse({
                "success": True,
                "job_id": job_id,
                "message": "Job deleted successfully",
            })

        return RedirectResponse(url="/radar?deleted=1", status_code=303)

    @app.post("/radar/jobs/bulk-delete")
    async def radar_jobs_bulk_delete(
        request: Request,
        background_tasks: BackgroundTasks,
        job_ids: list[int] = Form(default=[]),
    ) -> Response:
        """Delete multiple selected jobs from Radar in one batch action."""
        if not job_ids:
            try:
                body_json = await request.json()
                job_ids = [int(x) for x in body_json.get("job_ids", [])]
            except Exception:
                pass

        if not job_ids:
            if (
                request.headers.get("x-requested-with") == "XMLHttpRequest"
                or "application/json" in request.headers.get("accept", "")
            ):
                return JSONResponse({"success": False, "deleted_count": 0, "message": "No jobs selected."})
            return RedirectResponse(url="/radar", status_code=303)

        deleted_count = 0
        with get_session() as s:
            for j_id in job_ids:
                job = s.get(Job, j_id)
                if job:
                    key = job.dedup_key
                    s.delete(job)
                    deleted_count += 1
                    background_tasks.add_task(bg_delete_job, j_id, key)
            s.commit()

        if (
            request.headers.get("x-requested-with") == "XMLHttpRequest"
            or "application/json" in request.headers.get("accept", "")
        ):
            return JSONResponse({
                "success": True,
                "deleted_count": deleted_count,
                "job_ids": job_ids,
                "message": f"Successfully deleted {deleted_count} job postings.",
            })

        return RedirectResponse(url=f"/radar?deleted={deleted_count}", status_code=303)

    @app.post("/application/{app_id}/delete")
    def application_delete(app_id: int, request: Request) -> RedirectResponse:
        """Delete an application from Career Pulse and Supabase, and redirect cleanly."""
        user = getattr(request.state, "current_user", None)
        target_redirect = "/sent"
        with get_session() as s:
            app_row = s.get(Application, app_id)
            if app_row:
                if not _owns_application(user, app_row):
                    raise HTTPException(status_code=403, detail="Forbidden.")
                # Default redirect: back to the workspace unless it was sent.
                if app_row.status == "sent":
                    target_redirect = "/sent"
                else:
                    target_redirect = "/"

                # If linked to a job marked as applied, revert job status back to active so it does not resurrect
                if app_row.job_id:
                    j = s.get(Job, app_row.job_id)
                    if j and j.status == "applied":
                        j.status = "active"
                        if get_supabase_client:
                            sb = get_supabase_client()
                            if sb.is_configured:
                                sb.update_job_status(job_id=j.id, status="active", session=s)
                s.delete(app_row)
                s.commit()
                if get_supabase_client:
                    sb = get_supabase_client()
                    if sb.is_configured:
                        sb.delete_application(app_id)

                # Clean up local generated resume files if any exist
                try:
                    (RESUMES_OUTPUT_DIR / f"application_{app_id}.html").unlink(missing_ok=True)
                    (RESUMES_OUTPUT_DIR / f"application_{app_id}.pdf").unlink(missing_ok=True)
                except Exception:
                    pass

        referer = request.headers.get("referer", "")
        if referer:
            clean_ref = referer.split("?")[0]
            if f"/application/{app_id}" not in clean_ref:
                target_redirect = clean_ref

        # Prevent duplicate query params
        base_url = target_redirect.split("?")[0]
        return RedirectResponse(url=f"{base_url}?deleted=1", status_code=303)

    @app.post("/radar/job/{job_id}/queue")
    def radar_job_queue_to_application(job_id: int, request: Request) -> RedirectResponse:
        """Promote a discovered job directly into Career Pulse approval queue."""
        user = getattr(request.state, "current_user", None)
        with get_session() as s:
            job = s.get(Job, job_id)
            if not job:
                raise HTTPException(status_code=404, detail="Job not found")

            # Create tailored application immediately for current user
            row = create_application(
                s,
                email=job.email or "",
                jd_text=job.jd_text,
                job_title=job.title,
                company=job.company,
                link=job.link,
                job_id=job.id,
                user_id=user.id if user else None,
                user=user,
            )
            new_app_id = row.id
        return RedirectResponse(url=f"/application/{new_app_id}?from_radar=1", status_code=303)

    # ---- Resume Delivery Endpoints ----------------------------------------
    @app.get("/application/{app_id}/resume.pdf")
    def application_resume_pdf(app_id: int, request: Request):
        user = getattr(request.state, "current_user", None)
        with get_session() as s:
            row = s.get(Application, app_id)
            if row is None:
                raise HTTPException(status_code=404, detail="Application not found")
            if not _owns_application(user, row):
                raise HTTPException(status_code=403, detail="Forbidden.")

            app_user = s.get(User, row.user_id) if row.user_id else user
            candidate = load_candidate_for_user(app_user) if app_user else load_candidate(user)

            pdf_path = Path(row.resume_path) if row.resume_path else None
            if not pdf_path or not pdf_path.exists():
                html_path = RESUMES_OUTPUT_DIR / f"application_{app_id}.html"
                if not html_path.exists():
                    tmpl_id = row.template_id or getattr(app_user, "selected_template_id", None) or "apex_modern"
                    built = build_resume_content(candidate, row.jd_text or "", job_title=row.job_title or "", variant=tmpl_id)
                    try:
                        _atomic_write_text(html_path, built.html_content)
                    except OSError:
                        pass
                pdf_target = RESUMES_OUTPUT_DIR / f"application_{app_id}.pdf"
                try:
                    render_pdf_from_html(html_path, pdf_target)
                    row.resume_path = str(pdf_target)
                    s.commit()
                    pdf_path = pdf_target
                except PageOverflowError as exc:
                    # User-fixable: trim content and retry (page handling is automatic).
                    raise HTTPException(status_code=422, detail=str(exc))
                except Exception as exc:
                    raise HTTPException(status_code=500, detail=f"Failed to generate resume PDF: {exc}")

            raw_name = candidate.name.strip() if candidate and candidate.name else "Resume"
            clean_name = re.sub(r"[^\w\-]", "", raw_name.replace(" ", "-")) or "Resume"
            filename = f"{clean_name}-Resume.pdf"
            return FileResponse(
                path=str(pdf_path),
                media_type="application/pdf",
                filename=filename,
                content_disposition_type="inline",
            )

    @app.get("/application/{app_id}/resume.html", response_class=HTMLResponse)
    def application_resume_html(app_id: int, request: Request) -> HTMLResponse:
        user = getattr(request.state, "current_user", None)
        with get_session() as s:
            row = s.get(Application, app_id)
            if row is None:
                raise HTTPException(status_code=404, detail="Application not found")
            if not _owns_application(user, row):
                raise HTTPException(status_code=403, detail="Forbidden.")

            app_user = s.get(User, row.user_id) if row.user_id else user
            candidate = load_candidate_for_user(app_user) if app_user else load_candidate(user)

            html_path = RESUMES_OUTPUT_DIR / f"application_{app_id}.html"
            tmpl_id = row.template_id or getattr(app_user, "selected_template_id", None) or "apex_modern"
            if not html_path.exists():
                built = build_resume_content(candidate, row.jd_text or "", job_title=row.job_title or "", variant=tmpl_id)
                try:
                    _atomic_write_text(html_path, built.html_content)
                except OSError:
                    pass
                content = built.html_content
            else:
                content = html_path.read_text(encoding="utf-8")
                if not content or len(content.strip()) < 100 or "<!--REGION:SUMMARY-->" not in content:
                    built = build_resume_content(candidate, row.jd_text or "", job_title=row.job_title or "", variant=tmpl_id)
                    try:
                        _atomic_write_text(html_path, built.html_content)
                    except OSError:
                        pass
                    content = built.html_content

            # Guarantee viewport meta and screen styles are present even for legacy/cached files
            if '<meta name="viewport"' not in content:
                content = content.replace(
                    '<meta charset="utf-8">',
                    '<meta charset="utf-8">\n<meta name="viewport" content="width=device-width, initial-scale=1.0">',
                )
            if '@media screen' not in content:
                screen_css = """
  @media screen {
    html { background: #0f1620; padding: 0; margin: 0; }
    body { max-width: 820px; margin: 0 auto; background: #ffffff; min-height: 1125px; padding: 34px 44px 28px; font-size: 10.2pt; line-height: 1.42; box-shadow: 0 4px 24px rgba(0,0,0,0.35); }
    .name { font-size: 26px; }
    .title { font-size: 16px; }
    .contact { font-size: 11.5px; }
    h2 { font-size: 15px; margin: 12px 0 4px; }
    .entry { margin-top: 6px; }
    .proj { margin-top: 6px; }
    li { margin: 2px 0; }
  }
  @media screen and (max-width: 650px) {
    body { padding: 18px 18px 14px; font-size: 10pt; line-height: 1.4; max-width: 100%; box-shadow: none; min-height: auto; }
    .name { font-size: 22px; }
    .title { font-size: 14px; display: block; margin-left: 0; margin-top: 2px; }
    .contact { font-size: 10.5px; gap: 4px 10px; }
    .entry { flex-direction: column; }
    .entry .right { text-align: left; padding-left: 0; margin-top: 1px; font-size: 9pt; }
  }
  @media print {
    body { font-size: 9.6pt !important; padding: 26px 36px 18px !important; background: #fff !important; box-shadow: none !important; max-width: none !important; min-height: auto !important; }
  }
"""
                content = content.replace("</style>", f"{screen_css}\n</style>")

            # Ensure any aggressive legacy autoFitResume loop is stripped so text never gets crushed
            content = re.sub(r"<script>\s*\(function autoFitResume\(\)[\s\S]*?</script>", "", content)

            return HTMLResponse(
                content=content,
                headers={
                    "Content-Security-Policy": (
                        "default-src 'self' 'unsafe-inline' data:; style-src 'self' 'unsafe-inline'; "
                        "img-src 'self' data:; font-src 'self' data: https://fonts.gstatic.com;"
                    ),
                    "Cache-Control": "no-cache, no-store, must-revalidate",
                    "Pragma": "no-cache",
                    "Expires": "0",
                },
            )

    @app.post("/application/{app_id}/resume/edit")
    def application_resume_edit(
        request: Request,
        app_id: int,
        summary: str = Form(""),
        skills: str = Form(""),
        experience: str = Form(""),
        projects: str = Form(""),
        selected_projects: list[str] = Form(default=[]),
        selected_categories: list[str] = Form(default=[]),
        selected_experiences: list[str] = Form(default=[]),
        skills_by_category_json: str = Form(""),
        # Phase 10 (§U.13): full section coverage.
        edu_degree: str = Form(""),
        edu_institution: str = Form(""),
        edu_dates: str = Form(""),
        edu_location: str = Form(""),
        education_html: str = Form(""),
        certifications_html: str = Form(""),
        certifications_text: str = Form(""),
        links_html: str = Form(""),
        link_linkedin: str = Form(""),
        link_github: str = Form(""),
        link_portfolio: str = Form(""),
        link_website: str = Form(""),
        project_bullets_json: str = Form(""),
        experience_bullets_json: str = Form(""),
        section_order: str = Form(""),
    ) -> RedirectResponse:
        user = getattr(request.state, "current_user", None)
        with get_session() as s:
            row = s.get(Application, app_id)
            if row is None:
                raise HTTPException(status_code=404, detail="Application not found")
            if not _owns_application(user, row):
                raise HTTPException(status_code=403, detail="Forbidden.")

            app_user = s.get(User, row.user_id) if row.user_id else user
            candidate = load_candidate_for_user(app_user) if app_user else load_candidate(user)

            html_file = RESUMES_OUTPUT_DIR / f"application_{app_id}.html"
            if not html_file.exists():
                _, _ti = resolve_template(None)
                base_html = (TEMPLATES_DIR / _ti["template"]).read_text(encoding="utf-8")
            else:
                base_html = html_file.read_text(encoding="utf-8")

            # Parse skills_by_category_json if provided
            skills_by_cat = None
            if skills_by_category_json and skills_by_category_json.strip():
                try:
                    skills_by_cat = json.loads(skills_by_category_json)
                except Exception:
                    skills_by_cat = None

            # Phase 10 (§U.13): per-bullet edits arrive as JSON from the customizer.
            def _parse_json_map(raw: str):
                try:
                    val = json.loads(raw) if raw and raw.strip() else None
                except Exception:
                    return None
                return val if isinstance(val, dict) else None

            project_bullets = _parse_json_map(project_bullets_json)
            exp_bullets_raw = _parse_json_map(experience_bullets_json)
            experience_bullets = None
            if exp_bullets_raw:
                # Values may be newline-separated strings or lists of strings.
                experience_bullets = {
                    k: ([b.strip() for b in v.splitlines() if b.strip()] if isinstance(v, str) else [str(b).strip() for b in v if str(b).strip()])
                    for k, v in exp_bullets_raw.items()
                }

            # Links: start from the editor textarea (pre-filled with the current
            # LINKS region), then apply the quick URL fields on top.
            links_final = links_html or ""
            if not links_final.strip():
                regions_now = extract_regions(base_html)
                links_final = regions_now.get("LINKS", "")
            links_final = update_links_html(links_final, {
                "LinkedIn": link_linkedin,
                "GitHub": link_github,
                "Portfolio": link_portfolio,
                "Website": link_website,
            })

            certs_html_final = certifications_html
            if certifications_text and certifications_text.strip():
                certs_html_final = certifications_text_to_html(certifications_text)

            # Build clean structured HTML for structured education inputs
            if not education_html.strip() and (edu_degree.strip() or edu_institution.strip()):
                import html as _html
                deg = edu_degree.strip()
                if deg and not deg.endswith(","):
                    deg_label = f"{deg},"
                else:
                    deg_label = deg
                dates = edu_dates.strip()
                inst = edu_institution.strip()
                loc = edu_location.strip()
                
                line1 = (
                    '  <div class="entry">\n'
                    f'    <div class="left"><strong>{_html.escape(deg_label)}</strong></div>\n'
                    + (f'    <div class="right">{_html.escape(dates)}</div>\n' if dates else "")
                    + '  </div>'
                )
                line2_parts = []
                if inst:
                    line2_parts.append(f'<div class="left"><em>{_html.escape(inst)}</em></div>')
                if loc:
                    line2_parts.append(f'<div class="sub-right">{_html.escape(loc)}</div>')
                if line2_parts:
                    line2 = (
                        '  <div class="entry" style="margin-top:0;">\n    '
                        + "\n    ".join(line2_parts)
                        + "\n  </div>"
                    )
                    education_html = f"<h2>Education</h2>\n{line1}\n{line2}"
                else:
                    education_html = f"<h2>Education</h2>\n{line1}"

            built = rebuild_resume_from_custom_edits(
                base_html=base_html,
                summary=summary,
                skills_input=skills,
                experience_html=experience,
                projects_html=projects,
                selected_project_ids=selected_projects if selected_projects else None,
                selected_categories=selected_categories if selected_categories else None,
                selected_experiences=selected_experiences,
                skills_by_category=skills_by_cat,
                candidate=candidate,
                job_title=row.job_title or "",
                jd_text=row.jd_text or "",
                education_html=education_html,
                certifications_html=certs_html_final,
                links_html=links_final,
                project_bullets=project_bullets,
                experience_bullets=experience_bullets,
                section_order=section_order,
            )
            _atomic_write_text(html_file, built.html_content)
            pdf_path = RESUMES_OUTPUT_DIR / f"application_{app_id}.pdf"
            # Phase 12: if the PDF cannot fit the chosen page count, the HTML
            # edits + locks + version are still saved below; only the PDF
            # step reports the overflow so the user can switch to 2 pages.
            pdf_error = ""
            try:
                render_pdf_from_html(html_file, pdf_path)
            except PageOverflowError as exc:
                pdf_error = str(exc)

            new_score = score_resume(
                built.resume_text,
                row.jd_text,
                job_title=row.job_title,
                attested_candidate_skills=candidate.skills,
            )
            custom_note_parts = []
            if summary and summary.strip():
                custom_note_parts.append(f"Customized summary: {summary.strip()}")
            if skills and skills.strip():
                custom_note_parts.append(f"Skills: {skills.strip()}")
            if custom_note_parts:
                row.ats_note = " | ".join(custom_note_parts)

            row.ats_score = new_score.ats_readiness_score
            row.resume_path = str(pdf_path)
            row.updated_at = dt.datetime.now(dt.timezone.utc)

            # Phase 10 (§U.13): store exactly the regions the user touched as
            # inviolable locks, so the next recreate re-applies them verbatim.
            touched = set()
            if summary and summary.strip():
                touched.add("SUMMARY")
            if (skills and skills.strip()) or skills_by_cat or selected_categories:
                touched.add("SKILLS")
            if selected_projects or (projects and projects.strip()) or project_bullets:
                touched.add("PROJECTS")
            if (experience and experience.strip()) or selected_experiences or experience_bullets:
                touched.add("EXPERIENCE")
            if (education_html and education_html.strip()) or edu_degree.strip() or edu_institution.strip():
                touched.add("EDUCATION")
            if certs_html_final and certs_html_final.strip():
                touched.add("CERTIFICATIONS")
            if links_final.strip() != (extract_regions(base_html).get("LINKS", "") or "").strip():
                touched.add("LINKS")
            final_regions = extract_regions(built.html_content)
            new_locks = {k: final_regions[k] for k in touched if final_regions.get(k) and final_regions[k].strip()}
            try:
                merged_locks = json.loads(row.resume_locks_json or "{}") or {}
            except Exception:
                merged_locks = {}
            if isinstance(merged_locks, dict):
                merged_locks.update(new_locks)
                row.resume_locks_json = json.dumps(merged_locks)

            # Phase 15: the resume changed — prior approval no longer valid.
            _reset_approval_to_ready(row)

            # Phase 10: a manual edit is a restorable version too (FR-R-03).
            from types import SimpleNamespace
            from app.db import record_resume_version
            record_resume_version(
                s,
                application_id=app_id,
                result=SimpleNamespace(
                    resume_html_path=str(html_file),
                    resume_pdf_path=str(pdf_path),
                    ats_score=new_score.ats_readiness_score,
                    ats_attempts=0,
                    variant=built.variant,
                    iterations=[{
                        "attempt": 0,
                        "score": round(new_score.ats_readiness_score, 1),
                        "delta": 0.0,
                        "gaps_closed": [],
                        "via": "manual_edit",
                        "note": "Resume editor save",
                    }],
                    kb_snapshot_hash="",
                ),
                jd_text=row.jd_text or "",
                job_title=row.job_title or "",
            )
            s.commit()

            if get_supabase_client:
                sb = get_supabase_client()
                if sb and sb.is_configured:
                    sb.upsert_application({
                        "id": row.id,
                        "ats_score": row.ats_score,
                        "resume_path": row.resume_path,
                        "ats_note": row.ats_note,
                        "pdf_page_target": int(row.pdf_page_target or 1),
                        "resume_locks_json": row.resume_locks_json or "{}",
                        "updated_at": row.updated_at.isoformat(),
                    }, session=s)
        if pdf_error:
            return RedirectResponse(url=f"/application/{app_id}/preview?error={pdf_error}", status_code=303)
        return RedirectResponse(url=f"/application/{app_id}?resume_edited=1", status_code=303)

    @app.post("/application/{app_id}/resume/upload")
    async def application_resume_upload(
        request: Request,
        app_id: int,
        custom_resume: UploadFile = File(...),
    ) -> RedirectResponse:
        user = getattr(request.state, "current_user", None)
        with get_session() as s:
            row = s.get(Application, app_id)
            if row is None:
                raise HTTPException(status_code=404, detail="Application not found")
            if not _owns_application(user, row):
                raise HTTPException(status_code=403, detail="Forbidden.")
            try:
                custom_resume.file.seek(0, 2)
                pdf_size = custom_resume.file.tell()
                custom_resume.file.seek(0)
            except Exception:
                pdf_size = 0
            if pdf_size > MAX_CUSTOM_PDF_BYTES:
                return RedirectResponse(
                    url=f"/application/{app_id}?error=The uploaded PDF is too large (max 10 MB).",
                    status_code=303,
                )
            content = await custom_resume.read()
            if not content or not content.startswith(b"%PDF-"):
                return RedirectResponse(
                    url=f"/application/{app_id}?error=The uploaded file is not a valid PDF document.",
                    status_code=303,
                )
            custom_pdf_path = RESUMES_OUTPUT_DIR / f"application_{app_id}_custom.pdf"
            _atomic_write_bytes(custom_pdf_path, content)
            try:
                from app.supabase_storage import upload_resume_to_supabase
                upload_resume_to_supabase(custom_pdf_path)
            except Exception as exc:
                print(f"[Supabase Storage] Notice: custom resume upload deferred or failed: {exc}")
            row.resume_path = str(custom_pdf_path)
            # Phase 15: the resume changed — prior approval no longer valid.
            _reset_approval_to_ready(row)
            row.updated_at = dt.datetime.now(dt.timezone.utc)
            # Audit fix: push the new resume pointer to Supabase immediately,
            # otherwise a restart loses track of the custom PDF.
            sync_app_to_supabase(row, s)
        return RedirectResponse(url=f"/application/{app_id}?resume_uploaded=1", status_code=303)

    @app.post("/application/{app_id}/resume/revert-custom")
    def application_resume_revert_custom(request: Request, app_id: int) -> RedirectResponse:
        user = getattr(request.state, "current_user", None)
        with get_session() as s:
            row = s.get(Application, app_id)
            if row is None:
                raise HTTPException(status_code=404, detail="Application not found")
            if not _owns_application(user, row):
                raise HTTPException(status_code=403, detail="Forbidden.")
            
            generated_pdf = RESUMES_OUTPUT_DIR / f"application_{app_id}_resume.pdf"
            if generated_pdf.exists():
                row.resume_path = str(generated_pdf)
            else:
                app_user = s.get(User, row.user_id) if row.user_id else user
                candidate = load_candidate_for_user(app_user) if app_user else load_candidate(user)
                result = tailor_application_resume(
                    application_id=app_id,
                    jd_text=row.jd_text,
                    job_title=row.job_title,
                    company=row.company,
                    max_projects=5,
                    candidate=candidate,
                    user=app_user or user,
                )
                row.resume_path = result.resume_pdf_path
                row.ats_score = result.ats_score
                row.ats_attempts = result.ats_attempts

            _reset_approval_to_ready(row)
            row.updated_at = dt.datetime.now(dt.timezone.utc)
            s.commit()
            sync_app_to_supabase(row, s)
        return RedirectResponse(url=f"/application/{app_id}?restored_generated=1", status_code=303)

    @app.post("/application/{app_id}/resume/recreate")
    async def application_resume_recreate(
        request: Request,
        app_id: int,
        variant: str = Form("auto"),
        project_count: int = Form(5),
        custom_focus: str = Form(""),
        section_order: str = Form(""),
    ) -> RedirectResponse:
        user = getattr(request.state, "current_user", None)
        with get_session() as s:
            row = s.get(Application, app_id)
            if row is None:
                raise HTTPException(status_code=404, detail="Application not found")
            if not _owns_application(user, row):
                raise HTTPException(status_code=403, detail="Forbidden.")

            app_user = s.get(User, row.user_id) if row.user_id else user
            candidate = load_candidate_for_user(app_user) if app_user else load_candidate(user)
            
            # Resolve the intended template variant with clear precedence:
            # 1. Explicit form variant (if provided and != "auto")
            # 2. Existing application row.template_id
            # 3. User default selected_template_id
            # 4. Global system default 'apex_modern'
            eff_template = variant if (variant and variant != "auto") else (row.template_id or getattr(app_user, "selected_template_id", None) or "apex_modern")
            valid_id, _ = resolve_template(eff_template)
            row.template_id = valid_id
            var_override = valid_id

            # Phase 10 (§U.13): re-apply the user's manual-edit locks so
            # recreating never wipes what they typed in the resume editor.
            try:
                locked_regions = json.loads(row.resume_locks_json or "{}") or {}
            except Exception:
                locked_regions = {}

            # When the user submits AI Custom Instructions or triggers Recreate,
            # ensure project_count and AI skills categorization take full effect.
            if custom_focus and custom_focus.strip():
                locked_regions = {}
                row.resume_locks_json = "{}"
            else:
                # Recreating via AI should dynamically re-rank and select exact project_count projects
                locked_regions.pop("PROJECTS", None)
                # If skills lock was single category or stale, unlock it so AI populates full multi-category skills
                if "SKILLS" in locked_regions:
                    skills_lock_html = str(locked_regions.get("SKILLS") or "")
                    if skills_lock_html.count("<li>") <= 1:
                        locked_regions.pop("SKILLS", None)

            result = tailor_application_resume(
                application_id=app_id,
                jd_text=row.jd_text,
                job_title=row.job_title,
                company=row.company,
                max_projects=project_count,
                variant_override=var_override,
                custom_focus=custom_focus,
                candidate=candidate,
                user=app_user or user,
                locked_regions=locked_regions,
                section_order=section_order,
            )
            row.resume_path = result.resume_pdf_path
            row.ats_score = result.ats_score
            row.ats_attempts = result.ats_attempts
            row.pdf_page_target = int(result.pdf_actual_pages or 1)
            row.updated_at = dt.datetime.now(dt.timezone.utc)
            _reset_approval_to_ready(row)
            
            from app.db import record_resume_version
            record_resume_version(
                s,
                application_id=app_id,
                result=result,
                jd_text=row.jd_text,
                job_title=row.job_title,
            )
            sync_app_to_supabase(row, s)
        return RedirectResponse(
            url=f"/application/{app_id}?resume_recreated=1",
            status_code=303,
        )

    @app.post("/application/{app_id}/pdf-pages")
    async def application_pdf_pages(request: Request, app_id: int) -> Response:
        user = getattr(request.state, "current_user", None)
        form = await request.form()
        pages_raw = str(form.get("pages") or "").strip()
        try:
            pages = int(pages_raw)
            if pages not in (1, 2):
                raise ValueError()
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid page target. Must be 1 or 2.")

        with get_session() as s:
            row = s.get(Application, app_id)
            if row is None:
                raise HTTPException(status_code=404, detail="Application not found")
            if not _owns_application(user, row):
                raise HTTPException(status_code=403, detail="Forbidden.")

            row.pdf_page_target = pages
            html_p = RESUMES_OUTPUT_DIR / f"application_{app_id}.html"
            pdf_p = RESUMES_OUTPUT_DIR / f"application_{app_id}.pdf"
            if html_p.exists():
                try:
                    render_pdf_from_html(html_p, pdf_p, page_target=pages)
                    row.resume_path = str(pdf_p)
                except PageOverflowError as exc:
                    raise HTTPException(status_code=400, detail=str(exc))
            s.commit()
            sync_app_to_supabase(row, s)

        return RedirectResponse(url=f"/application/{app_id}/preview", status_code=303)

    @app.post("/application/{app_id}/resume/restore/{version_no}")
    def application_resume_restore(request: Request, app_id: int, version_no: int) -> RedirectResponse:
        """Phase 16 (FR-R-03): restore a version as the current resume.

        The restored AI content is the base; the user's current manual-edit
        locks are re-applied on top via replace_regions() — locks always win
        over restored AI content, so a restore never wipes the user's words.
        The restore is recorded as a new version (undoable), the ATS score is
        recomputed, and approval resets to ready (content changed).
        """
        user = getattr(request.state, "current_user", None)
        with get_session() as s:
            row = s.get(Application, app_id)
            if row is None:
                raise HTTPException(status_code=404, detail="Application not found")
            if not _owns_application(user, row):
                raise HTTPException(status_code=403, detail="Forbidden.")
            ver = (
                s.query(ResumeVersion)
                .filter_by(application_id=app_id, version_no=version_no)
                .first()
            )
            if ver is None:
                raise HTTPException(status_code=404, detail="Version not found")
            src = Path(ver.resume_path or "")
            if not src.exists():
                raise HTTPException(status_code=410, detail="Version file is no longer on disk.")
            restored_html = src.read_text(encoding="utf-8")

            # ponytail: locks win over restored AI content. The user
            # hand-edited these regions; a restore must not silently wipe
            # their words, so re-apply them verbatim on top of the restore.
            try:
                locks = json.loads(row.resume_locks_json or "{}") or {}
            except Exception:
                locks = {}
            if isinstance(locks, dict) and locks:
                restored_html = replace_regions(restored_html, locks)

            html_file = RESUMES_OUTPUT_DIR / f"application_{app_id}.html"
            _atomic_write_text(html_file, restored_html)
            pdf_path = RESUMES_OUTPUT_DIR / f"application_{app_id}.pdf"
            try:
                render_pdf_from_html(html_file, pdf_path)
            except PageOverflowError as exc:
                s.commit()
                return RedirectResponse(
                    url=f"/application/{app_id}?error={exc}",
                    status_code=303,
                )

            app_user = s.get(User, row.user_id) if row.user_id else user
            candidate = load_candidate_for_user(app_user) if app_user else load_candidate(user)
            new_score = score_resume(
                _html_to_text(restored_html),
                row.jd_text,
                job_title=row.job_title,
                attested_candidate_skills=candidate.skills,
            )
            row.ats_score = new_score.ats_readiness_score
            row.resume_path = str(pdf_path)
            row.updated_at = dt.datetime.now(dt.timezone.utc)
            # Phase 15: the resume changed — prior approval no longer valid.
            _reset_approval_to_ready(row)

            # The restore itself is a version, so it can be undone.
            from types import SimpleNamespace
            from app.db import record_resume_version
            record_resume_version(
                s,
                application_id=app_id,
                result=SimpleNamespace(
                    resume_html_path=str(html_file),
                    resume_pdf_path=str(pdf_path),
                    ats_score=new_score.ats_readiness_score,
                    ats_attempts=0,
                    variant=ver.template_id or "se_al",
                    iterations=[{
                        "attempt": 0,
                        "score": round(new_score.ats_readiness_score, 1),
                        "delta": 0.0,
                        "gaps_closed": [],
                        "via": "restore",
                        "note": f"Restored v{version_no}; manual-edit locks re-applied on top",
                    }],
                    kb_snapshot_hash=ver.kb_snapshot_hash or "",
                ),
                jd_text=row.jd_text or "",
                job_title=row.job_title or "",
            )
            s.commit()
            sync_app_to_supabase(row, s)
        return RedirectResponse(url=f"/application/{app_id}?restored=v{version_no}", status_code=303)

    @app.post("/application/{app_id}/resume/version/{version_no}/delete")
    def application_resume_version_delete(request: Request, app_id: int, version_no: int) -> RedirectResponse:
        """Delete one stored resume version (DB row + versioned HTML file on disk).

        The current (newest) version cannot be deleted — it is the live resume.
        Older versions are independent rows; deleting one leaves the others'
        version numbers untouched (gaps are fine, no renumbering).
        """
        user = getattr(request.state, "current_user", None)
        with get_session() as s:
            row = s.get(Application, app_id)
            if row is None:
                raise HTTPException(status_code=404, detail="Application not found")
            if not _owns_application(user, row):
                raise HTTPException(status_code=403, detail="Forbidden.")
            ver = (
                s.query(ResumeVersion)
                .filter_by(application_id=app_id, version_no=version_no)
                .first()
            )
            if ver is None:
                raise HTTPException(status_code=404, detail="Version not found")
            newest_no = (
                s.query(ResumeVersion.version_no)
                .filter_by(application_id=app_id)
                .order_by(ResumeVersion.version_no.desc())
                .first()
            )
            if newest_no is not None and version_no >= newest_no[0]:
                return RedirectResponse(
                    url=f"/application/{app_id}?error=Cannot+delete+the+current+resume+version.",
                    status_code=303,
                )
            # Remove the versioned HTML snapshot from disk (best effort).
            try:
                ver_file = Path(ver.resume_path or "")
                if ver_file.name and ver_file.exists():
                    ver_file.unlink()
            except Exception:
                pass
            s.delete(ver)
            s.commit()

            # Also delete from Supabase cloud
            try:
                from radar.supabase_client import SupabaseClient
                sb = SupabaseClient()
                if sb.is_configured:
                    sb.delete_resume_version(app_id, version_no)
            except Exception as sb_del_exc:
                logger.debug(f"Cloud delete resume version notice: {sb_del_exc}")

        return RedirectResponse(url=f"/application/{app_id}?version_deleted=v{version_no}", status_code=303)

    # ---- Template Selection & Customization Endpoints ---------------------
    @app.post("/application/{app_id}/change-template")
    def application_change_template(
        request: Request,
        app_id: int,
        template_id: str = Form(...),
    ) -> RedirectResponse:
        """Switch the visual template for an application, preserving all custom user edits."""
        user = getattr(request.state, "current_user", None)
        with get_session() as s:
            row = s.get(Application, app_id)
            if row is None:
                raise HTTPException(status_code=404, detail="Application not found")
            if not _owns_application(user, row):
                raise HTTPException(status_code=403, detail="Forbidden.")

            valid_id, template_meta = resolve_template(template_id)
            row.template_id = valid_id

            app_user = s.get(User, row.user_id) if row.user_id else user
            candidate = load_candidate_for_user(app_user) if app_user else load_candidate(user)

            html_file = RESUMES_OUTPUT_DIR / f"application_{app_id}.html"
            if html_file.exists():
                current_html = html_file.read_text(encoding="utf-8")
                new_html = switch_resume_template(current_html, valid_id, candidate=candidate, jd_text=row.jd_text or "", job_title=row.job_title or "")
            else:
                built = build_resume_content(candidate, row.jd_text or "", job_title=row.job_title or "", variant=valid_id)
                new_html = built.html_content

            _atomic_write_text(html_file, new_html)
            pdf_path = RESUMES_OUTPUT_DIR / f"application_{app_id}.pdf"

            pdf_error = ""
            try:
                render_pdf_from_html(html_file, pdf_path, page_target=row.pdf_page_target or 1)
                row.resume_path = str(pdf_path)
            except PageOverflowError as exc:
                pdf_error = str(exc)
            except Exception as exc:
                logger.warning(f"PDF compilation error on template change for app {app_id}: {exc}")

            new_score = score_resume(
                _html_to_text(new_html),
                row.jd_text,
                job_title=row.job_title,
                attested_candidate_skills=candidate.skills if candidate else None,
                candidate=candidate,
            )
            row.ats_score = new_score.ats_readiness_score
            row.updated_at = dt.datetime.now(dt.timezone.utc)
            _reset_approval_to_ready(row)

            # Record version with new template ID
            from types import SimpleNamespace
            from app.db import record_resume_version
            record_resume_version(
                s,
                application_id=app_id,
                result=SimpleNamespace(
                    resume_html_path=str(html_file),
                    resume_pdf_path=str(pdf_path),
                    ats_score=new_score.ats_readiness_score,
                    ats_attempts=0,
                    variant=valid_id,
                    iterations=[{
                        "attempt": 0,
                        "score": round(new_score.ats_readiness_score, 1),
                        "delta": 0.0,
                        "gaps_closed": [],
                        "via": "template_change",
                        "note": f"Switched template to {template_meta.get('name', valid_id)}",
                    }],
                    kb_snapshot_hash="",
                ),
                jd_text=row.jd_text or "",
                job_title=row.job_title or "",
            )
            s.commit()
            sync_app_to_supabase(row, s)

        if pdf_error:
            return RedirectResponse(url=f"/application/{app_id}?error={pdf_error}", status_code=303)
        return RedirectResponse(url=f"/application/{app_id}?template_changed={valid_id}", status_code=303)

    @app.get("/templates", response_class=HTMLResponse)
    def templates_gallery_page(request: Request) -> HTMLResponse:
        """Browse all available resume templates with live dual-pane interactive preview."""
        user = getattr(request.state, "current_user", None)
        selected_template = get_user_selected_template(user.id if user else None)
        all_templates = get_available_templates()
        return templates.TemplateResponse(
            request=request,
            name="templates_gallery.html",
            context={
                "current_user": user,
                "templates": all_templates,
                "current_template_id": selected_template,
                "user_default_template_id": selected_template,
                "selected_template": selected_template,
                "title": "Career Pulse — Resume Templates",
            },
        )

    @app.get("/templates/preview/{template_id}", response_class=HTMLResponse)
    def template_preview_html(template_id: str, request: Request) -> HTMLResponse:
        """Render live sample resume HTML for a specific template to preview in iframe."""
        user = getattr(request.state, "current_user", None)
        candidate = load_candidate_for_user(user) if user else load_candidate()
        valid_id, _ = resolve_template(template_id)
        sample_jd = (
            "Senior Full Stack Software Engineer specializing in Python, FastAPI, TypeScript, React, "
            "and cloud distributed systems. Experienced with Postgres, Docker, CI/CD, and system architecture."
        )
        built = build_resume_content(
            candidate=candidate,
            jd_text=sample_jd,
            job_title="Senior Full Stack Software Engineer",
            variant=valid_id,
            max_projects=5,
        )
        content = built.html_content
        return HTMLResponse(
            content=content,
            headers={
                "Content-Security-Policy": (
                    "default-src 'none'; style-src 'unsafe-inline'; "
                    "img-src 'self' data:; font-src 'self' data:;"
                )
            },
        )

    @app.post("/templates/select")
    async def template_select_default(request: Request) -> Response:
        """Save user's default template selection across sessions (supports JSON & Form)."""
        user = getattr(request.state, "current_user", None)
        if not user:
            raise HTTPException(status_code=401, detail="Authentication required")

        template_id = None
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            try:
                body = await request.json()
                template_id = body.get("template_id")
            except Exception:
                pass
        else:
            form = await request.form()
            template_id = form.get("template_id")

        if not template_id:
            raise HTTPException(status_code=400, detail="Missing template_id")

        valid_id, template_meta = resolve_template(str(template_id))
        ok = set_user_selected_template(user.id, valid_id)

        if "application/json" in content_type:
            return JSONResponse({
                "success": ok,
                "selected_template_id": valid_id,
                "template_name": template_meta.get("name", valid_id),
            })
        return RedirectResponse(url="/templates?selected=1", status_code=303)

    @app.get("/templates/sample-pdf/{template_id}")
    def template_sample_pdf(template_id: str, request: Request):
        """Compile and serve a sample PDF for the requested template style."""
        user = getattr(request.state, "current_user", None)
        candidate = load_candidate_for_user(user) if user else load_candidate()
        valid_id, ti = resolve_template(template_id)
        sample_jd = (
            "Senior Full Stack Software Engineer specializing in Python, FastAPI, TypeScript, React, "
            "and cloud distributed systems. Experienced with Postgres, Docker, CI/CD, and system architecture."
        )
        built = build_resume_content(
            candidate=candidate,
            jd_text=sample_jd,
            job_title="Senior Full Stack Software Engineer",
            variant=valid_id,
            max_projects=5,
        )
        sample_html = RESUMES_OUTPUT_DIR / f"sample_{valid_id}.html"
        sample_pdf = RESUMES_OUTPUT_DIR / f"sample_{valid_id}.pdf"
        _atomic_write_text(sample_html, built.html_content)
        render_pdf_from_html(sample_html, sample_pdf, page_target=1)

        filename = f"{valid_id}_Sample_Resume.pdf"
        return FileResponse(
            path=str(sample_pdf),
            media_type="application/pdf",
            filename=filename,
            content_disposition_type="inline",
        )

    return app


app = create_app()
