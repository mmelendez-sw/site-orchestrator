"""OpenStreetMap antenna / mast / telecom tower tags near a pin (Overpass).

Built alongside enrichment.osm_prefilter (same OVERPASS_URL and timeout) but
with a tighter radius and telecom-focused tags. Successful responses are
cached on disk; failures raise so collect_signals leaves the column blank
(fail-open: a failed lookup never counts as "nothing here").

Counted (``osm_antenna_count``): elements within the radius that are
  * man_made=antenna, or
  * man_made=mast|tower|communications_tower with a telecom tag, or
  * any element with a telecom tag,
where a telecom tag is tower:type=communication(s)/cellular/telecom,
communication:mobile_phone=yes (or any communication:*=yes),
telecom=* , or antenna:type / communication:type mentioning mobile/cellular.
A telecom-tagged element within OSM_ANTENNA_STRONG_M (default 30 m) makes the
site's signal_strength "strong".

Env: OSM_ANTENNA_RADIUS_M (60), OSM_ANTENNA_CACHE_DAYS (30), OSM_ANTENNA_RPM (60).
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from enrichment.geo import haversine_meters
from enrichment.osm_prefilter import OSM_TIMEOUT_S, OVERPASS_URL
from enrichment.signals import (
    OSM_TELECOM_NEAREST_KEY,
    RateLimiter,
    cache_path,
    cache_read,
    cache_write,
    round_m,
)
from envutil import env_float
from paths import data_root

logger = logging.getLogger(__name__)

OSM_ANTENNA_RADIUS_M = env_float("OSM_ANTENNA_RADIUS_M", 60)
OSM_ANTENNA_CACHE_DAYS = env_float("OSM_ANTENNA_CACHE_DAYS", 30)

_limiter = RateLimiter(env_float("OSM_ANTENNA_RPM", 60))

_STRUCTURE_VALUES = frozenset({"antenna", "mast", "tower", "communications_tower"})
_COMM_TOWER_TYPES = frozenset(
    {"communication", "communications", "cellular", "cell", "telecom", "telecommunication"}
)
_MOBILE_WORDS = ("mobile", "cellular", "gsm", "lte", "umts", "5g", "telecom")

CLASS_TELECOM = "telecom"
CLASS_ANTENNA = "antenna"


def cache_dir() -> Path:
    return data_root() / "cache" / "osm_antenna"


def overpass_query(lat: float, lon: float, radius_m: float) -> str:
    r = f"{max(10.0, float(radius_m)):.0f}"
    around = f"(around:{r},{lat:.6f},{lon:.6f})"
    parts = []
    for kind in ("node", "way"):
        parts.extend([
            f'{kind}["man_made"~"^(antenna|mast|tower|communications_tower)$"]{around};',
            f'{kind}["tower:type"]{around};',
            f'{kind}["communication:mobile_phone"]{around};',
            f'{kind}["telecom"]{around};',
        ])
    return (
        f"[out:json][timeout:{max(1, int(OSM_TIMEOUT_S))}];"
        f"({''.join(parts)});out tags center;"
    )


def _tag(tags: dict[str, Any], key: str) -> str:
    return str(tags.get(key) or "").strip().lower()


def _telecom_tagged(tags: dict[str, Any]) -> bool:
    if _tag(tags, "tower:type") in _COMM_TOWER_TYPES:
        return True
    if _tag(tags, "telecom"):
        return True
    for key, value in tags.items():
        k = str(key).lower()
        if k.startswith("communication:") and str(value).strip().lower() in {"yes", "true"}:
            return True
    for key in ("antenna:type", "communication:type", "operator:type"):
        value = _tag(tags, key)
        if value and any(word in value for word in _MOBILE_WORDS):
            return True
    return False


def classify_element(el: dict[str, Any]) -> str | None:
    """CLASS_TELECOM, CLASS_ANTENNA (untagged antenna), or None (not counted)."""
    tags = el.get("tags") or {}
    if not isinstance(tags, dict):
        return None
    if _telecom_tagged(tags):
        return CLASS_TELECOM
    if _tag(tags, "man_made") == "antenna":
        return CLASS_ANTENNA
    return None


def _element_point(el: dict[str, Any]) -> tuple[float, float] | None:
    for src in (el, el.get("center") or {}):
        try:
            return float(src["lat"]), float(src["lon"])
        except (KeyError, TypeError, ValueError):
            continue
    return None


def summarize(
    lat: float, lon: float, elements: list[dict[str, Any]], *, radius_m: float | None = None
) -> dict[str, Any]:
    radius = OSM_ANTENNA_RADIUS_M if radius_m is None else float(radius_m)
    count = 0
    telecom_near: float | None = None
    seen: set[tuple[str, Any]] = set()
    for el in elements:
        ident = (str(el.get("type")), el.get("id"))
        if el.get("id") is not None:
            if ident in seen:
                continue
            seen.add(ident)
        cls = classify_element(el)
        if cls is None:
            continue
        point = _element_point(el)
        dist = haversine_meters(lat, lon, *point) if point else None
        if dist is not None and dist > radius:
            continue
        count += 1
        if cls == CLASS_TELECOM and dist is not None:
            telecom_near = dist if telecom_near is None else min(telecom_near, dist)
    return {
        "osm_antenna_count": count,
        OSM_TELECOM_NEAREST_KEY: round_m(telecom_near),
    }


def _post_overpass(query: str) -> Any:
    data = urllib.parse.urlencode({"data": query}).encode("utf-8")
    req = urllib.request.Request(
        OVERPASS_URL,
        data=data,
        headers={"User-Agent": "site-orchestrator/signals-osm-antenna"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=OSM_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_elements(lat: float, lon: float, *, radius_m: float | None = None) -> list[dict[str, Any]]:
    """Overpass elements near the point (cached). Raises on network/parse errors."""
    radius = OSM_ANTENNA_RADIUS_M if radius_m is None else float(radius_m)
    query = overpass_query(lat, lon, radius)
    path = cache_path(cache_dir(), {"url": OVERPASS_URL, "q": query})
    cached = cache_read(path, OSM_ANTENNA_CACHE_DAYS * 86400.0)
    if isinstance(cached, list):
        return cached
    _limiter.wait()
    payload = _post_overpass(query)
    elements = payload.get("elements") if isinstance(payload, dict) else None
    if not isinstance(elements, list):
        raise ValueError("unexpected Overpass response")
    if isinstance(payload, dict) and payload.get("remark") and not elements:
        # Overpass reports timeouts / quota as a remark with no elements.
        raise ValueError(f"Overpass remark: {str(payload.get('remark'))[:120]}")
    slim = [
        {k: el.get(k) for k in ("type", "id", "lat", "lon", "center", "tags") if k in el}
        for el in elements
        if isinstance(el, dict)
    ]
    cache_write(path, slim)
    return slim


def lookup(lat: float, lon: float, *, radius_m: float | None = None) -> dict[str, Any]:
    """{"osm_antenna_count": n, "_osm_telecom_nearest_m": m|""}. Raises on failure."""
    elements = fetch_elements(lat, lon, radius_m=radius_m)
    return summarize(lat, lon, elements, radius_m=radius_m)
