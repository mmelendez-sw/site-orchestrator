"""Holdout triage reports — group by reason and surface weekly focus areas."""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any, Sequence

from enrichment.constants import (
    BUCKET_OTHER,
    BUCKET_POTENTIAL_UPDATE,
    BUCKET_ROOFTOP,
    DETAIL_CSV,
    HOLDOUT_TRIAGE_JSON,
    HOLDOUT_TRIAGE_MD,
    MATCH_SOURCE_NONE,
)

# Suggested next actions keyed by holdout_reason prefix / exact match.
_REASON_HINTS: dict[str, str] = {
    "imagery_only_needs_crop_or_localize_agree": (
        "Claude scene-level agree without crop/localize — re-check box quality "
        "or prefer FCC/TowerSource snap when unique in radius."
    ),
    "rooftop_needs_dual_model_cell": (
        "Gemini/Claude cell disagreement or soft-keep — sample HVAC vs panel FPs."
    ),
    "tower_needs_dual_model_cell": (
        "Tower cell claim without Claude hard-agree or Gemini >= 0.9 lock — "
        "check wrong-neighbor poles."
    ),
    "rooftop_needs_oblique_asset_box": (
        "Missing compact oblique box — repair/localize path or Nearmap coverage."
    ),
    "rooftop_needs_nearmap_obliques": (
        "Vert-only Nearmap — confirm coverage / tiered fetch."
    ),
    "tower_needs_nearmap_obliques": (
        "Imagery-only tower without obliques — do not auto-write."
    ),
    "rooftop_no_cell_equipment": (
        "True no-cell or over-cleared — spot-check for FN rooftops."
    ),
    "tower_no_cell_equipment": (
        "Tower without cell gear — utility poles vs real cell sites."
    ),
    "low_confidence_imagery_only": (
        "Imagery-only conf below bar — DB proximity miss or weak imagery."
    ),
    "rooftop_low_cell_confidence": (
        "Cell conf below bar — borderline HVAC/antenna cases."
    ),
    "asset_offset_": (
        "Asset box far from pin — pin↔address or wrong-neighbor scout."
    ),
    "rooftop_naip_only_forbidden": (
        "NAIP-only rooftop — Nearmap gap or fetch failure."
    ),
    "tower_naip_only_forbidden": (
        "NAIP-only tower without a DB-hit Gemini >= 0.9 lock — "
        "imagery-only still needs Nearmap, or conf is below the lock."
    ),
}


def _hint_for_reason(reason: str) -> str:
    text = str(reason or "").strip()
    if text in _REASON_HINTS:
        return _REASON_HINTS[text]
    for prefix, hint in _REASON_HINTS.items():
        if prefix.endswith("_") and text.startswith(prefix):
            return hint
    return "Review sample chips; add a golden regression if this is a new FP/FN pattern."


def build_holdout_triage(
    rows: Sequence[dict[str, Any]],
    *,
    top_n: int = 3,
) -> dict[str, Any]:
    """Aggregate holdout reasons and pick top focus areas."""
    holdouts = [
        r
        for r in rows
        if str(r.get("bucket") or "") in {BUCKET_ROOFTOP, BUCKET_OTHER}
    ]
    candidates = [
        r for r in rows if str(r.get("bucket") or "") == BUCKET_POTENTIAL_UPDATE
    ]
    reason_counts = Counter(
        str(r.get("holdout_reason") or "unknown").strip() or "unknown"
        for r in holdouts
    )
    by_reason = [
        {
            "reason": reason,
            "count": count,
            "hint": _hint_for_reason(reason),
            "sample_ids": [
                str(r.get("Id") or "")
                for r in holdouts
                if str(r.get("holdout_reason") or "").strip() == reason
            ][:5],
        }
        for reason, count in reason_counts.most_common()
    ]
    imagery_only_candidates = sum(
        1
        for r in candidates
        if str(r.get("match_source") or MATCH_SOURCE_NONE) == MATCH_SOURCE_NONE
    )
    db_candidates = len(candidates) - imagery_only_candidates
    focus = by_reason[: max(1, top_n)] if by_reason else []
    return {
        "total_rows": len(rows),
        "candidates": len(candidates),
        "db_backed_candidates": db_candidates,
        "imagery_only_candidates": imagery_only_candidates,
        "holdouts": len(holdouts),
        "reasons": by_reason,
        "weekly_focus": [
            {
                "rank": i + 1,
                "reason": item["reason"],
                "count": item["count"],
                "action": item["hint"],
                "sample_ids": item["sample_ids"],
            }
            for i, item in enumerate(focus)
        ],
    }


def render_holdout_triage_md(report: dict[str, Any]) -> str:
    lines = [
        "# Holdout triage",
        "",
        f"- Total rows: **{report.get('total_rows', 0)}**",
        f"- Candidates: **{report.get('candidates', 0)}** "
        f"(DB-backed {report.get('db_backed_candidates', 0)}, "
        f"imagery-only {report.get('imagery_only_candidates', 0)})",
        f"- Holdouts: **{report.get('holdouts', 0)}**",
        "",
        "## Weekly focus (top reasons)",
        "",
    ]
    focus = report.get("weekly_focus") or []
    if not focus:
        lines.append("_No holdouts in this run._")
    else:
        for item in focus:
            ids = ", ".join(item.get("sample_ids") or []) or "—"
            lines.extend(
                [
                    f"### {item.get('rank')}. `{item.get('reason')}` "
                    f"({item.get('count')})",
                    f"- Action: {item.get('action')}",
                    f"- Sample Ids: {ids}",
                    "",
                ]
            )
    lines.extend(["## All reasons", ""])
    for item in report.get("reasons") or []:
        lines.append(f"- `{item['reason']}`: {item['count']}")
    lines.append("")
    return "\n".join(lines)


def write_holdout_triage(
    run_dir: Path,
    rows: Sequence[dict[str, Any]] | None = None,
    *,
    top_n: int = 3,
) -> dict[str, Any]:
    """Write holdout_triage.json + .md under run_dir. Returns the report dict."""
    run_dir = Path(run_dir)
    if rows is None:
        detail = run_dir / DETAIL_CSV
        if not detail.exists():
            raise FileNotFoundError(f"Detail CSV not found: {detail}")
        with detail.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    report = build_holdout_triage(rows, top_n=top_n)
    (run_dir / HOLDOUT_TRIAGE_JSON).write_text(
        json.dumps(report, indent=2),
        encoding="utf-8",
    )
    (run_dir / HOLDOUT_TRIAGE_MD).write_text(
        render_holdout_triage_md(report),
        encoding="utf-8",
    )
    return report
