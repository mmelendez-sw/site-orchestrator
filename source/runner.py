"""CLI for running permit source adapters."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from source.adapters import ADAPTERS, get_adapter
from source.exporter import export_assets_csv, export_source_json
from source.record import SourceRecord

logger = logging.getLogger(__name__)


def list_sources() -> list[str]:
    return sorted(ADAPTERS)


def run_source(name: str, **kwargs) -> list[SourceRecord]:
    adapter = get_adapter(name)
    logger.info("Running source adapter: %s", name)
    return adapter.fetch(**kwargs)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a permit source adapter")
    parser.add_argument("source", choices=list_sources(), help="Source adapter name")
    parser.add_argument(
        "--output-csv",
        help="Write classifier-ready assets CSV (e.g. data/WI_assets.csv)",
    )
    parser.add_argument(
        "--output-json",
        help="Write source records JSON for Claude/manual review",
    )
    parser.add_argument(
        "--no-enrich",
        action="store_true",
        help="Skip MPROP enrichment (Milwaukee only)",
    )
    parser.add_argument(
        "--no-dedupe-addresses",
        action="store_true",
        help="Keep all permit rows instead of one row per address",
    )
    parser.add_argument(
        "--label",
        default="WI",
        help="Label column value for assets CSV export",
    )
    return parser.parse_args()


def main() -> list[SourceRecord]:
    args = _parse_args()
    records = run_source(
        args.source,
        enrich=not args.no_enrich,
        dedupe_addresses=not args.no_dedupe_addresses,
    )
    logger.info("Fetched %s source records from %s", len(records), args.source)

    if args.output_csv:
        path = export_assets_csv(records, args.output_csv, label=args.label)
        logger.info("Wrote assets CSV: %s", path)
    if args.output_json:
        path = export_source_json(records, args.output_json)
        logger.info("Wrote source JSON: %s", path)

    if not args.output_csv and not args.output_json:
        print(json.dumps([
            {
                "site_id": r.site_id,
                "address": r.full_address,
                "source_name": r.source_name,
                "permit_metadata": r.permit_metadata,
            }
            for r in records
        ], indent=2))

    return records


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    main()
