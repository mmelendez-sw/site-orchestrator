from __future__ import annotations

import unittest

from enrichment.naip_classify import dispute_oblique_stages


class DisputeObliqueStageTests(unittest.TestCase):
    def test_north_east_owned_buys_south_west(self):
        self.assertEqual(dispute_oblique_stages({"Vert": 1, "North": 1, "East": 1}, ["North", "East"]),
                         [["South", "West"]])

    def test_missing_configured_view_comes_first(self):
        self.assertEqual(dispute_oblique_stages({"Vert": 1, "North": 1}, ["North", "East"]),
                         [["East"], ["South", "West"]])

    def test_all_four_owned_buys_nothing(self):
        have = {"Vert": 1, "North": 1, "East": 1, "South": 1, "West": 1}
        self.assertEqual(dispute_oblique_stages(have, ["North", "East"]), [])


if __name__ == "__main__":
    unittest.main()
