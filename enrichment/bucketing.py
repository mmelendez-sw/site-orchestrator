"""Bucket enrichment results into update candidates vs holdouts."""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from classifier.evidence import (
    HEDGE_CUES,
    STEALTH_MAST_WRITE_CUES,
    WRITE_GATE_TELECOM_CUES,
    evidence_text,
    has_any,
)
from classifier.views import (
    supplemental_can_confirm,
    is_oblique_label,
    is_state_ortho_label,
    is_street_level_label,
    parse_box_2d,
)
from envutil import env_flag, env_float
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
    GEMINI_TOWER_SKIP_CLAUDE_CONF,
    MIN_IMAGERY_ONLY_CELL_CONFIDENCE,
    MIN_IMAGERY_ONLY_SITE_CONFIDENCE,
    MIN_IMAGERY_ONLY_SITE_CONFIDENCE_AGREE,
    MIN_ROOFTOP_CELL_CONFIDENCE,
    MIN_UPDATE_CONFIDENCE,
    VERIFIED_SITE_SOURCE_FCC,
    VERIFIED_SITE_SOURCE_NAIP,
    VERIFIED_SITE_SOURCE_NEARMAP,
    VERIFIED_SITE_SOURCE_TOWERSOURCE,
    ROOFTOP_CERTAIN_CONF,
    DISCERNIBLE_CELL_GEAR_KINDS,
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
    return has_any(evidence_text(classified), WRITE_GATE_TELECOM_CUES)


def _parse_asset_box_2d(classified: dict[str, Any]) -> list[int] | None:
    return parse_box_2d(classified.get("asset_box_2d"))


def _hedged_cell_claim(classified: dict[str, Any]) -> bool:
    """True when evidence sounds guessed (HVAC FP / 'likely conceals' language)."""
    return has_any(evidence_text(classified), HEDGE_CUES)


def _rooftop_cell_certain(classified: dict[str, Any]) -> bool:
    """True when NAIP/Gemini evidence is strong enough to auto-apply a rooftop.

    Requires explicit high site + cell confidence, a named telecom gear kind
    (not unclear/none), unhedged evidence that names the gear, and a box.
    """
    if str(classified.get("site_type") or "").strip().lower() != "rooftop":
        return False
    if not cell_equipment_confirmed(classified.get("cell_equipment")):
        return False
    if not _confidence_ok(classified.get("site_confidence"), ROOFTOP_CERTAIN_CONF):
        return False
    if not _rooftop_cell_confidence_ok(
        classified, minimum=ROOFTOP_CERTAIN_CONF
    ):
        return False
    kind = str(classified.get("cell_gear_kind") or "").strip().lower().replace(" ", "_")
    if kind not in DISCERNIBLE_CELL_GEAR_KINDS:
        return False
    if not _telecom_evidence_cues(classified):
        return False
    if _hedged_cell_claim(classified):
        return False
    if not _has_asset_box(classified):
        return False
    return True


def _cell_gear_kind_ok(classified: dict[str, Any]) -> bool:
    """Accept structured gear kinds that are not none/blank.

    `unclear` is allowed when free-text evidence still cites telecom gear.
    """
    kind = str(classified.get("cell_gear_kind") or "").strip().lower()
    if not kind:
        # Older runs / models may omit the field — fall back to text cues.
        return _telecom_evidence_cues(classified)
    if kind == "none":
        return False
    if kind == "unclear":
        return _telecom_evidence_cues(classified)
    return kind in CELL_GEAR_KINDS or kind.replace(" ", "_") in CELL_GEAR_KINDS


def _rooftop_oblique_imagery_ok(classified: dict[str, Any]) -> bool:
    """Rooftop SF writes require Nearmap oblique views (not NAIP/Vert-only).

    With SUPPLEMENTAL_CAN_CONFIRM=1 a boxed street-level photo also counts
    (``_street_confirm_source``).
    """
    if imagery_bucket(classified) == "nearmap_oblique":
        return True
    return _street_confirm_source(classified) is not None


# Verified_Site_Source__c picklist value per street-level source. Mapillary
# uses Google Map (the street-level value); a source missing here holds out.
STREET_VERIFIED_SOURCES = {"mapillary": "Google Map"}


# Street photos show architecture (cornices, finials, louvers) that a model
# can talk itself into calling gear; a street confirm must not hedge.
STREET_HEDGE_CUES: tuple[str, ...] = (
    "consistent with",
    "characteristic of",
    "appears",
    "resembl",
    "suggest",
    "could be",
)
STREET_CONFIRM_MIN_CELL_CONF = 0.9


def _street_photo_year(view: str) -> int | None:
    match = re.search(r"\b((?:19|20)\d{2})-\d{2}\b", view)
    return int(match.group(1)) if match else None


def _street_confirm_source(classified: dict[str, Any]) -> str | None:
    """``mapillary`` when a street photo may confirm a rooftop or tower.

    Opt-in (SUPPLEMENTAL_CAN_CONFIRM=1, default off). The asset box must sit
    on a street-level view, Claude must have boxed the gear itself
    (``agree_localize``, not a yes/no on Gemini's crop), cell confidence must
    reach 0.9 with unhedged evidence, and the photo must be at most
    STREET_CONFIRM_MAX_AGE_YEARS (default 5) old.
    """
    if not supplemental_can_confirm():
        return None
    view = str(classified.get("asset_view") or "")
    if not is_street_level_label(view) or not _compact_asset_box(classified):
        return None
    if str(classified.get("dual_model_resolution") or "").strip().lower() != "agree_localize":
        return None
    if not _confidence_ok(classified.get("cell_equipment_confidence"), STREET_CONFIRM_MIN_CELL_CONF):
        return None
    text = evidence_text(classified)
    if has_any(text, HEDGE_CUES) or has_any(text, STREET_HEDGE_CUES):
        return None
    year = _street_photo_year(view)
    max_age = env_float("STREET_CONFIRM_MAX_AGE_YEARS", 5.0)
    if year is None or date.today().year - year > max_age:
        return None
    if "mapillary" in view.lower():
        return "mapillary"
    return "street"


def _has_asset_box(classified: dict[str, Any]) -> bool:
    """True when we have geocoded coords or a valid model box on a named view."""
    try:
        lat = classified.get("asset_lat")
        lon = classified.get("asset_lon")
        if (
            lat is not None
            and lon is not None
            and str(lat).strip() != ""
            and str(lon).strip() != ""
        ):
            float(lat)
            float(lon)
            return True
    except (TypeError, ValueError):
        pass
    if not _parse_asset_box_2d(classified):
        return False
    return bool(str(classified.get("asset_view") or "").strip())


def _asset_view_is_nearmap_oblique(classified: dict[str, Any]) -> bool:
    return is_oblique_label(classified.get("asset_view"))


def _view_evidence_consistent(classified: dict[str, Any]) -> bool:
    """Reject NAIP boxes when cell evidence cites a Nearmap oblique direction."""
    evidence = str(classified.get("cell_equipment_evidence") or "").lower()
    view = str(classified.get("asset_view") or "").lower()
    if not evidence:
        return True
    directions = [d for d in ("north", "east", "south", "west") if d in evidence]
    cites_oblique = "oblique" in evidence or bool(directions)
    if cites_oblique and view.startswith("naip"):
        return False
    if directions and view and _asset_view_is_nearmap_oblique(classified):
        if not any(d in view for d in directions):
            return False
    return True


def _stealth_mast_cues(classified: dict[str, Any]) -> bool:
    """True when evidence names a disguised mast (monopalm/canister/etc.)."""
    return has_any(evidence_text(classified), STEALTH_MAST_WRITE_CUES)


def _tower_claimed_keep_ok(classified: dict[str, Any]) -> bool:
    """Outreach-verified Gemini stealth mast that Claude false-vetoed.

    Full Nearmap + disguise cues + site conf at the imagery-only bar.
    Does not unlock rooftops or bare-monopole Claude disagreements.
    """
    if str(classified.get("site_type") or "").strip().lower() != "tower":
        return False
    resolution = str(classified.get("dual_model_resolution") or "").strip().lower()
    if resolution != "claimed_site_keep_gemini":
        return False
    if not cell_equipment_confirmed(classified.get("cell_equipment")):
        return False
    if str(classified.get("nearmap_tier") or "").strip().lower() != "full":
        return False
    if not _confidence_ok(
        classified.get("site_confidence"), MIN_IMAGERY_ONLY_SITE_CONFIDENCE
    ):
        return False
    return _stealth_mast_cues(classified)


def _dual_model_cell_ok(classified: dict[str, Any]) -> bool:
    """True when Gemini+Claude agreed cell gear is present (not Gemini-solo)."""
    resolution = str(classified.get("dual_model_resolution") or "").strip().lower()
    if resolution in {
        "gemini_strong_solo",
        "soft_keep_gemini",
        "claimed_site_keep_gemini",
        "claude_veto",
        "box_required",
        "first_pass_gate",
    }:
        return False
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


def _tower_gemini_high_conf_ok(classified: dict[str, Any]) -> bool:
    """Gemini already locked a tower at GEMINI_TOWER_SKIP_CLAUDE_CONF — Claude not required.

    Accepts ``gemini_strong_solo`` or an empty dual_model_resolution (NAIP-only
    Gemini path never stamps Claude fields). Rooftop HVAC FPs still need Claude
    hard-agree. Imagery-only towers may write when this lock applies.
    """
    if str(classified.get("site_type") or "").strip().lower() != "tower":
        return False
    if not cell_equipment_confirmed(classified.get("cell_equipment")):
        return False
    # A tower boxed on a street photo goes through the street gate (Claude required).
    if is_street_level_label(classified.get("asset_view")):
        return False
    if not _confidence_ok(
        classified.get("site_confidence"), GEMINI_TOWER_SKIP_CLAUDE_CONF
    ):
        return False
    # Gear confidence too (a billboard pole at site 0.9 / cell 0.7 passed before).
    cell_raw = classified.get("cell_equipment_confidence")
    if cell_raw not in (None, "") and not _confidence_ok(cell_raw, env_float("GEMINI_TOWER_LOCK_CELL_CONF", 0.80)):
        return False
    resolution = str(classified.get("dual_model_resolution") or "").strip().lower()
    return resolution in {"", "gemini_strong_solo"}


def _dual_model_hard_agree(classified: dict[str, Any]) -> bool:
    """True only for real Gemini+Claude agree on cell gear.

    Soft-keep and rooftop Gemini-solo must not unlock Salesforce writes — those
    are the HVAC / wrong-neighbor FP paths. Towers at Gemini site_confidence
    >= GEMINI_TOWER_SKIP_CLAUDE_CONF may write via ``_tower_gemini_high_conf_ok``.
    """
    if not cell_equipment_confirmed(classified.get("cell_equipment")):
        return False
    if not _dual_model_cell_ok(classified):
        return False
    resolution = str(classified.get("dual_model_resolution") or "").strip().lower()
    if resolution not in {"agree", "agree_crop", "agree_localize"}:
        return False
    esc = str(classified.get("escalation_model") or "").strip().lower()
    return esc == "claude"


def _dual_model_localized_agree(classified: dict[str, Any]) -> bool:
    """Crop or localize Claude confirm — required for imagery-only SF writes.

    Bare ``agree`` (full-scene / already-escalated) is allowed for DB-backed
    hits where FCC/TowerSource already anchors the site. Imagery-only must
    prove the boxed region, not just a scene-level yes.
    """
    if not _dual_model_hard_agree(classified):
        return False
    resolution = str(classified.get("dual_model_resolution") or "").strip().lower()
    return resolution in {"agree_crop", "agree_localize"}


def _asset_view_is_nearmap_vert(classified: dict[str, Any]) -> bool:
    view = str(classified.get("asset_view") or "").strip().lower()
    if not view or "naip" in view:
        return False
    if is_street_level_label(view) or is_state_ortho_label(view):
        return False
    if "oblique" in view:
        return False
    if any(d in view for d in ("north", "east", "south", "west")):
        return False
    return "top-down" in view or "vert" in view


def _compact_asset_box(classified: dict[str, Any]) -> list[int] | None:
    box = _parse_asset_box_2d(classified)
    if not box:
        return None
    ymin, xmin, ymax, xmax = box
    if (ymax - ymin) > 400 or (xmax - xmin) > 400:
        return None
    return box


def _rooftop_oblique_box_ok(classified: dict[str, Any]) -> bool:
    """Compact box on a Nearmap oblique view."""
    if not _compact_asset_box(classified):
        return False
    return _asset_view_is_nearmap_oblique(classified)


def _rooftop_vert_box_dual_agree_ok(
    classified: dict[str, Any],
    *,
    require_localized: bool = False,
) -> bool:
    """Allow compact Nearmap Vert box when Claude agrees on cell gear.

    Oblique imagery must still have been fetched (full Nearmap). This recovers
    true positives like Southeast Financial Center where gear is clearest from
    Vert, without letting Gemini-only HVAC FPs through.

    Imagery-only writes pass ``require_localized=True`` so bare scene-level
    ``agree`` cannot unlock a Vert-box candidate.
    """
    agree_ok = (
        _dual_model_localized_agree(classified)
        if require_localized
        else _dual_model_hard_agree(classified)
    )
    if not agree_ok:
        return False
    if not _rooftop_oblique_imagery_ok(classified):
        return False
    if not _asset_view_is_nearmap_vert(classified):
        return False
    if not _compact_asset_box(classified):
        return False
    return _telecom_evidence_cues(classified)


def _rooftop_localization_box_ok(
    classified: dict[str, Any],
    *,
    require_localized: bool = False,
) -> bool:
    """Rooftop SF writes need a compact localization box on Nearmap imagery.

    Or, opt-in, on a street-level photo (SUPPLEMENTAL_CAN_CONFIRM=1).
    """
    if _rooftop_oblique_box_ok(classified):
        return True
    if _street_confirm_source(classified) is not None:
        return True
    return _rooftop_vert_box_dual_agree_ok(
        classified, require_localized=require_localized
    )


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
    return imagery_bucket(classified) in {"nearmap_vert", "nearmap_oblique"}


def imagery_bucket(classified_or_row: dict[str, Any]) -> str:
    """Coarse imagery label for run summaries: naip | nearmap_vert | nearmap_oblique.

    ``zoom`` / ``wide_aoi`` are pipeline stages, not proof of Nearmap. NAIP-only
    zoom scout must stay ``naip`` or rooftops look like they had obliques.
    """
    tier = str(
        classified_or_row.get("nearmap_tier")
        or classified_or_row.get("classification_stage")
        or ""
    ).strip().lower()
    views = str(classified_or_row.get("nearmap_views") or "")
    view_parts = {p.strip() for p in views.split(",") if p.strip()}
    has_oblique = bool(view_parts - {"Vert"})
    if has_oblique or tier == "full":
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
    - towers need Claude hard-agree, or Gemini site_confidence >= 0.9
      (``gemini_strong_solo``); rooftop Gemini-solo never writes
    - DB hits may use bare Claude ``agree``; imagery-only requires
      ``agree_crop`` or ``agree_localize`` unless the Gemini tower lock applies
    - imagery-only (no DB hit) uses stricter site/cell confidence bars
    - NAIP rooftops write only when cellular gear is certain (conf ≥ 0.95,
      named gear kind, unhedged evidence, asset box). Gemini towers at >= 0.9
      may write from NAIP with or without a DB hit
    - rooftops never verify as NAIP
    - update coords prefer FCC/TowerSource when present
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

    site_min = MIN_UPDATE_CONFIDENCE
    if imagery_only:
        site_min = MIN_IMAGERY_ONLY_SITE_CONFIDENCE
        if _dual_model_localized_agree(classified):
            site_min = MIN_IMAGERY_ONLY_SITE_CONFIDENCE_AGREE
    if not _confidence_ok(classified.get("site_confidence"), site_min):
        reason = "low_confidence_imagery_only" if imagery_only else "low_confidence"
        if site_type_raw == "rooftop":
            return _holdout(BUCKET_ROOFTOP, reason, classified)
        return _holdout(BUCKET_OTHER, reason, classified)

    # NAIP: rooftops write only when cellular gear is certain. Gemini towers
    # at GEMINI_TOWER_SKIP_CLAUDE_CONF may write.
    if img_bucket == "naip":
        if site_type_raw == "rooftop":
            # Opt-in street-level confirm takes the full rooftop gates below.
            if _street_confirm_source(classified) is None and not _rooftop_cell_certain(
                classified
            ):
                return _holdout(
                    BUCKET_ROOFTOP, "rooftop_not_certain_on_naip", classified
                )
        elif not _tower_gemini_high_conf_ok(classified) and not (
            _street_confirm_source(classified) is not None
            and _dual_model_hard_agree(classified)
        ):
            return _holdout(BUCKET_OTHER, "tower_naip_only_forbidden", classified)

    if site_type_raw == "tower" and not cell_equipment_confirmed(
        classified.get("cell_equipment")
    ):
        return _holdout(BUCKET_OTHER, "tower_no_cell_equipment", classified)

    # Auto-apply: Claude hard-agree, Gemini tower lock at >= GEMINI_SOLO, or a
    # claimed-site stealth mast Gemini found that Claude missed.
    tower_gemini_locked = _tower_gemini_high_conf_ok(classified)
    tower_claimed_keep = _tower_claimed_keep_ok(classified)
    if (
        site_type_raw == "tower"
        and not tower_gemini_locked
        and not tower_claimed_keep
        and not _dual_model_hard_agree(classified)
    ):
        return _holdout(BUCKET_OTHER, "tower_needs_dual_model_cell", classified)

    # Imagery-only is the exception path: require boxed crop/localize confirm
    # unless Gemini already locked the tower at GEMINI_SOLO or claimed-site keep.
    if (
        site_type_raw == "tower"
        and imagery_only
        and not tower_gemini_locked
        and not tower_claimed_keep
        and not _dual_model_localized_agree(classified)
    ):
        return _holdout(
            BUCKET_OTHER,
            "imagery_only_needs_crop_or_localize_agree",
            classified,
        )

    sf_site_type = map_site_type_for_upload(classified)
    if not sf_site_type:
        if site_type_raw == "rooftop":
            return _holdout(BUCKET_ROOFTOP, "rooftop_no_cell_equipment", classified)
        return _holdout(BUCKET_OTHER, "unmapped_site_type", classified)

    if site_type_raw == "rooftop":
        rooftop_certain = _rooftop_cell_certain(classified)
        if not rooftop_certain:
            # Imagery-only uses a higher cell-conf floor unless Gemini+Claude hard-agree.
            if imagery_only and _dual_model_hard_agree(classified):
                cell_min = MIN_ROOFTOP_CELL_CONFIDENCE
            elif imagery_only:
                cell_min = MIN_IMAGERY_ONLY_CELL_CONFIDENCE
            else:
                cell_min = MIN_ROOFTOP_CELL_CONFIDENCE
            if not _rooftop_cell_confidence_ok(classified, minimum=cell_min):
                return _holdout(BUCKET_ROOFTOP, "rooftop_low_cell_confidence", classified)
            if not _cell_gear_kind_ok(classified):
                return _holdout(BUCKET_ROOFTOP, "rooftop_no_telecom_evidence", classified)
            if not _rooftop_oblique_imagery_ok(classified):
                return _holdout(BUCKET_ROOFTOP, "rooftop_needs_nearmap_obliques", classified)
            if not _dual_model_hard_agree(classified):
                return _holdout(BUCKET_ROOFTOP, "rooftop_needs_dual_model_cell", classified)
            if imagery_only and not _dual_model_localized_agree(classified):
                return _holdout(
                    BUCKET_ROOFTOP,
                    "imagery_only_needs_crop_or_localize_agree",
                    classified,
                )
            if not _has_asset_box(classified):
                return _holdout(BUCKET_ROOFTOP, "rooftop_needs_asset_box", classified)
            if not _rooftop_localization_box_ok(
                classified, require_localized=imagery_only
            ):
                return _holdout(BUCKET_ROOFTOP, "rooftop_needs_oblique_asset_box", classified)
            if not _view_evidence_consistent(classified):
                return _holdout(BUCKET_ROOFTOP, "rooftop_view_evidence_mismatch", classified)
        elif not _view_evidence_consistent(classified):
            return _holdout(BUCKET_ROOFTOP, "rooftop_view_evidence_mismatch", classified)
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
    street_source = _street_confirm_source(classified)
    if match_source == MATCH_SOURCE_NONE and street_source is not None:
        picklist = STREET_VERIFIED_SOURCES.get(street_source)
        if not picklist:
            bucket = BUCKET_ROOFTOP if site_type_raw == "rooftop" else BUCKET_OTHER
            return _holdout(
                bucket, f"street_confirm_{street_source}_needs_picklist", classified
            )
        verified_source = picklist
    # Uncertain rooftops must never stamp Verified_Site_Source__c = NAIP.
    if (
        site_type_raw == "rooftop"
        and verified_source == VERIFIED_SITE_SOURCE_NAIP
        and not _rooftop_cell_certain(classified)
    ):
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
    """Prefer FCC/TowerSource coords whenever a proximity hit exists.

    Imagery asset-box snaps are the exception path (imagery-only sites).
    """
    if match_source != MATCH_SOURCE_NONE and db_lat is not None and db_lng is not None:
        return db_lat, db_lng, f"db:{match_source}"

    asset_lat = classified.get("asset_lat")
    asset_lon = classified.get("asset_lon")
    try:
        if asset_lat is not None and asset_lon is not None and str(asset_lat).strip() != "":
            source = str(classified.get("asset_coord_source") or "").strip()
            if not source:
                source = "asset_box"
            return float(asset_lat), float(asset_lon), source
    except (TypeError, ValueError):
        pass

    # Nearmap (or other) box without geocode: pin is acceptable when the model
    # drew an asset box on the pin-centered chip. Street photos are perspective
    # views (no geocode), so their boxes keep the pin too.
    if require_asset_box and _parse_asset_box_2d(classified):
        if sf_lat is not None and sf_lng is not None:
            if is_street_level_label(classified.get("asset_view")):
                return sf_lat, sf_lng, "street_photo_box_pin"
            return sf_lat, sf_lng, "nearmap_asset_box_pin"
        return None, None, "none"

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


def rooftop_presence_confirmed(classified: dict[str, Any]) -> bool:
    """True when the model labeled a building roof with usable confidence."""
    if classified.get("error"):
        return False
    site = str(classified.get("site_type") or "").strip().lower()
    if site != "rooftop":
        return False
    return _confidence_ok(classified.get("site_confidence"))


def naip_rooftop_confirm_decision(
    classified: dict[str, Any],
    *,
    existing_site_type: str | None = None,
) -> dict[str, Any]:
    """Building-roof audit: keep the sales Site_Type when a roof is present.

    Cellular gear is ignored. No Verified_Site or coordinate rewrite.
    Blank existing type becomes Rooftop.
    """
    persist = (existing_site_type or "").strip() or "Rooftop"
    if not rooftop_presence_confirmed(classified):
        return _holdout(BUCKET_OTHER, "naip_rooftop_unconfirmed", classified)
    return {
        "bucket": BUCKET_POTENTIAL_UPDATE,
        "holdout_reason": "naip_rooftop_confirm",
        "update_lat": "",
        "update_lng": "",
        "update_coord_source": "",
        "update_site_type": persist,
        "update_verified_site": "",
        "update_verified_site_source": "",
        "cell_equipment": classified.get("cell_equipment"),
        "cell_equipment_confirmed": False,
    }
