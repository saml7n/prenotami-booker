# Prenotami Booker

Automated appointment booking tool for the Italian Consulate via the [Prenot@mi](https://prenotami.esteri.it) portal. Designed for booking **Citizenship by Descent (Jure Sanguinis)** appointments at the **London consulate**, but configurable for other consulates.

## How It Works

The tool automates the booking flow using two key tricks discovered by the jure sanguinis community:

1. **OTP Trick**: Generates the OTP verification code via the Passport appointment page (which is always available) ~2 minutes before the release time, rather than waiting for the citizenship page which only opens at release time.

2. **Precise Timing**: Clicks the citizenship booking button at exactly the right moment (configurable, default: 2 seconds before release) for the fastest page load.

### London Consulate Schedule

- **Release days**: Mondays and Wednesdays
- **Release time**: 17:00 GMT/UTC
- **Booking window**: Up to 12 months in advance
- **Confirmation required**: 3 days before appointment or it auto-cancels

## Quick Start

### Prerequisites

- Python 3.11+
- Google Chrome browser
- ChromeDriver (matching your Chrome version)
- A registered [Prenot@mi](https://prenotami.esteri.it) account
- Email access via IMAP (for OTP retrieval)

### Installation

```bash
git clone https://github.com/saml7n/prenotami-booker.git
cd prenotami-booker
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -e .
```

### Configuration

1. Copy the example config files:

```bash
cp config.example.yaml config.yaml
cp .env.example .env
```

2. Edit `config.yaml` and/or `.env` with your details:

- **Prenot@mi credentials**: Your email and password for the portal
- **IMAP settings**: For reading OTP emails (Gmail users: use an [App Password](https://support.google.com/accounts/answer/185833))
- **SMTP settings** (optional): For email notifications on success/failure
- **Consulate settings**: Adjust release days/times if not using London

### Gmail Setup for OTP

If your Prenot@mi account uses Gmail:

1. Enable 2-Step Verification on your Google account
2. Generate an App Password: Google Account > Security > App Passwords
3. Use the App Password (not your regular password) for both IMAP and SMTP

### Usage

**Check configuration:**

```bash
prenotami check-config
```

**See next release time:**

```bash
prenotami next-release
```

**Run a single booking attempt** (waits for the next release window):

```bash
prenotami run
```

**Run in scheduled mode** (continuously waits for release windows until booking succeeds):

```bash
prenotami schedule
```

### Options

```bash
prenotami --config path/to/config.yaml --env path/to/.env run
prenotami --json-logs schedule  # JSON-formatted logs for piping
```

## Configuration Reference

### Consulate Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `name` | `London` | Consulate name |
| `release_days` | `monday, wednesday` | Days appointments are released |
| `release_time` | `17:00` | Release time in UTC |
| `timing_offset_seconds` | `-2` | Seconds before release to click (negative = before) |
| `calendar_months_ahead` | `3` | Months to skip forward in calendar |
| `otp_prefetch_seconds` | `120` | Seconds before release to generate OTP |
| `service_type` | `citizenship` | Service to book (`citizenship` or `passport`) |

### Other Consulates

The tool works with any consulate on the Prenot@mi system. Update the `consulate` section in `config.yaml`:

```yaml
consulate:
  name: "Manchester"
  release_days: ["monday", "wednesday"]  # Check your consulate's schedule
  release_time: "17:00"                   # Check your consulate's release time
```

## How the Booking Flow Works

1. **Login** (~10 min before release): Bot logs into Prenot@mi
2. **OTP Generation** (~2 min before): Navigates to Passport page, triggers OTP
3. **OTP Retrieval**: Reads OTP code from your email via IMAP
4. **Navigate to Services**: Returns to main services page
5. **Precise Click** (~2 sec before release): Clicks citizenship "Prenota" button
6. **Form Submission**: Pastes OTP, accepts privacy terms, clicks "Avanti"
7. **Calendar Navigation**: Uses arrow buttons to navigate to target month
8. **Booking**: Clicks first available (green) date and confirms
9. **Notification**: Sends email with appointment details

## Tips

- **Keep the site in Italian** - the English version loads slower
- **Run on a fast, wired connection** - speed is critical
- **Headless mode** (`headless: true`) for running on a server/VPS
- **Check your consulate's website** for any schedule changes or maintenance windows

## Troubleshooting

- **Login fails**: Verify your Prenot@mi credentials work manually
- **OTP not found**: Check IMAP settings, ensure emails from Prenot@mi aren't in spam
- **No appointments found**: Slots may have been taken. The bot will retry at the next release window in scheduled mode
- **Calendar errors**: The site can be unstable at release time. The bot includes retry logic

## Disclaimer

This tool is for personal use to help book your own legitimate appointments. Use responsibly and in accordance with the Prenot@mi portal's terms of service. The developers are not responsible for any consequences of using this tool.

## License

MIT
