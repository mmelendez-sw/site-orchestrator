"""Site orchestrator: source -> ingest -> dedupe -> classify -> Salesforce."""

from __future__ import annotations

import argparse
import csv
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from classifier.asset_classifier import classify_record
from dedupe.batch_postprocess import (
    apply_batch_postprocess,
    build_prescore_duplicate_row,
    prescore_duplicate_indices,
)
from dedupe.constants import (
    SF_ADDRESS_FIELD,
    SF_CITY_FIELD,
    SF_STATE_FIELD,
    SF_ZIP_FIELD,
    THRESHOLD_VERSION,
)
from dedupe.address_match import has_parseable_house_number
from dedupe.context import extract_zip_code
from dedupe.resolver import SiteResolver
from dedupe.urbanicity import urbanicity_for_record
from ingest.normalizer import normalize
from ingest.scraper import IngestRecord
from salesforce.sf_client import SalesforceClient
from salesforce.upload_template import build_upload_record, write_upload_csv
from source.record import SourceRecord
from source.runner import list_sources, run_source
from source.scope import parse_scope

load_dotenv()

logger = logging.getLogger(__name__)
RUNS_DIR = Path("runs")

REVIEW_LOG_FIELDS = [
    "timestamp",
    "address",
    "lat",
    "lng",
    "address_score",
    "combined_score",
    "matched_distance_m",
    "urbanicity_prefilter_radius_m",
    "urbanicity_tier",
    "matched_id",
    "matched_address",
    "house_number_delta",
    "suffix_mismatch",
    "city_mismatch",
    "runner_up_id",
    "runner_up_score",
    "tie_breaker_close",
    "routing_reason",
    "proximity_rule",
    "override_reason",
    "status_source",
    "zip_mismatch",
    "resolution_detail",
]

DEDUPE_RESULT_FIELDS = [
    "address",
    "lat",
    "lng",
    "zip_code",
    "urbanicity_tier",
    "zip_population",
    "urbanicity_prefilter_radius_m",
    "status",
    "address_score",
    "proximity_score",
    "combined_score",
    "matched_distance_m",
    "matched_coordinate_source",
    "spatial_candidate_count",
    "prefilter_candidate_count",
    "potential_duplicate",
    "candidate_count",
    "matched_id",
    "matched_address",
    "matched_city",
    "matched_state",
    "matched_zip",
    "house_number_delta",
    "suffix_mismatch",
    "city_mismatch",
    "runner_up_id",
    "runner_up_score",
    "tie_breaker_close",
    "routing_reason",
    "proximity_rule",
    "override_reason",
    "status_resolver",
    "status_recommended",
    "status_source",
    "scoring_mode",
    "top_candidates",
    "zip_mismatch",
    "threshold_version",
    "distance_override_applied",
    "resolution_detail",
]


def _write_sf_upload_csv(
    result_rows: list[dict[str, Any]],
    canonical_records: list[dict[str, Any]],
    run_dir: Path,
    *,
    classified_by_index: dict[int, dict[str, Any]] | None = None,
) -> Path | None:
    """Write sf_upload.csv for final net-new rows using the Salesforce template."""
    upload_records: list[dict[str, Any]] = []
    for index, row in enumerate(result_rows):
        if row.get("status") != "net_new":
            continue
        canonical = canonical_records[index] if index < len(canonical_records) else {}
        classified = (classified_by_index or {}).get(index)
        upload_records.append(
            build_upload_record(
                canonical,
                classified=classified,
                dedupe_row=row,
            )
        )
    if not upload_records:
        return None
    output = run_dir / "sf_upload.csv"
    write_upload_csv(upload_records, output)
    logger.info("Wrote Salesforce upload template to %s", output.resolve())
    return output


def _review_log_path(run_dir: Path) -> Path:
    return run_dir / "review_log.csv"


def _ensure_review_log_header(review_log: Path) -> None:
    review_log.parent.mkdir(parents=True, exist_ok=True)
    if review_log.exists():
        return
    with review_log.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=REVIEW_LOG_FIELDS)
        writer.writeheader()


def _log_review(
    record: dict[str, Any],
    resolution: dict[str, Any],
    *,
    review_log: Path,
) -> None:
    _ensure_review_log_header(review_log)
    matched = resolution.get("matched_record") or {}
    with review_log.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=REVIEW_LOG_FIELDS)
        writer.writerow({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "address": record.get("address"),
            "lat": record.get("lat"),
            "lng": record.get("lng"),
            "address_score": resolution.get("address_score"),
            "combined_score": resolution.get("combined_score"),
            "matched_distance_m": resolution.get("matched_distance_m"),
            "urbanicity_prefilter_radius_m": (resolution.get("urbanicity") or {}).get(
                "search_radius_m"
            ),
            "urbanicity_tier": (resolution.get("urbanicity") or {}).get("urbanicity_tier"),
            "matched_id": matched.get("Id"),
            "matched_address": matched.get(SF_ADDRESS_FIELD) or matched.get("Name"),
            "house_number_delta": resolution.get("house_number_delta"),
            "suffix_mismatch": resolution.get("suffix_mismatch"),
            "city_mismatch": resolution.get("city_mismatch"),
            "runner_up_id": (resolution.get("runner_up_record") or {}).get("Id"),
            "runner_up_score": resolution.get("runner_up_score"),
            "tie_breaker_close": resolution.get("tie_breaker_close"),
            "routing_reason": resolution.get("routing_reason"),
            "proximity_rule": resolution.get("proximity_rule"),
            "override_reason": resolution.get("override_reason"),
            "status_source": resolution.get("status_source"),
            "zip_mismatch": resolution.get("zip_mismatch"),
            "resolution_detail": resolution.get("resolution_detail"),
        })


def _to_ingest(raw: dict[str, Any] | IngestRecord | SourceRecord) -> IngestRecord:
    if isinstance(raw, SourceRecord):
        return raw.to_ingest_record()
    if isinstance(raw, IngestRecord):
        return raw
    return IngestRecord(
        address=raw.get("address"),
        lat=raw.get("lat"),
        lng=raw.get("lng"),
        permit_metadata=dict(raw.get("permit_metadata") or {}),
        source_url=raw.get("source_url"),
    )


def _normalize_batch(
    raw_records: list[dict[str, Any] | IngestRecord | SourceRecord],
    *,
    verbose: bool = False,
) -> tuple[list[dict[str, Any]], list[tuple[Any, Exception]]]:
    canonical_records: list[dict[str, Any]] = []
    failures: list[tuple[Any, Exception]] = []
    total = len(raw_records)

    if verbose and total:
        logger.info("=" * 72)
        logger.info("GEOCODE / NORMALIZE (%d records)", total)
        logger.info("  geocoder: Census first, Nominatim fallback (GEOCODER=%s)", 
                    __import__("os").environ.get("GEOCODER", "auto"))
        logger.info("=" * 72)

    for index, raw in enumerate(raw_records, start=1):
        source_address = _source_address(raw)
        try:
            canonical = normalize(_to_ingest(raw))
            if not canonical.get("zip_code"):
                zip_code = extract_zip_code(canonical)
                if zip_code:
                    canonical["zip_code"] = zip_code
            canonical_records.append(canonical)
            if verbose:
                logger.info(
                    "[%d/%d] geocoded OK  lat=%.6f lng=%.6f zip=%s",
                    index,
                    total,
                    canonical["lat"],
                    canonical["lng"],
                    canonical.get("zip_code") or "—",
                )
                logger.info("         in : %s", source_address[:100])
                logger.info("         out: %s", canonical["address"][:100])
        except Exception as exc:
            failures.append((raw, exc))
            if verbose:
                logger.error("[%d/%d] geocode FAILED: %s", index, total, source_address[:100])
                logger.error("         error: %s", exc)

    if verbose:
        logger.info(
            "Geocode complete — success=%d failed=%d",
            len(canonical_records),
            len(failures),
        )
        logger.info("=" * 72)

    return canonical_records, failures


def _source_address(raw: dict[str, Any] | IngestRecord | SourceRecord) -> str:
    if isinstance(raw, SourceRecord):
        return raw.full_address
    if isinstance(raw, IngestRecord):
        return raw.address or ""
    return str(raw.get("address") or "")


def _format_export_number(value: Any, *, precision: int = 2) -> Any:
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return value
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    if precision == 0:
        return int(round(number))
    return round(number, precision)


def _format_dedupe_export_row(row: dict[str, Any]) -> dict[str, Any]:
    export_fields = [field for field in DEDUPE_RESULT_FIELDS if not field.startswith("_")]
    formatted = {field: row.get(field, "") for field in export_fields}
    formatted["lat"] = _format_export_number(row.get("lat"), precision=6)
    formatted["lng"] = _format_export_number(row.get("lng"), precision=6)
    formatted["zip_population"] = _format_export_number(row.get("zip_population"), precision=0)
    formatted["urbanicity_prefilter_radius_m"] = _format_export_number(
        row.get("urbanicity_prefilter_radius_m"), precision=1
    )
    formatted["address_score"] = _format_export_number(row.get("address_score"), precision=0)
    formatted["proximity_score"] = _format_export_number(row.get("proximity_score"), precision=0)
    formatted["combined_score"] = _format_export_number(row.get("combined_score"), precision=0)
    formatted["matched_distance_m"] = _format_export_number(
        row.get("matched_distance_m"), precision=1
    )
    formatted["spatial_candidate_count"] = _format_export_number(
        row.get("spatial_candidate_count"), precision=0
    )
    formatted["prefilter_candidate_count"] = _format_export_number(
        row.get("prefilter_candidate_count"), precision=0
    )
    formatted["candidate_count"] = _format_export_number(row.get("candidate_count"), precision=0)
    formatted["potential_duplicate"] = bool(row.get("potential_duplicate"))
    formatted["zip_mismatch"] = bool(row.get("zip_mismatch"))
    formatted["suffix_mismatch"] = bool(row.get("suffix_mismatch"))
    formatted["city_mismatch"] = bool(row.get("city_mismatch"))
    formatted["tie_breaker_close"] = bool(row.get("tie_breaker_close"))
    formatted["distance_override_applied"] = bool(row.get("distance_override_applied"))
    formatted["house_number_delta"] = _format_export_number(
        row.get("house_number_delta"), precision=0
    )
    formatted["runner_up_score"] = _format_export_number(
        row.get("runner_up_score"), precision=0
    )
    return formatted


def _summarize_result_rows(rows: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "duplicates": sum(1 for row in rows if row.get("status") == "duplicate"),
        "review": sum(1 for row in rows if row.get("status") == "review"),
        "net_new": sum(1 for row in rows if row.get("status") == "net_new"),
    }


def _write_review_log_from_rows(run_dir: Path, rows: list[dict[str, Any]]) -> None:
    review_rows = [row for row in rows if row.get("status") == "review"]
    review_log = _review_log_path(run_dir)
    if not review_rows:
        if review_log.exists():
            review_log.unlink()
        return

    with review_log.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=REVIEW_LOG_FIELDS)
        writer.writeheader()
        for row in review_rows:
            writer.writerow({
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "address": row.get("address"),
                "lat": row.get("lat"),
                "lng": row.get("lng"),
                "address_score": _format_export_number(row.get("address_score"), precision=0),
                "combined_score": _format_export_number(row.get("combined_score"), precision=0),
                "matched_distance_m": _format_export_number(
                    row.get("matched_distance_m"), precision=1
                ),
                "urbanicity_prefilter_radius_m": _format_export_number(
                    row.get("urbanicity_prefilter_radius_m"), precision=1
                ),
                "urbanicity_tier": row.get("urbanicity_tier"),
                "matched_id": row.get("matched_id"),
                "matched_address": row.get("matched_address"),
                "house_number_delta": _format_export_number(
                    row.get("house_number_delta"), precision=0
                ),
                "suffix_mismatch": row.get("suffix_mismatch"),
                "city_mismatch": row.get("city_mismatch"),
                "runner_up_id": row.get("runner_up_id"),
                "runner_up_score": _format_export_number(
                    row.get("runner_up_score"), precision=0
                ),
                "tie_breaker_close": row.get("tie_breaker_close"),
                "routing_reason": row.get("routing_reason"),
                "proximity_rule": row.get("proximity_rule"),
                "override_reason": row.get("override_reason"),
                "status_source": row.get("status_source"),
                "zip_mismatch": row.get("zip_mismatch"),
                "resolution_detail": row.get("resolution_detail"),
            })


def _write_dedupe_results(
    run_dir: Path,
    rows: list[dict[str, Any]],
) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    output = run_dir / "dedupe_results.csv"
    export_rows = [_format_dedupe_export_row(row) for row in rows]
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=DEDUPE_RESULT_FIELDS)
        writer.writeheader()
        writer.writerows(export_rows)
    return output


def _process_dedupe_record(
    canonical: dict[str, Any],
    resolver: SiteResolver,
    sf_client: SalesforceClient | None,
    *,
    run_dir: Path,
    dry_run: bool,
    verbose: bool = False,
    index: int = 0,
    total: int = 0,
) -> tuple[dict[str, Any], dict[str, int]]:
    """Resolve one record; optionally log duplicates to Salesforce."""
    if verbose:
        prefix = f"[{index}/{total}] " if total else ""
        logger.info("-" * 72)
        logger.info("%sDEDUPE: %s", prefix, canonical.get("address", "")[:100])
        logger.info("         lat=%.6f lng=%.6f zip=%s",
                    canonical["lat"], canonical["lng"], canonical.get("zip_code") or "—")
        if dry_run:
            logger.info("         mode: DRY-RUN (no Salesforce writes)")

    summary_delta = {"duplicates": 0, "review": 0, "net_new": 0, "errors": 0}

    if not has_parseable_house_number(canonical.get("address")):
        urbanicity = urbanicity_for_record(canonical)
        urbanicity_data = urbanicity.as_dict()
        result_row = {
            "address": canonical.get("address"),
            "lat": canonical.get("lat"),
            "lng": canonical.get("lng"),
            "zip_code": canonical.get("zip_code"),
            "urbanicity_tier": urbanicity_data.get("urbanicity_tier"),
            "zip_population": urbanicity_data.get("zip_population"),
            "urbanicity_prefilter_radius_m": urbanicity_data.get("search_radius_m"),
            "status": "net_new",
            "status_resolver": "net_new",
            "status_recommended": "net_new",
            "address_score": 0,
            "proximity_score": None,
            "combined_score": 0,
            "matched_distance_m": None,
            "matched_coordinate_source": None,
            "spatial_candidate_count": 0,
            "prefilter_candidate_count": 0,
            "potential_duplicate": False,
            "candidate_count": 0,
            "matched_id": None,
            "matched_address": None,
            "matched_city": None,
            "matched_state": None,
            "matched_zip": None,
            "house_number_delta": None,
            "suffix_mismatch": False,
            "city_mismatch": False,
            "runner_up_id": None,
            "runner_up_score": None,
            "tie_breaker_close": False,
            "routing_reason": "unparseable_input",
            "proximity_rule": "unparseable_input",
            "override_reason": "unparseable_input",
            "status_source": "resolver",
            "zip_mismatch": False,
            "scoring_mode": "",
            "top_candidates": "",
            "_gated_candidates": [],
            "threshold_version": THRESHOLD_VERSION,
            "distance_override_applied": False,
            "resolution_detail": "status=net_new; routing_reason=unparseable_input",
        }
        summary_delta["net_new"] = 1
        logger.info("Net-new candidate (unparseable input): %s", canonical["address"])
        return result_row, summary_delta

    resolution = resolver.resolve(canonical)
    status = resolution["status"]
    matched = resolution.get("matched_record") or {}
    runner_up = resolution.get("runner_up_record") or {}
    urbanicity = resolution.get("urbanicity") or {}

    result_row = {
        "address": canonical.get("address"),
        "lat": canonical.get("lat"),
        "lng": canonical.get("lng"),
        "zip_code": canonical.get("zip_code"),
        "urbanicity_tier": urbanicity.get("urbanicity_tier"),
        "zip_population": urbanicity.get("zip_population"),
        "urbanicity_prefilter_radius_m": urbanicity.get("search_radius_m"),
        "status": status,
        "status_resolver": resolution.get("status_resolver"),
        "status_recommended": resolution.get("status_recommended"),
        "address_score": resolution.get("address_score"),
        "proximity_score": resolution.get("proximity_score"),
        "combined_score": resolution.get("combined_score"),
        "matched_distance_m": resolution.get("matched_distance_m"),
        "matched_coordinate_source": resolution.get("matched_coordinate_source"),
        "spatial_candidate_count": resolution.get("spatial_candidate_count"),
        "prefilter_candidate_count": resolution.get("prefilter_candidate_count"),
        "potential_duplicate": resolution.get("potential_duplicate"),
        "candidate_count": resolution.get("candidate_count"),
        "matched_id": matched.get("Id"),
        "matched_address": matched.get(SF_ADDRESS_FIELD) or matched.get("Name"),
        "matched_city": matched.get(SF_CITY_FIELD),
        "matched_state": matched.get(SF_STATE_FIELD),
        "matched_zip": matched.get(SF_ZIP_FIELD),
        "house_number_delta": resolution.get("house_number_delta"),
        "suffix_mismatch": resolution.get("suffix_mismatch"),
        "city_mismatch": resolution.get("city_mismatch"),
        "runner_up_id": runner_up.get("Id"),
        "runner_up_score": resolution.get("runner_up_score"),
        "tie_breaker_close": resolution.get("tie_breaker_close"),
        "routing_reason": resolution.get("routing_reason"),
        "proximity_rule": resolution.get("proximity_rule"),
        "override_reason": resolution.get("override_reason"),
        "status_source": resolution.get("status_source"),
        "zip_mismatch": resolution.get("zip_mismatch"),
        "scoring_mode": resolution.get("scoring_mode"),
        "top_candidates": resolution.get("top_candidates"),
        "_gated_candidates": resolution.get("_gated_candidates") or [],
        "threshold_version": resolution.get("threshold_version"),
        "distance_override_applied": resolution.get("distance_override_applied"),
        "resolution_detail": resolution.get("resolution_detail"),
    }

    if status == "duplicate":
        if sf_client and not dry_run:
            sf_client.log_duplicate(canonical, matched.get("Id", ""))
        summary_delta["duplicates"] = 1
        logger.info(
            "Duplicate%s: %s (combined=%s address=%s distance=%sm radius=%sm)",
            " (dry-run)" if dry_run else " skipped",
            canonical["address"],
            resolution.get("combined_score"),
            resolution.get("address_score"),
            f"{resolution.get('matched_distance_m'):.0f}"
            if resolution.get("matched_distance_m") is not None
            else "n/a",
            urbanicity.get("search_radius_m"),
        )
        return result_row, summary_delta

    if status == "review":
        summary_delta["review"] = 1
        logger.info(
            "Review queued: %s (combined=%s %s)",
            canonical["address"],
            resolution.get("combined_score"),
            resolution.get("resolution_detail"),
        )
        return result_row, summary_delta

    summary_delta["net_new"] = 1
    logger.info("Net-new candidate: %s", canonical["address"])
    return result_row, summary_delta


def run_dedupe_pipeline(
    raw_records: list[dict[str, Any] | IngestRecord | SourceRecord],
    *,
    dry_run: bool = False,
    verbose: bool = False,
    run_dir: Path | None = None,
) -> dict[str, int]:
    """Normalize source records and run Salesforce dedupe only."""
    if verbose:
        logger.info("")
        logger.info("#" * 72)
        logger.info("SITE ORCHESTRATOR — DEDUPE PIPELINE")
        logger.info("  records  : %d", len(raw_records))
        logger.info("  dry-run  : %s (query SF yes, write SF no)", dry_run)
        logger.info("  classify : no")
        logger.info("#" * 72)

    canonical_records, failures = _normalize_batch(raw_records, verbose=verbose)
    resolver = SiteResolver(verbose=verbose)
    sf_client = None if dry_run else SalesforceClient()
    run_dir = run_dir or RUNS_DIR / f"dedupe_{datetime.now().strftime('%Y-%m-%d_%H%M%S')}"
    run_dir.mkdir(parents=True, exist_ok=True)
    result_rows: list[dict[str, Any]] = []
    review_log = _review_log_path(run_dir)

    if verbose:
        logger.info("Output directory: %s", run_dir.resolve())

    summary = {
        "processed": len(raw_records),
        "duplicates": 0,
        "review": 0,
        "net_new": 0,
        "loaded": 0,
        "errors": len(failures),
    }

    if canonical_records:
        resolver.prefetch(canonical_records)
        if not verbose:
            logger.info(
                "Prefetched %s Salesforce candidates for %s dataset zip codes",
                len(resolver._candidate_cache or []),
                len((resolver._dataset_context or {}).get("zip_codes", [])),
            )

    for _, exc in failures:
        logger.exception("Failed to normalize record: %s", exc)

    dedupe_total = len(canonical_records)
    prescore_dupes = prescore_duplicate_indices(canonical_records)
    if verbose and dedupe_total:
        logger.info("=" * 72)
        logger.info("DEDUPE RESOLUTION (%d records)", dedupe_total)
        logger.info("  thresholds: duplicate >= 85, review >= 60 (combined score in-radius)")
        logger.info("=" * 72)

    for index, canonical in enumerate(canonical_records, start=1):
        try:
            if (index - 1) in prescore_dupes:
                result_row = build_prescore_duplicate_row(canonical)
                result_rows.append(result_row)
                summary["duplicates"] += 1
                if verbose:
                    prefix = f"[{index}/{dedupe_total}] " if dedupe_total else ""
                    logger.info(
                        "%sInput duplicate (pre-score): %s",
                        prefix,
                        canonical.get("address", "")[:100],
                    )
                continue
            result_row, delta = _process_dedupe_record(
                canonical,
                resolver,
                sf_client,
                run_dir=run_dir,
                dry_run=dry_run,
                verbose=verbose,
                index=index,
                total=dedupe_total,
            )
            result_rows.append(result_row)
            for key, value in delta.items():
                summary[key] += value
        except Exception as exc:
            summary["errors"] += 1
            logger.exception("Failed to dedupe record: %s", exc)

    if result_rows:
        batch_changes = apply_batch_postprocess(result_rows)
        if verbose and any(batch_changes.values()):
            logger.info(
                "Batch postprocess — input_dupes=%d near=%d matched_id=%d outliers=%d potential=%d",
                batch_changes["input_duplicates"],
                batch_changes["input_duplicates_near"],
                batch_changes["matched_id_reconciled"],
                batch_changes["address_exact_outliers"],
                batch_changes["potential_duplicate_promoted"],
            )
        status_counts = _summarize_result_rows(result_rows)
        summary["duplicates"] = status_counts["duplicates"]
        summary["review"] = status_counts["review"]
        summary["net_new"] = status_counts["net_new"]
        output = _write_dedupe_results(run_dir, result_rows)
        _write_review_log_from_rows(run_dir, result_rows)
        _write_sf_upload_csv(result_rows, canonical_records, run_dir)
        logger.info("Wrote dedupe results to %s", output.resolve())
        if summary["review"]:
            logger.info("Wrote review log to %s", review_log.resolve())

    if verbose:
        logger.info("")
        logger.info("#" * 72)
        logger.info("FINAL SUMMARY")
        logger.info("  processed  : %d", summary["processed"])
        logger.info("  duplicates : %d  (skip — already in Salesforce)", summary["duplicates"])
        logger.info("  review     : %d  (manual check — see %s)", summary["review"], review_log)
        logger.info("  net_new    : %d  (OK to classify / upload next)", summary["net_new"])
        logger.info("  errors     : %d", summary["errors"])
        if dry_run:
            logger.info("  mode       : DRY-RUN — no records written to Salesforce")
        logger.info("#" * 72)

    logger.info(
        "Dedupe summary — processed=%s duplicates=%s review=%s net_new=%s errors=%s%s",
        summary["processed"],
        summary["duplicates"],
        summary["review"],
        summary["net_new"],
        summary["errors"],
        " (dry-run, no Salesforce writes)" if dry_run else "",
    )
    return summary


def main(
    raw_records: list[dict[str, Any] | IngestRecord | SourceRecord] | None = None,
    *,
    classify: bool = True,
    dry_run: bool = False,
    verbose: bool = False,
) -> dict[str, int]:
    """Process records through normalize, dedupe, optional classify, and load."""
    if raw_records is None:
        raw_records = []

    if not classify:
        return run_dedupe_pipeline(raw_records, dry_run=dry_run, verbose=verbose)

    canonical_records, failures = _normalize_batch(raw_records, verbose=verbose)
    run_dir = RUNS_DIR / f"orchestrator_{datetime.now().strftime('%Y-%m-%d_%H%M%S')}"
    run_dir.mkdir(parents=True, exist_ok=True)

    resolver = SiteResolver(verbose=verbose)
    sf_client = None if dry_run else SalesforceClient()
    result_rows: list[dict[str, Any]] = []

    summary = {
        "processed": len(raw_records),
        "duplicates": 0,
        "review": 0,
        "net_new": 0,
        "loaded": 0,
        "errors": len(failures),
    }

    for _, exc in failures:
        logger.exception("Failed to normalize record: %s", exc)

    if canonical_records:
        candidates = resolver.prefetch(canonical_records)
        logger.info("Prefetched %s Salesforce candidates for dedupe", len(candidates))

    dedupe_total = len(canonical_records)
    classified_by_index: dict[int, dict[str, Any]] = {}
    for index, canonical in enumerate(canonical_records, start=1):
        try:
            result_row, delta = _process_dedupe_record(
                canonical,
                resolver,
                sf_client,
                run_dir=run_dir,
                dry_run=dry_run,
                verbose=verbose,
                index=index,
                total=dedupe_total,
            )
            result_rows.append(result_row)
            for key, value in delta.items():
                summary[key] += value

            if result_row["status"] != "net_new":
                continue

            classified = classify_record(canonical, run_dir=run_dir)
            classified_by_index[len(result_rows) - 1] = classified
            logger.info("Classified net-new site: %s", canonical["address"])

        except Exception as exc:
            summary["errors"] += 1
            logger.exception("Failed to process record: %s", exc)

    if result_rows:
        apply_batch_postprocess(result_rows)
        status_counts = _summarize_result_rows(result_rows)
        summary["duplicates"] = status_counts["duplicates"]
        summary["review"] = status_counts["review"]
        summary["net_new"] = status_counts["net_new"]
        output = _write_dedupe_results(run_dir, result_rows)
        _write_review_log_from_rows(run_dir, result_rows)
        _write_sf_upload_csv(
            result_rows,
            canonical_records,
            run_dir,
            classified_by_index=classified_by_index,
        )
        logger.info("Wrote dedupe results to %s", output)

        if sf_client and not dry_run:
            for index, row in enumerate(result_rows):
                if row.get("status") != "net_new":
                    continue
                classified = classified_by_index.get(index)
                if classified is None:
                    continue
                upload_record = build_upload_record(
                    canonical_records[index],
                    classified=classified,
                    dedupe_row=row,
                )
                sf_client.create_site(upload_record)
                summary["loaded"] += 1
                logger.info("Loaded net-new site: %s", canonical_records[index]["address"])
        elif dry_run and summary["net_new"]:
            logger.info(
                "Dry-run: %s net-new rows exported to sf_upload.csv (no Salesforce writes)",
                summary["net_new"],
            )

        if summary["review"]:
            logger.info("Wrote review log to %s", _review_log_path(run_dir))

    logger.info(
        "Summary — processed=%s duplicates=%s review=%s net_new=%s loaded=%s errors=%s%s",
        summary["processed"],
        summary["duplicates"],
        summary["review"],
        summary["net_new"],
        summary["loaded"],
        summary["errors"],
        " (dry-run, no Salesforce writes)" if dry_run else "",
    )
    return summary


def run_from_source(
    source_name: str,
    *,
    classify: bool = False,
    dry_run: bool = False,
    verbose: bool = False,
    scope: Any = None,
    **source_kwargs: Any,
) -> dict[str, int]:
    """Run a permit source adapter, then hand off to ingest + Salesforce dedupe."""
    if verbose:
        logger.info("Loading source adapter: %s", source_name)
        if source_kwargs.get("input_path"):
            logger.info("  input: %s", source_kwargs["input_path"])
    records = run_source(source_name, scope=scope, **source_kwargs)
    logger.info("Source '%s' produced %s records", source_name, len(records))
    return main(records, classify=classify, dry_run=dry_run, verbose=verbose)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the site orchestrator pipeline")
    parser.add_argument(
        "--source",
        choices=list_sources(),
        help="Permit source adapter to run before dedupe",
    )
    parser.add_argument(
        "--input",
        help="Input CSV/JSON path when using the file source",
    )
    parser.add_argument(
        "--classify",
        action="store_true",
        help="Run classifier + Salesforce load for net-new records",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Query Salesforce for dedupe but do not create sites or duplicate logs",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print detailed step-by-step progress (on by default with --dry-run)",
    )
    parser.add_argument("--country", help="Country scope (e.g. US)")
    parser.add_argument("--state", help="State scope (e.g. WI)")
    parser.add_argument("--county", help="County scope")
    parser.add_argument("--city", help="City scope")
    parser.add_argument("--zip", dest="zip_codes", help="Comma-separated zip codes")
    parser.add_argument(
        "--no-enrich",
        action="store_true",
        help="Skip MPROP enrichment when using the Milwaukee source",
    )
    parser.add_argument(
        "--no-dedupe-addresses",
        action="store_true",
        help="Keep all permit rows from the source instead of one per address",
    )
    return parser.parse_args()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = _parse_args()
    verbose = args.verbose or args.dry_run
    scope = parse_scope(
        country=args.country,
        state=args.state,
        county=args.county,
        city=args.city,
        zip_codes=args.zip_codes,
    ) if any([args.country, args.state, args.county, args.city, args.zip_codes]) else None

    if args.source:
        source_kwargs: dict[str, Any] = {
            "enrich": not args.no_enrich,
            "dedupe_addresses": not args.no_dedupe_addresses,
        }
        if args.source == "file":
            if not args.input:
                raise SystemExit("The file source requires --input")
            source_kwargs["input_path"] = args.input
        run_from_source(
            args.source,
            classify=args.classify,
            dry_run=args.dry_run,
            verbose=verbose,
            scope=scope,
            **source_kwargs,
        )
    else:
        main([])
