"""Email to and from banks. Outbound mail goes to the in-app outbox or SMTP;
inbound replies arrive by webhook, the in-app simulator, or a test."""

from __future__ import annotations

import asyncio
import logging
import smtplib
from email.message import EmailMessage
from typing import TYPE_CHECKING, Optional

from sqlalchemy import select

from ..db import Email, Step

if TYPE_CHECKING:  # pragma: no cover
    from ..services import Services

log = logging.getLogger("vouch.mail")


class Mailer:
    def __init__(self, services: "Services") -> None:
        self.s = services

    async def send(self, application_id: str, reference_id: Optional[str], to: Optional[str], subject: str, body: str) -> str:
        settings = self.s.settings
        delivery = "outbox"
        if settings.email_mode == "smtp" and settings.smtp_host and to:
            await asyncio.to_thread(self._smtp_send, to, subject, body)
            delivery = "smtp"
        with self.s.db.session() as s:
            email = Email(
                application_id=application_id,
                reference_id=reference_id,
                direction="outbound",
                to_addr=to,
                from_addr=settings.smtp_from or settings.seller_contact_email,
                subject=subject,
                body=body,
                delivery=delivery,
            )
            s.add(email)
            s.flush()
            email_id = email.id
        self.s.bus.publish("email.sent", f"Emailed {to or 'the bank'}: {subject}", application_id=application_id,
                           data={"email_id": email_id, "reference_id": reference_id, "delivery": delivery})
        return email_id

    def _smtp_send(self, to: str, subject: str, body: str) -> None:
        settings = self.s.settings
        msg = EmailMessage()
        msg["From"] = settings.smtp_from or settings.seller_contact_email
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        with smtplib.SMTP(settings.smtp_host or "", settings.smtp_port, timeout=20) as smtp:
            smtp.starttls()
            if settings.smtp_username:
                smtp.login(settings.smtp_username, settings.smtp_password or "")
            smtp.send_message(msg)

    def receive(
        self,
        application_id: str,
        from_addr: Optional[str],
        subject: str,
        body: str,
        delivery: str,
        reference_id: Optional[str] = None,
    ) -> str:
        with self.s.db.session() as s:
            email = Email(
                application_id=application_id,
                reference_id=reference_id,
                direction="inbound",
                from_addr=from_addr,
                to_addr=self.s.settings.seller_contact_email,
                subject=subject,
                body=body,
                delivery=delivery,
            )
            s.add(email)
            s.flush()
            email_id = email.id
            step = s.scalars(select(Step).where(Step.application_id == application_id, Step.kind == "bank")).first()
            step_id = step.id if step else None
        self.s.bus.publish("email.received", f"Reply from {from_addr or 'the bank'}", application_id=application_id,
                           data={"email_id": email_id, "reference_id": reference_id, "delivery": delivery})
        if step_id:
            self.s.engine.signal(step_id, {"email_id": email_id})
        return email_id
