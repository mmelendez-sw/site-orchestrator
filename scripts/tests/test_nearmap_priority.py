from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import build_nearmap_priority as bnp  # noqa: E402


def _row(**kw):
    row = {"Id": "a0Z1", "sf_lat": "40.712345", "sf_lng": "-74.006789",
           "audit_verdict": "inconclusive", "naip_site_type": "rooftop", "nearmap_tier": "naip_only"}
    row.update(kw)
    return row


class NearmapPriorityTests(unittest.TestCase):
    def test_tiers(self):
        self.assertEqual(bnp.nearmap_priority(_row(gemini_cell_equipment="True"))[0], 1)
        self.assertEqual(bnp.nearmap_priority(_row())[0], 2)
        self.assertEqual(bnp.nearmap_priority(_row(naip_site_type="other"))[0], 3)
        self.assertEqual(bnp.nearmap_priority(_row(nearmap_tier="full",
                                                   nearmap_views="Vert,North"))[0], 6)
        self.assertEqual(bnp.nearmap_priority(_row(sf_lat="40.712"))[0], 9)

    def test_building_evidence_promotes(self):
        self.assertEqual(bnp.nearmap_priority(_row(naip_site_type="other"), "tall building"),
                         (1.0, "tall building, no obliques"))

    def test_decided_sites_skipped(self):
        self.assertIsNone(bnp.nearmap_priority(_row(audit_verdict="confirmed")))
        self.assertIsNone(bnp.nearmap_priority(_row(audit_verdict="no_asset")))


if __name__ == "__main__":
    unittest.main()
