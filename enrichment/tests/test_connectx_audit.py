"""CONNECTX_AUDIT: verdict rules, Salesforce payload, skip list, and an offline run."""

from __future__ import annotations

import csv
import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from enrichment.connectx_audit import (
    AUDIT_DRY_RUN_SUFFIX,
    AUDIT_RUN_SUFFIX,
    audit_verdict,
    build_connectx_audit_query,
    prior_audit_ids,
    stamp_audit_verdict,
)
from enrichment.constants import DETAIL_CSV, SITE_ACQ_TEAM_OWNER_ID
from enrichment.metrics import outcome_class
from enrichment.outputs import DETAIL_COLUMNS, write_csv
from enrichment.sf_ops import build_row_payload, status_from_entry


def _nearmap_row(**overrides) -> dict:
    """A finished row: Nearmap obliques reviewed, roof present, no gear."""
    row = {
        "Id": "a0Z000000000001",
        "bucket": "potential_rooftop",
        "holdout_reason": "rooftop_no_cell_equipment",
        "match_source": "none",
        "match_distance_m": "",
        "naip_site_type": "rooftop",
        "naip_site_confidence": 0.9,
        "naip_cell_equipment": False,
        "cell_equipment_confidence": 0.9,
        "cell_gear_kind": "none",
        "gemini_cell_equipment": False,
        "claude_cell_equipment": "",
        "naip_screen_cell_equipment": False,
        "nearmap_tier": "full",
        "nearmap_views": "Vert,North,East",
        "cell_equipment_evidence": "Flat roof with HVAC units only.",
        "site_evidence": "Two-story commercial building.",
        "error": "",
    }
    row.update(overrides)
    return row


class AuditVerdictTests(unittest.TestCase):
    def test_nearmap_roof_without_gear_is_no_asset(self):
        self.assertEqual(audit_verdict(_nearmap_row()), ("no_asset", "nearmap_roof_no_gear"))

    def test_nearmap_empty_other_is_no_asset(self):
        row = _nearmap_row(naip_site_type="other", naip_cell_equipment="")
        self.assertEqual(audit_verdict(row), ("no_asset", "nearmap_empty"))

    def test_written_candidate_is_confirmed(self):
        self.assertEqual(
            audit_verdict(_nearmap_row(bucket="potential_update", holdout_reason="")),
            ("confirmed", "nearmap_cell_confirmed"),
        )
        self.assertEqual(
            audit_verdict(
                _nearmap_row(bucket="potential_update", holdout_reason="skip_classify_db_hit")
            ),
            ("confirmed", "db_hit"),
        )

    def test_naip_only_never_unqualifies(self):
        row = _nearmap_row(nearmap_tier="naip_only", nearmap_views="")
        self.assertEqual(audit_verdict(row), ("inconclusive", "no_nearmap_obliques"))
        row = _nearmap_row(nearmap_tier="vert_only", nearmap_views="Vert")
        self.assertEqual(audit_verdict(row), ("inconclusive", "no_nearmap_obliques"))

    def test_any_model_seeing_gear_blocks_unqualify(self):
        for key in ("naip_cell_equipment", "gemini_cell_equipment",
                    "claude_cell_equipment", "naip_screen_cell_equipment"):
            with self.subTest(key=key):
                self.assertEqual(
                    audit_verdict(_nearmap_row(**{key: "True"})),
                    ("inconclusive", "gear_seen_unconfirmed"),
                )

    def test_blockers_keep_the_site_with_its_rep(self):
        cases = {
            "nearmap_budget": _nearmap_row(holdout_reason="nearmap_budget"),
            "no_nearmap_coverage": _nearmap_row(nearmap_tier="no_coverage"),
            "db_record_nearby": _nearmap_row(match_source="FCC", match_distance_m="60"),
            "gear_kind_named": _nearmap_row(cell_gear_kind="sector_panel"),
            "hedged_evidence": _nearmap_row(
                cell_equipment_evidence="Roof is obscured by trees; cannot confirm."
            ),
            "possible_stealth_host": _nearmap_row(site_evidence="Church with a tall steeple."),
            "weak_nearmap_call": _nearmap_row(cell_equipment_confidence=0.6),
            "error": _nearmap_row(error="nearmap timeout"),
            "gear_claim_disputed": _nearmap_row(dual_model_resolution="claude_veto"),
            "signal_nearby": _nearmap_row(signal_strength="strong"),
        }
        for reason, row in cases.items():
            with self.subTest(reason=reason):
                self.assertEqual(audit_verdict(row), ("inconclusive", reason))

    def test_distant_db_record_does_not_block(self):
        row = _nearmap_row(match_source="FCC", match_distance_m="400")
        self.assertEqual(audit_verdict(row)[0], "no_asset")


class AuditPayloadTests(unittest.TestCase):
    def test_no_asset_row_unqualifies_and_reassigns(self):
        row = stamp_audit_verdict(
            _nearmap_row(), run_id="2026-10-07_010000_connectx_audit", today=date(2026, 10, 7)
        )
        self.assertEqual(row["bucket"], "audit_unqualify")
        payload = build_row_payload(row, write_holdout=False)
        self.assertEqual(
            payload,
            {
                "OwnerId": SITE_ACQ_TEAM_OWNER_ID,
                "Stage__c": "Unqualified",
                "Unqualified_Reason__c": "No Site/Decommissioned",
                "Other_Unqualified_Reason__c": (
                    "ConnectX audit 2026-10-07_010000_connectx_audit: "
                    "Nearmap obliques show roof present, no telecom gear."
                ),
                "Unqualified_Date__c": "2026-10-07",
                "LLM_Classified__c": True,
            },
        )
        entry = {"success": True, "dry_run": False, "payload": payload}
        self.assertEqual(status_from_entry(entry), "unqualified")
        row["sf_update_status"] = "unqualified"
        self.assertEqual(outcome_class(row), "holdout_empty_confirmed")

    def test_confirmed_row_can_be_assigned_to_a_rep(self):
        row = stamp_audit_verdict(
            _nearmap_row(bucket="potential_update", holdout_reason="",
                         update_site_type="Rooftop", update_verified_site=True,
                         update_verified_site_source="NearMap",
                         update_lat=39.6, update_lng=-104.9),
            run_id="r",
            assign_confirmed_to="005REP",
        )
        payload = build_row_payload(row, write_holdout=False)
        self.assertEqual(payload["OwnerId"], "005REP")
        self.assertEqual(payload["Site_Type__c"], "Rooftop")
        self.assertNotIn("Stage__c", payload)
        self.assertEqual(status_from_entry({"success": True, "payload": payload}), "updated")

    def test_tower_confirms_are_not_reassigned(self):
        row = stamp_audit_verdict(
            _nearmap_row(bucket="potential_update", holdout_reason="skip_classify_db_hit",
                         naip_site_type="tower",
                         update_site_type="Self Support / Lattice Tower"),
            run_id="r",
            assign_confirmed_to="005REP",
        )
        self.assertNotIn("OwnerId", build_row_payload(row, write_holdout=False))

    def test_assign_to_never_applies_to_no_asset_or_inconclusive(self):
        no_asset = stamp_audit_verdict(_nearmap_row(), run_id="r", assign_confirmed_to="005REP")
        self.assertEqual(build_row_payload(no_asset)["OwnerId"], SITE_ACQ_TEAM_OWNER_ID)
        weak = stamp_audit_verdict(_nearmap_row(cell_equipment_confidence=0.5), run_id="r",
                                   assign_confirmed_to="005REP")
        self.assertNotIn("update_owner_id", weak)

    def test_unqualify_owner_from_env(self):
        with patch.dict(os.environ, {"AUDIT_UNQUALIFY_OWNER": "0056O00000EpUOgQAN"}):
            row = stamp_audit_verdict(_nearmap_row(), run_id="r")
        self.assertEqual(build_row_payload(row)["OwnerId"], "0056O00000EpUOgQAN")

    def test_owner_override_from_env(self):
        with patch.dict(os.environ, {"SITE_ACQ_OWNER_ID": "005OVERRIDE"}):
            row = stamp_audit_verdict(_nearmap_row(), run_id="r")
        self.assertEqual(build_row_payload(row)["OwnerId"], "005OVERRIDE")

    def test_inconclusive_row_has_no_unqualify_fields(self):
        row = stamp_audit_verdict(_nearmap_row(nearmap_tier="naip_only", nearmap_views=""),
                                  run_id="r")
        self.assertEqual(row["audit_verdict"], "inconclusive")
        self.assertNotIn("update_owner_id", row)
        self.assertEqual(row["bucket"], "potential_rooftop")

    def test_csv_round_trip_rebuilds_the_same_payload(self):
        row = stamp_audit_verdict(_nearmap_row(), run_id="r", today=date(2026, 10, 7))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / DETAIL_CSV
            write_csv(path, [row], DETAIL_COLUMNS)
            with path.open(newline="", encoding="utf-8") as handle:
                (reread,) = list(csv.DictReader(handle))
        self.assertEqual(build_row_payload(reread), build_row_payload(row))


class AuditQueryTests(unittest.TestCase):
    def test_query_targets_rep_owned_connectx_rooftops(self):
        soql = build_connectx_audit_query(states=["co"], owners=["Jeremy Scott"])
        self.assertIn("Carrier_Leasing_Source__c LIKE '%ConnectX%'", soql)
        self.assertIn("Site_Type__c = 'Rooftop'", soql)
        self.assertIn(f"OwnerId != '{SITE_ACQ_TEAM_OWNER_ID}'", soql)
        self.assertIn(
            "Stage__c IN ('New/Unreviewed', 'Enhanced/Unreviewed', 'Outreach', "
            "'Outreach - Verified')",
            soql,
        )
        self.assertIn("Owner__c IN ('Jeremy Scott')", soql)
        self.assertIn("Site_State__c IN ('CO')", soql)
        self.assertNotIn("LLM_Classified__c =", soql)
        self.assertTrue(soql.endswith("ORDER BY Id"))

    def test_pool_query_targets_site_acq_team_with_llm_filter(self):
        soql = build_connectx_audit_query(pool=True, llm_classified=True)
        self.assertIn(f"OwnerId = '{SITE_ACQ_TEAM_OWNER_ID}'", soql)
        self.assertIn("LLM_Classified__c = true", soql)


class PriorAuditIdsTests(unittest.TestCase):
    def _run(self, root: Path, name: str, rows: list[dict], summary: dict | None = None):
        run = root / name
        write_csv(run / DETAIL_CSV, rows, DETAIL_COLUMNS)
        if summary is not None:
            (run / "summary.json").write_text(json.dumps(summary), encoding="utf-8")

    def test_skips_decided_ids_and_retries_transient_ones(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._run(root, f"2026-10-07_010000{AUDIT_RUN_SUFFIX}", [
                {"Id": "UNQ", "audit_verdict": "no_asset", "sf_update_status": "unqualified"},
                {"Id": "CONF", "audit_verdict": "confirmed", "sf_update_status": "updated"},
                {"Id": "FAIL", "audit_verdict": "no_asset", "sf_update_status": "failed"},
                {"Id": "INC", "audit_verdict": "inconclusive", "audit_reason": "no_nearmap_obliques",
                 "sf_update_status": "skipped"},
                {"Id": "BUDGET", "audit_verdict": "inconclusive", "audit_reason": "nearmap_budget",
                 "sf_update_status": "skipped"},
            ])
            # Dry run never applied: nothing from it counts.
            self._run(root, f"2026-10-06_220000{AUDIT_DRY_RUN_SUFFIX}", [
                {"Id": "DRY", "audit_verdict": "inconclusive", "audit_reason": "weak_nearmap_call",
                 "sf_update_status": "skipped"},
            ])
            # Not an audit folder.
            self._run(root, "2026-10-05_120000_sf_enrichment", [
                {"Id": "OTHER", "audit_verdict": "confirmed", "sf_update_status": "updated"},
            ])
            self.assertEqual(sorted(prior_audit_ids(root)), ["CONF", "INC", "UNQ"])
            self.assertEqual(
                sorted(prior_audit_ids(root, retry_inconclusive=True)), ["CONF", "UNQ"]
            )

    def test_dry_run_pushed_with_apply_existing_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._run(
                root,
                f"2026-10-06_220000{AUDIT_DRY_RUN_SUFFIX}",
                [{"Id": "INC", "audit_verdict": "inconclusive",
                  "audit_reason": "weak_nearmap_call", "sf_update_status": "skipped"}],
                summary={"apply": {"apply": True}},
            )
            self.assertEqual(prior_audit_ids(root), ["INC"])


# ------------------------------ offline run ---------------------------------

REAL_ID = "a0ZAUDITREAL0001"
EMPTY_ID = "a0ZAUDITEMPTY001"
STEEPLE_ID = "a0ZAUDITSTEEPLE1"

_GEAR = {"site_type": "rooftop", "site_confidence": 0.9, "site_evidence": "commercial roof",
         "cell_equipment": True, "cell_equipment_confidence": 0.92,
         "cell_equipment_evidence": "North oblique shows sector panel antennas on the parapet",
         "cell_gear_kind": "sector_panel", "asset_box_2d": [420, 430, 520, 540],
         "asset_view": "Nearmap oblique (North)"}
_NO_GEAR = {"site_type": "rooftop", "site_confidence": 0.9,
            "site_evidence": "two-story office building",
            "cell_equipment": False, "cell_equipment_confidence": 0.9,
            "cell_equipment_evidence": "flat roof with HVAC units only",
            "cell_gear_kind": "none"}
_STEEPLE = dict(_NO_GEAR, site_evidence="church with a tall steeple")

REPLIES = {
    REAL_ID: {"naip": dict(_GEAR, cell_equipment_confidence=0.8, cell_gear_kind="unclear"),
              "nearmap": _GEAR},
    EMPTY_ID: {"naip": _NO_GEAR, "nearmap": _NO_GEAR},
    STEEPLE_ID: {"naip": _STEEPLE, "nearmap": _STEEPLE},
}


class _AuditClaude:
    """Claude replies with the same per-site call Gemini made on Nearmap."""

    def __init__(self, log):
        self.log = log

    def create(self, *, model, max_tokens, tools, tool_choice, messages):
        from enrichment.tests import test_e2e_offline as e2e

        site = e2e._SITE_BY_THREAD.get("site")
        self.log.append((site, model))
        reply = dict(REPLIES[site]["nearmap"])

        class Block:
            type = "tool_use"
            name = tool_choice["name"]
            input = reply

        class Resp:
            content = [Block()]

        return Resp()


class OfflineAuditRunTests(unittest.TestCase):
    def test_audit_confirms_unqualifies_and_leaves_inconclusive(self):
        from classifier import llm
        from enrichment import naip_classify
        from enrichment.pipeline import run_enrichment
        from enrichment.tests import test_e2e_offline as e2e
        import enrichment.metrics as metrics_mod

        sf = e2e._Client()
        gemini_log: list = []
        claude_log: list = []
        clients = e2e._FakeClients(gemini_log, claude_log)
        clients["claude"].messages = _AuditClaude(claude_log)

        def classify(**kwargs):
            e2e._SITE_BY_THREAD["site"] = kwargs["site_id"]
            return naip_classify.classify_site_imagery(**kwargs)

        sites = [
            {"Id": sid, "Site_Latitude__c": 39.6 + i, "Site_Longitude__c": -104.9,
             "Stage__c": "New/Unreviewed", "Site_Type__c": "Rooftop", "Owner__c": "Rep"}
            for i, sid in enumerate((REAL_ID, EMPTY_ID, STEEPLE_ID))
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = {
                "NEARMAP_API_KEY": "x",
                "METRICS_SQL": "0",
                "SITE_ORCHESTRATOR_DATA": tmp,
                "NEARMAP_TIERED": "1",
                "BIFURCATED_AI": "1",
                "GEMINI_ONLY": "0",
                "NAIP_ONLY": "0",
                "OSM_PREFILTER": "0",
            }
            old_dir = metrics_mod.metrics_dir
            metrics_mod.metrics_dir = lambda: root
            try:
                with patch.dict(os.environ, env), \
                        patch.dict(e2e.REPLIES, REPLIES), \
                        patch("classifier.imagery.fetch_chip", side_effect=e2e._fake_chip), \
                        patch("classifier.imagery.fetch_nearmap_views",
                              side_effect=e2e._fake_nearmap), \
                        patch("classifier.asset_classifier.fetch_nearmap_views",
                              side_effect=e2e._fake_nearmap), \
                        patch("enrichment.osm_prefilter.lookup_osm_features",
                              return_value={"ok": False}), \
                        patch.object(naip_classify, "_build_clients", return_value=clients), \
                        patch.object(llm, "GEMINI_LIMITER", llm.RateLimiter(0)), \
                        patch.object(llm, "CLAUDE_LIMITER", llm.RateLimiter(0)):
                    summary = run_enrichment(
                        sf_client=sf,
                        sql_connection=e2e._EmptySql(),
                        run_dir=root / f"run{AUDIT_RUN_SUFFIX}",
                        sites=sites,
                        classify_fn=classify,
                        apply=True,
                        dequeue_holdouts=False,
                        verbose=False,
                        workers=1,
                        connectx_audit=True,
                    )
            finally:
                metrics_mod.metrics_dir = old_dir
            with (root / f"run{AUDIT_RUN_SUFFIX}" / DETAIL_CSV).open(
                newline="", encoding="utf-8"
            ) as handle:
                detail = {row["Id"]: row for row in csv.DictReader(handle)}
            skipped_next = sorted(prior_audit_ids(root))

        real, empty, steeple = detail[REAL_ID], detail[EMPTY_ID], detail[STEEPLE_ID]
        self.assertEqual(real["audit_verdict"], "confirmed", real["holdout_reason"])
        self.assertEqual(real["sf_update_status"], "updated")
        self.assertEqual(empty["audit_verdict"], "no_asset", empty["audit_reason"])
        self.assertEqual(empty["sf_update_status"], "unqualified")
        self.assertEqual(steeple["audit_verdict"], "inconclusive")
        self.assertEqual(steeple["audit_reason"], "possible_stealth_host")
        self.assertEqual(steeple["sf_update_status"], "skipped")

        written = {record_id: payload for record_id, payload in sf.sf.Site__c.calls}
        self.assertEqual(set(written), {REAL_ID, EMPTY_ID})
        self.assertEqual(written[REAL_ID]["Site_Type__c"], "Rooftop")
        self.assertNotIn("OwnerId", written[REAL_ID])
        self.assertNotIn("LLM_Holdout__c", written[REAL_ID])
        self.assertEqual(written[EMPTY_ID]["OwnerId"], SITE_ACQ_TEAM_OWNER_ID)
        self.assertEqual(written[EMPTY_ID]["Stage__c"], "Unqualified")
        self.assertEqual(written[EMPTY_ID]["Unqualified_Reason__c"], "No Site/Decommissioned")
        self.assertEqual(summary["apply"]["unqualified"], 1)
        self.assertEqual(skipped_next, sorted([REAL_ID, EMPTY_ID, STEEPLE_ID]))


if __name__ == "__main__":
    unittest.main()
