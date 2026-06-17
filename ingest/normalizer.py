"""Normalize ingest records into canonical site dicts."""

from __future__ import annotations

import math
from typing import Any

from dedupe.context import extract_zip_code
from ingest.geocoder import geocode, reverse_geocode
from ingest.scraper import IngestRecord

# ~111 km per degree latitude; used for address/coordinate alignment checks.
_METERS_PER_DEG_LAT = 111_320
_MAX_ALIGNMENT_METERS = 250


def _has_coords(record: IngestRecord) -> bool:
    return record.lat is not None and record.lng is not None


def _has_address(record: IngestRecord) -> bool:
    return bool(record.address and str(record.address).strip())


def _distance_meters(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    dlat = (lat2 - lat1) * _METERS_PER_DEG_LAT
    dlng = (lng2 - lng1) * _METERS_PER_DEG_LAT * math.cos(math.radians(lat1))
    return math.hypot(dlat, dlng)


def normalize(record: IngestRecord | dict[str, Any]) -> dict[str, Any]:
    """Return a canonical record with lat, lng, address, and permit_metadata."""
    if isinstance(record, dict):
        ingest = IngestRecord(
            address=record.get("address"),
            lat=record.get("lat"),
            lng=record.get("lng"),
            permit_metadata=dict(record.get("permit_metadata") or {}),
            source_url=record.get("source_url"),
        )
    else:
        ingest = record

    lat = ingest.lat
    lng = ingest.lng
    address = ingest.address.strip() if _has_address(ingest) else None
    geo_zip: str | None = None

    if _has_address(ingest) and not _has_coords(ingest):
        geo = geocode(address)
        lat, lng = geo["lat"], geo["lng"]
        address = geo["address"]
        geo_zip = geo.get("zip_code")
    elif _has_coords(ingest) and not _has_address(ingest):
        geo = reverse_geocode(lat, lng)
        lat, lng = geo["lat"], geo["lng"]
        address = geo["address"]
        geo_zip = geo.get("zip_code")
    elif _has_coords(ingest) and _has_address(ingest):
        geo = geocode(address)
        distance = _distance_meters(lat, lng, geo["lat"], geo["lng"])
        if distance > _MAX_ALIGNMENT_METERS:
            raise ValueError(
                f"Address and coordinates disagree by {distance:.0f}m "
                f"(max {_MAX_ALIGNMENT_METERS}m)"
            )
        address = geo["address"]
        geo_zip = geo.get("zip_code")

    if lat is None or lng is None or not address:
        raise ValueError("Record must resolve to lat, lng, and address")

    canonical = {
        "lat": lat,
        "lng": lng,
        "address": address,
        "permit_metadata": dict(ingest.permit_metadata),
    }
    zip_code = (
        ingest.permit_metadata.get("zip_code")
        or geo_zip
        or extract_zip_code({"address": address})
    )
    if zip_code:
        canonical["zip_code"] = zip_code
    if ingest.source_url:
        canonical["source_url"] = ingest.source_url
    return canonical
