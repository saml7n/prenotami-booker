"""Tests for notifications module."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from prenotami_booker.config import NotificationConfig
from prenotami_booker.notifications import (
    _send_email,
    send_failure_notification,
    send_success_notification,
)


class TestSendSuccessNotification:
    def test_sends_with_correct_subject(self, notification_config: NotificationConfig) -> None:
        with patch("prenotami_booker.notifications._send_email", return_value=True) as mock_send:
            result = send_success_notification(
                notification_config,
                correlation_id="test",
                appointment_date="2025-07-15",
                appointment_time="10:00",
                consulate="London",
            )

        assert result is True
        call_kwargs = mock_send.call_args
        assert "BOOKED" in call_kwargs.kwargs["subject"]
        assert "London" in call_kwargs.kwargs["subject"]

    def test_body_contains_appointment_details(
        self, notification_config: NotificationConfig
    ) -> None:
        with patch("prenotami_booker.notifications._send_email", return_value=True) as mock_send:
            send_success_notification(
                notification_config,
                correlation_id="test",
                appointment_date="2025-07-15",
                appointment_time="10:00",
                consulate="London",
            )

        body = mock_send.call_args.kwargs["body"]
        assert "2025-07-15" in body
        assert "10:00" in body
        assert "Jure Sanguinis" in body


class TestSendFailureNotification:
    def test_sends_with_correct_subject(self, notification_config: NotificationConfig) -> None:
        with patch("prenotami_booker.notifications._send_email", return_value=True) as mock_send:
            result = send_failure_notification(
                notification_config,
                correlation_id="test",
                reason="No slots available",
                consulate="London",
            )

        assert result is True
        assert "FAILED" in mock_send.call_args.kwargs["subject"]

    def test_body_contains_reason(self, notification_config: NotificationConfig) -> None:
        with patch("prenotami_booker.notifications._send_email", return_value=True) as mock_send:
            send_failure_notification(
                notification_config,
                correlation_id="test",
                reason="OTP timeout",
                consulate="London",
            )

        body = mock_send.call_args.kwargs["body"]
        assert "OTP timeout" in body


class TestSendEmail:
    def test_skips_when_no_smtp_config(self) -> None:
        """No SMTP server configured → skip silently."""
        config = NotificationConfig()
        result = _send_email(
            config, subject="Test", body="Test body", correlation_id="test"
        )
        assert result is False

    def test_skips_when_no_notify_email(self) -> None:
        config = NotificationConfig(smtp_server="smtp.gmail.com")
        result = _send_email(
            config, subject="Test", body="Test body", correlation_id="test"
        )
        assert result is False

    @patch("prenotami_booker.notifications.smtplib.SMTP")
    def test_sends_via_tls(self, mock_smtp_class: MagicMock) -> None:
        config = NotificationConfig(
            smtp_server="smtp.gmail.com",
            smtp_port=587,
            smtp_email="sender@example.com",
            smtp_password="pass",
            notify_email="recipient@example.com",
            use_tls=True,
        )
        mock_server = MagicMock()
        mock_smtp_class.return_value = mock_server

        result = _send_email(
            config, subject="Test", body="Body", correlation_id="test"
        )

        assert result is True
        mock_smtp_class.assert_called_once_with("smtp.gmail.com", 587)
        mock_server.starttls.assert_called_once()
        mock_server.login.assert_called_once_with("sender@example.com", "pass")
        mock_server.send_message.assert_called_once()
        mock_server.quit.assert_called_once()

    @patch("prenotami_booker.notifications.smtplib.SMTP_SSL")
    def test_sends_via_ssl_when_tls_disabled(self, mock_smtp_ssl: MagicMock) -> None:
        config = NotificationConfig(
            smtp_server="smtp.gmail.com",
            smtp_port=465,
            smtp_email="sender@example.com",
            smtp_password="pass",
            notify_email="recipient@example.com",
            use_tls=False,
        )
        mock_server = MagicMock()
        mock_smtp_ssl.return_value = mock_server

        result = _send_email(
            config, subject="Test", body="Body", correlation_id="test"
        )

        assert result is True
        mock_smtp_ssl.assert_called_once_with("smtp.gmail.com", 465)

    @patch("prenotami_booker.notifications.smtplib.SMTP")
    def test_returns_false_on_smtp_error(self, mock_smtp_class: MagicMock) -> None:
        """Notification failures should return False, not raise."""
        import smtplib

        config = NotificationConfig(
            smtp_server="smtp.gmail.com",
            smtp_port=587,
            smtp_email="sender@example.com",
            smtp_password="pass",
            notify_email="recipient@example.com",
            use_tls=True,
        )
        mock_smtp_class.side_effect = smtplib.SMTPException("auth failed")

        result = _send_email(
            config, subject="Test", body="Body", correlation_id="test"
        )

        assert result is False

    @patch("prenotami_booker.notifications.smtplib.SMTP")
    def test_returns_false_on_connection_error(self, mock_smtp_class: MagicMock) -> None:
        config = NotificationConfig(
            smtp_server="smtp.gmail.com",
            smtp_port=587,
            smtp_email="s@e.com",
            smtp_password="p",
            notify_email="r@e.com",
            use_tls=True,
        )
        mock_smtp_class.side_effect = ConnectionError("refused")

        result = _send_email(
            config, subject="Test", body="Body", correlation_id="test"
        )
        assert result is False
