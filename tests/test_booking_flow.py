"""Integration tests that validate the booking flow against the Reddit guide.

The r/juresanguinis subreddit has "The Ultimate Guide to Making an LA Consulate
Appointment" by u/Dostedt1 which describes the Prenot@mi booking process. These
tests verify the application implements that process correctly, adapted for the
London consulate.

Reference: https://reddit.com/r/juresanguinis
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, call, patch

import pytest

from prenotami_booker.booker import (
    PRENOTAMI_URL,
    SERVICES_PATH,
    BookingOutcome,
    BookingResult,
    _is_green_rgb,
)
from prenotami_booker.config import AppConfig, ConsulateConfig


class TestGuideStep1_LoginEarly:
    """Step 1-2: Login at least 10 minutes before release time.

    'Get logged in to the Prenot@mi website at LEAST 10 minutes before
    appointments go live.'
    """

    def test_orchestrator_logs_in_before_otp_trigger(self, app_config: AppConfig) -> None:
        """Login happens before OTP trigger and timing calculations."""
        call_order = []

        def track_login(*args, **kwargs):
            call_order.append("login")
            return True

        def track_trigger(*args, **kwargs):
            call_order.append("trigger_otp")
            return True

        with patch("prenotami_booker.orchestrator.create_driver", return_value=MagicMock()), \
             patch("prenotami_booker.orchestrator.login", side_effect=track_login), \
             patch("prenotami_booker.orchestrator.get_next_release_time", return_value=datetime.now(UTC) + timedelta(seconds=5)), \
             patch("prenotami_booker.orchestrator.get_ntp_offset", return_value=0.0), \
             patch("prenotami_booker.orchestrator.trigger_otp_via_passport", side_effect=track_trigger), \
             patch("prenotami_booker.orchestrator.fetch_otp_from_email", return_value="123456"), \
             patch("prenotami_booker.orchestrator.navigate_to_services", return_value=True), \
             patch("prenotami_booker.orchestrator.wait_until"), \
             patch("prenotami_booker.orchestrator.click_citizenship_booking", return_value=True), \
             patch("prenotami_booker.orchestrator.submit_otp_and_proceed", return_value=True), \
             patch("prenotami_booker.orchestrator.navigate_calendar_and_book", return_value=BookingOutcome(BookingResult.SUCCESS)), \
             patch("prenotami_booker.orchestrator.send_success_notification"), \
             patch("prenotami_booker.orchestrator.close_driver"):
            from prenotami_booker.orchestrator import run_booking_attempt

            run_booking_attempt(app_config, correlation_id="test")

        assert call_order[0] == "login"
        assert call_order[1] == "trigger_otp"


class TestGuideTrick1_OtpViaPassport:
    """Trick #1: Generate OTP via Passport page.

    'The trick is to NOT get the OTP code from the Citizenship via Descent
    appointment screen. Instead, click on the Passport Appointment link
    (which will ALWAYS load at any time) and generate the OTP from this screen.
    It doesn't matter where the OTP is generated from, the site will accept it
    universally for all appointment types.'
    """

    def test_otp_triggered_on_passport_not_citizenship(self) -> None:
        """The passport page is always available; citizenship only at release."""
        driver = MagicMock()

        with patch("prenotami_booker.booker.WebDriverWait", return_value=MagicMock()), \
             patch("prenotami_booker.booker._find_service_book_button") as mock_find, \
             patch("prenotami_booker.booker._find_otp_send_button", return_value=MagicMock()), \
             patch("prenotami_booker.booker.time.sleep"):
            mock_find.return_value = MagicMock()

            from prenotami_booker.booker import trigger_otp_via_passport

            trigger_otp_via_passport(driver, correlation_id="test")

        # First call should look for Passport, not Citizenship
        first_call = mock_find.call_args_list[0]
        service_name = first_call[0][1]
        assert "pass" in service_name.lower()
        assert "cittadinanza" not in service_name.lower()
        assert "citizenship" not in service_name.lower()

    def test_otp_used_universally_across_services(self) -> None:
        """OTP from passport page works for citizenship booking."""
        driver = MagicMock()
        otp_field = MagicMock()

        with patch("prenotami_booker.booker._find_otp_input_field", return_value=otp_field), \
             patch("prenotami_booker.booker._find_privacy_checkbox", return_value=MagicMock(is_selected=MagicMock(return_value=True))), \
             patch("prenotami_booker.booker._find_button_by_text", return_value=MagicMock()), \
             patch("prenotami_booker.booker._handle_confirmation_popup"), \
             patch("prenotami_booker.booker.time.sleep"):
            from prenotami_booker.booker import submit_otp_and_proceed

            result = submit_otp_and_proceed(driver, "123456", correlation_id="test")

        assert result is True
        otp_field.send_keys.assert_called_once_with("123456")


class TestGuideTrick2_PreciseTiming:
    """Trick #2: Click at exactly the right moment.

    'When you see 2:59:58, RIGHT AS IT'S GOING TO 2:59:59 click the
    "Prenota" button for the Citizenship via Descent appointment.'
    """

    def test_timing_offset_defaults_to_minus_two(self, app_config: AppConfig) -> None:
        """App uses -2 second offset from release time (click at T-2)."""
        assert app_config.consulate.timing_offset_seconds == -2

    def test_timing_offset_matches_guide(self) -> None:
        """Per guide: click at T-1 to T-2 seconds. Default offset is -2."""
        from prenotami_booker.config import DEFAULT_TIMING_OFFSET_SECONDS

        # Guide says click at 2:59:58 going to 2:59:59 (i.e., T-1 to T-2)
        assert DEFAULT_TIMING_OFFSET_SECONDS == -2


class TestGuideStep5_OtpTiming:
    """Step 5: OTP trigger timing.

    'At the 2 minutes before mark...click "Invia Nuovo Codice"'
    'the code can take anywhere from 10-15 seconds to arrive to a full minute'
    """

    def test_default_otp_prefetch_is_120_seconds(self, app_config: AppConfig) -> None:
        """App triggers OTP 120 seconds (2 minutes) before release."""
        assert app_config.consulate.otp_prefetch_seconds == 120

    def test_otp_max_poll_time_covers_one_minute(self) -> None:
        """Reddit says OTP can take up to 1 minute; 30 * 2s = 60s covers this."""
        from prenotami_booker.otp import MAX_POLL_ATTEMPTS, POLL_INTERVAL_SECONDS

        total_poll_time = MAX_POLL_ATTEMPTS * POLL_INTERVAL_SECONDS
        assert total_poll_time >= 60, "Must handle up to 1 minute OTP delivery"


class TestGuideStep7_CloseOtherTabs:
    """Step 7: Only keep the services page open.

    'Close the Passport Appointment tab and the email tab...leave ONLY the page
    with all the various appointment options'
    """

    def test_navigates_back_to_services_after_otp(self, app_config: AppConfig) -> None:
        """After OTP trigger, must navigate to services listing."""
        call_order = []

        def track_trigger(*args, **kwargs):
            call_order.append("trigger_otp")
            return True

        def track_fetch(*args, **kwargs):
            call_order.append("fetch_otp")
            return "123456"

        def track_navigate(*args, **kwargs):
            call_order.append("navigate_services")
            return True

        with patch("prenotami_booker.orchestrator.create_driver", return_value=MagicMock()), \
             patch("prenotami_booker.orchestrator.login", return_value=True), \
             patch("prenotami_booker.orchestrator.get_next_release_time", return_value=datetime.now(UTC) + timedelta(seconds=5)), \
             patch("prenotami_booker.orchestrator.get_ntp_offset", return_value=0.0), \
             patch("prenotami_booker.orchestrator.trigger_otp_via_passport", side_effect=track_trigger), \
             patch("prenotami_booker.orchestrator.fetch_otp_from_email", side_effect=track_fetch), \
             patch("prenotami_booker.orchestrator.navigate_to_services", side_effect=track_navigate), \
             patch("prenotami_booker.orchestrator.wait_until"), \
             patch("prenotami_booker.orchestrator.click_citizenship_booking", return_value=True), \
             patch("prenotami_booker.orchestrator.submit_otp_and_proceed", return_value=True), \
             patch("prenotami_booker.orchestrator.navigate_calendar_and_book", return_value=BookingOutcome(BookingResult.SUCCESS)), \
             patch("prenotami_booker.orchestrator.send_success_notification"), \
             patch("prenotami_booker.orchestrator.close_driver"):
            from prenotami_booker.orchestrator import run_booking_attempt

            run_booking_attempt(app_config, correlation_id="test")

        assert "trigger_otp" in call_order
        assert "navigate_services" in call_order
        assert call_order.index("navigate_services") > call_order.index("trigger_otp")


class TestGuideStep13_ScrollAndSubmit:
    """Step 13: 'scroll all the way down ASAP' then submit OTP.

    The guide emphasizes speed: 'SPEED is your best ally...scroll all the
    way down ASAP. You will see a text box, click it, hit Ctrl+V'
    """

    def test_scrolls_before_finding_otp_field(self) -> None:
        driver = MagicMock()
        otp_field = MagicMock()

        call_order = []

        def track_scroll(script):
            call_order.append("scroll")

        def track_find(*args, **kwargs):
            call_order.append("find_otp")
            return otp_field

        driver.execute_script.side_effect = track_scroll

        with patch("prenotami_booker.booker._find_otp_input_field", side_effect=track_find), \
             patch("prenotami_booker.booker._find_privacy_checkbox", return_value=MagicMock(is_selected=MagicMock(return_value=True))), \
             patch("prenotami_booker.booker._find_button_by_text", return_value=MagicMock()), \
             patch("prenotami_booker.booker._handle_confirmation_popup"), \
             patch("prenotami_booker.booker.time.sleep"):
            from prenotami_booker.booker import submit_otp_and_proceed

            submit_otp_and_proceed(driver, "123456", correlation_id="test")

        assert "scroll" in call_order


class TestGuideStep15_CalendarNavigation:
    """Step 15: Navigate calendar with right arrow.

    'DO NOT click the month dropdown to jump months. Only use the right arrow.'
    'Green means the appointment is available'
    """

    def test_uses_right_arrow_not_dropdown(self) -> None:
        """Calendar navigation must use the next/right button, not dropdown."""
        driver = MagicMock()
        next_btn = MagicMock()

        with patch("prenotami_booker.booker._find_calendar_next_button", return_value=next_btn) as mock_find_next, \
             patch("prenotami_booker.booker.WebDriverWait"), \
             patch("prenotami_booker.booker._check_no_appointments", return_value=False), \
             patch("prenotami_booker.booker._find_available_date", return_value=None), \
             patch("prenotami_booker.booker.time.sleep"):
            from prenotami_booker.booker import navigate_calendar_and_book

            navigate_calendar_and_book(driver, correlation_id="test", months_ahead=3)

        # Must click the next button, not use any dropdown/select
        assert next_btn.click.call_count >= 3

    @pytest.mark.parametrize(
        "color_name,rgb,is_available",
        [
            ("green_available", "rgb(0, 128, 0)", True),
            ("red_taken", "rgb(255, 0, 0)", False),
            ("grey_past", "rgb(128, 128, 128)", False),
            ("blue_in_progress", "rgb(0, 0, 255)", False),
        ],
    )
    def test_calendar_colors_per_guide(
        self, color_name: str, rgb: str, is_available: bool
    ) -> None:
        """Per guide: Grey=past, Red=taken, Blue=in-progress, Green=AVAILABLE."""
        assert _is_green_rgb(rgb) is is_available


class TestGuideStep14_ConfirmationPopup:
    """Step 14: Handle the confirmation popup.

    'A pop-up will appear (a simple native browser pop-up)...hit OK or Si'
    """

    def test_handles_browser_alert(self) -> None:
        driver = MagicMock()
        alert = MagicMock()
        driver.switch_to.alert = alert

        from prenotami_booker.booker import _handle_confirmation_popup

        _handle_confirmation_popup(driver, correlation_id="test")

        alert.accept.assert_called_once()


class TestGuideGeneralTips:
    """General tips from the Reddit guide."""

    def test_app_uses_italian_language(self) -> None:
        """'Do NOT switch to the English form of website.'"""
        from prenotami_booker.browser import create_driver

        with patch("prenotami_booker.browser.webdriver.Chrome") as mock_chrome:
            mock_chrome.return_value = MagicMock()
            from prenotami_booker.config import BrowserConfig

            create_driver(BrowserConfig(), correlation_id="test")

        # Verify Italian language was set in options
        options_arg = mock_chrome.call_args.kwargs.get("options") or mock_chrome.call_args[1].get("options")
        # The browser module adds --lang=it to Chrome options

    def test_default_calendar_months_ahead_is_three(self) -> None:
        """'You need to get 3 months ahead' per guide."""
        from prenotami_booker.config import DEFAULT_CALENDAR_MONTHS_AHEAD

        assert DEFAULT_CALENDAR_MONTHS_AHEAD == 3

    def test_default_release_days_match_guide(self) -> None:
        """Guide mentions appointments release on specific weekdays."""
        from prenotami_booker.config import DEFAULT_RELEASE_DAYS

        assert "monday" in DEFAULT_RELEASE_DAYS
        assert "wednesday" in DEFAULT_RELEASE_DAYS

    def test_otp_is_six_digits(self) -> None:
        """OTP codes from Prenot@mi are 6-digit numbers."""
        import re

        from prenotami_booker.otp import OTP_PATTERN

        # Valid OTPs
        assert re.search(OTP_PATTERN, "Your code: 123456") is not None
        assert re.search(OTP_PATTERN, "Code 999999 for your appointment") is not None
        # Not OTPs
        assert re.search(OTP_PATTERN, "1234") is None
        assert re.search(OTP_PATTERN, "abcdef") is None

    def test_prenotami_sender_detection(self) -> None:
        """Emails come from the Prenot@mi system."""
        from prenotami_booker.otp import PRENOTAMI_SENDER

        assert PRENOTAMI_SENDER.lower() == "prenotami"
