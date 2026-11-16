from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dex_admittance.config import load_config
from dex_admittance.engine import AdmittanceEngine
from dex_admittance.models import ContactFrame, ControlState, FingerPhase
from dex_admittance.simulator import SimulatedHand


def frame(
    timestamp_s: float,
    *,
    index_force_n: float = 0.0,
    middle_force_n: float = 0.0,
    angles_deg: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0),
    sensor_fault_count: int = 0,
    temperatures: tuple[int, int, int, int] = (30, 30, 30, 30),
    index_vector: tuple[float, float, float] | None = None,
    middle_vector: tuple[float, float, float] | None = None,
    thumb_vector: tuple[float, float, float] | None = None,
    palm_vector: tuple[float, float, float] | None = None,
) -> ContactFrame:
    vectors = [(0.0, 0.0, 0.0) for _ in range(10)]
    vectors[2] = index_vector or (0.0, 0.0, index_force_n)
    vectors[4] = middle_vector or (0.0, 0.0, middle_force_n)
    vectors[0] = thumb_vector or (0.0, 0.0, 0.0)
    vectors[8] = palm_vector or (0.0, 0.0, 0.0)
    return ContactFrame(
        timestamp_s=timestamp_s,
        force_vectors_n=tuple(vectors),
        angles_deg=angles_deg,
        motor_temperature_c=temperatures,
        sensor_fault_count=sensor_fault_count,
        control_mode="MOTOR_MODE_POSITION",
        motor_enabled=True,
    )


class EngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config(ROOT / "config" / "default.yaml")

    def test_contact_switches_to_force_phase(self) -> None:
        engine = AdmittanceEngine(self.config, "soft")
        initial = frame(0.0)
        engine.start(initial, 0.0)
        contact = frame(
            0.025,
            index_force_n=0.5,
            middle_force_n=0.5,
            angles_deg=(20.0, 14.0, 20.0, 14.0),
        )
        output = engine.step(contact, 0.025)
        self.assertEqual(output.index.phase, FingerPhase.FORCE)
        self.assertEqual(output.middle.phase, FingerPhase.FORCE)
        self.assertEqual(output.state, ControlState.CONTROLLING)

    def test_hard_force_limit_releases(self) -> None:
        engine = AdmittanceEngine(self.config, "soft")
        engine.start(frame(0.0), 0.0)
        output = engine.step(
            frame(0.025, index_force_n=5.0, angles_deg=(20.0, 14.0, 0.0, 0.0)),
            0.025,
        )
        self.assertEqual(output.state, ControlState.FORCE_LIMIT)
        self.assertLess(output.angles_deg[0], 20.0)

    def test_stale_data_releases_after_300ms(self) -> None:
        engine = AdmittanceEngine(self.config, "soft")
        engine.start(frame(0.0), 0.0)
        output = engine.step(frame(0.0, angles_deg=(20.0, 14.0, 0.0, 0.0)), 0.35)
        self.assertEqual(output.state, ControlState.STALE_RELEASE)
        self.assertLess(output.angles_deg[0], 20.0)

    def test_stale_data_freezes_between_100_and_300ms(self) -> None:
        engine = AdmittanceEngine(self.config, "soft")
        engine.start(frame(0.0, angles_deg=(20.0, 14.0, 0.0, 0.0)), 0.0)
        output = engine.step(frame(0.0, angles_deg=(20.0, 14.0, 0.0, 0.0)), 0.15)
        self.assertEqual(output.state, ControlState.STALE_HOLD)
        self.assertEqual(output.angles_deg[0], 20.0)

    def test_sensor_fault_enters_terminal_fault(self) -> None:
        engine = AdmittanceEngine(self.config, "soft")
        engine.start(frame(0.0, angles_deg=(20.0, 14.0, 20.0, 14.0)), 0.0)
        output = engine.step(frame(0.025, sensor_fault_count=1), 0.025)
        self.assertEqual(output.state, ControlState.FAULT)
        self.assertIn("sensor_fault", output.events)
        self.assertEqual(output.angles_deg, self.config.hand.open_angles_deg)

    def test_target_force_must_stay_below_hard_limit(self) -> None:
        engine = AdmittanceEngine(self.config, "soft")
        with self.assertRaises(ValueError):
            engine.set_target_force(self.config.safety.hard_force_limit_n)

    def test_fixed_side_vector_limit_stops_without_opening(self) -> None:
        engine = AdmittanceEngine(self.config, "soft")
        engine.start(frame(0.0, angles_deg=(20.0, 14.0, 0.0, 0.0)), 0.0)
        output = engine.step(
            frame(
                0.025,
                angles_deg=(20.0, 14.0, 0.0, 0.0),
                palm_vector=(5.1, 0.0, 0.0),
            ),
            0.025,
        )
        self.assertEqual(output.state, ControlState.FIXED_SIDE_FORCE_LIMIT)
        self.assertEqual(output.angles_deg[0], 20.0)
        self.assertIn("fixed_side_force_limit", output.events)

    def test_active_local_vector_limit_causes_relief(self) -> None:
        engine = AdmittanceEngine(self.config, "soft")
        engine.start(frame(0.0), 0.0)
        contact = frame(
            0.025,
            index_force_n=0.4,
            middle_force_n=0.4,
            angles_deg=(18.0, 12.0, 18.0, 12.0),
        )
        engine.step(contact, 0.025)
        output = engine.step(
            frame(
                0.050,
                index_vector=(1.3, 0.0, 0.4),
                middle_force_n=0.4,
                angles_deg=(18.0, 12.0, 18.0, 12.0),
            ),
            0.050,
        )
        self.assertEqual(output.index.phase, FingerPhase.LOCAL_RELIEF)
        self.assertTrue(output.index.local_relief)
        self.assertIn("index_local_relief", output.events)

    def test_soft_object_converges(self) -> None:
        engine = AdmittanceEngine(self.config, "soft", target_force_n=1.2)
        plant = SimulatedHand(self.config, "soft", noise_n=0.0)
        first = plant.frame()
        output = engine.start(first, 0.0)
        for step_index in range(1, 1201):
            plant.command(output.angles_deg)
            plant.advance(self.config.control.period_s)
            current = plant.frame()
            now = step_index * self.config.control.period_s
            output = engine.step(plant.frame(), now)
        self.assertEqual(output.state, ControlState.CONTROLLING)
        self.assertAlmostEqual(output.index.force_n, 1.2, delta=0.25)
        self.assertAlmostEqual(output.middle.force_n, 1.2, delta=0.25)
        self.assertNotEqual(
            round(output.angles_deg[0], 3),
            round(output.angles_deg[2], 3),
        )


if __name__ == "__main__":
    unittest.main()
