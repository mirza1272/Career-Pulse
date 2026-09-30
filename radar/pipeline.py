"""Ingestion and dispatch pipeline connecting discovery, deduplication, storage, and Career Pulse push."""

from __future__ import annotations

from dataclasses import dataclass, field
import datetime as dt
import json
import logging
from typing import Any
import httpx
from sqlalchemy import select

from radar import config
from radar.db import get_session, get_supabase_client
from radar.dedupe import generate_dedup_key
from radar.extractor import ExtractedJob
from radar.models import Application, Job
from radar.search import execute_search
from radar.validator import validate_apply_window, verify_live_url

logger = logging.getLogger("radar.pipeline")


@dataclass
class IngestionStats:
    total_found: int = 0
    valid_count: int = 0
    duplicates_skipped: int = 0
    stored_jobs: int = 0
    pushed_to_career_pulse: int = 0
    errors: list[str] = field(default_factory=list)
    message: str = ""
    queries_run: list[str] = field(default_factory=list)


def push_to_career_pulse_api(
    job_id: int | None,
    email: str,
    jd_text: str,
    job_title: str,
    company: str,
    link: str,
    api_url: str = config.CAREERPULSE_API_URL,
    timeout_s: float = 12.0,
) -> dict[str, Any] | None:
    """Push job with apply-to email to Career Pulse approval queue via API."""
    payload = {
        "job_id": job_id,
        "email": email,
        "jd_text": jd_text,
        "job_title": job_title,
        "company": company,
        "link": link,
    }
    url = f"{api_url}/api/applications"
    try:
        with httpx.Client(timeout=timeout_s) as client:
            res = client.post(url, json=payload)
            if res.status_code == 200:
                return res.json()
            logger.warning(f"Career Pulse API responded {res.status_code}: {res.text[:120]}")
    except Exception as exc:
        logger.info(f"Career Pulse API not reachable at {url} ({exc}); using shared database fallback.")
    return None


def push_to_career_pulse_db(
    job_id: int | None,
    email: str,
    jd_text: str,
    job_title: str,
    company: str,
    link: str,
) -> int:
    """Fallback direct database insertion into shared applications table when API is offline."""
    with get_session() as session:
        # Check if already present in applications
        existing = session.scalars(
            select(Application).where(Application.email == email, Application.job_title == job_title)
        ).first()
        if existing:
            return existing.id

        app = Application(
            job_id=job_id,
            email=email,
            job_title=job_title,
            company=company,
            link=link,
            jd_text=jd_text,
            status="pending",
        )
        session.add(app)
        session.flush()
        app_id = app.id

        supabase = get_supabase_client()
        if supabase.is_configured:
            supabase.upsert_application(
                {
                    "id": app.id,
                    "job_id": app.job_id,
                    "email": app.email,
                    "job_title": app.job_title,
                    "company": app.company,
                    "link": app.link,
                    "jd_text": app.jd_text,
                    "status": app.status,
                },
                session=session,
            )
    return app_id


def process_extracted_jobs(
    jobs: list[ExtractedJob],
    push_email_jobs: bool = True,
) -> IngestionStats:
    """Validate, deduplicate, store, and push a batch of extracted jobs."""
    stats = IngestionStats(total_found=len(jobs))
    now_utc = dt.datetime.now(dt.timezone.utc)
    supabase = get_supabase_client()

    with get_session() as session:
        for ext in jobs:
            # 1. Apply-window validation
            val = validate_apply_window(ext, now=now_utc)
            if not val.valid:
                logger.info(f"Skipping invalid job '{ext.title}': {val.reason}")
                continue

            # 1a. Canonicalize link to official company careers portal & reject aggregators
            from radar.search import canonicalize_job_link, is_spam_aggregator
            if ext.link:
                canonical, _ = canonicalize_job_link(ext.company, ext.link)
                if not canonical or is_spam_aggregator(canonical):
                    logger.info(f"Skipping job '{ext.title}' @ '{ext.company}': aggregator link rejected ({ext.link})")
                    continue
                ext.link = canonical

            # 1b. Live URL verification to prevent closed/expired postings from entering
            if ext.link and not ext.source.startswith("mock"):
                is_live, live_msg = verify_live_url(ext.link, timeout_s=4.0)
                if not is_live:
                    logger.info(f"Skipping expired/closed job '{ext.title}' @ '{ext.company}': {live_msg}")
                    continue

            stats.valid_count += 1

            # 2. Deduplication key
            dedup_key = generate_dedup_key(ext.company, ext.title, ext.link)
            existing_job = session.scalars(select(Job).where(Job.dedup_key == dedup_key)).first()
            if existing_job:
                stats.duplicates_skipped += 1
                logger.debug(f"Duplicate job skipped: {ext.title} @ {ext.company}")
                continue

            # 3. Calculate expires_at
            if ext.deadline:
                d_utc = ext.deadline if ext.deadline.tzinfo else ext.deadline.replace(tzinfo=dt.timezone.utc)
                expires_at = d_utc + dt.timedelta(hours=config.EXPIRY_HOURS_POST_DEADLINE)
            else:
                expires_at = now_utc + dt.timedelta(hours=config.EXPIRY_HOURS_DEFAULT)

            # 4. Insert into shared jobs table
            job_row = Job(
                dedup_key=dedup_key,
                title=ext.title,
                company=ext.company,
                location=ext.location,
                link=ext.link,
                has_email=ext.has_email,
                email=ext.email,
                jd_text=ext.jd_text,
                deadline=ext.deadline,
                status="active",
                source=ext.source,
                created_at=now_utc,
                expires_at=expires_at,
            )
            session.add(job_row)
            session.commit()
            stats.stored_jobs += 1
            saved_job_id = job_row.id

            # Sync to Supabase if configured
            if supabase.is_configured:
                supabase.insert_job(
                    {
                        "dedup_key": dedup_key,
                        "title": ext.title,
                        "company": ext.company,
                        "location": ext.location,
                        "link": ext.link,
                        "has_email": ext.has_email,
                        "email": ext.email,
                        "jd_text": ext.jd_text[:4000],
                        "deadline": ext.deadline.isoformat() if ext.deadline else None,
                        "status": "active",
                        "source": ext.source,
                    }
                )

            # 5. Push to Career Pulse if posting contains an apply email
            if push_email_jobs and ext.has_email and ext.email:
                api_res = push_to_career_pulse_api(
                    job_id=saved_job_id,
                    email=ext.email,
                    jd_text=ext.jd_text,
                    job_title=ext.title,
                    company=ext.company,
                    link=ext.link,
                )
                if api_res:
                    stats.pushed_to_career_pulse += 1
                    logger.info(f"Pushed to Career Pulse API: app_id={api_res.get('id')}")
                else:
                    app_id = push_to_career_pulse_db(
                        job_id=saved_job_id,
                        email=ext.email,
                        jd_text=ext.jd_text,
                        job_title=ext.title,
                        company=ext.company,
                        link=ext.link,
                    )
                    stats.pushed_to_career_pulse += 1
                    logger.info(f"Pushed to Career Pulse via shared DB: app_id={app_id}")

    return stats


def generate_search_queries_with_llm(
    role: str = "Junior Developer (AI / Full-Stack / ASE)",
    location: str = "worldwide_remote_or_pakistan_onsite",
    experience_level: str = "junior",
) -> list[str]:
    """Generate 3 to 4 distinct, high-yield job search queries using Groq LLM structured JSON output.
    
    Ensures diverse coverage: AI/ML engineering, Full-Stack / Python, and ASE roles across Remote and Pakistan.
    Always fails soft to 4 curated fallback queries if LLM API is unavailable.
    """
    clean_role = role or "Junior Developer"
    loc_label = "Worldwide Remote or Pakistan (Lahore/Islamabad/Karachi)"
    if "lahore" in location.lower():
        loc_label = "Lahore, Pakistan (and Remote)"
    elif "islamabad" in location.lower() or "rawalpindi" in location.lower():
        loc_label = "Islamabad, Pakistan (and Remote)"
    elif "faisalabad" in location.lower():
        loc_label = "Faisalabad, Pakistan (and Remote)"
    elif "karachi" in location.lower():
        loc_label = "Karachi, Pakistan (and Remote)"

    fallback = [
        f"Junior {clean_role} Remote".replace("Junior Junior", "Junior"),
        "Junior AI Engineer Pakistan",
        "Associate Software Engineer Lahore",
        "Junior Full Stack Developer Python React",
    ]

    if not config.LLM_API_KEY:
        return fallback

    prompt = f"""You are an expert technical recruitment and job discovery specialist.
Generate 3 to 4 distinct, high-yield job search queries for discovering fresh job postings matching:
- Target Role / Skills: {clean_role}
- Experience Level: {experience_level} (junior / entry-level / fresh graduate / 0-2 years)
- Target Locations: {loc_label}

CRITICAL RULES:
1. Each query must be concise (2 to 5 words), realistic for job search engines (e.g. "Junior AI Engineer Remote", "Associate Software Engineer Lahore", "Junior Python Developer Remote", "Junior Full Stack Developer").
2. Ensure queries cover distinct angles: specific role variant (AI vs Full Stack vs ASE), core tech stack, and location targets (Remote vs Pakistan).
3. Output ONLY a valid JSON object with a single key 'queries' containing an array of 3 or 4 query strings.

Example:
{{"queries": ["Junior AI Engineer Remote", "Associate Software Engineer Lahore", "Junior Python Developer Remote", "Junior Full Stack Developer Pakistan"]}}"""

    try:
        r = httpx.post(
            config.LLM_BASE_URL,
            headers={
                "Authorization": f"Bearer {config.LLM_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": config.LLM_MODEL,
                "messages": [
                    {
                        "role": "system",
                        "content": "You are a precise technical recruitment search query generator. Return only a valid JSON object.",
                    },
                    {"role": "user", "content": prompt},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.3,
            },
            timeout=15.0,
        )
        if r.status_code == 200:
            data = r.json()
            content = data["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            queries = parsed.get("queries", [])
            if isinstance(queries, list) and len(queries) >= 3:
                cleaned = [str(q).strip() for q in queries[:4] if str(q).strip()]
                if len(cleaned) >= 3:
                    logger.info(f"Groq LLM generated {len(cleaned)} structured search queries: {cleaned}")
                    return cleaned
    except Exception as exc:
        logger.warning(f"Groq LLM search query generation failed ({exc}); using fallback query set.")

    return fallback


def run_pipeline(
    query: str | None = None,
    role: str = "Junior Developer (AI / Full-Stack / ASE)",
    custom_role: str = "",
    experience_level: str = "junior",
    location: str = "worldwide_remote_or_pakistan_onsite",
    custom_location: str = "",
    include_remote: bool = True,
    platform: str = "all",
    limit: int = 10,
    push_email_jobs: bool = True,
) -> IngestionStats:
    """Discover jobs using 3 to 4 distinct structured search queries generated dynamically by Groq LLM."""
    effective_role = (custom_role.strip() if custom_role else "") or (query.strip() if query else "") or role or "Junior Developer (AI / Full-Stack / ASE)"
    effective_loc = (custom_location.strip() if custom_location else "") or location or "worldwide_remote_or_pakistan_onsite"

    # Step 1: Generate 3-4 structured search queries using Groq LLM
    queries_to_run = generate_search_queries_with_llm(
        role=effective_role,
        location=effective_loc,
        experience_level=experience_level,
    )

    # If the user passed an explicit manual query string and it's not already present, prioritize it
    if query and query.strip():
        q_strip = query.strip()
        if q_strip not in queries_to_run:
            queries_to_run.insert(0, q_strip)
            queries_to_run = queries_to_run[:4]

    logger.info(f"Pipeline running {len(queries_to_run)} structured queries: {queries_to_run}")

    all_jobs: list[ExtractedJob] = []
    seen_links: set[str] = set()
    per_query_limit = max(3, (limit // len(queries_to_run)) + 2)

    # Step 2: Execute search across each LLM-generated query
    for idx, q in enumerate(queries_to_run, 1):
        logger.info(f"Running LLM Query [{idx}/{len(queries_to_run)}]: '{q}' (<= 7 days freshness, {platform})")
        q_jobs = execute_search(
            query=q,
            role=q,
            custom_role="",
            experience_level=experience_level,
            location=effective_loc,
            custom_location="",
            include_remote=include_remote,
            platform=platform,
            limit=per_query_limit,
            max_days=7,
        )
        for j in q_jobs:
            if j.link and j.link not in seen_links:
                seen_links.add(j.link)
                all_jobs.append(j)

    # Step 3: If still under requested limit, run a pass with extended window (<= 14 days)
    if len(all_jobs) < limit:
        logger.info(f"Pass 1 yielded {len(all_jobs)} jobs. Expanding window to <= 14 days on top queries...")
        for q in queries_to_run[:2]:
            if len(all_jobs) >= limit:
                break
            expanded_jobs = execute_search(
                query=q,
                role=q,
                custom_role="",
                experience_level=experience_level,
                location=effective_loc,
                custom_location="",
                include_remote=include_remote,
                platform=platform,
                limit=limit - len(all_jobs),
                max_days=14,
            )
            for j in expanded_jobs:
                if j.link and j.link not in seen_links:
                    seen_links.add(j.link)
                    all_jobs.append(j)

    stats = process_extracted_jobs(all_jobs, push_email_jobs=push_email_jobs)
    stats.queries_run = queries_to_run
    if not all_jobs:
        stats.message = f"No active jobs found. Ran {len(queries_to_run)} AI queries: {', '.join(queries_to_run)}."
    else:
        stats.message = f"Ran {len(queries_to_run)} AI queries ({', '.join(queries_to_run[:2])}...). Found {len(all_jobs)} jobs."
    return stats


def cleanup_stale_aggregator_jobs() -> int:
    """Scrub existing database entries: rewrite recognized companies to official portals, delete dead aggregator spam."""
    from radar.search import canonicalize_job_link, is_spam_aggregator

    cleaned = 0
    with get_session() as session:
        jobs = session.scalars(select(Job)).all()
        for j in jobs:
            if is_spam_aggregator(j.link):
                canonical, _ = canonicalize_job_link(j.company, j.link)
                if canonical and not is_spam_aggregator(canonical):
                    logger.info(f"Rewrote aggregator link for job #{j.id} ({j.company}): {j.link} -> {canonical}")
                    j.link = canonical
                    j.source = f"official_portal ({j.company})"
                    cleaned += 1
                else:
                    logger.info(f"Removing dead aggregator job #{j.id} ({j.company}) with link {j.link}")
                    session.delete(j)
                    cleaned += 1
        session.commit()
    return cleaned
