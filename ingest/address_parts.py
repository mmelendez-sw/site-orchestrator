"""Parse US-style addresses into Salesforce upload components."""

from __future__ import annotations

import re

from dedupe.address_match import extract_city_from_address, extract_street_line_raw
from ingest.address_utils import parse_zip_from_address

_STATE_ZIP_TAIL_RE = re.compile(
    r",\s*([^,]+),\s*([A-Z]{2})\s+(\d{5})(?:-\d{4})?\s*$",
    re.IGNORECASE,
)
_STATE_ONLY_RE = re.compile(r",\s*([A-Z]{2})\s+\d{5}(?:-\d{4})?\s*$", re.IGNORECASE)


def parse_address_components(
    address: str | None,
    *,
    zip_code: str | None = None,
    country: str | None = None,
) -> dict[str, str | None]:
    """Split a formatted address into street, city, state, zip, and country."""
    text = str(address or "").strip()
    if not text:
        return {
            "site_street": None,
            "site_city": None,
            "site_state": None,
            "zip_code": zip_code,
            "site_country": country or "US",
        }

    resolved_zip = zip_code or parse_zip_from_address(text)
    city = extract_city_from_address(text)
    state: str | None = None

    tail = _STATE_ZIP_TAIL_RE.search(text)
    if tail:
        if not city:
            city = tail.group(1).strip()
        state = tail.group(2).upper()
        if not resolved_zip:
            resolved_zip = tail.group(3)
    else:
        state_match = _STATE_ONLY_RE.search(text)
        if state_match:
            state = state_match.group(1).upper()

    if state is None and re.search(r"\bDC\b", text.upper()):
        state = "DC"

    street = extract_street_line_raw(text)
    return {
        "site_street": street or None,
        "site_city": city,
        "site_state": state,
        "zip_code": resolved_zip,
        "site_country": country or "US",
    }
