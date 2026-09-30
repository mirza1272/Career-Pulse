"""Career Pulse Authentication & Multi-Tenant Session Management.

Provides secure PBKDF2 password hashing, Fernet AES-256 symmetric credential encryption,
HMAC-SHA256 signed session tokens, and database-backed user authentication.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import logging
import time
from fastapi import Request
from sqlalchemy import select
try:
    from Cryptodome.Cipher import AES
    from Cryptodome.Random import get_random_bytes
except ImportError:
    from Crypto.Cipher import AES
    from Crypto.Random import get_random_bytes

from app import config
from app.models import User

logger = logging.getLogger("careerpulse.auth")


def _get_aes_key() -> bytes:
    """Derive a deterministic 32-byte key from SESSION_SECRET."""
    secret_bytes = config.SESSION_SECRET.encode("utf-8")
    return hashlib.sha256(secret_bytes).digest()


def is_admin_email(email: str) -> bool:
    """Check if the provided email matches the configured system administrator."""
    if not email:
        return False
    return email.strip().lower() == config.ADMIN_EMAIL.strip().lower()


def hash_password(password: str) -> str:
    """Hash a password using PBKDF2-HMAC-SHA256 with 200,000 iterations and a random 16-byte salt."""
    salt = secrets.token_hex(16)
    iterations = 200_000
    derived = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        iterations,
    )
    return f"pbkdf2:sha256:{iterations}${salt}${derived.hex()}"


def verify_password(password: str, hashed: str) -> bool:
    """Verify a plain password against the stored PBKDF2 hash (or fallback to legacy plain text)."""
    if not hashed:
        return False

    if hashed.startswith("pbkdf2:sha256:"):
        try:
            algo_spec, salt, target_hash = hashed.split("$")
            _, _, iterations_str = algo_spec.split(":")
            iterations = int(iterations_str)
            computed = hashlib.pbkdf2_hmac(
                "sha256",
                password.encode("utf-8"),
                salt.encode("utf-8"),
                iterations,
            )
            return hmac.compare_digest(computed.hex(), target_hash)
        except Exception:
            return False

    # Legacy plain-text fallback (auto-upgraded on next login/change)
    return hmac.compare_digest(password.strip(), hashed.strip())


def encrypt_credential(text: str) -> str:
    """Encrypt sensitive string (such as SMTP app password) using AES-GCM."""
    if not text:
        return ""
    try:
        key = _get_aes_key()
        cipher = AES.new(key, AES.MODE_GCM)
        ciphertext, tag = cipher.encrypt_and_digest(text.strip().encode("utf-8"))
        # Format: base64(nonce + tag + ciphertext)
        payload = cipher.nonce + tag + ciphertext
        return base64.urlsafe_b64encode(payload).decode("utf-8")
    except Exception:
        return text


def decrypt_credential(cipher_text: str) -> str:
    """Decrypt AES-GCM ciphertext; returns raw text if already plaintext or decryption fails."""
    if not cipher_text:
        return ""
    try:
        payload = base64.urlsafe_b64decode(cipher_text.strip().encode("utf-8"))
        if len(payload) < 28: # 16 byte tag + 12 byte nonce + at least some ciphertext
            return cipher_text
        nonce = payload[:16] # Cryptodome default nonce length is 16 bytes for GCM
        tag = payload[16:32]
        ciphertext = payload[32:]
        
        key = _get_aes_key()
        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        return cipher.decrypt_and_verify(ciphertext, tag).decode("utf-8")
    except Exception:
        # Fallback if stored as legacy plaintext
        return cipher_text.strip()


def create_session_token(user_id: int, email: str) -> str:
    """Generate an HMAC-SHA256 signed session token for a user valid for 7 days."""
    timestamp = str(int(time.time()))
    payload = f"{user_id}:{email.strip().lower()}:{timestamp}"
    signature = hmac.new(
        config.SESSION_SECRET.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload}:{signature}"


def verify_session_token(token: str) -> tuple[int, str] | None:
    """Validate token integrity, expiration, and return (user_id, email) or None."""
    if not token or ":" not in token:
        return None
    parts = token.split(":")
    if len(parts) != 4:
        # Legacy 3-part token check (email:timestamp:sig)
        if len(parts) == 3:
            email, timestamp_str, signature = parts
            expected_sig = hmac.new(
                config.SESSION_SECRET.encode("utf-8"),
                f"{email}:{timestamp_str}".encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            if hmac.compare_digest(signature, expected_sig):
                return (1, email.strip().lower())
        return None

    user_id_str, email, timestamp_str, signature = parts

    try:
        ts = int(timestamp_str)
        user_id = int(user_id_str)
        now = time.time()
        # 7-day expiration window + 5-minute clock drift tolerance
        if now - ts > 7 * 86400 or now < ts - 300:
            return None
    except ValueError:
        return None

    expected_payload = f"{user_id}:{email.strip().lower()}:{timestamp_str}"
    expected_sig = hmac.new(
        config.SESSION_SECRET.encode("utf-8"),
        expected_payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if hmac.compare_digest(signature, expected_sig):
        return (user_id, email.strip().lower())
    return None


def get_current_user(request: Request) -> User | None:
    """Extract and authenticate current user from the session cookie."""
    token = request.cookies.get("careerpulse_auth", "")
    verified = verify_session_token(token)
    if not verified:
        return None

    user_id, email = verified
    from app.db import get_session

    try:
        with get_session() as session:
            user = session.scalar(
                select(User).where(User.id == user_id, User.is_active.is_(True))
            )
            if not user:
                # Fallback by email
                user = session.scalar(
                    select(User).where(User.email == email, User.is_active.is_(True))
                )
            
            # If user in memory has empty KB, check Supabase to hydrate it on demand
            if user and (not user.knowledge_base_json or user.knowledge_base_json.strip() in ("", "{}")):
                try:
                    from radar.supabase_client import SupabaseClient
                    sb = SupabaseClient()
                    if sb.is_configured:
                        cloud_u = sb.get_user_by_id(user.id) or sb.get_user_by_email(user.email)
                        if cloud_u and cloud_u.get("knowledge_base_json") and cloud_u.get("knowledge_base_json").strip() not in ("", "{}"):
                            user.knowledge_base_json = cloud_u.get("knowledge_base_json")
                            session.commit()
                except Exception:
                    pass

            return user
    except Exception:
        return None


def authenticate_user(email: str, password: str) -> User | None:
    """Authenticate user credentials against the database, with on-demand Supabase cloud sync."""
    from app.db import get_session

    target_email = email.strip().lower()
    try:
        # Check cloud to ensure freshest user record and KB data is synced into working memory
        try:
            from radar.supabase_client import SupabaseClient
            sb = SupabaseClient()
            if sb.is_configured:
                cloud_u = sb.get_user_by_email(target_email)
                if cloud_u:
                    with get_session() as session:
                        u_row = session.scalar(select(User).where(User.email == target_email))
                        if not u_row:
                            u_row = User(
                                id=cloud_u.get("id"),
                                email=target_email,
                                password_hash=cloud_u.get("password_hash") or "",
                                name=cloud_u.get("name") or target_email.split("@")[0],
                                is_active=bool(cloud_u.get("is_active", True)),
                                smtp_host=cloud_u.get("smtp_host") or "smtp.gmail.com",
                                smtp_port=int(cloud_u.get("smtp_port") or 587),
                                smtp_username=cloud_u.get("smtp_username") or "",
                                smtp_password_encrypted=cloud_u.get("smtp_password_encrypted") or "",
                                sender_name=cloud_u.get("sender_name") or "",
                                smtp_verified=bool(cloud_u.get("smtp_verified", False)),
                                knowledge_base_json=cloud_u.get("knowledge_base_json") or "{}",
                            )
                            session.add(u_row)
                        else:
                            if cloud_u.get("password_hash"):
                                u_row.password_hash = cloud_u.get("password_hash")
                            if cloud_u.get("name"):
                                u_row.name = cloud_u.get("name")
                            if cloud_u.get("knowledge_base_json") and cloud_u.get("knowledge_base_json").strip() not in ("", "{}"):
                                u_row.knowledge_base_json = cloud_u.get("knowledge_base_json")
                            if cloud_u.get("smtp_username"):
                                u_row.smtp_username = cloud_u.get("smtp_username")
                                u_row.smtp_password_encrypted = cloud_u.get("smtp_password_encrypted") or ""
                                u_row.smtp_verified = bool(cloud_u.get("smtp_verified", False))
                        session.commit()
        except Exception:
            pass

        with get_session() as session:
            user = session.scalar(
                select(User).where(User.email == target_email, User.is_active.is_(True))
            )
            if not user:
                return None

            if verify_password(password, user.password_hash):
                # Auto-upgrade to PBKDF2 if was legacy plain text
                if not user.password_hash.startswith("pbkdf2:sha256:"):
                    user.password_hash = hash_password(password)
                    session.commit()
                    try:
                        from radar.supabase_client import SupabaseClient
                        sb = SupabaseClient()
                        if sb.is_configured:
                            sb.upsert_user({"id": user.id, "password_hash": user.password_hash})
                    except Exception:
                        pass
                return user
    except Exception as exc:
        logger.warning(f"authenticate_user exception: {exc}")
        return None

    return None


def is_authenticated(request: Request) -> bool:
    """Check if the incoming request has a valid authenticated user."""
    return get_current_user(request) is not None


def change_user_password(user_id: int, old_password: str, new_password: str) -> tuple[bool, str]:
    """Change a user's password after verifying the old password."""
    if len(new_password) < 6:
        return False, "New password must be at least 6 characters."

    from app.db import get_session

    try:
        with get_session() as session:
            user = session.scalar(select(User).where(User.id == user_id))
            if not user:
                return False, "User not found."

            if not verify_password(old_password, user.password_hash):
                return False, "Current password does not match."

            user.password_hash = hash_password(new_password)
            session.commit()
            try:
                from radar.supabase_client import SupabaseClient
                sb = SupabaseClient()
                if sb.is_configured:
                    sb.upsert_user({"id": user.id, "password_hash": user.password_hash})
            except Exception:
                pass
            return True, "Password updated successfully."
    except Exception as exc:
        return False, f"Failed to update password: {exc}"
