"""Cheap non-imagery evidence signals near a site pin.

Before buying paid imagery, look for things that usually sit on or next to a
cellular rooftop:

* ``uls``        FCC ULS microwave license locations (rooftop backhaul dishes),
                 from ``dbo.FccUlsMicrowaveLocation`` (scripts/load_fcc_uls_microwave.py).
* ``opencellid`` OpenCelliD cell position estimates (coarse; weak signal only).
* ``osm``        OpenStreetMap antenna / mast / telecom tower tags (Overpass).

Env:
  SIGNALS=1                         turn signal collection on (default off)
  SIGNALS_SOURCES=uls,opencellid,osm  subset of sources to run (default all three)
  ULS_RADIUS_M / ULS_STRONG_M, OPENCELLID_*, OSM_ANTENNA_* — see the submodules.

``collect_signals`` never raises. A source that is disabled, unavailable, or
fails leaves its keys as ``""`` and logs one warning per source per process.
A source that ran and found nothing reports a count of 0 and a blank nearest
distance.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import time
from pathlib import Path
from typing import Any, Callable

from envutil import env_csv, env_flag, env_float

logger = logging.getLogger(__name__)

SIGNAL_COLUMNS = (
    "uls_nearest_m",
    "uls_count",
    "uls_call_signs",
    "opencellid_nearest_m",
    "opencellid_count",
    "osm_antenna_count",
    "signal_strength",
)

SOURCE_ULS = "uls"
SOURCE_OPENCELLID = "opencellid"
SOURCE_OSM = "osm"
ALL_SOURCES = (SOURCE_ULS, SOURCE_OPENCELLID, SOURCE_OSM)
_SOURCE_ALIASES = {
    "uls": SOURCE_ULS,
    "fcc": SOURCE_ULS,
    "fcc_uls": SOURCE_ULS,
    "microwave": SOURCE_ULS,
    "opencellid": SOURCE_OPENCELLID,
    "ocid": SOURCE_OPENCELLID,
    "osm": SOURCE_OSM,
    "osm_antenna": SOURCE_OSM,
}

SOURCE_KEYS: dict[str, tuple[str, ...]] = {
    SOURCE_ULS: ("uls_nearest_m", "uls_count", "uls_call_signs"),
    SOURCE_OPENCELLID: ("opencellid_nearest_m", "opencellid_count"),
    SOURCE_OSM: ("osm_antenna_count",),
}

STRENGTH_STRONG = "strong"
STRENGTH_WEAK = "weak"
STRENGTH_NONE = "none"

# Internal (non-column) key osm_antenna returns for the strength rule.
OSM_TELECOM_NEAREST_KEY = "_osm_telecom_nearest_m"


# --------------------------------------------------------------------- env


def signals_enabled() -> bool:
    """SIGNALS env flag (default "0"). Read at call time."""
    return env_flag("SIGNALS", "0")


def enabled_sources() -> tuple[str, ...]:
    """Sources named in SIGNALS_SOURCES (default all), in canonical order."""
    names = env_csv("SIGNALS_SOURCES")
    if names is None:
        return ALL_SOURCES
    picked: set[str] = set()
    for raw in names:
        canon = _SOURCE_ALIASES.get(raw.strip().lower())
        if canon is None:
            warn_once(f"unknown:{raw}", "SIGNALS_SOURCES: ignoring unknown source %r", raw)
            continue
        picked.add(canon)
    return tuple(s for s in ALL_SOURCES if s in picked)


def uls_strong_m() -> float:
    return env_float("ULS_STRONG_M", 30)


def osm_strong_m() -> float:
    return env_float("OSM_ANTENNA_STRONG_M", 30)


# ------------------------------------------------------------ warn once

_WARNED: set[str] = set()
_WARN_LOCK = threading.Lock()


def warn_once(token: str, msg: str, *args: Any) -> bool:
    """Log ``msg`` at WARNING the first time ``token`` is seen this process."""
    with _WARN_LOCK:
        if token in _WARNED:
            return False
        _WARNED.add(token)
    logger.warning(msg, *args)
    return True


def reset_warnings() -> None:
    """Test hook: forget which warnings were already logged."""
    with _WARN_LOCK:
        _WARNED.clear()


# ------------------------------------------------- shared small helpers


class RateLimiter:
    """Process-wide minimum spacing between calls (``rpm`` calls per minute)."""

    def __init__(
        self,
        rpm: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.interval = 60.0 / rpm if rpm and rpm > 0 else 0.0
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> float:
        """Block until the next slot; return seconds slept."""
        if self.interval <= 0:
            return 0.0
        with self._lock:
            now = self._clock()
            delay = max(0.0, self._next - now)
            self._next = max(now, self._next) + self.interval
        if delay > 0:
            self._sleep(delay)
        return delay


def cache_path(directory: Path, parts: dict[str, Any]) -> Path:
    """Stable file name for a request (never include secrets in ``parts``)."""
    blob = json.dumps(parts, sort_keys=True, separators=(",", ":"))
    return directory / (hashlib.sha1(blob.encode("utf-8")).hexdigest() + ".json")


def cache_read(path: Path, ttl_s: float, *, now: float | None = None) -> Any | None:
    """Payload stored by ``cache_write`` if present and younger than ``ttl_s``."""
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or "payload" not in record:
        return None
    fetched = record.get("fetched_at")
    if not isinstance(fetched, (int, float)):
        return None
    current = time.time() if now is None else now
    if ttl_s > 0 and current - fetched > ttl_s:
        return None
    return record["payload"]


def cache_write(path: Path, payload: Any, *, now: float | None = None) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"fetched_at": time.time() if now is None else now, "payload": payload}),
            encoding="utf-8",
        )
        tmp.replace(path)
    except OSError as exc:
        logger.debug("signal cache write failed for %s: %s", path, exc)


def valid_point(lat: Any, lon: Any) -> bool:
    try:
        lat_f, lon_f = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    if math.isnan(lat_f) or math.isnan(lon_f):
        return False
    if not (-90.0 <= lat_f <= 90.0 and -180.0 <= lon_f <= 180.0):
        return False
    return not (lat_f == 0.0 and lon_f == 0.0)


def round_m(value: float | None) -> float | str:
    return "" if value is None else round(float(value), 1)


# ------------------------------------------------------------- strength


def _num(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def signal_strength(values: dict[str, Any], ran: set[str] | frozenset[str]) -> str:
    """strong / weak / none from source values; "" when no source ran."""
    if not ran:
        return ""
    uls_near = _num(values.get("uls_nearest_m"))
    osm_near = _num(values.get(OSM_TELECOM_NEAREST_KEY))
    if uls_near is not None and uls_near <= uls_strong_m():
        return STRENGTH_STRONG
    if osm_near is not None and osm_near <= osm_strong_m():
        return STRENGTH_STRONG
    for key in ("uls_count", "opencellid_count", "osm_antenna_count"):
        count = _num(values.get(key))
        if count is not None and count > 0:
            return STRENGTH_WEAK
    return STRENGTH_NONE


# ------------------------------------------------------------- collect


def _blank_row() -> dict[str, Any]:
    return {key: "" for key in SIGNAL_COLUMNS}


def _source_failed(source: str, exc: BaseException) -> None:
    warn_once(
        f"fail:{source}",
        "signal source %s unavailable (%s: %s); its columns stay blank",
        source,
        type(exc).__name__,
        exc,
    )


def _finish(values: dict[str, Any], ran: set[str]) -> dict[str, Any]:
    row = _blank_row()
    for key in SIGNAL_COLUMNS:
        if key in values:
            row[key] = values[key]
    row["signal_strength"] = signal_strength(values, ran)
    return row


def _run_point_sources(
    lat: float, lon: float, sources: tuple[str, ...], values: dict[str, Any], ran: set[str]
) -> None:
    """OpenCelliD + OSM for one point; failures leave keys blank."""
    from enrichment.signals import opencellid, osm_antenna

    if SOURCE_OPENCELLID in sources:
        try:
            if not opencellid.enabled():
                warn_once(
                    "disabled:opencellid",
                    "signal source opencellid skipped: OPENCELLID_API_KEY not set",
                )
            else:
                result = opencellid.lookup(lat, lon)
                if result is not None:
                    values.update(result)
                    ran.add(SOURCE_OPENCELLID)
        except Exception as exc:  # noqa: BLE001 - signals never raise
            _source_failed(SOURCE_OPENCELLID, exc)
    if SOURCE_OSM in sources:
        try:
            result = osm_antenna.lookup(lat, lon)
            if result is not None:
                values.update(result)
                ran.add(SOURCE_OSM)
        except Exception as exc:  # noqa: BLE001
            _source_failed(SOURCE_OSM, exc)


def _uls_skip_reason(cursor) -> str | None:
    if cursor is None:
        warn_once("nocursor:uls", "signal source uls skipped: no SQL cursor passed")
        return "no cursor"
    return None


def collect_signals(lat: float, lon: float, *, cursor=None) -> dict[str, Any]:
    """Every ``SIGNAL_COLUMNS`` key for one point. Never raises."""
    try:
        if not signals_enabled() or not valid_point(lat, lon):
            return _blank_row()
        lat_f, lon_f = float(lat), float(lon)
        sources = enabled_sources()
        values: dict[str, Any] = {}
        ran: set[str] = set()
        if SOURCE_ULS in sources and _uls_skip_reason(cursor) is None:
            try:
                from enrichment.signals import uls

                result = uls.lookup(cursor, lat_f, lon_f)
                if result is not None:
                    values.update(result)
                    ran.add(SOURCE_ULS)
            except Exception as exc:  # noqa: BLE001
                _source_failed(SOURCE_ULS, exc)
        _run_point_sources(lat_f, lon_f, sources, values, ran)
        return _finish(values, ran)
    except Exception as exc:  # noqa: BLE001 - last-resort guard
        warn_once("fail:collect", "collect_signals failed: %s", exc)
        return _blank_row()


def collect_signals_bulk(
    points: dict[str, tuple[float, float]], *, cursor=None
) -> dict[str, dict[str, Any]]:
    """``collect_signals`` for many points keyed by caller key. Never raises.

    ULS runs as one temp-table join per chunk; OpenCelliD and OSM run per
    point (disk-cached, rate-limited).
    """
    out: dict[str, dict[str, Any]] = {str(k): _blank_row() for k in (points or {})}
    try:
        if not signals_enabled() or not points:
            return out
        valid = {
            str(key): (float(pt[0]), float(pt[1]))
            for key, pt in points.items()
            if pt is not None and len(pt) >= 2 and valid_point(pt[0], pt[1])
        }
        sources = enabled_sources()
        uls_results: dict[str, dict[str, Any] | None] = {}
        if valid and SOURCE_ULS in sources and _uls_skip_reason(cursor) is None:
            try:
                from enrichment.signals import uls

                uls_results = uls.lookup_bulk(cursor, valid) or {}
            except Exception as exc:  # noqa: BLE001
                _source_failed(SOURCE_ULS, exc)
                uls_results = {}
        for key, (lat, lon) in valid.items():
            values: dict[str, Any] = {}
            ran: set[str] = set()
            uls_row = uls_results.get(key)
            if uls_row is not None:
                values.update(uls_row)
                ran.add(SOURCE_ULS)
            try:
                _run_point_sources(lat, lon, sources, values, ran)
            except Exception as exc:  # noqa: BLE001
                warn_once("fail:collect", "collect_signals failed: %s", exc)
            out[key] = _finish(values, ran)
    except Exception as exc:  # noqa: BLE001
        warn_once("fail:collect_bulk", "collect_signals_bulk failed: %s", exc)
    return out


__all__ = [
    "SIGNAL_COLUMNS",
    "collect_signals",
    "collect_signals_bulk",
    "enabled_sources",
    "signal_strength",
    "signals_enabled",
]
