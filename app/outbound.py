"""Outbound email dispatch with multi-layer safety guards.

Three independent safety properties:
1. TEST_MODE (default: 1): Every email is redirected to JOBHUNTER_TEST_RECIPIENT
   (haseeb.rahman0566@gmail.com) with '[TEST MODE]' in the subject and original
   recipient saved in X-CareerPulse-Intended-To header.
2. ALLOW_REAL_EMAIL (default: 0): Sending to real employers requires CAREERPULSE_ALLOW_REAL_EMAIL=1.
3. Fail-safe drafting: Unconfigured SMTP or blocked sends draft to `data/outbox/` as .eml.
"""

from __future__ import annotations

import datetime as dt
import logging
import smtplib
import uuid
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path

from app import config
from app.knowledge import load_candidate

logger = logging.getLogger(__name__)


@dataclass
class OutboundResult:
    disposition: str  # sent | redirected | drafted | failed | blocked
    recipient: str
    original_recipient: str
    message_id: str | None = None
    draft_path: str | None = None
    reason: str = ""

    @property
    def success(self) -> bool:
        return self.disposition in ("sent", "redirected")


class OutboundEmailGuard:
    def __init__(self, outbox_dir: Path | str | None = None) -> None:
        self.outbox_dir = Path(outbox_dir or config.OUTBOX_DIR)
        try:
            self.outbox_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            self.outbox_dir = Path("/tmp/outbox")
            self.outbox_dir.mkdir(parents=True, exist_ok=True)

    def real_send_allowed(self) -> tuple[bool, str]:
        """Check if sending to an arbitrary real employer is permitted."""
        config.ensure_fresh_config()
        if config.TEST_MODE:
            return False, "CAREERPULSE_TEST_MODE is enabled"
        if not config.ALLOW_REAL_EMAIL:
            return False, "CAREERPULSE_ALLOW_REAL_EMAIL is not set to 1"
        return True, ""

    def resolve_recipient(self, intended: str) -> tuple[str, bool]:
        """Return (destination_email, was_redirected)."""
        config.ensure_fresh_config()
        intended = (intended or "").strip()
        if not intended:
            return "", False

        if config.TEST_MODE:
            fallback = config.TEST_RECIPIENT.strip()
            if not fallback:
                return "", True  # No test recipient to safely redirect to
            return fallback, fallback.lower() != intended.lower()

        return intended, False

    def compose_message(
        self,
        recipient: str,
        subject: str,
        body: str,
        resume_pdf_path: Path | str | None = None,
        intended_recipient: str | None = None,
        is_redirected: bool = False,
        user: object | None = None,
        tracking_token: str | None = None,
    ) -> EmailMessage:
        import re
        from app.knowledge import load_candidate

        candidate = load_candidate(user)
        msg = EmailMessage()

        # From header
        sender_name = getattr(user, "sender_name", "") or candidate.name or "Applicant"
        sender_email = (
            (getattr(user, "gmail_email", "") if getattr(user, "gmail_connected", False) else "")
            or getattr(user, "smtp_username", "")
            or getattr(user, "email", "")
            or config.SMTP_USERNAME
            or candidate.email
            or "mirzahaseeb0566@gmail.com"
        )
        msg["From"] = f"{sender_name} <{sender_email}>"
        msg["To"] = recipient

        # Subject line
        final_subject = f"[TEST MODE] {subject}" if is_redirected and not subject.startswith("[TEST MODE]") else subject
        msg["Subject"] = final_subject

        if is_redirected and intended_recipient:
            msg["X-CareerPulse-Intended-To"] = intended_recipient

        if tracking_token:
            msg["X-CareerPulse-Tracking-Token"] = tracking_token

        msg.set_content(body)

        # Attach Resume PDF if available with dynamic user name
        if resume_pdf_path:
            pdf_p = Path(resume_pdf_path)
            if pdf_p.exists():
                pdf_bytes = pdf_p.read_bytes()
                clean_name = re.sub(r"[^\w\-]", "", sender_name.strip().replace(" ", "-")) or "Resume"
                filename = f"{clean_name}-Resume.pdf"
                msg.add_attachment(
                    pdf_bytes,
                    maintype="application",
                    subtype="pdf",
                    filename=filename,
                )

        return msg

    def save_eml(self, msg: EmailMessage, prefix: str = "draft") -> Path:
        """Save .eml copy to data/outbox/ for auditing or offline review."""
        now_str = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S")
        uid = uuid.uuid4().hex[:6]
        eml_path = self.outbox_dir / f"{prefix}_{now_str}_{uid}.eml"
        eml_path.write_bytes(msg.as_bytes())
        return eml_path

    def send(
        self,
        intended_recipient: str,
        subject: str,
        body: str,
        resume_pdf_path: Path | str | None = None,
        user: object | None = None,
        application: object | None = None,
        db_session: object | None = None,
    ) -> OutboundResult:
        """Dispatch application email via Gmail API or SMTP with safety guards."""
        from app.auth import decrypt_credential

        config.ensure_fresh_config()

        intended = (intended_recipient or "").strip()
        if not intended:
            return OutboundResult(
                disposition="blocked",
                recipient="",
                original_recipient="",
                reason="No recipient address provided",
            )

        recipient, redirected = self.resolve_recipient(intended)

        tracking_token = getattr(application, "tracking_token", None) if application else None

        if config.TEST_MODE and not recipient:
            msg = self.compose_message(intended, subject, body, resume_pdf_path, user=user, tracking_token=tracking_token)
            draft = self.save_eml(msg, prefix="blocked_no_test_recipient")
            return OutboundResult(
                disposition="drafted",
                recipient=intended,
                original_recipient=intended,
                draft_path=str(draft),
                reason="CAREERPULSE_TEST_MODE is on but JOBHUNTER_TEST_RECIPIENT is empty",
            )

        if not config.TEST_MODE:
            allowed, why = self.real_send_allowed()
            if not allowed:
                msg = self.compose_message(intended, subject, body, resume_pdf_path, user=user, tracking_token=tracking_token)
                draft = self.save_eml(msg, prefix="blocked_real_send_forbidden")
                return OutboundResult(
                    disposition="drafted",
                    recipient=intended,
                    original_recipient=intended,
                    draft_path=str(draft),
                    reason=f"Real send forbidden: {why}",
                )

        msg = self.compose_message(
            recipient=recipient,
            subject=subject,
            body=body,
            resume_pdf_path=resume_pdf_path,
            intended_recipient=intended,
            is_redirected=redirected,
            user=user,
            tracking_token=tracking_token,
        )

        # Refresh config dynamically from .env
        config.reload_config()

        # =====================================================================
        # ROUTE A: DIRECT GMAIL OAUTH INTEGRATION (WHEN CONNECTED)
        # =====================================================================
        if user and getattr(user, "gmail_connected", False):
            from app import gmail
            from app.models import EmailActivity

            # Ensure we have a db session to update tokens/records if needed
            if db_session is not None:
                gmail_res = gmail.send_gmail_message(user, msg, db_session)
            else:
                from app.db import get_session
                with get_session() as s:
                    u_in_s = s.get(type(user), user.id) or user
                    gmail_res = gmail.send_gmail_message(u_in_s, msg, s)

            if gmail_res.get("success"):
                msg_id = gmail_res.get("message_id") or uuid.uuid4().hex[:12]
                thread_id = gmail_res.get("thread_id", "")
                self.save_eml(msg, prefix="sent_gmail_redirected" if redirected else "sent_gmail_live")

                # Update application metadata if provided
                if application:
                    setattr(application, "gmail_message_id", msg_id)
                    if thread_id:
                        setattr(application, "gmail_thread_id", thread_id)
                    now_utc = dt.datetime.now(dt.timezone.utc)
                    setattr(application, "followup_due_at", now_utc + dt.timedelta(days=3))

                # Record immutable audit activity event
                if db_session is not None and getattr(user, "id", None):
                    try:
                        act = EmailActivity(
                            user_id=user.id,
                            application_id=getattr(application, "id", None),
                            event_type="sent",
                            recipient=recipient,
                            subject=str(msg["Subject"]),
                            details_json='{"provider": "gmail_oauth", "message_id": "' + str(msg_id) + '"}',
                        )
                        db_session.add(act)
                        db_session.commit()

                        try:
                            from radar.supabase_client import SupabaseClient
                            sb = SupabaseClient()
                            if sb.is_configured:
                                sb.insert_email_activity({
                                    "user_id": user.id,
                                    "application_id": getattr(application, "id", None),
                                    "event_type": "sent",
                                    "recipient": recipient,
                                    "subject": str(msg["Subject"]),
                                    "details_json": act.details_json,
                                })
                        except Exception:
                            pass
                    except Exception as act_exc:
                        logger.debug("Failed to record EmailActivity: %s", act_exc)

                return OutboundResult(
                    disposition="redirected" if redirected else "sent",
                    recipient=recipient,
                    original_recipient=intended,
                    message_id=msg_id,
                )
            else:
                err_msg = gmail_res.get("error", "Unknown Gmail API error")
                logger.error("Gmail dispatch failed: %s", err_msg)
                draft = self.save_eml(msg, prefix="failed_gmail")
                return OutboundResult(
                    disposition="failed",
                    recipient=recipient,
                    original_recipient=intended,
                    draft_path=str(draft),
                    reason=f"Gmail API transmission error: {err_msg}",
                )

        # =====================================================================
        # ROUTE B: SMTP DISPATCH (FALLBACK / STANDARD CREDENTIALS)
        # =====================================================================
        smtp_host = getattr(user, "smtp_host", "") or config.SMTP_HOST
        smtp_port = getattr(user, "smtp_port", 0) or config.SMTP_PORT
        smtp_username = getattr(user, "smtp_username", "") or config.SMTP_USERNAME

        enc_pwd = getattr(user, "smtp_password_encrypted", "")
        if enc_pwd:
            smtp_password = decrypt_credential(enc_pwd)
        else:
            smtp_password = config.SMTP_PASSWORD

        if smtp_password and "gmail" in smtp_host.lower():
            smtp_password = smtp_password.replace(" ", "").strip()

        if not smtp_username or not smtp_password:
            draft = self.save_eml(msg, prefix="draft_no_smtp_creds")
            return OutboundResult(
                disposition="drafted",
                recipient=recipient,
                original_recipient=intended,
                draft_path=str(draft),
                reason="SMTP credentials not configured for user",
            )

        # Dispatch via SMTP with retry for transient network/DNS glitches
        max_retries = 3
        last_exc: Exception | None = None
        for attempt in range(1, max_retries + 1):
            try:
                with smtplib.SMTP(smtp_host, smtp_port, timeout=25) as server:
                    server.starttls()
                    server.login(smtp_username, smtp_password)
                    server.send_message(msg)

                # Audit copy in outbox
                self.save_eml(msg, prefix="sent_redirected" if redirected else "sent_live")
                msg_id = uuid.uuid4().hex[:12]

                # Update application metadata if provided
                if application:
                    now_utc = dt.datetime.now(dt.timezone.utc)
                    setattr(application, "followup_due_at", now_utc + dt.timedelta(days=3))

                # Record immutable audit activity event
                if db_session is not None and getattr(user, "id", None):
                    try:
                        from app.models import EmailActivity
                        act = EmailActivity(
                            user_id=user.id,
                            application_id=getattr(application, "id", None),
                            event_type="sent",
                            recipient=recipient,
                            subject=str(msg["Subject"]),
                            details_json='{"provider": "smtp", "message_id": "' + str(msg_id) + '"}',
                        )
                        db_session.add(act)
                        db_session.commit()

                        try:
                            from radar.supabase_client import SupabaseClient
                            sb = SupabaseClient()
                            if sb.is_configured:
                                sb.insert_email_activity({
                                    "user_id": user.id,
                                    "application_id": getattr(application, "id", None),
                                    "event_type": "sent",
                                    "recipient": recipient,
                                    "subject": str(msg["Subject"]),
                                    "details_json": act.details_json,
                                })
                        except Exception:
                            pass
                    except Exception as act_exc:
                        logger.debug("Failed to record EmailActivity: %s", act_exc)

                return OutboundResult(
                    disposition="redirected" if redirected else "sent",
                    recipient=recipient,
                    original_recipient=intended,
                    message_id=msg_id,
                )
            except Exception as exc:
                last_exc = exc
                logger.warning(f"SMTP dispatch attempt {attempt}/{max_retries} failed: {exc}")
                if attempt < max_retries:
                    import time
                    time.sleep(1.2)

        logger.error(f"SMTP dispatch permanently failed: {last_exc}")
        draft = self.save_eml(msg, prefix="failed_smtp")
        return OutboundResult(
            disposition="failed",
            recipient=recipient,
            original_recipient=intended,
            draft_path=str(draft),
            reason=f"SMTP transmission error: {last_exc}",
        )


def compose_followup_message(
    candidate_name: str,
    job_title: str,
    company: str,
    followup_num: int,
    original_subject: str,
    was_opened: bool = False,
) -> tuple[str, str]:
    """Compose structured, professional follow-up subject and body text."""
    clean_subj = original_subject.strip()
    if not clean_subj.lower().startswith("re:"):
        subject = f"Re: {clean_subj}"
    else:
        subject = clean_subj

    role_str = job_title.strip() or "the role"
    comp_str = f" at {company.strip()}" if company else ""
    cand_str = candidate_name.strip() or "Applicant"

    if followup_num == 1:
        if was_opened:
            body = (
                f"Hi Hiring Team,\n\n"
                f"I wanted to briefly follow up on my recent application for the {role_str} position{comp_str}.\n\n"
                f"I remain very enthusiastic about the opportunity to contribute to your engineering goals and bring my background in high-impact software systems to the team.\n\n"
                f"Please let me know if you need any additional project portfolio details or references. I look forward to the possibility of connecting!\n\n"
                f"Best regards,\n"
                f"{cand_str}"
            )
        else:
            body = (
                f"Hi Hiring Team,\n\n"
                f"I am writing to briefly check in regarding my recent application for the {role_str} position{comp_str}.\n\n"
                f"I am very keen on this opportunity and wanted to ensure my application and resume were successfully received.\n\n"
                f"Thank you for your time and consideration, and I look forward to hearing from you.\n\n"
                f"Best regards,\n"
                f"{cand_str}"
            )
    else:  # followup_num == 2 (Final follow-up, only sent if opened)
        body = (
            f"Hi Hiring Team,\n\n"
            f"I am reaching out with one final brief note regarding the {role_str} opening{comp_str}.\n\n"
            f"I understand your review schedule is likely very busy. Should this role still be actively open, I would welcome the opportunity to discuss how my skill set aligns with your upcoming engineering milestones.\n\n"
            f"If you have already moved forward with other candidates, I completely understand and appreciate your consideration.\n\n"
            f"Thank you once again for your time!\n\n"
            f"Best regards,\n"
            f"{cand_str}"
        )

    return subject, body


def dispatch_followup_for_application(
    application_id: int,
    user_id: int,
    session,
) -> dict:
    """Validate follow-up policy rules, compose message, and dispatch via Gmail/SMTP."""
    import json
    from app.models import Application, EmailActivity, User

    app_row = session.get(Application, application_id)
    if not app_row:
        return {"success": False, "error": "Application not found."}
    if app_row.user_id != user_id:
        return {"success": False, "error": "Unauthorized access to application."}
    if app_row.status != "sent":
        return {"success": False, "error": f"Cannot send follow-up for application in '{app_row.status}' status."}

    # Condition: If already replied
    if app_row.replied_at:
        app_row.followup_due_at = None
        session.commit()
        return {"success": False, "error": "Recruiter has already replied to this conversation. Follow-up stopped."}

    # Condition: If bounced
    if app_row.bounced_at:
        app_row.followup_due_at = None
        session.commit()
        return {"success": False, "error": "Original email bounced. Follow-up stopped."}

    current_count = int(app_row.followup_count or 0)
    was_opened = bool(app_row.opened_at or (app_row.open_count and app_row.open_count > 0))

    # Rule 1: Max 2 follow-ups total
    if current_count >= 2:
        app_row.followup_due_at = None
        session.commit()
        return {"success": False, "error": "Maximum follow-up limit (2 emails) reached. Sequence is closed."}

    # Rule 2: If unopened, max 1 follow-up
    if not was_opened and current_count >= 1:
        app_row.followup_due_at = None
        session.commit()
        return {"success": False, "error": "Unopened email limit (1 follow-up) reached. Sequence is closed."}

    user = session.get(User, user_id)
    if not user:
        return {"success": False, "error": "User account not found."}

    cand_name = user.sender_name or user.name or "Applicant"
    next_followup_num = current_count + 1
    subject, body = compose_followup_message(
        candidate_name=cand_name,
        job_title=app_row.job_title or "",
        company=app_row.company or "",
        followup_num=next_followup_num,
        original_subject=app_row.subject or f"Application for {app_row.job_title}",
        was_opened=was_opened,
    )

    guard = OutboundEmailGuard()
    res = guard.send(
        intended_recipient=app_row.email,
        subject=subject,
        body=body,
        resume_pdf_path=app_row.resume_path or None,
        application=app_row,
        user=user,
        db_session=session,
    )

    if res.success:
        now_utc = dt.datetime.now(dt.timezone.utc)
        app_row.followup_count = next_followup_num
        app_row.followup_sent_at = now_utc

        # Schedule next follow-up if opened and count == 1
        if was_opened and next_followup_num == 1:
            app_row.followup_due_at = now_utc + dt.timedelta(days=3)
        else:
            app_row.followup_due_at = None

        # Log EmailActivity
        act = EmailActivity(
            user_id=user.id,
            application_id=app_row.id,
            event_type="followup_sent",
            recipient=app_row.email,
            subject=subject,
            details_json=json.dumps({
                "followup_number": next_followup_num,
                "was_opened": was_opened,
                "next_due": app_row.followup_due_at.isoformat() if app_row.followup_due_at else None,
            }),
        )
        session.add(act)
        session.commit()

        try:
            from app.main import sync_app_to_supabase
            sync_app_to_supabase(app_row, session)
        except Exception:
            pass

        return {
            "success": True,
            "followup_count": app_row.followup_count,
            "followup_sent_at": app_row.followup_sent_at.isoformat(),
            "next_due": app_row.followup_due_at.isoformat() if app_row.followup_due_at else None,
            "message": f"Follow-up #{next_followup_num} sent successfully.",
        }
    else:
        return {
            "success": False,
            "error": res.reason or "Failed to transmit follow-up email.",
        }

