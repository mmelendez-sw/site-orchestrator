"""Bucket enrichment results into update candidates vs holdouts."""

from __future__ import annotations

from typing import Any

from salesforce.site_type_mapping import (
    cell_equipment_confirmed,
    map_site_type_for_upload,
)

from enrichment.constants import (
    BUCKET_OTHER,
    BUCKET_POTENTIAL_UPDATE,
    BUCKET_ROOFTOP,
    BUCKET_SKIP,
    MATCH_SOURCE_NONE,
    MATCH_SOURCE_TOWERSOURCE,
    MAX_ASSET_OFFSET_M,
    MIN_UPDATE_CONFIDENCE,
    VERIFIED_SITE_SOURCE_FCC,
    VERIFIED_SITE_SOURCE_NAIP,
    VERIFIED_SITE_SOURCE_TOWERSOURCE,
)


def _confidence_ok(value: Any, minimum: float = MIN_UPDATE_CONFIDENCE) -> bool:
    try:
        return float(value) >= minimum
    except (TypeError, ValueError):
        return False


def _asset_offset_too_far(
    classified: dict[str, Any],
    maximum: float = MAX_ASSET_OFFSET_M,
) -> float | None:
    """Return the offset when the model's asset sits beyond the match radius."""
    try:
        offset = float(classified.get("asset_offset_m"))
    except (TypeError, ValueError):
        return None
    return offset if offset > maximum else None


def verified_source_for_match(match_source: str) -> str:
    """Map an enrichment match source to the Salesforce picklist value."""
    if match_source == MATCH_SOURCE_TOWERSOURCE:
        return VERIFIED_SITE_SOURCE_TOWERSOURCE
    if match_source != MATCH_SOURCE_NONE:
        return VERIFIED_SITE_SOURCE_FCC
    return VERIFIED_SITE_SOURCE_NAIP


def bucket_classification(
    *,
    match_source: str,
    classified: dict[str, Any],
    db_lat: float | None,
    db_lng: float | None,
    sf_lat: float | None,
    sf_lng: float | None,
) -> dict[str, Any]:
    """Decide bucket and proposed Salesforce update fields.

    Rules:
    - rooftop → always holdout as potential_rooftop (NAIP rooftop signals are
      unreliable; Nearmap stage will handle rooftops later)
    - other / unclear / no_imagery / errors → holdout as other_or_else
    - tower (medium/high conf) → potential_update
    - asset located beyond MAX_ASSET_OFFSET_M from the classified point → holdout
    - DB hit coords preferred for lat/lng; else NAIP asset box; else SF pin
    - DB hits verify as FCC/TowerSource; NAIP-only hits verify as NAIP
    """
    site_type_raw = str(classified.get("site_type") or "").strip().lower()
    error = classified.get("error")
    holdout_reason = ""

    if error or site_type_raw in {"", "no_imagery"}:
        holdout_reason = str(error or site_type_raw or "no_classification")
        return _holdout(BUCKET_OTHER, holdout_reason, classified)

    if site_type_raw == "rooftop":
        return _holdout(BUCKET_ROOFTOP, "potential_rooftop", classified)

    far_offset = _asset_offset_too_far(classified)
    if far_offset is not None:
        return _holdout(
            BUCKET_OTHER,
            f"asset_offset_{far_offset:g}m_exceeds_{MAX_ASSET_OFFSET_M:g}m",
            classified,
        )

    if site_type_raw in {"other", "unclear"}:
        return _holdout(BUCKET_OTHER, site_type_raw, classified)
    if site_type_raw != "tower":
        return _holdout(BUCKET_OTHER, f"else:{site_type_raw}", classified)
    if not _confidence_ok(classified.get("site_confidence")):
        return _holdout(BUCKET_OTHER, "low_confidence", classified)

    sf_site_type = map_site_type_for_upload(classified)
    if not sf_site_type:
        return _holdout(BUCKET_OTHER, "unmapped_site_type", classified)

    update_lat, update_lng, coord_source = _resolve_update_coords(
        match_source=match_source,
        classified=classified,
        db_lat=db_lat,
        db_lng=db_lng,
        sf_lat=sf_lat,
        sf_lng=sf_lng,
    )
    if update_lat is None or update_lng is None:
        return _holdout(BUCKET_SKIP, "missing_coordinates", classified)

    verified_source = verified_source_for_match(match_source)
    return {
        "bucket": BUCKET_POTENTIAL_UPDATE,
        "holdout_reason": "",
        "update_lat": update_lat,
        "update_lng": update_lng,
        "update_coord_source": coord_source,
        "update_site_type": sf_site_type,
        "update_verified_site": True,
        "update_verified_site_source": verified_source,
        "cell_equipment": classified.get("cell_equipment"),
        "cell_equipment_confirmed": cell_equipment_confirmed(
            classified.get("cell_equipment")
        ),
    }


def _resolve_update_coords(
    *,
    match_source: str,
    classified: dict[str, Any],
    db_lat: float | None,
    db_lng: float | None,
    sf_lat: float | None,
    sf_lng: float | None,
) -> tuple[float | None, float | None, str]:
    if match_source != MATCH_SOURCE_NONE and db_lat is not None and db_lng is not None:
        return db_lat, db_lng, f"db:{match_source}"

    asset_lat = classified.get("asset_lat")
    asset_lon = classified.get("asset_lon")
    try:
        if asset_lat is not None and asset_lon is not None:
            return float(asset_lat), float(asset_lon), "naip_asset_box"
    except (TypeError, ValueError):
        pass

    if sf_lat is not None and sf_lng is not None:
        return sf_lat, sf_lng, "sf_pin"
    return None, None, "none"


def _holdout(bucket: str, reason: str, classified: dict[str, Any]) -> dict[str, Any]:
    return {
        "bucket": bucket,
        "holdout_reason": reason,
        "update_lat": "",
        "update_lng": "",
        "update_coord_source": "",
        "update_site_type": "",
        "update_verified_site": "",
        "update_verified_site_source": "",
        "cell_equipment": classified.get("cell_equipment"),
        "cell_equipment_confirmed": cell_equipment_confirmed(
            classified.get("cell_equipment")
        ),
    }
