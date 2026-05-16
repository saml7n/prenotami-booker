"""Tests for Gmail OAuth2 authentication module."""

from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from prenotami_booker.gmail_oauth import (
    DEFAULT_CLIENT_SECRETS_PATH,
    DEFAULT_TOKEN_PATH,
    SCOPES,
    build_xoauth2_string,
    get_gmail_credentials,
)


class TestBuildXoauth2String:
    def test_returns_base64_encoded_string(self) -> None:
        result = build_xoauth2_string("user@gmail.com", "access-token-123")
        decoded = base64.b64decode(result).decode()
        assert "user=user@gmail.com" in decoded
        assert "auth=Bearer access-token-123" in decoded

    def test_format_matches_xoauth2_spec(self) -> None:
        result = build_xoauth2_string("a@b.com", "tok")
        decoded = base64.b64decode(result).decode()
        assert decoded == "user=a@b.com\x01auth=Bearer tok\x01\x01"


class TestGetGmailCredentials:
    def test_raises_when_client_secrets_missing(self, tmp_path: Path) -> None:
        missing = tmp_path / "nope.json"
        with pytest.raises(FileNotFoundError, match="client secrets not found"):
            get_gmail_credentials(client_secrets_path=missing, token_path=tmp_path / "t.json")

    @patch("prenotami_booker.gmail_oauth.InstalledAppFlow")
    def test_runs_browser_flow_when_no_token(
        self, mock_flow_cls: MagicMock, tmp_path: Path
    ) -> None:
        secrets = tmp_path / "client_secret.json"
        secrets.write_text("{}")
        token_path = tmp_path / "token.json"

        mock_creds = MagicMock()
        mock_creds.valid = True
        mock_creds.to_json.return_value = '{"token": "abc"}'
        mock_flow = MagicMock()
        mock_flow.run_local_server.return_value = mock_creds
        mock_flow_cls.from_client_secrets_file.return_value = mock_flow

        result = get_gmail_credentials(client_secrets_path=secrets, token_path=token_path)

        mock_flow.run_local_server.assert_called_once_with(port=0)
        assert result is mock_creds
        assert token_path.exists()

    @patch("prenotami_booker.gmail_oauth.Credentials")
    def test_returns_cached_valid_token(
        self, mock_creds_cls: MagicMock, tmp_path: Path
    ) -> None:
        secrets = tmp_path / "client_secret.json"
        secrets.write_text("{}")
        token_path = tmp_path / "token.json"
        token_path.write_text('{"token": "cached"}')

        mock_creds = MagicMock()
        mock_creds.valid = True
        mock_creds_cls.from_authorized_user_file.return_value = mock_creds

        result = get_gmail_credentials(client_secrets_path=secrets, token_path=token_path)

        assert result is mock_creds

    @patch("prenotami_booker.gmail_oauth.Request")
    @patch("prenotami_booker.gmail_oauth.Credentials")
    def test_refreshes_expired_token(
        self, mock_creds_cls: MagicMock, mock_request_cls: MagicMock, tmp_path: Path
    ) -> None:
        secrets = tmp_path / "client_secret.json"
        secrets.write_text("{}")
        token_path = tmp_path / "token.json"
        token_path.write_text('{"token": "old"}')

        mock_creds = MagicMock()
        mock_creds.valid = False
        mock_creds.expired = True
        mock_creds.refresh_token = "refresh-tok"
        mock_creds.to_json.return_value = '{"token": "refreshed"}'
        mock_creds_cls.from_authorized_user_file.return_value = mock_creds

        get_gmail_credentials(client_secrets_path=secrets, token_path=token_path)

        mock_creds.refresh.assert_called_once()


class TestConstants:
    def test_scopes_include_gmail(self) -> None:
        assert any("mail.google.com" in s for s in SCOPES)

    def test_default_paths(self) -> None:
        assert DEFAULT_TOKEN_PATH == Path("token.json")
        assert DEFAULT_CLIENT_SECRETS_PATH == Path("client_secret.json")
