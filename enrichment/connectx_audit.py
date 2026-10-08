"""ConnectX rooftop audit (``CONNECTX_AUDIT=1``).

The ConnectX import typed ~3,700 sites Rooftop with Verified_Site_Source =
Verbal Confirmation, and the NAIP rooftop confirm only proved a building was
there. This audit runs the full classify path (FCC/TowerSource, NAIP, Nearmap
Vert + obliques, Claude cell confirm) on ConnectX rooftops still owned by an
individual rep, then gives every finished row one verdict:

- ``confirmed`` — the standard write gates passed (unique FCC/TowerSource hit,
  or Nearmap obliques + dual-model cell). Writes the usual Site_Type /
  coords / Verified_Site_Source; owner and stage are untouched.
- ``no_asset`` — Nearmap obliques were reviewed, no model saw cell gear, the
  call is confident and unhedged, no stealth host is described, and no
  FCC/TowerSource record sits within ``AUDIT_DB_VETO_M``. Reassigns to the
  Site Acquisition Team and unqualifies (No Site/Decommissioned).
- ``inconclusive`` — anything else (NAIP-only, no Nearmap coverage, budget
  stop, gear seen but not confirmed, weak call). No Salesforce write; the
  site stays with its rep.

Pool mode (``AUDIT_POOL=1``) audits the Site Acquisition Team's own ConnectX
rooftops instead, and ``AUDIT_ASSIGN_TO`` hands every confirmed *Rooftop* to
that user (e.g. to refill a rep's book). Tower confirms keep their owner.
"""

from __future__ import annotations

import csv
import json
import os
from datetime import date
from pathlib import Path
from typing import Any, Sequence

from classifier.evidence import evidence_text, has_any, has_stealth_form
from envutil import env_float

from enrichment.bucketing import imagery_bucket
from enrichment.coerce import lower_text, to_bool, to_float
from enrichment.constants import (
    AUDIT_UNQUALIFIED_REASON,
    AUDIT_UNQUALIFIED_STAGE,
    BUCKET_AUDIT_HOLDOUT,
    BUCKET_AUDIT_UNQUALIFY,
    BUCKET_POTENTIAL_UPDATE,
    CONNECTX_AUDIT_STAGE_FILTER,
    DETAIL_CSV,
    DISCERNIBLE_CELL_GEAR_KINDS,
    MATCH_SOURCE_NONE,
    SF_QUERY_FIELDS,
    SITE_ACQ_TEAM_OWNER_ID,
)

VERDICT_CONFIRMED = "confirmed"
VERDICT_NO_ASSET = "no_asset"
VERDICT_INCONCLUSIVE = "inconclusive"

# Live audit runs land in <stamp>_connectx_audit; APPLY=0 runs get the
# _dryrun suffix so they never feed the prior-audit skip list.
AUDIT_RUN_SUFFIX = "_connectx_audit"
AUDIT_DRY_RUN_SUFFIX = "_connectx_audit_dryrun"

# An FCC/TowerSource record this close to the pin blocks an unqualify.
AUDIT_DB_VETO_M = env_float("AUDIT_DB_VETO_M", 100)
# Nearmap "other" (no host structure) must be at least this confident.
AUDIT_EMPTY_SITE_CONF = env_float("AUDIT_EMPTY_SITE_CONF", 0.70)
# Nearmap rooftop with cell_equipment=false must be at least this confident.
AUDIT_NO_GEAR_CELL_CONF = env_float("AUDIT_NO_GEAR_CELL_CONF", 0.75)

# Inconclusive reasons a later audit should retry (transient, not evidence).
_RETRY_REASONS = frozenset(
    {"nearmap_budget", "sql_error", "classify_error", "error", "no_saved_chips"}
)
_UNCERTAIN_CUES = (
    "cannot confirm",
    "can't confirm",
    "uncertain",
    "low resolution",
    "too small to",
    "hard to tell",
    "unable to distinguish",
    "obscured",
)
# Rooftop hosts that hide antennas from obliques (concealment, not absence).
_STEALTH_HOST_CUES = (
    "steeple",
    "cupola",
    "bell tower",
    "clock tower",
    "chimney",
    "penthouse",
    "screen wall",
    "rf screen",
    "frp",
    "conceal",
    "stealth",
    "flagpole",
)
# Dual-model outcomes where one model claimed gear and a gate overrode it.
_DISPUTED_RESOLUTIONS = frozenset(
    {
        "claude_veto",
        "soft_keep_gemini",
        "claimed_site_keep_gemini",
        "first_pass_gate",
        "box_required",
    }
)
_CELL_FIELDS = (
    "naip_cell_equipment",
    "gemini_cell_equipment",
    "claude_cell_equipment",
    "naip_screen_cell_equipment",
)


def site_acq_owner_id() -> str:
    """The Site Acquisition Team pool user (``SITE_ACQ_OWNER_ID`` overrides)."""
    return (os.environ.get("SITE_ACQ_OWNER_ID") or "").strip() or SITE_ACQ_TEAM_OWNER_ID


def unqualify_owner_id() -> str:
    """Owner for ``no_asset`` sites: ``AUDIT_UNQUALIFY_OWNER``, else the pool."""
    return (os.environ.get("AUDIT_UNQUALIFY_OWNER") or "").strip() or site_acq_owner_id()


def audit_holdout_owner_id() -> str:
    """``AUDIT_HOLDOUT_OWNER``: when set, unconfirmed sites (no asset or
    inconclusive) get LLM_Holdout__c=true and this OwnerId instead of being
    unqualified."""
    return (os.environ.get("AUDIT_HOLDOUT_OWNER") or "").strip()


def _soql_quote(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _soql_in(values: Sequence[str]) -> str:
    return ", ".join(_soql_quote(v) for v in values)


def build_connectx_audit_query(
    *,
    stages: Sequence[str] | None = None,
    carrier_like: str | None = "ConnectX",
    states: Sequence[str] | None = None,
    owners: Sequence[str] | None = None,
    exclude_owners: Sequence[str] | None = None,
    exclude_owner_id: str = SITE_ACQ_TEAM_OWNER_ID,
    pool: bool = False,
    llm_classified: bool | None = None,
    fields: Sequence[str] = SF_QUERY_FIELDS,
) -> str:
    """SOQL for ConnectX rooftops owned by a rep (or, ``pool``, by the pool).

    No LLM_Classified filter unless ``llm_classified`` is set: most of these
    were flagged by the NAIP rooftop confirm, which is what is re-checked.
    """
    stage_list = [s for s in (stages or CONNECTX_AUDIT_STAGE_FILTER) if str(s).strip()]
    owner_op = "=" if pool else "!="
    clauses = [
        "Site_Type__c = 'Rooftop'",
        f"OwnerId {owner_op} {_soql_quote(exclude_owner_id)}",
        "Site_Latitude__c != null AND Site_Latitude__c != ''",
        "Site_Longitude__c != null AND Site_Longitude__c != ''",
        f"Stage__c IN ({_soql_in(stage_list)})",
    ]
    carrier = (carrier_like or "").strip()
    if carrier:
        escaped = carrier.replace("\\", "\\\\").replace("'", "\\'")
        clauses.insert(0, f"Carrier_Leasing_Source__c LIKE '%{escaped}%'")
    owner_list = [str(v).strip() for v in (owners or []) if str(v).strip()]
    if owner_list:
        clauses.append(f"Owner__c IN ({_soql_in(owner_list)})")
    excluded = [str(v).strip() for v in (exclude_owners or []) if str(v).strip()]
    if excluded:
        clauses.append(f"Owner__c NOT IN ({_soql_in(excluded)})")
    clean_states = [str(s).strip().upper() for s in (states or []) if str(s).strip()]
    if clean_states:
        clauses.append(f"Site_State__c IN ({_soql_in(clean_states)})")
    if llm_classified is not None:
        clauses.append(f"LLM_Classified__c = {'true' if llm_classified else 'false'}")
    return (
        f"SELECT {', '.join(fields)} FROM Site__c WHERE "
        + " AND ".join(clauses)
        + " ORDER BY Id"
    )


def query_connectx_audit_sites(client, **kwargs) -> list[dict[str, Any]]:
    from enrichment.sf_ops import query_all

    return query_all(client, build_connectx_audit_query(**kwargs))


def _confirmed_reason(row: dict[str, Any]) -> str:
    """Name the imagery that carried a confirmation."""
    if lower_text(row.get("update_verified_site_source")) == "google map":
        return "street_cell_confirmed"
    if imagery_bucket(row) in {"nearmap_oblique", "nearmap_vert"}:
        return "nearmap_cell_confirmed"
    return "naip_cell_confirmed"


def audit_verdict(row: dict[str, Any]) -> tuple[str, str]:
    """(verdict, reason) for one finished detail row. Pure; works on CSV rows."""
    if lower_text(row.get("bucket")) == BUCKET_POTENTIAL_UPDATE:
        if lower_text(row.get("holdout_reason")) == "skip_classify_db_hit":
            return VERDICT_CONFIRMED, "db_hit"
        return VERDICT_CONFIRMED, _confirmed_reason(row)
    reason = lower_text(row.get("holdout_reason"))
    if reason in _RETRY_REASONS:
        return VERDICT_INCONCLUSIVE, reason
    if str(row.get("error") or "").strip():
        return VERDICT_INCONCLUSIVE, "error"
    if any(to_bool(row.get(key)) is True for key in _CELL_FIELDS):
        return VERDICT_INCONCLUSIVE, "gear_seen_unconfirmed"
    if lower_text(row.get("dual_model_resolution")) in _DISPUTED_RESOLUTIONS:
        return VERDICT_INCONCLUSIVE, "gear_claim_disputed"
    if lower_text(row.get("nearmap_tier")) == "no_coverage":
        return VERDICT_INCONCLUSIVE, "no_nearmap_coverage"
    if imagery_bucket(row) != "nearmap_oblique":
        return VERDICT_INCONCLUSIVE, "no_nearmap_obliques"
    match_m = to_float(row.get("match_distance_m"))
    if (
        str(row.get("match_source") or MATCH_SOURCE_NONE) != MATCH_SOURCE_NONE
        and (match_m is None or match_m <= AUDIT_DB_VETO_M)
    ):
        return VERDICT_INCONCLUSIVE, "db_record_nearby"
    if lower_text(row.get("signal_strength")) == "strong":
        # Licensed microwave dish / tagged telecom antenna within ~30 m.
        return VERDICT_INCONCLUSIVE, "signal_nearby"
    if lower_text(row.get("cell_gear_kind")) in DISCERNIBLE_CELL_GEAR_KINDS:
        return VERDICT_INCONCLUSIVE, "gear_kind_named"
    text = evidence_text(row)
    if has_any(text, _UNCERTAIN_CUES):
        return VERDICT_INCONCLUSIVE, "hedged_evidence"
    if has_any(text, _STEALTH_HOST_CUES) or has_stealth_form(text):
        return VERDICT_INCONCLUSIVE, "possible_stealth_host"
    site = lower_text(row.get("naip_site_type"))
    site_conf = to_float(row.get("naip_site_confidence"))
    if site == "other" and site_conf is not None and site_conf >= AUDIT_EMPTY_SITE_CONF:
        return VERDICT_NO_ASSET, "nearmap_empty"
    cell_conf = to_float(row.get("cell_equipment_confidence"))
    if (
        site == "rooftop"
        and to_bool(row.get("naip_cell_equipment")) is False
        and cell_conf is not None
        and cell_conf >= AUDIT_NO_GEAR_CELL_CONF
    ):
        return VERDICT_NO_ASSET, "nearmap_roof_no_gear"
    return VERDICT_INCONCLUSIVE, "weak_nearmap_call"


def unqualify_note(reason: str, run_id: str) -> str:
    detail = {
        "nearmap_empty": "no host structure",
        "nearmap_roof_no_gear": "roof present, no telecom gear",
    }.get(reason, reason)
    return f"ConnectX audit {run_id}: Nearmap obliques show {detail}."[:255]


def stamp_audit_verdict(
    row: dict[str, Any],
    *,
    run_id: str,
    owner_id: str | None = None,
    assign_confirmed_to: str | None = None,
    holdout_owner: str | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Stamp the verdict; ``no_asset`` rows become unqualify candidates.

    ``assign_confirmed_to`` adds that OwnerId to confirmed Rooftop writes only.
    ``holdout_owner`` (default ``AUDIT_HOLDOUT_OWNER``) replaces the unqualify
    step: no-asset and inconclusive rows get LLM_Holdout__c=true and that
    OwnerId. Errored rows are left alone so a later run retries them.
    """
    verdict, reason = audit_verdict(row)
    row["audit_verdict"] = verdict
    row["audit_reason"] = reason
    holdout_owner = audit_holdout_owner_id() if holdout_owner is None else holdout_owner
    confirmed_owner = (os.environ.get("AUDIT_CONFIRMED_OWNER") or "").strip()
    if verdict == VERDICT_CONFIRMED and holdout_owner:
        # A re-review may confirm a site an earlier run held out.
        row["update_clear_holdout"] = True
    if verdict == VERDICT_CONFIRMED and confirmed_owner:
        row["update_owner_id"] = confirmed_owner
    # Budget blocks, SQL/classify errors and missing chips stay queued for a retry.
    if holdout_owner and verdict in {VERDICT_NO_ASSET, VERDICT_INCONCLUSIVE} and reason not in _RETRY_REASONS:
        row["bucket"] = BUCKET_AUDIT_HOLDOUT
        row["update_owner_id"] = holdout_owner
        return row
    # Only rooftops are being bought; tower confirms keep their owner.
    if (
        verdict == VERDICT_CONFIRMED
        and assign_confirmed_to
        and str(row.get("update_site_type") or "").strip() == "Rooftop"
    ):
        row["update_owner_id"] = assign_confirmed_to
    if verdict == VERDICT_NO_ASSET:
        row["bucket"] = BUCKET_AUDIT_UNQUALIFY
        row["update_owner_id"] = owner_id or unqualify_owner_id()
        row["update_stage"] = AUDIT_UNQUALIFIED_STAGE
        row["update_unqualified_reason"] = AUDIT_UNQUALIFIED_REASON
        row["update_unqualified_note"] = unqualify_note(reason, run_id)
        row["update_unqualified_date"] = (today or date.today()).isoformat()
    return row


def unqualify_payload(row: dict[str, Any]) -> dict[str, Any]:
    """Salesforce fields for an audit ``no_asset`` row."""
    return {
        "OwnerId": row["update_owner_id"],
        "Stage__c": row["update_stage"],
        "Unqualified_Reason__c": row["update_unqualified_reason"],
        "Other_Unqualified_Reason__c": row.get("update_unqualified_note") or "",
        "Unqualified_Date__c": row.get("update_unqualified_date") or date.today().isoformat(),
        "LLM_Classified__c": True,
    }


def is_audit_holdout_row(row: dict[str, Any]) -> bool:
    return lower_text(row.get("bucket")) == BUCKET_AUDIT_HOLDOUT and bool(
        str(row.get("update_owner_id") or "").strip()
    )


def audit_holdout_payload(row: dict[str, Any]) -> dict[str, Any]:
    """Salesforce fields for an audit holdout row: holdout flag + new owner only."""
    return {"OwnerId": row["update_owner_id"], "LLM_Holdout__c": True}


def confirmed_owner_id(row: dict[str, Any]) -> str:
    """OwnerId to add to a confirmed write, or '' to leave the owner alone."""
    if lower_text(row.get("audit_verdict")) != VERDICT_CONFIRMED:
        return ""
    return str(row.get("update_owner_id") or "").strip()


def is_unqualify_row(row: dict[str, Any]) -> bool:
    return lower_text(row.get("audit_verdict")) == VERDICT_NO_ASSET and bool(
        str(row.get("update_owner_id") or "").strip()
    )


def _audit_run_was_live(run: Path) -> bool:
    """Live suffix, or a dry-run folder later pushed with APPLY_EXISTING=1."""
    if run.name.endswith(AUDIT_RUN_SUFFIX):
        return True
    try:
        summary = json.loads((run / "summary.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (summary.get("apply") or {}).get("apply") is True


def iter_audit_rows(runs_root: Path):
    """Yield (run_dir, live, row) for every row of every audit run folder.

    Statuses are refreshed from each run's apply log, so a crashed run's
    pending rows show what actually reached Salesforce.
    """
    from enrichment.metrics import stamp_statuses_from_apply_log

    if not runs_root.is_dir():
        return
    for run in sorted(runs_root.iterdir()):
        if not (
            run.is_dir()
            and run.name.endswith((AUDIT_RUN_SUFFIX, AUDIT_DRY_RUN_SUFFIX))
        ):
            continue
        path = run / DETAIL_CSV
        if not path.is_file():
            continue
        with path.open(newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
        stamp_statuses_from_apply_log(run, rows)
        live = _audit_run_was_live(run)
        for row in rows:
            yield run, live, row


def prior_audit_ids(runs_root: Path, *, retry_inconclusive: bool = False) -> list[str]:
    """Ids an earlier live audit already decided (skip them next time).

    Written confirms / unqualifies always count (statuses are refreshed from
    the apply log, so a crashed run still counts). Inconclusive rows count
    when the run was live, unless ``retry_inconclusive`` or the reason was
    transient (budget stop, SQL drop, classify error). Failed writes retry.
    """
    ids: list[str] = []
    for _run, live, row in iter_audit_rows(runs_root):
        sf_id = str(row.get("Id") or "").strip()
        verdict = lower_text(row.get("audit_verdict"))
        status = lower_text(row.get("sf_update_status"))
        if not sf_id or status == "failed":
            continue
        if verdict in {VERDICT_CONFIRMED, VERDICT_NO_ASSET}:
            if status in {"updated", "unqualified"}:
                ids.append(sf_id)
        elif verdict == VERDICT_INCONCLUSIVE and live and not retry_inconclusive:
            if lower_text(row.get("audit_reason")) not in _RETRY_REASONS:
                ids.append(sf_id)
    return list(dict.fromkeys(ids))
