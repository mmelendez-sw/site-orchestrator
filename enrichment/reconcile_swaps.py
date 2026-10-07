"""Refill rep books with verified pool rooftops, against what each rep is owed.

python -m enrichment.reconcile_swaps

**Owed** (one slot per site a rep lost):

- every site ``enrichment.reconcile_pull`` moved to the pool
  (``swaps/pull_ledger.csv``, status applied), and
- every rep-owned site a live ConnectX audit unqualified (rep-books mode).

**Stock**: pool sites a live audit confirmed as Rooftop (``audit_verdict=
confirmed``, ``update_site_type=Rooftop``, written, owned by the pool when
audited, not already handed out with ``AUDIT_ASSIGN_TO``). A pulled site
that confirms is stock too, and goes back to its own rep first.

Both lists are re-checked live in Salesforce: a lost site must still be away
from its rep (audit losses must also still be Unqualified), the rep must be
active, and stock must still be a pool-owned New/Unreviewed Rooftop. Slots
fill round-robin across reps (so a shortfall is shared), each taking its own
site if confirmed, else the same Site_State__c, else any state. A fill sets
``OwnerId`` = rep and ``Site_Assignment_Date__c`` = today.

``SWAP_APPLY=1`` writes; anything else only plans. Each run writes
``swaps/<stamp>_swap_plan.csv`` and prints owed / filled / planned / open per
rep; applied fills append to ``swaps/swap_ledger.csv`` so no slot or stock
site is used twice. Optional: ``SWAP_LIMIT`` (max fills), ``SWAP_REPS``.
``SWAP_BRIDGE=1`` adds bridge stock: pool-owned, LLM_Classified non-ConnectX
Rooftops in New/Unreviewed, Enhanced/Unreviewed, Outreach, or Outreach -
Verified (stage kept). ConnectX audit stock is still preferred.
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
from enrichment.reconcile_pull import PULL_LEDGER_CSV, read_pull_ledger  # noqa: E402
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
    "source",
    "lost_id",
    "rep_owner_id",
    "rep_name",
    "lost_state",
    "replacement_id",
    "replacement_state",
    "stock_source",
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
    source: str = "audit"  # audit (unqualified) | pull (moved to pool)


@dataclass(frozen=True)
class Stock:
    site_id: str
    state: str
    source: str = "audit"  # audit (ConnectX confirmed) | bridge (non-ConnectX rooftop)


# Bridge stock (SWAP_BRIDGE=1): pool-owned, LLM_Classified non-ConnectX rooftops
# in these stages, unworked first. Already verified by earlier enrichment runs.
BRIDGE_STAGES = ("New/Unreviewed", "Enhanced/Unreviewed", "Outreach", "Outreach - Verified")


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


def losses_from_pull_ledger(rows: Iterable[dict[str, str]]) -> list[Loss]:
    """One owed slot per site the pull moved to the pool."""
    out: dict[str, Loss] = {}
    for row in rows:
        if row.get("status") != "applied":
            continue
        out[row["site_id"]] = Loss(
            lost_id=row["site_id"],
            rep_owner_id=row["rep_owner_id"],
            rep_name=row.get("rep_name") or "",
            state=(row.get("state") or "").strip().upper(),
            source="pull",
        )
    return list(out.values())


def _round_robin(losses: Sequence[Loss]) -> list[Loss]:
    """Interleave reps (largest owed first) so a shortfall is shared evenly."""
    by_rep: dict[str, list[Loss]] = {}
    for loss in losses:
        by_rep.setdefault(loss.rep_owner_id, []).append(loss)
    queues = sorted(by_rep.values(), key=len, reverse=True)
    out: list[Loss] = []
    while any(queues):
        for queue in queues:
            if queue:
                out.append(queue.pop(0))
    return out


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
    """One replacement per owed slot, round-robin across reps.

    A rep's own pulled site comes back first when it confirmed; otherwise the
    same state, else any remaining stock. Sites that are themselves owed are
    reserved for their own rep.
    """
    # ConnectX-confirmed (audit) stock before bridge stock; stable otherwise.
    available = sorted(
        (s for s in stock if s.site_id not in used_stock),
        key=lambda s: s.source == "bridge",
    )
    open_losses = [l for l in losses if l.lost_id not in used_losses]
    owed_ids = {l.lost_id[:15] for l in open_losses}
    plan: list[dict[str, str]] = []
    for loss in _round_robin(open_losses):
        if limit is not None and sum(r["status"] == "planned" for r in plan) >= limit:
            break
        pick = next((s for s in available if s.site_id[:15] == loss.lost_id[:15]), None)
        match = "own_site"
        if pick is None:
            others = [s for s in available if s.site_id[:15] not in owed_ids]
            pick = next((s for s in others if loss.state and s.state == loss.state), None)
            match = "same_state"
            if pick is None and others:
                pick, match = others[0], "any_state"
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
        "source": loss.source,
        "replacement_id": pick.site_id if pick else "",
        "replacement_state": pick.state if pick else "",
        "stock_source": pick.source if pick else "",
        "match": match,
        "status": "planned" if pick else "waiting",
        "error": "",
    }


def per_rep_summary(
    owed: Sequence[Loss], ledger: Sequence[dict], plan: Sequence[dict]
) -> dict[str, dict[str, int]]:
    """owed / filled (ledger) / planned (this run) / open, per rep name."""
    names = {l.rep_owner_id[:15]: l.rep_name for l in owed}
    out: dict[str, dict[str, int]] = {}

    def bucket(owner_id: str) -> dict[str, int]:
        name = names.get(owner_id[:15]) or owner_id
        return out.setdefault(name, {"owed": 0, "filled": 0, "planned": 0, "open": 0})

    for loss in {l.lost_id: l for l in owed}.values():
        bucket(loss.rep_owner_id)["owed"] += 1
    for row in ledger:
        bucket(row["rep_owner_id"])["filled"] += 1
    for row in plan:
        if row["status"] == "planned":
            bucket(row["rep_owner_id"])["planned"] += 1
    for c in out.values():
        c["open"] = c["owed"] - c["filled"] - c["planned"]
    return out


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


def query_bridge_stock(sf, pool_owner_id: str) -> list[Stock]:
    """Pool-owned, LLM_Classified non-ConnectX Rooftops in BRIDGE_STAGES."""
    stages = ",".join(f"'{s}'" for s in BRIDGE_STAGES)
    recs = sf.query_all(
        "SELECT Id, Site_State__c, Stage__c FROM Site__c "
        f"WHERE OwnerId = '{pool_owner_id}' AND LLM_Classified__c = true "
        "AND Site_Type__c = 'Rooftop' "
        "AND (Carrier_Leasing_Source__c = null OR (NOT Carrier_Leasing_Source__c LIKE '%ConnectX%')) "
        f"AND Stage__c IN ({stages}) ORDER BY Id"
    )["records"]
    rank = {stage: i for i, stage in enumerate(BRIDGE_STAGES)}
    recs.sort(key=lambda r: rank.get(r.get("Stage__c"), len(rank)))
    return [
        Stock(site_id=r["Id"], state=str(r.get("Site_State__c") or "").strip().upper(),
              source="bridge")
        for r in recs
    ]


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
        if rec is None:
            notes.append(f"loss {loss.lost_id}: not found — skipped")
        elif loss.source == "audit" and rec.get("Stage__c") != "Unqualified":
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
        stages = BRIDGE_STAGES if item.source == "bridge" else (REPLACEMENT_STAGE,)
        if (
            rec is not None
            and str(rec.get("OwnerId") or "")[:15] == pool_owner_id[:15]
            and rec.get("Stage__c") in stages
            and rec.get("Site_Type__c") == "Rooftop"
        ):
            kept_stock.append(item)
        else:
            notes.append(f"stock {item.site_id}: no longer an eligible pool Rooftop")
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
    losses = losses_from_pull_ledger(read_pull_ledger(swaps_dir() / PULL_LEDGER_CSV))
    pulled = {l.lost_id for l in losses}
    losses += [l for l in collect_losses(rows, pool_owner_id) if l.lost_id not in pulled]
    if reps:
        losses = [l for l in losses if l.rep_name.lower() in reps]
    stock = collect_stock(rows, pool_owner_id)
    ledger_path = swaps_dir() / LEDGER_CSV
    bridge = (os.environ.get("SWAP_BRIDGE") or "").strip().lower() in {"1", "true", "yes"}
    ledger = [r for r in read_ledger(ledger_path) if r.get("status") == "applied"]
    used_losses = {r["lost_id"] for r in ledger}
    used_stock = {r["replacement_id"] for r in ledger}
    print(
        f"owed slots: {len(losses)} (already filled {len(used_losses & {l.lost_id for l in losses})}) | "
        f"verified pool rooftops: {len(stock)} (already given {len(used_stock & {s.site_id for s in stock})})",
        flush=True,
    )

    sf = SalesforceClient().sf
    if bridge:
        audit_ids = {s.site_id[:15] for s in stock}
        extra = [b for b in query_bridge_stock(sf, pool_owner_id) if b.site_id[:15] not in audit_ids]
        print(f"bridge stock (non-ConnectX rooftops): {len(extra)}", flush=True)
        stock = stock + extra  # audit stock first: matching prefers it
    losses_all = list(losses)
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
    summary = per_rep_summary(losses_all, ledger, plan)
    print(f"  {'rep':26} {'owed':>5} {'filled':>6} {'planned':>7} {'open':>5}", flush=True)
    for name, c in sorted(summary.items(), key=lambda kv: -kv[1]["owed"]):
        print(
            f"  {name:26} {c['owed']:5} {c['filled']:6} {c['planned']:7} {c['open']:5}",
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
