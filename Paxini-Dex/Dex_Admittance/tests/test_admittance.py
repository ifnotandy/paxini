from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dex_admittance.admittance import FingerAdmittance, TargetForceRamp


class AdmittanceTests(unittest.TestCase):
    def make_admittance(self) -> FingerAdmittance:
        return FingerAdmittance(
            base_angles_deg=(20.0, 14.0),
            previous_angles_deg=(20.0, 14.0),
            proximal_limits=(0.0, 48.0),
            distal_limits=(0.0, 36.0),
            closure_sign=1.0,
            ratio=0.724,
            force_to_velocity_gain=4.0,
            velocity_damping_gain=0.2,
            maximum_velocity_deg_s=15.0,
            force_deadband_n=0.1,
        )

    def test_underforce_closes(self) -> None:
        controller = self.make_admittance()
        result = controller.update(0.0, 2.0, (20.0, 14.0), 0.025)
        self.assertGreater(result.closure_s_deg, 0.0)
        self.assertGreater(result.angles_deg[0], 20.0)

    def test_overforce_opens(self) -> None:
        controller = self.make_admittance()
        result = controller.update(3.0, 2.0, (20.0, 14.0), 0.025)
        self.assertLess(result.closure_s_deg, 0.0)
        self.assertLess(result.angles_deg[0], 20.0)

    def test_velocity_limit_is_enforced(self) -> None:
        controller = self.make_admittance()
        result = controller.update(0.0, 10.0, (20.0, 14.0), 1.0)
        self.assertLessEqual(abs(result.velocity_deg_s), 15.0)

    def test_target_force_ramp(self) -> None:
        ramp = TargetForceRamp(0.5, 1.0)
        ramp.set_target(2.0)
        self.assertAlmostEqual(ramp.update(0.5), 1.0)
        self.assertAlmostEqual(ramp.update(2.0), 2.0)


if __name__ == "__main__":
    unittest.main()
