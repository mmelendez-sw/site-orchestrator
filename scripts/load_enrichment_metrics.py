"""One-time: create enrichment metric tables in Azure SQL and load the JSONL ledger.

Uses the same Entra token connection as FCC/TowerSource (az login).

  python scripts/load_enrichment_metrics.py
  python scripts/load_enrichment_metrics.py --dry-run

Idempotent. Re-running replaces rows for run_ids present in metrics/runs.jsonl.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from enrichment.constants import DETAIL_CSV  # noqa: E402
from enrichment.metrics import RUNS_JSONL, SITES_JSONL, _read_jsonl, apply_slice_fields  # noqa: E402
from paths import metrics_dir, runs_dir  # noqa: E402
from enrichment.metrics_store import (  # noqa: E402
    ensure_tables,
    upsert_snapshot,
)
from enrichment.mssql import connect_mssql  # noqa: E402


def _load_ledger() -> tuple[list[dict], list[dict]]:
    root = metrics_dir()
    runs = _read_jsonl(root / RUNS_JSONL)
    sites = _read_jsonl(root / SITES_JSONL)
    return runs, sites


def _detail_by_id(run_id: str) -> dict[str, dict]:
    path = runs_dir() / run_id / DETAIL_CSV
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8", newline="") as handle:
        return {
            str(row.get("Id") or ""): row
            for row in csv.DictReader(handle)
            if row.get("Id")
        }


def _hydrate_site(rec: dict, detail: dict[str, dict]) -> dict:
    csv_row = detail.get(str(rec.get("Id") or ""))
    merged = {**(csv_row or {}), **rec}
    return apply_slice_fields(merged)


def _snaps_from_ledger(
    runs: list[dict], sites: list[dict]
) -> list[dict]:
    by_run: dict[str, list[dict]] = {}
    for rec in sites:
        rid = str(rec.get("run_id") or "")
        if rid:
            by_run.setdefault(rid, []).append(rec)
    snaps: list[dict] = []
    for run in runs:
        rid = str(run.get("run_id") or "")
        if not rid:
            continue
        detail = _detail_by_id(rid)
        snap = dict(run)
        snap["site_records"] = [
            _hydrate_site(rec, detail) for rec in by_run.get(rid, [])
        ]
        snaps.append(snap)
    return snaps


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print counts; do not connect or write",
    )
    args = parser.parse_args()
    runs, sites = _load_ledger()
    snaps = _snaps_from_ledger(runs, sites)
    print(f"ledger runs={len(runs)} site_rows={len(sites)} snaps={len(snaps)}")
    for snap in snaps:
        print(
            f"  {snap.get('run_id')}: sites={len(snap.get('site_records') or [])} "
            f"applied_rooftop={snap.get('applied_rooftop')}"
        )
    if args.dry_run:
        print("dry-run — no SQL writes")
        return 0

    conn = connect_mssql()
    try:
        cursor = conn.cursor()
        ensure_tables(cursor)
        total_sites = 0
        for snap in snaps:
            n = upsert_snapshot(cursor, snap)
            total_sites += n
            print(f"  upserted {snap.get('run_id')} ({n} site rows)")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    print(f"done — {len(snaps)} run(s), {total_sites} site row(s)")
    print("query: SELECT * FROM dbo.vEnrichmentKpis")
    print("query: SELECT * FROM dbo.vEnrichmentKpisByState")
    print("query: SELECT * FROM dbo.vEnrichmentKpisByMatchSource")
    print("query: SELECT * FROM dbo.vEnrichmentSiteLatest")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
