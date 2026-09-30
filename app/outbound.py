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
    ) -> EmailMessage:
        import re
        from app.knowledge import load_candidate

        candidate = load_candidate(user)
        msg = EmailMessage()

        # From header
        sender_name = getattr(user, "sender_name", "") or candidate.name or "Applicant"
        sender_email = getattr(user, "smtp_username", "") or getattr(user, "email", "") or config.SMTP_USERNAME or candidate.email or "mirzahaseeb0566@gmail.com"
        msg["From"] = f"{sender_name} <{sender_email}>"
        msg["To"] = recipient

        # Subject line
        final_subject = f"[TEST MODE] {subject}" if is_redirected and not subject.startswith("[TEST MODE]") else subject
        msg["Subject"] = final_subject

        if is_redirected and intended_recipient:
            msg["X-CareerPulse-Intended-To"] = intended_recipient

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
    ) -> OutboundResult:
        """Dispatch application email or safely redirect/draft."""
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

        if config.TEST_MODE and not recipient:
            msg = self.compose_message(intended, subject, body, resume_pdf_path, user=user)
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
                msg = self.compose_message(intended, subject, body, resume_pdf_path, user=user)
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
        )

        # Refresh config dynamically from .env
        config.reload_config()

        # Check SMTP credentials (user credentials first, fallback to config)
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
