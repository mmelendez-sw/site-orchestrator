"""Build a labelled evaluation set of Site__c rows from Salesforce (read-only).

Ground truth comes from what sales already decided:

- positive: Stage__c in Working - Connected / Working-Connected /
  Qualified (Converted) — a real cell site.
- negative: Stage__c = Unqualified with Unqualified_Reason__c
  No Site/Decommissioned or Not a Cellular tower — nothing there.

  python scripts/build_eval_set.py --dry-run
  python scripts/build_eval_set.py --carrier-like ConnectX --site-type Rooftop

Writes Id,label,stage,unqualified_reason,carrier,site_type,owner to
<data root>/eval/eval_set_<YYYY-MM-DD>.csv. Score runs against it with
scripts/eval_report.py. Only SELECTs; never writes to Salesforce.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

POSITIVE_STAGES = ("Working - Connected", "Working-Connected", "Qualified (Converted)")
NEGATIVE_STAGE = "Unqualified"
NEGATIVE_REASONS = ("No Site/Decommissioned", "Not a Cellular tower")
EVAL_COLUMNS = ("Id", "label", "stage", "unqualified_reason", "carrier", "site_type", "owner")
FIELDS = (
    "Id", "Stage__c", "Unqualified_Reason__c", "Carrier_Leasing_Source__c",
    "Site_Type__c", "Owner__c",
)


def _quote(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _in(values: Sequence[str]) -> str:
    return ", ".join(_quote(v) for v in values)


def build_eval_soql(
    *,
    carrier_like: str | None = None,
    site_types: Sequence[str] | None = None,
    require_coords: bool = True,
    limit: int | None = None,
) -> str:
    label_clause = (
        f"(Stage__c IN ({_in(POSITIVE_STAGES)}) OR "
        f"(Stage__c = {_quote(NEGATIVE_STAGE)} AND "
        f"Unqualified_Reason__c IN ({_in(NEGATIVE_REASONS)})))"
    )
    clauses = [label_clause]
    if carrier_like and carrier_like.strip():
        escaped = carrier_like.strip().replace("\\", "\\\\").replace("'", "\\'")
        clauses.append(f"Carrier_Leasing_Source__c LIKE '%{escaped}%'")
    kinds = [s.strip() for s in (site_types or []) if s and s.strip()]
    if kinds:
        clauses.append(f"Site_Type__c IN ({_in(kinds)})")
    if require_coords:
        clauses.append("Site_Latitude__c != null AND Site_Longitude__c != null")
    soql = f"SELECT {', '.join(FIELDS)} FROM Site__c WHERE " + " AND ".join(clauses) + " ORDER BY Id"
    if limit:
        soql += f" LIMIT {int(limit)}"
    return soql


def label_for(stage: Any, reason: Any) -> str | None:
    """positive / negative / None (not ground truth)."""
    stage = str(stage or "").strip()
    if stage in POSITIVE_STAGES:
        return "positive"
    if stage == NEGATIVE_STAGE and str(reason or "").strip() in NEGATIVE_REASONS:
        return "negative"
    return None


def eval_rows(records: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    out = []
    for rec in records:
        label = label_for(rec.get("Stage__c"), rec.get("Unqualified_Reason__c"))
        if not label:
            continue
        out.append(
            {
                "Id": str(rec.get("Id") or ""),
                "label": label,
                "stage": str(rec.get("Stage__c") or ""),
                "unqualified_reason": str(rec.get("Unqualified_Reason__c") or ""),
                "carrier": str(rec.get("Carrier_Leasing_Source__c") or ""),
                "site_type": str(rec.get("Site_Type__c") or ""),
                "owner": str(rec.get("Owner__c") or ""),
            }
        )
    return out


def write_eval_csv(path: Path, rows: Sequence[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=EVAL_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--carrier-like", default=None, help="Carrier_Leasing_Source__c LIKE %%value%%")
    parser.add_argument("--site-type", action="append", default=[],
                        help="Site_Type__c filter (repeatable), e.g. Rooftop")
    parser.add_argument("--allow-no-coords", action="store_true", help="keep sites without coordinates")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--out", type=Path, default=None, help="default: <data>/eval/eval_set_<date>.csv")
    parser.add_argument("--dry-run", action="store_true", help="print counts only; write nothing")
    args = parser.parse_args(argv)

    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    from enrichment.sf_ops import query_all
    from paths import data_root
    from salesforce.sf_client import SalesforceClient

    soql = build_eval_soql(
        carrier_like=args.carrier_like,
        site_types=args.site_type,
        require_coords=not args.allow_no_coords,
        limit=args.limit,
    )
    print(f"SOQL: {soql}", flush=True)
    rows = eval_rows(query_all(SalesforceClient(), soql))
    labels = Counter(r["label"] for r in rows)
    print(f"eval rows: {len(rows)} (positive {labels['positive']}, negative {labels['negative']})")
    for (label, stage, reason), n in Counter(
        (r["label"], r["stage"], r["unqualified_reason"]) for r in rows
    ).most_common():
        print(f"  {n:6}  {label:8} {stage}" + (f" / {reason}" if reason else ""))
    for site_type, n in Counter(r["site_type"] or "(blank)" for r in rows).most_common(8):
        print(f"  site type {site_type}: {n}")
    if args.dry_run:
        print("dry run: nothing written")
        return 0
    out = args.out or data_root() / "eval" / f"eval_set_{date.today().isoformat()}.csv"
    write_eval_csv(out, rows)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
