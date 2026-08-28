"""Queue-level cost gates: unique DB skip-classify and nearby-pin reuse."""

from __future__ import annotations

import os
from typing import Any, Sequence

from enrichment.geo import haversine_meters
from enrichment.constants import (
    GEMINI_TOWER_SKIP_CLAUDE_CONF,
    PROXIMITY_AMBIGUITY_GAP_M,
    PROXIMITY_CONFIDENT_M,
)
from enrichment.mssql import ProximityHit

PIN_CLUSTER_M = float(os.environ.get("PIN_CLUSTER_M", "50"))


def _env_flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes")


def should_auto_skip_classify(
    hit: ProximityHit | None,
    *,
    confident_m: float = PROXIMITY_CONFIDENT_M,
    ambiguity_gap_m: float = PROXIMITY_AMBIGUITY_GAP_M,
) -> bool:
    """True when a unique FCC/TowerSource hit is close enough to skip imagery."""
    if not _env_flag("AUTO_SKIP_CLASSIFY", default="1") or hit is None:
        return False
    if float(hit.distance_m) > float(confident_m):
        return False
    n = hit.candidate_count
    if n is None or n <= 1:
        return True
    gap = hit.runner_up_gap_m
    return gap is not None and float(gap) >= float(ambiguity_gap_m)


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


def should_spend_second_nearmap(
    *,
    unused_point: tuple[float, float] | None,
    skip_paid_imagery: bool,
    has_nearmap_key: bool,
    site_type: Any,
    site_confidence: Any,
    nearmap_tier: Any,
    rooftop_unlocked: bool,
) -> bool:
    """One extra Nearmap pack at the unused pin/Census point.

    Skip when the first full+oblique pack already locked empty (other/unclear
    at >= GEMINI_SOLO). Still spend on no-coverage probes and unlocked rooftops.
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
    return True
