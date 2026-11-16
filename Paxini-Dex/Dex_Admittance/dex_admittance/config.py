from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class HandConfig:
    side: str
    closure_sign: float
    distal_to_proximal_ratio: float
    open_angles_deg: tuple[float, float, float, float]
    safe_min_angles_deg: tuple[float, float, float, float]
    safe_max_angles_deg: tuple[float, float, float, float]
    index_sensor_indices: tuple[int, ...]
    middle_sensor_indices: tuple[int, ...]
    thumb_sensor_indices: tuple[int, ...]
    palm_sensor_indices: tuple[int, ...]
    index_sensor_weights: tuple[float, ...]
    middle_sensor_weights: tuple[float, ...]
    thumb_sensor_weights: tuple[float, ...]
    palm_sensor_weights: tuple[float, ...]


@dataclass(frozen=True)
class ControlConfig:
    period_s: float
    collection_hz: int
    force_filter_cutoff_hz: float
    force_deadband_n: float
    target_force_n: float
    target_force_ramp_n_s: float
    max_closure_velocity_deg_s: float
    prestart_open_velocity_deg_s: float
    release_velocity_deg_s: float
    activation_force_n: float
    contact_release_force_n: float
    contact_loss_hold_s: float
    reapproach_velocity_deg_s: float
    reapproach_span_deg: float
    local_control_force_limit_n: float
    local_control_release_force_n: float
    local_control_clear_duration_s: float
    local_control_cooldown_s: float
    local_control_max_relief_count: int
    local_control_relief_window_s: float


@dataclass(frozen=True)
class SafetyConfig:
    soft_force_limit_n: float
    hard_force_limit_n: float
    local_soft_force_limit_n: float
    fixed_side_hard_force_limit_n: float
    temperature_limit_c: int
    temperature_duration_s: float
    stale_freeze_s: float
    stale_release_s: float
    stale_fault_s: float


@dataclass(frozen=True)
class AdmittanceProfile:
    force_to_velocity_gain: float
    velocity_damping_gain: float
    approach_velocity_deg_s: float
    approach_limit_deg: float


@dataclass(frozen=True)
class AppConfig:
    hand: HandConfig
    control: ControlConfig
    safety: SafetyConfig
    profiles: dict[str, AdmittanceProfile] = field(default_factory=dict)

    def profile(self, name: str) -> AdmittanceProfile:
        try:
            return self.profiles[name]
        except KeyError as exc:
            raise ValueError(
                f"unknown profile {name!r}; choose from {sorted(self.profiles)}"
            ) from exc


def _tuple(prefix: str, values: Any, length: int, cast: type = float) -> tuple:
    if not isinstance(values, (list, tuple)) or len(values) != length:
        raise ValueError(f"{prefix} must contain exactly {length} values")
    return tuple(cast(value) for value in values)


def load_config(path: str | Path) -> AppConfig:
    path = Path(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("configuration root must be a mapping")

    hand_raw = data["hand"]
    control_raw = data["control"]
    safety_raw = data["safety"]
    profiles_raw = data["profiles"]

    hand = HandConfig(
        side=str(hand_raw["side"]).lower(),
        closure_sign=float(hand_raw["closure_sign"]),
        distal_to_proximal_ratio=float(hand_raw["distal_to_proximal_ratio"]),
        open_angles_deg=_tuple("open_angles_deg", hand_raw["open_angles_deg"], 4),
        safe_min_angles_deg=_tuple(
            "safe_min_angles_deg", hand_raw["safe_min_angles_deg"], 4
        ),
        safe_max_angles_deg=_tuple(
            "safe_max_angles_deg", hand_raw["safe_max_angles_deg"], 4
        ),
        index_sensor_indices=_tuple(
            "index_sensor_indices", hand_raw["index_sensor_indices"], 2, int
        ),
        middle_sensor_indices=_tuple(
            "middle_sensor_indices", hand_raw["middle_sensor_indices"], 4, int
        ),
        thumb_sensor_indices=_tuple(
            "thumb_sensor_indices", hand_raw["thumb_sensor_indices"], 2, int
        ),
        palm_sensor_indices=_tuple(
            "palm_sensor_indices", hand_raw["palm_sensor_indices"], 2, int
        ),
        index_sensor_weights=_tuple(
            "index_sensor_weights", hand_raw["index_sensor_weights"], 2
        ),
        middle_sensor_weights=_tuple(
            "middle_sensor_weights", hand_raw["middle_sensor_weights"], 4
        ),
        thumb_sensor_weights=_tuple(
            "thumb_sensor_weights", hand_raw["thumb_sensor_weights"], 2
        ),
        palm_sensor_weights=_tuple(
            "palm_sensor_weights", hand_raw["palm_sensor_weights"], 2
        ),
    )

    control = ControlConfig(
        period_s=float(control_raw["period_s"]),
        collection_hz=int(control_raw["collection_hz"]),
        force_filter_cutoff_hz=float(control_raw["force_filter_cutoff_hz"]),
        force_deadband_n=float(control_raw["force_deadband_n"]),
        target_force_n=float(control_raw["target_force_n"]),
        target_force_ramp_n_s=float(control_raw["target_force_ramp_n_s"]),
        max_closure_velocity_deg_s=float(
            control_raw["max_closure_velocity_deg_s"]
        ),
        prestart_open_velocity_deg_s=float(
            control_raw["prestart_open_velocity_deg_s"]
        ),
        release_velocity_deg_s=float(control_raw["release_velocity_deg_s"]),
        activation_force_n=float(control_raw["activation_force_n"]),
        contact_release_force_n=float(control_raw["contact_release_force_n"]),
        contact_loss_hold_s=float(control_raw["contact_loss_hold_s"]),
        reapproach_velocity_deg_s=float(
            control_raw["reapproach_velocity_deg_s"]
        ),
        reapproach_span_deg=float(control_raw["reapproach_span_deg"]),
        local_control_force_limit_n=float(
            control_raw["local_control_force_limit_n"]
        ),
        local_control_release_force_n=float(
            control_raw["local_control_release_force_n"]
        ),
        local_control_clear_duration_s=float(
            control_raw["local_control_clear_duration_s"]
        ),
        local_control_cooldown_s=float(
            control_raw["local_control_cooldown_s"]
        ),
        local_control_max_relief_count=int(
            control_raw["local_control_max_relief_count"]
        ),
        local_control_relief_window_s=float(
            control_raw["local_control_relief_window_s"]
        ),
    )

    safety = SafetyConfig(
        soft_force_limit_n=float(safety_raw["soft_force_limit_n"]),
        hard_force_limit_n=float(safety_raw["hard_force_limit_n"]),
        local_soft_force_limit_n=float(
            safety_raw["local_soft_force_limit_n"]
        ),
        fixed_side_hard_force_limit_n=float(
            safety_raw["fixed_side_hard_force_limit_n"]
        ),
        temperature_limit_c=int(safety_raw["temperature_limit_c"]),
        temperature_duration_s=float(safety_raw["temperature_duration_s"]),
        stale_freeze_s=float(safety_raw["stale_freeze_s"]),
        stale_release_s=float(safety_raw["stale_release_s"]),
        stale_fault_s=float(safety_raw["stale_fault_s"]),
    )

    profiles = {
        str(name): AdmittanceProfile(
            force_to_velocity_gain=float(raw["force_to_velocity_gain"]),
            velocity_damping_gain=float(raw["velocity_damping_gain"]),
            approach_velocity_deg_s=float(raw["approach_velocity_deg_s"]),
            approach_limit_deg=float(raw["approach_limit_deg"]),
        )
        for name, raw in profiles_raw.items()
    }
    _validate(hand, control, safety, profiles)
    return AppConfig(hand=hand, control=control, safety=safety, profiles=profiles)


def _validate(
    hand: HandConfig,
    control: ControlConfig,
    safety: SafetyConfig,
    profiles: dict[str, AdmittanceProfile],
) -> None:
    if hand.side not in {"left", "right"}:
        raise ValueError("hand.side must be left or right")
    if abs(hand.closure_sign) != 1.0:
        raise ValueError("hand.closure_sign must be +1 or -1")
    if not 0 < hand.distal_to_proximal_ratio <= 1.5:
        raise ValueError("distal_to_proximal_ratio must be in (0, 1.5]")
    for index in range(4):
        if hand.safe_min_angles_deg[index] > hand.safe_max_angles_deg[index]:
            raise ValueError(f"invalid safe angle range at joint {index}")
    if control.period_s <= 0:
        raise ValueError("control.period_s must be positive")
    if control.collection_hz <= 0:
        raise ValueError("collection_hz must be positive")
    if control.prestart_open_velocity_deg_s <= 0:
        raise ValueError("prestart_open_velocity_deg_s must be positive")
    if control.release_velocity_deg_s <= 0:
        raise ValueError("release_velocity_deg_s must be positive")
    if not 0 <= control.activation_force_n < safety.hard_force_limit_n:
        raise ValueError("activation force must be below hard force limit")
    if not (
        0
        <= control.contact_release_force_n
        <= control.activation_force_n
        < safety.hard_force_limit_n
    ):
        raise ValueError("invalid contact hysteresis thresholds")
    if not (
        0
        < control.local_control_release_force_n
        < control.local_control_force_limit_n
        < safety.hard_force_limit_n
    ):
        raise ValueError("invalid local force-control hysteresis thresholds")
    if control.local_control_clear_duration_s <= 0:
        raise ValueError("local_control_clear_duration_s must be positive")
    if control.local_control_cooldown_s <= 0:
        raise ValueError("local_control_cooldown_s must be positive")
    if control.local_control_max_relief_count <= 0:
        raise ValueError("local_control_max_relief_count must be positive")
    if control.local_control_relief_window_s <= 0:
        raise ValueError("local_control_relief_window_s must be positive")
    if safety.fixed_side_hard_force_limit_n <= 0:
        raise ValueError("fixed_side_hard_force_limit_n must be positive")
    if any(weight <= 0 for weight in (
        *hand.index_sensor_weights,
        *hand.middle_sensor_weights,
        *hand.thumb_sensor_weights,
        *hand.palm_sensor_weights,
    )):
        raise ValueError("all tactile fusion weights must be positive")
    if not (
        safety.stale_freeze_s
        < safety.stale_release_s
        < safety.stale_fault_s
    ):
        raise ValueError("stale thresholds must increase")
    if not profiles:
        raise ValueError("at least one admittance profile is required")
