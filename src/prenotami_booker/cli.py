"""Command-line interface for Prenotami Booker."""

from __future__ import annotations

import sys
from datetime import UTC, datetime

import click
import structlog

from prenotami_booker.config import load_config
from prenotami_booker.logging_config import generate_correlation_id, setup_logging
from prenotami_booker.orchestrator import run_booking_attempt, run_scheduled
from prenotami_booker.timing import get_next_release_time

logger = structlog.get_logger()


@click.group()
@click.option("--config", "-c", default="config.yaml", help="Path to config YAML file.")
@click.option("--env", "-e", default=".env", help="Path to .env file.")
@click.option("--json-logs", is_flag=True, default=False, help="Output logs as JSON.")
@click.pass_context
def main(ctx: click.Context, config: str, env: str, json_logs: bool) -> None:
    """Prenotami Booker - Automated Italian Consulate appointment booking.

    Books citizenship by descent (jure sanguinis) appointments at the
    Italian Consulate via the Prenot@mi portal.
    """
    setup_logging(json_output=json_logs)
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config
    ctx.obj["env_path"] = env


@main.command()
@click.pass_context
def run(ctx: click.Context) -> None:
    """Run a single booking attempt now.

    Attempts to book an appointment at the next release window.
    The bot will login, generate OTP, wait for the release time,
    and attempt to book.
    """
    cid = generate_correlation_id()
    logger.info("cli_run_started", correlation_id=cid)

    try:
        config = load_config(
            config_path=ctx.obj["config_path"],
            env_path=ctx.obj["env_path"],
        )
    except (ValueError, FileNotFoundError) as exc:
        click.echo(f"Configuration error: {exc}", err=True)
        sys.exit(1)

    outcome = run_booking_attempt(config, correlation_id=cid)

    if outcome.result.value == "success":
        click.echo("\nBOOKING SUCCESSFUL!")
        click.echo(f"Date: {outcome.appointment_date}")
        click.echo(f"Time: {outcome.appointment_time}")
        click.echo(f"Message: {outcome.message}")
    else:
        click.echo(f"\nBooking attempt unsuccessful: {outcome.result.value}")
        click.echo(f"Reason: {outcome.message}")
        sys.exit(1)


@main.command()
@click.pass_context
def schedule(ctx: click.Context) -> None:
    """Run in scheduled mode, waiting for release windows.

    Continuously monitors for release times (Mon/Wed 17:00 GMT for London)
    and attempts to book when slots are released. Runs until a booking
    succeeds or manually stopped.
    """
    cid = generate_correlation_id()
    logger.info("cli_schedule_started", correlation_id=cid)

    try:
        config = load_config(
            config_path=ctx.obj["config_path"],
            env_path=ctx.obj["env_path"],
        )
    except (ValueError, FileNotFoundError) as exc:
        click.echo(f"Configuration error: {exc}", err=True)
        sys.exit(1)

    click.echo(f"Starting scheduled booking for {config.consulate.name} Consulate")
    click.echo(f"Release days: {', '.join(config.consulate.release_days)}")
    click.echo(f"Release time: {config.consulate.release_time} UTC")
    click.echo("Press Ctrl+C to stop.\n")

    run_scheduled(config)


@main.command()
@click.pass_context
def next_release(ctx: click.Context) -> None:
    """Show when the next release window is.

    Displays the next appointment release time based on the
    configured consulate schedule.
    """
    setup_logging(json_output=False)
    cid = generate_correlation_id()

    try:
        config = load_config(
            config_path=ctx.obj["config_path"],
            env_path=ctx.obj["env_path"],
        )
    except (ValueError, FileNotFoundError) as exc:
        click.echo(f"Configuration error: {exc}", err=True)
        sys.exit(1)

    release = get_next_release_time(config.consulate, correlation_id=cid)
    now = datetime.now(UTC)
    delta = release - now

    hours = int(delta.total_seconds() // 3600)
    minutes = int((delta.total_seconds() % 3600) // 60)

    click.echo(f"Consulate: {config.consulate.name}")
    click.echo(f"Next release: {release.strftime('%A %Y-%m-%d %H:%M:%S')} UTC")
    click.echo(f"Time until release: {hours}h {minutes}m")
    click.echo(f"Release days: {', '.join(config.consulate.release_days)}")


@main.command()
@click.pass_context
def check_config(ctx: click.Context) -> None:
    """Validate configuration without running the booker.

    Loads and validates all configuration from the config file
    and environment variables, reporting any issues.
    """
    setup_logging(json_output=False)

    try:
        config = load_config(
            config_path=ctx.obj["config_path"],
            env_path=ctx.obj["env_path"],
        )
        click.echo("Configuration valid!")
        click.echo(f"  Consulate: {config.consulate.name}")
        click.echo(f"  Service: {config.consulate.service_type}")
        click.echo(f"  Release days: {', '.join(config.consulate.release_days)}")
        click.echo(f"  Release time: {config.consulate.release_time} UTC")
        click.echo(f"  Timing offset: {config.consulate.timing_offset_seconds}s")
        click.echo(f"  Calendar months ahead: {config.consulate.calendar_months_ahead}")
        click.echo(f"  Prenot@mi email: {config.prenotami.email}")
        click.echo(f"  IMAP server: {config.email.imap_server}")
        click.echo(f"  Headless browser: {config.browser.headless}")

        has_notifications = bool(config.notification.smtp_server)
        notif_status = "configured" if has_notifications else "not configured"
        click.echo(f"  Email notifications: {notif_status}")

    except (ValueError, FileNotFoundError) as exc:
        click.echo(f"Configuration error: {exc}", err=True)
        sys.exit(1)


@main.command()
def auth() -> None:
    """Authenticate with Gmail via OAuth2 (browser sign-in).

    Opens a browser window for Google sign-in so the bot can read
    OTP emails without needing your Gmail password. Only needs to be
    run once — the token is cached in token.json.

    Requires a client_secret.json file from Google Cloud Console.
    """
    from prenotami_booker.gmail_oauth import get_gmail_credentials

    click.echo("Starting Gmail OAuth2 authentication...")
    click.echo("A browser window will open for Google sign-in.\n")

    try:
        creds = get_gmail_credentials(correlation_id="cli-auth")
        click.echo("Authentication successful!")
        click.echo("Token saved to token.json — the bot can now read your emails.")
    except FileNotFoundError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    except Exception as exc:
        click.echo(f"Authentication failed: {exc}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
