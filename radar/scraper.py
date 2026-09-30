"""Direct HTTP scraper and HTML / JSON-LD parser."""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from bs4 import BeautifulSoup
import httpx

logger = logging.getLogger("radar.scraper")

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 (Radar JobHunter/1.0)"
)


def extract_jsonld(html: str) -> list[dict[str, Any]]:
    """Extract all JSON-LD blocks from HTML."""
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    blocks: list[dict[str, Any]] = []
    for script in soup.find_all("script", type="application/ld+json"):
        if script.string:
            try:
                parsed = json.loads(script.string.strip())
                if isinstance(parsed, dict):
                    blocks.append(parsed)
                elif isinstance(parsed, list):
                    blocks.extend([item for item in parsed if isinstance(item, dict)])
            except Exception:
                continue
    return blocks


def extract_clean_text(html: str) -> str:
    """Extract clean readable text from HTML, stripping non-content tags."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for element in soup(["script", "style", "noscript", "svg", "header", "footer", "nav", "aside"]):
        element.decompose()

    # Convert paragraphs and breaks to newlines for readable JD text
    for br in soup.find_all(["br", "p", "div", "li", "h1", "h2", "h3", "h4"]):
        br.append(" \n")

    text = soup.get_text(separator=" ", strip=True)
    # Normalize multiple blank lines and spaces
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


class Scraper:
    """Resilient HTTP scraper for job postings."""

    def __init__(self, timeout_s: float = 12.0, user_agent: str = DEFAULT_USER_AGENT) -> None:
        self.timeout_s = timeout_s
        self.headers = {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }

    def fetch(self, url: str) -> tuple[str, str, int]:
        """Fetch URL. Returns (final_url, html_content, status_code)."""
        try:
            with httpx.Client(
                timeout=self.timeout_s,
                follow_redirects=True,
                max_redirects=5,
                headers=self.headers,
            ) as client:
                res = client.get(url)
                return str(res.url), res.text, res.status_code
        except Exception as exc:
            logger.warning(f"Failed to fetch {url}: {exc}")
            return url, "", 0
