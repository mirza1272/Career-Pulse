"""Radar configuration and shared database resolver."""

from __future__ import annotations

import os
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]
CAREERPULSE_ROOT = ROOT


def _load_dotenv() -> None:
    """Load environment variables from project .env."""
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_dotenv()


def database_url() -> str:
    """Shared DB URL. Supabase Postgres in prod; in-memory SQLite if no URL is specified."""
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        return url
    return "sqlite:///:memory:"


CAREERPULSE_API_URL = os.environ.get("CAREERPULSE_API_URL", "http://127.0.0.1:8770").rstrip("/")
EXPIRY_HOURS_DEFAULT = int(os.environ.get("EXPIRY_HOURS_DEFAULT", "48"))
EXPIRY_HOURS_POST_DEADLINE = int(os.environ.get("EXPIRY_HOURS_POST_DEADLINE", "24"))
BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY", "").strip()
SERPAPI_API_KEY = os.environ.get("SERPAPI_API_KEY", "").strip()
FIRECRAWL_API_KEY = os.environ.get("FIRECRAWL_API", os.environ.get("FIRECRAWL_API_KEY", "")).strip()
APIFY_API_KEY_1 = os.environ.get("APIFY_API_KEY_1", "").strip()
APIFY_API_KEY_2 = os.environ.get("APIFY_API_KEY_2", "").strip()
APIFY_API_KEY_3 = os.environ.get("APIFY_API_KEY_3", "").strip()
APIFY_API_KEYS = os.environ.get("APIFY_API_KEYS", "").strip()
APIFY_API_KEY = os.environ.get("APIFY_API_KEY", os.environ.get("APIFY_TOKEN", "")).strip()
MAX_JOB_AGE_DAYS = int(os.environ.get("MAX_JOB_AGE_DAYS", "3"))
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
LLM_API_KEY = os.environ.get("LLM_API_KEY", "").strip()
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.groq.com/openai/v1/chat/completions").strip()
LLM_MODEL = os.environ.get("GROQ_MODEL", os.environ.get("LLM_MODEL", "openai/gpt-oss-120b")).strip()


def load_search_targets() -> dict:
    """Load configured target roles and discovery sources."""
    cfg_file = ROOT / "config" / "search_targets.yaml"
    if cfg_file.exists():
        try:
            return yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}
        except Exception:
            pass
    return {
        "targets": [
            {"role": "Machine Learning Engineer", "keywords": ["machine learning", "pytorch", "deep learning"]},
            {"role": "Full-Stack Developer", "keywords": ["full stack", "react", "python", "fastapi"]},
        ]
    }
