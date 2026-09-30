"""Shared database schema — re-exported from app.models for unified ORM mapping."""

from __future__ import annotations

from app.models import Base, Job, Application, User, ResumeVersion

__all__ = ["Base", "Job", "Application", "User", "ResumeVersion"]
