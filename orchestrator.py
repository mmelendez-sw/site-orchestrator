"""Site orchestrator: ingest -> dedup -> classify -> Salesforce."""

from __future__ import annotations

import csv
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from classifier.asset_classifier import classify_record
from dedup.resolver import SiteResolver
from ingest.normalizer import normalize
from ingest.scraper import IngestRecord
from salesforce.sf_client import SalesforceClient

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


def _to_ingest(raw: dict[str, Any] | IngestRecord) -> IngestRecord:
    if isinstance(raw, IngestRecord):
        return raw
    return IngestRecord(
        address=raw.get("address"),
        lat=raw.get("lat"),
        lng=raw.get("lng"),
        permit_metadata=dict(raw.get("permit_metadata") or {}),
        source_url=raw.get("source_url"),
    )


def main(raw_records: list[dict[str, Any] | IngestRecord] | None = None) -> dict[str, int]:
    """Process permit records through normalize, dedup, classify, and load."""
    if raw_records is None:
        raw_records = []

    run_dir = RUNS_DIR / f"orchestrator_{datetime.now().strftime('%Y-%m-%d_%H%M%S')}"
    run_dir.mkdir(parents=True, exist_ok=True)

    resolver = SiteResolver()
    sf_client = SalesforceClient()

    summary = {
        "processed": 0,
        "duplicates": 0,
        "review": 0,
        "net_new": 0,
        "loaded": 0,
        "errors": 0,
    }

    for raw in raw_records:
        summary["processed"] += 1
        try:
            canonical = normalize(_to_ingest(raw))
            resolution = resolver.resolve(canonical)
            status = resolution["status"]

            if status == "duplicate":
                matched = resolution.get("matched_record") or {}
                sf_client.log_duplicate(canonical, matched.get("Id", ""))
                summary["duplicates"] += 1
                logger.info("Duplicate skipped: %s (score=%s)", canonical["address"], resolution["score"])
                continue

            if status == "review":
                _log_review(canonical, resolution)
                summary["review"] += 1
                logger.info("Review queued: %s (score=%s)", canonical["address"], resolution["score"])
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


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    main([])
