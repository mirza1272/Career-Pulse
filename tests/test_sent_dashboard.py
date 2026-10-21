"""Sent pages and activity sync must work for Gmail-connected accounts."""
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from app import gmail
import app.main as main
from app.auth import create_session_token, hash_password
from app.db import get_session
from app.models import Application, User


@pytest.fixture
def account(request, monkeypatch):
    connected = request.param
    sync_calls = []
    monkeypatch.setattr(main, '_maybe_hydrate_from_cloud', lambda *args: None)
    monkeypatch.setattr(main, 'sync_app_to_supabase', lambda *args: None)

    def sync(user, session):
        sync_calls.append(user.id)
        return {'synced': 1, 'replies': 0, 'bounces': 0}

    monkeypatch.setattr(gmail, 'sync_user_gmail_threads', sync)
    email = f'sent-page-{uuid.uuid4().hex}@example.test'
    with get_session() as session:
        user = User(email=email, name='Sent Account', password_hash=hash_password('test-only'),
                    gmail_connected=connected, smtp_verified=True, smtp_username=email,
                    smtp_password_encrypted='test-only', knowledge_base_json=json.dumps({'skills': ['Python']}))
        session.add(user)
        session.flush()
        user_id = user.id
        own = Application(user_id=user_id, email='recruiter@example.test', job_title='Delivery Engineer',
                          company='Visible Employer', status='sent', disposition='sent', ats_score=91)
        other = Application(user_id=user_id + 1000000, email='private@example.test',
                            job_title='Other Tenant Private Role', status='sent', disposition='sent', ats_score=20)
        session.add_all([own, other])
        session.commit()
    client = TestClient(main.app, raise_server_exceptions=False)
    client.cookies.set('careerpulse_auth', create_session_token(user_id, email))
    yield client, user_id, sync_calls
    client.close()


@pytest.mark.parametrize('account', [True, False], indirect=True)
def test_sent_page_renders_for_gmail_and_smtp_accounts(account):
    client, user_id, sync_calls = account
    response = client.get('/sent')
    assert response.status_code == 200, response.text
    assert 'Sent Applications' in response.text
    assert 'Delivery Engineer' in response.text and 'Visible Employer' in response.text
    assert 'Live Sent' in response.text and '>91<' in response.text
    assert 'Other Tenant Private Role' not in response.text
    with get_session() as session:
        connected = session.get(User, user_id).gmail_connected
    assert sync_calls == ([user_id] if connected else [])


@pytest.mark.parametrize('account', [True, False], indirect=True)
def test_activity_sync_endpoint_uses_the_gmail_service(account):
    client, user_id, sync_calls = account
    with get_session() as session:
        connected = session.get(User, user_id).gmail_connected
    response = client.post('/api/gmail/sync-activity')
    assert response.status_code == (200 if connected else 400), response.text
    if connected:
        assert response.json() == {'success': True, 'synced': 1, 'replies': 0, 'bounces': 0}
        assert sync_calls == [user_id]
    else:
        assert response.json()['error'] == 'Gmail is not connected.'
        assert not sync_calls


def test_sent_page_requires_authentication():
    with TestClient(main.app) as client:
        response = client.get('/sent', follow_redirects=False)
    assert response.status_code == 303
    assert response.headers['location'].startswith('/login')
