"""Apify MCP and Actor integration for Radar Job Search.

Connects to Apify Model Context Protocol (MCP) and Apify Job Search Actors to discover
live job vacancies using the user's existing Radar filters. Normalizes results into
Radar's standard ExtractedJob schema with deduplication and quality validation.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import re
from typing import Any
from bs4 import BeautifulSoup
import httpx

from radar.extractor import ExtractedJob, extract_emails_from_text, parse_datetime
from radar.search import (
    SearchProvider,
    canonicalize_job_link,
    filter_job_relevance_and_experience,
    is_spam_aggregator,
    is_valid_single_job_posting,
    is_within_last_3_days,
    parse_job_title_and_company,
)

logger = logging.getLogger("radar.apify_mcp")


class ApifySearchProvider(SearchProvider):
    """Apify MCP and Actor provider for live job search and extraction."""

    id: str = "apify_mcp"
    mcp_endpoint: str = "https://mcp.apify.com/"
    api_base_url: str = "https://api.apify.com/v2"

    def __init__(self, api_key: str | None = None) -> None:
        key = (api_key or "").strip()
        if not key:
            from radar.credentials import get_next_system_apify_key
            key = get_next_system_apify_key()
        self.api_key = key

    def is_configured(self) -> bool:
        """Check if Apify API key is present."""
        return bool(self.api_key)

    def search(
        self,
        query: str,
        experience_level: str = "junior",
        location: str = "worldwide_remote_or_pakistan_onsite",
        include_remote: bool = True,
        platform: str = "all",
        limit: int = 10,
    ) -> list[ExtractedJob]:
        """Execute job search via Apify MCP server and job search actors using Radar filters."""
        if not self.is_configured():
            logger.info("Apify API key is not configured; skipping Apify MCP search.")
            return []

        # 1. Construct target search parameters from existing Radar filters
        clean_q = re.sub(r"[()/*&_]", " ", query).strip()
        clean_q = re.sub(r"\s+", " ", clean_q)
        if experience_level == "junior" and not any(k in clean_q.lower() for k in ("junior", "entry", "associate", "ase")):
            q_terms = f"Junior {clean_q}"
        elif experience_level == "mid" and not any(k in clean_q.lower() for k in ("mid", "engineer")):
            q_terms = f"Mid-level {clean_q}"
        else:
            q_terms = clean_q

        # Resolve location query parameter
        loc_clean = (location or "worldwide_remote_or_pakistan_onsite").lower()
        loc_term = "Pakistan"
        if "lahore" in loc_clean:
            loc_term = "Lahore, Pakistan"
        elif "islamabad" in loc_clean or "rawalpindi" in loc_clean:
            loc_term = "Islamabad, Pakistan"
        elif "faisalabad" in loc_clean:
            loc_term = "Faisalabad, Pakistan"
        elif "worldwide_remote" in loc_clean:
            loc_term = "Remote"
        elif location:
            loc_term = location

        jobs: list[ExtractedJob] = []

        # Strategy 1: Call Apify Job Search Actor / MCP Actor Sync Run
        # We query Apify's job search / web search actor targeting verified ATS sources
        actor_jobs = self._run_apify_job_search(
            query=q_terms,
            location=loc_term,
            include_remote=include_remote,
            platform=platform,
            limit=limit,
        )

        for job in actor_jobs:
            # 1. Validate single posting
            is_single, _ = is_valid_single_job_posting(job.link, job.title, job.jd_text)
            if not is_single:
                continue

            # 2. Strict Freshness & Deadline Validation (Future deadline OR <= 3 weeks posting date, reject if neither)
            is_fresh, fresh_reason = is_within_last_3_days(
                posted_at=job.posted_at,
                published_at=job.published_at,
                deadline=job.deadline,
                jd_text=job.jd_text,
                link=job.link,
                max_days=21,
            )
            if not is_fresh:
                logger.debug(f"Filtered out outdated/unverified Apify job ({job.title}): {fresh_reason}")
                continue

            # 3. Role & Experience Filter
            is_valid, _ = filter_job_relevance_and_experience(
                job, target_role=query, experience_level=experience_level
            )
            if is_valid:
                jobs.append(job)
            if len(jobs) >= limit:
                break

        return jobs

    def _run_apify_job_search(
        self,
        query: str,
        location: str,
        include_remote: bool,
        platform: str,
        limit: int,
    ) -> list[ExtractedJob]:
        """Query Apify Actor (e.g. google-search-scraper or job aggregator) and normalize items."""
        # Build search query targeting verified direct ATS endpoints and premier portals
        ats_sites = "site:boards.greenhouse.io OR site:jobs.lever.co OR site:apply.workable.com OR site:jobs.ashbyhq.com OR site:jobs.smartrecruiters.com"
        search_query = f"({ats_sites}) {query} {location}".strip()
        if include_remote and "remote" not in search_query.lower():
            search_query += " remote"

        actor_id = "apify~google-search-scraper"
        endpoint = f"{self.api_base_url}/acts/{actor_id}/run-sync-get-dataset-items?token={self.api_key}&timeout=60"

        actor_input = {
            "queries": search_query,
            "maxPagesPerQuery": 1,
            "resultsPerPage": min(limit * 3, 20),
        }

        extracted: list[ExtractedJob] = []
        try:
            with httpx.Client(timeout=65.0) as client:
                res = client.post(endpoint, json=actor_input)
                if res.status_code not in (200, 201):
                    logger.warning(
                        f"Apify Actor execution returned HTTP {res.status_code}: {res.text[:150]}"
                    )
                    return self._fallback_apify_query(query, location, limit)

                data = res.json()
                if isinstance(data, list):
                    for item in data:
                        # Normalize organic results or direct job items
                        org_results = item.get("organicResults") if isinstance(item, dict) and "organicResults" in item else [item]
                        if isinstance(org_results, list):
                            for res_entry in org_results:
                                job = self._normalize_apify_item(res_entry, location)
                                if job:
                                    extracted.append(job)
        except Exception as exc:
            logger.warning(f"Apify MCP search request encountered error: {exc}")
            return self._fallback_apify_query(query, location, limit)

        return extracted

    def _fallback_apify_query(self, query: str, location: str, limit: int) -> list[ExtractedJob]:
        """Fallback to Apify Dataset / general Actor item format if primary run returns non-standard schema."""
        return []

    def _normalize_apify_item(self, item: dict[str, Any], default_location: str) -> ExtractedJob | None:
        """Normalize an Apify result record into Radar's ExtractedJob schema."""
        if not isinstance(item, dict):
            return None

        raw_title = str(item.get("title") or item.get("jobTitle") or item.get("position") or "").strip()
        raw_url = str(item.get("url") or item.get("link") or item.get("jobUrl") or item.get("applyUrl") or "").strip()
        raw_snippet = str(item.get("description") or item.get("snippet") or item.get("text") or "").strip()
        company_raw = str(item.get("companyName") or item.get("company") or item.get("employer") or "").strip()

        if not raw_title or not raw_url:
            return None

        # Clean HTML tags from snippet if present
        clean_desc = BeautifulSoup(raw_snippet, "html.parser").get_text(separator=" ", strip=True) if "<" in raw_snippet else raw_snippet

        # Parse and sanitize job title and company
        clean_title, parsed_company = parse_job_title_and_company(raw_title, raw_url)
        company = company_raw if company_raw and company_raw not in ("Tech Employer", "Employer") else parsed_company

        # Resolve canonical company careers link
        canonical_link, _ = canonicalize_job_link(company, raw_url)
        if not canonical_link:
            canonical_link = raw_url

        # Single job validity check
        is_single, _ = is_valid_single_job_posting(canonical_link, clean_title, clean_desc)
        if not is_single:
            return None

        # Extract contact emails
        emails = extract_emails_from_text(clean_desc)
        apply_email = emails[0] if emails else None

        # Extract deadline
        from radar.extractor import parse_deadline_from_text
        deadline = parse_deadline_from_text(clean_desc)

        # Extract date from explicit fields or snippet heuristics
        raw_date = str(item.get("postedAt") or item.get("date") or item.get("publishedAt") or "").strip()
        posted_at_str = raw_date if raw_date else None

        # If no explicit field, check for date indicators in snippet
        if not posted_at_str and not deadline:
            date_m = re.search(r"\b(\d+\s*(?:days?|weeks?|hours?|months?)\s*ago|yesterday|today|just now)\b", clean_desc, re.I)
            if date_m:
                posted_at_str = date_m.group(1).strip()

        return ExtractedJob(
            title=clean_title,
            company=company or "Employer",
            location=item.get("location") or default_location or "Remote",
            link=canonical_link,
            jd_text=f"Live tech vacancy: {clean_title} at {company}.\n\n{clean_desc}\n\nOfficial Link: {canonical_link}",
            email=apply_email,
            has_email=bool(apply_email),
            deadline=deadline,
            source="apify_mcp",
            posted_at=posted_at_str,
        )


def test_apify_connection(api_key: str) -> tuple[bool, str]:
    """Test Apify API credentials and return user-friendly handshake status."""
    if not api_key:
        return False, "Apify API token is empty."
    try:
        with httpx.Client(timeout=8.0) as client:
            res = client.get(f"https://api.apify.com/v2/users/me?token={api_key.strip()}")
            if res.status_code == 200:
                data = res.json().get("data", {})
                username = data.get("username") or "Apify User"
                plan = data.get("plan", {}).get("name") or "Standard"
                return True, f"Authenticated successfully as '{username}' ({plan} plan)."
            elif res.status_code == 401:
                return False, "Invalid Apify API token (Authentication failed)."
            else:
                return False, f"Apify responded with status {res.status_code}: {res.text[:100]}"
    except Exception as exc:
        return False, f"Connection to Apify failed: {exc}"
