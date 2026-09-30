"""Deduplication and canonical fingerprint generation."""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

COMPANY_NOISE_RE = re.compile(
    r"\b(inc|incorporated|llc|corp|corporation|ltd|limited|pvt|co)\b|\.",
    re.IGNORECASE,
)

TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "ref",
    "source",
    "trackingId",
    "fbclid",
    "gclid",
}


def normalize_company(name: str) -> str:
    """Normalize company name by stripping legal suffixes and punctuation."""
    if not name:
        return "unknown"
    clean = COMPANY_NOISE_RE.sub("", name.lower())
    clean = re.sub(r"[^\w\s]", "", clean)
    return " ".join(clean.split()).strip() or "unknown"


def normalize_title(title: str) -> str:
    """Normalize role title by stripping punctuation and extra whitespace."""
    if not title:
        return "role"
    clean = re.sub(r"[^\w\s]", " ", title.lower())
    return " ".join(clean.split()).strip() or "role"


def clean_canonical_url(url: str) -> str:
    """Strip tracking parameters, fragments, and trailing slashes from URL."""
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        # Keep non-tracking query parameters
        kept_queries = [
            (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k.lower() not in TRACKING_PARAMS
        ]
        new_query = urlencode(kept_queries)
        path = parsed.path.rstrip("/")
        return urlunparse((parsed.scheme, parsed.netloc.lower(), path, parsed.params, new_query, ""))
    except Exception:
        return url.strip().rstrip("/")


def generate_dedup_key(company: str, title: str, link: str) -> str:
    """Generate a deterministic, canonical 32-character SHA-256 deduplication key."""
    norm_c = normalize_company(company)
    norm_t = normalize_title(title)
    norm_u = clean_canonical_url(link)
    payload = f"{norm_c}|{norm_t}|{norm_u}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def compute_simhash64(text: str) -> int:
    """Compute 64-bit locality-sensitive SimHash for text."""
    if not text:
        return 0
    tokens = [t for t in re.sub(r"[^\w\s]", " ", text.lower()).split() if len(t) > 1]
    if not tokens:
        return 0
    v = [0] * 64
    for token in tokens:
        h = int(hashlib.md5(token.encode("utf-8")).hexdigest()[:16], 16)
        for i in range(64):
            bit = (h >> i) & 1
            v[i] += 1 if bit == 1 else -1
    fingerprint = 0
    for i in range(64):
        if v[i] > 0:
            fingerprint |= 1 << i
    return fingerprint


def simhash_similarity(hash1: int, hash2: int) -> float:
    """Compute normalized similarity (0.0 to 1.0) between two 64-bit SimHashes."""
    xor_val = hash1 ^ hash2
    hamming_dist = bin(xor_val).count("1")
    return max(0.0, 1.0 - (hamming_dist / 64.0))
