"""Azure SQL / MSSQL helpers for FCC and TowerSource proximity search."""

from __future__ import annotations

import logging
import math
import os
import struct
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from enrichment.geo import haversine_meters

from enrichment.constants import (
    BBOX_BUFFER_DEG,
    FCC_TABLE,
    MATCH_SOURCE_FCC,
    MATCH_SOURCE_NONE,
    MATCH_SOURCE_TOWERSOURCE,
    PROXIMITY_ADDRESS_AFFINITY_M,
    PROXIMITY_AMBIGUITY_GAP_M,
    PROXIMITY_CONFIDENT_M,
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
    # Selection metadata (optional; filled by find_proximity_hit).
    distance_to_pin_m: float | None = None
    distance_to_address_m: float | None = None
    selection_reason: str | None = None
    candidate_count: int | None = None
    runner_up_gap_m: float | None = None


def _buffer_deg_for_radius(max_m: float, lat: float) -> tuple[float, float]:
    """Return (lat_buffer_deg, lng_buffer_deg) covering max_m with margin."""
    radius = max(float(max_m), 25.0) * 1.25
    lat_buf = max(BBOX_BUFFER_DEG, radius / 111_320.0)
    cos_lat = max(0.2, abs(math.cos(math.radians(lat))))
    lng_buf = max(BBOX_BUFFER_DEG, radius / (111_320.0 * cos_lat))
    return lat_buf, lng_buf


def _bbox(
    lat: float,
    lng: float,
    buffer_deg: float | None = None,
    *,
    max_m: float | None = None,
) -> tuple[float, float, float, float]:
    if max_m is not None:
        lat_buf, lng_buf = _buffer_deg_for_radius(max_m, lat)
    else:
        lat_buf = lng_buf = buffer_deg if buffer_deg is not None else BBOX_BUFFER_DEG
    return lat - lat_buf, lat + lat_buf, lng - lng_buf, lng + lng_buf


# Browser / device-code flows — blocked unless AZURE_SQL_ALLOW_INTERACTIVE=1.
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
    logger.info("Connecting to Azure SQL...")
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


def _row_dict(cursor, row) -> dict[str, Any]:
    columns = [col[0] for col in cursor.description]
    return dict(zip(columns, row))


def _fetch_fcc_candidates(
    cursor, lat: float, lng: float, *, max_m: float = PROXIMITY_MAX_M
) -> list[dict[str, Any]]:
    min_lat, max_lat, min_lng, max_lng = _bbox(lat, lng, max_m=max_m)
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


def _fetch_towersource_candidates(
    cursor, lat: float, lng: float, *, max_m: float = PROXIMITY_MAX_M
) -> list[dict[str, Any]]:
    min_lat, max_lat, min_lng, max_lng = _bbox(lat, lng, max_m=max_m)
    sql = f"""
        SELECT operator_site_identifier, asset_name, asset_type, asset_category,
               latitude, longitude, fcc_asr_number, street1, city, state, postal_code
        FROM {TOWERSOURCE_TABLE}
        WHERE latitude BETWEEN ? AND ?
          AND longitude BETWEEN ? AND ?
    """
    cursor.execute(sql, min_lat, max_lat, min_lng, max_lng)
    return [_row_dict(cursor, row) for row in cursor.fetchall()]


def _hits_from_rows(
    rows: Iterable[dict[str, Any]],
    *,
    pin_lat: float,
    pin_lng: float,
    source: str,
    coord_fn,
    id_keys: Sequence[str],
    asr_keys: Sequence[str],
    asset_type_keys: Sequence[str],
    max_m: float,
    address_lat: float | None = None,
    address_lng: float | None = None,
) -> list[ProximityHit]:
    hits: list[ProximityHit] = []
    for row in rows:
        coords = coord_fn(row)
        if coords is None:
            continue
        hit_lat, hit_lng = coords
        d_pin = haversine_meters(pin_lat, pin_lng, hit_lat, hit_lng)
        d_addr = None
        if address_lat is not None and address_lng is not None:
            d_addr = haversine_meters(address_lat, address_lng, hit_lat, hit_lng)
        # Keep if within max_m of pin OR (when address given) within max_m of address.
        if d_pin > max_m and (d_addr is None or d_addr > max_m):
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
        # distance_m = primary sort key: nearer of pin/address distances.
        primary = d_pin if d_addr is None else min(d_pin, d_addr)
        hits.append(
            ProximityHit(
                source=source,
                distance_m=primary,
                latitude=hit_lat,
                longitude=hit_lng,
                record_id=record_id,
                asr_number=asr_number,
                asset_type=asset_type,
                raw=row,
                distance_to_pin_m=d_pin,
                distance_to_address_m=d_addr,
            )
        )
    return hits


def _dedupe_hits(hits: Sequence[ProximityHit]) -> list[ProximityHit]:
    """Keep one row per (source, record_id or rounded lat/lng)."""
    best: dict[str, ProximityHit] = {}
    for hit in hits:
        if hit.record_id:
            key = f"{hit.source}:{hit.record_id}"
        else:
            key = f"{hit.source}:{hit.latitude:.5f},{hit.longitude:.5f}"
        prev = best.get(key)
        if prev is None or hit.distance_m < prev.distance_m:
            best[key] = hit
    return list(best.values())


def _with_meta(
    hit: ProximityHit,
    *,
    reason: str,
    candidate_count: int,
    runner_up_gap_m: float | None,
) -> ProximityHit:
    return ProximityHit(
        source=hit.source,
        distance_m=hit.distance_m,
        latitude=hit.latitude,
        longitude=hit.longitude,
        record_id=hit.record_id,
        asr_number=hit.asr_number,
        asset_type=hit.asset_type,
        raw=hit.raw,
        distance_to_pin_m=hit.distance_to_pin_m,
        distance_to_address_m=hit.distance_to_address_m,
        selection_reason=reason,
        candidate_count=candidate_count,
        runner_up_gap_m=runner_up_gap_m,
    )


def select_proximity_hit(
    hits: Sequence[ProximityHit],
    *,
    confident_m: float = PROXIMITY_CONFIDENT_M,
    ambiguity_gap_m: float = PROXIMITY_AMBIGUITY_GAP_M,
    address_affinity_m: float = PROXIMITY_ADDRESS_AFFINITY_M,
) -> ProximityHit | None:
    """Pick a hit that is confident, address-aligned, or uniquely nearest.

    Rejects extended-range clusters where two towers are nearly tied — that is
    how wrong-neighbor matches happen at 100–500 m.
    """
    if not hits:
        return None
    cands = _dedupe_hits(hits)
    n = len(cands)

    def _gap(best: ProximityHit, key) -> float | None:
        others = [h for h in cands if h is not best]
        if not others:
            return None
        second = min(others, key=key)
        return key(second) - key(best)

    # 1) Confident: within confident_m of pin.
    by_pin = sorted(
        cands,
        key=lambda h: (
            h.distance_to_pin_m if h.distance_to_pin_m is not None else h.distance_m,
            0 if h.source == MATCH_SOURCE_FCC else 1,
        ),
    )
    confident_pin = [
        h
        for h in by_pin
        if (h.distance_to_pin_m if h.distance_to_pin_m is not None else h.distance_m)
        <= confident_m
    ]
    if confident_pin:
        best = confident_pin[0]
        gap = _gap(
            best,
            lambda h: h.distance_to_pin_m
            if h.distance_to_pin_m is not None
            else h.distance_m,
        )
        return _with_meta(
            best,
            reason="confident_pin",
            candidate_count=n,
            runner_up_gap_m=None if gap is None else round(gap, 1),
        )

    # 2) Address affinity: within affinity of geocoded address.
    with_addr = [
        h
        for h in cands
        if h.distance_to_address_m is not None
        and h.distance_to_address_m <= address_affinity_m
    ]
    if with_addr:
        by_addr = sorted(
            with_addr,
            key=lambda h: (
                h.distance_to_address_m or 1e9,
                0 if h.source == MATCH_SOURCE_FCC else 1,
            ),
        )
        best = by_addr[0]
        gap = _gap(best, lambda h: h.distance_to_address_m or 1e9)
        if gap is None or gap >= ambiguity_gap_m:
            return _with_meta(
                best,
                reason="address_affinity",
                candidate_count=n,
                runner_up_gap_m=None if gap is None else round(gap, 1),
            )
        # Ambiguous near address — fall through to unique-nearest on pin.

    # 3) Extended unique nearest to pin (must clear ambiguity gap).
    best = by_pin[0]
    gap = _gap(
        best,
        lambda h: h.distance_to_pin_m
        if h.distance_to_pin_m is not None
        else h.distance_m,
    )
    if gap is None or gap >= ambiguity_gap_m:
        return _with_meta(
            best,
            reason="unique_nearest_extended",
            candidate_count=n,
            runner_up_gap_m=None if gap is None else round(gap, 1),
        )

    # Clustered neighbors at extended range — do not auto-pick.
    return None


def find_proximity_hit(
    cursor,
    lat: float,
    lng: float,
    *,
    max_m: float = PROXIMITY_MAX_M,
    address_lat: float | None = None,
    address_lng: float | None = None,
    confident_m: float = PROXIMITY_CONFIDENT_M,
    ambiguity_gap_m: float = PROXIMITY_AMBIGUITY_GAP_M,
    address_affinity_m: float = PROXIMITY_ADDRESS_AFFINITY_M,
) -> ProximityHit | None:
    """Return best FCC/TowerSource hit within max_m with anti-ambiguity rules.

    Searches a bbox large enough for max_m. Optional geocoded address enables
    address-affinity selection when the SF pin is offset from the street.
    """
    fcc_rows = _fetch_fcc_candidates(cursor, lat, lng, max_m=max_m)
    ts_rows = _fetch_towersource_candidates(cursor, lat, lng, max_m=max_m)
    # Also fetch around the address when it differs from the pin.
    if (
        address_lat is not None
        and address_lng is not None
        and (
            abs(address_lat - lat) > 1e-5
            or abs(address_lng - lng) > 1e-5
        )
    ):
        fcc_rows = list(fcc_rows) + _fetch_fcc_candidates(
            cursor, address_lat, address_lng, max_m=max_m
        )
        ts_rows = list(ts_rows) + _fetch_towersource_candidates(
            cursor, address_lat, address_lng, max_m=max_m
        )

    hits = _hits_from_rows(
        fcc_rows,
        pin_lat=lat,
        pin_lng=lng,
        source=MATCH_SOURCE_FCC,
        coord_fn=fcc_coordinates,
        id_keys=("ID",),
        asr_keys=("ASR_Number",),
        asset_type_keys=("Registration_Type", "Record_Type"),
        max_m=max_m,
        address_lat=address_lat,
        address_lng=address_lng,
    ) + _hits_from_rows(
        ts_rows,
        pin_lat=lat,
        pin_lng=lng,
        source=MATCH_SOURCE_TOWERSOURCE,
        coord_fn=towersource_coordinates,
        id_keys=("operator_site_identifier", "asset_name"),
        asr_keys=("fcc_asr_number",),
        asset_type_keys=("asset_type", "asset_category"),
        max_m=max_m,
        address_lat=address_lat,
        address_lng=address_lng,
    )
    return select_proximity_hit(
        hits,
        confident_m=confident_m,
        ambiguity_gap_m=ambiguity_gap_m,
        address_affinity_m=address_affinity_m,
    )


def describe_match(hit: ProximityHit | None) -> str:
    if hit is None:
        return MATCH_SOURCE_NONE
    return hit.source
