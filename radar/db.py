"""Shared database session manager for Radar and Career Pulse."""

from __future__ import annotations

import logging
from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app import db as app_db
from radar.supabase_client import SupabaseClient

logger = logging.getLogger("radar.db")

_supabase: SupabaseClient | None = None


def get_supabase_client() -> SupabaseClient:
    global _supabase
    if _supabase is None:
        _supabase = SupabaseClient()
    return _supabase


def get_engine() -> Engine:
    return app_db._engine


def get_session_factory() -> sessionmaker[Session]:
    return app_db._Session


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """Provide a transactional scope using the unified shared application database."""
    with app_db.get_session() as session:
        yield session


def init_db() -> None:
    """Initialize unified database tables."""
    app_db.init_db()
