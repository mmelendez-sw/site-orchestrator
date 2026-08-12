"""Reconcile Salesforce pin lat/lng against the geocoded street address."""

from __future__ import annotations

import os
from typing import Any

from dedupe.spatial import haversine_meters
from ingest.geocoder import geocode_address

# When pin and geocoded address diverge by this much, classify imagery on the
# address (building rooftop) instead of the pin (often a parking lot / ROW).
PIN_ADDRESS_MISMATCH_M = float(os.environ.get("PIN_ADDRESS_MISMATCH_M", "50"))


def format_site_geocode_query(site: dict[str, Any]) -> str | None:
    """Build a one-line address for Census/Nominatim from SF site fields."""
    parts = [
        str(site.get("Site_Street__c") or "").strip(),
        str(site.get("Site_City__c") or "").strip(),
        str(site.get("Site_State__c") or "").strip(),
        str(site.get("Site_Zip_Code__c") or "").strip(),
    ]
    street, city, state, zip_code = parts
    if not street:
        return None
    if city and state and zip_code:
        return f"{street}, {city}, {state} {zip_code}"
    if city and state:
        return f"{street}, {city}, {state}"
    if state:
        return f"{street}, {state}"
    return street


def reconcile_pin_to_address(
    site: dict[str, Any],
    sf_lat: float,
    sf_lng: float,
    *,
    mismatch_m: float | None = None,
) -> dict[str, Any]:
    """Geocode the SF street address and measure distance to the SF pin.

    Returns fields for enrichment_detail (empty geocode when address missing).
    """
    threshold = PIN_ADDRESS_MISMATCH_M if mismatch_m is None else float(mismatch_m)
    out: dict[str, Any] = {
        "address_query": "",
        "address_lat": "",
        "address_lng": "",
        "address_geocode_source": "",
        "address_matched": "",
        "pin_address_offset_m": "",
        "pin_address_mismatch": False,
        "pin_address_mismatch_m": threshold,
    }
    query = format_site_geocode_query(site)
    if not query:
        out["address_geocode_source"] = "no_street"
        return out
    out["address_query"] = query
    try:
        geo = geocode_address(query)
    except Exception as exc:  # noqa: BLE001
        out["address_geocode_source"] = f"error:{exc}"
        return out

    addr_lat = geo.get("lat")
    addr_lng = geo.get("lng") if geo.get("lng") is not None else geo.get("lon")
    if addr_lat is None or addr_lng is None:
        out["address_geocode_source"] = str(geo.get("geocode_source") or "failed")
        return out

    addr_lat_f = float(addr_lat)
    addr_lng_f = float(addr_lng)
    offset = haversine_meters(sf_lat, sf_lng, addr_lat_f, addr_lng_f)
    out["address_lat"] = addr_lat_f
    out["address_lng"] = addr_lng_f
    out["address_geocode_source"] = str(geo.get("geocode_source") or "")
    out["address_matched"] = str(
        geo.get("geocode_matched_address") or geo.get("address") or ""
    )
    out["pin_address_offset_m"] = round(offset, 1)
    out["pin_address_mismatch"] = bool(offset >= threshold)
    return out
