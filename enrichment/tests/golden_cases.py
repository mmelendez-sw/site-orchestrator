"""Golden regression fixtures for known enrichment FP/TP cases.

These are recorded classify dicts (no live APIs). Each case documents the
expected bucket or holdout_reason so gate changes cannot silently regress
Green Valley / Sunset / Southeast / HVAC patterns.
"""

from __future__ import annotations

from typing import Any

from enrichment.constants import (
    BUCKET_POTENTIAL_UPDATE,
    MATCH_SOURCE_FCC,
    MATCH_SOURCE_NONE,
)

# (name, match_source, classified, db_lat, db_lng, sf_lat, sf_lng, expect)
GoldenCase = tuple[
    str,
    str,
    dict[str, Any],
    float | None,
    float | None,
    float | None,
    float | None,
    dict[str, str],
]


def _tower(**overrides: Any) -> dict[str, Any]:
    base = {
        "site_type": "tower",
        "tower_subtype": "monopole",
        "site_confidence": 0.9,
        "cell_equipment": True,
        "cell_equipment_confidence": 0.9,
        "cell_equipment_evidence": "North oblique shows sector panels on monopole",
        "cell_gear_kind": "sector_panel",
        "cell_models_agree": True,
        "dual_model_resolution": "agree_crop",
        "escalation_model": "claude",
        "nearmap_tier": "full",
        "nearmap_views": "Vert,North,East,South,West",
        "asset_box_2d": "[220, 310, 360, 420]",
        "asset_view": "Nearmap oblique (North)",
        "asset_lat": 36.0255,
        "asset_lon": -115.0852,
        "asset_offset_m": 12.0,
    }
    base.update(overrides)
    return base


def _rooftop(**overrides: Any) -> dict[str, Any]:
    base = {
        "site_type": "rooftop",
        "site_confidence": 0.9,
        "cell_equipment": True,
        "cell_equipment_confidence": 0.9,
        "cell_equipment_evidence": "North oblique shows sector panel antennas",
        "cell_gear_kind": "sector_panel",
        "cell_models_agree": True,
        "dual_model_resolution": "agree_crop",
        "escalation_model": "claude",
        "asset_lat": 43.0005,
        "asset_lon": -89.0005,
        "asset_offset_m": 20.0,
        "asset_box_2d": "[220, 310, 360, 420]",
        "asset_view": "Nearmap oblique (North)",
        "nearmap_tier": "full",
        "nearmap_views": "Vert,North,East,South,West",
    }
    base.update(overrides)
    return base


GOLDEN_CASES: list[GoldenCase] = [
    (
        "green_valley_vert_only_tower_fp",
        MATCH_SOURCE_NONE,
        _tower(
            nearmap_tier="vert_only",
            nearmap_views="Vert",
            asset_view="Nearmap top-down",
            asset_box_2d="[188, 151, 584, 442]",
            cell_models_agree=False,
            dual_model_resolution="",
            escalation_model="",
        ),
        None,
        None,
        36.0255,
        -115.0852,
        {"holdout_reason": "tower_needs_nearmap_obliques"},
    ),
    (
        "sunset_stealth_fp_cleared_to_other_or_dual",
        MATCH_SOURCE_NONE,
        _tower(
            tower_subtype="stealth",
            cell_equipment=False,
            cell_equipment_confidence=0.4,
            cell_equipment_evidence="Likely conceals antennas in steeple",
            dual_model_resolution="claude_veto",
            cell_models_agree=False,
        ),
        None,
        None,
        36.03,
        -115.08,
        {"holdout_reason": "tower_no_cell_equipment"},
    ),
    (
        "southeast_financial_vert_box_agree_crop",
        MATCH_SOURCE_NONE,
        _rooftop(
            cell_equipment_confidence=0.78,
            dual_model_resolution="agree_crop",
            asset_view="Nearmap top-down",
            asset_box_2d="[476, 526, 632, 589]",
            asset_lat=25.7722,
            asset_lon=-80.1876,
            asset_offset_m=7.9,
            asset_coord_source="nearmap_vert_box",
            cell_equipment_evidence=(
                "Nearmap top-down shows sector panels and microwave dishes "
                "on a rooftop telecom frame"
            ),
        ),
        None,
        None,
        25.77225,
        -80.18767,
        {"bucket": BUCKET_POTENTIAL_UPDATE},
    ),
    (
        "imagery_only_bare_agree_held_out",
        MATCH_SOURCE_NONE,
        _rooftop(dual_model_resolution="agree"),
        None,
        None,
        43.0,
        -89.0,
        {"holdout_reason": "imagery_only_needs_crop_or_localize_agree"},
    ),
    (
        "db_hit_bare_agree_allowed",
        MATCH_SOURCE_FCC,
        _rooftop(dual_model_resolution="agree"),
        43.01,
        -89.01,
        43.0,
        -89.0,
        {"bucket": BUCKET_POTENTIAL_UPDATE, "coord_prefix": "db:"},
    ),
    (
        "hvac_gemini_solo_held_out",
        MATCH_SOURCE_NONE,
        _rooftop(
            dual_model_resolution="gemini_strong_solo",
            escalation_model="gemini_strong_solo",
            cell_equipment_confidence=0.92,
        ),
        None,
        None,
        43.0,
        -89.0,
        {"holdout_reason": "rooftop_needs_dual_model_cell"},
    ),
    (
        "hvac_soft_keep_held_out",
        MATCH_SOURCE_NONE,
        _rooftop(dual_model_resolution="soft_keep_gemini"),
        None,
        None,
        43.0,
        -89.0,
        {"holdout_reason": "rooftop_needs_dual_model_cell"},
    ),
    (
        "imagery_only_tower_agree_crop_ready",
        MATCH_SOURCE_NONE,
        _tower(dual_model_resolution="agree_crop"),
        None,
        None,
        36.0255,
        -115.0852,
        {"bucket": BUCKET_POTENTIAL_UPDATE},
    ),
    (
        "db_tower_prefers_db_coords",
        MATCH_SOURCE_FCC,
        _tower(
            dual_model_resolution="agree",
            asset_lat=36.1,
            asset_lon=-115.2,
        ),
        36.0256,
        -115.0853,
        36.0255,
        -115.0852,
        {"bucket": BUCKET_POTENTIAL_UPDATE, "coord_prefix": "db:"},
    ),
    (
        "far_imagery_only_rooftop_offset_held_out",
        MATCH_SOURCE_NONE,
        _rooftop(asset_offset_m=117.5, dual_model_resolution="agree_crop"),
        None,
        None,
        43.0,
        -89.0,
        {"holdout_reason_contains": "asset_offset_"},
    ),
]
