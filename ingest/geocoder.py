"""Google Maps Geocoding API helpers."""

from __future__ import annotations

import os
from typing import Any

import requests

_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"


def _api_key() -> str:
    key = os.environ.get("GEOCODE_API_KEY", "").strip()
    if not key:
        raise RuntimeError("GEOCODE_API_KEY is not set")
    return key


def _first_result(payload: dict[str, Any]) -> dict[str, Any]:
    status = payload.get("status")
    if status != "OK":
        raise RuntimeError(f"Geocoding failed: {status}")
    results = payload.get("results") or []
    if not results:
        raise RuntimeError("Geocoding returned no results")
    return results[0]


def _extract_postal_code(result: dict[str, Any]) -> str | None:
    for component in result.get("address_components") or []:
        if "postal_code" in component.get("types", []):
            return component.get("short_name")
    return None


def geocode(address: str) -> dict[str, Any]:
    """Forward-geocode a street address to lat/lng and formatted address."""
    response = requests.get(
        _GEOCODE_URL,
        params={"address": address, "key": _api_key()},
        timeout=30,
    )
    response.raise_for_status()
    result = _first_result(response.json())
    location = result["geometry"]["location"]
    payload = {
        "lat": location["lat"],
        "lng": location["lng"],
        "address": result.get("formatted_address", address),
    }
    zip_code = _extract_postal_code(result)
    if zip_code:
        payload["zip_code"] = zip_code
    return payload


def reverse_geocode(lat: float, lng: float) -> dict[str, Any]:
    """Reverse-geocode coordinates to a formatted address."""
    response = requests.get(
        _GEOCODE_URL,
        params={"latlng": f"{lat},{lng}", "key": _api_key()},
        timeout=30,
    )
    response.raise_for_status()
    result = _first_result(response.json())
    location = result["geometry"]["location"]
    payload = {
        "lat": location["lat"],
        "lng": location["lng"],
        "address": result.get("formatted_address", ""),
    }
    zip_code = _extract_postal_code(result)
    if zip_code:
        payload["zip_code"] = zip_code
    return payload
