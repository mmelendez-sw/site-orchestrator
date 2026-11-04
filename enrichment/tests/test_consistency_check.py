from __future__ import annotations

import unittest
from unittest.mock import patch

from enrichment import pipeline


class ConsistencyCheckTests(unittest.TestCase):
    base = {"classify_lat": 40.0, "classify_lng": -74.0, "bucket": "potential_update",
            "update_site_type": "Rooftop"}
    classified = {"site_type": "rooftop", "cell_equipment": True}
    kwargs = {"site_id": "a0Z1", "lat": 40.0, "lon": -74.0, "chip_dir": "C:/tmp/chips", "verbose": False}

    def _run(self, second_bucket, *, kwargs=None):
        calls = []

        def classify_fn(**kw):
            calls.append(kw)
            return {"site_type": "rooftop"}

        with patch.object(pipeline, "_bucket", return_value=second_bucket):
            out = pipeline._consistency_check(None, classify_fn, kwargs or self.kwargs, dict(self.base),
                                              self.classified, verbose=False)
        return out, calls

    def test_agree_keeps_the_write(self):
        out, calls = self._run({"bucket": "potential_update", "update_site_type": "Rooftop"})
        self.assertEqual(out["consistency_check"], "agree")
        self.assertNotIn("bucket", out)
        self.assertTrue(calls[0]["consistency_pass"])
        self.assertEqual([p.as_posix() for p in calls[0]["reuse_chips_dirs"]], ["C:/tmp/chips"])

    def test_disagree_holds_out(self):
        out, _calls = self._run({"bucket": "other_or_else", "holdout_reason": "other", "update_site_type": ""})
        self.assertEqual((out["consistency_check"], out["holdout_reason"]), ("disagree", "consistency_disagree"))
        self.assertEqual((out["bucket"], out["update_site_type"]), ("potential_rooftop", ""))

    def test_type_change_is_a_disagreement(self):
        out, _calls = self._run({"bucket": "potential_update", "update_site_type": "Monopole"})
        self.assertEqual(out["holdout_reason"], "consistency_disagree")

    def test_same_type_with_gear_agrees_even_if_gated(self):
        def classify_fn(**kw):
            return {"site_type": "rooftop", "cell_equipment": True}
        with patch.object(pipeline, "_bucket", return_value={"bucket": "potential_rooftop",
                                                             "holdout_reason": "rooftop_low_cell_confidence"}):
            out = pipeline._consistency_check(None, classify_fn, self.kwargs, dict(self.base),
                                              self.classified, verbose=False)
        self.assertEqual(out["consistency_check"], "agree")

    def test_rereview_requires_strict_second_write(self):
        import os

        def classify_fn(**kw):
            return {"site_type": "rooftop", "cell_equipment": True}
        with patch.dict(os.environ, {"AUDIT_RETRY_INCONCLUSIVE": "1"}),                 patch.object(pipeline, "_bucket", return_value={"bucket": "potential_rooftop",
                                                                 "holdout_reason": "rooftop_low_cell_confidence"}):
            out = pipeline._consistency_check(None, classify_fn, self.kwargs, dict(self.base),
                                              self.classified, verbose=False)
        self.assertEqual(out["holdout_reason"], "consistency_disagree")

    def test_sides_seen_counts_obliques_only(self):
        self.assertEqual(pipeline._oblique_sides_seen({"nearmap_views": "Vert,North"}), 1)
        self.assertEqual(pipeline._oblique_sides_seen({"nearmap_views": "Vert,North,East,South,West"}), 4)
        self.assertEqual(pipeline._oblique_sides_seen({}), 0)

    def _classify_prepared(self, buckets, views):
        calls = []

        def classify_fn(**kw):
            calls.append(kw)
            return {"site_type": "tower", "nearmap_views": views[len(calls) - 1]}

        prep = pipeline.PreparedSite(
            base={"Id": "a0Z1", "classify_lat": 40.0, "classify_lng": -74.0,
                  "pin_address_offset_m": "", "pin_address_mismatch": False},
            site={}, hit=object(), sf_lat=40.0, sf_lng=-74.0,
        )
        with patch.object(pipeline, "_bucket", side_effect=buckets):
            out = pipeline._classify_prepared(
                prep, classify_fn=classify_fn, chip_dir=None, verbose=False,
                cluster_cache=None, reuse_chips_dirs=None,
            )
        return out, calls

    def test_write_gate_holdout_buys_another_side(self):
        # 2026-11-03: a NAIP-only DB-hit tower was held out with no Nearmap.
        out, calls = self._classify_prepared(
            [{"bucket": "other_or_else", "holdout_reason": "tower_naip_only_forbidden"},
             {"bucket": "potential_update", "holdout_reason": "", "update_site_type": "Monopole"}],
            ["", "Vert,North"],
        )
        self.assertEqual([c.get("min_oblique_sides", 0) for c in calls], [0, 1])
        self.assertEqual(out["bucket"], "potential_update")

    def test_other_holdouts_do_not_buy_sides(self):
        out, calls = self._classify_prepared(
            [{"bucket": "potential_rooftop", "holdout_reason": "rooftop_no_cell_equipment"}],
            ["Vert,North"],
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(out["holdout_reason"], "rooftop_no_cell_equipment")

    def test_no_new_side_stops_the_re_check(self):
        out, calls = self._classify_prepared(
            [{"bucket": "potential_rooftop", "holdout_reason": "rooftop_needs_oblique_asset_box"}],
            ["Vert,North", "Vert,North"],
        )
        self.assertEqual(len(calls), 2)
        self.assertEqual(out["holdout_reason"], "rooftop_needs_oblique_asset_box")

    def test_no_chip_dir_skips(self):
        out, calls = self._run({}, kwargs=dict(self.kwargs, chip_dir=None))
        self.assertEqual((out, calls), ({"consistency_check": "skipped_no_chips"}, []))


if __name__ == "__main__":
    unittest.main()
