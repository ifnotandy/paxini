from __future__ import annotations

import math
import random
from dataclasses import dataclass

from .config import AppConfig
from .models import ContactFrame


@dataclass(frozen=True)
class ObjectModel:
    contact_angle_deg: float
    stiffness_n_per_deg: float
    maximum_force_n: float


OBJECT_MODELS = {
    "soft": ObjectModel(18.0, 0.18, 8.0),
    "semi_rigid": ObjectModel(12.0, 0.40, 10.0),
    "rigid": ObjectModel(8.0, 0.90, 14.0),
}


class SimulatedHand:
    """A deterministic, low-order plant for controller and safety testing."""

    def __init__(
        self,
        config: AppConfig,
        object_type: str = "soft",
        seed: int = 7,
        noise_n: float = 0.02,
    ) -> None:
        try:
            self.object_model = OBJECT_MODELS[object_type]
        except KeyError as exc:
            raise ValueError(
                f"unknown simulated object {object_type!r}; "
                f"choose from {sorted(OBJECT_MODELS)}"
            ) from exc
        self.config = config
        self.object_type = object_type
        self.noise_n = max(0.0, float(noise_n))
        self._random = random.Random(seed)
        self.actual_angles_deg = [0.0, 0.0, 0.0, 0.0]
        self.command_angles_deg = [0.0, 0.0, 0.0, 0.0]
        self.temperature_c = [31, 31, 31, 31]
        self.current_ma = [0, 0, 0, 0]
        self.control_mode = "MOTOR_MODE_POSITION"
        self.motor_enabled = True
        self.sensor_fault_count = 0
        self.timestamp_s = 0.0
        self._plant_time_constant_s = 0.08
        self._frozen_timestamp = False

    def command(self, angles_deg: tuple[float, float, float, float]) -> None:
        self.command_angles_deg = [float(value) for value in angles_deg]

    def set_sensor_fault(self, enabled: bool) -> None:
        self.sensor_fault_count = 1 if enabled else 0

    def freeze_frames(self, enabled: bool = True) -> None:
        self._frozen_timestamp = enabled

    def advance(self, dt_s: float) -> None:
        if dt_s <= 0:
            return
        alpha = min(1.0, dt_s / self._plant_time_constant_s)
        for index in range(4):
            error = self.command_angles_deg[index] - self.actual_angles_deg[index]
            self.actual_angles_deg[index] += alpha * error

        effective_closure = self._effective_closure(self.actual_angles_deg)
        force = self.object_model.stiffness_n_per_deg * max(
            0.0, effective_closure - self.object_model.contact_angle_deg
        )
        force = min(force, self.object_model.maximum_force_n)
        self.current_ma = [
            int(min(500, max(0, force * 18.0 + abs(v) * 1.5)))
            for v in (
                self.actual_angles_deg[0],
                self.actual_angles_deg[1],
                self.actual_angles_deg[2],
                self.actual_angles_deg[3],
            )
        ]
        for index in range(4):
            heating = 0.002 * self.current_ma[index] * dt_s
            self.temperature_c[index] = int(
                min(95, self.temperature_c[index] + heating)
            )
        if not self._frozen_timestamp:
            self.timestamp_s += dt_s

    def _effective_closure(self, angles_deg: list[float]) -> float:
        ratio = self.config.hand.distal_to_proximal_ratio
        index = (angles_deg[0] + ratio * angles_deg[1]) / (1.0 + ratio * ratio)
        middle = (angles_deg[2] + ratio * angles_deg[3]) / (1.0 + ratio * ratio)
        return 0.5 * (index + middle)

    def _finger_forces(self) -> tuple[float, float]:
        ratio = self.config.hand.distal_to_proximal_ratio
        index_s = (
            self.actual_angles_deg[0]
            + ratio * self.actual_angles_deg[1]
        ) / (1.0 + ratio * ratio)
        middle_s = (
            self.actual_angles_deg[2]
            + ratio * self.actual_angles_deg[3]
        ) / (1.0 + ratio * ratio)
        index_force = self.object_model.stiffness_n_per_deg * max(
            0.0, index_s - self.object_model.contact_angle_deg
        )
        middle_force = self.object_model.stiffness_n_per_deg * max(
            0.0, middle_s - self.object_model.contact_angle_deg - 0.8
        )
        return (
            min(index_force, self.object_model.maximum_force_n),
            min(middle_force, self.object_model.maximum_force_n),
        )

    @staticmethod
    def _point_z(force_n: float, fraction: float) -> int:
        value = max(0.0, force_n * fraction)
        return int(round(value / 0.1))

    def frame(self) -> ContactFrame:
        index_force, middle_force = self._finger_forces()
        reaction = 0.5 * (index_force + middle_force)
        point_force = [0] * 10
        point_force[0] = self._point_z(reaction, 0.55)
        point_force[1] = self._point_z(reaction, 0.45)
        point_force[2] = self._point_z(index_force, 0.55)
        point_force[3] = self._point_z(index_force, 0.45)
        point_force[4] = self._point_z(middle_force, 0.30)
        point_force[5] = self._point_z(middle_force, 0.20)
        point_force[6] = self._point_z(middle_force, 0.30)
        point_force[7] = self._point_z(middle_force, 0.20)
        point_force[8] = self._point_z(reaction, 0.70)
        point_force[9] = self._point_z(reaction, 0.30)

        vectors = []
        for value in point_force:
            noisy_n = max(0.0, value * 0.1 + self._random.gauss(0.0, self.noise_n))
            noisy_count = int(round(noisy_n / 0.1))
            vectors.append((0.0, 0.0, noisy_count / 10.0))

        return ContactFrame(
            timestamp_s=self.timestamp_s,
            force_vectors_n=tuple(vectors),
            angles_deg=tuple(self.actual_angles_deg),  # type: ignore[arg-type]
            motor_current_ma=tuple(self.current_ma),  # type: ignore[arg-type]
            motor_temperature_c=tuple(self.temperature_c),  # type: ignore[arg-type]
            sensor_fault_count=self.sensor_fault_count,
            control_mode=self.control_mode,
            motor_enabled=self.motor_enabled,
            raw_angles=tuple(int(value * 100) for value in self.actual_angles_deg),  # type: ignore[arg-type]
        )
