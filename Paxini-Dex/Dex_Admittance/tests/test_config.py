from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dex_admittance.config import load_config


class ConfigTests(unittest.TestCase):
    def test_default_config_loads(self) -> None:
        config = load_config(ROOT / "config" / "default.yaml")
        self.assertEqual(config.hand.side, "right")
        self.assertEqual(config.control.collection_hz, 60)
        self.assertIn("soft", config.profiles)
        self.assertLess(
            config.control.activation_force_n,
            config.safety.hard_force_limit_n,
        )

    def test_unknown_profile_is_rejected(self) -> None:
        config = load_config(ROOT / "config" / "default.yaml")
        with self.assertRaises(ValueError):
            config.profile("missing")


if __name__ == "__main__":
    unittest.main()
