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


def validate_job_freshness(
    job: ExtractedJob,
    now: dt.datetime | None = None,
    max_posting_age_days: int = 21,
) -> ValidationResult:
    """Validate strict job freshness according to user rules:
    1. If deadline is mentioned: deadline must be strictly in the future (>= today).
    2. If no deadline is mentioned: posting date must be <= 3 weeks old (<= 21 days).
    3. If neither deadline nor posting date is mentioned/detected: REJECT (avoid unverified stale postings).
    """
    current_time = now or dt.datetime.now(dt.timezone.utc)

    # 0. Check URL slug for previous years (e.g. /2024/, /2023/)
    if job.link:
        if re.search(r"\b202[0-4]\d{4}\b", job.link) or re.search(r"/(?:202[0-4])[-/]", job.link):
            return ValidationResult(False, f"URL slug indicates vacancy is from a previous year: {job.link}")

    # 1. Deadline check
    deadline = job.deadline
    if deadline is None and job.jd_text:
        from radar.extractor import parse_deadline_from_text
        deadline = parse_deadline_from_text(job.jd_text)
        if deadline:
            job.deadline = deadline

    if deadline is not None:
        d_utc = deadline if deadline.tzinfo else deadline.replace(tzinfo=dt.timezone.utc)
        if d_utc < current_time:
            return ValidationResult(
                False,
                f"Application deadline has passed (deadline: {d_utc.strftime('%Y-%m-%d')}, now: {current_time.strftime('%Y-%m-%d')})",
            )
        return ValidationResult(True, f"Application deadline is valid and active ({d_utc.strftime('%Y-%m-%d')})")

    # 2. Posting Date check (if no deadline)
    pub_dt = job.published_at
    if pub_dt is None and job.posted_at:
        from radar.extractor import parse_datetime
        pub_dt = parse_datetime(job.posted_at)

    if pub_dt is not None:
        p_utc = pub_dt if pub_dt.tzinfo else pub_dt.replace(tzinfo=dt.timezone.utc)
        age_days = (current_time - p_utc).total_seconds() / 86400.0
        if age_days > max_posting_age_days:
            return ValidationResult(
                False,
                f"Posting date is {age_days:.1f} days old (> 3 weeks / {max_posting_age_days} days limit)",
            )
        return ValidationResult(True, f"Posting date is {age_days:.1f} days old (within 3 weeks)")

    # Check relative text candidates in posted_at and description snippet
    text_candidates = []
    if job.posted_at:
        text_candidates.append(job.posted_at)
    if job.jd_text:
        text_candidates.append(job.jd_text[:1200])

    has_detected_date = False
    for text in text_candidates:
        t_clean = text.lower()
        if re.search(r"\b(\d+)?\s*(?:month|yr|year)s?\s*ago\b", t_clean) or re.search(r"\bposted\s+(\d+)?\s*(?:month|yr|year)s?\b", t_clean):
            return ValidationResult(False, "Posting date indicates vacancy is months/years old")

        w_match = re.search(r"(\d+)\s*weeks?\s*ago", t_clean) or re.search(r"posted\s+(\d+)\s*weeks?", t_clean)
        if w_match:
            has_detected_date = True
            weeks = int(w_match.group(1))
            if weeks > 3 or (weeks * 7) > max_posting_age_days:
                return ValidationResult(False, f"Posting is {weeks} weeks old (> 3 weeks limit)")
            return ValidationResult(True, f"Posting is {weeks} weeks old (within 3 weeks)")

        d_match = re.search(r"(\d+)\s*days?\s*ago", t_clean) or re.search(r"posted\s+(\d+)\s*days?", t_clean)
        if d_match:
            has_detected_date = True
            days = int(d_match.group(1))
            if days > max_posting_age_days:
                return ValidationResult(False, f"Posting is {days} days old (> 3 weeks limit)")
            return ValidationResult(True, f"Posting is {days} days old (within 3 weeks)")

        if any(term in t_clean for term in ("hour", "hr ago", "minute", "min ago", "just now", "today", "yesterday", "past 24 hours", "past week", "recently", "active vacancy", "hiring now", "posted_at")):
            has_detected_date = True
            return ValidationResult(True, f"Posting date verified fresh: '{t_clean[:50]}'")

    # 3. If neither deadline nor posting date is mentioned/detected: REJECT
    if not has_detected_date:
        return ValidationResult(False, "No verified posting date or deadline detected (avoiding unverified stale posting)")

    return ValidationResult(True, "Posting freshness verified")


def validate_apply_window(
    job: ExtractedJob,
    closed_phrases: list[str] | None = None,
    min_desc_len: int = 20,
    now: dt.datetime | None = None,
    max_posting_age_days: int = 21,
) -> ValidationResult:
    """Validate that the posting is currently open, not expired, and within the 3-week posting window."""
    current_time = now or dt.datetime.now(dt.timezone.utc)

    # 1. URL validity
    if not job.link or not (job.link.startswith("http://") or job.link.startswith("https://")):
        return ValidationResult(False, f"Invalid or missing apply link: {job.link}")

    # 2. Content length
    if not job.jd_text or len(job.jd_text.strip()) < min_desc_len:
        return ValidationResult(False, f"Job description too short ({len(job.jd_text)} chars, min {min_desc_len})")

    # 3. Closed phrase heuristics in description
    phrases = closed_phrases or DEFAULT_CLOSED_PHRASES
    desc_lower = job.jd_text.lower()
    for phrase in phrases:
        if phrase in desc_lower:
            return ValidationResult(False, f"Posting contains closed phrase: '{phrase}'")

    # 4. Enforce strict deadline and 3-week posting date verification
    freshness_res = validate_job_freshness(job, now=current_time, max_posting_age_days=max_posting_age_days)
    if not freshness_res.valid:
        return freshness_res

    return ValidationResult(True, "Posting is valid and apply window is open")
