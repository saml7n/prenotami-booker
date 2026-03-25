"""Main orchestrator that coordinates the full booking flow."""

from __future__ import annotations

import time
from datetime import UTC, datetime

import structlog

from prenotami_booker.booker import (
    BookingOutcome,
    BookingResult,
    click_citizenship_booking,
    login,
    navigate_calendar_and_book,
    navigate_to_services,
    submit_otp_and_proceed,
    trigger_otp_via_passport,
)
from prenotami_booker.browser import close_driver, create_driver
from prenotami_booker.config import AppConfig
from prenotami_booker.logging_config import generate_correlation_id
from prenotami_booker.notifications import send_failure_notification, send_success_notification
from prenotami_booker.otp import fetch_otp_from_email
from prenotami_booker.timing import get_next_release_time, wait_until

logger = structlog.get_logger()

MAX_RETRY_ATTEMPTS = 2


def run_booking_attempt(config: AppConfig, *, correlation_id: str | None = None) -> BookingOutcome:
    """Execute a single booking attempt through the full flow.

    Orchestrates:
    1. Browser setup and login
    2. OTP generation via passport page trick
    3. OTP retrieval from email
    4. Precise timing wait
    5. Citizenship booking click
    6. OTP submission
    7. Calendar navigation and booking

    Args:
        config: Full application configuration.
        correlation_id: Optional correlation ID. Generated if not provided.

    Returns:
        BookingOutcome with the result of the attempt.
    """
    cid = correlation_id or generate_correlation_id()
    driver = None

    logger.info(
        "booking_attempt_started",
        correlation_id=cid,
        consulate=config.consulate.name,
        service=config.consulate.service_type,
    )

    try:
        # Step 1: Create browser and login
        driver = create_driver(config.browser, correlation_id=cid)

        if not login(driver, config, correlation_id=cid):
            return _fail(config, cid, BookingResult.LOGIN_FAILED, "Could not login to Prenot@mi")

        # Step 2: Calculate timing
        release_time = get_next_release_time(config.consulate, correlation_id=cid)
        otp_trigger_offset = config.consulate.otp_prefetch_seconds

        # Step 3: Wait until OTP trigger time (~2 min before release)
        seconds_to_release = (release_time - datetime.now(UTC)).total_seconds()

        if seconds_to_release > otp_trigger_offset:
            logger.info(
                "waiting_for_otp_trigger_time",
                correlation_id=cid,
                seconds_until_otp_trigger=round(seconds_to_release - otp_trigger_offset, 1),
            )
            wait_until(
                release_time,
                correlation_id=cid,
                offset_seconds=-otp_trigger_offset,
            )

        # Step 4: Trigger OTP via passport page (Trick #1)
        if not trigger_otp_via_passport(driver, correlation_id=cid):
            logger.warning("otp_trigger_failed_retrying", correlation_id=cid)
            time.sleep(2)
            if not trigger_otp_via_passport(driver, correlation_id=cid):
                return _fail(
                    config,
                    cid,
                    BookingResult.OTP_FAILED,
                    "Could not trigger OTP from passport page",
                )

        # Step 5: Retrieve OTP from email
        otp_code = fetch_otp_from_email(config.email, correlation_id=cid)
        if not otp_code:
            return _fail(config, cid, BookingResult.OTP_FAILED, "Could not retrieve OTP from email")

        logger.info("otp_retrieved", correlation_id=cid)

        # Step 6: Navigate back to services page and prepare
        if not navigate_to_services(driver, correlation_id=cid):
            return _fail(config, cid, BookingResult.ERROR, "Could not navigate to services page")

        # Step 7: Wait for precise release time (Trick #2)
        seconds_remaining = (release_time - datetime.now(UTC)).total_seconds()
        if seconds_remaining > 0:
            logger.info(
                "waiting_for_release_time",
                correlation_id=cid,
                seconds_remaining=round(seconds_remaining, 1),
            )
            wait_until(
                release_time,
                correlation_id=cid,
                offset_seconds=config.consulate.timing_offset_seconds,
            )

        # Step 8: Click citizenship booking button at the precise moment
        if not click_citizenship_booking(driver, correlation_id=cid):
            # Retry once
            logger.warning("citizenship_click_failed_retrying", correlation_id=cid)
            time.sleep(2)
            navigate_to_services(driver, correlation_id=cid)
            if not click_citizenship_booking(driver, correlation_id=cid):
                return _fail(
                    config, cid, BookingResult.ERROR, "Could not access citizenship booking page"
                )

        # Step 9: Submit OTP and proceed
        if not submit_otp_and_proceed(driver, otp_code, correlation_id=cid):
            return _fail(
                config, cid, BookingResult.OTP_FAILED, "OTP submission failed on citizenship page"
            )

        # Step 10: Navigate calendar and book
        outcome = navigate_calendar_and_book(
            driver,
            correlation_id=cid,
            months_ahead=config.consulate.calendar_months_ahead,
        )

        # Send appropriate notification
        if outcome.result == BookingResult.SUCCESS:
            send_success_notification(
                config.notification,
                correlation_id=cid,
                appointment_date=outcome.appointment_date,
                appointment_time=outcome.appointment_time,
                consulate=config.consulate.name,
            )
            logger.info(
                "booking_attempt_succeeded",
                correlation_id=cid,
                date=outcome.appointment_date,
                time=outcome.appointment_time,
            )
        else:
            send_failure_notification(
                config.notification,
                correlation_id=cid,
                reason=outcome.message,
                consulate=config.consulate.name,
            )
            logger.warning(
                "booking_attempt_unsuccessful",
                correlation_id=cid,
                result=outcome.result.value,
                message=outcome.message,
            )

        return outcome

    except Exception as exc:
        logger.error(
            "booking_attempt_unexpected_error",
            correlation_id=cid,
            error=str(type(exc).__name__),
        )
        send_failure_notification(
            config.notification,
            correlation_id=cid,
            reason="Unexpected error during booking attempt",
            consulate=config.consulate.name,
        )
        return BookingOutcome(
            result=BookingResult.ERROR,
            message="Unexpected error occurred",
        )
    finally:
        close_driver(driver, correlation_id=cid)


def run_scheduled(config: AppConfig) -> None:
    """Run the booker on schedule, waiting for release windows.

    Continuously monitors for the next release window and attempts
    to book when the time comes. Runs indefinitely until stopped
    or a booking succeeds.

    Args:
        config: Full application configuration.
    """
    cid = generate_correlation_id()
    logger.info(
        "scheduled_mode_started",
        correlation_id=cid,
        consulate=config.consulate.name,
        release_days=config.consulate.release_days,
        release_time=config.consulate.release_time,
    )

    while True:
        attempt_cid = generate_correlation_id()

        try:
            release_time = get_next_release_time(config.consulate, correlation_id=attempt_cid)
            seconds_until = (release_time - datetime.now(UTC)).total_seconds()

            # Login early: 10 minutes before release
            login_time_seconds = seconds_until - 600
            if login_time_seconds > 0:
                logger.info(
                    "waiting_for_next_release",
                    correlation_id=attempt_cid,
                    release_time=release_time.isoformat(),
                    hours_until=round(seconds_until / 3600, 1),
                )
                time.sleep(max(0, login_time_seconds))

            # Run the booking attempt
            outcome = run_booking_attempt(config, correlation_id=attempt_cid)

            if outcome.result == BookingResult.SUCCESS:
                logger.info(
                    "booking_succeeded_stopping_scheduler",
                    correlation_id=attempt_cid,
                    date=outcome.appointment_date,
                )
                break

            logger.info(
                "attempt_finished_waiting_for_next",
                correlation_id=attempt_cid,
                result=outcome.result.value,
            )

            # Wait a bit before calculating next release to avoid tight loops
            time.sleep(60)

        except KeyboardInterrupt:
            logger.info("scheduler_stopped_by_user", correlation_id=attempt_cid)
            break
        except Exception as exc:
            logger.error(
                "scheduler_error",
                correlation_id=attempt_cid,
                error=str(type(exc).__name__),
            )
            time.sleep(300)  # Wait 5 minutes on unexpected errors


def _fail(
    config: AppConfig,
    correlation_id: str,
    result: BookingResult,
    message: str,
) -> BookingOutcome:
    """Create a failure outcome and send notification.

    Args:
        config: App configuration for notifications.
        correlation_id: Correlation ID for logging.
        result: The type of failure.
        message: Human-readable failure description.

    Returns:
        BookingOutcome representing the failure.
    """
    send_failure_notification(
        config.notification,
        correlation_id=correlation_id,
        reason=message,
        consulate=config.consulate.name,
    )
    return BookingOutcome(result=result, message=message)
