"""Shared database schema — the contract between Radar and Career Pulse.

Both projects point at the same database (Supabase Postgres in production). Radar
writes `jobs`; for jobs that carry an apply-to email it also creates an
`applications` row (the approval queue). Career Pulse reads that queue, drafts the
email, tailors the resume, and — after your approval — sends it.

Kept intentionally small: two tables, plain columns, no ORM cleverness.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class Base(DeclarativeBase):
    pass


class Job(Base):
    """A discovered job. Written by Radar; read by Career Pulse."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    dedup_key: Mapped[str] = mapped_column(String(120), index=True)
    title: Mapped[str] = mapped_column(String(400), default="")
    company: Mapped[str] = mapped_column(String(300), default="")
    location: Mapped[str] = mapped_column(String(300), default="")
    link: Mapped[str] = mapped_column(Text)  # every job must have a valid link
    has_email: Mapped[bool] = mapped_column(Boolean, default=False)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    jd_text: Mapped[str] = mapped_column(Text, default="")
    deadline: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)  # active|expired
    source: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


class User(Base):
    """User account model for multi-tenant access control."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(200), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # SMTP / Sender credentials (app passwords stored encrypted)
    smtp_host: Mapped[str] = mapped_column(String(255), default="smtp.gmail.com")
    smtp_port: Mapped[int] = mapped_column(Integer, default=587)
    smtp_username: Mapped[str] = mapped_column(String(320), default="")
    smtp_password_encrypted: Mapped[str] = mapped_column(Text, default="")
    sender_name: Mapped[str] = mapped_column(String(200), default="")
    smtp_verified: Mapped[bool] = mapped_column(Boolean, default=False)

    # Per-user Knowledge Base (JSON payload containing profile, experience, projects, skills)
    knowledge_base_json: Mapped[str] = mapped_column(Text, default="")
    # User's globally preferred resume template (e.g. 'apex_modern', 'oxford_editorial')
    selected_template_id: Mapped[str] = mapped_column(String(50), default="apex_modern")

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class Application(Base):
    """One item in the approval queue. Created by Radar (email jobs) or by hand."""

    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    email: Mapped[str] = mapped_column(String(320))  # recipient (employer)
    job_title: Mapped[str] = mapped_column(String(400), default="")
    company: Mapped[str] = mapped_column(String(300), default="")
    link: Mapped[str] = mapped_column(Text, default="")
    jd_text: Mapped[str] = mapped_column(Text, default="")

    drafted_email: Mapped[str] = mapped_column(Text, default="")
    subject: Mapped[str] = mapped_column(String(400), default="")
    resume_path: Mapped[str] = mapped_column(Text, default="")
    # Phase 12 (§U.8): user's chosen PDF page count (1 or 2); 1-page default.
    # The cached PDF filename is shared between variants; changing the target
    # deletes the cached file so the next render uses the new target.
    pdf_page_target: Mapped[int] = mapped_column(Integer, default=1)
    # Template used for this specific application resume
    template_id: Mapped[str] = mapped_column(String(50), default="apex_modern")
    ats_score: Mapped[float] = mapped_column(Float, default=0.0)
    ats_attempts: Mapped[int] = mapped_column(Integer, default=0)
    ats_note: Mapped[str] = mapped_column(Text, default="")
    # Phase 10 (§U.13): user manual edits are inviolable locks the AI never
    # overwrites. {REGION_NAME: final region HTML} re-applied on every recreate.
    resume_locks_json: Mapped[str] = mapped_column(Text, default="{}")

    # pending -> approved -> sent | failed
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    disposition: Mapped[str] = mapped_column(String(40), default="")
    sent_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class ResumeVersion(Base):
    """One persisted generation of an application's resume (FR-R-03, Phase 8).

    Every tailor_application_resume run inserts a row; the best build's HTML is
    copied to a versioned filename (application_<id>_v<n>.html) so any version
    is restorable. score_history_json holds the per-attempt score history.
    Local table; the matching Supabase migration is staged in schema.sql
    (SUPABASE MANUAL ACTION REQUIRED) and is not applied remotely yet.
    """

    __tablename__ = "resume_versions"
    __table_args__ = (UniqueConstraint("application_id", "version_no", name="uq_resume_versions_app_no"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), index=True
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    role: Mapped[str] = mapped_column(Text, default="")
    jd_text: Mapped[str] = mapped_column(Text, default="")
    kb_snapshot_hash: Mapped[str] = mapped_column(String(64), default="")
    resume_path: Mapped[str] = mapped_column(Text, default="")
    pdf_path: Mapped[str] = mapped_column(Text, default="")
    ats_score: Mapped[float] = mapped_column(Float, default=0.0)
    iterations: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    score_history_json: Mapped[str] = mapped_column(Text, default="[]")
    template_id: Mapped[str] = mapped_column(String(50), default="apex_modern")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now, onupdate=_now)
