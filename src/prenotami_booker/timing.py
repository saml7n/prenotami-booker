"""Precise timing utilities for appointment release windows."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import ntplib
import structlog

from prenotami_booker.config import ConsulateConfig

logger = structlog.get_logger()

# Day name to weekday number mapping (Monday=0)
DAY_MAP: dict[str, int] = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

NTP_SERVERS = ["pool.ntp.org", "time.google.com", "time.cloudflare.com"]


def get_ntp_offset(*, correlation_id: str) -> float:
    """Query NTP servers to determine local clock offset.

    Tries multiple NTP servers in order. Returns the offset in seconds
    (positive means local clock is ahead of real time).

    Args:
        correlation_id: Correlation ID for logging.

    Returns:
        Clock offset in seconds, or 0.0 if NTP is unreachable.
    """
    for server in NTP_SERVERS:
        try:
            client = ntplib.NTPClient()
            response = client.request(server, version=3)
            offset = response.offset
            logger.info(
                "ntp_sync_success",
                correlation_id=correlation_id,
                server=server,
                offset_seconds=round(offset, 4),
            )
            return offset
        except (ntplib.NTPException, OSError):
            logger.debug(
                "ntp_server_unreachable",
                correlation_id=correlation_id,
                server=server,
            )
            continue

    logger.warning(
        "ntp_sync_failed_using_local_clock",
        correlation_id=correlation_id,
    )
    return 0.0


def get_next_release_time(config: ConsulateConfig, *, correlation_id: str) -> datetime:
    """Calculate the next appointment release time.

    Based on the consulate's configured release days and time,
    determines when the next release window will occur.

    Args:
        config: Consulate configuration with release schedule.
        correlation_id: Correlation ID for logging.

    Returns:
        UTC datetime of the next release window.
    """
    now = datetime.now(UTC)
    hour, minute = map(int, config.release_time.split(":"))

    # Build list of target weekdays
    target_days = sorted(
        DAY_MAP[day.lower()] for day in config.release_days if day.lower() in DAY_MAP
    )

    if not target_days:
        logger.error(
            "no_valid_release_days",
            correlation_id=correlation_id,
            configured_days=config.release_days,
        )
        raise ValueError(f"No valid release days configured: {config.release_days}")

    # Check today and next 7 days
    for days_ahead in range(8):
        candidate = now + timedelta(days=days_ahead)
        if candidate.weekday() in target_days:
            release = candidate.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if release > now:
                logger.info(
                    "next_release_calculated",
                    correlation_id=correlation_id,
                    release_time=release.isoformat(),
                    day=candidate.strftime("%A"),
                )
                return release

    # Fallback: shouldn't reach here with valid config
    raise ValueError("Could not calculate next release time")


def wait_until(
    target: datetime,
    *,
    correlation_id: str,
    offset_seconds: int = 0,
    ntp_offset: float = 0.0,
) -> None:
    """Wait until the target time (with optional offset).

    Uses a coarse sleep followed by a busy-wait loop for sub-second
    precision near the target time.

    Args:
        target: UTC datetime to wait until.
        correlation_id: Correlation ID for logging.
        offset_seconds: Seconds to offset from target (negative = before).
        ntp_offset: NTP clock offset in seconds (from get_ntp_offset).
    """
    adjusted_target = target + timedelta(seconds=offset_seconds)
    now = datetime.now(UTC) + timedelta(seconds=ntp_offset)
    wait_seconds = (adjusted_target - now).total_seconds()

    if wait_seconds <= 0:
        logger.warning(
            "target_time_already_passed",
            correlation_id=correlation_id,
            target=adjusted_target.isoformat(),
        )
        return

    logger.info(
        "waiting_for_release",
        correlation_id=correlation_id,
        target=adjusted_target.isoformat(),
        wait_seconds=round(wait_seconds, 1),
        offset_seconds=offset_seconds,
        ntp_offset=round(ntp_offset, 4),
    )

    # Coarse sleep until 5 seconds before target
    if wait_seconds > 5:
        time.sleep(wait_seconds - 5)

    # Log countdown at 5 seconds
    remaining = (adjusted_target - (datetime.now(UTC) + timedelta(seconds=ntp_offset))).total_seconds()
    if remaining > 0:
        logger.info(
            "countdown_final_seconds",
            correlation_id=correlation_id,
            remaining_seconds=round(remaining, 2),
        )

    # Busy-wait for sub-second precision
    while (datetime.now(UTC) + timedelta(seconds=ntp_offset)) < adjusted_target:
        time.sleep(0.01)  # 10ms resolution

    logger.info(
        "target_time_reached",
        correlation_id=correlation_id,
        actual_time=datetime.now(UTC).isoformat(),
    )


def is_release_day(config: ConsulateConfig) -> bool:
    """Check if today is a configured release day.

    Args:
        config: Consulate configuration.

    Returns:
        True if today is a release day.
    """
    now = datetime.now(UTC)
    target_days = [DAY_MAP[d.lower()] for d in config.release_days if d.lower() in DAY_MAP]
    return now.weekday() in target_days


def seconds_until_release(config: ConsulateConfig) -> float:
    """Calculate seconds until the next release time today.

    Args:
        config: Consulate configuration.

    Returns:
        Seconds until release, or negative if already passed.
    """
    now = datetime.now(UTC)
    hour, minute = map(int, config.release_time.split(":"))
    release_today = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return (release_today - now).total_seconds()
