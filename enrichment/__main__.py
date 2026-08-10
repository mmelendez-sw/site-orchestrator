"""CLI: python -m enrichment.

With --apply, processed sites are collected first and updated at run end.
Eligible tower/rooftop sites get enrichment fields + LLM_Classified=true.
Holdouts are dequeued with LLM_Classified=false (blank Site Type stays blank).
Each Salesforce update remains isolated so one failure does not stop the rest.
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

from enrichment.constants import CANDIDATE_CSV, DETAIL_CSV, PROXIMITY_MAX_M  # noqa: E402
from enrichment.pipeline import (  # noqa: E402
    apply_candidate_csv,
    default_run_dir,
    run_enrichment,
)
from salesforce.sf_client import SalesforceClient  # noqa: E402


def _quiet_third_party_loggers() -> None:
    for name in (
        "httpx",
        "httpcore",
        "google",
        "google_genai",
        "google.genai",
        "urllib3",
        "openai",
    ):
        logging.getLogger(name).setLevel(logging.WARNING)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Enrich blank Site_Type__c Salesforce sites (Carrier_Leasing_Source__c "
            "LIKE '%NFL%') via FCC/TowerSource proximity and full imagery "
            "classification (Nearmap tiered + bifurcated Gemini→Claude when enabled)."
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
        help="Proximity radius in meters (default: 25)",
    )
    parser.add_argument(
        "--skip-classify",
        action="store_true",
        help="SQL proximity only — skip imagery/AI (no paid vision or Nearmap calls)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Write enrichment fields for eligible sites (LLM_Classified=true); "
            "dequeue holdouts with LLM_Classified=false after enrichment completes"
        ),
    )
    parser.add_argument(
        "--candidates",
        type=Path,
        default=None,
        help=f"Candidate CSV for --apply-only (default: <run-dir>/{CANDIDATE_CSV})",
    )
    parser.add_argument(
        "--apply-only",
        action="store_true",
        help="Only apply an existing candidates CSV (requires --apply)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help=(
            "Print full stage banners and per-step details "
            "(default: compact [n/N] Id | address | run time)"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s %(message)s",
    )
    _quiet_third_party_loggers()

    run_dir = args.run_dir or default_run_dir(ROOT / "runs")
    print(f"ENRICHMENT | limit={args.limit} | apply={bool(args.apply)} | {run_dir}", flush=True)

    print("=== AUTHENTICATE SALESFORCE ===", flush=True)
    sf_client = SalesforceClient()
    print("  ✓ authenticated", flush=True)

    if args.apply_only:
        if not args.apply:
            logging.error("--apply-only requires --apply")
            return 1
        candidate_csv = args.candidates or (run_dir / CANDIDATE_CSV)
        if not candidate_csv.exists():
            logging.error("Candidates CSV not found: %s", candidate_csv)
            return 1
        summary = apply_candidate_csv(
            sf_client=sf_client,
            candidate_csv=candidate_csv,
            run_dir=run_dir,
            apply=True,
            verbose=args.verbose,
        )
        print(summary)
        return 0 if summary.get("failed", 0) == 0 else 2

    summary = run_enrichment(
        sf_client=sf_client,
        run_dir=run_dir,
        limit=args.limit,
        max_m=args.max_m,
        skip_classify=args.skip_classify,
        verbose=args.verbose,
    )
    print(summary)

    if args.apply:
        candidate_csv = run_dir / DETAIL_CSV
        apply_summary = apply_candidate_csv(
            sf_client=sf_client,
            candidate_csv=candidate_csv,
            run_dir=run_dir,
            apply=True,
            verbose=args.verbose,
        )
        print(apply_summary)
        return 0 if apply_summary.get("failed", 0) == 0 else 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
