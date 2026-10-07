"""FCC ULS microwave license locations near a pin (rooftop backhaul dishes).

Data: ``dbo.FccUlsMicrowaveLocation`` loaded by scripts/load_fcc_uls_microwave.py
from the ULS complete weekly microwave file (l_micro.zip). Only active
licenses (HD license_status 'A') with valid coordinates are loaded.

Record layouts (pipe-delimited, 0-based field index), per the FCC ULS public
access SQL definitions (public_access_database_definitions_sql_20250417.txt):

HD (59 fields): 0 record_type, 1 unique_system_identifier, 2 uls_file_number,
    3 ebf_number, 4 call_sign, 5 license_status, 6 radio_service_code, ...
LO (51 fields): 0 record_type, 1 unique_system_identifier, 2 uls_file_number,
    3 ebf_number, 4 call_sign, 5 location_action_performed,
    6 location_type_code, 7 location_class_code, 8 location_number,
    9 site_status, ..., 14 location_state, 18 ground_elevation (m),
    19 lat_degrees, 20 lat_minutes, 21 lat_seconds, 22 lat_direction,
    23 long_degrees, 24 long_minutes, 25 long_seconds, 26 long_direction,
    ..., 37 tower_registration_number, 38 height_of_support_structure (m),
    39 overall_height_of_structure (m), 40 structure_type, ...
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Iterator, Sequence

from enrichment.geo import haversine_meters
from enrichment.signals import round_m, warn_once
from envutil import env_float, env_int

logger = logging.getLogger(__name__)

ULS_TABLE = "dbo.FccUlsMicrowaveLocation"
ULS_RADIUS_M = env_float("ULS_RADIUS_M", 150)
ULS_MAX_CALL_SIGNS = 3
ULS_BULK_CHUNK = max(1, env_int("SIGNALS_BULK_CHUNK", 200))
_BULK_POINTS = "#signals_uls_points"

ACTIVE_STATUS = "A"
# LO location_type_code values that describe an operating area, not a fixed
# antenna site (M = mobile). Their coordinates are area centers, so skip them.
NON_FIXED_LOCATION_TYPES = frozenset({"M"})

# HD field positions
HD_USI, HD_CALL_SIGN, HD_STATUS, HD_RADIO_SERVICE = 1, 4, 5, 6
HD_FIELD_COUNT = 59
# LO field positions
LO_USI, LO_CALL_SIGN = 1, 4
LO_TYPE_CODE, LO_NUMBER = 6, 8
LO_GROUND_ELEV = 18
LO_LAT = (19, 20, 21, 22)
LO_LON = (23, 24, 25, 26)
LO_SUPPORT_HEIGHT, LO_OVERALL_HEIGHT, LO_STRUCTURE_TYPE = 38, 39, 40
LO_FIELD_COUNT = 51

LOCATION_COLUMNS = (
    "unique_system_identifier",
    "call_sign",
    "location_number",
    "latitude",
    "longitude",
    "ground_elevation_m",
    "structure_height_m",
    "location_type",
    "license_status",
    "radio_service_code",
    "structure_type",
)


# ------------------------------------------------------------------ parsing


def _f(value: str | None) -> float | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _i(value: str | None) -> int | None:
    num = _f(value)
    return None if num is None else int(num)


def dms_to_decimal(
    degrees: Any, minutes: Any, seconds: Any, direction: Any, *, is_lat: bool
) -> float | None:
    """Signed decimal degrees from ULS D/M/S + N/S/E/W; None when invalid."""
    deg = _f(str(degrees) if degrees is not None else None)
    mins = _f(str(minutes) if minutes is not None else None) or 0.0
    secs = _f(str(seconds) if seconds is not None else None) or 0.0
    hemi = str(direction or "").strip().upper()
    if deg is None or deg < 0 or not (0 <= mins < 60) or not (0 <= secs < 60):
        return None
    if is_lat and hemi not in ("N", "S"):
        return None
    if not is_lat and hemi not in ("E", "W"):
        return None
    value = deg + mins / 60.0 + secs / 3600.0
    if value > (90.0 if is_lat else 180.0):
        return None
    return -value if hemi in ("S", "W") else value


def _field(fields: Sequence[str], idx: int) -> str:
    return fields[idx].strip() if idx < len(fields) else ""


def iter_records(lines: Iterable[str], record_type: str) -> Iterator[list[str]]:
    """Split pipe-delimited ULS lines into field lists.

    A few ULS records contain raw line breaks inside free-text fields; a line
    that does not start with ``"<record_type>|"`` continues the previous one.
    """
    prefix = f"{record_type}|"
    pending: str | None = None
    for raw in lines:
        line = raw.rstrip("\r\n")
        if line.startswith(prefix):
            if pending is not None:
                yield pending.split("|")
            pending = line
        elif pending is not None and line:
            pending += " " + line
    if pending is not None:
        yield pending.split("|")


def parse_hd(fields: Sequence[str]) -> dict[str, Any] | None:
    if _field(fields, 0) != "HD":
        return None
    usi = _i(_field(fields, HD_USI))
    if usi is None:
        return None
    return {
        "unique_system_identifier": usi,
        "call_sign": _field(fields, HD_CALL_SIGN),
        "license_status": _field(fields, HD_STATUS)[:1] or None,
        "radio_service_code": _field(fields, HD_RADIO_SERVICE) or None,
    }


def parse_lo(fields: Sequence[str]) -> dict[str, Any] | None:
    """Location row with signed decimal coords; None when coords are invalid."""
    if _field(fields, 0) != "LO":
        return None
    usi = _i(_field(fields, LO_USI))
    if usi is None:
        return None
    lat = dms_to_decimal(*(_field(fields, i) for i in LO_LAT), is_lat=True)
    lon = dms_to_decimal(*(_field(fields, i) for i in LO_LON), is_lat=False)
    if lat is None or lon is None or (lat == 0.0 and lon == 0.0):
        return None
    height = _f(_field(fields, LO_SUPPORT_HEIGHT))
    if height is None:
        height = _f(_field(fields, LO_OVERALL_HEIGHT))
    return {
        "unique_system_identifier": usi,
        "call_sign": _field(fields, LO_CALL_SIGN),
        "location_number": _i(_field(fields, LO_NUMBER)) or 0,
        "latitude": lat,
        "longitude": lon,
        "ground_elevation_m": _f(_field(fields, LO_GROUND_ELEV)),
        "structure_height_m": height,
        "location_type": _field(fields, LO_TYPE_CODE)[:4] or None,
        "structure_type": _field(fields, LO_STRUCTURE_TYPE)[:16] or None,
    }


def active_headers(hd_lines: Iterable[str]) -> tuple[dict[int, dict[str, Any]], int]:
    """{usi: header} for active licenses, plus total HD records seen."""
    headers: dict[int, dict[str, Any]] = {}
    total = 0
    for fields in iter_records(hd_lines, "HD"):
        total += 1
        hd = parse_hd(fields)
        if hd and hd["license_status"] == ACTIVE_STATUS:
            headers[hd["unique_system_identifier"]] = hd
    return headers, total


def build_locations(
    hd_lines: Iterable[str], lo_lines: Iterable[str]
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Active-license fixed locations with valid coords, deduped on (usi, location_number)."""
    headers, hd_total = active_headers(hd_lines)
    stats = {
        "hd_records": hd_total,
        "hd_active": len(headers),
        "lo_records": 0,
        "lo_inactive": 0,
        "lo_bad_coords": 0,
        "lo_mobile": 0,
        "lo_duplicates": 0,
    }
    rows: dict[tuple[int, int], dict[str, Any]] = {}
    for fields in iter_records(lo_lines, "LO"):
        stats["lo_records"] += 1
        usi = _i(_field(fields, LO_USI))
        header = headers.get(usi) if usi is not None else None
        if header is None:
            stats["lo_inactive"] += 1
            continue
        loc = parse_lo(fields)
        if loc is None:
            stats["lo_bad_coords"] += 1
            continue
        if loc["location_type"] in NON_FIXED_LOCATION_TYPES:
            stats["lo_mobile"] += 1
            continue
        loc["license_status"] = header["license_status"]
        loc["radio_service_code"] = header["radio_service_code"]
        loc["call_sign"] = loc["call_sign"] or header["call_sign"]
        key = (loc["unique_system_identifier"], loc["location_number"])
        if key in rows:
            stats["lo_duplicates"] += 1
        rows[key] = loc
    stats["locations"] = len(rows)
    return list(rows.values()), stats


# ------------------------------------------------------------------ lookup

_table_missing = False


def reset_table_state() -> None:
    """Test hook: forget a cached missing-table result."""
    global _table_missing
    _table_missing = False


def _is_missing_table(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "42s02" in text or "invalid object name" in text


def _mark_missing(exc: BaseException) -> None:
    global _table_missing
    _table_missing = True
    warn_once(
        "missing:uls",
        "signal source uls unavailable: %s missing (run scripts/load_fcc_uls_microwave.py): %s",
        ULS_TABLE,
        exc,
    )


def bbox(lat: float, lon: float, radius_m: float) -> tuple[float, float, float, float]:
    """(min_lat, max_lat, min_lon, max_lon) covering radius_m with margin."""
    from enrichment.mssql import _buffer_deg_for_radius

    lat_buf, lon_buf = _buffer_deg_for_radius(radius_m, lat)
    return lat - lat_buf, lat + lat_buf, lon - lon_buf, lon + lon_buf


def summarize(
    lat: float, lon: float, rows: Iterable[dict[str, Any]], *, radius_m: float | None = None
) -> dict[str, Any]:
    """uls_nearest_m / uls_count / uls_call_signs from candidate rows."""
    radius = ULS_RADIUS_M if radius_m is None else float(radius_m)
    hits: list[tuple[float, str]] = []
    for row in rows:
        try:
            r_lat, r_lon = float(row["latitude"]), float(row["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        dist = haversine_meters(lat, lon, r_lat, r_lon)
        if dist <= radius:
            hits.append((dist, str(row.get("call_sign") or "").strip()))
    hits.sort(key=lambda h: h[0])
    signs: list[str] = []
    for _, sign in hits:
        if sign and sign not in signs:
            signs.append(sign)
        if len(signs) >= ULS_MAX_CALL_SIGNS:
            break
    return {
        "uls_nearest_m": round_m(hits[0][0]) if hits else "",
        "uls_count": len(hits),
        "uls_call_signs": ";".join(signs),
    }


_SELECT_COLUMNS = "u.call_sign, u.latitude, u.longitude"
_BBOX_WHERE = "(u.latitude BETWEEN {a} AND {b} AND u.longitude BETWEEN {c} AND {d})"


def _row_dict(cursor, row) -> dict[str, Any]:
    columns = [col[0] for col in cursor.description]
    return dict(zip(columns, row))


def lookup(
    cursor, lat: float, lon: float, *, radius_m: float | None = None
) -> dict[str, Any] | None:
    """Nearest/count/call signs within radius; None when the table is missing."""
    if _table_missing:
        return None
    radius = ULS_RADIUS_M if radius_m is None else float(radius_m)
    min_lat, max_lat, min_lon, max_lon = bbox(lat, lon, radius)
    where = _BBOX_WHERE.format(a="?", b="?", c="?", d="?")
    try:
        cursor.execute(
            f"SELECT {_SELECT_COLUMNS} FROM {ULS_TABLE} AS u WHERE {where}",
            min_lat, max_lat, min_lon, max_lon,
        )
        rows = [_row_dict(cursor, r) for r in cursor.fetchall()]
    except Exception as exc:
        if _is_missing_table(exc):
            _mark_missing(exc)
            return None
        raise
    return summarize(lat, lon, rows, radius_m=radius)


def _bulk_chunk(
    cursor, chunk: Sequence[tuple[str, float, float]], radius: float
) -> dict[str, list[dict[str, Any]]]:
    cursor.execute(
        f"IF OBJECT_ID('tempdb..{_BULK_POINTS}') IS NOT NULL DROP TABLE {_BULK_POINTS}; "
        f"CREATE TABLE {_BULK_POINTS} (query_key nvarchar(64) NOT NULL, "
        "min_lat float NOT NULL, max_lat float NOT NULL, "
        "min_lng float NOT NULL, max_lng float NOT NULL)"
    )
    try:
        cursor.fast_executemany = True
    except AttributeError:
        pass
    cursor.executemany(
        f"INSERT INTO {_BULK_POINTS} (query_key, min_lat, max_lat, min_lng, max_lng) "
        "VALUES (?, ?, ?, ?, ?)",
        [(key, *bbox(lat, lon, radius)) for key, lat, lon in chunk],
    )
    where = _BBOX_WHERE.format(a="p.min_lat", b="p.max_lat", c="p.min_lng", d="p.max_lng")
    cursor.execute(
        f"SELECT p.query_key AS query_key, {_SELECT_COLUMNS} "
        f"FROM {_BULK_POINTS} AS p JOIN {ULS_TABLE} AS u ON {where}"
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for raw in cursor.fetchall():
        row = _row_dict(cursor, raw)
        grouped.setdefault(str(row.pop("query_key")), []).append(row)
    cursor.execute(f"DROP TABLE {_BULK_POINTS}")
    return grouped


def lookup_bulk(
    cursor,
    points: dict[str, tuple[float, float]],
    *,
    radius_m: float | None = None,
    chunk_size: int | None = None,
) -> dict[str, dict[str, Any] | None]:
    """``lookup`` for many points: one temp-table join per chunk.

    Returns {key: summary}; every value is None when the table is missing.
    Other SQL errors raise (collect_signals_bulk catches them).
    """
    keys = [str(k) for k in points]
    if _table_missing:
        return {k: None for k in keys}
    radius = ULS_RADIUS_M if radius_m is None else float(radius_m)
    size = max(1, int(chunk_size or ULS_BULK_CHUNK))
    items = [(str(k), float(v[0]), float(v[1])) for k, v in points.items()]
    out: dict[str, dict[str, Any] | None] = {}
    for start in range(0, len(items), size):
        chunk = items[start : start + size]
        try:
            grouped = _bulk_chunk(cursor, chunk, radius)
        except Exception as exc:
            if _is_missing_table(exc):
                _mark_missing(exc)
                return {k: None for k in keys}
            raise
        for key, lat, lon in chunk:
            out[key] = summarize(lat, lon, grouped.get(key, []), radius_m=radius)
    return out
