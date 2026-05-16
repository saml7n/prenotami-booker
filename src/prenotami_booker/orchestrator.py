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
from prenotami_booker.timing import get_next_release_time, get_ntp_offset, wait_until

logger = structlog.get_logger()

MAX_RETRY_ATTEMPTS = 2

# ── Stage reporting helpers ────────────────────────────────────────────────────
_SEP = "━" * 62


def _stage_start(cid: str, n: int, total: int, name: str) -> None:
    logger.info(_SEP, correlation_id=cid)
    logger.info(f"STAGE {n}/{total}: {name}", correlation_id=cid)


def _stage_ok(cid: str, name: str, **kw: object) -> None:
    logger.info(f"✓ {name}", correlation_id=cid, **kw)


def _stage_err(cid: str, name: str, reason: str, exc: BaseException | None = None) -> None:
    if exc is not None:
        logger.error(
            f"✗ {name}",
            correlation_id=cid,
            reason=reason,
            error_type=type(exc).__name__,
            error_detail=str(exc),
            exc_info=exc,
        )
    else:
        logger.error(f"✗ {name}", correlation_id=cid, reason=reason)


def _run_summary(cid: str, completed: list[str], failed_at: str | None, outcome_msg: str) -> None:
    logger.info(_SEP, correlation_id=cid)
    logger.info("RUN SUMMARY", correlation_id=cid)
    for stage in completed:
        logger.info(f"  ✓ {stage}", correlation_id=cid)
    if failed_at:
        logger.error(f"  ✗ {failed_at} ← FAILED HERE", correlation_id=cid)
    logger.info(f"  OUTCOME: {outcome_msg}", correlation_id=cid)
    logger.info(_SEP, correlation_id=cid)


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
    completed: list[str] = []
    failed_at: str | None = None
    outcome_msg = "unknown"

    logger.info(_SEP, correlation_id=cid)
    logger.info(
        "BOOKING ATTEMPT STARTED",
        correlation_id=cid,
        consulate=config.consulate.name,
        service=config.consulate.service_type,
        release_days=config.consulate.release_days,
        release_time=config.consulate.release_time,
    )

    try:
        # ── Stage 1: Browser setup & login ───────────────────────────────────
        _stage_start(cid, 1, 7, "Browser setup & login")
        driver = create_driver(config.browser, correlation_id=cid)

        if not login(driver, config, correlation_id=cid):
            failed_at = "Login"
            _stage_err(cid, "Login", "Authentication failed — check credentials in config.yaml")
            return _fail(config, cid, BookingResult.LOGIN_FAILED, "Could not login to Prenot@mi")
        _stage_ok(cid, "Login", url=driver.current_url)
        completed.append("Login")

        # ── Stage 2: NTP clock sync ──────────────────────────────────────────
        _stage_start(cid, 2, 7, "NTP clock sync")
        ntp_offset = get_ntp_offset(correlation_id=cid)
        _stage_ok(cid, "NTP sync", clock_offset_ms=round(ntp_offset * 1000, 1))
        completed.append("NTP sync")

        # ── Stage 3: Calculate release timing ────────────────────────────────
        _stage_start(cid, 3, 7, "Timing calculation")
        release_time = get_next_release_time(config.consulate, correlation_id=cid)
        otp_trigger_offset = config.consulate.otp_prefetch_seconds
        seconds_to_release = (release_time - datetime.now(UTC)).total_seconds()
        _stage_ok(
            cid,
            "Timing calculated",
            release_time=release_time.isoformat(),
            hours_until=round(seconds_to_release / 3600, 2),
        )
        completed.append("Timing calculated")

        # ── Stage 4: Wait for OTP pre-fetch window ───────────────────────────
        if seconds_to_release > otp_trigger_offset:
            _stage_start(cid, 4, 7, "Waiting for OTP pre-fetch window")
            wait_minutes = round((seconds_to_release - otp_trigger_offset) / 60, 1)
            logger.info(
                "waiting_for_otp_window",
                correlation_id=cid,
                minutes_until_otp_trigger=wait_minutes,
            )
            wait_until(
                release_time,
                correlation_id=cid,
                offset_seconds=-otp_trigger_offset,
                ntp_offset=ntp_offset,
            )
            _stage_ok(cid, "OTP pre-fetch window reached")
        completed.append("OTP window")

        # ── Stage 5: Trigger OTP email ───────────────────────────────────────
        _stage_start(cid, 5, 7, "OTP generation (Trick #1 — passport page)")
        if not trigger_otp_via_passport(driver, correlation_id=cid):
            logger.warning("otp_trigger_failed_retrying_once", correlation_id=cid)
            time.sleep(2)
            if not trigger_otp_via_passport(driver, correlation_id=cid):
                failed_at = "OTP generation"
                _stage_err(
                    cid,
                    "OTP generation",
                    "All services showed 'posti esauriti' modal — no slots available to generate OTP",
                )
                return _fail(
                    config, cid, BookingResult.OTP_FAILED,
                    "Could not trigger OTP from passport page",
                )
        _stage_ok(cid, "OTP triggered", note="Check email for 6-digit code")
        completed.append("OTP generation")

        # ── Stage 6: Retrieve OTP from inbox ─────────────────────────────────
        _stage_start(cid, 6, 7, "OTP retrieval from email (IMAP)")
        otp_code = fetch_otp_from_email(config.email, correlation_id=cid)
        if not otp_code:
            failed_at = "OTP retrieval"
            _stage_err(
                cid,
                "OTP retrieval",
                "No OTP email found in inbox within timeout — check IMAP credentials",
            )
            return _fail(
                config, cid, BookingResult.OTP_FAILED, "Could not retrieve OTP from email"
            )
        _stage_ok(cid, "OTP retrieved", code_length=len(otp_code))
        completed.append("OTP retrieval")

        # ── Navigate back to services and wait for release ───────────────────
        if not navigate_to_services(driver, correlation_id=cid):
            failed_at = "Navigate to services"
            _stage_err(cid, "Navigate to services", "Could not load /Services page")
            return _fail(config, cid, BookingResult.ERROR, "Could not navigate to services page")

        seconds_remaining = (release_time - datetime.now(UTC)).total_seconds()
        if seconds_remaining > 0:
            logger.info(
                "waiting_for_exact_release_time",
                correlation_id=cid,
                seconds_remaining=round(seconds_remaining, 1),
            )
            wait_until(
                release_time,
                correlation_id=cid,
                offset_seconds=config.consulate.timing_offset_seconds,
                ntp_offset=ntp_offset,
            )

        # ── Stage 7: Click citizenship at release moment ──────────────────────
        _stage_start(cid, 7, 7, "Citizenship button click + OTP submit + calendar booking")
        if not click_citizenship_booking(driver, correlation_id=cid):
            logger.warning("citizenship_click_failed_retrying", correlation_id=cid)
            time.sleep(2)
            navigate_to_services(driver, correlation_id=cid)
            if not click_citizenship_booking(driver, correlation_id=cid):
                failed_at = "Citizenship click"
                _stage_err(
                    cid,
                    "Citizenship click",
                    "Booking page did not load — button may still be showing 'posti esauriti'",
                )
                return _fail(
                    config, cid, BookingResult.ERROR, "Could not access citizenship booking page"
                )
        logger.info("citizenship_page_loaded", correlation_id=cid)
        completed.append("Citizenship click")

        if not submit_otp_and_proceed(driver, otp_code, correlation_id=cid):
            failed_at = "OTP submission"
            _stage_err(
                cid,
                "OTP submission",
                "Could not enter OTP or click Avanti — form elements not found or form rejected OTP",
            )
            return _fail(
                config, cid, BookingResult.OTP_FAILED, "OTP submission failed on citizenship page"
            )
        logger.info("otp_submitted", correlation_id=cid)
        completed.append("OTP submission")

        outcome = navigate_calendar_and_book(
            driver,
            correlation_id=cid,
            months_ahead=config.consulate.calendar_months_ahead,
        )

        if outcome.result == BookingResult.SUCCESS:
            outcome_msg = f"BOOKED ✓ — {outcome.appointment_date} {outcome.appointment_time}"
            _stage_ok(
                cid,
                "Appointment booked!",
                date=outcome.appointment_date,
                time=outcome.appointment_time,
                consulate=config.consulate.name,
            )
            completed.append("Calendar & booking")
            send_success_notification(
                config.notification,
                correlation_id=cid,
                appointment_date=outcome.appointment_date,
                appointment_time=outcome.appointment_time,
                consulate=config.consulate.name,
            )
        else:
            failed_at = "Calendar & booking"
            outcome_msg = f"No booking — {outcome.result.value}: {outcome.message}"
            _stage_err(cid, "Calendar & booking", outcome.message or outcome.result.value)
            send_failure_notification(
                config.notification,
                correlation_id=cid,
                reason=outcome.message,
                consulate=config.consulate.name,
            )

        return outcome

    except Exception as exc:
        failed_at = failed_at or "Unexpected error"
        outcome_msg = f"CRASHED — {type(exc).__name__}: {exc}"
        _stage_err(cid, "Unexpected error", str(exc), exc=exc)
        send_failure_notification(
            config.notification,
            correlation_id=cid,
            reason=f"Unexpected {type(exc).__name__}: {exc}",
            consulate=config.consulate.name,
        )
        return BookingOutcome(result=BookingResult.ERROR, message="Unexpected error occurred")

    finally:
        _run_summary(cid, completed, failed_at, outcome_msg)
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
                error_type=type(exc).__name__,
                error_detail=str(exc),
                exc_info=exc,
                note="Will retry in 5 minutes",
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
