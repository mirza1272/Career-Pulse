"""Supabase Storage Integration for Storing and Serving Generated Resumes."""

from __future__ import annotations

import logging
import os
from pathlib import Path
import httpx

from app import config

logger = logging.getLogger("careerpulse.storage")

def _get_env_vars() -> tuple[str, str, str]:
    url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    bucket = os.environ.get("SUPABASE_RESUME_BUCKET", "resumes").strip()
    return url, key, bucket


def is_storage_configured() -> bool:
    """Check if Supabase Storage is configured."""
    url, key, bucket = _get_env_vars()
    return bool(url and key and bucket)


def upload_resume_to_supabase(
    pdf_path: Path | str,
    destination_filename: str | None = None,
    bucket: str | None = None,
    timeout_s: float = 15.0,
) -> str | None:
    """Upload a resume PDF to Supabase Storage bucket and return its public URL.

    Returns the public URL if successful, or None if skipped/failed.
    """
    if not is_storage_configured():
        logger.debug("Supabase Storage credentials not configured; skipping cloud resume upload.")
        return None

    url, key, default_bucket = _get_env_vars()
    path = Path(pdf_path)
    if not path.exists():
        logger.warning("PDF file not found for Supabase upload: %s", path)
        return None

    target_bucket = (bucket or default_bucket).strip()
    filename = destination_filename or path.name

    upload_url = f"{url}/storage/v1/object/{target_bucket}/{filename}"
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/pdf",
        "x-upsert": "true",  # Overwrite if exists
    }

    try:
        pdf_bytes = path.read_bytes()
        with httpx.Client(timeout=timeout_s) as client:
            res = client.post(upload_url, headers=headers, content=pdf_bytes)
            if res.status_code in (200, 201):
                public_url = f"{url}/storage/v1/object/public/{target_bucket}/{filename}"
                logger.info("Successfully uploaded resume to Supabase Storage: %s", public_url)
                return public_url
            else:
                logger.warning(
                    "Supabase Storage upload failed (status %s): %s",
                    res.status_code,
                    res.text[:160],
                )
    except Exception as exc:
        logger.warning("Error uploading resume to Supabase Storage: %s", exc)

    return None
