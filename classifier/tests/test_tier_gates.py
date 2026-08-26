"""Unit tests for rooftop Nearmap tier stop + Claude escalation gates."""

from __future__ import annotations

import unittest

from unittest.mock import patch

from classifier.asset_classifier import (
    confirm_rooftop_cell_with_claude,
    escalation_reason,
    rooftop_requires_nearmap_tiers,
    should_skip_claude_for_gemini_tower,
    tier_confident_stop,
    tower_cell_requires_nearmap_obliques,
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


class TowerObliqueFetchTests(unittest.TestCase):
    _locked = {
        "site_type": "tower",
        "site_confidence": 0.9,
        "cell_equipment": True,
    }

    def test_imagery_only_tower_cell_requires_obliques(self):
        self.assertTrue(
            tower_cell_requires_nearmap_obliques(self._locked, db_backed=False)
        )

    def test_db_hit_gemini_lock_skips_obliques(self):
        self.assertFalse(
            tower_cell_requires_nearmap_obliques(self._locked, db_backed=True)
        )

    def test_db_hit_below_lock_still_requires_obliques(self):
        self.assertTrue(
            tower_cell_requires_nearmap_obliques(
                {
                    "site_type": "tower",
                    "site_confidence": 0.85,
                    "cell_equipment": True,
                },
                db_backed=True,
            )
        )

    def test_tower_cell_false_does_not_require_obliques(self):
        self.assertFalse(
            tower_cell_requires_nearmap_obliques(
                {
                    "site_type": "tower",
                    "site_confidence": 0.9,
                    "cell_equipment": False,
                },
                db_backed=False,
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

    def test_other_at_high_gemini_conf_skips_claude(self):
        self.assertIsNone(
            escalation_reason(
                {
                    "site_type": "other",
                    "site_confidence": 0.9,
                    "cell_equipment": None,
                }
            )
        )

    def test_other_below_0_7_skips_claude(self):
        self.assertIsNone(
            escalation_reason(
                {
                    "site_type": "other",
                    "site_confidence": 0.5,
                    "cell_equipment": None,
                }
            )
        )
        self.assertIsNone(
            escalation_reason(
                {
                    "site_type": "other",
                    "site_confidence": 0.69,
                    "cell_equipment": None,
                }
            )
        )

    def test_other_at_0_7_still_escalates(self):
        self.assertEqual(
            escalation_reason(
                {
                    "site_type": "other",
                    "site_confidence": 0.7,
                    "cell_equipment": None,
                }
            ),
            "other_type",
        )

    def test_unclear_at_high_conf_still_escalates(self):
        self.assertEqual(
            escalation_reason(
                {
                    "site_type": "unclear",
                    "site_confidence": 0.95,
                    "cell_equipment": None,
                }
            ),
            "unclear_type",
        )

    def test_rooftop_cell_unconfirmed_below_0_7_skips_claude(self):
        self.assertIsNone(
            escalation_reason(
                {
                    "site_type": "rooftop",
                    "site_confidence": 0.65,
                    "cell_equipment": False,
                }
            )
        )

    def test_rooftop_cell_unconfirmed_at_high_conf_still_escalates(self):
        self.assertEqual(
            escalation_reason(
                {
                    "site_type": "rooftop",
                    "site_confidence": 0.95,
                    "cell_equipment": False,
                }
            ),
            "rooftop_cell_unconfirmed",
        )


class GeminiTowerSkipClaudeTests(unittest.TestCase):
    def test_skip_when_tower_site_conf_at_least_0_9(self):
        self.assertTrue(
            should_skip_claude_for_gemini_tower(
                {
                    "site_type": "tower",
                    "site_confidence": 0.9,
                    "cell_equipment": True,
                }
            )
        )

    def test_do_not_skip_below_0_9(self):
        self.assertFalse(
            should_skip_claude_for_gemini_tower(
                {
                    "site_type": "tower",
                    "site_confidence": 0.89,
                    "cell_equipment": True,
                }
            )
        )

    def test_do_not_skip_rooftop(self):
        self.assertFalse(
            should_skip_claude_for_gemini_tower(
                {
                    "site_type": "rooftop",
                    "site_confidence": 0.99,
                    "cell_equipment": True,
                }
            )
        )

    def test_do_not_skip_wide_rescue(self):
        self.assertFalse(
            should_skip_claude_for_gemini_tower(
                {
                    "site_type": "tower",
                    "site_confidence": 1.0,
                    "cell_equipment": True,
                },
                from_wide_rescue=True,
            )
        )

    def test_dual_model_skips_claude_for_high_conf_tower(self):
        res = {
            "site_type": "tower",
            "site_confidence": 1.0,
            "cell_equipment": True,
            "cell_equipment_confidence": 0.82,
            "cell_equipment_evidence": "lattice tower with sector panels",
            "cell_gear_kind": "sector_panel",
            "asset_box_2d": [220, 310, 360, 420],
            "asset_view": "Nearmap oblique (East)",
        }
        with patch(
            "classifier.asset_classifier.classify_site"
        ) as mock_classify:
            out, model, agree = confirm_rooftop_cell_with_claude(
                res,
                {"claude": object()},
                [],
                already_escalated=False,
                allow_soft_keep=False,
                allow_gemini_solo=False,
                used_crop=False,
            )
        mock_classify.assert_not_called()
        self.assertTrue(agree)
        self.assertEqual(model, "gemini_strong_solo")
        self.assertEqual(out["dual_model_resolution"], "gemini_strong_solo")

    def test_rooftop_crop_no_falls_back_to_localize(self):
        from PIL import Image

        img = Image.new("RGB", (64, 64), "gray")
        full_views = [("Nearmap oblique (North)", img)]
        crop_views = [("cell crop (Nearmap oblique (North))", img), full_views[0]]
        res = {
            "site_type": "rooftop",
            "site_confidence": 0.9,
            "cell_equipment": True,
            "cell_equipment_confidence": 0.88,
            "cell_equipment_evidence": "North oblique shows sector panels",
            "cell_gear_kind": "sector_panel",
            "asset_box_2d": [220, 310, 360, 420],
            "asset_view": "Nearmap oblique (North)",
        }

        def _fake_classify(_provider, _clients, views, prompt=None, **_kwargs):
            labels = [str(v[0]) for v in views]
            if any("cell crop" in lab for lab in labels):
                return {
                    "site_type": "rooftop",
                    "cell_equipment": False,
                    "cell_equipment_evidence": "crop too tight, only roof membrane",
                    "cell_gear_kind": "none",
                }
            return {
                "site_type": "rooftop",
                "cell_equipment": True,
                "cell_equipment_confidence": 0.9,
                "cell_equipment_evidence": "sector panels on north facade",
                "cell_gear_kind": "sector_panel",
                "asset_box_2d": [200, 300, 340, 400],
                "asset_view": "Nearmap oblique (North)",
            }

        with patch(
            "classifier.asset_classifier.classify_site",
            side_effect=_fake_classify,
        ):
            out, model, agree = confirm_rooftop_cell_with_claude(
                res,
                {"claude": object()},
                crop_views,
                already_escalated=False,
                allow_soft_keep=False,
                allow_gemini_solo=False,
                used_crop=True,
                all_views=full_views,
            )
        self.assertTrue(agree)
        self.assertEqual(model, "claude")
        self.assertEqual(out["dual_model_resolution"], "agree_localize")
        self.assertTrue(out["cell_equipment"])


if __name__ == "__main__":
    unittest.main()
