"""
IAAI (Insurance Auto Auctions) Listing Adapter

IAAI requires an authenticated account to view full VIN and listing photos.
Session cookies are stored in the IAAI_SESSION_COOKIES environment variable
as a JSON string ({"key": "value", ...}).

Cookie refresh cadence: ~every 30 days. When cookies expire:
  1. Log into iaai.com manually in a browser
  2. Open DevTools → Application → Cookies
  3. Copy all cookie values for iaai.com as JSON
  4. Update IAAI_SESSION_COOKIES in Railway env vars
  5. Any IAAI auth failure triggers an immediate alert email (see alerts.py)

Photos are captured via Playwright network response interception rather than
DOM scraping. IAAI's thumbnail images route through vis.iaai.com/resizer which
redirects to the actual CDN URL. Chromium handles that redirect internally;
we intercept the clean final CDN URL from the 200 response.
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


def _parse_cookies() -> dict:
    if not _COOKIES_RAW:
        return {}
    try:
        return json.loads(_COOKIES_RAW)
    except Exception:
        log.warning("IAAI_SESSION_COOKIES is not valid JSON — proceeding without auth cookies")
        return {}


class IAAIAdapter:
    async def fetch(self, url: str) -> dict:
        """
        Fetch an IAAI listing and extract photos + condition notes.
        Returns {"photos": [...], "condition_notes": "..."}.

        Raises IAAIAuthError if the page looks like a login redirect
        (triggers the immediate alert in worker.py).
        """
        cookies = _parse_cookies()
        photos: list[str] = []

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

                # Intercept network responses to capture clean CDN photo URLs.
                # IAAI thumbnails request vis.iaai.com/resizer which 302-redirects
                # to the actual CDN. Chromium follows that redirect fine; we capture
                # the final 200 response URL — no \n / control-char issues.
                def _on_response(response):
                    if len(photos) >= 20:
                        return
                    if response.status != 200:
                        return
                    if not response.headers.get("content-type", "").startswith("image/"):
                        return
                    # Only keep images that came via the IAAI resizer redirect
                    redirected_from = response.request.redirected_from
                    if not (redirected_from and "vis.iaai.com" in redirected_from.url):
                        return
                    cdn_url = response.url
                    if cdn_url not in photos:
                        photos.append(cdn_url)

                page.on("response", _on_response)
                await page.goto(url, wait_until="load", timeout=45_000)
                await page.wait_for_timeout(3_000)

                # Detect auth failure (redirected to login page)
                page_url = page.url
                if "login" in page_url.lower() or "signin" in page_url.lower():
                    raise IAAIAuthError(f"IAAI session expired or invalid — redirected to {page_url}")

                title_el = await page.query_selector("title")
                if title_el:
                    title = (await title_el.text_content() or "").lower()
                    if "sign in" in title or "login" in title:
                        raise IAAIAuthError("IAAI page title indicates login wall — session may be expired")

                condition_notes = await self._extract_damage(page)
            finally:
                await browser.close()

        log.warning("IAAI: %d photos via network intercept (url=%s)", len(photos), url)
        return {"photos": photos, "condition_notes": condition_notes}

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
