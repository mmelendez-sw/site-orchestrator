"""Coalesce enrichment_detail (+ apply logs) into runs/_combined_enrichment_latest.csv.

Keeps the latest row per Salesforce Id (by run folder name). Optional --since YYYY-MM-DD.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "runs"
ARTIFACTS = RUNS / "_artifacts"
OUT_CSV = ARTIFACTS / "_combined_enrichment_latest.csv"
OUT_JSON = ARTIFACTS / "_outcome_matrix_summary.json"

OUT_COLS = [
    "Id",
    "bucket",
    "holdout_reason",
    "match_source",
    "match_distance_m",
    "naip_site_type",
    "naip_tower_subtype",
    "naip_site_confidence",
    "asset_offset_m",
    "update_coord_source",
    "update_site_type",
    "update_verified_site_source",
    "sf_update_status",
    "Site_Street__c",
    "Site_City__c",
    "Site_State__c",
    "_run",
    "apply_status",
    "apply_payload_kind",
]


def _norm_holdout(reason: str) -> str:
    reason = (reason or "").strip()
    if reason.startswith("asset_offset_"):
        return "asset_offset_exceeds_50m"
    return reason


def _parse_day(name: str) -> datetime | None:
    try:
        return datetime.strptime(name[:10], "%Y-%m-%d")
    except ValueError:
        return None


def _iter_enrichment_run_dirs(root: Path) -> list[Path]:
    """Find *_sf_enrichment dirs at any depth under runs/."""
    found: list[Path] = []
    for path in root.rglob("*_sf_enrichment"):
        if path.is_dir() and path.name.endswith("_sf_enrichment"):
            found.append(path)
    return sorted(found, key=lambda p: p.name)


def coalesce(*, since: datetime | None = None) -> dict:
    detail_runs: list[tuple[str, Path]] = []
    for d in _iter_enrichment_run_dirs(RUNS):
        day = _parse_day(d.name)
        if day is None:
            continue
        if since is not None and day < since:
            continue
        detail = d / "enrichment_detail.csv"
        if detail.exists():
            detail_runs.append((d.name, detail))

    by_id: dict[str, dict] = {}
    total_rows = 0
    for name, path in detail_runs:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                sid = (row.get("Id") or "").strip()
                if not sid:
                    continue
                total_rows += 1
                if sid not in by_id or name >= by_id[sid]["_run"]:
                    rec = dict(row)
                    rec["_run"] = name
                    rec["holdout_reason"] = _norm_holdout(rec.get("holdout_reason") or "")
                    by_id[sid] = rec

    apply_by_id: dict[str, dict] = {}
    for name, path in detail_runs:
        apply_path = path.parent / "sf_update_apply_log.csv"
        if not apply_path.exists():
            continue
        with apply_path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                sid = (row.get("Id") or "").strip()
                if not sid:
                    continue
                if sid not in apply_by_id or name >= apply_by_id[sid]["_run"]:
                    rec = dict(row)
                    rec["_run"] = name
                    apply_by_id[sid] = rec

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUT_COLS, extrasaction="ignore")
        writer.writeheader()
        for sid, row in by_id.items():
            apply = apply_by_id.get(sid)
            payload = (apply or {}).get("payload_json") or ""
            if apply is None:
                kind = "none"
                status = "(no apply)"
            elif "Site_Type__c" in payload or "Site_Latitude__c" in payload:
                kind = "tower_enrichment"
                status = apply.get("status") or ""
            else:
                kind = "llm_classified_only"
                status = apply.get("status") or ""
            out = {col: row.get(col, "") for col in OUT_COLS}
            out["Id"] = sid
            out["apply_status"] = status
            out["apply_payload_kind"] = kind
            writer.writerow(out)

    # Convenience copy at runs/ root for older paths
    root_copy = RUNS / "_combined_enrichment_latest.csv"
    root_copy.write_bytes(OUT_CSV.read_bytes())

    bucket = Counter(r.get("bucket") or "" for r in by_id.values())
    holdout = Counter(r.get("holdout_reason") or "" for r in by_id.values())
    summary = {
        "since": since.date().isoformat() if since else None,
        "runs": len(detail_runs),
        "unique_ids": len(by_id),
        "total_rows_all_runs": total_rows,
        "unique_applied": len(apply_by_id),
        "bucket": dict(bucket.most_common()),
        "holdout_reason": dict(holdout.most_common()),
        "combined_csv": str(OUT_CSV),
        "combined_csv_root_copy": str(root_copy),
    }
    OUT_JSON.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since",
        default="2026-08-01",
        help="Include runs on/after this date (YYYY-MM-DD). Empty = all.",
    )
    args = parser.parse_args()
    since = datetime.strptime(args.since, "%Y-%m-%d") if args.since else None
    summary = coalesce(since=since)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
