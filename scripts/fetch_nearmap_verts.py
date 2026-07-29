"""Fetch Nearmap imagery only — no AI / no NAIP.

Default pull per site:
  - Vert (overhead) at 50 m
  - one oblique at 50 m
  - one oblique at 250 m

Oblique preference: North, then East, South, West (first with coverage wins).
Preserves every input CSV column and appends photo + capture-date columns.

Requires NEARMAP_API_KEY in the environment or .env.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from classifier.asset_classifier import (  # noqa: E402
    NEARMAP_API_KEY,
    OBLIQUE_VIEWS,
    fetch_nearmap_views,
    setup_run_directory,
)

# Local annotate helper (no API)
from scripts.annotate_nearmap_arrows import (  # noqa: E402
    draw_poi_arrow,
    write_xlsx,
)

PHOTO_COLS = (
    "Photo Vert 50m",
    "Photo Oblique 50m",
    "Photo Oblique 250m",
)
DATE_COL = "Capture Date"


def _col(df: pd.DataFrame, *names: str) -> str | None:
    lower = {c.lower().strip(): c for c in df.columns}
    for name in names:
        if name.lower() in lower:
            return lower[name.lower()]
    return None


def _id_key(val) -> str:
    return str(val).strip()


def load_input(path: Path) -> tuple[pd.DataFrame, str, str, str]:
    df = pd.read_csv(path)
    id_col = _col(df, "id", "easement id", "easement_id", "site_id")
    lat_col = _col(df, "lat", "latitude", "y")
    lon_col = _col(df, "lon", "lng", "long", "longitude", "x")
    if not id_col or not lat_col or not lon_col:
        raise SystemExit(
            f"Need id/lat/lon columns in {path}. Found: {list(df.columns)}"
        )
    if df.empty:
        raise SystemExit(f"No rows in {path}")
    return df, id_col, lat_col, lon_col


def _pick_oblique(lat: float, lon: float, chip_m: float,
                  prefer: list[str]) -> tuple[str | None, object | None, str | None]:
    """Return (view_name, image, capture_date) for the first oblique with coverage."""
    views, capture_date = fetch_nearmap_views(
        lat, lon, chip_m=chip_m, views=prefer,
    )
    for name in prefer:
        if name in views:
            return name, views[name], capture_date
    return None, None, capture_date


def _save_chip(img, path: Path) -> Path:
    """Save chip with blue POI arrow at image center (AOI lat/lon)."""
    marked = draw_poi_arrow(img)
    marked.save(path, quality=92)
    return path


def _site_complete(row) -> bool:
    return all(str(row.get(c) or "").strip() for c in PHOTO_COLS)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Nearmap vert 50m + oblique 50m + oblique 250m (no AI)")
    parser.add_argument(
        "--input", "-i",
        default=str(_REPO_ROOT / "Alma Realty_Locations.csv"),
        help="Input CSV (all columns preserved in output)",
    )
    parser.add_argument(
        "--vert-m",
        type=float,
        default=50.0,
        help="Overhead (Vert) AOI side length in meters (default: 50)",
    )
    parser.add_argument(
        "--oblique-near-m",
        type=float,
        default=50.0,
        help="Near oblique AOI side length in meters (default: 50)",
    )
    parser.add_argument(
        "--oblique-far-m",
        type=float,
        default=250.0,
        help="Far oblique AOI side length in meters (default: 250)",
    )
    parser.add_argument(
        "--oblique-prefer",
        default="North,East,South,West",
        help="Oblique direction preference order (first with coverage wins)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only fetch the first N rows from the input CSV",
    )
    parser.add_argument(
        "--run-dir",
        default=None,
        help="Resume an existing runs/ folder",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-fetch even when all photo columns are already set",
    )
    args = parser.parse_args()

    if not NEARMAP_API_KEY:
        raise SystemExit(
            "NEARMAP_API_KEY is not set. Add it to .env or the environment."
        )

    input_path = Path(args.input)
    if not input_path.is_file():
        raise SystemExit(f"Input not found: {input_path}")

    prefer = [v.strip() for v in args.oblique_prefer.split(",") if v.strip()]
    bad = [v for v in prefer if v not in OBLIQUE_VIEWS]
    if bad:
        raise SystemExit(
            f"Unknown oblique views {bad}. Allowed: {OBLIQUE_VIEWS}"
        )

    df, id_col, lat_col, lon_col = load_input(input_path)
    if args.limit is not None:
        if args.limit < 1:
            raise SystemExit("--limit must be >= 1")
        df = df.head(args.limit).copy()

    prefix = input_path.stem.replace(" ", "_")
    run_root = setup_run_directory(prefix, args.run_dir)
    chip_dir = run_root / "chips"
    output_path = run_root / f"{prefix}_with_nearmap.csv"

    if not args.run_dir:
        shutil.copy2(input_path, run_root / input_path.name)

    extra_cols = list(PHOTO_COLS) + [DATE_COL]
    if output_path.is_file():
        out_df = pd.read_csv(output_path)
        for col in list(df.columns) + extra_cols:
            if col not in out_df.columns:
                out_df[col] = ""
        out_df = out_df.set_index(out_df[id_col].map(_id_key), drop=False)
    else:
        out_df = df.copy()
        for col in extra_cols:
            out_df[col] = ""
        out_df = out_df.set_index(out_df[id_col].map(_id_key), drop=False)

    # Drop legacy single Photo column if present
    drop_legacy = [c for c in out_df.columns
                   if c == "Photo" or c.startswith("Photo ") and c not in PHOTO_COLS]
    if drop_legacy:
        out_df = out_df.drop(columns=drop_legacy, errors="ignore")

    out_df = out_df[
        [c for c in list(df.columns) + extra_cols if c in out_df.columns]
    ]

    done_ids: set[str] = set()
    if not args.force:
        for i, row in out_df.iterrows():
            if _site_complete(row):
                done_ids.add(_id_key(i))

    pending_idxs = [
        idx for idx, row in df.iterrows()
        if args.force or _id_key(row[id_col]) not in done_ids
    ]
    print(
        f"Nearmap Vert {args.vert_m:g}m + oblique {args.oblique_near_m:g}m "
        f"+ oblique {args.oblique_far_m:g}m | {len(df)} sites | "
        f"{len(done_ids)} done | {len(pending_idxs)} to fetch",
        flush=True,
    )
    print(f"Run folder: {run_root}", flush=True)
    print(f"Oblique prefer: {','.join(prefer)}", flush=True)

    for n, idx in enumerate(pending_idxs, 1):
        row = df.loc[idx]
        asset_id = _id_key(row[id_col])
        try:
            lat = float(row[lat_col])
            lon = float(row[lon_col])
        except (TypeError, ValueError):
            print(f"[{n}/{len(pending_idxs)}] {asset_id}  skip: bad lat/lon",
                  flush=True)
            continue

        print(
            f"[{n}/{len(pending_idxs)}] {asset_id}  {lat:.6f},{lon:.6f}",
            flush=True,
        )
        t0 = time.time()
        capture_dates: list[str] = []

        if asset_id not in out_df.index:
            new_row = {c: row.get(c, "") for c in df.columns}
            for col in extra_cols:
                new_row[col] = ""
            out_df.loc[asset_id] = new_row

        # 1) Overhead Vert @ near AOI
        vert_views, vert_date = fetch_nearmap_views(
            lat, lon, chip_m=args.vert_m, views=["Vert"],
        )
        if vert_date:
            capture_dates.append(str(vert_date))
        vert = vert_views.get("Vert")
        if vert is not None:
            path = chip_dir / f"{asset_id}_nearmap_vert_{int(args.vert_m)}m.jpg"
            _save_chip(vert, path)
            out_df.at[asset_id, PHOTO_COLS[0]] = str(path.as_posix())
            print(f"  saved {path.name}", flush=True)
        else:
            out_df.at[asset_id, PHOTO_COLS[0]] = ""
            print("  no Vert coverage", flush=True)

        # 2) One oblique @ near AOI
        name_n, img_n, date_n = _pick_oblique(
            lat, lon, args.oblique_near_m, prefer,
        )
        if date_n:
            capture_dates.append(str(date_n))
        if img_n is not None and name_n:
            path = (chip_dir /
                    f"{asset_id}_nearmap_{name_n.lower()}_"
                    f"{int(args.oblique_near_m)}m.jpg")
            _save_chip(img_n, path)
            out_df.at[asset_id, PHOTO_COLS[1]] = str(path.as_posix())
            print(f"  saved {path.name}", flush=True)
        else:
            out_df.at[asset_id, PHOTO_COLS[1]] = ""
            print(f"  no oblique coverage @ {args.oblique_near_m:g}m",
                  flush=True)

        # 3) One oblique @ far AOI
        name_f, img_f, date_f = _pick_oblique(
            lat, lon, args.oblique_far_m, prefer,
        )
        if date_f:
            capture_dates.append(str(date_f))
        if img_f is not None and name_f:
            path = (chip_dir /
                    f"{asset_id}_nearmap_{name_f.lower()}_"
                    f"{int(args.oblique_far_m)}m.jpg")
            _save_chip(img_f, path)
            out_df.at[asset_id, PHOTO_COLS[2]] = str(path.as_posix())
            print(f"  saved {path.name}", flush=True)
        else:
            out_df.at[asset_id, PHOTO_COLS[2]] = ""
            print(f"  no oblique coverage @ {args.oblique_far_m:g}m",
                  flush=True)

        # Prefer a single capture date (all surveys usually match)
        out_df.at[asset_id, DATE_COL] = capture_dates[0] if capture_dates else ""
        print(
            f"  date={out_df.at[asset_id, DATE_COL] or 'unknown'}  "
            f"({time.time() - t0:.1f}s)",
            flush=True,
        )

        save_df = out_df.reset_index(drop=True)
        save_df = save_df[
            [c for c in list(df.columns) + extra_cols if c in save_df.columns]
        ]
        order = {_id_key(v): i for i, v in enumerate(df[id_col])}
        save_df["_ord"] = save_df[id_col].map(
            lambda v: order.get(_id_key(v), 10_000)
        )
        save_df = save_df.sort_values("_ord").drop(columns="_ord")
        save_df.to_csv(output_path, index=False)

    ok = sum(1 for _, r in out_df.iterrows() if _site_complete(r))
    save_df = out_df.reset_index(drop=True)
    save_df = save_df[
        [c for c in list(df.columns) + extra_cols if c in save_df.columns]
    ]
    xlsx_path = run_root / f"{prefix}_with_nearmap.xlsx"
    try:
        saved_xlsx = write_xlsx(save_df, xlsx_path, PHOTO_COLS)
    except Exception as exc:
        saved_xlsx = None
        print(f"Excel export skipped: {exc}", flush=True)
    print(
        f"\nDone. {ok} sites with all 3 photos.\n"
        f"  chips:   {chip_dir}\n"
        f"  output:  {output_path}"
        + (f"\n  xlsx:    {saved_xlsx}" if saved_xlsx else ""),
        flush=True,
    )


if __name__ == "__main__":
    main()
