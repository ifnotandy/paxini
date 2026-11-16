from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dex_admittance.cli import _load_baseline, build_parser


class CliTests(unittest.TestCase):
    def test_baseline_parser_has_data_root(self) -> None:
        args = build_parser().parse_args(["calibrate-baseline"])
        self.assertTrue(hasattr(args, "data_root"))

    def test_sensor_zero_requires_confirmation(self) -> None:
        args = build_parser().parse_args(["calibrate-sensors"])
        self.assertFalse(args.confirm_zero)

    def test_position_mode_requires_confirmation(self) -> None:
        args = build_parser().parse_args(["mode-position"])
        self.assertFalse(args.confirm_mode)

    def test_force_sweep_can_switch_mode_in_same_session(self) -> None:
        args = build_parser().parse_args(
            ["calibrate-force-sweep", "--switch-position-mode"]
        )
        self.assertTrue(args.switch_position_mode)

    def test_missing_baseline_has_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            missing = Path(temp_dir) / "missing.json"
            with self.assertRaisesRegex(ValueError, "baseline file not found"):
                _load_baseline(missing)

    def test_simulate_smoke(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "dex_admittance",
                "simulate",
                "--duration",
                "3",
                "--release-duration",
                "0.2",
                "--no-log",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("state=controlling", completed.stdout)

    def test_connection_requires_explicit_ack(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "dex_admittance",
                "check",
                "--port",
                "/dev/definitely-not-a-pxdex",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("--ack-connect-enables-motors", completed.stderr)


if __name__ == "__main__":
    unittest.main()
