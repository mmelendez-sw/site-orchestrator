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
    CELL_GEAR_KINDS,
    MATCH_SOURCE_NONE,
    MATCH_SOURCE_TOWERSOURCE,
    MAX_ASSET_OFFSET_M,
    MIN_IMAGERY_ONLY_CELL_CONFIDENCE,
    MIN_IMAGERY_ONLY_SITE_CONFIDENCE,
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


def _rooftop_cell_confidence_ok(
    classified: dict[str, Any],
    *,
    minimum: float = MIN_ROOFTOP_CELL_CONFIDENCE,
) -> bool:
    """Require an explicit cell_equipment_confidence at/above the minimum."""
    raw = classified.get("cell_equipment_confidence")
    if raw is None or str(raw).strip() == "":
        return False
    return _confidence_ok(raw, minimum)


def _telecom_evidence_cues(classified: dict[str, Any]) -> bool:
    """True when evidence text cites antenna/panel/dish/RRU-style gear."""
    text = " ".join(
        str(classified.get(key) or "")
        for key in ("cell_equipment_evidence", "site_evidence")
    ).lower()
    cues = (
        "antenna",
        "sector",
        "rru",
        "microwave",
        "backhaul",
        "panel",
        "parapet mast",
        "radio",
        "telecom",
        "dish",
        "facade mount",
        "wall-mounted",
        "wall mounted",
    )
    return any(cue in text for cue in cues)


def _cell_gear_kind_ok(classified: dict[str, Any]) -> bool:
    """Accept structured gear kinds that are not none/unclear/blank."""
    kind = str(classified.get("cell_gear_kind") or "").strip().lower()
    if not kind:
        # Older runs / models may omit the field — fall back to text cues.
        return _telecom_evidence_cues(classified)
    if kind in {"none", "unclear"}:
        return False
    return kind in CELL_GEAR_KINDS or kind.replace(" ", "_") in CELL_GEAR_KINDS


def _rooftop_oblique_imagery_ok(classified: dict[str, Any]) -> bool:
    """Rooftop SF writes require Nearmap oblique views (not NAIP/Vert-only)."""
    return imagery_bucket(classified) == "nearmap_oblique"


def _has_asset_box(classified: dict[str, Any]) -> bool:
    try:
        lat = classified.get("asset_lat")
        lon = classified.get("asset_lon")
        if lat is None or lon is None or str(lat).strip() == "" or str(lon).strip() == "":
            return False
        float(lat)
        float(lon)
        return True
    except (TypeError, ValueError):
        return False


def _dual_model_cell_ok(classified: dict[str, Any]) -> bool:
    """Rooftops require Gemini+Claude agreement that cell gear is present."""
    agree = classified.get("cell_models_agree")
    if isinstance(agree, bool):
        return agree
    text = str(agree or "").strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    # If Claude never ran, do not treat as agreed.
    if not str(classified.get("escalation_model") or "").strip():
        return False
    return cell_equipment_confirmed(classified.get("cell_equipment"))


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

    High-precision rules for rooftop/tower Salesforce candidacy:
    - cell_equipment must be confirmed true (towers and rooftops)
    - rooftops need Nearmap obliques, dual-model cell agreement, asset box,
      telecom evidence / cell_gear_kind, and cell conf ≥ bar
    - imagery-only (no DB hit) uses stricter site/cell confidence bars
    - NAIP-only imagery never writes rooftop or tower Site Type
    - rooftops never verify as NAIP
    """
    site_type_raw = str(classified.get("site_type") or "").strip().lower()
    error = classified.get("error")
    imagery_only = match_source == MATCH_SOURCE_NONE
    img_bucket = imagery_bucket(classified)

    if error or site_type_raw in {"", "no_imagery"}:
        return _holdout(
            BUCKET_OTHER,
            str(error or site_type_raw or "no_classification"),
            classified,
        )

    far_offset = _asset_offset_too_far(classified)

    if site_type_raw in {"other", "unclear"}:
        return _holdout(BUCKET_OTHER, site_type_raw, classified)
    if site_type_raw not in {"tower", "rooftop"}:
        return _holdout(BUCKET_OTHER, f"else:{site_type_raw}", classified)

    site_min = (
        MIN_IMAGERY_ONLY_SITE_CONFIDENCE if imagery_only else MIN_UPDATE_CONFIDENCE
    )
    if not _confidence_ok(classified.get("site_confidence"), site_min):
        reason = "low_confidence_imagery_only" if imagery_only else "low_confidence"
        if site_type_raw == "rooftop":
            return _holdout(BUCKET_ROOFTOP, reason, classified)
        return _holdout(BUCKET_OTHER, reason, classified)

    # Never write Site Type from NAIP-only chips (too many HVAC/utility FPs).
    if img_bucket == "naip":
        if site_type_raw == "rooftop":
            return _holdout(BUCKET_ROOFTOP, "rooftop_naip_only_forbidden", classified)
        return _holdout(BUCKET_OTHER, "tower_naip_only_forbidden", classified)

    if site_type_raw == "tower" and not cell_equipment_confirmed(
        classified.get("cell_equipment")
    ):
        return _holdout(BUCKET_OTHER, "tower_no_cell_equipment", classified)

    sf_site_type = map_site_type_for_upload(classified)
    if not sf_site_type:
        if site_type_raw == "rooftop":
            return _holdout(BUCKET_ROOFTOP, "rooftop_no_cell_equipment", classified)
        return _holdout(BUCKET_OTHER, "unmapped_site_type", classified)

    if site_type_raw == "rooftop":
        cell_min = (
            MIN_IMAGERY_ONLY_CELL_CONFIDENCE
            if imagery_only
            else MIN_ROOFTOP_CELL_CONFIDENCE
        )
        if not _rooftop_cell_confidence_ok(classified, minimum=cell_min):
            return _holdout(BUCKET_ROOFTOP, "rooftop_low_cell_confidence", classified)
        if not _cell_gear_kind_ok(classified):
            return _holdout(BUCKET_ROOFTOP, "rooftop_no_telecom_evidence", classified)
        if not _rooftop_oblique_imagery_ok(classified):
            return _holdout(BUCKET_ROOFTOP, "rooftop_needs_nearmap_obliques", classified)
        if not _dual_model_cell_ok(classified):
            return _holdout(BUCKET_ROOFTOP, "rooftop_needs_dual_model_cell", classified)
        if not _has_asset_box(classified):
            return _holdout(BUCKET_ROOFTOP, "rooftop_needs_asset_box", classified)
        if far_offset is not None:
            limit = _effective_max_asset_offset_m()
            return _holdout(
                BUCKET_ROOFTOP,
                f"asset_offset_{far_offset:g}m_exceeds_{limit:g}m",
                classified,
            )

    if (
        site_type_raw == "tower"
        and far_offset is not None
        and imagery_only
    ):
        limit = _effective_max_asset_offset_m()
        return _holdout(
            BUCKET_OTHER,
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
        require_asset_box=(site_type_raw == "rooftop"),
    )
    if update_lat is None or update_lng is None:
        return _holdout(BUCKET_SKIP, "missing_coordinates", classified)
    if site_type_raw == "rooftop" and coord_source == "sf_pin":
        return _holdout(BUCKET_ROOFTOP, "rooftop_needs_asset_box", classified)

    verified_source = verified_source_for_match(
        match_source, classified=classified
    )
    # Rooftops must never stamp Verified_Site_Source__c = NAIP.
    if site_type_raw == "rooftop" and verified_source == VERIFIED_SITE_SOURCE_NAIP:
        return _holdout(BUCKET_ROOFTOP, "rooftop_naip_verified_forbidden", classified)

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
    require_asset_box: bool = False,
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

    if require_asset_box:
        return None, None, "none"

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
