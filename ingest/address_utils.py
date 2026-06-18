"""Address parsing helpers shared by geocoding and dedupe."""

from __future__ import annotations

import re

# Prefer "ST 53212" or ", WI 53212" at end of line — not leading street numbers.
_STATE_ZIP_RE = re.compile(
    r"(?:,\s*)?[A-Z]{2}\s+(\d{5})(?:-\d{4})?\s*$",
    re.IGNORECASE,
)
_TRAILING_ZIP_RE = re.compile(r"(?:,\s*)(\d{5})(?:-\d{4})?\s*$")
_BARE_ZIP_RE = re.compile(r"^\d{5}$")
_ANY_ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")


def parse_zip_from_address(address: str | None) -> str | None:
    """Extract a US zip code from a formatted address string."""
    if not address:
        return None
    text = str(address).strip()
    if _BARE_ZIP_RE.match(text):
        return text
    match = _STATE_ZIP_RE.search(text)
    if match:
        return match.group(1)
    match = _TRAILING_ZIP_RE.search(text)
    if match:
        return match.group(1)
    matches = _ANY_ZIP_RE.findall(text)
    if matches:
        return matches[-1]
    return None
