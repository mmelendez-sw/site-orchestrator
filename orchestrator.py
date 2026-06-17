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
                "matched_id",
                "matched_address",
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
                "matched_id",
                "matched_address",
            ],
        )
        writer.writerow({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "address": record.get("address"),
            "lat": record.get("lat"),
            "lng": record.get("lng"),
            "score": resolution.get("score"),
            "matched_id": matched.get("Id"),
            "matched_address": matched.get("Address__c") or matched.get("Name"),
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
) -> tuple[list[dict[str, Any]], list[tuple[Any, Exception]]]:
    canonical_records: list[dict[str, Any]] = []
    failures: list[tuple[Any, Exception]] = []
    for raw in raw_records:
        try:
            canonical_records.append(normalize(_to_ingest(raw)))
        except Exception as exc:
            failures.append((raw, exc))
    return canonical_records, failures


def run_dedupe_pipeline(
    raw_records: list[dict[str, Any] | IngestRecord | SourceRecord],
) -> dict[str, int]:
    """Normalize source records and run Salesforce dedupe only."""
    canonical_records, failures = _normalize_batch(raw_records)
    resolver = SiteResolver()
    sf_client = SalesforceClient()

    summary = {
        "processed": len(raw_records),
        "duplicates": 0,
        "review": 0,
        "net_new": 0,
        "loaded": 0,
        "errors": len(failures),
    }

    if canonical_records:
        candidates = resolver.prefetch(canonical_records)
        logger.info(
            "Prefetched %s Salesforce candidates for %s dataset zip codes",
            len(candidates),
            len((resolver._dataset_context or {}).get("zip_codes", [])),
        )

    for _, exc in failures:
        logger.exception("Failed to normalize record: %s", exc)

    for canonical in canonical_records:
        try:
            resolution = resolver.resolve(canonical)
            status = resolution["status"]

            if status == "duplicate":
                matched = resolution.get("matched_record") or {}
                sf_client.log_duplicate(canonical, matched.get("Id", ""))
                summary["duplicates"] += 1
                logger.info(
                    "Duplicate skipped: %s (score=%s)",
                    canonical["address"],
                    resolution["score"],
                )
                continue

            if status == "review":
                _log_review(canonical, resolution)
                summary["review"] += 1
                logger.info(
                    "Review queued: %s (score=%s)",
                    canonical["address"],
                    resolution["score"],
                )
                continue

            summary["net_new"] += 1
            logger.info("Net-new candidate: %s", canonical["address"])

        except Exception as exc:
            summary["errors"] += 1
            logger.exception("Failed to dedupe record: %s", exc)

    logger.info(
        "Dedupe summary — processed=%s duplicates=%s review=%s net_new=%s errors=%s",
        summary["processed"],
        summary["duplicates"],
        summary["review"],
        summary["net_new"],
        summary["errors"],
    )
    return summary


def main(
    raw_records: list[dict[str, Any] | IngestRecord | SourceRecord] | None = None,
    *,
    classify: bool = True,
) -> dict[str, int]:
    """Process records through normalize, dedupe, optional classify, and load."""
    if raw_records is None:
        raw_records = []

    if not classify:
        return run_dedupe_pipeline(raw_records)

    canonical_records, failures = _normalize_batch(raw_records)
    run_dir = RUNS_DIR / f"orchestrator_{datetime.now().strftime('%Y-%m-%d_%H%M%S')}"
    run_dir.mkdir(parents=True, exist_ok=True)

    resolver = SiteResolver()
    sf_client = SalesforceClient()

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

    for canonical in canonical_records:
        try:
            resolution = resolver.resolve(canonical)
            status = resolution["status"]

            if status == "duplicate":
                matched = resolution.get("matched_record") or {}
                sf_client.log_duplicate(canonical, matched.get("Id", ""))
                summary["duplicates"] += 1
                logger.info(
                    "Duplicate skipped: %s (score=%s)",
                    canonical["address"],
                    resolution["score"],
                )
                continue

            if status == "review":
                _log_review(canonical, resolution)
                summary["review"] += 1
                logger.info(
                    "Review queued: %s (score=%s)",
                    canonical["address"],
                    resolution["score"],
                )
                continue

            summary["net_new"] += 1
            classified = classify_record(canonical, run_dir=run_dir)
            sf_client.create_site(classified)
            summary["loaded"] += 1
            logger.info("Loaded net-new site: %s", canonical["address"])

        except Exception as exc:
            summary["errors"] += 1
            logger.exception("Failed to process record: %s", exc)

    logger.info(
        "Summary — processed=%s duplicates=%s review=%s net_new=%s loaded=%s errors=%s",
        summary["processed"],
        summary["duplicates"],
        summary["review"],
        summary["net_new"],
        summary["loaded"],
        summary["errors"],
    )
    return summary


def run_from_source(
    source_name: str,
    *,
    classify: bool = False,
    scope: Any = None,
    **source_kwargs: Any,
) -> dict[str, int]:
    """Run a permit source adapter, then hand off to ingest + Salesforce dedupe."""
    records = run_source(source_name, scope=scope, **source_kwargs)
    logger.info("Source '%s' produced %s records", source_name, len(records))
    return main(records, classify=classify)


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
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _parse_args()
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
            scope=scope,
            **source_kwargs,
        )
    else:
        main([])
