"""Distance helpers and Census geocode for pin vs street-address checks."""

from __future__ import annotations

import json
import logging
import math
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from enrichment.constants import PIN_ADDRESS_MISMATCH_M, ROOFTOP_HOST_OFFSET_M

logger = logging.getLogger(__name__)

CENSUS_GEOCODER_URL = (
    "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
)
GEOCODE_TIMEOUT_S = float(os.environ.get("GEOCODE_TIMEOUT_S", "5"))


def haversine_meters(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Return great-circle distance in meters between two WGS84 points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lng2 - lng1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * 6_371_000 * math.asin(math.sqrt(a))


def geocode_address_enabled() -> bool:
    return os.environ.get("GEOCODE_ADDRESS", "1").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def build_site_address(site: dict[str, Any]) -> str | None:
    """Compose a one-line address from Salesforce street/city/state/zip."""
    street = str(site.get("Site_Street__c") or "").strip()
    if not street:
        return None
    city = str(site.get("Site_City__c") or "").strip()
    state = str(site.get("Site_State__c") or "").strip()
    zipc = str(site.get("Site_Zip_Code__c") or "").strip()
    locality = ", ".join(p for p in (city, f"{state} {zipc}".strip()) if p)
    return f"{street}, {locality}" if locality else street


def parse_census_geocode_payload(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """Pull the first Census address match. None if empty or malformed."""
    if not payload:
        return None
    matches = (payload.get("result") or {}).get("addressMatches") or []
    if not isinstance(matches, list) or not matches:
        return None
    first = matches[0] if isinstance(matches[0], dict) else None
    if not first:
        return None
    coords = first.get("coordinates") or {}
    try:
        lng = float(coords.get("x"))
        lat = float(coords.get("y"))
    except (TypeError, ValueError):
        return None
    matched = str(first.get("matchedAddress") or "").strip()
    return {
        "lat": lat,
        "lng": lng,
        "matched": matched,
        "source": "census",
    }


def geocode_census(address: str) -> dict[str, Any] | None:
    """Geocode a US address via Census. Fail-open (None) on any error."""
    text = str(address or "").strip()
    if not text or not geocode_address_enabled():
        return None
    params = urllib.parse.urlencode(
        {
            "address": text,
            "benchmark": "Public_AR_Current",
            "format": "json",
        }
    )
    url = f"{CENSUS_GEOCODER_URL}?{params}"
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "site-orchestrator/enrichment"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=GEOCODE_TIMEOUT_S) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (
        urllib.error.URLError,
        TimeoutError,
        ValueError,
        OSError,
        json.JSONDecodeError,
    ) as exc:
        logger.info("Census geocode skipped: %s", exc)
        return None
    if not isinstance(payload, dict):
        return None
    return parse_census_geocode_payload(payload)


def pin_address_is_mismatch(offset_m: float | None) -> bool:
    if offset_m is None:
        return False
    return float(offset_m) >= float(PIN_ADDRESS_MISMATCH_M)


def should_compare_rooftop_hosts(
    offset_m: float | None, *, db_backed: bool = False
) -> bool:
    """No FCC/TowerSource hit: pin vs Census when they are not the same parcel.

    Towers are DB-anchored. Rooftops sit on a building; a 25 m parking-lot pin
    is enough to look at the street geocode before buying Nearmap.
    """
    if db_backed or offset_m is None:
        return False
    return float(offset_m) >= float(ROOFTOP_HOST_OFFSET_M)


def osm_anchor_score(osm: dict[str, Any] | None) -> int:
    """Rooftop-first OSM preference: host building matters; towers still win."""
    if not osm or not osm.get("ok"):
        return 0
    score = 0
    if osm.get("communication_tower"):
        score += 40
    if osm.get("has_tower_or_mast"):
        score += 25
    if osm.get("has_building"):
        score += 25
    return score


def naip_anchor_score(res: dict[str, Any] | None) -> int:
    """NAIP screen preference. Weak rooftop/tower labels do not win."""
    if not res:
        return 0
    site = str(res.get("site_type") or "").strip().lower()
    try:
        conf = float(res.get("site_confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    if site == "tower" and conf >= 0.6:
        return 50
    if site == "rooftop" and conf >= 0.6:
        return 40
    if site == "tower":
        return 15
    if site == "rooftop":
        return 12
    return 0


def pick_classify_anchor(
    *,
    pin_osm: dict[str, Any] | None = None,
    address_osm: dict[str, Any] | None = None,
    pin_naip: dict[str, Any] | None = None,
    address_naip: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Choose SF pin vs Census address before buying Nearmap.

    Tie keeps the pin: Google rooftop-snap is often the building centroid,
    while Census interpolates the street. Callers should run this on the
    no-DB (rooftop) path only.
    """
    pin_s = osm_anchor_score(pin_osm) + naip_anchor_score(pin_naip)
    addr_s = osm_anchor_score(address_osm) + naip_anchor_score(address_naip)
    if addr_s > pin_s:
        return "address", f"address score {addr_s} > pin {pin_s}"
    if pin_s > addr_s:
        return "pin", f"pin score {pin_s} > address {addr_s}"
    return "pin", "tie — keep SF pin"


def mismatch_osm_is_decisive(
    pin_osm: dict[str, Any] | None,
    address_osm: dict[str, Any] | None,
) -> bool:
    """True when OSM already disagrees, so skip a dual NAIP screen."""
    return osm_anchor_score(pin_osm) != osm_anchor_score(address_osm)
