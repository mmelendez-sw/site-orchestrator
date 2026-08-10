"""Unit tests for rooftop Nearmap tier stop + Claude escalation gates."""

from __future__ import annotations

import unittest

from classifier.asset_classifier import (
    escalation_reason,
    rooftop_requires_nearmap_tiers,
    tier_confident_stop,
)


class TierConfidentStopTests(unittest.TestCase):
    def test_tower_stops_when_cell_false(self):
        self.assertTrue(
            tier_confident_stop(
                {
                    "site_type": "tower",
                    "site_confidence": 0.8,
                    "cell_equipment": False,
                }
            )
        )

    def test_rooftop_does_not_stop_on_cell_false(self):
        self.assertFalse(
            tier_confident_stop(
                {
                    "site_type": "rooftop",
                    "site_confidence": 0.85,
                    "cell_equipment": False,
                }
            )
        )

    def test_rooftop_does_not_stop_on_cell_null(self):
        self.assertFalse(
            tier_confident_stop(
                {
                    "site_type": "rooftop",
                    "site_confidence": 0.85,
                    "cell_equipment": None,
                }
            )
        )

    def test_rooftop_stops_when_cell_confirmed(self):
        self.assertTrue(
            tier_confident_stop(
                {
                    "site_type": "rooftop",
                    "site_confidence": 0.8,
                    "cell_equipment": True,
                    "cell_equipment_confidence": 0.85,
                }
            )
        )

    def test_rooftop_does_not_stop_on_low_cell_confidence(self):
        self.assertFalse(
            tier_confident_stop(
                {
                    "site_type": "rooftop",
                    "site_confidence": 0.9,
                    "cell_equipment": True,
                    "cell_equipment_confidence": 0.5,
                }
            )
        )

    def test_rooftop_always_requires_nearmap_tiers(self):
        self.assertTrue(
            rooftop_requires_nearmap_tiers(
                {
                    "site_type": "rooftop",
                    "site_confidence": 0.95,
                    "cell_equipment": True,
                    "cell_equipment_confidence": 0.95,
                }
            )
        )
        self.assertFalse(
            rooftop_requires_nearmap_tiers(
                {"site_type": "tower", "site_confidence": 0.9, "cell_equipment": True}
            )
        )


class EscalationReasonTests(unittest.TestCase):
    def test_tower_cell_false_skips_claude(self):
        self.assertIsNone(
            escalation_reason(
                {
                    "site_type": "tower",
                    "site_confidence": 0.8,
                    "cell_equipment": False,
                }
            )
        )

    def test_rooftop_cell_false_escalates(self):
        self.assertEqual(
            escalation_reason(
                {
                    "site_type": "rooftop",
                    "site_confidence": 0.85,
                    "cell_equipment": False,
                }
            ),
            "rooftop_cell_unconfirmed",
        )

    def test_rooftop_cell_null_escalates(self):
        self.assertEqual(
            escalation_reason(
                {
                    "site_type": "rooftop",
                    "site_confidence": 0.85,
                    "cell_equipment": None,
                }
            ),
            "rooftop_cell_unconfirmed",
        )

    def test_rooftop_low_cell_conf_escalates(self):
        self.assertEqual(
            escalation_reason(
                {
                    "site_type": "rooftop",
                    "site_confidence": 0.9,
                    "cell_equipment": True,
                    "cell_equipment_confidence": 0.6,
                }
            ),
            "rooftop_low_cell_confidence",
        )

    def test_rooftop_confirmed_cell_skips_claude(self):
        self.assertIsNone(
            escalation_reason(
                {
                    "site_type": "rooftop",
                    "site_confidence": 0.9,
                    "cell_equipment": True,
                    "cell_equipment_confidence": 0.85,
                }
            )
        )


if __name__ == "__main__":
    unittest.main()
