from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dex_admittance.calibration import clamp_joint_step
from dex_admittance.config import load_config


class CalibrationTests(unittest.TestCase):
    def test_joint_step_is_clamped(self) -> None:
        config = load_config(ROOT / "config" / "default.yaml")
        target = clamp_joint_step((47.0, 10.0, 0.0, 0.0), 0, 4.0, config)
        self.assertEqual(target[0], 48.0)
        self.assertEqual(target[1:], (10.0, 0.0, 0.0))

    def test_invalid_joint_is_rejected(self) -> None:
        config = load_config(ROOT / "config" / "default.yaml")
        with self.assertRaises(ValueError):
            clamp_joint_step((0.0, 0.0, 0.0, 0.0), 4, 1.0, config)


if __name__ == "__main__":
    unittest.main()
