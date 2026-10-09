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
        with patch.object(ac, "classify_site", return_value={"cell_equipment": True,
                                                            "cell_equipment_confidence": 0.9}) as call:
            res, _model, agree = ac.confirm_rooftop_cell_with_claude(
                self._res(), {"claude": object()}, [("crop", _img())], already_escalated=False,
                allow_soft_keep=False, used_crop=True, allow_gemini_solo=False,
                all_views=[("Nearmap oblique (North)", _img())])
        self.assertTrue(agree)
        self.assertEqual(res["dual_model_resolution"], "agree_crop")
        self.assertEqual(call.call_count, 1)


class DisputedLabelTests(unittest.TestCase):
    def test_claude_veto_is_disputed_not_gear_seen(self):
        from enrichment.connectx_audit import audit_verdict

        row = {"bucket": "potential_rooftop", "holdout_reason": "rooftop_no_cell_equipment",
               "gemini_cell_equipment": "True", "claude_cell_equipment": "False",
               "dual_model_resolution": "claude_veto", "nearmap_tier": "full"}
        self.assertEqual(audit_verdict(row), ("inconclusive", "gear_claim_disputed"))


if __name__ == "__main__":
    unittest.main()
