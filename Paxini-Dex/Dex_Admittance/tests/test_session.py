from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dex_admittance.config import load_config
from dex_admittance.engine import AdmittanceEngine
from dex_admittance.session import SessionLogger
from dex_admittance.simulator import SimulatedHand


class SessionTests(unittest.TestCase):
    def test_session_files_are_created(self) -> None:
        config = load_config(ROOT / "config" / "default.yaml")
        plant = SimulatedHand(config, "soft", noise_n=0.0)
        engine = AdmittanceEngine(config, "soft")
        initial = plant.frame()
        output = engine.start(initial, 0.0)
        with tempfile.TemporaryDirectory() as temp_dir:
            with SessionLogger(
                temp_dir,
                {"operator": "test"},
                config,
                object_name="sponge",
                target_force_n=2.0,
            ) as logger:
                logger.record(initial, output)
                directory = logger.directory
            self.assertTrue((directory / "metadata.json").is_file())
            self.assertTrue((directory / "samples.csv").is_file())
            self.assertTrue((directory / "events.csv").is_file())
            self.assertTrue((directory / "config.yaml").is_file())


if __name__ == "__main__":
    unittest.main()
