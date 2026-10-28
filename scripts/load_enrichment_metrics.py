"""Create/alter the enrichment metric tables in Azure SQL and load the JSONL ledger.

Uses the same Entra token connection as FCC/TowerSource (az login).

  python scripts/load_enrichment_metrics.py --dry-run
  python scripts/load_enrichment_metrics.py
  python scripts/load_enrichment_metrics.py --backfill-processed --dry-run
  python scripts/load_enrichment_metrics.py --backfill-processed

Default: upsert every run in metrics/runs.jsonl with its site rows from
metrics/sites.jsonl (idempotent per run_id).

--backfill-processed: rebuild metrics/sites.jsonl from each live run's
enrichment_detail.csv so UniqueSites counts every processed site (applied,
held out, missed, errored) for runs recorded when the ledger only kept
successful writes. Replaces all EnrichmentSiteOutcome rows with the rebuilt
set. Combine with --dry-run to compare counts without writing anything.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from enrichment.metrics import (  # noqa: E402
    KPIS_JSON,
    RUNS_JSONL,
    SITES_JSONL,
    _read_jsonl,
    _rewrite_jsonl,
    rebuild_site_ledger,
    rollup_kpis,
)
from enrichment.metrics_store import (  # noqa: E402
    delete_all_site_outcomes,
    ensure_tables,
    upsert_snapshot,
)
from paths import metrics_dir, runs_dir  # noqa: E402


def _snapshots(runs: list[dict], sites: list[dict]) -> list[dict]:
    """One snapshot per run_id (last header wins) with that run's site rows."""
    by_run: dict[str, list[dict]] = {}
    for rec in sites:
        by_run.setdefault(str(rec.get("run_id") or ""), []).append(rec)
    headers: dict[str, dict] = {}
    for run in runs:
        rid = str(run.get("run_id") or "")
        if rid:
            headers[rid] = run
    return [
        {**header, "site_records": by_run.get(rid, [])}
        for rid, header in headers.items()
    ]


def _print_kpis(label: str, kpis: dict) -> None:
    print(
        f"{label}: unique_sites={kpis.get('unique_sites')} "
        f"written_sites={kpis.get('written_sites')} "
        f"rooftop={kpis.get('rooftop_sf_writes')} tower={kpis.get('tower_sf_writes')} "
        f"db_skip={kpis.get('db_skip_sf_writes')} outcomes={kpis.get('outcomes')}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Print counts; write nothing")
    parser.add_argument(
        "--backfill-processed",
        action="store_true",
        help="Rebuild sites.jsonl + SQL site rows from run CSVs (every processed site)",
    )
    parser.add_argument(
        "--rebuild-sites",
        action="store_true",
        help="Delete all EnrichmentSiteOutcome rows before loading the ledger",
    )
    args = parser.parse_args()

    root = metrics_dir()
    runs = _read_jsonl(root / RUNS_JSONL)
    sites = _read_jsonl(root / SITES_JSONL)
    _print_kpis("ledger now", rollup_kpis(sites))
    if args.backfill_processed:
        sites, stats = rebuild_site_ledger(runs_root=runs_dir(), root=root)
        print(f"backfill: {stats}")
        _print_kpis("after backfill", rollup_kpis(sites))
    snaps = _snapshots(runs, sites)
    print(f"runs={len(snaps)} site_rows={sum(len(s['site_records']) for s in snaps)}")
    if args.dry_run:
        print("dry-run — no JSONL or SQL writes")
        return 0

    if args.backfill_processed:
        _rewrite_jsonl(root / SITES_JSONL, sites)
    kpis = rollup_kpis(sites)
    import json

    (root / KPIS_JSON).write_text(json.dumps(kpis, indent=2), encoding="utf-8")

    from enrichment.mssql import connect_mssql

    conn = connect_mssql()
    try:
        cursor = conn.cursor()
        ensure_tables(cursor)
        if args.rebuild_sites or args.backfill_processed:
            delete_all_site_outcomes(cursor)
            print("cleared dbo.EnrichmentSiteOutcome")
        total = 0
        for snap in snaps:
            total += upsert_snapshot(cursor, snap)
        conn.commit()
        cursor.execute(
            "SELECT UniqueSites, WrittenSites, RooftopSfWrites, TowerSfWrites, "
            "AppliedDbSkip FROM dbo.vEnrichmentKpis"
        )
        row = cursor.fetchone()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    print(f"done — {len(snaps)} run(s), {total} site row(s) loaded")
    if row:
        print(
            f"sql UniqueSites={row[0]} WrittenSites={row[1]} RooftopSfWrites={row[2]} "
            f"TowerSfWrites={row[3]} AppliedDbSkip={row[4]}"
        )
    print("query: SELECT * FROM dbo.vEnrichmentKpis")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
