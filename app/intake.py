"""Application intake: (JD text or image) + email -> a pending approval.

This is the shared entry point for both the API (Radar pushes here) and the GUI
manual form. Image JDs are OCR'd via RapidOCR; then Groq LLM (openai/gpt-oss-120b)
extracts structured details (company, role, email, link, clean JD).
The LLM drafts the application email and tailors the 1-page resume.
"""

from __future__ import annotations

import json
import logging
import re
from sqlalchemy.orm import Session

from app import config
from app.knowledge import load_candidate
from app.llm import _post, write_application_email
from app.models import Application
from app.ocr import image_to_text
from app.readiness import format_blocked_message, kb_completeness
from app.roles import detect_and_select_role
from app.tailor import tailor_application_resume

logger = logging.getLogger("careerpulse.intake")


class IntakeError(ValueError):
    """Raised when there is no usable JD (e.g. OCR produced nothing)."""


_VALID_EMAIL_RE = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)*\.[a-zA-Z]{2,}$")


def _clean_email(raw: str, fallback: str = "") -> str:
    """Validate and clean an email address extracted from LLM or OCR output.

    - Strips leading/trailing whitespace and trailing dots (OCR artefacts).
    - If the cleaned string passes a strict email regex, returns it.
    - Otherwise returns *fallback* (usually the deterministic regex hit).
    """
    candidate_email = (raw or "").strip().rstrip(". ")
    # Remove any trailing ellipsis or dot sequences produced by OCR truncation
    candidate_email = re.sub(r'\.{2,}$', '', candidate_email)
    if _VALID_EMAIL_RE.match(candidate_email):
        return candidate_email
    # LLM gave something malformed – use the deterministic regex fallback
    return (fallback or "").strip()


def _clean_company_name(company: str, email: str = "", raw_text: str = "") -> str:
    """Clean and sanitize extracted company name.

    Strips promotional phrases like "We're Hiring!", "Join X as...",
    emojis, URLs, and overly long sentences. If empty or invalid,
    infers from raw_text or email domain.
    """
    c = (company or "").strip().strip("*#_\"' ")
    # Remove emojis
    c = re.sub(r"[\U00010000-\U0010ffff]", "", c).strip()

    # Pattern: Join <Company> as / for / team
    m = re.search(r"\bjoin\s+([A-Za-z0-9\s&.\-]+?)\s+(?:as|for|team)\b", c, re.I)
    if m:
        c = m.group(1).strip()
    else:
        # Pattern: at / for / with <Company>
        m2 = re.search(r"\b(?:at|with|for)\s+([A-Za-z0-9\s&.\-]+?)(?:,|\.|\s+we\s+|\s+is\s+|$)", c, re.I)
        if m2 and len(m2.group(1).split()) <= 4:
            cand = m2.group(1).strip()
            if not re.search(r"\b(degree|computer|engineering|science|team|our|fast|hiring)\b", cand, re.I):
                c = cand

    # Strip prefixes like 'We are hiring!', 'Hiring:', 'About', etc.
    c = re.sub(r"^(?:we(?:\'re|\s+are)\s+hiring!?\s*|hiring\s*(?:for)?\s*[:\-–]?\s*|about\s+)", "", c, flags=re.I).strip()
    # Strip trailing role fragments
    c = re.sub(r"\s+as\s+(?:an?\s+)?(?:software|engineer|developer|intern|lead|manager|specialist|analyst).*", "", c, flags=re.I).strip()

    # If empty or still a sentence (>4 words or >35 chars), infer from raw_text hashtags or email domain
    if not c or len(c.split()) > 4 or len(c) > 35:
        inferred = ""
        if raw_text:
            lines = [ln.strip() for ln in raw_text.splitlines() if ln.strip()]
            for ln in lines[:4]:
                if not re.search(r"\b(following|likes this|others|hiring|we\'re|who we|how to|apply|comments)\b", ln, re.I) and len(ln.split()) <= 4 and len(ln) <= 30:
                    inferred = ln
                    break
            if not inferred:
                m_join = re.search(r"\bjoin\s+([A-Za-z0-9\s&.\-]+?)\s+(?:as|for|team)\b", raw_text, re.I)
                if m_join and len(m_join.group(1).split()) <= 4:
                    inferred = m_join.group(1).strip()
                else:
                    m_at = re.search(r"\bat\s+([A-Za-z0-9\s&.\-]+?)(?:,|\.|\s+we\s+|\s+is\s+|$)", raw_text, re.I)
                    if m_at and len(m_at.group(1).split()) <= 4:
                        cand = m_at.group(1).strip()
                        if not re.search(r"\b(degree|computer|engineering|science|team|our|fast|hiring)\b", cand, re.I):
                            inferred = cand
        if inferred:
            c = inferred

        if (not c or len(c.split()) > 4 or len(c) > 35) and email:
            domain = email.split("@")[-1].lower()
            generic = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com", "proton.me", "mail.com"}
            if domain not in generic:
                base = domain.split(".")[0]
                if base == "aicrucio":
                    c = "AI Crucio"
                else:
                    c = base.capitalize()

    # Clean up AI casing & formatting
    c = re.sub(r"\bAi\b", "AI", c)
    return re.sub(r"\s+", " ", c).strip()


def _clean_job_title(title: str, raw_text: str = "") -> str:
    """Clean and sanitize extracted job title."""
    if not title:
        return "Software Engineer"
    t = title.strip().strip("*#_\"' ")
    t = re.sub(r"[\U00010000-\U0010ffff]", "", t).strip()
    t = re.sub(r"^.*?join\s+[A-Za-z0-9\s&.\-]+?\s+as\s+(?:an?\s+)?", "", t, flags=re.I).strip()
    t = re.sub(r"^(?:we(?:\'re|\s+are)\s+hiring!?\s*|hiring\s*(?:for)?\s*|looking\s+for\s+(?:an?\s+)?)[:\-–]?\s*", "", t, flags=re.I).strip()
    # Remove marketing suffixes e.g. "| Fresh Graduates Welcome!" or "- Apply Now"
    t = re.sub(r"\s*[|•\-–—]\s*(?:fresh\s+graduates?|apply\s+now|urgent|full\s*time|part\s*time|internship|remote|on\s*site|hybrid|immediate).*$", "", t, flags=re.I).strip()
    # Normalize OCR "Al" -> "AI"
    t = re.sub(r"\bAl\b(?=\s+(?:Engineer|Developer|Scientist|Specialist|Researcher|Intern|Architect|Lead|Associate|Manager|Agent|Agents|Concepts|Model|Models|ML|GenAI|Prompt))", "AI", t)
    if "engineer" in t.lower() or "developer" in t.lower() or "agent" in t.lower() or "scientist" in t.lower():
        t = re.sub(r"\bAl\b", "AI", t)
    t = re.sub(r"\bAi\b", "AI", t)
    t = re.sub(r"^[:\-–—.,|]+\s*", "", t).strip()
    t = re.sub(r"[.!?:;]+$", "", t).strip()
    return t or "Software Engineer"


def _clean_subject(subject: str, job_title: str = "", company: str = "") -> str:
    """Clean and sanitize extracted email subject line."""
    if not subject:
        return f"Application for {job_title}" if job_title else "Application"
    s = subject.strip().strip("*#_\"' ")
    s = re.sub(r"^subject\s*:\s*", "", s, flags=re.I).strip()
    # Normalize OCR "Al" -> "AI" and separate words if joined
    s = re.sub(r"\bAl([A-Z])", r"AI \1", s)
    s = re.sub(r"\bAi([A-Z])", r"AI \1", s)
    s = re.sub(r"\bAl\b", "AI", s)
    s = re.sub(r"\bAi\b", "AI", s)
    # Fix camel-case like AIEngineer -> AI Engineer
    s = re.sub(r"\bAI([A-Z][a-z]+)", r"AI \1", s)
    return s.strip() or (f"Application for {job_title}" if job_title else "Application")


def parse_job_posting_with_llm(raw_text: str, target_role: str = "") -> dict[str, str]:
    """Parse raw job posting text (from image OCR or text paste) using Groq LLM.
    Extracts structured fields: company, job_title, email, whatsapp, link, location, prescribed_subject, jd_text.
    Filters out all mobile UI artifacts, status bars, and social media engagement fluff.
    Falls back gracefully to robust regex heuristics if LLM is unavailable.
    """
    cleaned_input = (raw_text or "").strip()
    if not cleaned_input:
        return {
            "company": "",
            "job_title": target_role or "Software Engineer",
            "email": "",
            "whatsapp": "",
            "link": "",
            "location": "",
            "prescribed_subject": "",
            "jd_text": "",
        }

    # 1. Deterministic regex fallbacks
    found_emails = re.findall(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", cleaned_input)
    fallback_email = found_emails[0] if found_emails else ""

    found_links = re.findall(r"https?://[^\s<>\"']+", cleaned_input)
    fallback_link = found_links[0] if found_links else ""

    fallback_title = target_role or ""
    fallback_company = ""
    for line in cleaned_input.splitlines()[:10]:
        line_clean = line.strip().strip("*# ")
        if not fallback_title and re.search(
            r"\b(engineer|developer|specialist|intern|architect|consultant|analyst|ase|lead)\b",
            line_clean,
            re.I,
        ):
            fallback_title = _clean_job_title(line_clean)
        if not fallback_company and re.search(r"\b(company|inc|llc|tech|software|logics|corp|work)\b", line_clean, re.I):
            fallback_company = _clean_company_name(line_clean, email=fallback_email, raw_text=cleaned_input)

    fallback_company = _clean_company_name(fallback_company, email=fallback_email, raw_text=cleaned_input)
    fallback_title = _clean_job_title(fallback_title or target_role)

    fallback_prescribed_sub = ""
    sub_match = re.search(
        r"(?:email\s+subject|subject(?:\s+line)?)\s*[:\-–]\s*([^\n\r]+)",
        cleaned_input,
        re.I,
    )
    if sub_match:
        fallback_prescribed_sub = sub_match.group(1).strip().strip("\"'")

    fallback_result = {
        "company": fallback_company,
        "job_title": fallback_title or "Software Engineer",
        "email": fallback_email,
        "whatsapp": "",
        "link": fallback_link,
        "location": "",
        "prescribed_subject": fallback_prescribed_sub,
        "subject": fallback_prescribed_sub,
        "jd_text": cleaned_input,
    }

    # 2. Query LLM (Groq openai/gpt-oss-120b or fast qwen fallback)
    if not config.LLM_API_KEY:
        return fallback_result

    target_role_instruction = (
        f"CRITICAL TARGET ROLE: The applicant is specifically applying for: '{target_role}'. "
        f"If the posting lists multiple positions, extract requirements and tailor all fields EXCLUSIVELY for '{target_role}'."
        if target_role
        else "Extract the primary role advertised in the posting."
    )

    prompt = (
        "You are an expert recruitment parser and information extractor. Analyze the following raw job description text "
        "extracted from a job posting or mobile screenshot OCR. Extract the genuine structured job details into a single valid JSON object.\n\n"
        "STRICT NOISE FILTERING & EXTRACTION RULES:\n"
        "- COMPANY NAME: Extract ONLY the concise official company name (e.g. 'AI Crucio', 'Systems Limited'). NEVER include promotional sentences or phrases like 'We are hiring! Join AI Crucio as an Associate Software' in the company field.\n"
        "- JOB TITLE: Extract the clean official job title (e.g. 'Associate Software Engineer', 'Machine Learning Engineer').\n"
        "- IGNORE all mobile status bar noise (e.g. clock times like '9:39', '1:52', battery percentages '19%', Wi-Fi symbols, notification icons).\n"
        "- IGNORE social media fluff (e.g. 'Asad Nadeem and Muhammad Farjad Ali Raza like this', comment counts, 'Following', share buttons).\n"
        "- EMAIL: Extract the complete valid email address to submit applications to (e.g. 'contact@aicrucio.com').\n\n"
        f"{target_role_instruction}\n\n"
        f"RAW TEXT:\n\"\"\"\n{cleaned_input[:6000]}\n\"\"\"\n\n"
        "Output ONLY a single valid JSON object with these exact keys:\n"
        "{\n"
        '  "company": "Clean Company Name (e.g. AI Crucio)",\n'
        '  "job_title": "Clean Job Title / Role (e.g. Associate Software Engineer)",\n'
        '  "email": "COMPLETE valid email address (format: user@domain.tld) to submit the CV/application to.",\n'
        '  "whatsapp": "Contact WhatsApp or phone number if specified (or empty string)",\n'
        '  "link": "Application URL link (or empty string if none)",\n'
        '  "location": "Location / Remote status (e.g. 100% Remote, Lahore, etc.)",\n'
        '  "prescribed_subject": "Exact required email subject line if the employer specified one, else empty string"\n'
        "}\n"
    )

    try:
        resp = _post(
            {
                "model": config.LLM_MODEL,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are a precise recruitment JSON extractor. You strip away all noise, "
                            "extract the exact required details, and output valid JSON only with no conversational text."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.1,
                "reasoning_effort": "low",
                "max_tokens": 2000,
            },
            timeout=35.0,
        )
        if resp:
            m = re.search(r"\{.*\}", resp, re.DOTALL)
            if m:
                data = json.loads(m.group(0))
                detected_company = str(data.get("company") or "").strip()
                detected_title = str(data.get("job_title") or "").strip()
                detected_email_raw = str(data.get("email") or "").strip()
                # Validate LLM email; fall back to deterministic regex if malformed/truncated
                detected_email = _clean_email(detected_email_raw, fallback=fallback_email)
                detected_subject = str(data.get("prescribed_subject") or "").strip()

                final_company = _clean_company_name(detected_company or fallback_company, email=detected_email, raw_text=cleaned_input)
                final_title = target_role or _clean_job_title(detected_title or fallback_result["job_title"])
                final_sub = _clean_subject(detected_subject or fallback_prescribed_sub, job_title=final_title, company=final_company)
                return {
                    "company": final_company,
                    "job_title": final_title,
                    "email": detected_email,
                    "whatsapp": str(data.get("whatsapp") or "").strip(),
                    "link": str(data.get("link") or "").strip() or fallback_link,
                    "location": str(data.get("location") or "").strip(),
                    "prescribed_subject": final_sub,
                    "subject": final_sub,
                    "jd_text": cleaned_input,
                }
    except Exception:
        pass

    return fallback_result


def resolve_intake_text(
    jd_text: str | None = None,
    image_bytes: bytes | None = None,
    image_mime: str = "image/png",
    client_ocr_text: str = "",
) -> str:
    """Resolve the raw JD text from pasted text and/or an uploaded image.

    Shared by the /new UI (role gate runs before generation) and
    create_application. Raises IntakeError when nothing readable is found.
    """
    raw_text = (jd_text or "").strip()

    if image_bytes or client_ocr_text:
        ocr_text = (image_to_text(image_bytes, client_text=client_ocr_text) or "").strip()
        if ocr_text:
            raw_text = f"{raw_text}\n\n{ocr_text}".strip() if raw_text else ocr_text
        elif not raw_text:
            raise IntakeError(
                "Could not read text from the image. Please upload a clearer screenshot or paste the job description text."
            )
    if not raw_text:
        raise IntakeError("Please upload a job posting screenshot or paste the job description text.")
    return raw_text


def create_application(
    session: Session,
    email: str = "",
    jd_text: str | None = None,
    image_bytes: bytes | None = None,
    image_mime: str = "image/png",
    job_title: str = "",
    target_role: str = "",
    company: str = "",
    link: str = "",
    job_id: int | None = None,
    client_ocr_text: str = "",
    user_id: int | None = None,
    user: object | None = None,
    pre_resolved_text: str | None = None,
    match_report=None,
) -> Application:
    raw_text = (pre_resolved_text or "").strip() or resolve_intake_text(
        jd_text, image_bytes, image_mime, client_ocr_text
    )
    effective_target_role = (target_role or job_title or "").strip()

    candidate = load_candidate(user)

    # Readiness gate: below-minimum profiles cannot start generation.
    readiness = kb_completeness(candidate)
    if not readiness.ready:
        raise IntakeError(format_blocked_message(readiness))

    # Role detection + best-fit selection in a single structured LLM call
    # (structured input: JD + KB profile; structured output: roles + best_role).
    # An explicit target_role from the caller is used as-is (no LLM call).
    # Never blocks the intake flow.
    resolution = detect_and_select_role(
        raw_text,
        user_pick=target_role,
        job_title=job_title,
        candidate=candidate,
    )
    if resolution.role:
        effective_target_role = resolution.role
        logger.info("role resolved via %s: %r", resolution.method, resolution.role)

    # Automatically parse structured fields with Groq LLM (openai/gpt-oss-120b)
    parsed = parse_job_posting_with_llm(raw_text, target_role=effective_target_role)

    # Use manual parameters if explicitly provided, otherwise use parsed
    final_email = (email or "").strip() or parsed.get("email", "")
    final_job_title = _clean_job_title(effective_target_role or parsed.get("job_title", "") or "Software Engineer")
    final_company = _clean_company_name((company or "").strip() or parsed.get("company", ""), email=final_email, raw_text=raw_text)
    final_link = (link or "").strip() or parsed.get("link", "")
    final_jd = (raw_text or "").strip().replace("\u2011", "-")

    # Determine email subject line:
    # 1. If employer prescribed a specific subject in the JD, adopt it exactly!
    prescribed_subject = (parsed.get("prescribed_subject") or "").strip()
    if prescribed_subject:
        # Substitute name placeholder if present, e.g. [Your Name] -> candidate.name
        subject = re.sub(
            r"\[(?:your\s+)?name\]|\((?:your\s+)?name\)|<name>",
            candidate.name,
            prescribed_subject,
            flags=re.IGNORECASE,
        ).strip()
        # Clean common OCR typos in subject: e.g. "AlAutomation" -> "AI Automation", unicode hyphens.
        # NOTE: the bare-word "\bAl\b" -> "AI" replacement was removed — it
        # rewrote the legitimate chemical symbol "Al" (aluminium) anywhere it
        # appeared as a standalone word.
        subject = subject.replace("\u2011", "-")
        subject = re.sub(r"\bAl([A-Z])", r"AI \1", subject)
        subject = re.sub(r"\bAI([A-Z])", r"AI \1", subject)
    elif final_company and final_job_title:
        subject = f"Application for {final_job_title} — {final_company}"
    else:
        subject = f"Application for {final_job_title or 'Open Role'}"

    project_names = [
        p.get("short_name") or p.get("name")
        for p in candidate.projects[:2]
    ]
    # Drop missing names — the email fallback must never render "None".
    project_names = [n for n in project_names if n]
    drafted = write_application_email(
        candidate, jd_text=final_jd, company=final_company, role=final_job_title, project_names=project_names
    )

    selected_template = getattr(user, "selected_template_id", None) or "apex_modern"
    app_row = Application(
        user_id=user_id,
        job_id=job_id,
        email=final_email,
        job_title=final_job_title,
        company=final_company,
        link=final_link,
        jd_text=final_jd,
        subject=subject,
        drafted_email=drafted,
        template_id=selected_template,
        # Phase 15: explicit approval workflow — every application starts as
        # a draft; tailoring success below promotes it to 'ready'.
        status="draft",
    )
    session.add(app_row)
    session.flush()

    try:
        res = tailor_application_resume(
            application_id=app_row.id,
            jd_text=final_jd,
            job_title=final_job_title,
            company=final_company,
            max_projects=5,
            variant_override=selected_template,
            candidate=candidate,
            user=user,
            # Phase 5: the pre-generation gap report steers the optimizer
            # directly (no extra LLM interpretation call, no lost refinement).
            optimizer_brief=match_report.optimizer_focus if match_report else "",
        )
        app_row.resume_path = res.resume_pdf_path
        app_row.ats_score = res.ats_score
        app_row.ats_attempts = res.ats_attempts
        app_row.ats_note = getattr(res.ats_report, "note", "") or ""
        # Phase 15: tailoring succeeded — the application is ready for review.
        app_row.status = "ready"
        session.flush()
        # Phase 8: persist this generation as a restorable version (FR-R-03).
        from app.db import record_resume_version
        record_resume_version(
            session,
            application_id=app_row.id,
            result=res,
            jd_text=final_jd,
            job_title=final_job_title,
        )
    except Exception as exc:
        print(f"[Intake] Notice: Resume tailoring warning: {exc}")

    # Immediately persist to Supabase cloud database
    try:
        from radar.supabase_client import SupabaseClient
        sb = SupabaseClient()
        if sb.is_configured:
            cloud_res = sb.upsert_application(
                {
                    "id": app_row.id,
                    "user_id": app_row.user_id,
                    "job_id": app_row.job_id,
                    "email": app_row.email or "portal-application@careerpulse.internal",
                    "job_title": app_row.job_title or "",
                    "company": app_row.company or "",
                    "link": app_row.link or "",
                    "jd_text": (app_row.jd_text or "")[:4000],
                    "drafted_email": (app_row.drafted_email or "")[:4000],
                    "subject": app_row.subject or "",
                    "resume_path": app_row.resume_path or "",
                    "ats_score": float(app_row.ats_score or 0.0),
                    "ats_attempts": int(app_row.ats_attempts or 0),
                    "ats_note": app_row.ats_note or "",
                    "status": app_row.status or "draft",
                    "disposition": app_row.disposition or "",
                    "sent_at": app_row.sent_at.isoformat() if app_row.sent_at else None,
                    "created_at": app_row.created_at.isoformat() if app_row.created_at else None,
                    "updated_at": app_row.updated_at.isoformat() if app_row.updated_at else None,
                },
                session=session,
            )
            if cloud_res and cloud_res.get("id"):
                app_row.id = int(cloud_res["id"])
                session.commit()
    except Exception as exc:
        print(f"[Intake] Notice: Supabase sync warning: {exc}")

    return app_row

