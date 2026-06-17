"""Dataset context for batch Salesforce dedupe queries."""

from __future__ import annotations

import math
import re
from typing import Any

from dedupe.constants import DEFAULT_RADIUS_METERS

_ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")


def extract_zip_code(record: dict[str, Any]) -> str | None:
    """Pull a 5-digit zip from explicit fields or the address string."""
    for key in ("zip_code", "zip", "postal_code"):
        normalized = _normalize_zip(record.get(key))
        if normalized:
            return normalized

    metadata = record.get("permit_metadata") or {}
    for key in ("zip_code", "zip", "postal_code", "GEO_ZIP_CODE"):
        normalized = _normalize_zip(metadata.get(key))
        if normalized:
            return normalized

    address = record.get("address") or ""
    match = _ZIP_RE.search(str(address))
    return match.group(1) if match else None


def extract_zip_codes(records: list[dict[str, Any]]) -> list[str]:
    """Return sorted unique zip codes present in a normalized dataset."""
    zips = {zip_code for record in records if (zip_code := extract_zip_code(record))}
    return sorted(zips)


def build_dataset_bounding_box(
    records: list[dict[str, Any]],
    *,
    meters: float = DEFAULT_RADIUS_METERS,
) -> dict[str, float] | None:
    """Expand the dataset-wide min/max lat/lng by ±meters (not per site)."""
    lats = [float(record["lat"]) for record in records if record.get("lat") is not None]
    lngs = [float(record["lng"]) for record in records if record.get("lng") is not None]
    if not lats or not lngs:
        return None

    min_lat, max_lat = min(lats), max(lats)
    min_lng, max_lng = min(lngs), max(lngs)
    delta_lat = meters / 111_320
    delta_lng = max(
        meters / (111_320 * math.cos(math.radians(min_lat))),
        meters / (111_320 * math.cos(math.radians(max_lat))),
    )
    return {
        "min_lat": min_lat - delta_lat,
        "max_lat": max_lat + delta_lat,
        "min_lng": min_lng - delta_lng,
        "max_lng": max_lng + delta_lng,
    }


def build_dataset_context(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize zip codes and expanded bounding box for a batch."""
    return {
        "zip_codes": extract_zip_codes(records),
        "bbox": build_dataset_bounding_box(records),
        "record_count": len(records),
    }


def _normalize_zip(value: Any) -> str | None:
    if value is None:
        return None
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    if len(digits) >= 5:
        return digits[:5]
    return None
