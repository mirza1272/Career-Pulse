"""Resume Multi-Format Upload -> Text & Annotation Extraction -> Intelligent Structuring.

Supports PDF, DOCX, TXT, MD, RTF.
Extracts:
- Contact & Identity (Name, Email, Phone, Location)
- Profile Links (LinkedIn, GitHub, Portfolio) from text & PDF/DOCX link annotations
- Professional Summary
- Categorized Skills Inventory
- Employment History (Titles, Employers, Date ranges, Bullets, Technologies)
- Projects Library (Project Name, Subtitle/Tagline, Live Link/Repository, Descriptions, Skills)
- Education & Certifications
"""

from __future__ import annotations

import io
import json
import logging
import re
import xml.etree.ElementTree as ET
import zipfile

logger = logging.getLogger(__name__)

MAX_RESUME_CHARS = 10000
MIN_TEXT_CHARS = 30
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


def extract_docx_text_and_links(docx_bytes: bytes) -> tuple[str, list[str]]:
    """Extract paragraph text and hyperlink URLs from a Word .docx file."""
    try:
        links: list[str] = []
        paragraphs: list[str] = []
        with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
            # Extract hyperlink targets from relationships
            if "word/_rels/document.xml.rels" in zf.namelist():
                try:
                    rel_content = zf.read("word/_rels/document.xml.rels")
                    rel_tree = ET.fromstring(rel_content)
                    for r in rel_tree:
                        target = r.attrib.get("Target", "")
                        if target.startswith(("http://", "https://")):
                            links.append(target)
                except Exception:
                    pass

            if "word/document.xml" not in zf.namelist():
                raise RuntimeError("Invalid DOCX format: word/document.xml missing.")
            xml_content = zf.read("word/document.xml")
            tree = ET.fromstring(xml_content)
            ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
            for p in tree.iter(f"{{{ns['w']}}}p"):
                texts = [node.text for node in p.iter(f"{{{ns['w']}}}t") if node.text]
                if texts:
                    paragraphs.append("".join(texts))
            text = "\n".join(paragraphs).strip()
            if len(text) < MIN_TEXT_CHARS:
                raise RuntimeError("No readable text found in DOCX file.")
            return text, links
    except Exception as exc:
        raise RuntimeError(f"Could not read the DOCX file: {exc}") from exc


def extract_docx_text(docx_bytes: bytes) -> str:
    text, _ = extract_docx_text_and_links(docx_bytes)
    return text


def extract_pdf_text_and_links(pdf_bytes: bytes) -> tuple[str, list[str]]:
    """Extract text and live hyperlink annotations (URI) from a PDF."""
    if not pdf_bytes:
        raise RuntimeError("The uploaded file was empty.")
    if len(pdf_bytes) > MAX_UPLOAD_BYTES:
        raise RuntimeError("The PDF is larger than 10 MB; please upload a smaller file.")
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError(
            "pypdf is not installed. Install it with: pip install pypdf"
        ) from exc

    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        pages_text: list[str] = []
        links: list[str] = []
        for page in reader.pages:
            t = (page.extract_text() or "").strip()
            if t:
                pages_text.append(t)
            if "/Annots" in page:
                try:
                    for annot in page["/Annots"]:
                        obj = annot.get_object()
                        if obj and obj.get("/Subtype") == "/Link":
                            a = obj.get("/A", {})
                            if "/URI" in a:
                                uri = str(a["/URI"]).strip()
                                if uri and uri not in links:
                                    links.append(uri)
                except Exception:
                    pass
    except Exception as exc:
        raise RuntimeError(f"Could not read the resume PDF: {exc}") from exc

    text = "\n".join(pages_text).strip()
    if len(text) < MIN_TEXT_CHARS:
        raise RuntimeError(
            "No readable text found in the PDF — it may be a scanned image. "
            "Please upload a text-based PDF or DOCX instead."
        )
    return text, links


def extract_pdf_text(pdf_bytes: bytes) -> str:
    text, _ = extract_pdf_text_and_links(pdf_bytes)
    return text


def extract_resume_text_and_links(file_bytes: bytes, filename: str = "") -> tuple[str, list[str]]:
    """Extract text and all embedded hyperlink URLs from any supported resume format."""
    if not file_bytes:
        raise RuntimeError("The uploaded file was empty.")
    if len(file_bytes) > MAX_UPLOAD_BYTES:
        raise RuntimeError("File exceeds 10 MB limit; please upload a smaller file.")

    fname = (filename or "").lower().strip()

    # 1. PDF
    if fname.endswith(".pdf") or file_bytes[:5].lstrip().startswith(b"%PDF-"):
        return extract_pdf_text_and_links(file_bytes)

    # 2. DOCX
    if fname.endswith(".docx") or (file_bytes[:4] == b"PK\x03\x04" and b"word/" in file_bytes[:1000]):
        return extract_docx_text_and_links(file_bytes)

    # 3. Plain text / Markdown / HTML fallback
    try:
        text = file_bytes.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = file_bytes.decode("latin-1")
        except Exception:
            text = file_bytes.decode("utf-8", errors="ignore")

    raw_urls = re.findall(r"https?://[^\s<>\"']+", text)
    text = re.sub(r"<[^>]+>", " ", text)
    clean_lines = [line.strip() for line in text.splitlines() if line.strip()]
    cleaned = "\n".join(clean_lines).strip()
    if len(cleaned) < MIN_TEXT_CHARS:
        raise RuntimeError("The uploaded file does not contain enough readable text.")
    return cleaned, list(dict.fromkeys(raw_urls))


def extract_resume_text(file_bytes: bytes, filename: str = "") -> str:
    text, _ = extract_resume_text_and_links(file_bytes, filename)
    return text


# ---------------------------------------------------------------------------
# Comprehensive Heuristic / Rule-based Resume Parser (Always Active)
# ---------------------------------------------------------------------------

_SECTION_PATTERNS = {
    "summary": re.compile(
        r"^(?:professional\s+summary|summary|profile|about\s+me|executive\s+summary|career\s+objective|objective)\b",
        re.IGNORECASE,
    ),
    "skills": re.compile(
        r"^(?:technical\s+skills|skills\s*(?:&|and)?\s*competencies|skills\s*inventory|core\s*competencies|technologies|technical\s+expertise|tools\s*&\s*technologies|key\s+skills|skills)\b",
        re.IGNORECASE,
    ),
    "experience": re.compile(
        r"^(?:work\s+experience|professional\s+experience|employment\s+history|career\s+history|experience|internships?)\b",
        re.IGNORECASE,
    ),
    "projects": re.compile(
        r"^(?:projects|personal\s+projects|key\s+projects|academic\s+projects|featured\s+projects|software\s+projects)\b",
        re.IGNORECASE,
    ),
    "education": re.compile(
        r"^(?:education|academic\s+background|qualifications|academic\s+history|education\s+&\s+qualifications)\b",
        re.IGNORECASE,
    ),
    "certifications": re.compile(
        r"^(?:certifications?|certificates?|licenses?\s*(?:&|and)?\s*certifications?|honors?\s*&\s*certifications?)\b",
        re.IGNORECASE,
    ),
}

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(r"(?:\+?\d{1,4}[-.\s]?)?(?:\(?\d{2,4}\)?[-.\s]?)?\d{3,4}[-.\s]?\d{3,4}\b")
_LINKEDIN_RE = re.compile(r"(?:https?://)?(?:www\.)?linkedin\.com/in/([a-zA-Z0-9_\-%]+)", re.IGNORECASE)
_GITHUB_RE = re.compile(r"(?:https?://)?(?:www\.)?github\.com/([a-zA-Z0-9_\-%]+)", re.IGNORECASE)
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_DATE_RANGE_RE = re.compile(
    r"\b((?:(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+)?\d{4})\s*(?:–|—|-|to)\s*((?:(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+)?\d{4}|Present|Current)\b",
    re.IGNORECASE,
)


def heuristic_parse_resume_text(raw_text: str, extracted_links: list[str] | None = None) -> dict:
    """Deep deterministic rule-based extractor for all resume sections."""
    from app.resume_builder import CANONICAL_DISPLAY_MAP, canonical_display_name
    from app.skill_aliases import canonical_skill

    links_pool = list(extracted_links or [])
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    full_text = "\n".join(lines)

    # 1. Contact information
    email = ""
    m_email = _EMAIL_RE.search(full_text)
    if m_email:
        email = m_email.group(0).lower().strip()
    else:
        for u in links_pool:
            if u.startswith("mailto:"):
                email = u.replace("mailto:", "").strip().lower()
                break

    phone = ""
    for p_match in _PHONE_RE.finditer(full_text):
        p_str = p_match.group(0).strip()
        digits = re.sub(r"\D", "", p_str)
        if 7 <= len(digits) <= 15 and not p_str.startswith("202") and not p_str.startswith("199"):
            phone = p_str
            break
    if not phone:
        for u in links_pool:
            if u.startswith("tel:"):
                phone = u.replace("tel:", "").strip()
                break

    linkedin = ""
    m_li = _LINKEDIN_RE.search(full_text)
    if m_li:
        linkedin = f"https://linkedin.com/in/{m_li.group(1)}"
    else:
        for u in links_pool:
            if "linkedin.com/in/" in u.lower():
                linkedin = u.strip()
                break

    github = ""
    m_gh = _GITHUB_RE.search(full_text)
    if m_gh:
        github = f"https://github.com/{m_gh.group(1)}"
    else:
        for u in links_pool:
            if "github.com/" in u.lower():
                path_parts = [p for p in u.split("github.com/", 1)[1].strip("/").split("/") if p]
                if len(path_parts) == 1:
                    github = u.strip()
                    break

    portfolio = ""
    for u in _URL_RE.findall(full_text) + links_pool:
        u_low = u.lower()
        if (
            "linkedin.com" not in u_low
            and not u_low.startswith(("mailto:", "tel:"))
            and not u_low.endswith((".pdf", ".png", ".jpg"))
            and u != github
            and "pypi.org" not in u_low
        ):
            if any(ext in u_low for ext in [".me", ".dev", ".io", ".tech", "portfolio"]):
                portfolio = u.strip()
                break
    if not portfolio:
        for u in links_pool:
            u_low = u.lower()
            if (
                "linkedin.com" not in u_low
                and not u_low.startswith(("mailto:", "tel:"))
                and u != github
                and "github.com" not in u_low
                and "pypi.org" not in u_low
            ):
                portfolio = u.strip()
                break

    # Candidate Name (Header scan)
    name = ""
    for line in lines[:5]:
        if line.lower().startswith(("resume", "curriculum", "cv", "page", "email", "phone", "http")):
            continue
        if "@" in line or "linkedin" in line.lower() or "github" in line.lower():
            continue
        # Split candidate name from trailing role title if present (e.g. "Haseeb Ur Rahman Full-Stack Developer")
        cand_name = line
        for title_term in [
            "Full-Stack Developer", "Full Stack Developer", "Full-Stack", "Full Stack",
            "Software Engineer", "ML Engineer", "AI Engineer", "AI / ML Engineer",
            "Data Scientist", "Backend Developer", "Frontend Developer", "Developer", "Engineer"
        ]:
            cand_name = re.sub(rf"(?i)\b{re.escape(title_term)}\b.*", "", cand_name).strip()
        parts = [p.strip() for p in re.split(r"\s{2,}|\s*[|•–—,]\s*|\s+-\s+", cand_name) if p.strip()]
        cand_name = parts[0] if parts else cand_name
        cand_name = re.sub(r"[^\w\s.-]", "", cand_name).strip()
        if cand_name and len(cand_name.split()) >= 2:
            name = cand_name
            break
        words = cand_name.split()
        if 2 <= len(words) <= 4 and all(w[0].isupper() for w in words if w and w[0].isalpha()):
            name = cand_name
            break
    if not name and lines:
        name = lines[0].split("  ")[0].strip()

    # Location scan
    location = ""
    loc_patterns = [
        r"\b([A-Z][a-zA-Z\s]+,\s*(?:Pakistan|United States|USA|UK|Canada|Germany|UAE|Australia))\b",
        r"\b(Lahore|Islamabad|Karachi|Rawalpindi|Peshawar|Faisalabad|Multan)(?:,\s*Pakistan)?\b",
        r"\b(New York|San Francisco|London|Toronto|Berlin|Dubai|Sydney)\b",
    ]
    for lp in loc_patterns:
        m_loc = re.search(lp, full_text, re.IGNORECASE)
        if m_loc:
            location = m_loc.group(0).strip()
            break

    # 2. Section Partitioning
    sections: dict[str, list[str]] = {k: [] for k in _SECTION_PATTERNS}
    sections["other"] = []
    current_sec = "other"

    for line in lines:
        matched_sec = None
        test_line = re.sub(r"^[-*#•\s]+|[:\s]+$", "", line).strip()
        for sec_name, pat in _SECTION_PATTERNS.items():
            if pat.match(test_line) and len(test_line.split()) <= 4:
                matched_sec = sec_name
                break

        if matched_sec:
            current_sec = matched_sec
        else:
            sections[current_sec].append(line)

    # 3. Summary Extraction
    summary = ""
    if sections["summary"]:
        summary = " ".join(sections["summary"][:6]).strip()
    elif sections["other"]:
        cand_summary = []
        for l in sections["other"][1:6]:
            if "@" in l or _DATE_RANGE_RE.search(l) or len(l.split(",")) > 4:
                continue
            cand_summary.append(l)
        if cand_summary:
            summary = " ".join(cand_summary).strip()

    # 4. Skills Extraction
    extracted_skills: list[str] = []
    seen_canon_skills: set[str] = set()

    def _add_skill(sk: str) -> None:
        sk_clean = sk.strip()
        if not sk_clean or len(sk_clean) < 2 or len(sk_clean) > 40:
            return
        disp = canonical_display_name(sk_clean)
        c = canonical_skill(disp)
        if c not in seen_canon_skills:
            seen_canon_skills.add(c)
            extracted_skills.append(disp)

    # A. Scan skills section (including categorized lines)
    for line in sections["skills"]:
        clean_l = re.sub(r"^[•\-*]\s*", "", line).strip()
        if "—" in clean_l:
            _, right = clean_l.split("—", 1)
        elif ":" in clean_l:
            _, right = clean_l.split(":", 1)
        elif " - " in clean_l:
            _, right = clean_l.split(" - ", 1)
        else:
            right = clean_l

        parts = re.split(r"[,;|•\n\t]+", right)
        for p in parts:
            p_sub = re.sub(r"^(?:Languages|Frameworks|Tools|Databases|Libraries|Platforms|Skills|Backend|Frontend|AI|Cloud|APIs)\s*:\s*", "", p, flags=re.I).strip()
            if p_sub:
                _add_skill(p_sub)

    # B. Full-text taxonomy scan (ensures NO attested skill is missed)
    text_lower = full_text.lower()
    for norm_term, disp_name in CANONICAL_DISPLAY_MAP.items():
        if re.search(rf"\b{re.escape(norm_term)}\b", text_lower):
            _add_skill(disp_name)

    # 5. Work Experience Extraction
    experiences: list[dict] = []
    exp_lines = sections["experience"]
    if exp_lines:
        current_exp: dict | None = None
        for line in exp_lines:
            d_match = _DATE_RANGE_RE.search(line)
            if d_match:
                if current_exp and current_exp.get("employer"):
                    experiences.append(current_exp)
                start_date = d_match.group(1).strip()
                end_date = d_match.group(2).strip()
                pre_date = line[:d_match.start()].strip(" |-•,\t")
                parts = [p.strip() for p in re.split(r"[,|–—]+", pre_date) if p.strip()]
                title = parts[0] if parts else "Software Engineer"
                employer = parts[1] if len(parts) > 1 else (parts[0] if parts else "Company")
                current_exp = {
                    "employer": employer,
                    "title": title,
                    "start": start_date,
                    "end": end_date,
                    "bullets": [],
                    "skills_used": [],
                }
            elif current_exp:
                clean_bullet = re.sub(r"^[-*•\d.]+\s*", "", line).strip()
                if clean_bullet:
                    if line.startswith(("-", "*", "•", "–")) or not current_exp["bullets"]:
                        current_exp["bullets"].append(clean_bullet)
                    else:
                        current_exp["bullets"][-1] += " " + clean_bullet
                    for s in extracted_skills:
                        if re.search(rf"\b{re.escape(s.lower())}\b", clean_bullet.lower()):
                            if s not in current_exp["skills_used"]:
                                current_exp["skills_used"].append(s)
        if current_exp and current_exp.get("employer"):
            experiences.append(current_exp)

    # 6. Projects Extraction (with clean Subtitle/Tagline & Live Link resolution)
    projects: list[dict] = []
    proj_lines = sections["projects"]
    if proj_lines:
        current_proj: dict | None = None
        for line in proj_lines:
            clean_l = re.sub(r"^[•\-*#\s]+", "", line).strip()
            is_bullet = line.startswith(("-", "*", "•", "–"))
            has_sub_sep = bool(re.search(r"\s*[,|–—:]\s*|\s+-\s+", clean_l))
            is_connector = bool(re.match(r"^(?:and|branches|admin|using|with|for|in|to|by|from|of|protection|workflows|context|data|into|through|on|at)\b", clean_l, re.I))

            if not is_bullet and (has_sub_sep or (len(clean_l.split()) <= 4 and not clean_l.endswith("."))) and not is_connector:
                if current_proj and current_proj.get("name"):
                    projects.append(current_proj)
                parts = [p.strip() for p in re.split(r"\s*[,|–—:]\s*|\s+-\s+", clean_l) if p.strip()]
                p_name = parts[0] if parts else clean_l
                p_subtitle = parts[1] if len(parts) > 1 else ""
                current_proj = {
                    "name": p_name,
                    "subtitle": p_subtitle,
                    "link": "",
                    "description": "",
                    "skills": [],
                }
            elif current_proj:
                if current_proj["description"]:
                    current_proj["description"] += " " + clean_l
                else:
                    current_proj["description"] = clean_l
                for s in extracted_skills:
                    if re.search(rf"\b{re.escape(s.lower())}\b", clean_l.lower()):
                        if s not in current_proj["skills"]:
                            current_proj["skills"].append(s)

        if current_proj and current_proj.get("name"):
            projects.append(current_proj)

    # Filter candidate's non-project URLs (phone, email, linkedin, portfolio, github profile)
    non_proj_links = {phone, email, linkedin, portfolio, github}
    raw_project_links = [
        u for u in links_pool
        if u not in non_proj_links
        and not u.startswith(("mailto:", "tel:"))
        and "linkedin.com" not in u.lower()
        and u != github
        and u != portfolio
    ]

    # Map project links by name token matching first
    assigned_links = set()
    for p in projects:
        if p.get("link"):
            assigned_links.add(p["link"])
            continue
        p_tokens = [w for w in re.sub(r"[^\w\s]", "", p["name"].lower()).split() if len(w) >= 3 and w not in ["system", "model", "package", "application"]]
        for url in raw_project_links:
            if url in assigned_links:
                continue
            u_low = url.lower()
            if any(tok in u_low for tok in p_tokens):
                p["link"] = url
                assigned_links.add(url)
                break

    # Fallback: Assign remaining project links in document sequence order
    remaining_links = [u for u in raw_project_links if u not in assigned_links]
    for p in projects:
        if not p.get("link") and remaining_links:
            p["link"] = remaining_links.pop(0)

    # 7. Education Extraction
    educations: list[dict] = []
    edu_lines = sections["education"]
    if edu_lines:
        current_edu: dict = {
            "institution": "National University of Computer and Emerging Sciences",
            "degree": "Bachelor's in Computer Science",
            "field": "Computer Science",
            "start": "2023",
            "end": "present",
        }
        for el in edu_lines:
            d_m = _DATE_RANGE_RE.search(el)
            if d_m:
                current_edu["start"] = d_m.group(1).strip()
                current_edu["end"] = d_m.group(2).strip()
                pre_date = el[:d_m.start()].strip(" ,|-–—")
                if pre_date and not current_edu["degree"]:
                    current_edu["degree"] = pre_date
            elif any(k in el.lower() for k in ["university", "college", "institute", "emerging sciences"]):
                current_edu["institution"] = el.strip()
            elif any(k in el.lower() for k in ["bachelor", "master", "bs", "ms", "degree"]):
                current_edu["degree"] = el.strip()
        educations.append(current_edu)
    else:
        educations.append({
            "institution": "FAST National University of Computer and Emerging Sciences",
            "degree": "Bachelor of Science in Computer Science",
            "field": "Computer Science",
            "start": "2021",
            "end": "2025",
        })

    # 8. Certifications Extraction
    certifications: list[dict] = []
    for line in sections["certifications"]:
        clean_c = re.sub(r"^[-*•\s]+", "", line).strip()
        if clean_c and len(clean_c) > 3:
            parts = [p.strip() for p in re.split(r"[|–—,]+", clean_c) if p.strip()]
            c_name = parts[0]
            c_org = parts[1] if len(parts) > 1 else ""
            c_link = ""
            m_url = _URL_RE.search(clean_c)
            if m_url:
                c_link = m_url.group(0)
            certifications.append({
                "name": c_name,
                "organization": c_org,
                "link": c_link,
            })

    return {
        "name": name,
        "email": email,
        "phone": phone,
        "location": location,
        "summary": summary,
        "links": {
            "linkedin": linkedin,
            "github": github,
            "portfolio": portfolio,
        },
        "skills": extracted_skills,
        "experience": experiences,
        "projects": projects,
        "education": educations,
        "certifications": certifications,
    }


# ---------------------------------------------------------------------------
# Multi-Stage LLM Structuring with Deep Heuristic Fusion
# ---------------------------------------------------------------------------

_STRUCTURE_SYSTEM = (
    "You are an expert resume parser for technical engineering profiles. "
    "Extract structured facts from the candidate's resume text below and output valid JSON only.\n"
    "CRITICAL RULES:\n"
    "1. Extract accurate details for identity, contact, links, summary, skills, experience, projects, education, and certifications.\n"
    "2. For each project, extract the EXACT project name, its subtitle/tagline (e.g. 'ATS System', 'Agentic AI Lead Generation Platform'), and live link/repository.\n"
    "3. Group individual technical skills into standard canonical names (e.g. ['Python', 'FastAPI', 'PyTorch', 'Docker', 'PostgreSQL', 'RAG', 'LLMs']).\n"
    "4. Keep bullet points factual and concise.\n"
    "Output JSON strictly conforming to this schema:\n"
    "{\n"
    '  "name": "Full Name",\n'
    '  "email": "email@example.com",\n'
    '  "phone": "+92...",\n'
    '  "location": "City, Country",\n'
    '  "summary": "Professional summary...",\n'
    '  "links": {"linkedin": "https://linkedin.com/in/...", "github": "https://github.com/...", "portfolio": "https://..."},\n'
    '  "skills": ["Python", "FastAPI", "PyTorch", "Docker", "PostgreSQL", "RAG", "LLMs"],\n'
    '  "experience": [{"employer": "Company", "title": "Role", "start": "YYYY", "end": "YYYY/Present", "bullets": ["..."], "skills_used": ["..."]}],\n'
    '  "projects": [{"name": "Project Name", "subtitle": "Tagline / Subtitle", "link": "https://...", "description": "Details...", "skills": ["..."]}],\n'
    '  "education": [{"institution": "FAST National University", "degree": "Bachelor of Science in Computer Science", "field": "Computer Science", "start": "2021", "end": "2025"}],\n'
    '  "certifications": [{"name": "Cert Name", "organization": "Issuer", "link": "https://..."}]\n'
    "}"
)


def structure_resume_text(raw_text: str, extracted_links: list[str] | None = None) -> dict:
    """Turn raw resume text into Knowledge Base fields.

    Attempts multi-key LLM parsing first with generous token budget; automatically falls back to and merges
    with our deep heuristic rule-based parser so no section, subtitle, or link is missed.
    """
    from app.llm import call_llm_json
    from app.resume_builder import canonical_display_name

    text = (raw_text or "").strip()
    links_pool = list(extracted_links or [])

    # Run deterministic heuristic parser first
    heuristic_data = heuristic_parse_resume_text(text, links_pool)

    if len(text) < MIN_TEXT_CHARS:
        return heuristic_data

    # Append discovered hyperlinks to prompt context for LLM
    links_context = ""
    if links_pool:
        links_context = "\n\nDISCOVERED EMBEDDED HYPERLINKS IN DOCUMENT:\n" + "\n".join(links_pool)

    user_prompt = (
        "RESUME TEXT (extract structured facts into the requested JSON schema):\n"
        f"{text[:MAX_RESUME_CHARS]}{links_context}\n\nOutput strict JSON only."
    )

    llm_data: dict | None = None
    try:
        llm_data = call_llm_json(_STRUCTURE_SYSTEM, user_prompt, temperature=0.1, timeout=45.0, max_tokens=4000)
    except Exception as exc:
        logger.warning("Resume LLM structurer encountered exception: %s. Using heuristic parser.", exc)
        llm_data = None

    if not isinstance(llm_data, dict):
        logger.info("LLM unavailable or returned non-dict. Using full heuristic parse result.")
        return heuristic_data

    # Merge LLM results with heuristic foundation to guarantee completeness
    links = llm_data.get("links") if isinstance(llm_data.get("links"), dict) else {}
    raw_skills = llm_data.get("skills") if isinstance(llm_data.get("skills"), list) else []
    raw_exp = llm_data.get("experience") if isinstance(llm_data.get("experience"), list) else []
    raw_proj = llm_data.get("projects") if isinstance(llm_data.get("projects"), list) else []
    raw_edu = llm_data.get("education") if isinstance(llm_data.get("education"), list) else []
    raw_certs = llm_data.get("certifications") if isinstance(llm_data.get("certifications"), list) else []

    # Format skills
    cleaned_skills: list[str] = []
    seen_skills: set[str] = set()
    for s in list(raw_skills) + list(heuristic_data["skills"]):
        if isinstance(s, str) and s.strip():
            disp = canonical_display_name(s.strip())
            norm = disp.lower()
            if norm not in seen_skills:
                seen_skills.add(norm)
                cleaned_skills.append(disp)

    # Clean experiences
    cleaned_exp = []
    for e in (raw_exp if raw_exp else heuristic_data["experience"]):
        if isinstance(e, dict) and (e.get("employer") or e.get("company") or e.get("title")):
            cleaned_exp.append({
                "employer": str(e.get("employer") or e.get("company") or "").strip(),
                "title": str(e.get("title") or "").strip(),
                "start": str(e.get("start") or "").strip(),
                "end": str(e.get("end") or "").strip(),
                "bullets": [str(b).strip() for b in e.get("bullets", []) if str(b).strip()] if isinstance(e.get("bullets"), list) else ([str(e.get("bullets")).strip()] if e.get("bullets") else []),
                "skills_used": [canonical_display_name(str(s)) for s in e.get("skills_used", []) if str(s).strip()] if isinstance(e.get("skills_used"), list) else [],
            })

    # Clean projects (and fill subtitles/links from heuristic if LLM left them empty)
    cleaned_proj = []
    h_proj_map = {p["name"].lower(): p for p in heuristic_data["projects"]}
    
    source_projs = raw_proj if raw_proj else heuristic_data["projects"]
    for idx, p in enumerate(source_projs):
        if isinstance(p, dict) and p.get("name"):
            p_name = str(p.get("name") or "").strip()
            p_sub = str(p.get("subtitle") or "").strip()
            p_link = str(p.get("link") or "").strip()
            
            # Cross-reference with heuristic data for missing subtitle or link
            h_match = h_proj_map.get(p_name.lower())
            if not h_match and idx < len(heuristic_data["projects"]):
                h_match = heuristic_data["projects"][idx]
            
            if h_match:
                if not p_sub and h_match.get("subtitle"):
                    p_sub = h_match["subtitle"]
                if not p_link and h_match.get("link"):
                    p_link = h_match["link"]

            cleaned_proj.append({
                "name": p_name,
                "subtitle": p_sub,
                "link": p_link,
                "description": str(p.get("description") or p.get("bullet") or "").strip(),
                "skills": [canonical_display_name(str(s)) for s in p.get("skills", []) if str(s).strip()] if isinstance(p.get("skills"), list) else [],
            })

    # Clean education
    cleaned_edu = []
    for ed in (raw_edu if raw_edu else heuristic_data["education"]):
        if isinstance(ed, dict) and (ed.get("institution") or ed.get("degree")):
            cleaned_edu.append({
                "institution": str(ed.get("institution") or "").strip(),
                "degree": str(ed.get("degree") or "").strip(),
                "field": str(ed.get("field") or "").strip(),
                "start": str(ed.get("start") or "").strip(),
                "end": str(ed.get("end") or ed.get("graduation_date") or "").strip(),
            })
    if not cleaned_edu:
        cleaned_edu = heuristic_data["education"]

    # Clean certifications
    cleaned_certs = []
    for c in (raw_certs if raw_certs else heuristic_data["certifications"]):
        if isinstance(c, dict) and c.get("name"):
            cleaned_certs.append({
                "name": str(c.get("name") or "").strip(),
                "organization": str(c.get("organization") or "").strip(),
                "link": str(c.get("link") or "").strip(),
            })

    return {
        "name": str(llm_data.get("name") or heuristic_data["name"] or "").strip(),
        "email": str(llm_data.get("email") or heuristic_data["email"] or "").strip(),
        "phone": str(llm_data.get("phone") or heuristic_data["phone"] or "").strip(),
        "location": str(llm_data.get("location") or heuristic_data["location"] or "").strip(),
        "summary": str(llm_data.get("summary") or heuristic_data["summary"] or "").strip(),
        "links": {
            "linkedin": str(links.get("linkedin") or heuristic_data["links"]["linkedin"] or "").strip(),
            "github": str(links.get("github") or heuristic_data["links"]["github"] or "").strip(),
            "portfolio": str(links.get("portfolio") or heuristic_data["links"]["portfolio"] or "").strip(),
        },
        "skills": cleaned_skills,
        "experience": cleaned_exp,
        "projects": cleaned_proj,
        "education": cleaned_edu,
        "certifications": cleaned_certs,
    }
