"""CLI: python -m enrichment

Default is dry-run (build CSVs only). Pass --apply to write Salesforce updates
from an existing potential_sf_updates.csv.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

from enrichment.constants import CANDIDATE_CSV, PROXIMITY_MAX_M  # noqa: E402
from enrichment.pipeline import (  # noqa: E402
    apply_candidate_csv,
    default_run_dir,
    run_enrichment,
)
from salesforce.sf_client import SalesforceClient  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Enrich blank Site_Type__c Salesforce sites via FCC/TowerSource "
            "proximity (0-50m) and NAIP-only classification."
        )
    )
    parser.add_argument(
        "--run-dir",
        type=Path,
        default=None,
        help="Output directory (default: runs/<timestamp>_sf_enrichment)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process at most N Salesforce sites",
    )
    parser.add_argument(
        "--max-m",
        type=float,
        default=PROXIMITY_MAX_M,
        help="Proximity radius in meters (default: 50)",
    )
    parser.add_argument(
        "--skip-classify",
        action="store_true",
        help="SQL proximity only — skip NAIP/AI (no paid vision calls)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Apply Salesforce updates from --candidates CSV. "
            "Without this flag, no Salesforce writes occur."
        ),
    )
    parser.add_argument(
        "--candidates",
        type=Path,
        default=None,
        help=(
            f"Candidate CSV for --apply (default: <run-dir>/{CANDIDATE_CSV}). "
            "When set with --apply, enrichment classify step is skipped."
        ),
    )
    parser.add_argument(
        "--apply-only",
        action="store_true",
        help="Only apply an existing candidates CSV (requires --candidates or --run-dir)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    run_dir = args.run_dir or default_run_dir(ROOT / "runs")
    print("=" * 72, flush=True)
    print("ENRICHMENT — FCC/TowerSource + NAIP (no Nearmap)", flush=True)
    print(f"  run_dir : {run_dir}", flush=True)
    print(f"  limit   : {args.limit}", flush=True)
    print(f"  apply SF: {bool(args.apply)}", flush=True)
    print("=" * 72, flush=True)

    print("\n=== STAGE: AUTHENTICATE SALESFORCE ===", flush=True)
    sf_client = SalesforceClient()
    print("  ✓ Salesforce authenticated", flush=True)

    if args.apply_only or (args.apply and args.candidates):
        candidate_csv = args.candidates
        if candidate_csv is None:
            candidate_csv = run_dir / CANDIDATE_CSV
        if not candidate_csv.exists():
            logging.error("Candidates CSV not found: %s", candidate_csv)
            return 1
        if not args.apply:
            logging.error("--apply-only requires --apply to write to Salesforce")
            return 1
        summary = apply_candidate_csv(
            sf_client=sf_client,
            candidate_csv=candidate_csv,
            run_dir=run_dir,
            apply=True,
            verbose=True,
        )
        print(summary)
        return 0 if summary.get("failed", 0) == 0 else 2

    # Enrichment phase — never writes Site__c updates unless --apply with candidates.
    summary = run_enrichment(
        sf_client=sf_client,
        run_dir=run_dir,
        limit=args.limit,
        max_m=args.max_m,
        skip_classify=args.skip_classify,
        verbose=True,
    )
    print(summary)

    if args.apply:
        candidate_csv = args.candidates or (run_dir / CANDIDATE_CSV)
        apply_summary = apply_candidate_csv(
            sf_client=sf_client,
            candidate_csv=candidate_csv,
            run_dir=run_dir,
            apply=True,
            verbose=True,
        )
        print(apply_summary)
        return 0 if apply_summary.get("failed", 0) == 0 else 2

    print(
        f"\nReview CSVs in {run_dir}, then apply with:\n"
        f"  python -m enrichment --apply --apply-only "
        f"--candidates {run_dir / CANDIDATE_CSV} -v\n",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
