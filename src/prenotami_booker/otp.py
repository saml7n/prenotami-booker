"""OTP code retrieval from email via IMAP."""

from __future__ import annotations

import email
import imaplib
import re
import time
from email.header import decode_header

import structlog

from prenotami_booker.config import EmailConfig

logger = structlog.get_logger()

OTP_PATTERN = re.compile(r"\b(\d{6})\b")
PRENOTAMI_SENDER = "prenotami"
MAX_POLL_ATTEMPTS = 30
POLL_INTERVAL_SECONDS = 2


def fetch_otp_from_email(
    config: EmailConfig,
    *,
    correlation_id: str,
    max_age_seconds: int = 300,
) -> str | None:
    """Poll IMAP inbox for the latest Prenot@mi OTP code.

    Connects to the configured IMAP server and searches for recent emails
    from Prenot@mi containing a 6-digit OTP code.

    Args:
        config: Email/IMAP configuration.
        correlation_id: Correlation ID for structured logging.
        max_age_seconds: Maximum age of email to consider (default 5 minutes).

    Returns:
        The 6-digit OTP code string, or None if not found within timeout.
    """
    logger.info(
        "otp_fetch_started",
        correlation_id=correlation_id,
        imap_server=config.imap_server,
    )

    for attempt in range(1, MAX_POLL_ATTEMPTS + 1):
        try:
            otp_code = _check_inbox_for_otp(config, correlation_id=correlation_id)
            if otp_code:
                logger.info(
                    "otp_found",
                    correlation_id=correlation_id,
                    attempt=attempt,
                )
                return otp_code
        except (imaplib.IMAP4.error, ConnectionError, OSError) as exc:
            logger.warning(
                "otp_imap_error",
                correlation_id=correlation_id,
                attempt=attempt,
                error=str(type(exc).__name__),
            )

        if attempt < MAX_POLL_ATTEMPTS:
            time.sleep(POLL_INTERVAL_SECONDS)

    logger.error(
        "otp_not_found",
        correlation_id=correlation_id,
        max_attempts=MAX_POLL_ATTEMPTS,
    )
    return None


def _check_inbox_for_otp(
    config: EmailConfig,
    *,
    correlation_id: str,
) -> str | None:
    """Check IMAP inbox for OTP email from Prenot@mi.

    Args:
        config: Email/IMAP configuration.
        correlation_id: Correlation ID for logging.

    Returns:
        OTP code if found, None otherwise.
    """
    if config.use_ssl:
        mail = imaplib.IMAP4_SSL(config.imap_server, config.imap_port)
    else:
        mail = imaplib.IMAP4(config.imap_server, config.imap_port)

    try:
        mail.login(config.email, config.password)
        mail.select("INBOX")

        # Search for recent unread emails
        status, message_ids = mail.search(None, "(UNSEEN)")
        if status != "OK" or not message_ids[0]:
            return None

        # Check most recent emails first
        ids = message_ids[0].split()
        for msg_id in reversed(ids[-20:]):
            status, msg_data = mail.fetch(msg_id, "(RFC822)")
            if status != "OK" or not msg_data[0]:
                continue

            raw_email = msg_data[0]
            if not isinstance(raw_email, tuple) or len(raw_email) < 2:
                continue

            msg = email.message_from_bytes(raw_email[1])

            # Check if from Prenot@mi
            from_header = str(msg.get("From", "")).lower()
            subject_header = _decode_email_header(msg.get("Subject", ""))

            is_prenotami = (
                PRENOTAMI_SENDER in from_header
                or PRENOTAMI_SENDER in subject_header.lower()
                or "otp" in subject_header.lower()
                or "codice" in subject_header.lower()
            )

            if not is_prenotami:
                continue

            # Extract body and find OTP
            body = _get_email_body(msg)
            otp_match = OTP_PATTERN.search(body)
            if otp_match:
                otp_code = otp_match.group(1)
                logger.debug(
                    "otp_extracted_from_email",
                    correlation_id=correlation_id,
                    subject=subject_header[:50],
                )
                return otp_code

        return None
    finally:
        try:
            mail.logout()
        except imaplib.IMAP4.error:
            pass


def _decode_email_header(header: str | None) -> str:
    """Decode an email header value.

    Args:
        header: Raw email header string.

    Returns:
        Decoded header string.
    """
    if not header:
        return ""
    decoded_parts = decode_header(header)
    parts: list[str] = []
    for part, charset in decoded_parts:
        if isinstance(part, bytes):
            parts.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            parts.append(str(part))
    return " ".join(parts)


def _get_email_body(msg: email.message.Message) -> str:
    """Extract plain text body from email message.

    Args:
        msg: Parsed email message.

    Returns:
        Plain text body content.
    """
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type == "text/plain":
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if isinstance(payload, bytes):
            charset = msg.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
    return ""
