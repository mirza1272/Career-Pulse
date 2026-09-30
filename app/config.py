"""Career Pulse configuration — environment only, no framework.

In production set DATABASE_URL to your Supabase Postgres connection string;
for local development it falls back to in-memory SQLite so the app runs with
zero setup.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Module-level variable definitions with defaults
is_serverless: bool = bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))
SMTP_HOST: str = "smtp.gmail.com"
SMTP_PORT: int = 587
SMTP_USERNAME: str = ""
SMTP_PASSWORD: str = ""
TEST_MODE: bool = True
TEST_RECIPIENT: str = ""
ALLOW_REAL_EMAIL: bool = False
OUTBOX_DIR: Path = Path("/tmp/outbox") if is_serverless else (ROOT / "data/outbox")
LLM_API_KEY: str = ""
# Three-key rotation for Groq rate-limit resilience. LLM_API_KEY is kept as a
# legacy fallback and is tried last. Never log or expose key values.
GROQ_API_KEY_1: str = ""
GROQ_API_KEY_2: str = ""
GROQ_API_KEY_3: str = ""
LLM_MODEL: str = "openai/gpt-oss-120b"
FAST_LLM_MODEL: str = "openai/gpt-oss-20b"
LLM_BASE_URL: str = "https://api.groq.com/openai/v1/chat/completions"
ADMIN_EMAIL: str = "mirzahaseeb0566@gmail.com"
AUTH_EMAIL: str = "mirzahaseeb0566@gmail.com"
# No default password: the initial admin account is only seeded when
# AUTH_PASSWORD is explicitly set (see app/db.py seed_admin_user).
AUTH_PASSWORD: str = ""
SESSION_SECRET: str = ""
# No default OCR key: set OCR_SPACE_API_KEY in the environment if needed.
OCR_SPACE_API_KEY: str = ""


_env_last_mtime: float = 0.0


def _parse_bool(val: str | bool | None, default: bool = False) -> bool:
    """Safely parse boolean representations like '0', '1', 'true', 'false', 'yes', 'no'."""
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    s = str(val).strip().lower()
    if s in ("1", "true", "yes", "on", "t"):
        return True
    if s in ("0", "false", "no", "off", "f"):
        return False
    return default


def _resolve_session_secret(serverless: bool) -> str:
    """Return SESSION_SECRET, failing hard when it is missing in production.

    A missing secret in a serverless/prod environment is a hard startup error:
    sessions would otherwise be signed with a publicly known key.
    """
    val = os.environ.get("SESSION_SECRET", "").strip()
    if val:
        return val
    if serverless:
        raise RuntimeError(
            "SESSION_SECRET is not set. Set it in Vercel -> Project -> Settings -> "
            "Environment Variables (Production) and redeploy."
        )
    # ponytail: local dev only — a fixed dev secret is acceptable because the
    # local database is throwaway in-memory SQLite. Never use this in production.
    import warnings

    warnings.warn(
        "SESSION_SECRET not set; using insecure dev-only fallback. "
        "Set SESSION_SECRET for any real deployment."
    )
    return "dev-only-insecure-session-secret"


def _load_dotenv() -> None:
    """Minimal .env loader (no dependency). Ignores comments and blanks."""
    global _env_last_mtime
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    try:
        _env_last_mtime = env_path.stat().st_mtime
    except OSError:
        pass
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ[key] = value


def database_url() -> str:
    """Shared DB URL. Supabase Postgres if configured; in-memory SQLite for ephemeral working engine."""
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        return url
    return "sqlite:///:memory:"


def ensure_fresh_config() -> None:
    """Check if .env file has been modified on disk and reload if needed."""
    env_path = ROOT / ".env"
    if env_path.exists():
        try:
            mtime = env_path.stat().st_mtime
            if mtime != _env_last_mtime:
                reload_config()
        except OSError:
            pass


def reload_config() -> None:
    """Reload .env into os.environ and refresh module-level constants."""
    global SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, TEST_MODE, TEST_RECIPIENT, ALLOW_REAL_EMAIL
    global OUTBOX_DIR, LLM_API_KEY, GROQ_API_KEY_1, GROQ_API_KEY_2, GROQ_API_KEY_3
    global LLM_MODEL, FAST_LLM_MODEL, LLM_BASE_URL, is_serverless
    global ADMIN_EMAIL, AUTH_EMAIL, AUTH_PASSWORD, SESSION_SECRET, OCR_SPACE_API_KEY

    _load_dotenv()

    SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
    SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "").strip()

    # Normalize Google app password by removing any spaces
    raw_pwd = os.environ.get("SMTP_PASSWORD", "").strip()
    SMTP_PASSWORD = raw_pwd.replace(" ", "") if "gmail" in SMTP_HOST.lower() else raw_pwd

    # Flexible test mode parsing: checks CAREERPULSE_TEST_MODE first, fallback to TEST_MODE
    raw_test_mode = os.environ.get("CAREERPULSE_TEST_MODE")
    if raw_test_mode is None:
        raw_test_mode = os.environ.get("TEST_MODE", "1")
    TEST_MODE = _parse_bool(raw_test_mode, default=True)

    # Flexible allow real email parsing: checks CAREERPULSE_ALLOW_REAL_EMAIL first, fallback to ALLOW_REAL_EMAIL
    raw_allow_real = os.environ.get("CAREERPULSE_ALLOW_REAL_EMAIL")
    if raw_allow_real is None:
        raw_allow_real = os.environ.get("ALLOW_REAL_EMAIL", "0")
    ALLOW_REAL_EMAIL = _parse_bool(raw_allow_real, default=False)

    TEST_RECIPIENT = (os.environ.get("JOBHUNTER_TEST_RECIPIENT") or os.environ.get("TEST_RECIPIENT") or "").strip()

    is_serverless = bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))
    OUTBOX_DIR = Path("/tmp/outbox") if is_serverless else (ROOT / "data/outbox")

    GROQ_API_KEY_1 = os.environ.get("GROQ_API_KEY_1", "").strip()
    GROQ_API_KEY_2 = os.environ.get("GROQ_API_KEY_2", "").strip()
    GROQ_API_KEY_3 = os.environ.get("GROQ_API_KEY_3", "").strip()
    LLM_API_KEY = os.environ.get("LLM_API_KEY", "").strip() or GROQ_API_KEY_1 or GROQ_API_KEY_2 or GROQ_API_KEY_3
    LLM_MODEL = os.environ.get("LLM_MODEL", "openai/gpt-oss-120b")
    FAST_LLM_MODEL = os.environ.get("FAST_LLM_MODEL", "openai/gpt-oss-20b")
    LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.groq.com/openai/v1/chat/completions")

    AUTH_EMAIL = os.environ.get("AUTH_EMAIL", os.environ.get("EMAIL", "mirzahaseeb0566@gmail.com")).strip()
    ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", AUTH_EMAIL).strip()
    # No default password: the initial admin account is only seeded when
    # AUTH_PASSWORD is explicitly set (see app/db.py seed_admin_user).
    AUTH_PASSWORD = os.environ.get("AUTH_PASSWORD", os.environ.get("PASSWORD", "")).strip()
    SESSION_SECRET = _resolve_session_secret(is_serverless)
    # OCR.Space API key: no hardcoded default. If unset, OCR falls back to the
    # local RapidOCR engine (when installed) and otherwise asks the user to
    # paste text — screenshots are never sent anywhere without a key.
    OCR_SPACE_API_KEY = os.environ.get("OCR_SPACE_API_KEY", "")


def is_test_mode() -> bool:
    ensure_fresh_config()
    return TEST_MODE


def is_allow_real_email() -> bool:
    ensure_fresh_config()
    return ALLOW_REAL_EMAIL


def get_test_recipient() -> str:
    ensure_fresh_config()
    return TEST_RECIPIENT


# Load initially on import
reload_config()
