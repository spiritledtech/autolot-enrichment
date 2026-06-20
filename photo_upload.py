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
import os
from pathlib import PurePosixPath

import httpx
from PIL import Image
from supabase import create_client

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

    async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT, follow_redirects=True) as client:
        for i, url in enumerate(urls_to_process):
            try:
                resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                resp.raise_for_status()

                content = resp.content
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
                # Non-fatal: log and skip this photo
                import logging
                logging.getLogger(__name__).warning(
                    "Photo %d failed for vehicle %s (%s): %s", i, vehicle_id, url[:80], exc
                )

    return public_urls
