"""
One-time setup: create the 'vehicle-photos' Supabase Storage bucket.

Run once before deploying the worker:
  cd enrichment && python setup_storage.py
"""

import os
from supabase import create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

try:
    supabase.storage.create_bucket("vehicle-photos", options={"public": True})
    print("Created bucket: vehicle-photos (public)")
except Exception as e:
    if "already exists" in str(e).lower() or "Duplicate" in str(e):
        print("Bucket vehicle-photos already exists — nothing to do")
    else:
        raise
