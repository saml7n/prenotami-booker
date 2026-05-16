"""Tests for config module."""

from __future__ import annotations

import os
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from prenotami_booker.config import (
    DEFAULT_CALENDAR_MONTHS_AHEAD,
    DEFAULT_OTP_PREFETCH_SECONDS,
    DEFAULT_RELEASE_DAYS,
    DEFAULT_RELEASE_TIME,
    DEFAULT_TIMING_OFFSET_SECONDS,
    AppConfig,
    BrowserConfig,
    ConsulateConfig,
    EmailConfig,
    NotificationConfig,
    PrenotamiCredentials,
    load_config,
)


class TestPrenotamiCredentials:
    def test_frozen(self, prenotami_credentials: PrenotamiCredentials) -> None:
        with pytest.raises(AttributeError):
            prenotami_credentials.email = "other@example.com"  # type: ignore[misc]

    def test_fields(self, prenotami_credentials: PrenotamiCredentials) -> None:
        assert prenotami_credentials.email == "user@example.com"
        assert prenotami_credentials.password == "secret123"


class TestEmailConfig:
    def test_defaults(self) -> None:
        cfg = EmailConfig(
            imap_server="imap.gmail.com",
            imap_port=993,
            email="a@b.com",
            password="pw",
        )
        assert cfg.use_ssl is True

    def test_ssl_override(self) -> None:
        cfg = EmailConfig(
            imap_server="imap.example.com",
            imap_port=143,
            email="a@b.com",
            password="pw",
            use_ssl=False,
        )
        assert cfg.use_ssl is False

    def test_oauth_defaults_to_false(self) -> None:
        cfg = EmailConfig(
            imap_server="imap.gmail.com",
            imap_port=993,
            email="a@b.com",
            password="pw",
        )
        assert cfg.use_oauth is False

    def test_oauth_enabled(self) -> None:
        cfg = EmailConfig(
            imap_server="imap.gmail.com",
            imap_port=993,
            email="a@b.com",
            use_oauth=True,
        )
        assert cfg.use_oauth is True
        assert cfg.password == ""


class TestConsulateConfig:
    def test_london_defaults(self) -> None:
        cfg = ConsulateConfig()
        assert cfg.name == "London"
        assert cfg.release_days == list(DEFAULT_RELEASE_DAYS)
        assert cfg.release_time == DEFAULT_RELEASE_TIME
        assert cfg.timing_offset_seconds == DEFAULT_TIMING_OFFSET_SECONDS
        assert cfg.calendar_months_ahead == DEFAULT_CALENDAR_MONTHS_AHEAD
        assert cfg.otp_prefetch_seconds == DEFAULT_OTP_PREFETCH_SECONDS
        assert cfg.service_type == "citizenship"

    def test_custom_consulate(self) -> None:
        cfg = ConsulateConfig(
            name="Los Angeles",
            release_days=["sunday", "monday", "tuesday", "wednesday"],
            release_time="00:00",
            timing_offset_seconds=-1,
            calendar_months_ahead=4,
            otp_prefetch_seconds=90,
        )
        assert cfg.name == "Los Angeles"
        assert "sunday" in cfg.release_days


class TestLoadConfig:
    def test_load_from_yaml(self, tmp_path: Path) -> None:
        yaml_content = textwrap.dedent("""\
            prenotami:
              email: "yaml@example.com"
              password: "yaml-pass"
            email:
              imap_server: "imap.test.com"
              imap_port: 993
              email: "yaml@example.com"
              password: "imap-pass"
            consulate:
              name: "Rome"
              release_days:
                - "tuesday"
                - "thursday"
              release_time: "12:00"
        """)
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml_content)

        # Ensure no env vars interfere
        env_vars_to_clear = [
            "PRENOTAMI_EMAIL", "PRENOTAMI_PASSWORD",
            "IMAP_SERVER", "IMAP_PORT", "IMAP_EMAIL", "IMAP_PASSWORD",
        ]
        with patch.dict(os.environ, {}, clear=False):
            for var in env_vars_to_clear:
                os.environ.pop(var, None)

            config = load_config(config_path=str(cfg_file))

        assert config.prenotami.email == "yaml@example.com"
        assert config.prenotami.password == "yaml-pass"
        assert config.email.imap_server == "imap.test.com"
        assert config.consulate.name == "Rome"
        assert config.consulate.release_days == ["tuesday", "thursday"]
        assert config.consulate.release_time == "12:00"

    def test_env_vars_override_yaml(self, tmp_path: Path) -> None:
        yaml_content = textwrap.dedent("""\
            prenotami:
              email: "yaml@example.com"
              password: "yaml-pass"
        """)
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml_content)

        env_overrides = {
            "PRENOTAMI_EMAIL": "env@example.com",
            "PRENOTAMI_PASSWORD": "env-pass",
        }
        with patch.dict(os.environ, env_overrides, clear=False):
            config = load_config(config_path=str(cfg_file))

        assert config.prenotami.email == "env@example.com"
        assert config.prenotami.password == "env-pass"

    def test_missing_credentials_raises(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("consulate:\n  name: Test\n")

        env_vars_to_clear = ["PRENOTAMI_EMAIL", "PRENOTAMI_PASSWORD"]
        with patch.dict(os.environ, {}, clear=False):
            for var in env_vars_to_clear:
                os.environ.pop(var, None)

            with pytest.raises(ValueError, match="credentials required"):
                load_config(config_path=str(cfg_file))

    def test_missing_config_file_uses_defaults(self, tmp_path: Path) -> None:
        """When no config file exists, env vars must supply credentials."""
        env = {
            "PRENOTAMI_EMAIL": "env@example.com",
            "PRENOTAMI_PASSWORD": "env-pass",
        }
        with patch.dict(os.environ, env, clear=False):
            config = load_config(config_path=str(tmp_path / "nonexistent.yaml"))

        assert config.prenotami.email == "env@example.com"
        assert config.consulate.name == "London"  # default

    def test_notification_defaults_when_not_configured(self, tmp_path: Path) -> None:
        yaml_content = textwrap.dedent("""\
            prenotami:
              email: "a@b.com"
              password: "pass"
        """)
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml_content)

        env_vars_to_clear = ["SMTP_SERVER", "SMTP_PORT", "SMTP_EMAIL", "SMTP_PASSWORD", "NOTIFY_EMAIL"]
        with patch.dict(os.environ, {}, clear=False):
            for var in env_vars_to_clear:
                os.environ.pop(var, None)
            config = load_config(config_path=str(cfg_file))

        assert config.notification.smtp_server == ""
        assert config.notification.notify_email == ""

    def test_browser_defaults(self, tmp_path: Path) -> None:
        yaml_content = textwrap.dedent("""\
            prenotami:
              email: "a@b.com"
              password: "pass"
        """)
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml_content)

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PRENOTAMI_EMAIL", None)
            os.environ.pop("PRENOTAMI_PASSWORD", None)
            config = load_config(config_path=str(cfg_file))

        assert config.browser.headless is False
        assert config.browser.browser == "chrome"
        assert config.browser.implicit_wait == 10
        assert config.browser.page_load_timeout == 30

    def test_full_config_example(self, tmp_path: Path) -> None:
        """Load a config matching config.example.yaml structure."""
        yaml_content = textwrap.dedent("""\
            prenotami:
              email: "test@example.com"
              password: "test-pass"
            email:
              imap_server: "imap.gmail.com"
              imap_port: 993
              email: "test@example.com"
              password: "imap-app-pass"
              use_ssl: true
            notification:
              smtp_server: "smtp.gmail.com"
              smtp_port: 587
              smtp_email: "test@example.com"
              smtp_password: "smtp-app-pass"
              notify_email: "notifications@example.com"
              use_tls: true
            consulate:
              name: "London"
              release_days: ["monday", "wednesday"]
              release_time: "17:00"
              timing_offset_seconds: -2
              calendar_months_ahead: 3
              otp_prefetch_seconds: 120
              service_type: "citizenship"
            browser:
              headless: true
              browser: "chrome"
              implicit_wait: 10
              page_load_timeout: 30
        """)
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml_content)

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PRENOTAMI_EMAIL", None)
            os.environ.pop("PRENOTAMI_PASSWORD", None)
            config = load_config(config_path=str(cfg_file))

        assert isinstance(config, AppConfig)
        assert config.notification.smtp_server == "smtp.gmail.com"
        assert config.notification.notify_email == "notifications@example.com"
        assert config.consulate.timing_offset_seconds == -2
        assert config.browser.headless is True
