"""OSM Overpass lookup for nearby towers/buildings.

Used as a positive signal (communication tower nearby). An empty Overpass
result does **not** skip Nearmap — this queue is carrier-claimed cell sites,
and OSM misses rooftop/stealth/small monopoles constantly.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

OVERPASS_URL = os.environ.get(
    "OVERPASS_URL", "https://overpass-api.de/api/interpreter"
).strip()
OSM_RADIUS_M = float(os.environ.get("OSM_RADIUS_M", "80"))
OSM_TIMEOUT_S = float(os.environ.get("OSM_TIMEOUT_S", "8"))


def _env_flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes")


OSM_PREFILTER = _env_flag("OSM_PREFILTER", default="1")

_TOWER_VALUES = frozenset({"tower", "mast", "communications_tower", "antenna"})
_COMM_TOWER_TYPES = frozenset(
    {"communication", "communications", "cellular", "cell", "telecom"}
)


def _overpass_query(lat: float, lon: float, radius_m: float) -> str:
    r = max(10.0, float(radius_m))
    return (
        f"[out:json][timeout:{max(1, int(OSM_TIMEOUT_S))}];"
        f"("
        f'node["man_made"~"^(tower|mast)$"](around:{r:.0f},{lat:.6f},{lon:.6f});'
        f'way["man_made"~"^(tower|mast)$"](around:{r:.0f},{lat:.6f},{lon:.6f});'
        f'node["tower:type"](around:{r:.0f},{lat:.6f},{lon:.6f});'
        f'way["building"](around:{r:.0f},{lat:.6f},{lon:.6f});'
        f'node["building"](around:{r:.0f},{lat:.6f},{lon:.6f});'
        f");out tags center;"
    )


def _element_flags(el: dict[str, Any]) -> tuple[bool, bool, bool]:
    tags = el.get("tags") or {}
    man_made = str(tags.get("man_made") or "").strip().lower()
    tower_type = str(tags.get("tower:type") or "").strip().lower()
    has_building = "building" in tags
    has_tower = man_made in _TOWER_VALUES or bool(tower_type)
    comm = tower_type in _COMM_TOWER_TYPES or str(
        tags.get("communication:mobile") or tags.get("telecom") or ""
    ).strip().lower() in {"yes", "cellular", "mobile"}
    return has_building, has_tower, comm and has_tower


def parse_overpass_elements(elements: list[dict[str, Any]]) -> dict[str, Any]:
    has_building = has_tower = comm = False
    for el in elements:
        b, t, c = _element_flags(el)
        has_building = has_building or b
        has_tower = has_tower or t
        comm = comm or c
    return {
        "ok": True,
        "has_building": has_building,
        "has_tower_or_mast": has_tower,
        "communication_tower": comm,
        "count": len(elements),
    }


def lookup_osm_features(
    lat: float,
    lon: float,
    *,
    radius_m: float | None = None,
) -> dict[str, Any]:
    """Return nearby building/tower flags. Fail-open on any error."""
    empty = {
        "ok": False,
        "has_building": False,
        "has_tower_or_mast": False,
        "communication_tower": False,
        "count": 0,
    }
    if not OSM_PREFILTER:
        empty["skipped"] = True
        return empty
    query = _overpass_query(lat, lon, radius_m or OSM_RADIUS_M)
    try:
        data = urllib.parse.urlencode({"data": query}).encode("utf-8")
        req = urllib.request.Request(
            OVERPASS_URL,
            data=data,
            headers={"User-Agent": "site-orchestrator/osm-prefilter"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=OSM_TIMEOUT_S) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        elements = payload.get("elements") or []
        if not isinstance(elements, list):
            return empty
        return parse_overpass_elements(elements)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError, json.JSONDecodeError) as exc:
        logger.info("OSM prefilter skipped: %s", exc)
        return empty


def osm_suggests_empty_chip(info: dict[str, Any] | None) -> bool:
    """True when OSM shows no building and no tower/mast.

    Informational only — do not use this to skip Nearmap on a claimed site.
    """
    if not info or not info.get("ok"):
        return False
    return not info.get("has_building") and not info.get("has_tower_or_mast")
