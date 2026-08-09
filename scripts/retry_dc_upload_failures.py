"""Rebuild the 20 failed DC upload rows and optionally update leasing source.

Uses existing classify results (no re-classify). Fixes Nominatim city/street
parse, sets Carrier Leasing Source to JF_PermitScraping_aug2026, then can:
  --write-csv     write retry sf_upload CSV
  --update-loaded update Carrier_Leasing_Source__c on already-loaded run Ids
  --upload        create the retry rows via Salesforce API
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

from ingest.address_parts import parse_address_components
from salesforce.field_map import OBJECT_NAME
from salesforce.sf_client import SalesforceClient, map_upload_record_to_payload
from salesforce.site_type_mapping import map_site_type_for_upload
from salesforce.upload_template import (
    UPLOAD_CSV_COLUMNS,
    csv_row_to_upload_record,
    default_carrier_leasing_source,
    upload_record_to_csv_row,
    validate_upload_record,
    write_upload_csv,
)

load_dotenv()
logger = logging.getLogger(__name__)

TARGET_LEASING = "JF_PermitScraping_aug2026"
DEFAULT_RUN = ROOT / "runs" / "orchestrator_2026-08-08_130233"

# Streets that failed with Salesforce "Malformed request" (local CSV looked valid).
MALFORMED_STREETS = {
    "110 IRVING ST NW",
    "1150 VARNUM ST NE",
    "1221 22ND ST NW",
    "1250 4TH ST SW",
    "1301 DELAWARE AVE SW",
    "1375 FAIRMONT ST NW",
    "1500 S CAPITOL ST SE",
}


def _norm_street(value: str) -> str:
    text = re.sub(r"\s+", " ", (value or "").strip().upper())
    text = text.replace("4TH", "4TH").replace("22ND", "22ND")
    return text


def _round_coord(value: Any) -> str:
    try:
        return f"{float(value):.5f}"
    except (TypeError, ValueError):
        return ""


def parse_loaded_ids(summary_path: Path) -> list[str]:
    """Extract loaded Salesforce Ids from RUN_SUMMARY.txt."""
    text = summary_path.read_text(encoding="utf-8", errors="replace")
    ids: list[str] = []
    in_loaded = False
    for line in text.splitlines():
        if line.strip().startswith("loaded"):
            in_loaded = True
            continue
        if in_loaded and line.strip().startswith("failed"):
            break
        if not in_loaded:
            continue
        match = re.search(r"\(Id=([a-zA-Z0-9]+)\)", line)
        if match:
            ids.append(match.group(1))
    return ids


def build_retry_records(run_dir: Path) -> list[dict[str, Any]]:
    upload_rows = list(
        csv.DictReader((run_dir / "sf_upload.csv").open(encoding="utf-8-sig", newline=""))
    )
    detail_rows = list(
        csv.DictReader((run_dir / "results_detail.csv").open(encoding="utf-8-sig", newline=""))
    )
    detail_by_coord = {
        (_round_coord(r.get("lat")), _round_coord(r.get("lon"))): r for r in detail_rows
    }

    blank_city = [r for r in upload_rows if not (r.get("Site City") or "").strip()]
    malformed = [
        r
        for r in upload_rows
        if _norm_street(r.get("Site Street") or "") in {_norm_street(s) for s in MALFORMED_STREETS}
    ]

    # Prefer results_detail full address for blank-city (street was truncated to house/POI).
    selected: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def _append_from_upload(row: dict[str, str], *, full_address: str | None = None) -> None:
        carrier = TARGET_LEASING
        if full_address:
            parts = parse_address_components(
                full_address,
                zip_code=row.get("Site Zip Code") or None,
            )
            record = csv_row_to_upload_record(row)
            record.update({k: v for k, v in parts.items() if v})
            record["carrier_leasing_source"] = carrier
            record["address"] = full_address
        else:
            record = csv_row_to_upload_record(row)
            record["carrier_leasing_source"] = carrier
            # Re-title-case street/city in case we only swap leasing.
            parts = parse_address_components(
                ", ".join(
                    p
                    for p in [
                        row.get("Site Street"),
                        row.get("Site City"),
                        row.get("Site State"),
                        row.get("Site Zip Code"),
                    ]
                    if p
                ),
                zip_code=row.get("Site Zip Code") or None,
            )
            if parts.get("site_street"):
                record["site_street"] = parts["site_street"]
            if parts.get("site_city"):
                record["site_city"] = parts["site_city"]

        key = (
            _norm_street(str(record.get("site_street") or "")),
            str(record.get("zip_code") or ""),
        )
        if key in seen:
            logger.info("Skipping duplicate retry row: %s", key[0])
            return
        errors = validate_upload_record(record)
        if errors:
            logger.error("Still invalid after fix: %s — %s", key[0], errors)
            return
        seen.add(key)
        selected.append(record)

    for row in blank_city:
        detail = detail_by_coord.get(
            (_round_coord(row.get("Site Latitude")), _round_coord(row.get("Site Longitude")))
        )
        address = (detail or {}).get("address")
        if not address:
            logger.error(
                "No results_detail address for blank-city lat/lng %s,%s street=%r",
                row.get("Site Latitude"),
                row.get("Site Longitude"),
                row.get("Site Street"),
            )
            continue
        if detail and not (row.get("Site Type") or "").strip():
            mapped = map_site_type_for_upload(
                {
                    "site_type": detail.get("site_type"),
                    "tower_subtype": detail.get("tower_subtype"),
                    "cell_equipment": detail.get("cell_equipment"),
                }
            )
            if mapped:
                row = dict(row)
                row["Site Type"] = mapped
        _append_from_upload(row, full_address=address)

    for row in malformed:
        detail = detail_by_coord.get(
            (_round_coord(row.get("Site Latitude")), _round_coord(row.get("Site Longitude")))
        )
        if detail and not (row.get("Site Type") or "").strip():
            mapped = map_site_type_for_upload(
                {
                    "site_type": detail.get("site_type"),
                    "tower_subtype": detail.get("tower_subtype"),
                    "cell_equipment": detail.get("cell_equipment"),
                }
            )
            if mapped:
                row = dict(row)
                row["Site Type"] = mapped
        _append_from_upload(row)

    return selected


def update_loaded_leasing(run_dir: Path, *, dry_run: bool) -> dict[str, int]:
    ids = parse_loaded_ids(run_dir / "RUN_SUMMARY.txt")
    summary = {"total": len(ids), "updated": 0, "errors": 0}
    logger.info("Updating Carrier_Leasing_Source__c on %d loaded sites → %s", len(ids), TARGET_LEASING)
    if dry_run:
        logger.info("Dry-run: no Salesforce updates")
        return summary

    client = SalesforceClient()
    for index, record_id in enumerate(ids, start=1):
        try:
            getattr(client.sf, OBJECT_NAME).update(
                record_id,
                {"Carrier_Leasing_Source__c": TARGET_LEASING},
            )
            summary["updated"] += 1
            if index % 25 == 0 or index == len(ids):
                logger.info("  leasing update progress %d/%d", index, len(ids))
        except Exception as exc:
            summary["errors"] += 1
            logger.error("  failed Id=%s: %s", record_id, exc)
    return summary


def upload_records(records: list[dict[str, Any]], *, dry_run: bool, verbose: bool) -> dict[str, int]:
    summary = {"total": len(records), "loaded": 0, "errors": 0}
    client = None if dry_run else SalesforceClient()
    for index, record in enumerate(records, start=1):
        street = record.get("site_street")
        logger.info("[%d/%d] %s", index, len(records), street)
        if verbose:
            logger.info("  payload: %s", map_upload_record_to_payload(record))
        if dry_run:
            errors = validate_upload_record(record)
            if errors:
                summary["errors"] += 1
                logger.error("  validation: %s", errors)
            else:
                summary["loaded"] += 1
            continue
        assert client is not None
        try:
            result = client.create_site(record, verbose=verbose)
            summary["loaded"] += 1
            logger.info("  loaded Id=%s", result.get("id"))
        except Exception as exc:
            summary["errors"] += 1
            logger.error("  FAILED: %s", exc)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN)
    parser.add_argument("--write-csv", action="store_true")
    parser.add_argument("--update-loaded", action="store_true")
    parser.add_argument("--upload", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    run_dir = args.run_dir
    out_dir = run_dir / "retry_aug2026"
    out_dir.mkdir(parents=True, exist_ok=True)

    records = build_retry_records(run_dir)
    logger.info(
        "Retry cohort: %d unique rows (leasing=%s, env default=%s)",
        len(records),
        TARGET_LEASING,
        default_carrier_leasing_source(),
    )

    csv_path = out_dir / "sf_upload_retry.csv"
    if args.write_csv or args.upload or args.dry_run:
        write_upload_csv(records, csv_path, include_picklist_reference=False)
        logger.info("Wrote %s (%d rows)", csv_path, len(records))
        # Also dump a preview of fixed city/street.
        preview = out_dir / "retry_preview.csv"
        with preview.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(UPLOAD_CSV_COLUMNS))
            writer.writeheader()
            for record in records:
                writer.writerow(upload_record_to_csv_row(record))

    if args.update_loaded:
        leasing_summary = update_loaded_leasing(run_dir, dry_run=args.dry_run)
        logger.info("Leasing update summary: %s", leasing_summary)

    if args.upload or (args.dry_run and not args.update_loaded):
        if args.upload or args.dry_run:
            upload_summary = upload_records(records, dry_run=args.dry_run, verbose=args.verbose)
            logger.info("Upload summary: %s", upload_summary)
            if upload_summary["errors"]:
                return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
