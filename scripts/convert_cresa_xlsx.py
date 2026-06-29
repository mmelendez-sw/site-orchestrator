"""Convert Cresa_Subleases_and_Sales.xlsx to an orchestrator-ready CSV."""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd


def parse_city_state(val) -> tuple[str | None, str | None]:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None, None
    text = str(val).strip()
    if "," in text:
        city, state = text.rsplit(",", 1)
        return city.strip(), state.strip()
    return text, None


def norm_zip(val) -> str | None:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    text = str(val).strip()
    if not text or text.lower() == "nan":
        return None
    return text


def convert(src: Path, out: Path) -> int:
    df = pd.read_excel(src)
    rows = []
    for _, row in df.iterrows():
        city, state = parse_city_state(row.get("City/State"))
        rows.append({
            "id": f"cresa_{int(row['#']):03d}",
            "address": str(row["Address"]).strip(),
            "zip_code": norm_zip(row.get("ZIP/Postal")),
            "label": "cresa",
            "input_confidence": "high",
            "city_state": row.get("City/State"),
            "city": city,
            "state": state,
            "title": row.get("Title"),
            "property_type": row.get("Property Type"),
            "transaction": row.get("Transaction"),
            "space_size": row.get("Space Size"),
            "unit": row.get("Unit"),
            "listing_url": row.get("Listing URL"),
        })

    out.parent.mkdir(parents=True, exist_ok=True)
    out_df = pd.DataFrame(rows)
    out_df.to_csv(out, index=False)
    return len(out_df)


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        default=root / "Cresa_Subleases_and_Sales.xlsx",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=root / "data" / "cresa_subleases_and_sales.csv",
    )
    args = parser.parse_args()
    count = convert(args.input, args.output)
    print(f"Wrote {count} rows to {args.output}")


if __name__ == "__main__":
    main()
