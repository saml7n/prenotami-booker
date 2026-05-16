"""Tests for orchestrator module - the main booking flow coordinator."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from prenotami_booker.booker import BookingOutcome, BookingResult
from prenotami_booker.config import AppConfig
from prenotami_booker.orchestrator import (
    MAX_RETRY_ATTEMPTS,
    _fail,
    run_booking_attempt,
    run_scheduled,
)


class TestRunBookingAttempt:
    """Test the 10-step booking orchestration flow.

    Per the Reddit guide, the correct sequence is:
    1. Login (10 min before)
    2. Navigate to Passport page
    3. At T-2min: Click 'Invia Nuovo Codice' for OTP
    4. Retrieve OTP from email
    5. Navigate to services listing
    6. At T-1sec: Click citizenship 'Prenota'
    7. Scroll down, paste OTP, check privacy, click 'Avanti'
    8. Handle confirmation popup
    9. Calendar: navigate forward months, find green date
    10. Click date, click Prenota, get confirmation
    """

    @pytest.fixture(autouse=True)
    def _mock_ntp(self):
        with patch("prenotami_booker.orchestrator.get_ntp_offset", return_value=0.0):
            yield

    @patch("prenotami_booker.orchestrator.close_driver")
    @patch("prenotami_booker.orchestrator.send_success_notification")
    @patch("prenotami_booker.orchestrator.navigate_calendar_and_book")
    @patch("prenotami_booker.orchestrator.submit_otp_and_proceed")
    @patch("prenotami_booker.orchestrator.click_citizenship_booking")
    @patch("prenotami_booker.orchestrator.wait_until")
    @patch("prenotami_booker.orchestrator.navigate_to_services")
    @patch("prenotami_booker.orchestrator.fetch_otp_from_email")
    @patch("prenotami_booker.orchestrator.trigger_otp_via_passport")
    @patch("prenotami_booker.orchestrator.get_next_release_time")
    @patch("prenotami_booker.orchestrator.login")
    @patch("prenotami_booker.orchestrator.create_driver")
    def test_successful_full_flow(
        self,
        mock_create_driver: MagicMock,
        mock_login: MagicMock,
        mock_get_release: MagicMock,
        mock_trigger_otp: MagicMock,
        mock_fetch_otp: MagicMock,
        mock_nav_services: MagicMock,
        mock_wait_until: MagicMock,
        mock_click_citizenship: MagicMock,
        mock_submit_otp: MagicMock,
        mock_calendar: MagicMock,
        mock_notify: MagicMock,
        mock_close: MagicMock,
        app_config: AppConfig,
    ) -> None:
        # Setup
        mock_login.return_value = True
        mock_get_release.return_value = datetime.now(UTC) + timedelta(seconds=5)
        mock_trigger_otp.return_value = True
        mock_fetch_otp.return_value = "123456"
        mock_nav_services.return_value = True
        mock_click_citizenship.return_value = True
        mock_submit_otp.return_value = True
        mock_calendar.return_value = BookingOutcome(
            BookingResult.SUCCESS,
            appointment_date="2025-07-15",
            appointment_time="10:00",
            message="Booked!",
        )

        outcome = run_booking_attempt(app_config, correlation_id="test")

        assert outcome.result == BookingResult.SUCCESS
        assert outcome.appointment_date == "2025-07-15"

        # Verify flow order
        mock_create_driver.assert_called_once()
        mock_login.assert_called_once()
        mock_trigger_otp.assert_called_once()
        mock_fetch_otp.assert_called_once()
        mock_nav_services.assert_called_once()
        mock_click_citizenship.assert_called_once()
        mock_submit_otp.assert_called_once()
        mock_calendar.assert_called_once()
        mock_notify.assert_called_once()
        mock_close.assert_called_once()

    @patch("prenotami_booker.orchestrator.close_driver")
    @patch("prenotami_booker.orchestrator.send_failure_notification")
    @patch("prenotami_booker.orchestrator.login")
    @patch("prenotami_booker.orchestrator.create_driver")
    def test_login_failure(
        self,
        mock_create: MagicMock,
        mock_login: MagicMock,
        mock_notify: MagicMock,
        mock_close: MagicMock,
        app_config: AppConfig,
    ) -> None:
        mock_login.return_value = False

        outcome = run_booking_attempt(app_config, correlation_id="test")

        assert outcome.result == BookingResult.LOGIN_FAILED
        mock_notify.assert_called_once()
        mock_close.assert_called_once()

    @patch("prenotami_booker.orchestrator.close_driver")
    @patch("prenotami_booker.orchestrator.send_failure_notification")
    @patch("prenotami_booker.orchestrator.wait_until")
    @patch("prenotami_booker.orchestrator.get_next_release_time")
    @patch("prenotami_booker.orchestrator.trigger_otp_via_passport")
    @patch("prenotami_booker.orchestrator.login")
    @patch("prenotami_booker.orchestrator.create_driver")
    def test_otp_trigger_retries_once(
        self,
        mock_create: MagicMock,
        mock_login: MagicMock,
        mock_trigger: MagicMock,
        mock_get_release: MagicMock,
        mock_wait: MagicMock,
        mock_notify: MagicMock,
        mock_close: MagicMock,
        app_config: AppConfig,
    ) -> None:
        """If OTP trigger fails, it retries once before giving up."""
        mock_login.return_value = True
        mock_get_release.return_value = datetime.now(UTC) + timedelta(seconds=5)
        mock_trigger.side_effect = [False, True]

        with patch("prenotami_booker.orchestrator.fetch_otp_from_email", return_value="123456"), \
             patch("prenotami_booker.orchestrator.navigate_to_services", return_value=True), \
             patch("prenotami_booker.orchestrator.click_citizenship_booking", return_value=True), \
             patch("prenotami_booker.orchestrator.submit_otp_and_proceed", return_value=True), \
             patch("prenotami_booker.orchestrator.navigate_calendar_and_book", return_value=BookingOutcome(BookingResult.SUCCESS)), \
             patch("prenotami_booker.orchestrator.send_success_notification"), \
             patch("prenotami_booker.orchestrator.time.sleep"):
            outcome = run_booking_attempt(app_config, correlation_id="test")

        assert outcome.result == BookingResult.SUCCESS
        assert mock_trigger.call_count == 2

    @patch("prenotami_booker.orchestrator.close_driver")
    @patch("prenotami_booker.orchestrator.send_failure_notification")
    @patch("prenotami_booker.orchestrator.time.sleep")
    @patch("prenotami_booker.orchestrator.wait_until")
    @patch("prenotami_booker.orchestrator.get_next_release_time")
    @patch("prenotami_booker.orchestrator.trigger_otp_via_passport")
    @patch("prenotami_booker.orchestrator.login")
    @patch("prenotami_booker.orchestrator.create_driver")
    def test_otp_trigger_fails_after_retry(
        self,
        mock_create: MagicMock,
        mock_login: MagicMock,
        mock_trigger: MagicMock,
        mock_get_release: MagicMock,
        mock_wait: MagicMock,
        mock_sleep: MagicMock,
        mock_notify: MagicMock,
        mock_close: MagicMock,
        app_config: AppConfig,
    ) -> None:
        mock_login.return_value = True
        mock_get_release.return_value = datetime.now(UTC) + timedelta(seconds=5)
        mock_trigger.return_value = False

        outcome = run_booking_attempt(app_config, correlation_id="test")

        assert outcome.result == BookingResult.OTP_FAILED
        assert mock_trigger.call_count == 2

    @patch("prenotami_booker.orchestrator.close_driver")
    @patch("prenotami_booker.orchestrator.send_failure_notification")
    @patch("prenotami_booker.orchestrator.fetch_otp_from_email")
    @patch("prenotami_booker.orchestrator.trigger_otp_via_passport")
    @patch("prenotami_booker.orchestrator.wait_until")
    @patch("prenotami_booker.orchestrator.get_next_release_time")
    @patch("prenotami_booker.orchestrator.login")
    @patch("prenotami_booker.orchestrator.create_driver")
    def test_otp_email_retrieval_failure(
        self,
        mock_create: MagicMock,
        mock_login: MagicMock,
        mock_get_release: MagicMock,
        mock_wait: MagicMock,
        mock_trigger: MagicMock,
        mock_fetch: MagicMock,
        mock_notify: MagicMock,
        mock_close: MagicMock,
        app_config: AppConfig,
    ) -> None:
        mock_login.return_value = True
        mock_get_release.return_value = datetime.now(UTC) + timedelta(seconds=5)
        mock_trigger.return_value = True
        mock_fetch.return_value = None

        outcome = run_booking_attempt(app_config, correlation_id="test")

        assert outcome.result == BookingResult.OTP_FAILED
        assert "retrieve OTP" in outcome.message

    @patch("prenotami_booker.orchestrator.close_driver")
    @patch("prenotami_booker.orchestrator.send_failure_notification")
    @patch("prenotami_booker.orchestrator.navigate_to_services")
    @patch("prenotami_booker.orchestrator.fetch_otp_from_email")
    @patch("prenotami_booker.orchestrator.trigger_otp_via_passport")
    @patch("prenotami_booker.orchestrator.wait_until")
    @patch("prenotami_booker.orchestrator.get_next_release_time")
    @patch("prenotami_booker.orchestrator.login")
    @patch("prenotami_booker.orchestrator.create_driver")
    def test_navigate_services_failure(
        self,
        mock_create: MagicMock,
        mock_login: MagicMock,
        mock_get_release: MagicMock,
        mock_wait: MagicMock,
        mock_trigger: MagicMock,
        mock_fetch: MagicMock,
        mock_nav: MagicMock,
        mock_notify: MagicMock,
        mock_close: MagicMock,
        app_config: AppConfig,
    ) -> None:
        mock_login.return_value = True
        mock_get_release.return_value = datetime.now(UTC) + timedelta(seconds=5)
        mock_trigger.return_value = True
        mock_fetch.return_value = "123456"
        mock_nav.return_value = False

        outcome = run_booking_attempt(app_config, correlation_id="test")

        assert outcome.result == BookingResult.ERROR

    @patch("prenotami_booker.orchestrator.close_driver")
    @patch("prenotami_booker.orchestrator.send_failure_notification")
    @patch("prenotami_booker.orchestrator.login")
    @patch("prenotami_booker.orchestrator.create_driver")
    def test_unexpected_error_handled(
        self,
        mock_create: MagicMock,
        mock_login: MagicMock,
        mock_notify: MagicMock,
        mock_close: MagicMock,
        app_config: AppConfig,
    ) -> None:
        mock_login.side_effect = RuntimeError("unexpected")

        outcome = run_booking_attempt(app_config, correlation_id="test")

        assert outcome.result == BookingResult.ERROR
        mock_close.assert_called_once()

    @patch("prenotami_booker.orchestrator.close_driver")
    @patch("prenotami_booker.orchestrator.send_failure_notification")
    @patch("prenotami_booker.orchestrator.send_success_notification")
    @patch("prenotami_booker.orchestrator.navigate_calendar_and_book")
    @patch("prenotami_booker.orchestrator.submit_otp_and_proceed")
    @patch("prenotami_booker.orchestrator.click_citizenship_booking")
    @patch("prenotami_booker.orchestrator.wait_until")
    @patch("prenotami_booker.orchestrator.navigate_to_services")
    @patch("prenotami_booker.orchestrator.fetch_otp_from_email")
    @patch("prenotami_booker.orchestrator.trigger_otp_via_passport")
    @patch("prenotami_booker.orchestrator.get_next_release_time")
    @patch("prenotami_booker.orchestrator.login")
    @patch("prenotami_booker.orchestrator.create_driver")
    def test_no_slots_sends_failure_notification(
        self,
        mock_create: MagicMock,
        mock_login: MagicMock,
        mock_get_release: MagicMock,
        mock_trigger: MagicMock,
        mock_fetch: MagicMock,
        mock_nav: MagicMock,
        mock_wait: MagicMock,
        mock_click: MagicMock,
        mock_submit: MagicMock,
        mock_calendar: MagicMock,
        mock_success_notify: MagicMock,
        mock_fail_notify: MagicMock,
        mock_close: MagicMock,
        app_config: AppConfig,
    ) -> None:
        mock_login.return_value = True
        mock_get_release.return_value = datetime.now(UTC) + timedelta(seconds=5)
        mock_trigger.return_value = True
        mock_fetch.return_value = "123456"
        mock_nav.return_value = True
        mock_click.return_value = True
        mock_submit.return_value = True
        mock_calendar.return_value = BookingOutcome(
            BookingResult.NO_SLOTS, message="No green dates"
        )

        outcome = run_booking_attempt(app_config, correlation_id="test")

        assert outcome.result == BookingResult.NO_SLOTS
        mock_fail_notify.assert_called_once()
        mock_success_notify.assert_not_called()

    @patch("prenotami_booker.orchestrator.close_driver")
    @patch("prenotami_booker.orchestrator.send_failure_notification")
    @patch("prenotami_booker.orchestrator.time.sleep")
    @patch("prenotami_booker.orchestrator.navigate_to_services")
    @patch("prenotami_booker.orchestrator.click_citizenship_booking")
    @patch("prenotami_booker.orchestrator.wait_until")
    @patch("prenotami_booker.orchestrator.fetch_otp_from_email")
    @patch("prenotami_booker.orchestrator.trigger_otp_via_passport")
    @patch("prenotami_booker.orchestrator.get_next_release_time")
    @patch("prenotami_booker.orchestrator.login")
    @patch("prenotami_booker.orchestrator.create_driver")
    def test_citizenship_click_retries_once(
        self,
        mock_create: MagicMock,
        mock_login: MagicMock,
        mock_get_release: MagicMock,
        mock_trigger: MagicMock,
        mock_fetch: MagicMock,
        mock_wait: MagicMock,
        mock_click: MagicMock,
        mock_nav: MagicMock,
        mock_sleep: MagicMock,
        mock_notify: MagicMock,
        mock_close: MagicMock,
        app_config: AppConfig,
    ) -> None:
        """If citizenship click fails, retries once (re-navigates first)."""
        mock_login.return_value = True
        mock_get_release.return_value = datetime.now(UTC) + timedelta(seconds=5)
        mock_trigger.return_value = True
        mock_fetch.return_value = "123456"
        mock_nav.return_value = True
        mock_click.side_effect = [False, True]

        with patch("prenotami_booker.orchestrator.submit_otp_and_proceed", return_value=True), \
             patch("prenotami_booker.orchestrator.navigate_calendar_and_book", return_value=BookingOutcome(BookingResult.SUCCESS)), \
             patch("prenotami_booker.orchestrator.send_success_notification"):
            outcome = run_booking_attempt(app_config, correlation_id="test")

        assert outcome.result == BookingResult.SUCCESS
        assert mock_click.call_count == 2

    @patch("prenotami_booker.orchestrator.close_driver")
    @patch("prenotami_booker.orchestrator.send_success_notification")
    @patch("prenotami_booker.orchestrator.navigate_calendar_and_book")
    @patch("prenotami_booker.orchestrator.submit_otp_and_proceed")
    @patch("prenotami_booker.orchestrator.click_citizenship_booking")
    @patch("prenotami_booker.orchestrator.wait_until")
    @patch("prenotami_booker.orchestrator.navigate_to_services")
    @patch("prenotami_booker.orchestrator.fetch_otp_from_email")
    @patch("prenotami_booker.orchestrator.trigger_otp_via_passport")
    @patch("prenotami_booker.orchestrator.get_next_release_time")
    @patch("prenotami_booker.orchestrator.login")
    @patch("prenotami_booker.orchestrator.create_driver")
    def test_otp_passed_to_submit(
        self,
        mock_create: MagicMock,
        mock_login: MagicMock,
        mock_get_release: MagicMock,
        mock_trigger: MagicMock,
        mock_fetch: MagicMock,
        mock_nav: MagicMock,
        mock_wait: MagicMock,
        mock_click: MagicMock,
        mock_submit: MagicMock,
        mock_calendar: MagicMock,
        mock_notify: MagicMock,
        mock_close: MagicMock,
        app_config: AppConfig,
    ) -> None:
        """OTP retrieved from email must be correctly passed to submission."""
        mock_login.return_value = True
        mock_get_release.return_value = datetime.now(UTC) + timedelta(seconds=5)
        mock_trigger.return_value = True
        mock_fetch.return_value = "789012"
        mock_nav.return_value = True
        mock_click.return_value = True
        mock_submit.return_value = True
        mock_calendar.return_value = BookingOutcome(BookingResult.SUCCESS)

        run_booking_attempt(app_config, correlation_id="test")

        # Verify the OTP code was passed through correctly
        mock_submit.assert_called_once()
        call_args = mock_submit.call_args
        assert call_args[0][1] == "789012"  # Second positional arg is otp_code


class TestRunScheduled:
    @patch("prenotami_booker.orchestrator.time.sleep")
    @patch("prenotami_booker.orchestrator.run_booking_attempt")
    @patch("prenotami_booker.orchestrator.get_next_release_time")
    def test_stops_on_success(
        self,
        mock_release: MagicMock,
        mock_attempt: MagicMock,
        mock_sleep: MagicMock,
        app_config: AppConfig,
    ) -> None:
        mock_release.return_value = datetime.now(UTC) + timedelta(seconds=1)
        mock_attempt.return_value = BookingOutcome(
            BookingResult.SUCCESS,
            appointment_date="2025-07-15",
        )

        run_scheduled(app_config)

        mock_attempt.assert_called_once()

    @patch("prenotami_booker.orchestrator.time.sleep")
    @patch("prenotami_booker.orchestrator.run_booking_attempt")
    @patch("prenotami_booker.orchestrator.get_next_release_time")
    def test_retries_on_failure(
        self,
        mock_release: MagicMock,
        mock_attempt: MagicMock,
        mock_sleep: MagicMock,
        app_config: AppConfig,
    ) -> None:
        mock_release.return_value = datetime.now(UTC) + timedelta(seconds=1)
        mock_attempt.side_effect = [
            BookingOutcome(BookingResult.NO_SLOTS),
            BookingOutcome(BookingResult.SUCCESS, appointment_date="2025-07-15"),
        ]

        run_scheduled(app_config)

        assert mock_attempt.call_count == 2

    @patch("prenotami_booker.orchestrator.time.sleep")
    @patch("prenotami_booker.orchestrator.run_booking_attempt")
    @patch("prenotami_booker.orchestrator.get_next_release_time")
    def test_handles_keyboard_interrupt(
        self,
        mock_release: MagicMock,
        mock_attempt: MagicMock,
        mock_sleep: MagicMock,
        app_config: AppConfig,
    ) -> None:
        mock_release.return_value = datetime.now(UTC) + timedelta(seconds=1)
        mock_attempt.side_effect = KeyboardInterrupt()

        # Should not raise
        run_scheduled(app_config)


class TestFailHelper:
    def test_creates_failure_outcome(self, app_config: AppConfig) -> None:
        with patch("prenotami_booker.orchestrator.send_failure_notification"):
            outcome = _fail(
                app_config, "test-cid", BookingResult.LOGIN_FAILED, "Bad credentials"
            )

        assert outcome.result == BookingResult.LOGIN_FAILED
        assert outcome.message == "Bad credentials"

    def test_sends_notification(self, app_config: AppConfig) -> None:
        with patch("prenotami_booker.orchestrator.send_failure_notification") as mock_notify:
            _fail(app_config, "test-cid", BookingResult.ERROR, "Something broke")

        mock_notify.assert_called_once_with(
            app_config.notification,
            correlation_id="test-cid",
            reason="Something broke",
            consulate="London",
        )
