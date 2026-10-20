"""Cloud-backed startup must only read, even in email test mode or on failure."""
import json
from contextlib import contextmanager
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import config, db
from app.models import Base, User
from radar import supabase_client


@pytest.fixture
def local_db(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    @contextmanager
    def session():
        with Session(engine, expire_on_commit=False) as value:
            yield value

    monkeypatch.setattr(db, "_engine", engine)
    monkeypatch.setattr(db, "get_session", session)
    monkeypatch.setattr(config, "ADMIN_EMAIL", "cloud@example.test")
    monkeypatch.setattr(config, "AUTH_PASSWORD", "offline-only-password")
    monkeypatch.setattr(config, "TEST_MODE", True)
    yield session
    engine.dispose()


@pytest.mark.parametrize("profile", [{}, {
    "skills": ["python", "n8n"],
    "experience": [{"employer": "First"}, {"employer": "Second"}, {"employer": "Spiral Lab"}],
}])
def test_cloud_startup_loads_profile_in_test_mode_without_writing(local_db, monkeypatch, profile):
    cloud = Mock(is_configured=True)
    cloud.fetch_all_users.return_value = [{
        "id": 1001, "email": config.ADMIN_EMAIL,
        "knowledge_base_json": json.dumps(profile),
    }]
    for name in ("fetch_all_jobs", "fetch_all_applications", "fetch_all_resume_versions", "fetch_all_email_activities"):
        getattr(cloud, name).return_value = []
    monkeypatch.setattr(supabase_client, "SupabaseClient", lambda: cloud)
    monkeypatch.delitem(db.sys.modules, "pytest")
    db.init_db()
    with local_db() as session:
        user = session.get(User, 1001)
        assert user is not None
        assert json.loads(user.knowledge_base_json) == profile
    assert {call[0] for call in cloud.method_calls} == {
        "fetch_all_users", "fetch_all_jobs", "fetch_all_applications",
        "fetch_all_resume_versions", "fetch_all_email_activities",
    }
    assert config.TEST_MODE is True


def test_cloud_failure_does_not_seed_local_defaults_or_upsert(local_db, monkeypatch):
    cloud = Mock(is_configured=True)
    cloud.fetch_all_users.side_effect = RuntimeError("Unavailable")
    monkeypatch.setattr(supabase_client, "SupabaseClient", lambda: cloud)
    monkeypatch.delitem(db.sys.modules, "pytest")
    db.init_db()
    with local_db() as session:
        assert session.query(User).count() == 0
    assert [call[0] for call in cloud.method_calls] == ["fetch_all_users"]


def test_offline_admin_bootstrap_still_works(local_db, monkeypatch):
    monkeypatch.setattr(supabase_client, "SupabaseClient", lambda: Mock(is_configured=False))
    db.seed_admin_user()
    with local_db() as session:
        user = session.query(User).one()
        assert user.email == config.ADMIN_EMAIL
        assert json.loads(user.knowledge_base_json)["skills"]
