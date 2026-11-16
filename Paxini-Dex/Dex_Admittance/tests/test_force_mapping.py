from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dex_admittance.force import ForceEstimator
from dex_admittance.mapping import (
    closure_limits,
    closure_to_angles,
    measured_closure_speed,
)


class ForceTests(unittest.TestCase):
    def test_force_is_grouped_by_finger(self) -> None:
        estimator = ForceEstimator(
            (2, 3),
            (4, 5, 6, 7),
            (0, 1),
            (8, 9),
            cutoff_hz=8.0,
        )
        vectors = [(0.0, 0.0, 0.0) for _ in range(10)]
        vectors[2] = (0.0, 0.0, 1.0)
        vectors[3] = (0.0, 0.0, 0.5)
        vectors[4] = (0.0, 0.0, 0.4)
        vectors[5] = (0.0, 0.0, 0.6)
        result = estimator.update(vectors, 1.0)
        self.assertAlmostEqual(result.index_n, 1.5)
        self.assertAlmostEqual(result.middle_n, 1.0)

    def test_negative_force_is_not_counted_as_compression(self) -> None:
        estimator = ForceEstimator(
            (2, 3), (4, 5, 6, 7), (0, 1), (8, 9), cutoff_hz=8.0
        )
        vectors = [(0.0, 0.0, -1.0) for _ in range(10)]
        result = estimator.update(vectors, 1.0)
        self.assertEqual(result.index_n, 0.0)

    def test_sensor_weights_and_local_vector_magnitude(self) -> None:
        estimator = ForceEstimator(
            (2, 3),
            (4, 5, 6, 7),
            (0, 1),
            (8, 9),
            cutoff_hz=8.0,
            index_weights=(1.0, 0.41),
            middle_weights=(1.0, 0.41, 1.0, 0.41),
        )
        vectors = [(0.0, 0.0, 0.0) for _ in range(10)]
        vectors[2] = (1.3, 0.0, 0.5)
        vectors[3] = (0.0, 0.0, 1.0)
        result = estimator.update(vectors, 1.0)
        self.assertAlmostEqual(result.index_n, 0.91)
        self.assertAlmostEqual(result.index_local_max_n, (1.3**2 + 0.5**2) ** 0.5)
        raw = estimator.raw_local_magnitudes(vectors)
        self.assertAlmostEqual(raw[2], (1.3**2 + 0.5**2) ** 0.5)


class MappingTests(unittest.TestCase):
    def test_closure_maps_to_both_joints(self) -> None:
        angles = closure_to_angles(
            (10.0, 8.0),
            10.0,
            closure_sign=1.0,
            ratio=0.724,
            proximal_limits=(0.0, 48.0),
            distal_limits=(0.0, 36.0),
        )
        self.assertAlmostEqual(angles[0], 20.0)
        self.assertAlmostEqual(angles[1], 15.24)

    def test_closure_limits_include_open_direction(self) -> None:
        limits = closure_limits(
            (20.0, 15.0),
            (0.0, 48.0),
            (0.0, 36.0),
            closure_sign=1.0,
            ratio=0.724,
        )
        self.assertLess(limits.minimum_s_deg, 0.0)
        self.assertGreater(limits.maximum_s_deg, 0.0)

    def test_measured_closure_speed(self) -> None:
        speed = measured_closure_speed(
            (10.0, 7.24),
            (9.0, 6.516),
            dt_s=0.1,
            closure_sign=1.0,
            ratio=0.724,
        )
        self.assertAlmostEqual(speed, 10.0)


if __name__ == "__main__":
    unittest.main()
