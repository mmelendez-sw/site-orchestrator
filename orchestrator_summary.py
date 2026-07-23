"""End-of-run rollup for orchestrator: dedupe, imagery tiers, Salesforce outcomes."""

from __future__ import annotations

import logging
from collections import Counter
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_TIER_LABELS = {
    "naip_only": "NAIP only",
    "naip_wide": "NAIP wide",
    "vert_only": "Nearmap vert",
    "full": "Nearmap obliques",
    "zoom": "Zoom stage",
    "wide_aoi": "Wide AOI",
}

_SITE_TYPE_BUCKETS = ("tower", "rooftop", "other")


def _bucket_site_type(site_type: str | None) -> str:
    text = str(site_type or "").strip().lower()
    if text == "tower":
        return "tower"
    if text == "rooftop":
        return "rooftop"
    return "other"


def _tier_label(tier: str | None) -> str:
    if tier is None:
        return "unknown"
    try:
        if tier != tier:  # float NaN
            return "unknown"
    except Exception:
        pass
    key = str(tier).strip().lower()
    if not key or key == "nan":
        return "unknown"
    return _TIER_LABELS.get(key, key)


def _short_address(row: dict[str, Any], limit: int = 72) -> str:
    text = str(row.get("address") or "").strip() or "—"
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build_run_summary(
    *,
    processed: int,
    geocode_ok: int,
    geocode_failed: int,
    result_rows: list[dict[str, Any]],
    classified_by_index: dict[int, dict[str, Any]] | None = None,
    upload_outcomes: list[dict[str, Any]] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Build a structured rollup from dedupe/classify/upload artifacts."""
    classified_by_index = classified_by_index or {}
    upload_outcomes = upload_outcomes or []

    duplicates = [row for row in result_rows if row.get("status") == "duplicate"]
    review = [row for row in result_rows if row.get("status") == "review"]
    net_new = [row for row in result_rows if row.get("status") == "net_new"]

    imagery: dict[str, Counter[str]] = {}
    for index, row in enumerate(result_rows):
        if row.get("status") != "net_new":
            continue
        classified = classified_by_index.get(index) or {}
        tier = _tier_label(classified.get("nearmap_tier") or row.get("nearmap_tier"))
        bucket = _bucket_site_type(classified.get("site_type") or row.get("site_type"))
        imagery.setdefault(tier, Counter())[bucket] += 1

    loaded = [item for item in upload_outcomes if item.get("status") == "loaded"]
    failed = [item for item in upload_outcomes if item.get("status") == "failed"]
    skipped_upload = [
        item for item in upload_outcomes if item.get("status") == "skipped"
    ]

    return {
        "processed": processed,
        "geocode_ok": geocode_ok,
        "geocode_failed": geocode_failed,
        "duplicates": duplicates,
        "review": review,
        "net_new": net_new,
        "imagery": {tier: dict(counts) for tier, counts in imagery.items()},
        "upload_attempted": len(upload_outcomes),
        "upload_loaded": loaded,
        "upload_failed": failed,
        "upload_skipped": skipped_upload,
        "dry_run": dry_run,
    }


def format_run_summary(summary: dict[str, Any]) -> str:
    """Render a compact multi-section summary for the terminal / file."""
    lines: list[str] = [
        "=" * 72,
        "RUN SUMMARY",
        (
            f"  input={summary['processed']}  geocoded={summary['geocode_ok']}  "
            f"geocode_failed={summary['geocode_failed']}"
        ),
        "",
        "DEDUPE",
        f"  duplicates : {len(summary['duplicates'])}",
    ]
    for row in summary["duplicates"]:
        matched = row.get("matched_id") or "—"
        lines.append(f"    - {_short_address(row)}  (matched Id={matched})")

    lines.append(f"  review     : {len(summary['review'])}")
    for row in summary["review"]:
        lines.append(f"    - {_short_address(row)}")

    lines.append(f"  net_new    : {len(summary['net_new'])}")
    for row in summary["net_new"]:
        lines.append(f"    - {_short_address(row)}")

    lines.extend(["", "CLASSIFICATION (net_new by final imagery tier)"])
    imagery: dict[str, dict[str, int]] = summary.get("imagery") or {}
    if not imagery:
        lines.append("  (none classified)")
    else:
        preferred = [
            "NAIP only",
            "NAIP wide",
            "Nearmap vert",
            "Nearmap obliques",
            "Zoom stage",
            "Wide AOI",
        ]
        ordered = [label for label in preferred if label in imagery]
        ordered.extend(sorted(label for label in imagery if label not in ordered))
        for tier in ordered:
            counts = imagery[tier]
            total = sum(counts.values())
            parts = [f"{bucket}={counts.get(bucket, 0)}" for bucket in _SITE_TYPE_BUCKETS]
            lines.append(f"  {tier}: {total}  ({', '.join(parts)})")

    lines.extend(["", "SALESFORCE"])
    if summary.get("dry_run"):
        lines.append(
            f"  dry-run — {len(summary['net_new'])} net_new in sf_upload.csv "
            "(no Salesforce writes)"
        )
    else:
        lines.append(f"  attempted : {summary['upload_attempted']}")
        lines.append(f"  loaded    : {len(summary['upload_loaded'])}")
        for item in summary["upload_loaded"]:
            sf_id = item.get("sf_id") or "—"
            lines.append(f"    - {_short_address(item)}  (Id={sf_id})")
        lines.append(f"  failed    : {len(summary['upload_failed'])}")
        for item in summary["upload_failed"]:
            reason = str(item.get("error") or "error").split("\n")[0]
            if len(reason) > 90:
                reason = reason[:89] + "…"
            lines.append(f"    - {_short_address(item)}  ({reason})")
        if summary["upload_skipped"]:
            lines.append(f"  skipped   : {len(summary['upload_skipped'])}")
            for item in summary["upload_skipped"]:
                reason = str(item.get("error") or "skipped").split("\n")[0]
                if len(reason) > 90:
                    reason = reason[:89] + "…"
                lines.append(f"    - {_short_address(item)}  ({reason})")

    lines.append("=" * 72)
    return "\n".join(lines)


def write_run_summary(summary: dict[str, Any], run_dir: Path) -> Path:
    path = Path(run_dir) / "RUN_SUMMARY.txt"
    path.write_text(format_run_summary(summary) + "\n", encoding="utf-8")
    return path


def log_run_summary(summary: dict[str, Any], run_dir: Path | None = None) -> Path | None:
    text = format_run_summary(summary)
    for line in text.splitlines():
        logger.info(line)
    if run_dir is None:
        return None
    path = write_run_summary(summary, run_dir)
    logger.info("Wrote run summary to %s", path.resolve())
    return path
