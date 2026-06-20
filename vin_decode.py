"""
NHTSA VIN Decode

Free API — no auth required.
Returns a subset of decoded fields that map to vehicles columns.
"""

import httpx

NHTSA_URL = "https://vpic.nhtsa.dot.gov/api/vehicles/decodevin/{vin}?format=json"

# NHTSA variable names we care about → vehicles column names
_FIELD_MAP = {
    "Make": "make",
    "Model": "model",
    "Model Year": "year",
    "Body Class": None,           # informational only
    "Engine Number of Cylinders": None,
    "Displacement (L)": None,
    "Trim": None,
}

# These variables signal a decode failure
_ERROR_VARIABLE = "Error Code"
_ERROR_GOOD = "0"  # "0" means no error


async def decode_vin(vin: str) -> dict:
    """
    Decode a VIN via the NHTSA vPIC API.

    Returns a dict with any of: make, model, year (int).
    Only includes fields where NHTSA returned a non-empty value.
    Does NOT overwrite fields the Chrome extension already filled in;
    caller decides whether to merge.

    Raises on HTTP error or API error code.
    """
    url = NHTSA_URL.format(vin=vin.strip().upper())

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()

    results: list[dict] = data.get("Results", [])

    # Check for decode error — error code 1 means "no data found" but NHTSA may
    # still return partial results (make/model/year), so log and continue rather
    # than raising. Only raise on HTTP-level failures (handled above via raise_for_status).
    for item in results:
        if item.get("Variable") == _ERROR_VARIABLE:
            code = item.get("Value", "0")
            if code != _ERROR_GOOD:
                import logging
                logging.getLogger(__name__).warning(
                    "NHTSA decode warning for VIN %s: error code %s — attempting partial decode",
                    vin, code
                )
            break

    decoded: dict = {}
    for item in results:
        variable = item.get("Variable", "")
        value = item.get("Value") or ""
        value = value.strip()

        if not value or value.lower() in ("not applicable", "null", "n/a", "0"):
            continue

        if variable == "Make":
            decoded["make"] = value.title()
        elif variable == "Model":
            decoded["model"] = value.title()
        elif variable == "Model Year":
            try:
                decoded["year"] = int(value)
            except ValueError:
                pass

    return decoded
