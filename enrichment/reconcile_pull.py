"""Pull not-worked ConnectX rooftops from rep books back to the Site Acquisition Team.

python -m enrichment.reconcile_pull

Selects sites assigned on ``PULL_ASSIGNED_ON`` (default 2026-09-22) that are
ConnectX, ``LLM_Classified__c = true``, Site_Type Rooftop, Stage
New/Unreviewed, and owned by an individual user (not the pool). Sites a rep
has touched since the assignment day (field history or last-modified by
anyone other than the assigner / automation) stay with the rep.

``PULL_APPLY=1`` writes ``OwnerId`` = Site Acquisition Team; anything else
only plans. Every pulled site lands in ``swaps/pull_ledger.csv`` with its rep,
state, and original assignment date — the per-rep "owed" count that
``enrichment.reconcile_swaps`` later fills with verified rooftops.
Optional: ``PULL_REPS`` (comma list of rep names), ``PULL_LIMIT``.
"""

from __future__ import annotations

import collections
import csv
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from enrichment.connectx_audit import site_acq_owner_id  # noqa: E402

PULL_LEDGER_CSV = "pull_ledger.csv"
PULL_COLUMNS = (
    "pulled_at",
    "site_id",
    "name",
    "rep_owner_id",
    "rep_name",
    "state",
    "assigned_on",
    "status",
    "error",
)
# Edits by these users do not count as a rep working the site.
NON_REP_USER_NAMES = ("Matthew Melendez", "Automation User", "Automated Process")


@dataclass(frozen=True)
class PullCandidate:
    site_id: str
    name: str
    rep_owner_id: str
    rep_name: str
    state: str
    assigned_on: str


def swaps_dir() -> Path:
    from paths import data_root

    return data_root() / "swaps"


def build_pull_query(*, assigned_on: str, pool_owner_id: str) -> str:
    return (
        "SELECT Id, Name, OwnerId, Owner__c, Site_State__c, Site_Assignment_Date__c, "
        "LastModifiedById FROM Site__c WHERE "
        f"Site_Assignment_Date__c = {assigned_on} "
        "AND Carrier_Leasing_Source__c LIKE '%ConnectX%' "
        "AND LLM_Classified__c = true "
        "AND Site_Type__c = 'Rooftop' "
        "AND Stage__c = 'New/Unreviewed' "
        f"AND OwnerId != '{pool_owner_id}' "
        "AND Owner.Type = 'User' "
        "ORDER BY Owner__c, Id"
    )


def _chunks(items: Sequence[str], size: int = 200):
    for start in range(0, len(items), size):
        yield items[start : start + size]


def non_rep_user_ids(sf) -> set[str]:
    names = ",".join(f"'{n}'" for n in NON_REP_USER_NAMES)
    recs = sf.query_all(f"SELECT Id FROM User WHERE Name IN ({names})")["records"]
    return {r["Id"][:15] for r in recs}


def rep_touched_ids(
    sf, records: Sequence[dict[str, Any]], *, assigned_on: str, non_rep: set[str]
) -> set[str]:
    """Sites edited after the assignment day by someone other than ``non_rep``."""
    touched = {
        r["Id"][:15]
        for r in records
        if str(r.get("LastModifiedById") or "")[:15] not in non_rep
    }
    ids = [r["Id"] for r in records]
    for chunk in _chunks(ids):
        quoted = ",".join(f"'{i}'" for i in chunk)
        for h in sf.query_all(
            "SELECT ParentId, CreatedById FROM Site__History "
            f"WHERE ParentId IN ({quoted}) AND CreatedDate > {assigned_on}T23:59:59-05:00"
        )["records"]:
            if str(h.get("CreatedById") or "")[:15] not in non_rep:
                touched.add(h["ParentId"][:15])
    return touched


def select_candidates(
    records: Iterable[dict[str, Any]],
    *,
    touched: set[str],
    already_pulled: set[str],
    reps: set[str] | None = None,
    limit: int | None = None,
) -> tuple[list[PullCandidate], collections.Counter]:
    """Untouched, not-yet-pulled sites; plus a Counter of why others were kept."""
    kept: collections.Counter = collections.Counter()
    out: list[PullCandidate] = []
    for rec in records:
        sf_id = rec["Id"]
        rep = str(rec.get("Owner__c") or "").strip()
        if sf_id[:15] in {i[:15] for i in already_pulled}:
            kept["already_pulled"] += 1
            continue
        if reps and rep.lower() not in reps:
            kept["other_rep"] += 1
            continue
        if sf_id[:15] in touched:
            kept["rep_touched"] += 1
            continue
        if limit is not None and len(out) >= limit:
            kept["over_limit"] += 1
            continue
        out.append(
            PullCandidate(
                site_id=sf_id,
                name=str(rec.get("Name") or ""),
                rep_owner_id=str(rec.get("OwnerId") or ""),
                rep_name=rep,
                state=str(rec.get("Site_State__c") or "").strip().upper(),
                assigned_on=str(rec.get("Site_Assignment_Date__c") or ""),
            )
        )
    return out, kept


def read_pull_ledger(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def apply_pull(sf, candidates: Sequence[PullCandidate], *, pool_owner_id: str) -> list[dict]:
    """OwnerId → pool for each candidate; ledger rows with per-site status."""
    rows: list[dict] = []
    for chunk in _chunks(list(candidates)):
        body = {
            "allOrNone": False,
            "records": [
                {"attributes": {"type": "Site__c"}, "id": c.site_id, "OwnerId": pool_owner_id}
                for c in chunk
            ],
        }
        result = sf.restful("composite/sobjects", method="PATCH", json=body)
        stamp = datetime.now().isoformat(timespec="seconds")
        for cand, item in zip(chunk, result):
            ok = bool(item.get("success"))
            rows.append(
                {
                    "pulled_at": stamp,
                    "site_id": cand.site_id,
                    "name": cand.name,
                    "rep_owner_id": cand.rep_owner_id,
                    "rep_name": cand.rep_name,
                    "state": cand.state,
                    "assigned_on": cand.assigned_on,
                    "status": "applied" if ok else "failed",
                    "error": ""
                    if ok
                    else "; ".join(
                        f"{e.get('statusCode')}: {e.get('message')}"
                        for e in item.get("errors") or []
                    ),
                }
            )
    return rows


def _write(path: Path, rows: Iterable[dict], *, append: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not append or not path.is_file()
    with path.open("w" if fresh else "a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(PULL_COLUMNS), extrasaction="ignore")
        if fresh:
            writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    load_dotenv(ROOT / ".env")
    from salesforce.sf_client import SalesforceClient

    apply = (os.environ.get("PULL_APPLY") or "").strip().lower() in {"1", "true", "yes"}
    assigned_on = (os.environ.get("PULL_ASSIGNED_ON") or "2026-09-22").strip()
    date.fromisoformat(assigned_on)  # reject anything that is not a plain date
    reps = {
        r.strip().lower() for r in (os.environ.get("PULL_REPS") or "").split(",") if r.strip()
    }
    limit_raw = (os.environ.get("PULL_LIMIT") or "").strip()
    pool_owner_id = site_acq_owner_id()

    sf = SalesforceClient().sf
    records = sf.query_all(build_pull_query(assigned_on=assigned_on, pool_owner_id=pool_owner_id))[
        "records"
    ]
    touched = rep_touched_ids(
        sf, records, assigned_on=assigned_on, non_rep=non_rep_user_ids(sf)
    )
    ledger_path = swaps_dir() / PULL_LEDGER_CSV
    already = {r["site_id"] for r in read_pull_ledger(ledger_path) if r.get("status") == "applied"}
    candidates, kept = select_candidates(
        records,
        touched=touched,
        already_pulled=already,
        reps=reps or None,
        limit=int(limit_raw) if limit_raw else None,
    )

    print(
        f"matched {len(records)} not-worked ConnectX rooftops assigned {assigned_on}; "
        f"pull {len(candidates)}; kept with rep: {dict(kept) or 'none'}",
        flush=True,
    )
    by_rep = collections.Counter(c.rep_name for c in candidates)
    for rep, n in by_rep.most_common():
        print(f"  {rep:28} {n:5}", flush=True)

    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    plan_rows = [
        {**c.__dict__, "pulled_at": "", "status": "planned", "error": ""} for c in candidates
    ]
    plan_path = swaps_dir() / f"{stamp}_pull_plan.csv"
    _write(plan_path, plan_rows, append=False)
    print(f"plan → {plan_path}", flush=True)
    if not apply:
        print("dry run — set PULL_APPLY=1 to write", flush=True)
        return 0
    rows = apply_pull(sf, candidates, pool_owner_id=pool_owner_id)
    _write(ledger_path, rows, append=True)
    failed = sum(1 for r in rows if r["status"] == "failed")
    print(f"pulled {len(rows) - failed}, failed {failed} → {ledger_path}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
