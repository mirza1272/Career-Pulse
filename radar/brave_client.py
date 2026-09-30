"""Brave Search API integration for company career discovery and vacancy search."""

from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Any
import httpx
from bs4 import BeautifulSoup

from radar import config
from radar.extractor import ExtractedJob, extract_emails_from_text

logger = logging.getLogger("radar.brave")

# Common spam/aggregator domains to exclude from official company search results
EXCLUDED_DOMAINS = [
    "ai-search.io", "getpakjob.com", "bebee.com", "jooble.org", "paperpk.com",
    "jobz.pk", "talents.vaia.com", "careers-page.com", "pk.prosple.com",
    "pk.indeed.com", "clickajob.com", "rozee.pk", "mustakbil.com", "bayt.com",
    "salary.com", "glassdoor.com", "ziprecruiter.com", "jobssection.com",
    "itjobsinpakistan.com", "jang.com.pk", "postjobfree.com",
]


class BraveSearchClient:
    """Client for Brave Web Search API (https://api.search.brave.com)."""

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = (api_key or config.BRAVE_API_KEY).strip()
        self.base_url = "https://api.search.brave.com/res/v1/web/search"

    def is_configured(self) -> bool:
        """Check if Brave Search API key is available."""
        return bool(self.api_key)

    def _get_headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": self.api_key,
        }

    def find_company_career_page(self, company_name: str, city: str = "Pakistan") -> str | None:
        """Discover the genuine official career portal or job openings page for a company."""
        if not self.is_configured():
            return None

        # Exclude aggregators from search
        excl_str = " ".join(f"-site:{d}" for d in EXCLUDED_DOMAINS[:6])
        q = f'"{company_name}" official careers jobs {city} {excl_str}'
        params = {
            "q": q,
            "count": 5,
            "safesearch": "moderate",
        }

        try:
            with httpx.Client(timeout=10.0) as client:
                res = client.get(self.base_url, headers=self._get_headers(), params=params)
                if res.status_code != 200:
                    logger.warning(f"Brave Search error {res.status_code} finding career page for {company_name}")
                    return None
                data = res.json()
        except Exception as exc:
            logger.warning(f"Brave Search request failed for {company_name}: {exc}")
            return None

        results = data.get("web", {}).get("results", [])
        for r in results:
            url = r.get("url", "")
            title = (r.get("title") or "").lower()
            url_lower = url.lower()

            # Skip aggregators
            if any(bad in url_lower for bad in EXCLUDED_DOMAINS):
                continue

            # Prioritize dedicated career URLs
            if any(k in url_lower for k in ("career", "job", "openings", "work-with-us", "join-us", "positions", "apply.")):
                return url
            if any(k in title for k in ("career", "jobs", "join our team", "openings")):
                return url

        # Fallback to first non-aggregator domain if found
        if results:
            first_url = results[0].get("url")
            if first_url and not any(bad in first_url.lower() for bad in EXCLUDED_DOMAINS):
                return first_url

        return None

    def search_company_vacancies(
        self,
        company_name: str,
        careers_url: str = "",
        target_role: str = "junior developer software engineer",
        city: str = "Pakistan",
        limit: int = 4,
    ) -> list[ExtractedJob]:
        """Search for live tech openings directly on the official company domain via Brave Search."""
        if not self.is_configured():
            return []

        # Extract domain from careers_url if provided
        domain_match = re.search(r"https?://(?:www\.)?([^/]+)", careers_url)
        domain = domain_match.group(1) if domain_match else ""

        if domain:
            query = f"site:{domain} ({target_role} OR engineer OR developer OR ase) -internship"
        else:
            excl = " ".join(f"-site:{d}" for d in EXCLUDED_DOMAINS[:8])
            query = f'"{company_name}" ({target_role} OR engineer OR developer) {city} careers {excl}'

        params = {
            "q": query,
            "count": min(limit * 2, 10),
            "freshness": "pw",  # Past week for fresh postings
        }

        try:
            with httpx.Client(timeout=10.0) as client:
                res = client.get(self.base_url, headers=self._get_headers(), params=params)
                if res.status_code != 200:
                    # Retry without freshness filter if past week was too narrow
                    params.pop("freshness", None)
                    res = client.get(self.base_url, headers=self._get_headers(), params=params)
                    if res.status_code != 200:
                        logger.warning(f"Brave Search returned {res.status_code} for {company_name} vacancies")
                        return []
                data = res.json()
        except Exception as exc:
            logger.warning(f"Brave Search vacancy lookup failed for {company_name}: {exc}")
            return []

        jobs: list[ExtractedJob] = []
        now_utc = dt.datetime.now(dt.timezone.utc)

        for item in data.get("web", {}).get("results", []):
            url = item.get("url", "")
            title = item.get("title", "").strip()
            desc = item.get("description", "").strip()
            url_lower = url.lower()

            # Skip aggregators
            if any(bad in url_lower for bad in EXCLUDED_DOMAINS):
                continue

            # Ensure this is an engineering / developer role
            title_lower = title.lower()
            if not any(k in title_lower for k in ("engineer", "developer", "architect", "programmer", "specialist", "ase", "full stack", "ai", "software")):
                continue

            # Clean title
            clean_title = re.sub(r"\s*[-|–]\s*(?:Careers|Jobs|Systems Limited|Devsinc|10Pearls|Arbisoft).*$", "", title, flags=re.IGNORECASE).strip()

            emails = extract_emails_from_text(desc)
            apply_email = emails[0] if emails else None

            job = ExtractedJob(
                title=clean_title or title,
                company=company_name,
                location=f"{city}, Pakistan" if city else "Pakistan",
                link=url,
                jd_text=f"Official job vacancy at {company_name}.\n\n{desc}\n\nOfficial Portal: {url}",
                email=apply_email,
                has_email=bool(apply_email),
                deadline=None,
                source=f"brave:{company_name.lower().replace(' ', '_')}",
                posted_at="1 day ago",
                published_at=now_utc - dt.timedelta(hours=12),
            )
            jobs.append(job)
            if len(jobs) >= limit:
                break

        return jobs
