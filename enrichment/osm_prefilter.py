"""OSM Overpass lookup for nearby towers/buildings.

Used as a positive signal (communication tower nearby) and, after an empty
NAIP screen, as a cheap skip: no building and no tower/mast means do not
buy Nearmap. A failed Overpass lookup never skips (fail-open). OSM still
misses rooftop/stealth gear when a building is present — those still buy
Nearmap.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from envutil import env_flag, env_float, env_str

logger = logging.getLogger(__name__)

OVERPASS_URL = env_str("OVERPASS_URL", "https://overpass-api.de/api/interpreter")
OSM_RADIUS_M = env_float("OSM_RADIUS_M", 80)
OSM_TIMEOUT_S = env_float("OSM_TIMEOUT_S", 20)
OSM_PREFILTER = env_flag("OSM_PREFILTER", True)

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
    cache = _cache_path(query)
    cached = _cache_read(cache)
    if cached is not None:
        return parse_overpass_elements(cached)
    # A failed lookup never skips Nearmap, so a dead OVERPASS_URL silently
    # turns the free empty-pin skip off: fall back across servers.
    data = urllib.parse.urlencode({"data": query}).encode("utf-8")
    for url in _overpass_urls():
        for attempt in (1, 2):
            try:
                req = urllib.request.Request(
                    url,
                    data=data,
                    headers={"User-Agent": "site-orchestrator/osm-prefilter"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=OSM_TIMEOUT_S) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                elements = payload.get("elements") if isinstance(payload, dict) else None
                if not isinstance(elements, list) or payload.get("remark"):
                    break  # timeout/quota remark: try the next server
                _cache_write(cache, elements)
                return parse_overpass_elements(elements)
            except urllib.error.HTTPError as exc:
                logger.info("OSM prefilter %s HTTP %s", url, exc.code)
                if exc.code == 429 and attempt == 1:
                    time.sleep(3.0)
                    continue
                break
            except (urllib.error.URLError, TimeoutError, ValueError, OSError, json.JSONDecodeError) as exc:
                logger.info("OSM prefilter %s failed: %s", url, exc)
                break
    logger.info("OSM prefilter skipped: no Overpass server answered")
    return empty


def _overpass_urls() -> list[str]:
    """OVERPASS_URL then the shared mirror list (OVERPASS_MIRRORS)."""
    try:
        from enrichment.signals.osm_antenna import overpass_urls
    except Exception:  # noqa: BLE001
        return [OVERPASS_URL]
    return overpass_urls()


def _cache_path(query: str) -> Path | None:
    if not env_flag("IMAGERY_CACHE", True):
        return None
    from paths import data_root

    digest = hashlib.sha1(query.encode("utf-8")).hexdigest()
    return data_root() / "cache" / "osm_prefilter" / f"{digest}.json"


def _cache_read(path: Path | None) -> list | None:
    if path is None or not path.is_file():
        return None
    max_age_s = env_float("OSM_PREFILTER_CACHE_DAYS", 90) * 86400
    try:
        if time.time() - path.stat().st_mtime > max_age_s:
            return None
        elements = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return elements if isinstance(elements, list) else None


def _cache_write(path: Path | None, elements: list) -> None:
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(elements), encoding="utf-8")
    except OSError as exc:
        logger.info("OSM prefilter cache write skipped: %s", exc)


def osm_suggests_empty_chip(info: dict[str, Any] | None) -> bool:
    """True when a successful OSM lookup shows no building and no tower/mast.

    Used to skip Nearmap after an empty NAIP screen. Failed lookups
    (``ok`` False) never skip.
    """
    if not info or not info.get("ok"):
        return False
    return not info.get("has_building") and not info.get("has_tower_or_mast")
