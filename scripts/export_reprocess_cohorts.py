"""Export Nearmap/Claude reprocess cohorts from local enrichment outputs.

Works off runs/_combined_enrichment_latest.csv (or any coalesced detail).
Does NOT reset Salesforce LLM_Classified__c — holdouts already left the blank
Site_Type enrichment queue; reprocess them offline, then write SF updates.

Why local (not SF reset):
- Mass-resetting LLM_Classified__c would dump ~50k sites back into the NAIP
  enrichment query and re-spend for sites you already classified.
- Address-pin / rural-offset cases need a *different* pipeline (Nearmap + Claude,
  optional DB snap), not another pass of the same blank-Site_Type NAIP job.
- Fresh work (e.g. remaining DC / new SF pulls) should use the full orchestrator;
  already-classified holdouts should use this export.

Suspect tags baked from offset semantics:
- suspect_address_pin — no DB hit; model asset far from SF pin (pin likely
  address/parcel, not tower pad)
- db_hit_model_disagrees — FCC/TowerSource hit used as classify pin, but NAIP
  asset box still >50 m away
- rural_or_wide_gap — same as large offset, kept explicit for filtering
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "runs" / "_artifacts" / "_combined_enrichment_latest.csv"
DEFAULT_OUT_DIR = ROOT / "runs" / "_artifacts" / "reprocess_cohorts"

OFFSET_LIMIT_M = 50.0

# Cohorts intended for Nearmap + Claude (not another NAIP-only enrichment pass).
DEFAULT_REASONS = (
    "asset_offset_exceeds_50m",
    "unclear",
    "potential_rooftop",
    "no_naip_imagery",
    "no_imagery",
    "classify_error",
)

OUT_COLS = [
    "Id",
    "reprocess_cohort",
    "suspect_tag",
    "recommended_next_step",
    "bucket",
    "holdout_reason",
    "match_source",
    "match_distance_m",
    "naip_site_type",
    "naip_tower_subtype",
    "naip_site_confidence",
    "asset_offset_m",
    "Site_Street__c",
    "Site_City__c",
    "Site_State__c",
    "apply_status",
    "apply_payload_kind",
    "_run",
]


def _float(value: object) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _norm_reason(row: dict) -> str:
    reason = (row.get("holdout_reason") or "").strip()
    if reason.startswith("asset_offset_"):
        return "asset_offset_exceeds_50m"
    naip = (row.get("naip_site_type") or "").strip().lower()
    if not reason and (row.get("bucket") or "") == "potential_rooftop":
        return "potential_rooftop"
    if reason in {"no_imagery", "no_naip_imagery"} or naip == "no_imagery":
        return "no_naip_imagery"
    return reason


def tag_row(row: dict) -> tuple[str, str, str]:
    """Return (cohort, suspect_tag, recommended_next_step)."""
    reason = _norm_reason(row)
    match = (row.get("match_source") or "none").strip() or "none"
    offset = _float(row.get("asset_offset_m"))
    naip = (row.get("naip_site_type") or "").strip().lower()

    suspect = ""
    if offset is not None and offset > OFFSET_LIMIT_M:
        if match == "none":
            suspect = "suspect_address_pin"
        else:
            suspect = "db_hit_model_disagrees"

    if reason == "asset_offset_exceeds_50m" or (
        naip == "tower" and offset is not None and offset > OFFSET_LIMIT_M
    ):
        cohort = "offset_tower_or_far_asset"
        if suspect == "suspect_address_pin":
            next_step = (
                "Nearmap+Claude on wider chip; treat SF pin as address — "
                "prefer model/DB tower pad if found"
            )
        elif suspect == "db_hit_model_disagrees":
            next_step = (
                "Nearmap+Claude review: FCC/TS pin vs NAIP asset box disagreement"
            )
        else:
            next_step = "Nearmap+Claude wider review (offset >50m)"
        return cohort, suspect or "rural_or_wide_gap", next_step

    if reason == "potential_rooftop":
        return (
            "rooftop_nearmap",
            suspect,
            "Nearmap rooftop stage (NAIP rooftop held out by design)",
        )

    if reason == "unclear":
        return (
            "unclear_reclassify",
            suspect,
            "Nearmap+Claude reclassify (NAIP unclear)",
        )

    if reason == "no_naip_imagery":
        return (
            "no_imagery_nearmap",
            suspect,
            "Nearmap (or other imagery) — NAIP missing",
        )

    if reason == "classify_error":
        return "classify_retry", suspect, "Retry classify (transient/error)"

    if reason == "other":
        return (
            "other_qa_sample",
            suspect,
            "Sample QA before bulk reprocess — likely non-tower",
        )

    return f"holdout:{reason or 'unknown'}", suspect, "Manual review"


def export_cohorts(
    *,
    input_csv: Path,
    out_dir: Path,
    reasons: set[str] | None = None,
    include_other: bool = False,
) -> dict:
    reasons = set(reasons or DEFAULT_REASONS)
    if include_other:
        reasons.add("other")

    with input_csv.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))

    selected: list[dict] = []
    for row in rows:
        # Skip successful tower enrichment path unless explicitly offset-tagged.
        if (row.get("bucket") or "") == "potential_update":
            continue
        reason = _norm_reason(row)
        if reason not in reasons and not (
            # also catch tower rows whose raw reason wasn't normalized in older files
            (row.get("naip_site_type") or "").strip().lower() == "tower"
            and (_float(row.get("asset_offset_m")) or 0) > OFFSET_LIMIT_M
            and "asset_offset_exceeds_50m" in reasons
        ):
            continue

        cohort, suspect, next_step = tag_row(row)
        out = {col: row.get(col, "") for col in OUT_COLS}
        out["Id"] = row.get("Id") or ""
        out["reprocess_cohort"] = cohort
        out["suspect_tag"] = suspect
        out["recommended_next_step"] = next_step
        out["holdout_reason"] = reason
        selected.append(out)

    out_dir.mkdir(parents=True, exist_ok=True)
    all_path = out_dir / "reprocess_all.csv"
    with all_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUT_COLS)
        writer.writeheader()
        writer.writerows(selected)

    by_cohort: dict[str, list[dict]] = {}
    for row in selected:
        by_cohort.setdefault(row["reprocess_cohort"], []).append(row)

    for cohort, cohort_rows in by_cohort.items():
        path = out_dir / f"{cohort}.csv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=OUT_COLS)
            writer.writeheader()
            writer.writerows(cohort_rows)

    # Ids-only for piping into other tools
    ids_path = out_dir / "reprocess_ids.txt"
    ids_path.write_text(
        "\n".join(r["Id"] for r in selected if r["Id"]) + ("\n" if selected else ""),
        encoding="utf-8",
    )

    summary = {
        "input": str(input_csv),
        "out_dir": str(out_dir),
        "selected": len(selected),
        "by_cohort": dict(Counter(r["reprocess_cohort"] for r in selected)),
        "by_suspect_tag": dict(Counter(r["suspect_tag"] or "(none)" for r in selected)),
        "files": {
            "all": str(all_path),
            "ids": str(ids_path),
            **{k: str(out_dir / f"{k}.csv") for k in by_cohort},
        },
    }
    (out_dir / "summary.json").write_text(
        __import__("json").dumps(summary, indent=2), encoding="utf-8"
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_INPUT,
        help="Coalesced enrichment CSV (default: runs/_combined_enrichment_latest.csv)",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help="Output directory for cohort CSVs",
    )
    parser.add_argument(
        "--include-other",
        action="store_true",
        help="Also export holdout_reason=other (usually QA-sample only)",
    )
    args = parser.parse_args()
    if not args.input.exists():
        fallback = ROOT / "runs" / "_combined_enrichment_latest.csv"
        if fallback.exists():
            args.input = fallback
        else:
            raise SystemExit(
                f"Missing {args.input} — run scripts/coalesce_enrichment_latest.py first"
            )
    summary = export_cohorts(
        input_csv=args.input,
        out_dir=args.out_dir,
        include_other=args.include_other,
    )
    print(__import__("json").dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
