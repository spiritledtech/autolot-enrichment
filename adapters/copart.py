"""
Copart Auction Listing Adapter

Fetches a Copart vehicle listing and extracts:
  - Photo URLs (full-size, not thumbnails)
  - Condition/damage notes

IMPORTANT — DOM selectors need validation against live Copart pages.
Copart is a JS-heavy SPA with Cloudflare protection. If PlayWright
fetches start failing, check:
  1. Whether Scrapling/Playwright is still bypassing Cloudflare
  2. Whether the CSS selectors below still match (Copart updates them often)
  3. IAAI cookies (separate env var, separate adapter)

Selector audit: open a Copart lot page, right-click a photo → Inspect.
Find the img tag's class and update PHOTO_SELECTORS below.
"""

import logging
import re

from scrapling.fetchers import PlayWrightFetcher as PlayWright

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
        page = await PlayWright(
            auto_match=True,
            headless=True,
            network_idle=True,          # wait for SPA to finish loading
            timeout=30_000,
        ).async_fetch(url)

        photos = self._extract_photos(page)
        condition_notes = self._extract_damage(page)

        log.info("Copart: %d photos, condition_notes=%r (url=%s)", len(photos), condition_notes[:40] if condition_notes else "", url)

        return {"photos": photos, "condition_notes": condition_notes}

    def _extract_photos(self, page) -> list[str]:
        photos: list[str] = []
        for selector in PHOTO_SELECTORS:
            elements = page.css(selector)
            if not elements:
                continue
            for el in elements:
                src = el.attrib.get("src", "")
                if not src or "thumbnail" in src or "/th/" in src:
                    continue
                # Copart thumbnail URLs contain /th/ or have small dimension hints.
                # Convert known thumbnail patterns to full-size.
                src = re.sub(r"/[A-Z]{1,3}_pic/", "/f/", src)
                if src not in photos:
                    photos.append(src)
            if photos:
                break  # found photos with this selector, stop trying others
        return photos[:20]

    def _extract_damage(self, page) -> str:
        for selector in DAMAGE_SELECTORS:
            elements = page.css(selector)
            if elements:
                text = elements[0].text or ""
                text = text.strip()
                if text:
                    return text
        return ""
