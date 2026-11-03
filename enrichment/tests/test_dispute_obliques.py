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
        self.assertTrue(site_qualified({"cell_equipment_confidence": 0.5, "site_type": "tower",
                                        "dual_model_resolution": "gemini_strong_solo"}, True))
        self.assertFalse(site_qualified({"cell_equipment_confidence": 0.5, "site_type": "rooftop",
                                         "dual_model_resolution": "gemini_strong_solo"}, True))

    def test_all_four_owned_buys_nothing(self):
        have = {"Vert": 1, "North": 1, "East": 1, "South": 1, "West": 1}
        self.assertEqual(dispute_oblique_stages(have, ["North", "East"]), [])


if __name__ == "__main__":
    unittest.main()
