"""
Alert system for AutoLot enrichment worker.

Two alert triggers (per engineering design):
  1. IAAI session auth failure  → immediate email to admin
  2. >20% failed enrichment_status in a rolling 24h window → alert email

Uses SMTP (configured via env vars). Sendgrid, Mailgun, or any SMTP relay works.
For Railway/Render: set ALERT_SMTP_* env vars.
"""

import logging
import os
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

from supabase import Client

log = logging.getLogger(__name__)

ADMIN_EMAIL = os.getenv("ALERT_ADMIN_EMAIL", "spiritledtech@gmail.com")
SMTP_HOST = os.getenv("ALERT_SMTP_HOST", "")
SMTP_PORT = int(os.getenv("ALERT_SMTP_PORT", "587"))
SMTP_USER = os.getenv("ALERT_SMTP_USER", "")
SMTP_PASS = os.getenv("ALERT_SMTP_PASS", "")
FROM_EMAIL = os.getenv("ALERT_FROM_EMAIL", SMTP_USER)

FAILURE_RATE_THRESHOLD = 0.20  # 20% of new vehicles in 24h
MIN_VEHICLES_FOR_RATE_CHECK = 3  # skip rate check if fewer than this were processed


def send_alert(subject: str, body: str) -> None:
    """
    Send an alert email. Logs the alert regardless of whether SMTP is configured.
    If SMTP is not configured, logs a warning — the alert is not silently dropped.
    """
    log.error("ALERT: %s\n%s", subject, body)

    if not SMTP_HOST or not SMTP_USER or not SMTP_PASS:
        log.warning(
            "SMTP not configured (ALERT_SMTP_HOST/USER/PASS) — alert not emailed. "
            "Set these env vars in Railway/Render to receive email alerts."
        )
        return

    msg = MIMEText(body)
    msg["Subject"] = f"[AutoLot] {subject}"
    msg["From"] = FROM_EMAIL
    msg["To"] = ADMIN_EMAIL

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(SMTP_USER, SMTP_PASS)
            smtp.sendmail(FROM_EMAIL, [ADMIN_EMAIL], msg.as_string())
        log.info("Alert email sent to %s: %s", ADMIN_EMAIL, subject)
    except Exception as exc:
        log.error("Failed to send alert email: %s", exc)


async def check_failure_rate(supabase: Client) -> None:
    """
    Check whether >20% of vehicles enriched in the last 24 hours have
    enrichment_status='failed'. If so, send an alert.

    Called every FAILURE_CHECK_INTERVAL seconds from the main loop.
    """
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

    result = (
        supabase.table("vehicles")
        .select("enrichment_status")
        .gte("updated_at", since)
        .in_("enrichment_status", ["complete", "failed"])
        .execute()
    )

    rows = result.data or []
    total = len(rows)
    if total < MIN_VEHICLES_FOR_RATE_CHECK:
        return

    failed = sum(1 for r in rows if r["enrichment_status"] == "failed")
    rate = failed / total

    if rate > FAILURE_RATE_THRESHOLD:
        send_alert(
            subject=f"High enrichment failure rate: {rate:.0%} in last 24h",
            body=(
                f"AutoLot enrichment failure rate alert.\n\n"
                f"In the last 24 hours:\n"
                f"  Total vehicles processed: {total}\n"
                f"  Failed: {failed} ({rate:.0%})\n\n"
                f"Likely causes:\n"
                f"  • Auction site DOM changed (update CSS selectors in adapters/)\n"
                f"  • IAAI session cookies expired (update IAAI_SESSION_COOKIES env var)\n"
                f"  • Scrapling/Playwright update broke compatibility\n\n"
                f"Check Railway/Render logs for details."
            ),
        )
