"""
IAAI (Insurance Auto Auctions) Listing Adapter

Photos are captured by intercepting Playwright network responses during page load.
Chromium handles vis.iaai.com resizer URLs natively — including the literal \n
characters in the HTML src attributes that every Python HTTP client rejects.
We read the raw image bytes directly from the browser's already-completed
responses, skipping any separate download step entirely.
"""

import json
import logging
import os

from playwright.async_api import async_playwright

log = logging.getLogger(__name__)

_COOKIES_RAW = os.getenv("IAAI_SESSION_COOKIES", "")

DAMAGE_SELECTORS = [
    "[data-testid='damage-description']",
    ".damage-description",
    "[class*='DamageDescription']",
    "[class*='damage']",
]

_MIN_PHOTO_BYTES = 5_000       # skip images smaller than 5 KB (icons, pixels)
_MAX_PHOTO_BYTES = 20_971_520  # 20 MB cap


def _parse_cookies() -> dict:
    if not _COOKIES_RAW:
        return {}
    try:
        return json.loads(_COOKIES_RAW)
    except Exception:
        log.warning("IAAI_SESSION_COOKIES is not valid JSON — proceeding without auth cookies")
        return {}


def _is_iaai_image(response) -> bool:
    """True if this response (or any redirect ancestor) came from an iaai.com domain."""
    req = response.request
    while req:
        if "iaai.com" in req.url:
            return True
        req = req.redirected_from
    return "iaai.com" in response.url


class IAAIAdapter:
    async def fetch(self, url: str) -> dict:
        """
        Fetch an IAAI listing and extract photos + condition notes.
        Returns {"photo_data": [bytes, ...], "condition_notes": "..."}.

        Raises IAAIAuthError if the page looks like a login redirect.
        """
        cookies = _parse_cookies()
        pending: list = []  # Response objects; bodies read after page load

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                context = await browser.new_context()
                if cookies:
                    await context.add_cookies([
                        {"name": k, "value": v, "domain": ".iaai.com", "path": "/"}
                        for k, v in cookies.items()
                    ])
                page = await context.new_page()

                # Collect IAAI image Response objects synchronously.
                # We read their bodies asynchronously after the page finishes loading,
                # while the browser is still open and the responses are still valid.
                def _on_response(response):
                    if len(pending) >= 20:
                        return
                    if response.status != 200:
                        return
                    if not response.headers.get("content-type", "").startswith("image/"):
                        return
                    if _is_iaai_image(response):
                        pending.append(response)

                page.on("response", _on_response)
                await page.goto(url, wait_until="load", timeout=45_000)

                # Scroll to trigger lazy-loaded images then wait for them
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(2_000)
                await page.evaluate("window.scrollTo(0, 0)")
                await page.wait_for_timeout(1_000)

                # Auth checks
                page_url = page.url
                if "login" in page_url.lower() or "signin" in page_url.lower():
                    raise IAAIAuthError(f"IAAI session expired — redirected to {page_url}")

                title_el = await page.query_selector("title")
                if title_el:
                    title = (await title_el.text_content() or "").lower()
                    if "sign in" in title or "login" in title:
                        raise IAAIAuthError("IAAI page title indicates login wall")

                # Read response bodies while the browser is still open
                photo_data: list[bytes] = []
                seen: set[int] = set()
                for resp in pending:
                    try:
                        body = await resp.body()
                        h = hash(body)
                        if _MIN_PHOTO_BYTES <= len(body) <= _MAX_PHOTO_BYTES and h not in seen:
                            seen.add(h)
                            photo_data.append(body)
                    except Exception as exc:
                        log.warning("IAAI: failed to read response body: %s", exc)

                condition_notes = await self._extract_damage(page)
            finally:
                await browser.close()

        log.warning("IAAI: %d photos captured (url=%s)", len(photo_data), url)
        return {"photo_data": photo_data, "condition_notes": condition_notes}

    async def _extract_damage(self, page) -> str:
        for selector in DAMAGE_SELECTORS:
            elements = await page.query_selector_all(selector)
            if elements:
                text = (await elements[0].text_content() or "").strip()
                if text:
                    return text
        return ""


class IAAIAuthError(Exception):
    """Raised when IAAI redirects to login — signals expired session cookies."""
