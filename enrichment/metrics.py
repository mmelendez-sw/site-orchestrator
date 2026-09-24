"""Cumulative enrichment KPIs for leadership reporting.

Each live run (``APPLY=1``) appends one header to ``runs.jsonl`` and one row
per processed site to ``sites.jsonl`` under SITE_ORCHESTRATOR_DATA/metrics/.
Azure SQL (``metrics_store``) holds the same grain.

UniqueSites counts every distinct Salesforce Id a live run processed —
applied, held out, missed, or errored. WrittenSites is the subset whose
site type / coordinates Salesforce accepted (``sf_update_status=updated``).
Per-Id outcome counts use that Id's latest observation, so a retry that
later succeeds is not double-counted. Dry runs (``APPLY=0``) never touch the
cumulative KPIs.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from enrichment.coerce import is_true, lower_text, text_or_none, to_float
from enrichment.constants import (
    BUCKET_POTENTIAL_UPDATE,
    BUCKET_ROOFTOP,
    NEARMAP_EMPTY_LOCK_CONF,
)
from paths import metrics_dir

RUNS_JSONL = "runs.jsonl"
SITES_JSONL = "sites.jsonl"
KPIS_JSON = "kpis.json"

EMPTY_SCREEN = frozenset({"other", "unclear"})

# Outcome taxonomy. Every processed site gets exactly one.
APPLIED_OUTCOMES = ("applied_rooftop", "applied_tower", "applied_db_skip", "applied_other")
OUTCOMES: tuple[str, ...] = APPLIED_OUTCOMES + (
    "apply_failed",
    "holdout_empty_confirmed",
    "holdout_weak_rooftop",
    "holdout_weak_tower",
    "holdout_empty",
    "holdout_no_nearmap",
    "holdout_no_imagery",
    "holdout_other",
    "db_only_miss",
    "skipped",
    "error",
)

_ERROR_REASONS = frozenset({"classify_error", "sql_error", "missing_sf_coordinates"})
_SKIPPED_REASONS = frozenset({"no_saved_chips", "skip_classify_no_db_hit"})
_NO_IMAGERY_REASONS = frozenset({"no_imagery", "no_naip_imagery"})
# Salesforce statuses meaning an eligible write did not land.
_APPLY_FAILED_STATUSES = frozenset({"failed", "dequeued"})


def screen_site_type(row: dict[str, Any]) -> str:
    """NAIP-screen label when stamped; else final label (legacy CSVs)."""
    return lower_text(row.get("naip_screen_site_type") or row.get("naip_site_type"))


def final_site_type(row: dict[str, Any]) -> str:
    return lower_text(row.get("naip_site_type") or row.get("site_type"))


def nearmap_ran(row: dict[str, Any]) -> bool:
    tier = lower_text(row.get("nearmap_tier"))
    imagery = lower_text(row.get("imagery_used"))
    return tier in {"full", "vert_only", "wide_aoi"} or imagery.startswith("nearmap")


def claude_ran(row: dict[str, Any]) -> bool:
    return lower_text(row.get("escalation_model")) == "claude" or row.get(
        "claude_cell_equipment"
    ) not in (None, "", False)


def outcome_class(row: dict[str, Any]) -> str:
    """Stable outcome taxonomy for rollups (see ``OUTCOMES``).

    A Salesforce-eligible row whose write was refused (``failed``, or dequeued
    by the holdout retry) is ``apply_failed``, not an applied outcome.
    """
    bucket = lower_text(row.get("bucket"))
    site = final_site_type(row)
    reason = lower_text(row.get("holdout_reason"))
    update_type = lower_text(row.get("update_site_type"))
    status = lower_text(row.get("sf_update_status"))
    db_skip = reason == "skip_classify_db_hit"
    if db_skip or bucket == BUCKET_POTENTIAL_UPDATE:
        if status in _APPLY_FAILED_STATUSES:
            return "apply_failed"
        if db_skip:
            return "applied_db_skip"
        if update_type == "rooftop" or site == "rooftop":
            return "applied_rooftop"
        if update_type == "tower" or site == "tower":
            return "applied_tower"
        return "applied_other"
    if reason == "db_only_no_unique_hit":
        return "db_only_miss"
    if reason in _SKIPPED_REASONS:
        return "skipped"
    if reason in _ERROR_REASONS:
        return "error"
    if reason in _NO_IMAGERY_REASONS:
        return "holdout_no_imagery"
    if lower_text(row.get("nearmap_tier")) == "no_coverage":
        return "holdout_no_nearmap"
    conf = to_float(row.get("naip_site_confidence"))
    if (
        site in EMPTY_SCREEN
        and nearmap_ran(row)
        and conf is not None
        and conf >= NEARMAP_EMPTY_LOCK_CONF
    ):
        return "holdout_empty_confirmed"
    if bucket == BUCKET_ROOFTOP or site == "rooftop":
        return "holdout_weak_rooftop"
    if site == "tower":
        return "holdout_weak_tower"
    if site in EMPTY_SCREEN:
        return "holdout_empty"
    return "holdout_other"


def _address_parts(row: dict[str, Any]) -> list[str]:
    return [p.strip() for p in str(row.get("address") or "").split(",") if p.strip()]


def site_state(row: dict[str, Any]) -> str | None:
    raw = text_or_none(row.get("site_state") or row.get("Site_State__c"), 8)
    if raw:
        return raw.upper()
    parts = _address_parts(row)
    if parts:
        token = parts[-1].replace(".", "")
        if 2 <= len(token) <= 3 and token.isalpha():
            return token.upper()
    return None


def site_city(row: dict[str, Any]) -> str | None:
    raw = text_or_none(row.get("site_city") or row.get("Site_City__c"), 80)
    if raw:
        return raw
    parts = _address_parts(row)
    return parts[-2][:80] if len(parts) >= 2 else None


def apply_slice_fields(row: dict[str, Any]) -> dict[str, Any]:
    """Fill Power BI slice keys from a detail row or JSONL record."""
    rec = dict(row)
    rec["site_state"] = site_state(rec)
    rec["site_city"] = site_city(rec)
    rec["carrier"] = text_or_none(
        rec.get("carrier") or rec.get("Carrier_Leasing_Source__c"), 120
    )
    rec["match_source"] = text_or_none(lower_text(rec.get("match_source")) or None, 32)
    rec["dual_model_resolution"] = text_or_none(rec.get("dual_model_resolution"), 48)
    rec["classify_coord_source"] = text_or_none(rec.get("classify_coord_source"), 48)
    rec["asset_offset_m"] = to_float(rec.get("asset_offset_m"))
    return rec


def _queue_limit() -> int | None:
    number = to_float(os.environ.get("LIMIT"))
    return None if number is None else int(number)


def site_record(row: dict[str, Any], *, run_id: str) -> dict[str, Any]:
    """One processed site as stored in ``sites.jsonl`` / EnrichmentSiteOutcome."""
    screen = screen_site_type(row)
    final = final_site_type(row)
    empty_to_nearmap = screen in EMPTY_SCREEN and nearmap_ran(row)
    empty_to_rooftop = empty_to_nearmap and final == "rooftop"
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
        "final_confidence": to_float(row.get("naip_site_confidence")),
        "nearmap_ran": nearmap_ran(row),
        "nearmap_tier": lower_text(row.get("nearmap_tier")),
        "claude_ran": claude_ran(row),
        "escalation_reason": str(row.get("escalation_reason") or ""),
        "second_nearmap": str(row.get("second_nearmap") or ""),
        "empty_to_nearmap": empty_to_nearmap,
        "empty_to_rooftop": empty_to_rooftop,
        "empty_to_rooftop_apply": empty_to_rooftop
        and lower_text(row.get("bucket")) == BUCKET_POTENTIAL_UPDATE,
        "bucket": lower_text(row.get("bucket")),
        "holdout_reason": str(row.get("holdout_reason") or ""),
        "update_site_type": str(row.get("update_site_type") or ""),
        "outcome": outcome_class(row),
        "sf_update_status": str(row.get("sf_update_status") or ""),
    }


def is_successful_sf_write(rec: dict[str, Any]) -> bool:
    """True when Salesforce accepted a site-type/coords update for this record."""
    if lower_text(rec.get("outcome")) not in APPLIED_OUTCOMES:
        return False
    if str(rec.get("holdout_reason") or "").strip() == "db_only_no_unique_hit":
        return False
    return lower_text(rec.get("sf_update_status")) == "updated"


_OUTCOME_SITE_TYPE = {"applied_rooftop": "rooftop", "applied_tower": "tower"}


def written_site_type(rec: dict[str, Any]) -> str:
    """rooftop | tower | other for a write, whoever decided it (imagery or DB)."""
    final = lower_text(rec.get("final_site_type"))
    if final in {"rooftop", "tower"}:
        return final
    return _OUTCOME_SITE_TYPE.get(lower_text(rec.get("outcome")), "other")


def _outcome_counts(records: Iterable[dict[str, Any]]) -> Counter:
    return Counter(str(rec.get("outcome") or "holdout_other") for rec in records)


def _rate(part: int, whole: int) -> float | None:
    return round(part / whole, 3) if whole else None


def snapshot_run(
    rows: Iterable[dict[str, Any]],
    *,
    run_id: str,
    apply_summary: dict[str, Any] | None = None,
    db_only: bool = False,
    confirm_rooftop: bool = False,
) -> dict[str, Any]:
    """This-run header + one site record per processed Salesforce Id."""
    sites = [site_record(r, run_id=run_id) for r in rows if str(r.get("Id") or "")]
    for rec in sites:
        if db_only:
            rec["db_only"] = True
        if confirm_rooftop:
            rec["confirm_rooftop"] = True
    by_outcome = _outcome_counts(sites)
    empty_nm = sum(1 for s in sites if s["empty_to_nearmap"])
    empty_rt_apply = sum(1 for s in sites if s["empty_to_rooftop_apply"])
    apply = apply_summary or {}
    snap: dict[str, Any] = {
        "run_id": run_id,
        "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sites": len(sites),
    }
    snap.update({key: by_outcome.get(key, 0) for key in OUTCOMES if key != "error"})
    snap.update(
        {
            "errors": by_outcome.get("error", 0),
            "nearmap_sites": sum(1 for s in sites if s["nearmap_ran"]),
            "claude_sites": sum(1 for s in sites if s["claude_ran"]),
            "naip_empty_to_nearmap": empty_nm,
            "naip_empty_to_rooftop": sum(1 for s in sites if s["empty_to_rooftop"]),
            "naip_empty_to_rooftop_apply": empty_rt_apply,
            "empty_to_rooftop_apply_rate": _rate(empty_rt_apply, empty_nm),
            "sf_writes": int(apply.get("success") or 0),
            "sf_holdouts_dequeued": int(apply.get("dequeued_holdouts") or 0),
            "sf_write_failed": int(apply.get("failed") or 0),
            "apply_enabled": 1 if apply_summary is not None else 0,
            "queue_states": text_or_none(os.environ.get("STATES"), 80),
            "queue_limit": _queue_limit(),
            "site_records": sites,
        }
    )
    return snap


def rollup_kpis(site_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Cumulative KPIs over live-run site records (chronological order).

    UniqueSites / outcome counts use each Id's latest record. Write counts use
    each Id's latest accepted write. Spend and funnel counts are "ever"
    (any live run ran Nearmap / Claude on that Id).
    """
    latest: dict[str, dict[str, Any]] = {}
    written: dict[str, dict[str, Any]] = {}
    ever: dict[str, set[str]] = {"nearmap": set(), "claude": set(), "empty_nm": set()}
    for rec in site_rows:
        sid = str(rec.get("Id") or "")
        if not sid:
            continue
        latest[sid] = rec
        if is_successful_sf_write(rec):
            written[sid] = rec
        if is_true(rec.get("nearmap_ran")):
            ever["nearmap"].add(sid)
        if is_true(rec.get("claude_ran")):
            ever["claude"].add(sid)
        if is_true(rec.get("empty_to_nearmap")):
            ever["empty_nm"].add(sid)
    n = len(latest)
    by_outcome = _outcome_counts(latest.values())
    # Grouped by the Site_Type written (imagery or tower-database decided);
    # db_skip_sf_writes is the tower-database share of those, not a third kind.
    write_types = Counter(written_site_type(rec) for rec in written.values())
    rooftop = write_types.get("rooftop", 0)
    tower = write_types.get("tower", 0)
    empty_nm = len(ever["empty_nm"])
    empty_rt_apply = sum(
        1 for rec in written.values() if is_true(rec.get("empty_to_rooftop_apply"))
    )
    kpis: dict[str, Any] = {
        "unique_sites": n,
        "written_sites": len(written),
        "rooftop_sf_writes": rooftop,
        "tower_sf_writes": tower,
        "db_skip_sf_writes": sum(
            1 for rec in written.values() if rec.get("outcome") == "applied_db_skip"
        ),
        "rooftop_write_rate": _rate(rooftop, n),
        "tower_write_rate": _rate(tower, n),
        "total_write_rate": _rate(len(written), n),
        "nearmap_sites": len(ever["nearmap"]),
        "claude_sites": len(ever["claude"]),
        "naip_empty_to_nearmap": empty_nm,
        "naip_empty_to_rooftop_apply": empty_rt_apply,
        "empty_to_rooftop_apply_rate": _rate(empty_rt_apply, empty_nm),
    }
    kpis.update(
        {key: by_outcome.get(key, 0) for key in OUTCOMES if key not in APPLIED_OUTCOMES}
    )
    kpis["errors"] = kpis.pop("error")
    kpis["outcomes"] = dict(by_outcome)
    kpis["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return kpis


def rec_true(value: Any) -> bool:
    return is_true(value)


RUN_METRIC_KEYS: tuple[str, ...] = (
    "sites",
    "applied_rooftop",
    "applied_tower",
    "applied_db_skip",
    "apply_failed",
    "holdout_empty_confirmed",
    "holdout_weak_rooftop",
    "holdout_weak_tower",
    "holdout_empty",
    "holdout_no_nearmap",
    "holdout_no_imagery",
    "holdout_other",
    "db_only_miss",
    "skipped",
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
    "written_sites",
    "rooftop_sf_writes",
    "tower_sf_writes",
    "db_skip_sf_writes",
    "total_write_rate",
    "rooftop_write_rate",
    "tower_write_rate",
    "naip_empty_to_nearmap",
    "naip_empty_to_rooftop_apply",
    "empty_to_rooftop_apply_rate",
    "apply_failed",
    "holdout_empty_confirmed",
    "holdout_weak_rooftop",
    "holdout_weak_tower",
    "holdout_empty",
    "holdout_no_nearmap",
    "holdout_no_imagery",
    "holdout_other",
    "db_only_miss",
    "skipped",
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


# Terminal labels. Keep JSON/SQL keys (nearmap_sites / claude_sites) stable;
# those counts are spend (imagery/model ran), not successful applies.
METRIC_DISPLAY_NAMES: dict[str, str] = {
    "nearmap_sites": "nearmap_imagery_ran",
    "claude_sites": "claude_ai_ran",
}


def metric_lines(data: dict[str, Any] | None, keys: tuple[str, ...]) -> list[str]:
    if not data:
        return []
    return [
        f"    {METRIC_DISPLAY_NAMES.get(key, key)}: {format_metric_value(key, data.get(key))}"
        for key in keys
        if key in data
    ]


# ------------------------------ JSONL ledger --------------------------------


def _append_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for rec in records:
            handle.write(json.dumps(rec, ensure_ascii=True) + "\n")


def _rewrite_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        for rec in records:
            handle.write(json.dumps(rec, ensure_ascii=True) + "\n")
    tmp.replace(path)


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


def _write_kpis(root: Path, site_rows: list[dict[str, Any]]) -> dict[str, Any]:
    kpis = rollup_kpis(site_rows)
    (root / KPIS_JSON).write_text(json.dumps(kpis, indent=2), encoding="utf-8")
    return kpis


def drop_runs_from_ledger(
    run_ids: Iterable[str],
    *,
    root: Path | None = None,
) -> dict[str, Any]:
    """Remove run headers and site rows from the local JSONL ledger, then rewrite kpis.json."""
    drop = {str(rid).strip() for rid in run_ids if str(rid).strip()}
    root = root or metrics_dir()

    def keep(rec: dict[str, Any]) -> bool:
        return str(rec.get("run_id") or "") not in drop

    runs = [rec for rec in _read_jsonl(root / RUNS_JSONL) if keep(rec)]
    sites = [rec for rec in _read_jsonl(root / SITES_JSONL) if keep(rec)]
    _rewrite_jsonl(root / RUNS_JSONL, runs)
    _rewrite_jsonl(root / SITES_JSONL, sites)
    return {
        "dropped": sorted(drop),
        "runs": len(runs),
        "site_rows": len(sites),
        "kpis": _write_kpis(root, sites),
    }


def refresh_kpis_from_ledger(*, root: Path | None = None, write: bool = True) -> dict[str, Any]:
    """KPIs from sites.jsonl; rewrites kpis.json unless ``write`` is False."""
    root = root or metrics_dir()
    sites = _read_jsonl(root / SITES_JSONL)
    return _write_kpis(root, sites) if write else rollup_kpis(sites)


_LIVE_STATUSES = frozenset({"updated", "dequeued", "classified_only", "failed"})


def _read_csv(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    import csv

    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _run_is_live(header: dict[str, Any], detail: list[dict[str, Any]]) -> bool:
    """Live run? Old headers predate ``apply_enabled``; infer from SF statuses."""
    if "apply_enabled" in header:
        return is_true(header.get("apply_enabled"))
    return any(
        lower_text(row.get("sf_update_status")) in _LIVE_STATUSES for row in detail
    )


def stamp_statuses_from_apply_log(run_dir: Path, detail: list[dict[str, Any]]) -> None:
    """Fill missing/pending sf_update_status from the run's apply log (old runs)."""
    from enrichment.constants import APPLY_LOG_CSV

    log = {
        str(row.get("Id") or "").strip(): row
        for row in _read_csv(run_dir / APPLY_LOG_CSV)
        if str(row.get("Id") or "").strip()
    }
    for row in detail:
        if lower_text(row.get("sf_update_status")) not in {"", "pending"}:
            continue
        entry = log.get(str(row.get("Id") or "").strip())
        if not entry:
            continue
        try:
            payload = json.loads(entry.get("payload_json") or "{}")
        except json.JSONDecodeError:
            payload = {}
        from enrichment.sf_ops import status_from_entry

        row["sf_update_status"] = status_from_entry(
            {
                "success": is_true(entry.get("success")),
                "dry_run": is_true(entry.get("dry_run")),
                "payload": payload,
            }
        )


def rebuild_site_ledger(
    *, runs_root: Path, root: Path | None = None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Every processed site of every live run, from the runs' own CSVs.

    Backfills the "all processed sites" grain for runs recorded when
    ``sites.jsonl`` only kept successful writes. Runs whose
    ``enrichment_detail.csv`` is gone keep their existing ledger rows.
    Returns (site records in run order, per-run counts).
    """
    from enrichment.constants import DETAIL_CSV

    root = root or metrics_dir()
    headers: dict[str, dict[str, Any]] = {}
    for rec in _read_jsonl(root / RUNS_JSONL):
        rid = str(rec.get("run_id") or "")
        if rid:
            headers[rid] = rec  # re-applied runs: last header wins
    old_by_run: dict[str, list[dict[str, Any]]] = {}
    for rec in _read_jsonl(root / SITES_JSONL):
        old_by_run.setdefault(str(rec.get("run_id") or ""), []).append(rec)
    ordered = sorted(headers.values(), key=lambda h: str(h.get("recorded_at") or ""))
    sites: list[dict[str, Any]] = []
    stats = {"runs": 0, "from_csv": 0, "kept_ledger_rows": 0, "skipped_dry_runs": 0}
    for header in ordered:
        rid = str(header["run_id"])
        run_dir = runs_root / rid
        detail = _read_csv(run_dir / DETAIL_CSV)
        if not detail:
            kept = old_by_run.get(rid, [])
            sites.extend(kept)
            stats["kept_ledger_rows"] += len(kept)
            stats["runs"] += 1 if kept else 0
            continue
        stamp_statuses_from_apply_log(run_dir, detail)
        if not _run_is_live(header, detail):
            stats["skipped_dry_runs"] += 1
            continue
        records = [site_record(row, run_id=rid) for row in detail if row.get("Id")]
        sites.extend(records)
        stats["runs"] += 1
        stats["from_csv"] += len(records)
    return sites, stats


def record_run(
    *,
    run_dir: Path,
    detail_rows: list[dict[str, Any]],
    apply_summary: dict[str, Any] | None = None,
    db_only: bool = False,
    confirm_rooftop: bool = False,
    write_sql: bool | None = None,
) -> dict[str, Any]:
    """Append this run to the ledger, refresh kpis.json, and upsert Azure SQL.

    The run header is always appended (``apply_enabled`` says whether it was
    live). Live runs also append every processed site and rewrite
    ``kpis.json``. ``APPLY=0`` leaves the cumulative ledger and SQL alone.
    """
    run_id = run_dir.name
    snap = snapshot_run(
        detail_rows,
        run_id=run_id,
        apply_summary=apply_summary,
        db_only=db_only,
        confirm_rooftop=confirm_rooftop,
    )
    snap["db_only"] = db_only
    snap["confirm_rooftop"] = confirm_rooftop
    root = metrics_dir()
    root.mkdir(parents=True, exist_ok=True)
    _append_jsonl(root / RUNS_JSONL, [{k: v for k, v in snap.items() if k != "site_records"}])
    live = apply_summary is not None
    snap["kpis"] = None
    if live:
        prior_sites = _read_jsonl(root / SITES_JSONL)
        _append_jsonl(root / SITES_JSONL, snap["site_records"])
        snap["kpis"] = _write_kpis(root, prior_sites + snap["site_records"])
    if live if write_sql is None else write_sql:
        from enrichment.metrics_store import try_write_snapshot

        try_write_snapshot(snap)
    return snap
