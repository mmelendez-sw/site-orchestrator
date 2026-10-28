"""OpenCelliD cells near a pin (weak signal only).

OpenCelliD cell positions are crowd-sourced *estimates* (the averaged
location of phones that heard the cell, rounded to 3 decimals ~ 100 m), not
antenna locations. A nearby cell says "there is coverage here", not "the
antenna is on this roof", so it only ever contributes a ``weak`` signal.

API (https://wiki.opencellid.org/docs/api/cells-in-area):
  GET https://opencellid.org/cell/getInArea?key=..&BBOX=latmin,lonmin,latmax,lonmax&format=json
  limit max 50 (default 50); BBOX area max 4,000,000 m^2 (error code 3);
  1,000 request credits per day per key (error code 7 / HTTP 429);
  empty result -> {"count":0,"cells":[]}; "Cell not found" is code 1.

Env:
  OPENCELLID_API_KEY    required; the source is disabled when unset. Never logged.
  OPENCELLID_RADIUS_M   half-width of the query box and count radius (default 300)
  OPENCELLID_RPM        process-wide request spacing (default 30/min)
  OPENCELLID_TIMEOUT_S  HTTP timeout (default 10)
  OPENCELLID_CACHE_DAYS on-disk cache TTL (default 30)
  OPENCELLID_URL        endpoint override
"""

from __future__ import annotations

import json
import logging
import math
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from enrichment.geo import haversine_meters
from enrichment.signals import (
    RateLimiter,
    cache_path,
    cache_read,
    cache_write,
    round_m,
    warn_once,
)
from envutil import env_float, env_str
from paths import data_root

logger = logging.getLogger(__name__)

OPENCELLID_URL = env_str("OPENCELLID_URL", "https://opencellid.org/cell/getInArea")
OPENCELLID_RADIUS_M = env_float("OPENCELLID_RADIUS_M", 300)
OPENCELLID_TIMEOUT_S = env_float("OPENCELLID_TIMEOUT_S", 10)
OPENCELLID_CACHE_DAYS = env_float("OPENCELLID_CACHE_DAYS", 30)
OPENCELLID_LIMIT = 50
MAX_BBOX_AREA_M2 = 4_000_000.0
# Keep the square box safely under the 4 km^2 API cap.
MAX_RADIUS_M = math.sqrt(MAX_BBOX_AREA_M2) / 2.0 * 0.95

ERR_NOT_FOUND = 1
ERR_BAD_KEY = 2
ERR_DAILY_LIMIT = 7

_limiter = RateLimiter(env_float("OPENCELLID_RPM", 30))
_disabled_reason: str | None = None


class OpenCellIdError(RuntimeError):
    """Request failed; message is already key-redacted."""


def api_key() -> str:
    return env_str("OPENCELLID_API_KEY", "")


def enabled() -> bool:
    return bool(api_key()) and _disabled_reason is None


def reset_state(rpm: float | None = None) -> None:
    """Test hook: clear the process-level disable flag / rate limiter."""
    global _disabled_reason, _limiter
    _disabled_reason = None
    if rpm is not None:
        _limiter = RateLimiter(rpm)


def redact(text: Any, key: str | None = None) -> str:
    """Replace the API key (raw and URL-encoded) with ***."""
    out = str(text)
    secret = api_key() if key is None else key
    if secret:
        for form in {secret, urllib.parse.quote(secret, safe=""), urllib.parse.quote_plus(secret)}:
            out = out.replace(form, "***")
    return out


def cache_dir() -> Path:
    return data_root() / "cache" / "opencellid"


def query_bbox(lat: float, lon: float, radius_m: float) -> tuple[float, float, float, float]:
    """(latmin, lonmin, latmax, lonmax) for a square of half-width radius_m."""
    r = min(max(float(radius_m), 10.0), MAX_RADIUS_M)
    dlat = r / 111_320.0
    dlon = r / (111_320.0 * max(0.2, abs(math.cos(math.radians(lat)))))
    return lat - dlat, lon - dlon, lat + dlat, lon + dlon


def parse_cells(payload: Any) -> list[dict[str, Any]]:
    """Cells from a getInArea JSON body. Raises OpenCellIdError on API errors."""
    if not isinstance(payload, dict):
        raise OpenCellIdError("unexpected OpenCelliD response shape")
    if "error" in payload or ("code" in payload and "cells" not in payload):
        code = payload.get("code")
        try:
            code = int(code)
        except (TypeError, ValueError):
            code = None
        if code == ERR_NOT_FOUND:
            return []
        raise OpenCellIdError(redact(f"OpenCelliD error code={code}: {payload.get('error')}"))
    cells = payload.get("cells") or []
    if not isinstance(cells, list):
        raise OpenCellIdError("unexpected OpenCelliD cells field")
    out: list[dict[str, Any]] = []
    for cell in cells:
        if not isinstance(cell, dict):
            continue
        try:
            lat, lon = float(cell["lat"]), float(cell["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append({
            "lat": lat,
            "lon": lon,
            "radio": cell.get("radio"),
            "range": cell.get("range"),
            "samples": cell.get("samples"),
        })
    return out


def _disable(reason: str) -> None:
    global _disabled_reason
    _disabled_reason = reason
    warn_once("disabled:opencellid:" + reason, "OpenCelliD disabled for this process: %s", reason)


def _http_get_json(url: str, timeout: float) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "site-orchestrator/signals"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_cells(lat: float, lon: float, *, radius_m: float | None = None) -> list[dict[str, Any]]:
    """Cells in the query box (cached on disk). Raises OpenCellIdError."""
    key = api_key()
    if not key or _disabled_reason is not None:
        raise OpenCellIdError(_disabled_reason or "OPENCELLID_API_KEY not set")
    radius = OPENCELLID_RADIUS_M if radius_m is None else float(radius_m)
    box = query_bbox(lat, lon, radius)
    bbox_text = ",".join(f"{v:.6f}" for v in box)
    path = cache_path(cache_dir(), {"bbox": bbox_text, "limit": OPENCELLID_LIMIT})
    cached = cache_read(path, OPENCELLID_CACHE_DAYS * 86400.0)
    if isinstance(cached, list):
        return cached
    query = urllib.parse.urlencode(
        {"key": key, "BBOX": bbox_text, "format": "json", "limit": OPENCELLID_LIMIT}
    )
    _limiter.wait()
    try:
        payload = _http_get_json(f"{OPENCELLID_URL}?{query}", OPENCELLID_TIMEOUT_S)
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        if exc.code in (401, 403):
            _disable(f"HTTP {exc.code} (API key rejected)")
        elif exc.code == 429:
            _disable("HTTP 429 (daily limit exceeded)")
        raise OpenCellIdError(redact(f"OpenCelliD HTTP {exc.code}: {body}", key)) from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise OpenCellIdError(redact(f"OpenCelliD request failed: {exc}", key)) from None
    try:
        cells = parse_cells(payload)
    except OpenCellIdError:
        code = payload.get("code") if isinstance(payload, dict) else None
        if str(code) == str(ERR_BAD_KEY):
            _disable("API key not known")
        elif str(code) == str(ERR_DAILY_LIMIT):
            _disable("daily limit exceeded")
        raise
    cache_write(path, cells)
    return cells


def summarize(
    lat: float, lon: float, cells: list[dict[str, Any]], *, radius_m: float | None = None
) -> dict[str, Any]:
    radius = OPENCELLID_RADIUS_M if radius_m is None else float(radius_m)
    dists = sorted(
        d
        for d in (haversine_meters(lat, lon, c["lat"], c["lon"]) for c in cells)
        if d <= radius
    )
    return {
        "opencellid_nearest_m": round_m(dists[0]) if dists else "",
        "opencellid_count": len(dists),
    }


def lookup(lat: float, lon: float, *, radius_m: float | None = None) -> dict[str, Any] | None:
    """Nearest cell distance and count within radius; None when disabled.

    Raises OpenCellIdError (key-redacted) on request failures.
    """
    if not enabled():
        if not api_key():
            warn_once("disabled:opencellid", "signal source opencellid skipped: OPENCELLID_API_KEY not set")
        return None
    cells = fetch_cells(lat, lon, radius_m=radius_m)
    return summarize(lat, lon, cells, radius_m=radius_m)
