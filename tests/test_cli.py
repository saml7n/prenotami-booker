"""Tests for CLI module."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from prenotami_booker.booker import BookingOutcome, BookingResult
from prenotami_booker.cli import main


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def valid_config_file(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        """prenotami:
  email: "user@example.com"
  password: "pass123"
email:
  imap_server: "imap.gmail.com"
  imap_email: "user@example.com"
  imap_password: "app-password"
consulate:
  name: "London"
  release_days: ["monday"]
  release_time: "17:00"
"""
    )
    return str(config)


class TestMainGroup:
    def test_help(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "Prenot@mi" in result.output or "prenotami" in result.output.lower()


class TestRunCommand:
    @patch("prenotami_booker.cli.run_booking_attempt")
    @patch("prenotami_booker.cli.load_config")
    @patch("prenotami_booker.cli.setup_logging")
    def test_run_invokes_booking(
        self,
        mock_logging: MagicMock,
        mock_load: MagicMock,
        mock_run: MagicMock,
        runner: CliRunner,
        app_config,
    ) -> None:
        mock_load.return_value = app_config
        mock_run.return_value = BookingOutcome(BookingResult.SUCCESS)

        result = runner.invoke(main, ["run"])
        assert result.exit_code == 0
        mock_run.assert_called_once()


    @patch("prenotami_booker.cli.load_config")
    @patch("prenotami_booker.cli.setup_logging")
    def test_run_with_config_path(
        self,
        mock_logging: MagicMock,
        mock_load: MagicMock,
        runner: CliRunner,
        app_config,
        valid_config_file: str,
    ) -> None:
        mock_load.return_value = app_config

        with patch("prenotami_booker.cli.run_booking_attempt") as mock_run:
            mock_run.return_value = BookingOutcome(BookingResult.SUCCESS)
            result = runner.invoke(main, ["-c", valid_config_file, "run"])

        assert result.exit_code == 0
        mock_load.assert_called_once()
        call_args = mock_load.call_args
        assert valid_config_file in str(call_args)

    @patch("prenotami_booker.cli.load_config")
    @patch("prenotami_booker.cli.setup_logging")
    def test_run_handles_config_error(
        self,
        mock_logging: MagicMock,
        mock_load: MagicMock,
        runner: CliRunner,
    ) -> None:
        mock_load.side_effect = ValueError("Missing required field")

        result = runner.invoke(main, ["run"])
        assert result.exit_code != 0

    @patch("prenotami_booker.cli.load_config")
    @patch("prenotami_booker.cli.setup_logging")
    def test_run_handles_file_not_found(
        self,
        mock_logging: MagicMock,
        mock_load: MagicMock,
        runner: CliRunner,
    ) -> None:
        mock_load.side_effect = FileNotFoundError("config.yaml not found")

        result = runner.invoke(main, ["run"])
        assert result.exit_code != 0


class TestScheduleCommand:
    @patch("prenotami_booker.cli.run_scheduled")
    @patch("prenotami_booker.cli.load_config")
    @patch("prenotami_booker.cli.setup_logging")
    def test_schedule_invokes_scheduler(
        self,
        mock_logging: MagicMock,
        mock_load: MagicMock,
        mock_sched: MagicMock,
        runner: CliRunner,
        app_config,
    ) -> None:
        mock_load.return_value = app_config

        result = runner.invoke(main, ["schedule"])
        assert result.exit_code == 0
        mock_sched.assert_called_once()


class TestNextReleaseCommand:
    @patch("prenotami_booker.cli.get_next_release_time")
    @patch("prenotami_booker.cli.load_config")
    @patch("prenotami_booker.cli.setup_logging")
    def test_next_release_prints_time(
        self,
        mock_logging: MagicMock,
        mock_load: MagicMock,
        mock_next: MagicMock,
        runner: CliRunner,
        app_config,
    ) -> None:
        mock_load.return_value = app_config
        mock_next.return_value = datetime(2025, 7, 14, 17, 0, tzinfo=UTC)

        result = runner.invoke(main, ["next-release"])
        assert result.exit_code == 0
        assert "2025" in result.output


class TestCheckConfigCommand:
    @patch("prenotami_booker.cli.load_config")
    @patch("prenotami_booker.cli.setup_logging")
    def test_check_config_valid(
        self,
        mock_logging: MagicMock,
        mock_load: MagicMock,
        runner: CliRunner,
        app_config,
    ) -> None:
        mock_load.return_value = app_config

        result = runner.invoke(main, ["check-config"])
        assert result.exit_code == 0

    @patch("prenotami_booker.cli.load_config")
    @patch("prenotami_booker.cli.setup_logging")
    def test_check_config_invalid(
        self,
        mock_logging: MagicMock,
        mock_load: MagicMock,
        runner: CliRunner,
    ) -> None:
        mock_load.side_effect = ValueError("Missing email")

        result = runner.invoke(main, ["check-config"])
        assert result.exit_code != 0


class TestAuthCommand:
    @patch("prenotami_booker.gmail_oauth.get_gmail_credentials")
    def test_auth_success(
        self,
        mock_get_creds: MagicMock,
        runner: CliRunner,
    ) -> None:
        mock_get_creds.return_value = MagicMock()

        result = runner.invoke(main, ["auth"])
        assert result.exit_code == 0
        assert "successful" in result.output.lower()

    @patch("prenotami_booker.gmail_oauth.get_gmail_credentials")
    def test_auth_missing_client_secret(
        self,
        mock_get_creds: MagicMock,
        runner: CliRunner,
    ) -> None:
        mock_get_creds.side_effect = FileNotFoundError("client secrets not found")

        result = runner.invoke(main, ["auth"])
        assert result.exit_code != 0
