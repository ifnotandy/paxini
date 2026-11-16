from __future__ import annotations

from dataclasses import dataclass

from .models import clamp


@dataclass(frozen=True)
class ClosureLimits:
    minimum_s_deg: float
    maximum_s_deg: float


def joint_indices(finger: str) -> tuple[int, int]:
    if finger == "index":
        return (0, 1)
    if finger == "middle":
        return (2, 3)
    raise ValueError(f"unknown finger: {finger}")


def closure_limits(
    base_angles_deg: tuple[float, float],
    proximal_limits: tuple[float, float],
    distal_limits: tuple[float, float],
    closure_sign: float,
    ratio: float,
) -> ClosureLimits:
    """Return feasible s values for q_pip=base+s and q_dip=base+ratio*s."""

    bounds: list[float] = []
    for base, sign_coefficient, lower, upper in (
        (base_angles_deg[0], closure_sign, proximal_limits[0], proximal_limits[1]),
        (
            base_angles_deg[1],
            closure_sign * ratio,
            distal_limits[0],
            distal_limits[1],
        ),
    ):
        low = (lower - base) / sign_coefficient
        high = (upper - base) / sign_coefficient
        bounds.extend((low, high))
    return ClosureLimits(min(bounds), max(bounds))


def closure_to_angles(
    base_angles_deg: tuple[float, float],
    closure_s_deg: float,
    closure_sign: float,
    ratio: float,
    proximal_limits: tuple[float, float],
    distal_limits: tuple[float, float],
) -> tuple[float, float]:
    return (
        clamp(
            base_angles_deg[0] + closure_sign * closure_s_deg,
            proximal_limits[0],
            proximal_limits[1],
        ),
        clamp(
            base_angles_deg[1] + closure_sign * ratio * closure_s_deg,
            distal_limits[0],
            distal_limits[1],
        ),
    )


def measured_closure_speed(
    current_angles_deg: tuple[float, float],
    previous_angles_deg: tuple[float, float],
    dt_s: float,
    closure_sign: float,
    ratio: float,
) -> float:
    if dt_s <= 0:
        raise ValueError("dt_s must be positive")
    proximal = closure_sign * (
        current_angles_deg[0] - previous_angles_deg[0]
    ) / dt_s
    distal = closure_sign * (
        current_angles_deg[1] - previous_angles_deg[1]
    ) / (ratio * dt_s)
    return 0.5 * (proximal + distal)
