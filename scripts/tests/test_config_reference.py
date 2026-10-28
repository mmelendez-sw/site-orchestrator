"""docs/configuration.md must match the environment variables the code reads."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gen_config_reference as ref  # noqa: E402


class ConfigReferenceTests(unittest.TestCase):
    def test_reference_is_current(self):
        current = ref.OUT.read_text(encoding="utf-8")
        self.assertEqual(
            current, ref.render(),
            "docs/configuration.md is stale: run python scripts/gen_config_reference.py",
        )

    def test_core_settings_listed(self):
        names = ref.collect()
        for name in ("APPLY", "NEARMAP_MONTHLY_BUDGET_MB", "CONNECTX_AUDIT", "SUPPLEMENTAL_IMAGERY"):
            self.assertIn(name, names)
        self.assertNotIn("STATE_ORTHO_SOURCES", names)


if __name__ == "__main__":
    unittest.main()
