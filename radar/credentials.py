"""Secure management and resolution for optional job search provider credentials.

Supports Apify, Tavily, Firecrawl, and SerpAPI credentials with AES-256 GCM encryption,
optional per-user storage, and server-side cooldown enforcement for free/limited mode.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from typing import Any

from app.auth import decrypt_credential, encrypt_credential
from app.db import get_session
from app.models import User
from radar import config

import threading

logger = logging.getLogger("radar.credentials")

COOLDOWN_SECONDS_LIMITED_MODE = 2 * 60 * 60  # 2 hours
MAX_JOBS_LIMITED_MODE = 3

_APIFY_KEY_LOCK = threading.Lock()
_apify_key_index = 0


def get_system_apify_keys() -> list[str]:
    """Return all configured system Apify API keys from environment."""
    keys: list[str] = []
    # 1. Numbered keys: APIFY_API_KEY_1, APIFY_API_KEY_2, APIFY_API_KEY_3
    for var in ("APIFY_API_KEY_1", "APIFY_API_KEY_2", "APIFY_API_KEY_3"):
        val = os.environ.get(var, "").strip()
        if val and val not in keys:
            keys.append(val)

    # 2. Comma-separated: APIFY_API_KEYS
    comma_keys = os.environ.get("APIFY_API_KEYS", "").strip()
    if comma_keys:
        for k in comma_keys.split(","):
            k_clean = k.strip()
            if k_clean and k_clean not in keys:
                keys.append(k_clean)

    # 3. Fallbacks: APIFY_API_KEY, APIFY_TOKEN
    for var in ("APIFY_API_KEY", "APIFY_TOKEN"):
        val = os.environ.get(var, "").strip()
        if val and val not in keys:
            keys.append(val)

    return keys


def get_next_system_apify_key() -> str:
    """Return next Apify API key using round-robin strategy across configured system pool."""
    global _apify_key_index
    pool = get_system_apify_keys()
    if not pool:
        return ""
    with _APIFY_KEY_LOCK:
        key = pool[_apify_key_index % len(pool)]
        _apify_key_index = (_apify_key_index + 1) % len(pool)
        return key


def reset_apify_key_rotation() -> None:
    """Reset round robin rotation counter (useful for unit tests)."""
    global _apify_key_index
    with _APIFY_KEY_LOCK:
        _apify_key_index = 0


def _mask_key(key: str) -> str:
    """Safely mask an API key for UI display (e.g. 'apify_api_1234...wxyz')."""
    if not key:
        return ""
    clean = key.strip()
    if len(clean) <= 8:
        return "••••••••"
    prefix = clean[:6]
    suffix = clean[-4:]
    return f"{prefix}••••••••{suffix}"


def get_user_custom_keys(user: User | None) -> dict[str, str]:
    """Retrieve only the custom keys explicitly provided by the user (excluding env fallbacks)."""
    user_keys: dict[str, str] = {
        "apify_api_key": "",
        "tavily_api_key": "",
        "firecrawl_api_key": "",
        "serpapi_api_key": "",
    }
    if not user:
        return user_keys

    # 1. Check dedicated attributes if present
    for field in ("apify_api_key_encrypted", "tavily_api_key_encrypted", "firecrawl_api_key_encrypted", "serpapi_api_key_encrypted"):
        val = getattr(user, field, None)
        if val:
            k = field.replace("_encrypted", "")
            dec = decrypt_credential(val)
            if dec:
                user_keys[k] = dec

    # 2. Check knowledge_base_json
    if user.knowledge_base_json and user.knowledge_base_json.strip() not in ("", "{}"):
        try:
            kb_data = json.loads(user.knowledge_base_json)
            p_creds = kb_data.get("provider_credentials", {})
            if isinstance(p_creds, dict):
                for k in ("apify_api_key", "tavily_api_key", "firecrawl_api_key", "serpapi_api_key"):
                    if not user_keys.get(k):
                        enc_val = p_creds.get(f"{k}_encrypted") or p_creds.get(k, "")
                        if enc_val:
                            dec = decrypt_credential(enc_val)
                            if dec:
                                user_keys[k] = dec
        except Exception as exc:
            logger.debug(f"Error reading user provider_credentials: {exc}")

    return user_keys


def get_user_provider_credentials(user: User | None) -> dict[str, str]:
    """Retrieve user credentials with round-robin fallback to system env variables for execution.

    Returns decrypted plaintext dict:
    {
        "apify_api_key": "...",
        "tavily_api_key": "...",
        "firecrawl_api_key": "...",
        "serpapi_api_key": "..."
    }
    """
    creds = get_user_custom_keys(user)

    # 2. If user did not provide custom Apify key, cycle to next available key in system round-robin pool
    if not creds["apify_api_key"]:
        creds["apify_api_key"] = get_next_system_apify_key()
    if not creds["tavily_api_key"]:
        creds["tavily_api_key"] = os.environ.get("TAVILY_API_KEY", "").strip()
    if not creds["firecrawl_api_key"]:
        creds["firecrawl_api_key"] = config.FIRECRAWL_API_KEY
    if not creds["serpapi_api_key"]:
        creds["serpapi_api_key"] = config.SERPAPI_API_KEY

    return creds


def get_masked_provider_credentials(user: User | None) -> dict[str, dict[str, Any]]:
    """Return safe metadata and masked keys for rendering the Credentials tab.

    Never exposes full API keys to the browser.
    Clearly distinguishes user-provided custom keys from system default fallbacks.
    """
    user_keys = get_user_custom_keys(user)
    system_apify_pool = get_system_apify_keys()
    has_user_apify = bool(user_keys.get("apify_api_key"))
    has_system_apify = bool(system_apify_pool)

    resolved = get_user_provider_credentials(user)

    if has_user_apify:
        apify_masked = _mask_key(user_keys["apify_api_key"])
        apify_status = "custom"
        apify_status_text = "Custom (Active)"
    elif has_system_apify:
        apify_masked = f"{len(system_apify_pool)} System Keys (Round Robin)"
        apify_status = "system_default"
        apify_status_text = "System Default"
    else:
        apify_masked = ""
        apify_status = "not_configured"
        apify_status_text = "Not Set"

    def _get_provider_info(field_name: str, display_name: str, desc: str, placeholder: str):
        u_val = user_keys.get(field_name, "")
        sys_val = resolved.get(field_name, "")
        is_user = bool(u_val)
        has_sys = bool(sys_val)

        if is_user:
            masked = _mask_key(u_val)
            status = "custom"
            status_text = "Custom (Active)"
        elif has_sys:
            masked = ""
            status = "system_default"
            status_text = "System Default"
        else:
            masked = ""
            status = "not_configured"
            status_text = "Not Set"

        return {
            "name": display_name,
            "field_name": field_name,
            "configured": is_user,
            "is_user_provided": is_user,
            "has_system_default": has_sys,
            "status": status,
            "status_text": status_text,
            "masked_key": masked if is_user else ("System Default (.env)" if has_sys else ""),
            "description": desc,
            "placeholder": placeholder,
        }

    providers = {
        "apify": {
            "name": "Apify MCP",
            "field_name": "apify_api_key",
            "configured": has_user_apify,
            "is_user_provided": has_user_apify,
            "has_system_default": has_system_apify,
            "status": apify_status,
            "status_text": apify_status_text,
            "masked_key": apify_masked,
            "system_pool_size": len(system_apify_pool),
            "description": "Used when Apify MCP is selected for job searching across global web sources and ATS boards. Optional users cycle through 3 system keys in round-robin sequence.",
            "placeholder": "apify_api_************************",
        },
        "tavily": _get_provider_info(
            "tavily_api_key",
            "Tavily Search API",
            "Used when Tavily is selected for real-time live job discovery across verified platforms.",
            "tvly-************************",
        ),
        "firecrawl": _get_provider_info(
            "firecrawl_api_key",
            "Firecrawl Web Crawler",
            "Used for deep job page scraping and clean markdown extraction in Radar.",
            "fc-************************",
        ),
        "serpapi": _get_provider_info(
            "serpapi_api_key",
            "SerpAPI Google Jobs",
            "Used for direct Google Jobs search indexing and official company portal resolution.",
            "************************",
        ),
    }

    return providers


def save_user_provider_credentials(user_id: int, new_keys: dict[str, str]) -> tuple[bool, str]:
    """Encrypt and persist user's optional provider API keys in knowledge_base_json and database."""
    with get_session() as session:
        u = session.get(User, user_id)
        if not u:
            return False, "User not found"

        kb_data: dict[str, Any] = {}
        if u.knowledge_base_json and u.knowledge_base_json.strip() not in ("", "{}"):
            try:
                kb_data = json.loads(u.knowledge_base_json)
            except Exception:
                kb_data = {}

        if "provider_credentials" not in kb_data or not isinstance(kb_data["provider_credentials"], dict):
            kb_data["provider_credentials"] = {}

        for k in ("apify_api_key", "tavily_api_key", "firecrawl_api_key", "serpapi_api_key"):
            if k in new_keys:
                val = new_keys[k].strip()
                if val:
                    # Encrypt key with AES-GCM
                    kb_data["provider_credentials"][f"{k}_encrypted"] = encrypt_credential(val)
                elif f"{k}_encrypted" in kb_data["provider_credentials"] and new_keys.get(f"clear_{k}"):
                    # User explicitly requested removal
                    del kb_data["provider_credentials"][f"{k}_encrypted"]

        u.knowledge_base_json = json.dumps(kb_data, ensure_ascii=False)
        u.updated_at = dt.datetime.now(dt.timezone.utc)
        session.commit()

        # Sync user knowledge base to Supabase if connected
        try:
            from app.db import sync_user_to_supabase
            sync_user_to_supabase(u)
        except Exception as exc:
            logger.debug(f"Supabase sync after provider credentials save: {exc}")

    return True, "Provider credentials updated and AES-256 encrypted successfully."


def check_limited_search_cooldown(user: User | None, provider: str = "tavily") -> tuple[bool, int, str]:
    """Check if the user can execute a search or if the 2-hour cooldown is active in limited mode.

    Returns:
        (can_search: bool, remaining_seconds: int, mode: "user_credential" | "limited_free")
    """
    user_keys = get_user_custom_keys(user)
    has_user_key = False

    if provider in ("apify_mcp", "apify"):
        has_user_key = bool(user_keys.get("apify_api_key"))
    elif provider in ("tavily", "all"):
        has_user_key = bool(user_keys.get("tavily_api_key"))

    # If user provided a valid custom API key for the selected provider, no cooldown is enforced
    if has_user_key:
        return True, 0, "user_credential"

    # In limited / free mode without user keys, check cooldown timestamp
    last_ts_str = ""
    if user and user.knowledge_base_json and user.knowledge_base_json.strip() not in ("", "{}"):
        try:
            kb_data = json.loads(user.knowledge_base_json)
            usage = kb_data.get("provider_usage", {})
            if isinstance(usage, dict):
                last_ts_str = str(usage.get("last_limited_search_at", ""))
        except Exception:
            pass

    if not last_ts_str:
        return True, 0, "limited_free"

    try:
        last_dt = dt.datetime.fromisoformat(last_ts_str)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=dt.timezone.utc)
        now_utc = dt.datetime.now(dt.timezone.utc)
        elapsed = (now_utc - last_dt).total_seconds()
        if elapsed < COOLDOWN_SECONDS_LIMITED_MODE:
            remaining = int(COOLDOWN_SECONDS_LIMITED_MODE - elapsed)
            return False, remaining, "limited_free"
    except Exception as exc:
        logger.debug(f"Error parsing last_limited_search_at: {exc}")

    return True, 0, "limited_free"


def record_limited_search(user: User | None, provider: str = "tavily") -> None:
    """Record timestamp when a free/limited mode search is performed to enforce 2-hour cooldown."""
    if not user:
        return

    # Check if user has key; if they have user key, we do not need to trigger limited cooldown
    user_keys = get_user_custom_keys(user)
    if provider in ("apify_mcp", "apify") and user_keys.get("apify_api_key"):
        return
    if provider in ("tavily", "all") and user_keys.get("tavily_api_key"):
        return

    with get_session() as session:
        u = session.get(User, user.id)
        if not u:
            return
        kb_data: dict[str, Any] = {}
        if u.knowledge_base_json and u.knowledge_base_json.strip() not in ("", "{}"):
            try:
                kb_data = json.loads(u.knowledge_base_json)
            except Exception:
                kb_data = {}

        if "provider_usage" not in kb_data or not isinstance(kb_data["provider_usage"], dict):
            kb_data["provider_usage"] = {}

        kb_data["provider_usage"]["last_limited_search_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        u.knowledge_base_json = json.dumps(kb_data, ensure_ascii=False)
        session.commit()
