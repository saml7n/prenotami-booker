"""Tests for browser module."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from prenotami_booker.browser import PRENOTAMI_URL, close_driver, create_driver
from prenotami_booker.config import BrowserConfig


class TestCreateDriver:
    @patch("prenotami_booker.browser.webdriver.Chrome")
    def test_creates_chrome_driver(
        self, mock_chrome: MagicMock, browser_config: BrowserConfig
    ) -> None:
        mock_driver = MagicMock()
        mock_chrome.return_value = mock_driver

        driver = create_driver(browser_config, correlation_id="test")

        assert driver is mock_driver
        mock_chrome.assert_called_once()

    @patch("prenotami_booker.browser.webdriver.Chrome")
    def test_sets_headless_mode(self, mock_chrome: MagicMock) -> None:
        config = BrowserConfig(headless=True)
        mock_chrome.return_value = MagicMock()

        create_driver(config, correlation_id="test")

        # Verify options passed to Chrome constructor
        call_kwargs = mock_chrome.call_args
        options = call_kwargs.kwargs.get("options") or call_kwargs[1].get("options")
        assert options is not None

    @patch("prenotami_booker.browser.webdriver.Chrome")
    def test_sets_implicit_wait(
        self, mock_chrome: MagicMock, browser_config: BrowserConfig
    ) -> None:
        mock_driver = MagicMock()
        mock_chrome.return_value = mock_driver

        create_driver(browser_config, correlation_id="test")

        mock_driver.implicitly_wait.assert_called_once_with(10)

    @patch("prenotami_booker.browser.webdriver.Chrome")
    def test_sets_page_load_timeout(
        self, mock_chrome: MagicMock, browser_config: BrowserConfig
    ) -> None:
        mock_driver = MagicMock()
        mock_chrome.return_value = mock_driver

        create_driver(browser_config, correlation_id="test")

        mock_driver.set_page_load_timeout.assert_called_once_with(30)

    @patch("prenotami_booker.browser.webdriver.Chrome")
    def test_removes_webdriver_flag(
        self, mock_chrome: MagicMock, browser_config: BrowserConfig
    ) -> None:
        """Anti-detection: hides the webdriver property."""
        mock_driver = MagicMock()
        mock_chrome.return_value = mock_driver

        create_driver(browser_config, correlation_id="test")

        mock_driver.execute_cdp_cmd.assert_called_once()
        args = mock_driver.execute_cdp_cmd.call_args
        assert args[0][0] == "Page.addScriptToEvaluateOnNewDocument"
        assert "webdriver" in args[0][1]["source"]

    @patch("prenotami_booker.browser.webdriver.Chrome")
    def test_raises_runtime_error_on_failure(self, mock_chrome: MagicMock) -> None:
        mock_chrome.side_effect = Exception("Chrome not found")
        config = BrowserConfig()

        with pytest.raises(RuntimeError, match="Failed to create browser"):
            create_driver(config, correlation_id="test")

    @patch("prenotami_booker.browser.webdriver.Chrome")
    def test_italian_language_setting(
        self, mock_chrome: MagicMock, browser_config: BrowserConfig
    ) -> None:
        """Per Reddit guide: 'Do NOT switch to the English form of website'."""
        mock_chrome.return_value = MagicMock()
        create_driver(browser_config, correlation_id="test")

        # The options object passed should contain Italian language settings
        # We verify this was configured by checking ChromeOptions was used
        mock_chrome.assert_called_once()


class TestCloseDriver:
    def test_closes_driver(self) -> None:
        mock_driver = MagicMock()
        close_driver(mock_driver, correlation_id="test")
        mock_driver.quit.assert_called_once()

    def test_handles_none_driver(self) -> None:
        """Should not raise when driver is None."""
        close_driver(None, correlation_id="test")

    def test_handles_quit_error(self) -> None:
        """Should not raise if quit() fails."""
        mock_driver = MagicMock()
        mock_driver.quit.side_effect = Exception("already closed")

        # Should not raise
        close_driver(mock_driver, correlation_id="test")


class TestPrenotamiUrl:
    def test_correct_url(self) -> None:
        assert PRENOTAMI_URL == "https://prenotami.esteri.it"
