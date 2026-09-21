"""Enrichment pipeline: SF blank Site_Type → FCC/TowerSource → imagery classify → Salesforce."""

from __future__ import annotations

import csv
import json
import logging
import time
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from enrichment.bucketing import (
    bucket_classification,
    imagery_bucket,
    naip_rooftop_confirm_decision,
    verified_source_for_match,
)
from enrichment.metrics import RUN_METRIC_KEYS, outcome_class, record_run
from enrichment.naip_classify import classify_site_imagery
from enrichment.constants import (
    APPLY_LOG_CSV,
    BUCKET_OTHER,
    BUCKET_POTENTIAL_UPDATE,
    BUCKET_ROOFTOP,
    CANDIDATE_CSV,
    DETAIL_CSV,
    HOLDOUT_CSV,
    MATCH_SOURCE_NONE,
    PROXIMITY_MAX_M,
    DEFAULT_STAGE_FILTER,
    HIGH_INPUT_CONFIDENCE_STAGES,
)
from enrichment.geo import (
    build_site_address,
    geocode_census,
    haversine_meters,
    pin_address_is_mismatch,
    should_compare_rooftop_hosts,
)
from enrichment.cost_policy import (
    auto_skip_classify_reason,
    find_cluster_match,
)
from enrichment.mssql import connect_mssql, describe_match, find_proximity_hit
from enrichment.outputs import (
    CANDIDATE_COLUMNS,
    DETAIL_COLUMNS,
    HOLDOUT_COLUMNS,
    write_csv,
)
from enrichment import progress
from enrichment.sf_ops import (
    apply_one_update,
    apply_updates_idempotent,
    is_enrichment_payload,
    parse_sf_lat_lng,
    query_blank_site_type_sites,
    query_sites_by_ids,
)
from salesforce.site_type_mapping import (
    is_sales_tower_type,
    site_type_from_db_asset_type,
)
from paths import runs_dir


logger = logging.getLogger(__name__)


def default_run_dir(root: Path | None = None) -> Path:
    from paths import ensure_data_layout

    ensure_data_layout()
    base = root or runs_dir()
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    return base / f"{stamp}_sf_enrichment"


_ALREADY_WRITTEN = frozenset({"updated", "classified_only", "dequeued"})


def apply_paused_run(
    *,
    sf_client,
    run_dir: Path,
    apply: bool = True,
    confirm_rooftop: bool = False,
    confirm_existing: bool = False,
    db_only: bool = False,
    dequeue_holdouts: bool = False,
    verbose: bool = True,
    max_sites: int | None = None,
) -> dict[str, Any]:
    """Apply Salesforce updates from a classify run that never reached stage 4/4.

    Reads ``enrichment_detail.csv`` (rewritten after each site). Live runs
    also Salesforce-apply after each site; use this when a job died before
    that write. Does not re-query Salesforce or re-classify. Confirm path
    writes Site_Type + LLM_Classified only for ``potential_update`` rows
    still pending.
    """
    path = run_dir / DETAIL_CSV
    if not path.is_file():
        raise FileNotFoundError(f"No {DETAIL_CSV} in {run_dir}")
    with path.open(newline="", encoding="utf-8-sig") as handle:
        detail_rows = list(csv.DictReader(handle))
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
) -> dict[str, Any]:
    """Run proximity + NAIP/Nearmap/Claude enrichment and Salesforce-apply.

    Each classified site is written to ``enrichment_detail.csv`` and, when
    ``apply`` is true, pushed to Salesforce before the next site. An
    end-of-run sweep applies any rows still pending.

    Certain rooftops and high-conf towers become Salesforce updates
    (LLM_Classified=true, LLM_Holdout=false). Remaining rows dequeue
    (LLM_Classified=false, LLM_Holdout=true) unless ``dequeue_holdouts``
    is false — then holdouts are left untouched.

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
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    chip_dir = run_dir / "chips"
    progress.reset_run_timer()

    if verbose:
        progress.stage(
            "START",
            f"offset={offset} | limit={limit!s} | "
            f"db_only={str(db_only).lower()} | "
            f"confirm_rooftop={str(confirm_rooftop).lower()} | "
            f"confirm_existing={str(confirm_existing).lower()} | "
            f"stages={','.join(stages or list(DEFAULT_STAGE_FILTER))} | "
            f"run_dir={run_dir.name}"
            + (
                f" | reuse_chips={len(reuse_chips_dirs)}"
                if reuse_chips_dirs
                else ""
            ),
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

    try:
        if sites is None:
            if site_ids:
                if verbose:
                    progress.stage(
                        "2/4 QUERY SALESFORCE",
                        f"{len(site_ids)} explicit Id(s)",
                    )
                with progress.busy("querying Salesforce") if not verbose else nullcontext():
                    sites = query_sites_by_ids(sf_client, site_ids)
            else:
                state_label = ",".join(states) if states else "all"
                stage_filter = stages or list(DEFAULT_STAGE_FILTER)
                stage_label = ",".join(stage_filter)
                owner_filter = owners
                owner_label = ",".join(owner_filter) if owner_filter else "any"
                exclude_owner_label = (
                    ",".join(exclude_owners) if exclude_owners else "none"
                )
                wanted_type = (site_type or "").strip()
                omit_type = wanted_type.lower() in {"any", "all", "*", "none"}
                if omit_type:
                    queue_label = "any Site_Type"
                    query_site_type = "any"
                elif wanted_type:
                    queue_label = f"Site_Type={wanted_type}"
                    query_site_type = wanted_type
                else:
                    queue_label = "blank Site_Type"
                    query_site_type = None
                if verbose:
                    progress.stage(
                        "2/4 QUERY SALESFORCE",
                        f"{queue_label} | stages={stage_label} | "
                        f"owners={owner_label} | "
                        f"exclude_owners={exclude_owner_label} | "
                        f"carrier_like={carrier_like!r} | "
                        f"metro={metro_classification!r} | "
                        f"llm_classified={str(llm_classified).lower()} | "
                        f"states={state_label}",
                    )
                with progress.busy("querying Salesforce") if not verbose else nullcontext():
                    sites = query_blank_site_type_sites(
                        sf_client,
                        stages=stage_filter,
                        owners=owner_filter,
                        exclude_owners=exclude_owners,
                        carrier_like=carrier_like,
                        metro_classification=metro_classification,
                        states=states,
                        llm_classified=llm_classified,
                        site_type=query_site_type,
                    )
            if verbose:
                progress.result(f"{len(sites)} site(s)")
        queued = len(sites)
        sites = apply_queue_window(
            sites, offset=offset, limit=limit, skip_ids=skip_ids
        )
        if verbose and (offset or limit is not None or skip_ids):
            skip_n = len({str(s).strip() for s in (skip_ids or []) if str(s).strip()})
            extras = []
            if skip_n:
                extras.append(f"skip_from={skip_n} id(s)")
            if offset:
                extras.append(f"offset={offset}")
            if limit is not None:
                extras.append(f"limit={limit}")
            suffix = f" ({', '.join(extras)})" if extras else ""
            progress.step(f"processing {len(sites)} of {queued}{suffix}")

        detail_rows: list[dict[str, Any]] = []
        cursor = sql_connection.cursor() if sql_connection is not None else None
        cluster_cache: list[dict[str, Any]] = []
        leave_failures = confirm_rooftop or confirm_existing
        write_holdout = not db_only and not leave_failures
        error_holdout = not leave_failures
        classify_error: BaseException | None = None

        try:
            for index, site in enumerate(sites, start=1):
                sf_id = str(site.get("Id") or "")
                address = progress.format_site_address(site)
                if verbose:
                    progress.stage(
                        f"3/4 SITE {index}/{len(sites)}",
                        f"{sf_id} | {address}".strip(" |"),
                    )
                site_status = (
                    progress.busy(
                        f"[{index}/{len(sites)}] {sf_id.strip() or '-'} | "
                        f"{address.strip() or '-'}"
                    )
                    if not verbose
                    else nullcontext()
                )
                with site_status:
                    site_t0 = time.monotonic()
                    row = _process_site(
                        site,
                        cursor=cursor,
                        max_m=max_m,
                        skip_classify=skip_classify,
                        db_only=db_only,
                        classify_fn=classify,
                        chip_dir=chip_dir,
                        verbose=verbose,
                        cluster_cache=cluster_cache,
                        reuse_chips_dirs=reuse_chips_dirs,
                        confirm_rooftop=confirm_rooftop,
                        confirm_existing=confirm_existing,
                    )
                    row["outcome_class"] = outcome_class(row)
                    site_elapsed = time.monotonic() - site_t0
                    row.setdefault("sf_update_status", "")
                    row.setdefault("sf_update_error", "")
                    if row.get("bucket") == BUCKET_POTENTIAL_UPDATE:
                        row["sf_update_status"] = "pending"
                    else:
                        row["sf_update_status"] = "skipped"

                    detail_rows.append(row)
                    if apply and _row_eligible_for_apply(
                        row,
                        dequeue_holdouts=dequeue_holdouts,
                        db_only=db_only,
                        confirm_rooftop=confirm_rooftop,
                        confirm_existing=confirm_existing,
                    ):
                        entry = apply_one_update(
                            sf_client,
                            row,
                            dry_run=False,
                            verbose=verbose,
                            write_holdout=write_holdout,
                            error_holdout=error_holdout,
                        )
                        stamp_apply_status([row], [entry])
                        _append_apply_log(run_dir, [entry])
                    if verbose:
                        progress.result(
                            f"{row.get('bucket')} | type={row.get('naip_site_type') or '—'} | "
                            f"img={row.get('imagery_used') or '—'} | "
                            f"tier={row.get('nearmap_tier') or '—'} | "
                            f"ai={row.get('escalation_model') or row.get('primary_model') or '—'} | "
                            f"src={row.get('update_verified_site_source') or '—'} | "
                            f"sf={row.get('sf_update_status') or '—'}",
                            elapsed_s=site_elapsed,
                        )
                    write_csv(run_dir / DETAIL_CSV, detail_rows, DETAIL_COLUMNS)
        except KeyboardInterrupt:
            if verbose:
                progress.warn(
                    f"stopped after {len(detail_rows)} classified site(s) — "
                    "flushing any remaining Salesforce updates"
                )
        except Exception as exc:
            classify_error = exc
            if verbose:
                progress.warn(
                    f"classify stopped after {len(detail_rows)} site(s): {exc} — "
                    "flushing any remaining Salesforce updates"
                )

        summary = _write_and_apply_run(
            sf_client=sf_client,
            run_dir=run_dir,
            detail_rows=detail_rows,
            apply=apply,
            dequeue_holdouts=dequeue_holdouts,
            db_only=db_only,
            confirm_rooftop=confirm_rooftop,
            confirm_existing=confirm_existing,
            verbose=verbose,
        )
        if classify_error is not None:
            raise classify_error
        return summary
    finally:
        if own_sql:
            try:
                sql_connection.close()
            except Exception:  # pragma: no cover
                pass


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
) -> list[dict[str, Any]]:
    apply_rows = [
        row
        for row in detail_rows
        if row.get("bucket") == BUCKET_POTENTIAL_UPDATE and _needs_sf_apply(row)
    ]
    leave_failures = confirm_rooftop or confirm_existing
    if not leave_failures and (dequeue_holdouts or db_only):
        apply_rows.extend(
            row
            for row in detail_rows
            if row.get("bucket") != BUCKET_POTENTIAL_UPDATE and _needs_sf_apply(row)
        )
    return apply_rows


def _row_eligible_for_apply(
    row: dict[str, Any],
    *,
    dequeue_holdouts: bool,
    db_only: bool,
    confirm_rooftop: bool,
    confirm_existing: bool,
) -> bool:
    return bool(
        _collect_apply_rows(
            [row],
            dequeue_holdouts=dequeue_holdouts,
            db_only=db_only,
            confirm_rooftop=confirm_rooftop,
            confirm_existing=confirm_existing,
        )
    )


def _apply_info_from_detail(
    detail_rows: list[dict[str, Any]],
    *,
    apply: bool,
    log: str = "",
) -> dict[str, Any]:
    def _status(row: dict[str, Any]) -> str:
        return str(row.get("sf_update_status") or "").strip().lower()

    success = sum(1 for row in detail_rows if _status(row) == "updated")
    dequeued = sum(1 for row in detail_rows if _status(row) == "dequeued")
    classified_only = sum(
        1 for row in detail_rows if _status(row) == "classified_only"
    )
    failed = sum(1 for row in detail_rows if _status(row) == "failed")
    return {
        "total": success + dequeued + classified_only + failed,
        "success": success,
        "dequeued_holdouts": dequeued,
        "classified_only": classified_only,
        "failed": failed,
        "apply": apply,
        "log": log,
    }


def _append_apply_log(run_dir: Path, results: list[dict[str, Any]]) -> Path:
    log_path = run_dir / APPLY_LOG_CSV
    existing: list[dict[str, Any]] = []
    if log_path.is_file():
        with log_path.open(newline="", encoding="utf-8-sig") as handle:
            existing = list(csv.DictReader(handle))
    start = len(existing)
    new_rows = []
    for offset, entry in enumerate(results):
        payload = entry.get("payload") or {}
        new_rows.append(
            {
                "index": entry.get("index") or start + offset + 1,
                "Id": entry.get("Id"),
                "success": entry.get("success"),
                "dry_run": entry.get("dry_run"),
                "status": entry.get("status", ""),
                "error": entry.get("error", ""),
                "payload_json": json.dumps(payload),
            }
        )
    write_csv(log_path, existing + new_rows, _APPLY_LOG_COLUMNS)
    return log_path


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
    verbose: bool,
) -> dict[str, Any]:
    """Write run CSVs and optionally apply Salesforce updates."""
    candidates = [
        r for r in detail_rows if r.get("bucket") == BUCKET_POTENTIAL_UPDATE
    ]
    holdouts = [
        r
        for r in detail_rows
        if r.get("bucket") in {BUCKET_ROOFTOP, BUCKET_OTHER}
    ]
    if verbose:
        progress.stage("4/4 WRITE CSVs", str(run_dir.name))
    write_csv(run_dir / CANDIDATE_CSV, candidates, CANDIDATE_COLUMNS)
    write_csv(run_dir / HOLDOUT_CSV, holdouts, HOLDOUT_COLUMNS)
    if verbose:
        progress.result(
            f"updates={len(candidates)} holdouts={len(holdouts)} total={len(detail_rows)}"
        )

    apply_info: dict[str, Any] | None = None
    if apply:
        apply_rows = _collect_apply_rows(
            detail_rows,
            dequeue_holdouts=dequeue_holdouts,
            db_only=db_only,
            confirm_rooftop=confirm_rooftop,
            confirm_existing=confirm_existing,
        )
        leave_failures = confirm_rooftop or confirm_existing
        if apply_rows:
            batch_csv = run_dir / "_apply_batch.csv"
            write_csv(batch_csv, apply_rows, DETAIL_COLUMNS)
            apply_info = apply_candidate_csv(
                sf_client=sf_client,
                candidate_csv=batch_csv,
                run_dir=run_dir,
                apply=True,
                verbose=verbose,
                write_holdout=not db_only and not leave_failures,
                error_holdout=not leave_failures,
            )
            stamp_apply_status(
                detail_rows, apply_info.pop("results", None) or []
            )
            write_csv(run_dir / DETAIL_CSV, detail_rows, DETAIL_COLUMNS)
            write_csv(
                run_dir / CANDIDATE_CSV,
                [
                    r
                    for r in detail_rows
                    if r.get("bucket") == BUCKET_POTENTIAL_UPDATE
                ],
                CANDIDATE_COLUMNS,
            )
        apply_info = _apply_info_from_detail(
            detail_rows,
            apply=True,
            log=str(run_dir / APPLY_LOG_CSV),
        )
    else:
        for row in detail_rows:
            if row.get("sf_update_status") == "pending":
                row["sf_update_status"] = "dry_run"
        write_csv(run_dir / DETAIL_CSV, detail_rows, DETAIL_COLUMNS)
        write_csv(
            run_dir / CANDIDATE_CSV,
            [
                r
                for r in detail_rows
                if r.get("bucket") == BUCKET_POTENTIAL_UPDATE
            ],
            CANDIDATE_COLUMNS,
        )
    run_block: dict[str, Any] = {
        "sites": len(detail_rows),
        "applied_rooftop": sum(
            1 for r in detail_rows if r.get("outcome_class") == "applied_rooftop"
        ),
        "applied_tower": sum(
            1 for r in detail_rows if r.get("outcome_class") == "applied_tower"
        ),
        "sf_writes": (apply_info or {}).get("success", 0) if apply else 0,
        "sf_holdouts_dequeued": (apply_info or {}).get("dequeued_holdouts", 0)
        if apply
        else 0,
        "sf_write_failed": (apply_info or {}).get("failed", 0) if apply else 0,
    }
    kpis_block: dict[str, Any] | None = None
    try:
        metrics_snap = record_run(
            run_dir=run_dir,
            detail_rows=detail_rows,
            apply_summary=apply_info if apply else None,
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
    (run_dir / "summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    if verbose:
        progress.dump_summary(summary)
    return summary


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
) -> dict[str, Any]:
    sf_id = str(site.get("Id") or "")
    coords = parse_sf_lat_lng(site)
    base = {
        "Id": sf_id,
        "sf_lat": "",
        "sf_lng": "",
        "Site_Street__c": site.get("Site_Street__c") or "",
        "Site_City__c": site.get("Site_City__c") or "",
        "Site_State__c": site.get("Site_State__c") or "",
        "Site_Zip_Code__c": site.get("Site_Zip_Code__c") or "",
        "Stage__c": site.get("Stage__c") or "",
        "Owner__c": site.get("Owner__c") or "",
        "Carrier_Leasing_Source__c": site.get("Carrier_Leasing_Source__c") or "",
        "Metro_Classification__c": site.get("Metro_Classification__c") or "",
        "match_source": MATCH_SOURCE_NONE,
        "match_distance_m": "",
        "match_selection_reason": "",
        "match_candidate_count": "",
        "match_runner_up_gap_m": "",
        "match_record_id": "",
        "match_asr_number": "",
        "match_asset_type": "",
        "classify_lat": "",
        "classify_lng": "",
        "classify_coord_source": "",
        "address_query": "",
        "address_lat": "",
        "address_lng": "",
        "address_geocode_source": "",
        "address_matched": "",
        "pin_address_offset_m": "",
        "pin_address_mismatch": "",
        "naip_site_type": "",
        "naip_tower_subtype": "",
        "naip_site_confidence": "",
        "naip_cell_equipment": "",
        "cell_equipment_confidence": "",
        "cell_equipment_evidence": "",
        "cell_gear_kind": "",
        "site_evidence": "",
        "gemini_cell_equipment": "",
        "claude_cell_equipment": "",
        "cell_models_agree": "",
        "dual_model_resolution": "",
        "classification_stage": "",
        "nearmap_tier": "",
        "nearmap_views": "",
        "imagery_used": "",
        "primary_model": "",
        "escalation_model": "",
        "escalation_reason": "",
        "naip_screen_site_type": "",
        "naip_screen_site_confidence": "",
        "naip_screen_cell_equipment": "",
        "second_nearmap": "",
        "outcome_class": "",
        "asset_lat": "",
        "asset_lon": "",
        "asset_offset_m": "",
        "asset_coord_source": "",
        "bucket": BUCKET_OTHER,
        "holdout_reason": "",
        "update_lat": "",
        "update_lng": "",
        "update_coord_source": "",
        "update_site_type": "",
        "update_verified_site": "",
        "update_verified_site_source": "",
        "sf_update_status": "",
        "sf_update_error": "",
        "error": "",
    }

    rooftop_confirm = _use_rooftop_confirm(
        site,
        confirm_rooftop=confirm_rooftop,
        confirm_existing=confirm_existing,
    )

    if coords is None:
        if verbose:
            progress.warn("Missing Salesforce lat/lng — skipping proximity/NAIP")
        base["bucket"] = BUCKET_OTHER
        base["holdout_reason"] = "missing_sf_coordinates"
        base["error"] = "missing_sf_coordinates"
        return base

    sf_lat, sf_lng = coords
    base["sf_lat"] = sf_lat
    base["sf_lng"] = sf_lng
    if verbose:
        progress.step(f"SF pin: {sf_lat:.6f}, {sf_lng:.6f}")

    hit = None
    db_lat = db_lng = None
    addr_lat = addr_lng = None
    classify_lat, classify_lng = sf_lat, sf_lng
    base["classify_lat"] = classify_lat
    base["classify_lng"] = classify_lng
    base["classify_coord_source"] = "sf_pin"

    if rooftop_confirm:
        if verbose:
            progress.step(
                "NAIP rooftop confirm (Nearmap only if NAIP does not confirm)"
            )
    if not rooftop_confirm:
        if verbose:
            progress.stage("PROXIMITY", f"≤{max_m:g} m")
        addr_lat = None
        addr_lng = None
        address_query = build_site_address(site)
        if address_query:
            base["address_query"] = address_query
            geo = geocode_census(address_query)
            if geo:
                addr_lat, addr_lng = geo["lat"], geo["lng"]
                base["address_lat"] = addr_lat
                base["address_lng"] = addr_lng
                base["address_geocode_source"] = geo.get("source") or "census"
                base["address_matched"] = geo.get("matched") or ""
                offset_m = haversine_meters(sf_lat, sf_lng, addr_lat, addr_lng)
                base["pin_address_offset_m"] = round(offset_m, 1)
                mismatch = pin_address_is_mismatch(offset_m)
                base["pin_address_mismatch"] = mismatch
                if verbose:
                    extra = ""
                    if mismatch:
                        extra = " — far mismatch, pick pin vs Census before Nearmap"
                    elif should_compare_rooftop_hosts(offset_m, db_backed=False):
                        extra = " — rooftop host compare if no FCC/TS hit"
                    progress.result(
                        f"Census address {offset_m:.0f} m from pin{extra}"
                    )
        try:
            hit = find_proximity_hit(
                cursor,
                sf_lat,
                sf_lng,
                max_m=max_m,
                address_lat=addr_lat,
                address_lng=addr_lng,
            )
        except Exception as exc:  # noqa: BLE001
            if verbose:
                progress.warn(f"SQL proximity failed: {exc}")
            base["error"] = f"sql_proximity_failed: {exc}"
            base["holdout_reason"] = "sql_error"
            return base

        if hit is not None:
            base["match_source"] = describe_match(hit)
            base["match_distance_m"] = round(hit.distance_m, 2)
            base["match_selection_reason"] = hit.selection_reason or ""
            base["match_candidate_count"] = (
                hit.candidate_count if hit.candidate_count is not None else ""
            )
            base["match_runner_up_gap_m"] = (
                hit.runner_up_gap_m if hit.runner_up_gap_m is not None else ""
            )
            base["match_record_id"] = hit.record_id or ""
            base["match_asr_number"] = hit.asr_number or ""
            base["match_asset_type"] = hit.asset_type or ""
            classify_lat, classify_lng = hit.latitude, hit.longitude
            db_lat, db_lng = hit.latitude, hit.longitude
            base["classify_coord_source"] = f"db:{base['match_source']}"
            if verbose:
                reason = hit.selection_reason or "nearest"
                progress.result(
                    f"{base['match_source']} @ {hit.distance_m:.1f} m ({reason})"
                )
        else:
            db_lat = db_lng = None
            classify_lat, classify_lng = sf_lat, sf_lng
            base["classify_coord_source"] = "sf_pin"
            if verbose:
                if should_compare_rooftop_hosts(
                    base.get("pin_address_offset_m") or None, db_backed=False
                ):
                    progress.result(
                        "no tower DB hit → rooftop path "
                        "(pick host building before Nearmap)"
                    )
                else:
                    progress.result("no DB hit → classify on SF pin")

        base["classify_lat"] = classify_lat
        base["classify_lng"] = classify_lng

    if not rooftop_confirm:
        skip_reason = auto_skip_classify_reason(hit, force=db_only)
        if db_only:
            if skip_reason is not None and hit is not None:
                if verbose:
                    progress.step(f"db-only unique hit ({skip_reason})")
                return _stamp_unique_db_update(base, hit, classify_lat, classify_lng)
            if verbose:
                progress.step("db-only — no unique FCC/TowerSource hit (no imagery)")
            base["bucket"] = BUCKET_OTHER
            base["holdout_reason"] = "db_only_no_unique_hit"
            return base

        auto_skip = skip_classify or skip_reason is not None
        if auto_skip:
            if verbose:
                progress.step(
                    "skip classify"
                    if skip_classify
                    else f"auto skip classify ({skip_reason})"
                )
            if hit is not None:
                return _stamp_unique_db_update(base, hit, classify_lat, classify_lng)
            base["bucket"] = BUCKET_OTHER
            base["holdout_reason"] = "skip_classify_no_db_hit"
            return base

    if cluster_cache is not None and hit is None and not rooftop_confirm:
        reuse = find_cluster_match(
            cluster_cache, float(classify_lat), float(classify_lng)
        )
        if reuse is not None:
            classified = dict(reuse["classified"])
            classified["classification_stage"] = "cluster_reuse"
            if verbose:
                progress.step(
                    f"reuse nearby pin ({reuse.get('lat')}, {reuse.get('lon')})"
                )
            base["naip_site_type"] = classified.get("site_type") or ""
            base["naip_tower_subtype"] = classified.get("tower_subtype") or ""
            base["naip_site_confidence"] = classified.get("site_confidence") or ""
            base["naip_cell_equipment"] = classified.get("cell_equipment")
            base["cell_equipment_confidence"] = (
                classified.get("cell_equipment_confidence") or ""
            )
            base["cell_equipment_evidence"] = (
                classified.get("cell_equipment_evidence") or ""
            )
            base["cell_gear_kind"] = classified.get("cell_gear_kind") or ""
            base["site_evidence"] = classified.get("site_evidence") or ""
            base["cell_models_agree"] = classified.get("cell_models_agree", "")
            base["dual_model_resolution"] = classified.get("dual_model_resolution") or ""
            base["classification_stage"] = "cluster_reuse"
            base["nearmap_tier"] = classified.get("nearmap_tier") or ""
            base["nearmap_views"] = classified.get("nearmap_views") or ""
            base["imagery_used"] = imagery_bucket(classified)
            base["primary_model"] = classified.get("primary_model") or ""
            base["escalation_model"] = classified.get("escalation_model") or ""
            base["asset_box_2d"] = classified.get("asset_box_2d") or ""
            base["asset_view"] = classified.get("asset_view") or ""
            decision = bucket_classification(
                match_source=base["match_source"],
                classified=classified,
                db_lat=db_lat,
                db_lng=db_lng,
                sf_lat=sf_lat,
                sf_lng=sf_lng,
            )
            base.update(decision)
            cluster_cache.append(
                {
                    "lat": float(classify_lat),
                    "lon": float(classify_lng),
                    "classified": classified,
                }
            )
            return base

    if verbose:
        progress.stage("CLASSIFY")
    classify_kwargs = {
        "site_id": sf_id,
        "lat": float(classify_lat),
        "lon": float(classify_lng),
        "chip_dir": chip_dir,
        "verbose": verbose,
        "pin_lat": float(sf_lat),
        "pin_lon": float(sf_lng),
        "address_lat": addr_lat,
        "address_lon": addr_lng,
        "pin_address_offset_m": (
            float(base["pin_address_offset_m"])
            if base["pin_address_offset_m"] != ""
            else None
        ),
        "pin_address_mismatch": bool(base["pin_address_mismatch"]),
        "db_backed": hit is not None,
        "input_confidence": (
            "high"
            if str(site.get("Stage__c") or "").strip() in HIGH_INPUT_CONFIDENCE_STAGES
            else "medium"
        ),
        "reuse_chips_dirs": reuse_chips_dirs or None,
    }
    if rooftop_confirm:
        classify_kwargs["presence_only"] = True
    try:
        classified = classify_fn(**classify_kwargs)
    except TypeError:
        # Test doubles / older classify_fn may not accept pin_* kwargs.
        try:
            classified = classify_fn(
                site_id=sf_id,
                lat=float(classify_lat),
                lon=float(classify_lng),
                chip_dir=chip_dir,
                verbose=verbose,
                db_backed=hit is not None,
            )
        except TypeError:
            classified = classify_fn(
                site_id=sf_id,
                lat=float(classify_lat),
                lon=float(classify_lng),
                chip_dir=chip_dir,
            )
    except Exception as exc:  # noqa: BLE001
        if verbose:
            progress.warn(f"classify failed: {exc}")
        base["error"] = f"classify_failed: {exc}"
        base["bucket"] = BUCKET_OTHER
        base["holdout_reason"] = "classify_error"
        return base

    base["naip_site_type"] = classified.get("site_type") or ""
    base["naip_tower_subtype"] = classified.get("tower_subtype") or ""
    base["naip_site_confidence"] = classified.get("site_confidence") or ""
    base["naip_cell_equipment"] = classified.get("cell_equipment")
    base["cell_equipment_confidence"] = (
        classified.get("cell_equipment_confidence") or ""
    )
    base["cell_equipment_evidence"] = classified.get("cell_equipment_evidence") or ""
    base["cell_gear_kind"] = classified.get("cell_gear_kind") or ""
    base["site_evidence"] = classified.get("site_evidence") or ""
    base["gemini_cell_equipment"] = classified.get("gemini_cell_equipment", "")
    base["claude_cell_equipment"] = classified.get("claude_cell_equipment", "")
    base["cell_models_agree"] = classified.get("cell_models_agree", "")
    base["dual_model_resolution"] = classified.get("dual_model_resolution") or ""
    base["classification_stage"] = classified.get("classification_stage") or ""
    base["nearmap_tier"] = classified.get("nearmap_tier") or ""
    base["nearmap_views"] = classified.get("nearmap_views") or ""
    base["imagery_used"] = imagery_bucket(classified)
    base["primary_model"] = classified.get("primary_model") or ""
    base["escalation_model"] = classified.get("escalation_model") or ""
    base["escalation_reason"] = classified.get("escalation_reason") or ""
    base["naip_screen_site_type"] = classified.get("naip_screen_site_type") or ""
    base["naip_screen_site_confidence"] = (
        classified.get("naip_screen_site_confidence") or ""
    )
    base["naip_screen_cell_equipment"] = classified.get(
        "naip_screen_cell_equipment"
    )
    base["second_nearmap"] = classified.get("second_nearmap") or ""
    base["asset_lat"] = classified.get("asset_lat") or ""
    base["asset_lon"] = classified.get("asset_lon") or ""
    base["asset_offset_m"] = classified.get("asset_offset_m") or ""
    base["asset_coord_source"] = classified.get("asset_coord_source") or ""
    base["asset_box_2d"] = classified.get("asset_box_2d") or ""
    base["asset_view"] = classified.get("asset_view") or ""
    if classified.get("error"):
        base["error"] = classified.get("error")
        if classified.get("error") == "no_saved_chips":
            base["bucket"] = BUCKET_OTHER
            base["holdout_reason"] = "no_saved_chips"
            return base
    if hit is None:
        src = classified.get("classify_coord_source")
        if src:
            base["classify_coord_source"] = src
        try:
            if classified.get("lat") is not None and classified.get("lon") is not None:
                classify_lat = float(classified["lat"])
                classify_lng = float(classified["lon"])
                base["classify_lat"] = classify_lat
                base["classify_lng"] = classify_lng
        except (TypeError, ValueError):
            pass

    if rooftop_confirm:
        decision = naip_rooftop_confirm_decision(
            classified,
            existing_site_type=str(site.get("Site_Type__c") or ""),
        )
    else:
        decision = bucket_classification(
            match_source=base["match_source"],
            classified=classified,
            db_lat=db_lat,
            db_lng=db_lng,
            sf_lat=sf_lat,
            sf_lng=sf_lng,
        )
    base.update(decision)
    if (
        cluster_cache is not None
        and hit is None
        and not classified.get("error")
    ):
        cluster_cache.append(
            {
                "lat": float(classify_lat),
                "lon": float(classify_lng),
                "classified": classified,
            }
        )
    return base


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
    base["update_verified_site_source"] = verified_source_for_match(
        base["match_source"]
    )
    return base


def stamp_apply_status(
    detail_rows: list[dict[str, Any]],
    apply_results: list[dict[str, Any]] | None,
) -> None:
    """Copy Salesforce apply outcomes onto site rows before metrics snapshot.

    ``apply_candidate_csv`` writes ``sf_update_apply_log.csv`` but does not
    mutate detail rows. Without this, ``record_run`` snapshots
    ``sf_update_status=pending`` even after a live write.
    """
    by_id: dict[str, dict[str, Any]] = {}
    for entry in apply_results or []:
        sid = str(entry.get("Id") or "").strip()
        if sid:
            by_id[sid] = entry
    for row in detail_rows:
        sid = str(row.get("Id") or "").strip()
        entry = by_id.get(sid)
        if not entry:
            continue
        if entry.get("dry_run"):
            row["sf_update_status"] = "dry_run"
            row["sf_update_error"] = ""
            continue
        if entry.get("success"):
            payload = entry.get("payload")
            payload = payload if isinstance(payload, dict) else {}
            if is_enrichment_payload(payload):
                row["sf_update_status"] = "updated"
            elif payload.get("LLM_Holdout__c") is True:
                row["sf_update_status"] = "dequeued"
            elif payload.get("LLM_Classified__c"):
                row["sf_update_status"] = "classified_only"
            else:
                row["sf_update_status"] = "dequeued"
            row["sf_update_error"] = ""
        else:
            row["sf_update_status"] = "failed"
            row["sf_update_error"] = str(entry.get("error") or "")


def apply_candidate_csv(
    *,
    sf_client,
    candidate_csv: Path,
    run_dir: Path | None = None,
    apply: bool = False,
    verbose: bool = True,
    write_holdout: bool = True,
    error_holdout: bool = True,
) -> dict[str, Any]:
    """Apply enrichment rows one at a time (idempotent on failure)."""
    import csv

    with candidate_csv.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))

    if progress.run_elapsed() == 0.0:
        progress.reset_run_timer()
    if verbose:
        progress.stage(
            "APPLY SALESFORCE UPDATES" if apply else "DRY-RUN SF UPDATE PREVIEW",
            f"{len(rows)} row(s) from {candidate_csv} | "
            f"{'LIVE WRITES' if apply else 'no writes'}",
        )
    results = apply_updates_idempotent(
        sf_client,
        rows,
        dry_run=not apply,
        verbose=verbose,
        write_holdout=write_holdout,
        error_holdout=error_holdout,
    )
    out_dir = run_dir or candidate_csv.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = _append_apply_log(out_dir, results)
    tower_updated = sum(
        1
        for r in results
        if r.get("success") and is_enrichment_payload(r.get("payload"))
    )
    dequeued = sum(
        1
        for r in results
        if r.get("success")
        and not is_enrichment_payload(r.get("payload"))
        and (r.get("payload") or {}).get("LLM_Holdout__c") is True
    )
    classified_only = sum(
        1
        for r in results
        if r.get("success")
        and not is_enrichment_payload(r.get("payload"))
        and (r.get("payload") or {}).get("LLM_Holdout__c") is not True
    )
    failed = sum(1 for r in results if not r.get("success"))
    summary = {
        "total": len(results),
        "success": tower_updated,
        "dequeued_holdouts": dequeued,
        "classified_only": classified_only,
        "failed": failed,
        "apply": apply,
        "log": str(log_path),
    }
    if verbose:
        progress.result(
            f"sf_writes={tower_updated} classified_only={classified_only} "
            f"dequeued_holdouts={dequeued} failed={failed}"
        )
    logger.info("Apply summary: %s", summary)
    summary["results"] = results
    return summary
