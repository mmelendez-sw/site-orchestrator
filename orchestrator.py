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
from dedupe.constants import (
    SF_ADDRESS_FIELD,
    SF_CITY_FIELD,
    SF_STATE_FIELD,
    SF_ZIP_FIELD,
)
from dedupe.resolver import SiteResolver
from ingest.normalizer import normalize
from ingest.scraper import IngestRecord
from salesforce.sf_client import SalesforceClient
from source.record import SourceRecord
from source.runner import list_sources, run_source
from source.scope import parse_scope

load_dotenv()

logger = logging.getLogger(__name__)
RUNS_DIR = Path("runs")
REVIEW_LOG = RUNS_DIR / "review_log.csv"


def _ensure_review_log_header() -> None:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    if REVIEW_LOG.exists():
        return
    with REVIEW_LOG.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "timestamp",
                "address",
                "lat",
                "lng",
                "score",
                "address_score",
                "combined_score",
                "matched_distance_m",
                "search_radius_m",
                "urbanicity_tier",
                "matched_id",
                "matched_address",
                "resolution_detail",
            ],
        )
        writer.writeheader()


def _log_review(record: dict[str, Any], resolution: dict[str, Any]) -> None:
    _ensure_review_log_header()
    matched = resolution.get("matched_record") or {}
    with REVIEW_LOG.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "timestamp",
                "address",
                "lat",
                "lng",
                "score",
                "address_score",
                "combined_score",
                "matched_distance_m",
                "search_radius_m",
                "urbanicity_tier",
                "matched_id",
                "matched_address",
                "resolution_detail",
            ],
        )
        writer.writerow({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "address": record.get("address"),
            "lat": record.get("lat"),
            "lng": record.get("lng"),
            "score": resolution.get("score"),
            "address_score": resolution.get("address_score"),
            "combined_score": resolution.get("combined_score"),
            "matched_distance_m": resolution.get("matched_distance_m"),
            "search_radius_m": (resolution.get("urbanicity") or {}).get("search_radius_m"),
            "urbanicity_tier": (resolution.get("urbanicity") or {}).get("urbanicity_tier"),
            "matched_id": matched.get("Id"),
            "matched_address": matched.get(SF_ADDRESS_FIELD) or matched.get("Name"),
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


def _write_dedupe_results(
    run_dir: Path,
    rows: list[dict[str, Any]],
) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    output = run_dir / "dedupe_results.csv"
    fieldnames = [
        "address",
        "lat",
        "lng",
        "zip_code",
        "urbanicity_tier",
        "zip_population",
        "search_radius_m",
        "status",
        "score",
        "address_score",
        "proximity_score",
        "combined_score",
        "matched_distance_m",
        "spatial_candidate_count",
        "candidate_count",
        "matched_id",
        "matched_address",
        "matched_city",
        "matched_state",
        "matched_zip",
        "resolution_detail",
    ]
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return output


def _process_dedupe_record(
    canonical: dict[str, Any],
    resolver: SiteResolver,
    sf_client: SalesforceClient | None,
    *,
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
    resolution = resolver.resolve(canonical)
    status = resolution["status"]
    matched = resolution.get("matched_record") or {}
    urbanicity = resolution.get("urbanicity") or {}

    result_row = {
        "address": canonical.get("address"),
        "lat": canonical.get("lat"),
        "lng": canonical.get("lng"),
        "zip_code": canonical.get("zip_code"),
        "urbanicity_tier": urbanicity.get("urbanicity_tier"),
        "zip_population": urbanicity.get("zip_population"),
        "search_radius_m": urbanicity.get("search_radius_m"),
        "status": status,
        "score": resolution.get("score"),
        "address_score": resolution.get("address_score"),
        "proximity_score": resolution.get("proximity_score"),
        "combined_score": resolution.get("combined_score"),
        "matched_distance_m": resolution.get("matched_distance_m"),
        "spatial_candidate_count": resolution.get("spatial_candidate_count"),
        "candidate_count": resolution.get("candidate_count"),
        "matched_id": matched.get("Id"),
        "matched_address": matched.get(SF_ADDRESS_FIELD) or matched.get("Name"),
        "matched_city": matched.get(SF_CITY_FIELD),
        "matched_state": matched.get(SF_STATE_FIELD),
        "matched_zip": matched.get(SF_ZIP_FIELD),
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
        _log_review(canonical, resolution)
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
    result_rows: list[dict[str, Any]] = []

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
    if verbose and dedupe_total:
        logger.info("=" * 72)
        logger.info("DEDUPE RESOLUTION (%d records)", dedupe_total)
        logger.info("  thresholds: duplicate >= 85, review >= 60 (combined score in-radius)")
        logger.info("=" * 72)

    for index, canonical in enumerate(canonical_records, start=1):
        try:
            result_row, delta = _process_dedupe_record(
                canonical,
                resolver,
                sf_client,
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
        output = _write_dedupe_results(run_dir, result_rows)
        logger.info("Wrote dedupe results to %s", output.resolve())

    if verbose:
        logger.info("")
        logger.info("#" * 72)
        logger.info("FINAL SUMMARY")
        logger.info("  processed  : %d", summary["processed"])
        logger.info("  duplicates : %d  (skip — already in Salesforce)", summary["duplicates"])
        logger.info("  review     : %d  (manual check — see review_log.csv)", summary["review"])
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
    for index, canonical in enumerate(canonical_records, start=1):
        try:
            result_row, delta = _process_dedupe_record(
                canonical,
                resolver,
                sf_client,
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
            if sf_client and not dry_run:
                sf_client.create_site(classified)
                summary["loaded"] += 1
                logger.info("Loaded net-new site: %s", canonical["address"])
            else:
                logger.info(
                    "Classified net-new site%s: %s",
                    " (dry-run, not uploaded)" if dry_run else "",
                    canonical["address"],
                )

        except Exception as exc:
            summary["errors"] += 1
            logger.exception("Failed to process record: %s", exc)

    if result_rows:
        output = _write_dedupe_results(run_dir, result_rows)
        logger.info("Wrote dedupe results to %s", output)

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
