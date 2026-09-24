"""Azure SQL / MSSQL helpers for FCC and TowerSource proximity search."""

from __future__ import annotations

import logging
import math
import os
import struct
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Sequence

from enrichment.coerce import to_float as _to_float
from enrichment.geo import haversine_meters
from envutil import env_int

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
_AZ_SQL_RESOURCE = "https://database.windows.net/"
_AZ_CMD_CANDIDATE = Path(r"C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.cmd")

# (token, unix expiry). Access tokens last ~1h; a long enrichment run must refresh.
_cached_sql_token: tuple[str, float] | None = None
_TOKEN_REFRESH_SKEW_S = 300.0
_TOKEN_FALLBACK_TTL_S = 3300.0


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


def _token_via_az_cmd() -> str | None:
    """Windows: azure-identity cannot spawn az.cmd; call it through cmd.exe."""
    import json
    import shutil
    import subprocess
    import sys

    if sys.platform != "win32":
        return None
    az = shutil.which("az") or shutil.which("az.cmd")
    if not az and _AZ_CMD_CANDIDATE.is_file():
        az = str(_AZ_CMD_CANDIDATE)
    if not az:
        return None
    quoted = f'"{az}"' if " " in az else az
    cmdline = (
        f"{quoted} account get-access-token "
        f"--resource {_AZ_SQL_RESOURCE} -o json"
    )
    try:
        proc = subprocess.run(
            cmdline,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            shell=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    parsed = _parse_az_token_payload(data)
    if parsed is None:
        return None
    token, expires_at = parsed
    _store_sql_token(token, expires_at)
    return token


def _parse_az_token_payload(
    data: dict[str, Any] | None, *, now: float | None = None
) -> tuple[str, float] | None:
    """Return (access_token, unix_expiry) from `az account get-access-token` JSON."""
    if not isinstance(data, dict):
        return None
    token = str(data.get("accessToken") or "").strip()
    if not token:
        return None
    clock = time.time() if now is None else now
    raw = data.get("expires_on")
    if raw is None:
        raw = data.get("expiresOn")
    expires_at: float | None = None
    if isinstance(raw, (int, float)):
        expires_at = float(raw)
    elif isinstance(raw, str) and raw.strip():
        text = raw.strip()
        if text.isdigit():
            expires_at = float(text)
        else:
            from datetime import datetime

            try:
                expires_at = datetime.strptime(
                    text.split(".")[0], "%Y-%m-%d %H:%M:%S"
                ).timestamp()
            except ValueError:
                expires_at = None
    if expires_at is None:
        expires_at = clock + _TOKEN_FALLBACK_TTL_S
    return token, expires_at


def _cached_token_if_fresh(*, now: float | None = None) -> str | None:
    global _cached_sql_token
    if not _cached_sql_token:
        return None
    token, expires_at = _cached_sql_token
    clock = time.time() if now is None else now
    if clock + _TOKEN_REFRESH_SKEW_S >= expires_at:
        _cached_sql_token = None
        return None
    return token


def _store_sql_token(token: str, expires_at: float) -> None:
    global _cached_sql_token
    _cached_sql_token = (token, float(expires_at))


def _invalidate_sql_token() -> None:
    global _cached_sql_token
    _cached_sql_token = None


def _is_expired_sql_login(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "token is expired" in text or (
        "18456" in text and "token" in text
    )


def is_sql_link_failure(exc: BaseException) -> bool:
    """True when the ODBC session is dead and a new connection can recover it."""
    chunks = [str(exc)]
    chunks.extend(str(arg) for arg in getattr(exc, "args", ()))
    text = " ".join(chunks).lower()
    needles = (
        "08s01",
        "08001",
        "hy010",
        "communication link failure",
        "invalid cursor state",
        "physical connection is not usable",
        "connection is closed",
        "tcp provider",
    )
    return any(needle in text for needle in needles)


def reconnect_mssql(sql_state: dict[str, Any]) -> None:
    """Close a dead Azure SQL session and replace ``conn`` / ``cursor`` in place."""
    old = sql_state.get("conn")
    if old is not None:
        try:
            old.close()
        except Exception:
            pass
    sql_state["conn"] = None
    sql_state["cursor"] = None
    conn = connect_mssql()
    sql_state["conn"] = conn
    sql_state["cursor"] = conn.cursor()


def _entra_sql_token() -> str:
    """Entra token for Azure SQL. Prefer az.cmd on Windows; refresh before expiry."""
    cached = _cached_token_if_fresh()
    if cached:
        return cached

    token = _token_via_az_cmd()
    if token:
        return token

    try:
        from azure.identity import DefaultAzureCredential
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "azure-identity is required for non-interactive Azure SQL auth. "
            "Install with: pip install azure-identity"
        ) from exc

    # AzureCliCredential looks for `az.exe` and dumps a WARNING on Windows.
    credential = DefaultAzureCredential(
        exclude_interactive_browser_credential=True,
        exclude_azure_cli_credential=True,
    )
    issued = credential.get_token(AZURE_SQL_SCOPE)
    _store_sql_token(issued.token, float(issued.expires_on))
    return issued.token


def _access_token_struct() -> bytes:
    """Fetch an Entra token for Azure SQL in the packed form ODBC expects."""
    encoded = _entra_sql_token().encode("utf-16-le")
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
    if not use_token:
        return pyodbc.connect(conn_str, timeout=30)
    try:
        return pyodbc.connect(
            conn_str,
            timeout=30,
            attrs_before={SQL_COPT_SS_ACCESS_TOKEN: _access_token_struct()},
        )
    except Exception as exc:
        if not _is_expired_sql_login(exc):
            raise
        logger.warning("Azure SQL token expired; refreshing and retrying once")
        _invalidate_sql_token()
        return pyodbc.connect(
            conn_str,
            timeout=30,
            attrs_before={SQL_COPT_SS_ACCESS_TOKEN: _access_token_struct()},
        )


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


_FCC_COLUMNS = (
    "ID, ASR_Number, Latitude_Decimal, Longitude_Decimal, "
    "Latitude_Calculated, Longitude_Calculated, "
    "Registration_Type, Record_Type, Entity_Name"
)
_TS_COLUMNS = (
    "operator_site_identifier, asset_name, asset_type, asset_category, "
    "latitude, longitude, fcc_asr_number, street1, city, state, postal_code"
)
_FCC_BBOX = (
    "((f.Latitude_Decimal BETWEEN {a} AND {b} AND f.Longitude_Decimal BETWEEN {c} AND {d})"
    " OR (f.Latitude_Calculated BETWEEN {a} AND {b} AND f.Longitude_Calculated BETWEEN {c} AND {d}))"
)
_TS_BBOX = "(t.latitude BETWEEN {a} AND {b} AND t.longitude BETWEEN {c} AND {d})"


def _prefixed(columns: str, alias: str) -> str:
    return ", ".join(f"{alias}.{col.strip()}" for col in columns.split(","))


def _fetch_fcc_candidates(
    cursor, lat: float, lng: float, *, max_m: float = PROXIMITY_MAX_M
) -> list[dict[str, Any]]:
    min_lat, max_lat, min_lng, max_lng = _bbox(lat, lng, max_m=max_m)
    where = _FCC_BBOX.format(a="?", b="?", c="?", d="?")
    cursor.execute(
        f"SELECT {_prefixed(_FCC_COLUMNS, 'f')} FROM {FCC_TABLE} AS f WHERE {where}",
        min_lat, max_lat, min_lng, max_lng,
        min_lat, max_lat, min_lng, max_lng,
    )
    return [_row_dict(cursor, row) for row in cursor.fetchall()]


def _fetch_towersource_candidates(
    cursor, lat: float, lng: float, *, max_m: float = PROXIMITY_MAX_M
) -> list[dict[str, Any]]:
    min_lat, max_lat, min_lng, max_lng = _bbox(lat, lng, max_m=max_m)
    where = _TS_BBOX.format(a="?", b="?", c="?", d="?")
    cursor.execute(
        f"SELECT {_prefixed(_TS_COLUMNS, 't')} FROM {TOWERSOURCE_TABLE} AS t WHERE {where}",
        min_lat, max_lat, min_lng, max_lng,
    )
    return [_row_dict(cursor, row) for row in cursor.fetchall()]


def _first_value(row: dict[str, Any], keys: Sequence[str]) -> str | None:
    for key in keys:
        if row.get(key) not in (None, ""):
            return str(row.get(key))
    return None


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
        # distance_m = primary sort key: nearer of pin/address distances.
        primary = d_pin if d_addr is None else min(d_pin, d_addr)
        hits.append(
            ProximityHit(
                source=source,
                distance_m=primary,
                latitude=hit_lat,
                longitude=hit_lng,
                record_id=_first_value(row, id_keys),
                asr_number=_first_value(row, asr_keys),
                asset_type=_first_value(row, asset_type_keys),
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


def _pin_distance(hit: ProximityHit) -> float:
    return hit.distance_to_pin_m if hit.distance_to_pin_m is not None else hit.distance_m


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

    def _pick(best: ProximityHit, reason: str, gap: float | None) -> ProximityHit:
        return replace(
            best,
            selection_reason=reason,
            candidate_count=n,
            runner_up_gap_m=None if gap is None else round(gap, 1),
        )

    # 1) Confident: within confident_m of pin.
    by_pin = sorted(
        cands,
        key=lambda h: (_pin_distance(h), 0 if h.source == MATCH_SOURCE_FCC else 1),
    )
    confident_pin = [h for h in by_pin if _pin_distance(h) <= confident_m]
    if confident_pin:
        best = confident_pin[0]
        return _pick(best, "confident_pin", _gap(best, _pin_distance))

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
            return _pick(best, "address_affinity", gap)
        # Ambiguous near address — fall through to unique-nearest on pin.

    # 3) Extended unique nearest to pin (must clear ambiguity gap).
    best = by_pin[0]
    gap = _gap(best, _pin_distance)
    if gap is None or gap >= ambiguity_gap_m:
        return _pick(best, "unique_nearest_extended", gap)

    # Clustered neighbors at extended range — do not auto-pick.
    return None


def _address_differs(lat, lng, address_lat, address_lng) -> bool:
    return (
        address_lat is not None
        and address_lng is not None
        and (abs(address_lat - lat) > 1e-5 or abs(address_lng - lng) > 1e-5)
    )


def select_from_candidate_rows(
    fcc_rows: Iterable[dict[str, Any]],
    ts_rows: Iterable[dict[str, Any]],
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
    """Turn raw FCC/TowerSource rows near a pin (and address) into one hit."""
    common = dict(
        pin_lat=lat,
        pin_lng=lng,
        max_m=max_m,
        address_lat=address_lat,
        address_lng=address_lng,
    )
    hits = _hits_from_rows(
        fcc_rows,
        source=MATCH_SOURCE_FCC,
        coord_fn=fcc_coordinates,
        id_keys=("ID",),
        asr_keys=("ASR_Number",),
        asset_type_keys=("Registration_Type", "Record_Type"),
        **common,
    ) + _hits_from_rows(
        ts_rows,
        source=MATCH_SOURCE_TOWERSOURCE,
        coord_fn=towersource_coordinates,
        id_keys=("operator_site_identifier", "asset_name"),
        asr_keys=("fcc_asr_number",),
        asset_type_keys=("asset_type", "asset_category"),
        **common,
    )
    return select_proximity_hit(
        hits,
        confident_m=confident_m,
        ambiguity_gap_m=ambiguity_gap_m,
        address_affinity_m=address_affinity_m,
    )


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
    if _address_differs(lat, lng, address_lat, address_lng):
        fcc_rows = list(fcc_rows) + _fetch_fcc_candidates(
            cursor, address_lat, address_lng, max_m=max_m
        )
        ts_rows = list(ts_rows) + _fetch_towersource_candidates(
            cursor, address_lat, address_lng, max_m=max_m
        )
    return select_from_candidate_rows(
        fcc_rows,
        ts_rows,
        lat,
        lng,
        max_m=max_m,
        address_lat=address_lat,
        address_lng=address_lng,
        confident_m=confident_m,
        ambiguity_gap_m=ambiguity_gap_m,
        address_affinity_m=address_affinity_m,
    )


# ------------------------------ bulk proximity ------------------------------


@dataclass(frozen=True)
class ProximityQuery:
    """One site's lookup: the SF pin plus an optional geocoded address."""

    key: str
    lat: float
    lng: float
    address_lat: float | None = None
    address_lng: float | None = None


PROXIMITY_BULK_CHUNK = max(1, env_int("PROXIMITY_BULK_CHUNK", 200))
_BULK_POINTS = "#enrichment_prox_points"


def _bulk_join_sql(columns: str, alias: str, table: str, bbox: str) -> str:
    where = bbox.format(a="p.min_lat", b="p.max_lat", c="p.min_lng", d="p.max_lng")
    return (
        f"SELECT p.query_key AS query_key, {_prefixed(columns, alias)} "
        f"FROM {_BULK_POINTS} AS p JOIN {table} AS {alias} ON {where}"
    )


def _bulk_points(queries: Sequence[ProximityQuery], max_m: float) -> list[tuple]:
    points: list[tuple] = []
    for q in queries:
        centers = [(q.lat, q.lng)]
        if _address_differs(q.lat, q.lng, q.address_lat, q.address_lng):
            centers.append((q.address_lat, q.address_lng))
        for lat, lng in centers:
            min_lat, max_lat, min_lng, max_lng = _bbox(lat, lng, max_m=max_m)
            points.append((q.key, min_lat, max_lat, min_lng, max_lng))
    return points


def _grouped_rows(cursor, sql: str) -> dict[str, list[dict[str, Any]]]:
    cursor.execute(sql)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for raw in cursor.fetchall():
        row = _row_dict(cursor, raw)
        grouped.setdefault(str(row.pop("query_key")), []).append(row)
    return grouped


def _bulk_chunk(cursor, queries: Sequence[ProximityQuery], max_m: float):
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
        _bulk_points(queries, max_m),
    )
    fcc = _grouped_rows(cursor, _bulk_join_sql(_FCC_COLUMNS, "f", FCC_TABLE, _FCC_BBOX))
    ts = _grouped_rows(
        cursor, _bulk_join_sql(_TS_COLUMNS, "t", TOWERSOURCE_TABLE, _TS_BBOX)
    )
    cursor.execute(f"DROP TABLE {_BULK_POINTS}")
    return fcc, ts


def find_proximity_hits_bulk(
    cursor,
    queries: Sequence[ProximityQuery],
    *,
    max_m: float = PROXIMITY_MAX_M,
    chunk_size: int = PROXIMITY_BULK_CHUNK,
) -> dict[str, ProximityHit | None]:
    """Same selection as ``find_proximity_hit`` for many sites in few queries.

    Each chunk loads bbox points (pin, plus address when it differs) into a
    session temp table and joins FCC and TowerSource once each, instead of
    two to four queries per site. Raises on SQL errors; callers fall back.
    """
    results: dict[str, ProximityHit | None] = {}
    for start in range(0, len(queries), max(1, int(chunk_size))):
        chunk = list(queries[start : start + chunk_size])
        fcc, ts = _bulk_chunk(cursor, chunk, max_m)
        for q in chunk:
            results[q.key] = select_from_candidate_rows(
                fcc.get(q.key, []),
                ts.get(q.key, []),
                q.lat,
                q.lng,
                max_m=max_m,
                address_lat=q.address_lat,
                address_lng=q.address_lng,
            )
    return results


def describe_match(hit: ProximityHit | None) -> str:
    if hit is None:
        return MATCH_SOURCE_NONE
    return hit.source
