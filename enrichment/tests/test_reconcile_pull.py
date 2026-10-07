"""Pull not-worked ConnectX rooftops to the pool, and refill from the owed ledger."""

from __future__ import annotations

import unittest

from enrichment.constants import SITE_ACQ_TEAM_OWNER_ID as POOL
from enrichment.reconcile_pull import (
    PullCandidate,
    apply_pull,
    build_pull_query,
    rep_touched_ids,
    select_candidates,
)
from enrichment.reconcile_swaps import (
    Loss,
    Stock,
    filter_live,
    losses_from_pull_ledger,
    per_rep_summary,
    plan_swaps,
)

MATT = "0056O00000EpUOgQAN"
AUTO = "005Uq00000IiMk1IAF"
JEREMY = "005JEREMY0000001AA"
DARREN = "005DARREN0000001AA"


def _rec(sf_id, owner=JEREMY, name="Jeremy Scott", state="co", modified_by=MATT):
    return {"Id": sf_id, "Name": f"site {sf_id}", "OwnerId": owner, "Owner__c": name,
            "Site_State__c": state, "Site_Assignment_Date__c": "2026-09-22",
            "LastModifiedById": modified_by}


class _HistorySF:
    def __init__(self, history):
        self.history = history
        self.soql: list[str] = []
        self.patches: list[dict] = []

    def query_all(self, soql):
        self.soql.append(soql)
        return {"records": self.history}

    def restful(self, path, method, json):
        self.patches.append(json)
        return [{"success": r["id"] != "FAIL", "errors": [] if r["id"] != "FAIL"
                 else [{"statusCode": "X", "message": "nope"}]} for r in json["records"]]


class PullTests(unittest.TestCase):
    def test_query_targets_not_worked_rep_connectx_rooftops(self):
        soql = build_pull_query(assigned_on="2026-09-22", pool_owner_id=POOL)
        for clause in (
            "Site_Assignment_Date__c = 2026-09-22",
            "Carrier_Leasing_Source__c LIKE '%ConnectX%'",
            "LLM_Classified__c = true",
            "Site_Type__c = 'Rooftop'",
            "Stage__c = 'New/Unreviewed'",
            f"OwnerId != '{POOL}'",
            "Owner.Type = 'User'",
        ):
            self.assertIn(clause, soql)

    def test_rep_edits_keep_the_site_with_the_rep(self):
        recs = [_rec("A"), _rec("B", modified_by=JEREMY), _rec("C", modified_by=AUTO)]
        sf = _HistorySF([{"ParentId": "C", "CreatedById": JEREMY},
                         {"ParentId": "A", "CreatedById": AUTO}])
        touched = rep_touched_ids(sf, recs, assigned_on="2026-09-22",
                                  non_rep={MATT[:15], AUTO[:15]})
        self.assertEqual(touched, {"B", "C"})
        self.assertIn("CreatedDate > 2026-09-22T23:59:59-05:00", sf.soql[0])

    def test_select_skips_touched_pulled_other_reps_and_limit(self):
        recs = [_rec("A"), _rec("B"), _rec("C"), _rec("D", owner=DARREN, name="Darren Katz"),
                _rec("E")]
        out, kept = select_candidates(
            recs, touched={"B"}, already_pulled={"C"}, reps={"jeremy scott"}, limit=1
        )
        self.assertEqual([c.site_id for c in out], ["A"])
        self.assertEqual(out[0].state, "CO")
        self.assertEqual(
            dict(kept), {"rep_touched": 1, "already_pulled": 1, "other_rep": 1, "over_limit": 1}
        )

    def test_apply_moves_to_pool_and_records_failures(self):
        sf = _HistorySF([])
        cands = [PullCandidate("A", "a", JEREMY, "Jeremy Scott", "CO", "2026-09-22"),
                 PullCandidate("FAIL", "f", JEREMY, "Jeremy Scott", "CO", "2026-09-22")]
        rows = apply_pull(sf, cands, pool_owner_id=POOL)
        self.assertEqual(sf.patches[0]["records"][0],
                         {"attributes": {"type": "Site__c"}, "id": "A", "OwnerId": POOL})
        self.assertEqual([r["status"] for r in rows], ["applied", "failed"])
        self.assertEqual(rows[1]["error"], "X: nope")


class FillFromLedgerTests(unittest.TestCase):
    def _ledger(self):
        return [
            {"site_id": "J1", "rep_owner_id": JEREMY, "rep_name": "Jeremy Scott",
             "state": "CO", "status": "applied"},
            {"site_id": "J2", "rep_owner_id": JEREMY, "rep_name": "Jeremy Scott",
             "state": "CO", "status": "applied"},
            {"site_id": "J3", "rep_owner_id": JEREMY, "rep_name": "Jeremy Scott",
             "state": "NY", "status": "applied"},
            {"site_id": "D1", "rep_owner_id": DARREN, "rep_name": "Darren Katz",
             "state": "IL", "status": "applied"},
            {"site_id": "X", "rep_owner_id": DARREN, "rep_name": "Darren Katz",
             "state": "IL", "status": "failed"},
        ]

    def test_own_site_first_reserved_and_round_robin(self):
        losses = losses_from_pull_ledger(self._ledger())
        self.assertEqual([l.lost_id for l in losses], ["J1", "J2", "J3", "D1"])
        # J2 confirmed (own site), D1 confirmed, plus two pool rooftops.
        stock = [Stock("D1", "IL"), Stock("J2", "CO"), Stock("P_CO", "CO"), Stock("P_TX", "TX")]
        plan = plan_swaps(losses, stock)
        got = [(r["lost_id"], r["replacement_id"], r["match"]) for r in plan]
        # Round robin: J1, D1, J2, J3. J1 must not take J2 (reserved for its owner).
        self.assertEqual(
            got,
            [
                ("J1", "P_CO", "same_state"),
                ("D1", "D1", "own_site"),
                ("J2", "J2", "own_site"),
                ("J3", "P_TX", "any_state"),
            ],
        )

    def test_shortfall_is_shared_across_reps(self):
        losses = losses_from_pull_ledger(self._ledger())
        plan = plan_swaps(losses, [Stock("P1", "TX"), Stock("P2", "TX")])
        filled = {r["rep_name"] for r in plan if r["status"] == "planned"}
        self.assertEqual(filled, {"Jeremy Scott", "Darren Katz"})

    def test_pulled_loss_live_check_does_not_require_unqualified(self):
        class SF:
            def query_all(self, soql):
                if "FROM User" in soql:
                    return {"records": [{"Id": JEREMY}]}
                return {"records": [
                    {"Id": "J1", "OwnerId": POOL, "Stage__c": "New/Unreviewed"},
                    {"Id": "J2", "OwnerId": JEREMY, "Stage__c": "New/Unreviewed"},
                ]}

        losses = [Loss("J1", JEREMY, "Jeremy Scott", "CO", "pull"),
                  Loss("J2", JEREMY, "Jeremy Scott", "CO", "pull")]
        kept, _stock, notes = filter_live(SF(), losses, [], POOL)
        self.assertEqual([l.lost_id for l in kept], ["J1"])
        self.assertIn("back with the rep", notes[0])

    def test_per_rep_summary(self):
        owed = losses_from_pull_ledger(self._ledger())
        ledger = [{"rep_owner_id": JEREMY, "lost_id": "J1"}]
        plan = [{"rep_owner_id": JEREMY, "status": "planned"},
                {"rep_owner_id": DARREN, "status": "waiting"}]
        summary = per_rep_summary(owed, ledger, plan)
        self.assertEqual(summary["Jeremy Scott"],
                         {"owed": 3, "filled": 1, "planned": 1, "open": 1})
        self.assertEqual(summary["Darren Katz"],
                         {"owed": 1, "filled": 0, "planned": 0, "open": 1})


if __name__ == "__main__":
    unittest.main()
