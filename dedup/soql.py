"""SOQL query builders for spatial site lookups."""

from __future__ import annotations


def build_bbox_query(
    min_lat: float,
    max_lat: float,
    min_lng: float,
    max_lng: float,
    *,
    object_name: str = "Site__c",
    lat_field: str = "Latitude__c",
    lng_field: str = "Longitude__c",
) -> str:
    """Build a SOQL query for site records within a lat/lng bounding box."""
    return (
        f"SELECT Id, Name, {lat_field}, {lng_field}, Address__c "
        f"FROM {object_name} "
        f"WHERE {lat_field} >= {min_lat} AND {lat_field} <= {max_lat} "
        f"AND {lng_field} >= {min_lng} AND {lng_field} <= {max_lng}"
    )
