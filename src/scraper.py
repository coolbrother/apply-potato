"""
Playwright-based web scraper for ApplyPotato.
Fetches job pages with JavaScript rendering support.
"""

import asyncio
import logging
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse, parse_qs

from playwright.async_api import async_playwright, Browser, Page, TimeoutError as PlaywrightTimeout

from src.config import Config, get_config

logger = logging.getLogger(__name__)


class PlaywrightScraper:
    """
    Fetches job pages using Playwright with JavaScript rendering.

    No caching - deduplication is handled by checking Google Sheets
    in the main script before calling the scraper.

    Usage:
        async with PlaywrightScraper() as scraper:
            content = await scraper.fetch_page("https://example.com/job/123")
    """

    def __init__(self, config: Optional[Config] = None):
        """
        Initialize the scraper.

        Args:
            config: Optional config object. Uses global config if not provided.
        """
        self.config = config or get_config()
        self._playwright = None
        self._browser: Optional[Browser] = None

    async def __aenter__(self) -> "PlaywrightScraper":
        """Async context manager entry - launches browser."""
        await self._start_browser()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit - closes browser."""
        await self.close()

    async def _start_browser(self) -> None:
        """Launch Playwright browser if not already running."""
        if self._browser is not None:
            return

        logger.debug("Launching Playwright browser...")
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
        )
        logger.info("Playwright browser launched")

    async def close(self) -> None:
        """Close the browser and cleanup resources."""
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
            logger.debug("Browser closed")

        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
            logger.debug("Playwright stopped")

    async def fetch_page(self, url: str, render_delay: Optional[float] = None) -> Tuple[Optional[str], Optional[str], bool]:
        """
        Fetch a single page and extract text content.

        Args:
            url: The URL to fetch.
            render_delay: Seconds to wait after page load for JS to render.
                          If None, uses config.render_delay_seconds.
                          Increase for slow-rendering sites like Workday.

        Returns:
            Tuple of (content, final_url, is_blocked) where:
            - content: Page text content, or None if fetch failed
            - final_url: The final URL after redirects, or None if fetch failed
            - is_blocked: True if anti-scraping block detected (403/Forbidden)
        """
        if self._browser is None:
            await self._start_browser()

        timeout_ms = self.config.page_timeout_seconds * 1000
        last_error: Optional[Exception] = None
        actual_render_delay = render_delay if render_delay is not None else self.config.render_delay_seconds

        for attempt in range(1, self.config.max_retries + 1):
            try:
                logger.debug(f"Fetching (attempt {attempt}/{self.config.max_retries}): {url}")

                page: Page = await self._browser.new_page()
                try:
                    # Set viewport for consistent rendering
                    await page.set_viewport_size({"width": 1280, "height": 800})

                    # Navigate and wait for page load
                    # Using "load" instead of "networkidle" because some sites
                    # (like Lever apply pages) have continuous network activity
                    await page.goto(url, timeout=timeout_ms, wait_until="load")

                    # Wait for JavaScript to render content
                    await asyncio.sleep(actual_render_delay)

                    # Capture final URL after redirects
                    final_url = page.url

                    # Check for Greenhouse embedded job pages (gh_jid in URL)
                    # These sites show a job listing, then load specific job in iframe/modal
                    parsed = urlparse(url)
                    query_params = parse_qs(parsed.query)
                    is_greenhouse_embed = "gh_jid" in query_params

                    content = None
                    if is_greenhouse_embed:
                        # Try to find and extract from Greenhouse iframe
                        try:
                            # Wait for Greenhouse iframe to appear
                            iframe_selector = 'iframe[src*="boards.greenhouse.io"], iframe[src*="greenhouse.io"]'
                            await page.wait_for_selector(iframe_selector, timeout=5000)

                            iframe = page.frame_locator(iframe_selector).first
                            # Wait for job content to load in iframe
                            await asyncio.sleep(1)
                            content = await iframe.locator("body").inner_text()
                            logger.debug("Extracted content from Greenhouse iframe")
                        except Exception as e:
                            logger.debug(f"No Greenhouse iframe found, using page body: {e}")

                    # Fall back to page body content
                    if not content:
                        content = await page.inner_text("body")

                    # Check for anti-scraping/403 error pages
                    content_lower = content.lower() if content else ""
                    is_blocked = (
                        len(content) < 1000 and
                        any(indicator in content_lower for indicator in [
                            "403", "forbidden", "access denied", "blocked",
                            "not authorized", "captcha", "verify you are human",
                            "unusual traffic", "bot detected", "security check"
                        ])
                    )

                    if is_blocked:
                        logger.warning(f"Anti-scraping block detected for {url} (403/Forbidden)")

                    # Log success with content preview
                    content_len = len(content)
                    preview = content[:200].replace("\n", " ").strip()
                    logger.info(f"Fetched {final_url} ({content_len} chars)")
                    logger.debug(f"Content preview: {preview}...")

                    return content, final_url, is_blocked

                finally:
                    await page.close()

            except PlaywrightTimeout as e:
                last_error = e
                logger.warning(f"Timeout fetching {url} (attempt {attempt}/{self.config.max_retries})")

            except Exception as e:
                last_error = e
                logger.warning(f"Error fetching {url} (attempt {attempt}/{self.config.max_retries}): {e}")

            # Exponential backoff before retry
            if attempt < self.config.max_retries:
                wait_time = self.config.retry_base_delay_seconds * (2 ** attempt)
                logger.debug(f"Waiting {wait_time}s before retry...")
                await asyncio.sleep(wait_time)

        # All retries exhausted
        logger.error(f"Failed to fetch {url} after {self.config.max_retries} attempts: {last_error}")
        return None, None, False

    async def fetch_pages(self, urls: List[str]) -> Dict[str, Tuple[Optional[str], Optional[str], bool]]:
        """
        Fetch multiple pages sequentially.

        Args:
            urls: List of URLs to fetch.

        Returns:
            Dictionary mapping URL to (content, final_url, is_blocked) tuple.
        """
        results: Dict[str, Tuple[Optional[str], Optional[str], bool]] = {}

        for i, url in enumerate(urls):
            logger.info(f"Fetching page {i + 1}/{len(urls)}: {url}")
            content, final_url, is_blocked = await self.fetch_page(url)
            results[url] = (content, final_url, is_blocked)

            # Small delay between requests to be polite
            if i < len(urls) - 1:
                await asyncio.sleep(1)

        # Summary
        success_count = sum(1 for c, _, _ in results.values() if c is not None)
        logger.info(f"Fetched {success_count}/{len(urls)} pages successfully")

        return results


# Convenience function for one-off fetches
async def fetch_page(url: str, config: Optional[Config] = None) -> Tuple[Optional[str], Optional[str]]:
    """
    Fetch a single page (convenience function).

    For fetching multiple pages, use PlaywrightScraper context manager
    to reuse the browser instance.

    Args:
        url: The URL to fetch.
        config: Optional config object.

    Returns:
        Tuple of (content, final_url) or (None, None) if fetch failed.
    """
    async with PlaywrightScraper(config) as scraper:
        return await scraper.fetch_page(url)
