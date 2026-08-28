"""Write enrichment metrics to Azure SQL (fail-open from the pipeline)."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Iterable

from enrichment.metrics import rec_true
from enrichment.metrics_ddl import DDL_STATEMENTS

logger = logging.getLogger(__name__)


def metrics_sql_enabled() -> bool:
    flag = os.environ.get("METRICS_SQL", "1").strip().lower()
    if flag in {"0", "false", "no"}:
        return False
    return bool((os.environ.get("AZURE_SQL_SERVER") or "").strip())


def _bit(value: Any) -> int:
    return 1 if rec_true(value) else 0


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _dec(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _naive_utc(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return _naive_utc(value)
    text = str(value or "").strip()
    if text:
        try:
            return datetime.strptime(text.replace("Z", ""), "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            pass
    return _naive_utc(datetime.now(timezone.utc))


def run_row(run: dict[str, Any]) -> tuple[Any, ...]:
    return (
        str(run.get("run_id") or "")[:80],
        _dt(run.get("recorded_at")),
        _int(run.get("sites")),
        _int(run.get("applied_rooftop")),
        _int(run.get("applied_tower")),
        _int(run.get("applied_db_skip")),
        _int(run.get("holdout_empty_confirmed")),
        _int(run.get("holdout_weak_rooftop")),
        _int(run.get("holdout_weak_tower")),
        _int(run.get("holdout_empty")),
        _int(run.get("holdout_no_nearmap")),
        _int(run.get("errors")),
        _int(run.get("nearmap_sites")),
        _int(run.get("claude_sites")),
        _int(run.get("naip_empty_to_nearmap")),
        _int(run.get("naip_empty_to_rooftop")),
        _int(run.get("naip_empty_to_rooftop_apply")),
        _dec(run.get("empty_to_rooftop_apply_rate")),
        _int(run.get("sf_writes")),
        _int(run.get("sf_holdouts_dequeued")),
        _int(run.get("sf_write_failed")),
        (str(run.get("notes") or "") or None),
    )


def site_row(site: dict[str, Any]) -> tuple[Any, ...]:
    return (
        str(site.get("run_id") or "")[:80],
        str(site.get("Id") or site.get("SalesforceId") or "")[:18],
        (str(site.get("address") or "") or None),
        (str(site.get("screen_site_type") or "") or None),
        (str(site.get("final_site_type") or "") or None),
        _dec(site.get("final_confidence")),
        _bit(site.get("nearmap_ran")),
        (str(site.get("nearmap_tier") or "") or None),
        _bit(site.get("claude_ran")),
        (str(site.get("escalation_reason") or "") or None),
        (str(site.get("second_nearmap") or "") or None),
        _bit(site.get("empty_to_nearmap")),
        _bit(site.get("empty_to_rooftop")),
        _bit(site.get("empty_to_rooftop_apply")),
        (str(site.get("bucket") or "") or None),
        (str(site.get("holdout_reason") or "") or None),
        (str(site.get("update_site_type") or "") or None),
        str(site.get("outcome") or "holdout_other")[:64],
        (str(site.get("sf_update_status") or "") or None),
        (str(site.get("notes") or "") or None),
    )


_UPSERT_RUN = """
MERGE dbo.EnrichmentRun AS t
USING (SELECT
    ? AS RunId, ? AS RecordedAt, ? AS Sites, ? AS AppliedRooftop, ? AS AppliedTower,
    ? AS AppliedDbSkip, ? AS HoldoutEmptyConfirmed, ? AS HoldoutWeakRooftop,
    ? AS HoldoutWeakTower, ? AS HoldoutEmpty, ? AS HoldoutNoNearmap, ? AS Errors,
    ? AS NearmapSites, ? AS ClaudeSites, ? AS NaipEmptyToNearmap,
    ? AS NaipEmptyToRooftop, ? AS NaipEmptyToRooftopApply, ? AS EmptyToRooftopApplyRate,
    ? AS SfWrites, ? AS SfHoldoutsDequeued, ? AS SfWriteFailed, ? AS Notes
) AS s
ON t.RunId = s.RunId
WHEN MATCHED THEN UPDATE SET
    RecordedAt = s.RecordedAt, Sites = s.Sites, AppliedRooftop = s.AppliedRooftop,
    AppliedTower = s.AppliedTower, AppliedDbSkip = s.AppliedDbSkip,
    HoldoutEmptyConfirmed = s.HoldoutEmptyConfirmed,
    HoldoutWeakRooftop = s.HoldoutWeakRooftop, HoldoutWeakTower = s.HoldoutWeakTower,
    HoldoutEmpty = s.HoldoutEmpty, HoldoutNoNearmap = s.HoldoutNoNearmap,
    Errors = s.Errors, NearmapSites = s.NearmapSites, ClaudeSites = s.ClaudeSites,
    NaipEmptyToNearmap = s.NaipEmptyToNearmap, NaipEmptyToRooftop = s.NaipEmptyToRooftop,
    NaipEmptyToRooftopApply = s.NaipEmptyToRooftopApply,
    EmptyToRooftopApplyRate = s.EmptyToRooftopApplyRate, SfWrites = s.SfWrites,
    SfHoldoutsDequeued = s.SfHoldoutsDequeued, SfWriteFailed = s.SfWriteFailed,
    Notes = s.Notes
WHEN NOT MATCHED THEN INSERT (
    RunId, RecordedAt, Sites, AppliedRooftop, AppliedTower, AppliedDbSkip,
    HoldoutEmptyConfirmed, HoldoutWeakRooftop, HoldoutWeakTower, HoldoutEmpty,
    HoldoutNoNearmap, Errors, NearmapSites, ClaudeSites, NaipEmptyToNearmap,
    NaipEmptyToRooftop, NaipEmptyToRooftopApply, EmptyToRooftopApplyRate,
    SfWrites, SfHoldoutsDequeued, SfWriteFailed, Notes
) VALUES (
    s.RunId, s.RecordedAt, s.Sites, s.AppliedRooftop, s.AppliedTower, s.AppliedDbSkip,
    s.HoldoutEmptyConfirmed, s.HoldoutWeakRooftop, s.HoldoutWeakTower, s.HoldoutEmpty,
    s.HoldoutNoNearmap, s.Errors, s.NearmapSites, s.ClaudeSites, s.NaipEmptyToNearmap,
    s.NaipEmptyToRooftop, s.NaipEmptyToRooftopApply, s.EmptyToRooftopApplyRate,
    s.SfWrites, s.SfHoldoutsDequeued, s.SfWriteFailed, s.Notes
);
"""

_INSERT_SITE = """
INSERT INTO dbo.EnrichmentSiteOutcome (
    RunId, SalesforceId, Address, ScreenSiteType, FinalSiteType, FinalConfidence,
    NearmapRan, NearmapTier, ClaudeRan, EscalationReason, SecondNearmap,
    EmptyToNearmap, EmptyToRooftop, EmptyToRooftopApply, Bucket, HoldoutReason,
    UpdateSiteType, Outcome, SfUpdateStatus, Notes
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def ensure_tables(cursor) -> None:
    for stmt in DDL_STATEMENTS:
        cursor.execute(stmt)


def upsert_snapshot(cursor, snap: dict[str, Any]) -> int:
    """Replace one run header + its site rows. Returns site count written."""
    run_id = str(snap.get("run_id") or "")
    if not run_id:
        raise ValueError("snapshot missing run_id")
    sites: Iterable[dict[str, Any]] = snap.get("site_records") or []
    site_list = [s for s in sites if str(s.get("Id") or s.get("SalesforceId") or "")]
    cursor.execute("DELETE FROM dbo.EnrichmentSiteOutcome WHERE RunId = ?", run_id)
    cursor.execute(_UPSERT_RUN, run_row(snap))
    for rec in site_list:
        row = site_row({**rec, "run_id": rec.get("run_id") or run_id})
        cursor.execute(_INSERT_SITE, row)
    return len(site_list)


def write_snapshot(snap: dict[str, Any]) -> int:
    """Open a connection, ensure tables, upsert. Raises on SQL failure."""
    from enrichment.mssql import connect_mssql

    conn = connect_mssql()
    try:
        cursor = conn.cursor()
        ensure_tables(cursor)
        n = upsert_snapshot(cursor, snap)
        conn.commit()
        return n
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def try_write_snapshot(snap: dict[str, Any]) -> None:
    """Pipeline hook: skip when SQL is off; log and continue on failure."""
    if not metrics_sql_enabled():
        return
    try:
        n = write_snapshot(snap)
        logger.info("metrics SQL upsert run_id=%s sites=%s", snap.get("run_id"), n)
    except Exception:
        logger.exception("metrics SQL upsert skipped")
