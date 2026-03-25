"""Selenium browser management for Prenot@mi automation."""

from __future__ import annotations

import structlog
from selenium import webdriver
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.remote.webdriver import WebDriver

from prenotami_booker.config import BrowserConfig

logger = structlog.get_logger()

PRENOTAMI_URL = "https://prenotami.esteri.it"


def create_driver(config: BrowserConfig, *, correlation_id: str) -> WebDriver:
    """Create and configure a Selenium WebDriver instance.

    Args:
        config: Browser configuration settings.
        correlation_id: Correlation ID for logging.

    Returns:
        Configured WebDriver instance.

    Raises:
        RuntimeError: If the browser cannot be initialized.
    """
    logger.info(
        "browser_creating",
        correlation_id=correlation_id,
        browser=config.browser,
        headless=config.headless,
    )

    try:
        options = ChromeOptions()

        if config.headless:
            options.add_argument("--headless=new")

        # Performance optimizations
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-extensions")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-infobars")
        options.add_argument("--disable-notifications")

        # Reduce detection
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)

        # Language - keep Italian as recommended by the guide
        options.add_argument("--lang=it")
        options.add_experimental_option("prefs", {"intl.accept_languages": "it,it-IT"})

        driver = webdriver.Chrome(options=options)

        driver.implicitly_wait(config.implicit_wait)
        driver.set_page_load_timeout(config.page_load_timeout)

        # Remove webdriver flag
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": """
                    Object.defineProperty(navigator, 'webdriver', {
                        get: () => undefined
                    })
                """
            },
        )

        logger.info(
            "browser_created",
            correlation_id=correlation_id,
        )
        return driver

    except Exception as exc:
        logger.error(
            "browser_creation_failed",
            correlation_id=correlation_id,
            error=str(type(exc).__name__),
        )
        raise RuntimeError("Failed to create browser instance") from exc


def close_driver(driver: WebDriver | None, *, correlation_id: str) -> None:
    """Safely close and quit the WebDriver.

    Args:
        driver: WebDriver instance to close, or None.
        correlation_id: Correlation ID for logging.
    """
    if driver is None:
        return

    try:
        driver.quit()
        logger.info("browser_closed", correlation_id=correlation_id)
    except Exception:
        logger.warning("browser_close_error", correlation_id=correlation_id)
