"""Step-by-step verification of all booking flow stages.

Runs each stage in sequence and reports PASS/FAIL clearly.
Goes all the way through — including a real booking if slots are available.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# Load .env before importing config
from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).parent / "src"))

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as ec
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import TimeoutException, NoSuchElementException

from prenotami_booker.browser import create_driver, close_driver
from prenotami_booker.config import load_config
from prenotami_booker.booker import (
    PRENOTAMI_URL, SERVICES_PATH, IAM_LOGIN_DOMAIN, PAGE_LOAD_TIMEOUT,
    login, trigger_otp_via_passport, navigate_to_services,
    click_citizenship_booking, submit_otp_and_proceed, navigate_calendar_and_book,
    BookingResult,
    _find_service_book_button, _find_otp_input_field, _find_privacy_checkbox,
    _find_button_by_text, _find_calendar_next_button,
)
from prenotami_booker.otp import fetch_otp_from_email
from prenotami_booker.notifications import send_success_notification

SCREENSHOTS_DIR = Path("/tmp/verify_screenshots")
SCREENSHOTS_DIR.mkdir(exist_ok=True)

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

results: list[tuple[str, str, str]] = []  # (step, status, detail)

def step(n: int, name: str) -> None:
    print(f"\n{BOLD}{CYAN}Step {n}: {name}{RESET}")

def passed(name: str, detail: str = "") -> None:
    msg = f"  {GREEN}✓ PASS{RESET}  {detail}"
    print(msg)
    results.append((name, "PASS", detail))

def failed(name: str, detail: str = "") -> None:
    msg = f"  {RED}✗ FAIL{RESET}  {detail}"
    print(msg)
    results.append((name, "FAIL", detail))

def skipped(name: str, reason: str = "") -> None:
    msg = f"  {YELLOW}~ SKIP{RESET}  {reason}"
    print(msg)
    results.append((name, "SKIP", reason))

def screenshot(driver, name: str) -> None:
    path = SCREENSHOTS_DIR / f"{name}.png"
    driver.save_screenshot(str(path))
    print(f"         Screenshot: {path}")

# ── Load config ────────────────────────────────────────────────────────────────
print(f"\n{BOLD}Loading configuration...{RESET}")
config = load_config()
print(f"  Prenotami email : {config.prenotami.email}")
print(f"  IMAP email      : {config.email.email}")
print(f"  Consulate       : {config.consulate.name}")
CID = "verify-001"

driver = None
otp_code: str | None = None

try:
    # ── Step 1: Login ──────────────────────────────────────────────────────────
    step(1, "Login (~10 min before release)")
    driver = create_driver(config.browser, correlation_id=CID)
    ok = login(driver, config, correlation_id=CID)
    if ok:
        screenshot(driver, "01_login_success")
        passed("Login", f"Landed on {driver.current_url}")
    else:
        screenshot(driver, "01_login_failed")
        failed("Login", f"Current URL: {driver.current_url}")
        print(f"\n{RED}Cannot continue without login. Aborting.{RESET}")
        sys.exit(1)

    # ── Step 2: OTP Generation ─────────────────────────────────────────────────
    step(2, "OTP Generation (~2 min before release) — will send a real OTP email")
    ok = trigger_otp_via_passport(driver, correlation_id=CID)
    if ok:
        screenshot(driver, "02_otp_triggered")
        passed("OTP Generation", "Passport page OTP trigger clicked")
    else:
        screenshot(driver, "02_otp_failed")
        failed("OTP Generation", "Could not trigger OTP from passport page")

    # ── Step 3: OTP Retrieval ──────────────────────────────────────────────────
    step(3, "OTP Retrieval (polling IMAP inbox)")
    if results[-1][1] == "FAIL":
        skipped("OTP Retrieval", "Step 2 failed — no OTP email to fetch")
    else:
        print("  Polling email for up to 60 seconds...")
        otp_code = fetch_otp_from_email(config.email, correlation_id=CID)
        if otp_code:
            passed("OTP Retrieval", f"Got OTP code: {otp_code}")
        else:
            failed("OTP Retrieval", "No OTP found in inbox within 60s")

    # ── Step 4: Navigate to Services ──────────────────────────────────────────
    step(4, "Navigate to Services page")
    ok = navigate_to_services(driver, correlation_id=CID)
    if ok:
        screenshot(driver, "04_services")
        passed("Navigate to Services", driver.current_url)
    else:
        screenshot(driver, "04_services_failed")
        failed("Navigate to Services", "Could not load /Services page")

    # ── Step 5: Citizenship button findable ───────────────────────────────────
    step(5, "Precise Click — verify citizenship 'Prenota' button is findable")
    citizenship_btn = (
        _find_service_book_button(driver, "Cittadinanza")
        or _find_service_book_button(driver, "Citizenship")
        or _find_service_book_button(driver, "discendenza")
        or _find_service_book_button(driver, "descent")
    )
    if citizenship_btn:
        is_enabled = citizenship_btn.is_enabled()
        screenshot(driver, "05_citizenship_btn")
        passed(
            "Citizenship button found",
            f"Button enabled={is_enabled} "
            f"(outside release window it is {'disabled — normal' if not is_enabled else 'enabled!'})"
        )
        # Click the citizenship button if enabled, otherwise just report found/disabled
        if not is_enabled:
            passed(
                "Precise Click (actual)",
                "Button found but disabled outside release window — correct behaviour",
            )
            # Mark Steps 6-8 as skipped: no booking form to reach
            for s_name in ("Form Submission", "Calendar Navigation", "Booking"):
                skipped(s_name, "Citizenship button disabled — not at release window")
        else:
            print(f"  {YELLOW}Button is ENABLED — clicking to attempt booking!{RESET}")
            ok = click_citizenship_booking(driver, correlation_id=CID)
            if ok:
                screenshot(driver, "05_citizenship_clicked")
                passed("Precise Click (actual)", f"Landed on {driver.current_url}")
            else:
                screenshot(driver, "05_citizenship_click_failed")
                failed("Precise Click (actual)", "click_citizenship_booking returned False")

            # ── Step 6: Form Submission ──────────────────────────────────────
            step(6, "Form Submission — OTP + privacy checkbox + Avanti")
            if results[-1][1] == "FAIL":
                skipped("Form Submission", "Step 5 failed — not on booking form")
                skipped("Calendar Navigation", "Step 5 failed")
                skipped("Booking", "Step 5 failed")
            elif not otp_code:
                failed("Form Submission", "No OTP code retrieved in Step 3")
                skipped("Calendar Navigation", "No OTP")
                skipped("Booking", "No OTP")
            else:
                print(f"  Entering OTP: {otp_code}")
                ok = submit_otp_and_proceed(driver, otp_code, correlation_id=CID)
                if ok:
                    screenshot(driver, "06_otp_submitted")
                    passed("Form Submission", "OTP entered, privacy checked, Avanti clicked")
                else:
                    screenshot(driver, "06_form_submit_failed")
                    failed("Form Submission", "submit_otp_and_proceed returned False")

                # ── Step 7 & 8: Calendar + Booking ──────────────────────────
                step(7, "Calendar Navigation + Booking")
                if results[-1][1] == "FAIL":
                    skipped("Calendar Navigation", "Step 6 failed")
                    skipped("Booking", "Step 6 failed")
                else:
                    outcome = navigate_calendar_and_book(driver, correlation_id=CID)
                    screenshot(driver, "07_calendar_booking")
                    if outcome.result == BookingResult.SUCCESS:
                        passed("Calendar Navigation + Booking", f"BOOKED: {outcome.message}")
                        skipped("Booking", "Combined with Step 7")
                    elif outcome.result == BookingResult.NO_SLOTS:
                        passed("Calendar Navigation", "Calendar navigated successfully")
                        failed("Booking", f"No slots found: {outcome.message}")
                    else:
                        screenshot(driver, "07_booking_failed")
                        failed("Calendar Navigation + Booking", str(outcome.message))
                        skipped("Booking", "Combined with Step 7")
    else:
        screenshot(driver, "05_no_citizenship_btn")
        failed("Citizenship button found", "Could not find citizenship Prenota button on /Services")
        for s_name in ("Precise Click (actual)", "Form Submission", "Calendar Navigation", "Booking"):
            skipped(s_name, "Step 5 failed — citizenship button not found")

    # ── Step 9: Notification ──────────────────────────────────────────────────
    step(9, "Notification — send test email")
    ok = send_success_notification(
        config.notification,
        correlation_id=CID,
        appointment_date="TEST DATE (verification run)",
        appointment_time="TEST TIME",
        consulate=config.consulate.name,
    )
    if ok:
        passed("Notification", f"Test email sent to {config.notification.notify_email}")
    else:
        failed("Notification", "Email send failed — check SMTP config")

finally:
    if driver:
        close_driver(driver, correlation_id=CID)

# ── Summary ────────────────────────────────────────────────────────────────────
print(f"\n{BOLD}{'─'*60}")
print(f"  VERIFICATION SUMMARY")
print(f"{'─'*60}{RESET}")
for name, status, detail in results:
    colour = GREEN if status == "PASS" else (RED if status == "FAIL" else YELLOW)
    marker = "✓" if status == "PASS" else ("✗" if status == "FAIL" else "~")
    print(f"  {colour}{marker} {status:<4}{RESET}  {name}")
    if detail:
        print(f"              {detail}")

passes  = sum(1 for _, s, _ in results if s == "PASS")
fails   = sum(1 for _, s, _ in results if s == "FAIL")
skips   = sum(1 for _, s, _ in results if s == "SKIP")
print(f"\n{BOLD}  {GREEN}{passes} passed{RESET}  {RED}{fails} failed{RESET}  {YELLOW}{skips} skipped{RESET}\n")
print(f"  Screenshots saved in {SCREENSHOTS_DIR}\n")

sys.exit(1 if fails else 0)
