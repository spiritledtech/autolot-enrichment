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

Photos are captured via Playwright network response interception. IAAI images
route through a resizer (vis.iaai.com or similar) that 302-redirects to the
actual CDN. Chromium follows that redirect internally; we capture the clean
CDN URL from the final 200 response.

Fallback: if network interception yields 0 photos (e.g. lazy-loaded or
non-redirect CDN), we pull img.currentSrc from the DOM — the browser has
already resolved and normalized these URLs.
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

                # Strategy 1: network response interception.
                # Any image/* response that came via a redirect is a vehicle photo
                # (CDN served directly = UI asset; CDN via resizer redirect = photo).
                # Works for vis.iaai.com/resizer and any regional variant.
                def _on_response(response):
                    if len(photos) >= 20:
                        return
                    if response.status != 200:
                        return
                    if not response.headers.get("content-type", "").startswith("image/"):
                        return
                    if not response.request.redirected_from:
                        return  # no redirect = likely a UI asset, skip
                    cdn_url = response.url
                    if cdn_url not in photos:
                        photos.append(cdn_url)

                page.on("response", _on_response)
                await page.goto(url, wait_until="load", timeout=45_000)

                # Scroll to trigger lazy-loaded images, then wait for them
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(2_000)
                await page.evaluate("window.scrollTo(0, 0)")
                await page.wait_for_timeout(1_000)

                # Detect auth failure (redirected to login page)
                page_url = page.url
                if "login" in page_url.lower() or "signin" in page_url.lower():
                    raise IAAIAuthError(f"IAAI session expired or invalid — redirected to {page_url}")

                title_el = await page.query_selector("title")
                if title_el:
                    title = (await title_el.text_content() or "").lower()
                    if "sign in" in title or "login" in title:
                        raise IAAIAuthError("IAAI page title indicates login wall — session may be expired")

                # Strategy 2: DOM currentSrc fallback.
                # If network interception got nothing (e.g. direct CDN, no redirect),
                # read img.currentSrc — the browser has normalized these URLs (no \n).
                if not photos:
                    log.warning("IAAI: network intercept got 0 photos, trying DOM currentSrc fallback")
                    srcs = await page.evaluate("""
                        () => [...document.querySelectorAll('img')]
                            .map(img => img.currentSrc || img.src || '')
                            .filter(src => src.startsWith('https://') && src.length > 60)
                    """)
                    for src in srcs:
                        if src and src not in photos:
                            photos.append(src)
                            if len(photos) >= 20:
                                break

                condition_notes = await self._extract_damage(page)
            finally:
                await browser.close()

        log.warning("IAAI: %d photos (url=%s)", len(photos), url)
        return {"photos": photos[:20], "condition_notes": condition_notes}

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
