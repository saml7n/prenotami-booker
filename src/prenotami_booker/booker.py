"""Core booking automation for Prenot@mi appointments.

Implements the full booking flow:
1. Login to Prenot@mi
2. Navigate to passport page to trigger OTP (the OTP trick)
3. Retrieve OTP from email
4. Wait for precise release time
5. Click citizenship booking button at the right moment
6. Submit OTP and privacy checkbox
7. Navigate calendar to find available slot
8. Confirm booking
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from enum import Enum

import structlog
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.support import expected_conditions as ec
from selenium.webdriver.support.ui import WebDriverWait

from prenotami_booker.config import AppConfig

logger = structlog.get_logger()

PRENOTAMI_URL = "https://prenotami.esteri.it"
IAM_LOGIN_DOMAIN = "iam.esteri.it"
SERVICES_PATH = "/Services"
BOOKING_PATH = "/Services/Ede"  # Citizenship by descent booking page

# Timeout constants
LOGIN_TIMEOUT = 30
LOGIN_MAX_RETRIES = 3
LOGIN_RETRY_DELAY = 5
PAGE_LOAD_TIMEOUT = 30
CALENDAR_LOAD_TIMEOUT = 60
ELEMENT_WAIT_TIMEOUT = 10


class BookingResult(Enum):
    """Outcome of a booking attempt."""

    SUCCESS = "success"
    NO_SLOTS = "no_slots"
    OTP_FAILED = "otp_failed"
    LOGIN_FAILED = "login_failed"
    CALENDAR_ERROR = "calendar_error"
    TIMEOUT = "timeout"
    ERROR = "error"


class BookingOutcome:
    """Result details from a booking attempt."""

    def __init__(
        self,
        result: BookingResult,
        *,
        appointment_date: str = "",
        appointment_time: str = "",
        message: str = "",
    ) -> None:
        self.result = result
        self.appointment_date = appointment_date
        self.appointment_time = appointment_time
        self.message = message


def login(driver: WebDriver, config: AppConfig, *, correlation_id: str) -> bool:
    """Login to the Prenot@mi portal via the iam.esteri.it OAuth flow.

    Retries up to LOGIN_MAX_RETRIES times to handle transient server errors
    (the /pingid callback is known to return 500 intermittently).

    The flow per attempt is:
    1. Load prenotami.esteri.it
    2. Click the "EFFETTUARE IL LOGIN" link → redirects to iam.esteri.it
    3. Fill username + password on the IAM login form
    4. Click submit → redirects back to prenotami.esteri.it/Services

    Args:
        driver: Selenium WebDriver instance.
        config: Application configuration with credentials.
        correlation_id: Correlation ID for logging.

    Returns:
        True if login successful, False otherwise.
    """
    for attempt in range(1, LOGIN_MAX_RETRIES + 1):
        logger.info(
            "login_attempt",
            correlation_id=correlation_id,
            attempt=attempt,
            max_retries=LOGIN_MAX_RETRIES,
        )
        success = _login_attempt(driver, config, correlation_id=correlation_id)
        if success:
            return True

        if attempt < LOGIN_MAX_RETRIES:
            logger.warning(
                "login_retrying",
                correlation_id=correlation_id,
                attempt=attempt,
                delay=LOGIN_RETRY_DELAY,
            )
            time.sleep(LOGIN_RETRY_DELAY)

    logger.error(
        "login_all_attempts_failed",
        correlation_id=correlation_id,
        attempts=LOGIN_MAX_RETRIES,
    )
    return False


def _login_attempt(
    driver: WebDriver, config: AppConfig, *, correlation_id: str
) -> bool:
    """Single login attempt via iam.esteri.it OAuth flow."""
    try:
        driver.get(PRENOTAMI_URL)
        wait = WebDriverWait(driver, LOGIN_TIMEOUT)

        # Step 1: Click the login link on the Prenot@mi homepage
        login_link = wait.until(
            ec.element_to_be_clickable((By.CSS_SELECTOR, "a.button.primary"))
        )
        logger.info("login_clicking_oauth_link", correlation_id=correlation_id)
        login_link.click()

        # Step 2: Wait for redirect to iam.esteri.it and fill the login form
        wait.until(ec.url_contains(IAM_LOGIN_DOMAIN))
        logger.info(
            "login_on_iam_page",
            correlation_id=correlation_id,
            url=driver.current_url,
        )

        email_field = wait.until(
            ec.presence_of_element_located((By.NAME, "callback_1"))
        )
        password_field = driver.find_element(By.NAME, "callback_2")

        # Clear and fill credentials
        email_field.clear()
        email_field.send_keys(config.prenotami.email)
        password_field.clear()
        password_field.send_keys(config.prenotami.password)

        # Click submit
        submit_btn = driver.find_element(By.CSS_SELECTOR, "button[type='submit']")
        submit_btn.click()

        # Step 3: Wait until the OAuth /pingid callback completes and the server
        # redirects us to a proper post-login page (/UserArea or /Services).
        # We specifically exclude /pingid itself because it can return 500 briefly
        # before redirecting, and a transient prenotami.esteri.it URL is not
        # sufficient to confirm a valid session.
        wait.until(
            lambda d: d.current_url.startswith(PRENOTAMI_URL)
            and not d.current_url.startswith(f"{PRENOTAMI_URL}/pingid")
        )

        logger.info("login_successful", correlation_id=correlation_id)
        return True

    except TimeoutException:
        logger.error(
            "login_timeout",
            correlation_id=correlation_id,
            current_url=driver.current_url,
        )
        return False
    except (NoSuchElementException, WebDriverException) as exc:
        logger.error(
            "login_error",
            correlation_id=correlation_id,
            error=str(type(exc).__name__),
        )
        return False


def trigger_otp_via_passport(driver: WebDriver, *, correlation_id: str) -> bool:
    """Trigger OTP generation via any available service booking page.

    This implements "Trick #1" from the guide: the OTP can be generated from
    any service booking form, so we try passport first (usually available), then
    fall back to any other enabled PRENOTA button.

    Some services show a "no slots" modal instead of loading the booking form.
    In that case we dismiss the modal and try the next service.

    Args:
        driver: Selenium WebDriver instance.
        correlation_id: Correlation ID for logging.

    Returns:
        True if OTP was triggered successfully, False otherwise.
    """
    logger.info("otp_trigger_started", correlation_id=correlation_id)

    try:
        driver.get(f"{PRENOTAMI_URL}{SERVICES_PATH}")
        wait = WebDriverWait(driver, PAGE_LOAD_TIMEOUT)
        wait.until(ec.presence_of_element_located((By.CSS_SELECTOR, ".card, table, .list-group")))
        time.sleep(2)

        # Preferred order: passport first, then any other service.
        # We re-fetch buttons after each modal dismiss to get fresh references.
        preferred = ["Passaporto", "Passport", "Carta d'identità", "Cittadinanza"]

        for service_name in preferred:
            btn = _find_service_book_button(driver, service_name)
            if btn and _try_otp_via_service(driver, btn, service_name, correlation_id=correlation_id):
                return True

        # Last resort: try every remaining enabled PRENOTA button
        try:
            all_buttons = driver.find_elements(By.CSS_SELECTOR, "button, a.btn")
            for btn in all_buttons:
                if "prenota" in btn.text.strip().lower() and btn.is_enabled():
                    if _try_otp_via_service(driver, btn, btn.text.strip(), correlation_id=correlation_id):
                        return True
        except (NoSuchElementException, WebDriverException):
            pass

        logger.error("otp_send_button_not_found", correlation_id=correlation_id)
        return False

    except (TimeoutException, WebDriverException) as exc:
        logger.error(
            "otp_trigger_error",
            correlation_id=correlation_id,
            error=str(type(exc).__name__),
        )
        return False


def _try_otp_via_service(
    driver: WebDriver,
    btn: object,
    service_name: str,
    *,
    correlation_id: str,
) -> bool:
    """Click a PRENOTA button and attempt to send an OTP from its booking form.

    Returns True if OTP was triggered, False if the service had no slots or
    the OTP button was not found.
    """
    services_url = f"{PRENOTAMI_URL}{SERVICES_PATH}"
    try:
        btn.click()  # type: ignore[union-attr]
        time.sleep(3)

        # If a "no slots" modal appeared, the URL stays on /Services.
        # Dismiss the modal and report failure for this service.
        if BOOKING_PATH not in driver.current_url:
            logger.info(
                "otp_service_no_slots_modal",
                correlation_id=correlation_id,
                service=service_name,
            )
            _dismiss_no_slots_modal(driver)
            time.sleep(1)
            return False

        # We're on the booking form — find and click the OTP send button.
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(1)
        otp_button = _find_otp_send_button(driver)
        if otp_button:
            otp_button.click()
            logger.info(
                "otp_triggered",
                correlation_id=correlation_id,
                service=service_name,
            )
            return True

        logger.warning(
            "otp_button_not_on_form",
            correlation_id=correlation_id,
            service=service_name,
        )
        # Navigate back to services for next attempt
        driver.get(services_url)
        time.sleep(2)
        return False

    except (NoSuchElementException, WebDriverException):
        # Element gone (e.g. stale ref after modal dismiss) — navigate back
        try:
            driver.get(services_url)
            time.sleep(2)
        except WebDriverException:
            pass
        return False


def _dismiss_no_slots_modal(driver: WebDriver) -> None:
    """Click OK on the 'no available slots' modal if present."""
    try:
        ok_buttons = driver.find_elements(By.CSS_SELECTOR, "button, .btn")
        for btn in ok_buttons:
            if btn.text.strip().upper() in ("OK", "CHIUDI", "CLOSE"):
                btn.click()
                return
        # Fallback: try any visible dialog confirm button
        driver.find_element(By.CSS_SELECTOR, "[data-dismiss='modal'], .modal .btn-primary").click()
    except (NoSuchElementException, WebDriverException):
        pass


def navigate_to_services(driver: WebDriver, *, correlation_id: str) -> bool:
    """Navigate back to the services list page.

    Args:
        driver: Selenium WebDriver instance.
        correlation_id: Correlation ID for logging.

    Returns:
        True if navigation successful.
    """
    try:
        driver.get(f"{PRENOTAMI_URL}{SERVICES_PATH}")
        wait = WebDriverWait(driver, PAGE_LOAD_TIMEOUT)
        wait.until(ec.presence_of_element_located((By.CSS_SELECTOR, ".card, table, .list-group")))
        time.sleep(1)
        logger.info("navigated_to_services", correlation_id=correlation_id)
        return True
    except (TimeoutException, WebDriverException):
        logger.error("navigate_to_services_failed", correlation_id=correlation_id)
        return False


def click_citizenship_booking(driver: WebDriver, *, correlation_id: str) -> bool:
    """Click the citizenship by descent booking button at the precise moment.

    This should be called at the exact release time (with the configured
    timing offset). The button will only work at/near release time.

    Args:
        driver: Selenium WebDriver instance.
        correlation_id: Correlation ID for logging.

    Returns:
        True if the booking page loaded, False otherwise.
    """
    logger.info(
        "clicking_citizenship_booking",
        correlation_id=correlation_id,
        timestamp=datetime.now(UTC).isoformat(),
    )

    try:
        # Find citizenship booking button.
        # Search most-specific first to avoid matching "Riacquisto della cittadinanza"
        # (also CITTADINANZA category, but has no PRENOTA button).
        citizenship_btn = (
            _find_service_book_button(driver, "discendenza")
            or _find_service_book_button(driver, "descent")
            or _find_service_book_button(driver, "Cittadinanza")
            or _find_service_book_button(driver, "Citizenship")
        )

        if not citizenship_btn:
            logger.error("citizenship_button_not_found", correlation_id=correlation_id)
            return False

        if not citizenship_btn.is_enabled():  # type: ignore[union-attr]
            logger.warning("citizenship_button_disabled", correlation_id=correlation_id)
            return False

        citizenship_btn.click()  # type: ignore[union-attr]

        # Check quickly if a "no slots" modal appeared (URL stays on /Services)
        time.sleep(3)
        if BOOKING_PATH not in driver.current_url:
            _dismiss_no_slots_modal(driver)
            logger.warning(
                "citizenship_no_slots_modal",
                correlation_id=correlation_id,
                url=driver.current_url,
            )
            return False

        # We're on the booking form — wait for it to fully load
        WebDriverWait(driver, PAGE_LOAD_TIMEOUT).until(
            ec.url_contains(BOOKING_PATH)
        )
        time.sleep(2)

        logger.info(
            "citizenship_page_loading",
            correlation_id=correlation_id,
            current_url=driver.current_url,
        )
        return True

    except (TimeoutException, WebDriverException) as exc:
        logger.error(
            "citizenship_booking_click_error",
            correlation_id=correlation_id,
            error=str(type(exc).__name__),
        )
        return False


def submit_otp_and_proceed(
    driver: WebDriver,
    otp_code: str,
    *,
    correlation_id: str,
) -> bool:
    """Submit the OTP code on the booking form and proceed.

    Pastes the OTP, checks the privacy checkbox, and clicks "Avanti" (Forward).

    Args:
        driver: Selenium WebDriver instance.
        otp_code: 6-digit OTP code to submit.
        correlation_id: Correlation ID for logging.

    Returns:
        True if submission was successful and calendar loaded.
    """
    logger.info("submitting_otp", correlation_id=correlation_id)

    try:
        # Scroll down to find OTP field (as per guide: "scroll all the way down ASAP")
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(0.5)

        # Find OTP input field
        otp_field = _find_otp_input_field(driver)
        if not otp_field:
            logger.error("otp_input_not_found", correlation_id=correlation_id)
            return False

        # Enter OTP code
        otp_field.clear()
        otp_field.send_keys(otp_code)

        # Check privacy checkbox ("Informativa sulla Privacy")
        privacy_checkbox = _find_privacy_checkbox(driver)
        if privacy_checkbox and not privacy_checkbox.is_selected():
            privacy_checkbox.click()

        # Click "Avanti" (Forward) button
        avanti_btn = _find_button_by_text(driver, "Avanti")
        if not avanti_btn:
            avanti_btn = _find_button_by_text(driver, "Forward")
        if not avanti_btn:
            # Try submit button as fallback
            avanti_btn = _find_button_by_text(driver, "Submit")

        if not avanti_btn:
            logger.error("avanti_button_not_found", correlation_id=correlation_id)
            return False

        avanti_btn.click()

        # Handle confirmation popup ("Are you sure" dialog)
        time.sleep(1)
        _handle_confirmation_popup(driver, correlation_id=correlation_id)

        logger.info("otp_submitted_successfully", correlation_id=correlation_id)
        return True

    except (NoSuchElementException, WebDriverException) as exc:
        logger.error(
            "otp_submission_error",
            correlation_id=correlation_id,
            error=str(type(exc).__name__),
        )
        return False


def navigate_calendar_and_book(
    driver: WebDriver,
    *,
    correlation_id: str,
    months_ahead: int = 3,
) -> BookingOutcome:
    """Navigate the calendar to find and book an available appointment.

    Follows the guide's advice: use right arrow to navigate months
    (NOT the dropdown), look for green dates, click and confirm.

    Args:
        driver: Selenium WebDriver instance.
        correlation_id: Correlation ID for logging.
        months_ahead: Number of months to skip forward in calendar.

    Returns:
        BookingOutcome with result details.
    """
    logger.info(
        "calendar_navigation_started",
        correlation_id=correlation_id,
        months_ahead=months_ahead,
    )

    try:
        # Wait for calendar to load (can be the longest wait per the guide)
        wait = WebDriverWait(driver, CALENDAR_LOAD_TIMEOUT)

        # Wait for either the calendar or a "no appointments" message
        time.sleep(3)  # Initial wait for page to settle

        # Check for error or "no appointments" message
        if _check_no_appointments(driver):
            logger.warning("no_appointments_available", correlation_id=correlation_id)
            return BookingOutcome(
                result=BookingResult.NO_SLOTS,
                message="No appointments available at this time",
            )

        # Wait for calendar widget to appear
        try:
            wait.until(
                ec.presence_of_element_located(
                    (By.CSS_SELECTOR, ".datepicker, .calendar, [class*='calendar'], table.table")
                )
            )
        except TimeoutException:
            logger.warning("calendar_not_loaded", correlation_id=correlation_id)
            # Try refreshing
            driver.refresh()
            time.sleep(5)
            if _check_no_appointments(driver):
                return BookingOutcome(
                    result=BookingResult.NO_SLOTS,
                    message="No appointments after refresh",
                )

        # Navigate forward by months using the right arrow
        # Per the guide: "DO NOT click the month dropdown"
        for i in range(months_ahead):
            next_btn = _find_calendar_next_button(driver)
            if next_btn:
                next_btn.click()
                time.sleep(1.5)  # Let calendar load
                logger.debug(
                    "calendar_month_advanced",
                    correlation_id=correlation_id,
                    month_skip=i + 1,
                )
            else:
                logger.warning(
                    "calendar_next_button_not_found",
                    correlation_id=correlation_id,
                    at_month=i,
                )
                break

        # Look for available (green) dates
        available_date = _find_available_date(driver)
        if not available_date:
            # Try one more month
            next_btn = _find_calendar_next_button(driver)
            if next_btn:
                next_btn.click()
                time.sleep(1.5)
                available_date = _find_available_date(driver)

        if not available_date:
            logger.warning("no_green_dates_found", correlation_id=correlation_id)
            return BookingOutcome(
                result=BookingResult.NO_SLOTS,
                message="No available (green) dates found in calendar",
            )

        # Click the available date
        available_date.click()
        time.sleep(2)

        # Get date text for logging
        date_text = available_date.text.strip()
        logger.info(
            "available_date_clicked",
            correlation_id=correlation_id,
            date=date_text,
        )

        # Wait for time slots to appear and click "Prenota" (Book)
        time.sleep(2)
        book_btn = _find_button_by_text(driver, "Prenota")
        if not book_btn:
            book_btn = _find_button_by_text(driver, "Book")
        if not book_btn:
            # Try finding any primary/submit button in the time slot area
            try:
                book_btn = driver.find_element(
                    By.CSS_SELECTOR, ".btn-primary, button[type='submit']"
                )
            except NoSuchElementException:
                pass

        if not book_btn:
            logger.error("book_button_not_found", correlation_id=correlation_id)
            return BookingOutcome(
                result=BookingResult.CALENDAR_ERROR,
                message="Could not find booking confirmation button",
            )

        book_btn.click()
        time.sleep(3)

        # Handle any final confirmation popup
        _handle_confirmation_popup(driver, correlation_id=correlation_id)
        time.sleep(2)

        # Check for success indicators
        page_source = driver.page_source.lower()
        if any(
            indicator in page_source
            for indicator in ["conferma", "confirmation", "appuntamento confermato", "confirmed"]
        ):
            # Try to extract appointment details
            appointment_date, appointment_time = _extract_appointment_details(driver)

            logger.info(
                "booking_successful",
                correlation_id=correlation_id,
                date=appointment_date,
                time=appointment_time,
            )

            return BookingOutcome(
                result=BookingResult.SUCCESS,
                appointment_date=appointment_date or date_text,
                appointment_time=appointment_time or "See confirmation page",
                message="Appointment booked successfully!",
            )

        # If we got here without clear success, check if it still looks like it worked
        logger.warning(
            "booking_result_uncertain",
            correlation_id=correlation_id,
            current_url=driver.current_url,
        )

        return BookingOutcome(
            result=BookingResult.SUCCESS,
            appointment_date=date_text,
            message="Booking appears successful - please verify in Prenot@mi",
        )

    except TimeoutException:
        logger.error("calendar_timeout", correlation_id=correlation_id)
        return BookingOutcome(
            result=BookingResult.TIMEOUT,
            message="Calendar page timed out",
        )
    except WebDriverException as exc:
        logger.warning(
            "calendar_error_retrying",
            correlation_id=correlation_id,
            error=str(type(exc).__name__),
        )
        # Per guide advice: refresh and retry once on browser errors
        try:
            driver.refresh()
            time.sleep(5)
            available_date = _find_available_date(driver)
            if available_date:
                available_date.click()
                time.sleep(2)
                book_btn = _find_button_by_text(driver, "Prenota")
                if not book_btn:
                    book_btn = _find_button_by_text(driver, "Book")
                if book_btn:
                    book_btn.click()
                    time.sleep(3)
                    _handle_confirmation_popup(driver, correlation_id=correlation_id)
                    time.sleep(2)
                    appointment_date, appointment_time = _extract_appointment_details(driver)
                    return BookingOutcome(
                        result=BookingResult.SUCCESS,
                        appointment_date=appointment_date or "",
                        appointment_time=appointment_time or "See confirmation page",
                        message="Appointment booked after calendar retry",
                    )
        except WebDriverException:
            pass

        logger.error(
            "calendar_error_retry_failed",
            correlation_id=correlation_id,
        )
        return BookingOutcome(
            result=BookingResult.CALENDAR_ERROR,
            message="Browser error during calendar navigation",
        )


# --- Private helper functions ---


def _find_service_book_button(driver: WebDriver, service_text: str) -> object | None:
    """Find a 'Prenota' (Book) button associated with a specific service.

    Searches the page for rows/cards containing the service text and
    returns the associated booking button.

    Args:
        driver: WebDriver instance.
        service_text: Text to match in the service name (e.g., "Passaporto").

    Returns:
        WebElement for the book button, or None if not found.
    """
    try:
        # Strategy 1: Look for table rows containing the service text
        rows = driver.find_elements(By.CSS_SELECTOR, "tr, .list-group-item, .card")
        for row in rows:
            if service_text.lower() in row.text.lower():
                buttons = row.find_elements(By.CSS_SELECTOR, "a.btn, button.btn, a[href*='Book']")
                for btn in buttons:
                    btn_text = btn.text.strip().lower()
                    if btn_text in ("prenota", "book", "") and btn.is_enabled():
                        return btn
                # If no labeled button, try any link/button
                links = row.find_elements(By.CSS_SELECTOR, "a, button")
                for link in links:
                    if ("prenota" in link.text.lower() or "book" in link.text.lower()) \
                            and link.is_enabled():
                        return link

        # Strategy 2: Look for buttons with data attributes or specific hrefs
        all_buttons = driver.find_elements(By.CSS_SELECTOR, "a.btn, button.btn")
        for btn in all_buttons:
            parent_text = btn.find_element(By.XPATH, "..").text.lower()
            if service_text.lower() in parent_text:
                return btn

    except (NoSuchElementException, WebDriverException):
        pass

    return None


def _find_otp_send_button(driver: WebDriver) -> object | None:
    """Find the 'Invia Nuovo Codice' (Send New Code) OTP button.

    Args:
        driver: WebDriver instance.

    Returns:
        WebElement for the OTP send button, or None.
    """
    search_texts = ["invia nuovo codice", "send new code", "invia codice", "send code", "otp"]

    try:
        buttons = driver.find_elements(By.CSS_SELECTOR, "button, a.btn, input[type='button']")
        for btn in buttons:
            btn_text = btn.text.strip().lower()
            for search in search_texts:
                if search in btn_text:
                    return btn
    except (NoSuchElementException, WebDriverException):
        pass

    return None


def _find_otp_input_field(driver: WebDriver) -> object | None:
    """Find the OTP code input field.

    Args:
        driver: WebDriver instance.

    Returns:
        WebElement for the OTP input field, or None.
    """
    selectors = [
        "input[name*='otp']",
        "input[name*='OTP']",
        "input[name*='codice']",
        "input[name*='code']",
        "input[id*='otp']",
        "input[id*='OTP']",
        "input[placeholder*='OTP']",
        "input[placeholder*='codice']",
        "input[placeholder*='code']",
        "input[type='text'][maxlength='6']",
        "input[type='number'][maxlength='6']",
    ]

    for selector in selectors:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
            if elements:
                return elements[0]
        except (NoSuchElementException, WebDriverException):
            continue

    # Fallback: find any visible text input near OTP-related text
    try:
        inputs = driver.find_elements(By.CSS_SELECTOR, "input[type='text'], input[type='number']")
        for inp in inputs:
            if inp.is_displayed():
                parent = inp.find_element(By.XPATH, "..")
                parent_text = parent.text.lower()
                if any(kw in parent_text for kw in ("otp", "codice", "code", "verifica")):
                    return inp
    except (NoSuchElementException, WebDriverException):
        pass

    return None


def _find_privacy_checkbox(driver: WebDriver) -> object | None:
    """Find the privacy/terms checkbox.

    Args:
        driver: WebDriver instance.

    Returns:
        WebElement for the checkbox, or None.
    """
    try:
        checkboxes = driver.find_elements(By.CSS_SELECTOR, "input[type='checkbox']")
        for cb in checkboxes:
            parent = cb.find_element(By.XPATH, "..")
            parent_text = parent.text.lower()
            if any(kw in parent_text for kw in ("privacy", "informativa", "accetto", "accept")):
                return cb
        # Return first visible checkbox as fallback
        for cb in checkboxes:
            if cb.is_displayed():
                return cb
    except (NoSuchElementException, WebDriverException):
        pass

    return None


def _find_button_by_text(driver: WebDriver, text: str) -> object | None:
    """Find a button by its text content.

    Args:
        driver: WebDriver instance.
        text: Text to search for (case-insensitive).

    Returns:
        WebElement for the button, or None.
    """
    try:
        buttons = driver.find_elements(
            By.CSS_SELECTOR, "button, a.btn, input[type='submit'], input[type='button']"
        )
        for btn in buttons:
            if text.lower() in btn.text.strip().lower():
                if btn.is_displayed():
                    return btn
    except (NoSuchElementException, WebDriverException):
        pass

    return None


def _handle_confirmation_popup(driver: WebDriver, *, correlation_id: str) -> None:
    """Handle confirmation popup dialogs (e.g., 'Are you sure?').

    Args:
        driver: WebDriver instance.
        correlation_id: Correlation ID for logging.
    """
    try:
        # Check for JavaScript alert
        alert = driver.switch_to.alert
        logger.info(
            "confirmation_popup_found",
            correlation_id=correlation_id,
            text=alert.text[:100],
        )
        alert.accept()
        return
    except Exception:
        pass

    # Check for modal dialog
    confirm_texts = ["ok", "si", "yes", "conferma", "confirm", "accetta", "accept"]
    try:
        modal_buttons = driver.find_elements(
            By.CSS_SELECTOR, ".modal button, .modal a.btn, .swal2-confirm, .bootbox-accept"
        )
        for btn in modal_buttons:
            btn_text = btn.text.strip().lower()
            if btn_text in confirm_texts and btn.is_displayed():
                btn.click()
                logger.info(
                    "modal_confirmed",
                    correlation_id=correlation_id,
                    button_text=btn_text,
                )
                return
    except (NoSuchElementException, WebDriverException):
        pass


def _find_calendar_next_button(driver: WebDriver) -> object | None:
    """Find the calendar's 'next month' navigation button.

    Args:
        driver: WebDriver instance.

    Returns:
        WebElement for the next button, or None.
    """
    selectors = [
        ".datepicker .next",
        ".calendar .next",
        "button[aria-label='Next']",
        "button[aria-label='next']",
        ".fc-next-button",
        "[class*='next']",
        "[class*='right']",
        "th.next",
        ".datepicker-days th.next",
    ]

    for selector in selectors:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
            for el in elements:
                if el.is_displayed():
                    return el
        except (NoSuchElementException, WebDriverException):
            continue

    # Fallback: find right arrow character or icon
    try:
        buttons = driver.find_elements(By.CSS_SELECTOR, "button, th, a, span")
        for btn in buttons:
            text = btn.text.strip()
            if text in ("›", "»", ">", "→") and btn.is_displayed():
                return btn
    except (NoSuchElementException, WebDriverException):
        pass

    return None


def _find_available_date(driver: WebDriver) -> object | None:
    """Find an available (green) date in the calendar.

    Available dates are typically styled with green background or
    a specific CSS class.

    Args:
        driver: WebDriver instance.

    Returns:
        WebElement for the first available date, or None.
    """
    # Common CSS classes/styles for available dates
    selectors = [
        "td.day.available",
        "td.active:not(.disabled)",
        "td[style*='green']",
        "td.green",
        "td[class*='available']",
        "td[class*='free']",
        ".day:not(.disabled):not(.old):not(.new)",
    ]

    for selector in selectors:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
            for el in elements:
                if el.is_displayed():
                    # Check if it looks available (green-ish background)
                    bg_color = el.value_of_css_property("background-color")
                    classes = el.get_attribute("class") or ""

                    # Accept if it's clearly an available/green date
                    is_green = "green" in bg_color.lower() or _is_green_rgb(bg_color)
                    is_available_class = any(
                        c in classes.lower() for c in ("available", "free", "active", "success")
                    )
                    is_disabled = any(
                        c in classes.lower() for c in ("disabled", "grey", "gray", "past")
                    )

                    if (is_green or is_available_class) and not is_disabled:
                        return el
        except (NoSuchElementException, WebDriverException):
            continue

    # Broader fallback: find any clickable, non-disabled calendar day
    try:
        days = driver.find_elements(By.CSS_SELECTOR, "td.day, td[data-date]")
        for day in days:
            classes = (day.get_attribute("class") or "").lower()
            if not any(c in classes for c in ("disabled", "old", "grey", "past")):
                bg_color = day.value_of_css_property("background-color")
                if _is_green_rgb(bg_color):
                    return day
    except (NoSuchElementException, WebDriverException):
        pass

    return None


def _is_green_rgb(color_str: str) -> bool:
    """Check if an RGB color string represents a green color.

    Args:
        color_str: CSS color value like 'rgba(0, 128, 0, 1)' or 'rgb(0, 128, 0)'.

    Returns:
        True if the color is green-ish.
    """
    try:
        # Extract RGB values
        color_str = color_str.replace("rgba", "").replace("rgb", "")
        color_str = color_str.strip("()")
        parts = [int(p.strip()) for p in color_str.split(",")[:3]]
        if len(parts) >= 3:
            r, g, b = parts[0], parts[1], parts[2]
            # Green if G channel is dominant
            return g > 80 and g > r * 1.5 and g > b * 1.5
    except (ValueError, IndexError):
        pass
    return False


def _check_no_appointments(driver: WebDriver) -> bool:
    """Check if the page shows a 'no appointments available' message.

    Args:
        driver: WebDriver instance.

    Returns:
        True if no appointments are available.
    """
    no_appt_phrases = [
        "non ci sono date disponibili",
        "no dates available",
        "nessun appuntamento",
        "no appointment",
        "non disponibile",
        "not available",
        "fully booked",
        "completo",
    ]

    try:
        page_text = driver.page_source.lower()
        return any(phrase in page_text for phrase in no_appt_phrases)
    except WebDriverException:
        return False


def _extract_appointment_details(driver: WebDriver) -> tuple[str, str]:
    """Extract appointment date and time from confirmation page.

    Args:
        driver: WebDriver instance.

    Returns:
        Tuple of (date_string, time_string). Empty strings if not found.
    """
    try:
        page_text = driver.find_element(By.TAG_NAME, "body").text
        # Simple extraction - look for date-like patterns
        import re

        date_match = re.search(r"(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4})", page_text)
        time_match = re.search(r"(\d{1,2}:\d{2})", page_text)

        date_str = date_match.group(1) if date_match else ""
        time_str = time_match.group(1) if time_match else ""

        return date_str, time_str
    except (NoSuchElementException, WebDriverException):
        return "", ""
