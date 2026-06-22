"""Export net-new dedupe rows to the Salesforce upload CSV template."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import Any

from salesforce.upload_template import build_upload_record, write_upload_csv


def load_dedupe_results(path: Path) -> list[dict[str, Any]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def export_from_dedupe_results(
    dedupe_csv: Path,
    *,
    output: Path | None = None,
) -> Path:
    rows = load_dedupe_results(dedupe_csv)
    net_new = [row for row in rows if (row.get("status") or "").lower() == "net_new"]
    upload_records = [
        build_upload_record(
            {
                "address": row.get("address"),
                "lat": row.get("lat"),
                "lng": row.get("lng"),
                "zip_code": row.get("zip_code"),
            },
            dedupe_row=row,
        )
        for row in net_new
    ]
    target = output or dedupe_csv.parent / "sf_upload.csv"
    return write_upload_csv(upload_records, target)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build Salesforce upload CSV from dedupe_results.csv net-new rows"
    )
    parser.add_argument(
        "--input",
        required=True,
        help="Path to dedupe_results.csv",
    )
    parser.add_argument(
        "--output",
        help="Output CSV path (default: sf_upload.csv beside input)",
    )
    args = parser.parse_args()
    input_path = Path(args.input)
    output_path = Path(args.output) if args.output else None
    result = export_from_dedupe_results(input_path, output=output_path)
    print(f"Wrote {result.resolve()}")


if __name__ == "__main__":
    main()
