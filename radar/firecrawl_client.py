"""Firecrawl API integration for deep job page crawling & verification."""

from __future__ import annotations

import logging
from typing import Any
import httpx

from radar import config

logger = logging.getLogger("radar.firecrawl")


class FirecrawlClient:
    """Client for Firecrawl web scraper API (https://firecrawl.dev)."""

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = (api_key or config.FIRECRAWL_API_KEY).strip()
        self.base_url = "https://api.firecrawl.dev/v1"

    def is_configured(self) -> bool:
        """Check if Firecrawl API key is loaded."""
        return bool(self.api_key)

    def scrape(self, url: str, timeout: float = 25.0) -> dict[str, Any] | None:
        """Scrape a job posting URL and return clean markdown and metadata."""
        if not self.is_configured():
            logger.debug("Firecrawl API key not set; skipping Firecrawl scrape.")
            return None

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "url": url,
            "formats": ["markdown"],
            "onlyMainContent": True,
            "waitFor": 1000,
        }

        try:
            with httpx.Client(timeout=timeout) as client:
                res = client.post(f"{self.base_url}/scrape", headers=headers, json=payload)
                if res.status_code == 200:
                    data = res.json()
                    return data.get("data", {})
                else:
                    logger.warning(f"Firecrawl scrape failed for {url} with status {res.status_code}: {res.text[:120]}")
                    return None
        except Exception as exc:
            logger.warning(f"Firecrawl scrape error for {url}: {exc}")
            return None
