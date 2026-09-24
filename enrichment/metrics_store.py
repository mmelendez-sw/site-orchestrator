"""Write enrichment metrics to Azure SQL (fail-open from the pipeline).

Tables (see sql/enrichment_metrics.sql):
  dbo.EnrichmentRun          one row per run_id (this-run header)
  dbo.EnrichmentSiteOutcome  one row per (run_id, Salesforce Id) processed

Every column is declared once in RUN_COLUMNS / SITE_COLUMNS; the MERGE and
INSERT statements and the row converters are generated from those specs.

Persistence during a run: ``SiteSink`` upserts the run header at start and
each site row as soon as that site finishes, so a crash keeps completed
sites. ``record_run`` then reconciles the whole run at the end.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Sequence

from enrichment.coerce import is_true, text_or_none, to_float, to_int
from enrichment.metrics_ddl import ddl_statements
from envutil import env_flag, env_str

logger = logging.getLogger(__name__)

Converter = Callable[[Any], Any]


def metrics_sql_enabled() -> bool:
    return env_flag("METRICS_SQL", True) and bool(env_str("AZURE_SQL_SERVER"))


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


def _text(max_len: int) -> Converter:
    return lambda value: str(value or "")[:max_len]


def _opt_text(max_len: int) -> Converter:
    return lambda value: text_or_none(value, max_len)


def _count(value: Any) -> int:
    return to_int(value, default=0)


def _opt_count(value: Any) -> int | None:
    number = to_float(value)
    return None if number is None else int(number)


def _bit(value: Any) -> int:
    return 1 if is_true(value) else 0


# (SQL column, record key, converter)
RUN_COLUMNS: tuple[tuple[str, str, Converter], ...] = (
    ("RunId", "run_id", _text(80)),
    ("RecordedAt", "recorded_at", _dt),
    ("Sites", "sites", _count),
    ("AppliedRooftop", "applied_rooftop", _count),
    ("AppliedTower", "applied_tower", _count),
    ("AppliedDbSkip", "applied_db_skip", _count),
    ("AppliedOther", "applied_other", _count),
    ("ApplyFailed", "apply_failed", _count),
    ("HoldoutEmptyConfirmed", "holdout_empty_confirmed", _count),
    ("HoldoutWeakRooftop", "holdout_weak_rooftop", _count),
    ("HoldoutWeakTower", "holdout_weak_tower", _count),
    ("HoldoutEmpty", "holdout_empty", _count),
    ("HoldoutNoNearmap", "holdout_no_nearmap", _count),
    ("HoldoutNoImagery", "holdout_no_imagery", _count),
    ("HoldoutOther", "holdout_other", _count),
    ("HoldoutNearmapBudget", "holdout_nearmap_budget", _count),
    ("DbOnlyMiss", "db_only_miss", _count),
    ("Skipped", "skipped", _count),
    ("Errors", "errors", _count),
    ("NearmapSites", "nearmap_sites", _count),
    ("ClaudeSites", "claude_sites", _count),
    ("NaipEmptyToNearmap", "naip_empty_to_nearmap", _count),
    ("NaipEmptyToRooftop", "naip_empty_to_rooftop", _count),
    ("NaipEmptyToRooftopApply", "naip_empty_to_rooftop_apply", _count),
    ("EmptyToRooftopApplyRate", "empty_to_rooftop_apply_rate", to_float),
    ("SfWrites", "sf_writes", _count),
    ("SfHoldoutsDequeued", "sf_holdouts_dequeued", _count),
    ("SfWriteFailed", "sf_write_failed", _count),
    ("NearmapBytes", "nearmap_bytes", _count),
    ("NearmapTiles", "nearmap_tiles", _count),
    ("NearmapCacheHits", "nearmap_cache_hits", _count),
    ("ApplyEnabled", "apply_enabled", _count),
    ("QueueStates", "queue_states", _opt_text(80)),
    ("QueueLimit", "queue_limit", _opt_count),
    ("Notes", "notes", _opt_text(400)),
)

SITE_COLUMNS: tuple[tuple[str, str, Converter], ...] = (
    ("RunId", "run_id", _text(80)),
    ("SalesforceId", "Id", _text(18)),
    ("Address", "address", _opt_text(300)),
    ("SiteState", "site_state", _opt_text(8)),
    ("SiteCity", "site_city", _opt_text(80)),
    ("Carrier", "carrier", _opt_text(120)),
    ("MatchSource", "match_source", _opt_text(32)),
    ("DualModelResolution", "dual_model_resolution", _opt_text(48)),
    ("ClassifyCoordSource", "classify_coord_source", _opt_text(48)),
    ("AssetOffsetM", "asset_offset_m", to_float),
    ("ScreenSiteType", "screen_site_type", _opt_text(32)),
    ("FinalSiteType", "final_site_type", _opt_text(32)),
    ("FinalConfidence", "final_confidence", to_float),
    ("NearmapRan", "nearmap_ran", _bit),
    ("NearmapTier", "nearmap_tier", _opt_text(32)),
    ("ClaudeRan", "claude_ran", _bit),
    ("EscalationReason", "escalation_reason", _opt_text(80)),
    ("SecondNearmap", "second_nearmap", _opt_text(32)),
    ("EmptyToNearmap", "empty_to_nearmap", _bit),
    ("EmptyToRooftop", "empty_to_rooftop", _bit),
    ("EmptyToRooftopApply", "empty_to_rooftop_apply", _bit),
    ("Bucket", "bucket", _opt_text(64)),
    ("HoldoutReason", "holdout_reason", _opt_text(128)),
    ("UpdateSiteType", "update_site_type", _opt_text(32)),
    ("Outcome", "outcome", lambda v: str(v or "holdout_other")[:64]),
    ("SfUpdateStatus", "sf_update_status", _opt_text(32)),
    ("NearmapBytes", "nearmap_bytes", _count),
    ("NearmapTiles", "nearmap_tiles", _count),
    ("NearmapCacheHits", "nearmap_cache_hits", _count),
    ("Notes", "notes", _opt_text(400)),
)


def _values(columns: Sequence[tuple[str, str, Converter]], rec: dict[str, Any]) -> tuple:
    return tuple(convert(rec.get(key)) for _col, key, convert in columns)


def run_row(run: dict[str, Any]) -> tuple[Any, ...]:
    return _values(RUN_COLUMNS, run)


def site_row(site: dict[str, Any]) -> tuple[Any, ...]:
    from enrichment.metrics import apply_slice_fields

    rec = apply_slice_fields(site)
    rec["Id"] = rec.get("Id") or rec.get("SalesforceId")
    return _values(SITE_COLUMNS, rec)


def _merge_sql(table: str, columns: Sequence[tuple[str, str, Converter]], keys: Sequence[str]) -> str:
    names = [col for col, _key, _conv in columns]
    return (
        f"MERGE {table} AS t USING (SELECT "
        + ", ".join(f"? AS {name}" for name in names)
        + ") AS s ON "
        + " AND ".join(f"t.{key} = s.{key}" for key in keys)
        + " WHEN MATCHED THEN UPDATE SET "
        + ", ".join(f"{name} = s.{name}" for name in names if name not in keys)
        + " WHEN NOT MATCHED THEN INSERT ("
        + ", ".join(names)
        + ") VALUES ("
        + ", ".join(f"s.{name}" for name in names)
        + ");"
    )


def _insert_sql(table: str, columns: Sequence[tuple[str, str, Converter]]) -> str:
    names = [col for col, _key, _conv in columns]
    return (
        f"INSERT INTO {table} ({', '.join(names)}) "
        f"VALUES ({', '.join('?' for _ in names)})"
    )


_UPSERT_RUN = _merge_sql("dbo.EnrichmentRun", RUN_COLUMNS, ("RunId",))
_UPSERT_SITE = _merge_sql(
    "dbo.EnrichmentSiteOutcome", SITE_COLUMNS, ("RunId", "SalesforceId")
)
_INSERT_SITE = _insert_sql("dbo.EnrichmentSiteOutcome", SITE_COLUMNS)


def ensure_tables(cursor) -> None:
    for stmt in ddl_statements():
        cursor.execute(stmt)


def _salesforce_id(rec: dict[str, Any]) -> str:
    return str(rec.get("Id") or rec.get("SalesforceId") or "").strip()


def _site_rows(run_id: str, sites: Iterable[dict[str, Any]]) -> list[tuple]:
    """Converted rows, one per distinct Salesforce Id (last record wins)."""
    by_id: dict[str, dict[str, Any]] = {}
    for rec in sites:
        sid = _salesforce_id(rec)
        if sid:
            by_id[sid] = {**rec, "run_id": rec.get("run_id") or run_id}
    return [site_row(rec) for rec in by_id.values()]


def upsert_snapshot(cursor, snap: dict[str, Any]) -> int:
    """Replace one run header and all of its site rows. Returns site count written."""
    run_id = str(snap.get("run_id") or "")
    if not run_id:
        raise ValueError("snapshot missing run_id")
    rows = _site_rows(run_id, snap.get("site_records") or [])
    cursor.execute(_UPSERT_RUN, run_row(snap))
    cursor.execute("DELETE FROM dbo.EnrichmentSiteOutcome WHERE RunId = ?", run_id)
    if rows:
        try:
            cursor.fast_executemany = True
        except AttributeError:
            pass
        cursor.executemany(_INSERT_SITE, rows)
    return len(rows)


def _with_connection(action: Callable[[Any], Any], *, ensure: bool = True):
    from enrichment.mssql import connect_mssql

    conn = connect_mssql()
    try:
        cursor = conn.cursor()
        if ensure:
            ensure_tables(cursor)
        result = action(cursor)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def write_snapshot(snap: dict[str, Any]) -> int:
    """Open a connection, ensure tables, upsert. Raises on SQL failure."""
    return _with_connection(lambda cursor: upsert_snapshot(cursor, snap))


def try_write_snapshot(snap: dict[str, Any]) -> None:
    """Pipeline hook: skip when SQL is off; log and continue on failure."""
    if not metrics_sql_enabled():
        return
    try:
        n = write_snapshot(snap)
        logger.info("metrics SQL upsert run_id=%s sites=%s", snap.get("run_id"), n)
    except Exception:
        logger.exception("metrics SQL upsert skipped")


class SiteSink:
    """Incremental per-site persistence for one live run (fail-open).

    ``begin`` upserts the run header so site rows can reference it; ``add``
    upserts finished sites immediately. After the first failure the sink goes
    quiet — ``record_run`` still reconciles the full run at the end.
    """

    def __init__(self, run_id: str, *, enabled: bool | None = None) -> None:
        self.run_id = run_id
        self.enabled = metrics_sql_enabled() if enabled is None else enabled
        self._conn = None

    def _cursor(self):
        if self._conn is None:
            from enrichment.mssql import connect_mssql

            self._conn = connect_mssql()
            ensure_tables(self._conn.cursor())
            self._conn.commit()
        return self._conn.cursor()

    def _run(self, action: Callable[[Any], None], what: str) -> None:
        if not self.enabled:
            return
        try:
            cursor = self._cursor()
            action(cursor)
            self._conn.commit()
        except Exception as exc:  # noqa: BLE001 — metrics never stop a run
            logger.warning("metrics SQL %s skipped (%s); end-of-run reconcile will retry", what, exc)
            self.enabled = False
            self.close()

    def begin(self, header: dict[str, Any]) -> None:
        self._run(
            lambda cursor: cursor.execute(
                _UPSERT_RUN, run_row({**header, "run_id": self.run_id})
            ),
            "run header",
        )

    def add(self, records: Iterable[dict[str, Any]]) -> None:
        rows = _site_rows(self.run_id, records)
        if not rows:
            return

        def upsert(cursor) -> None:
            for row in rows:
                cursor.execute(_UPSERT_SITE, row)

        self._run(upsert, "site upsert")

    def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def processed_ids() -> set[str]:
    """Every Salesforce Id any live run has recorded (for SKIP_FROM=sql)."""
    def fetch(cursor) -> set[str]:
        cursor.execute("SELECT DISTINCT SalesforceId FROM dbo.EnrichmentSiteOutcome")
        return {str(row[0]) for row in cursor.fetchall() if row and row[0]}

    return _with_connection(fetch, ensure=False)


def delete_all_site_outcomes(cursor) -> None:
    """Empty the site fact table. Run headers stay; caller reloads sites."""
    cursor.execute("DELETE FROM dbo.EnrichmentSiteOutcome")


def delete_run(run_id: str) -> None:
    """Remove one run header and its site rows from Azure SQL."""
    rid = str(run_id or "").strip()
    if not rid:
        raise ValueError("run_id is required")

    def drop(cursor) -> None:
        cursor.execute("DELETE FROM dbo.EnrichmentSiteOutcome WHERE RunId = ?", rid)
        cursor.execute("DELETE FROM dbo.EnrichmentRun WHERE RunId = ?", rid)

    _with_connection(drop, ensure=False)
    logger.info("metrics SQL deleted run_id=%s", rid)
