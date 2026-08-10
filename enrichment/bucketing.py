"""Bucket enrichment results into update candidates vs holdouts."""

from __future__ import annotations

from typing import Any

from salesforce.site_type_mapping import (
    cell_equipment_confirmed,
    map_site_type_for_upload,
)

from enrichment.constants import (
    ASSET_OFFSET_LEEWAY_M,
    BUCKET_OTHER,
    BUCKET_POTENTIAL_UPDATE,
    BUCKET_ROOFTOP,
    BUCKET_SKIP,
    MATCH_SOURCE_NONE,
    MATCH_SOURCE_TOWERSOURCE,
    MAX_ASSET_OFFSET_M,
    MIN_ROOFTOP_CELL_CONFIDENCE,
    MIN_UPDATE_CONFIDENCE,
    VERIFIED_SITE_SOURCE_FCC,
    VERIFIED_SITE_SOURCE_NAIP,
    VERIFIED_SITE_SOURCE_NEARMAP,
    VERIFIED_SITE_SOURCE_TOWERSOURCE,
)


def _confidence_ok(value: Any, minimum: float = MIN_UPDATE_CONFIDENCE) -> bool:
    try:
        return float(value) >= minimum
    except (TypeError, ValueError):
        return False


def _rooftop_cell_confidence_ok(classified: dict[str, Any]) -> bool:
    """When cell confidence is reported, require the rooftop minimum."""
    raw = classified.get("cell_equipment_confidence")
    if raw is None or str(raw).strip() == "":
        return True
    return _confidence_ok(raw, MIN_ROOFTOP_CELL_CONFIDENCE)


def _effective_max_asset_offset_m() -> float:
    return MAX_ASSET_OFFSET_M + ASSET_OFFSET_LEEWAY_M


def _asset_offset_too_far(
    classified: dict[str, Any],
    maximum: float | None = None,
) -> float | None:
    """Return the offset when the model's asset sits beyond snap radius + leeway."""
    limit = _effective_max_asset_offset_m() if maximum is None else maximum
    try:
        offset = float(classified.get("asset_offset_m"))
    except (TypeError, ValueError):
        return None
    return offset if offset > limit else None


def verified_source_for_match(
    match_source: str,
    *,
    classified: dict[str, Any] | None = None,
) -> str:
    """Map match + imagery used to the Salesforce Verified_Site_Source__c value."""
    if match_source == MATCH_SOURCE_TOWERSOURCE:
        return VERIFIED_SITE_SOURCE_TOWERSOURCE
    if match_source != MATCH_SOURCE_NONE:
        return VERIFIED_SITE_SOURCE_FCC
    if classified and _used_nearmap_imagery(classified):
        return VERIFIED_SITE_SOURCE_NEARMAP
    return VERIFIED_SITE_SOURCE_NAIP


def _used_nearmap_imagery(classified: dict[str, Any]) -> bool:
    """True when classification consumed Nearmap (vert and/or obliques)."""
    tier = str(classified.get("nearmap_tier") or "").strip().lower()
    if tier in {"vert_only", "full", "wide_aoi", "zoom"}:
        return True
    views = str(classified.get("nearmap_views") or "").strip()
    if not views:
        return False
    # Explicit NAIP-only tiers should not flip to NearMap just from empty leftovers.
    if tier in {"naip_only", "naip_wide"}:
        return False
    return True


def imagery_bucket(classified_or_row: dict[str, Any]) -> str:
    """Coarse imagery label for run summaries: naip | nearmap_vert | nearmap_oblique."""
    tier = str(
        classified_or_row.get("nearmap_tier")
        or classified_or_row.get("classification_stage")
        or ""
    ).strip().lower()
    views = str(classified_or_row.get("nearmap_views") or "")
    view_parts = {p.strip() for p in views.split(",") if p.strip()}
    has_oblique = bool(view_parts - {"Vert"}) or tier == "full"
    if tier in {"full", "wide_aoi", "zoom"} or has_oblique:
        return "nearmap_oblique"
    if tier in {"vert_only"} or "Vert" in view_parts:
        return "nearmap_vert"
    return "naip"


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
    - other / unclear / no_imagery / errors → holdout as other_or_else
    - tower or rooftop (medium/high conf) → potential_update
      (rooftop requires confirmed cell_equipment; maps to Site_Type__c = Rooftop)
    - rooftop without cell gear / low conf → holdout as potential_rooftop
    - rooftop asset beyond MAX_ASSET_OFFSET_M + leeway → holdout (no SF update)
    - towers still update even when the asset box is farther than that radius
    - DB hit coords preferred for lat/lng; else NAIP asset box; else SF pin
    - DB hits verify as FCC/TowerSource; imagery-only hits verify as NearMap
      when Nearmap was used, else NAIP
    """
    site_type_raw = str(classified.get("site_type") or "").strip().lower()
    error = classified.get("error")
    holdout_reason = ""

    if error or site_type_raw in {"", "no_imagery"}:
        holdout_reason = str(error or site_type_raw or "no_classification")
        return _holdout(BUCKET_OTHER, holdout_reason, classified)

    far_offset = _asset_offset_too_far(classified)

    if site_type_raw in {"other", "unclear"}:
        return _holdout(BUCKET_OTHER, site_type_raw, classified)
    if site_type_raw not in {"tower", "rooftop"}:
        return _holdout(BUCKET_OTHER, f"else:{site_type_raw}", classified)
    if not _confidence_ok(classified.get("site_confidence")):
        if site_type_raw == "rooftop":
            return _holdout(BUCKET_ROOFTOP, "low_confidence", classified)
        return _holdout(BUCKET_OTHER, "low_confidence", classified)

    sf_site_type = map_site_type_for_upload(classified)
    if not sf_site_type:
        if site_type_raw == "rooftop":
            return _holdout(BUCKET_ROOFTOP, "rooftop_no_cell_equipment", classified)
        return _holdout(BUCKET_OTHER, "unmapped_site_type", classified)

    if site_type_raw == "rooftop" and not _rooftop_cell_confidence_ok(classified):
        return _holdout(BUCKET_ROOFTOP, "rooftop_low_cell_confidence", classified)

    if site_type_raw == "rooftop" and far_offset is not None:
        limit = _effective_max_asset_offset_m()
        return _holdout(
            BUCKET_ROOFTOP,
            f"asset_offset_{far_offset:g}m_exceeds_{limit:g}m",
            classified,
        )

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

    verified_source = verified_source_for_match(
        match_source, classified=classified
    )
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
