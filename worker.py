"""
AutoLot Enrichment Worker

Polls enrichment_jobs every POLL_INTERVAL_SECONDS. For each pending job:
  1. Claim it atomically (set status='processing', increment attempts)
  2. Fetch vehicle record
  3. VIN decode via NHTSA
  4. Scrape auction listing for photos + condition notes
  5. Upload photos to Supabase Storage
  6. Write enriched fields back; mark job complete
  7. On failure: retry up to MAX_ATTEMPTS, then mark failed

Stale jobs (stuck in 'processing' > STALE_MINUTES) are reset to 'pending'
on each poll cycle to recover from crashed runs.
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone

from supabase import create_client, Client

from adapters.copart import CopartAdapter
from adapters.iaai import IAAIAdapter, IAAIAuthError
from vin_decode import decode_vin
from photo_upload import upload_photos
from alerts import check_failure_rate, send_alert

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "10"))
MAX_ATTEMPTS = 3
STALE_MINUTES = 5
FAILURE_CHECK_INTERVAL = 300  # seconds between failure-rate checks

log = logging.getLogger(__name__)


def get_supabase() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def reset_stale_jobs(supabase: Client) -> None:
    """Reset jobs stuck in 'processing' for over STALE_MINUTES (crash recovery)."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=STALE_MINUTES)
    ).isoformat()
    result = (
        supabase.table("enrichment_jobs")
        .update({"status": "pending"})
        .eq("status", "processing")
        .lt("updated_at", cutoff)
        .execute()
    )
    if result.data:
        log.warning("Reset %d stale processing jobs", len(result.data))


def claim_job(supabase: Client) -> dict | None:
    """
    Claim the oldest pending job. Returns the claimed job dict or None.

    Two-step: SELECT then UPDATE with a status guard. Safe for a single
    worker; if we ever run multiple workers, replace with a Postgres RPC
    that uses FOR UPDATE SKIP LOCKED.
    """
    candidates = (
        supabase.table("enrichment_jobs")
        .select("id, vehicle_id, attempts, status")
        .eq("status", "pending")
        .order("created_at")
        .limit(1)
        .execute()
    )
    if not candidates.data:
        return None

    job = candidates.data[0]
    new_attempts = job["attempts"] + 1

    # Guard: only claim if still pending (prevents double-processing)
    claimed = (
        supabase.table("enrichment_jobs")
        .update({"status": "processing", "attempts": new_attempts})
        .eq("id", job["id"])
        .eq("status", "pending")
        .execute()
    )
    if not claimed.data:
        return None  # Another worker claimed it between SELECT and UPDATE

    claimed_job = claimed.data[0]
    claimed_job["attempts"] = new_attempts
    return claimed_job


def get_vehicle(supabase: Client, vehicle_id: str) -> dict | None:
    result = (
        supabase.table("vehicles")
        .select("*")
        .eq("id", vehicle_id)
        .single()
        .execute()
    )
    return result.data


def mark_complete(supabase: Client, job_id: str, vehicle_id: str, enriched: dict) -> None:
    supabase.table("enrichment_jobs").update({"status": "complete"}).eq("id", job_id).execute()
    update_payload = {"enrichment_status": "complete"}
    update_payload.update(enriched)
    supabase.table("vehicles").update(update_payload).eq("id", vehicle_id).execute()
    log.info("Vehicle %s enriched: %s", vehicle_id, list(enriched.keys()))


def mark_failed_or_retry(supabase: Client, job_id: str, vehicle_id: str, attempts: int) -> None:
    if attempts >= MAX_ATTEMPTS:
        supabase.table("enrichment_jobs").update({"status": "failed"}).eq("id", job_id).execute()
        supabase.table("vehicles").update({"enrichment_status": "failed"}).eq("id", vehicle_id).execute()
        log.error("Vehicle %s permanently failed after %d attempts", vehicle_id, attempts)
    else:
        # Reset to pending — will be picked up on next poll
        supabase.table("enrichment_jobs").update({"status": "pending"}).eq("id", job_id).execute()
        log.warning(
            "Vehicle %s failed (attempt %d/%d) — queued for retry",
            vehicle_id, attempts, MAX_ATTEMPTS,
        )


async def enrich_vehicle(vehicle: dict) -> dict:
    """
    Run the full enrichment pipeline for one vehicle.
    Returns a dict of fields to write back to vehicles.
    Partial success is fine — we write whatever we got.
    """
    enriched: dict = {}

    # 1. VIN decode via NHTSA (free, no auth, reliable)
    vin = vehicle.get("vin")
    if vin:
        try:
            vin_data = await decode_vin(vin)
            enriched.update(vin_data)
            log.info("VIN decoded: %s → %s", vin, vin_data)
        except Exception as exc:
            log.warning("VIN decode failed for %s: %s", vin, exc)

    # 2. Scrape auction listing for photos + condition data
    listing_url: str | None = vehicle.get("auction_listing_url")
    auction_source: str | None = vehicle.get("auction_source")

    if listing_url:
        adapter = None
        if auction_source == "copart" or (listing_url and "copart.com" in listing_url):
            adapter = CopartAdapter()
        elif auction_source == "iaai" or (listing_url and "iaai.com" in listing_url):
            adapter = IAAIAdapter()

        if adapter:
            try:
                scraped = await adapter.fetch(listing_url)

                # Only backfill condition_notes if not already set
                if scraped.get("condition_notes") and not vehicle.get("condition_notes"):
                    enriched["condition_notes"] = scraped["condition_notes"]

                # 3. Upload photos to Supabase Storage
                raw_urls: list[str] = scraped.get("photos", [])
                if raw_urls:
                    try:
                        public_urls = await upload_photos(vehicle["id"], raw_urls)
                        if public_urls:
                            enriched["photos"] = public_urls
                            log.info("Uploaded %d photos for vehicle %s", len(public_urls), vehicle["id"])
                    except Exception as exc:
                        log.warning("Photo upload failed for vehicle %s: %s", vehicle["id"], exc)

            except IAAIAuthError as exc:
                # Session cookie expired — alert immediately so 818 admin can refresh
                send_alert(
                    subject="IAAI session expired — enrichment blocked",
                    body=(
                        f"IAAI authentication failed while processing vehicle {vehicle['id']}.\n\n"
                        f"Error: {exc}\n\n"
                        f"To fix:\n"
                        f"  1. Log into iaai.com in a browser\n"
                        f"  2. Open DevTools → Application → Cookies → iaai.com\n"
                        f"  3. Copy all cookies as JSON\n"
                        f"  4. Update IAAI_SESSION_COOKIES env var in Railway/Render\n"
                        f"  5. Restart the worker\n\n"
                        f"Until fixed, IAAI vehicles will fail enrichment (partial records accepted)."
                    ),
                )
                raise  # re-raise so the job is retried / marked failed normally
            except Exception as exc:
                log.warning("Listing scrape failed for %s: %s", listing_url, exc)
        else:
            log.debug("No adapter for auction_source=%r url=%r", auction_source, listing_url)

    return enriched


async def process_one(supabase: Client) -> bool:
    """Claim and process one job. Returns True if a job was processed."""
    job = claim_job(supabase)
    if not job:
        return False

    job_id = job["id"]
    vehicle_id = job["vehicle_id"]
    attempts = job["attempts"]

    vehicle = get_vehicle(supabase, vehicle_id)
    if not vehicle:
        log.error("Vehicle %s not found for job %s — marking failed", vehicle_id, job_id)
        mark_failed_or_retry(supabase, job_id, vehicle_id, MAX_ATTEMPTS)
        return True

    try:
        enriched = await enrich_vehicle(vehicle)
        mark_complete(supabase, job_id, vehicle_id, enriched)
    except Exception as exc:
        log.error("Enrichment pipeline error for vehicle %s (attempt %d): %s", vehicle_id, attempts, exc)
        mark_failed_or_retry(supabase, job_id, vehicle_id, attempts)

    return True


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log.info("AutoLot enrichment worker starting — polling every %ds", POLL_INTERVAL)
    supabase = get_supabase()
    last_failure_check = 0.0

    while True:
        try:
            reset_stale_jobs(supabase)
            worked = await process_one(supabase)

            now = time.monotonic()
            if now - last_failure_check > FAILURE_CHECK_INTERVAL:
                await check_failure_rate(supabase)
                last_failure_check = now

            if not worked:
                await asyncio.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            log.info("Worker stopped by user")
            break
        except Exception as exc:
            log.error("Unexpected error in main loop: %s", exc, exc_info=True)
            await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
