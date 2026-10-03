"""Gmail Integration Service for CareerPulse.

Implements Google OAuth 2.0 flow, AES-256 encrypted credential storage,
automatic token refresh, RFC 2822 message dispatch via Gmail REST API,
and thread activity/reply telemetry without requiring Google passwords.
"""

from __future__ import annotations

import base64
import datetime as dt
from email.message import EmailMessage
import hashlib
import hmac
import logging
import time
from urllib.parse import urlencode

import httpx
from sqlalchemy.orm import Session

from app import config
from app.auth import decrypt_credential, encrypt_credential
from app.models import User

logger = logging.getLogger("careerpulse.gmail")

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"

# Scopes requested:
# - gmail.send: dispatch application emails & tailored resumes
# - gmail.readonly: check thread replies and delivery bounce notifications
# - userinfo.email: retrieve verified Gmail address
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/userinfo.email",
]


def create_oauth_state(user_id: int) -> str:
    """Generate an HMAC-SHA256 signed OAuth state token containing user_id and timestamp."""
    timestamp = str(int(time.time()))
    payload = f"{user_id}:{timestamp}"
    signature = hmac.new(
        config.SESSION_SECRET.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload}:{signature}"


def verify_oauth_state(state: str, expected_user_id: int) -> bool:
    """Validate OAuth state parameter against CSRF and check 15-minute expiration."""
    if not state or ":" not in state:
        return False
    parts = state.split(":")
    if len(parts) != 3:
        return False

    user_id_str, timestamp_str, signature = parts
    try:
        user_id = int(user_id_str)
        timestamp = int(timestamp_str)
        if user_id != expected_user_id:
            return False
        # 15-minute TTL + 60s clock drift
        now = time.time()
        if now - timestamp > 900 or now < timestamp - 60:
            return False
    except ValueError:
        return False

    expected_payload = f"{user_id}:{timestamp_str}"
    expected_sig = hmac.new(
        config.SESSION_SECRET.encode("utf-8"),
        expected_payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(signature, expected_sig)


def build_google_auth_url(user_id: int) -> str:
    """Generate the Google OAuth consent URL with offline access and signed state."""
    config.ensure_fresh_config()
    state = create_oauth_state(user_id)
    params = {
        "client_id": config.GOOGLE_CLIENT_ID,
        "redirect_uri": config.GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(GMAIL_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


def exchange_code_for_tokens(code: str) -> dict:
    """Exchange authorization code for access and refresh tokens."""
    config.ensure_fresh_config()
    payload = {
        "code": code,
        "client_id": config.GOOGLE_CLIENT_ID,
        "client_secret": config.GOOGLE_CLIENT_SECRET,
        "redirect_uri": config.GOOGLE_REDIRECT_URI,
        "grant_type": "authorization_code",
    }
    with httpx.Client(timeout=20.0) as client:
        resp = client.post(GOOGLE_TOKEN_URL, data=payload)
        if resp.status_code != 200:
            logger.error("Failed to exchange OAuth code for tokens: status=%s body=%s", resp.status_code, resp.text)
            resp.raise_for_status()
        return resp.json()


def fetch_google_userinfo(access_token: str) -> dict:
    """Fetch profile information (email) for the connected Google account."""
    headers = {"Authorization": f"Bearer {access_token}"}
    with httpx.Client(timeout=15.0) as client:
        resp = client.get(GOOGLE_USERINFO_URL, headers=headers)
        if resp.status_code != 200:
            logger.error("Failed to fetch Google userinfo: status=%s", resp.status_code)
            resp.raise_for_status()
        return resp.json()


def refresh_gmail_access_token(user: User, session: Session) -> str | None:
    """Retrieve a valid access token, automatically refreshing if expired or expiring soon."""
    config.ensure_fresh_config()
    if not user.gmail_connected or not user.gmail_refresh_token_encrypted:
        return None

    now = dt.datetime.now(dt.timezone.utc)
    # Check if existing access token is still valid (with 2 min buffer)
    if (
        user.gmail_access_token_encrypted
        and user.gmail_token_expires_at
        and user.gmail_token_expires_at.replace(tzinfo=dt.timezone.utc if user.gmail_token_expires_at.tzinfo is None else user.gmail_token_expires_at.tzinfo) > (now + dt.timedelta(minutes=2))
    ):
        decrypted = decrypt_credential(user.gmail_access_token_encrypted)
        if decrypted:
            return decrypted

    # Token is expired or expiring soon -> Refresh using refresh_token
    refresh_token = decrypt_credential(user.gmail_refresh_token_encrypted)
    if not refresh_token:
        logger.warning("No refresh token available for user %s", user.id)
        return None

    payload = {
        "client_id": config.GOOGLE_CLIENT_ID,
        "client_secret": config.GOOGLE_CLIENT_SECRET,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }

    try:
        with httpx.Client(timeout=20.0) as client:
            resp = client.post(GOOGLE_TOKEN_URL, data=payload)
            if resp.status_code == 200:
                data = resp.json()
                new_access_token = data.get("access_token")
                expires_in = int(data.get("expires_in", 3600))
                
                user.gmail_access_token_encrypted = encrypt_credential(new_access_token)
                user.gmail_token_expires_at = now + dt.timedelta(seconds=expires_in)
                session.commit()
                return new_access_token
            elif resp.status_code in (400, 401):
                err_data = resp.json() if "application/json" in resp.headers.get("content-type", "") else {}
                err = err_data.get("error", "")
                if err in ("invalid_grant", "unauthorized_client"):
                    logger.warning("Gmail refresh token revoked or expired for user %s: %s", user.id, err)
                    user.gmail_connected = False
                    user.gmail_access_token_encrypted = ""
                    user.gmail_refresh_token_encrypted = ""
                    session.commit()
                return None
            else:
                logger.error("Gmail token refresh unexpected HTTP %s: %s", resp.status_code, resp.text)
                return None
    except Exception as exc:
        logger.error("Exception during Gmail token refresh for user %s: %s", user.id, exc)
        return None


def send_gmail_message(user: User, email_msg: EmailMessage, session: Session) -> dict:
    """Send an RFC 2822 EmailMessage via Gmail REST API.

    Returns dict:
      {"message_id": str, "thread_id": str, "success": bool, "error": str}
    """
    token = refresh_gmail_access_token(user, session)
    if not token:
        return {
            "success": False,
            "error": "Gmail connection expired or authorization revoked. Please reconnect.",
            "message_id": "",
            "thread_id": "",
        }

    raw_bytes = email_msg.as_bytes()
    raw_b64 = base64.urlsafe_b64encode(raw_bytes).decode("utf-8")

    send_payload = {"raw": raw_b64}
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                f"{GMAIL_API_BASE}/messages/send",
                headers=headers,
                json=send_payload,
            )
            if resp.status_code == 200:
                res_data = resp.json()
                msg_id = res_data.get("id", "")
                thread_id = res_data.get("threadId", "")
                return {
                    "success": True,
                    "message_id": msg_id,
                    "thread_id": thread_id,
                    "error": "",
                }
            else:
                err_text = resp.text
                logger.error("Gmail API send failed: status=%s response=%s", resp.status_code, err_text)
                return {
                    "success": False,
                    "error": f"Gmail API error ({resp.status_code}): {err_text}",
                    "message_id": "",
                    "thread_id": "",
                }
    except Exception as exc:
        logger.error("Gmail API network or dispatch error: %s", exc)
        return {
            "success": False,
            "error": f"Gmail dispatch error: {exc}",
            "message_id": "",
            "thread_id": "",
        }


def check_gmail_thread_activity(
    user: User,
    thread_id: str,
    sent_message_id: str,
    session: Session,
) -> dict:
    """Inspect thread messages to detect replies and bounce notifications.

    Returns:
      {
        "has_reply": bool,
        "reply_from": str,
        "reply_snippet": str,
        "is_bounce": bool,
        "bounce_reason": str,
      }
    """
    result = {
        "has_reply": False,
        "reply_from": "",
        "reply_snippet": "",
        "is_bounce": False,
        "bounce_reason": "",
    }
    if not thread_id or not user.gmail_connected:
        return result

    token = refresh_gmail_access_token(user, session)
    if not token:
        return result

    headers = {"Authorization": f"Bearer {token}"}
    try:
        with httpx.Client(timeout=20.0) as client:
            resp = client.get(
                f"{GMAIL_API_BASE}/threads/{thread_id}?format=metadata&metadataHeaders=From&metadataHeaders=Subject&metadataHeaders=Date",
                headers=headers,
            )
            if resp.status_code != 200:
                logger.debug("Gmail thread check returned status %s for thread %s", resp.status_code, thread_id)
                return result

            thread_data = resp.json()
            messages = thread_data.get("messages", [])
            if len(messages) <= 1:
                return result

            user_email_lower = (user.gmail_email or user.email).strip().lower()

            for msg in messages:
                msg_id = msg.get("id")
                # Skip the original sent message
                if msg_id == sent_message_id:
                    continue

                snippet = msg.get("snippet", "")
                payload = msg.get("payload", {})
                headers_list = payload.get("headers", [])

                from_val = ""
                for h in headers_list:
                    if h.get("name", "").lower() == "from":
                        from_val = h.get("value", "")
                        break

                from_val_lower = from_val.lower()

                # Check for Delivery Status Notification (DSN) / Mailer Daemon bounce
                if any(
                    kw in from_val_lower or kw in snippet.lower()
                    for kw in ("mailer-daemon", "mail delivery subsystem", "postmaster", "delivery status notification", "address not found", "undelivered mail")
                ):
                    result["is_bounce"] = True
                    result["bounce_reason"] = snippet[:250] if snippet else "Delivery failed or address not found"
                    continue

                # Check if message is from an external recipient (genuine reply)
                if from_val and user_email_lower not in from_val_lower:
                    result["has_reply"] = True
                    result["reply_from"] = from_val[:320]
                    result["reply_snippet"] = snippet[:300] if snippet else ""

            return result
    except Exception as exc:
        logger.warning("Error checking Gmail thread %s activity: %s", thread_id, exc)
        return result


def disconnect_gmail_account(user: User, session: Session) -> bool:
    """Revoke Google OAuth token and clear stored credentials in database."""
    refresh_token = ""
    if user.gmail_refresh_token_encrypted:
        refresh_token = decrypt_credential(user.gmail_refresh_token_encrypted)

    if refresh_token:
        try:
            with httpx.Client(timeout=10.0) as client:
                client.post(GOOGLE_REVOKE_URL, params={"token": refresh_token})
        except Exception as exc:
            logger.debug("Google token revocation request note: %s", exc)

    user.gmail_connected = False
    user.gmail_email = ""
    user.gmail_access_token_encrypted = ""
    user.gmail_refresh_token_encrypted = ""
    user.gmail_token_expires_at = None
    user.gmail_token_scopes = ""
    user.gmail_connected_at = None
    session.commit()
    return True


def sync_user_gmail_threads(user: User, session: Session) -> dict:
    """Scan and synchronize all active Gmail threads for replies and bounces."""
    import json
    from app.models import Application, EmailActivity

    if not user or not user.gmail_connected:
        return {"synced": 0, "replies": 0, "bounces": 0}

    # Find active sent applications with Gmail thread IDs
    apps = (
        session.query(Application)
        .filter(
            Application.user_id == user.id,
            Application.status == "sent",
            Application.gmail_thread_id != "",
            Application.replied_at.is_(None),
            Application.bounced_at.is_(None),
        )
        .all()
    )

    synced_count = 0
    replies_count = 0
    bounces_count = 0

    now_utc = dt.datetime.now(dt.timezone.utc)

    for app_row in apps:
        activity = check_gmail_thread_activity(
            user=user,
            thread_id=app_row.gmail_thread_id,
            sent_message_id=app_row.gmail_message_id,
            session=session,
        )
        synced_count += 1

        if activity.get("has_reply"):
            app_row.status = "replied"
            app_row.replied_at = now_utc
            app_row.reply_snippet = activity.get("reply_snippet", "")
            app_row.followup_due_at = None  # Cease follow-ups when replied
            replies_count += 1

            act = EmailActivity(
                user_id=user.id,
                application_id=app_row.id,
                event_type="replied",
                recipient=activity.get("reply_from", app_row.email),
                subject=app_row.subject,
                details_json=json.dumps({"snippet": app_row.reply_snippet}),
            )
            session.add(act)

        elif activity.get("is_bounce"):
            app_row.status = "bounced"
            app_row.bounced_at = now_utc
            app_row.bounce_reason = activity.get("bounce_reason", "")
            app_row.followup_due_at = None  # Cease follow-ups when bounced
            bounces_count += 1

            act = EmailActivity(
                user_id=user.id,
                application_id=app_row.id,
                event_type="bounced",
                recipient=app_row.email,
                subject=app_row.subject,
                details_json=json.dumps({"reason": app_row.bounce_reason}),
            )
            session.add(act)

        if activity.get("has_reply") or activity.get("is_bounce"):
            session.commit()
            try:
                from app.main import sync_app_to_supabase
                sync_app_to_supabase(app_row, session)
            except Exception:
                pass

    return {"synced": synced_count, "replies": replies_count, "bounces": bounces_count}

