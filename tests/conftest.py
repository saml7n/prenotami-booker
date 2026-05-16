"""Shared fixtures for prenotami-booker tests."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from prenotami_booker.config import (
    AppConfig,
    BrowserConfig,
    ConsulateConfig,
    EmailConfig,
    NotificationConfig,
    PrenotamiCredentials,
)


@pytest.fixture
def prenotami_credentials() -> PrenotamiCredentials:
    return PrenotamiCredentials(email="user@example.com", password="secret123")


@pytest.fixture
def email_config() -> EmailConfig:
    return EmailConfig(
        imap_server="imap.gmail.com",
        imap_port=993,
        email="user@example.com",
        password="app-password",
        use_ssl=True,
    )


@pytest.fixture
def notification_config() -> NotificationConfig:
    return NotificationConfig(
        smtp_server="smtp.gmail.com",
        smtp_port=587,
        smtp_email="user@example.com",
        smtp_password="app-password",
        notify_email="user@example.com",
        use_tls=True,
    )


@pytest.fixture
def consulate_config() -> ConsulateConfig:
    return ConsulateConfig(
        name="London",
        release_days=["monday", "wednesday"],
        release_time="17:00",
        timing_offset_seconds=-2,
        calendar_months_ahead=3,
        otp_prefetch_seconds=120,
        service_type="citizenship",
    )


@pytest.fixture
def browser_config() -> BrowserConfig:
    return BrowserConfig(
        headless=True,
        browser="chrome",
        implicit_wait=10,
        page_load_timeout=30,
    )


@pytest.fixture
def app_config(
    prenotami_credentials: PrenotamiCredentials,
    email_config: EmailConfig,
    notification_config: NotificationConfig,
    consulate_config: ConsulateConfig,
    browser_config: BrowserConfig,
) -> AppConfig:
    return AppConfig(
        prenotami=prenotami_credentials,
        email=email_config,
        notification=notification_config,
        consulate=consulate_config,
        browser=browser_config,
    )


@pytest.fixture
def mock_driver() -> MagicMock:
    """Mock Selenium WebDriver with common attributes."""
    driver = MagicMock()
    driver.current_url = "https://prenotami.esteri.it/Services"
    driver.page_source = "<html><body>Test page</body></html>"
    return driver
