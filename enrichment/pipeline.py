"""Enrichment pipeline: SF blank Site_Type → FCC/TowerSource → NAIP → CSVs."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from enrichment.bucketing import bucket_classification
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
)
from enrichment.mssql import connect_mssql, describe_match, find_proximity_hit
from enrichment.naip_classify import classify_naip_only
from enrichment.outputs import (
    CANDIDATE_COLUMNS,
    DETAIL_COLUMNS,
    HOLDOUT_COLUMNS,
    write_csv,
)
from enrichment import progress
from enrichment.sf_ops import (
    apply_updates_idempotent,
    parse_sf_lat_lng,
    query_blank_site_type_sites,
)

logger = logging.getLogger(__name__)


def default_run_dir(root: Path | None = None) -> Path:
    base = root or Path("runs")
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
    verbose: bool = True,
) -> dict[str, Any]:
    """Run proximity + NAIP enrichment and write candidate/holdout CSVs."""
    run_dir.mkdir(parents=True, exist_ok=True)
    chip_dir = run_dir / "chips"

    progress.stage(
        "START",
        f"limit={limit!s} | run_dir={run_dir.name}",
    )

    own_sql = False
    if sql_connection is None:
        progress.stage("1/4 CONNECT SQL")
        sql_connection = connect_mssql()
        own_sql = True
        progress.result("connected")

    classify = classify_fn or classify_naip_only

    try:
        if sites is None:
            progress.stage("2/4 QUERY SALESFORCE", "blank Site_Type + coords required")
            sites = query_blank_site_type_sites(sf_client)
            progress.result(f"{len(sites)} site(s)")
        if limit is not None:
            sites = sites[: max(0, limit)]
            progress.step(f"processing first {len(sites)}")

        detail_rows: list[dict[str, Any]] = []
        cursor = sql_connection.cursor()

        for index, site in enumerate(sites, start=1):
            sf_id = str(site.get("Id") or "")
            street = site.get("Site_Street__c") or ""
            city = site.get("Site_City__c") or ""
            progress.stage(
                f"3/4 SITE {index}/{len(sites)}",
                f"{sf_id} | {street}, {city}".strip(" |"),
            )
            row = _process_site(
                site,
                cursor=cursor,
                max_m=max_m,
                skip_classify=skip_classify,
                classify_fn=classify,
                chip_dir=chip_dir,
                verbose=verbose,
            )
            row.setdefault("sf_update_status", "")
            row.setdefault("sf_update_error", "")
            if row.get("bucket") == BUCKET_POTENTIAL_UPDATE:
                row["sf_update_status"] = "pending"
            else:
                row["sf_update_status"] = "skipped"

            detail_rows.append(row)
            progress.result(
                f"{row.get('bucket')} | match={row.get('match_source')} | "
                f"naip={row.get('naip_site_type') or '—'} | "
                f"sf={row.get('sf_update_status') or '—'}"
            )
            write_csv(run_dir / DETAIL_CSV, detail_rows, DETAIL_COLUMNS)

        progress.stage("4/4 WRITE CSVs", str(run_dir.name))
        candidates = [r for r in detail_rows if r.get("bucket") == BUCKET_POTENTIAL_UPDATE]
        holdouts = [
            r
            for r in detail_rows
            if r.get("bucket") in {BUCKET_ROOFTOP, BUCKET_OTHER}
        ]
        write_csv(run_dir / CANDIDATE_CSV, candidates, CANDIDATE_COLUMNS)
        write_csv(run_dir / HOLDOUT_CSV, holdouts, HOLDOUT_COLUMNS)
        progress.result(
            f"updates={len(candidates)} holdouts={len(holdouts)} total={len(detail_rows)}"
        )

        summary = {
            "run_dir": str(run_dir),
            "total": len(detail_rows),
            "db_hits": sum(
                1 for r in detail_rows if r.get("match_source") not in ("", MATCH_SOURCE_NONE)
            ),
            "potential_updates": len(candidates),
            "holdout_rooftop": sum(1 for r in holdouts if r.get("bucket") == BUCKET_ROOFTOP),
            "holdout_other": sum(1 for r in holdouts if r.get("bucket") == BUCKET_OTHER),
            "errors": sum(1 for r in detail_rows if r.get("error")),
        }
        (run_dir / "summary.json").write_text(
            json.dumps(summary, indent=2), encoding="utf-8"
        )
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
        "match_source": MATCH_SOURCE_NONE,
        "match_distance_m": "",
        "match_record_id": "",
        "match_asr_number": "",
        "match_asset_type": "",
        "classify_lat": "",
        "classify_lng": "",
        "naip_site_type": "",
        "naip_tower_subtype": "",
        "naip_site_confidence": "",
        "naip_cell_equipment": "",
        "asset_lat": "",
        "asset_lon": "",
        "asset_offset_m": "",
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
        progress.warn("Missing Salesforce lat/lng — skipping proximity/NAIP")
        base["bucket"] = BUCKET_OTHER
        base["holdout_reason"] = "missing_sf_coordinates"
        base["error"] = "missing_sf_coordinates"
        return base

    sf_lat, sf_lng = coords
    base["sf_lat"] = sf_lat
    base["sf_lng"] = sf_lng
    progress.step(f"SF pin: {sf_lat:.6f}, {sf_lng:.6f}")

    progress.stage("PROXIMITY", f"≤{max_m:g} m")
    try:
        hit = find_proximity_hit(cursor, sf_lat, sf_lng, max_m=max_m)
    except Exception as exc:  # noqa: BLE001
        progress.warn(f"SQL proximity failed: {exc}")
        base["error"] = f"sql_proximity_failed: {exc}"
        base["holdout_reason"] = "sql_error"
        return base

    if hit is not None:
        base["match_source"] = describe_match(hit)
        base["match_distance_m"] = round(hit.distance_m, 2)
        base["match_record_id"] = hit.record_id or ""
        base["match_asr_number"] = hit.asr_number or ""
        base["match_asset_type"] = hit.asset_type or ""
        classify_lat, classify_lng = hit.latitude, hit.longitude
        db_lat, db_lng = hit.latitude, hit.longitude
        progress.result(f"{base['match_source']} @ {hit.distance_m:.1f} m")
    else:
        classify_lat, classify_lng = sf_lat, sf_lng
        db_lat = db_lng = None
        progress.result("no DB hit → NAIP on SF pin")

    base["classify_lat"] = classify_lat
    base["classify_lng"] = classify_lng

    if skip_classify:
        progress.step("skip classify")
        if hit is not None:
            base["bucket"] = BUCKET_POTENTIAL_UPDATE
            base["holdout_reason"] = "skip_classify_db_hit"
            base["update_lat"] = classify_lat
            base["update_lng"] = classify_lng
            base["update_coord_source"] = f"db:{base['match_source']}"
            base["update_verified_site"] = True
            base["update_verified_site_source"] = "FCC"
        else:
            base["bucket"] = BUCKET_OTHER
            base["holdout_reason"] = "skip_classify_no_db_hit"
        return base

    progress.stage("NAIP")
    try:
        classified = classify_fn(
            site_id=sf_id,
            lat=float(classify_lat),
            lon=float(classify_lng),
            chip_dir=chip_dir,
            verbose=verbose,
        )
    except TypeError:
        # Test doubles may not accept verbose=
        classified = classify_fn(
            site_id=sf_id,
            lat=float(classify_lat),
            lon=float(classify_lng),
            chip_dir=chip_dir,
        )
    except Exception as exc:  # noqa: BLE001
        progress.warn(f"NAIP classify failed: {exc}")
        base["error"] = f"naip_classify_failed: {exc}"
        base["bucket"] = BUCKET_OTHER
        base["holdout_reason"] = "classify_error"
        return base

    base["naip_site_type"] = classified.get("site_type") or ""
    base["naip_tower_subtype"] = classified.get("tower_subtype") or ""
    base["naip_site_confidence"] = classified.get("site_confidence") or ""
    base["naip_cell_equipment"] = classified.get("cell_equipment")
    base["asset_lat"] = classified.get("asset_lat") or ""
    base["asset_lon"] = classified.get("asset_lon") or ""
    base["asset_offset_m"] = classified.get("asset_offset_m") or ""
    if classified.get("error"):
        base["error"] = classified.get("error")

    decision = bucket_classification(
        match_source=base["match_source"],
        classified=classified,
        db_lat=db_lat,
        db_lng=db_lng,
        sf_lat=sf_lat,
        sf_lng=sf_lng,
    )
    base.update(decision)
    return base


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
    summary = {
        "total": len(results),
        "success": sum(1 for r in results if r.get("success")),
        "failed": sum(1 for r in results if not r.get("success")),
        "apply": apply,
        "log": str(log_path),
    }
    logger.info("Apply summary: %s", summary)
    return summary
