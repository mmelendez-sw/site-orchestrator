"""Replay stored classifications through today's Salesforce write rules.

Before shipping a threshold or gate change, run this against recent run
folders. It rebuilds each site's classifier result from
``enrichment_detail.csv`` and re-runs bucketing (no imagery, no models, no
Salesforce), then reports every site whose decision would change.

  python scripts/replay_decisions.py 2026-09
  python scripts/replay_decisions.py 2026-09-17_130656_sf_enrichment --show 50

Exit code 1 when any decision changed, so it can gate a local pre-merge check.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from enrichment.constants import DETAIL_CSV  # noqa: E402
from enrichment.outputs import expand_run_specs  # noqa: E402
from enrichment.pipeline import replay_decision  # noqa: E402
from paths import runs_dir  # noqa: E402


def _decision_key(bucket: str, reason: str, site_type: str) -> str:
    return f"{bucket or '-'} / {reason or 'write'} / {site_type or '-'}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("specs", nargs="+", help="run folder names or YYYY-MM[-DD] prefixes")
    parser.add_argument("--show", type=int, default=20, help="changed rows to print")
    args = parser.parse_args()

    run_dirs: list[Path] = []
    for spec in args.specs:
        if len(spec) == 7 and spec[4] == "-":  # YYYY-MM: every run that month
            run_dirs.extend(sorted(p for p in runs_dir().glob(f"{spec}-*") if p.is_dir()))
        else:
            run_dirs.extend(expand_run_specs([spec], runs_root=runs_dir()))

    replayed = changed = 0
    transitions: Counter = Counter()
    examples: list[str] = []
    for run_dir in run_dirs:
        path = run_dir / DETAIL_CSV
        if not path.is_file():
            continue
        with path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                decision = replay_decision(row)
                if decision is None:
                    continue
                replayed += 1
                before = _decision_key(
                    row.get("bucket", ""), row.get("holdout_reason", ""),
                    row.get("update_site_type", ""),
                )
                after = _decision_key(
                    decision.get("bucket", ""), decision.get("holdout_reason", ""),
                    decision.get("update_site_type", ""),
                )
                if before != after:
                    changed += 1
                    transitions[(before, after)] += 1
                    if len(examples) < args.show:
                        examples.append(f"  {run_dir.name} {row.get('Id')}: {before}  ->  {after}")

    print(f"replayed {replayed} classified site(s) from {len(run_dirs)} run folder(s)")
    print(f"changed decisions: {changed}")
    for (before, after), count in transitions.most_common():
        print(f"  {count:5d}  {before}  ->  {after}")
    if examples:
        print("examples:")
        print("\n".join(examples))
    return 1 if changed else 0


if __name__ == "__main__":
    raise SystemExit(main())
