"""Shared plumbing for supplemental imagery sources.

Everything here is source-agnostic: the ``SupplementalView`` result type, the
thread-local ``SourceMeter``, geometry helpers (haversine distance, initial
bearing, Web Mercator bounding boxes), the on-disk cache, a per-thread HTTP
session with a configurable timeout, image sanity checks, and redaction of
API keys / tokens from anything that might be logged.

The cache follows ``classifier.imagery``: it lives under
``SITE_ORCHESTRATOR_DATA/cache/<source>`` and ``IMAGERY_CACHE=0`` disables it.
The helpers are re-implemented here (rather than imported from
``classifier.imagery``) so this package does not pull in rasterio / pystac.
"""

from __future__ import annotations

import io
import logging
import math
import os
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import requests
from PIL import Image, ImageStat

from envutil import env_flag, env_float

logger = logging.getLogger("classifier.sources")

SUPPORTED_SOURCES: tuple[str, ...] = ("state_ortho", "mapillary", "streetview")

# Mean Earth radius (m) for haversine; Web Mercator uses the WGS84 major axis.
EARTH_RADIUS_M = 6_371_008.8
MERCATOR_R = 6_378_137.0
MERCATOR_MAX_LAT = 85.05112878


@dataclass
class SupplementalView:
    """One extra image for the vision model.

    ``label`` is model-facing and says what the image is (source, date, where
    the camera stood). ``source`` is one of ``SUPPORTED_SOURCES``.
    """

    label: str
    image: Image.Image
    source: str
    captured: str | None = None
    meta: dict = field(default_factory=dict)


# ------------------------------ spend metering ------------------------------


class SourceMeter:
    """Per-site supplemental imagery usage.

    ``requests``: HTTP requests sent (any source). ``billable``: requests that
    cost money (Street View image fetches only). ``cache_hits``: responses
    served from the disk cache. ``by_source``: views returned per source.
    """

    def __init__(self) -> None:
        self.requests = 0
        self.billable = 0
        self.cache_hits = 0
        self.by_source: dict[str, int] = {}
        self._lock = threading.Lock()

    def add_request(self, *, billable: bool = False) -> None:
        with self._lock:
            self.requests += 1
            if billable:
                self.billable += 1

    def add_cache_hit(self) -> None:
        with self._lock:
            self.cache_hits += 1

    def add_views(self, source: str, count: int) -> None:
        if count <= 0:
            return
        with self._lock:
            self.by_source[source] = self.by_source.get(source, 0) + int(count)

    def as_row(self) -> dict[str, Any]:
        with self._lock:
            sources = ",".join(f"{name}:{n}" for name, n in sorted(self.by_source.items()))
            return {
                "supplemental_sources": sources,
                "supplemental_requests": self.requests,
                "supplemental_billable": self.billable,
                "supplemental_cache_hits": self.cache_hits,
            }


_meter_local = threading.local()


@contextmanager
def source_meter() -> Iterator[SourceMeter]:
    """Meter every supplemental request made on this thread (one site)."""
    prior = getattr(_meter_local, "meter", None)
    meter = SourceMeter()
    _meter_local.meter = meter
    try:
        yield meter
    finally:
        _meter_local.meter = prior


def current_meter() -> SourceMeter | None:
    return getattr(_meter_local, "meter", None)


def record_request(*, billable: bool = False) -> None:
    meter = current_meter()
    if meter is not None:
        meter.add_request(billable=billable)


def record_cache_hit() -> None:
    meter = current_meter()
    if meter is not None:
        meter.add_cache_hit()


def record_views(source: str, count: int) -> None:
    meter = current_meter()
    if meter is not None:
        meter.add_views(source, count)


# --------------------------------- logging ----------------------------------

# Env vars whose values are secrets; their values are scrubbed from any text
# passed through ``redact``. Sources add per-config token env names at runtime.
_SECRET_ENV_NAMES: set[str] = {"MAPILLARY_ACCESS_TOKEN", "GOOGLE_MAPS_API_KEY"}
_secret_lock = threading.Lock()
_SECRET_PARAM_RE = re.compile(
    r"(?i)\b(access_token|api_key|apikey|key|token|signature|client_secret)=([^&\s'\"<>]+)"
)
_AUTH_HEADER_RE = re.compile(r"(?i)\b(OAuth|Bearer|Apikey)\s+[A-Za-z0-9._|:\-]{6,}")


def register_secret_env(name: str) -> None:
    if name:
        with _secret_lock:
            _SECRET_ENV_NAMES.add(str(name))


def redact(text: Any) -> str:
    """Remove API keys / tokens from a URL, exception message or log line."""
    out = str(text)
    out = _SECRET_PARAM_RE.sub(lambda m: f"{m.group(1)}=REDACTED", out)
    out = _AUTH_HEADER_RE.sub(lambda m: f"{m.group(1)} REDACTED", out)
    with _secret_lock:
        names = sorted(_SECRET_ENV_NAMES)
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if len(value) >= 4:
            out = out.replace(value, "REDACTED")
    return out


_logged_once: set[str] = set()
_logged_lock = threading.Lock()


def log_once(key: str, level: int, msg: str, *args: Any) -> None:
    """Log ``msg`` the first time ``key`` is seen in this process."""
    with _logged_lock:
        if key in _logged_once:
            return
        _logged_once.add(key)
    logger.log(level, redact(msg % args if args else msg))


def _reset_log_once() -> None:
    """Test hook."""
    with _logged_lock:
        _logged_once.clear()


# --------------------------------- geometry ---------------------------------


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def initial_bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Compass bearing (0 = north, clockwise) from point 1 toward point 2."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def destination_point(lat: float, lon: float, distance_m: float, bearing_deg: float) -> tuple[float, float]:
    """Point ``distance_m`` from (lat, lon) along ``bearing_deg``."""
    d = distance_m / EARTH_RADIUS_M
    b = math.radians(bearing_deg)
    p1, l1 = math.radians(lat), math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(p1),
                         math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), (math.degrees(l2) + 540.0) % 360.0 - 180.0


def angle_diff_deg(a: float, b: float) -> float:
    """Smallest absolute difference between two compass angles (0-180)."""
    d = abs((float(a) - float(b)) % 360.0)
    return 360.0 - d if d > 180.0 else d


_COMPASS_8 = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


def compass_abbrev(bearing_deg: float) -> str:
    """8-point compass abbreviation (``N``, ``NE``, ...).

    Labels use abbreviations on purpose: ``classifier.views.is_oblique_label``
    treats the words north/east/south/west as oblique markers.
    """
    return _COMPASS_8[int(((float(bearing_deg) % 360.0) + 22.5) // 45.0) % 8]


def lonlat_to_mercator(lon: float, lat: float) -> tuple[float, float]:
    lat = max(-MERCATOR_MAX_LAT, min(MERCATOR_MAX_LAT, float(lat)))
    x = MERCATOR_R * math.radians(float(lon))
    y = MERCATOR_R * math.log(math.tan(math.pi / 4.0 + math.radians(lat) / 2.0))
    return x, y


def mercator_bbox(lat: float, lon: float, chip_m: float) -> tuple[float, float, float, float]:
    """Square EPSG:3857 bbox (minx, miny, maxx, maxy) covering ``chip_m`` of
    ground around the point. Mercator units are stretched by 1/cos(lat), so the
    half side in projected metres is ``chip_m / 2 / cos(lat)``."""
    x, y = lonlat_to_mercator(lon, lat)
    cos_lat = max(1e-6, math.cos(math.radians(lat)))
    half = float(chip_m) / 2.0 / cos_lat
    return x - half, y - half, x + half, y + half


def lonlat_bbox(lat: float, lon: float, half_m: float) -> tuple[float, float, float, float]:
    """(minlon, minlat, maxlon, maxlat) square of ``half_m`` metres each side."""
    dlat = half_m / 111_320.0
    dlon = half_m / (111_320.0 * max(1e-6, math.cos(math.radians(lat))))
    return lon - dlon, lat - dlat, lon + dlon, lat + dlat


# ---------------------------------- cache -----------------------------------


def cache_dir(source: str) -> Path | None:
    """``<data root>/cache/<source>``, or None when ``IMAGERY_CACHE=0``."""
    if not env_flag("IMAGERY_CACHE", True):
        return None
    from paths import data_root

    return data_root() / "cache" / source


def safe_key(text: Any) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in str(text))


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{threading.get_ident()}.tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def cache_read(path: Path | None, *, max_age_days: float | None = None) -> bytes | None:
    """Cached bytes (recording a cache hit), or None if absent / expired."""
    if path is None or not path.is_file():
        return None
    try:
        if max_age_days is not None:
            age_s = time.time() - path.stat().st_mtime
            if age_s > max_age_days * 86400.0:
                return None
        data = path.read_bytes()
    except OSError:
        return None
    if not data:
        return None
    record_cache_hit()
    return data


def cache_write(path: Path | None, data: bytes) -> None:
    if path is None or not data:
        return
    try:
        atomic_write(path, data)
    except OSError as exc:
        logger.info("Supplemental cache write skipped (%s): %s", path.name, exc)


def cache_delete(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink()
    except OSError:
        pass


# ----------------------------------- HTTP -----------------------------------

_session_local = threading.local()


def session() -> requests.Session:
    """One Session per thread (sources run from worker threads)."""
    sess = getattr(_session_local, "session", None)
    if sess is None:
        sess = requests.Session()
        _session_local.session = sess
    return sess


def timeout_s() -> float:
    return max(1.0, env_float("SUPPLEMENTAL_TIMEOUT_S", 15.0))


def http_get(
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    billable: bool = False,
) -> requests.Response:
    """GET through the thread's session; counts the request on the meter."""
    record_request(billable=billable)
    return session().get(url, params=params, headers=headers, timeout=timeout_s())


# ---------------------------------- images ----------------------------------


def decode_image(data: bytes | None, content_type: str | None = None) -> Image.Image | None:
    """RGB image from response bytes, or None for non-image payloads (e.g. an
    ArcGIS JSON error or an XML WMS ServiceException returned with 200)."""
    if not data:
        return None
    ctype = (content_type or "").lower()
    if ctype and not ctype.startswith("image/") and "octet-stream" not in ctype:
        return None
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            return im.convert("RGB")
    except Exception:
        return None


def is_blank_image(img: Image.Image, *, uniform_frac: float = 0.95, min_stddev: float = 2.0) -> bool:
    """True when the image is (near) one colour: no coverage, nodata fill, or
    a placeholder tile."""
    rgb = img.convert("RGB")
    stddev = ImageStat.Stat(rgb).stddev
    if max(stddev) < min_stddev:
        return True
    small = rgb.resize((64, 64), Image.Resampling.NEAREST)
    colors = small.getcolors(64 * 64) or []
    if not colors:
        return False
    top = max(count for count, _color in colors)
    return top / float(64 * 64) >= uniform_frac


def to_jpeg_bytes(img: Image.Image, quality: int = 90) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def iso_date_from_epoch_ms(value: Any) -> str | None:
    try:
        ms = float(value)
    except (TypeError, ValueError):
        return None
    if ms <= 0:
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).date().isoformat()
