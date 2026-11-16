from __future__ import annotations

from dataclasses import dataclass

from .mapping import (
    ClosureLimits,
    closure_limits,
    closure_to_angles,
    measured_closure_speed,
)
from .models import clamp


@dataclass(frozen=True)
class AdmittanceResult:
    closure_s_deg: float
    velocity_deg_s: float
    angles_deg: tuple[float, float]


class FingerAdmittance:
    """First-order force admittance with measured-rate damping."""

    def __init__(
        self,
        base_angles_deg: tuple[float, float],
        previous_angles_deg: tuple[float, float],
        proximal_limits: tuple[float, float],
        distal_limits: tuple[float, float],
        closure_sign: float,
        ratio: float,
        force_to_velocity_gain: float,
        velocity_damping_gain: float,
        maximum_velocity_deg_s: float,
        force_deadband_n: float,
    ) -> None:
        if force_to_velocity_gain <= 0:
            raise ValueError("force_to_velocity_gain must be positive")
        if velocity_damping_gain < 0:
            raise ValueError("velocity_damping_gain cannot be negative")
        if maximum_velocity_deg_s <= 0:
            raise ValueError("maximum_velocity_deg_s must be positive")

        self.base_angles_deg = base_angles_deg
        self.previous_angles_deg = previous_angles_deg
        self.proximal_limits = proximal_limits
        self.distal_limits = distal_limits
        self.closure_sign = closure_sign
        self.ratio = ratio
        self.kp = force_to_velocity_gain
        self.kd = velocity_damping_gain
        self.maximum_velocity_deg_s = maximum_velocity_deg_s
        self.force_deadband_n = force_deadband_n
        self.closure_s_deg = 0.0
        self.limits: ClosureLimits = closure_limits(
            base_angles_deg,
            proximal_limits,
            distal_limits,
            closure_sign,
            ratio,
        )

    def update(
        self,
        force_n: float,
        target_force_n: float,
        current_angles_deg: tuple[float, float],
        dt_s: float,
    ) -> AdmittanceResult:
        if dt_s <= 0:
            raise ValueError("dt_s must be positive")
        error = target_force_n - force_n
        if abs(error) <= self.force_deadband_n:
            error = 0.0
        measured_speed = measured_closure_speed(
            current_angles_deg,
            self.previous_angles_deg,
            dt_s,
            self.closure_sign,
            self.ratio,
        )
        velocity = self.kp * error - self.kd * measured_speed
        velocity = clamp(
            velocity,
            -self.maximum_velocity_deg_s,
            self.maximum_velocity_deg_s,
        )
        proposed_s = self.closure_s_deg + velocity * dt_s
        bounded_s = clamp(
            proposed_s,
            self.limits.minimum_s_deg,
            self.limits.maximum_s_deg,
        )
        if bounded_s != proposed_s:
            velocity = 0.0
        self.closure_s_deg = bounded_s
        self.previous_angles_deg = current_angles_deg
        angles = closure_to_angles(
            self.base_angles_deg,
            self.closure_s_deg,
            self.closure_sign,
            self.ratio,
            self.proximal_limits,
            self.distal_limits,
        )
        return AdmittanceResult(
            closure_s_deg=self.closure_s_deg,
            velocity_deg_s=velocity,
            angles_deg=angles,
        )


class TargetForceRamp:
    def __init__(self, initial_force_n: float, maximum_rate_n_s: float) -> None:
        if maximum_rate_n_s <= 0:
            raise ValueError("maximum_rate_n_s must be positive")
        self.current_force_n = float(initial_force_n)
        self.requested_force_n = float(initial_force_n)
        self.maximum_rate_n_s = float(maximum_rate_n_s)

    def set_target(self, force_n: float) -> None:
        self.requested_force_n = max(0.0, float(force_n))

    def update(self, dt_s: float) -> float:
        delta = self.requested_force_n - self.current_force_n
        maximum_delta = self.maximum_rate_n_s * dt_s
        self.current_force_n += clamp(delta, -maximum_delta, maximum_delta)
        return self.current_force_n
