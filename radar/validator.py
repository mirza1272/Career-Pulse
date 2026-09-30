"""Apply-window and posting validity verification."""

from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
import re
import httpx

from radar.extractor import ExtractedJob

DEFAULT_CLOSED_PHRASES = [
    "position has been filled",
    "no longer accepting applications",
    "this job has expired",
    "applications are closed",
    "job listing is closed",
    "this opening is closed",
    "we are no longer hiring",
    "job is no longer available",
    "this position is closed",
    "this job post has expired",
    "posting has closed",
    "closed for applications",
    "job offer has expired",
    "this listing has expired",
    "the job you are looking for is no longer available",
    "this vacancy has closed",
    "this job posting has closed",
    "no longer available",
    "this role has been filled",
    "page not found",
    "404 not found",
    "sorry, this job is no longer active",
    "this posting is no longer available",
    "application closed",
    "this vacancy is now closed",
    "not currently accepting applications",
    "job has been removed",
    "this position has closed",
    "the requisition is closed",
]


@dataclass
class ValidationResult:
    valid: bool
    reason: str = ""


def verify_live_url(url: str, timeout_s: float = 6.0) -> tuple[bool, str]:
    """Test live posting URL to ensure it is reachable and not expired/closed.

    Returns (is_active, reason).
    """
    if not url or not (url.startswith("http://") or url.startswith("https://")):
        return False, "Invalid URL format"

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        with httpx.Client(timeout=timeout_s, follow_redirects=True, headers=headers) as client:
            res = client.get(url)
            # Explicit dead or missing status
            if res.status_code in (404, 410, 502, 503):
                return False, f"HTTP status {res.status_code} (Link dead or removed)"
            if res.status_code >= 400:
                # E.g. 403 or 401: Cloudflare/anti-bot protection; do not assume closed
                return True, "Reachable (protected by WAF/anti-bot)"

            # Check if redirected to an expired/closed endpoint
            final_url_str = str(res.url).lower()
            if any(term in final_url_str for term in ("/expired", "/closed", "/404", "/job-not-found", "/error")):
                return False, f"Redirected to closure page: {res.url}"

            # Scan HTML content for closure indicators
            page_text = res.text[:38000].lower()
            for phrase in DEFAULT_CLOSED_PHRASES:
                if phrase in page_text:
                    return False, f"Page content indicates closed position: '{phrase}'"

            return True, "URL is live and open"
    except httpx.TimeoutException:
        return True, "URL verification timed out; kept as active"
    except Exception as exc:
        return True, f"Verification skipped due to network ({exc})"


def validate_apply_window(
    job: ExtractedJob,
    closed_phrases: list[str] | None = None,
    min_desc_len: int = 20,
    now: dt.datetime | None = None,
) -> ValidationResult:
    """Validate that the posting is currently open and has a valid apply window."""
    current_time = now or dt.datetime.now(dt.timezone.utc)

    # 1. URL validity
    if not job.link or not (job.link.startswith("http://") or job.link.startswith("https://")):
        return ValidationResult(False, f"Invalid or missing apply link: {job.link}")

    # 2. Content length
    if not job.jd_text or len(job.jd_text.strip()) < min_desc_len:
        return ValidationResult(False, f"Job description too short ({len(job.jd_text)} chars, min {min_desc_len})")

    # 3. Explicit deadline in the past
    if job.deadline is not None:
        # Ensure timezone-aware comparison
        d_utc = job.deadline if job.deadline.tzinfo else job.deadline.replace(tzinfo=dt.timezone.utc)
        if d_utc < current_time:
            return ValidationResult(
                False,
                f"Application deadline has passed (deadline: {d_utc.isoformat()}, now: {current_time.isoformat()})",
            )

    # 4. Closed phrase heuristics in description
    phrases = closed_phrases or DEFAULT_CLOSED_PHRASES
    desc_lower = job.jd_text.lower()
    for phrase in phrases:
        if phrase in desc_lower:
            return ValidationResult(False, f"Posting contains closed phrase: '{phrase}'")

    return ValidationResult(True, "Posting is valid and apply window is open")
