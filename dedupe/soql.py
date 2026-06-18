"""SOQL query builders for spatial site lookups."""

from __future__ import annotations

from dedupe.constants import (
    SF_ADDRESS_FIELD,
    SF_CITY_FIELD,
    SF_LAT_FIELD,
    SF_LNG_FIELD,
    SF_OBJECT_NAME,
    SF_STATE_FIELD,
    SF_ZIP_FIELD,
)


def _quote_zips(zip_codes: list[str]) -> str:
    return ", ".join(f"'{zip_code}'" for zip_code in zip_codes)


def _quote_coord(value: float) -> str:
    """Quote lat/lng for SOQL — UAT Site__c geo fields are stored as text."""
    return f"'{value}'"


def build_bbox_query(
    min_lat: float,
    max_lat: float,
    min_lng: float,
    max_lng: float,
    *,
    object_name: str = SF_OBJECT_NAME,
    lat_field: str = SF_LAT_FIELD,
    lng_field: str = SF_LNG_FIELD,
    zip_field: str = SF_ZIP_FIELD,
    address_field: str = SF_ADDRESS_FIELD,
) -> str:
    """Build a SOQL query for site records within a lat/lng bounding box."""
    return (
        f"SELECT Id, Name, {lat_field}, {lng_field}, {address_field}, "
        f"{SF_CITY_FIELD}, {SF_STATE_FIELD}, {zip_field} "
        f"FROM {object_name} "
        f"WHERE {lat_field} >= {_quote_coord(min_lat)} AND {lat_field} <= {_quote_coord(max_lat)} "
        f"AND {lng_field} >= {_quote_coord(min_lng)} AND {lng_field} <= {_quote_coord(max_lng)}"
    )


def build_dedupe_query(
    zip_codes: list[str],
    bbox: dict[str, float] | None,
    *,
    object_name: str = SF_OBJECT_NAME,
    lat_field: str = SF_LAT_FIELD,
    lng_field: str = SF_LNG_FIELD,
    zip_field: str = SF_ZIP_FIELD,
    address_field: str = SF_ADDRESS_FIELD,
) -> str:
    """Build SOQL matching dataset zip codes OR the expanded dataset bounding box."""
    clauses: list[str] = []
    if zip_codes:
        clauses.append(f"{zip_field} IN ({_quote_zips(zip_codes)})")
    if bbox:
        clauses.append(
            f"({lat_field} >= {_quote_coord(bbox['min_lat'])} "
            f"AND {lat_field} <= {_quote_coord(bbox['max_lat'])} "
            f"AND {lng_field} >= {_quote_coord(bbox['min_lng'])} "
            f"AND {lng_field} <= {_quote_coord(bbox['max_lng'])})"
        )
    if not clauses:
        raise ValueError("Dedupe query requires zip codes and/or a bounding box")

    where = " OR ".join(clauses)
    return (
        f"SELECT Id, Name, {lat_field}, {lng_field}, {address_field}, "
        f"{SF_CITY_FIELD}, {SF_STATE_FIELD}, {zip_field} "
        f"FROM {object_name} WHERE {where}"
    )
