"""SOQL query builders for spatial site lookups."""

from __future__ import annotations

from dedupe.constants import (
    SF_ADDRESS_FIELD,
    SF_LAT_FIELD,
    SF_LNG_FIELD,
    SF_OBJECT_NAME,
    SF_ZIP_FIELD,
)


def _quote_zips(zip_codes: list[str]) -> str:
    return ", ".join(f"'{zip_code}'" for zip_code in zip_codes)


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
        f"SELECT Id, Name, {lat_field}, {lng_field}, {address_field}, {zip_field} "
        f"FROM {object_name} "
        f"WHERE {lat_field} >= {min_lat} AND {lat_field} <= {max_lat} "
        f"AND {lng_field} >= {min_lng} AND {lng_field} <= {max_lng}"
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
            f"({lat_field} >= {bbox['min_lat']} AND {lat_field} <= {bbox['max_lat']} "
            f"AND {lng_field} >= {bbox['min_lng']} AND {lng_field} <= {bbox['max_lng']})"
        )
    if not clauses:
        raise ValueError("Dedupe query requires zip codes and/or a bounding box")

    where = " OR ".join(clauses)
    return (
        f"SELECT Id, Name, {lat_field}, {lng_field}, {address_field}, {zip_field} "
        f"FROM {object_name} WHERE {where}"
    )
