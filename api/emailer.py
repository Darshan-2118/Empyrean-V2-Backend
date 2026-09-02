"""
Shared, fail-soft SMTP email sending.

Both the Celery alert task (``tasks/alerts.py``) and the password-reset flow
(``api/auth.py``) send email. This module centralises the transport so one
SMTP connection carries a batch and every failure is logged and swallowed —
email must never fail a Celery task or an HTTP request.

The send helper is synchronous so Callers on an event loop dispatch it through
``asyncio.to_thread`` (the SMTP handshake is blocking and can take seconds).
When SMTP is not configured the module is a silent no-op (fail-soft), exactly
mirroring the pre-existing alert-email behaviour.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from typing import Iterable

from config import get_config

logger = logging.getLogger("empyrean.email")

# Optional static type for a single outgoing message's content.
EmailSpec = tuple[str, str, str]  # (subject, body, recipient)


def _smtp_settings():
    """Pull SMTP transport settings from config (resolved fresh per call)."""
    cfg = get_config()
    return (
        cfg.SMTP_HOST,
        cfg.SMTP_PORT,
        cfg.SMTP_USERNAME,
        cfg.SMTP_PASSWORD,
        cfg.SMTP_FROM,
        cfg.SMTP_USE_TLS,
    )


def send_emails(messages: Iterable[EmailSpec]) -> int:
    """Send each ``(subject, body, recipient)`` over ONE SMTP connection.

    Returns the number of messages delivered. Fail-soft: unconfigured SMTP,
    an authentication/network error, or a per-message failure is logged and
    skipped — this never raises, so a caller's transaction is never rolled
    back or an HTTP request failed because of email.
    """
    host, port, user, password, sender, use_tls = _smtp_settings()
    list_messages = list(messages)

    if not host:
        logger.debug(
            "SMTP_HOST not configured — skipping %d email(s)", len(list_messages)
        )
        return 0
    if not list_messages:
        return 0

    try:
        with smtplib.SMTP(host, port, timeout=10) as smtp:
            if use_tls:
                smtp.starttls()
            if user:
                smtp.login(user, password)
            sent = 0
            for subject, body, recipient in list_messages:
                msg = EmailMessage()
                msg["Subject"] = subject
                msg["From"] = sender
                msg["To"] = recipient
                msg.set_content(body)
                try:
                    smtp.send_message(msg)
                    sent += 1
                except Exception:  # noqa: BLE001 — one bad message skips, not the batch
                    logger.warning(
                        "Failed to send email to %s — skipped", recipient
                    )
        logger.info("Sent %d/%d email(s)", sent, len(list_messages))
        return sent
    except Exception:  # noqa: BLE001 — email is fail-soft, never raises
        logger.warning(
            "Failed to send email batch (%d message(s)) — skipped", len(list_messages)
        )
        return 0
