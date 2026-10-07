"""State / county orthoimagery services (config-driven, no hard-coded URLs).

Many states publish leaf-off orthoimagery at 15-30 cm through public ArcGIS
ImageServer / MapServer or WMS endpoints. That is 2-4x sharper than NAIP's
60 cm and often enough to see rooftop equipment. Which services exist, and
their URLs, are deployment config: ``STATE_ORTHO_SOURCES`` points at a JSON
file (see ``docs/state_ortho_sources.example.json``) holding a list of
entries::

    {"name": "NYS 2024", "states": ["NY"], "bbox": [minlon, minlat, maxlon, maxlat],
     "type": "arcgis_image" | "arcgis_map" | "wms", "url": "...", "layer": "...",
     "year": 2024, "resolution_cm": 15, "chip_m": 120, "px": 1024,
     "token_env": "OPTIONAL_ENV_NAME"}

The first entry matching the site's state (and/or whose bbox contains the
point) is used. Objects without ``url`` (e.g. ``{"_comment": ...}``) are
ignored. The file may also be ``{"sources": [...]}``.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from classifier.sources import base
from envutil import env_int, env_str

NAME = "state_ortho"
TYPES = ("arcgis_image", "arcgis_map", "wms")
DEFAULT_CHIP_M = 120.0
DEFAULT_PX = 1024

logger = logging.getLogger(__name__)

_config_lock = threading.Lock()
_config_cache: dict[str, Any] = {"key": None, "entries": []}


def max_views() -> int:
    return max(0, env_int("STATE_ORTHO_MAX_VIEWS", 1))


def config_path() -> Path | None:
    raw = env_str("STATE_ORTHO_SOURCES")
    return Path(raw) if raw else None


def _normalize_entry(raw: Any, index: int) -> dict | None:
    if not isinstance(raw, dict) or not raw.get("url"):
        return None
    kind = str(raw.get("type") or "").strip().lower()
    if kind not in TYPES:
        base.log_once(f"state_ortho:type:{index}", logging.WARNING,
                      "STATE_ORTHO_SOURCES entry %s has unknown type %r; skipped", index, kind)
        return None
    states = [str(s).strip().upper() for s in (raw.get("states") or []) if str(s).strip()]
    bbox = raw.get("bbox")
    if bbox is not None:
        try:
            bbox = [float(v) for v in bbox]
            if len(bbox) != 4 or bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
                raise ValueError
        except (TypeError, ValueError):
            base.log_once(f"state_ortho:bbox:{index}", logging.WARNING,
                          "STATE_ORTHO_SOURCES entry %s has a bad bbox; skipped", index)
            return None
    if not states and bbox is None:
        base.log_once(f"state_ortho:scope:{index}", logging.WARNING,
                      "STATE_ORTHO_SOURCES entry %s has neither states nor bbox; skipped", index)
        return None
    token_env = str(raw.get("token_env") or "").strip()
    if token_env:
        base.register_secret_env(token_env)

    def _num(key: str, default: float) -> float:
        try:
            value = float(raw.get(key))
            return value if value > 0 else default
        except (TypeError, ValueError):
            return default

    return {
        "name": str(raw.get("name") or f"state_ortho_{index}"),
        "states": states,
        "bbox": bbox,
        "type": kind,
        "url": str(raw["url"]).rstrip("/"),
        "layer": str(raw.get("layer") or "").strip(),
        "year": raw.get("year"),
        "captured": raw.get("captured"),
        "resolution_cm": raw.get("resolution_cm"),
        "chip_m": _num("chip_m", DEFAULT_CHIP_M),
        "px": int(_num("px", DEFAULT_PX)),
        "token_env": token_env,
    }


def load_config() -> list[dict]:
    """Parsed entries from ``STATE_ORTHO_SOURCES`` (reloaded when the file changes)."""
    path = config_path()
    if path is None:
        return []
    try:
        mtime = path.stat().st_mtime
    except OSError:
        base.log_once(f"state_ortho:missing:{path}", logging.WARNING,
                      "STATE_ORTHO_SOURCES file not found: %s", path)
        return []
    key = (str(path), mtime)
    with _config_lock:
        if _config_cache["key"] == key:
            return list(_config_cache["entries"])
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        base.log_once(f"state_ortho:parse:{key}", logging.WARNING,
                      "STATE_ORTHO_SOURCES could not be read (%s): %s", path, exc)
        return []
    if isinstance(payload, dict):
        payload = payload.get("sources") or []
    entries = []
    for index, raw in enumerate(payload if isinstance(payload, list) else []):
        entry = _normalize_entry(raw, index)
        if entry is not None:
            entries.append(entry)
    with _config_lock:
        _config_cache["key"] = key
        _config_cache["entries"] = entries
    return list(entries)


def available() -> tuple[bool, str]:
    if config_path() is None:
        return False, "STATE_ORTHO_SOURCES is not set"
    if not load_config():
        return False, "STATE_ORTHO_SOURCES has no usable entries"
    return True, ""


def _bbox_contains(bbox: list[float], lat: float, lon: float) -> bool:
    return bbox[0] <= lon <= bbox[2] and bbox[1] <= lat <= bbox[3]


def match_entry(lat: float, lon: float, state: str | None, entries: list[dict] | None = None) -> dict | None:
    """First entry consistent with the site's state and/or location.

    An entry with a bbox only matches points inside it; an entry with states
    only matches when ``state`` is one of them (a states-only entry cannot be
    matched without ``state``). An entry with both requires both to agree,
    except that a missing ``state`` is resolved by the bbox alone.
    """
    entries = load_config() if entries is None else entries
    st = (state or "").strip().upper()
    for entry in entries:
        bbox = entry.get("bbox")
        states = entry.get("states") or []
        if bbox is not None and not _bbox_contains(bbox, lat, lon):
            continue
        if states:
            if st and st not in states:
                continue
            if not st and bbox is None:
                continue
        if entry.get("token_env") and not env_str(entry["token_env"]):
            base.log_once(f"state_ortho:token:{entry['name']}", logging.WARNING,
                          "State ortho %r skipped: %s is not set", entry["name"], entry["token_env"])
            continue
        return entry
    return None


def build_request(entry: dict, lat: float, lon: float) -> tuple[str, dict]:
    """(url, params) for a square chip of ``entry['chip_m']`` metres."""
    minx, miny, maxx, maxy = base.mercator_bbox(lat, lon, entry["chip_m"])
    bbox = f"{minx:.3f},{miny:.3f},{maxx:.3f},{maxy:.3f}"
    px = int(entry["px"])
    kind = entry["type"]
    if kind == "arcgis_image":
        url = f"{entry['url']}/exportImage"
        params = {"bbox": bbox, "bboxSR": "3857", "imageSR": "3857",
                  "size": f"{px},{px}", "format": "jpg", "f": "image"}
    elif kind == "arcgis_map":
        url = f"{entry['url']}/export"
        params = {"bbox": bbox, "bboxSR": "3857", "imageSR": "3857",
                  "size": f"{px},{px}", "format": "jpg", "f": "image",
                  "transparent": "false"}
        if entry.get("layer"):
            params["layers"] = f"show:{entry['layer']}"
    elif kind == "wms":
        url = entry["url"]
        params = {"SERVICE": "WMS", "VERSION": "1.3.0", "REQUEST": "GetMap",
                  "CRS": "EPSG:3857", "BBOX": bbox, "WIDTH": str(px), "HEIGHT": str(px),
                  "LAYERS": entry.get("layer") or "", "STYLES": "",
                  "FORMAT": "image/jpeg"}
    else:  # pragma: no cover - filtered by _normalize_entry
        raise ValueError(f"unknown state ortho type {kind!r}")
    if entry.get("token_env"):
        token = env_str(entry["token_env"])
        if token:
            params["token"] = token
    return url, params


def _cache_path(entry: dict, lat: float, lon: float) -> Path | None:
    root = base.cache_dir(NAME)
    if root is None:
        return None
    stem = f"{lat:.5f}_{lon:.5f}_{float(entry['chip_m']):g}_{int(entry['px'])}"
    return root / base.safe_key(entry["name"]) / f"{base.safe_key(stem)}.img"


def _label(entry: dict, state: str | None) -> str:
    # State code only (not the free-text entry name), so a name such as
    # "West ..." cannot make views.is_oblique_label misread the view.
    region = (state or "").strip().upper() or (entry["states"][0] if entry["states"] else "regional")
    parts = [region]
    if entry.get("year"):
        parts[0] = f"{region} {entry['year']}"
    if entry.get("resolution_cm"):
        parts.append(f"~{float(entry['resolution_cm']):g} cm")
    parts.append(f"{float(entry['chip_m']):g} m across, site at center")
    return f"State orthoimagery top-down ({', '.join(parts)})"


def fetch(lat: float, lon: float, *, state: str | None = None) -> list[base.SupplementalView]:
    entry = match_entry(lat, lon, state)
    if entry is None:
        return []
    path = _cache_path(entry, lat, lon)
    data = base.cache_read(path)
    img = base.decode_image(data) if data else None
    if img is None:
        url, params = build_request(entry, lat, lon)
        resp = base.http_get(url, params=params)
        if not resp.ok:
            logger.warning("State ortho %r HTTP %s for %s", entry["name"], resp.status_code,
                           base.redact(url))
            return []
        data = resp.content
        img = base.decode_image(data, (resp.headers or {}).get("Content-Type"))
        if img is None:
            logger.warning("State ortho %r returned a non-image response", entry["name"])
            return []
        if base.is_blank_image(img):
            logger.info("State ortho %r: blank image (no coverage) at %.5f,%.5f",
                        entry["name"], lat, lon)
            return []
        base.cache_write(path, data)
    elif base.is_blank_image(img):
        return []
    year = entry.get("year")
    captured = str(entry.get("captured") or year) if (entry.get("captured") or year) else None
    meta = {
        "service": entry["name"],
        "service_type": entry["type"],
        "resolution_cm": entry.get("resolution_cm"),
        "chip_m": entry["chip_m"],
        "px": entry["px"],
        "year": year,
    }
    return [base.SupplementalView(label=_label(entry, state), image=img, source=NAME,
                                  captured=captured, meta=meta)]
