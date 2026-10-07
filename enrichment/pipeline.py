"""Enrichment pipeline: SF blank Site_Type → FCC/TowerSource → imagery classify → Salesforce.

Per run:
  1. Query the Salesforce queue and apply OFFSET / LIMIT / SKIP_FROM.
  2. Prepare (main thread): batch-geocode addresses, bulk FCC/TowerSource
     lookup, and settle every site that needs no imagery (DB-only, unique
     DB hit, missing coords, SQL error).
  3. Classify the rest on ``CLASSIFY_WORKERS`` threads. Pins within
     PIN_CLUSTER_M of each other stay in one worker so nearby-pin reuse
     still works.
  4. As each site finishes, the main thread writes it to Salesforce, appends
     it to ``enrichment_detail.csv``, and upserts its metrics row — a crash
     keeps every completed site.
  5. End-of-run sweep: retry anything still pending, write the summary CSVs,
     and record run metrics (JSONL + Azure SQL).

All Salesforce and SQL I/O stays on the main thread.
"""

from __future__ import annotations

import csv
import json
import logging
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from enrichment import progress
from enrichment.bucketing import (
    bucket_classification,
    imagery_bucket,
    naip_rooftop_confirm_decision,
    verified_source_for_match,
)
from enrichment.coerce import to_float
from enrichment.constants import (
    APPLY_LOG_CSV,
    BUCKET_AUDIT_UNQUALIFY,
    BUCKET_OTHER,
    BUCKET_POTENTIAL_UPDATE,
    BUCKET_ROOFTOP,
    CANDIDATE_CSV,
    DEFAULT_STAGE_FILTER,
    DETAIL_CSV,
    HIGH_INPUT_CONFIDENCE_STAGES,
    HOLDOUT_CSV,
    MATCH_SOURCE_NONE,
    PROXIMITY_MAX_M,
)
from enrichment.connectx_audit import (
    query_connectx_audit_sites,
    site_acq_owner_id,
    stamp_audit_verdict,
)
from enrichment.cost_policy import (
    auto_skip_classify_reason,
    cluster_groups,
    find_cluster_match,
    remember_cluster_result,
)
from enrichment.geo import (
    build_site_address,
    geocode_census,
    geocode_sites,
    haversine_meters,
    pin_address_is_mismatch,
    should_compare_rooftop_hosts,
)
from enrichment.metrics import (
    RUN_METRIC_KEYS,
    month_to_date_nearmap_bytes,
    record_nearmap_usage,
    outcome_class,
    record_run,
    site_record,
    stamp_statuses_from_apply_log,
)
from enrichment.metrics_store import SiteSink
from enrichment.mssql import (
    ProximityQuery,
    connect_mssql,
    describe_match,
    find_proximity_hit,
    find_proximity_hits_bulk,
    is_sql_link_failure,
    reconnect_mssql,
)
from enrichment.naip_classify import classify_site_imagery
from enrichment.outputs import (
    CANDIDATE_COLUMNS,
    DETAIL_COLUMNS,
    HOLDOUT_COLUMNS,
    CsvAppender,
    write_csv,
)
from enrichment.sf_ops import (
    apply_one_update,
    apply_updates_batch,
    parse_sf_lat_lng,
    query_blank_site_type_sites,
    status_from_entry,
    query_sites_by_ids,
)
from envutil import env_float, env_int
from paths import runs_dir
from salesforce.site_type_mapping import (
    is_sales_tower_type,
    site_type_from_db_asset_type,
)

logger = logging.getLogger(__name__)

_ALREADY_WRITTEN = frozenset({"updated", "classified_only", "dequeued", "unqualified"})
_SQL_ERROR_HOLDOUT = "sql_error"
_NEARMAP_BUDGET_HOLDOUT = "nearmap_budget"
# Transient holdouts stay in the Salesforce queue for a later run.
_KEEP_QUEUED_HOLDOUTS = frozenset({_SQL_ERROR_HOLDOUT, _NEARMAP_BUDGET_HOLDOUT})
# Sentinel: "no prefetched proximity for this site — look it up inline".
_NOT_PREFETCHED = object()


MAX_CLASSIFY_WORKERS = 32


def classify_workers() -> int:
    """Parallel imagery-classify threads (``CLASSIFY_WORKERS``, default 10, max 32).

    Throughput tops out at the shared model pacing, not the thread count:
    roughly GEMINI_RPM / (Gemini calls per site) sites per minute. Raise
    GEMINI_RPM with CLASSIFY_WORKERS, up to your Gemini quota.
    """
    return max(1, min(MAX_CLASSIFY_WORKERS, env_int("CLASSIFY_WORKERS", 10)))


def apply_batch_size() -> int:
    """Salesforce rows per live sObject Collections write (``APPLY_BATCH_SIZE``).

    Default 25 (max 200). A batch also goes out after ``APPLY_FLUSH_S``
    seconds, on Ctrl+C, and at run end. 1 = write each site as it finishes.
    Finished sites are in ``enrichment_detail.csv`` (``pending``) before their
    batch is sent, so after a hard crash ``APPLY_EXISTING=1`` with that
    ``RUN_DIR`` pushes whatever had not gone out.
    """
    return max(1, min(200, env_int("APPLY_BATCH_SIZE", 25)))


def apply_flush_s() -> float:
    """Max seconds a finished site waits in the Salesforce batch (``APPLY_FLUSH_S``)."""
    return max(1.0, env_float("APPLY_FLUSH_S", 60))


def default_run_dir(root: Path | None = None, *, suffix: str = "_sf_enrichment") -> Path:
    from paths import ensure_data_layout

    ensure_data_layout()
    base = root or runs_dir()
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    return base / f"{stamp}{suffix}"


def apply_paused_run(
    *,
    sf_client,
    run_dir: Path,
    apply: bool = True,
    confirm_rooftop: bool = False,
    confirm_existing: bool = False,
    db_only: bool = False,
    connectx_audit: bool = False,
    dequeue_holdouts: bool = False,
    verbose: bool = True,
    max_sites: int | None = None,
) -> dict[str, Any]:
    """Apply Salesforce updates from a classify run that never reached stage 4/4.

    Reads ``enrichment_detail.csv`` (appended after each site). Live runs
    also Salesforce-apply after each site; use this when a job died before
    that write. Does not re-query Salesforce or re-classify. Confirm path
    writes Site_Type + LLM_Classified only for ``potential_update`` rows
    still pending. ``connectx_audit`` also pushes pending unqualify rows.
    """
    path = run_dir / DETAIL_CSV
    if not path.is_file():
        raise FileNotFoundError(f"No {DETAIL_CSV} in {run_dir}")
    with path.open(newline="", encoding="utf-8-sig") as handle:
        detail_rows = list(csv.DictReader(handle))
    # Sites whose batch already went out are 'updated' in the apply log even
    # though the detail CSV (written before the batch) still says 'pending'.
    stamp_statuses_from_apply_log(run_dir, detail_rows)
    if max_sites is not None:
        detail_rows = detail_rows[: max(0, int(max_sites))]
    if verbose:
        progress.warn(
            f"apply existing {run_dir.name}: {len(detail_rows)} classified row(s)"
        )
    return _write_and_apply_run(
        sf_client=sf_client,
        run_dir=run_dir,
        detail_rows=detail_rows,
        apply=apply,
        dequeue_holdouts=dequeue_holdouts,
        db_only=db_only,
        confirm_rooftop=confirm_rooftop,
        confirm_existing=confirm_existing,
        connectx_audit=connectx_audit,
        verbose=verbose,
    )


def apply_queue_window(
    sites: list[dict[str, Any]],
    *,
    offset: int = 0,
    limit: int | None = None,
    skip_ids: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Drop already-attempted Ids, then slice ``sites[offset:offset+limit]``.

    Without holdouts, non-hits stay at the front of the Salesforce queue.
    ``skip_ids`` (prior run CSVs) is the way to walk past them. ``offset`` is
    only stable for a frozen snapshot — hits that leave Salesforce shift
    later rows up.
    """
    skip = {str(sid).strip() for sid in (skip_ids or []) if str(sid).strip()}
    if skip:
        sites = [
            row for row in sites if str(row.get("Id") or "").strip() not in skip
        ]
    start = max(0, int(offset or 0))
    if limit is None:
        return sites[start:]
    return sites[start : start + max(0, int(limit))]


def _use_rooftop_confirm(
    site: dict[str, Any],
    *,
    confirm_rooftop: bool,
    confirm_existing: bool,
) -> bool:
    """Rooftop presence path vs FCC/NAIP tower path for one Salesforce row."""
    if confirm_rooftop:
        return True
    if not confirm_existing:
        return False
    return not is_sales_tower_type(site.get("Site_Type__c"))


# --------------------------------- queue ------------------------------------


def _query_queue(
    sf_client,
    *,
    site_ids,
    stages,
    owners,
    exclude_owners,
    carrier_like,
    metro_classification,
    states,
    llm_classified,
    site_type,
    verbose,
    connectx_audit=False,
    audit_pool=False,
    audit_llm_classified=None,
) -> list[dict[str, Any]]:
    if site_ids:
        if verbose:
            progress.stage("2/4 QUERY SALESFORCE", f"{len(site_ids)} explicit Id(s)")
        with progress.busy("querying Salesforce") if not verbose else nullcontext():
            return query_sites_by_ids(sf_client, site_ids)
    if connectx_audit:
        owner_id = site_acq_owner_id()
        if verbose:
            progress.stage(
                "2/4 QUERY SALESFORCE",
                f"ConnectX audit | Site_Type=Rooftop | "
                f"OwnerId{'=' if audit_pool else '!='}{owner_id} | "
                f"llm_classified={audit_llm_classified if audit_llm_classified is not None else 'any'} | "
                f"stages={','.join(stages) if stages else 'default'} | "
                f"carrier_like={carrier_like!r} | "
                f"owners={','.join(owners) if owners else 'any rep'} | "
                f"states={','.join(states) if states else 'all'}",
            )
        with progress.busy("querying Salesforce") if not verbose else nullcontext():
            return query_connectx_audit_sites(
                sf_client,
                stages=stages,
                carrier_like=carrier_like,
                states=states,
                owners=owners,
                exclude_owners=exclude_owners,
                exclude_owner_id=owner_id,
                pool=audit_pool,
                llm_classified=audit_llm_classified,
            )
    stage_filter = stages or list(DEFAULT_STAGE_FILTER)
    wanted_type = (site_type or "").strip()
    if wanted_type.lower() in {"any", "all", "*", "none"}:
        queue_label, query_site_type = "any Site_Type", "any"
    elif wanted_type:
        queue_label, query_site_type = f"Site_Type={wanted_type}", wanted_type
    else:
        queue_label, query_site_type = "blank Site_Type", None
    if verbose:
        progress.stage(
            "2/4 QUERY SALESFORCE",
            f"{queue_label} | stages={','.join(stage_filter)} | "
            f"owners={','.join(owners) if owners else 'any'} | "
            f"exclude_owners={','.join(exclude_owners) if exclude_owners else 'none'} | "
            f"carrier_like={carrier_like!r} | "
            f"metro={metro_classification!r} | "
            f"llm_classified={str(llm_classified).lower()} | "
            f"states={','.join(states) if states else 'all'}",
        )
    with progress.busy("querying Salesforce") if not verbose else nullcontext():
        return query_blank_site_type_sites(
            sf_client,
            stages=stage_filter,
            owners=owners,
            exclude_owners=exclude_owners,
            carrier_like=carrier_like,
            metro_classification=metro_classification,
            states=states,
            llm_classified=llm_classified,
            site_type=query_site_type,
        )


# ------------------------------ apply helpers -------------------------------


def _needs_sf_apply(row: dict[str, Any]) -> bool:
    status = str(row.get("sf_update_status") or "").strip().lower()
    return status not in _ALREADY_WRITTEN


_APPLY_LOG_COLUMNS = (
    "index",
    "Id",
    "success",
    "dry_run",
    "status",
    "error",
    "payload_json",
)


def _collect_apply_rows(
    detail_rows: list[dict[str, Any]],
    *,
    dequeue_holdouts: bool,
    db_only: bool,
    confirm_rooftop: bool,
    confirm_existing: bool,
    connectx_audit: bool = False,
) -> list[dict[str, Any]]:
    apply_rows = [
        row
        for row in detail_rows
        if row.get("bucket") in {BUCKET_POTENTIAL_UPDATE, BUCKET_AUDIT_UNQUALIFY}
        and _needs_sf_apply(row)
    ]
    leave_failures = confirm_rooftop or confirm_existing or connectx_audit
    if not leave_failures and (dequeue_holdouts or db_only):
        apply_rows.extend(
            row
            for row in detail_rows
            if row.get("bucket") != BUCKET_POTENTIAL_UPDATE
            and _needs_sf_apply(row)
            and str(row.get("holdout_reason") or "") not in _KEEP_QUEUED_HOLDOUTS
        )
    return apply_rows


def _row_eligible_for_apply(
    row: dict[str, Any],
    *,
    dequeue_holdouts: bool,
    db_only: bool,
    confirm_rooftop: bool,
    confirm_existing: bool,
    connectx_audit: bool = False,
) -> bool:
    return bool(
        _collect_apply_rows(
            [row],
            dequeue_holdouts=dequeue_holdouts,
            db_only=db_only,
            confirm_rooftop=confirm_rooftop,
            confirm_existing=confirm_existing,
            connectx_audit=connectx_audit,
        )
    )


def _status_counts(detail_rows: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts = {
        "updated": 0,
        "dequeued": 0,
        "classified_only": 0,
        "unqualified": 0,
        "failed": 0,
    }
    for row in detail_rows:
        status = str(row.get("sf_update_status") or "").strip().lower()
        if status in counts:
            counts[status] += 1
    return counts


def _apply_info_from_detail(
    detail_rows: list[dict[str, Any]],
    *,
    apply: bool,
    log: str = "",
) -> dict[str, Any]:
    counts = _status_counts(detail_rows)
    return {
        "total": sum(counts.values()),
        "success": counts["updated"],
        "dequeued_holdouts": counts["dequeued"],
        "classified_only": counts["classified_only"],
        "unqualified": counts["unqualified"],
        "failed": counts["failed"],
        "apply": apply,
        "log": log,
    }


def _append_apply_log(run_dir: Path, results: list[dict[str, Any]]) -> Path:
    log_path = run_dir / APPLY_LOG_CSV
    start = 0
    if log_path.is_file():
        with log_path.open(newline="", encoding="utf-8-sig") as handle:
            start = max(0, sum(1 for _ in csv.reader(handle)) - 1)
    appender = CsvAppender(log_path, _APPLY_LOG_COLUMNS)
    for offset, entry in enumerate(results):
        appender.append(
            {
                "index": start + offset + 1,
                "Id": entry.get("Id"),
                "success": entry.get("success"),
                "dry_run": entry.get("dry_run"),
                "status": entry.get("status", ""),
                "error": entry.get("error", ""),
                "payload_json": json.dumps(entry.get("payload") or {}),
            }
        )
    appender.close()
    return log_path


def stamp_apply_status(
    detail_rows: list[dict[str, Any]],
    apply_results: list[dict[str, Any]] | None,
) -> None:
    """Copy Salesforce apply outcomes onto site rows (status, error, outcome)."""
    by_id = {
        str(entry.get("Id") or "").strip(): entry
        for entry in apply_results or []
        if str(entry.get("Id") or "").strip()
    }
    for row in detail_rows:
        entry = by_id.get(str(row.get("Id") or "").strip())
        if not entry:
            continue
        status = status_from_entry(entry)
        row["sf_update_status"] = status
        row["sf_update_error"] = str(entry.get("error") or "") if status == "failed" else ""
        row["outcome_class"] = outcome_class(row)


def _apply_rows(
    sf_client,
    rows: list[dict[str, Any]],
    *,
    run_dir: Path,
    verbose: bool,
    write_holdout: bool,
    error_holdout: bool,
    batch: bool,
) -> list[dict[str, Any]]:
    """Write rows to Salesforce, log them, and stamp their detail status."""
    if batch:
        results = apply_updates_batch(
            sf_client,
            rows,
            dry_run=False,
            verbose=verbose,
            write_holdout=write_holdout,
            error_holdout=error_holdout,
        )
    else:
        results = [
            apply_one_update(
                sf_client,
                row,
                dry_run=False,
                verbose=verbose,
                write_holdout=write_holdout,
                error_holdout=error_holdout,
            )
            for row in rows
        ]
    stamp_apply_status(rows, results)
    _append_apply_log(run_dir, results)
    return results


def _write_summary_csvs(run_dir: Path, detail_rows: list[dict[str, Any]]) -> tuple[int, int]:
    candidates = [r for r in detail_rows if r.get("bucket") == BUCKET_POTENTIAL_UPDATE]
    holdouts = [r for r in detail_rows if r.get("bucket") in {BUCKET_ROOFTOP, BUCKET_OTHER}]
    write_csv(run_dir / DETAIL_CSV, detail_rows, DETAIL_COLUMNS)
    write_csv(run_dir / CANDIDATE_CSV, candidates, CANDIDATE_COLUMNS)
    write_csv(run_dir / HOLDOUT_CSV, holdouts, HOLDOUT_COLUMNS)
    return len(candidates), len(holdouts)


def _write_and_apply_run(
    *,
    sf_client,
    run_dir: Path,
    detail_rows: list[dict[str, Any]],
    apply: bool,
    dequeue_holdouts: bool,
    db_only: bool,
    confirm_rooftop: bool,
    confirm_existing: bool = False,
    connectx_audit: bool = False,
    verbose: bool,
) -> dict[str, Any]:
    """End-of-run sweep: apply anything pending, write CSVs, record metrics."""
    if verbose:
        progress.stage("4/4 WRITE CSVs", str(run_dir.name))
    leave_failures = confirm_rooftop or confirm_existing or connectx_audit
    apply_info: dict[str, Any] | None = None
    if apply:
        apply_rows = _collect_apply_rows(
            detail_rows,
            dequeue_holdouts=dequeue_holdouts,
            db_only=db_only,
            confirm_rooftop=confirm_rooftop,
            confirm_existing=confirm_existing,
            connectx_audit=connectx_audit,
        )
        if apply_rows:
            if verbose:
                progress.stage(
                    "APPLY SALESFORCE UPDATES", f"{len(apply_rows)} pending row(s) | LIVE WRITES"
                )
            _apply_rows(
                sf_client,
                apply_rows,
                run_dir=run_dir,
                verbose=verbose,
                write_holdout=not db_only and not leave_failures,
                error_holdout=not leave_failures,
                batch=True,
            )
        apply_info = _apply_info_from_detail(
            detail_rows, apply=True, log=str(run_dir / APPLY_LOG_CSV)
        )
    else:
        for row in detail_rows:
            if row.get("sf_update_status") == "pending":
                row["sf_update_status"] = "dry_run"
    n_candidates, n_holdouts = _write_summary_csvs(run_dir, detail_rows)
    if verbose:
        progress.result(
            f"updates={n_candidates} holdouts={n_holdouts} total={len(detail_rows)}"
        )

    run_block: dict[str, Any] = {"sites": len(detail_rows)}
    kpis_block: dict[str, Any] | None = None
    try:
        metrics_snap = record_run(
            run_dir=run_dir,
            detail_rows=detail_rows,
            apply_summary=apply_info,
            db_only=db_only,
            confirm_rooftop=confirm_rooftop or confirm_existing,
            write_sql=bool(apply),
        )
        run_block = {
            key: metrics_snap[key]
            for key in ("run_id", *RUN_METRIC_KEYS)
            if key in metrics_snap
        }
        kpis_block = metrics_snap.get("kpis")
    except Exception as exc:  # noqa: BLE001
        logger.exception("metrics ledger skipped: %s", exc)
        if verbose:
            progress.warn(f"metrics ledger skipped: {exc}")
    summary = {
        "run_dir": str(run_dir),
        "run": run_block,
        "kpis": kpis_block,
        "apply": apply_info or {"failed": 0, "success": 0, "dequeued_holdouts": 0},
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    if verbose:
        progress.dump_summary(summary)
    return summary


# ------------------------------- proximity ----------------------------------


def _active_sql_cursor(cursor, sql_state: dict[str, Any] | None):
    if sql_state is not None and sql_state.get("cursor") is not None:
        return sql_state["cursor"]
    return cursor


def _proximity_hit(
    cursor,
    sql_state: dict[str, Any] | None,
    sf_lat: float,
    sf_lng: float,
    *,
    max_m: float,
    address_lat,
    address_lng,
    verbose: bool,
):
    """FCC/TowerSource lookup; reconnect once on a dropped ODBC link."""
    kwargs = dict(max_m=max_m, address_lat=address_lat, address_lng=address_lng)
    try:
        return find_proximity_hit(_active_sql_cursor(cursor, sql_state), sf_lat, sf_lng, **kwargs)
    except Exception as exc:
        if sql_state is None or not is_sql_link_failure(exc):
            raise
        logger.warning("SQL link failed; reconnecting once: %s", exc)
        if verbose:
            progress.warn("SQL link dropped — reconnecting once")
        reconnect_mssql(sql_state)
        return find_proximity_hit(sql_state["cursor"], sf_lat, sf_lng, **kwargs)


def _prefetch_geocodes(sites: list[dict[str, Any]], *, verbose: bool) -> dict[str, Any]:
    if not sites:
        return {}
    with progress.busy(f"geocoding {len(sites)} address(es)") if not verbose else nullcontext():
        geocodes = geocode_sites(sites)
    if verbose:
        found = sum(1 for value in geocodes.values() if value)
        progress.result(f"Census geocode: {found}/{len(geocodes)} address(es) matched")
    return geocodes


def _prefetch_proximity(
    sites: list[dict[str, Any]],
    geocodes: dict[str, Any],
    sql_state: dict[str, Any],
    *,
    max_m: float,
    verbose: bool,
) -> dict[str, Any]:
    """Bulk FCC/TowerSource lookup keyed by Salesforce Id. {} → per-site fallback."""
    queries = []
    for site in sites:
        coords = parse_sf_lat_lng(site)
        sf_id = str(site.get("Id") or "")
        if coords is None or not sf_id:
            continue
        geo = geocodes.get(build_site_address(site) or "") or {}
        queries.append(
            ProximityQuery(
                key=sf_id,
                lat=coords[0],
                lng=coords[1],
                address_lat=geo.get("lat"),
                address_lng=geo.get("lng"),
            )
        )
    if not queries or sql_state.get("cursor") is None:
        return {}
    for attempt in (1, 2):
        try:
            with progress.busy(f"FCC/TowerSource bulk lookup ({len(queries)})") if not verbose else nullcontext():
                hits = find_proximity_hits_bulk(sql_state["cursor"], queries, max_m=max_m)
            if verbose:
                progress.result(
                    f"bulk proximity: {sum(1 for h in hits.values() if h)}/{len(hits)} DB hit(s)"
                )
            return hits
        except Exception as exc:  # noqa: BLE001 — per-site lookups still run
            if attempt == 1 and is_sql_link_failure(exc):
                logger.warning("bulk proximity: SQL link dropped, reconnecting: %s", exc)
                reconnect_mssql(sql_state)
                continue
            logger.warning("bulk proximity unavailable (%s); per-site lookups", exc)
            if verbose:
                progress.warn(f"bulk proximity unavailable — per-site lookups ({exc})")
            return {}
    return {}


# ------------------------------- per site -----------------------------------


@dataclass
class PreparedSite:
    """A site after address + proximity, ready to classify (or already settled)."""

    base: dict[str, Any]
    site: dict[str, Any]
    done: bool = False
    rooftop_confirm: bool = False
    hit: Any = None
    sf_lat: float | None = None
    sf_lng: float | None = None
    addr_lat: float | None = None
    addr_lng: float | None = None
    db_lat: float | None = None
    db_lng: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)


_SF_COPY_FIELDS = (
    "Site_Street__c",
    "Site_City__c",
    "Site_State__c",
    "Site_Zip_Code__c",
    "Stage__c",
    "Owner__c",
    "Carrier_Leasing_Source__c",
    "Metro_Classification__c",
)


def _base_row(site: dict[str, Any]) -> dict[str, Any]:
    base: dict[str, Any] = dict.fromkeys(DETAIL_COLUMNS, "")
    base.update({key: site.get(key) or "" for key in _SF_COPY_FIELDS})
    base["Id"] = str(site.get("Id") or "")
    base["match_source"] = MATCH_SOURCE_NONE
    base["bucket"] = BUCKET_OTHER
    return base


def _stamp_address(base: dict[str, Any], geo, *, sf_lat, sf_lng, verbose):
    """Copy the Census geocode onto the row; return (addr_lat, addr_lng)."""
    if not base["address_query"] or not geo:
        return None, None
    addr_lat, addr_lng = geo["lat"], geo["lng"]
    offset_m = haversine_meters(sf_lat, sf_lng, addr_lat, addr_lng)
    mismatch = pin_address_is_mismatch(offset_m)
    base.update(
        {
            "address_lat": addr_lat,
            "address_lng": addr_lng,
            "address_geocode_source": geo.get("source") or "census",
            "address_matched": geo.get("matched") or "",
            "pin_address_offset_m": round(offset_m, 1),
            "pin_address_mismatch": mismatch,
        }
    )
    if verbose:
        extra = ""
        if mismatch:
            extra = " — far mismatch, pick pin vs Census before Nearmap"
        elif should_compare_rooftop_hosts(offset_m, db_backed=False):
            extra = " — rooftop host compare if no FCC/TS hit"
        progress.result(f"Census address {offset_m:.0f} m from pin{extra}")
    return addr_lat, addr_lng


def _stamp_match(base: dict[str, Any], hit) -> None:
    base.update(
        {
            "match_source": describe_match(hit),
            "match_distance_m": round(hit.distance_m, 2),
            "match_selection_reason": hit.selection_reason or "",
            "match_candidate_count": hit.candidate_count if hit.candidate_count is not None else "",
            "match_runner_up_gap_m": hit.runner_up_gap_m if hit.runner_up_gap_m is not None else "",
            "match_record_id": hit.record_id or "",
            "match_asr_number": hit.asr_number or "",
            "match_asset_type": hit.asset_type or "",
            "classify_lat": hit.latitude,
            "classify_lng": hit.longitude,
            "classify_coord_source": f"db:{describe_match(hit)}",
        }
    )


def _prepare_site(
    site: dict[str, Any],
    *,
    cursor,
    sql_state: dict[str, Any] | None,
    max_m: float,
    skip_classify: bool,
    db_only: bool,
    confirm_rooftop: bool,
    confirm_existing: bool,
    verbose: bool,
    geocodes: dict[str, Any] | None = None,
    proximity: Any = _NOT_PREFETCHED,
) -> PreparedSite:
    """Address + FCC/TowerSource for one site; settle it if no imagery is needed.

    ``geocodes`` / ``proximity`` carry prefetched batch results; when absent
    the lookup runs inline for this site (same rules, same reconnect).
    """
    base = _base_row(site)
    prep = PreparedSite(
        base=base,
        site=site,
        rooftop_confirm=_use_rooftop_confirm(
            site, confirm_rooftop=confirm_rooftop, confirm_existing=confirm_existing
        ),
    )
    coords = parse_sf_lat_lng(site)
    if coords is None:
        if verbose:
            progress.warn("Missing Salesforce lat/lng — skipping proximity/NAIP")
        base["holdout_reason"] = "missing_sf_coordinates"
        base["error"] = "missing_sf_coordinates"
        prep.done = True
        return prep

    sf_lat, sf_lng = coords
    prep.sf_lat, prep.sf_lng = sf_lat, sf_lng
    base.update(
        {
            "sf_lat": sf_lat,
            "sf_lng": sf_lng,
            "classify_lat": sf_lat,
            "classify_lng": sf_lng,
            "classify_coord_source": "sf_pin",
        }
    )
    if verbose:
        progress.step(f"SF pin: {sf_lat:.6f}, {sf_lng:.6f}")
    if prep.rooftop_confirm:
        if verbose:
            progress.step("NAIP rooftop confirm (Nearmap only if NAIP does not confirm)")
        return prep

    if verbose:
        progress.stage("PROXIMITY", f"≤{max_m:g} m")
    base["address_query"] = build_site_address(site) or ""
    if base["address_query"]:
        geo = (
            geocodes.get(base["address_query"])
            if geocodes is not None
            else geocode_census(base["address_query"])
        )
        prep.addr_lat, prep.addr_lng = _stamp_address(
            base, geo, sf_lat=sf_lat, sf_lng=sf_lng, verbose=verbose
        )
    try:
        if proximity is _NOT_PREFETCHED:
            hit = _proximity_hit(
                cursor,
                sql_state,
                sf_lat,
                sf_lng,
                max_m=max_m,
                address_lat=prep.addr_lat,
                address_lng=prep.addr_lng,
                verbose=verbose,
            )
        else:
            hit = proximity
    except Exception as exc:  # noqa: BLE001
        if verbose:
            progress.warn(f"SQL proximity failed: {exc}")
        base["error"] = f"sql_proximity_failed: {exc}"
        base["holdout_reason"] = _SQL_ERROR_HOLDOUT
        prep.done = True
        return prep

    prep.hit = hit
    if hit is not None:
        _stamp_match(base, hit)
        prep.db_lat, prep.db_lng = hit.latitude, hit.longitude
        if verbose:
            progress.result(
                f"{base['match_source']} @ {hit.distance_m:.1f} m "
                f"({hit.selection_reason or 'nearest'})"
            )
    elif verbose:
        if should_compare_rooftop_hosts(base.get("pin_address_offset_m") or None, db_backed=False):
            progress.result("no tower DB hit → rooftop path (pick host building before Nearmap)")
        else:
            progress.result("no DB hit → classify on SF pin")

    skip_reason = auto_skip_classify_reason(hit, force=db_only)
    if db_only:
        prep.done = True
        if skip_reason is not None and hit is not None:
            if verbose:
                progress.step(f"db-only unique hit ({skip_reason})")
            _stamp_unique_db_update(base, hit, base["classify_lat"], base["classify_lng"])
            return prep
        if verbose:
            progress.step("db-only — no unique FCC/TowerSource hit (no imagery)")
        base["holdout_reason"] = "db_only_no_unique_hit"
        return prep
    if skip_classify or skip_reason is not None:
        prep.done = True
        if verbose:
            progress.step("skip classify" if skip_classify else f"auto skip classify ({skip_reason})")
        if hit is not None:
            _stamp_unique_db_update(base, hit, base["classify_lat"], base["classify_lng"])
        else:
            base["holdout_reason"] = "skip_classify_no_db_hit"
    return prep


# classified result key → detail column (same name unless listed).
_CLASSIFIED_FIELDS = (
    ("site_type", "naip_site_type"),
    ("tower_subtype", "naip_tower_subtype"),
    ("site_confidence", "naip_site_confidence"),
    ("cell_equipment_confidence", "cell_equipment_confidence"),
    ("cell_equipment_evidence", "cell_equipment_evidence"),
    ("cell_gear_kind", "cell_gear_kind"),
    ("site_evidence", "site_evidence"),
    ("dual_model_resolution", "dual_model_resolution"),
    ("classification_stage", "classification_stage"),
    ("nearmap_tier", "nearmap_tier"),
    ("nearmap_views", "nearmap_views"),
    ("primary_model", "primary_model"),
    ("escalation_model", "escalation_model"),
    ("asset_box_2d", "asset_box_2d"),
    ("asset_view", "asset_view"),
)
# Fields copied only from a fresh classify (not a cluster-reuse copy).
_FRESH_CLASSIFIED_FIELDS = (
    "escalation_reason",
    "naip_screen_site_type",
    "naip_screen_site_confidence",
    "second_nearmap",
    "asset_lat",
    "asset_lon",
    "asset_offset_m",
    "asset_coord_source",
    "nearmap_bytes",
    "nearmap_tiles",
    "nearmap_cache_hits",
    "nearmap_spend",
)


def _stamp_classified(base: dict[str, Any], classified: dict[str, Any], *, fresh: bool) -> None:
    """Copy classifier output onto the detail row."""
    for key, column in _CLASSIFIED_FIELDS:
        base[column] = classified.get(key) or ""
    base["naip_cell_equipment"] = classified.get("cell_equipment")
    base["cell_models_agree"] = classified.get("cell_models_agree", "")
    base["imagery_used"] = imagery_bucket(classified)
    if fresh:
        base["gemini_cell_equipment"] = classified.get("gemini_cell_equipment", "")
        base["claude_cell_equipment"] = classified.get("claude_cell_equipment", "")
        base["naip_screen_cell_equipment"] = classified.get("naip_screen_cell_equipment")
        for key in _FRESH_CLASSIFIED_FIELDS:
            base[key] = classified.get(key) or ""


def classified_from_detail(row: dict[str, Any]) -> dict[str, Any]:
    """Rebuild the classifier result a detail row was stamped from (replay)."""
    classified = {key: row.get(column, "") for key, column in _CLASSIFIED_FIELDS}
    classified["cell_equipment"] = row.get("naip_cell_equipment")
    classified["cell_models_agree"] = row.get("cell_models_agree", "")
    for key in _FRESH_CLASSIFIED_FIELDS:
        classified[key] = row.get(key, "")
    if row.get("error"):
        classified["error"] = row.get("error")
    return classified


def replay_decision(row: dict[str, Any]) -> dict[str, Any] | None:
    """Re-run today's bucketing rules on a stored detail row.

    None for rows that never reached imagery bucketing (DB skip, SQL error,
    missing coords, DB-only misses) — their decision is not rule-driven.
    """
    reason = str(row.get("holdout_reason") or "")
    if reason in {
        "skip_classify_db_hit",
        _SQL_ERROR_HOLDOUT,
        "missing_sf_coordinates",
        "db_only_no_unique_hit",
        "skip_classify_no_db_hit",
        "classify_error",
        "no_saved_chips",
    }:
        return None
    classified = classified_from_detail(row)
    if reason in {"naip_rooftop_confirm", "naip_rooftop_unconfirmed"}:
        return naip_rooftop_confirm_decision(
            classified, existing_site_type=str(row.get("update_site_type") or "")
        )
    has_match = str(row.get("match_source") or MATCH_SOURCE_NONE) != MATCH_SOURCE_NONE
    return bucket_classification(
        match_source=str(row.get("match_source") or MATCH_SOURCE_NONE),
        classified=classified,
        db_lat=to_float(row.get("classify_lat")) if has_match else None,
        db_lng=to_float(row.get("classify_lng")) if has_match else None,
        sf_lat=to_float(row.get("sf_lat")),
        sf_lng=to_float(row.get("sf_lng")),
    )


def _call_classify(classify_fn, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Call classify_fn; older / test doubles may accept fewer kwargs."""
    try:
        return classify_fn(**kwargs)
    except TypeError:
        pass
    try:
        return classify_fn(
            **{k: kwargs[k] for k in ("site_id", "lat", "lon", "chip_dir", "verbose", "db_backed")}
        )
    except TypeError:
        return classify_fn(**{k: kwargs[k] for k in ("site_id", "lat", "lon", "chip_dir")})


def _bucket(prep: PreparedSite, classified: dict[str, Any]) -> dict[str, Any]:
    if prep.rooftop_confirm:
        return naip_rooftop_confirm_decision(
            classified, existing_site_type=str(prep.site.get("Site_Type__c") or "")
        )
    return bucket_classification(
        match_source=prep.base["match_source"],
        classified=classified,
        db_lat=prep.db_lat,
        db_lng=prep.db_lng,
        sf_lat=prep.sf_lat,
        sf_lng=prep.sf_lng,
    )


def _classify_prepared(
    prep: PreparedSite,
    *,
    classify_fn: Callable[..., dict[str, Any]],
    chip_dir: Path,
    verbose: bool,
    cluster_cache: list[dict[str, Any]] | None,
    reuse_chips_dirs: list[Path] | None,
) -> dict[str, Any]:
    """Imagery classify + bucket for a prepared site (thread-safe)."""
    base = prep.base
    classify_lat = float(base["classify_lat"])
    classify_lng = float(base["classify_lng"])
    use_cluster = cluster_cache is not None and prep.hit is None and not prep.rooftop_confirm

    if use_cluster:
        reuse = find_cluster_match(cluster_cache, classify_lat, classify_lng)
        if reuse is not None:
            classified = dict(reuse["classified"])
            classified["classification_stage"] = "cluster_reuse"
            if verbose:
                progress.step(f"reuse nearby pin ({reuse.get('lat')}, {reuse.get('lon')})")
            _stamp_classified(base, classified, fresh=False)
            base.update(_bucket(prep, classified))
            remember_cluster_result(cluster_cache, classify_lat, classify_lng, classified)
            return base

    if verbose:
        progress.stage("CLASSIFY")
    kwargs = {
        "site_id": base["Id"],
        "lat": classify_lat,
        "lon": classify_lng,
        "chip_dir": chip_dir,
        "verbose": verbose,
        "pin_lat": float(prep.sf_lat),
        "pin_lon": float(prep.sf_lng),
        "address_lat": prep.addr_lat,
        "address_lon": prep.addr_lng,
        "pin_address_offset_m": (
            float(base["pin_address_offset_m"]) if base["pin_address_offset_m"] != "" else None
        ),
        "pin_address_mismatch": bool(base["pin_address_mismatch"]),
        "db_backed": prep.hit is not None,
        "input_confidence": (
            "high"
            if str(prep.site.get("Stage__c") or "").strip() in HIGH_INPUT_CONFIDENCE_STAGES
            else "medium"
        ),
        "reuse_chips_dirs": reuse_chips_dirs or None,
    }
    if prep.rooftop_confirm:
        kwargs["presence_only"] = True
    try:
        classified = _call_classify(classify_fn, kwargs)
    except Exception as exc:  # noqa: BLE001
        if verbose:
            progress.warn(f"classify failed: {exc}")
        base["error"] = f"classify_failed: {exc}"
        base["bucket"] = BUCKET_OTHER
        base["holdout_reason"] = "classify_error"
        return base

    _stamp_classified(base, classified, fresh=True)
    if classified.get("error"):
        base["error"] = classified.get("error")
        if classified.get("error") == "no_saved_chips":
            base["bucket"] = BUCKET_OTHER
            base["holdout_reason"] = "no_saved_chips"
            return base
    if prep.hit is None:
        if classified.get("classify_coord_source"):
            base["classify_coord_source"] = classified["classify_coord_source"]
        try:
            if classified.get("lat") is not None and classified.get("lon") is not None:
                classify_lat = float(classified["lat"])
                classify_lng = float(classified["lon"])
                base["classify_lat"] = classify_lat
                base["classify_lng"] = classify_lng
        except (TypeError, ValueError):
            pass
    base.update(_bucket(prep, classified))
    if classified.get("nearmap_budget_blocked") and base.get("bucket") != BUCKET_POTENTIAL_UPDATE:
        # Needed Nearmap the monthly budget refused: keep it queued for next month.
        base["holdout_reason"] = _NEARMAP_BUDGET_HOLDOUT
    if use_cluster and not classified.get("error"):
        remember_cluster_result(cluster_cache, classify_lat, classify_lng, classified)
    return base


def _process_site(
    site: dict[str, Any],
    *,
    cursor,
    max_m: float,
    skip_classify: bool,
    db_only: bool = False,
    classify_fn: Callable[..., dict[str, Any]] | None,
    chip_dir: Path,
    verbose: bool = True,
    cluster_cache: list[dict[str, Any]] | None = None,
    reuse_chips_dirs: list[Path] | None = None,
    confirm_rooftop: bool = False,
    confirm_existing: bool = False,
    sql_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Prepare + classify one site inline (single-site entry point)."""
    prep = _prepare_site(
        site,
        cursor=cursor,
        sql_state=sql_state,
        max_m=max_m,
        skip_classify=skip_classify,
        db_only=db_only,
        confirm_rooftop=confirm_rooftop,
        confirm_existing=confirm_existing,
        verbose=verbose,
    )
    if prep.done:
        return prep.base
    return _classify_prepared(
        prep,
        classify_fn=classify_fn,
        chip_dir=chip_dir,
        verbose=verbose,
        cluster_cache=cluster_cache,
        reuse_chips_dirs=reuse_chips_dirs,
    )


def _stamp_unique_db_update(
    base: dict[str, Any],
    hit,
    classify_lat: float,
    classify_lng: float,
) -> dict[str, Any]:
    """Salesforce candidate from a unique FCC/TowerSource hit (no imagery)."""
    sf_type = site_type_from_db_asset_type(hit.asset_type)
    base["bucket"] = BUCKET_POTENTIAL_UPDATE
    base["holdout_reason"] = "skip_classify_db_hit"
    base["naip_site_type"] = "rooftop" if sf_type == "Rooftop" else "tower"
    base["update_lat"] = classify_lat
    base["update_lng"] = classify_lng
    base["update_coord_source"] = f"db:{base['match_source']}"
    base["update_site_type"] = sf_type
    base["update_verified_site"] = True
    base["update_verified_site_source"] = verified_source_for_match(base["match_source"])
    return base


# ------------------------------- run driver ---------------------------------


class _RunState:
    """Main-thread bookkeeping for one run: apply, CSV, metrics, progress."""

    def __init__(
        self,
        *,
        sf_client,
        run_dir: Path,
        apply: bool,
        verbose: bool,
        total: int,
        eligible: Callable[[dict[str, Any]], bool],
        write_holdout: bool,
        error_holdout: bool,
        sink: SiteSink | None,
        compact: bool,
        decorate: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.sf_client = sf_client
        self.run_dir = run_dir
        self.apply = apply
        self.verbose = verbose
        self.total = total
        self.eligible = eligible
        self.write_holdout = write_holdout
        self.error_holdout = error_holdout
        self.sink = sink
        self.compact = compact
        # Per-mode verdict stamped before bucketing is final (ConnectX audit).
        self.decorate = decorate
        self.detail_rows: list[dict[str, Any]] = []
        self.batch: list[dict[str, Any]] = []
        self.batch_size = apply_batch_size()
        self.flush_s = apply_flush_s()
        self._batch_since: float | None = None
        # Rows land here as they finish (status pending/skipped); the
        # end-of-run write replaces the file with final Salesforce statuses.
        self.csv = CsvAppender(run_dir / DETAIL_CSV, DETAIL_COLUMNS, truncate=True)

    def _record_metrics(self, rows: list[dict[str, Any]]) -> None:
        if self.sink is not None:
            self.sink.add(site_record(row, run_id=self.run_dir.name) for row in rows)

    def flush(self) -> None:
        if not self.batch:
            return
        rows, self.batch = self.batch, []
        self._batch_since = None
        if self.verbose:
            progress.step(f"Salesforce batch: {len(rows)} site(s)")
        _apply_rows(
            self.sf_client,
            rows,
            run_dir=self.run_dir,
            verbose=self.verbose,
            write_holdout=self.write_holdout,
            error_holdout=self.error_holdout,
            batch=len(rows) > 1,
        )
        self._record_metrics(rows)

    def maybe_flush(self) -> None:
        """Send the batch when it is full or its oldest site has waited APPLY_FLUSH_S."""
        if not self.batch:
            return
        waited = time.monotonic() - (self._batch_since or time.monotonic())
        if len(self.batch) >= self.batch_size or waited >= self.flush_s:
            self.flush()

    def finish(self, row: dict[str, Any], *, elapsed_s: float) -> None:
        if self.decorate is not None:
            row = self.decorate(row)
        row["outcome_class"] = outcome_class(row)
        row.setdefault("sf_update_error", "")
        row["sf_update_status"] = (
            "pending"
            if row.get("bucket") in {BUCKET_POTENTIAL_UPDATE, BUCKET_AUDIT_UNQUALIFY}
            else "skipped"
        )
        self.detail_rows.append(row)
        self.csv.append(row)
        record_nearmap_usage(self.run_dir.name, row)
        if self.apply and self.eligible(row):
            self.batch.append(row)
            if self._batch_since is None:
                self._batch_since = time.monotonic()
            self._report(row, elapsed_s)
            self.maybe_flush()
            return
        self._report(row, elapsed_s)
        self._record_metrics([row])

    def _report(self, row: dict[str, Any], elapsed_s: float) -> None:
        if self.verbose:
            who = f"{row.get('Id')} | " if self.compact else ""
            audit = (
                f"audit={row.get('audit_verdict')}:{row.get('audit_reason')} | "
                if row.get("audit_verdict")
                else ""
            )
            progress.result(
                f"{who}{audit}{row.get('bucket')} | type={row.get('naip_site_type') or '—'} | "
                f"img={row.get('imagery_used') or '—'} | "
                f"tier={row.get('nearmap_tier') or '—'} | "
                f"ai={row.get('escalation_model') or row.get('primary_model') or '—'} | "
                f"src={row.get('update_verified_site_source') or '—'} | "
                f"sf={row.get('sf_update_status') or '—'}",
                elapsed_s=elapsed_s,
            )
        elif self.compact:
            progress.row_count(
                len(self.detail_rows),
                self.total,
                sf_id=str(row.get("Id") or ""),
                address=f"{row.get('bucket')} | sf={row.get('sf_update_status') or '—'}",
            )

    def close(self) -> None:
        self.flush()
        self.csv.close()


def _site_label(index: int, total: int, site: dict[str, Any]) -> str:
    sf_id = str(site.get("Id") or "").strip() or "-"
    address = progress.format_site_address(site).strip() or "-"
    return f"[{index}/{total}] {sf_id} | {address}"


def _classify_all(
    preps: list[tuple[int, PreparedSite]],
    state: _RunState,
    *,
    workers: int,
    classify_kwargs: dict[str, Any],
) -> None:
    """Classify prepared sites; hand each finished row to ``state`` on this thread."""
    verbose = state.verbose
    total = state.total

    def run_one(index: int, prep: PreparedSite) -> tuple[dict[str, Any], float]:
        t0 = time.monotonic()
        if verbose:
            progress.stage(
                f"3/4 SITE {index}/{total}",
                f"{prep.base['Id']} | {progress.format_site_address(prep.site)}".strip(" |"),
            )
        row = _classify_prepared(prep, verbose=verbose, **classify_kwargs)
        return row, time.monotonic() - t0

    if workers <= 1 or len(preps) <= 1:
        for index, prep in preps:
            status = (
                progress.busy(_site_label(index, total, prep.site))
                if not verbose
                else nullcontext()
            )
            with status:
                row, elapsed = run_one(index, prep)
            state.finish(row, elapsed_s=elapsed)
        return

    groups = cluster_groups(
        [(float(p.base["classify_lat"]), float(p.base["classify_lng"])) for _i, p in preps]
    )
    results: queue.Queue = queue.Queue()
    stop = threading.Event()

    def run_group(members: list[int]) -> None:
        try:
            for member in members:
                if stop.is_set():
                    break
                index, prep = preps[member]
                # Hand the row over only after the site's buffered block has
                # printed, so its result line follows its own steps.
                try:
                    with progress.site_context(
                        f"{index}/{total} {prep.base['Id']}", buffered=verbose
                    ):
                        outcome = run_one(index, prep)
                except BaseException as exc:  # noqa: BLE001 — surface on main thread
                    results.put(("error", exc))
                    return
                results.put(("row", outcome))
        finally:
            results.put(("group_done", None))

    if verbose:
        progress.step(f"classifying {len(preps)} site(s) on {workers} worker(s)")
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="classify")
    for members in groups:
        pool.submit(run_group, members)
    remaining = len(groups)
    first_error: BaseException | None = None
    interrupted = False
    try:
        while remaining:
            try:
                kind, payload = results.get(timeout=0.5)
            except queue.Empty:
                state.maybe_flush()
                continue
            except KeyboardInterrupt:
                if interrupted:
                    raise
                interrupted = True
                stop.set()
                progress.warn("stopping — finishing in-flight site(s); Ctrl+C again to abort")
                continue
            if kind == "group_done":
                remaining -= 1
            elif kind == "error":
                first_error = first_error or payload
                stop.set()
            else:
                row, elapsed = payload
                state.finish(row, elapsed_s=elapsed)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    if first_error is not None:
        raise first_error
    if interrupted:
        raise KeyboardInterrupt


@contextmanager
def _hold_interrupts():
    """Ignore Ctrl+C while the final Salesforce batch and metrics are written.

    A second or third Ctrl+C used to land mid-flush and exit before the
    summary / metrics step. Close the terminal only if you must — the detail
    CSV still lets ``APPLY_EXISTING=1`` finish the job.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    import signal

    def notice(_signum, _frame):
        progress.warn("still writing Salesforce + metrics — please wait")

    prior = signal.signal(signal.SIGINT, notice)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, prior)


def run_enrichment(
    *,
    sf_client,
    sql_connection=None,
    run_dir: Path,
    limit: int | None = None,
    offset: int = 0,
    skip_ids: Iterable[str] | None = None,
    max_m: float = PROXIMITY_MAX_M,
    skip_classify: bool = False,
    db_only: bool = False,
    classify_fn: Callable[..., dict[str, Any]] | None = None,
    sites: list[dict[str, Any]] | None = None,
    site_ids: list[str] | None = None,
    carrier_like: str | None = None,
    metro_classification: str | None = "Major NFL Metro",
    states: list[str] | None = None,
    stages: list[str] | None = None,
    owners: list[str] | None = None,
    exclude_owners: list[str] | None = None,
    site_type: str | None = None,
    llm_classified: bool = False,
    verbose: bool = True,
    apply: bool = True,
    dequeue_holdouts: bool = True,
    reuse_chips_dirs: list[Path] | None = None,
    confirm_rooftop: bool = False,
    confirm_existing: bool = False,
    connectx_audit: bool = False,
    audit_pool: bool = False,
    audit_llm_classified: bool | None = None,
    audit_assign_to: str | None = None,
    workers: int | None = None,
) -> dict[str, Any]:
    """Run proximity + NAIP/Nearmap/Claude enrichment and Salesforce-apply.

    Each finished site is written to Salesforce (when ``apply``), appended to
    ``enrichment_detail.csv``, and upserted to Azure SQL metrics before the
    run moves on. An end-of-run sweep applies any rows still pending.

    Certain rooftops and high-conf towers become Salesforce updates
    (LLM_Classified=true, LLM_Holdout=false). Remaining rows dequeue
    (LLM_Classified=false, LLM_Holdout=true) unless ``dequeue_holdouts``
    is false — then holdouts are left untouched. Transient ``sql_error``
    rows are never dequeued so a dropped Azure SQL link can retry later.

    ``db_only`` never fetches NAIP/Nearmap/Gemini/Claude. Successful rows
    are marked LLM_Classified=true (no LLM_Holdout). Unique FCC or
    TowerSource hits also write Site_Type and coords. Blanks stay classified
    true until a later flip. Failed Salesforce writes retry once with
    LLM_Classified=false and LLM_Holdout=true.

    ``confirm_rooftop`` does not filter Site_Type. It classifies NAIP then
    Nearmap only if NAIP does not confirm a building roof (unless NAIP_ONLY=1).
    Success keeps the existing Site_Type (blank → Rooftop). Inconclusive
    rows are left unchanged (no holdout).

    ``confirm_existing`` is the Outreach - Verified sales-typed path: rooftop
    picklist rows use the presence confirm (NAIP first, Nearmap on fail);
    tower picklist rows use FCC/TowerSource + NAIP/Nearmap. Failures are
    left unchanged.

    ``connectx_audit`` queries ConnectX rooftops not owned by the Site
    Acquisition Team and runs the full classify path. Confirmed rows get the
    normal write; Nearmap-oblique no-gear rows are reassigned to the pool and
    unqualified (No Site/Decommissioned); inconclusive rows are left as-is.
    ``audit_pool`` audits the pool's own sites instead; ``audit_assign_to``
    adds that OwnerId to every confirmed write. See ``enrichment.connectx_audit``.

    ``workers`` (default ``CLASSIFY_WORKERS``) classify imagery in parallel.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    chip_dir = run_dir / "chips"
    progress.reset_run_timer()
    workers = classify_workers() if workers is None else max(1, int(workers))
    from classifier import imagery

    # Ledger bytes this month, plus any use it cannot see (spend before
    # metering shipped); ICEMAN is not metered, so size the budget to this
    # pipeline's share.
    imagery.BUDGET.seed(
        month_to_date_nearmap_bytes() + int(env_float("NEARMAP_PRIOR_USE_MB", 0) * 1024 * 1024)
    )
    if verbose:
        progress.step(imagery.BUDGET.describe())

    if verbose:
        progress.stage(
            "START",
            f"offset={offset} | limit={limit!s} | "
            f"db_only={str(db_only).lower()} | "
            f"confirm_rooftop={str(confirm_rooftop).lower()} | "
            f"confirm_existing={str(confirm_existing).lower()} | "
            f"connectx_audit={str(connectx_audit).lower()} | "
            f"stages={','.join(stages or list(DEFAULT_STAGE_FILTER))} | "
            f"workers={workers} | run_dir={run_dir.name}"
            + (f" | reuse_chips={len(reuse_chips_dirs)}" if reuse_chips_dirs else ""),
        )

    own_sql = False
    if confirm_rooftop:
        sql_connection = None
        if classify_fn is None:
            def classify(**kwargs):
                return classify_site_imagery(presence_only=True, **kwargs)
        else:
            classify = classify_fn
    else:
        if sql_connection is None:
            if verbose:
                progress.stage("1/4 CONNECT SQL")
            with progress.busy("connecting SQL") if not verbose else nullcontext():
                sql_connection = connect_mssql()
            own_sql = True
            if verbose:
                progress.result("connected")
        classify = None if db_only else (classify_fn or classify_site_imagery)

    sql_state: dict[str, Any] = {"conn": sql_connection, "cursor": None}
    sink: SiteSink | None = None
    state: _RunState | None = None
    try:
        if sites is None:
            sites = _query_queue(
                sf_client,
                site_ids=site_ids,
                stages=stages,
                owners=owners,
                exclude_owners=exclude_owners,
                carrier_like=carrier_like,
                metro_classification=metro_classification,
                states=states,
                llm_classified=llm_classified,
                site_type=site_type,
                verbose=verbose,
                connectx_audit=connectx_audit,
                audit_pool=audit_pool,
                audit_llm_classified=audit_llm_classified,
            )
            if verbose:
                progress.result(f"{len(sites)} site(s)")
        queued = len(sites)
        sites = apply_queue_window(sites, offset=offset, limit=limit, skip_ids=skip_ids)
        if verbose and (offset or limit is not None or skip_ids):
            skip_n = len({str(s).strip() for s in (skip_ids or []) if str(s).strip()})
            extras = [
                label
                for label, on in (
                    (f"skip_from={skip_n} id(s)", skip_n),
                    (f"offset={offset}", offset),
                    (f"limit={limit}", limit is not None),
                )
                if on
            ]
            suffix = f" ({', '.join(extras)})" if extras else ""
            progress.step(f"processing {len(sites)} of {queued}{suffix}")

        sql_state["cursor"] = sql_connection.cursor() if sql_connection is not None else None
        leave_failures = confirm_rooftop or confirm_existing or connectx_audit
        if apply:
            sink = SiteSink(run_dir.name)
            sink.begin({"sites": len(sites), "apply_enabled": 1})
        state = _RunState(
            sf_client=sf_client,
            run_dir=run_dir,
            apply=apply,
            verbose=verbose,
            total=len(sites),
            eligible=lambda row: _row_eligible_for_apply(
                row,
                dequeue_holdouts=dequeue_holdouts,
                db_only=db_only,
                confirm_rooftop=confirm_rooftop,
                confirm_existing=confirm_existing,
                connectx_audit=connectx_audit,
            ),
            write_holdout=not db_only and not leave_failures,
            error_holdout=not leave_failures,
            sink=sink,
            compact=workers > 1,
            decorate=(
                (
                    lambda row: stamp_audit_verdict(
                        row,
                        run_id=run_dir.name,
                        assign_confirmed_to=audit_assign_to,
                    )
                )
                if connectx_audit
                else None
            ),
        )

        prefetch_sites = [
            site
            for site in sites
            if not _use_rooftop_confirm(
                site, confirm_rooftop=confirm_rooftop, confirm_existing=confirm_existing
            )
        ]
        if verbose and prefetch_sites:
            progress.stage("PREPARE", f"{len(prefetch_sites)} site(s): geocode + FCC/TowerSource")
        geocodes = _prefetch_geocodes(prefetch_sites, verbose=verbose)
        proximity = _prefetch_proximity(
            prefetch_sites, geocodes, sql_state, max_m=max_m, verbose=verbose
        )

        pending: list[tuple[int, PreparedSite]] = []
        classify_error: BaseException | None = None
        try:
            for index, site in enumerate(sites, start=1):
                sf_id = str(site.get("Id") or "")
                t0 = time.monotonic()
                if verbose:
                    progress.stage(
                        f"3/4 SITE {index}/{len(sites)} PREPARE",
                        f"{sf_id} | {progress.format_site_address(site)}".strip(" |"),
                    )
                prep = _prepare_site(
                    site,
                    cursor=sql_state.get("cursor"),
                    sql_state=sql_state,
                    max_m=max_m,
                    skip_classify=skip_classify,
                    db_only=db_only,
                    confirm_rooftop=confirm_rooftop,
                    confirm_existing=confirm_existing,
                    verbose=verbose,
                    geocodes=geocodes,
                    proximity=proximity.get(sf_id, _NOT_PREFETCHED),
                )
                if prep.done:
                    state.finish(prep.base, elapsed_s=time.monotonic() - t0)
                else:
                    pending.append((index, prep))
            _classify_all(
                pending,
                state,
                workers=workers,
                classify_kwargs=dict(
                    classify_fn=classify,
                    chip_dir=chip_dir,
                    cluster_cache=[],
                    reuse_chips_dirs=reuse_chips_dirs,
                ),
            )
        except KeyboardInterrupt:
            if verbose:
                progress.warn(
                    f"stopped after {len(state.detail_rows)} classified site(s) — "
                    "flushing any remaining Salesforce updates"
                )
        except Exception as exc:  # noqa: BLE001
            classify_error = exc
            if verbose:
                progress.warn(
                    f"classify stopped after {len(state.detail_rows)} site(s): {exc} — "
                    "flushing any remaining Salesforce updates"
                )
        with _hold_interrupts():
            state.close()
            if sink is not None:
                sink.close()
            summary = _write_and_apply_run(
                sf_client=sf_client,
                run_dir=run_dir,
                detail_rows=state.detail_rows,
                apply=apply,
                dequeue_holdouts=dequeue_holdouts,
                db_only=db_only,
                confirm_rooftop=confirm_rooftop,
                confirm_existing=confirm_existing,
                connectx_audit=connectx_audit,
                verbose=verbose,
            )
        if classify_error is not None:
            raise classify_error
        return summary
    finally:
        if sink is not None:
            sink.close()
        if own_sql:
            try:
                conn = sql_state.get("conn") or sql_connection
                if conn is not None:
                    conn.close()
            except Exception:  # pragma: no cover
                pass
