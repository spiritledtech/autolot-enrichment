"""
Copart Auction Listing Adapter

Fetches a Copart vehicle listing and extracts:
  - Photo URLs (full-size, not thumbnails)
  - Condition/damage notes

IMPORTANT — DOM selectors need validation against live Copart pages.
Copart is a JS-heavy SPA with Cloudflare protection. If fetches start
failing, check:
  1. Whether Playwright is still bypassing Cloudflare
  2. Whether the CSS selectors below still match (Copart updates them often)

Selector audit: open a Copart lot page, right-click a photo → Inspect.
Find the img tag's class and update PHOTO_SELECTORS below.
"""

import logging
import re

from playwright.async_api import async_playwright

log = logging.getLogger(__name__)

# CSS selectors to try in order — first match wins.
# Copart photo containers. Update these after verifying on a live lot page.
PHOTO_SELECTORS = [
    "img[src*='cs.copart.com'][src*='/f/']",      # full-size images (preferred)
    "img[src*='cs.copart.com']",                   # any Copart CDN image
    ".lot-images img",                              # lot image container
    "[data-uname*='image'] img",
]

DAMAGE_SELECTORS = [
    "[data-uname='lotdetailDamageDescriptionValue']",
    ".damage-description",
    "[class*='damage']",
]


class CopartAdapter:
    async def fetch(self, url: str) -> dict:
        """
        Fetch a Copart listing and extract photos + condition notes.
        Returns {"photos": [...], "condition_notes": "..."}.
        """
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            try:
                page = await browser.new_page()
                await page.goto(url, wait_until="load", timeout=30_000)
                await page.wait_for_timeout(3_000)
                photos = await self._extract_photos(page)
                condition_notes = await self._extract_damage(page)
            finally:
                await browser.close()

        log.info("Copart: %d photos, condition_notes=%r (url=%s)", len(photos), condition_notes[:40] if condition_notes else "", url)
        return {"photos": photos, "condition_notes": condition_notes}

    async def _extract_photos(self, page) -> list[str]:
        photos: list[str] = []
        for selector in PHOTO_SELECTORS:
            elements = await page.query_selector_all(selector)
            if not elements:
                continue
            for el in elements:
                src = (await el.get_attribute("src") or "").strip()
                if not src or "thumbnail" in src or "/th/" in src:
                    continue
                src = re.sub(r"/[A-Z]{1,3}_pic/", "/f/", src)
                if src not in photos:
                    photos.append(src)
            if photos:
                break
        return photos[:20]

    async def _extract_damage(self, page) -> str:
        for selector in DAMAGE_SELECTORS:
            elements = await page.query_selector_all(selector)
            if elements:
                text = (await elements[0].text_content() or "").strip()
                if text:
                    return text
        return ""
