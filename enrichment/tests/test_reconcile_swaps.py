"""Hot swap: losses, verified pool stock, matching, live checks, and the write."""

from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path

from enrichment.constants import SITE_ACQ_TEAM_OWNER_ID as POOL
from enrichment.reconcile_swaps import (
    Loss,
    Stock,
    apply_plan,
    collect_losses,
    collect_stock,
    filter_live,
    plan_swaps,
)

REP = "005REP000000001AAA"
RUN = Path("2026-10-08_010000_connectx_audit")


def _row(**kw) -> tuple[Path, bool, dict]:
    live = kw.pop("live", True)
    return RUN, live, kw


class CollectTests(unittest.TestCase):
    def test_losses_are_rep_sites_the_audit_unqualified(self):
        rows = [
            _row(Id="L1", OwnerId=REP, Owner__c="Jeremy Scott", Site_State__c="co",
                 audit_verdict="no_asset", sf_update_status="unqualified"),
            _row(Id="POOLNA", OwnerId=POOL, audit_verdict="no_asset",
                 sf_update_status="unqualified"),
            _row(Id="DRY", OwnerId=REP, audit_verdict="no_asset",
                 sf_update_status="unqualified", live=False),
            _row(Id="FAILED", OwnerId=REP, audit_verdict="no_asset", sf_update_status="failed"),
            _row(Id="OLD", Owner__c="Jeremy Scott", audit_verdict="no_asset",
                 sf_update_status="unqualified"),  # no OwnerId recorded
        ]
        self.assertEqual(
            collect_losses(rows, POOL),
            [Loss("L1", REP, "Jeremy Scott", "CO")],
        )

    def test_stock_is_pool_rooftops_confirmed_and_not_handed_out(self):
        rows = [
            _row(Id="S1", OwnerId=POOL, Site_State__c="NY", audit_verdict="confirmed",
                 update_site_type="Rooftop", sf_update_status="updated"),
            _row(Id="S2", Owner__c="Site Acquisition Team", Site_State__c="IL",
                 audit_verdict="confirmed", update_site_type="Rooftop",
                 sf_update_status="updated"),  # older run without OwnerId
            _row(Id="TOWER", OwnerId=POOL, audit_verdict="confirmed",
                 update_site_type="Monopole", sf_update_status="updated"),
            _row(Id="GIVEN", OwnerId=POOL, audit_verdict="confirmed", update_site_type="Rooftop",
                 sf_update_status="updated", update_owner_id=REP),
            _row(Id="REPS", OwnerId=REP, audit_verdict="confirmed", update_site_type="Rooftop",
                 sf_update_status="updated"),
        ]
        self.assertEqual(collect_stock(rows, POOL), [Stock("S1", "NY"), Stock("S2", "IL")])


class PlanTests(unittest.TestCase):
    def test_same_state_first_then_any_then_waiting(self):
        losses = [Loss("L1", REP, "A", "CO"), Loss("L2", REP, "A", "TX"), Loss("L3", REP, "A", "CO")]
        stock = [Stock("S_NY", "NY"), Stock("S_CO", "CO")]
        plan = plan_swaps(losses, stock)
        self.assertEqual(
            [(r["lost_id"], r["replacement_id"], r["match"], r["status"]) for r in plan],
            [
                ("L1", "S_CO", "same_state", "planned"),
                ("L2", "S_NY", "any_state", "planned"),
                ("L3", "", "no_stock", "waiting"),
            ],
        )

    def test_ledger_and_limit(self):
        losses = [Loss("L1", REP, "A", "CO"), Loss("L2", REP, "A", "CO")]
        stock = [Stock("S1", "CO"), Stock("S2", "CO")]
        plan = plan_swaps(losses, stock, used_losses={"L1"}, used_stock={"S1"})
        self.assertEqual([(r["lost_id"], r["replacement_id"]) for r in plan], [("L2", "S2")])
        self.assertEqual(len(plan_swaps(losses, stock, limit=1)), 1)


class _FakeSF:
    def __init__(self, sites: dict, active: set[str]):
        self.sites = sites
        self.active = active
        self.patches: list[dict] = []

    def query_all(self, soql):
        ids = [p.strip(" '") for p in soql.split("IN (")[1].rstrip(")").split(",")]
        if "FROM User" in soql:
            return {"records": [{"Id": i} for i in ids if i in self.active]}
        return {"records": [dict(self.sites[i], Id=i) for i in ids if i in self.sites]}

    def restful(self, path, method, json):
        self.patches.append(json)
        return [{"success": True} for _ in json["records"]]


class LiveCheckTests(unittest.TestCase):
    def test_filter_live_drops_stale_losses_and_stock(self):
        sf = _FakeSF(
            {
                "L_OK": {"OwnerId": "0056O00000EpUOgQAN", "Stage__c": "Unqualified"},
                "L_BACK": {"OwnerId": REP, "Stage__c": "Unqualified"},
                "L_REOPENED": {"OwnerId": POOL, "Stage__c": "New/Unreviewed"},
                "S_OK": {"OwnerId": POOL, "Stage__c": "New/Unreviewed", "Site_Type__c": "Rooftop"},
                "S_TAKEN": {"OwnerId": REP, "Stage__c": "New/Unreviewed", "Site_Type__c": "Rooftop"},
                "S_WORKED": {"OwnerId": POOL, "Stage__c": "Outreach", "Site_Type__c": "Rooftop"},
            },
            active={REP},
        )
        losses = [Loss(i, REP, "A", "CO") for i in ("L_OK", "L_BACK", "L_REOPENED")]
        losses.append(Loss("L_INACTIVE", "005GONE", "B", "CO"))
        sf.sites["L_INACTIVE"] = {"OwnerId": POOL, "Stage__c": "Unqualified"}
        stock = [Stock(i, "CO") for i in ("S_OK", "S_TAKEN", "S_WORKED")]
        kept_losses, kept_stock, notes = filter_live(sf, losses, stock, POOL)
        self.assertEqual([l.lost_id for l in kept_losses], ["L_OK"])
        self.assertEqual([s.site_id for s in kept_stock], ["S_OK"])
        self.assertEqual(len(notes), 5)

    def test_apply_writes_owner_and_assignment_date(self):
        sf = _FakeSF({}, set())
        plan = plan_swaps([Loss("L1", REP, "A", "CO")], [Stock("S1", "CO")])
        apply_plan(sf, plan, today=date(2026, 10, 8))
        (body,) = sf.patches
        self.assertEqual(
            body["records"],
            [{"attributes": {"type": "Site__c"}, "id": "S1", "OwnerId": REP,
              "Site_Assignment_Date__c": "2026-10-08"}],
        )
        self.assertEqual(plan[0]["status"], "applied")


if __name__ == "__main__":
    unittest.main()
