"""Nearmap month-to-date spend across every lane: ``python -m enrichment.budget``.

Reads metrics/nearmap_purchases.jsonl (one row per billed purchase, written
by every ``python -m enrichment`` process, dry runs included) plus the legacy
part of metrics/nearmap_usage.jsonl (rows the purchases ledger does not
cover), i.e. the same figure ``classifier.imagery.BUDGET`` enforces.

    python -m enrichment.budget                 # this month
    python -m enrichment.budget --month 2026-09
    python -m enrichment.budget --json

Read-only; no API calls.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from enrichment import metrics
from envutil import env_float

_MB = 1024 * 1024


def _int(value: Any) -> int:
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return 0


def _run_key(rec: dict[str, Any]) -> str:
    run_id = str(rec.get("run_id") or "").strip()
    if run_id:
        return run_id
    proc = str(rec.get("proc") or "").strip()
    return f"pid {proc or rec.get('pid') or '?'}"


def budget_report(*, month: str | None = None, root: Path | None = None) -> dict[str, Any]:
    """Month-to-date Nearmap spend, limits, and per-day / per-run / per-purpose splits.

    Limits use the same env vars and defaults as ``classifier.imagery.NearmapBudget``
    (NEARMAP_MONTHLY_BUDGET_MB=1500, NEARMAP_BUDGET_SOFT_PCT=90); prior use is
    NEARMAP_PRIOR_USE_MB, which the pipeline adds for the current month.
    """
    root = root or metrics.metrics_dir()
    month = month or datetime.now(timezone.utc).strftime("%Y-%m")
    purchases = [
        rec for rec in metrics.read_nearmap_purchases(root=root)
        if str(rec.get("at") or "")[:7] == month
    ]
    usage = [
        rec for rec in metrics._read_jsonl(root / metrics.NEARMAP_USAGE_JSONL)
        if str(rec.get("at") or "")[:7] == month
    ]
    legacy_bytes = sum(_int(rec.get("bytes")) for rec in usage if metrics.is_legacy_usage_row(rec))
    purchase_bytes = sum(_int(rec.get("bytes")) for rec in purchases)
    prior_bytes = int(env_float("NEARMAP_PRIOR_USE_MB", 0) * _MB)
    limit_mb = env_float("NEARMAP_MONTHLY_BUDGET_MB", 1500)
    soft_pct = env_float("NEARMAP_BUDGET_SOFT_PCT", 90)
    limit_bytes = int(limit_mb * _MB) if limit_mb > 0 else 0
    soft_bytes = int(limit_bytes * max(0.0, min(1.0, soft_pct / 100.0)))
    total = legacy_bytes + purchase_bytes + prior_bytes

    by_day: dict[str, int] = defaultdict(int)
    by_run: dict[str, dict[str, Any]] = {}
    by_purpose: dict[str, int] = defaultdict(int)
    for rec in purchases:
        nbytes = _int(rec.get("bytes"))
        at = str(rec.get("at") or "")
        by_day[at[:10]] += nbytes
        by_purpose[str(rec.get("purpose") or "?")] += nbytes
        run = by_run.setdefault(_run_key(rec), {"bytes": 0, "purchases": 0, "first": at, "last": at})
        run["bytes"] += nbytes
        run["purchases"] += 1
        run["first"] = min(run["first"], at)
        run["last"] = max(run["last"], at)

    return {
        "month": month,
        "root": str(root),
        "total_bytes": total,
        "purchases_bytes": purchase_bytes,
        "legacy_usage_bytes": legacy_bytes,
        "prior_use_bytes": prior_bytes,
        "usage_ledger_bytes": sum(_int(rec.get("bytes")) for rec in usage),
        "limit_bytes": limit_bytes,
        "soft_stop_bytes": soft_bytes,
        "by_day": dict(sorted(by_day.items())),
        "by_run": dict(sorted(by_run.items(), key=lambda kv: kv[1]["first"])),
        "by_purpose": dict(sorted(by_purpose.items(), key=lambda kv: -kv[1])),
    }


def format_report(rep: dict[str, Any]) -> list[str]:
    def mb(nbytes: int) -> str:
        return f"{nbytes / _MB:,.1f} MB"

    lines = [f"Nearmap spend {rep['month']} (ledger: {rep['root']})"]
    lines.append(f"  Month to date:   {mb(rep['total_bytes'])}")
    lines.append(
        f"    purchases {mb(rep['purchases_bytes'])} + legacy usage {mb(rep['legacy_usage_bytes'])}"
        f" + prior use (NEARMAP_PRIOR_USE_MB) {mb(rep['prior_use_bytes'])}"
    )
    if rep["limit_bytes"]:
        left = rep["limit_bytes"] - rep["total_bytes"]
        lines.append(f"  Limit:           {mb(rep['limit_bytes'])}  ({mb(max(0, left))} left)")
        lines.append(
            f"  Soft stop:       {mb(rep['soft_stop_bytes'])}  "
            + ("(REACHED: optional purchases off)" if rep["total_bytes"] >= rep["soft_stop_bytes"] else "")
        )
        if left <= 0:
            lines.append("  HARD LIMIT REACHED: Nearmap purchases are blocked")
    else:
        lines.append("  Limit:           off (NEARMAP_MONTHLY_BUDGET_MB=0)")
    if rep["by_day"]:
        lines.append("  By day (purchases ledger):")
        lines.extend(f"    {day}  {mb(n):>12}" for day, n in rep["by_day"].items())
    if rep["by_run"]:
        lines.append("  By run (purchases ledger):")
        for run, info in rep["by_run"].items():
            lines.append(
                f"    {run:<40} {mb(info['bytes']):>12}  {info['purchases']:>6} buys  "
                f"{info['first']} .. {info['last']}"
            )
    if rep["by_purpose"]:
        lines.append("  By purpose: " + ", ".join(f"{k} {mb(v)}" for k, v in rep["by_purpose"].items()))
    if not rep["purchases_bytes"]:
        lines.append("  (no purchases-ledger rows this month yet)")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m enrichment.budget", description=__doc__.splitlines()[0])
    parser.add_argument("--month", help="YYYY-MM (default: current UTC month)")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)
    rep = budget_report(month=args.month)
    if args.json:
        print(json.dumps(rep, indent=2))
    else:
        print("\n".join(format_report(rep)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
