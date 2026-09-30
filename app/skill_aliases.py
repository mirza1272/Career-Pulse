"""Skill alias map — canonical skill names and their equivalent phrasings.

Problem this solves: the JD may say "object-oriented programming" while the
candidate's Knowledge Base has "OOP"; or the JD says "AI Agents" while the KB
has "Agentic AI". These are the SAME skill, but exact-string matching flagged
them as missing, which (a) showed false "Missing from your profile" badges,
(b) lowered the ATS score, and (c) made the LLM claim the skill was missing.

This module provides:
- canonical_skill(name): normalize any phrasing to one canonical form.
- skill_matches(a, b): True when two phrasings mean the same skill.

Display rule (user-set): the resume always shows the clean canonical skill
name — never a "JD Term (KB name)" parenthetical. Alias-aware matching keeps
ATS keyword scoring correct behind the scenes.

Rules:
- NEVER add an entry that is not truly the same skill (e.g. Git != GitHub).
- Aliases are matched case-insensitively after normalization.
"""

from __future__ import annotations

import re


def _norm(s: str) -> str:
    """Lowercase, collapse separators, strip plurals-ish noise for comparison."""
    s = (s or "").lower().strip()
    s = re.sub(r"[._/]+", " ", s)      # react.js -> react js
    s = re.sub(r"[-]+", " ", s)        # retrieval-augmented -> retrieval augmented
    s = re.sub(r"\s+", " ", s).strip()
    return s


def normalize_skill(s: str) -> str:
    """Public access to the comparison normalization used by the alias map."""
    return _norm(s)


# canonical normalized name -> list of equivalent normalized phrasings
# (the canonical key itself is always an accepted phrasing).
SKILL_ALIASES: dict[str, list[str]] = {
    "oop": [
        "object oriented programming",
        "object-oriented programming",
        "oops",
        "oo programming",
    ],
    "ai agents": [
        "agentic ai",
        "ai agent",
        "autonomous agents",
        "autonomous agent",
        "agentic",
    ],
    "machine learning": ["ml"],
    "deep learning": ["dl"],
    "natural language processing": ["nlp"],
    "large language models": [
        "large language model",
        "llm",
        "llms",
    ],
    "retrieval augmented generation": [
        "retrieval-augmented generation",
        "rag",
    ],
    "computer vision": ["cv"],
    "neural networks": ["neural network", "nn"],
    "generative ai": ["genai", "gen ai"],
    "prompt engineering": ["prompt eng"],
    "fine tuning": ["fine-tuning", "finetuning"],
    "vector database": ["vector db", "vector databases"],
    "knowledge graph": ["knowledge graphs", "kg"],
    "javascript": ["js"],
    "typescript": ["ts"],
    "html": ["html5"],
    "css": ["css3"],
    "react": ["react js", "reactjs"],
    "node": ["node js", "nodejs"],
    "next js": ["nextjs", "next"],
    "fastapi": ["fast api"],
    "rest api": ["restful api", "rest apis", "restful apis", "rest"],
    "graphql": ["graph ql"],
    "websockets": ["websocket"],
    "oauth": ["oauth2", "oauth 2"],
    "jwt": ["json web token", "json web tokens"],
    "microservices": ["microservice", "microservice architecture", "microservices architecture"],
    "system design": ["system architecture", "systems design"],
    "api development": ["api design", "apis"],
    "data structures": ["dsa"],
    "algorithms": ["algo", "algorithm"],
    "sql": ["structured query language"],
    "nosql": ["no sql"],
    "postgresql": ["postgres"],
    "mongodb": ["mongo"],
    "pytorch": ["torch"],
    "tensorflow": ["tf"],
    "scikit learn": ["sklearn", "scikit-learn"],
    "kubernetes": ["k8s"],
    "ci cd": ["cicd", "continuous integration", "continuous deployment"],
    "google cloud": ["gcp", "google cloud platform"],
    "aws": ["amazon web services", "aws cloud", "ec2", "s3", "lambda"],
    "azure": ["microsoft azure", "azure cloud", "azure openai"],
    "docker": ["containerization", "containers", "dockerfile"],
    "data pipelines": ["data pipeline", "etl", "etl pipelines", "data processing", "asynchronous data pipelines", "data ingestion"],
    "data preprocessing": ["data cleaning", "feature engineering", "data normalization"],
    "predictive modeling": ["predictive models", "supervised learning", "classification", "regression"],
    "vector search": ["vector similarity", "vector embeddings", "embeddings", "semantic search"],
    "knowledge graphs": ["knowledge graph", "graph databases", "graph rag", "kg"],
    "security": ["application security", "appsec", "cybersecurity", "secure coding", "owasp"],
    "ssrf protection": ["ssrf", "server side request forgery", "url validation"],
    "http clients": ["httpx", "aiohttp", "requests", "http client", "api client"],
    "automation": ["workflow automation", "process automation", "automated workflows"],
    "optimization": ["performance optimization", "latency optimization", "query optimization", "high throughput"],
    "pydantic": ["data validation", "data schemas", "json schemas", "pydantic models"],
    "keras": ["tf keras", "tensorflow keras"],
    "voice ai": ["conversational ai", "voicebot", "voice agents"],
    "vapi": ["vapi ai", "voice api"],
    "chromadb": ["chroma", "chroma db"],
    "falkordb": ["falkor", "falkor db"],
    "supabase": ["supabase postgres", "supabase db"],
    "groq": ["groq cloud", "groq api", "groq llm"],
    "google gemini": ["gemini", "gemini pro", "gemini flash", "google ai"],
    "openai": ["openai api", "gpt 4", "gpt 4o", "chatgpt"],
    "git": ["github", "gitlab", "version control"],
    "python": ["python3", "python 3"],
    "java": [],
    "c++": ["cpp"],
    "langchain": ["lang chain"],
    "langgraph": ["lang graph"],
    "fastmcp": ["mcp", "model context protocol"],
    "cnn": ["convolutional neural networks", "convolutional neural network"],
    "ann": ["artificial neural networks", "artificial neural network"],
    "k means": ["kmeans", "k-means clustering", "clustering"],
    "pandas": ["dataframes", "dataframe"],
    "numpy": ["numerical python"],
    "matplotlib": ["data visualization"],
    "seaborn": ["statistical visualization"],
    "tailwind css": ["tailwind", "tailwindcss"],
    "express": ["express js", "expressjs"],
}

# Reverse lookup: normalized phrasing -> canonical normalized name
_ALIAS_TO_CANONICAL: dict[str, str] = {}
for _canon, _aliases in SKILL_ALIASES.items():
    _ALIAS_TO_CANONICAL[_canon] = _canon
    for _a in _aliases:
        _ALIAS_TO_CANONICAL[_norm(_a)] = _canon


def canonical_skill(name: str) -> str:
    """Return the canonical normalized form of a skill phrasing."""
    return _ALIAS_TO_CANONICAL.get(_norm(name), _norm(name))


def skill_matches(a: str, b: str) -> bool:
    """True when two skill phrasings refer to the same skill.

    Falls back to the old substring behavior for pairs with no alias entry,
    so existing fuzzy matches (e.g. "Next.js" vs "nextjs") keep working.
    """
    if not a or not b:
        return False
    ca, cb = canonical_skill(a), canonical_skill(b)
    if ca == cb:
        return True
    # Substring fallback (only when neither side resolved via the alias map
    # to something different — avoids "java" matching "javascript").
    if ca != _norm(a) or cb != _norm(b):
        return False
    na, nb = _norm(a), _norm(b)
    if len(na) < 4 or len(nb) < 4:
        return False
    # Word-boundary substring only: "git" must not match "github",
    # "java" must not match "javascript".
    def _word_hit(short: str, long: str) -> bool:
        return re.search(rf"(?<![a-z0-9]){re.escape(short)}(?![a-z0-9])", long) is not None
    return _word_hit(na, nb) or _word_hit(nb, na)


def all_phrasings(name: str) -> list[str]:
    """All known phrasings (normalized) for a skill, canonical first."""
    canon = canonical_skill(name)
    out = [canon]
    for a in SKILL_ALIASES.get(canon, []):
        na = _norm(a)
        if na not in out:
            out.append(na)
    return out


def phrasing_regex(phrasing: str) -> str:
    """Regex fragment matching a phrasing tolerating hyphen/space/dot variants.

    'object oriented programming' matches 'object-oriented programming',
    'object oriented programming', etc.
    """
    parts = _norm(phrasing).split()
    return r"[\s\-.]+".join(re.escape(w) for w in parts)


def phrasing_in_text(phrasing: str, text_lower: str) -> bool:
    """True when a skill phrasing appears in (lowercased) text, hyphen-tolerant."""
    return re.search(rf"\b{phrasing_regex(phrasing)}\b", text_lower) is not None


def find_phrasing_in_text(phrasing: str, text_lower: str) -> str:
    """Return the actual matched substring (preserving hyphens), or ''."""
    m = re.search(rf"\b({phrasing_regex(phrasing)})\b", text_lower)
    return m.group(1) if m else ""
