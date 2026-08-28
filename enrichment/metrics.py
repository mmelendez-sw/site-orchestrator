"""Cumulative enrichment KPIs for leadership reporting.

Each run appends to JSONL under SITE_ORCHESTRATOR_DATA/metrics/.
``kpis.json`` is last-wins per Salesforce Id so retries do not double-count.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from enrichment.constants import (
    BUCKET_POTENTIAL_UPDATE,
    BUCKET_ROOFTOP,
    GEMINI_TOWER_SKIP_CLAUDE_CONF,
)
from paths import metrics_dir

RUNS_JSONL = "runs.jsonl"
SITES_JSONL = "sites.jsonl"
KPIS_JSON = "kpis.json"

EMPTY_SCREEN = frozenset({"other", "unclear"})


def _lower(value: Any) -> str:
    return str(value or "").strip().lower()


def _conf(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def screen_site_type(row: dict[str, Any]) -> str:
    """NAIP-screen label when stamped; else final label (legacy CSVs)."""
    return _lower(row.get("naip_screen_site_type") or row.get("naip_site_type"))


def final_site_type(row: dict[str, Any]) -> str:
    return _lower(row.get("naip_site_type") or row.get("site_type"))


def nearmap_ran(row: dict[str, Any]) -> bool:
    tier = _lower(row.get("nearmap_tier"))
    imagery = _lower(row.get("imagery_used"))
    return tier in {"full", "vert_only", "wide_aoi"} or imagery.startswith("nearmap")


def claude_ran(row: dict[str, Any]) -> bool:
    return _lower(row.get("escalation_model")) == "claude" or row.get(
        "claude_cell_equipment"
    ) not in (None, "", False)


def outcome_class(row: dict[str, Any]) -> str:
    """Stable outcome taxonomy for rollups."""
    bucket = _lower(row.get("bucket"))
    site = final_site_type(row)
    reason = _lower(row.get("holdout_reason"))
    update_type = str(row.get("update_site_type") or "").strip().lower()
    if reason == "skip_classify_db_hit":
        return "applied_db_skip"
    if bucket == BUCKET_POTENTIAL_UPDATE:
        if update_type == "rooftop" or site == "rooftop":
            return "applied_rooftop"
        if update_type == "tower" or site == "tower":
            return "applied_tower"
        return "applied_other"
    if "skip_classify_db" in reason:
        return "applied_db_skip" if bucket == BUCKET_POTENTIAL_UPDATE else "holdout_db_skip"
    if reason in {"classify_error", "sql_error", "missing_sf_coordinates"}:
        return "error"
    if _lower(row.get("nearmap_tier")) == "no_coverage":
        return "holdout_no_nearmap"
    conf = _conf(row.get("naip_site_confidence"))
    if site in EMPTY_SCREEN and nearmap_ran(row) and conf is not None and conf >= GEMINI_TOWER_SKIP_CLAUDE_CONF:
        return "holdout_empty_confirmed"
    if bucket == BUCKET_ROOFTOP or (site == "rooftop" and bucket != BUCKET_POTENTIAL_UPDATE):
        return "holdout_weak_rooftop"
    if site == "tower":
        return "holdout_weak_tower"
    if site in EMPTY_SCREEN:
        return "holdout_empty"
    return "holdout_other"


def _opt(value: Any, n: int) -> str | None:
    text = str(value or "").strip()
    return text[:n] if text else None


def site_state(row: dict[str, Any]) -> str | None:
    raw = _opt(row.get("site_state") or row.get("Site_State__c"), 8)
    if raw:
        return raw.upper()
    parts = [
        p.strip()
        for p in str(row.get("address") or "").split(",")
        if p.strip()
    ]
    if parts:
        token = parts[-1].replace(".", "")
        if 2 <= len(token) <= 3 and token.isalpha():
            return token.upper()
    return None


def site_city(row: dict[str, Any]) -> str | None:
    raw = _opt(row.get("site_city") or row.get("Site_City__c"), 80)
    if raw:
        return raw
    parts = [
        p.strip()
        for p in str(row.get("address") or "").split(",")
        if p.strip()
    ]
    if len(parts) >= 2:
        return parts[-2][:80]
    return None


def apply_slice_fields(row: dict[str, Any]) -> dict[str, Any]:
    """Fill Power BI slice keys from a detail row or JSONL record."""
    rec = dict(row)
    rec["site_state"] = site_state(rec)
    rec["site_city"] = site_city(rec)
    rec["carrier"] = _opt(
        rec.get("carrier") or rec.get("Carrier_Leasing_Source__c"), 120
    )
    rec["match_source"] = _opt(_lower(rec.get("match_source")) or None, 32)
    rec["dual_model_resolution"] = _opt(rec.get("dual_model_resolution"), 48)
    rec["classify_coord_source"] = _opt(rec.get("classify_coord_source"), 48)
    rec["asset_offset_m"] = _conf(rec.get("asset_offset_m"))
    return rec


def _queue_states() -> str | None:
    return _opt(os.environ.get("STATES"), 80)


def _queue_limit() -> int | None:
    raw = (os.environ.get("LIMIT") or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def site_record(row: dict[str, Any], *, run_id: str) -> dict[str, Any]:
    screen = screen_site_type(row)
    final = final_site_type(row)
    empty_to_nearmap = screen in EMPTY_SCREEN and nearmap_ran(row)
    empty_to_rooftop = empty_to_nearmap and final == "rooftop"
    empty_to_rooftop_apply = empty_to_rooftop and _lower(row.get("bucket")) == BUCKET_POTENTIAL_UPDATE
    rec = apply_slice_fields(row)
    return {
        "run_id": run_id,
        "Id": str(row.get("Id") or ""),
        "address": ", ".join(
            p
            for p in (
                str(row.get("Site_Street__c") or "").strip(),
                str(row.get("Site_City__c") or "").strip(),
                str(row.get("Site_State__c") or "").strip(),
            )
            if p
        ),
        "site_state": rec["site_state"],
        "site_city": rec["site_city"],
        "carrier": rec["carrier"],
        "match_source": rec["match_source"],
        "dual_model_resolution": rec["dual_model_resolution"],
        "classify_coord_source": rec["classify_coord_source"],
        "asset_offset_m": rec["asset_offset_m"],
        "screen_site_type": screen,
        "final_site_type": final,
        "final_confidence": _conf(row.get("naip_site_confidence")),
        "nearmap_ran": nearmap_ran(row),
        "nearmap_tier": _lower(row.get("nearmap_tier")),
        "claude_ran": claude_ran(row),
        "escalation_reason": str(row.get("escalation_reason") or ""),
        "second_nearmap": str(row.get("second_nearmap") or ""),
        "empty_to_nearmap": empty_to_nearmap,
        "empty_to_rooftop": empty_to_rooftop,
        "empty_to_rooftop_apply": empty_to_rooftop_apply,
        "bucket": _lower(row.get("bucket")),
        "holdout_reason": str(row.get("holdout_reason") or ""),
        "update_site_type": str(row.get("update_site_type") or ""),
        "outcome": outcome_class(row),
        "sf_update_status": str(row.get("sf_update_status") or ""),
    }


def snapshot_run(
    rows: Iterable[dict[str, Any]],
    *,
    run_id: str,
    apply_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sites = [site_record(r, run_id=run_id) for r in rows if str(r.get("Id") or "")]
    n = len(sites)
    empty_nm = sum(1 for s in sites if s["empty_to_nearmap"])
    empty_rt = sum(1 for s in sites if s["empty_to_rooftop"])
    empty_rt_apply = sum(1 for s in sites if s["empty_to_rooftop_apply"])
    apply = apply_summary or {}
    by_outcome: dict[str, int] = {}
    for rec in sites:
        key = str(rec.get("outcome") or "unknown")
        by_outcome[key] = by_outcome.get(key, 0) + 1
    return {
        "run_id": run_id,
        "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sites": n,
        "applied_rooftop": by_outcome.get("applied_rooftop", 0),
        "applied_tower": by_outcome.get("applied_tower", 0),
        "applied_db_skip": by_outcome.get("applied_db_skip", 0),
        "holdout_empty_confirmed": by_outcome.get("holdout_empty_confirmed", 0),
        "holdout_weak_rooftop": by_outcome.get("holdout_weak_rooftop", 0),
        "holdout_weak_tower": by_outcome.get("holdout_weak_tower", 0),
        "holdout_empty": by_outcome.get("holdout_empty", 0),
        "holdout_no_nearmap": by_outcome.get("holdout_no_nearmap", 0),
        "errors": by_outcome.get("error", 0),
        "nearmap_sites": sum(1 for s in sites if s["nearmap_ran"]),
        "claude_sites": sum(1 for s in sites if s["claude_ran"]),
        "naip_empty_to_nearmap": empty_nm,
        "naip_empty_to_rooftop": empty_rt,
        "naip_empty_to_rooftop_apply": empty_rt_apply,
        "empty_to_rooftop_apply_rate": (
            round(empty_rt_apply / empty_nm, 3) if empty_nm else None
        ),
        "sf_writes": int(apply.get("success") or 0),
        "sf_holdouts_dequeued": int(apply.get("dequeued_holdouts") or 0),
        "sf_write_failed": int(apply.get("failed") or 0),
        "apply_enabled": 1 if apply_summary else 0,
        "queue_states": _queue_states(),
        "queue_limit": _queue_limit(),
        "site_records": sites,
    }


def _append_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for rec in records:
            handle.write(json.dumps(rec, ensure_ascii=True) + "\n")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            rows.append(rec)
    return rows


def rollup_kpis(site_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Last observation per Salesforce Id."""
    latest: dict[str, dict[str, Any]] = {}
    for rec in site_rows:
        sid = str(rec.get("Id") or "")
        if sid:
            latest[sid] = rec
    sites = list(latest.values())
    n = len(sites)
    empty_nm = sum(1 for s in sites if s.get("empty_to_nearmap"))
    empty_rt_apply = sum(1 for s in sites if s.get("empty_to_rooftop_apply"))
    by_outcome: dict[str, int] = {}
    for rec in sites:
        key = str(rec.get("outcome") or "unknown")
        by_outcome[key] = by_outcome.get(key, 0) + 1
    rooftop_writes = by_outcome.get("applied_rooftop", 0)
    return {
        "unique_sites": n,
        "rooftop_sf_writes": rooftop_writes,
        "tower_sf_writes": by_outcome.get("applied_tower", 0),
        "holdout_empty_confirmed": by_outcome.get("holdout_empty_confirmed", 0),
        "holdout_weak_rooftop": by_outcome.get("holdout_weak_rooftop", 0),
        "holdout_weak_tower": by_outcome.get("holdout_weak_tower", 0),
        "holdout_empty": by_outcome.get("holdout_empty", 0),
        "errors": by_outcome.get("error", 0),
        "nearmap_sites": sum(1 for s in sites if rec_true(s.get("nearmap_ran"))),
        "claude_sites": sum(1 for s in sites if rec_true(s.get("claude_ran"))),
        "naip_empty_to_nearmap": empty_nm,
        "naip_empty_to_rooftop_apply": empty_rt_apply,
        "empty_to_rooftop_apply_rate": (
            round(empty_rt_apply / empty_nm, 3) if empty_nm else None
        ),
        "rooftop_write_rate": round(rooftop_writes / n, 3) if n else None,
        "outcomes": by_outcome,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def rec_true(value: Any) -> bool:
    return value is True or str(value).strip().lower() in {"true", "1"}


RUN_METRIC_KEYS: tuple[str, ...] = (
    "sites",
    "applied_rooftop",
    "applied_tower",
    "applied_db_skip",
    "holdout_empty_confirmed",
    "holdout_weak_rooftop",
    "holdout_weak_tower",
    "holdout_empty",
    "holdout_no_nearmap",
    "errors",
    "nearmap_sites",
    "claude_sites",
    "naip_empty_to_nearmap",
    "naip_empty_to_rooftop",
    "naip_empty_to_rooftop_apply",
    "empty_to_rooftop_apply_rate",
    "sf_writes",
    "sf_holdouts_dequeued",
    "sf_write_failed",
)

KPI_METRIC_KEYS: tuple[str, ...] = (
    "unique_sites",
    "rooftop_sf_writes",
    "tower_sf_writes",
    "rooftop_write_rate",
    "naip_empty_to_nearmap",
    "naip_empty_to_rooftop_apply",
    "empty_to_rooftop_apply_rate",
    "holdout_empty_confirmed",
    "holdout_weak_rooftop",
    "holdout_weak_tower",
    "holdout_empty",
    "errors",
    "nearmap_sites",
    "claude_sites",
)


def format_metric_value(key: str, value: Any) -> str:
    if value is None or value == "":
        return "—"
    if key.endswith("_rate"):
        try:
            return f"{float(value) * 100:.1f}%"
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def metric_lines(data: dict[str, Any] | None, keys: tuple[str, ...]) -> list[str]:
    if not data:
        return []
    lines: list[str] = []
    for key in keys:
        if key not in data:
            continue
        lines.append(f"    {key}: {format_metric_value(key, data.get(key))}")
    return lines


def record_run(
    *,
    run_dir: Path,
    detail_rows: list[dict[str, Any]],
    apply_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append this run to the cumulative ledger and refresh kpis.json."""
    run_id = run_dir.name
    snap = snapshot_run(detail_rows, run_id=run_id, apply_summary=apply_summary)
    root = metrics_dir()
    root.mkdir(parents=True, exist_ok=True)
    run_rec = {k: v for k, v in snap.items() if k != "site_records"}
    _append_jsonl(root / RUNS_JSONL, [run_rec])
    _append_jsonl(root / SITES_JSONL, snap["site_records"])
    kpis = rollup_kpis(_read_jsonl(root / SITES_JSONL))
    (root / KPIS_JSON).write_text(json.dumps(kpis, indent=2), encoding="utf-8")
    snap["kpis"] = kpis
    from enrichment.metrics_store import try_write_snapshot

    try_write_snapshot(snap)
    return snap
