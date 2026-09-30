"""Expiry worker: enforces 24h post-deadline and 48h maximum freshness windows."""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import select

from radar import config
from radar.db import get_session, get_supabase_client
from radar.models import Job
from radar.validator import verify_live_url

logger = logging.getLogger("radar.expiry")


def check_job_expired(
    job: Job,
    now: dt.datetime | None = None,
    default_hours: int = 48,
    post_deadline_hours: int = 24,
) -> bool:
    """Determine if an active job has expired."""
    curr = now or dt.datetime.now(dt.timezone.utc)

    # 1. Deadline-based expiry (24h after deadline)
    if job.deadline:
        d_utc = job.deadline if job.deadline.tzinfo else job.deadline.replace(tzinfo=dt.timezone.utc)
        deadline_expiry = d_utc + dt.timedelta(hours=post_deadline_hours)
        if curr > deadline_expiry:
            return True

    # 2. Maximum lifetime expiry (48h after creation)
    c_utc = job.created_at if job.created_at.tzinfo else job.created_at.replace(tzinfo=dt.timezone.utc)
    max_lifetime = c_utc + dt.timedelta(hours=default_hours)
    if curr > max_lifetime:
        return True

    return False


def run_expiry_check(
    now: dt.datetime | None = None,
    check_live_urls: bool = True,
) -> int:
    """Scan all active jobs and mark expired ones (deadline, lifetime, or dead/closed URL).

    Returns number of jobs marked as expired.
    """
    curr = now or dt.datetime.now(dt.timezone.utc)
    expired_count = 0
    supabase = get_supabase_client()

    with get_session() as session:
        stmt = select(Job).where(Job.status == "active")
        active_jobs = session.scalars(stmt).all()

        for job in active_jobs:
            is_expired = False
            reason = ""

            # Check rule-based expiry
            if check_job_expired(
                job,
                now=curr,
                default_hours=config.EXPIRY_HOURS_DEFAULT,
                post_deadline_hours=config.EXPIRY_HOURS_POST_DEADLINE,
            ):
                is_expired = True
                reason = "deadline or lifetime exceeded"

            # Check live URL freshness if enabled and not already expired
            elif check_live_urls and job.link:
                is_live, check_msg = verify_live_url(job.link, timeout_s=5.0)
                if not is_live:
                    is_expired = True
                    reason = f"live URL closed/dead ({check_msg})"

            if is_expired:
                job.status = "expired"
                expired_count += 1
                logger.info(f"Marked expired job id={job.id} '{job.title}' @ '{job.company}': {reason}")
                if supabase.is_configured:
                    supabase.update_job_status(job.id, "expired")

    return expired_count
