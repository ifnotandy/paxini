from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Sequence


JOINT_LABELS = (
    "index_proximal",
    "index_distal",
    "middle_proximal",
    "middle_distal",
)


class ControlState(str, Enum):
    IDLE = "idle"
    APPROACH = "approach"
    CONTROLLING = "controlling"
    HOLD = "hold"
    RELEASE = "release"
    NO_CONTACT = "no_contact"
    FORCE_LIMIT = "force_limit"
    FIXED_SIDE_FORCE_LIMIT = "fixed_side_force_limit"
    LOCAL_LIMIT_HOLD = "local_limit_hold"
    STALE_HOLD = "stale_hold"
    STALE_RELEASE = "stale_release"
    FAULT = "fault"
    STOPPED = "stopped"


class FingerPhase(str, Enum):
    APPROACH = "approach"
    FORCE = "force"
    REAPPROACH = "reapproach"
    LOCAL_RELIEF = "local_relief"
    LOCAL_LIMIT_HOLD = "local_limit_hold"
    HOLD = "hold"
    NO_CONTACT = "no_contact"


@dataclass(frozen=True)
class ContactFrame:
    """One coherent PXDex sample.

    Force vectors are expressed in sensor-local coordinates and converted to N.
    Angle order is index proximal, index distal, middle proximal, middle distal.
    """

    timestamp_s: float
    force_vectors_n: tuple[tuple[float, float, float], ...]
    angles_deg: tuple[float, float, float, float]
    motor_current_ma: tuple[int, int, int, int] = (0, 0, 0, 0)
    motor_temperature_c: tuple[int, int, int, int] = (0, 0, 0, 0)
    sensor_fault_count: int = 0
    control_mode: str = "unknown"
    motor_enabled: bool = False
    raw_angles: tuple[int, int, int, int] | None = None

    def __post_init__(self) -> None:
        if len(self.force_vectors_n) != 10:
            raise ValueError("force_vectors_n must contain 10 sensor vectors")
        if len(self.angles_deg) != 4:
            raise ValueError("angles_deg must contain 4 joint angles")
        if len(self.motor_current_ma) != 4:
            raise ValueError("motor_current_ma must contain 4 values")
        if len(self.motor_temperature_c) != 4:
            raise ValueError("motor_temperature_c must contain 4 values")


@dataclass(frozen=True)
class FingerCommand:
    phase: FingerPhase
    force_n: float
    target_force_n: float
    closure_s_deg: float
    velocity_deg_s: float
    angles_deg: tuple[float, float]
    local_force_n: float = 0.0
    local_relief: bool = False


@dataclass(frozen=True)
class ControlOutput:
    timestamp_s: float
    state: ControlState
    reason: str
    angles_deg: tuple[float, float, float, float]
    index: FingerCommand
    middle: FingerCommand
    sensor_force_n: tuple[float, ...]
    filtered_force_n: tuple[tuple[float, float, float], ...]
    temperature_c: tuple[int, int, int, int]
    current_ma: tuple[int, int, int, int]
    local_force_n: tuple[float, ...] = ()
    events: tuple[str, ...] = ()


def clamp(value: float, lower: float, upper: float) -> float:
    if lower > upper:
        raise ValueError(f"invalid clamp range: {lower} > {upper}")
    return max(lower, min(upper, value))


def clamp_angles(
    angles: Sequence[float],
    lower: Sequence[float],
    upper: Sequence[float],
) -> tuple[float, float, float, float]:
    if not (len(angles) == len(lower) == len(upper) == 4):
        raise ValueError("angles and limits must each contain four values")
    return tuple(clamp(float(value), float(lo), float(hi)) for value, lo, hi in zip(angles, lower, upper))  # type: ignore[return-value]
