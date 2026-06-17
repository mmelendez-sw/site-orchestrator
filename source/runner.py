"""CLI for running permit source adapters."""

from __future__ import annotations

import argparse
import json
import logging

from source.adapters import ADAPTERS, adapters_for_scope, get_adapter
from source.exporter import export_assets_csv, export_source_json
from source.record import SourceRecord
from source.scope import SourceScope, parse_scope

logger = logging.getLogger(__name__)


def list_sources(scope: SourceScope | None = None) -> list[str]:
    if scope is None:
        return sorted(ADAPTERS)
    return adapters_for_scope(scope)


def run_source(
    name: str,
    *,
    scope: SourceScope | None = None,
    **kwargs,
) -> list[SourceRecord]:
    adapter = get_adapter(name)
    if scope and not adapter.supports_scope(scope):
        compatible = ", ".join(adapters_for_scope(scope))
        raise ValueError(
            f"Source '{name}' does not support scope {scope.to_metadata()}. "
            f"Compatible sources: {compatible or 'none'}"
        )
    logger.info("Running source adapter: %s", name)
    return adapter.fetch(scope=scope, **kwargs)


def _add_scope_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--country", help="Country scope (e.g. US)")
    parser.add_argument("--state", help="State scope (e.g. WI)")
    parser.add_argument("--county", help="County scope (e.g. Milwaukee County)")
    parser.add_argument("--city", help="City scope (e.g. Milwaukee)")
    parser.add_argument("--zip", dest="zip_codes", help="Comma-separated zip codes")


def _scope_from_args(args: argparse.Namespace) -> SourceScope | None:
    if not any([args.country, args.state, args.county, args.city, args.zip_codes]):
        return None
    return parse_scope(
        country=args.country,
        state=args.state,
        county=args.county,
        city=args.city,
        zip_codes=args.zip_codes,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a permit source adapter")
    parser.add_argument(
        "source",
        nargs="?",
        choices=sorted(ADAPTERS),
        help="Source adapter name",
    )
    parser.add_argument(
        "--list-sources",
        action="store_true",
        help="List available source adapters for the provided scope",
    )
    parser.add_argument(
        "--input",
        help="Input CSV/JSON path (required for the file source)",
    )
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
        help="Label column value for assets CSV export (defaults to scope state)",
    )
    _add_scope_args(parser)
    return parser.parse_args()


def main() -> list[SourceRecord]:
    args = _parse_args()
    scope = _scope_from_args(args)

    if args.list_sources:
        print(json.dumps(list_sources(scope), indent=2))
        return []

    if not args.source:
        raise SystemExit("Provide a source name or use --list-sources")

    fetch_kwargs = {
        "enrich": not args.no_enrich,
        "dedupe_addresses": not args.no_dedupe_addresses,
    }
    if args.source == "file":
        if not args.input:
            raise SystemExit("The file source requires --input")
        fetch_kwargs["input_path"] = args.input

    records = run_source(args.source, scope=scope, **fetch_kwargs)
    logger.info("Fetched %s source records from %s", len(records), args.source)

    label = args.label or (scope.label if scope else None)
    if args.output_csv:
        path = export_assets_csv(records, args.output_csv, label=label)
        logger.info("Wrote assets CSV: %s", path)
    if args.output_json:
        path = export_source_json(records, args.output_json)
        logger.info("Wrote source JSON: %s", path)

    if not args.output_csv and not args.output_json:
        print(json.dumps([
            {
                "site_id": r.site_id,
                "address": r.full_address,
                "city": r.city,
                "state": r.state,
                "county": r.county,
                "country": r.country,
                "zip_code": r.zip_code,
                "source_name": r.source_name,
                "permit_metadata": r.permit_metadata,
            }
            for r in records
        ], indent=2))

    return records


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    main()
