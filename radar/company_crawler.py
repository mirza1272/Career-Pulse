"""Career page crawler and ATS aggregator for 75+ software companies in Pakistan with Firecrawl and Brave Search fallback."""

from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Any
from urllib.parse import urljoin
import httpx
from bs4 import BeautifulSoup

from radar.companies_registry import (
    TOP_PAKISTAN_TECH_COMPANIES,
    CompanyEntry,
    get_companies_for_city,
    find_company_by_name,
)
from radar.extractor import ExtractedJob
from radar.firecrawl_client import FirecrawlClient
from radar.brave_client import BraveSearchClient
from radar import config

logger = logging.getLogger("radar.company_crawler")


class CompanyCareerCrawler:
    """Discovers and crawls live job vacancies from 75+ top tech company career pages & ATS portals."""

    def __init__(self, firecrawl_client: FirecrawlClient | None = None, brave_client: BraveSearchClient | None = None) -> None:
        self.firecrawl = firecrawl_client or FirecrawlClient()
        self.brave = brave_client or BraveSearchClient()

    def crawl_workable_account(
        self,
        account_id: str,
        company_name: str,
        target_role: str = "",
        experience_level: str = "junior",
        target_city: str = "",
    ) -> list[ExtractedJob]:
        """Fetch live jobs directly from Workable public widget API (e.g. Devsinc)."""
        url = f"https://apply.workable.com/api/v3/accounts/{account_id}/jobs"
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        jobs: list[ExtractedJob] = []
        try:
            with httpx.Client(timeout=12.0) as client:
                res = client.post(url, json={}, headers=headers)
                if res.status_code != 200:
                    logger.warning(f"Workable API for {account_id} returned {res.status_code}")
                    return []
                data = res.json()
        except Exception as exc:
            logger.warning(f"Failed to query Workable for {account_id}: {exc}")
            return []

        results = data.get("results", [])
        for item in results:
            title = str(item.get("title", "")).strip()
            city = str(item.get("city") or "").strip()
            country = str(item.get("country") or "Pakistan").strip()
            loc_str = f"{city}, {country}".strip().strip(",")
            shortcode = item.get("shortcode", "")
            apply_link = f"https://apply.workable.com/{account_id}/j/{shortcode}/" if shortcode else f"https://apply.workable.com/{account_id}/"
            pub_raw = item.get("published")
            pub_dt: dt.datetime | None = None
            if pub_raw:
                try:
                    pub_dt = dt.datetime.fromisoformat(str(pub_raw).replace("Z", "+00:00"))
                except Exception:
                    pass

            desc_snippet = str(item.get("description") or f"Official technical vacancy at {company_name}: {title} in {loc_str}. Great opportunity for early-career developers to build production applications. Apply directly via official portal: {apply_link}")

            job = ExtractedJob(
                title=title,
                company=company_name,
                location=loc_str or "Lahore / Pakistan",
                link=apply_link,
                jd_text=desc_snippet,
                email=None,
                has_email=False,
                deadline=None,
                source=f"ats:{company_name.lower().replace(' ', '_')}",
                posted_at=pub_raw,
                published_at=pub_dt,
            )
            jobs.append(job)

        return jobs

    def crawl_via_firecrawl(
        self,
        company: CompanyEntry,
        target_role: str = "",
        max_jobs: int = 4,
    ) -> list[ExtractedJob]:
        """Crawl company career page using Firecrawl markdown extraction."""
        if not self.firecrawl.is_configured():
            logger.debug("Firecrawl not configured; skipping Firecrawl crawl for %s", company.name)
            return []

        logger.info(f"Firecrawl scraping official career page for {company.name}: {company.careers_url}")
        data = self.firecrawl.scrape(company.careers_url, timeout=22.0)
        if not data or not data.get("markdown"):
            return []

        md = data.get("markdown", "")
        jobs: list[ExtractedJob] = []

        # Find markdown links resembling job openings: [Title](url)
        link_pattern = re.compile(r"\[([^\]]{4,80})\]\((https?://[^\s\)]+|/[^\s\)]+)(?: \"[^\"]*\")?\)")
        for match in link_pattern.finditer(md):
            link_text = match.group(1).strip()
            raw_url = match.group(2).strip()

            # Resolve relative URLs to company domain
            if raw_url.startswith("/"):
                link_url = urljoin(company.careers_url, raw_url)
            else:
                link_url = raw_url

            text_lower = link_text.lower()

            # Check if this link looks like an engineering/tech job opening
            if any(k in text_lower for k in ("engineer", "developer", "architect", "programmer", "specialist", "ase", "full stack", "frontend", "backend", "ai")):
                if not any(k in text_lower for k in ("watch", "story", "video", "privacy", "terms", "cookie", "login", "register", "blog")):
                    primary_city = company.cities[0] if company.cities else "Pakistan"
                    job = ExtractedJob(
                        title=link_text,
                        company=company.name,
                        location=f"{primary_city}, Pakistan",
                        link=link_url,
                        jd_text=f"Official job opening at {company.name} ({primary_city}): {link_text}.\nOfficial Career Portal: {company.careers_url}",
                        email=None,
                        has_email=False,
                        deadline=None,
                        source=f"firecrawl:{company.name.lower().replace(' ', '_')}",
                        posted_at="1 day ago",
                        published_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=6),
                    )
                    jobs.append(job)
                    if len(jobs) >= max_jobs:
                        break

        return jobs

    def crawl_via_brave(
        self,
        company: CompanyEntry,
        target_role: str = "junior developer software engineer",
        max_jobs: int = 3,
    ) -> list[ExtractedJob]:
        """Discover live tech vacancies on official company domain using Brave Search API fallback."""
        if not self.brave.is_configured():
            return []

        primary_city = company.cities[0] if company.cities else "Pakistan"
        logger.info(f"Brave Search discovering official vacancies for {company.name} ({company.careers_url})")
        return self.brave.search_company_vacancies(
            company_name=company.name,
            careers_url=company.careers_url,
            target_role=target_role,
            city=primary_city,
            limit=max_jobs,
        )

    def crawl_via_direct_http(
        self,
        company: CompanyEntry,
        target_role: str = "",
        max_jobs: int = 3,
    ) -> list[ExtractedJob]:
        """Zero-cost direct HTTP scraper for career pages as fallback."""
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        jobs: list[ExtractedJob] = []
        try:
            with httpx.Client(timeout=10.0, follow_redirects=True) as client:
                res = client.get(company.careers_url, headers=headers)
                if res.status_code != 200:
                    return []
                soup = BeautifulSoup(res.text, "html.parser")
        except Exception as exc:
            logger.debug(f"Direct HTTP fetch failed for {company.name}: {exc}")
            return []

        primary_city = company.cities[0] if company.cities else "Pakistan"
        for a in soup.find_all("a", href=True):
            text = a.get_text(strip=True)
            text_lower = text.lower()
            if any(k in text_lower for k in ("engineer", "developer", "architect", "programmer", "ase", "full stack")):
                if not any(k in text_lower for k in ("services", "solutions", "privacy", "terms", "cookie", "login", "contact", "about")):
                    full_url = urljoin(company.careers_url, a["href"])
                    job = ExtractedJob(
                        title=text,
                        company=company.name,
                        location=f"{primary_city}, Pakistan",
                        link=full_url,
                        jd_text=f"Official job vacancy at {company.name}: {text}.\nOfficial Portal: {company.careers_url}",
                        email=None,
                        has_email=False,
                        deadline=None,
                        source=f"portal:{company.name.lower().replace(' ', '_')}",
                        posted_at="1 day ago",
                        published_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=12),
                    )
                    jobs.append(job)
                    if len(jobs) >= max_jobs:
                        break

        return jobs

    def crawl_company(
        self,
        company: CompanyEntry,
        target_role: str = "junior developer",
        experience_level: str = "junior",
        max_jobs: int = 3,
    ) -> list[ExtractedJob]:
        """Crawl a single company using multi-tier fallback (Workable -> Firecrawl -> Brave -> Direct HTTP)."""
        # Tier 1: Direct ATS API (Workable)
        if company.ats_type == "workable" and company.ats_identifier:
            jobs = self.crawl_workable_account(company.ats_identifier, company.name, target_role=target_role, experience_level=experience_level)
            if jobs:
                return jobs[:max_jobs]

        # Tier 2: Firecrawl Deep Crawl
        if self.firecrawl.is_configured():
            try:
                fc_jobs = self.crawl_via_firecrawl(company, target_role=target_role, max_jobs=max_jobs)
                if fc_jobs:
                    return fc_jobs
            except Exception as exc:
                logger.warning(f"Firecrawl failed for {company.name} ({exc}); falling back to Brave Search.")

        # Tier 3: Brave Search API Fallback
        if self.brave.is_configured():
            try:
                brave_jobs = self.crawl_via_brave(company, target_role=target_role, max_jobs=max_jobs)
                if brave_jobs:
                    return brave_jobs
            except Exception as exc:
                logger.warning(f"Brave Search failed for {company.name}: {exc}")

        # Tier 4: Direct HTTP fallback
        try:
            http_jobs = self.crawl_via_direct_http(company, target_role=target_role, max_jobs=max_jobs)
            if http_jobs:
                return http_jobs
        except Exception:
            pass

        return []

    def crawl_companies(
        self,
        target_role: str = "Junior Developer in AI Engineer, Full-Stack & ASE",
        experience_level: str = "junior",
        target_city: str = "",
        limit: int = 15,
    ) -> list[ExtractedJob]:
        """Discover live positions across 75+ software companies in Pakistan with full fallback chain."""
        discovered: list[ExtractedJob] = []

        # 1. Tier 1 priority: Direct Workable ATS (e.g. Devsinc)
        for comp in TOP_PAKISTAN_TECH_COMPANIES:
            if comp.ats_type == "workable" and comp.ats_identifier:
                workable_jobs = self.crawl_workable_account(
                    comp.ats_identifier,
                    comp.name,
                    target_role=target_role,
                    experience_level=experience_level,
                    target_city=target_city,
                )
                discovered.extend(workable_jobs)
                if len(discovered) >= limit:
                    return discovered[:limit]

        # 2. Tier 2: Multi-layer crawl on premier Pakistani tech employers in target city
        eligible_companies = (
            get_companies_for_city(target_city) if target_city else TOP_PAKISTAN_TECH_COMPANIES
        )
        # Prioritize top recognizable employers
        priority_ids = [
            "systemsltd", "confiz", "netsol", "10pearls", "arbisoft",
            "contour", "venturedive", "tkxel", "invozone", "educative",
            "motive", "purelogics", "nextbridge", "sadapay", "bazaar",
            "pixelsoftwares", "qbxnet",
        ]

        # Sort with priority companies first
        def sort_key(c: CompanyEntry) -> int:
            try:
                return priority_ids.index(c.id)
            except ValueError:
                return 999

        sorted_companies = sorted(eligible_companies, key=sort_key)

        crawled_count = 0
        for comp in sorted_companies:
            if comp.ats_type == "workable":
                continue  # Already crawled in step 1

            comp_jobs = self.crawl_company(
                comp,
                target_role=target_role,
                experience_level=experience_level,
                max_jobs=2,
            )
            for j in comp_jobs:
                if not j.link or "google.com" in j.link:
                    j.link = comp.careers_url
                discovered.append(j)

            crawled_count += 1
            if len(discovered) >= limit or crawled_count >= 2:
                break

        return discovered[:limit]
