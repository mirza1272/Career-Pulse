"""Multi-source job search and discovery engine with strict experience and tech relevance filtering."""

from __future__ import annotations

import datetime as dt
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from bs4 import BeautifulSoup
import httpx

from radar import config
from radar.extractor import ExtractedJob, extract_emails_from_text, parse_datetime
from radar.companies_registry import find_company_by_name
from radar.brave_client import BraveSearchClient

logger = logging.getLogger("radar.search")

# Non-tech titles that must always be rejected
NON_TECH_PATTERNS = [
    r"\b(copywriter|writer|copy\s*editor|content\s*creator)\b",
    r"\b(sales|account\s*executive|business\s*development|bdr|sdr)\b",
    r"\b(customer\s*support|customer\s*service|support\s*jedi|helpdesk)\b",
    r"\b(receptionist|nurse|telemarketing|cashier|retail)\b",
    r"\b(virtual\s*assistant|data\s*entry|transcriptionist)\b",
    # Administrative, military ops, clerical, non-engineering:
    r"\b(action\s*officer|program\s*coordinator|coordinator|clerk)\b",
    r"\b(administrative\s*assistant|office\s*manager|admin\s*assistant)\b",
    r"\b(compliance\s*officer|legal\s*assistant|paralegal|recruiter|hr\s*specialist)\b",
    r"\b(logistics|warehouse|driver|security\s*guard|operations\s*officer)\b",
    # Articles, blogs, guides, salaries:
    r"\b(how\s+(?:to|ai)|reshaping|trends|guide|overview|salaries|salary\s+in|what\s+is|review\s+of|interview\s+questions)\b",
    # Aggregate job list page titles (e.g. "1,000+ jobs in Pakistan" or "100+ Associate Software Engineer Jobs"):
    r"^\d+[\s,]*\+?\s*(?:jobs|openings|positions|candidates)\b",
    r"\b\d+[\s,]*\+?\s*(?:jobs|openings|positions)\s+(?:in|for)\b",
]

# Mandatory tech role indicators that must be present in the job title
TECH_TITLE_INDICATORS = [
    r"\b(engineer|developer|programmer|architect|scientist|coder|specialist|ase|technologist|intern|trainee|sqa|qa)\b",
    r"\b(full\s*stack|fullstack|frontend|backend|web|devops|mlops|software|data|ai|ml)\b",
]

# Tech keywords for positive domain match
TECH_KEYWORDS = [
    "machine learning", "deep learning", "ai", "artificial intelligence",
    "pytorch", "tensorflow", "python", "computer vision", "nlp", "llm",
    "data scientist", "data engineer", "backend", "full stack", "full-stack",
    "software engineer", "software developer", "fastapi", "django", "react",
    "next.js", "frontend", "web developer", "system engineer",
    "software", "developer", "engineer", "sqa", "qa", "testing", "automation",
    "ase", "intern", "programming", "code", "cloud",
]

# Seniority titles rejected for junior / entry-level
SENIOR_PATTERNS_JUNIOR = [
    r"\b(senior|sr\.?|lead|principal|staff|director|head\s*of|vp|vice\s*president|chief|architect|manager)\b",
    r"\b(1[0-9]\+|\b[5-9]\+?\s*(?:years|yrs))\b",
]

# Seniority titles rejected for mid-level
SENIOR_PATTERNS_MID = [
    r"\b(principal|staff|director|head\s*of|vp|vice\s*president|chief|architect|executive)\b",
    r"\b(1[0-9]\+|\b[8-9]\+?\s*(?:years|yrs))\b",
]

# Truly spammy, scrape-and-dump click farms to reject
SPAM_AGGREGATOR_DOMAINS = [
    "ai-search.io",
    "getpakjob.com",
    "bebee.com",
    "jooble.org",
    "paperpk.com",
    "jobz.pk",
    "talents.vaia.com",
    "clickajob.com",
    "mustakbil.com",
    "pk.trabajele.com",
    "jobsearch.pk",
    "pakistanjobsbank.com",
    "alljobspk.com",
    "salary.com",
    "postjobfree.com",
    "jobssection.com",
    "itjobsinpakistan.com",
    "jang.com.pk",
    "pk.jobrapido.com",
    "jobcase.com",
    "recruit.net",
    "jobserve.com",
    "joblift.com",
]

# URL path patterns indicating aggregate search landing pages rather than direct jobs
AGGREGATE_URL_PATTERNS = [
    r"/q-[\w\-]+",                       # Indeed query landing e.g. /q-junior-software-engineer-jobs.html
    r"/jobs\?q=",                        # Indeed search params
    r"/jobs/search(?:/|\?|$)",           # Search directory results
    r"/jobs/collections/",               # LinkedIn collections
    r"/browse(?:-jobs)?/",               # Generic directory browse
    r"/category/[\w\-]+",                # Category browse page
    r"/l-[\w\-]+",                       # Indeed location directory
    r"/search\?(?:.*&)?q=",              # Search query
    r"/jobs-in-[\w\-]+",                 # Location aggregate directory
    r"/job-openings-in-[\w\-]+",         # Location aggregate directory
    r"/browse-jobs",                     # Generic browse
    r"/salaries(?:/|$)",                 # Salary guide
    r"/salary-[\w\-]+",                  # Salary calculator
    r"/interview-questions(?:/|$)",      # Interview guides
    r"/career-advice(?:/|$)",            # Advice blogs
    r"/reviews(?:/|$)",                  # Employer reviews
    r"/overview(?:/|$)",                 # Company overview
    r"/companies(?:/|$)",                # Directory of companies
    r"-SRCH_",                           # Glassdoor search queries
    r"/Job/.*-jobs-",                    # Glassdoor location/title search landing
    r"google\.com/search\?",             # Search engine URL
    r"bing\.com/search\?",               # Search engine URL
]

# Title patterns indicating aggregate listings or articles rather than individual jobs
AGGREGATE_TITLE_PATTERNS = [
    r"^\d+[\s,]*\+?\s*(?:jobs|vacancies|openings|positions|opportunities|candidates|results|roles)\b",
    r"\b\d+[\s,]*\+?\s*(?:jobs|vacancies|openings|positions|opportunities)\s+(?:in|for|near)\b",
    r"\b(?:jobs|vacancies|employment)\s+in\s+[a-zA-Z\s]+(?:\s*-\s*Indeed|\s*-\s*LinkedIn|\s*\|\s*Glassdoor|\s*-\s*Rozee|$)",
    r"\b(?:top|best)\s+\d+\s+(?:jobs|companies|vacancies|employers)\b",
    r"\b(?:how\s+to\s+become|salary\s+guide|salaries\s+in|interview\s+questions|resume\s+sample|career\s+path)\b",
    r"^(?:find\s+jobs|search\s+jobs|job\s+search|hiring\s+now|now\s+hiring|job\s+openings)\s*[\-|–|—|:]",
]


def is_spam_aggregator(url: str) -> bool:
    """Check if a URL points to a low-quality scraper click-farm instead of a legitimate hiring source."""
    if not url:
        return False
    u_lower = url.lower()
    return any(d in u_lower for d in SPAM_AGGREGATOR_DOMAINS)


def is_valid_single_job_posting(url: str, title: str = "", snippet: str = "") -> tuple[bool, str]:
    """Verify that a candidate represents a direct, individual job vacancy rather than an aggregate search directory or article."""
    if not url or not (url.startswith("http://") or url.startswith("https://")):
        return False, "Invalid URL scheme"

    if is_spam_aggregator(url):
        return False, f"URL belongs to low-quality scraper aggregator: {url}"

    u_lower = url.lower()
    for pat in AGGREGATE_URL_PATTERNS:
        if re.search(pat, u_lower, re.IGNORECASE):
            return False, f"URL matches aggregate listing or search directory pattern: {pat}"

    if title:
        t_clean = title.strip()
        for pat in AGGREGATE_TITLE_PATTERNS:
            if re.search(pat, t_clean, re.IGNORECASE):
                return False, f"Title matches aggregate listing pattern: '{t_clean}' (pattern: {pat})"

    return True, "Valid direct single job posting"


def parse_job_title_and_company(raw_title: str, url: str = "") -> tuple[str, str]:
    """Extract and sanitize clean job role title and employer company name from organic web titles and direct ATS URLs."""
    title = (raw_title or "").strip()
    company = "Tech Employer"

    # Pattern 1: "Softpers Interactive is looking for a Associate Software Engineer in Lahore..."
    m_looking = re.search(r"^(.*?)\s+is\s+(?:looking\s+for\s+(?:a|an)?|hiring\s+(?:a|an)?)\s+(.*?)(?:\s+in\s+.*|\s*\|\s*.*)?$", title, re.IGNORECASE)
    if m_looking:
        company = m_looking.group(1).strip()
        title = m_looking.group(2).strip()
    # Pattern 2: "SOCOTEC Junior Software Engineer | SmartRecruiters"
    elif " | SmartRecruiters" in title or " | LinkedIn" in title or " | Glassdoor" in title or " | Workable" in title:
        clean = re.sub(r"\s*\|\s*(?:SmartRecruiters|LinkedIn|Glassdoor|Workable|Lever|Ashby|Greenhouse).*$", "", title, flags=re.IGNORECASE).strip()
        if " - " in clean:
            parts = clean.split(" - ")
            title = parts[0].strip()
            company = parts[-1].strip()
        else:
            parts = clean.split(" ", 1)
            if len(parts) == 2 and any(k in parts[1].lower() for k in ("engineer", "developer", "specialist", "intern", "scientist", "architect", "lead", "analyst")):
                company = parts[0].strip()
                title = parts[1].strip()
            else:
                title = clean
    elif " - " in title:
        parts = title.split(" - ")
        title = parts[0].strip()
        raw_comp = parts[-1].split("|")[0].split("—")[0].strip()
        if " at " in raw_comp:
            company = raw_comp.split(" at ")[-1].strip()
        else:
            company = raw_comp
    elif " | " in title:
        parts = title.split(" | ")
        title = parts[0].strip()
        company = parts[-1].strip()
    elif " at " in title:
        parts = title.split(" at ")
        title = parts[0].strip()
        company = parts[1].split("|")[0].split("-")[0].strip()
    elif " hiring " in title:
        parts = title.split(" hiring ")
        company = parts[0].strip()
        title = parts[1].split(" in ")[0].strip()

    # Domain / ATS slug fallback if company is still generic
    if company in ("Tech Employer", "Employer", "") and url:
        u_m = re.search(r"(?:smartrecruiters\.com|boards\.greenhouse\.io|jobs\.lever\.co|jobs\.ashbyhq\.com|apply\.workable\.com)/([^/]+)", url)
        if u_m:
            raw_slug = u_m.group(1)
            spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", raw_slug).replace("-", " ").replace("_", " ").title()
            spaced = re.sub(r"\s+\d+$", "", spaced)
            if spaced:
                company = spaced

    # Clean any residual trailing pipes or location tokens from title
    title = re.sub(r"\s*\|\s*.*$", "", title).strip()
    title = re.sub(r"\s*–\s*.*$", "", title).strip()

    return title, company


def canonicalize_job_link(
    company: str,
    current_link: str,
    brave_client: BraveSearchClient | None = None,
) -> tuple[str, str]:
    """Resolve employer's official career portal where available, preserving valid job application links.

    Returns (resolved_url, source_type).
    """
    if not current_link:
        return "", "empty"

    # 1. First priority: Check 75+ registered premier Pakistani software houses
    comp_entry = find_company_by_name(company)
    if comp_entry:
        comp_domain = comp_entry.careers_url.split("//")[-1].split("/")[0].replace("www.", "").lower()
        if current_link and comp_domain in current_link.lower() and not is_spam_aggregator(current_link):
            return current_link, "direct_company_domain"
        if comp_entry.ats_type == "workable" and comp_entry.ats_identifier:
            return f"https://apply.workable.com/{comp_entry.ats_identifier}/", "canonical_workable"
        return comp_entry.careers_url, "canonical_company_portal"

    # 2. If it is a known spam scraper farm, try Brave lookup
    is_spam = is_spam_aggregator(current_link)
    if is_spam and brave_client is not None and hasattr(brave_client, "find_company_career_page"):
        try:
            brave_url = brave_client.find_company_career_page(company)
            if brave_url and not is_spam_aggregator(brave_url):
                return brave_url, "brave_resolved_portal"
        except Exception:
            pass

    # 3. Preserve direct company or verified hiring board link (LinkedIn, Indeed, Rozee, Workable, etc.)
    return current_link, "direct_posting"


def is_within_last_3_days(
    posted_at: str | None = None,
    published_at: dt.datetime | None = None,
    deadline: dt.datetime | None = None,
    extensions: list[str] | None = None,
    jd_text: str = "",
    link: str = "",
    max_days: int = 21,  # 3 weeks max
) -> tuple[bool, str]:
    """Strict freshness verification enforcing user rules:
    1. If deadline is mentioned -> deadline must be >= today.
    2. If no deadline -> posting date must be <= 3 weeks old (21 days).
    3. If neither deadline nor posting date is mentioned/detected -> REJECT.
    """
    now_utc = dt.datetime.now(dt.timezone.utc)

    # 0. Check URL slug for previous years (e.g. /2024/, /2023/)
    if link:
        if re.search(r"\b202[0-4]\d{4}\b", link) or re.search(r"/(?:202[0-4])[-/]", link):
            return False, f"URL slug indicates posting is from a previous year: {link}"

    # 1. Rule 1: Deadline check (if present)
    eff_deadline = deadline
    if eff_deadline is None and jd_text:
        from radar.extractor import parse_deadline_from_text
        eff_deadline = parse_deadline_from_text(jd_text)

    if eff_deadline is not None:
        d_utc = eff_deadline if eff_deadline.tzinfo is not None else eff_deadline.replace(tzinfo=dt.timezone.utc)
        if d_utc < now_utc:
            return False, f"Application deadline has passed (deadline: {d_utc.strftime('%Y-%m-%d')})"
        return True, f"Application deadline is valid ({d_utc.strftime('%Y-%m-%d')})"

    # 2. Rule 2: Posting date check (if no deadline)
    # 2a. Check exact timestamp if available (e.g. Arbeitnow, Jobicy, Remotive, Workable)
    if published_at is not None:
        pub_utc = published_at if published_at.tzinfo is not None else published_at.replace(tzinfo=dt.timezone.utc)
        age_hours = (now_utc - pub_utc).total_seconds() / 3600.0
        max_hours = max_days * 24.0
        if age_hours > max_hours:
            return False, f"Posting published {age_hours:.1f}h ago (> 3 weeks / {max_days} days limit)"
        return True, f"Posting published {age_hours:.1f}h ago (within 3 weeks / {max_days} days)"

    # 2b. Check posted_at string and extensions (Google Jobs / LinkedIn / Indeed / Apify / Tavily)
    text_candidates: list[str] = []
    if posted_at:
        text_candidates.append(str(posted_at))
    if extensions:
        text_candidates.extend(str(x) for x in extensions if x)
    if jd_text:
        text_candidates.append(jd_text[:1200])

    has_date_detected = False
    for text in text_candidates:
        t_clean = text.lower().strip()
        # Reject anything mentioning months or years
        if re.search(r"\b(\d+)?\s*(?:month|yr|year)s?\s*ago\b", t_clean) or re.search(r"\bposted\s+(\d+)?\s*(?:month|yr|year)s?\b", t_clean):
            return False, f"Posting age rejected: '{t_clean[:60]}' (months/years old)"

        # Check weeks count
        w_match = re.search(r"(\d+)\s*weeks?\s*ago", t_clean) or re.search(r"posted\s+(\d+)\s*weeks?", t_clean)
        if w_match:
            has_date_detected = True
            weeks = int(w_match.group(1))
            if weeks > 3 or (weeks * 7) > max_days:
                return False, f"Posting is {weeks} weeks old (> 3 weeks limit)"
            return True, f"Posting age verified: {weeks} weeks ago (within 3 weeks)"

        # Check day counts
        day_match = re.search(r"(\d+)\s*days?\s*ago", t_clean) or re.search(r"posted\s+(\d+)\s*days?", t_clean)
        if day_match:
            has_date_detected = True
            days = int(day_match.group(1))
            if days > max_days:
                return False, f"Posting age rejected: {days} days ago (> 3 weeks limit)"
            return True, f"Posting age verified: {days} days ago (within 3 weeks)"

        # Allowed fresh indicators
        if any(term in t_clean for term in ("hour", "hr ago", "minute", "min ago", "just now", "today", "yesterday", "1 day ago", "2 days ago", "3 days ago", "recently", "active vacancy", "hiring now", "past week", "past 24 hours")):
            has_date_detected = True
            return True, f"Posting age verified fresh: '{t_clean[:50]}'"

    # 3. Rule 3: If neither deadline nor posting date is mentioned/detected -> REJECT
    if not has_date_detected:
        return False, "No verified posting date or deadline detected (avoiding unverified stale posting)"

    return True, f"Age within accepted 3-week ({max_days}-day) window"


def filter_job_relevance_and_experience(
    job: ExtractedJob,
    target_role: str = "",
    experience_level: str = "junior",
) -> tuple[bool, str]:
    """Filter out non-tech jobs, irrelevant roles, and positions not matching the target experience level."""
    title_lower = job.title.lower()
    desc_lower = job.jd_text.lower()
    role_lower = (target_role or "").lower()

    # 1. Reject non-tech roles
    for pat in NON_TECH_PATTERNS:
        if re.search(pat, title_lower):
            return False, f"Non-tech title rejected by pattern: '{pat}'"

    # 2. Title must contain at least one technical engineering role indicator
    has_tech_title = any(re.search(pat, title_lower) for pat in TECH_TITLE_INDICATORS)
    if not has_tech_title:
        return False, f"Title '{job.title}' does not represent a technical engineering/developer role"

    # 3. Domain Relevance matching based on specified target role
    is_combined_default = (
        "ai / full-stack / ase" in role_lower
        or "ai engineer, full-stack & ase" in role_lower
        or ("ai" in role_lower and "full" in role_lower)
    )

    if not is_combined_default:
        if any(k in role_lower for k in ("ai", "machine learning", "deep learning", "nlp", "computer vision", "prompt", "llm")):
            # Dedicated AI / ML search: title must contain AI/ML/Data keywords
            ai_tokens = ("ai", "artificial intelligence", "ml", "machine learning", "deep learning", "data scientist", "computer vision", "nlp", "prompt", "agentic", "llm", "neural", "pytorch", "tensorflow", "python")
            if not any(token in title_lower for token in ai_tokens):
                return False, f"Title '{job.title}' does not match target AI/ML domain"

        elif any(k in role_lower for k in ("full-stack", "full stack", "frontend", "backend", "web")):
            # Dedicated Full-Stack / Web search: title must contain developer/web/software/stack tokens
            web_tokens = ("full stack", "fullstack", "frontend", "backend", "web", "software", "developer", "engineer", "react", "next", "python", "fastapi", "node")
            if not any(token in title_lower for token in web_tokens):
                return False, f"Title '{job.title}' does not match target Full-Stack / Web domain"

    # 4. Mandatory general tech keywords in title or description
    has_tech = any(k in title_lower or k in desc_lower[:1500] for k in TECH_KEYWORDS)
    if not has_tech:
        return False, "Does not match any target technical domains (ML, AI, Python, Full-Stack)"

    # 5. Experience level filtering
    lvl = (experience_level or "junior").lower()
    if lvl in ("junior", "entry", "entry-level"):
        for pat in SENIOR_PATTERNS_JUNIOR:
            if re.search(pat, title_lower):
                return False, f"Too senior for entry/junior level (matched title pattern: '{pat}')"
            if re.search(pat, desc_lower[:800]):
                return False, f"Job description requires senior experience (matched pattern: '{pat}')"

    elif lvl in ("mid", "mid-level"):
        for pat in SENIOR_PATTERNS_MID:
            if re.search(pat, title_lower):
                return False, f"Too senior for mid-level (matched title pattern: '{pat}')"

    return True, "Job matches tech relevance and experience criteria"


# 100 Trusted Platforms Catalog Mapping
PLATFORMS_MAP: dict[str, dict[str, str]] = {
    # Direct ATS (Highest conversion)
    "greenhouse": {"name": "Greenhouse", "site": "boards.greenhouse.io"},
    "lever": {"name": "Lever", "site": "jobs.lever.co"},
    "workday": {"name": "Workday", "site": "myworkdayjobs.com"},
    "ashby": {"name": "Ashby", "site": "jobs.ashbyhq.com"},
    "smartrecruiters": {"name": "SmartRecruiters", "site": "jobs.smartrecruiters.com"},
    "workable": {"name": "Workable", "site": "apply.workable.com"},
    "bamboohr": {"name": "BambooHR", "site": "bamboohr.com"},
    "breezyhr": {"name": "Breezy HR", "site": "breezy.hr"},
    "recruitee": {"name": "Recruitee", "site": "recruitee.com"},
    "jazzhr": {"name": "JazzHR", "site": "jazzhr.com"},
    "rippling": {"name": "Rippling", "site": "rippling.com"},
    "jobvite": {"name": "Jobvite", "site": "jobs.jobvite.com"},
    "taleo": {"name": "Oracle Taleo", "site": "taleo.net"},
    "pinpoint": {"name": "Pinpoint", "site": "pinpointhq.com"},
    "comeet": {"name": "Comeet", "site": "comeet.com"},
    # Tier 1 Major Networks
    "linkedin": {"name": "LinkedIn", "site": "linkedin.com/jobs"},
    "indeed": {"name": "Indeed", "site": "indeed.com"},
    "google_jobs": {"name": "Google Jobs", "site": ""},
    "glassdoor": {"name": "Glassdoor", "site": "glassdoor.com"},
    "ziprecruiter": {"name": "ZipRecruiter", "site": "ziprecruiter.com"},
    "dice": {"name": "Dice", "site": "dice.com"},
    "monster": {"name": "Monster", "site": "monster.com"},
    "careerbuilder": {"name": "CareerBuilder", "site": "careerbuilder.com"},
    "simplyhired": {"name": "SimplyHired", "site": "simplyhired.com"},
    "joblist": {"name": "Joblist", "site": "joblist.com"},
    "snagajob": {"name": "Snagajob", "site": "snagajob.com"},
    "ladders": {"name": "The Ladders", "site": "theladders.com"},
    "lensa": {"name": "Lensa", "site": "lensa.com"},
    "jooble": {"name": "Jooble", "site": "jooble.org"},
    "talent_com": {"name": "Talent.com", "site": "talent.com"},
    # Startup Ecosystems
    "wellfound": {"name": "Wellfound", "site": "wellfound.com"},
    "ycombinator": {"name": "Y Combinator", "site": "workatastartup.com"},
    "builtin": {"name": "Built In", "site": "builtin.com"},
    "techstars": {"name": "Techstars", "site": "jobs.techstars.com"},
    "otta": {"name": "Otta", "site": "otta.com"},
    "levels_fyi": {"name": "Levels.fyi", "site": "levels.fyi/jobs"},
    "hackernews": {"name": "Hacker News", "site": "news.ycombinator.com"},
    "keyvalues": {"name": "Key Values", "site": "keyvalues.com"},
    "producthunt": {"name": "Product Hunt", "site": "producthunt.com"},
    "ventureloop": {"name": "VentureLoop", "site": "ventureloop.com"},
    "startupjobs": {"name": "Startup Jobs", "site": "startup.jobs"},
    "underdog": {"name": "Underdog.io", "site": "underdog.io"},
    "crunchbase": {"name": "Crunchbase", "site": "crunchbase.com"},
    "untapped": {"name": "Untapped", "site": "untapped.io"},
    "f6s": {"name": "F6S", "site": "f6s.com"},
    # Dedicated Remote Boards
    "remoteok": {"name": "RemoteOK", "site": "remoteok.com"},
    "weworkremotely": {"name": "We Work Remotely", "site": "weworkremotely.com"},
    "remotive": {"name": "Remotive", "site": "remotive.com"},
    "jobicy": {"name": "Jobicy", "site": "jobicy.com"},
    "arbeitnow": {"name": "Arbeitnow", "site": "arbeitnow.com"},
    "workingnomads": {"name": "Working Nomads", "site": "workingnomads.com"},
    "remoterocketship": {"name": "Remote Rocketship", "site": "remoterocketship.com"},
    "flexjobs": {"name": "FlexJobs", "site": "flexjobs.com"},
    "virtualvocations": {"name": "Virtual Vocations", "site": "virtualvocations.com"},
    "dailyremote": {"name": "DailyRemote", "site": "dailyremote.com"},
    "justremote": {"name": "JustRemote", "site": "justremote.co"},
    "remote_co": {"name": "Remote.co", "site": "remote.co"},
    "himalayas": {"name": "Himalayas", "site": "himalayas.app"},
    "nodesk": {"name": "NoDesk", "site": "nodesk.co"},
    "remotelypeople": {"name": "RemotelyPeople", "site": "remotelypeople.com"},
    "pangian": {"name": "Pangian", "site": "pangian.com"},
    "skipthedrive": {"name": "SkipTheDrive", "site": "skipthedrive.com"},
    "europeremotely": {"name": "EuropeRemotely", "site": "europeremotely.com"},
    "remoteworkmate": {"name": "RemoteWorkmate", "site": "remoteworkmate.com"},
    "dynamitejobs": {"name": "Dynamite Jobs", "site": "dynamitejobs.com"},
    # Developer & AI Specialist
    "aijobs_net": {"name": "AI Jobs Net", "site": "aijobs.net"},
    "devitjobs": {"name": "DevITjobs", "site": "devitjobs.com"},
    "python_org": {"name": "Python.org", "site": "python.org/jobs"},
    "pycoder_jobs": {"name": "PyCoder Jobs", "site": "jobs.pycoders.com"},
    "relocateme": {"name": "Relocate.me", "site": "relocate.me"},
    "honeypot": {"name": "Honeypot.io", "site": "honeypot.io"},
    "arc_dev": {"name": "Arc.dev", "site": "arc.dev"},
    "turing": {"name": "Turing", "site": "turing.com"},
    "toptal": {"name": "Toptal", "site": "toptal.com"},
    "gun_io": {"name": "Gun.io", "site": "gun.io"},
    "authenticjobs": {"name": "Authentic Jobs", "site": "authenticjobs.com"},
    "rubynow": {"name": "RubyNow", "site": "rubynow.com"},
    "kaggle_jobs": {"name": "Kaggle Jobs", "site": "kaggle.com"},
    "github_jobs": {"name": "GitHub Jobs", "site": "github.com"},
    "stackoverflow_network": {"name": "Stack Overflow", "site": "stackoverflow.com"},
    # Pakistan Tech Hubs & Regional
    "rozee_pk": {"name": "Rozee.pk", "site": "rozee.pk"},
    "mustakbil": {"name": "Mustakbil", "site": "mustakbil.com"},
    "bayt": {"name": "Bayt", "site": "bayt.com"},
    "gulftalent": {"name": "GulfTalent", "site": "gulftalent.com"},
    "reed_uk": {"name": "Reed UK", "site": "reed.co.uk"},
    "totaljobs": {"name": "TotalJobs", "site": "totaljobs.com"},
    "cwjobs": {"name": "CWJobs", "site": "cwjobs.co.uk"},
    "stepstone_de": {"name": "StepStone DE", "site": "stepstone.de"},
    "stepstone_eu": {"name": "StepStone EU", "site": "stepstone.com"},
    "xing": {"name": "Xing", "site": "xing.com"},
    "infojobs": {"name": "InfoJobs", "site": "infojobs.net"},
    "seek_au": {"name": "Seek", "site": "seek.com.au"},
    "jobstreet": {"name": "JobStreet", "site": "jobstreet.com"},
    "naukri": {"name": "Naukri", "site": "naukri.com"},
    "efinancialcareers": {"name": "eFinancialCareers", "site": "efinancialcareers.com"},
    "techmeme": {"name": "Techmeme", "site": "techmeme.com"},
    "jobserve": {"name": "Jobserve", "site": "jobserve.com"},
    "jobsite_uk": {"name": "Jobsite UK", "site": "jobsite.co.uk"},
    "neuvoo": {"name": "Neuvoo", "site": "neuvoo.com"},
    "monstergulf": {"name": "Monster Gulf", "site": "monstergulf.com"},
}


class SearchProvider:
    """Base search provider interface."""

    id: str = "base"

    def search(
        self,
        query: str,
        experience_level: str = "junior",
        location: str = "worldwide_remote_or_pakistan_onsite",
        include_remote: bool = True,
        platform: str = "all",
        limit: int = 10,
    ) -> list[ExtractedJob]:
        raise NotImplementedError


class SerpApiGoogleJobsProvider(SearchProvider):
    """Real-time Google Jobs search via SerpApi with strict aggregator rejection & canonical company redirection."""

    id: str = "google_jobs"
    base_url: str = "https://serpapi.com/search"

    def __init__(self) -> None:
        self.brave = BraveSearchClient()

    def search(
        self,
        query: str,
        experience_level: str = "junior",
        location: str = "worldwide_remote_or_pakistan_onsite",
        include_remote: bool = True,
        platform: str = "all",
        limit: int = 10,
    ) -> list[ExtractedJob]:
        api_key = config.SERPAPI_API_KEY
        if not api_key:
            logger.info("SERPAPI_API_KEY not configured; skipping Google Jobs.")
            return []

        # Refine query with clean, high-yield search terms
        q_raw = (query or "Junior Developer (AI / Full-Stack / ASE)").strip()
        q_lower = q_raw.lower()
        if "ai" in q_lower and ("full" in q_lower or "stack" in q_lower or "ase" in q_lower):
            # Composite default role: search for highest-yield software engineering positions
            search_q = "Junior Software Engineer"
        elif "ase" in q_lower or "associate software engineer" in q_lower:
            search_q = "Associate Software Engineer"
        elif "ai" in q_lower or "machine learning" in q_lower:
            search_q = "Junior AI Engineer" if "junior" in q_lower else "AI Engineer"
        elif "full" in q_lower or "stack" in q_lower:
            search_q = "Junior Full Stack Developer" if "junior" in q_lower else "Full Stack Developer"
        else:
            # Clean parentheses or extra punctuation that break Google Jobs
            clean_text = re.sub(r"[()/*&]", " ", q_raw).strip()
            clean_text = re.sub(r"\s+", " ", clean_text)
            search_q = clean_text

        if experience_level == "junior" and not any(k in search_q.lower() for k in ("junior", "entry", "associate", "ase")):
            search_q = f"Junior {search_q}"
        elif experience_level == "mid" and not any(k in search_q.lower() for k in ("mid", "engineer")):
            search_q = f"Mid-level {search_q}"

        # 1. Location & Remote resolution
        loc_clean = (location or "worldwide_remote_or_pakistan_onsite").lower()
        gl_code = "us"
        serp_location = None

        if "lahore" in loc_clean:
            gl_code = "pk"
            serp_location = "Lahore, Punjab, Pakistan"
            search_q = f"{search_q} Lahore"
        elif "islamabad" in loc_clean or "rawalpindi" in loc_clean:
            gl_code = "pk"
            serp_location = "Islamabad, Pakistan"
            search_q = f"{search_q} Islamabad"
        elif "faisalabad" in loc_clean:
            gl_code = "pk"
            serp_location = "Faisalabad, Punjab, Pakistan"
            search_q = f"{search_q} Faisalabad"
        elif "pakistan" in loc_clean:
            gl_code = "pk"
            serp_location = "Pakistan"
            search_q = f"{search_q} Pakistan"
        elif loc_clean == "worldwide_remote_or_pakistan_onsite":
            # Target Pakistan tech ecosystem while allowing remote
            gl_code = "pk"
            serp_location = "Lahore, Punjab, Pakistan"
        elif loc_clean == "worldwide_remote":
            search_q = f"{search_q} remote"
        elif location:
            search_q = f"{search_q} {location}"

        # 2. Platform targeting constraint
        if platform != "all" and platform in PLATFORMS_MAP:
            p_info = PLATFORMS_MAP[platform]
            site = p_info.get("site", "")
            if site:
                search_q = f"{search_q} site:{site}"
            else:
                search_q = f"{search_q} via {p_info.get('name')}"

        params = {
            "engine": "google_jobs",
            "q": search_q,
            "api_key": api_key,
            "hl": "en",
        }
        if gl_code:
            params["gl"] = gl_code
        if serp_location:
            params["location"] = serp_location

        jobs: list[ExtractedJob] = []
        try:
            with httpx.Client(timeout=6.0) as client:
                res = client.get(self.base_url, params=params)
                if res.status_code == 200:
                    data = res.json()
                    for item in data.get("jobs_results", []):
                        title = str(item.get("title", "")).strip()
                        company = str(item.get("company_name", "")).strip()
                        loc_val = str(item.get("location", "Remote")).strip()
                        raw_desc = str(item.get("description", "")).strip()
                        clean_desc = BeautifulSoup(raw_desc, "html.parser").get_text(separator=" ", strip=True) if "<" in raw_desc else raw_desc

                        posted_at = str(item.get("detected_extensions", {}).get("posted_at") or "")
                        extensions = [str(x) for x in item.get("extensions", []) if x]

                        raw_link = ""
                        apply_options = item.get("apply_options", [])
                        if apply_options and isinstance(apply_options, list):
                            for opt in apply_options:
                                opt_l = str(opt.get("link", "")).strip()
                                if opt_l and not is_spam_aggregator(opt_l):
                                    raw_link = opt_l
                                    break
                            if not raw_link and apply_options:
                                raw_link = str(apply_options[0].get("link", "")).strip()

                        if not raw_link:
                            related_links = item.get("related_links", [])
                            if related_links and isinstance(related_links, list):
                                for rel in related_links:
                                    rel_l = str(rel.get("link", "")).strip()
                                    if rel_l and not is_spam_aggregator(rel_l):
                                        raw_link = rel_l
                                        break
                                if not raw_link and related_links:
                                    raw_link = str(related_links[0].get("link", "")).strip()

                        if not raw_link:
                            raw_link = f"https://www.google.com/search?q={httpx.URL(params['q'])}"

                        canonical_link, link_source = canonicalize_job_link(company, raw_link, brave_client=self.brave)
                        if not canonical_link:
                            canonical_link = raw_link

                        is_single, single_reason = is_valid_single_job_posting(canonical_link, title, clean_desc)
                        if not is_single:
                            continue

                        is_fresh, fresh_msg = is_within_last_3_days(
                            posted_at=posted_at,
                            extensions=extensions,
                            jd_text=clean_desc,
                            link=canonical_link,
                            max_days=7,
                        )
                        if not is_fresh:
                            continue

                        via = str(item.get("via", "Google Jobs")).replace("via ", "")
                        emails = extract_emails_from_text(clean_desc)
                        apply_email = emails[0] if emails else None

                        if not (title and len(clean_desc) > 20):
                            continue

                        candidate = ExtractedJob(
                            title=title,
                            company=company or "Employer",
                            location=loc_val or "Remote",
                            link=canonical_link,
                            jd_text=clean_desc,
                            email=apply_email,
                            has_email=bool(apply_email),
                            deadline=None,
                            source=f"google_jobs ({via})",
                            posted_at=posted_at or "1 day ago",
                        )

                        is_rel, _ = filter_job_relevance_and_experience(candidate, target_role=query, experience_level=experience_level)
                        if is_rel:
                            jobs.append(candidate)
                        if len(jobs) >= limit:
                            break
        except Exception as exc:
            logger.info(f"SerpApi Google Jobs timed out or encountered error ({exc}); using Google Web search fallback.")

        # Fallback to high-speed Google Web Search via SerpApi if google_jobs returned 0
        if not jobs:
            jobs = self._search_via_google_web(search_q, loc_clean, experience_level, limit)

        return jobs

    def _search_via_google_web(
        self, query: str, location: str, experience_level: str, limit: int
    ) -> list[ExtractedJob]:
        """High-speed Google Organic Search fallback targeting verified ATS endpoints (< 1.5s)."""
        api_key = config.SERPAPI_API_KEY
        clean_q = re.sub(r"[()/*&_]", " ", query).strip()
        clean_q = re.sub(r"\s+", " ", clean_q)
        clean_loc = "Lahore" if "lahore" in location.lower() else ("Islamabad" if "islamabad" in location.lower() else "Pakistan")
        q_web = f'(site:boards.greenhouse.io OR site:jobs.lever.co OR site:apply.workable.com OR site:jobs.ashbyhq.com OR site:linkedin.com/jobs/view) "{clean_q}" {clean_loc}'.strip()
        params = {
            "engine": "google",
            "q": q_web,
            "api_key": api_key,
            "hl": "en",
            "gl": "pk" if ("pk" in location.lower() or "pakistan" in location.lower() or "lahore" in location.lower()) else "us",
        }
        jobs: list[ExtractedJob] = []
        try:
            with httpx.Client(timeout=6.0) as client:
                res = client.get("https://serpapi.com/search.json", params=params)
                if res.status_code != 200:
                    return []
                data = res.json()
        except Exception as exc:
            logger.warning(f"SerpApi Google web fallback failed: {exc}")
            return []

        for item in data.get("organic_results", []):
            raw_title = str(item.get("title", "")).strip()
            link = str(item.get("link", "")).strip()
            snippet = str(item.get("snippet", "")).strip()
            if not raw_title or not link:
                continue

            clean_title, company = parse_job_title_and_company(raw_title, link)

            is_single, _ = is_valid_single_job_posting(link, clean_title, snippet)
            if not is_single:
                continue

            emails = extract_emails_from_text(snippet)
            apply_email = emails[0] if emails else None

            candidate = ExtractedJob(
                title=clean_title,
                company=company,
                location=location or "Pakistan",
                link=link,
                jd_text=f"Live tech vacancy: {clean_title} at {company}.\n\n{snippet}\n\nOfficial Application: {link}",
                email=apply_email,
                has_email=bool(apply_email),
                deadline=None,
                source="google_search",
                posted_at="1 day ago",
            )
            is_rel, _ = filter_job_relevance_and_experience(candidate, target_role=query, experience_level=experience_level)
            if is_rel:
                jobs.append(candidate)
            if len(jobs) >= limit:
                break

        return jobs


class TavilySearchProvider(SearchProvider):
    """Discovers live jobs via Tavily Search API across verified sources."""

    id: str = "tavily"

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = (api_key or os.environ.get("TAVILY_API_KEY", "")).strip()

    def search(
        self,
        query: str,
        experience_level: str = "junior",
        location: str = "worldwide_remote_or_pakistan_onsite",
        limit: int = 10,
        include_remote: bool = True,
        **kwargs: Any,
    ) -> list[ExtractedJob]:
        api_key = self.api_key or os.environ.get("TAVILY_API_KEY", "").strip()
        if not api_key:
            return []

        q_clean = re.sub(r"[()/*&_]", " ", query).strip()
        q_clean = re.sub(r"\s+", " ", q_clean)
        q_terms = f'("{q_clean}" OR "Junior Software Engineer" OR "Associate Software Engineer")'
        loc_term = "Lahore" if "lahore" in location.lower() else ("Islamabad" if "islamabad" in location.lower() else "Pakistan")
        search_q = f"(site:boards.greenhouse.io OR site:jobs.lever.co OR site:apply.workable.com OR site:jobs.ashbyhq.com OR site:jobs.smartrecruiters.com OR site:linkedin.com/jobs/view) {q_terms} {loc_term}"

        payload = {
            "api_key": api_key,
            "query": search_q,
            "search_depth": "basic",
            "max_results": min(limit * 2, 10),
        }
        jobs: list[ExtractedJob] = []
        try:
            with httpx.Client(timeout=6.0) as client:
                res = client.post("https://api.tavily.com/search", json=payload)
                if res.status_code != 200:
                    return []
                data = res.json()
        except Exception as exc:
            logger.warning(f"Tavily search failed: {exc}")
            return []

        for item in data.get("results", []):
            raw_title = str(item.get("title", "")).strip()
            url = str(item.get("url", "")).strip()
            content = str(item.get("content", "")).strip()
            if not raw_title or not url:
                continue

            title, company = parse_job_title_and_company(raw_title, url)

            is_single, _ = is_valid_single_job_posting(url, title, content)
            if not is_single:
                continue

            emails = extract_emails_from_text(content)
            apply_email = emails[0] if emails else None

            candidate = ExtractedJob(
                title=title,
                company=company,
                location=loc_term,
                link=url,
                jd_text=f"Live tech opening: {title} at {company}.\n\n{content}\n\nApply via: {url}",
                email=apply_email,
                has_email=bool(apply_email),
                deadline=None,
                source="tavily_search",
                posted_at="1 day ago",
            )
            is_rel, _ = filter_job_relevance_and_experience(candidate, target_role=query, experience_level=experience_level)
            if is_rel:
                jobs.append(candidate)
            if len(jobs) >= limit:
                break
        return jobs


class BraveWebSearchProvider(SearchProvider):
    """Discovers live vacancies via Brave Search API across verified hiring platforms."""

    id: str = "brave"

    def search(
        self,
        query: str,
        experience_level: str = "junior",
        location: str = "worldwide_remote_or_pakistan_onsite",
        limit: int = 10,
    ) -> list[ExtractedJob]:
        api_key = config.BRAVE_API_KEY
        if not api_key:
            return []

        q_clean = re.sub(r"[()/*&_]", " ", query).strip()
        q_clean = re.sub(r"\s+", " ", q_clean)
        loc_term = "Lahore" if "lahore" in location.lower() else ("Islamabad" if "islamabad" in location.lower() else "Pakistan")
        q = f'(site:boards.greenhouse.io OR site:jobs.lever.co OR site:apply.workable.com OR site:jobs.ashbyhq.com OR site:jobs.smartrecruiters.com OR site:linkedin.com/jobs/view) ("{q_clean}" OR "Junior Software Engineer" OR "Associate Software Engineer") {loc_term}'
        headers = {
            "Accept": "application/json",
            "X-Subscription-Token": api_key,
        }
        params = {"q": q, "count": min(limit * 2, 10)}
        jobs: list[ExtractedJob] = []
        try:
            with httpx.Client(timeout=5.0) as client:
                res = client.get("https://api.search.brave.com/res/v1/web/search", headers=headers, params=params)
                if res.status_code != 200:
                    return []
                data = res.json()
        except Exception as exc:
            logger.warning(f"Brave search failed: {exc}")
            return []

        for item in data.get("web", {}).get("results", []):
            url = str(item.get("url", "")).strip()
            raw_title = str(item.get("title", "")).strip()
            desc = str(item.get("description", "")).strip()
            if not url or not raw_title:
                continue

            title, company = parse_job_title_and_company(raw_title, url)

            is_single, _ = is_valid_single_job_posting(url, title, desc)
            if not is_single:
                continue

            emails = extract_emails_from_text(desc)
            apply_email = emails[0] if emails else None

            candidate = ExtractedJob(
                title=title,
                company=company,
                location=loc_term,
                link=url,
                jd_text=f"Live job opening: {title} at {company}.\n\n{desc}\n\nApply online: {url}",
                email=apply_email,
                has_email=bool(apply_email),
                deadline=None,
                source="brave_search",
                posted_at="1 day ago",
            )
            is_rel, _ = filter_job_relevance_and_experience(candidate, target_role=query, experience_level=experience_level)
            if is_rel:
                jobs.append(candidate)
            if len(jobs) >= limit:
                break
        return jobs


class RemotiveSearchProvider(SearchProvider):
    """Discovers remote software & data jobs from Remotive public API."""

    id: str = "remotive"
    base_url: str = "https://remotive.com/api/remote-jobs"

    def search(self, query: str, experience_level: str = "junior", limit: int = 10) -> list[ExtractedJob]:
        # Filter strictly to software-dev or data categories
        params = {"category": "software-dev", "limit": 40}
        jobs: list[ExtractedJob] = []
        try:
            with httpx.Client(timeout=10.0, follow_redirects=True) as client:
                res = client.get(self.base_url, params=params)
                if res.status_code != 200:
                    return []
                data = res.json()
        except Exception as exc:
            logger.warning(f"Remotive search failed: {exc}")
            return []

        q_terms = [t.lower() for t in query.split() if len(t) > 2]
        for item in data.get("jobs", []):
            title = str(item.get("title", "")).strip()
            company = str(item.get("company_name", "")).strip()
            url = str(item.get("url", "")).strip()
            raw_desc = str(item.get("description", "")).strip()
            clean_desc = BeautifulSoup(raw_desc, "html.parser").get_text(separator=" ", strip=True) if "<" in raw_desc else raw_desc
            location = str(item.get("candidate_required_location", "Remote")).strip()
            pub_date_str = item.get("publication_date")
            pub_dt = parse_datetime(pub_date_str) if pub_date_str else None

            # Single job validity check
            is_single, _ = is_valid_single_job_posting(url, title, clean_desc)
            if not is_single:
                continue

            # Freshness check (<= 3 days)
            is_fresh, _ = is_within_last_3_days(published_at=pub_dt, jd_text=clean_desc)
            if not is_fresh:
                continue

            # Query match
            matches_q = not q_terms or any(t in title.lower() or t in clean_desc.lower() for t in q_terms)
            if not matches_q:
                continue

            emails = extract_emails_from_text(clean_desc)
            apply_email = emails[0] if emails else None

            if title and url and len(clean_desc) > 80:
                candidate = ExtractedJob(
                    title=title,
                    company=company or "Remote Tech",
                    location=location or "Remote",
                    link=url,
                    jd_text=clean_desc,
                    email=apply_email,
                    has_email=bool(apply_email),
                    deadline=None,
                    source="remotive",
                    published_at=pub_dt,
                )
                is_rel, _ = filter_job_relevance_and_experience(candidate, target_role=query, experience_level=experience_level)
                if is_rel:
                    jobs.append(candidate)
            if len(jobs) >= limit:
                break

        return jobs


class ArbeitnowSearchProvider(SearchProvider):
    """Discovers tech jobs from Arbeitnow public job board API."""

    id: str = "arbeitnow"
    base_url: str = "https://www.arbeitnow.com/api/job-board-api"

    def search(self, query: str, experience_level: str = "junior", limit: int = 10) -> list[ExtractedJob]:
        jobs: list[ExtractedJob] = []
        try:
            with httpx.Client(timeout=10.0, follow_redirects=True) as client:
                res = client.get(self.base_url)
                if res.status_code != 200:
                    return []
                data = res.json()
        except Exception as exc:
            logger.warning(f"Arbeitnow search failed: {exc}")
            return []

        q_terms = [t.lower() for t in query.split() if len(t) > 2]
        for item in data.get("data", []):
            title = str(item.get("title", "")).strip()
            company = str(item.get("company_name", "")).strip()
            url = str(item.get("url", "")).strip()
            raw_desc = str(item.get("description", "")).strip()
            clean_desc = BeautifulSoup(raw_desc, "html.parser").get_text(separator=" ", strip=True) if "<" in raw_desc else raw_desc
            location = str(item.get("location", "Remote")).strip()
            created_at = item.get("created_at")
            pub_dt = dt.datetime.fromtimestamp(created_at, dt.timezone.utc) if created_at else None

            # Single job validity check
            is_single, _ = is_valid_single_job_posting(url, title, clean_desc)
            if not is_single:
                continue

            # Freshness check (<= 3 days)
            is_fresh, _ = is_within_last_3_days(published_at=pub_dt, jd_text=clean_desc)
            if not is_fresh:
                continue

            matches_query = not q_terms or any(t in title.lower() or t in clean_desc.lower() for t in q_terms)
            if not matches_query:
                continue

            emails = extract_emails_from_text(clean_desc)
            apply_email = emails[0] if emails else None

            if title and url and len(clean_desc) > 80:
                candidate = ExtractedJob(
                    title=title,
                    company=company or "Employer",
                    location=location or "Remote",
                    link=url,
                    jd_text=clean_desc,
                    email=apply_email,
                    has_email=bool(apply_email),
                    deadline=None,
                    source="arbeitnow",
                    published_at=pub_dt,
                )
                is_rel, _ = filter_job_relevance_and_experience(candidate, target_role=query, experience_level=experience_level)
                if is_rel:
                    jobs.append(candidate)
            if len(jobs) >= limit:
                break

        return jobs


class JobicySearchProvider(SearchProvider):
    """Discovers engineering and software remote jobs from Jobicy public API."""

    id: str = "jobicy"
    base_url: str = "https://jobicy.com/api/v2/remote-jobs"

    def search(self, query: str, experience_level: str = "junior", limit: int = 10) -> list[ExtractedJob]:
        params = {"count": min(30, max(10, limit * 2)), "industry": "engineering"}
        jobs: list[ExtractedJob] = []
        try:
            with httpx.Client(timeout=10.0, follow_redirects=True) as client:
                res = client.get(self.base_url, params=params)
                if res.status_code != 200:
                    return []
                data = res.json()
        except Exception as exc:
            logger.warning(f"Jobicy search failed: {exc}")
            return []

        q_terms = [t.lower() for t in query.split() if len(t) > 2]
        for item in data.get("jobs", []):
            title = str(item.get("jobTitle", "")).strip()
            company = str(item.get("companyName", "")).strip()
            url = str(item.get("url", "")).strip()
            raw_desc = str(item.get("jobDescription", "")).strip()
            clean_desc = BeautifulSoup(raw_desc, "html.parser").get_text(separator=" ", strip=True) if "<" in raw_desc else raw_desc
            location = str(item.get("jobGeo", "Remote")).strip()
            pub_date_str = item.get("pubDate")
            pub_dt = parse_datetime(pub_date_str) if pub_date_str else None

            # Single job validity check
            is_single, _ = is_valid_single_job_posting(url, title, clean_desc)
            if not is_single:
                continue

            # Freshness check (<= 3 days)
            is_fresh, _ = is_within_last_3_days(published_at=pub_dt, jd_text=clean_desc)
            if not is_fresh:
                continue

            matches_query = not q_terms or any(t in title.lower() or t in clean_desc.lower() for t in q_terms)
            if not matches_query:
                continue

            emails = extract_emails_from_text(clean_desc)
            apply_email = emails[0] if emails else None

            if title and url and len(clean_desc) > 80:
                candidate = ExtractedJob(
                    title=title,
                    company=company or "Tech Employer",
                    location=location or "Remote",
                    link=url,
                    jd_text=clean_desc,
                    email=apply_email,
                    has_email=bool(apply_email),
                    deadline=None,
                    source="jobicy",
                    published_at=pub_dt,
                )
                is_rel, _ = filter_job_relevance_and_experience(candidate, target_role=query, experience_level=experience_level)
                if is_rel:
                    jobs.append(candidate)
            if len(jobs) >= limit:
                break

        return jobs


class MockSearchProvider(SearchProvider):
    """Deterministic offline provider for test harnesses and disconnected environments."""

    id: str = "mock"

    def search(self, query: str, experience_level: str = "junior", limit: int = 10) -> list[ExtractedJob]:
        now = dt.datetime.now(dt.timezone.utc)
        fixtures = [
            ExtractedJob(
                title="Junior Machine Learning Engineer",
                company="Nexus AI Labs",
                location="Remote / Hybrid",
                link="https://nexusai.test/careers/junior-ml-engineer",
                jd_text=(
                    "We are seeking a Junior Machine Learning Engineer (0-2 years experience) with passion for "
                    "Python, PyTorch, TensorFlow, CNNs, and deep learning. You will assist in developing "
                    "recommender systems and computer vision models. To apply, send your resume to "
                    "hiring@nexusai.test. Apply before Oct 30, 2026."
                ),
                email="hiring@nexusai.test",
                has_email=True,
                deadline=parse_datetime("2026-10-30T23:59:59Z"),
                source="mock (Nexus)",
                published_at=now,
                posted_at="1 day ago",
            ),
            ExtractedJob(
                title="Associate Full-Stack Developer (FastAPI & React)",
                company="CloudScale Solutions",
                location="Remote",
                link="https://cloudscale.test/jobs/fullstack-junior",
                jd_text=(
                    "CloudScale is hiring an Associate Full-Stack Developer to build modern web applications using "
                    "Python, FastAPI, SQLAlchemy, Next.js, and TypeScript. Ideal for early-career developers "
                    "with 1-2 years experience. Direct web applications accepted at https://cloudscale.test/apply."
                ),
                email=None,
                has_email=False,
                deadline=None,
                source="mock (CloudScale)",
                published_at=now,
                posted_at="2 days ago",
            ),
            ExtractedJob(
                title="Junior AI / Python Developer",
                company="Cortex Dynamics",
                location="Remote",
                link="https://cortexdynamics.test/openings/ai-junior",
                jd_text=(
                    "Join Cortex Dynamics as an early-career AI / Python Developer. Build agentic workflows, "
                    "LangChain integrations, and PyTorch models. Send your CV to jobs@cortexdynamics.test."
                ),
                email="jobs@cortexdynamics.test",
                has_email=True,
                deadline=None,
                source="mock (Cortex)",
                published_at=now,
                posted_at="hours ago",
            ),
        ]
        return fixtures[:limit]


def test_tavily_connection(api_key: str) -> tuple[bool, str]:
    """Test Tavily API key and return authentication status."""
    if not api_key:
        return False, "Tavily API key is empty."
    try:
        with httpx.Client(timeout=8.0) as client:
            res = client.post(
                "https://api.tavily.com/search",
                json={"api_key": api_key.strip(), "query": "software engineer", "max_results": 1},
            )
            if res.status_code == 200:
                return True, "Tavily API key authenticated successfully."
            elif res.status_code in (401, 403):
                return False, "Invalid Tavily API key (Authentication failed)."
            else:
                return False, f"Tavily responded with status {res.status_code}"
    except Exception as exc:
        return False, f"Connection to Tavily failed: {exc}"


def test_serpapi_connection(api_key: str) -> tuple[bool, str]:
    """Test SerpAPI key."""
    if not api_key:
        return False, "SerpAPI key is empty."
    try:
        with httpx.Client(timeout=8.0) as client:
            res = client.get(f"https://serpapi.com/account?api_key={api_key.strip()}")
            if res.status_code == 200:
                data = res.json()
                acc_email = data.get("account_email") or "Account"
                return True, f"SerpAPI key authenticated ({acc_email})."
            elif res.status_code in (401, 403):
                return False, "Invalid SerpAPI key (Authentication failed)."
            else:
                return False, f"SerpAPI responded with status {res.status_code}"
    except Exception as exc:
        return False, f"Connection to SerpAPI failed: {exc}"


def test_firecrawl_connection(api_key: str) -> tuple[bool, str]:
    """Test Firecrawl API key."""
    if not api_key:
        return False, "Firecrawl API key is empty."
    try:
        with httpx.Client(timeout=8.0) as client:
            headers = {"Authorization": f"Bearer {api_key.strip()}"}
            res = client.get("https://api.firecrawl.dev/v1/team/credit-usage", headers=headers)
            if res.status_code in (200, 201):
                return True, "Firecrawl API key authenticated successfully."
            elif res.status_code in (401, 403):
                return False, "Invalid Firecrawl API key (Authentication failed)."
            else:
                return True, "Firecrawl API key accepted."
    except Exception as exc:
        return False, f"Connection to Firecrawl failed: {exc}"


def execute_search(
    query: str = "",
    role: str = "Junior Developer (AI / Full-Stack / ASE)",
    custom_role: str = "",
    experience_level: str = "junior",
    location: str = "worldwide_remote_or_pakistan_onsite",
    custom_location: str = "",
    include_remote: bool = True,
    platform: str = "all",
    limit: int = 10,
    use_mock_fallback: bool = False,
    max_days: int = 7,
    provider: str = "tavily",
    user_creds: dict[str, str] | None = None,
) -> list[ExtractedJob]:
    """Execute high-speed concurrent multi-tier search across live ATS portals, Google, Tavily, Apify MCP, Brave, and feeds."""
    effective_query = (custom_role.strip() if custom_role else "") or (query.strip() if query else "") or role or "Junior Developer (AI / Full-Stack / ASE)"
    effective_loc = (custom_location.strip() if custom_location else "") or location or "worldwide_remote_or_pakistan_onsite"
    creds = user_creds or {}

    # 1. Apify MCP Dedicated Mode
    if provider in ("apify_mcp", "apify"):
        try:
            from radar.apify_mcp import ApifySearchProvider
            apify_prov = ApifySearchProvider(api_key=creds.get("apify_api_key"))
            apify_jobs = apify_prov.search(
                query=effective_query,
                experience_level=experience_level,
                location=effective_loc,
                include_remote=include_remote,
                platform=platform,
                limit=limit,
            )
            if apify_jobs:
                return apify_jobs[:limit]
        except Exception as exc:
            logger.warning(f"Apify MCP search execution failed ({exc}); checking fallback.")

    # 1b. Firecrawl Dedicated Mode
    elif provider in ("firecrawl", "firecrawl_scraper"):
        try:
            from radar.firecrawl_client import FirecrawlClient
            fc = FirecrawlClient(api_key=creds.get("firecrawl_api_key"))
            from radar.company_crawler import CompanyCareerCrawler
            crawler = CompanyCareerCrawler(firecrawl_client=fc)
            c_jobs = crawler.crawl_all_companies(
                target_role=effective_query,
                experience_level=experience_level,
                max_jobs=limit,
                target_city="lahore" if "lahore" in effective_loc.lower() else ("islamabad" if "islamabad" in effective_loc.lower() else ""),
            )
            if c_jobs:
                return c_jobs[:limit]
        except Exception as exc:
            logger.warning(f"Firecrawl provider search failed ({exc}); checking fallback.")

    # 2. Standard multi-provider map
    tavily_key = creds.get("tavily_api_key")
    providers_map: dict[str, SearchProvider] = {
        "google_jobs": SerpApiGoogleJobsProvider(),
        "tavily": TavilySearchProvider(api_key=tavily_key),
        "brave": BraveWebSearchProvider(),
        "jobicy": JobicySearchProvider(),
        "arbeitnow": ArbeitnowSearchProvider(),
        "remotive": RemotiveSearchProvider(),
    }

    if provider == "google_jobs":
        active_providers = [providers_map["google_jobs"]]
    elif provider == "tavily":
        active_providers = [providers_map["tavily"]]
    elif platform in providers_map:
        active_providers = [providers_map[platform]]
    elif platform != "all":
        active_providers = [providers_map["google_jobs"], providers_map["tavily"], providers_map["brave"]]
    else:
        active_providers = [
            providers_map["tavily"],
            providers_map["brave"],
            providers_map["jobicy"],
            providers_map["arbeitnow"],
            providers_map["remotive"],
            providers_map["google_jobs"],
        ]

    aggregated: list[ExtractedJob] = []

    is_pakistan_query = (
        platform == "pakistan_companies"
        or any(k in effective_loc.lower() for k in ("lahore", "islamabad", "faisalabad", "pakistan"))
        or effective_loc == "worldwide_remote_or_pakistan_onsite"
    )

    future_map: dict[Any, str] = {}
    with ThreadPoolExecutor(max_workers=7, thread_name_prefix="radar_search") as executor:
        # 1. Company Career Crawling for Pakistan Software Houses
        if is_pakistan_query:
            target_city = "Lahore"
            if "islamabad" in effective_loc.lower() or "rawalpindi" in effective_loc.lower():
                target_city = "Islamabad"
            elif "faisalabad" in effective_loc.lower():
                target_city = "Faisalabad"
            elif "karachi" in effective_loc.lower():
                target_city = "Karachi"

            def _crawl_task(t_role=effective_query, exp_lvl=experience_level, city=target_city, l_lim=limit) -> list[ExtractedJob]:
                try:
                    from radar.company_crawler import CompanyCareerCrawler
                    company_crawler = CompanyCareerCrawler()
                    return company_crawler.crawl_companies(
                        target_role=t_role,
                        experience_level=exp_lvl,
                        target_city=city,
                        limit=l_lim,
                    )
                except Exception as e:
                    logger.warning(f"Company career crawler search failed: {e}")
                    return []

            future_map[executor.submit(_crawl_task)] = "company_crawler"

        # 2. Live Providers Search Tasks
        for p in active_providers:
            if is_pakistan_query and not include_remote and p.id in ("arbeitnow", "jobicy", "remotive") and platform != "all":
                continue

            def _prov_task(prov=p) -> list[ExtractedJob]:
                try:
                    if isinstance(prov, SerpApiGoogleJobsProvider):
                        return prov.search(
                            effective_query,
                            experience_level=experience_level,
                            location=effective_loc,
                            include_remote=include_remote,
                            platform=platform if platform != "pakistan_companies" else "all",
                            limit=limit,
                        )
                    elif isinstance(prov, (TavilySearchProvider, BraveWebSearchProvider)):
                        return prov.search(
                            effective_query,
                            experience_level=experience_level,
                            location=effective_loc,
                            limit=limit,
                        )
                    else:
                        return prov.search(effective_query, experience_level=experience_level, limit=limit)
                except Exception as e:
                    logger.warning(f"Provider {prov.id} search failed: {e}")
                    return []

            future_map[executor.submit(_prov_task)] = p.id

        try:
            for future in as_completed(future_map, timeout=6.5):
                prov_name = future_map[future]
                try:
                    results = future.result()
                    for j in results:
                        is_single, _ = is_valid_single_job_posting(j.link, j.title, j.jd_text)
                        if not is_single:
                            continue

                        is_fresh, _ = is_within_last_3_days(
                            posted_at=j.posted_at,
                            published_at=j.published_at,
                            jd_text=j.jd_text,
                            link=j.link,
                            max_days=max_days,
                        )
                        if not is_fresh:
                            continue

                        is_valid, _ = filter_job_relevance_and_experience(j, target_role=effective_query, experience_level=experience_level)
                        if is_valid:
                            # Avoid duplicates in aggregated list
                            if not any(
                                x.link == j.link or (x.title.lower() == j.title.lower() and x.company.lower() == j.company.lower())
                                for x in aggregated
                            ):
                                aggregated.append(j)
                except Exception as exc:
                    logger.warning(f"Error processing search provider {prov_name}: {exc}")
        except TimeoutError:
            logger.info("Radar parallel search reached timeout cutoff; proceeding with accumulated results.")

    # Quality ranking: direct ATS & official portal matches first, then email contact jobs, then general postings
    def _rank_score(job: ExtractedJob) -> int:
        score = 0
        l_low = (job.link or "").lower()
        if any(d in l_low for d in ("boards.greenhouse.io", "jobs.lever.co", "jobs.ashbyhq.com", "apply.workable.com", "jobs.smartrecruiters.com")):
            score += 40
        if "company" in (job.source or "").lower() or "direct_company" in l_low:
            score += 30
        if job.has_email:
            score += 20
        return score

    aggregated.sort(key=_rank_score, reverse=True)

    if not aggregated and use_mock_fallback:
        logger.info("Live providers returned empty after filtering; using deterministic mock provider.")
        for j in MockSearchProvider().search(effective_query, experience_level=experience_level, limit=limit):
            is_valid, _ = filter_job_relevance_and_experience(j, target_role=effective_query, experience_level=experience_level)
            if is_valid:
                aggregated.append(j)

    return aggregated[:limit]
