"""Rank undecided sites for Nearmap so the budget buys the likeliest confirms first.

python scripts/build_nearmap_priority.py --runs 2026-10-08 --out priority.csv
python -m enrichment.lanes --pool-audit --priority-csv priority.csv ...

Reads the newest detail row per Id across the given runs (folder names or
YYYY-MM-DD prefixes, dry runs included) and writes ``Id,tier,why``; lower
tiers run first in ``enrichment.lanes``:

1  a model saw gear (street photo / NAIP / zoom) and no Nearmap oblique yet
2  rooftop or tower host, no gear call, no Nearmap oblique yet
3  other / unclear call, no Nearmap oblique yet
6  already had Nearmap obliques and still undecided (a third view at most)
9  imprecise pin (<= 3 decimals on a coordinate): fix the pin before buying

Confirmed and no-asset rows are left out.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from enrichment.bucketing import imagery_bucket  # noqa: E402
from enrichment.coerce import lower_text, to_bool  # noqa: E402
from enrichment.connectx_audit import (  # noqa: E402
    VERDICT_CONFIRMED,
    VERDICT_NO_ASSET,
    audit_verdict,
)
from enrichment.constants import DETAIL_CSV  # noqa: E402

GEAR_FIELDS = (
    "naip_cell_equipment",
    "gemini_cell_equipment",
    "claude_cell_equipment",
    "naip_screen_cell_equipment",
    "gemini_pre_escalation_cell",
)


def _decimals(value: Any) -> int:
    text = str(value or "").strip()
    return len(text.split(".", 1)[1]) if "." in text else 0


def nearmap_priority(row: dict[str, Any]) -> tuple[float, str] | None:
    """(tier, why) for one detail row, or None when the site is decided."""
    verdict = lower_text(row.get("audit_verdict")) or audit_verdict(row)[0]
    if verdict in {VERDICT_CONFIRMED, VERDICT_NO_ASSET}:
        return None
    lat, lng = row.get("sf_lat"), row.get("sf_lng")
    if min(_decimals(lat), _decimals(lng)) <= 3:
        return 9.0, "imprecise pin"
    if imagery_bucket(row) == "nearmap_oblique":
        return 6.0, "had obliques, still undecided"
    site = lower_text(row.get("naip_site_type") or row.get("site_type"))
    if any(to_bool(row.get(key)) is True for key in GEAR_FIELDS):
        return 1.0, f"gear seen ({site or 'unknown'}), no obliques"
    if site in {"rooftop", "tower"}:
        return 2.0, f"{site} host, no obliques"
    return 3.0, f"{site or 'unknown'}, no obliques"


def newest_rows(run_dirs: list[Path]) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for run in sorted(run_dirs, key=lambda p: p.name):
        path = run / DETAIL_CSV
        if not path.is_file():
            continue
        with path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                sf_id = str(row.get("Id") or "").strip()
                if sf_id:
                    rows[sf_id] = row
    return rows


def main(argv: list[str] | None = None) -> int:
    from enrichment.outputs import expand_run_specs
    from paths import runs_dir

    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--runs", required=True, help="comma list of run folders or YYYY-MM-DD prefixes")
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args(argv)

    specs = [s.strip() for s in args.runs.split(",") if s.strip()]
    rows = newest_rows(list(expand_run_specs(specs, runs_root=runs_dir())))
    ranked = []
    for sf_id, row in rows.items():
        pick = nearmap_priority(row)
        if pick is not None:
            ranked.append((pick[0], sf_id, pick[1]))
    ranked.sort()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Id", "tier", "why"])
        for tier, sf_id, why in ranked:
            writer.writerow([sf_id, f"{tier:g}", why])
    counts: dict[str, int] = {}
    for tier, _sf_id, _why in ranked:
        counts[f"{tier:g}"] = counts.get(f"{tier:g}", 0) + 1
    print(f"{len(rows)} sites read, {len(ranked)} undecided -> {args.out}  tiers: {dict(sorted(counts.items()))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
