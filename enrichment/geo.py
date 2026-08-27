"""Distance helpers for enrichment proximity and pin clustering."""

from __future__ import annotations

import math


def haversine_meters(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Return great-circle distance in meters between two WGS84 points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lng2 - lng1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * 6_371_000 * math.asin(math.sqrt(a))
