"""Configuration management for Prenotami Booker."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
import yaml
from dotenv import load_dotenv

logger = structlog.get_logger()

# London consulate defaults
DEFAULT_RELEASE_DAYS = ["monday", "wednesday"]
DEFAULT_RELEASE_TIME = "17:00"  # GMT/UTC
DEFAULT_TIMING_OFFSET_SECONDS = -2  # Click 2 seconds before release
DEFAULT_CALENDAR_MONTHS_AHEAD = 3
DEFAULT_OTP_PREFETCH_SECONDS = 120  # Generate OTP 2 minutes before


@dataclass(frozen=True)
class PrenotamiCredentials:
    """Prenot@mi portal login credentials."""

    email: str
    password: str


@dataclass(frozen=True)
class EmailConfig:
    """IMAP email configuration for OTP retrieval."""

    imap_server: str
    imap_port: int
    email: str
    password: str = ""
    use_ssl: bool = True
    use_oauth: bool = False


@dataclass(frozen=True)
class NotificationConfig:
    """Notification settings."""

    smtp_server: str = ""
    smtp_port: int = 587
    smtp_email: str = ""
    smtp_password: str = ""
    notify_email: str = ""
    use_tls: bool = True


@dataclass(frozen=True)
class ConsulateConfig:
    """Consulate-specific configuration."""

    name: str = "London"
    release_days: list[str] = field(default_factory=lambda: list(DEFAULT_RELEASE_DAYS))
    release_time: str = DEFAULT_RELEASE_TIME
    timing_offset_seconds: int = DEFAULT_TIMING_OFFSET_SECONDS
    calendar_months_ahead: int = DEFAULT_CALENDAR_MONTHS_AHEAD
    otp_prefetch_seconds: int = DEFAULT_OTP_PREFETCH_SECONDS
    service_type: str = "citizenship"


@dataclass(frozen=True)
class BrowserConfig:
    """Browser/Selenium configuration."""

    headless: bool = False
    browser: str = "chrome"
    implicit_wait: int = 10
    page_load_timeout: int = 30


@dataclass(frozen=True)
class AppConfig:
    """Top-level application configuration."""

    prenotami: PrenotamiCredentials
    email: EmailConfig
    notification: NotificationConfig = field(default_factory=NotificationConfig)
    consulate: ConsulateConfig = field(default_factory=ConsulateConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)


def load_config(config_path: str | None = None, env_path: str | None = None) -> AppConfig:
    """Load configuration from YAML file and environment variables.

    Args:
        config_path: Path to YAML config file. Defaults to ./config.yaml.
        env_path: Path to .env file. Defaults to ./.env.

    Returns:
        Fully populated AppConfig instance.

    Raises:
        FileNotFoundError: If config file doesn't exist.
        ValueError: If required configuration is missing.
    """
    correlation_id = "config-load"

    # Load .env file
    env_file = Path(env_path) if env_path else Path(".env")
    if env_file.exists():
        load_dotenv(env_file)
        logger.info("loaded_env_file", correlation_id=correlation_id, path=str(env_file))

    # Load YAML config
    yaml_config: dict[str, Any] = {}
    cfg_file = Path(config_path) if config_path else Path("config.yaml")
    if cfg_file.exists():
        with open(cfg_file) as f:
            yaml_config = yaml.safe_load(f) or {}
        logger.info("loaded_yaml_config", correlation_id=correlation_id, path=str(cfg_file))

    # Build credentials (env vars take precedence)
    prenotami_defaults = yaml_config.get("prenotami", {})
    prenotami_email = os.getenv("PRENOTAMI_EMAIL", prenotami_defaults.get("email", ""))
    prenotami_password = os.getenv("PRENOTAMI_PASSWORD", prenotami_defaults.get("password", ""))

    if not prenotami_email or not prenotami_password:
        raise ValueError(
            "Prenot@mi credentials required. Set PRENOTAMI_EMAIL and PRENOTAMI_PASSWORD "
            "in .env or config.yaml"
        )

    prenotami_creds = PrenotamiCredentials(email=prenotami_email, password=prenotami_password)

    # Build email config for OTP
    email_cfg_raw = yaml_config.get("email", {})
    imap_password = os.getenv("IMAP_PASSWORD", email_cfg_raw.get("password", ""))
    use_oauth = email_cfg_raw.get("use_oauth", not imap_password)
    email_config = EmailConfig(
        imap_server=os.getenv("IMAP_SERVER", email_cfg_raw.get("imap_server", "imap.gmail.com")),
        imap_port=int(os.getenv("IMAP_PORT", email_cfg_raw.get("imap_port", 993))),
        email=os.getenv("IMAP_EMAIL", email_cfg_raw.get("email", prenotami_email)),
        password=imap_password,
        use_ssl=email_cfg_raw.get("use_ssl", True),
        use_oauth=use_oauth,
    )

    # Build notification config
    notif_raw = yaml_config.get("notification", {})
    notification_config = NotificationConfig(
        smtp_server=os.getenv("SMTP_SERVER", notif_raw.get("smtp_server", "")),
        smtp_port=int(os.getenv("SMTP_PORT", notif_raw.get("smtp_port", 587))),
        smtp_email=os.getenv("SMTP_EMAIL", notif_raw.get("smtp_email", "")),
        smtp_password=os.getenv("SMTP_PASSWORD", notif_raw.get("smtp_password", "")),
        notify_email=os.getenv("NOTIFY_EMAIL", notif_raw.get("notify_email", "")),
        use_tls=notif_raw.get("use_tls", True),
    )

    # Build consulate config
    consulate_raw = yaml_config.get("consulate", {})
    consulate_config = ConsulateConfig(
        name=consulate_raw.get("name", "London"),
        release_days=consulate_raw.get("release_days", list(DEFAULT_RELEASE_DAYS)),
        release_time=consulate_raw.get("release_time", DEFAULT_RELEASE_TIME),
        timing_offset_seconds=int(
            consulate_raw.get("timing_offset_seconds", DEFAULT_TIMING_OFFSET_SECONDS)
        ),
        calendar_months_ahead=int(
            consulate_raw.get("calendar_months_ahead", DEFAULT_CALENDAR_MONTHS_AHEAD)
        ),
        otp_prefetch_seconds=int(
            consulate_raw.get("otp_prefetch_seconds", DEFAULT_OTP_PREFETCH_SECONDS)
        ),
        service_type=consulate_raw.get("service_type", "citizenship"),
    )

    # Build browser config
    browser_raw = yaml_config.get("browser", {})
    browser_config = BrowserConfig(
        headless=browser_raw.get("headless", False),
        browser=browser_raw.get("browser", "chrome"),
        implicit_wait=int(browser_raw.get("implicit_wait", 10)),
        page_load_timeout=int(browser_raw.get("page_load_timeout", 30)),
    )

    config = AppConfig(
        prenotami=prenotami_creds,
        email=email_config,
        notification=notification_config,
        consulate=consulate_config,
        browser=browser_config,
    )

    logger.info(
        "config_loaded",
        correlation_id=correlation_id,
        consulate=config.consulate.name,
        service=config.consulate.service_type,
        release_days=config.consulate.release_days,
        release_time=config.consulate.release_time,
    )

    return config
