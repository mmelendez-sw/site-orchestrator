"""Hot swap: replace each rep site the ConnectX audit unqualified with a verified pool rooftop.

python -m enrichment.reconcile_swaps

Reads every live ConnectX audit run (``runs/*_connectx_audit``):

- **Losses**: rep-owned sites the audit unqualified (``audit_verdict=no_asset``,
  ``sf_update_status=unqualified``, original ``OwnerId`` not the pool).
- **Stock**: pool sites the audit confirmed as Rooftop (``audit_verdict=confirmed``,
  ``update_site_type=Rooftop``, written, original owner the pool, not already
  handed to someone with ``AUDIT_ASSIGN_TO``).

Both lists are re-checked live in Salesforce before matching: the loss must
still be Unqualified and away from the rep, the replacement must still be a
pool-owned, New/Unreviewed Rooftop, and the rep must be an active user.
Each loss gets one replacement, same Site_State__c first, else any state.
A replacement write sets ``OwnerId`` = rep and ``Site_Assignment_Date__c`` =
today.

``SWAP_APPLY=1`` writes; anything else only plans. Every run writes its plan
to ``swaps/<stamp>_swap_plan.csv``; applied swaps append to
``swaps/swap_ledger.csv``, which keeps a loss or a replacement from being
used twice. Optional: ``SWAP_LIMIT`` (max swaps), ``SWAP_REPS`` (comma list
of rep names to serve).
"""

from __future__ import annotations

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

from enrichment.coerce import lower_text  # noqa: E402
from enrichment.connectx_audit import (  # noqa: E402
    VERDICT_CONFIRMED,
    VERDICT_NO_ASSET,
    iter_audit_rows,
    site_acq_owner_id,
)

POOL_OWNER_NAME = "Site Acquisition Team"
REPLACEMENT_STAGE = "New/Unreviewed"
LEDGER_CSV = "swap_ledger.csv"
LEDGER_COLUMNS = (
    "swapped_at",
    "lost_id",
    "rep_owner_id",
    "rep_name",
    "lost_state",
    "replacement_id",
    "replacement_state",
    "match",
    "status",
    "error",
)
PLAN_COLUMNS = LEDGER_COLUMNS


@dataclass(frozen=True)
class Loss:
    lost_id: str
    rep_owner_id: str
    rep_name: str
    state: str


@dataclass(frozen=True)
class Stock:
    site_id: str
    state: str


def swaps_dir() -> Path:
    from paths import data_root

    return data_root() / "swaps"


def _is_pool_row(row: dict[str, Any], pool_owner_id: str) -> bool:
    owner_id = str(row.get("OwnerId") or "").strip()
    if owner_id:
        return owner_id[:15] == pool_owner_id[:15]
    # Runs before OwnerId was recorded only carry the formula name.
    return str(row.get("Owner__c") or "").strip() == POOL_OWNER_NAME


def collect_losses(rows: Iterable[tuple[Path, bool, dict]], pool_owner_id: str) -> list[Loss]:
    """Rep sites a live audit unqualified (needs the original OwnerId)."""
    out: dict[str, Loss] = {}
    for _run, live, row in rows:
        if not live:
            continue
        if lower_text(row.get("audit_verdict")) != VERDICT_NO_ASSET:
            continue
        if lower_text(row.get("sf_update_status")) != "unqualified":
            continue
        owner_id = str(row.get("OwnerId") or "").strip()
        if not owner_id or _is_pool_row(row, pool_owner_id):
            continue
        sf_id = str(row.get("Id") or "").strip()
        out[sf_id] = Loss(
            lost_id=sf_id,
            rep_owner_id=owner_id,
            rep_name=str(row.get("Owner__c") or "").strip(),
            state=str(row.get("Site_State__c") or "").strip().upper(),
        )
    return list(out.values())


def collect_stock(rows: Iterable[tuple[Path, bool, dict]], pool_owner_id: str) -> list[Stock]:
    """Pool sites a live audit confirmed as Rooftop and left with the pool."""
    out: dict[str, Stock] = {}
    for _run, live, row in rows:
        if not live:
            continue
        if lower_text(row.get("audit_verdict")) != VERDICT_CONFIRMED:
            continue
        if str(row.get("update_site_type") or "").strip() != "Rooftop":
            continue
        if lower_text(row.get("sf_update_status")) != "updated":
            continue
        if str(row.get("update_owner_id") or "").strip():
            continue  # already handed out by AUDIT_ASSIGN_TO
        if not _is_pool_row(row, pool_owner_id):
            continue
        sf_id = str(row.get("Id") or "").strip()
        out[sf_id] = Stock(
            site_id=sf_id, state=str(row.get("Site_State__c") or "").strip().upper()
        )
    return list(out.values())


def read_ledger(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def plan_swaps(
    losses: Sequence[Loss],
    stock: Sequence[Stock],
    *,
    used_losses: set[str] = frozenset(),
    used_stock: set[str] = frozenset(),
    limit: int | None = None,
) -> list[dict[str, str]]:
    """One replacement per loss: same state first, else any remaining stock."""
    available = [s for s in stock if s.site_id not in used_stock]
    plan: list[dict[str, str]] = []
    for loss in losses:
        if loss.lost_id in used_losses:
            continue
        if limit is not None and len(plan) >= limit:
            break
        pick = next((s for s in available if loss.state and s.state == loss.state), None)
        match = "same_state"
        if pick is None and available:
            pick, match = available[0], "any_state"
        if pick is None:
            plan.append(_plan_row(loss, None, "no_stock"))
            continue
        available.remove(pick)
        plan.append(_plan_row(loss, pick, match))
    return plan


def _plan_row(loss: Loss, pick: Stock | None, match: str) -> dict[str, str]:
    return {
        "swapped_at": "",
        "lost_id": loss.lost_id,
        "rep_owner_id": loss.rep_owner_id,
        "rep_name": loss.rep_name,
        "lost_state": loss.state,
        "replacement_id": pick.site_id if pick else "",
        "replacement_state": pick.state if pick else "",
        "match": match,
        "status": "planned" if pick else "waiting",
        "error": "",
    }


def _chunks(items: Sequence[str], size: int = 200):
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _query_by_ids(sf, soql_head: str, ids: Sequence[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for chunk in _chunks(list(ids)):
        quoted = ",".join(f"'{i}'" for i in chunk)
        for rec in sf.query_all(f"{soql_head} WHERE Id IN ({quoted})")["records"]:
            out[rec["Id"][:15]] = rec
    return out


def filter_live(
    sf,
    losses: list[Loss],
    stock: list[Stock],
    pool_owner_id: str,
) -> tuple[list[Loss], list[Stock], list[str]]:
    """Drop losses / stock whose live Salesforce state no longer fits."""
    notes: list[str] = []
    live_loss = _query_by_ids(
        sf, "SELECT Id, OwnerId, Stage__c FROM Site__c", [l.lost_id for l in losses]
    )
    live_stock = _query_by_ids(
        sf,
        "SELECT Id, OwnerId, Stage__c, Site_Type__c FROM Site__c",
        [s.site_id for s in stock],
    )
    rep_ids = sorted({l.rep_owner_id for l in losses})
    active = {
        rec["Id"][:15]
        for chunk in _chunks(rep_ids)
        for rec in sf.query_all(
            "SELECT Id FROM User WHERE IsActive = true AND Id IN ("
            + ",".join(f"'{i}'" for i in chunk)
            + ")"
        )["records"]
    }
    kept_losses = []
    for loss in losses:
        rec = live_loss.get(loss.lost_id[:15])
        if rec is None or rec.get("Stage__c") != "Unqualified":
            notes.append(f"loss {loss.lost_id}: no longer Unqualified — skipped")
        elif str(rec.get("OwnerId") or "")[:15] == loss.rep_owner_id[:15]:
            notes.append(f"loss {loss.lost_id}: back with the rep — skipped")
        elif loss.rep_owner_id[:15] not in active:
            notes.append(f"loss {loss.lost_id}: rep {loss.rep_name} inactive — skipped")
        else:
            kept_losses.append(loss)
    kept_stock = []
    for item in stock:
        rec = live_stock.get(item.site_id[:15])
        if (
            rec is not None
            and str(rec.get("OwnerId") or "")[:15] == pool_owner_id[:15]
            and rec.get("Stage__c") == REPLACEMENT_STAGE
            and rec.get("Site_Type__c") == "Rooftop"
        ):
            kept_stock.append(item)
        else:
            notes.append(f"stock {item.site_id}: no longer a pool New/Unreviewed Rooftop")
    return kept_losses, kept_stock, notes


def apply_plan(sf, plan: list[dict[str, str]], *, today: date) -> None:
    """Write OwnerId + Site_Assignment_Date__c for every planned swap (in place)."""
    todo = [row for row in plan if row["status"] == "planned"]
    for chunk in _chunks(todo):
        body = {
            "allOrNone": False,
            "records": [
                {
                    "attributes": {"type": "Site__c"},
                    "id": row["replacement_id"],
                    "OwnerId": row["rep_owner_id"],
                    "Site_Assignment_Date__c": today.isoformat(),
                }
                for row in chunk
            ],
        }
        result = sf.restful("composite/sobjects", method="PATCH", json=body)
        stamp = datetime.now().isoformat(timespec="seconds")
        for row, item in zip(chunk, result):
            row["swapped_at"] = stamp
            if item.get("success"):
                row["status"] = "applied"
            else:
                row["status"] = "failed"
                row["error"] = "; ".join(
                    f"{e.get('statusCode')}: {e.get('message')}" for e in item.get("errors") or []
                )


def _write(path: Path, rows: Iterable[dict], *, append: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not append or not path.is_file()
    with path.open("w" if fresh else "a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(LEDGER_COLUMNS), extrasaction="ignore")
        if fresh:
            writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    load_dotenv(ROOT / ".env")
    from paths import runs_dir
    from salesforce.sf_client import SalesforceClient

    apply = (os.environ.get("SWAP_APPLY") or "").strip().lower() in {"1", "true", "yes"}
    limit_raw = (os.environ.get("SWAP_LIMIT") or "").strip()
    reps = {
        r.strip().lower() for r in (os.environ.get("SWAP_REPS") or "").split(",") if r.strip()
    }
    pool_owner_id = site_acq_owner_id()

    rows = list(iter_audit_rows(runs_dir()))
    losses = collect_losses(rows, pool_owner_id)
    if reps:
        losses = [l for l in losses if l.rep_name.lower() in reps]
    stock = collect_stock(rows, pool_owner_id)
    ledger_path = swaps_dir() / LEDGER_CSV
    ledger = [r for r in read_ledger(ledger_path) if r.get("status") == "applied"]
    used_losses = {r["lost_id"] for r in ledger}
    used_stock = {r["replacement_id"] for r in ledger}
    print(
        f"audit losses: {len(losses)} (already swapped {len(used_losses & {l.lost_id for l in losses})}) | "
        f"verified pool rooftops: {len(stock)} (already given {len(used_stock & {s.site_id for s in stock})})",
        flush=True,
    )

    sf = SalesforceClient().sf
    losses = [l for l in losses if l.lost_id not in used_losses]
    stock = [s for s in stock if s.site_id not in used_stock]
    losses, stock, notes = filter_live(sf, losses, stock, pool_owner_id)
    for note in notes:
        print(f"  {note}", flush=True)
    plan = plan_swaps(losses, stock, limit=int(limit_raw) if limit_raw else None)

    planned = [r for r in plan if r["status"] == "planned"]
    print(
        f"plan: {len(planned)} swap(s), {len(plan) - len(planned)} rep site(s) waiting for stock",
        flush=True,
    )
    for row in plan:
        print(
            f"  {row['rep_name']:24} lost {row['lost_id']} ({row['lost_state'] or '?'}) → "
            f"{row['replacement_id'] or '—'} ({row['replacement_state'] or '-'}) [{row['match']}]",
            flush=True,
        )
    if apply and planned:
        apply_plan(sf, plan, today=date.today())
        _write(ledger_path, [r for r in plan if r["status"] in {"applied", "failed"}], append=True)
        failed = sum(1 for r in plan if r["status"] == "failed")
        print(f"applied {len(planned) - failed}, failed {failed} → {ledger_path}", flush=True)
    elif not apply:
        print("dry run — set SWAP_APPLY=1 to write", flush=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    plan_path = swaps_dir() / f"{stamp}_swap_plan.csv"
    _write(plan_path, plan)
    print(f"plan → {plan_path}", flush=True)
    return 1 if any(r["status"] == "failed" for r in plan) else 0


if __name__ == "__main__":
    raise SystemExit(main())
