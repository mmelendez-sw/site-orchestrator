"""Free geocoding via US Census and OpenStreetMap Nominatim."""

from __future__ import annotations

import os
import re
import time
from typing import Any

import requests

CENSUS_GEOCODE_URL = (
    "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
)
CENSUS_BENCHMARK = "Public_AR_Current"
NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
GEOCODE_DELAY_S = 1.1
_ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")

GEOCODER = os.environ.get("GEOCODER", "auto").strip().lower()
GEOCODER_USER_AGENT = os.environ.get(
    "GEOCODER_USER_AGENT",
    "site-orchestrator/1.0 (cell-site imagery pipeline)",
)

_last_nominatim_at = 0.0


def _throttle_nominatim() -> None:
    global _last_nominatim_at
    elapsed = time.time() - _last_nominatim_at
    if elapsed < GEOCODE_DELAY_S:
        time.sleep(GEOCODE_DELAY_S - elapsed)
    _last_nominatim_at = time.time()


def _extract_zip_code(text: str | None) -> str | None:
    if not text:
        return None
    match = _ZIP_RE.search(str(text))
    return match.group(1) if match else None


def geocode_census(address: str) -> dict[str, Any] | None:
    """US Census Bureau oneline geocoder - free, no API key, CONUS-focused."""
    response = requests.get(
        CENSUS_GEOCODE_URL,
        params={
            "address": address,
            "benchmark": CENSUS_BENCHMARK,
            "format": "json",
        },
        timeout=30,
    )
    response.raise_for_status()
    matches = response.json().get("result", {}).get("addressMatches", [])
    if not matches:
        return None

    match = matches[0]
    coords = match["coordinates"]
    matched_address = match.get("matchedAddress") or address
    lng = float(coords["x"])
    lat = float(coords["y"])
    return {
        "lat": lat,
        "lng": lng,
        "lon": lng,
        "address": matched_address,
        "geocode_matched_address": matched_address,
        "zip_code": _extract_zip_code(matched_address),
        "geocode_source": "census",
        "geocode_quality": "census_match",
    }


def geocode_nominatim(address: str) -> dict[str, Any] | None:
    """OpenStreetMap Nominatim - free, worldwide, 1 req/sec usage policy."""
    _throttle_nominatim()
    response = requests.get(
        NOMINATIM_SEARCH_URL,
        params={"q": address, "format": "json", "limit": 1},
        headers={"User-Agent": GEOCODER_USER_AGENT},
        timeout=30,
    )
    response.raise_for_status()
    results = response.json()
    if not results:
        return None

    hit = results[0]
    matched_address = hit.get("display_name") or address
    lng = float(hit["lon"])
    lat = float(hit["lat"])
    postcode = (hit.get("address") or {}).get("postcode")
    return {
        "lat": lat,
        "lng": lng,
        "lon": lng,
        "address": matched_address,
        "geocode_matched_address": matched_address,
        "zip_code": _extract_zip_code(postcode) or _extract_zip_code(matched_address),
        "geocode_source": "nominatim",
        "geocode_quality": hit.get("type") or hit.get("class"),
    }


def geocode_address(address: str) -> dict[str, Any]:
    """Resolve a street address to coordinates. Raises RuntimeError if no match."""
    errors: list[str] = []

    if GEOCODER in ("auto", "census"):
        try:
            result = geocode_census(address)
            if result:
                return result
            errors.append("census: no match")
        except Exception as exc:
            errors.append(f"census: {exc}")

    if GEOCODER in ("auto", "nominatim"):
        try:
            result = geocode_nominatim(address)
            if result:
                return result
            errors.append("nominatim: no match")
        except Exception as exc:
            errors.append(f"nominatim: {exc}")

    raise RuntimeError(
        f"Geocoding failed ({'; '.join(errors)}): {address}"
    )


def geocode(address: str) -> dict[str, Any]:
    """Forward-geocode a street address for the ingest normalizer."""
    result = geocode_address(address)
    return {
        "lat": result["lat"],
        "lng": result["lng"],
        "address": result["geocode_matched_address"],
        "zip_code": result.get("zip_code"),
    }


def reverse_geocode(lat: float, lng: float) -> dict[str, Any]:
    """Reverse-geocode coordinates to a formatted address."""
    _throttle_nominatim()
    response = requests.get(
        NOMINATIM_REVERSE_URL,
        params={"lat": lat, "lon": lng, "format": "json"},
        headers={"User-Agent": GEOCODER_USER_AGENT},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload:
        raise RuntimeError(f"Reverse geocoding returned no results for {lat}, {lng}")

    matched_address = payload.get("display_name") or ""
    if not matched_address:
        raise RuntimeError(f"Reverse geocoding failed for {lat}, {lng}")

    postcode = (payload.get("address") or {}).get("postcode")
    return {
        "lat": lat,
        "lng": lng,
        "address": matched_address,
        "zip_code": _extract_zip_code(postcode) or _extract_zip_code(matched_address),
    }
