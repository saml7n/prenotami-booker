"""Gmail OAuth2 authentication for IMAP access.

Provides a browser-based OAuth2 sign-in flow so users don't need to store
their Gmail password or create app passwords. The refresh token is cached
locally so the browser flow only runs once.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import structlog
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

logger = structlog.get_logger()

# Gmail IMAP scope — read-only access to email
SCOPES = ["https://mail.google.com/"]

# Default paths (relative to project root)
DEFAULT_TOKEN_PATH = Path("token.json")
DEFAULT_CLIENT_SECRETS_PATH = Path("client_secret.json")


def get_gmail_credentials(
    *,
    client_secrets_path: Path = DEFAULT_CLIENT_SECRETS_PATH,
    token_path: Path = DEFAULT_TOKEN_PATH,
    correlation_id: str = "oauth",
) -> Credentials:
    """Get valid Gmail OAuth2 credentials, launching browser if needed.

    On first run, opens a browser window for Google sign-in. The resulting
    refresh token is saved to token_path so subsequent runs don't need
    the browser.

    Args:
        client_secrets_path: Path to Google Cloud OAuth client secrets JSON.
        token_path: Path to store/load the cached refresh token.
        correlation_id: For structured logging.

    Returns:
        Valid Google OAuth2 Credentials.

    Raises:
        FileNotFoundError: If client_secrets_path doesn't exist.
        ValueError: If the OAuth flow fails or is cancelled.
    """
    if not client_secrets_path.exists():
        raise FileNotFoundError(
            f"OAuth client secrets not found at {client_secrets_path}. "
            "Download this from Google Cloud Console → APIs & Services → Credentials. "
            "See README.md for setup instructions."
        )

    creds: Credentials | None = None

    # Load cached token if it exists
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        logger.debug("oauth_token_loaded", correlation_id=correlation_id)

    # Refresh or run the browser flow
    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        logger.info("oauth_refreshing_token", correlation_id=correlation_id)
        creds.refresh(Request())
    else:
        logger.info("oauth_browser_flow_starting", correlation_id=correlation_id)
        flow = InstalledAppFlow.from_client_secrets_file(
            str(client_secrets_path), SCOPES
        )
        creds = flow.run_local_server(port=0)
        logger.info("oauth_browser_flow_complete", correlation_id=correlation_id)

    # Cache the token
    token_path.write_text(creds.to_json())
    logger.info("oauth_token_saved", correlation_id=correlation_id, path=str(token_path))

    return creds


def build_xoauth2_string(email: str, access_token: str) -> str:
    """Build the XOAUTH2 authentication string for IMAP.

    Args:
        email: Gmail address.
        access_token: Valid OAuth2 access token.

    Returns:
        Base64-encoded XOAUTH2 string for IMAP AUTHENTICATE command.
    """
    auth_string = f"user={email}\x01auth=Bearer {access_token}\x01\x01"
    return base64.b64encode(auth_string.encode()).decode()
