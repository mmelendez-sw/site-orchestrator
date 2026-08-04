"""Snap net-new geocode coords to nearby FCC / TowerSource tower points."""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_ORCHESTRATOR_PROXIMITY_MAX_M = 25.0


def proximity_max_m() -> float:
    raw = os.environ.get("ORCHESTRATOR_PROXIMITY_MAX_M", "").strip()
    if not raw:
        return DEFAULT_ORCHESTRATOR_PROXIMITY_MAX_M
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "Invalid ORCHESTRATOR_PROXIMITY_MAX_M=%r — using %s m",
            raw,
            DEFAULT_ORCHESTRATOR_PROXIMITY_MAX_M,
        )
        return DEFAULT_ORCHESTRATOR_PROXIMITY_MAX_M
    return max(0.0, value)


def apply_tower_db_snap(
    classify_targets: list[tuple[int, dict[str, Any]]],
    result_rows: list[dict[str, Any]],
    *,
    max_m: float | None = None,
    sql_connection=None,
    verbose: bool = False,
) -> dict[str, int]:
    """After Salesforce dedupe, snap net-new lat/lng to FCC/TowerSource within max_m.

    Mutates canonical records and matching result_rows in place so classify and
    Salesforce upload use the cleaned coordinates. On SQL failure, logs and
    leaves original geocode coords unchanged.
    """
    from enrichment.mssql import connect_mssql, find_proximity_hit

    radius = proximity_max_m() if max_m is None else max_m
    stats = {"checked": 0, "snapped": 0, "no_hit": 0, "errors": 0}

    if not classify_targets or radius <= 0:
        return stats

    own_sql = False
    connection = sql_connection
    if connection is None:
        try:
            connection = connect_mssql()
            own_sql = True
        except Exception as exc:  # noqa: BLE001 — keep classify running
            logger.warning(
                "Tower DB snap skipped — could not connect to Azure SQL: %s",
                exc,
            )
            stats["errors"] = len(classify_targets)
            return stats

    logger.info(
        "TOWER SNAP — checking %d net-new site(s) vs FCC/TowerSource within %.0f m",
        len(classify_targets),
        radius,
    )

    try:
        cursor = connection.cursor()
        for index, canonical in classify_targets:
            stats["checked"] += 1
            try:
                lat = float(canonical["lat"])
                lng = float(canonical["lng"])
            except (KeyError, TypeError, ValueError):
                stats["errors"] += 1
                logger.warning(
                    "Tower snap skipped — missing lat/lng for %s",
                    (canonical.get("address") or "")[:80],
                )
                continue

            try:
                hit = find_proximity_hit(cursor, lat, lng, max_m=radius)
            except Exception as exc:  # noqa: BLE001
                stats["errors"] += 1
                logger.warning(
                    "Tower snap query failed for %s: %s",
                    (canonical.get("address") or "")[:80],
                    exc,
                )
                continue

            if hit is None:
                stats["no_hit"] += 1
                canonical["tower_snap_source"] = ""
                canonical["tower_snap_distance_m"] = None
                if index < len(result_rows):
                    result_rows[index]["tower_snap_source"] = ""
                    result_rows[index]["tower_snap_distance_m"] = None
                continue

            canonical["geocode_lat"] = lat
            canonical["geocode_lng"] = lng
            canonical["lat"] = hit.latitude
            canonical["lng"] = hit.longitude
            if "lon" in canonical:
                canonical["lon"] = hit.longitude
            canonical["tower_snap_source"] = hit.source
            canonical["tower_snap_distance_m"] = round(hit.distance_m, 2)
            canonical["tower_snap_asr"] = hit.asr_number or ""
            canonical["tower_snap_record_id"] = hit.record_id or ""

            if index < len(result_rows):
                row = result_rows[index]
                row["geocode_lat"] = lat
                row["geocode_lng"] = lng
                row["lat"] = hit.latitude
                row["lng"] = hit.longitude
                row["tower_snap_source"] = hit.source
                row["tower_snap_distance_m"] = round(hit.distance_m, 2)
                row["tower_snap_asr"] = hit.asr_number or ""
                row["tower_snap_record_id"] = hit.record_id or ""

            stats["snapped"] += 1
            message = (
                f"snapped {hit.distance_m:.1f} m → {hit.source}"
                f" ({hit.latitude:.6f}, {hit.longitude:.6f})"
            )
            if verbose:
                logger.info(
                    "  [%d] %s — %s",
                    index + 1,
                    (canonical.get("address") or "")[:72],
                    message,
                )
            else:
                logger.info(
                    "Tower snap [%d/%d] %s — %s",
                    stats["checked"],
                    len(classify_targets),
                    (canonical.get("address") or "")[:60],
                    message,
                )
    finally:
        if own_sql and connection is not None:
            try:
                connection.close()
            except Exception:  # noqa: BLE001
                pass

    logger.info(
        "TOWER SNAP — snapped=%d  no_hit=%d  errors=%d  checked=%d",
        stats["snapped"],
        stats["no_hit"],
        stats["errors"],
        stats["checked"],
    )
    return stats
