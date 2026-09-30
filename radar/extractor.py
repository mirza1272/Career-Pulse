"""Extraction logic for job postings: metadata, full JD, apply email, and deadline."""

from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
import logging
import re
from typing import Any
from bs4 import BeautifulSoup

from radar.scraper import extract_clean_text, extract_jsonld

logger = logging.getLogger("radar.extractor")

EMAIL_REGEX = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")

# Ignored email prefixes and noise
IGNORED_EMAIL_DOMAINS = {"example.com", "yourcompany.com", "domain.com", "placeholder.com"}
IGNORED_EMAIL_PREFIXES = {"support", "privacy", "legal", "security", "noreply", "no-reply", "sales", "press", "billing"}


@dataclass
class ExtractedJob:
    title: str
    company: str
    location: str
    link: str
    jd_text: str
    email: str | None
    has_email: bool
    deadline: dt.datetime | None
    source: str = "web"
    posted_at: str | None = None
    published_at: dt.datetime | None = None


def _clean_str(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, str):
        return val.strip()
    if isinstance(val, dict):
        for k in ("name", "value", "text"):
            if k in val:
                return _clean_str(val[k])
        return ""
    if isinstance(val, (list, tuple)):
        return ", ".join(_clean_str(x) for x in val if x)
    return str(val).strip()


def parse_datetime(raw: str) -> dt.datetime | None:
    """Parse ISO or common date string into UTC datetime."""
    if not raw:
        return None
    clean = raw.strip().rstrip(".,;:!)(\"'")
    if not clean:
        return None

    # Try ISO formats
    iso_clean = clean.replace("Z", "+00:00")
    try:
        parsed_iso = dt.datetime.fromisoformat(iso_clean)
        return parsed_iso if parsed_iso.tzinfo else parsed_iso.replace(tzinfo=dt.timezone.utc)
    except Exception:
        pass

    # Try standard date formats
    for fmt in (
        "%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y",
        "%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y",
        "%d %B %Y", "%d %b %Y", "%d-%b-%Y", "%d-%B-%Y"
    ):
        try:
            parsed = dt.datetime.strptime(clean[:25].strip(), fmt)
            return parsed.replace(tzinfo=dt.timezone.utc)
        except Exception:
            continue
    return None


def extract_emails_from_text(text: str) -> list[str]:
    """Find and filter valid apply/recruitment email addresses from text."""
    if not text:
        return []
    matches = EMAIL_REGEX.findall(text)
    candidates: list[str] = []
    for m in matches:
        m_lower = m.lower().strip(".")
        # Skip image file extensions matched accidentally
        if any(m_lower.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")):
            continue
        user, _, domain = m_lower.partition("@")
        if not domain or domain in IGNORED_EMAIL_DOMAINS:
            continue
        if user in IGNORED_EMAIL_PREFIXES:
            continue
        if len(user) > 60 or len(domain) > 100:
            continue
        if m_lower not in candidates:
            candidates.append(m_lower)

    # Sort so hiring-related emails appear first
    priority_keywords = ("career", "job", "apply", "hire", "recruitment", "talent", "hr")
    candidates.sort(key=lambda e: (0 if any(k in e for k in priority_keywords) else 1))
    return candidates


def parse_deadline_from_text(text: str) -> dt.datetime | None:
    """Extract application deadline from text using heuristics."""
    patterns = [
        r"(?:deadline|apply before|closing date|valid through|applications close)[:\s]+([A-Za-z0-9, -/]+)",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            snippet = m.group(1).strip()[:30]
            parsed = parse_datetime(snippet)
            if parsed:
                return parsed
    return None


def extract_from_jsonld_block(block: dict[str, Any], page_url: str) -> ExtractedJob | None:
    """Extract job attributes from a schema.org/JobPosting JSON-LD block."""
    b_type = _clean_str(block.get("@type", "")).lower()
    if "jobposting" not in b_type:
        return None

    title = _clean_str(block.get("title", ""))
    company = _clean_str(block.get("hiringOrganization", ""))
    raw_desc = _clean_str(block.get("description", ""))

    # Strip HTML tags from description if present
    clean_desc = BeautifulSoup(raw_desc, "html.parser").get_text(separator=" ", strip=True) if "<" in raw_desc else raw_desc

    # Location
    location = ""
    job_loc = block.get("jobLocation")
    if isinstance(job_loc, list):
        job_loc = job_loc[0] if job_loc else None
    if isinstance(job_loc, dict):
        addr = job_loc.get("address", {})
        if isinstance(addr, dict):
            parts = [_clean_str(addr.get(k)) for k in ("addressLocality", "addressRegion", "addressCountry")]
            location = ", ".join(p for p in parts if p)
        else:
            location = _clean_str(addr)

    # Apply link (or fallback to page url)
    link = _clean_str(block.get("url", "")) or page_url

    # Deadline
    valid_through = block.get("validThrough")
    deadline = parse_datetime(_clean_str(valid_through)) if valid_through else None

    # Search for emails in description
    emails = extract_emails_from_text(clean_desc)
    apply_email = emails[0] if emails else None

    if title and len(clean_desc) > 50:
        return ExtractedJob(
            title=title,
            company=company or "Confidential",
            location=location or "Remote",
            link=link,
            jd_text=clean_desc,
            email=apply_email,
            has_email=bool(apply_email),
            deadline=deadline,
            source="jsonld",
        )
    return None


def extract_job_from_html(html: str, url: str) -> ExtractedJob | None:
    """Full extraction ladder: JSON-LD first, then heuristic fallback."""
    if not html:
        return None

    # 1. Try JSON-LD schema
    blocks = extract_jsonld(html)
    for b in blocks:
        extracted = extract_from_jsonld_block(b, url)
        if extracted:
            return extracted

    # 2. Heuristic fallback
    soup = BeautifulSoup(html, "html.parser")
    clean_desc = extract_clean_text(html)
    if len(clean_desc) < 80:
        return None

    # Title detection
    title = ""
    h1 = soup.find("h1")
    if h1 and len(h1.get_text(strip=True)) > 3:
        title = h1.get_text(strip=True)
    elif soup.title:
        title = soup.title.get_text(strip=True).split("|")[0].split(" - ")[0].strip()

    # Company detection
    company = ""
    og_site = soup.find("meta", property="og:site_name")
    if og_site and og_site.get("content"):
        company = str(og_site["content"]).strip()
    if not company and soup.title:
        parts = re.split(r"[-–—|@]", soup.title.get_text(strip=True))
        if len(parts) > 1:
            company = parts[-1].strip()

    # Location
    location = "Remote"
    loc_meta = soup.find(attrs={"class": re.compile(r"location", re.I)})
    if loc_meta:
        location = loc_meta.get_text(strip=True)

    # Email mining
    emails = extract_emails_from_text(clean_desc)
    apply_email = emails[0] if emails else None

    # Deadline mining
    deadline = parse_deadline_from_text(clean_desc)

    return ExtractedJob(
        title=title or "Software Engineer",
        company=company or "Hiring Employer",
        location=location,
        link=url,
        jd_text=clean_desc,
        email=apply_email,
        has_email=bool(apply_email),
        deadline=deadline,
        source="heuristics",
    )


def extract_via_firecrawl(url: str) -> ExtractedJob | None:
    """Deep crawl job page using Firecrawl API if configured."""
    from radar.firecrawl_client import FirecrawlClient
    fc = FirecrawlClient()
    if not fc.is_configured():
        return None
    data = fc.scrape(url)
    if not data or not data.get("markdown"):
        return None
    md = data.get("markdown", "")
    meta = data.get("metadata", {}) or {}
    title = meta.get("title") or meta.get("ogTitle") or ""
    emails = extract_emails_from_text(md)
    apply_email = emails[0] if emails else None
    deadline = parse_deadline_from_text(md)
    return ExtractedJob(
        title=title or "Software Engineer",
        company=meta.get("siteName") or meta.get("ogSiteName") or "",
        location="Remote",
        link=url,
        jd_text=md,
        email=apply_email,
        has_email=bool(apply_email),
        deadline=deadline,
        source="firecrawl",
    )

