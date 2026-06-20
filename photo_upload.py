"""
Photo Downloader + Supabase Storage Uploader

Downloads photos from auction listing URLs and uploads them to the
'vehicle-photos' Supabase Storage bucket. Returns an array of public URLs
to store in vehicles.photos (jsonb).

Bucket must be created before first use:
  Supabase dashboard → Storage → New bucket → "vehicle-photos" → Public
"""

import hashlib
import io
import logging
import os
import re
from pathlib import PurePosixPath
from urllib.parse import quote, urlsplit, urlunsplit

import aiohttp
from PIL import Image
from supabase import create_client

log = logging.getLogger(__name__)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
BUCKET = "vehicle-photos"
MAX_PHOTOS = 20
DOWNLOAD_TIMEOUT = 30  # seconds per photo
MAX_PHOTO_BYTES = 20 * 1024 * 1024  # 20 MB hard cap per image

_CONTENT_TYPE_MAP = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def _safe_url(url: str) -> str:
    """Percent-encode ASCII control characters in URL components.

    re.sub stripping \n has proven unreliable across Railway deploys.
    Encoding \n as %0A is equivalent: httpx accepts it, and IAAI's resizer
    decodes %0A server-side, so the image key resolves correctly.
    """
    try:
        p = urlsplit(url)
        safe_chars = "/:@!$&'()*+,;=~.-_"
        return urlunsplit((
            p.scheme,
            p.netloc,
            quote(p.path, safe=safe_chars),
            quote(p.query, safe=safe_chars + "?"),
            p.fragment,
        ))
    except Exception:
        return re.sub(r"[\x00-\x1f\x7f]", "", url)


def _ext_from_url(url: str) -> str:
    path = PurePosixPath(url.split("?")[0])
    ext = path.suffix.lower()
    return ext if ext in _CONTENT_TYPE_MAP else ".jpg"


def _storage_path(vehicle_id: str, photo_url: str, index: int) -> str:
    url_hash = hashlib.sha256(photo_url.encode()).hexdigest()[:8]
    return f"{vehicle_id}/{index:02d}_{url_hash}.jpg"


def _compress(content: bytes) -> bytes:
    """Resize to max 1200px wide and re-encode as JPEG 75%. Returns compressed bytes."""
    img = Image.open(io.BytesIO(content)).convert("RGB")
    if img.width > 1200:
        new_height = int(img.height * 1200 / img.width)
        img = img.resize((1200, new_height), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=75, optimize=True)
    return buf.getvalue()


async def upload_photos(vehicle_id: str, photo_urls: list[str]) -> list[str]:
    """
    Download up to MAX_PHOTOS images from photo_urls and upload to Supabase Storage.
    Skips individual photos that fail — partial success is fine.
    Returns list of public Supabase Storage URLs.
    """
    supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
    public_urls: list[str] = []
    urls_to_process = photo_urls[:MAX_PHOTOS]

    _HEADERS = {"User-Agent": "Mozilla/5.0"}
    # Use aiohttp instead of httpx — httpx 0.28.1 raises InvalidURL for IAAI's
    # resizer URLs even after sanitization due to an internal validation quirk
    # with the ~ characters in imageKeys. aiohttp/yarl is more lenient.
    timeout = aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for i, url in enumerate(urls_to_process):
            url = _safe_url(url)
            if not url.startswith(("http://", "https://")):
                continue
            try:
                async with session.get(url, headers=_HEADERS, allow_redirects=True) as resp:
                    resp.raise_for_status()
                    content = await resp.read()
                if len(content) > MAX_PHOTO_BYTES:
                    continue  # skip oversized

                content = _compress(content)
                storage_path = _storage_path(vehicle_id, url, i)

                supabase.storage.from_(BUCKET).upload(
                    path=storage_path,
                    file=content,
                    file_options={"content-type": "image/jpeg", "upsert": "true"},
                )

                public_url = (
                    f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/{storage_path}"
                )
                public_urls.append(public_url)

            except Exception as exc:
                log.warning("Photo %d failed for vehicle %s (%r): %s", i, vehicle_id, url[:150], exc)

    return public_urls
