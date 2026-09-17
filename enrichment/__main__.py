"""python -m enrichment

Salesforce blank Site_Type → FCC/TowerSource → NAIP/Nearmap + Gemini/Claude → auto-apply.
Holdouts dequeue unless DEQUEUE_HOLDOUTS=0. Optional env: STATES, STAGES,
LIMIT, OFFSET, SKIP_FROM, IDS, CARRIER_LIKE, METRO_CLASSIFICATION,
LLM_CLASSIFIED, APPLY, DEQUEUE_HOLDOUTS, RUN_DIR, VERBOSE,
RERUN_SITES_FROM, RERUN_HOLDOUTS_FROM, REUSE_CHIPS_FROM, DB_ONLY,
CONFIRM_ROOFTOP, APPLY_EXISTING.
Set APPLY=0 to classify without Salesforce writes.
Set CONFIRM_ROOFTOP=1 to audit existing Site_Type=Rooftop with NAIP/Gemini
only (no Nearmap, no cell-gear bar). A building roof at the pin is enough;
cellular antennas are not required. Rooftop label → persist type +
LLM_Classified=true. Inconclusive → no Salesforce write.
Set DB_ONLY=1 to skip all imagery. Every processed site is marked
LLM_Classified=true (no LLM_Holdout on success). Unique FCC/TowerSource hits ≤25 m
(or on-structure ≤5 m) also write Site_Type and coords. Remaining blanks
stay classified true until you flip them back (blank Site_Type +
LLM_Classified=true). Default DB-only stages: New/Unreviewed,
Enhanced/Unreviewed, Outreach, Outreach - Verified, Marketing (any owner).
Override stages with STAGES or LEAD_STAGES (comma-separated).
Set OWNERS=none (DB-only default) or a comma list. METRO_CLASSIFICATION=none
includes every metro.
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
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def apply_dotenv_limit(
    file_vals: dict[str, str | None],
    environ: dict[str, str] | None = None,
) -> None:
    """Queue LIMIT comes from ``.env`` only.

    Leftover PowerShell ``$env:LIMIT`` stays in the process after a slice run
    and would otherwise cap later jobs even when ``.env`` has no LIMIT.
    """
    env = os.environ if environ is None else environ
    raw = str(file_vals.get("LIMIT") or "").strip()
    if raw:
        env["LIMIT"] = raw
    else:
        env.pop("LIMIT", None)


load_dotenv(ROOT / ".env")
apply_dotenv_limit(dotenv_values(ROOT / ".env"))

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
)
from enrichment.constants import (  # noqa: E402
    CONFIRM_ROOFTOP_STAGE_FILTER,
    DB_ONLY_STAGE_FILTER,
    DEFAULT_OWNER_FILTER,
)
from paths import ensure_data_layout, runs_dir  # noqa: E402
from salesforce.sf_client import SalesforceClient  # noqa: E402


def _csv_env(name: str) -> list[str] | None:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return None
    return [part.strip() for part in raw.split(",") if part.strip()]


def _flag(name: str, default: str = "0") -> bool:
    return (os.environ.get(name) or default).strip().lower() in {"1", "true", "yes"}


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
    run_dir = Path(os.environ["RUN_DIR"]) if os.environ.get("RUN_DIR") else default_run_dir(
        runs_dir()
    )
    limit_raw = (os.environ.get("LIMIT") or "").strip()
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
    skip_from = _csv_env("SKIP_FROM")
    skip_ids: list[str] = []
    if skip_from:
        skip_ids = site_ids_from_run_specs(
            skip_from,
            runs_root=runs_dir(),
            stages=[],
        )
        print(
            f"  skip already attempted from {', '.join(skip_from)}: "
            f"{len(skip_ids)} id(s)",
            flush=True,
        )

    print("=== AUTHENTICATE SALESFORCE ===", flush=True)
    sf_client = SalesforceClient()
    print("  authenticated", flush=True)

    db_only = _flag("DB_ONLY") or _flag("SKIP_CLASSIFY")
    confirm_rooftop = _flag("CONFIRM_ROOFTOP")
    if confirm_rooftop and db_only:
        raise SystemExit("CONFIRM_ROOFTOP=1 cannot be combined with DB_ONLY=1")
    if confirm_rooftop:
        os.environ["NAIP_ONLY"] = "1"
        os.environ["GEMINI_ONLY"] = "1"
        os.environ["BIFURCATED_AI"] = "0"
        print(
            "  CONFIRM_ROOFTOP=1 — NAIP/Gemini building-roof audit "
            "(no Nearmap, no Claude, no cell-gear bar)",
            flush=True,
        )
        if not stages:
            stages = list(CONFIRM_ROOFTOP_STAGE_FILTER)
        print(f"  lead stages: {', '.join(stages)}", flush=True)
    if db_only:
        print(
            "  DB_ONLY=1 — FCC/TowerSource unique hits only (no Nearmap/NAIP/LLM)",
            flush=True,
        )
        if not stages:
            stages = list(DB_ONLY_STAGE_FILTER)
        print(f"  lead stages: {', '.join(stages)}", flush=True)
    dequeue_default = "0" if db_only or confirm_rooftop else "1"
    owners = parse_owners(
        os.environ.get("OWNERS"),
        default=None if db_only or confirm_rooftop else DEFAULT_OWNER_FILTER,
    )
    metro_raw = os.environ.get("METRO_CLASSIFICATION")
    metro_default = (
        "none" if confirm_rooftop else "Major NFL Metro"
    )

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
            db_only=db_only,
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
        carrier_like=parse_carrier_like(os.environ.get("CARRIER_LIKE")),
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
    )
    failed = (summary.get("apply") or {}).get("failed", 0)
    return 0 if not failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
