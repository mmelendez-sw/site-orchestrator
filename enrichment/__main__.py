"""CLI: python -m enrichment.

Auto-apply (airtight) flow:
  python -m enrichment --states CA,MA,FL,NV --apply --dequeue-holdouts

Only Claude hard-agree candidates are pushed. Imagery-only also requires
agree_crop / agree_localize. Soft-keep and Gemini-solo never write. Holdouts
dequeue (LLM_Classified=false, LLM_Holdout=true) for later reconciliation.

Each run writes review/, spot_audit.html (~10% sample), and holdout_triage.md.

Optional human review still available:
  1) Classify without --apply → review/ package
  2) python -m enrichment --apply-reviewed --run-dir <run>
  3) python -m enrichment --triage --run-dir <run>
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

from enrichment.audit import write_spot_audit_package  # noqa: E402
from enrichment.constants import (  # noqa: E402
    CANDIDATE_CSV,
    DETAIL_CSV,
    HOLDOUT_TRIAGE_MD,
    PROXIMITY_MAX_M,
    REVIEW_DIR_NAME,
    SPOT_AUDIT_HTML,
)
from enrichment.pipeline import (  # noqa: E402
    apply_candidate_csv,
    default_run_dir,
    run_enrichment,
)
from enrichment.review import load_approved_ids, write_review_package  # noqa: E402
from enrichment.triage import write_holdout_triage  # noqa: E402
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
            "Enrich blank Site_Type__c Salesforce sites via FCC/TowerSource "
            "proximity and full imagery classification. Use --apply to "
            "auto-push airtight Claude-agreed candidates; holdouts dequeue "
            "with --dequeue-holdouts for later reconciliation."
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
        "--ids",
        type=str,
        default=None,
        help=(
            "Comma-separated Site__c Ids for a controlled test batch "
            "(bypasses NFL / blank-Site_Type queue filters)"
        ),
    )
    parser.add_argument(
        "--carrier-like",
        type=str,
        default="NFL",
        help=(
            "Carrier_Leasing_Source__c LIKE filter (default: NFL). "
            "Pass empty string to disable carrier filter."
        ),
    )
    parser.add_argument(
        "--states",
        type=str,
        default=None,
        help=(
            "Comma-separated Site_State__c values (e.g. CA,FL,NV,MA). "
            "Default: no state filter."
        ),
    )
    parser.add_argument(
        "--max-m",
        type=float,
        default=PROXIMITY_MAX_M,
        help=f"Proximity radius in meters (default: {PROXIMITY_MAX_M:g})",
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
            "After classify, auto-push potential_update candidates to Salesforce "
            "(Claude hard-agree only). Pair with --dequeue-holdouts to clear "
            "non-candidates from the enrichment queue."
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
        help="With --apply-reviewed: treat every ready candidate as approved",
    )
    parser.add_argument(
        "--dequeue-holdouts",
        action="store_true",
        help=(
            "With --apply or --apply-reviewed: dequeue non-candidate rows "
            "(LLM_Classified__c=false, LLM_Holdout__c=true)"
        ),
    )
    parser.add_argument(
        "--rebuild-review",
        action="store_true",
        help="Rebuild review/ from an existing run-dir candidates CSV and exit",
    )
    parser.add_argument(
        "--triage",
        action="store_true",
        help="Rebuild holdout_triage.json/.md (+ spot audit) for --run-dir and exit",
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
        write_spot_audit_package(run_dir)
        write_holdout_triage(run_dir)
        print(f"Rebuilt review package -> {review_dir}")
        return 0

    if args.triage:
        if not args.run_dir:
            logging.error("--triage requires --run-dir")
            return 1
        report = write_holdout_triage(run_dir)
        audit = write_spot_audit_package(run_dir)
        print(
            f"Triage → {run_dir / HOLDOUT_TRIAGE_MD} | "
            f"focus={report.get('weekly_focus')} | "
            f"spot audit → {audit / SPOT_AUDIT_HTML}"
        )
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

    mode = "auto-apply" if args.apply else "review-first"
    print(
        f"ENRICHMENT | limit={args.limit} | {mode} | {run_dir}",
        flush=True,
    )

    print("=== AUTHENTICATE SALESFORCE ===", flush=True)
    sf_client = SalesforceClient()
    print("  authenticated", flush=True)

    site_ids = None
    if args.ids:
        site_ids = [part.strip() for part in str(args.ids).split(",") if part.strip()]
    carrier_like = args.carrier_like
    if carrier_like is not None and str(carrier_like).strip() == "":
        carrier_like = None
    states = None
    if args.states:
        states = [
            part.strip().upper()
            for part in str(args.states).split(",")
            if part.strip()
        ] or None

    summary = run_enrichment(
        sf_client=sf_client,
        run_dir=run_dir,
        limit=args.limit,
        max_m=args.max_m,
        skip_classify=args.skip_classify,
        site_ids=site_ids,
        carrier_like=carrier_like,
        states=states,
        verbose=args.verbose,
    )
    print(summary)

    if args.apply:
        import csv

        detail_csv = run_dir / DETAIL_CSV
        with detail_csv.open(encoding="utf-8-sig", newline="") as handle:
            all_rows = list(csv.DictReader(handle))
        candidates = [
            r for r in all_rows if r.get("bucket") == "potential_update"
        ]
        holdouts: list[dict] = []
        if args.dequeue_holdouts:
            holdouts = [
                r for r in all_rows if r.get("bucket") != "potential_update"
            ]
        print(
            f"\nAUTO-APPLY | candidates={len(candidates)} | "
            f"dequeue_holdouts={len(holdouts)} | {run_dir}",
            flush=True,
        )
        if not candidates and not holdouts:
            print("Nothing to push or dequeue.", flush=True)
            return 0
        apply_summary = apply_candidate_csv(
            sf_client=sf_client,
            candidate_csv=_write_temp_apply_csv(
                run_dir, candidates + holdouts
            ),
            run_dir=run_dir,
            apply=True,
            verbose=args.verbose,
        )
        write_spot_audit_package(
            run_dir,
            candidates,
            applied_ids=[r.get("Id") for r in candidates],
        )
        print(apply_summary, flush=True)
        print(
            f"Spot-audit after apply → "
            f"{(run_dir / REVIEW_DIR_NAME / SPOT_AUDIT_HTML).resolve()}",
            flush=True,
        )
        return 0 if apply_summary.get("failed", 0) == 0 else 2

    print(
        f"\nReview candidates: {(run_dir / REVIEW_DIR_NAME / 'index.html').resolve()}\n"
        f"After approving, push:\n"
        f"  python -m enrichment --apply-reviewed --run-dir {run_dir}\n"
        f"Or auto-apply next time:\n"
        f"  python -m enrichment --states CA,MA,FL,NV --apply --dequeue-holdouts\n",
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
    write_spot_audit_package(
        run_dir,
        candidates,
        applied_ids=[r.get("Id") for r in candidates],
    )
    print(summary)
    print(
        f"Spot-audit after apply → "
        f"{(run_dir / REVIEW_DIR_NAME / SPOT_AUDIT_HTML).resolve()}",
        flush=True,
    )
    return 0 if summary.get("failed", 0) == 0 else 2


def _write_temp_apply_csv(run_dir: Path, rows: list[dict]) -> Path:
    import csv

    path = run_dir / "review" / "_apply_batch.csv"
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
