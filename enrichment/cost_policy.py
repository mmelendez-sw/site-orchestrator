"""Queue-level cost gates: unique DB skip-classify and nearby-pin reuse."""

from __future__ import annotations

import os
from typing import Any, Sequence

from dedupe.spatial import haversine_meters
from enrichment.constants import PROXIMITY_AMBIGUITY_GAP_M, PROXIMITY_CONFIDENT_M
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
