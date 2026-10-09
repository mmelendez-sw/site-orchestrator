"""Skip / confirm decisions line up with the write gates (2026-10-08 review)."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from PIL import Image

from classifier import asset_classifier as ac


def _img():
    return Image.new("RGB", (64, 64))


class LowCropYesTests(unittest.TestCase):
    def _res(self):
        return {"site_type": "rooftop", "site_confidence": 0.9, "cell_equipment": True,
                "cell_equipment_confidence": 0.9, "cell_gear_kind": "sector_panel",
                "asset_box_2d": [400, 400, 500, 500], "asset_view": "Nearmap oblique (North)",
                "nearmap_tier": "full", "nearmap_views": "Vert,North"}

    def test_low_confidence_crop_yes_takes_localize(self):
        replies = [{"cell_equipment": True, "cell_equipment_confidence": 0.7},
                   {"cell_equipment": True, "cell_equipment_confidence": 0.9,
                    "asset_box_2d": [300, 300, 420, 420], "asset_view": "Nearmap oblique (East)"}]
        views = [("Nearmap oblique (North)", _img()), ("Nearmap oblique (East)", _img())]
        with patch.object(ac, "classify_site", side_effect=lambda *a, **k: dict(replies.pop(0))):
            res, model, agree = ac.confirm_rooftop_cell_with_claude(
                self._res(), {"claude": object()}, views[:1], already_escalated=False,
                allow_soft_keep=False, used_crop=True, allow_gemini_solo=False, all_views=views)
        self.assertTrue(agree)
        self.assertEqual(res["dual_model_resolution"], "agree_localize")
        self.assertEqual(res["cell_equipment_confidence"], 0.9)
        self.assertEqual(res["asset_view"], "Nearmap oblique (East)")

    def test_confident_crop_yes_unchanged(self):
        with patch.object(ac, "classify_site", return_value={
                "cell_equipment": True, "cell_equipment_confidence": 0.9, "cell_gear_kind": "sector_panel",
                "cell_equipment_evidence": "Three sector panels on a pipe mast at the parapet."}) as call:
            res, _model, agree = ac.confirm_rooftop_cell_with_claude(
                self._res(), {"claude": object()}, [("crop", _img())], already_escalated=False,
                allow_soft_keep=False, used_crop=True, allow_gemini_solo=False,
                all_views=[("Nearmap oblique (North)", _img())])
        self.assertTrue(agree)
        self.assertEqual(res["dual_model_resolution"], "agree_crop")
        self.assertEqual(call.call_count, 1)


class StrictCropTests(unittest.TestCase):
    def test_hedged_or_unnamed_crop_yes_is_not_a_write(self):
        for reply in ({"cell_equipment": True, "cell_equipment_confidence": 0.9, "cell_gear_kind": "sector_panel",
                       "cell_equipment_evidence": "equipment that appears consistent with sector panels"},
                      {"cell_equipment": True, "cell_equipment_confidence": 0.9, "cell_gear_kind": "unclear",
                       "cell_equipment_evidence": "rooftop equipment"},
                      {"cell_equipment": True, "cell_equipment_confidence": 0.78, "cell_gear_kind": "rru",
                       "cell_equipment_evidence": "RRUs beside panels"}):
            self.assertFalse(ac._strict_rooftop_crop_yes(reply), reply)
        self.assertTrue(ac._strict_rooftop_crop_yes(
            {"cell_equipment": True, "cell_equipment_confidence": 0.85, "cell_gear_kind": "microwave",
             "cell_equipment_evidence": "A round microwave dish on a steel frame."}))

    def test_hedged_crop_yes_without_localize_holds_out(self):
        import os
        os.environ["ROOFTOP_CROP_STRICT"] = "1"
        self.addCleanup(os.environ.pop, "ROOFTOP_CROP_STRICT", None)
        replies = [{"cell_equipment": True, "cell_equipment_confidence": 0.9, "cell_gear_kind": "sector_panel",
                    "cell_equipment_evidence": "what appears to be panel antennas"},
                   {"cell_equipment": None, "cell_equipment_confidence": 0.4}]
        res = LowCropYesTests()._res()
        with patch.object(ac, "classify_site", side_effect=lambda *a, **k: dict(replies.pop(0))):
            out, _m, agree = ac.confirm_rooftop_cell_with_claude(
                res, {"claude": object()}, [("crop", _img())], already_escalated=False, allow_soft_keep=False,
                used_crop=True, allow_gemini_solo=False, all_views=[("Nearmap oblique (North)", _img())])
        self.assertFalse(agree)
        self.assertIsNot(out["cell_equipment"], True)


class TowerLockGearConfidenceTests(unittest.TestCase):
    def test_low_gear_confidence_does_not_lock(self):
        from enrichment.bucketing import _tower_gemini_high_conf_ok

        pole = {"site_type": "tower", "site_confidence": 0.9, "cell_equipment": True,
                "cell_equipment_confidence": 0.7, "dual_model_resolution": "gemini_strong_solo"}
        self.assertFalse(ac.should_skip_claude_for_gemini_tower(dict(pole)))
        self.assertFalse(_tower_gemini_high_conf_ok(dict(pole)))
        strong = dict(pole, cell_equipment_confidence=0.82)
        self.assertTrue(ac.should_skip_claude_for_gemini_tower(dict(strong)))
        self.assertTrue(_tower_gemini_high_conf_ok(dict(strong)))


class DisputedLabelTests(unittest.TestCase):
    def test_claude_veto_is_disputed_not_gear_seen(self):
        from enrichment.connectx_audit import audit_verdict

        row = {"bucket": "potential_rooftop", "holdout_reason": "rooftop_no_cell_equipment",
               "gemini_cell_equipment": "True", "claude_cell_equipment": "False",
               "dual_model_resolution": "claude_veto", "nearmap_tier": "full"}
        self.assertEqual(audit_verdict(row), ("inconclusive", "gear_claim_disputed"))


if __name__ == "__main__":
    unittest.main()
