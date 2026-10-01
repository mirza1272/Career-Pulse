"""Resume Builder — Modern HTML/CSS template tailoring & PDF generation.

Matches the candidate's clean, high-impact design (font, colour, spacing, single column).
Locked regions: HEADER, CONTACT, EDUCATION.
Editable regions: SUMMARY, SKILLS, PROJECTS, EXPERIENCE.
"""

from __future__ import annotations

import html as _html
import html.parser as _html_parser
import json
import logging
import os
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app import config
from app.knowledge import Candidate, load_candidate
from app.skill_aliases import (
    SKILL_ALIASES,
    all_phrasings,
    canonical_skill,
    normalize_skill,
    phrasing_in_text,
    skill_matches,
)


# ---------------------------------------------------------------------------
# User-HTML sanitizer (stored-XSS fix). Allowlist-based, stdlib only.
# Every region the user can type into (resume editor textareas, custom skill
# names, link URLs) passes through this before it is stored in the resume
# file, which is served as same-origin HTML and auto-loaded in an iframe.
# ---------------------------------------------------------------------------

class _ResumeHTMLSanitizer(_html_parser.HTMLParser):
    """Strip scripts, event handlers and unsafe URLs; keep basic formatting."""

    _ALLOWED_TAGS = frozenset({
        "p", "br", "strong", "b", "em", "i", "u", "ul", "ol", "li",
        "span", "div", "h1", "h2", "h3", "h4", "a",
    })
    # Tags whose content must be dropped entirely (not just the tag itself).
    _DROP_CONTENT_TAGS = frozenset({
        "script", "style", "iframe", "object", "embed", "form",
        "input", "button", "textarea", "select", "meta", "link", "base",
    })
    _ALLOWED_ATTRS = {
        "a": frozenset({"href", "target", "class", "rel"}),
        "span": frozenset({"class"}),
        "div": frozenset({"class"}),
        "p": frozenset({"class"}),
        "li": frozenset({"class"}),
        "ul": frozenset({"class"}),
        "ol": frozenset({"class"}),
        "h1": frozenset({"class"}),
        "h2": frozenset({"class"}),
        "h3": frozenset({"class"}),
        "h4": frozenset({"class"}),
    }
    _SAFE_SCHEMES = ("http://", "https://", "mailto:")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._drop_depth = 0

    # -- helpers ---------------------------------------------------------
    def _is_safe_url(self, url: str) -> bool:
        u = (url or "").strip().lower()
        return u.startswith(self._SAFE_SCHEMES) or u.startswith("#") or u.startswith("/")

    # -- parser callbacks ------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self._DROP_CONTENT_TAGS:
            self._drop_depth += 1
            return
        if self._drop_depth or tag not in self._ALLOWED_TAGS:
            return  # drop the tag but keep its text content
        allowed = self._ALLOWED_ATTRS.get(tag, frozenset())
        clean: list[tuple[str, str]] = []
        for k, v in attrs:
            k = (k or "").lower()
            if k.startswith("on") or k not in allowed:
                continue  # no event handlers, no style/src/etc.
            v = v or ""
            if tag == "a" and k == "href" and not self._is_safe_url(v):
                continue  # no javascript:/data: URLs
            clean.append((k, v))
        attr_str = "".join(f' {k}="{_html.escape(v, quote=True)}"' for k, v in clean)
        self._out.append(f"<{tag}{attr_str}>")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._DROP_CONTENT_TAGS:
            self._drop_depth = max(0, self._drop_depth - 1)
            return
        if not self._drop_depth and tag in self._ALLOWED_TAGS:
            self._out.append(f"</{tag}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "br":
            self._out.append("<br>")
        else:
            self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        if not self._drop_depth:
            self._out.append(_html.escape(data))

    def handle_comment(self, data: str) -> None:
        pass  # drop comments entirely

    def get_html(self) -> str:
        return "".join(self._out)


def sanitize_resume_html(raw_html: str | None) -> str:
    """Sanitize user-supplied resume region HTML (stored-XSS defense).

    Keeps basic formatting tags (p/br/strong/em/lists/links); strips
    <script>, event-handler attributes, and unsafe URL schemes.
    """
    if not raw_html:
        return ""
    parser = _ResumeHTMLSanitizer()
    try:
        parser.feed(raw_html)
        parser.close()
    except Exception:
        # On any parse failure, fail closed to plain escaped text.
        return _html.escape(raw_html)
    return parser.get_html()


def _is_safe_link_url(url: str) -> bool:
    """Allow only http/https/mailto links in resume link fields."""
    u = (url or "").strip().lower()
    return u.startswith(("http://", "https://", "mailto:"))


CANONICAL_DISPLAY_MAP: dict[str, str] = {
    "python": "Python",
    "machine learning": "Machine Learning",
    "deep learning": "Deep Learning",
    "rag": "RAG",
    "llms": "LLMs",
    "llm": "LLMs",
    "agentic ai": "Agentic AI",
    "fastapi": "FastAPI",
    "scikit-learn": "Scikit-learn",
    "sklearn": "Scikit-learn",
    "pytorch": "PyTorch",
    "tensorflow": "TensorFlow",
    "keras": "Keras",
    "numpy": "NumPy",
    "pandas": "Pandas",
    "matplotlib": "Matplotlib",
    "seaborn": "Seaborn",
    "react.js": "React.js",
    "react": "React.js",
    "rest apis": "REST APIs",
    "rest api": "REST APIs",
    "apis": "APIs",
    "pydantic": "Pydantic",
    "sql": "SQL",
    "javascript": "JavaScript",
    "js": "JavaScript",
    "typescript": "TypeScript",
    "ts": "TypeScript",
    "c++": "C++",
    "cpp": "C++",
    "groq": "Groq",
    "google gemini": "Google Gemini",
    "gemini": "Google Gemini",
    "vapi": "Vapi",
    "voice ai": "Voice AI",
    "mongodb": "MongoDB",
    "chromadb": "ChromaDB",
    "falkordb": "FalkorDB",
    "supabase": "Supabase",
    "vercel": "Vercel",
    "git": "Git",
    "github": "GitHub",
    "cnn": "CNN",
    "ann": "ANN",
    "k-means": "K-Means",
    "kmeans": "K-Means",
    "knowledge graphs": "Knowledge Graphs",
    "data preprocessing": "Data Preprocessing",
    "predictive modeling": "Predictive Modeling",
    "data pipelines": "Data Pipelines",
    "aws": "AWS",
    "azure": "Azure",
    "docker": "Docker",
    "kubernetes": "Kubernetes",
    "k8s": "Kubernetes",
    "mlops": "MLOps",
    "devops": "DevOps",
    "vector search": "Vector Search",
    "automation": "Automation",
    "security": "Security",
    "http clients": "HTTP Clients",
    "ssrf protection": "SSRF Protection",
    "optimization": "Optimization",
    "html": "HTML",
    "css": "CSS",
    "node.js": "Node.js",
    "node": "Node.js",
    "express.js": "Express.js",
    "express": "Express.js",
    "openai": "OpenAI",
    "langchain": "LangChain",
    "langgraph": "LangGraph",
    "tailwind css": "Tailwind CSS",
    "postgresql": "PostgreSQL",
    "postgres": "PostgreSQL",
    "redis": "Redis",
    "ci/cd": "CI/CD",
    "cicd": "CI/CD",
    "fastmcp": "FastMCP",
    "mcp": "MCP",
    "oop": "OOP",
    "graphql": "GraphQL",
    "grpc": "gRPC",
    "kafka": "Kafka",
    "spark": "Spark",
    "airflow": "Airflow",
    "dbt": "dbt",
    "mlflow": "MLflow",
    "wandb": "W&B",
    "dvc": "DVC",
    "onnx": "ONNX",
    "langsmith": "LangSmith",
    "llamaindex": "LlamaIndex",
    "huggingface": "Hugging Face",
    "hugging face": "Hugging Face",
    "transformers": "Transformers",
    "bert": "BERT",
    "gpt": "GPT",
    "claude": "Claude",
    "crewai": "CrewAI",
    "autogen": "AutoGen",
    "qdrant": "Qdrant",
    "pinecone": "Pinecone",
    "weaviate": "Weaviate",
    "faiss": "FAISS",
    "neo4j": "Neo4j",
    "nlp": "NLP",
    "cv": "Computer Vision",
    "computer vision": "Computer Vision",
    "genai": "GenAI",
    "generative ai": "Generative AI",
}

CANDIDATE_SKILL_CATEGORIES: dict[str, list[str]] = {
    "AI & Machine Learning": [
        "Machine Learning", "Deep Learning", "PyTorch", "TensorFlow", "Keras",
        "Scikit-learn", "CNN", "ANN", "K-Means", "Predictive Modeling",
        "Data Preprocessing", "NumPy", "Pandas", "Matplotlib", "Seaborn"
    ],
    "Agentic AI & GenAI": [
        "Agentic AI", "RAG", "LLMs", "Vector Search", "Knowledge Graphs",
        "Groq", "Google Gemini", "OpenAI", "Voice AI", "Vapi", "LangChain",
        "LangGraph", "FastMCP"
    ],
    "Backend, Cloud & APIs": [
        "FastAPI", "REST APIs", "APIs", "Pydantic", "Node.js", "Express.js",
        "AWS", "Azure", "Docker", "Data Pipelines", "HTTP Clients",
        "SSRF Protection", "Optimization", "Security", "Automation", "CI/CD"
    ],
    "Programming Languages": [
        "Python", "SQL", "JavaScript", "TypeScript", "C++"
    ],
    "Databases & Storage": [
        "PostgreSQL", "MongoDB", "ChromaDB", "FalkorDB", "Supabase", "Redis"
    ],
    "Frontend & Web": [
        "React.js", "Tailwind CSS", "HTML", "CSS", "Vercel", "Git", "GitHub"
    ],
}

# ---------------------------------------------------------------------------
# Canonical display names for free-typed skills (display-rule fix).
# "ML" -> "Machine Learning", "js" -> "JavaScript", unknown -> as typed.
# ---------------------------------------------------------------------------

_DISPLAY_NAME_BY_NORM: dict[str, str] = {}


def _display_name_map() -> dict[str, str]:
    global _DISPLAY_NAME_BY_NORM
    if not _DISPLAY_NAME_BY_NORM:
        for _cat_skills in CANDIDATE_SKILL_CATEGORIES.values():
            for _disp in _cat_skills:
                _DISPLAY_NAME_BY_NORM.setdefault(normalize_skill(_disp), _disp)
        for _canon_norm, _aliases in SKILL_ALIASES.items():
            # Resolve the canonical key to a display name: direct taxonomy
            # hit first, otherwise via any of its aliases (e.g. canonical
            # "node" -> alias "node js" -> display "Node.js").
            _disp = _DISPLAY_NAME_BY_NORM.get(_canon_norm)
            if not _disp:
                for _a in _aliases:
                    _disp = _DISPLAY_NAME_BY_NORM.get(normalize_skill(_a))
                    if _disp:
                        break
            if not _disp:
                continue
            _DISPLAY_NAME_BY_NORM.setdefault(_canon_norm, _disp)
            for _a in _aliases:
                _DISPLAY_NAME_BY_NORM.setdefault(normalize_skill(_a), _disp)
    return _DISPLAY_NAME_BY_NORM


def canonical_display_name(name: str) -> str:
    """Map a free-typed skill phrasing to its canonical display name.

    Respects the user-set display rule: the resume always shows the clean
    canonical skill name (e.g. 'FastAPI', 'PyTorch', 'AWS', 'PostgreSQL', 'MLOps').
    """
    if not name:
        return ""
    s = name.strip()
    norm = normalize_skill(s)
    raw_low = s.lower()
    if norm in CANONICAL_DISPLAY_MAP:
        return CANONICAL_DISPLAY_MAP[norm]
    if raw_low in CANONICAL_DISPLAY_MAP:
        return CANONICAL_DISPLAY_MAP[raw_low]
    disp = _display_name_map().get(norm) or _display_name_map().get(raw_low)
    if disp:
        return disp
    # Preserve explicit casing if user typed mixed case like MLOps, GraphQL, gRPC
    if any(c.isupper() for c in s[1:]):
        return s
    if re.search(r"[+#./]|\d", s):
        return s
    return s.title()


def _ats_friendly_skill_label(skill: str, jd_lower: str) -> str:
    """Display label for a KB skill on the resume.

    Always the clean canonical skill name (e.g. "Machine Learning",
    "Node.js", "JavaScript", "HTML") — never the JD's phrasing and never a
    "JD Term (KB name)" parenthetical. Recruiters already know the standard
    names, so the brackets only add noise. Alias-aware ATS scoring
    (app/ats.py via skill_matches) still credits the JD keyword behind the
    scenes, so nothing is lost for keyword matching.
    """
    return skill

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = ROOT / "templates/resumes"
FONTS_DIR = ROOT / "assets/fonts"
is_serverless = bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))
RESUMES_OUTPUT_DIR = Path("/tmp/resumes") if is_serverless else (ROOT / "data/resumes")

FONT_DIR_TOKEN = "{{FONT_DIR}}"
_REGION_RE = re.compile(
    r"<!--REGION:([A-Za-z0-9_]+)-->(.*?)<!--END:\1-->",
    re.DOTALL,
)
_LI_RE = re.compile(r"<li>(.*?)</li>", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")

TEMPLATES_REGISTRY: dict[str, dict[str, Any]] = {
    "apex_modern": {
        "id": "apex_modern",
        "name": "Apex Modern",
        "category": "Modern Tech",
        "badge": "Top ATS Match",
        "font_family": "Nunito",
        "font_css_family": "'Nunito', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
        "primary_color": "#137f96",
        "secondary_color": "#2491a9",
        "accent_color": "#137f96",
        "dark_color": "#111827",
        "body_color": "#1f2937",
        "muted_color": "#64748b",
        "domain": "all",
        "template": "apex_modern.html",
        "description": "Dynamic Teal accents with modern Nunito typography. Engineered for high-impact readability and flawless ATS parsing.",
        "features": ["Single-column high ATS layout", "Custom contact icon row", "Teal section headers", "Optimized line heights"],
    },
    "oxford_editorial": {
        "id": "oxford_editorial",
        "name": "Oxford Editorial",
        "category": "Classic Serif",
        "badge": "Distinguished",
        "font_family": "Alegreya",
        "font_css_family": "'Alegreya', Georgia, 'Times New Roman', serif",
        "primary_color": "#334155",
        "secondary_color": "#475569",
        "accent_color": "#1e293b",
        "dark_color": "#0f172a",
        "body_color": "#1e293b",
        "muted_color": "#64748b",
        "domain": "all",
        "template": "oxford_editorial.html",
        "description": "Prestigious bookish serif based on Alegreya. Centered editorial header, refined dividers, and timeless academic elegance.",
        "features": ["Centered editorial header", "Classic uppercase section rules", "Refined serif typography", "High-density editorial layout"],
    },
    "silicon_compact": {
        "id": "silicon_compact",
        "name": "Silicon Compact",
        "category": "Tech Compact",
        "badge": "High Density",
        "font_family": "Source Sans 3",
        "font_css_family": "'Source Sans 3', 'Segoe UI', Roboto, sans-serif",
        "primary_color": "#0f766e",
        "secondary_color": "#0284c7",
        "accent_color": "#0f766e",
        "dark_color": "#0f172a",
        "body_color": "#1e293b",
        "muted_color": "#475569",
        "domain": "all",
        "template": "silicon_compact.html",
        "description": "Crisp, dense Silicon Valley engineering layout with Source Sans 3. Inline pipe dividers and maximized data density.",
        "features": ["Condensed tech layout", "Inline pipe separators", "Deep emerald accents", "Maximized content throughput"],
    },
    "monarch_executive": {
        "id": "monarch_executive",
        "name": "Monarch Executive",
        "category": "Executive Prestige",
        "badge": "Leadership",
        "font_family": "Amiri",
        "font_css_family": "'Amiri', Garamond, 'Times New Roman', serif",
        "primary_color": "#1e293b",
        "secondary_color": "#881337",
        "accent_color": "#881337",
        "dark_color": "#090d16",
        "body_color": "#1e293b",
        "muted_color": "#475569",
        "domain": "all",
        "template": "monarch_executive.html",
        "description": "Prestige executive typography with Amiri serif and Royal Burgundy accents. Prominent side-by-side header for senior professionals.",
        "features": ["Side-by-side executive header", "Royal burgundy highlights", "Authoritative serif typography", "Stately border accents"],
    },
    "nova_minimalist": {
        "id": "nova_minimalist",
        "name": "Nova Minimalist",
        "category": "Minimalist Modern",
        "badge": "Ultra Clean",
        "font_family": "Source Sans 3",
        "font_css_family": "'Source Sans 3', -apple-system, sans-serif",
        "primary_color": "#18181b",
        "secondary_color": "#71717a",
        "accent_color": "#27272a",
        "dark_color": "#09090b",
        "body_color": "#27272a",
        "muted_color": "#71717a",
        "domain": "all",
        "template": "nova_minimalist.html",
        "description": "Ultra-clean modern Scandinavian minimalism. Crisp monochrome palette, subtle hairline dividers, and spacious typography.",
        "features": ["Subtle hairline dividers", "Crisp monochrome palette", "Modern Scandinavian aesthetic", "Refined letter spacing"],
    },
}

TEMPLATE_ALIASES: dict[str, str] = {
    "se_al": "apex_modern",
    "se_am": "apex_modern",
    "se_fd": "apex_modern",
}

# Backward compatibility alias
VARIANTS = TEMPLATES_REGISTRY

LOCKED_REGIONS = {"HEADER", "CONTACT", "EDUCATION"}
EDITABLE_REGIONS = {"SUMMARY", "SKILLS", "PROJECTS", "EXPERIENCE", "EDUCATION", "CERTIFICATIONS", "LINKS"}

DEFAULT_TEMPLATE_ID = "apex_modern"


def resolve_template(template_id: str | None) -> tuple[str, dict[str, Any]]:
    """Resolve a template identifier to (id, registry entry), fail-soft with alias support.

    Unknown ids or missing template files fall back to the default template.
    """
    raw_id = (template_id or "").strip()
    tid = TEMPLATE_ALIASES.get(raw_id, raw_id) or DEFAULT_TEMPLATE_ID
    info = TEMPLATES_REGISTRY.get(tid)
    if info is not None and (TEMPLATES_DIR / info["template"]).exists():
        return tid, info
    if tid != DEFAULT_TEMPLATE_ID:
        logger.warning("unknown/missing resume template %r; falling back to %r", template_id, DEFAULT_TEMPLATE_ID)
    return DEFAULT_TEMPLATE_ID, TEMPLATES_REGISTRY[DEFAULT_TEMPLATE_ID]


def get_available_templates() -> list[dict[str, Any]]:
    """Return all available resume templates with metadata for selection gallery."""
    templates = []
    for tid, info in TEMPLATES_REGISTRY.items():
        templates.append({
            "id": tid,
            "name": info["name"],
            "category": info.get("category", "Modern"),
            "badge": info.get("badge", ""),
            "font_family": info.get("font_family", "Nunito"),
            "primary_color": info.get("primary_color", "#137f96"),
            "description": info.get("description", ""),
            "features": info.get("features", []),
            "template_file": info.get("template", ""),
        })
    return templates


DEFAULT_SECTION_ORDER = ["summary", "experience", "education", "skills", "projects", "certifications"]

SECTION_PATTERNS = {
    "summary": re.compile(r'(?:<!--\s*PROFILE[^\n]*-->\s*)?<h2>(?:(?!</h2>).)*</h2>\s*<p[^>]*><!--REGION:SUMMARY-->.*?<!--END:SUMMARY--></p>', re.DOTALL | re.I),
    "experience": re.compile(r'(?:<!--\s*EMPLOYMENT[^\n]*-->\s*)?<h2>(?:(?!</h2>).)*</h2>\s*<!--REGION:EXPERIENCE-->.*?<!--END:EXPERIENCE-->', re.DOTALL | re.I),
    "education": re.compile(r'(?:<!--\s*EDUCATION[^\n]*-->\s*)?(?:<h2>(?:(?!</h2>).)*</h2>\s*)?<!--REGION:EDUCATION-->.*?<!--END:EDUCATION-->|<!--REGION:EDUCATION-->\s*<h2>(?:(?!</h2>).)*</h2>.*?<!--END:EDUCATION-->', re.DOTALL | re.I),
    "skills": re.compile(r'(?:<!--\s*SKILLS[^\n]*-->\s*)?<h2>(?:(?!</h2>).)*</h2>\s*<ul[^>]*><!--REGION:SKILLS-->.*?<!--END:SKILLS--></ul>', re.DOTALL | re.I),
    "projects": re.compile(r'(?:<!--\s*PROJECTS[^\n]*-->\s*)?<h2>(?:(?!</h2>).)*</h2>\s*<!--REGION:PROJECTS-->.*?<!--END:PROJECTS-->', re.DOTALL | re.I),
    "certifications": re.compile(r'(?:<!--\s*CERTIFICATIONS[^\n]*-->\s*)?(?:<h2>(?:(?!</h2>).)*</h2>\s*)?<!--REGION:CERTIFICATIONS-->.*?<!--END:CERTIFICATIONS-->', re.DOTALL | re.I),
}


def detect_section_order(doc_html: str | BuiltResume | None) -> list[str]:
    """Extract the current order of sections in a resume HTML document."""
    if doc_html is None:
        return list(DEFAULT_SECTION_ORDER)
    if hasattr(doc_html, "html_content"):
        if getattr(doc_html, "section_order", None):
            return list(doc_html.section_order)
        doc_html = getattr(doc_html, "html_content", "") or ""
    elif not isinstance(doc_html, str):
        doc_html = str(doc_html)

    positions: dict[str, int] = {}
    region_map = {
        "summary": "SUMMARY",
        "experience": "EXPERIENCE",
        "education": "EDUCATION",
        "skills": "SKILLS",
        "projects": "PROJECTS",
        "certifications": "CERTIFICATIONS",
    }
    for sec_name, reg_name in region_map.items():
        m = re.search(rf'<!--REGION:{reg_name}-->', doc_html or "", re.I)
        if m:
            positions[sec_name] = m.start()

    ordered = sorted(positions.keys(), key=lambda k: positions[k])
    for k in DEFAULT_SECTION_ORDER:
        if k not in ordered:
            ordered.append(k)
    return ordered


def reorder_html_sections(doc_html: str, target_order: list[str] | str | None) -> str:
    """Rearrange the sections in resume HTML according to target_order."""
    if not doc_html or not target_order:
        return doc_html

    if isinstance(target_order, str):
        parsed_order = [s.strip().lower() for s in target_order.replace(";", ",").split(",") if s.strip()]
    else:
        parsed_order = [str(s).strip().lower() for s in target_order if str(s).strip()]

    if not parsed_order:
        return doc_html

    # Extract header / metadata prefix up to the first section
    first_section_pos = None
    for pat in SECTION_PATTERNS.values():
        m = pat.search(doc_html)
        if m:
            if first_section_pos is None or m.start() < first_section_pos:
                first_section_pos = m.start()

    if first_section_pos is not None:
        prefix = doc_html[:first_section_pos]
    else:
        # Fallback to header match
        header_match = re.search(r'^(.*?<!--\s*HEADER[^\n]*-->.*?</div>\s*</div>\s*)', doc_html, re.DOTALL | re.I)
        if not header_match:
            header_match = re.search(r'^(.*?<div class="header">.*?</div>\s*)', doc_html, re.DOTALL | re.I)
        if not header_match:
            return doc_html
        prefix = header_match.group(1)

    blocks: dict[str, str] = {}
    for name, pat in SECTION_PATTERNS.items():
        m = pat.search(doc_html)
        if m:
            blocks[name] = m.group(0).strip()
        else:
            # Fallback by region marker
            reg_m = re.search(rf'(?:<h2>.*?</h2>\s*)?<!--REGION:{name.upper()}-->.*?<!--END:{name.upper()}-->', doc_html, re.DOTALL | re.I)
            if reg_m:
                blocks[name] = reg_m.group(0).strip()

    active_order = list(parsed_order)
    for k in DEFAULT_SECTION_ORDER:
        if k not in active_order:
            active_order.append(k)

    ordered_content: list[str] = []
    for k in active_order:
        if k in blocks and blocks[k]:
            ordered_content.append(f"  {blocks[k]}")

    if not ordered_content:
        return doc_html

    body_joined = "\n\n".join(ordered_content)
    return f"{prefix.rstrip()}\n\n{body_joined}\n\n</body>\n</html>"


@dataclass
class ResumeContent:
    """The content half of resume generation: all tailored sections, no template.

    Rendered into any registered template via render_resume(). Keeping content
    separate from rendering is what makes future templates pluggable.
    """

    template_id: str
    summary_html: str
    skills_html: str
    projects_html: str
    education_html: str
    experience_html: str | None  # None -> keep the template's own EXPERIENCE region
    header_title: str
    certifications_html: str = ""
    links_html: str | None = None  # None -> keep the template's own LINKS region
    selected_projects: list[str] = field(default_factory=list)
    section_order: list[str] | str | None = None


@dataclass
class BuiltResume:
    variant: str
    html_content: str
    resume_text: str
    selected_projects: list[str] = field(default_factory=list)
    html_path: Path | None = None
    pdf_path: Path | None = None
    section_order: list[str] = field(default_factory=list)


def select_variant(job_title: str, jd_text: str) -> str:
    """Select the best template variant based on role and JD text."""
    combined = f"{job_title} {jd_text}".lower()

    ai_keywords = [
        "ai", "ml", "machine learning", "deep learning", "nlp", "computer vision",
        "tensorflow", "pytorch", "cnn", "lstm", "data scientist", "llm"
    ]
    fs_keywords = [
        "full stack", "frontend", "backend", "react", "node", "express",
        "next.js", "django", "fastapi", "vue", "web developer", "mern"
    ]

    ai_score = sum(1 for k in ai_keywords if re.search(rf"\b{re.escape(k)}\b", combined))
    fs_score = sum(1 for k in fs_keywords if re.search(rf"\b{re.escape(k)}\b", combined))

    return DEFAULT_TEMPLATE_ID


def html_to_plain_text(html_content: str) -> str:
    """Extract clean text content from HTML for ATS scoring."""
    # Remove script and style elements
    clean = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html_content, flags=re.DOTALL | re.IGNORECASE)
    # Replace block elements and line breaks with newlines
    clean = re.sub(r"</?(p|div|li|h[1-6]|br)[^>]*>", "\n", clean, flags=re.IGNORECASE)
    # Remove remaining HTML tags
    clean = _TAG_RE.sub(" ", clean)
    # Unescape HTML entities
    clean = _html.unescape(clean)
    # Collapse extra whitespace
    lines = [line.strip() for line in clean.splitlines() if line.strip()]
    return "\n".join(lines)


def _match_skill_in_text(skill: str, text_lower: str) -> bool:
    """Robust keyword matching handling acronyms, hyphens, and common aliases."""
    sl = skill.lower()
    if sl == "react.js" or sl == "react":
        return bool(re.search(r"\b(react(\.js)?)\b", text_lower))
    if sl == "node.js" or sl == "node":
        return bool(re.search(r"\b(node(\.js)?)\b", text_lower))
    if sl == "express.js" or sl == "express":
        return bool(re.search(r"\b(express(\.js)?)\b", text_lower))
    if sl == "tailwind css" or sl == "tailwind":
        return bool(re.search(r"\b(tailwind(\s*css)?)\b", text_lower))
    if sl in ("scikit-learn", "sklearn"):
        return bool(re.search(r"\b(scikit-learn|sklearn)\b", text_lower))
    if sl in ("k-means", "kmeans"):
        return bool(re.search(r"\b(k-means|kmeans)\b", text_lower))
    if sl == "ann":
        return bool(re.search(r"\b(ann|artificial neural network|artificial neural networks)\b", text_lower))
    if sl == "cnn":
        return bool(re.search(r"\b(cnn|convolutional neural network|convolutional neural networks)\b", text_lower))
    if sl in ("rest apis", "rest api", "apis", "rest"):
        return bool(re.search(r"\b(rest(\s*apis?)?|restful(\s*apis?)?|apis?)\b", text_lower))
    if sl in ("javascript", "js"):
        return bool(re.search(r"\b(javascript|js|es6)\b", text_lower))
    if sl in ("typescript", "ts"):
        return bool(re.search(r"\b(typescript|ts)\b", text_lower))
    if sl in ("c++", "cpp"):
        return bool(re.search(r"(?<![a-z0-9])(c\+\+|cpp)(?![a-z0-9])", text_lower))
    if sl in ("aws", "amazon web services"):
        return bool(re.search(r"\b(aws|amazon web services)\b", text_lower))
    if sl in ("azure", "microsoft azure"):
        return bool(re.search(r"\b(azure|microsoft azure)\b", text_lower))
    if sl in ("data pipelines", "data pipeline", "etl"):
        return bool(re.search(r"\b(data\s+pipelines?|etl(\s+pipelines?)?|data\s+processing)\b", text_lower))
    if sl in ("vector search", "vector embeddings", "embeddings"):
        return bool(re.search(r"\b(vector\s+search|vector\s+embeddings?|embeddings?)\b", text_lower))
    if sl in ("knowledge graphs", "knowledge graph"):
        return bool(re.search(r"\b(knowledge\s+graphs?)\b", text_lower))
    if sl in ("voice ai", "vapi"):
        return bool(re.search(r"\b(voice\s+ai|vapi|conversational\s+ai)\b", text_lower))
    if sl in ("ssrf protection", "security"):
        return bool(re.search(r"\b(ssrf|security|appsec|cybersecurity)\b", text_lower))
    if sl == "sql":
        return bool(re.search(r"\b(sql|relational databases?)\b", text_lower))
    if sl == "rag":
        return bool(re.search(r"\b(rag|retrieval-augmented generation|retrieval augmented generation)\b", text_lower))
    if sl in ("llm", "llms"):
        return bool(re.search(r"\b(llms?|large language models?)\b", text_lower))

    # Alias-map fallback
    for phrasing in all_phrasings(skill):
        if phrasing_in_text(phrasing, text_lower):
            return True
    return False


def get_categorized_candidate_skills(candidate: Candidate) -> dict[str, list[str]]:
    """Organize ALL candidate-attested skills from the Knowledge Base into balanced categories."""
    raw_skills = candidate.skills or []
    categorized: dict[str, list[str]] = {cat: [] for cat in CANDIDATE_SKILL_CATEGORIES}
    categorized["Tools & Technologies"] = []

    seen_canons: set[str] = set()

    for s in raw_skills:
        s_clean = str(s).strip()
        if not s_clean:
            continue
        norm = normalize_skill(s_clean)
        canon = canonical_skill(s_clean)
        if canon in seen_canons:
            continue
        seen_canons.add(canon)

        disp = canonical_display_name(s_clean)
        assigned = False

        # Match against predefined categories first
        for cat_name, cat_members in CANDIDATE_SKILL_CATEGORIES.items():
            if any(normalize_skill(m) == norm or canonical_skill(m) == canon or skill_matches(m, s_clean) for m in cat_members):
                categorized[cat_name].append(disp)
                assigned = True
                break

        if not assigned:
            # Smart semantic heuristic for custom/unmapped candidate skills
            s_low = s_clean.lower()
            if any(k in s_low for k in ["ai", "ml", "neural", "vision", "learn", "model", "data", "keras", "torch", "tensor"]):
                categorized["AI & Machine Learning"].append(disp)
            elif any(k in s_low for k in ["rag", "agent", "llm", "prompt", "graph", "vector", "gemini", "groq", "gpt"]):
                categorized["Agentic AI & GenAI"].append(disp)
            elif any(k in s_low for k in ["api", "cloud", "aws", "azure", "docker", "k8s", "kubernetes", "server", "fastapi", "backend", "pipe", "sec", "devops", "ci"]):
                categorized["Backend, Cloud & APIs"].append(disp)
            elif any(k in s_low for k in ["db", "sql", "mongo", "redis", "store", "base", "postgres"]):
                categorized["Databases & Storage"].append(disp)
            elif any(k in s_low for k in ["react", "vue", "front", "web", "html", "css", "tailwind", "ui"]):
                categorized["Frontend & Web"].append(disp)
            elif any(k in s_low for k in ["python", "c++", "cpp", "java", "script", "lang", "rust", "golang", "typescript"]):
                categorized["Programming Languages"].append(disp)
            else:
                categorized["Tools & Technologies"].append(disp)

    # Remove empty categories
    return {k: v for k, v in categorized.items() if v}


def _category_matches(query: str, category_name: str) -> bool:
    """Fuzzy check if query (e.g. 'web', 'frontend', 'db', 'ai') matches category name."""
    q = query.lower().strip()
    c = category_name.lower().strip()
    if not q or not c:
        return False
    if q == c or q in c or c in q:
        return True
    aliases = {
        "Frontend & Web": ["frontend", "web", "react", "html", "css", "tailwind", "ui", "full-stack", "fullstack"],
        "Databases & Storage": ["database", "databases", "db", "storage", "postgres", "mongodb", "sql", "redis", "chromadb"],
        "Backend, Cloud & APIs": ["backend", "cloud", "api", "apis", "fastapi", "docker", "aws", "azure", "devops", "ci/cd"],
        "AI & Machine Learning": ["ai", "ml", "machine learning", "deep learning", "cv", "vision", "neural", "pytorch", "tensorflow"],
        "Agentic AI & GenAI": ["agentic", "genai", "gen ai", "rag", "llm", "llms", "agents", "langchain", "groq", "gemini"],
        "Programming Languages": ["programming", "languages", "coding", "python", "c++", "cpp", "javascript", "typescript"],
    }
    for cat_key, words in aliases.items():
        if cat_key.lower() == c:
            if any(w == q or (len(w) >= 3 and w in q) for w in words):
                return True
    return False


def generate_tailored_skills_html(
    candidate: Candidate,
    jd_text: str,
    job_title: str = "",
    variant: str = "se_al",
    extra_target_skills: list[str] | None = None,
    allowed_categories: list[str] | None = None,
    excluded_categories: list[str] | None = None,
    excluded_skills: set[str] | list[str] | None = None,
    max_skills_per_cat: int = 6,
    max_categories: int | None = None,
) -> str:
    """Generate ATS-optimized skills list, prioritizing JD-matched skills and categories.
    Guaranteed: 100% strictly candidate-attested skills (plus user custom additions), dynamically categorized.
    """
    jd_lower = f"{job_title} {jd_text}".lower()
    target_set = {s.lower().strip() for s in (extra_target_skills or []) if s and s.strip()}
    
    # Include any custom requested skills in the candidate skill set for this tailored build
    custom_additions = [s.strip() for s in (extra_target_skills or []) if s and s.strip()]
    merged_skills = list(candidate.skills or [])
    for cs in custom_additions:
        if not any(cs.lower() == s.lower() for s in merged_skills):
            merged_skills.append(cs)
    
    candidate_skills_set = {s.lower() for s in merged_skills}

    excluded_skills_set = {s.lower().strip() for s in (excluded_skills or []) if s and s.strip()}
    allowed_cats_lower = [c.lower().strip() for c in (allowed_categories or []) if c and c.strip()]
    excluded_cats_lower = [c.lower().strip() for c in (excluded_categories or []) if c and c.strip()]

    # Dynamically extract all categorized skills including additions
    temp_candidate = Candidate(
        name=candidate.name,
        email=candidate.email,
        phone=candidate.phone,
        skills=merged_skills,
        projects=candidate.projects,
        experience=candidate.experience,
        education=candidate.education,
        certifications=candidate.certifications,
        links=candidate.links,
    )
    candidate_categories = get_categorized_candidate_skills(temp_candidate)
    scored_cats: list[tuple[float, str, list[str]]] = []

    is_frontend_role = variant == "se_fd" or any(k in jd_lower for k in ["frontend", "react", "next.js", "vue", "web developer", "ui", "ux", "mern", "full stack", "fullstack"])
    is_vision_role = any(k in jd_lower for k in ["computer vision", "vision", "image processing", "cnn", "convolutional", "object detection", "opencv", "segmentation", "pneumonia", "x-ray"])
    is_agentic_role = any(k in jd_lower for k in ["agentic", "rag", "retrieval", "vector search", "llm", "large language model", "agents", "langchain", "langgraph", "vapi", "voice ai", "groq", "genai", "generative ai", "ai engineer", "ai developer", "artificial intelligence", "ai intern", "ai specialist"])
    is_backend_role = any(k in jd_lower for k in ["backend", "python developer", "fastapi", "rest api", "microservice", "ssrf", "http", "api developer", "data pipelines"])
    is_ml_role = variant == "se_am" or any(k in jd_lower for k in ["machine learning", "ml engineer", "data scientist", "deep learning", "predictive", "scikit-learn", "sklearn"])
    is_software_engineer_role = (
        not (is_frontend_role or is_vision_role or is_agentic_role or is_backend_role or is_ml_role)
        or any(k in jd_lower for k in ["software engineer", "software developer", "associate software", "ase", "engineer", "developer"])
    )

    for cat_name, raw_skills in candidate_categories.items():
        # Check allowed / excluded categories using robust alias matching
        if allowed_cats_lower and not any(_category_matches(ac, cat_name) for ac in allowed_cats_lower):
            continue
        if excluded_cats_lower and any(_category_matches(ec, cat_name) for ec in excluded_cats_lower):
            continue

        # Filter to candidate-attested skills minus exclusions
        attested = [
            s for s in raw_skills
            if (s.lower() in candidate_skills_set
                or any(skill_matches(s, cs) for cs in candidate_skills_set))
            and s.lower() not in excluded_skills_set
            and not any(ex == s.lower() or (len(ex) >= 3 and ex in s.lower()) for ex in excluded_skills_set)
        ]
        if not attested:
            continue

        # Split into JD matched / targeted vs unmatched
        matched = [
            s for s in attested
            if s.lower() in target_set or any(t in s.lower() or s.lower() in t for t in target_set) or _match_skill_in_text(s, jd_lower)
        ]
        unmatched = [s for s in attested if s not in matched]

        # Prioritize all matched keywords, plus complementary skills for breadth
        unmatched_fill = max(0, max_skills_per_cat - len(matched))
        combined = (matched + unmatched[:unmatched_fill])[:max_skills_per_cat]

        if not combined:
            continue

        # Score category relevance
        score = float(len(matched) * 15.0)
        
        # Strong domain alignment category elevation
        cat_upper = cat_name.upper()
        if is_frontend_role:
            if "FRONTEND" in cat_upper or "WEB" in cat_upper:
                score += 25.0
            elif "PROGRAMMING" in cat_upper:
                score += 10.0
            elif "BACKEND" in cat_upper:
                score += 5.0
        elif is_vision_role or is_ml_role:
            if "AI" in cat_upper or "MACHINE" in cat_upper:
                score += 25.0
            elif "PROGRAMMING" in cat_upper:
                score += 10.0
            elif "AGENTIC" in cat_upper:
                score += 5.0
        elif is_agentic_role:
            if "AGENTIC" in cat_upper or "GENAI" in cat_upper:
                score += 25.0
            elif "BACKEND" in cat_upper:
                score += 10.0
            elif "AI" in cat_upper or "MACHINE" in cat_upper:
                score += 5.0
        elif is_backend_role:
            if "BACKEND" in cat_upper or "CLOUD" in cat_upper:
                score += 25.0
            elif "DATABASES" in cat_upper:
                score += 15.0
            elif "PROGRAMMING" in cat_upper:
                score += 10.0
        elif is_software_engineer_role:
            if "PROGRAMMING" in cat_upper:
                score += 25.0
            elif "BACKEND" in cat_upper or "FRAMEWORKS" in cat_upper:
                score += 20.0
            elif "AI" in cat_upper or "MACHINE" in cat_upper or "AGENTIC" in cat_upper:
                score += 15.0
            elif "DATABASES" in cat_upper or "CLOUD" in cat_upper:
                score += 12.0
            elif "FRONTEND" in cat_upper or "WEB" in cat_upper:
                score += 10.0

        scored_cats.append((score, cat_name, combined))

    scored_cats.sort(key=lambda x: x[0], reverse=True)

    # Cross-category deduplication: canonical uniqueness across categories
    seen_canonical: set[str] = set()
    deduped_cats: list[tuple[float, str, list[str]]] = []
    for score, cat_name, skills in scored_cats:
        kept: list[str] = []
        for s in skills:
            canon = canonical_skill(s)
            if canon in seen_canonical:
                continue
            seen_canonical.add(canon)
            kept.append(s)
        if kept:
            deduped_cats.append((score, cat_name, kept))
    scored_cats = deduped_cats

    if max_categories and len(scored_cats) > max_categories:
        scored_cats = scored_cats[:max_categories]

    html_lines = []
    for _, cat_name, skills in scored_cats:
        skills_str = ", ".join(canonical_display_name(s) for s in skills)
        html_lines.append(f"<li><strong>{cat_name}</strong> — {skills_str}</li>")

    return "\n    ".join(html_lines)


def select_projects(
    candidate: Candidate,
    variant_domain: str,
    jd_text: str,
    job_title: str = "",
    max_projects: int = 5,
    recommended_project_ids: list[str] | None = None,
    use_llm: bool = True,
) -> list[dict]:
    """Score and select candidate projects against the JD using domain intelligence,
    LLM reasoning (Qwen 27B), and candidate profile relevance.
    Guarantees that Frontend, Computer Vision, Agentic AI, and Backend roles select
    distinct, highly relevant projects rather than identical duplicates.
    """
    all_projects = candidate.projects or []
    if not all_projects:
        return []

    jd_lower = f"{job_title} {jd_text}".lower()

    def _has_kw(text: str, kws: list[str]) -> bool:
        for kw in kws:
            if len(kw) <= 4 or kw in ("ui", "ux", "cnn", "rag", "llm", "api", "css", "vue", "html", "mern"):
                if re.search(rf"\b{re.escape(kw)}\b", text, re.I):
                    return True
            else:
                if kw in text:
                    return True
        return False

    # Detect domain profiles from target role and JD
    is_frontend = _has_kw(jd_lower, [
        "frontend", "react", "next.js", "vue", "web developer", "ui", "ux", "mern",
        "full stack", "fullstack", "javascript", "css", "tailwind", "html", "web application"
    ])
    is_vision = _has_kw(jd_lower, [
        "computer vision", "vision", "image processing", "cnn", "convolutional",
        "object detection", "opencv", "segmentation", "pneumonia", "x-ray", "image classification"
    ])
    is_agentic = _has_kw(jd_lower, [
        "agentic", "rag", "retrieval", "vector search", "llm", "large language model",
        "agents", "langchain", "langgraph", "vapi", "voice ai", "groq", "genai", "generative ai", "multi-agent",
        "ai engineer", "ai developer", "artificial intelligence", "ai intern", "ai specialist"
    ])
    is_backend = _has_kw(jd_lower, [
        "backend", "python developer", "fastapi", "rest api", "microservice", "ssrf",
        "http", "api developer", "data pipelines", "cloud backend", "security package"
    ])
    is_ml_ds = _has_kw(jd_lower, [
        "machine learning", "ml engineer", "data scientist", "deep learning", "predictive",
        "scikit-learn", "sklearn", "classification", "clustering", "k-means", "tabular"
    ])
    is_general_se = (
        not (is_frontend or is_vision or is_agentic or is_backend or is_ml_ds)
        or _has_kw(jd_lower, ["software engineer", "software developer", "associate software", "ase", "graduate engineer", "junior developer", "engineer", "developer"])
    )

    rec_ids = [str(x).strip().lower() for x in (recommended_project_ids or []) if str(x).strip()]
    penalized_ids: set[str] = set()

    # --- Phase A: Algorithmic Domain & Keyword Scoring ---
    scored: list[tuple[float, dict]] = []
    for p in all_projects:
        p_id = (p.get("id") or p.get("short_name") or p.get("name") or "").lower().replace(" ", "_")
        p_name = (p.get("name") or "").lower()
        p_short = (p.get("short_name") or "").lower()
        p_sub = (p.get("subtitle") or "").lower()
        p_skills = [s.lower() for s in p.get("skills", [])]
        p_tags = [t.lower() for t in p.get("tags", [])]
        domains = p.get("domains", [])
        score = 0.0

        # Direct recommendation boost from Stage 1 LLM
        if any(r == p_id or r in p_id or r == p_short or r in p_short or r in p_name for r in rec_ids):
            score += 25.0

        # Domain specific targeted boost & penalty
        if is_frontend:
            if "hrconnect" in p_id or "react" in p_skills or "mern" in p_tags:
                score += 25.0
            elif "safe_web_access" in p_id or "http" in p_sub:
                score += 12.0
            elif "signal_reach" in p_id or "civicheat" in p_id or "nexus" in p_id:
                score += 8.0
            elif "x-ray" in p_id or "xray" in p_id or "x_ray" in p_id or "chest" in p_id or "income_classification" in p_id:
                score -= 25.0
                for k in (p.get("id"), p.get("name"), p.get("short_name")):
                    if k:
                        penalized_ids.add(str(k).strip().lower())
                        penalized_ids.add(str(k).strip().lower().replace(" ", "_"))
        elif is_vision:
            if "x-ray" in p_id or "xray" in p_id or "x_ray" in p_id or "chest" in p_id or "cnn" in p_skills or "vision" in p_id:
                score += 30.0
            elif "recommendation" in p_id or "pytorch" in p_skills:
                score += 15.0
            elif "income_classification" in p_id or "deep learning" in p_sub:
                score += 12.0
            elif "nemetron" in p_id:
                score += 8.0
            elif "hrconnect" in p_id:
                score -= 30.0
                for k in (p.get("id"), p.get("name"), p.get("short_name")):
                    if k:
                        penalized_ids.add(str(k).strip().lower())
                        penalized_ids.add(str(k).strip().lower().replace(" ", "_"))
        elif is_agentic:
            if "nemetron" in p_id or "nexus" in p_id:
                score += 22.0
            elif "civicheat" in p_id or "signal_reach" in p_id:
                score += 18.0
            elif "safe_web_access" in p_id:
                score += 12.0
            elif "hrconnect" in p_id or "x-ray" in p_id or "xray" in p_id or "x_ray" in p_id or "chest" in p_id:
                score -= 15.0
                for k in (p.get("id"), p.get("name"), p.get("short_name")):
                    if k:
                        penalized_ids.add(str(k).strip().lower())
                        penalized_ids.add(str(k).strip().lower().replace(" ", "_"))
        elif is_backend:
            if "safe_web_access" in p_id or "signal_reach" in p_id:
                score += 22.0
            elif "nexus" in p_id or "civicheat" in p_id:
                score += 16.0
            elif "nemetron" in p_id:
                score += 12.0
            elif "x-ray" in p_id or "xray" in p_id or "x_ray" in p_id or "chest" in p_id:
                score -= 15.0
                for k in (p.get("id"), p.get("name"), p.get("short_name")):
                    if k:
                        penalized_ids.add(str(k).strip().lower())
                        penalized_ids.add(str(k).strip().lower().replace(" ", "_"))
        elif is_ml_ds:
            if "recommendation" in p_id or "income_classification" in p_id:
                score += 22.0
            elif "x-ray" in p_id or "xray" in p_id or "x_ray" in p_id or "chest" in p_id or "nemetron" in p_id:
                score += 15.0
            elif "hrconnect" in p_id:
                score -= 20.0
                for k in (p.get("id"), p.get("name"), p.get("short_name")):
                    if k:
                        penalized_ids.add(str(k).strip().lower())
                        penalized_ids.add(str(k).strip().lower().replace(" ", "_"))
        elif is_general_se:
            if "hrconnect" in p_id or "nemetron" in p_id:
                score += 18.0
            elif "safe_web_access" in p_id:
                score += 16.0
            elif "civicheat" in p_id:
                score += 15.0
            elif "signal_reach" in p_id or "nexus" in p_id:
                score += 14.0
            elif "recommendation" in p_id:
                score += 10.0

        # General domain alignment
        if variant_domain != "all" and domains and variant_domain in domains:
            score += 4.0

        # Skill matches against JD (+4.0 per skill)
        for s in p.get("skills", []):
            if _match_skill_in_text(s, jd_lower):
                score += 4.0

        # Tag matches (+3.0 per tag)
        for t in p.get("tags", []):
            if _match_skill_in_text(t, jd_lower):
                score += 3.0

        # Title / Subtitle keywords (+2.0)
        for word in re.findall(r"\b[a-z]{3,}\b", f"{p_name} {p_sub}"):
            if word in jd_lower and word not in {"and", "the", "for", "with", "system", "using"}:
                score += 2.0

        # Live deployed link proof-of-work bonus
        link = (p.get("link") or "").lower()
        if "vercel.app" in link or "pypi.org" in link:
            score += 1.0

        # Minimal priority tie breaker
        priority = p.get("priority", 99)
        score += max(0.0, (10 - priority) * 0.005)

        scored.append((score, p))

    scored.sort(key=lambda x: x[0], reverse=True)
    algo_selected = [p for _, p in scored]

    # --- Phase B: LLM Thinking & Selection (Groq Qwen 27B) ---
    if use_llm and config.LLM_API_KEY and len(all_projects) > max_projects:
        try:
            from app.llm import call_llm_json
            nonce = uuid.uuid4().hex[:8]
            projects_summary = [
                {
                    "id": p.get("id") or (p.get("short_name") or p.get("name") or "").lower().replace(" ", "_"),
                    "name": p.get("name"),
                    "subtitle": p.get("subtitle"),
                    "skills": p.get("skills", []),
                    "link": p.get("link"),
                    "bullet": p.get("bullet", "")[:140],
                }
                for p in all_projects
            ]

            system_prompt = (
                "You are an expert ATS technical recruiter and resume ranking engine. "
                f"Select the top {max_projects} projects from the candidate's exact project library "
                "that provide the highest keyword relevance, technical evidence, and maximum ATS score "
                "for the target job description.\n"
                "CRITICAL RULES:\n"
                "1. ONLY select project IDs from the provided candidate projects.\n"
                "2. Prioritize projects that directly prove technologies required by the role.\n"
                "3. Strictly avoid selecting projects from orthogonal domains (e.g. do NOT select web/MERN apps for AI/Vision/Deep Learning roles, and do NOT select medical/CNN models for Web/Frontend roles).\n"
                "4. Output ONLY a valid JSON object with key 'selected_ids': [\"id1\", \"id2\", ...]."
            )

            sparse_note = ""
            jd_words = len((jd_text or "").strip().split())
            if jd_words < 35 or len((jd_text or "").strip()) < 250:
                sparse_note = (
                    f"\nNOTE: The job description is brief/minimal ({jd_words} words). "
                    f"Select the top {max_projects} projects from the candidate's catalog that best demonstrate "
                    f"elite technical excellence, practical depth, and high value for the role '{job_title or 'Software Engineer'}'.\n"
                )

            user_prompt = (
                f"TARGET ROLE: {job_title or 'Software Engineer'}\n"
                f"CANDIDATE PROJECTS:\n{json.dumps(projects_summary, indent=1)}\n\n"
                f"{sparse_note}"
                f"JOB DESCRIPTION (untrusted, fenced <<{nonce}>>):\n<<{nonce}>>\n{jd_text[:3500]}\n<<{nonce}>>\n\n"
                f"Return the top {max_projects} project IDs ranked by ATS relevance."
            )

            llm_res = call_llm_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                model=config.FAST_LLM_MODEL,
                temperature=0.1,
                timeout=25.0,
            )

            if llm_res and isinstance(llm_res.get("selected_ids"), list):
                selected_ids = [str(x).strip().lower() for x in llm_res["selected_ids"] if str(x).strip()]
                proj_by_id = {}
                for p in all_projects:
                    for k in (p.get("id"), p.get("name"), p.get("short_name")):
                        if k:
                            proj_by_id[str(k).strip().lower()] = p
                            proj_by_id[str(k).strip().lower().replace(" ", "_")] = p

                for sc, p in scored:
                    if sc < 0:
                        for k in (p.get("id"), p.get("name"), p.get("short_name")):
                            if k:
                                penalized_ids.add(str(k).strip().lower())
                                penalized_ids.add(str(k).strip().lower().replace(" ", "_"))

                llm_chosen: list[dict] = []
                seen_ids: set[str] = set()

                for sid in selected_ids:
                    p_match = proj_by_id.get(sid)
                    if not p_match:
                        # Try fuzzy key match
                        for k, p_candidate in proj_by_id.items():
                            if sid in k or k in sid:
                                p_match = p_candidate
                                break
                    if p_match:
                        p_id_clean = (p_match.get("id") or p_match.get("short_name") or p_match.get("name") or "").lower().replace(" ", "_")
                        p_name_clean = (p_match.get("name") or "").strip().lower()
                        is_pen = any(pen and (pen in p_id_clean or pen in p_name_clean or p_id_clean in pen) for pen in penalized_ids)
                        if is_pen:
                            continue  # Ignore severely mismatched projects
                        p_key = p_match.get("name") or p_match.get("id")
                        if p_key not in seen_ids:
                            llm_chosen.append(p_match)
                            seen_ids.add(p_key)
                    if len(llm_chosen) >= max_projects:
                        break

                # Fill any remaining slots from top algorithmic scorers (excluding penalized)
                for p in algo_selected:
                    p_id_clean = (p.get("id") or p.get("short_name") or p.get("name") or "").lower().replace(" ", "_")
                    p_name_clean = (p.get("name") or "").strip().lower()
                    is_pen = any(pen and (pen in p_id_clean or pen in p_name_clean or p_id_clean in pen) for pen in penalized_ids)
                    if is_pen and len(llm_chosen) >= 1:
                        continue
                    p_key = p.get("name") or p.get("id")
                    if p_key not in seen_ids:
                        llm_chosen.append(p)
                        seen_ids.add(p_key)
                    if len(llm_chosen) >= max_projects:
                        break

                if len(llm_chosen) >= min(max_projects, len(all_projects)):
                    return llm_chosen[:max_projects]

        except Exception as exc:
            logger.warning("LLM project selection fallback to algorithmic: %s", exc)

    return algo_selected[:max_projects]


def render_projects_html(projects: list[dict]) -> str:
    """Format selected projects into structured HTML with link icons."""
    if not projects:
        return ""
    blocks: list[str] = []
    link_svg = '<svg class="link-icon" viewBox="0 0 24 24"><path d="M3.9 12a3.1 3.1 0 0 1 3.1-3.1h4V7h-4a5 5 0 1 0 0 10h4v-1.9h-4A3.1 3.1 0 0 1 3.9 12zM8 13h8v-2H8v2zm5-6v1.9h4a3.1 3.1 0 0 1 0 6.2h-4V17h4a5 5 0 0 0 0-10h-4z"/></svg>'
    for p in projects:
        name = _html.escape(p.get("short_name") or p.get("name") or p.get("title") or "")
        subtitle = _html.escape(p.get("subtitle") or p.get("tagline") or "")
        bullet = _html.escape(p.get("bullet") or p.get("description") or p.get("summary") or "")
        link = p.get("link") or p.get("url") or p.get("github")
        if link:
            head = f'<strong><a href="{_html.escape(link)}" target="_blank" style="text-decoration:none;color:inherit;">{name}</a></strong> <a href="{_html.escape(link)}" target="_blank" style="text-decoration:none;color:#137f96;">{link_svg}</a>'
        else:
            head = f"<strong>{name}</strong>"
        if subtitle:
            head += f", <em>{subtitle}</em>"
        blocks.append(
            f'<div class="proj"><div class="head">{head}</div>'
            f"<ul><li>{bullet}</li></ul></div>"
        )
    return "".join(blocks)


def render_experience_html(experiences: list[dict]) -> str:
    """Format selected experiences into structured HTML."""
    if not experiences:
        return ""
    blocks: list[str] = []
    link_svg = '<svg class="link-icon" viewBox="0 0 24 24"><path d="M3.9 12a3.1 3.1 0 0 1 3.1-3.1h4V7h-4a5 5 0 1 0 0 10h4v-1.9h-4A3.1 3.1 0 0 1 3.9 12zM8 13h8v-2H8v2zm5-6v1.9h4a3.1 3.1 0 0 1 0 6.2h-4V17h4a5 5 0 0 0 0-10h-4z"/></svg>'
    
    months = {
        "01": "Jan", "02": "Feb", "03": "Mar", "04": "Apr", "05": "May", "06": "Jun",
        "07": "Jul", "08": "Aug", "09": "Sep", "10": "Oct", "11": "Nov", "12": "Dec"
    }
    def _format_date(d_str: str) -> str:
        if not d_str:
            return ""
        if "-" in d_str:
            parts = d_str.split("-")
            if len(parts) >= 2 and parts[1] in months:
                return f"{months[parts[1]]} {parts[0]}"
        return d_str

    for exp in experiences:
        employer = _html.escape(exp.get("employer") or exp.get("company") or "")
        title = _html.escape(exp.get("title") or exp.get("role") or exp.get("position") or "")
        start_d = _format_date(exp.get("start") or exp.get("start_date") or "")
        end_d = _format_date(exp.get("end") or exp.get("end_date") or "") or "Present"
        date_range = f"{start_d} – {end_d}" if start_d else end_d

        svg_snippet = f" {link_svg}" if "broadstone" in employer.lower() else ""
        head_left = f"<strong>{title}</strong>, <em>{employer}</em>{svg_snippet}"
        
        raw_bullets = exp.get("bullets") or exp.get("highlights") or exp.get("bullet_points") or []
        if isinstance(raw_bullets, str):
            raw_bullets = [raw_bullets]
        bullet_lis = "\n  ".join(f"<li>{_html.escape(b)}</li>" for b in raw_bullets if b)
        
        blocks.append(
            f'<div class="entry">\n'
            f'  <div class="left">{head_left}</div>\n'
            f'  <div class="right">{date_range}</div>\n'
            f'</div>\n'
            f'<ul>\n'
            f'  {bullet_lis}\n'
            f'</ul>'
        )
    return "\n".join(blocks)


def reorder_skills_html(default_skills_html: str, target_skills: list[str]) -> str:
    """Reorder existing skills so that those mentioned in the JD come first."""
    items = _LI_RE.findall(default_skills_html)
    if not items:
        return default_skills_html

    ts = {s.strip().lower() for s in target_skills}
    # Score each <li> item by number of matched target skills
    def _score_item(li_text: str) -> int:
        clean = li_text.lower()
        return sum(1 for s in ts if re.search(rf"\b{re.escape(s)}\b", clean))

    items_sorted = sorted(items, key=_score_item, reverse=True)
    return "".join(f"<li>{i}</li>" for i in items_sorted)



def skills_csv_to_html(skills_csv: str) -> str:
    """Convert comma-separated skills into <li> elements.

    Each item is mapped to its canonical display name first ("ML" ->
    "Machine Learning"), per the user-set display rule, then HTML-escaped.
    """
    if not skills_csv:
        return ""
    items = [s.strip() for s in re.split(r"[,;\n]+", skills_csv) if s.strip()]
    return "".join(f"<li>{_html.escape(canonical_display_name(s))}</li>" for s in items)


def skills_html_to_csv(skills_html: str) -> str:
    """Extract list of skills from <li> elements and join with comma."""
    items = _LI_RE.findall(skills_html or "")
    if items:
        return ", ".join(items)
    # Strip any tags
    clean = _TAG_RE.sub(" ", skills_html or "")
    return ", ".join(s.strip() for s in clean.split() if s.strip())


def extract_regions(template_html: str) -> dict[str, str]:
    return {m.group(1): m.group(2).strip() for m in _REGION_RE.finditer(template_html)}


def replace_regions(
    template_html: str,
    replacements: dict[str, str],
) -> str:
    """Safely replace content in editable regions, keeping markers and locked regions intact."""
    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in replacements and name in EDITABLE_REGIONS:
            new_val = replacements[name].strip()
            return f"<!--REGION:{name}-->{new_val}<!--END:{name}-->"
        return match.group(0)

    updated = _REGION_RE.sub(_sub, template_html)
    # Resolve font directory
    font_uri = FONTS_DIR.resolve().as_uri()
    return updated.replace(FONT_DIR_TOKEN, font_uri)


NON_CS_TERMS = {
    "counsel", "legal", "attorney", "lawyer", "law", "compliance", "medical", "nursing",
    "doctor", "healthcare", "sales", "marketing", "recruiter", "hr", "human resources",
    "accountant", "accounting", "finance", "paralegal", "tax", "audit", "clerk", "content writer",
    "business development", "operations manager", "admin", "administrator"
}

VISA_TERMS = {
    "visa", "sponsorship", "work permit", "work authorization", "authorized to work",
    "citizen", "greencard", "green card", "eu citizen", "uk citizen", "us citizen"
}


def sanitize_cs_role_summary(job_title: str = "", variant: str = "se_am") -> str:
    """Ensure summary strictly describes Computer Science / Software / AI Engineering.
    Disallows any non-CS field or profession (e.g. counsel, legal, sales) and strips
    geographic / regional location labels (EMEA, APAC, US, UK, Remote, etc.).
    """
    default_role = {
        "se_am": "Artificial Intelligence & Machine Learning",
        "se_fd": "Full-Stack Software Development",
        "se_al": "Computer Science & Software Engineering",
    }.get(variant, "Computer Science & Software Engineering")

    if not job_title:
        return default_role

    title_clean = job_title.strip()
    title_lower = title_clean.lower()

    # Reject non-CS fields completely
    if any(re.search(rf"\b{re.escape(term)}\b", title_lower) for term in NON_CS_TERMS):
        return default_role

    # Remove regional/location words like EMEA, APAC, LATAM, USA, UK, Remote, etc.
    cleaned = re.sub(r"\b(EMEA|APAC|LATAM|USA?|UK|EU|Remote|Hybrid|Onsite)\b", "", title_clean, flags=re.IGNORECASE)
    cleaned = re.sub(r"[,/|\-–—]+$", "", cleaned).strip()
    cleaned = re.sub(r"^[,/|\-–—]+", "", cleaned).strip()

    if not cleaned or len(cleaned) < 3:
        return default_role

    return cleaned


def clean_header_title(job_title: str, variant: str = "se_al") -> str:
    """Derive clean technical job title for resume header."""
    combined = (job_title or "").lower()
    if any(k in combined for k in ["counsel", "legal", "sales", "marketing", "medical"]):
        return {"se_am": "AI / ML Engineer", "se_fd": "Full-Stack Developer"}.get(variant, "Software Engineer")
    if any(k in combined for k in ["agentic", "llm", "rag"]):
        return "AI & RAG Engineer"
    if any(k in combined for k in ["machine learning", "ml engineer"]):
        return "Machine Learning Engineer"
    if any(k in combined for k in ["ai", "artificial intelligence"]):
        return "AI Engineer"
    if any(k in combined for k in ["full stack", "fullstack", "frontend"]):
        return "Full-Stack Developer"
    if any(k in combined for k in ["backend", "python"]):
        return "Backend Developer"
    if any(k in combined for k in ["vision", "deep learning"]):
        return "AI & Deep Learning Engineer"
    return {"se_am": "AI / ML Engineer", "se_fd": "Full-Stack Developer"}.get(variant, "Software Engineer")


def sync_candidate_contact_info(html_content: str, candidate: Candidate | None) -> str:
    """Synchronize contact details (LinkedIn, GitHub, Portfolio, Email, Phone) in resume HTML with candidate profile."""
    if not candidate:
        return html_content

    links = getattr(candidate, "links", {}) or {}
    linkedin_url = links.get("linkedin") or "https://www.linkedin.com/in/alexmorgan"
    github_url = links.get("github") or "https://github.com/alexmorgan"
    portfolio_url = links.get("portfolio") or "https://alexmorgan.dev"

    if linkedin_url:
        html_content = re.sub(
            r'(<a\s+href=")[^"]*("(?:\s+[^>]*?)?>\s*LinkedIn\s*</a>)',
            rf'\g<1>{linkedin_url}\g<2>',
            html_content,
            flags=re.IGNORECASE,
        )

    if github_url:
        html_content = re.sub(
            r'(<a\s+href=")[^"]*("(?:\s+[^>]*?)?>\s*GitHub\s*</a>)',
            rf'\g<1>{github_url}\g<2>',
            html_content,
            flags=re.IGNORECASE,
        )

    if portfolio_url:
        html_content = re.sub(
            r'(<a\s+href=")[^"]*("(?:\s+[^>]*?)?>\s*Portfolio\s*</a>)',
            rf'\g<1>{portfolio_url}\g<2>',
            html_content,
            flags=re.IGNORECASE,
        )

    return html_content


_GENERIC_LINK_SVG = (
    '<svg class="link-icon" viewBox="0 0 24 24"><path d="M3.9 12a3.1 3.1 0 0 1 3.1-3.1h4V7h-4a5 5 0 '
    '1 0 0 10h4v-1.9h-4A3.1 3.1 0 0 1 3.9 12zM8 13h8v-2H8v2zm5-6v1.9h4a3.1 3.1 0 0 1 0 6.2h-4V17h4a5 5 '
    '0 0 0 0-10h-4z"/></svg>'
)


def update_links_html(links_html: str, urls: dict[str, str]) -> str:
    """Apply per-label URL updates to a LINKS region's HTML (Phase 10 §U.13).

    For each label -> URL pair with a non-empty URL: replaces the href of the
    existing link item whose anchor text matches the label (case-insensitive),
    or appends a new item with a generic link icon when the label is absent.
    """
    out = links_html or ""
    for label, url in (urls or {}).items():
        url = (url or "").strip()
        label = (label or "").strip()
        if not url or not label:
            continue
        if not _is_safe_link_url(url):
            continue  # refuse javascript:/data:/vbscript: URLs (stored-XSS fix)
        pat = re.compile(
            r'(<a\s+href=")[^"]*("(?:\s+[^>]*?)?>\s*' + re.escape(label) + r'\s*</a>)',
            flags=re.IGNORECASE,
        )
        if pat.search(out):
            out = pat.sub(rf'\g<1>{url}\g<2>', out)
        else:
            out += (
                f'<span class="item"><span class="ico">{_GENERIC_LINK_SVG}</span>'
                f'<a href="{_html.escape(url)}" target="_blank" class="ext-link">'
                f'{_html.escape(label)}</a></span>'
            )
    return out


def generate_role_tailored_summary(
    job_title: str,
    jd_text: str = "",
    variant: str = "se_al",
    candidate: Candidate | None = None,
) -> str:
    """Dynamically construct a 2-part hybrid profile description:
    - Part 1 (~60%): Authentic introduction directly grounded in candidate Knowledge Base / FAST-NUCES CS background.
    - Part 2 (~40%): Role & JD technical specialization and matching skills.
    Never invents non-CS fields, never mentions foreign visas or hallucinated senior titles.
    """
    combined = f"{job_title} {jd_text}".lower()

    # Part 2: Technical specialization & JD alignment
    if any(k in combined for k in ["agentic", "rag", "llm", "retrieval", "vector search", "langchain", "langgraph", "prompt engineering", "generative ai", "multi-agent", "ai engineer", "ai developer", "artificial intelligence", "genai", "ai intern", "ai specialist"]):
        fallback_role = "AI Engineer specializing in Agentic AI architectures, Retrieval-Augmented Generation (RAG), and Large Language Model workflows."
        jd_tailoring = "Experienced in building multi-agent systems, contextual vector retrieval with ChromaDB, and streaming LLM integrations using Groq and OpenAI. Focused on architecting scalable, high-accuracy AI systems."
    elif any(k in combined for k in ["computer vision", "vision", "image processing", "object detection", "cnn", "opencv", "pneumonia", "x-ray", "image classification"]):
        fallback_role = "Computer Vision & Deep Learning Engineer with hands-on expertise developing convolutional neural networks (CNNs) and PyTorch models."
        jd_tailoring = "Experienced in training, evaluating, and optimizing deep learning architectures for real-world image classification and vision pipelines."
    elif any(k in combined for k in ["machine learning", "ml engineer", "data scientist", "data science", "predictive", "scikit-learn", "sklearn", "clustering", "k-means"]):
        fallback_role = "Machine Learning Engineer with extensive experience developing end-to-end predictive modeling pipelines."
        jd_tailoring = "Proficient in PyTorch, Scikit-learn, and feature engineering for real-world datasets, focused on solving complex analytical problems with measurable impact."
    elif any(k in combined for k in ["frontend", "react", "next.js", "vue", "web developer", "ui/ux", "mern", "full stack", "fullstack", "javascript", "tailwind"]):
        fallback_role = "Full-Stack & Frontend Developer with hands-on expertise building responsive web applications using React.js and RESTful APIs."
        jd_tailoring = "Experienced in modern component design, Tailwind CSS, MERN architectures, and integrating robust backend services for seamless user experiences."
    elif any(k in combined for k in ["backend", "python developer", "fastapi", "flask", "microservice", "database", "postgres", "sql", "api developer", "ssrf"]):
        fallback_role = "Backend Software Engineer with strong proficiency in Python, FastAPI, and scalable REST API architectures."
        jd_tailoring = "Experienced in building asynchronous data pipelines, implementing security and SSRF protection protocols, and managing database storage for high-throughput services."
    else:
        fallback_role = "Software Engineer with a strong foundation in Computer Science from FAST National University."
        jd_tailoring = "Proficient in Python, modern web development, and intelligent system architectures, experienced in full-lifecycle software development and REST APIs."

    # Part 1: Candidate authentic intro from Knowledge Base
    kb_summary = (candidate.summary or "").strip() if candidate else ""
    if kb_summary:
        clean_kb = kb_summary.rstrip(". ")
        # If user typed their authentic introduction in KB, merge 60% KB intro + 40% JD specialization
        if len(clean_kb) >= 15:
            return f"{clean_kb}. {jd_tailoring}"

    # Default fallback when KB summary is empty:
    return f"{fallback_role} {jd_tailoring}"


def build_safe_cs_summary(
    raw_summary: str | None,
    job_title: str,
    variant: str = "se_al",
    jd_text: str = "",
    candidate: Candidate | None = None,
) -> str:
    """Validate summary against CS-only & no-visa guardrails and tailor to role."""
    role_tailored_fallback = generate_role_tailored_summary(job_title, jd_text=jd_text, variant=variant, candidate=candidate)
    if not raw_summary or len(raw_summary.strip()) < 25:
        return role_tailored_fallback

    s_lower = raw_summary.lower()
    if any(re.search(rf"\b{re.escape(term)}\b", s_lower) for term in NON_CS_TERMS):
        return role_tailored_fallback
    if any(re.search(rf"\b{re.escape(term)}\b", s_lower) for term in VISA_TERMS):
        return role_tailored_fallback
    for hostile in ["rust", "haskell", "brain surgery", "chief technology officer"]:
        if re.search(rf"\b{re.escape(hostile)}\b", s_lower):
            return role_tailored_fallback
    if not any(k in s_lower for k in [
        "computer science", "software", "ai", "machine learning", "developer", "engineer",
        "engineering", "frontend", "backend", "full-stack", "full stack", "python", "react",
        "data", "web", "deep learning", "vision", "agentic", "rag", "student"
    ]):
        return role_tailored_fallback

    return raw_summary.strip()


def render_education_html(candidate: Candidate) -> str:
    """Render the EDUCATION region from the KB matching the template layout.

    Accepts both the KB-form shape {institution, degree, field, start, end}
    and the profile.yaml shape {degree, field, institution, graduation_date,
    status, start, location}. Returns "" when there is no education entry,
    so the whole section (header included) disappears cleanly.
    """
    import html as _html

    entries = [e for e in (candidate.education or []) if isinstance(e, dict)]
    if not entries:
        return ""

    blocks = ["<h2>Education</h2>"]
    for e in entries:
        degree = (e.get("degree") or e.get("title") or "").strip()
        field = (e.get("field") or e.get("major") or "").strip()
        institution = (e.get("institution") or e.get("school") or e.get("university") or e.get("college") or "").strip()
        location = (e.get("location") or e.get("city") or "").strip()
        start = (e.get("start") or e.get("start_date") or "").strip()
        end = (e.get("end") or e.get("graduation_date") or e.get("end_date") or "").strip()
        if (e.get("status") or "").strip().lower() == "in_progress":
            end = "present"
        if not (degree or institution):
            continue

        degree_label = degree
        if degree_label and not degree_label.endswith(","):
            degree_label = f"{degree_label},"

        date_range = f"{start} – {end}".strip(" –") if (start or end) else ""

        line1 = (
            '  <div class="entry">\n'
            f'    <div class="left"><strong>{_html.escape(degree_label)}</strong></div>\n'
            + (f'    <div class="right">{_html.escape(date_range)}</div>\n' if date_range else "")
            + '  </div>'
        )
        line2_parts = []
        if institution:
            line2_parts.append(f'<div class="left"><em>{_html.escape(institution)}</em></div>')
        if location:
            line2_parts.append(f'<div class="sub-right">{_html.escape(location)}</div>')

        if line2_parts:
            line2 = (
                '  <div class="entry" style="margin-top:0;">\n    '
                + "\n    ".join(line2_parts)
                + "\n  </div>"
            )
            blocks.append(f"{line1}\n{line2}")
        else:
            blocks.append(line1)

    return "\n".join(blocks) if len(blocks) > 1 else ""


def _normalize_cert_dict(raw: object) -> dict:
    """Normalize one certification to {name, organization, link}.

    Accepts the structured dict form and legacy plain-string entries.
    """
    if isinstance(raw, dict):
        return {
            "name": str(raw.get("name") or "").strip(),
            "organization": str(raw.get("organization") or "").strip(),
            "link": str(raw.get("link") or "").strip(),
        }
    if isinstance(raw, str) and raw.strip():
        return {"name": raw.strip(), "organization": "", "link": ""}
    return {"name": "", "organization": "", "link": ""}


def render_certification_li(cert: object) -> str:
    """Render one certification <li>: name, then bold organization, then live link.

    Format: Name — <strong>Organization</strong> <a href="link">link</a>
    (organization and link parts only appear when present; name is never bold).
    """
    c = _normalize_cert_dict(cert)
    name, org, link = c["name"], c["organization"], c["link"]
    if not name:
        return ""
    parts = [_html.escape(name)]
    if org:
        parts.append(f"— <strong>{_html.escape(org)}</strong>")
    if link:
        safe = _html.escape(link, quote=True)
        parts.append(f'<a href="{safe}">link</a>')
    return f"  <li>{' '.join(parts)}</li>"


def render_certifications_html(candidate: Candidate | None) -> str:
    """Render the CERTIFICATIONS region from the KB (never hardcoded, never invented).

    Returns "" when there are no certifications, so the whole section
    (header included) disappears cleanly.
    """
    raw = (candidate.certifications or []) if candidate else []
    items = [render_certification_li(c) for c in raw]
    items = [it for it in items if it]
    if not items:
        return ""
    return f"<h2>Certifications</h2>\n<ul>\n" + "\n".join(items) + "\n</ul>"


def parse_certification_line(line: str) -> dict:
    """Parse one certification text line: 'Name | Organization | https://link'.

    Organization and link are optional; a plain line without '|' is treated
    as a name-only certification (legacy format).
    """
    parts = [p.strip() for p in (line or "").split("|")]
    name = parts[0].strip().lstrip("-•* ").strip() if parts else ""
    org = parts[1] if len(parts) > 1 else ""
    link = parts[2] if len(parts) > 2 else ""
    return {"name": name, "organization": org, "link": link}


def certifications_text_to_html(text: str) -> str:
    """Convert certification text lines into CERTIFICATIONS region HTML.

    Each line: 'Name | Organization | https://link' (org/link optional).
    """
    lines = [ln.strip() for ln in (text or "").splitlines()]
    certs = [parse_certification_line(ln) for ln in lines]
    certs = [c for c in certs if c["name"]]
    if not certs:
        return ""
    items = "\n".join(render_certification_li(c) for c in certs)
    return f"<h2>Certifications</h2>\n<ul>\n{items}\n</ul>"


def certification_li_to_text(li_html: str) -> str:
    """Convert one rendered certification <li> back to 'Name | Organization | link'.

    Round-trips render_certification_li losslessly (link URL included).
    """
    m_link = re.search(r'<a\s+href="([^"]+)"', li_html or "")
    link = m_link.group(1).strip() if m_link else ""
    m_org = re.search(r"<strong>(.*?)</strong>", li_html or "", re.S)
    org = _TAG_RE.sub("", m_org.group(1)).strip() if m_org else ""
    plain = _TAG_RE.sub(" ", li_html or "").strip()
    # plain looks like "Name — Org link" / "Name — Org" / "Name link" / "Name"
    if link and plain.endswith("link"):
        plain = plain[: -len("link")].strip()
    name = plain
    if org and " — " in plain:
        name = plain.rsplit(" — ", 1)[0].strip()
    elif not org and " — " in plain:
        # No org: the em dash came from the name itself; keep whole plain text
        name = plain
    parts = [name]
    if org or link:
        parts.append(org)
    if link:
        parts.append(link)
    return " | ".join(parts).strip(" |")


def _trim_projects_html(projects_html: str, max_projects: int) -> str:
    """Trim a PROJECTS HTML block to at most max_projects entries.

    The Section Customizer lock is inviolable for content, but the per-recreate
    "Number of Projects" dropdown is an explicit user choice for this run, so
    it wins on count. Keeps the first max_projects div.proj entries.
    """
    if not projects_html or max_projects < 1:
        return projects_html
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(projects_html, "html.parser")
    projs = soup.find_all("div", class_="proj")
    if len(projs) <= max_projects:
        return projects_html
    for extra in projs[max_projects:]:
        extra.decompose()
    return str(soup)


def build_resume_sections(
    candidate: Candidate,
    jd_text: str,
    job_title: str = "",
    template_id: str | None = None,
    summary_override: str | None = None,
    skills_override_html: str | None = None,
    projects_override_html: str | None = None,
    experience_override_html: str | None = None,
    education_override_html: str | None = None,
    certifications_override_html: str | None = None,
    links_override_html: str | None = None,
    max_projects: int = 5,
    recommended_project_ids: list[str] | None = None,
    section_order: list[str] | str | None = None,
) -> ResumeContent:
    """Content half: tailor every resume section. No template involved.

    All truthfulness guardrails live here (section builders); rendering cannot
    invent facts because it only places these strings into regions.
    """
    tid, template_info = resolve_template(template_id or select_variant(job_title, jd_text))

    # 1. SUMMARY (Strictly Computer Science / AI / Software, 2-part hybrid blend from KB intro + JD alignment)
    summary = build_safe_cs_summary(summary_override, job_title=job_title, variant=tid, jd_text=jd_text, candidate=candidate)

    # 2. SKILLS (ATS-optimized dynamic category prioritization and keyword elevation)
    if skills_override_html:
        skills_html = skills_override_html
    else:
        skills_html = generate_tailored_skills_html(
            candidate, jd_text, job_title=job_title, variant=tid
        )

    # 3. PROJECTS (LLM reasoning & ATS score ranking, up to max_projects)
    selected_p = select_projects(
        candidate,
        template_info.get("domain", "all"),
        jd_text,
        job_title=job_title,
        max_projects=max_projects,
        recommended_project_ids=recommended_project_ids,
    )
    if projects_override_html:
        # Lock wins on content if it has the exact requested count;
        # if project count changed, use selected_p to satisfy the user's project_count selection.
        trimmed = _trim_projects_html(projects_override_html, max_projects)
        from bs4 import BeautifulSoup as _BS
        _p_soup = _BS(trimmed, "html.parser")
        _p_count = len(_p_soup.find_all("div", class_="proj"))
        if _p_count == max_projects:
            projects_html = trimmed
        else:
            projects_html = render_projects_html(selected_p)
    else:
        projects_html = render_projects_html(selected_p)

    return ResumeContent(
        template_id=tid,
        summary_html=summary,
        skills_html=skills_html,
        projects_html=projects_html,
        education_html=education_override_html if education_override_html is not None else render_education_html(candidate),
        experience_html=experience_override_html if experience_override_html is not None else render_experience_html(candidate.experience or []),
        header_title=clean_header_title(job_title, variant=tid),
        certifications_html=certifications_override_html if certifications_override_html is not None else render_certifications_html(candidate),
        links_html=links_override_html,
        selected_projects=[p.get("short_name") or p.get("name") or "" for p in selected_p],
        section_order=section_order,
    )


def render_resume(
    content: ResumeContent,
    candidate: Candidate | None = None,
    section_order: list[str] | str | None = None,
) -> str:
    """Render half: place built sections into the template's regions.

    Pure placement + header/contact sync; no content decisions happen here.
    """
    _, template_info = resolve_template(content.template_id)
    template_html = (TEMPLATES_DIR / template_info["template"]).read_text(encoding="utf-8")

    replacements = {
        "SUMMARY": content.summary_html,
        "SKILLS": content.skills_html,
        "PROJECTS": content.projects_html,
        "EDUCATION": content.education_html,
        "CERTIFICATIONS": content.certifications_html,
    }
    if content.experience_html is not None:
        replacements["EXPERIENCE"] = content.experience_html
    if content.links_html is not None:
        replacements["LINKS"] = content.links_html

    final_html = replace_regions(template_html, replacements)

    # Update header title to reflect role and sync candidate contact info
    final_html = re.sub(
        r'(<span class="title">)[^<]*(</span>)',
        rf'\g<1>{content.header_title}\g<2>',
        final_html,
    )
    final_html = sync_candidate_contact_info(final_html, candidate)

    eff_order = section_order or content.section_order
    if eff_order:
        final_html = reorder_html_sections(final_html, eff_order)

    return final_html


def build_resume_content(
    candidate: Candidate,
    jd_text: str,
    job_title: str = "",
    variant: str | None = None,
    summary_override: str | None = None,
    skills_override_html: str | None = None,
    projects_override_html: str | None = None,
    experience_override_html: str | None = None,
    education_override_html: str | None = None,
    certifications_override_html: str | None = None,
    links_override_html: str | None = None,
    max_projects: int = 5,
    recommended_project_ids: list[str] | None = None,
    section_order: list[str] | str | None = None,
) -> BuiltResume:
    """Build tailored HTML resume and plain-text representation (default: 5 projects).

    Thin orchestrator over the two seams: build_resume_sections() (content)
    + render_resume() (template). Signature and behavior unchanged.
    """
    content = build_resume_sections(
        candidate,
        jd_text,
        job_title=job_title,
        template_id=variant,
        summary_override=summary_override,
        skills_override_html=skills_override_html,
        projects_override_html=projects_override_html,
        experience_override_html=experience_override_html,
        education_override_html=education_override_html,
        certifications_override_html=certifications_override_html,
        links_override_html=links_override_html,
        max_projects=max_projects,
        recommended_project_ids=recommended_project_ids,
        section_order=section_order,
    )
    final_html = render_resume(content, candidate, section_order=section_order)
    plain_text = html_to_plain_text(final_html)

    return BuiltResume(
        variant=content.template_id,
        html_content=final_html,
        resume_text=plain_text,
        selected_projects=content.selected_projects,
        section_order=detect_section_order(final_html),
    )



def rebuild_resume_from_custom_edits(
    base_html: str,
    summary: str = "",
    skills_input: str = "",
    experience_html: str = "",
    projects_html: str = "",
    selected_project_ids: list[str] | None = None,
    candidate: Candidate | None = None,
    selected_categories: list[str] | None = None,
    selected_experiences: list[str] | None = None,
    skills_by_category: dict[str, list[str]] | None = None,
    job_title: str = "",
    jd_text: str = "",
    # Phase 10 (§U.13): full section coverage.
    education_html: str = "",
    certifications_html: str = "",
    links_html: str = "",
    project_bullets: dict[str, str] | None = None,  # {project id/name -> edited bullet text}
    experience_bullets: dict[str, list[str]] | None = None,  # {employer -> edited bullet list}
    extra_regions: dict[str, str] | None = None,  # other sections: {REGION_NAME -> html}
    section_order: list[str] | str | None = None,
) -> BuiltResume:
    """Rebuild resume from user live edits, re-formatting skills and regions cleanly.

    Every free-typed HTML region is sanitized (stored-XSS defense) before it
    is stored: scripts, event handlers and unsafe URL schemes are stripped.
    """
    replacements = {}
    if summary and summary.strip():
        replacements["SUMMARY"] = sanitize_resume_html(summary.strip())

    def _apply_project_bullets(projs: list[dict]) -> list[dict]:
        if not project_bullets:
            return projs
        out = []
        for p in projs:
            p = dict(p)
            for key in (p.get("id"), p.get("name"), p.get("short_name")):
                kl = str(key or "").strip().lower()
                hit = next((v for k, v in project_bullets.items() if k and str(k).strip().lower() == kl), None)
                if hit is not None:
                    p["bullet"] = hit
                    break
            out.append(p)
        return out

    def _apply_experience_bullets(exps: list[dict]) -> list[dict]:
        if not experience_bullets:
            return exps
        out = []
        for e in exps:
            e = dict(e)
            emp = str(e.get("employer") or "").strip().lower()
            hit = next((v for k, v in experience_bullets.items() if k and str(k).strip().lower() == emp), None)
            if hit is not None:
                e["bullets"] = [b for b in hit if b and str(b).strip()]
            out.append(e)
        return out

    # 1. Structured candidate projects
    if selected_project_ids is not None and candidate:
        all_p = candidate.projects or []
        sel_lower = [str(x).strip().lower() for x in selected_project_ids if str(x).strip()]
        matched_projs = []
        for p in all_p:
            p_id = (p.get("id") or "").lower()
            p_name = (p.get("name") or "").lower()
            p_short = (p.get("short_name") or "").lower()
            if any(s == p_id or s == p_name or s == p_short or s in p_id or s in p_name for s in sel_lower):
                matched_projs.append(p)
        replacements["PROJECTS"] = render_projects_html(_apply_project_bullets(matched_projs))
    elif project_bullets and candidate:
        # Bullet-only edit: keep the currently rendered projects, swap bullet text.
        replacements["PROJECTS"] = render_projects_html(_apply_project_bullets(candidate.projects or []))
    elif projects_html.strip():
        replacements["PROJECTS"] = sanitize_resume_html(projects_html.strip())

    # 2. Structured candidate experiences
    if experience_html and experience_html.strip():
        replacements["EXPERIENCE"] = sanitize_resume_html(experience_html.strip())
    elif selected_experiences is not None and candidate:
        all_e = candidate.experience or []
        sel_lower = [str(x).strip().lower() for x in selected_experiences if str(x).strip()]
        matched_exps = []
        for e in all_e:
            emp = (e.get("employer") or "").lower()
            if any(s == emp or s in emp for s in sel_lower):
                matched_exps.append(e)
        replacements["EXPERIENCE"] = render_experience_html(_apply_experience_bullets(matched_exps))
    elif experience_bullets and candidate:
        replacements["EXPERIENCE"] = render_experience_html(_apply_experience_bullets(candidate.experience or []))

    # 3. Skills formatting: custom text takes priority if explicitly typed, then sub-skills dictionary, then categories
    if skills_input and skills_input.strip():
        if "<li>" not in skills_input:
            skills_html = skills_csv_to_html(skills_input)
        else:
            # Raw <li> HTML from the editor: sanitize before storing.
            skills_html = sanitize_resume_html(skills_input.strip())
        replacements["SKILLS"] = skills_html
    elif skills_by_category is not None:
        lines = []
        for cat_name, s_list in skills_by_category.items():
            cleaned_skills = [canonical_display_name(s) for s in s_list if s and s.strip()]
            if cleaned_skills:
                lines.append(
                    f"<li><strong>{_html.escape(cat_name.strip())}</strong> — "
                    f"{', '.join(_html.escape(cs) for cs in cleaned_skills)}</li>"
                )
        replacements["SKILLS"] = "\n    ".join(lines)
    elif selected_categories is not None and candidate:
        replacements["SKILLS"] = generate_tailored_skills_html(
            candidate=candidate,
            jd_text=jd_text,
            job_title=job_title,
            allowed_categories=selected_categories,
        )

    # 4. Education / certifications / links (§U.13) — raw HTML from the editor.
    if education_html and education_html.strip():
        replacements["EDUCATION"] = sanitize_resume_html(education_html.strip())
    if certifications_html and certifications_html.strip():
        replacements["CERTIFICATIONS"] = sanitize_resume_html(certifications_html.strip())
    if links_html and links_html.strip():
        replacements["LINKS"] = sanitize_resume_html(links_html.strip())

    # 5. Other sections: generic region replacement for any extra editable region.
    for rname, rhtml in (extra_regions or {}).items():
        if rname and rhtml and rhtml.strip():
            replacements[str(rname).strip().upper()] = sanitize_resume_html(rhtml.strip())

    updated_html = replace_regions(base_html, replacements)
    if candidate:
        updated_html = sync_candidate_contact_info(updated_html, candidate)

    if section_order:
        updated_html = reorder_html_sections(updated_html, section_order)

    plain_text = html_to_plain_text(updated_html)

    return BuiltResume(
        variant="custom",
        html_content=updated_html,
        resume_text=plain_text,
        section_order=detect_section_order(updated_html),
    )


def switch_resume_template(
    current_html: str,
    new_template_id: str,
    candidate: Candidate | None = None,
    jd_text: str = "",
    job_title: str = "",
) -> str:
    """Switch the styling template of an existing resume HTML while strictly
    preserving all section contents, custom user edits, locked regions,
    contact details, and custom section ordering."""
    tid, ti = resolve_template(new_template_id)
    tmpl_path = TEMPLATES_DIR / ti["template"]
    if not tmpl_path.exists():
        tmpl_path = TEMPLATES_DIR / "apex_modern.html"
    base_template_html = tmpl_path.read_text(encoding="utf-8")

    current_regions = extract_regions(current_html) if current_html else {}
    current_order = detect_section_order(current_html) if current_html else list(DEFAULT_SECTION_ORDER)

    # Ensure fallback if current_regions lacks critical content
    if not current_regions.get("SUMMARY") or not current_regions.get("EXPERIENCE") or not current_regions.get("SKILLS"):
        base_cand = candidate or load_candidate()
        built = build_resume_content(base_cand, jd_text or "", job_title=job_title or "", variant=tid)
        return built.html_content

    new_html = replace_regions(base_template_html, current_regions)
    if candidate:
        new_html = sync_candidate_contact_info(new_html, candidate)
    if current_order:
        new_html = reorder_html_sections(new_html, current_order)
    return new_html



def parse_resume_regions_detail(
    html_content: str,
    candidate: Candidate,
    jd_text: str = "",
    job_title: str = "",
) -> dict:
    """Extract detailed active sections (summary, active categories, skills, and active project IDs)
    to populate the interactive GUI section editor.
    """
    regions = extract_regions(html_content) if html_content else {}
    summary = regions.get("SUMMARY", "")
    skills_html = regions.get("SKILLS", "")
    projects_html = regions.get("PROJECTS", "")
    experience_html = regions.get("EXPERIENCE", "")
    education_html = regions.get("EDUCATION", "")
    certifications_html = regions.get("CERTIFICATIONS", "")
    links_html = regions.get("LINKS", "")

    # Parse active categories and active skills from SKILLS HTML
    active_categories = []
    active_skills = []
    active_category_skills: dict[str, list[str]] = {}
    for line in skills_html.splitlines():
        line_s = line.strip()
        m = re.match(r"<li><strong>([^<]+)</strong>\s*—\s*(.*?)</li>", line_s)
        if m:
            cat = m.group(1).strip()
            active_categories.append(cat)
            skills = [s.strip() for s in m.group(2).split(",") if s.strip()]
            active_skills.extend(skills)
            active_category_skills[cat] = skills

    # Parse candidate categorized skills for rich fallback & category availability
    base_cand = load_candidate()
    cand_cats = get_categorized_candidate_skills(candidate if candidate else base_cand)
    for cat_name, cat_skills in cand_cats.items():
        if cat_name not in active_category_skills or not active_category_skills[cat_name]:
            active_category_skills[cat_name] = cat_skills[:5]

    if len(active_categories) <= 1:
        # Default all available candidate categories as active so the user has rich categories
        active_categories = [cat for cat in cand_cats.keys() if cand_cats[cat]]


    # Parse candidate projects (ranked by JD relevance when jd_text/job_title provided)
    base_cand = load_candidate()
    raw_projects = candidate.projects if candidate and candidate.projects else list(base_cand.projects)
    if (jd_text or job_title) and raw_projects:
        all_projects = select_projects(
            candidate if candidate else base_cand,
            select_variant(job_title, jd_text),
            jd_text,
            job_title=job_title,
            max_projects=len(raw_projects),
            use_llm=False,
        )
    else:
        all_projects = list(raw_projects)

    active_project_ids = []
    for p in all_projects:
        p_name = p.get("name") or ""
        p_short = p.get("short_name") or ""
        p_id = p.get("id") or ""
        if p_name and p_name in projects_html:
            active_project_ids.append(p_id or p_name)
        elif p_short and p_short in projects_html:
            active_project_ids.append(p_id or p_name)
        elif p_id and p_id in projects_html:
            active_project_ids.append(p_id or p_name)

    if not active_project_ids and all_projects:
        active_project_ids = [p.get("id") or p.get("name") for p in all_projects[:5]]

    # Parse active experiences from EXPERIENCE HTML
    all_experiences = candidate.experience if candidate and candidate.experience else list(base_cand.experience)
    active_experience_employers = []
    for exp in all_experiences:
        emp = exp.get("employer") or ""
        if emp and emp.lower() in experience_html.lower():
            active_experience_employers.append(emp)
    if not active_experience_employers and all_experiences:
        active_experience_employers = [exp.get("employer") for exp in all_experiences if exp.get("employer")]

    # Phase 10 (§U.13): pre-fill data for the full-coverage editor.
    def _plain(html_frag: str) -> str:
        return _TAG_RE.sub(" ", html_frag or "").strip()

    certifications_text = "\n".join(
        certification_li_to_text(li) for li in _LI_RE.findall(certifications_html) if certification_li_to_text(li)
    )
    link_urls: dict[str, str] = {}
    for m in re.finditer(r'<a\s+href="([^"]+)"[^>]*>\s*([^<]+?)\s*</a>', links_html or ""):
        label = m.group(2).strip().lower()
        if label and label not in link_urls:
            link_urls[label] = m.group(1).strip()
    project_bullets = {
        (p.get("id") or p.get("name") or ""): (p.get("bullet") or "")
        for p in all_projects if (p.get("id") or p.get("name"))
    }
    experience_bullets = {
        (exp.get("employer") or ""): "\n".join(exp.get("bullets") or [])
        for exp in all_experiences if exp.get("employer")
    }
    all_educations = [e for e in (candidate.education or []) if isinstance(e, dict)]

    return {
        "summary": summary,
        "skills_html": skills_html,
        "skills_csv": ", ".join(active_skills),
        "experience_html": experience_html,
        "projects_html": projects_html,
        "education_html": education_html,
        "education_text": _plain(education_html),
        "all_educations": all_educations,
        "certifications_html": certifications_html,
        "certifications_text": certifications_text,
        "links_html": links_html,
        "link_urls": link_urls,
        "project_bullets": project_bullets,
        "experience_bullets": experience_bullets,
        "active_categories": active_categories,
        "active_skills": active_skills,
        "active_category_skills": active_category_skills,
        "active_project_ids": active_project_ids,
        "all_categories": CANDIDATE_SKILL_CATEGORIES,
        "all_projects": all_projects,
        "all_experiences": all_experiences,
        "active_experience_employers": active_experience_employers,
    }


def _clean_text_for_pdf(text: str) -> str:
    """Sanitize text for clean rendering in PDF."""
    if not text:
        return ""
    replacements = {
        "\u00a0": " ",    # non-breaking space
        "\u200b": "",     # zero-width space
        "\u200e": "",     # LTR mark
        "\u200f": "",     # RTL mark
        "\t": " ",
    }
    for orig, rep in replacements.items():
        text = text.replace(orig, rep)
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)


def _ensure_static_nunito_fonts() -> tuple[Path, Path, Path]:
    """Ensure static Regular, Bold, and Italic Nunito TTF font files exist."""
    fonts_dir = Path(__file__).resolve().parent.parent / "assets" / "fonts"
    reg_font = fonts_dir / "Nunito-Regular.ttf"
    bold_font = fonts_dir / "Nunito-Bold.ttf"
    it_font = fonts_dir / "Nunito-Italic-Static.ttf"

    if not (reg_font.exists() and bold_font.exists() and it_font.exists()):
        try:
            from fontTools.ttLib import TTFont
            from fontTools.varLib.mutator import instantiateVariableFont
            var_ttf = fonts_dir / "Nunito.ttf"
            if var_ttf.exists():
                if not reg_font.exists():
                    instantiateVariableFont(TTFont(str(var_ttf)), {"wght": 400.0}).save(str(reg_font))
                if not bold_font.exists():
                    instantiateVariableFont(TTFont(str(var_ttf)), {"wght": 700.0}).save(str(bold_font))
            var_it = fonts_dir / "Nunito-Italic.ttf"
            if var_it.exists() and not it_font.exists():
                instantiateVariableFont(TTFont(str(var_it)), {"wght": 400.0}).save(str(it_font))
        except Exception:
            pass

    return reg_font, bold_font, it_font


def _hex_to_rgb(hex_str: str, default: tuple[int, int, int] = (31, 41, 55)) -> tuple[int, int, int]:
    h = (hex_str or "").lstrip("#")
    if len(h) == 6:
        try:
            return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
        except ValueError:
            pass
    return default


def _register_pdf_fonts(pdf: FPDF, font_family: str) -> str:
    """Register the appropriate font files with FPDF2 for the given family."""
    fonts_dir = FONTS_DIR
    f_fam = (font_family or "").strip()

    if f_fam == "Alegreya":
        reg = fonts_dir / "Alegreya-Regular.ttf"
        bold = fonts_dir / "Alegreya-Bold.ttf"
        it = fonts_dir / "Alegreya-Italic.ttf"
        bi = fonts_dir / "Alegreya-BoldItalic.ttf"
        if reg.exists() and bold.exists():
            pdf.add_font("Alegreya", "", str(reg))
            pdf.add_font("Alegreya", "B", str(bold))
            pdf.add_font("Alegreya", "I", str(it if it.exists() else reg))
            pdf.add_font("Alegreya", "BI", str(bi if bi.exists() else bold))
            return "Alegreya"

    elif f_fam in ("Source Sans 3", "SourceSans3"):
        reg = fonts_dir / "SourceSans3-Regular.ttf"
        bold = fonts_dir / "SourceSans3-Bold.ttf"
        it = fonts_dir / "SourceSans3-Italic.ttf"
        bi = fonts_dir / "SourceSans3-BoldItalic.ttf"
        if reg.exists() and bold.exists():
            pdf.add_font("Source Sans 3", "", str(reg))
            pdf.add_font("Source Sans 3", "B", str(bold))
            pdf.add_font("Source Sans 3", "I", str(it if it.exists() else reg))
            pdf.add_font("Source Sans 3", "BI", str(bi if bi.exists() else bold))
            return "Source Sans 3"

    elif f_fam == "Amiri":
        reg = fonts_dir / "Amiri-Regular.ttf"
        bold = fonts_dir / "Amiri-Bold.ttf"
        it = fonts_dir / "Amiri-Italic.ttf"
        bi = fonts_dir / "Amiri-BoldItalic.ttf"
        if reg.exists() and bold.exists():
            pdf.add_font("Amiri", "", str(reg))
            pdf.add_font("Amiri", "B", str(bold))
            pdf.add_font("Amiri", "I", str(it if it.exists() else reg))
            pdf.add_font("Amiri", "BI", str(bi if bi.exists() else bold))
            return "Amiri"

    # Default: Nunito
    reg_font, bold_font, it_font = _ensure_static_nunito_fonts()
    if reg_font.exists() and bold_font.exists():
        pdf.add_font("Nunito", "", str(reg_font))
        pdf.add_font("Nunito", "B", str(bold_font))
        if it_font.exists():
            pdf.add_font("Nunito", "I", str(it_font))
        else:
            pdf.add_font("Nunito", "I", str(reg_font))
        return "Nunito"

    return "helvetica"


def _build_pdf_page(soup: BeautifulSoup, scale_factor: float = 1.0, flow_pages: bool = False) -> FPDF:
    """Build an exact single-page or multi-page styled resume PDF with template-specific styling.

    flow_pages=True enables fpdf2 auto page-break so long resumes flow onto page 2 cleanly.
    """
    from fpdf import FPDF

    # Detect template_id from HTML body attribute or default
    body_tag = soup.find("body")
    template_attr = body_tag.get("data-template", "") if body_tag else ""
    tid, info = resolve_template(template_attr)

    pdf = FPDF(format="A4", unit="mm")
    if flow_pages:
        pdf.set_auto_page_break(auto=True, margin=12.0)
    else:
        pdf.set_auto_page_break(auto=False)

    font_family_pref = info.get("font_family", "Nunito")
    font_name = _register_pdf_fonts(pdf, font_family_pref)

    margin_x = 10.0
    margin_top = max(6.0, 7.5 * scale_factor)
    pdf.set_margins(margin_x, margin_top, margin_x)
    pdf.add_page()

    # Template-specific color palette
    c_primary = _hex_to_rgb(info.get("primary_color", "#137f96"), (19, 127, 150))
    c_secondary = _hex_to_rgb(info.get("secondary_color", "#2491a9"), (36, 145, 169))
    c_dark = _hex_to_rgb(info.get("dark_color", "#111827"), (17, 24, 39))
    c_body = _hex_to_rgb(info.get("body_color", "#1f2937"), (31, 41, 55))
    c_muted = _hex_to_rgb(info.get("muted_color", "#64748b"), (100, 116, 139))

    # Proportional typography sizes scaled
    fs_name = 22.5 * scale_factor
    fs_title = 12.5 * scale_factor
    fs_contact = 8.8 * scale_factor
    fs_h2 = 10.8 * scale_factor
    fs_body = 9.4 * scale_factor
    fs_bullet = 9.0 * scale_factor

    lh_body = 4.35 * scale_factor
    lh_bullet = 4.05 * scale_factor

    # 1. Header (Name & Subtitle)
    name_el = soup.find(class_="name")
    title_el = soup.find(class_="title")
    name_text = _clean_text_for_pdf(name_el.get_text(strip=True) if name_el else "Alex Morgan")
    title_text = _clean_text_for_pdf(title_el.get_text(strip=True) if title_el else "Software Engineer")

    contact_div = soup.find(class_="contact")
    contact_items: list[tuple[str, str]] = []
    if contact_div:
        # Search for individual items or plain text separated by symbols
        items_spans = contact_div.find_all(class_="item")
        if items_spans:
            for span in items_spans:
                a_tag = span.find("a")
                text = _clean_text_for_pdf(span.get_text(strip=True))
                href = a_tag["href"].strip() if a_tag and a_tag.get("href") else ""
                if not href and "@" in text:
                    href = f"mailto:{text}"
                elif not href and re.search(r"\+?\d[\d\s-]{7,}", text):
                    clean_num = re.sub(r"[^\d+]", "", text)
                    href = f"tel:{clean_num}"
                if text:
                    contact_items.append((text, href))
        else:
            raw_contact = _clean_text_for_pdf(contact_div.get_text(separator=" ", strip=True))
            for chunk in re.split(r"[•|;]+", raw_contact):
                chunk = chunk.strip()
                if chunk:
                    href = f"mailto:{chunk}" if "@" in chunk else ""
                    contact_items.append((chunk, href))

    # Render Header Layout according to template style
    if tid == "oxford_editorial":
        # Centered Classic Editorial Header
        pdf.set_font(font_name, "B", fs_name + 1.5)
        pdf.set_text_color(*c_dark)
        pdf.cell(0, 7.5 * scale_factor, name_text.upper(), align="C", new_x="LMARGIN", new_y="NEXT")
        if title_text:
            pdf.set_font(font_name, "I", fs_title)
            pdf.set_text_color(*c_secondary)
            pdf.cell(0, 5.0 * scale_factor, title_text, align="C", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(1.0 * scale_factor)

        # Centered contact row with bullets
        if contact_items:
            pdf.set_font(font_name, "", fs_contact)
            pdf.set_text_color(*c_muted)
            contact_str = "   •   ".join(t for t, _ in contact_items)
            pdf.cell(0, 4.5 * scale_factor, contact_str, align="C", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2.0 * scale_factor)

    elif tid == "monarch_executive":
        # Distinguished Side-by-Side Executive Header
        start_y = pdf.get_y()
        pdf.set_font(font_name, "B", fs_name + 2.0)
        pdf.set_text_color(*c_dark)
        pdf.write(7.0 * scale_factor, name_text)
        pdf.ln(7.0 * scale_factor)

        if title_text:
            pdf.set_font(font_name, "I", fs_title)
            pdf.set_text_color(*c_secondary)
            pdf.write(5.0 * scale_factor, title_text)
        
        # Right-aligned contact items
        if contact_items:
            pdf.set_font(font_name, "", fs_contact)
            pdf.set_text_color(*c_muted)
            c_y = start_y
            for t, href in contact_items:
                _tw = pdf.get_string_width(t)
                pdf.set_xy(pdf.w - pdf.r_margin - _tw, c_y)
                if href:
                    pdf.set_text_color(*c_secondary)
                    pdf.write(3.8 * scale_factor, t, link=href)
                else:
                    pdf.set_text_color(*c_muted)
                    pdf.write(3.8 * scale_factor, t)
                c_y += 3.8 * scale_factor
            pdf.set_y(max(pdf.get_y(), c_y) + 2.0 * scale_factor)
        else:
            pdf.ln(5.0 * scale_factor)

        # Executive divider line
        pdf.set_draw_color(*c_primary)
        pdf.set_line_width(0.4)
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
        pdf.ln(2.5 * scale_factor)

    elif tid == "silicon_compact":
        # Tech Header with Inline Right Title & Pipe Separators
        pdf.set_font(font_name, "B", fs_name)
        pdf.set_text_color(*c_primary)
        pdf.write(6.5 * scale_factor, name_text)
        
        if title_text:
            pdf.set_font(font_name, "B", fs_title - 1.0)
            pdf.set_text_color(*c_muted)
            _tw = pdf.get_string_width(title_text.upper())
            pdf.set_x(pdf.w - pdf.r_margin - _tw)
            pdf.cell(_tw, 6.5 * scale_factor, title_text.upper(), new_x="LMARGIN", new_y="NEXT")
        else:
            pdf.ln(6.5 * scale_factor)

        if contact_items:
            pdf.set_font(font_name, "", fs_contact)
            for i, (text, href) in enumerate(contact_items):
                if i > 0:
                    pdf.set_text_color(203, 213, 225)
                    pdf.write(4.2 * scale_factor, "  |  ")
                if href:
                    pdf.set_text_color(*c_primary)
                    pdf.write(4.2 * scale_factor, text, link=href)
                else:
                    pdf.set_text_color(*c_muted)
                    pdf.write(4.2 * scale_factor, text)
            pdf.ln(4.5 * scale_factor)

        # Emerald Divider
        pdf.set_draw_color(*c_primary)
        pdf.set_line_width(0.35)
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
        pdf.ln(2.0 * scale_factor)

    else:
        # Apex Modern & Nova Minimalist Standard Layout
        pdf.set_font(font_name, "B", fs_name)
        pdf.set_text_color(*c_primary)
        pdf.write(7.0 * scale_factor, name_text)

        pdf.set_font(font_name, "I", fs_title)
        pdf.set_text_color(*c_secondary)
        pdf.write(7.0 * scale_factor, f"   {title_text}")
        pdf.ln(7.5 * scale_factor)

        if contact_items:
            pdf.set_font(font_name, "", fs_contact)
            total_items_w = sum(pdf.get_string_width(t) for t, _ in contact_items)
            n_items = len(contact_items)
            avail_w = pdf.w - pdf.l_margin - pdf.r_margin
            gap = (avail_w - total_items_w) / (n_items - 1) if n_items > 1 else 0

            if gap >= 2.0:
                cur_x = pdf.l_margin
                pdf_y = pdf.get_y()
                for text, href in contact_items:
                    w = pdf.get_string_width(text)
                    pdf.set_xy(cur_x, pdf_y)
                    if href:
                        pdf.set_text_color(*c_primary)
                        pdf.write(4.5 * scale_factor, text, link=href)
                    else:
                        pdf.set_text_color(*c_muted)
                        pdf.write(4.5 * scale_factor, text)
                    cur_x += w + gap
                pdf.ln(5.0 * scale_factor)
            else:
                for i, (text, href) in enumerate(contact_items):
                    if i > 0:
                        pdf.set_text_color(148, 163, 184)
                        pdf.write(4.5 * scale_factor, "   •   ")
                    if href:
                        pdf.set_text_color(*c_primary)
                        pdf.write(4.5 * scale_factor, text, link=href)
                    else:
                        pdf.set_text_color(*c_muted)
                        pdf.write(4.5 * scale_factor, text)
                pdf.ln(5.0 * scale_factor)

    # 3. Resume Sections
    for h2 in soup.find_all("h2"):
        sec_title = _clean_text_for_pdf(h2.get_text(strip=True)).upper()
        pdf.ln(1.8 * scale_factor)
        pdf.set_font(font_name, "B", fs_h2)
        pdf.set_text_color(*c_primary)
        pdf.cell(0, 4.8 * scale_factor, sec_title, new_x="LMARGIN", new_y="NEXT")

        # Section divider line
        pdf.set_draw_color(*c_primary)
        pdf.set_line_width(0.32)
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
        pdf.ln(1.6 * scale_factor)

        curr = h2.next_sibling
        while curr and curr.name != "h2":
            if curr.name == "p":
                p_text = _clean_text_for_pdf(curr.get_text(strip=True))
                if p_text:
                    pdf.set_font(font_name, "", fs_body)
                    pdf.set_text_color(*c_body)
                    pdf.multi_cell(0, lh_body, p_text, new_x="LMARGIN", new_y="NEXT")
                    pdf.ln(0.8 * scale_factor)

            elif curr.name == "div" and ("entry" in curr.get("class", []) or "role" in curr.get("class", [])):
                left = curr.find(class_="left")
                right = curr.find(class_="right") or curr.find(class_="sub-right") or curr.find(class_="role-dates")

                left_title = ""
                left_subtitle = ""
                if left:
                    strong_el = left.find("strong")
                    em_el = left.find("em")
                    if strong_el and em_el:
                        left_title = _clean_text_for_pdf(strong_el.get_text(strip=True).rstrip(","))
                        left_subtitle = _clean_text_for_pdf(em_el.get_text(strip=True).lstrip(", "))
                    elif strong_el:
                        left_title = _clean_text_for_pdf(strong_el.get_text(strip=True))
                    elif em_el:
                        left_subtitle = _clean_text_for_pdf(em_el.get_text(strip=True))
                    else:
                        clean_left = re.sub(r"\s*,\s*", ", ", left.get_text(separator=" ", strip=True))
                        left_title = _clean_text_for_pdf(clean_left)
                else:
                    r_title = curr.find(class_="role-title")
                    r_comp = curr.find(class_="role-comp")
                    left_title = _clean_text_for_pdf(r_title.get_text(strip=True) if r_title else "")
                    left_subtitle = _clean_text_for_pdf(r_comp.get_text(strip=True) if r_comp else "")

                right_s = _clean_text_for_pdf(right.get_text(strip=True) if right else "")

                if left_title:
                    pdf.set_font(font_name, "B", fs_body + 0.2)
                    pdf.set_text_color(*c_dark)
                    pdf.write(4.3 * scale_factor, left_title)
                if left_subtitle:
                    pdf.set_font(font_name, "I", fs_body - 0.2)
                    pdf.set_text_color(*c_muted)
                    prefix = ", " if left_title else ""
                    pdf.write(4.3 * scale_factor, f"{prefix}{left_subtitle}")

                if right_s:
                    pdf.set_font(font_name, "I", fs_body - 0.3)
                    pdf.set_text_color(*c_primary if any(yr in right_s for yr in ["2023", "2024", "2025", "2026", "present"]) else c_muted)
                    _rw = pdf.get_string_width(right_s)
                    _avail = pdf.w - pdf.r_margin - pdf.get_x()
                    if _rw + 2.0 <= _avail:
                        pdf.set_x(pdf.w - pdf.r_margin - _rw)
                        pdf.cell(_rw, 4.3 * scale_factor, right_s, new_x="LMARGIN", new_y="NEXT")
                    else:
                        pdf.ln(4.3 * scale_factor)
                        pdf.cell(0, 4.3 * scale_factor, right_s, align="R", new_x="LMARGIN", new_y="NEXT")
                else:
                    pdf.ln(4.3 * scale_factor)

                for li in curr.find_all("li"):
                    li_text = _clean_text_for_pdf(li.get_text(strip=True))
                    if li_text:
                        pdf.set_font(font_name, "", fs_bullet)
                        pdf.set_text_color(*c_body)
                        pdf.set_x(margin_x + 1)
                        pdf.write(lh_bullet, "•  ")
                        cur_x = pdf.get_x()
                        pdf.multi_cell(pdf.w - margin_x - cur_x, lh_bullet, li_text, new_x="LMARGIN", new_y="NEXT")
                pdf.ln(0.6 * scale_factor)

            elif curr.name == "div" and "proj" in curr.get("class", []):
                head_div = curr.find(class_="head")
                strong_el = head_div.find("strong") if head_div else None
                a_el = head_div.find("a", href=True) if head_div else None
                em_el = head_div.find("em") if head_div else None

                p_name = _clean_text_for_pdf(strong_el.get_text(strip=True) if strong_el else "")
                link_url = a_el["href"].strip() if a_el and a_el.get("href") else ""
                sub_text = _clean_text_for_pdf(em_el.get_text(strip=True) if em_el else "")
                clean_sub = sub_text.lstrip(", -—").strip()

                pdf.set_font(font_name, "B", fs_body + 0.3)
                if link_url:
                    pdf.set_text_color(*c_primary)
                    pdf.write(4.3 * scale_factor, p_name, link=link_url)
                else:
                    pdf.set_text_color(*c_dark)
                    pdf.write(4.3 * scale_factor, p_name)

                if clean_sub:
                    pdf.set_font(font_name, "I", fs_body - 0.4)
                    pdf.set_text_color(*c_muted)
                    pdf.write(4.3 * scale_factor, f", {clean_sub}")
                pdf.ln(4.3 * scale_factor)

                for li in curr.find_all("li"):
                    li_text = _clean_text_for_pdf(li.get_text(strip=True))
                    if li_text:
                        pdf.set_font(font_name, "", fs_bullet)
                        pdf.set_text_color(*c_body)
                        pdf.set_x(margin_x + 1)
                        pdf.write(lh_bullet, "•  ")
                        cur_x = pdf.get_x()
                        pdf.multi_cell(pdf.w - margin_x - cur_x, lh_bullet, li_text, new_x="LMARGIN", new_y="NEXT")
                pdf.ln(0.6 * scale_factor)

            elif curr.name == "ul":
                is_skills = "skills-list" in curr.get("class", []) or "skill" in sec_title.lower()
                for li in curr.find_all("li"):
                    if is_skills:
                        strong_tag = li.find("strong")
                        if strong_tag:
                            cat_text = _clean_text_for_pdf(strong_tag.get_text(strip=True))
                            full_li = _clean_text_for_pdf(li.get_text(strip=True))
                            rest = full_li[len(cat_text):].lstrip(" :—–-")

                            pdf.set_x(margin_x + 1)
                            pdf.set_font(font_name, "", fs_bullet)
                            pdf.set_text_color(*c_primary)
                            pdf.write(lh_bullet, "•  ")

                            pdf.set_font(font_name, "B", fs_bullet + 0.1)
                            pdf.set_text_color(*c_dark)
                            pdf.write(lh_bullet, cat_text)

                            pdf.set_font(font_name, "", fs_bullet)
                            pdf.set_text_color(*c_body)
                            pdf.write(lh_bullet, f" — {rest}\n")
                        else:
                            li_text = _clean_text_for_pdf(li.get_text(strip=True))
                            pdf.set_x(margin_x + 1)
                            pdf.set_font(font_name, "", fs_bullet)
                            pdf.set_text_color(*c_body)
                            pdf.write(lh_bullet, f"•  {li_text}\n")
                    else:
                        li_text = _clean_text_for_pdf(li.get_text(strip=True))
                        if li_text:
                            pdf.set_font(font_name, "", fs_bullet)
                            pdf.set_text_color(*c_body)
                            pdf.set_x(margin_x + 1)
                            pdf.write(lh_bullet, "•  ")
                            cur_x = pdf.get_x()
                            pdf.multi_cell(pdf.w - margin_x - cur_x, lh_bullet, li_text, new_x="LMARGIN", new_y="NEXT")
                pdf.ln(0.6 * scale_factor)

            curr = curr.next_sibling
        pdf.ln(0.8 * scale_factor)

    return pdf


class PageOverflowError(ValueError):
    """Raised when resume content cannot fit the requested PDF page count.

    Page-count enforcement is real — the renderer measures and condenses
    (font size/spacing via scale_factor, never content deletion). If content
    still overflows at the readability floor, we fail loudly instead of
    silently clipping content off the page. The caller should ask the user
    to trim lower-priority content.
    """


# Readability floor: body font never goes below 9.0pt (scale 1.0; 0.98 for tight fit).
_PDF_MIN_SCALE = 0.98
# Bottom of the usable area on A4 (297mm tall); content past this would clip.
_PDF_MAX_Y_MM = 278.0


def count_pdf_pages(pdf_path: Path | str) -> int:
    """Count actual pages in a generated PDF (for honesty checks).

    Parses the PDF binary for /Type /Page markers (not /Pages). Works for
    fpdf2-generated files. Returns 0 if unreadable.
    """
    try:
        data = Path(pdf_path).read_bytes()
        return len(re.findall(rb"/Type\s*/Page[^s]", data))
    except Exception:
        return 0


def _render_pdf_fallback(html_p: Path, pdf_p: Path, page_target: int | None = None) -> Path:
    """Pure-Python PDF generator with auto-fitted / target page handling.

    Tries to fit on 1 page first by testing scale factors from spacious (1.20)
    down to the 9pt floor (0.98/1.0). If content is short, it uses a larger font/spacing
    to fill the page. If content is long, it scales down to 9pt to fit on 1 page.
    If the content cannot fit on 1 page even at 9pt, it flows onto 2 pages.
    Raises PageOverflowError when the content cannot fit the requested/maximum pages.
    """
    if page_target not in (None, 1, 2):
        raise ValueError(f"Invalid page_target: {page_target}. Must be 1, 2, or None.")

    from bs4 import BeautifulSoup

    html_text = html_p.read_text(encoding="utf-8")
    soup = BeautifulSoup(html_text, "html.parser")

    if page_target != 2:
        # 1-page attempt: dynamically scale from 1.28 down to the 9pt minimum floor (0.98).
        # We test largest to smallest so the first one that fits gives the best page-filling size.
        for sf in (1.28, 1.22, 1.16, 1.10, 1.05, 1.00, _PDF_MIN_SCALE):
            pdf_attempt = _build_pdf_page(soup, scale_factor=sf)
            if pdf_attempt.page_no() == 1 and pdf_attempt.get_y() <= _PDF_MAX_Y_MM:
                pdf_attempt.output(str(pdf_p))
                return pdf_p
        if page_target == 1:
            raise PageOverflowError(
                "Resume content exceeds 1 page at the 9pt minimum font size. "
                "Switch to 2-page format or trim lower-priority content."
            )
    # 2 pages attempt: flow naturally onto 2 pages at comfortable font size
    for sf in (1.10, 1.05, 1.00, _PDF_MIN_SCALE):
        pdf_attempt = _build_pdf_page(soup, scale_factor=sf, flow_pages=True)
        if pdf_attempt.page_no() <= 2:
            pdf_attempt.output(str(pdf_p))
            return pdf_p
    raise PageOverflowError(
        "Resume content exceeds 2 pages even at the 9pt minimum font size. "
        "Trim lower-priority content before exporting."
    )


def render_pdf_from_html(html_path: Path | str, output_pdf_path: Path | str, page_target: int | None = None) -> Path:
    """Render HTML file to A4 PDF using our pure-Python auto-fitted engine.

    Fully automatic page handling: fits on 1 page when possible, flows to
    2 pages when the content needs it. Raises PageOverflowError instead of
    silently clipping. Ensures beautiful typography, clickable links, and
    no empty space.
    """
    html_p = Path(html_path).resolve()
    pdf_p = Path(output_pdf_path).resolve()
    try:
        pdf_p.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    try:
        _render_pdf_fallback(html_p, pdf_p, page_target=page_target)
        try:
            from app.supabase_storage import upload_resume_to_supabase
            upload_resume_to_supabase(pdf_p)
        except Exception:
            pass
        return pdf_p
    except (PageOverflowError, ValueError):
        # Real page-count enforcement and parameter validation — propagate unwrapped
        raise
    except Exception as exc:
        logger.warning(f"Python PDF generator failed ({exc}), trying Chrome CLI...")
        chrome_bin = shutil.which("google-chrome") or shutil.which("chromium-browser") or shutil.which("chromium")
        if chrome_bin:
            cmd = [
                chrome_bin,
                "--headless=new",
                "--disable-gpu",
                "--no-sandbox",
                "--no-pdf-header-footer",
                f"--print-to-pdf={pdf_p}",
                html_p.as_uri(),
            ]
            try:
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                if res.returncode == 0 and pdf_p.exists():
                    return pdf_p
            except Exception:
                pass
        raise RuntimeError(f"PDF rendering failed: {exc}")
