"""python -m enrichment

Salesforce blank Site_Type → FCC/TowerSource → NAIP/Nearmap + Gemini/Claude → auto-apply.
Holdouts dequeue. Optional env: STATES, LIMIT, IDS, CARRIER_LIKE, LLM_CLASSIFIED, APPLY, RUN_DIR, VERBOSE.
Set APPLY=0 to classify without Salesforce writes.
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
from enrichment.sf_ops import parse_carrier_like  # noqa: E402
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

    print("=== AUTHENTICATE SALESFORCE ===", flush=True)
    sf_client = SalesforceClient()
    print("  authenticated", flush=True)

    summary = run_enrichment(
        sf_client=sf_client,
        run_dir=run_dir,
        limit=int(limit_raw) if limit_raw else None,
        site_ids=_csv_env("IDS"),
        states=states,
        carrier_like=parse_carrier_like(os.environ.get("CARRIER_LIKE")),
        llm_classified=_flag("LLM_CLASSIFIED", "0"),
        apply=_flag("APPLY", "1"),
        verbose=_flag("VERBOSE"),
    )
    print(summary, flush=True)
    failed = (summary.get("apply") or {}).get("failed", 0)
    return 0 if not failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
