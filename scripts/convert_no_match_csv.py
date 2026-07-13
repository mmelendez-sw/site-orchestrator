"""Convert no_match.csv to classifier-ready CSVs (full + NAIP subset)."""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

# NAIP covers the lower 48; exclude AK, HI, PR, and other out-of-coverage points.
NAIP_LAT_MIN = 24.5
NAIP_LAT_MAX = 49.0
NAIP_LON_MIN = -125.0
NAIP_LON_MAX = -66.5


def clean_no_match(df: pd.DataFrame) -> pd.DataFrame:
    """Map Salesforce no-match export to classifier input columns."""
    required = {"asset_id", "asset_lat", "asset_lon"}
    missing = required - set(df.columns)
    if missing:
        raise SystemExit(f"no_match.csv missing columns: {sorted(missing)}")

    out = pd.DataFrame({
        "id": df["asset_id"].astype(str).str.strip(),
        "lat": pd.to_numeric(df["asset_lat"], errors="coerce"),
        "lon": pd.to_numeric(df["asset_lon"], errors="coerce"),
        "label": "no_match",
        "input_confidence": "medium",
        "confidence_tier": df.get("confidence_tier"),
        "review_flag": df.get("review_flag"),
        "asset_type": df.get("asset_type"),
    })

    out = out.dropna(subset=["id", "lat", "lon"])
    out = out.drop_duplicates(subset=["id"], keep="first")
    return out.reset_index(drop=True)


def naip_eligible(df: pd.DataFrame) -> pd.DataFrame:
    mask = (
        df["lat"].between(NAIP_LAT_MIN, NAIP_LAT_MAX)
        & df["lon"].between(NAIP_LON_MIN, NAIP_LON_MAX)
    )
    return df[mask].reset_index(drop=True)


def sample_subset(df: pd.DataFrame, n: int, *, seed: int = 42) -> pd.DataFrame:
    if n >= len(df):
        return df.copy()
    idx = pd.Series(range(len(df))).sample(n, random_state=seed).sort_values()
    subset = df.iloc[idx].reset_index(drop=True)
    subset["id"] = [f"no_match_pilot_{i + 1:03d}" for i in range(len(subset))]
    subset["source_id"] = df.iloc[idx]["id"].values
    return subset


def convert(
    src: Path,
    *,
    full_out: Path,
    subset_out: Path | None,
    subset_size: int,
    seed: int,
) -> tuple[int, int, int]:
    raw = pd.read_csv(src)
    cleaned = clean_no_match(raw)
    eligible = naip_eligible(cleaned)
    eligible.to_csv(full_out, index=False)

    subset_n = 0
    if subset_out is not None:
        subset = sample_subset(eligible, subset_size, seed=seed)
        subset.to_csv(subset_out, index=False)
        subset_n = len(subset)

    return len(raw), len(eligible), subset_n


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=root / "no_match.csv")
    parser.add_argument(
        "--output",
        type=Path,
        default=root / "data" / "no_match_classify.csv",
    )
    parser.add_argument(
        "--subset-output",
        type=Path,
        default=root / "data" / "no_match_naip_150.csv",
    )
    parser.add_argument("--subset-size", type=int, default=150)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    raw_n, eligible_n, subset_n = convert(
        args.input,
        full_out=args.output,
        subset_out=args.subset_output,
        subset_size=args.subset_size,
        seed=args.seed,
    )
    print(f"Read {raw_n} rows from {args.input}")
    print(f"Wrote {eligible_n} NAIP-eligible rows to {args.output}")
    if args.subset_output:
        print(f"Wrote {subset_n} row subset to {args.subset_output}")


if __name__ == "__main__":
    main()
