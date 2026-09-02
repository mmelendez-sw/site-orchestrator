"""python -m enrichment

Salesforce blank Site_Type → FCC/TowerSource → NAIP/Nearmap + Gemini/Claude → auto-apply.
Holdouts dequeue unless DEQUEUE_HOLDOUTS=0. Optional env: STATES, STAGES,
LIMIT, IDS, CARRIER_LIKE, METRO_CLASSIFICATION, LLM_CLASSIFIED, APPLY,
DEQUEUE_HOLDOUTS, RUN_DIR, VERBOSE, RERUN_SITES_FROM, RERUN_HOLDOUTS_FROM,
REUSE_CHIPS_FROM.
Set APPLY=0 to classify without Salesforce writes.
Set DEQUEUE_HOLDOUTS=0 to apply successes only and leave failed holdouts as-is.
Set RERUN_SITES_FROM to run folder names or YYYY-MM-DD prefixes to re-classify
those runs' Outreach - Verified Ids (bypasses LLM_Holdout / blank Site_Type).
Set RERUN_HOLDOUTS_FROM to re-classify holdout Ids only.
Set REUSE_CHIPS_FROM to classify saved JPEGs (no Nearmap fetch). If unset,
chips are reused from RERUN_SITES_FROM or RERUN_HOLDOUTS_FROM.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

from enrichment.pipeline import default_run_dir, run_enrichment  # noqa: E402
from enrichment.outputs import (  # noqa: E402
    holdout_ids_from_run_specs,
    site_ids_from_run_specs,
)
from enrichment.naip_classify import resolve_reuse_chips_dirs  # noqa: E402
from enrichment.sf_ops import parse_carrier_like, parse_metro_classification  # noqa: E402
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
    states = _csv_env("STATES")
    if states:
        states = [s.upper() for s in states]
    stages = _csv_env("STAGES")
    site_ids = _csv_env("IDS") or []
    rerun_sites_from = _csv_env("RERUN_SITES_FROM")
    if rerun_sites_from:
        prior_ids = site_ids_from_run_specs(rerun_sites_from, runs_root=runs_dir())
        print(
            f"  rerun sites from {', '.join(rerun_sites_from)}: {len(prior_ids)} id(s)",
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

    print("=== AUTHENTICATE SALESFORCE ===", flush=True)
    sf_client = SalesforceClient()
    print("  authenticated", flush=True)

    summary = run_enrichment(
        sf_client=sf_client,
        run_dir=run_dir,
        limit=int(limit_raw) if limit_raw else None,
        site_ids=site_ids or None,
        states=states,
        stages=stages,
        carrier_like=parse_carrier_like(os.environ.get("CARRIER_LIKE")),
        metro_classification=parse_metro_classification(
            os.environ.get("METRO_CLASSIFICATION")
        ),
        llm_classified=_flag("LLM_CLASSIFIED", "0"),
        apply=_flag("APPLY", "1"),
        dequeue_holdouts=_flag("DEQUEUE_HOLDOUTS", "1"),
        verbose=_flag("VERBOSE"),
        reuse_chips_dirs=reuse_chips_dirs,
    )
    failed = (summary.get("apply") or {}).get("failed", 0)
    return 0 if not failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
