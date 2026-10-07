"""Eval harness: labels, SOQL, latest-row selection, metrics math (offline)."""

from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import build_eval_set as bes  # noqa: E402
import eval_report as er  # noqa: E402


class BuildEvalSetTests(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(bes.label_for("Working - Connected", ""), "positive")
        self.assertEqual(bes.label_for("Working-Connected", None), "positive")
        self.assertEqual(bes.label_for("Qualified (Converted)", ""), "positive")
        self.assertEqual(bes.label_for("Unqualified", "No Site/Decommissioned"), "negative")
        self.assertEqual(bes.label_for("Unqualified", "Not a Cellular tower"), "negative")
        self.assertIsNone(bes.label_for("Unqualified", "Owner not interested"))
        self.assertIsNone(bes.label_for("Outreach", ""))

    def test_soql_is_select_with_filters(self):
        soql = bes.build_eval_soql(carrier_like="Connect'X", site_types=["Rooftop"], limit=10)
        self.assertTrue(soql.startswith("SELECT Id, Stage__c"))
        self.assertIn("'Working - Connected', 'Working-Connected', 'Qualified (Converted)'", soql)
        self.assertIn("Unqualified_Reason__c IN ('No Site/Decommissioned', 'Not a Cellular tower')", soql)
        self.assertIn("Carrier_Leasing_Source__c LIKE '%Connect\\'X%'", soql)
        self.assertIn("Site_Type__c IN ('Rooftop')", soql)
        self.assertTrue(soql.endswith("ORDER BY Id LIMIT 10"))
        self.assertNotIn("Carrier_Leasing_Source__c LIKE", bes.build_eval_soql())

    def test_eval_rows_drop_unlabelled(self):
        rows = bes.eval_rows([
            {"Id": "a1", "Stage__c": "Working - Connected", "Owner__c": "Rep", "Site_Type__c": "Rooftop"},
            {"Id": "a2", "Stage__c": "Unqualified", "Unqualified_Reason__c": "Other"},
            {"Id": "a3", "Stage__c": "Unqualified", "Unqualified_Reason__c": "Not a Cellular tower"},
        ])
        self.assertEqual([(r["Id"], r["label"]) for r in rows], [("a1", "positive"), ("a3", "negative")])
        self.assertEqual(rows[0]["owner"], "Rep")
        self.assertEqual(set(rows[0]), set(bes.EVAL_COLUMNS))


class MetricsTests(unittest.TestCase):
    def test_binary_metrics_math(self):
        pairs = [(True, True), (True, True), (True, False), (True, None),
                 (False, True), (False, False), (False, False), (False, None)]
        m = er.binary_metrics(pairs)
        self.assertEqual((m["tp"], m["fp"], m["fn"], m["tn"]), (2, 1, 1, 2))
        self.assertEqual((m["n"], m["called"]), (8, 6))
        self.assertAlmostEqual(m["precision"], 2 / 3)
        self.assertAlmostEqual(m["recall"], 2 / 3)
        self.assertAlmostEqual(m["coverage"], 6 / 8)
        self.assertAlmostEqual(m["accuracy"], 4 / 6)

    def test_empty_metrics_are_none(self):
        m = er.binary_metrics([(True, None)])
        self.assertIsNone(m["precision"])
        self.assertIsNone(m["recall"])
        self.assertEqual(m["coverage"], 0)
        self.assertIsNone(er.binary_metrics([])["coverage"])

    def test_predictions(self):
        self.assertTrue(er.pred_bucket({"bucket": "potential_update"}))
        self.assertFalse(er.pred_bucket({"bucket": "other_or_else"}))
        self.assertIsNone(er.pred_bucket({"bucket": ""}))
        self.assertIsNone(er.pred_bucket(None))
        self.assertTrue(er.pred_audit({"audit_verdict": "confirmed"}))
        self.assertFalse(er.pred_audit({"audit_verdict": "no_asset"}))
        self.assertIsNone(er.pred_audit({"audit_verdict": "inconclusive"}))
        self.assertTrue(er.pred_naip_cell({"naip_cell_equipment": "True"}))
        self.assertFalse(er.pred_naip_cell({"naip_cell_equipment": "", "naip_screen_cell_equipment": "False"}))
        self.assertIsNone(er.pred_naip_cell({"naip_cell_equipment": "null"}))

    def test_grouped_metrics_by_imagery(self):
        records = [
            {"truth": True, "row": {"imagery_used": "naip", "naip_cell_equipment": "True"}},
            {"truth": False, "row": {"imagery_used": "naip", "naip_cell_equipment": "True"}},
            {"truth": True, "row": {"imagery_used": "nearmap_oblique", "naip_cell_equipment": "False"}},
        ]
        groups = er.grouped_metrics(records, er.pred_naip_cell, lambda r: r["row"]["imagery_used"])
        self.assertEqual(groups["naip"]["precision"], 0.5)
        self.assertEqual(groups["nearmap_oblique"]["recall"], 0.0)

    def test_verdict_table(self):
        table = er.verdict_table([
            {"truth": True, "row": {"audit_verdict": "confirmed"}},
            {"truth": False, "row": {"audit_verdict": "confirmed"}},
            {"truth": False, "row": {}},
        ])
        self.assertEqual(table["confirmed"], {"positive": 1, "negative": 1})
        self.assertEqual(table["(no audit)"], {"positive": 0, "negative": 1})


class LatestRowTests(unittest.TestCase):
    def test_latest_run_wins_and_ids_match_on_15_chars(self):
        rows = [
            ("2026-10-02_run", {"Id": "a0Z000000000001AAA", "bucket": "other_or_else"}),
            ("2026-10-05_run", {"Id": "a0Z000000000001", "bucket": "potential_update"}),
            ("2026-10-03_run", {"Id": "a0Z000000000001AAA", "bucket": "skip"}),
            ("2026-10-04_run", {"Id": "a0Z000000000002AAA", "bucket": "skip"}),
        ]
        latest = er.select_latest(rows, {"a0Z000000000001"})
        self.assertEqual(list(latest), ["a0Z000000000001"])
        self.assertEqual(latest["a0Z000000000001"][0], "2026-10-05_run")
        self.assertEqual(latest["a0Z000000000001"][1]["bucket"], "potential_update")

    def test_end_to_end_from_run_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fields = ["Id", "bucket", "audit_verdict", "naip_cell_equipment", "imagery_used"]
            for name, rows in {
                "2026-10-01_a": [{"Id": "S1", "bucket": "other_or_else"}, {"Id": "S2", "bucket": "potential_update"}],
                "2026-10-06_b_connectx_audit": [{"Id": "S1", "bucket": "potential_update",
                                                 "audit_verdict": "confirmed", "naip_cell_equipment": "True",
                                                 "imagery_used": "naip"}],
            }.items():
                (root / name).mkdir()
                with (root / name / er.DETAIL_CSV).open("w", newline="", encoding="utf-8") as handle:
                    writer = csv.DictWriter(handle, fieldnames=fields)
                    writer.writeheader()
                    writer.writerows(rows)
            eval_path = root / "eval.csv"
            eval_path.write_text("Id,label,stage\nS1,positive,Working - Connected\n"
                                 "S2,negative,Unqualified\nS3,positive,Qualified (Converted)\n", encoding="utf-8")
            eval_set = er.load_eval(eval_path)
            runs = er.resolve_runs(None, root)
            latest = er.select_latest(er.iter_run_rows(runs), set(eval_set))
            records = er.join_records(eval_set, latest)
            m = er.binary_metrics((r["truth"], er.pred_bucket(r["row"])) for r in records)
            self.assertEqual((m["tp"], m["fp"], m["called"], m["n"]), (1, 1, 2, 3))
            text = er.report(records)
            self.assertIn("a. bucket potential_update", text)
            self.assertIn("naip", text)
            out = root / "joined.csv"
            er.write_joined(out, records)
            with out.open(newline="", encoding="utf-8") as handle:
                joined = {r["Id"]: r for r in csv.DictReader(handle)}
            self.assertEqual(joined["S1"]["run"], "2026-10-06_b_connectx_audit")
            self.assertEqual(joined["S1"]["pred_audit"], "1")
            self.assertEqual(joined["S3"]["pred_bucket"], "")


if __name__ == "__main__":
    unittest.main()
