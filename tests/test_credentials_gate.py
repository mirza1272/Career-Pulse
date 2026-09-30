"""Test Credentials Live Handshake & Mandatory Verification Gate."""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from fastapi.testclient import TestClient
from app import config
from app.auth import create_session_token, encrypt_credential
from app.db import get_session, init_db
from app.main import app
from app.models import User


def test_credentials_handshake_and_gate():
    init_db()
    with get_session() as s:
        # Create a fresh test user
        test_user = s.query(User).filter(User.email == "test_gate@example.com").first()
        if not test_user:
            test_user = User(
                email="test_gate@example.com",
                password_hash="dummy_hash",
                name="Gate Test User",
                smtp_verified=False,
            )
            s.add(test_user)
            s.commit()
            s.refresh(test_user)
        else:
            test_user.smtp_verified = False
            test_user.smtp_username = ""
            test_user.smtp_password_encrypted = ""
            s.commit()
        uid = test_user.id
        email = test_user.email

    token = create_session_token(uid, email)
    client = TestClient(app)
    client.cookies.set("careerpulse_auth", token)

    # 1. Unverified user accessing root workspace must be redirected to /credentials?setup_required=1
    r_gate = client.get("/", follow_redirects=False)
    assert r_gate.status_code == 303, f"Expected 303 redirect, got {r_gate.status_code}"
    assert "/credentials?setup_required=1" in r_gate.headers["location"]
    print("PASS unverified user blocked by credentials gate")

    # 2. Submitting invalid credentials where SMTP handshake fails must return 400 and NOT verify
    with patch("smtplib.SMTP") as mock_smtp:
        instance = mock_smtp.return_value.__enter__.return_value
        import smtplib
        instance.login.side_effect = smtplib.SMTPAuthenticationError(535, b"Authentication failed")

        r_bad = client.post(
            "/credentials",
            data={
                "sender_name": "Test User",
                "smtp_username": "bad_email@gmail.com",
                "smtp_password": "wrongpassword1234",
                "smtp_host": "smtp.gmail.com",
                "smtp_port": 587,
            },
            headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
        )
        assert r_bad.status_code == 400, f"Expected 400, got {r_bad.status_code}"
        bad_json = r_bad.json()
        assert not bad_json["success"]
        assert "Authentication Failed" in bad_json["message"]

    with get_session() as s:
        u = s.get(User, uid)
        assert not u.smtp_verified, "User should NOT be marked verified after failed handshake"
    print("PASS failed handshake rejected with 400 and keeps user unverified")

    # 3. Submitting valid credentials where SMTP handshake succeeds marks user verified
    with patch("smtplib.SMTP") as mock_smtp:
        instance = mock_smtp.return_value.__enter__.return_value
        instance.login.return_value = (235, b"2.7.0 Accepted")

        r_good = client.post(
            "/credentials",
            data={
                "sender_name": "Test User",
                "smtp_username": "valid_user@gmail.com",
                "smtp_password": "validapppassword",
                "smtp_host": "smtp.gmail.com",
                "smtp_port": 587,
            },
            headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
        )
        assert r_good.status_code == 200, f"Expected 200, got {r_good.status_code}"
        good_json = r_good.json()
        assert good_json["success"]

    with get_session() as s:
        u = s.get(User, uid)
        assert u.smtp_verified, "User must be marked verified after successful handshake"
        assert u.smtp_username == "valid_user@gmail.com"
    print("PASS successful handshake verified and saved credentials")

    # 4. Verified user can now access workspace
    r_open = client.get("/", follow_redirects=False)
    assert r_open.status_code == 200, f"Expected 200 for verified user, got {r_open.status_code}"
    print("PASS verified user permitted full access to workspace")

    print("ALL CREDENTIALS GATE TESTS PASSED")


if __name__ == "__main__":
    test_credentials_handshake_and_gate()
