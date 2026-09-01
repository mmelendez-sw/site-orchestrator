"""Queue-level cost gates: unique DB skip-classify and nearby-pin reuse."""

from __future__ import annotations

import os
from typing import Any, Sequence

from enrichment.geo import haversine_meters
from enrichment.constants import (
    AUTO_SKIP_ON_STRUCTURE_M,
    GEMINI_TOWER_SKIP_CLAUDE_CONF,
    PROXIMITY_AMBIGUITY_GAP_M,
    PROXIMITY_CONFIDENT_M,
)
from enrichment.mssql import ProximityHit

PIN_CLUSTER_M = float(os.environ.get("PIN_CLUSTER_M", "50"))


def _env_flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes")


def _hit_pin_distance_m(hit: ProximityHit) -> float:
    if hit.distance_to_pin_m is not None:
        return float(hit.distance_to_pin_m)
    return float(hit.distance_m)


def auto_skip_classify_reason(
    hit: ProximityHit | None,
    *,
    confident_m: float = PROXIMITY_CONFIDENT_M,
    on_structure_m: float = AUTO_SKIP_ON_STRUCTURE_M,
    ambiguity_gap_m: float = PROXIMITY_AMBIGUITY_GAP_M,
) -> str | None:
    """Operator label when imagery can be skipped, else None.

    Unique ≤25 m still skips. A pin on the structure (≤5 m) also skips when
    extra FCC/TowerSource rows share the pad — that is collocation, not a
    wrong-neighbor choice. 10–25 m clusters still need the 75 m gap.
    """
    if not _env_flag("AUTO_SKIP_CLASSIFY", default="1") or hit is None:
        return None
    dist = float(hit.distance_m)
    if dist > float(confident_m):
        return None
    pin_dist = _hit_pin_distance_m(hit)
    if pin_dist <= float(on_structure_m):
        return f"DB hit on-structure ≤{float(on_structure_m):g} m"
    n = hit.candidate_count
    if n is None or n <= 1:
        return f"unique DB hit ≤{float(confident_m):g} m"
    gap = hit.runner_up_gap_m
    if gap is not None and float(gap) >= float(ambiguity_gap_m):
        return f"unique DB hit ≤{float(confident_m):g} m"
    return None


def should_auto_skip_classify(
    hit: ProximityHit | None,
    *,
    confident_m: float = PROXIMITY_CONFIDENT_M,
    on_structure_m: float = AUTO_SKIP_ON_STRUCTURE_M,
    ambiguity_gap_m: float = PROXIMITY_AMBIGUITY_GAP_M,
) -> bool:
    """True when a unique or on-structure FCC/TowerSource hit can skip imagery."""
    return (
        auto_skip_classify_reason(
            hit,
            confident_m=confident_m,
            on_structure_m=on_structure_m,
            ambiguity_gap_m=ambiguity_gap_m,
        )
        is not None
    )


def find_cluster_match(
    cache: Sequence[dict[str, Any]],
    lat: float,
    lon: float,
    *,
    max_m: float = PIN_CLUSTER_M,
) -> dict[str, Any] | None:
    """Return a prior imagery result within max_m, if any."""
    for entry in cache:
        try:
            elat = float(entry["lat"])
            elon = float(entry["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if haversine_meters(lat, lon, elat, elon) <= max_m:
            classified = entry.get("classified")
            if isinstance(classified, dict) and not classified.get("error"):
                return entry
    return None


def _site_conf(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def nearmap_empty_is_locked(
    site_type: Any,
    site_confidence: Any,
    nearmap_tier: Any,
    *,
    lock_conf: float = GEMINI_TOWER_SKIP_CLAUDE_CONF,
) -> bool:
    """True when full Nearmap+obliques already locked an empty claimed site."""
    if str(nearmap_tier or "").strip().lower() != "full":
        return False
    if str(site_type or "").strip().lower() not in {"other", "unclear"}:
        return False
    conf = _site_conf(site_confidence)
    return conf is not None and conf >= float(lock_conf)


def nearmap_empty_without_cell(
    site_type: Any,
    nearmap_tier: Any,
    cell_equipment: Any,
) -> bool:
    """Full Nearmap other/unclear with no confirmed cell gear.

    Claude and a second Nearmap pack do not recover these — skip both.
    ``cell_equipment`` False or null both count as unconfirmed.
    """
    if str(nearmap_tier or "").strip().lower() != "full":
        return False
    if str(site_type or "").strip().lower() not in {"other", "unclear"}:
        return False
    return cell_equipment is not True


def should_spend_second_nearmap(
    *,
    unused_point: tuple[float, float] | None,
    skip_paid_imagery: bool,
    has_nearmap_key: bool,
    site_type: Any,
    site_confidence: Any,
    nearmap_tier: Any,
    rooftop_unlocked: bool,
    cell_equipment: Any = None,
) -> bool:
    """One extra Nearmap pack at the unused pin/Census point.

    Skip when the first full+oblique pack is already empty with no cell
    (other/unclear and cell is not True), including the 0.90 lock.
    Still spend on no-coverage probes, unlocked rooftops, and a first pack
    that still looks like a positive site_type.
    """
    if unused_point is None or skip_paid_imagery or not has_nearmap_key:
        return False
    tier = str(nearmap_tier or "").strip().lower()
    if tier not in {"full", "vert_only", "no_coverage"}:
        return False
    if tier == "no_coverage":
        return True
    if rooftop_unlocked:
        return True
    if str(site_type or "").strip().lower() not in {"other", "unclear"}:
        return False
    if nearmap_empty_is_locked(site_type, site_confidence, tier):
        return False
    if nearmap_empty_without_cell(site_type, tier, cell_equipment):
        return False
    return True
