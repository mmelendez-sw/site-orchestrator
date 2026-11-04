from __future__ import annotations

import unittest

from enrichment.naip_classify import dispute_oblique_stages


class DisputeObliqueStageTests(unittest.TestCase):
    def test_north_east_owned_buys_south_west(self):
        self.assertEqual(dispute_oblique_stages({"Vert": 1, "North": 1, "East": 1}, ["North", "East"]),
                         [["South"], ["West"]])

    def test_missing_configured_view_comes_first(self):
        self.assertEqual(dispute_oblique_stages({"Vert": 1, "North": 1}, ["North", "East"]),
                         [["East"], ["South"], ["West"]])

    def test_only_a_claude_qualified_call_stops_the_side_escalation(self):
        from enrichment.naip_classify import site_qualified

        self.assertFalse(site_qualified({"cell_equipment_confidence": 0.95}, False))  # Gemini sure, Claude no
        self.assertFalse(site_qualified({"cell_equipment_confidence": 0.65,
                                         "dual_model_resolution": "agree_crop"}, True))  # lukewarm Claude
        self.assertTrue(site_qualified({"cell_equipment_confidence": 0.85,
                                        "dual_model_resolution": "agree_crop"}, True))
        self.assertTrue(site_qualified(dict(self.tower_lock), True))
        self.assertFalse(site_qualified({"cell_equipment_confidence": 0.5, "site_type": "rooftop",
                                         "dual_model_resolution": "gemini_strong_solo"}, True))

    tower_lock = {"site_type": "tower", "site_confidence": 1.0, "cell_equipment": True,
                  "cell_equipment_confidence": 1.0, "dual_model_resolution": "gemini_strong_solo",
                  "asset_view": "Nearmap oblique (North)"}
    rooftop_agree = {"site_type": "rooftop", "site_confidence": 0.9, "cell_equipment": True,
                     "cell_equipment_confidence": 0.85, "dual_model_resolution": "agree_crop",
                     "nearmap_tier": "full", "asset_view": "Nearmap oblique (North)",
                     "asset_box_2d": [400, 400, 600, 600]}

    def test_a_call_the_write_gate_refuses_keeps_buying_sides(self):
        # 2026-11-03: these stopped on one side and were then held out.
        from enrichment.naip_classify import site_qualified

        street = "Street-level photo (Mapillary 2020-01, camera 87 m NW of site, facing SE toward site)"
        self.assertFalse(site_qualified(dict(self.tower_lock, asset_view=street), True))
        self.assertTrue(site_qualified(dict(self.rooftop_agree), True))
        self.assertFalse(site_qualified(dict(self.rooftop_agree, asset_box_2d=[0, 0, 900, 900]), True))
        self.assertFalse(site_qualified(dict(self.rooftop_agree, asset_view=street), True))

    def test_all_four_owned_buys_nothing(self):
        have = {"Vert": 1, "North": 1, "East": 1, "South": 1, "West": 1}
        self.assertEqual(dispute_oblique_stages(have, ["North", "East"]), [])


if __name__ == "__main__":
    unittest.main()
