"""Database engine, session, and Supabase cloud synchronization for Career Pulse.

The system uses Supabase PostgreSQL as the primary persistent cloud database.
An in-memory engine (sqlite:///:memory: via StaticPool) serves as the fast,
thread-safe working replica, eliminating all local database disk files and locks.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import datetime as dt
import json
import logging

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import config
from app.config import database_url
from app.models import Application, Base, Job, ResumeVersion, User

logger = logging.getLogger("careerpulse.db")

db_url = database_url()
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

if db_url.startswith("postgresql://") and "+" not in db_url.split("://")[0]:
    try:
        import psycopg2  # noqa: F401
        db_url = db_url.replace("postgresql://", "postgresql+psycopg2://", 1)
    except ImportError:
        try:
            import psycopg  # noqa: F401
            db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)
        except ImportError:
            pass

connect_args = {}
engine_kwargs = {"future": True}
if db_url.startswith("sqlite"):
    connect_args = {"check_same_thread": False}
    engine_kwargs["poolclass"] = StaticPool
else:
    engine_kwargs["pool_pre_ping"] = True
    engine_kwargs["pool_recycle"] = 300

_engine = create_engine(db_url, connect_args=connect_args, **engine_kwargs)
_Session = sessionmaker(bind=_engine, expire_on_commit=False, future=True)


def _parse_dt(val: object) -> dt.datetime | None:
    if not val:
        return None
    try:
        return dt.datetime.fromisoformat(str(val).replace("Z", "+00:00"))
    except Exception:
        return None


def sync_from_supabase_to_memory(session: Session) -> None:
    """Hydrate in-memory replica with all data from Supabase cloud database."""
    try:
        from radar.supabase_client import SupabaseClient
        sb = SupabaseClient()
        if not sb.is_configured:
            return

        # 1. Hydrate Users
        cloud_users = sb.fetch_all_users()
        for u in cloud_users:
            uid = u.get("id")
            if not uid:
                continue
            u_email = (u.get("email") or "").strip().lower()
            existing = session.get(User, uid)
            if not existing and u_email:
                existing = session.scalar(select(User).where(User.email == u_email))
            
            kb_cloud = u.get("knowledge_base_json")
            if not existing:
                new_u = User(
                    id=uid,
                    email=u_email,
                    password_hash=u.get("password_hash") or "",
                    name=u.get("name") or "",
                    is_active=bool(u.get("is_active", True)),
                    smtp_host=u.get("smtp_host") or "smtp.gmail.com",
                    smtp_port=int(u.get("smtp_port") or 587),
                    smtp_username=u.get("smtp_username") or "",
                    smtp_password_encrypted=u.get("smtp_password_encrypted") or "",
                    sender_name=u.get("sender_name") or "",
                    smtp_verified=bool(u.get("smtp_verified", False)),
                    knowledge_base_json=kb_cloud if kb_cloud and kb_cloud.strip() not in ("", "{}") else "{}",
                    selected_template_id=u.get("selected_template_id") or "apex_modern",
                    created_at=_parse_dt(u.get("created_at")) or dt.datetime.now(dt.timezone.utc),
                    updated_at=_parse_dt(u.get("updated_at")) or dt.datetime.now(dt.timezone.utc),
                )
                session.add(new_u)
            else:
                existing.email = u_email or existing.email
                if u.get("password_hash"):
                    existing.password_hash = u.get("password_hash")
                if u.get("name"):
                    existing.name = u.get("name")
                existing.is_active = bool(u.get("is_active", existing.is_active))
                existing.smtp_host = u.get("smtp_host") or existing.smtp_host
                existing.smtp_port = int(u.get("smtp_port") or existing.smtp_port)
                existing.smtp_username = u.get("smtp_username") or existing.smtp_username
                existing.smtp_password_encrypted = u.get("smtp_password_encrypted") or existing.smtp_password_encrypted
                existing.sender_name = u.get("sender_name") or existing.sender_name
                existing.smtp_verified = bool(u.get("smtp_verified", existing.smtp_verified))
                if u.get("selected_template_id"):
                    existing.selected_template_id = u.get("selected_template_id")
                if kb_cloud and kb_cloud.strip() not in ("", "{}"):
                    existing.knowledge_base_json = kb_cloud

        # 2. Hydrate Jobs
        cloud_jobs = sb.fetch_all_jobs(limit=1000)
        for j in cloud_jobs:
            jid = j.get("id")
            if not jid:
                continue
            j_uid = j.get("user_id")
            existing_j = session.get(Job, jid)
            if not existing_j and j.get("dedup_key"):
                if j_uid is not None:
                    existing_j = session.scalar(select(Job).where(Job.dedup_key == j.get("dedup_key"), Job.user_id == j_uid))
                else:
                    existing_j = session.scalar(select(Job).where(Job.dedup_key == j.get("dedup_key"), Job.user_id.is_(None)))
            
            if not existing_j:
                new_j = Job(
                    id=jid,
                    user_id=j_uid,
                    dedup_key=j.get("dedup_key") or f"job_{jid}",
                    title=j.get("title") or "",
                    company=j.get("company") or "",
                    location=j.get("location") or "",
                    link=j.get("link") or "",
                    has_email=bool(j.get("has_email", False)),
                    email=j.get("email"),
                    jd_text=j.get("jd_text") or "",
                    deadline=_parse_dt(j.get("deadline")),
                    status=j.get("status") or "active",
                    source=j.get("source") or "",
                    created_at=_parse_dt(j.get("created_at")) or dt.datetime.now(dt.timezone.utc),
                    expires_at=_parse_dt(j.get("expires_at")),
                )
                session.add(new_j)
            else:
                existing_j.status = j.get("status") or existing_j.status
                existing_j.title = j.get("title") or existing_j.title
                existing_j.company = j.get("company") or existing_j.company
                if j_uid is not None:
                    existing_j.user_id = j_uid

        # 3. Hydrate Applications
        cloud_apps = sb.fetch_all_applications()
        for a in cloud_apps:
            aid = a.get("id")
            if not aid:
                continue
            existing_a = session.get(Application, aid)
            if not existing_a:
                new_a = Application(
                    id=aid,
                    user_id=a.get("user_id"),
                    job_id=a.get("job_id"),
                    email=a.get("email") or "",
                    job_title=a.get("job_title") or "",
                    company=a.get("company") or "",
                    link=a.get("link") or "",
                    jd_text=a.get("jd_text") or "",
                    drafted_email=a.get("drafted_email") or "",
                    subject=a.get("subject") or "",
                    resume_path=a.get("resume_path") or "",
                    template_id=a.get("template_id") or "apex_modern",
                    ats_score=float(a.get("ats_score") or 0.0),
                    ats_attempts=int(a.get("ats_attempts") or 0),
                    ats_note=a.get("ats_note") or "",
                    status=a.get("status") or "draft",
                    disposition=a.get("disposition") or "",
                    sent_at=_parse_dt(a.get("sent_at")),
                    created_at=_parse_dt(a.get("created_at")) or dt.datetime.now(dt.timezone.utc),
                    updated_at=_parse_dt(a.get("updated_at")) or dt.datetime.now(dt.timezone.utc),
                )
                session.add(new_a)
            else:
                existing_a.status = a.get("status") or existing_a.status
                existing_a.sent_at = _parse_dt(a.get("sent_at")) or existing_a.sent_at
                existing_a.disposition = a.get("disposition") or existing_a.disposition
                existing_a.ats_score = float(a.get("ats_score") or existing_a.ats_score)
                existing_a.ats_note = a.get("ats_note") or existing_a.ats_note
                existing_a.user_id = a.get("user_id") or existing_a.user_id
                existing_a.job_title = a.get("job_title") or existing_a.job_title
                existing_a.company = a.get("company") or existing_a.company
                existing_a.email = a.get("email") or existing_a.email
                if a.get("template_id"):
                    existing_a.template_id = a.get("template_id")
        # 4. Hydrate Resume Versions from Supabase Cloud
        try:
            cloud_versions = sb.fetch_all_resume_versions()
            for v in cloud_versions:
                v_app_id = v.get("application_id")
                v_no = v.get("version_no")
                if not v_app_id or not v_no:
                    continue
                existing_v = session.scalar(
                    select(ResumeVersion).where(
                        ResumeVersion.application_id == v_app_id,
                        ResumeVersion.version_no == int(v_no),
                    )
                )
                if not existing_v:
                    new_v = ResumeVersion(
                        application_id=int(v_app_id),
                        version_no=int(v_no),
                        role=v.get("role") or "",
                        jd_text=v.get("jd_text") or "",
                        kb_snapshot_hash=v.get("kb_snapshot_hash") or "",
                        resume_path=v.get("resume_path") or "",
                        pdf_path=v.get("pdf_path") or "",
                        ats_score=float(v.get("ats_score") or 0.0),
                        iterations=int(v.get("iterations") or 1),
                        score_history_json=v.get("score_history_json") or "[]",
                        template_id=v.get("template_id") or "apex_modern",
                        created_at=_parse_dt(v.get("created_at")) or dt.datetime.now(dt.timezone.utc),
                        updated_at=_parse_dt(v.get("updated_at")) or dt.datetime.now(dt.timezone.utc),
                    )
                    session.add(new_v)
        except Exception as ver_exc:
            logger.debug(f"Cloud resume versions sync note: {ver_exc}")

        # 5. Disk Resume Version Hydration (Ensures versions persist across in-memory DB restarts)
        try:
            import re
            import shutil
            from pathlib import Path
            from app.tailor import RESUMES_OUTPUT_DIR
            if RESUMES_OUTPUT_DIR.exists():
                for vfile in RESUMES_OUTPUT_DIR.glob("application_*_v*.html"):
                    m = re.match(r"^application_(\d+)_v(\d+)\.html$", vfile.name)
                    if m:
                        aid = int(m.group(1))
                        vno = int(m.group(2))
                        app_row = session.get(Application, aid)
                        if not app_row:
                            continue
                        existing_v = session.scalar(
                            select(ResumeVersion).where(
                                ResumeVersion.application_id == aid,
                                ResumeVersion.version_no == vno,
                            )
                        )
                        if not existing_v:
                            vrow = ResumeVersion(
                                application_id=aid,
                                version_no=vno,
                                role=app_row.job_title or "",
                                jd_text=app_row.jd_text or "",
                                kb_snapshot_hash="",
                                resume_path=str(vfile),
                                pdf_path=str(RESUMES_OUTPUT_DIR / f"application_{aid}.pdf"),
                                ats_score=app_row.ats_score or 0.0,
                                iterations=app_row.ats_attempts or 1,
                                score_history_json="[]",
                                template_id=app_row.template_id or "apex_modern",
                                created_at=app_row.created_at or dt.datetime.now(dt.timezone.utc),
                                updated_at=app_row.updated_at or dt.datetime.now(dt.timezone.utc),
                            )
                            session.add(vrow)

                # For any application with an existing resume on disk and 0 versions, seed version 1
                for app_row in session.query(Application).all():
                    ver_count = session.query(ResumeVersion).filter_by(application_id=app_row.id).count()
                    if ver_count == 0:
                        main_html = RESUMES_OUTPUT_DIR / f"application_{app_row.id}.html"
                        if main_html.exists():
                            v1_file = RESUMES_OUTPUT_DIR / f"application_{app_row.id}_v1.html"
                            if not v1_file.exists():
                                try:
                                    shutil.copy2(main_html, v1_file)
                                except Exception:
                                    v1_file = main_html
                            v1_row = ResumeVersion(
                                application_id=app_row.id,
                                version_no=1,
                                role=app_row.job_title or "",
                                jd_text=app_row.jd_text or "",
                                kb_snapshot_hash="",
                                resume_path=str(v1_file),
                                pdf_path=str(RESUMES_OUTPUT_DIR / f"application_{app_row.id}.pdf"),
                                ats_score=app_row.ats_score or 0.0,
                                iterations=app_row.ats_attempts or 1,
                                score_history_json="[]",
                                template_id=app_row.template_id or "apex_modern",
                                created_at=app_row.created_at or dt.datetime.now(dt.timezone.utc),
                                updated_at=app_row.updated_at or dt.datetime.now(dt.timezone.utc),
                            )
                            session.add(v1_row)
        except Exception as disk_exc:
            logger.debug(f"Disk resume version hydration note: {disk_exc}")

        session.commit()
    except Exception as exc:
        logger.warning(f"Supabase hydration warning: {exc}")


def seed_admin_user() -> None:
    """Ensure the system administrator exists and is seeded with candidate knowledge base in Supabase."""
    from app.auth import encrypt_credential, hash_password
    from app.knowledge import load_candidate

    admin_email = config.ADMIN_EMAIL.strip().lower()
    if not admin_email:
        return

    try:
        with get_session() as session:
            admin_user = session.scalar(select(User).where(User.email == admin_email))
            bootstrap_password = config.AUTH_PASSWORD.strip()
            
            if not admin_user:
                if not bootstrap_password:
                    logger.warning(
                        "seed_admin_user: no admin user exists and AUTH_PASSWORD is not set — "
                        "skipping admin bootstrap. Set AUTH_PASSWORD to create the initial admin."
                    )
                    return
                candidate = load_candidate()
                kb_data = {
                    "name": candidate.name or "Haseeb Ur Rahman",
                    "email": candidate.email or admin_email,
                    "phone": candidate.phone or "",
                    "location": candidate.location or "Sahiwal, Pakistan",
                    "links": candidate.links or {},
                    "summary": candidate.summary or "Full-Stack & AI/ML Engineer specializing in Agentic AI, RAG, and FastAPI.",
                    "skills": candidate.skills or [],
                    "projects": candidate.projects or [],
                    "experience": candidate.experience or [],
                    "education": candidate.education or [],
                    "certifications": candidate.certifications or [],
                }
                admin_user = User(
                    email=admin_email,
                    password_hash=hash_password(bootstrap_password),
                    name=candidate.name or "Haseeb Ur Rahman",
                    is_active=True,
                    smtp_host=config.SMTP_HOST,
                    smtp_port=config.SMTP_PORT,
                    smtp_username=config.SMTP_USERNAME,
                    smtp_password_encrypted=encrypt_credential(config.SMTP_PASSWORD),
                    sender_name=candidate.name or "Haseeb Ur Rahman",
                    smtp_verified=bool(config.SMTP_USERNAME and config.SMTP_PASSWORD),
                    knowledge_base_json=json.dumps(kb_data, ensure_ascii=False),
                )
                session.add(admin_user)
                session.commit()

                # Also sync to Supabase cloud directly
                try:
                    from radar.supabase_client import SupabaseClient
                    sb = SupabaseClient()
                    if sb.is_configured:
                        sb.upsert_user({
                            "email": admin_user.email,
                            "password_hash": admin_user.password_hash,
                            "name": admin_user.name,
                            "is_active": admin_user.is_active,
                            "smtp_host": admin_user.smtp_host,
                            "smtp_port": admin_user.smtp_port,
                            "smtp_username": admin_user.smtp_username,
                            "smtp_password_encrypted": admin_user.smtp_password_encrypted,
                            "sender_name": admin_user.sender_name,
                            "smtp_verified": admin_user.smtp_verified,
                            "knowledge_base_json": admin_user.knowledge_base_json,
                            "selected_template_id": admin_user.selected_template_id,
                        })
                except Exception:
                    pass
            else:
                # If admin exists but has completely empty KB and no KB in cloud, seed with default
                if not admin_user.knowledge_base_json or admin_user.knowledge_base_json.strip() in ("", "{}"):
                    candidate = load_candidate()
                    kb_data = {
                        "name": candidate.name or "Haseeb Ur Rahman",
                        "email": candidate.email or admin_email,
                        "phone": candidate.phone or "",
                        "location": candidate.location or "Sahiwal, Pakistan",
                        "links": candidate.links or {},
                        "summary": candidate.summary or "Full-Stack & AI/ML Engineer specializing in Agentic AI, RAG, and FastAPI.",
                        "skills": candidate.skills or [],
                        "projects": candidate.projects or [],
                        "experience": candidate.experience or [],
                        "education": candidate.education or [],
                        "certifications": candidate.certifications or [],
                    }
                    admin_user.knowledge_base_json = json.dumps(kb_data, ensure_ascii=False)
                    session.commit()
    except Exception as exc:
        logger.warning(f"seed_admin_user notice: {exc}")

            # NOTE: NULL user_id applications are intentionally NOT backfilled to
            # any account. Ownership must be established by investigation first
            # (see the listing query in schema.sql). New rows always get a
            # user_id server-side; _owns_application() denies NULL rows.
    except Exception as exc:
        logger.warning(f"seed_admin_user notice: {exc}")


def init_db() -> None:
    """Create in-memory schema, hydrate live data from Supabase, and ensure admin exists."""
    Base.metadata.create_all(_engine)
    with get_session() as s:
        sync_from_supabase_to_memory(s)
    seed_admin_user()


@contextmanager
def get_session() -> Iterator[Session]:
    session = _Session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def record_resume_version(
    session,
    *,
    application_id: int,
    result,
    jd_text: str = "",
    job_title: str = "",
) -> "ResumeVersion | None":
    """Persist one generation as a version row (FR-R-03, Phase 8).

    Copies the best build's HTML to a versioned filename
    (``application_<id>_v<n>.html``) so any version is restorable, then inserts
    the ResumeVersion row with the per-attempt score history. Never raises —
    version persistence must not break the generation flow.
    """
    try:
        import shutil

        from sqlalchemy import func

        from app.tailor import RESUMES_OUTPUT_DIR  # lazy: tailor never imports db

        # Use MAX(version_no) + 1, not COUNT + 1 — a deleted version leaves a
        # gap, and COUNT would then collide with an existing version_no.
        max_no = (
            session.query(func.max(ResumeVersion.version_no))
            .filter_by(application_id=application_id)
            .scalar()
        )
        version_no = (max_no or 0) + 1
        RESUMES_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        versioned = RESUMES_OUTPUT_DIR / f"application_{application_id}_v{version_no}.html"
        shutil.copy2(result.resume_html_path, versioned)
        row = ResumeVersion(
            application_id=application_id,
            version_no=version_no,
            role=job_title or "",
            jd_text=jd_text or "",
            kb_snapshot_hash=result.kb_snapshot_hash or "",
            resume_path=str(versioned),
            pdf_path=result.resume_pdf_path or "",
            ats_score=result.ats_score,
            iterations=result.ats_attempts,
            score_history_json=json.dumps(result.iterations),
            template_id=result.variant or "apex_modern",
        )
        session.add(row)
        session.flush()

        # Real-time Cloud Sync to Supabase
        try:
            from radar.supabase_client import SupabaseClient
            sb = SupabaseClient()
            if sb.is_configured:
                sb.upsert_resume_version({
                    "application_id": application_id,
                    "version_no": version_no,
                    "role": row.role,
                    "jd_text": (row.jd_text or "")[:4000],
                    "kb_snapshot_hash": row.kb_snapshot_hash,
                    "resume_path": row.resume_path,
                    "pdf_path": row.pdf_path,
                    "ats_score": row.ats_score,
                    "iterations": row.iterations,
                    "score_history_json": row.score_history_json,
                    "template_id": row.template_id,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                })
        except Exception as sb_exc:
            logger.debug(f"Supabase resume version push warning: {sb_exc}")

        return row
    except Exception as exc:
        logger.warning("record_resume_version failed for app %s: %s", application_id, exc)
        return None


def get_user_selected_template(user_id: int | None) -> str:
    """Retrieve the selected template ID for a specific user, with multi-user safety."""
    if not user_id:
        return "apex_modern"
    try:
        with get_session() as s:
            u = s.get(User, user_id)
            if u and u.selected_template_id:
                return u.selected_template_id
    except Exception as exc:
        logger.warning(f"Error fetching selected template for user {user_id}: {exc}")
    return "apex_modern"


def set_user_selected_template(user_id: int | None, template_id: str) -> bool:
    """Update and persist the user's selected template in memory and Supabase cloud."""
    if not user_id or not template_id:
        return False
    try:
        with get_session() as s:
            u = s.get(User, user_id)
            if u:
                u.selected_template_id = template_id
                s.commit()

        # Cloud sync to Supabase
        try:
            from radar.supabase_client import SupabaseClient
            sb = SupabaseClient()
            if sb.is_configured:
                sb.upsert_user({"id": user_id, "selected_template_id": template_id})
        except Exception as cloud_exc:
            logger.warning(f"Failed to sync selected template to Supabase for user {user_id}: {cloud_exc}")
        return True
    except Exception as exc:
        logger.warning(f"Error setting selected template for user {user_id}: {exc}")
        return False
