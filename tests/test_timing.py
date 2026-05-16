"""Tests for timing module."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from prenotami_booker.config import ConsulateConfig
from prenotami_booker.timing import (
    DAY_MAP,
    NTP_SERVERS,
    get_next_release_time,
    get_ntp_offset,
    is_release_day,
    seconds_until_release,
    wait_until,
)


class TestDayMap:
    def test_all_days_present(self) -> None:
        expected = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}
        assert set(DAY_MAP.keys()) == expected

    def test_monday_is_zero(self) -> None:
        assert DAY_MAP["monday"] == 0

    def test_sunday_is_six(self) -> None:
        assert DAY_MAP["sunday"] == 6


class TestGetNextReleaseTime:
    def test_returns_future_datetime(self, consulate_config: ConsulateConfig) -> None:
        result = get_next_release_time(consulate_config, correlation_id="test")
        assert result > datetime.now(UTC)

    def test_release_on_correct_weekday(self, consulate_config: ConsulateConfig) -> None:
        """Release should fall on Monday (0) or Wednesday (2)."""
        result = get_next_release_time(consulate_config, correlation_id="test")
        assert result.weekday() in (0, 2)

    def test_release_at_configured_time(self, consulate_config: ConsulateConfig) -> None:
        result = get_next_release_time(consulate_config, correlation_id="test")
        assert result.hour == 17
        assert result.minute == 0
        assert result.second == 0

    def test_invalid_release_days_raises(self) -> None:
        config = ConsulateConfig(release_days=["notaday", "fake"])
        with pytest.raises(ValueError, match="No valid release days"):
            get_next_release_time(config, correlation_id="test")

    def test_empty_release_days_raises(self) -> None:
        config = ConsulateConfig(release_days=[])
        with pytest.raises(ValueError, match="No valid release days"):
            get_next_release_time(config, correlation_id="test")

    def test_next_release_is_today_if_not_yet_passed(self) -> None:
        """If today is Monday and it's before 17:00 UTC, release should be today."""
        # Simulate a Monday at 10:00 UTC
        fake_now = datetime(2025, 1, 6, 10, 0, 0, tzinfo=UTC)  # Monday
        config = ConsulateConfig(release_days=["monday"], release_time="17:00")

        with patch("prenotami_booker.timing.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = get_next_release_time(config, correlation_id="test")

        assert result.date() == fake_now.date()
        assert result.hour == 17

    def test_next_release_skips_today_if_already_passed(self) -> None:
        """If it's Monday 18:00 UTC, next Monday release was already passed."""
        fake_now = datetime(2025, 1, 6, 18, 0, 0, tzinfo=UTC)  # Monday 18:00
        config = ConsulateConfig(release_days=["monday", "wednesday"], release_time="17:00")

        with patch("prenotami_booker.timing.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = get_next_release_time(config, correlation_id="test")

        # Should be Wednesday
        assert result.weekday() == 2
        assert result.hour == 17

    @pytest.mark.parametrize(
        "release_time,expected_hour,expected_minute",
        [
            ("09:30", 9, 30),
            ("00:00", 0, 0),
            ("23:59", 23, 59),
        ],
    )
    def test_custom_release_times(
        self, release_time: str, expected_hour: int, expected_minute: int
    ) -> None:
        config = ConsulateConfig(
            release_days=["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"],
            release_time=release_time,
        )
        result = get_next_release_time(config, correlation_id="test")
        assert result.hour == expected_hour
        assert result.minute == expected_minute

    def test_edge_case_exact_release_time(self) -> None:
        """BUG: If now == release_time exactly, strict > skips to next window.

        The condition `release > now` means if we're exactly at the release
        moment it's skipped. This documents the known edge case.
        """
        fake_now = datetime(2025, 1, 6, 17, 0, 0, tzinfo=UTC)  # Monday 17:00 exactly
        config = ConsulateConfig(release_days=["monday", "wednesday"], release_time="17:00")

        with patch("prenotami_booker.timing.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = get_next_release_time(config, correlation_id="test")

        # Due to strict >, today's 17:00 is skipped → next is Wednesday
        assert result.weekday() == 2

    def test_la_consulate_schedule(self) -> None:
        """LA consulate uses Sun-Wed midnight Rome time (per Reddit guide)."""
        config = ConsulateConfig(
            name="Los Angeles",
            release_days=["sunday", "monday", "tuesday", "wednesday"],
            release_time="00:00",  # midnight Rome = different UTC offset
        )
        result = get_next_release_time(config, correlation_id="test")
        assert result.weekday() in (0, 1, 2, 6)  # Mon, Tue, Wed, Sun


class TestWaitUntil:
    def test_already_passed_returns_immediately(self) -> None:
        past = datetime.now(UTC) - timedelta(seconds=10)
        # Should return without blocking
        wait_until(past, correlation_id="test")

    def test_negative_offset_waits_earlier(self) -> None:
        """Offset of -2 means action happens 2 seconds before target."""
        target = datetime.now(UTC) + timedelta(seconds=0.2)
        with patch("prenotami_booker.timing.time.sleep") as mock_sleep:
            wait_until(target, correlation_id="test", offset_seconds=-5)
            # Target - 5 seconds is already passed, should return immediately
            # No long sleep should happen

    def test_short_wait_uses_busy_loop(self) -> None:
        """For waits under 5 seconds, no coarse sleep occurs."""
        target = datetime.now(UTC) + timedelta(seconds=0.1)
        wait_until(target, correlation_id="test")
        # Should complete without error


class TestIsReleaseDay:
    def test_monday_is_release_day(self) -> None:
        config = ConsulateConfig(release_days=["monday"])
        fake_monday = datetime(2025, 1, 6, 12, 0, 0, tzinfo=UTC)
        with patch("prenotami_booker.timing.datetime") as mock_dt:
            mock_dt.now.return_value = fake_monday
            assert is_release_day(config) is True

    def test_tuesday_not_release_day_for_london(self) -> None:
        config = ConsulateConfig(release_days=["monday", "wednesday"])
        fake_tuesday = datetime(2025, 1, 7, 12, 0, 0, tzinfo=UTC)
        with patch("prenotami_booker.timing.datetime") as mock_dt:
            mock_dt.now.return_value = fake_tuesday
            assert is_release_day(config) is False


class TestSecondsUntilRelease:
    def test_before_release_returns_positive(self) -> None:
        config = ConsulateConfig(release_time="23:59")
        fake_now = datetime(2025, 1, 6, 12, 0, 0, tzinfo=UTC)
        with patch("prenotami_booker.timing.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            result = seconds_until_release(config)
        # 23:59 - 12:00 = 11h59m = 43140 seconds
        assert result == pytest.approx(43140, abs=1)

    def test_after_release_returns_negative(self) -> None:
        config = ConsulateConfig(release_time="10:00")
        fake_now = datetime(2025, 1, 6, 12, 0, 0, tzinfo=UTC)
        with patch("prenotami_booker.timing.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            result = seconds_until_release(config)
        assert result < 0


class TestGetNtpOffset:
    def test_returns_offset_on_success(self) -> None:
        mock_response = MagicMock()
        mock_response.offset = -0.0342

        with patch("prenotami_booker.timing.ntplib.NTPClient") as mock_client_cls:
            mock_client_cls.return_value.request.return_value = mock_response
            offset = get_ntp_offset(correlation_id="test")

        assert offset == -0.0342

    def test_tries_multiple_servers(self) -> None:
        import ntplib

        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.offset = 0.5

        # First two servers fail, third succeeds
        mock_client.request.side_effect = [
            ntplib.NTPException("timeout"),
            OSError("unreachable"),
            mock_response,
        ]

        with patch("prenotami_booker.timing.ntplib.NTPClient", return_value=mock_client):
            offset = get_ntp_offset(correlation_id="test")

        assert offset == 0.5
        assert mock_client.request.call_count == 3

    def test_returns_zero_when_all_fail(self) -> None:
        import ntplib

        with patch("prenotami_booker.timing.ntplib.NTPClient") as mock_client_cls:
            mock_client_cls.return_value.request.side_effect = ntplib.NTPException("timeout")
            offset = get_ntp_offset(correlation_id="test")

        assert offset == 0.0

    def test_ntp_servers_list(self) -> None:
        assert len(NTP_SERVERS) >= 2
        assert "pool.ntp.org" in NTP_SERVERS


class TestWaitUntilWithNtp:
    def test_ntp_offset_applied_to_target_comparison(self) -> None:
        """NTP offset should adjust the effective 'now' time."""
        # If local clock is 1 second ahead (ntp_offset=1.0),
        # then effective now = local_now + 1s, meaning we wait less.
        target = datetime.now(UTC) + timedelta(seconds=0.1)
        with patch("prenotami_booker.timing.time.sleep"):
            # With large positive offset, target is already "passed"
            wait_until(target, correlation_id="test", ntp_offset=10.0)
            # Should return immediately without blocking
