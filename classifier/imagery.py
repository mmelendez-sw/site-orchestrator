"""Aerial imagery: NAIP chips (Planetary Computer) and Nearmap Tile API views.

Both sources are cached on disk under ``SITE_ORCHESTRATOR_DATA/cache`` so a
rerun, a wide/re-centered AOI, or a neighboring pin never downloads the same
pixels twice:

* NAIP: one PNG + JSON per (STAC item, exact point, chip size). The STAC
  search still runs so a newer NAIP year is picked up.
* Nearmap: one JPEG per tile, keyed by the survey capture date that the
  coverage API reports for the point. A new survey means a new key, so stale
  tiles are never served. Tiles with an unknown capture date are not cached.

Set ``IMAGERY_CACHE=0`` (or ``NEARMAP_TILE_CACHE=0``) to disable. Check your
Nearmap agreement for how long cached tiles may be retained; the run folders
already keep per-site JPEGs indefinitely.
"""

from __future__ import annotations

import io
import json
import logging
import math
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import numpy as np
import planetary_computer
import rasterio
import requests
from PIL import Image
from pyproj import Transformer
from pystac_client import Client
from rasterio.windows import from_bounds

from classifier.views import (
    _is_naip_view,
    asset_view_is_nearmap_oblique,
    coerce_asset_box,
    parse_box_2d,
)
from envutil import env_flag, env_float, env_int, env_str

logger = logging.getLogger(__name__)

STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
COLLECTION = "naip"

# Optional Nearmap integration (Tile API). Obliques show the vertical sides of
# structures, which is what makes rooftop antennas and towers visible. The
# Tile API bills against the subscription's monthly GB allowance; the
# Transactional Content API needs a separate credits add-on (coverage/v2/tx
# returns 403 on this subscription).
NEARMAP_TILE_URL = "https://api.nearmap.com/tiles/v3/{content}/{z}/{x}/{y}.jpg"
NEARMAP_COVERAGE_POINT_URL = "https://api.nearmap.com/coverage/v2/point/{lon},{lat}"
NEARMAP_CHIP_M = 100       # side length of the Nearmap AOI, in meters
# Wide-AOI fallback: rural sites often have vert-only Nearmap coverage and the
# recorded coordinates can put the real asset outside the narrow AOI.
NEARMAP_FALLBACK_CHIP_M = 250
NEARMAP_VERT_ZOOM = env_int("NEARMAP_VERT_ZOOM", 20)
NEARMAP_OBLIQUE_ZOOM = env_int("NEARMAP_OBLIQUE_ZOOM", 20)
NEARMAP_MAX_PX = env_int("NEARMAP_MAX_PX", 1024)
NEARMAP_TILE_WORKERS = max(1, env_int("NEARMAP_TILE_WORKERS", 4))
# Wide rooftop-host scout: Vert only at this zoom (19 ≈ 1/4 the tiles of 20).
NEARMAP_SCOUT_ZOOM = env_int("NEARMAP_SCOUT_ZOOM", 19)
_TILE_PX = 256


def _csv_oblique_views(raw: str, default: tuple[str, ...] = ("North", "East")) -> list[str]:
    allowed = {"North", "East", "South", "West"}
    parts = [p.strip().title() for p in str(raw or "").split(",") if p.strip()]
    views = [p for p in parts if p in allowed]
    return views or list(default)


OBLIQUE_VIEWS = _csv_oblique_views(env_str("NEARMAP_OBLIQUE_VIEWS", "North,East"))
NEARMAP_VIEWS = ["Vert", *OBLIQUE_VIEWS]


def nearmap_api_key() -> str:
    """Current key from the environment (``.env`` may load after import)."""
    return env_str("NEARMAP_API_KEY")


# --------------------------------- caches -----------------------------------


def _cache_root() -> Path | None:
    if not env_flag("IMAGERY_CACHE", True):
        return None
    from paths import data_root

    return data_root() / "cache"


def _tile_cache_dir() -> Path | None:
    root = _cache_root()
    if root is None or not env_flag("NEARMAP_TILE_CACHE", True):
        return None
    return root / "nearmap"


def _naip_cache_dir() -> Path | None:
    root = _cache_root()
    return None if root is None else root / "naip"


def _safe_key(text: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in str(text))


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{threading.get_ident()}.tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


# ---------------------------------- NAIP ------------------------------------

_catalog = None
_catalog_lock = threading.Lock()


def get_catalog():
    global _catalog
    with _catalog_lock:
        if _catalog is None:
            _catalog = Client.open(STAC_URL, modifier=planetary_computer.sign_inplace)
        return _catalog


def _age_years(image_date: str | None) -> float | None:
    if not image_date:
        return None
    try:
        acquired = date.fromisoformat(str(image_date)[:10])
    except ValueError:
        return None
    return round((date.today() - acquired).days / 365.25, 1)


def _naip_image_meta(item) -> dict:
    """Pull acquisition/refresh fields from NAIP STAC item properties."""
    acquired = item.datetime.date() if item.datetime else None
    props = item.properties or {}
    image_date = acquired.isoformat() if acquired else None
    return {
        "image_date": image_date,
        "naip_year": props.get("naip:year"),
        "naip_state": props.get("naip:state"),
        "naip_gsd_m": props.get("gsd"),
        "image_age_years": _age_years(image_date),
        "naip_chip_m": None,
    }


def _naip_cache_paths(item_id: str, lat: float, lon: float, chip_m: float):
    root = _naip_cache_dir()
    if root is None:
        return None
    stem = f"{lat:.7f}_{lon:.7f}_{float(chip_m):g}"
    base = root / _safe_key(item_id) / _safe_key(stem)
    return base.with_suffix(".png"), base.with_suffix(".json")


def _load_naip_cache(paths) -> tuple[Image.Image, dict, dict] | None:
    if paths is None:
        return None
    png, meta_path = paths
    if not (png.is_file() and meta_path.is_file()):
        return None
    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
        with Image.open(png) as im:
            img = im.convert("RGB")
    except (OSError, ValueError):
        return None
    meta = dict(payload.get("meta") or {})
    meta["image_age_years"] = _age_years(meta.get("image_date"))
    return img, meta, dict(payload.get("geo") or {})


def _store_naip_cache(paths, img: Image.Image, meta: dict, geo: dict) -> None:
    if paths is None:
        return
    png, meta_path = paths
    try:
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        _atomic_write(png, buf.getvalue())
        _atomic_write(
            meta_path,
            json.dumps({"meta": meta, "geo": geo}).encode("utf-8"),
        )
    except OSError as exc:
        logger.info("NAIP cache write skipped: %s", exc)


def fetch_chip(lat: float, lon: float, chip_m: float):
    """Return (PIL.Image, meta, geo) for the newest NAIP scene at a point, or
    (None, None, None) if no imagery covers the location.

    `meta` includes acquisition date / NAIP year / GSD from STAC photo metadata.
    `geo` holds the chip's CRS and projected bounds so a detection box drawn on
    the image can be converted back to real-world coordinates.
    """
    search = get_catalog().search(
        collections=[COLLECTION],
        intersects={"type": "Point", "coordinates": [lon, lat]},
    )
    items = sorted(search.items(), key=lambda i: i.datetime, reverse=True)
    if not items:
        return None, None, None

    item = items[0]
    cache_paths = _naip_cache_paths(item.id, lat, lon, chip_m)
    cached = _load_naip_cache(cache_paths)
    if cached is not None:
        return cached

    href = item.assets["image"].href
    with rasterio.open(href) as src:
        # NAIP rasters are in UTM; project the WGS84 point into the raster CRS
        transformer = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True)
        x, y = transformer.transform(lon, lat)
        half = chip_m / 2.0
        window = from_bounds(x - half, y - half, x + half, y + half, src.transform)
        # Read RGB bands only; boundless handles points near scene edges
        data = src.read([1, 2, 3], window=window, boundless=True, fill_value=0)
        geo = {"crs": str(src.crs),
               "x_min": x - half, "x_max": x + half,
               "y_min": y - half, "y_max": y + half,
               "chip_m": chip_m}

    img = Image.fromarray(np.transpose(data, (1, 2, 0)).astype(np.uint8))
    meta = _naip_image_meta(item)
    meta["naip_chip_m"] = chip_m
    _store_naip_cache(cache_paths, img, meta, geo)
    return img, meta, geo


# ----------------------------- box -> lat/lon -------------------------------


def box_to_latlon(geo: dict, box) -> tuple[float, float, float] | None:
    """Convert a [ymin, xmin, ymax, xmax] box in 0-1000 normalized image
    coordinates on the NAIP chip into (lat, lon, offset_m), where offset_m is
    the distance from the box center to the chip center (the input coordinate).
    Returns None if the box is malformed."""
    try:
        ymin, xmin, ymax, xmax = (float(v) for v in box[:4])
    except (TypeError, ValueError):
        return None
    if not (0 <= ymin <= ymax <= 1000 and 0 <= xmin <= xmax <= 1000):
        return None
    # Normalized box center -> projected coordinates (y axis is flipped:
    # image row 0 is the chip's northern edge / max projected y)
    cx_n = (xmin + xmax) / 2000.0
    cy_n = (ymin + ymax) / 2000.0
    x = geo["x_min"] + cx_n * (geo["x_max"] - geo["x_min"])
    y = geo["y_max"] - cy_n * (geo["y_max"] - geo["y_min"])
    to_wgs84 = Transformer.from_crs(geo["crs"], "EPSG:4326", always_xy=True)
    lon, lat = to_wgs84.transform(x, y)
    center_x = (geo["x_min"] + geo["x_max"]) / 2.0
    center_y = (geo["y_min"] + geo["y_max"]) / 2.0
    offset_m = math.hypot(x - center_x, y - center_y)
    return lat, lon, offset_m


def box_to_latlon_centered(
    lat: float, lon: float, chip_m: float, box
) -> tuple[float, float, float] | None:
    """Geocode a chip box when the AOI is a square centered on lat/lon.

    Used for Nearmap Vert and as an approximation for Nearmap obliques: the
    mosaic is still fetched around the same AOI, so the box center is a nearby
    pin (perspective error is usually tens of meters, not hundreds).
    """
    try:
        ymin, xmin, ymax, xmax = (float(v) for v in box[:4])
        side = float(chip_m)
    except (TypeError, ValueError):
        return None
    if side <= 0:
        return None
    if ymin > ymax:
        ymin, ymax = ymax, ymin
    if xmin > xmax:
        xmin, xmax = xmax, xmin
    if not (0 <= ymin < ymax <= 1000 and 0 <= xmin < xmax <= 1000):
        return None
    cx = (xmin + xmax) / 2000.0
    cy = (ymin + ymax) / 2000.0
    east_m = (cx - 0.5) * side
    south_m = (cy - 0.5) * side
    dlat = -south_m / 111_320.0
    cos_lat = math.cos(math.radians(lat))
    if abs(cos_lat) < 1e-6:
        return None
    dlon = east_m / (111_320.0 * cos_lat)
    offset_m = math.hypot(east_m, south_m)
    return lat + dlat, lon + dlon, offset_m


def locate_asset_box_latlon(
    *,
    lat: float,
    lon: float,
    box,
    box_view: str | None,
    naip_geo: dict | None = None,
    nearmap_aoi_m: float | None = None,
) -> tuple[float, float, float, str] | None:
    """Map asset_box_2d → (lat, lon, offset_m, source) when possible.

    Preference: true NAIP geo → Nearmap Vert → Nearmap oblique AOI approximation.
    """
    valid = coerce_asset_box(box)
    if valid is None and isinstance(box, (list, tuple)):
        # Allow slightly-oversized boxes through for geocode only.
        valid = parse_box_2d(box)
    if not valid:
        return None

    if _is_naip_view(box_view) and naip_geo:
        located = box_to_latlon(naip_geo, valid)
        if located:
            return located[0], located[1], located[2], "naip_asset_box"

    chip_m = float(nearmap_aoi_m or NEARMAP_CHIP_M)
    view = str(box_view or "").lower()
    if "top-down" in view or ("vert" in view and "oblique" not in view):
        located = box_to_latlon_centered(lat, lon, chip_m, valid)
        if located:
            return located[0], located[1], located[2], "nearmap_vert_box"

    if asset_view_is_nearmap_oblique(box_view):
        located = box_to_latlon_centered(lat, lon, chip_m, valid)
        if located:
            return located[0], located[1], located[2], "nearmap_oblique_box"

    return None


# --------------------------------- Nearmap ----------------------------------

_session_local = threading.local()
_nearmap_coverage_cache: dict[tuple[float, float], tuple[bool, str | None]] = {}
_coverage_lock = threading.Lock()


def _session() -> requests.Session:
    """One Session per thread (tile fetches run in a pool)."""
    session = getattr(_session_local, "session", None)
    if session is None:
        session = requests.Session()
        _session_local.session = session
    return session


def _nearmap_get(url: str) -> requests.Response:
    """GET with header auth (keeps the API key out of logged URLs) and a
    short retry on rate-limit/transient errors."""
    import time

    for attempt in range(3):
        resp = _session().get(
            url, headers={"Authorization": f"Apikey {nearmap_api_key()}"},
            timeout=60)
        if resp.status_code in (429, 502, 503) and attempt < 2:
            time.sleep(2 * (attempt + 1))
            continue
        return resp
    return resp


def nearmap_point_coverage(
    lat: float, lon: float
) -> tuple[bool, str | None]:
    """Return (has_survey, capture_date). Fail-open on transport errors.

    Cached per ~1 m so Vert then oblique fetches at the same pin do not
    re-hit the coverage API.
    """
    if not nearmap_api_key():
        return False, None
    key = (round(float(lat), 5), round(float(lon), 5))
    with _coverage_lock:
        cached = _nearmap_coverage_cache.get(key)
    if cached is not None:
        return cached
    try:
        resp = _nearmap_get(
            NEARMAP_COVERAGE_POINT_URL.format(lon=lon, lat=lat) + "?limit=1"
        )
        if resp.status_code in (401, 403):
            result = (True, None)
        elif not resp.ok:
            result = (False, None)
        else:
            surveys = resp.json().get("surveys") or []
            capture = None
            if surveys:
                capture = surveys[0].get("captureDate")
            result = (bool(surveys), capture)
    except Exception:
        result = (True, None)
    with _coverage_lock:
        _nearmap_coverage_cache[key] = result
    return result


def _tile_range(lat: float, lon: float, half_m: float, zoom: int):
    """Slippy-tile x/y index range covering a half_m-radius box at a zoom."""
    dlat = half_m / 111_320.0
    dlon = half_m / (111_320.0 * math.cos(math.radians(lat)))
    n = 2 ** zoom

    def tile_xy(la, lo):
        x = (lo + 180.0) / 360.0 * n
        y = (1.0 - math.asinh(math.tan(math.radians(la))) / math.pi) / 2.0 * n
        return x, y

    x_west, y_north = tile_xy(lat + dlat, lon - dlon)
    x_east, y_south = tile_xy(lat - dlat, lon + dlon)
    return int(x_west), int(x_east), int(y_north), int(y_south)


def _tile_cache_path(capture_date: str | None, view: str, z: int, x: int, y: int):
    root = _tile_cache_dir()
    if root is None or not capture_date:
        return None
    return root / _safe_key(capture_date) / view / str(z) / f"{x}_{y}.jpg"


def _fetch_tile(
    view: str, z: int, x: int, y: int, capture_date: str | None,
    cache_only: bool = False,
) -> tuple[bytes | None, bool]:
    """(tile JPEG bytes or None on 404, served-from-cache).

    ``cache_only`` never calls Nearmap: a tile missing from the cache is None.
    """
    path = _tile_cache_path(capture_date, view, z, x, y)
    if path is not None and path.is_file():
        try:
            return path.read_bytes(), True
        except OSError:
            pass
    if cache_only:
        return None, False
    resp = _nearmap_get(NEARMAP_TILE_URL.format(content=view, z=z, x=x, y=y))
    if resp.status_code == 404:   # no coverage for this tile/view
        return None, False
    resp.raise_for_status()
    data = resp.content
    if path is not None:
        try:
            _atomic_write(path, data)
        except OSError as exc:
            logger.info("Nearmap tile cache write skipped: %s", exc)
    return data, False


def _tile_position(view: str, tx: int, ty: int, x0: int, x1: int, y0: int, y1: int):
    """Canvas offset of one tile; obliques are rotated so 'up' faces the camera."""
    if view in ("Vert", "North"):     # north-up
        return (tx - x0) * _TILE_PX, (ty - y0) * _TILE_PX
    if view == "South":               # south-up: both axes flip
        return (x1 - tx) * _TILE_PX, (y1 - ty) * _TILE_PX
    if view == "East":                # east-up: up = +x, right = +y
        return (ty - y0) * _TILE_PX, (x1 - tx) * _TILE_PX
    return (y1 - ty) * _TILE_PX, (tx - x0) * _TILE_PX   # west-up


# ------------------------------ spend metering ------------------------------

# Purposes the budget guard drops first (at the soft limit); "pack" (the
# primary Vert / first oblique) only stops at the hard limit.
OPTIONAL_PURPOSES = frozenset({"wide", "second", "recenter", "oblique_extra"})


class NearmapMeter:
    """Per-site Nearmap spend: billed bytes/tiles, cache hits, by purpose."""

    def __init__(self, site_id: str | None = None) -> None:
        self.site_id = site_id
        self.bytes = 0
        self.tiles = 0
        self.cache_hits = 0
        self.by_purpose: dict[str, int] = {}
        self.budget_blocked = False
        self.budget_skipped: list[str] = []

    def record(self, purpose: str, tiles: list[tuple[bytes | None, bool]]) -> int:
        billed = sum(len(data) for data, cached in tiles if data and not cached)
        self.bytes += billed
        self.tiles += sum(1 for data, cached in tiles if data and not cached)
        self.cache_hits += sum(1 for data, cached in tiles if data and cached)
        self.by_purpose[purpose] = self.by_purpose.get(purpose, 0) + billed
        return billed

    def as_row(self) -> dict[str, Any]:
        return {
            "nearmap_bytes": self.bytes,
            "nearmap_tiles": self.tiles,
            "nearmap_cache_hits": self.cache_hits,
            "nearmap_spend": json.dumps(self.by_purpose, sort_keys=True) if self.by_purpose else "",
            "nearmap_budget_blocked": self.budget_blocked,
        }


_meter_local = threading.local()


@contextmanager
def nearmap_meter(site_id: str | None = None) -> Iterator[NearmapMeter]:
    """Meter every Nearmap purchase made on this thread (one site).

    ``site_id`` (optional) tags this site's rows in the purchases ledger.
    """
    prior = getattr(_meter_local, "meter", None)
    meter = NearmapMeter(site_id=site_id)
    _meter_local.meter = meter
    try:
        yield meter
    finally:
        _meter_local.meter = prior


def _current_meter() -> NearmapMeter | None:
    return getattr(_meter_local, "meter", None)


class NearmapBudget:
    """Month-to-date Nearmap bytes vs ``NEARMAP_MONTHLY_BUDGET_MB`` (0 = off).

    Checks happen per view, so concurrent workers can overshoot by at most a
    few views (~0.5 MB each) plus what other lanes bought within one refresh
    interval.

    Local mode (``shared=False``, plain constructor): ``seed`` sets the total
    and ``add`` grows it; nothing touches disk.

    Shared mode (``BUDGET``, built by ``from_env``): every ``add`` appends a
    row to ``metrics/nearmap_purchases.jsonl`` (inter-process-safe), and
    ``spent`` is the fleet-wide figure re-read from disk at most every
    ``NEARMAP_BUDGET_REFRESH_S`` (incremental tail, so a refresh only parses
    new rows)::

        spent = usage-ledger bytes this month NOT flagged in_purchases_ledger
                (legacy rows / lanes still on old code)
              + purchases-ledger bytes this month (every lane, dry runs too)
              + this process's purchases not yet read back from disk
              + off-ledger prior use (``seed`` / ``set_prior_bytes``, this month)

    ``seed(x)`` keeps its old contract (``x`` = usage-ledger month-to-date +
    NEARMAP_PRIOR_USE_MB): the part of ``x`` above the usage ledger becomes
    the prior-use offset; the ledger part is already counted from disk.
    """

    def __init__(self, limit_mb: float, soft_pct: float, *, shared: bool = False,
                 root: Path | str | None = None, refresh_s: float | None = None,
                 clock=None, now=None) -> None:
        self.limit_bytes = int(limit_mb * 1024 * 1024) if limit_mb > 0 else 0
        self.soft_bytes = int(self.limit_bytes * max(0.0, min(1.0, soft_pct / 100.0)))
        self.shared = bool(shared)
        self.refresh_s = max(0.0, float(
            env_float("NEARMAP_BUDGET_REFRESH_S", 5) if refresh_s is None else refresh_s
        ))
        # Tags this process's purchase rows (pid alone can be recycled).
        self.proc = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.run_id: str | None = None
        self.write_failures = 0
        self._root = Path(root) if root is not None else None
        self._clock = clock or time.monotonic
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._spent = 0
        self._lock = threading.Lock()
        # Shared-mode state (per "YYYY-MM").
        self._tails: tuple[Any, Any] | None = None
        self._tails_root: Path | None = None
        self._purchased: dict[str, int] = {}
        self._legacy: dict[str, int] = {}
        self._usage_all: dict[str, int] = {}
        self._own_unread: dict[str, int] = {}
        self._prior: tuple[str, int] = ("", 0)
        self._last_refresh: float | None = None

    @classmethod
    def from_env(cls) -> "NearmapBudget":
        return cls(
            env_float("NEARMAP_MONTHLY_BUDGET_MB", 1500),
            env_float("NEARMAP_BUDGET_SOFT_PCT", 90),
            shared=True,
        )

    # ---- shared ledger plumbing ----

    def ledger_root(self) -> Path:
        if self._root is not None:
            return self._root
        from enrichment import metrics

        return metrics.metrics_dir()

    def _month(self) -> str:
        return self._now().strftime("%Y-%m")

    def _refresh_locked(self, *, force: bool = False) -> None:
        tick = self._clock()
        if (not force and self._last_refresh is not None
                and tick - self._last_refresh < self.refresh_s):
            return
        from enrichment import metrics

        root = self.ledger_root()
        if self._tails is None or self._tails_root != root:
            self._tails_root = root
            self._tails = (
                metrics.JsonlTail(root / metrics.NEARMAP_PURCHASES_JSONL),
                metrics.JsonlTail(root / metrics.NEARMAP_USAGE_JSONL),
            )
            self._purchased.clear()
            self._legacy.clear()
            self._usage_all.clear()
        purchases, usage = self._tails
        rows, reset = purchases.read_new()
        if reset:
            self._purchased.clear()
        for rec in rows:
            month, nbytes = str(rec.get("at") or "")[:7], _ledger_int(rec.get("bytes"))
            self._purchased[month] = self._purchased.get(month, 0) + nbytes
            if rec.get("proc") == self.proc:
                self._own_unread[month] = max(0, self._own_unread.get(month, 0) - nbytes)
        rows, reset = usage.read_new()
        if reset:
            self._legacy.clear()
            self._usage_all.clear()
        for rec in rows:
            month, nbytes = str(rec.get("at") or "")[:7], _ledger_int(rec.get("bytes"))
            self._usage_all[month] = self._usage_all.get(month, 0) + nbytes
            if metrics.is_legacy_usage_row(rec):
                self._legacy[month] = self._legacy.get(month, 0) + nbytes
        self._last_refresh = tick

    def _shared_total_locked(self) -> int:
        month = self._month()
        total = (
            self._legacy.get(month, 0)
            + self._purchased.get(month, 0)
            + self._own_unread.get(month, 0)
        )
        if self._prior[0] == month:
            total += self._prior[1]
        return total

    def refresh(self) -> int:
        """Force a re-read of the shared ledgers now; returns ``spent``."""
        with self._lock:
            if self.shared:
                self._refresh_locked(force=True)
                return self._shared_total_locked()
            return self._spent

    # ---- public API ----

    @property
    def spent(self) -> int:
        with self._lock:
            if not self.shared:
                return self._spent
            self._refresh_locked()
            return self._shared_total_locked()

    def seed(self, month_to_date_bytes: int) -> None:
        """Local: set the total. Shared: keep the excess over the usage ledger
        (e.g. NEARMAP_PRIOR_USE_MB) as this month's off-ledger prior use."""
        with self._lock:
            if not self.shared:
                self._spent = max(0, int(month_to_date_bytes))
                return
            self._refresh_locked(force=True)
            month = self._month()
            excess = int(month_to_date_bytes) - self._usage_all.get(month, 0)
            self._prior = (month, max(0, excess))

    def set_prior_bytes(self, nbytes: int) -> None:
        """Off-ledger Nearmap use this month (shared mode's NEARMAP_PRIOR_USE_MB)."""
        with self._lock:
            if not self.shared:
                self._spent = max(0, int(nbytes))
                return
            self._prior = (self._month(), max(0, int(nbytes)))

    def add(self, nbytes: int, *, purpose: str = "pack", site_id: str | None = None,
            tiles: int | None = None) -> None:
        nbytes = max(0, int(nbytes))
        if not self.shared:
            with self._lock:
                self._spent += nbytes
            return
        if not nbytes:
            return
        at = self._now().strftime("%Y-%m-%dT%H:%M:%SZ")
        month = at[:7]
        with self._lock:
            # Counted locally first; the refresh that reads the row back
            # moves it from _own_unread to _purchased (never double counted).
            self._own_unread[month] = self._own_unread.get(month, 0) + nbytes
        try:
            from enrichment import metrics

            metrics.record_nearmap_purchase(
                nbytes, purpose=purpose, proc=self.proc, run_id=self.run_id,
                site_id=site_id, tiles=tiles, at=at, root=self.ledger_root(),
            )
        except OSError as exc:
            with self._lock:
                self.write_failures += 1
            logger.warning(
                "Nearmap purchase (%s bytes) not written to the shared ledger "
                "(%s); counted by this process only", nbytes, exc,
            )

    def allows(self, purpose: str) -> bool:
        if not self.limit_bytes:
            return True
        spent = self.spent
        if spent >= self.limit_bytes:
            return False
        return not (purpose in OPTIONAL_PURPOSES and spent >= self.soft_bytes)

    def describe(self) -> str:
        mb = 1024 * 1024
        scope = " across all lanes" if self.shared else ""
        if not self.limit_bytes:
            used = f" ({self.spent / mb:.0f} MB used this month{scope})" if self.shared else ""
            return f"Nearmap budget: off (NEARMAP_MONTHLY_BUDGET_MB=0){used}"
        return (
            f"Nearmap budget: {self.spent / mb:.0f} of {self.limit_bytes / mb:.0f} MB used this month"
            f"{scope} (optional purchases stop at {self.soft_bytes / mb:.0f} MB)"
        )


def _ledger_int(value: Any) -> int:
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return 0


BUDGET = NearmapBudget.from_env()


def _stitch_view(lat: float, lon: float, chip_m: float, view: str,
                 capture_date: str | None, *, zoom: int | None = None,
                 purpose: str = "pack", cache_only: bool = False) -> Image.Image | None:
    zoom = zoom or (NEARMAP_VERT_ZOOM if view == "Vert" else NEARMAP_OBLIQUE_ZOOM)
    x0, x1, y0, y1 = _tile_range(lat, lon, chip_m / 2.0, zoom)
    cols, rows = x1 - x0 + 1, y1 - y0 + 1
    # East/West mosaics have the slippy x axis running vertically.
    if view in ("East", "West"):
        canvas = Image.new("RGB", (rows * _TILE_PX, cols * _TILE_PX))
    else:
        canvas = Image.new("RGB", (cols * _TILE_PX, rows * _TILE_PX))

    coords = [(tx, ty) for ty in range(y0, y1 + 1) for tx in range(x0, x1 + 1)]
    with ThreadPoolExecutor(max_workers=min(NEARMAP_TILE_WORKERS, len(coords))) as pool:
        tiles = list(pool.map(
            lambda xy: _fetch_tile(view, zoom, xy[0], xy[1], capture_date, cache_only), coords
        ))
    meter = _current_meter()
    billed = meter.record(purpose, tiles) if meter is not None else sum(
        len(data) for data, cached in tiles if data and not cached
    )
    BUDGET.add(
        billed,
        purpose=purpose,
        site_id=meter.site_id if meter is not None else None,
        tiles=sum(1 for data, cached in tiles if data and not cached),
    )

    got_any = False
    for (tx, ty), (data, _cached) in zip(coords, tiles):
        if data is None:
            continue
        tile = Image.open(io.BytesIO(data)).convert("RGB")
        canvas.paste(tile, _tile_position(view, tx, ty, x0, x1, y0, y1))
        got_any = True
    if not got_any:
        return None
    if view != "Vert":
        # Compensate the 45-degree foreshortening (256 -> 192 height)
        canvas = canvas.resize((canvas.width, max(1, int(canvas.height * 0.75))))
    canvas.thumbnail((NEARMAP_MAX_PX, NEARMAP_MAX_PX))
    return canvas


def _cached_capture_date(lat: float, lon: float, chip_m: float, view: str) -> str | None:
    """Newest cached survey date holding every tile this view needs, or None."""
    root = _tile_cache_dir()
    if root is None or not root.is_dir():
        return None
    zoom = NEARMAP_VERT_ZOOM if view == "Vert" else NEARMAP_OBLIQUE_ZOOM
    x0, x1, y0, y1 = _tile_range(lat, lon, chip_m / 2.0, zoom)
    for date_dir in sorted((p for p in root.iterdir() if p.is_dir()), reverse=True):
        tile_dir = date_dir / view / str(zoom)
        if tile_dir.is_dir() and all(
            (tile_dir / f"{x}_{y}.jpg").is_file()
            for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)
        ):
            return date_dir.name
    return None


def cached_nearmap_views(lat: float, lon: float, chip_m: float = NEARMAP_CHIP_M,
                         views: list[str] | None = None) -> tuple[dict, str | None]:
    """Nearmap views rebuilt only from the on-disk tile cache (never billed).

    Tiles bought for neighbouring sites often cover this pin too. A view is
    used only when every tile it needs is cached for one survey date.
    Returns ({view_name: PIL.Image}, newest capture date).
    """
    result: dict[str, Any] = {}
    dates: list[str] = []
    for view in (views if views is not None else NEARMAP_VIEWS):
        capture = _cached_capture_date(lat, lon, chip_m, view)
        if capture is None:
            continue
        canvas = _stitch_view(lat, lon, chip_m, view, capture, purpose="cache", cache_only=True)
        if canvas is not None:
            result[view] = canvas
            dates.append(capture)
    return result, (max(dates) if dates else None)


def fetch_nearmap_views(lat: float, lon: float, chip_m: float = NEARMAP_CHIP_M,
                        views: list[str] | None = None, *, zoom: int | None = None,
                        purpose: str = "pack"):
    """Fetch Nearmap content for a point via the Tile API: high-res vertical
    plus 45-degree oblique panoramas (N/E/S/W), stitched from XYZ tiles.

    Returns ({view_name: PIL.Image}, capture_date). Empty dict when the key is
    not set, the location has no Nearmap coverage, or the monthly budget
    refuses this ``purpose`` (see ``NearmapBudget``; the site meter records
    that as ``budget_blocked`` / ``budget_skipped``).

    Optional `views` limits which orientations to fetch (e.g. ["Vert"] or
    OBLIQUE_VIEWS). Defaults to all NEARMAP_VIEWS when omitted. ``zoom``
    overrides the per-view zoom (cheap wide scouting). ``purpose`` labels the
    spend: pack | oblique_extra | wide | second | recenter.
    """
    if not nearmap_api_key():
        return {}, None

    has_survey, capture_date = nearmap_point_coverage(lat, lon)
    if not has_survey:
        return {}, None

    result: dict[str, Any] = {}
    for view in (views if views is not None else NEARMAP_VIEWS):
        if not BUDGET.allows(purpose):
            meter = _current_meter()
            if meter is not None:
                if purpose in OPTIONAL_PURPOSES and BUDGET.spent < BUDGET.limit_bytes:
                    meter.budget_skipped.append(purpose)
                else:
                    meter.budget_blocked = True
            logger.warning("Nearmap %s skipped: %s", purpose, BUDGET.describe())
            break
        canvas = _stitch_view(lat, lon, chip_m, view, capture_date, zoom=zoom, purpose=purpose)
        if canvas is not None:
            result[view] = canvas
    if not result:
        return {}, None
    return result, capture_date
