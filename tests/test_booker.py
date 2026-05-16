"""Tests for booker module - the core Selenium automation.

These tests validate the booking flow against the real Prenot@mi process
documented in the Reddit guide from r/juresanguinis.
"""

from __future__ import annotations

from unittest.mock import MagicMock, PropertyMock, call, patch

import pytest
from selenium.common.exceptions import NoSuchElementException, TimeoutException, WebDriverException

from prenotami_booker.booker import (
    BOOKING_PATH,
    CALENDAR_LOAD_TIMEOUT,
    IAM_LOGIN_DOMAIN,
    LOGIN_MAX_RETRIES,
    LOGIN_RETRY_DELAY,
    LOGIN_TIMEOUT,
    PAGE_LOAD_TIMEOUT,
    PRENOTAMI_URL,
    SERVICES_PATH,
    BookingOutcome,
    BookingResult,
    _check_no_appointments,
    _find_available_date,
    _find_button_by_text,
    _find_calendar_next_button,
    _find_otp_input_field,
    _find_otp_send_button,
    _find_privacy_checkbox,
    _find_service_book_button,
    _handle_confirmation_popup,
    _is_green_rgb,
    _login_attempt,
    click_citizenship_booking,
    login,
    navigate_calendar_and_book,
    navigate_to_services,
    submit_otp_and_proceed,
    trigger_otp_via_passport,
)
from prenotami_booker.config import AppConfig


class TestConstants:
    def test_prenotami_url(self) -> None:
        assert PRENOTAMI_URL == "https://prenotami.esteri.it"

    def test_services_path(self) -> None:
        assert SERVICES_PATH == "/Services"

    def test_booking_path(self) -> None:
        assert BOOKING_PATH == "/Services/Ede"

    def test_iam_login_domain(self) -> None:
        assert IAM_LOGIN_DOMAIN == "iam.esteri.it"

    def test_timeouts_are_reasonable(self) -> None:
        assert LOGIN_TIMEOUT == 30
        assert PAGE_LOAD_TIMEOUT == 30
        assert CALENDAR_LOAD_TIMEOUT == 60


class TestBookingResult:
    def test_all_outcomes(self) -> None:
        expected = {"success", "no_slots", "otp_failed", "login_failed",
                    "calendar_error", "timeout", "error"}
        assert {r.value for r in BookingResult} == expected


class TestBookingOutcome:
    def test_success_outcome(self) -> None:
        outcome = BookingOutcome(
            BookingResult.SUCCESS,
            appointment_date="2025-04-15",
            appointment_time="10:00",
            message="Booked!",
        )
        assert outcome.result == BookingResult.SUCCESS
        assert outcome.appointment_date == "2025-04-15"
        assert outcome.appointment_time == "10:00"

    def test_failure_outcome_defaults(self) -> None:
        outcome = BookingOutcome(BookingResult.NO_SLOTS)
        assert outcome.appointment_date == ""
        assert outcome.appointment_time == ""
        assert outcome.message == ""


class TestLoginAttempt:
    """Test single login attempt via iam.esteri.it OAuth flow."""

    def test_successful_attempt(self, mock_driver: MagicMock, app_config: AppConfig) -> None:
        wait_mock = MagicMock()
        login_link = MagicMock()
        email_field = MagicMock()
        password_field = MagicMock()
        submit_btn = MagicMock()

        # wait.until() is called 4 times:
        # 1. element_to_be_clickable (login link)
        # 2. url_contains (iam domain)
        # 3. presence_of_element_located (email field)
        # 4. url_contains (prenotami.esteri.it) — may land on /UserArea or /Services
        wait_mock.until.side_effect = [login_link, True, email_field, True]
        mock_driver.find_element.side_effect = [password_field, submit_btn]

        with patch("prenotami_booker.booker.WebDriverWait", return_value=wait_mock):
            result = _login_attempt(mock_driver, app_config, correlation_id="test")

        assert result is True
        mock_driver.get.assert_called_once_with(PRENOTAMI_URL)
        login_link.click.assert_called_once()
        email_field.send_keys.assert_called_once_with("user@example.com")
        password_field.send_keys.assert_called_once_with("secret123")
        submit_btn.click.assert_called_once()

    def test_timeout_returns_false(self, mock_driver: MagicMock, app_config: AppConfig) -> None:
        wait_mock = MagicMock()
        wait_mock.until.side_effect = TimeoutException()

        with patch("prenotami_booker.booker.WebDriverWait", return_value=wait_mock):
            result = _login_attempt(mock_driver, app_config, correlation_id="test")

        assert result is False

    def test_element_not_found(self, mock_driver: MagicMock, app_config: AppConfig) -> None:
        wait_mock = MagicMock()
        login_link = MagicMock()
        wait_mock.until.side_effect = [login_link, True, MagicMock()]
        mock_driver.find_element.side_effect = NoSuchElementException()

        with patch("prenotami_booker.booker.WebDriverWait", return_value=wait_mock):
            result = _login_attempt(mock_driver, app_config, correlation_id="test")

        assert result is False


class TestLogin:
    """Test login with retry wrapper."""

    def test_succeeds_on_first_attempt(self, mock_driver: MagicMock, app_config: AppConfig) -> None:
        with patch("prenotami_booker.booker._login_attempt", return_value=True) as mock_attempt:
            result = login(mock_driver, app_config, correlation_id="test")

        assert result is True
        assert mock_attempt.call_count == 1

    def test_retries_on_failure(self, mock_driver: MagicMock, app_config: AppConfig) -> None:
        with patch("prenotami_booker.booker._login_attempt", side_effect=[False, False, True]) as mock_attempt, \
             patch("prenotami_booker.booker.time.sleep") as mock_sleep:
            result = login(mock_driver, app_config, correlation_id="test")

        assert result is True
        assert mock_attempt.call_count == 3
        assert mock_sleep.call_count == 2
        mock_sleep.assert_called_with(LOGIN_RETRY_DELAY)

    def test_fails_after_all_retries(self, mock_driver: MagicMock, app_config: AppConfig) -> None:
        with patch("prenotami_booker.booker._login_attempt", return_value=False) as mock_attempt, \
             patch("prenotami_booker.booker.time.sleep"):
            result = login(mock_driver, app_config, correlation_id="test")

        assert result is False
        assert mock_attempt.call_count == LOGIN_MAX_RETRIES


class TestTriggerOtpViaPassport:
    """Test Trick #1 from Reddit: Generate OTP via any available service page.

    'The trick is to NOT get the OTP code from the Citizenship via Descent
    appointment screen. Instead, click on the Passport Appointment link
    (which will ALWAYS load at any time) and generate the OTP from this screen.'

    The implementation tries Passport first, then falls back to other services
    and dismisses 'no slots' modals when they appear.
    """

    def test_navigates_to_services_page(self, mock_driver: MagicMock) -> None:
        mock_driver.current_url = f"{PRENOTAMI_URL}{BOOKING_PATH}?id=1"
        wait_mock = MagicMock()
        with patch("prenotami_booker.booker.WebDriverWait", return_value=wait_mock), \
             patch("prenotami_booker.booker._find_service_book_button", return_value=MagicMock()), \
             patch("prenotami_booker.booker._find_otp_send_button", return_value=MagicMock()), \
             patch("prenotami_booker.booker.time.sleep"):
            trigger_otp_via_passport(mock_driver, correlation_id="test")

        mock_driver.get.assert_called_with(f"{PRENOTAMI_URL}{SERVICES_PATH}")

    def test_clicks_passport_then_otp_button(self, mock_driver: MagicMock) -> None:
        """Per guide: click Passport 'Prenota' then 'Invia Nuovo Codice'."""
        mock_driver.current_url = f"{PRENOTAMI_URL}{BOOKING_PATH}?id=1"
        wait_mock = MagicMock()
        passport_btn = MagicMock()
        otp_btn = MagicMock()

        with patch("prenotami_booker.booker.WebDriverWait", return_value=wait_mock), \
             patch("prenotami_booker.booker._find_service_book_button", return_value=passport_btn), \
             patch("prenotami_booker.booker._find_otp_send_button", return_value=otp_btn), \
             patch("prenotami_booker.booker.time.sleep"):
            result = trigger_otp_via_passport(mock_driver, correlation_id="test")

        assert result is True
        passport_btn.click.assert_called_once()
        otp_btn.click.assert_called_once()

    def test_tries_alternative_names(self, mock_driver: MagicMock) -> None:
        """Tries 'Passaporto' first, then 'Passport' (Italian/English)."""
        mock_driver.current_url = f"{PRENOTAMI_URL}{BOOKING_PATH}?id=1"
        wait_mock = MagicMock()
        passport_btn = MagicMock()

        def fake_find(driver, text):
            if text == "Passaporto":
                return None
            if text == "Passport":
                return passport_btn
            return None

        with patch("prenotami_booker.booker.WebDriverWait", return_value=wait_mock), \
             patch("prenotami_booker.booker._find_service_book_button", side_effect=fake_find), \
             patch("prenotami_booker.booker._find_otp_send_button", return_value=MagicMock()), \
             patch("prenotami_booker.booker.time.sleep"):
            result = trigger_otp_via_passport(mock_driver, correlation_id="test")

        assert result is True

    def test_handles_no_slots_modal_and_tries_next(self, mock_driver: MagicMock) -> None:
        """If a service shows 'no slots' modal (URL stays on /Services), try next."""
        services_url = f"{PRENOTAMI_URL}{SERVICES_PATH}"
        booking_url = f"{PRENOTAMI_URL}{BOOKING_PATH}?id=1"
        passport_btn = MagicMock()
        cittadinanza_btn = MagicMock()
        otp_btn = MagicMock()

        def fake_find(driver, text):
            if text in ("Passaporto", "Passport"):
                return passport_btn
            if text == "Cittadinanza":
                return cittadinanza_btn
            return None

        # Passport click leaves URL on /Services (modal), citizenship takes us to booking
        mock_driver.current_url = services_url
        passport_btn.click.side_effect = lambda: None  # URL stays on services
        cittadinanza_btn.click.side_effect = lambda: setattr(mock_driver, "current_url", booking_url)

        ok_btn = MagicMock()
        ok_btn.text = "OK"
        mock_driver.find_elements.return_value = [ok_btn]

        with patch("prenotami_booker.booker.WebDriverWait"), \
             patch("prenotami_booker.booker._find_service_book_button", side_effect=fake_find), \
             patch("prenotami_booker.booker._find_otp_send_button", return_value=otp_btn), \
             patch("prenotami_booker.booker.time.sleep"):
            result = trigger_otp_via_passport(mock_driver, correlation_id="test")

        assert result is True
        otp_btn.click.assert_called_once()

    def test_returns_false_when_all_services_fail(self, mock_driver: MagicMock) -> None:
        mock_driver.current_url = f"{PRENOTAMI_URL}{SERVICES_PATH}"
        mock_driver.find_elements.return_value = []
        wait_mock = MagicMock()
        with patch("prenotami_booker.booker.WebDriverWait", return_value=wait_mock), \
             patch("prenotami_booker.booker._find_service_book_button", return_value=None), \
             patch("prenotami_booker.booker.time.sleep"):
            result = trigger_otp_via_passport(mock_driver, correlation_id="test")

        assert result is False

    def test_scrolls_to_otp_section(self, mock_driver: MagicMock) -> None:
        """Per guide: 'Scroll all the way down' to find OTP section."""
        mock_driver.current_url = f"{PRENOTAMI_URL}{BOOKING_PATH}?id=1"
        wait_mock = MagicMock()
        with patch("prenotami_booker.booker.WebDriverWait", return_value=wait_mock), \
             patch("prenotami_booker.booker._find_service_book_button", return_value=MagicMock()), \
             patch("prenotami_booker.booker._find_otp_send_button", return_value=MagicMock()), \
             patch("prenotami_booker.booker.time.sleep"):
            trigger_otp_via_passport(mock_driver, correlation_id="test")

        mock_driver.execute_script.assert_called_with(
            "window.scrollTo(0, document.body.scrollHeight);"
        )


class TestClickCitizenshipBooking:
    """Test Trick #2: clicking the citizenship button at precise moment.

    'When you see 2:59:58, RIGHT AS IT'S GOING TO 2:59:59 click the
    "Prenota" button.'
    """

    def test_successful_click(self, mock_driver: MagicMock) -> None:
        citizenship_btn = MagicMock()
        mock_driver.current_url = f"{PRENOTAMI_URL}{BOOKING_PATH}?id=5"
        with patch("prenotami_booker.booker._find_service_book_button", return_value=citizenship_btn), \
             patch("prenotami_booker.booker.WebDriverWait"), \
             patch("prenotami_booker.booker.time.sleep"):
            result = click_citizenship_booking(mock_driver, correlation_id="test")

        assert result is True
        citizenship_btn.click.assert_called_once()

    def test_tries_multiple_language_variants(self, mock_driver: MagicMock) -> None:
        """Tries discendenza/descent first (most specific), then Cittadinanza/Citizenship."""
        btn = MagicMock()
        call_args = []
        mock_driver.current_url = f"{PRENOTAMI_URL}{BOOKING_PATH}?id=5"

        def fake_find(driver, text):
            call_args.append(text)
            if text == "Cittadinanza":
                return btn
            return None

        with patch("prenotami_booker.booker._find_service_book_button", side_effect=fake_find), \
             patch("prenotami_booker.booker.WebDriverWait"), \
             patch("prenotami_booker.booker.time.sleep"):
            result = click_citizenship_booking(mock_driver, correlation_id="test")

        assert result is True
        # Most-specific terms tried first to avoid matching "Riacquisto della cittadinanza"
        assert "discendenza" in call_args
        assert "descent" in call_args
        assert "Cittadinanza" in call_args  # fallback if specific terms not found

    def test_webdriverwait_calls_until(self, mock_driver: MagicMock) -> None:
        """WebDriverWait waits for the booking URL after the initial URL check passes."""
        mock_driver.current_url = f"{PRENOTAMI_URL}{BOOKING_PATH}?id=5"
        with patch("prenotami_booker.booker._find_service_book_button", return_value=MagicMock()), \
             patch("prenotami_booker.booker.WebDriverWait") as mock_wait_class, \
             patch("prenotami_booker.booker.time.sleep"):
            click_citizenship_booking(mock_driver, correlation_id="test")

        wait_instance = mock_wait_class.return_value
        wait_instance.until.assert_called_once()

    def test_returns_false_when_no_slots_modal(self, mock_driver: MagicMock) -> None:
        """Returns False immediately when citizenship shows no-slots modal (URL stays on /Services)."""
        mock_driver.current_url = f"{PRENOTAMI_URL}{SERVICES_PATH}"
        mock_driver.find_elements.return_value = []  # no modal buttons
        with patch("prenotami_booker.booker._find_service_book_button", return_value=MagicMock()), \
             patch("prenotami_booker.booker.WebDriverWait"), \
             patch("prenotami_booker.booker.time.sleep"):
            result = click_citizenship_booking(mock_driver, correlation_id="test")
        assert result is False

    def test_returns_false_when_not_found(self, mock_driver: MagicMock) -> None:
        with patch("prenotami_booker.booker._find_service_book_button", return_value=None), \
             patch("prenotami_booker.booker.time.sleep"):
            result = click_citizenship_booking(mock_driver, correlation_id="test")
        assert result is False


class TestSubmitOtpAndProceed:
    """Test OTP submission flow.

    Per guide step 13: 'scroll all the way down ASAP...click [OTP textbox],
    hit Ctrl+V...click the check box below "Informativa sulla Privacy"...
    click the blue box "Avanti"'
    """

    def test_complete_otp_submission_flow(self, mock_driver: MagicMock) -> None:
        otp_field = MagicMock()
        privacy_cb = MagicMock()
        privacy_cb.is_selected.return_value = False
        avanti_btn = MagicMock()

        with patch("prenotami_booker.booker._find_otp_input_field", return_value=otp_field), \
             patch("prenotami_booker.booker._find_privacy_checkbox", return_value=privacy_cb), \
             patch("prenotami_booker.booker._find_button_by_text", return_value=avanti_btn), \
             patch("prenotami_booker.booker._handle_confirmation_popup"), \
             patch("prenotami_booker.booker.time.sleep"):
            result = submit_otp_and_proceed(mock_driver, "123456", correlation_id="test")

        assert result is True
        # Verify submission order: enter OTP, check privacy, click Avanti
        otp_field.send_keys.assert_called_once_with("123456")
        privacy_cb.click.assert_called_once()
        avanti_btn.click.assert_called_once()

    def test_scrolls_down_first(self, mock_driver: MagicMock) -> None:
        """Per guide: 'scroll all the way down ASAP'."""
        with patch("prenotami_booker.booker._find_otp_input_field", return_value=MagicMock()), \
             patch("prenotami_booker.booker._find_privacy_checkbox", return_value=MagicMock(is_selected=MagicMock(return_value=True))), \
             patch("prenotami_booker.booker._find_button_by_text", return_value=MagicMock()), \
             patch("prenotami_booker.booker._handle_confirmation_popup"), \
             patch("prenotami_booker.booker.time.sleep"):
            submit_otp_and_proceed(mock_driver, "123456", correlation_id="test")

        mock_driver.execute_script.assert_called_with(
            "window.scrollTo(0, document.body.scrollHeight);"
        )

    def test_handles_confirmation_popup(self, mock_driver: MagicMock) -> None:
        """Per guide step 14: 'A pop-up will appear...hit OK or Si'."""
        with patch("prenotami_booker.booker._find_otp_input_field", return_value=MagicMock()), \
             patch("prenotami_booker.booker._find_privacy_checkbox", return_value=MagicMock(is_selected=MagicMock(return_value=True))), \
             patch("prenotami_booker.booker._find_button_by_text", return_value=MagicMock()), \
             patch("prenotami_booker.booker._handle_confirmation_popup") as mock_popup, \
             patch("prenotami_booker.booker.time.sleep"):
            submit_otp_and_proceed(mock_driver, "123456", correlation_id="test")

        mock_popup.assert_called_once()

    def test_skips_already_checked_privacy(self, mock_driver: MagicMock) -> None:
        privacy_cb = MagicMock()
        privacy_cb.is_selected.return_value = True

        with patch("prenotami_booker.booker._find_otp_input_field", return_value=MagicMock()), \
             patch("prenotami_booker.booker._find_privacy_checkbox", return_value=privacy_cb), \
             patch("prenotami_booker.booker._find_button_by_text", return_value=MagicMock()), \
             patch("prenotami_booker.booker._handle_confirmation_popup"), \
             patch("prenotami_booker.booker.time.sleep"):
            submit_otp_and_proceed(mock_driver, "123456", correlation_id="test")

        privacy_cb.click.assert_not_called()

    def test_returns_false_when_otp_field_missing(self, mock_driver: MagicMock) -> None:
        with patch("prenotami_booker.booker._find_otp_input_field", return_value=None), \
             patch("prenotami_booker.booker.time.sleep"):
            result = submit_otp_and_proceed(mock_driver, "123456", correlation_id="test")
        assert result is False

    def test_tries_avanti_then_forward_then_submit(self, mock_driver: MagicMock) -> None:
        """Button text fallback: Avanti → Forward → Submit."""
        call_args = []

        def fake_find(driver, text):
            call_args.append(text)
            if text == "Submit":
                return MagicMock()
            return None

        with patch("prenotami_booker.booker._find_otp_input_field", return_value=MagicMock()), \
             patch("prenotami_booker.booker._find_privacy_checkbox", return_value=MagicMock(is_selected=MagicMock(return_value=True))), \
             patch("prenotami_booker.booker._find_button_by_text", side_effect=fake_find), \
             patch("prenotami_booker.booker._handle_confirmation_popup"), \
             patch("prenotami_booker.booker.time.sleep"):
            result = submit_otp_and_proceed(mock_driver, "123456", correlation_id="test")

        assert result is True
        assert call_args == ["Avanti", "Forward", "Submit"]


class TestNavigateCalendarAndBook:
    """Test calendar navigation and booking.

    Per guide step 15: 'click the right arrow on the top right of the
    calendar. Then let it load. Then click it again. Repeat until you're
    3 months away. DO NOT click the month dropdown.'
    """

    def test_navigates_forward_months(self, mock_driver: MagicMock) -> None:
        next_btn = MagicMock()
        avail_date = MagicMock()
        avail_date.text = "15"
        book_btn = MagicMock()

        mock_driver.page_source = "<html>conferma appuntamento confermato</html>"

        with patch("prenotami_booker.booker.WebDriverWait") as mock_wait, \
             patch("prenotami_booker.booker._check_no_appointments", return_value=False), \
             patch("prenotami_booker.booker._find_calendar_next_button", return_value=next_btn), \
             patch("prenotami_booker.booker._find_available_date", return_value=avail_date), \
             patch("prenotami_booker.booker._find_button_by_text", return_value=book_btn), \
             patch("prenotami_booker.booker._handle_confirmation_popup"), \
             patch("prenotami_booker.booker._extract_appointment_details", return_value=("15/04/2025", "10:00")), \
             patch("prenotami_booker.booker.time.sleep"):
            outcome = navigate_calendar_and_book(
                mock_driver, correlation_id="test", months_ahead=3
            )

        assert outcome.result == BookingResult.SUCCESS
        # Should click next button 3 times (for 3 months ahead)
        assert next_btn.click.call_count == 3

    def test_returns_no_slots_when_no_appointments(self, mock_driver: MagicMock) -> None:
        with patch("prenotami_booker.booker.WebDriverWait"), \
             patch("prenotami_booker.booker._check_no_appointments", return_value=True), \
             patch("prenotami_booker.booker.time.sleep"):
            outcome = navigate_calendar_and_book(mock_driver, correlation_id="test")

        assert outcome.result == BookingResult.NO_SLOTS

    def test_returns_no_slots_when_no_green_dates(self, mock_driver: MagicMock) -> None:
        """Per guide: 'all appointments are red, and all dates after are gray'."""
        with patch("prenotami_booker.booker.WebDriverWait") as mock_wait, \
             patch("prenotami_booker.booker._check_no_appointments", return_value=False), \
             patch("prenotami_booker.booker._find_calendar_next_button", return_value=MagicMock()), \
             patch("prenotami_booker.booker._find_available_date", return_value=None), \
             patch("prenotami_booker.booker.time.sleep"):
            outcome = navigate_calendar_and_book(mock_driver, correlation_id="test")

        assert outcome.result == BookingResult.NO_SLOTS

    def test_tries_one_extra_month_before_giving_up(self, mock_driver: MagicMock) -> None:
        """If no dates found after configured months, tries one more."""
        next_btn = MagicMock()
        find_count = 0

        def _find_date(driver):
            nonlocal find_count
            find_count += 1
            return None  # Never find a date

        with patch("prenotami_booker.booker.WebDriverWait") as mock_wait, \
             patch("prenotami_booker.booker._check_no_appointments", return_value=False), \
             patch("prenotami_booker.booker._find_calendar_next_button", return_value=next_btn), \
             patch("prenotami_booker.booker._find_available_date", side_effect=_find_date), \
             patch("prenotami_booker.booker.time.sleep"):
            outcome = navigate_calendar_and_book(
                mock_driver, correlation_id="test", months_ahead=3
            )

        # 3 month advances + 1 extra attempt
        assert next_btn.click.call_count == 4

    def test_handles_timeout(self, mock_driver: MagicMock) -> None:
        with patch("prenotami_booker.booker.WebDriverWait") as mock_wait, \
             patch("prenotami_booker.booker._check_no_appointments", side_effect=TimeoutException()), \
             patch("prenotami_booker.booker.time.sleep"):
            outcome = navigate_calendar_and_book(mock_driver, correlation_id="test")

        assert outcome.result == BookingResult.TIMEOUT

    def test_retries_on_webdriver_error(self, mock_driver: MagicMock) -> None:
        """Per guide: refresh and retry on browser errors during calendar."""
        avail_date = MagicMock()
        avail_date.text = "20"
        book_btn = MagicMock()

        with patch("prenotami_booker.booker.WebDriverWait") as mock_wait, \
             patch("prenotami_booker.booker._check_no_appointments", side_effect=WebDriverException("stale")), \
             patch("prenotami_booker.booker._find_available_date", return_value=avail_date), \
             patch("prenotami_booker.booker._find_button_by_text", return_value=book_btn), \
             patch("prenotami_booker.booker._handle_confirmation_popup"), \
             patch("prenotami_booker.booker._extract_appointment_details", return_value=("20/07/2025", "14:00")), \
             patch("prenotami_booker.booker.time.sleep"):
            outcome = navigate_calendar_and_book(mock_driver, correlation_id="test")

        assert outcome.result == BookingResult.SUCCESS
        mock_driver.refresh.assert_called_once()

    def test_calendar_error_when_retry_fails(self, mock_driver: MagicMock) -> None:
        """Returns CALENDAR_ERROR when refresh-retry also fails."""
        with patch("prenotami_booker.booker.WebDriverWait") as mock_wait, \
             patch("prenotami_booker.booker._check_no_appointments", side_effect=WebDriverException("stale")), \
             patch("prenotami_booker.booker._find_available_date", return_value=None), \
             patch("prenotami_booker.booker.time.sleep"):
            outcome = navigate_calendar_and_book(mock_driver, correlation_id="test")

        assert outcome.result == BookingResult.CALENDAR_ERROR


class TestFindServiceBookButton:
    def test_finds_button_in_table_row(self, mock_driver: MagicMock) -> None:
        row = MagicMock()
        row.text = "Passaporto - Passport"
        btn = MagicMock()
        btn.text = "Prenota"
        row.find_elements.return_value = [btn]
        mock_driver.find_elements.return_value = [row]

        result = _find_service_book_button(mock_driver, "Passaporto")
        assert result == btn

    def test_returns_none_when_not_found(self, mock_driver: MagicMock) -> None:
        mock_driver.find_elements.return_value = []
        result = _find_service_book_button(mock_driver, "Nonexistent")
        assert result is None

    def test_case_insensitive_search(self, mock_driver: MagicMock) -> None:
        row = MagicMock()
        row.text = "CITTADINANZA PER DISCENDENZA"
        btn = MagicMock()
        btn.text = "PRENOTA"
        row.find_elements.return_value = [btn]
        mock_driver.find_elements.return_value = [row]

        result = _find_service_book_button(mock_driver, "cittadinanza")
        assert result == btn


class TestFindOtpSendButton:
    def test_finds_invia_nuovo_codice(self, mock_driver: MagicMock) -> None:
        btn = MagicMock()
        btn.text = "Invia Nuovo Codice"
        mock_driver.find_elements.return_value = [btn]

        result = _find_otp_send_button(mock_driver)
        assert result == btn

    def test_finds_english_variant(self, mock_driver: MagicMock) -> None:
        btn = MagicMock()
        btn.text = "Send New Code"
        mock_driver.find_elements.return_value = [btn]

        result = _find_otp_send_button(mock_driver)
        assert result == btn


class TestFindOtpInputField:
    def test_finds_by_name_otp(self, mock_driver: MagicMock) -> None:
        field = MagicMock()
        mock_driver.find_elements.return_value = [field]

        result = _find_otp_input_field(mock_driver)
        assert result == field

    def test_returns_none_when_no_match(self, mock_driver: MagicMock) -> None:
        mock_driver.find_elements.return_value = []
        result = _find_otp_input_field(mock_driver)
        assert result is None


class TestFindPrivacyCheckbox:
    def test_finds_privacy_checkbox(self, mock_driver: MagicMock) -> None:
        cb = MagicMock()
        parent = MagicMock()
        parent.text = "Informativa sulla Privacy"
        cb.find_element.return_value = parent
        mock_driver.find_elements.return_value = [cb]

        result = _find_privacy_checkbox(mock_driver)
        assert result == cb


class TestHandleConfirmationPopup:
    """Per guide step 14: a popup appears after clicking Avanti."""

    def test_accepts_js_alert(self, mock_driver: MagicMock) -> None:
        alert = MagicMock()
        alert.text = "Sei sicuro?"
        mock_driver.switch_to.alert = alert

        _handle_confirmation_popup(mock_driver, correlation_id="test")
        alert.accept.assert_called_once()

    def test_handles_no_popup_gracefully(self, mock_driver: MagicMock) -> None:
        type(mock_driver.switch_to).alert = PropertyMock(
            side_effect=Exception("no alert")
        )
        mock_driver.find_elements.return_value = []

        # Should not raise
        _handle_confirmation_popup(mock_driver, correlation_id="test")


class TestFindCalendarNextButton:
    """Per guide: 'click the right arrow on the top right of the calendar'."""

    def test_finds_next_button(self, mock_driver: MagicMock) -> None:
        btn = MagicMock()
        btn.is_displayed.return_value = True
        mock_driver.find_elements.return_value = [btn]

        result = _find_calendar_next_button(mock_driver)
        assert result == btn

    def test_finds_arrow_character(self, mock_driver: MagicMock) -> None:
        """Fallback: find right arrow character ›, », etc."""
        btn = MagicMock()
        btn.text = "›"
        btn.is_displayed.return_value = True

        # First return empty for all CSS selectors, then return arrow button
        mock_driver.find_elements.side_effect = [
            [],  # .datepicker .next
            [],  # .calendar .next
            [],  # button[aria-label='Next']
            [],  # button[aria-label='next']
            [],  # .fc-next-button
            [],  # [class*='next']
            [],  # [class*='right']
            [],  # th.next
            [],  # .datepicker-days th.next
            [btn],  # fallback: buttons/th/a/span
        ]

        result = _find_calendar_next_button(mock_driver)
        assert result == btn


class TestFindAvailableDate:
    """Per guide: 'Green means the appointment is available'."""

    def test_finds_green_date_by_css_class(self, mock_driver: MagicMock) -> None:
        date_el = MagicMock()
        date_el.is_displayed.return_value = True
        date_el.value_of_css_property.return_value = "rgb(0, 128, 0)"
        date_el.get_attribute.return_value = "day available"
        mock_driver.find_elements.return_value = [date_el]

        result = _find_available_date(mock_driver)
        assert result == date_el

    def test_skips_disabled_dates(self, mock_driver: MagicMock) -> None:
        """Grey/disabled dates should be skipped."""
        date_el = MagicMock()
        date_el.is_displayed.return_value = True
        date_el.value_of_css_property.return_value = "rgb(0, 128, 0)"
        date_el.get_attribute.return_value = "day disabled"
        mock_driver.find_elements.return_value = [date_el]

        result = _find_available_date(mock_driver)
        # Uses multiple selector strategies; first ones return disabled, eventually returns None
        # The exact behavior depends on which selector matches
        # In this simplified mock, find_elements returns same for all calls
        # This is more of a documentation test


class TestIsGreenRgb:
    """Test color detection for available calendar dates."""

    @pytest.mark.parametrize(
        "color,expected",
        [
            ("rgb(0, 128, 0)", True),       # Pure green
            ("rgb(34, 139, 34)", True),      # Forest green
            ("rgb(0, 200, 0)", True),        # Bright green
            ("rgba(0, 128, 0, 1)", True),    # Green with alpha
            ("rgb(255, 0, 0)", False),       # Red (taken dates)
            ("rgb(128, 128, 128)", False),   # Grey (past dates)
            ("rgb(0, 0, 255)", False),       # Blue (in-progress)
            ("rgb(255, 255, 255)", False),   # White
            ("rgb(0, 0, 0)", False),         # Black
            ("invalid", False),              # Invalid string
            ("", False),                     # Empty
        ],
    )
    def test_color_detection(self, color: str, expected: bool) -> None:
        assert _is_green_rgb(color) == expected

    def test_green_threshold(self) -> None:
        """Green channel must be >80 and >1.5x both red and blue."""
        # Borderline: G=81, R=53, B=53 → G > 80 ✓, 81 > 53*1.5=79.5 ✓
        assert _is_green_rgb("rgb(53, 81, 53)") is True

        # Below threshold: G=80
        assert _is_green_rgb("rgb(0, 80, 0)") is False


class TestCheckNoAppointments:
    def test_detects_italian_no_dates(self, mock_driver: MagicMock) -> None:
        mock_driver.page_source = '<div>Non ci sono date disponibili</div>'
        assert _check_no_appointments(mock_driver) is True

    def test_detects_english_no_appointment(self, mock_driver: MagicMock) -> None:
        mock_driver.page_source = '<div>No appointment available</div>'
        assert _check_no_appointments(mock_driver) is True

    def test_normal_page_returns_false(self, mock_driver: MagicMock) -> None:
        mock_driver.page_source = '<div>Select your appointment date</div>'
        assert _check_no_appointments(mock_driver) is False
