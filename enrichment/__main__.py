"""python -m enrichment

Salesforce blank Site_Type → FCC/TowerSource → NAIP/Nearmap + Gemini/Claude → auto-apply after each site.
Holdouts dequeue unless DEQUEUE_HOLDOUTS=0. Transient sql_error rows stay
in the Salesforce queue either way. Optional env: STATES, STAGES,
LIMIT, OFFSET, SKIP_FROM, IDS, CARRIER_LIKE, METRO_CLASSIFICATION,
OWNERS, OWNERS_EXCLUDE, SITE_TYPE, LLM_CLASSIFIED, APPLY, DEQUEUE_HOLDOUTS, RUN_DIR, VERBOSE,
RERUN_SITES_FROM, RERUN_HOLDOUTS_FROM, REUSE_CHIPS_FROM, DB_ONLY,
CONFIRM_ROOFTOP, CONFIRM_EXISTING, CONNECTX_AUDIT, APPLY_EXISTING.
Set APPLY=0 to classify without Salesforce writes.
Set CONFIRM_ROOFTOP=1 to audit rooftops with a building-roof check (no
cell-gear bar, no Claude). NAIP first; Nearmap only if that fails.
Success keeps the existing Site_Type (blank → Rooftop). Inconclusive →
no Salesforce write.
Set CONFIRM_EXISTING=1 for Outreach - Verified sales-typed sites: rooftop
picklist rows use that NAIP-then-Nearmap confirm; tower picklist rows use
FCC/NAIP/Nearmap. Failures are left unchanged. Defaults: any owner, no
metro, any Site_Type.
Set CONNECTX_AUDIT=1 to re-verify ConnectX rooftops still owned by a rep
(OwnerId is not the Site Acquisition Team; stages New/Unreviewed,
Enhanced/Unreviewed, Outreach, Outreach - Verified). Full FCC/NAIP/Nearmap/
Claude path. Confirmed → normal Site_Type write. Nearmap obliques with no
gear → OwnerId=Site Acquisition Team, Stage=Unqualified, Unqualified_Reason=
No Site/Decommissioned. Inconclusive → no write. Ids an earlier live audit
decided are skipped (AUDIT_RETRY_INCONCLUSIVE=1 retries inconclusive ones).
AUDIT_POOL=1 audits the Site Acquisition Team's own sites instead;
AUDIT_ASSIGN_TO=<User Id> reassigns every confirmed Rooftop to that user.
AUDIT_UNQUALIFY_OWNER=<User Id> owns no-asset sites (default: the pool).
AUDIT_HOLDOUT_OWNER=<User Id> replaces the unqualify step: no-asset and
inconclusive sites get LLM_Holdout__c=true and that OwnerId only.
SAVED_NEARMAP_CHIPS=1 classifies on Nearmap chips an earlier run bought.
LLM_CLASSIFIED=1/0 filters the audit queue (unset = either).
Set DB_ONLY=1 to skip all imagery. Every processed site is marked
LLM_Classified=true (no LLM_Holdout on success). Unique FCC/TowerSource hits ≤25 m
(or on-structure ≤5 m) also write Site_Type and coords. Remaining blanks
stay classified true until you flip them back (blank Site_Type +
LLM_Classified=true). Default DB-only stages: New/Unreviewed,
Enhanced/Unreviewed, Outreach, Outreach - Verified, Marketing (any owner).
Override stages with STAGES or LEAD_STAGES (comma-separated).
Owner__c and Metro_Classification__c are not filtered by default. Set
OWNERS (comma list, e.g. Site Acquisition Team,Marketing Campaign) or
METRO_CLASSIFICATION (e.g. Major NFL Metro) to scope a run.
Set OWNERS_EXCLUDE to a comma list for Owner__c NOT IN (...).
Set SITE_TYPE=any (or none) to drop the Site_Type filter (blank and typed).
LIMIT takes the first N remaining sites (stable ORDER BY Id). Processed
rows leave the default queue via LLM_Classified=true; SKIP_FROM still
skips prior-run Ids if you re-pull. Salesforce apply errors retry once
with LLM_Classified=false and LLM_Holdout=true.
Set DEQUEUE_HOLDOUTS=0 to apply successes only and leave failed holdouts as-is.
Set APPLY_EXISTING=1 with RUN_DIR to Salesforce-apply a paused run's
enrichment_detail.csv (no re-classify). CONFIRM_ROOFTOP=1 still writes
Site_Type + LLM_Classified only.
Set RERUN_SITES_FROM to run folder names or YYYY-MM-DD prefixes to re-classify
those runs' Outreach - Verified Ids (bypasses LLM_Holdout / blank Site_Type).
RERUN_SKIP_APPLIED=1 (default) drops Ids that already wrote Site_Type.
Set RERUN_HOLDOUTS_FROM to re-classify holdout Ids only.
Set REUSE_CHIPS_FROM to classify saved JPEGs (no Nearmap fetch). If unset,
chips are reused from RERUN_SITES_FROM or RERUN_HOLDOUTS_FROM.
SKIP_FROM=sql skips every Id any live run already recorded in Azure SQL
(dbo.EnrichmentSiteOutcome); combine with run folders / dates by comma.
Throughput: CLASSIFY_WORKERS (default 10) parallel classify threads in one
process; GEMINI_RPM / CLAUDE_RPM (default 120 / 50) pace every model call.
APPLY_BATCH_SIZE (default 25) / APPLY_FLUSH_S (default 60) batch live
Salesforce writes through sObject Collections; 1 writes each site at once.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

ROOT = Path(__file__).resolve().parents[1]

# Banners and progress use non-ASCII (arrows, dashes). On Windows a redirected
# stdout defaults to cp1252 and the first "->" arrow would crash the run.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def limit_source(
    file_vals: dict[str, str | None],
    environ: dict[str, str] | None = None,
) -> str:
    """Where the queue LIMIT comes from: ``terminal``, ``.env``, or ``none``.

    A terminal ``$env:LIMIT`` wins over ``.env`` (load_dotenv never
    overrides). The start banner prints the source so a leftover shell
    value from an earlier slice is visible — clear it with
    ``Remove-Item Env:LIMIT``.
    """
    env = os.environ if environ is None else environ
    if str(env.get("LIMIT") or "").strip():
        return "terminal"
    return ".env" if str(file_vals.get("LIMIT") or "").strip() else "none"


LIMIT_SOURCE = limit_source(dotenv_values(ROOT / ".env"))
load_dotenv(ROOT / ".env")

from enrichment.pipeline import apply_paused_run, default_run_dir, run_enrichment  # noqa: E402
from enrichment.outputs import (  # noqa: E402
    holdout_ids_from_run_specs,
    site_ids_from_run_specs,
)
from enrichment.naip_classify import resolve_reuse_chips_dirs  # noqa: E402
from enrichment.sf_ops import (  # noqa: E402
    parse_carrier_like,
    parse_metro_classification,
    parse_owners,
    parse_site_type,
)
from enrichment.constants import (  # noqa: E402
    CONFIRM_ROOFTOP_STAGE_FILTER,
    CONNECTX_AUDIT_STAGE_FILTER,
    DB_ONLY_STAGE_FILTER,
    DEFAULT_STAGE_FILTER,
)
from enrichment.connectx_audit import (  # noqa: E402
    AUDIT_DRY_RUN_SUFFIX,
    AUDIT_RUN_SUFFIX,
    prior_audit_ids,
    site_acq_owner_id,
    unqualify_owner_id,
)
from paths import ensure_data_layout, runs_dir  # noqa: E402
from salesforce.sf_client import SalesforceClient  # noqa: E402


from envutil import env_csv as _csv_env  # noqa: E402
from envutil import env_flag  # noqa: E402

_RETIRED_DELAYS = ("GEMINI_DELAY_S", "CLAUDE_DELAY_S")
_SQL_SKIP_SPEC = "sql"


def _flag(name: str, default: str = "0") -> bool:
    return env_flag(name, default)


def skip_ids_from_specs(specs: list[str] | None) -> list[str]:
    """SKIP_FROM Ids: run folders / YYYY-MM-DD prefixes, plus ``sql`` for Azure SQL."""
    if not specs:
        return []
    ids: list[str] = []
    run_specs = [spec for spec in specs if spec.lower() != _SQL_SKIP_SPEC]
    if run_specs:
        ids.extend(site_ids_from_run_specs(run_specs, runs_root=runs_dir(), stages=[]))
    if len(run_specs) != len(specs):
        from enrichment.metrics_store import processed_ids

        ids.extend(sorted(processed_ids()))
    return list(dict.fromkeys(ids))


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
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

    ensure_data_layout()
    connectx_audit = _flag("CONNECTX_AUDIT")
    audit_suffix = AUDIT_RUN_SUFFIX if _flag("APPLY", "1") else AUDIT_DRY_RUN_SUFFIX
    run_dir = Path(os.environ["RUN_DIR"]) if os.environ.get("RUN_DIR") else default_run_dir(
        runs_dir(), **({"suffix": audit_suffix} if connectx_audit else {})
    )
    limit_raw = (os.environ.get("LIMIT") or "").strip()
    print(f"  queue LIMIT: {limit_raw or 'none'} (from {LIMIT_SOURCE})", flush=True)
    offset_raw = (os.environ.get("OFFSET") or os.environ.get("QUEUE_OFFSET") or "").strip()
    states = _csv_env("STATES")
    if states:
        states = [s.upper() for s in states]
    stages = _csv_env("STAGES") or _csv_env("LEAD_STAGES")
    site_ids = _csv_env("IDS") or []
    rerun_sites_from = _csv_env("RERUN_SITES_FROM")
    if rerun_sites_from:
        skip_applied = _flag("RERUN_SKIP_APPLIED", "1")
        prior_ids = site_ids_from_run_specs(
            rerun_sites_from,
            runs_root=runs_dir(),
            skip_applied=skip_applied,
        )
        skipped = 0
        if skip_applied:
            skipped = len(
                site_ids_from_run_specs(rerun_sites_from, runs_root=runs_dir())
            ) - len(prior_ids)
        print(
            f"  rerun sites from {', '.join(rerun_sites_from)}: {len(prior_ids)} id(s)"
            + (f" ({skipped} already applied, skipped)" if skipped else ""),
            flush=True,
        )
        site_ids = list(dict.fromkeys([*site_ids, *prior_ids]))
        if not prior_ids:
            raise SystemExit(
                "RERUN_SITES_FROM matched no Outreach - Verified Ids"
            )
    rerun_from = _csv_env("RERUN_HOLDOUTS_FROM")
    if rerun_from:
        holdout_ids = holdout_ids_from_run_specs(rerun_from, runs_root=runs_dir())
        print(
            f"  rerun holdouts from {', '.join(rerun_from)}: {len(holdout_ids)} id(s)",
            flush=True,
        )
        site_ids = list(dict.fromkeys([*site_ids, *holdout_ids]))
    reuse_from = _csv_env("REUSE_CHIPS_FROM") or rerun_sites_from or rerun_from
    reuse_chips_dirs = None
    if reuse_from:
        reuse_chips_dirs = resolve_reuse_chips_dirs(reuse_from, runs_root=runs_dir())
        print(
            f"  reuse chips from {len(reuse_chips_dirs)} folder(s) (no Nearmap fetch)",
            flush=True,
        )
    retired = [name for name in _RETIRED_DELAYS if os.environ.get(name)]
    if retired:
        print(
            f"  note: {', '.join(retired)} no longer used — pacing is GEMINI_RPM / "
            "CLAUDE_RPM shared across CLASSIFY_WORKERS",
            flush=True,
        )
    skip_from = _csv_env("SKIP_FROM")
    skip_ids = skip_ids_from_specs(skip_from)
    if skip_from:
        print(
            f"  skip already attempted from {', '.join(skip_from)}: "
            f"{len(skip_ids)} id(s)",
            flush=True,
        )
    if connectx_audit and not _flag("APPLY_EXISTING"):
        audited = prior_audit_ids(
            runs_dir(), retry_inconclusive=_flag("AUDIT_RETRY_INCONCLUSIVE")
        )
        print(
            f"  skip Ids decided by earlier live ConnectX audits: {len(audited)}",
            flush=True,
        )
        skip_ids = list(dict.fromkeys([*skip_ids, *audited]))

    print("=== AUTHENTICATE SALESFORCE ===", flush=True)
    sf_client = SalesforceClient()
    print("  authenticated", flush=True)

    db_only = _flag("DB_ONLY") or _flag("SKIP_CLASSIFY")
    confirm_rooftop = _flag("CONFIRM_ROOFTOP")
    confirm_existing = _flag("CONFIRM_EXISTING")
    if confirm_rooftop and db_only:
        raise SystemExit("CONFIRM_ROOFTOP=1 cannot be combined with DB_ONLY=1")
    if confirm_existing and (confirm_rooftop or db_only):
        raise SystemExit(
            "CONFIRM_EXISTING=1 cannot be combined with CONFIRM_ROOFTOP=1 or DB_ONLY=1"
        )
    if connectx_audit and (confirm_rooftop or confirm_existing or db_only):
        raise SystemExit(
            "CONNECTX_AUDIT=1 cannot be combined with CONFIRM_ROOFTOP, "
            "CONFIRM_EXISTING, or DB_ONLY"
        )
    if connectx_audit:
        print(
            "  CONNECTX_AUDIT=1 — rep-owned ConnectX rooftops: full FCC/NAIP/"
            "Nearmap/Claude; no gear on obliques → unqualify + reassign to "
            f"{unqualify_owner_id()} (pool {site_acq_owner_id()})",
            flush=True,
        )
        if not stages:
            stages = list(CONNECTX_AUDIT_STAGE_FILTER)
        print(f"  lead stages: {', '.join(stages)}", flush=True)
    audit_pool = connectx_audit and _flag("AUDIT_POOL")
    audit_assign_to = (os.environ.get("AUDIT_ASSIGN_TO") or "").strip() or None
    audit_llm_raw = (os.environ.get("LLM_CLASSIFIED") or "").strip()
    audit_llm_classified = _flag("LLM_CLASSIFIED") if audit_llm_raw else None
    if connectx_audit:
        print(
            f"  audit owners: {'Site Acquisition Team (pool)' if audit_pool else 'reps'}"
            f" | confirmed → {audit_assign_to or 'owner unchanged'}"
            f" | llm_classified={audit_llm_classified if audit_llm_raw else 'any'}",
            flush=True,
        )
    if confirm_rooftop:
        print(
            "  CONFIRM_ROOFTOP=1 — building-roof audit "
            "(NAIP first, Nearmap only if NAIP fails; no Claude, no cell-gear bar)",
            flush=True,
        )
        if not stages:
            stages = list(CONFIRM_ROOFTOP_STAGE_FILTER)
        print(f"  lead stages: {', '.join(stages)}", flush=True)
    if confirm_existing:
        print(
            "  CONFIRM_EXISTING=1 — keep sales Site_Type; rooftops NAIP then "
            "Nearmap on fail; towers FCC/NAIP/Nearmap",
            flush=True,
        )
        if not stages:
            stages = list(DEFAULT_STAGE_FILTER)
        print(f"  lead stages: {', '.join(stages)}", flush=True)
    if db_only:
        print(
            "  DB_ONLY=1 — FCC/TowerSource unique hits only (no Nearmap/NAIP/LLM)",
            flush=True,
        )
        if not stages:
            stages = list(DB_ONLY_STAGE_FILTER)
        print(f"  lead stages: {', '.join(stages)}", flush=True)
    dequeue_default = (
        "0" if db_only or confirm_rooftop or confirm_existing or connectx_audit else "1"
    )
    # Owner and metro filters are off by default; set OWNERS /
    # METRO_CLASSIFICATION to scope a run.
    owners = parse_owners(os.environ.get("OWNERS"), default=None)
    exclude_owners = parse_owners(
        os.environ.get("OWNERS_EXCLUDE"),
        default=None,
    )
    site_type = parse_site_type(
        os.environ.get("SITE_TYPE"),
        default="any" if confirm_rooftop or confirm_existing else None,
    )
    metro_raw = os.environ.get("METRO_CLASSIFICATION")
    metro_default = "none"

    if _flag("APPLY_EXISTING"):
        if not os.environ.get("RUN_DIR"):
            raise SystemExit(
                "APPLY_EXISTING=1 requires RUN_DIR pointing at the paused run folder"
            )
        print(f"  APPLY_EXISTING=1 — {run_dir}", flush=True)
        summary = apply_paused_run(
            sf_client=sf_client,
            run_dir=run_dir,
            apply=_flag("APPLY", "1"),
            confirm_rooftop=confirm_rooftop,
            confirm_existing=confirm_existing,
            db_only=db_only,
            connectx_audit=connectx_audit,
            dequeue_holdouts=_flag("DEQUEUE_HOLDOUTS", dequeue_default),
            verbose=_flag("VERBOSE"),
            max_sites=int(limit_raw) if limit_raw else None,
        )
        failed = (summary.get("apply") or {}).get("failed", 0)
        return 0 if not failed else 2

    summary = run_enrichment(
        sf_client=sf_client,
        run_dir=run_dir,
        limit=int(limit_raw) if limit_raw else None,
        offset=int(offset_raw) if offset_raw else 0,
        skip_ids=skip_ids or None,
        site_ids=site_ids or None,
        states=states,
        stages=stages,
        owners=owners,
        exclude_owners=exclude_owners,
        site_type=site_type,
        carrier_like=parse_carrier_like(
            os.environ.get("CARRIER_LIKE"),
            default="ConnectX" if connectx_audit else None,
        ),
        metro_classification=parse_metro_classification(
            metro_raw, default=metro_default
        ),
        llm_classified=_flag("LLM_CLASSIFIED", "0"),
        apply=_flag("APPLY", "1"),
        dequeue_holdouts=_flag("DEQUEUE_HOLDOUTS", dequeue_default),
        verbose=_flag("VERBOSE"),
        reuse_chips_dirs=reuse_chips_dirs,
        db_only=db_only,
        confirm_rooftop=confirm_rooftop,
        confirm_existing=confirm_existing,
        connectx_audit=connectx_audit,
        audit_pool=audit_pool,
        audit_llm_classified=audit_llm_classified,
        audit_assign_to=audit_assign_to,
    )
    failed = (summary.get("apply") or {}).get("failed", 0)
    return 0 if not failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
