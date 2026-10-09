from __future__ import annotations

import unittest

from enrichment.pipeline import review_reason


class ReviewQueueTests(unittest.TestCase):
    def test_reasons(self):
        self.assertEqual(review_reason({"bucket": "potential_rooftop", "dual_model_resolution": "claude_veto",
                                        "gemini_cell_confidence": "0.93"}), "gemini 0.93 vs claude veto")
        self.assertEqual(review_reason({"bucket": "potential_rooftop", "dual_model_resolution": "claude_veto",
                                        "gemini_cell_confidence": "0.7"}), "")
        self.assertEqual(review_reason({"bucket": "other_or_else", "audit_reason": "possible_stealth_host"}),
                         "possible stealth host")
        self.assertEqual(review_reason({"bucket": "potential_rooftop", "holdout_reason": "consistency_disagree"}),
                         "single-model confirm, second pass disagreed")
        self.assertEqual(review_reason({"bucket": "potential_update", "dual_model_resolution": "claude_veto",
                                        "gemini_cell_confidence": "0.99"}), "")


if __name__ == "__main__":
    unittest.main()
