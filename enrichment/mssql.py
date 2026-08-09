"""Azure SQL / MSSQL helpers for FCC and TowerSource proximity search."""

from __future__ import annotations

import logging
import os
import struct
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


# Browser / device-code flows â€” blocked unless AZURE_SQL_ALLOW_INTERACTIVE=1.
_INTERACTIVE_AUTH_MODES = {
    "activedirectoryinteractive",
    "activedirectorydevicecode",
}

# Values the ODBC driver itself understands. "ActiveDirectoryDefault" is a
# .NET SqlClient concept and is rejected by the driver, so it maps to the
# access-token path below instead.
_ODBC_AUTH_MODES = {
    "sqlpassword",
    "activedirectorypassword",
    "activedirectoryintegrated",
    "activedirectoryinteractive",
    "activedirectorydevicecode",
    "activedirectoryserviceprincipal",
    "activedirectorymsi",
    "activedirectorymanagedidentity",
}

# Auth handled by azure-identity: acquire a token and hand it to the driver.
_TOKEN_AUTH_MODES = {"", "accesstoken", "default", "activedirectorydefault"}

SQL_COPT_SS_ACCESS_TOKEN = 1256
AZURE_SQL_SCOPE = "https://database.windows.net/.default"


def resolve_authentication(authentication: str | None = None) -> str:
    """Normalize the configured auth mode; '' means access-token auth."""
    resolved = (
        authentication
        if authentication is not None
        else os.environ.get("AZURE_SQL_ODBC_AUTHENTICATION", "")
    ).strip()

    if resolved.lower() in _TOKEN_AUTH_MODES:
        return ""

    if resolved.lower() not in _ODBC_AUTH_MODES:
        raise ValueError(
            f"Unsupported AZURE_SQL_ODBC_AUTHENTICATION={resolved!r}. "
            "Leave it blank for non-interactive token auth (az login / managed "
            "identity), or use one of: "
            "ActiveDirectoryServicePrincipal, ActiveDirectoryIntegrated, "
            "ActiveDirectoryPassword, SqlPassword."
        )

    allow_interactive = (
        os.environ.get("AZURE_SQL_ALLOW_INTERACTIVE", "").strip().lower()
        in {"1", "true", "yes"}
    )
    if resolved.lower() in _INTERACTIVE_AUTH_MODES and not allow_interactive:
        raise ValueError(
            f"Refusing interactive SQL auth ({resolved}). "
            "Leave AZURE_SQL_ODBC_AUTHENTICATION blank for token auth, or use "
            "ActiveDirectoryServicePrincipal with AZURE_SQL_UID/PWD. "
            "Set AZURE_SQL_ALLOW_INTERACTIVE=1 only if you intentionally want a browser prompt."
        )
    return resolved


def build_odbc_connection_string(
    *,
    server: str | None = None,
    database: str | None = None,
    driver: str | None = None,
    authentication: str | None = None,
    uid: str | None = None,
    pwd: str | None = None,
) -> str:
    """Build an ODBC connection string from env or explicit overrides.

    With no explicit auth mode the string carries no credentials; callers pair it
    with an Entra access token (see connect_mssql), which works non-interactively
    from `az login`, environment credentials, or a managed identity.
    """
    server = (server or os.environ.get("AZURE_SQL_SERVER") or "").strip()
    database = (database or os.environ.get("AZURE_SQL_DATABASE") or "").strip()
    driver = (
        driver
        or os.environ.get("AZURE_SQL_DRIVER")
        or "ODBC Driver 18 for SQL Server"
    ).strip()
    authentication = resolve_authentication(authentication)
    uid = uid if uid is not None else os.environ.get("AZURE_SQL_UID", "").strip()
    pwd = pwd if pwd is not None else os.environ.get("AZURE_SQL_PWD", "").strip()

    if not server or not database:
        raise ValueError(
            "AZURE_SQL_SERVER and AZURE_SQL_DATABASE must be set in the environment"
        )

    if authentication.lower() == "activedirectoryserviceprincipal" and (
        not uid or not pwd
    ):
        raise ValueError(
            "ActiveDirectoryServicePrincipal requires AZURE_SQL_UID (app id) "
            "and AZURE_SQL_PWD (client secret)"
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


def _access_token_struct() -> bytes:
    """Fetch an Entra token for Azure SQL in the packed form ODBC expects."""
    try:
        from azure.identity import DefaultAzureCredential
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "azure-identity is required for non-interactive Azure SQL auth. "
            "Install with: pip install azure-identity"
        ) from exc

    credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
    token = credential.get_token(AZURE_SQL_SCOPE).token
    encoded = token.encode("utf-16-le")
    return struct.pack("<I", len(encoded)) + encoded


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
    use_token = connection_string is None and not resolve_authentication()
    logger.info("Connecting to Azure SQLâ€¦")
    if use_token:
        return pyodbc.connect(
            conn_str,
            timeout=30,
            attrs_before={SQL_COPT_SS_ACCESS_TOKEN: _access_token_struct()},
        )
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
