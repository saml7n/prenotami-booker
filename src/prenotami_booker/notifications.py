"""Notification system for booking results."""

from __future__ import annotations

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import structlog

from prenotami_booker.config import NotificationConfig

logger = structlog.get_logger()


def send_success_notification(
    config: NotificationConfig,
    *,
    correlation_id: str,
    appointment_date: str,
    appointment_time: str,
    consulate: str,
) -> bool:
    """Send email notification for a successful booking.

    Args:
        config: Notification configuration with SMTP settings.
        correlation_id: Correlation ID for logging.
        appointment_date: The booked appointment date.
        appointment_time: The booked appointment time.
        consulate: Name of the consulate.

    Returns:
        True if notification sent successfully, False otherwise.
    """
    subject = f"Prenot@mi: Appointment BOOKED at {consulate} Consulate!"
    body = (
        f"Your appointment has been successfully booked.\n\n"
        f"Consulate: Italian Consulate {consulate}\n"
        f"Date: {appointment_date}\n"
        f"Time: {appointment_time}\n"
        f"Service: Citizenship by Descent (Jure Sanguinis)\n\n"
        f"IMPORTANT: You must confirm the appointment on the Prenot@mi system "
        f"within 3 days before the actual appointment date, or it will be "
        f"automatically cancelled.\n\n"
        f"Log in to https://prenotami.esteri.it to view your appointment details.\n\n"
        f"Correlation ID: {correlation_id}"
    )

    return _send_email(config, subject=subject, body=body, correlation_id=correlation_id)


def send_failure_notification(
    config: NotificationConfig,
    *,
    correlation_id: str,
    reason: str,
    consulate: str,
) -> bool:
    """Send email notification for a failed booking attempt.

    Args:
        config: Notification configuration with SMTP settings.
        correlation_id: Correlation ID for logging.
        reason: Description of why the booking failed.
        consulate: Name of the consulate.

    Returns:
        True if notification sent successfully, False otherwise.
    """
    subject = f"Prenot@mi: Booking attempt FAILED at {consulate} Consulate"
    body = (
        f"The automated booking attempt was unsuccessful.\n\n"
        f"Consulate: Italian Consulate {consulate}\n"
        f"Reason: {reason}\n\n"
        f"The bot will try again at the next release window.\n\n"
        f"Correlation ID: {correlation_id}"
    )

    return _send_email(config, subject=subject, body=body, correlation_id=correlation_id)


def _send_email(
    config: NotificationConfig,
    *,
    subject: str,
    body: str,
    correlation_id: str,
) -> bool:
    """Send an email notification via SMTP.

    Args:
        config: SMTP configuration.
        subject: Email subject line.
        body: Email body text.
        correlation_id: Correlation ID for logging.

    Returns:
        True if sent successfully, False otherwise.
    """
    if not config.smtp_server or not config.notify_email:
        logger.warning(
            "notification_skipped_no_config",
            correlation_id=correlation_id,
        )
        return False

    msg = MIMEMultipart()
    msg["From"] = config.smtp_email
    msg["To"] = config.notify_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    try:
        if config.use_tls:
            server = smtplib.SMTP(config.smtp_server, config.smtp_port)
            server.starttls()
        else:
            server = smtplib.SMTP_SSL(config.smtp_server, config.smtp_port)

        server.login(config.smtp_email, config.smtp_password)
        server.send_message(msg)
        server.quit()

        logger.info(
            "notification_sent",
            correlation_id=correlation_id,
            recipient=config.notify_email,
            subject=subject,
        )
        return True

    except (smtplib.SMTPException, ConnectionError, OSError) as exc:
        logger.error(
            "notification_failed",
            correlation_id=correlation_id,
            error=str(type(exc).__name__),
        )
        return False
