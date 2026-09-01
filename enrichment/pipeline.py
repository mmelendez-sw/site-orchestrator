"""Enrichment pipeline: SF blank Site_Type → FCC/TowerSource → imagery classify → CSVs."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from enrichment.bucketing import (
    bucket_classification,
    imagery_bucket,
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
)
from enrichment.geo import (
    build_site_address,
    geocode_census,
    haversine_meters,
    pin_address_is_mismatch,
    should_compare_rooftop_hosts,
)
from enrichment.cost_policy import (
    find_cluster_match,
    should_auto_skip_classify,
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
    apply_updates_idempotent,
    is_enrichment_payload,
    parse_sf_lat_lng,
    query_blank_site_type_sites,
    query_sites_by_ids,
)
from paths import runs_dir


logger = logging.getLogger(__name__)


def default_run_dir(root: Path | None = None) -> Path:
    from paths import ensure_data_layout

    ensure_data_layout()
    base = root or runs_dir()
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    return base / f"{stamp}_sf_enrichment"


def run_enrichment(
    *,
    sf_client,
    sql_connection=None,
    run_dir: Path,
    limit: int | None = None,
    max_m: float = PROXIMITY_MAX_M,
    skip_classify: bool = False,
    classify_fn: Callable[..., dict[str, Any]] | None = None,
    sites: list[dict[str, Any]] | None = None,
    site_ids: list[str] | None = None,
    carrier_like: str | None = None,
    metro_classification: str | None = "Major NFL Metro",
    states: list[str] | None = None,
    stages: list[str] | None = None,
    llm_classified: bool = False,
    verbose: bool = True,
    apply: bool = True,
    dequeue_holdouts: bool = True,
) -> dict[str, Any]:
    """Run proximity + NAIP/Nearmap/Claude enrichment, write CSVs, auto-apply.

    Certain rooftops and high-conf towers become Salesforce updates
    (LLM_Classified=true, LLM_Holdout=false). Remaining rows dequeue
    (LLM_Classified=false, LLM_Holdout=true) unless ``dequeue_holdouts``
    is false — then holdouts are left untouched.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    chip_dir = run_dir / "chips"
    progress.reset_run_timer()

    if verbose:
        progress.stage(
            "START",
            f"limit={limit!s} | "
            f"stages={','.join(stages or list(DEFAULT_STAGE_FILTER))} | "
            f"run_dir={run_dir.name}",
        )

    own_sql = False
    if sql_connection is None:
        if verbose:
            progress.stage("1/4 CONNECT SQL")
        sql_connection = connect_mssql()
        own_sql = True
        if verbose:
            progress.result("connected")

    classify = classify_fn or classify_site_imagery

    try:
        if sites is None:
            if site_ids:
                if verbose:
                    progress.stage(
                        "2/4 QUERY SALESFORCE",
                        f"{len(site_ids)} explicit Id(s)",
                    )
                sites = query_sites_by_ids(sf_client, site_ids)
            else:
                state_label = ",".join(states) if states else "all"
                stage_filter = stages or list(DEFAULT_STAGE_FILTER)
                stage_label = ",".join(stage_filter)
                if verbose:
                    progress.stage(
                        "2/4 QUERY SALESFORCE",
                        f"blank Site_Type | stages={stage_label} | "
                        f"carrier_like={carrier_like!r} | "
                        f"metro={metro_classification!r} | "
                        f"llm_classified={str(llm_classified).lower()} | "
                        f"states={state_label}",
                    )
                sites = query_blank_site_type_sites(
                    sf_client,
                    stages=stage_filter,
                    carrier_like=carrier_like,
                    metro_classification=metro_classification,
                    states=states,
                    llm_classified=llm_classified,
                )
            if verbose:
                progress.result(f"{len(sites)} site(s)")
        if limit is not None:
            sites = sites[: max(0, limit)]
            if verbose:
                progress.step(f"processing first {len(sites)}")

        detail_rows: list[dict[str, Any]] = []
        cursor = sql_connection.cursor()
        cluster_cache: list[dict[str, Any]] = []

        for index, site in enumerate(sites, start=1):
            sf_id = str(site.get("Id") or "")
            address = progress.format_site_address(site)
            if verbose:
                progress.stage(
                    f"3/4 SITE {index}/{len(sites)}",
                    f"{sf_id} | {address}".strip(" |"),
                )
            else:
                progress.row_count(
                    index, len(sites), sf_id=sf_id, address=address
                )
            site_t0 = time.monotonic()
            row = _process_site(
                site,
                cursor=cursor,
                max_m=max_m,
                skip_classify=skip_classify,
                classify_fn=classify,
                chip_dir=chip_dir,
                verbose=verbose,
                cluster_cache=cluster_cache,
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

        candidates = [r for r in detail_rows if r.get("bucket") == BUCKET_POTENTIAL_UPDATE]
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
            apply_rows = list(candidates)
            if dequeue_holdouts:
                apply_rows.extend(
                    r
                    for r in detail_rows
                    if r.get("bucket") != BUCKET_POTENTIAL_UPDATE
                )
            if apply_rows:
                batch_csv = run_dir / "_apply_batch.csv"
                write_csv(batch_csv, apply_rows, DETAIL_COLUMNS)
                apply_info = apply_candidate_csv(
                    sf_client=sf_client,
                    candidate_csv=batch_csv,
                    run_dir=run_dir,
                    apply=True,
                    verbose=verbose,
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
            else:
                apply_info = {
                    "total": 0,
                    "success": 0,
                    "dequeued_holdouts": 0,
                    "failed": 0,
                    "apply": True,
                }
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
    finally:
        if own_sql:
            try:
                sql_connection.close()
            except Exception:  # pragma: no cover
                pass


def _process_site(
    site: dict[str, Any],
    *,
    cursor,
    max_m: float,
    skip_classify: bool,
    classify_fn: Callable[..., dict[str, Any]],
    chip_dir: Path,
    verbose: bool = True,
    cluster_cache: list[dict[str, Any]] | None = None,
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

    auto_skip = skip_classify or should_auto_skip_classify(hit)
    if auto_skip:
        if verbose:
            progress.step(
                "skip classify"
                if skip_classify
                else "auto skip classify (unique DB hit ≤25 m)"
            )
        if hit is not None:
            base["bucket"] = BUCKET_POTENTIAL_UPDATE
            base["holdout_reason"] = "skip_classify_db_hit"
            base["update_lat"] = classify_lat
            base["update_lng"] = classify_lng
            base["update_coord_source"] = f"db:{base['match_source']}"
            base["update_verified_site"] = True
            base["update_verified_site_source"] = verified_source_for_match(
                base["match_source"]
            )
        else:
            base["bucket"] = BUCKET_OTHER
            base["holdout_reason"] = "skip_classify_no_db_hit"
        return base

    if cluster_cache is not None and hit is None:
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
    }
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
            if is_enrichment_payload(
                payload if isinstance(payload, dict) else None
            ):
                row["sf_update_status"] = "updated"
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
    )
    out_dir = run_dir or candidate_csv.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / APPLY_LOG_CSV
    log_rows = []
    for entry in results:
        payload = entry.get("payload") or {}
        log_rows.append(
            {
                "index": entry.get("index"),
                "Id": entry.get("Id"),
                "success": entry.get("success"),
                "dry_run": entry.get("dry_run"),
                "status": entry.get("status", ""),
                "error": entry.get("error", ""),
                "payload_json": json.dumps(payload),
            }
        )
    write_csv(
        log_path,
        log_rows,
        (
            "index",
            "Id",
            "success",
            "dry_run",
            "status",
            "error",
            "payload_json",
        ),
    )
    tower_updated = sum(
        1
        for r in results
        if r.get("success") and is_enrichment_payload(r.get("payload"))
    )
    dequeued = sum(
        1
        for r in results
        if r.get("success") and not is_enrichment_payload(r.get("payload"))
    )
    failed = sum(1 for r in results if not r.get("success"))
    summary = {
        "total": len(results),
        "success": tower_updated,
        "dequeued_holdouts": dequeued,
        "failed": failed,
        "apply": apply,
        "log": str(log_path),
    }
    if verbose:
        progress.result(
            f"sf_writes={tower_updated} dequeued_holdouts={dequeued} failed={failed}"
        )
    logger.info("Apply summary: %s", summary)
    summary["results"] = results
    return summary
