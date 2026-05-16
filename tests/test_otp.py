"""Tests for OTP email retrieval module."""

from __future__ import annotations

import email
from email.mime.text import MIMEText
from unittest.mock import MagicMock, patch

import pytest

from prenotami_booker.config import EmailConfig
from prenotami_booker.otp import (
    MAX_POLL_ATTEMPTS,
    OTP_PATTERN,
    POLL_INTERVAL_SECONDS,
    PRENOTAMI_SENDER,
    _check_inbox_for_otp,
    _decode_email_header,
    _get_email_body,
    fetch_otp_from_email,
)


class TestOTPPattern:
    """Test the OTP regex pattern used to extract codes."""

    def test_matches_six_digit_code(self) -> None:
        assert OTP_PATTERN.search("Your code is 123456")

    def test_extracts_correct_code(self) -> None:
        match = OTP_PATTERN.search("Your OTP code is 789012. Please use it within 5 minutes.")
        assert match is not None
        assert match.group(1) == "789012"

    def test_no_match_on_shorter_numbers(self) -> None:
        """5-digit numbers shouldn't match as word boundary prevents partial match."""
        text = "Code 12345 is invalid"
        match = OTP_PATTERN.search(text)
        assert match is None or len(match.group(1)) == 6

    def test_no_match_on_longer_numbers(self) -> None:
        """7+ digit numbers shouldn't match the 6-digit pattern."""
        text = "Code 1234567 is too long"
        match = OTP_PATTERN.search(text)
        # \b(\d{6})\b won't match inside 1234567 due to word boundaries
        assert match is None

    def test_matches_first_six_digit_in_email_body(self) -> None:
        """The OTP is typically the first 6-digit number in the email."""
        body = "Il codice OTP è: 456789\nRef: 111222"
        match = OTP_PATTERN.search(body)
        assert match is not None
        assert match.group(1) == "456789"

    def test_known_weakness_matches_any_six_digit(self) -> None:
        """OTP_PATTERN is broad: any 6-digit number matches.

        This is a known weakness - if the email body contains other
        6-digit numbers before the OTP, the wrong number could be extracted.
        """
        body = "Reference 100000. Your OTP is 567890."
        match = OTP_PATTERN.search(body)
        assert match is not None
        # It matches the FIRST 6-digit number, which is the reference, not the OTP
        assert match.group(1) == "100000"


class TestPrenotamiSender:
    def test_sender_string(self) -> None:
        assert PRENOTAMI_SENDER == "prenotami"


class TestPollConstants:
    def test_max_poll_attempts(self) -> None:
        assert MAX_POLL_ATTEMPTS == 30

    def test_poll_interval(self) -> None:
        assert POLL_INTERVAL_SECONDS == 2

    def test_total_wait_time(self) -> None:
        """Total OTP polling timeout: 30 * 2 = 60 seconds.

        Per the Reddit guide, OTP typically arrives in 10-15 seconds
        but can take up to 1 minute. 60 seconds is the edge case limit.
        """
        total = MAX_POLL_ATTEMPTS * POLL_INTERVAL_SECONDS
        assert total == 60


class TestFetchOtpFromEmail:
    @patch("prenotami_booker.otp._check_inbox_for_otp")
    @patch("prenotami_booker.otp.time.sleep")
    def test_returns_otp_on_first_attempt(
        self, mock_sleep: MagicMock, mock_check: MagicMock, email_config: EmailConfig
    ) -> None:
        mock_check.return_value = "123456"

        result = fetch_otp_from_email(email_config, correlation_id="test")

        assert result == "123456"
        mock_sleep.assert_not_called()

    @patch("prenotami_booker.otp._check_inbox_for_otp")
    @patch("prenotami_booker.otp.time.sleep")
    def test_retries_until_found(
        self, mock_sleep: MagicMock, mock_check: MagicMock, email_config: EmailConfig
    ) -> None:
        mock_check.side_effect = [None, None, None, "654321"]

        result = fetch_otp_from_email(email_config, correlation_id="test")

        assert result == "654321"
        assert mock_check.call_count == 4
        assert mock_sleep.call_count == 3

    @patch("prenotami_booker.otp._check_inbox_for_otp")
    @patch("prenotami_booker.otp.time.sleep")
    def test_returns_none_after_max_attempts(
        self, mock_sleep: MagicMock, mock_check: MagicMock, email_config: EmailConfig
    ) -> None:
        mock_check.return_value = None

        result = fetch_otp_from_email(email_config, correlation_id="test")

        assert result is None
        assert mock_check.call_count == MAX_POLL_ATTEMPTS

    @patch("prenotami_booker.otp._check_inbox_for_otp")
    @patch("prenotami_booker.otp.time.sleep")
    def test_handles_imap_errors_gracefully(
        self, mock_sleep: MagicMock, mock_check: MagicMock, email_config: EmailConfig
    ) -> None:
        """IMAP errors should be caught and retried, not crash."""
        import imaplib

        mock_check.side_effect = [
            imaplib.IMAP4.error("connection lost"),
            ConnectionError("timeout"),
            "999888",
        ]

        result = fetch_otp_from_email(email_config, correlation_id="test")

        assert result == "999888"
        assert mock_check.call_count == 3


class TestCheckInboxForOtp:
    def _make_email_message(
        self,
        from_addr: str = "noreply@prenotami.esteri.it",
        subject: str = "Codice OTP",
        body: str = "Il tuo codice OTP è: 123456",
    ) -> bytes:
        msg = MIMEText(body, "plain")
        msg["From"] = from_addr
        msg["Subject"] = subject
        return msg.as_bytes()

    @patch("prenotami_booker.otp.imaplib")
    def test_finds_otp_from_prenotami_email(
        self, mock_imaplib: MagicMock, email_config: EmailConfig
    ) -> None:
        mock_mail = MagicMock()
        mock_imaplib.IMAP4_SSL.return_value = mock_mail

        mock_mail.search.return_value = ("OK", [b"1"])
        raw_email = self._make_email_message()
        mock_mail.fetch.return_value = ("OK", [(b"1", raw_email)])

        result = _check_inbox_for_otp(email_config, correlation_id="test")

        assert result == "123456"
        mock_mail.login.assert_called_once_with(email_config.email, email_config.password)
        mock_mail.select.assert_called_once_with("INBOX")

    @patch("prenotami_booker.otp.imaplib")
    def test_returns_none_when_no_unread(
        self, mock_imaplib: MagicMock, email_config: EmailConfig
    ) -> None:
        mock_mail = MagicMock()
        mock_imaplib.IMAP4_SSL.return_value = mock_mail
        mock_mail.search.return_value = ("OK", [b""])

        result = _check_inbox_for_otp(email_config, correlation_id="test")
        assert result is None

    @patch("prenotami_booker.otp.imaplib")
    def test_skips_non_prenotami_emails(
        self, mock_imaplib: MagicMock, email_config: EmailConfig
    ) -> None:
        mock_mail = MagicMock()
        mock_imaplib.IMAP4_SSL.return_value = mock_mail

        mock_mail.search.return_value = ("OK", [b"1"])
        raw_email = self._make_email_message(
            from_addr="newsletter@random.com",
            subject="Weekly digest",
            body="Your verification code is 111222",
        )
        mock_mail.fetch.return_value = ("OK", [(b"1", raw_email)])

        result = _check_inbox_for_otp(email_config, correlation_id="test")
        assert result is None

    @patch("prenotami_booker.otp.imaplib")
    def test_uses_non_ssl_when_configured(
        self, mock_imaplib: MagicMock
    ) -> None:
        config = EmailConfig(
            imap_server="imap.example.com",
            imap_port=143,
            email="a@b.com",
            password="pw",
            use_ssl=False,
        )

        mock_mail = MagicMock()
        mock_imaplib.IMAP4.return_value = mock_mail
        mock_mail.search.return_value = ("OK", [b""])

        _check_inbox_for_otp(config, correlation_id="test")
        mock_imaplib.IMAP4.assert_called_once_with("imap.example.com", 143)

    @patch("prenotami_booker.otp.imaplib")
    def test_always_logs_out(
        self, mock_imaplib: MagicMock, email_config: EmailConfig
    ) -> None:
        """IMAP connection must be closed even on error."""
        mock_mail = MagicMock()
        mock_imaplib.IMAP4_SSL.return_value = mock_mail
        mock_mail.search.side_effect = Exception("search failed")

        with pytest.raises(Exception, match="search failed"):
            _check_inbox_for_otp(email_config, correlation_id="test")

        mock_mail.logout.assert_called_once()

    @patch("prenotami_booker.otp.imaplib")
    def test_detects_otp_in_subject(
        self, mock_imaplib: MagicMock, email_config: EmailConfig
    ) -> None:
        """Email should be recognized by 'otp' or 'codice' in subject."""
        mock_mail = MagicMock()
        mock_imaplib.IMAP4_SSL.return_value = mock_mail

        mock_mail.search.return_value = ("OK", [b"1"])
        raw_email = self._make_email_message(
            from_addr="noreply@esteri.it",  # No 'prenotami' in from
            subject="Codice di verifica OTP",
            body="654321",
        )
        mock_mail.fetch.return_value = ("OK", [(b"1", raw_email)])

        result = _check_inbox_for_otp(email_config, correlation_id="test")
        assert result == "654321"

    @patch("prenotami_booker.gmail_oauth.get_gmail_credentials")
    @patch("prenotami_booker.gmail_oauth.build_xoauth2_string", return_value="base64auth")
    @patch("prenotami_booker.otp.imaplib")
    def test_uses_oauth2_when_configured(
        self, mock_imaplib: MagicMock, mock_build: MagicMock, mock_get_creds: MagicMock
    ) -> None:
        config = EmailConfig(
            imap_server="imap.gmail.com",
            imap_port=993,
            email="a@gmail.com",
            use_oauth=True,
        )

        mock_creds = MagicMock()
        mock_creds.expired = False
        mock_creds.token = "access-tok"
        mock_get_creds.return_value = mock_creds

        mock_mail = MagicMock()
        mock_imaplib.IMAP4_SSL.return_value = mock_mail
        mock_mail.search.return_value = ("OK", [b""])

        _check_inbox_for_otp(config, correlation_id="test")

        mock_mail.authenticate.assert_called_once()
        mock_mail.login.assert_not_called()


class TestDecodeEmailHeader:
    def test_plain_string(self) -> None:
        assert _decode_email_header("Hello World") == "Hello World"

    def test_none_returns_empty(self) -> None:
        assert _decode_email_header(None) == ""

    def test_empty_returns_empty(self) -> None:
        assert _decode_email_header("") == ""


class TestGetEmailBody:
    def test_plain_text_email(self) -> None:
        msg = MIMEText("Test body content", "plain")
        parsed = email.message_from_bytes(msg.as_bytes())
        result = _get_email_body(parsed)
        assert "Test body content" in result

    def test_empty_email(self) -> None:
        msg = email.message_from_string("")
        result = _get_email_body(msg)
        assert result == ""
