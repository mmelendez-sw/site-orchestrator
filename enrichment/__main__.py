"""CLI: python -m enrichment.

Default flow (golden / high-precision):
  1) Classify → write CSVs + review/ package (no Salesforce site-field writes)
  2) Open review/index.html, approve true cellular sites
  3) python -m enrichment --apply-reviewed --run-dir <run>

--apply alone no longer auto-pushes enrichment fields after classify.
Use --apply-reviewed (optionally --approve-all) to push after human review.
Holdouts are dequeued only when applying reviewed candidates (or with
--dequeue-holdouts on apply-reviewed).
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

from enrichment.constants import (  # noqa: E402
    CANDIDATE_CSV,
    DETAIL_CSV,
    PROXIMITY_MAX_M,
    REVIEW_DIR_NAME,
)
from enrichment.pipeline import (  # noqa: E402
    apply_candidate_csv,
    default_run_dir,
    run_enrichment,
)
from enrichment.review import load_approved_ids, write_review_package  # noqa: E402
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
            "classification. Writes a review/ package; Salesforce site-field "
            "pushes require --apply-reviewed after human approval."
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
            "DEPRECATED for auto-push after classify. Use --apply-reviewed after "
            "opening review/index.html. If passed alone with classify, enrichment "
            "still runs and only builds the review package."
        ),
    )
    parser.add_argument(
        "--apply-reviewed",
        action="store_true",
        help=(
            "Push Salesforce updates for rows marked approved=yes in "
            "review/review_manifest.csv (requires --run-dir)"
        ),
    )
    parser.add_argument(
        "--approve-all",
        action="store_true",
        help="With --apply-reviewed: treat every candidate as approved",
    )
    parser.add_argument(
        "--dequeue-holdouts",
        action="store_true",
        help=(
            "With --apply-reviewed: also dequeue non-candidate rows from "
            "enrichment_detail.csv (LLM_Classified=false)"
        ),
    )
    parser.add_argument(
        "--rebuild-review",
        action="store_true",
        help="Rebuild review/ from an existing run-dir candidates CSV and exit",
    )
    parser.add_argument(
        "--candidates",
        type=Path,
        default=None,
        help=f"Candidate CSV override (default: <run-dir>/{CANDIDATE_CSV})",
    )
    parser.add_argument(
        "--apply-only",
        action="store_true",
        help="Legacy alias: same as --apply-reviewed without approve filter (blocked)",
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

    if args.rebuild_review:
        if not args.run_dir:
            logging.error("--rebuild-review requires --run-dir")
            return 1
        review_dir = write_review_package(run_dir)
        print(f"Rebuilt review package -> {review_dir}")
        return 0

    if args.apply_only:
        logging.error(
            "--apply-only is disabled. Use --apply-reviewed after approving "
            "sites in review/index.html (or --approve-all)."
        )
        return 1

    if args.apply_reviewed:
        if not args.run_dir:
            logging.error("--apply-reviewed requires --run-dir")
            return 1
        return _apply_reviewed(
            run_dir=run_dir,
            approve_all=args.approve_all,
            dequeue_holdouts=args.dequeue_holdouts,
            verbose=args.verbose,
        )

    print(
        f"ENRICHMENT | limit={args.limit} | review-first | {run_dir}",
        flush=True,
    )
    if args.apply:
        print(
            "NOTE: --apply no longer auto-pushes Site Type. "
            "Classify will build review/; push with --apply-reviewed.",
            flush=True,
        )

    print("=== AUTHENTICATE SALESFORCE ===", flush=True)
    sf_client = SalesforceClient()
    print("  ✓ authenticated", flush=True)

    summary = run_enrichment(
        sf_client=sf_client,
        run_dir=run_dir,
        limit=args.limit,
        max_m=args.max_m,
        skip_classify=args.skip_classify,
        verbose=args.verbose,
    )
    print(summary)
    print(
        f"\nReview candidates: {(run_dir / REVIEW_DIR_NAME / 'index.html').resolve()}\n"
        f"After approving, push:\n"
        f"  python -m enrichment --apply-reviewed --run-dir {run_dir}\n",
        flush=True,
    )
    return 0


def _apply_reviewed(
    *,
    run_dir: Path,
    approve_all: bool,
    dequeue_holdouts: bool,
    verbose: bool,
) -> int:
    import csv

    review_dir = run_dir / REVIEW_DIR_NAME
    if not review_dir.exists():
        write_review_package(run_dir)

    try:
        approved_ids = load_approved_ids(review_dir, approve_all=approve_all)
    except FileNotFoundError as exc:
        logging.error("%s", exc)
        return 1

    detail_csv = run_dir / DETAIL_CSV
    if not detail_csv.exists():
        logging.error("Detail CSV not found: %s", detail_csv)
        return 1

    with detail_csv.open(encoding="utf-8-sig", newline="") as handle:
        all_rows = list(csv.DictReader(handle))

    candidates = [
        r
        for r in all_rows
        if r.get("bucket") == "potential_update" and r.get("Id") in approved_ids
    ]
    holdouts = []
    if dequeue_holdouts:
        holdouts = [
            r
            for r in all_rows
            if r.get("bucket") != "potential_update"
        ]

    if not candidates and not holdouts:
        logging.error(
            "No approved candidates to push. Mark approved=yes in %s "
            "or pass --approve-all.",
            review_dir / "review_manifest.csv",
        )
        return 1

    print("=== AUTHENTICATE SALESFORCE ===", flush=True)
    sf_client = SalesforceClient()
    print("  ✓ authenticated", flush=True)
    print(
        f"APPLY REVIEWED | approved={len(candidates)} | "
        f"dequeue_holdouts={len(holdouts)} | {run_dir}",
        flush=True,
    )

    rows = candidates + holdouts
    summary = apply_candidate_csv(
        sf_client=sf_client,
        candidate_csv=_write_temp_apply_csv(run_dir, rows),
        run_dir=run_dir,
        apply=True,
        verbose=verbose,
    )
    print(summary)
    return 0 if summary.get("failed", 0) == 0 else 2


def _write_temp_apply_csv(run_dir: Path, rows: list[dict]) -> Path:
    import csv

    path = run_dir / "review" / "_approved_apply_batch.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return path
    fields = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path


if __name__ == "__main__":
    raise SystemExit(main())
