"""
Reading pages that are an empty shell around a job-board iframe.

Mercor career pages render nothing in the host page; the posting is a full-window
Ashby iframe, so inner_text("body") came back with 0 chars. read_page_text falls back
to the iframes' text when the body is near-empty.

Offline: headless Chromium on hand-built HTML. Skipped when no browser is installed.
"""

import asyncio
import html

import pytest
from playwright.async_api import async_playwright

from src.scraper import EMBED_FALLBACK_CHARS, read_page_text


POSTING = "Software Engineer Intern. San Francisco. About the Role: write production code from your first week."


def _shell_page(body: str = "") -> str:
    """A host page with `body` as its own text and the posting in an iframe."""
    frame = html.escape(f"<html><body><h1>Mercor</h1><p>{POSTING}</p></body></html>", quote=True)
    return f"""<html><body>
      <div id="__next">{body}</div>
      <iframe srcdoc="{frame}" style="width:100%;height:600px"></iframe>
      <iframe srcdoc="" style="width:1px;height:1px;visibility:hidden"></iframe>
    </body></html>"""


def _read(page_html: str) -> str:
    async def run():
        async with async_playwright() as p:
            try:
                browser = await p.chromium.launch(headless=True)
            except Exception as e:
                pytest.skip(f"No Chromium for Playwright: {e}")
            page = await browser.new_page()
            await page.set_content(page_html)
            text = await read_page_text(page)
            await browser.close()
            return text
    return asyncio.run(run())


def test_iframe_text_is_read_when_the_body_is_empty():
    text = _read(_shell_page())
    assert POSTING in text
    assert "Mercor" in text


def test_short_body_text_is_kept_alongside_the_iframe():
    text = _read(_shell_page("Cookie notice"))
    assert "Cookie notice" in text
    assert POSTING in text


def test_a_page_with_enough_body_text_is_left_alone():
    body = "Plain job posting text. " * (EMBED_FALLBACK_CHARS // 10)
    text = _read(_shell_page(body))
    assert "Plain job posting text." in text
    assert POSTING not in text
