"""Azure SQL / MSSQL helpers for FCC and TowerSource proximity search."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from dedupe.spatial import haversine_meters

from enrichment.constants import (
    BBOX_BUFFER_DEG,
    FCC_TABLE,
    MATCH_SOURCE_FCC,
    MATCH_SOURCE_NONE,
    MATCH_SOURCE_TOWERSOURCE,
    PROXIMITY_MAX_M,
    TOWERSOURCE_TABLE,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProximityHit:
    """Nearest tower match within the proximity radius."""

    source: str
    distance_m: float
    latitude: float
    longitude: float
    record_id: str | None = None
    asr_number: str | None = None
    asset_type: str | None = None
    raw: dict[str, Any] | None = None


def build_odbc_connection_string(
    *,
    server: str | None = None,
    database: str | None = None,
    driver: str | None = None,
    authentication: str | None = None,
    uid: str | None = None,
    pwd: str | None = None,
) -> str:
    """Build an ODBC connection string from env or explicit overrides."""
    server = (server or os.environ.get("AZURE_SQL_SERVER") or "").strip()
    database = (database or os.environ.get("AZURE_SQL_DATABASE") or "").strip()
    driver = (
        driver
        or os.environ.get("AZURE_SQL_DRIVER")
        or "ODBC Driver 18 for SQL Server"
    ).strip()
    authentication = (
        authentication
        or os.environ.get("AZURE_SQL_ODBC_AUTHENTICATION")
        or ""
    ).strip()
    uid = uid if uid is not None else os.environ.get("AZURE_SQL_UID", "").strip()
    pwd = pwd if pwd is not None else os.environ.get("AZURE_SQL_PWD", "").strip()

    if not server or not database:
        raise ValueError(
            "AZURE_SQL_SERVER and AZURE_SQL_DATABASE must be set in the environment"
        )

    parts = [
        f"Driver={{{driver}}}",
        f"Server=tcp:{server},1433",
        f"Database={database}",
        "Encrypt=yes",
        "TrustServerCertificate=no",
    ]
    if authentication:
        parts.append(f"Authentication={authentication}")
    if uid:
        parts.append(f"Uid={uid}")
    if pwd:
        parts.append(f"Pwd={pwd}")
    return ";".join(parts)


def connect_mssql(connection_string: str | None = None):
    """Open a pyodbc connection using Azure SQL env settings."""
    try:
        import pyodbc
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "pyodbc is required for FCC/TowerSource matching. "
            "Install with: pip install pyodbc"
        ) from exc

    conn_str = connection_string or build_odbc_connection_string()
    logger.info("Connecting to Azure SQL…")
    return pyodbc.connect(conn_str, timeout=30)


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return number


def fcc_coordinates(row: dict[str, Any]) -> tuple[float, float] | None:
    """Prefer decimal lat/lng; fall back to calculated columns."""
    lat = _to_float(row.get("Latitude_Decimal"))
    if lat is None:
        lat = _to_float(row.get("Latitude_Calculated"))
    lng = _to_float(row.get("Longitude_Decimal"))
    if lng is None:
        lng = _to_float(row.get("Longitude_Calculated"))
    if lat is None or lng is None:
        return None
    return lat, lng


def towersource_coordinates(row: dict[str, Any]) -> tuple[float, float] | None:
    lat = _to_float(row.get("latitude"))
    lng = _to_float(row.get("longitude"))
    if lat is None or lng is None:
        return None
    return lat, lng


def _bbox(lat: float, lng: float, buffer_deg: float = BBOX_BUFFER_DEG) -> tuple[float, float, float, float]:
    return lat - buffer_deg, lat + buffer_deg, lng - buffer_deg, lng + buffer_deg


def _row_dict(cursor, row) -> dict[str, Any]:
    columns = [col[0] for col in cursor.description]
    return dict(zip(columns, row))


def _fetch_fcc_candidates(cursor, lat: float, lng: float) -> list[dict[str, Any]]:
    min_lat, max_lat, min_lng, max_lng = _bbox(lat, lng)
    sql = f"""
        SELECT ID, ASR_Number, Latitude_Decimal, Longitude_Decimal,
               Latitude_Calculated, Longitude_Calculated,
               Registration_Type, Record_Type, Entity_Name
        FROM {FCC_TABLE}
        WHERE (
                (Latitude_Decimal BETWEEN ? AND ? AND Longitude_Decimal BETWEEN ? AND ?)
             OR (Latitude_Calculated BETWEEN ? AND ? AND Longitude_Calculated BETWEEN ? AND ?)
              )
    """
    cursor.execute(
        sql,
        min_lat, max_lat, min_lng, max_lng,
        min_lat, max_lat, min_lng, max_lng,
    )
    return [_row_dict(cursor, row) for row in cursor.fetchall()]


def _fetch_towersource_candidates(cursor, lat: float, lng: float) -> list[dict[str, Any]]:
    min_lat, max_lat, min_lng, max_lng = _bbox(lat, lng)
    sql = f"""
        SELECT operator_site_identifier, asset_name, asset_type, asset_category,
               latitude, longitude, fcc_asr_number, street1, city, state, postal_code
        FROM {TOWERSOURCE_TABLE}
        WHERE latitude BETWEEN ? AND ?
          AND longitude BETWEEN ? AND ?
    """
    cursor.execute(sql, min_lat, max_lat, min_lng, max_lng)
    return [_row_dict(cursor, row) for row in cursor.fetchall()]


def _nearest_from_rows(
    rows: Iterable[dict[str, Any]],
    *,
    lat: float,
    lng: float,
    source: str,
    coord_fn,
    id_keys: Sequence[str],
    asr_keys: Sequence[str],
    asset_type_keys: Sequence[str],
    max_m: float,
) -> ProximityHit | None:
    best: ProximityHit | None = None
    for row in rows:
        coords = coord_fn(row)
        if coords is None:
            continue
        hit_lat, hit_lng = coords
        distance = haversine_meters(lat, lng, hit_lat, hit_lng)
        if distance > max_m:
            continue
        record_id = None
        for key in id_keys:
            if row.get(key) not in (None, ""):
                record_id = str(row.get(key))
                break
        asr_number = None
        for key in asr_keys:
            if row.get(key) not in (None, ""):
                asr_number = str(row.get(key))
                break
        asset_type = None
        for key in asset_type_keys:
            if row.get(key) not in (None, ""):
                asset_type = str(row.get(key))
                break
        candidate = ProximityHit(
            source=source,
            distance_m=distance,
            latitude=hit_lat,
            longitude=hit_lng,
            record_id=record_id,
            asr_number=asr_number,
            asset_type=asset_type,
            raw=row,
        )
        if best is None or candidate.distance_m < best.distance_m:
            best = candidate
        elif (
            best is not None
            and abs(candidate.distance_m - best.distance_m) < 1e-9
            and source == MATCH_SOURCE_FCC
        ):
            # FCC wins equal-distance ties.
            best = candidate
    return best


def find_proximity_hit(
    cursor,
    lat: float,
    lng: float,
    *,
    max_m: float = PROXIMITY_MAX_M,
) -> ProximityHit | None:
    """Return nearest FCC or TowerSource hit within max_m, preferring FCC on ties."""
    fcc_rows = _fetch_fcc_candidates(cursor, lat, lng)
    ts_rows = _fetch_towersource_candidates(cursor, lat, lng)

    fcc_hit = _nearest_from_rows(
        fcc_rows,
        lat=lat,
        lng=lng,
        source=MATCH_SOURCE_FCC,
        coord_fn=fcc_coordinates,
        id_keys=("ID",),
        asr_keys=("ASR_Number",),
        asset_type_keys=("Registration_Type", "Record_Type"),
        max_m=max_m,
    )
    ts_hit = _nearest_from_rows(
        ts_rows,
        lat=lat,
        lng=lng,
        source=MATCH_SOURCE_TOWERSOURCE,
        coord_fn=towersource_coordinates,
        id_keys=("operator_site_identifier", "asset_name"),
        asr_keys=("fcc_asr_number",),
        asset_type_keys=("asset_type", "asset_category"),
        max_m=max_m,
    )

    if fcc_hit is None:
        return ts_hit
    if ts_hit is None:
        return fcc_hit
    if abs(fcc_hit.distance_m - ts_hit.distance_m) < 1e-9:
        return fcc_hit
    return fcc_hit if fcc_hit.distance_m <= ts_hit.distance_m else ts_hit


def describe_match(hit: ProximityHit | None) -> str:
    if hit is None:
        return MATCH_SOURCE_NONE
    return hit.source
