"""Merge zip population rows into data/zip_populations.csv.

DC counts sourced from demographics-us.com (2021 Census estimates):
https://demographics-us.com/zips/dc
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

# demographics-us.com District of Columbia ZIP table (2021 Census).
DC_ZIP_POPULATIONS: dict[str, int] = {
    "20001": 45551,
    "20002": 68201,
    "20003": 34535,
    "20004": 1685,
    "20005": 13444,
    "20006": 1072,
    "20007": 24311,
    "20008": 30461,
    "20009": 51288,
    "20010": 33656,
    "20011": 67386,
    "20012": 17526,
    "20015": 15677,
    "20016": 34422,
    "20017": 20221,
    "20018": 20084,
    "20019": 64031,
    "20020": 54812,
    "20024": 14525,
    "20032": 40716,
    "20036": 4669,
    "20037": 13037,
    "20045": 0,
    "20052": 3581,
    "20057": 3914,
    "20059": 1518,
    "20064": 1873,
    "20204": 0,
    "20220": 0,
    "20230": 0,
    "20240": 0,
    "20245": 0,
    "20250": 0,
    "20260": 0,
    "20317": 0,
    "20319": 50,
    "20373": 437,
    "20388": 0,
    "20390": 404,
    "20408": 0,
    "20415": 0,
    "20418": 0,
    "20422": 67,
    "20427": 0,
    "20431": 0,
    "20510": 0,
    "20515": 0,
    "20520": 0,
    "20530": 0,
    "20535": 0,
    "20540": 0,
    "20542": 0,
    "20551": 0,
    "20560": 0,
    "20565": 0,
    "20566": 0,
    "20591": 0,
    # Federal / agency zips in asset list but absent from public table.
    "20071": 0,
    "20210": 0,
    "20242": 0,
    "20433": 0,
    "20463": 0,
    "20548": 0,
    "20549": 0,
    "20554": 0,
    "20585": 0,
    "20590": 0,
    "20597": 0,
}


def load_existing(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}
    populations: dict[str, int] = {}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            zip_code = (row.get("zip") or row.get("zip_code") or "").strip()
            if not zip_code:
                continue
            populations[zip_code] = int(row["population"])
    return populations


def merge_populations(*sources: dict[str, int]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for source in sources:
        merged.update(source)
    return merged


def write_populations(path: Path, populations: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["zip", "population"])
        for zip_code in sorted(populations):
            writer.writerow([zip_code, populations[zip_code]])


def main() -> None:
    parser = argparse.ArgumentParser(description="Build data/zip_populations.csv")
    parser.add_argument(
        "--output",
        default="data/zip_populations.csv",
        help="Output CSV path",
    )
    parser.add_argument(
        "--include-dc",
        action="store_true",
        default=True,
        help="Merge DC zip populations from demographics-us.com",
    )
    args = parser.parse_args()
    output = Path(args.output)
    existing = load_existing(output)
    sources = [existing]
    if args.include_dc:
        sources.append(DC_ZIP_POPULATIONS)
    merged = merge_populations(*sources)
    write_populations(output, merged)
    print(f"Wrote {len(merged)} zips to {output.resolve()}")


if __name__ == "__main__":
    main()
