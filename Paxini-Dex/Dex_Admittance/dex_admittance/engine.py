from __future__ import annotations

from dataclasses import dataclass

from .admittance import FingerAdmittance, TargetForceRamp
from .config import AppConfig, AdmittanceProfile
from .force import FilteredForce, ForceEstimator
from .mapping import closure_to_angles
from .models import (
    ContactFrame,
    ControlOutput,
    ControlState,
    FingerCommand,
    FingerPhase,
    clamp,
    clamp_angles,
)


@dataclass
class _FingerRuntime:
    phase: FingerPhase
    base_angles_deg: tuple[float, float]
    command_angles_deg: tuple[float, float]
    previous_actual_angles_deg: tuple[float, float]
    progress_deg: float = 0.0
    lost_for_s: float = 0.0
    admittance: FingerAdmittance | None = None
    velocity_deg_s: float = 0.0
    closure_s_deg: float = 0.0
    local_relief: bool = False
    local_clear_for_s: float = 0.0
    local_cooldown_s: float = 0.0
    local_relief_count: int = 0
    last_local_relief_s: float | None = None
    hold_after_local_relief: bool = False


class AdmittanceEngine:
    """Two independent finger force-admittance loops in position mode."""

    def __init__(
        self,
        config: AppConfig,
        profile_name: str,
        target_force_n: float | None = None,
        baseline_vectors_n: tuple[tuple[float, float, float], ...] | None = None,
    ) -> None:
        self.config = config
        self.profile_name = profile_name
        self.profile: AdmittanceProfile = config.profile(profile_name)
        self.requested_force_n = (
            config.control.target_force_n
            if target_force_n is None
            else float(target_force_n)
        )
        self.baseline_vectors_n = baseline_vectors_n
        if not 0 <= self.requested_force_n < config.safety.hard_force_limit_n:
            raise ValueError("target force must be non-negative and below hard limit")

        self.state = ControlState.IDLE
        self.reason = "not started"
        self._force_estimator: ForceEstimator | None = None
        self._last_filtered: FilteredForce | None = None
        self._target_ramp = TargetForceRamp(
            min(self.requested_force_n, 0.5),
            config.control.target_force_ramp_n_s,
        )
        self._target_ramp.set_target(self.requested_force_n)
        self._fingers: dict[str, _FingerRuntime] = {}
        self._angles_cmd = config.hand.open_angles_deg
        self._last_output: ControlOutput | None = None
        self._last_now_s: float | None = None
        self._temperature_high_for_s = 0.0
        self._previous_state = ControlState.IDLE
        self._event_keys: set[str] = set()
        self._hard_limit_latched = False
        self._fixed_side_limit_latched = False
        self._soft_warning_latched = False

    @property
    def active(self) -> bool:
        return self.state not in {
            ControlState.IDLE,
            ControlState.STOPPED,
            ControlState.FAULT,
        }

    def start(self, frame: ContactFrame, now_s: float | None = None) -> ControlOutput:
        now = frame.timestamp_s if now_s is None else float(now_s)
        self._force_estimator = ForceEstimator(
            self.config.hand.index_sensor_indices,
            self.config.hand.middle_sensor_indices,
            self.config.hand.thumb_sensor_indices,
            self.config.hand.palm_sensor_indices,
            self.config.control.force_filter_cutoff_hz,
            baseline_vectors_n=self.baseline_vectors_n,
            index_weights=self.config.hand.index_sensor_weights,
            middle_weights=self.config.hand.middle_sensor_weights,
            thumb_weights=self.config.hand.thumb_sensor_weights,
            palm_weights=self.config.hand.palm_sensor_weights,
        )
        self._last_filtered = self._force_estimator.update(
            frame.force_vectors_n, frame.timestamp_s
        )
        self._angles_cmd = clamp_angles(
            frame.angles_deg,
            self.config.hand.safe_min_angles_deg,
            self.config.hand.safe_max_angles_deg,
        )
        self._fingers = {
            "index": self._new_approach_runtime(frame.angles_deg[0:2]),
            "middle": self._new_approach_runtime(frame.angles_deg[2:4]),
        }
        self.state = ControlState.APPROACH
        self.reason = f"approaching with profile={self.profile_name}"
        self._last_now_s = now
        self._event_keys.clear()
        self._hard_limit_latched = False
        self._fixed_side_limit_latched = False
        self._soft_warning_latched = False
        self._temperature_high_for_s = 0.0
        output = self._make_output(
            frame,
            self._last_filtered,
            now,
            events=("control_started",),
        )
        self._last_output = output
        return output

    def hold(self, frame: ContactFrame, now_s: float) -> ControlOutput:
        if not self.active:
            raise RuntimeError(f"cannot hold while state={self.state.value}")
        self._previous_state = self.state
        self.state = ControlState.HOLD
        self.reason = "operator hold"
        for runtime in self._fingers.values():
            runtime.phase = FingerPhase.HOLD
        filtered = self._last_filtered or self._force_estimator.update(
            frame.force_vectors_n, frame.timestamp_s
        )
        self._last_now_s = now_s
        return self._make_output(frame, filtered, now_s, events=("operator_hold",))

    def resume(self, frame: ContactFrame, now_s: float) -> ControlOutput:
        if self.state != ControlState.HOLD:
            raise RuntimeError("resume is only valid from hold")
        for name, runtime in self._fingers.items():
            runtime.phase = (
                FingerPhase.FORCE
                if runtime.admittance is not None
                else FingerPhase.REAPPROACH
            )
            runtime.progress_deg = 0.0
            if runtime.phase == FingerPhase.REAPPROACH:
                runtime.base_angles_deg = runtime.command_angles_deg
        self.state = ControlState.CONTROLLING
        self.reason = "resumed after operator hold"
        return self.step(frame, now_s)

    def release(self, frame: ContactFrame, now_s: float) -> ControlOutput:
        self._fixed_side_limit_latched = False
        self.state = ControlState.RELEASE
        self.reason = "operator release"
        for runtime in self._fingers.values():
            runtime.phase = FingerPhase.HOLD
        filtered = self._last_filtered or self._force_estimator.update(
            frame.force_vectors_n, frame.timestamp_s
        )
        self._last_now_s = now_s
        return self._make_output(frame, filtered, now_s, events=("operator_release",))

    def stop(self, frame: ContactFrame, now_s: float) -> ControlOutput:
        filtered = self._last_filtered or self._force_estimator.update(
            frame.force_vectors_n, frame.timestamp_s
        )
        self.state = ControlState.STOPPED
        self.reason = "operator stop"
        self._last_now_s = now_s
        output = self._make_output(frame, filtered, now_s, events=("operator_stop",))
        self._last_output = output
        return output

    def set_target_force(self, force_n: float) -> None:
        if not 0 <= force_n < self.config.safety.hard_force_limit_n:
            raise ValueError("target force must be non-negative and below hard limit")
        self.requested_force_n = float(force_n)
        self._target_ramp.set_target(force_n)
        for runtime in self._fingers.values():
            runtime.local_relief_count = 0
            runtime.last_local_relief_s = None
            runtime.hold_after_local_relief = False
            if runtime.phase == FingerPhase.LOCAL_LIMIT_HOLD:
                runtime.local_relief = False
                runtime.local_cooldown_s = 0.0
                runtime.phase = FingerPhase.REAPPROACH
                runtime.progress_deg = 0.0
        if self.state == ControlState.LOCAL_LIMIT_HOLD:
            self.state = ControlState.CONTROLLING
            self.reason = "target changed; local force search resumed"

    def step(
        self,
        frame: ContactFrame,
        now_s: float,
        dt_s: float | None = None,
    ) -> ControlOutput:
        if self._force_estimator is None or self._last_filtered is None:
            raise RuntimeError("start() must be called before step()")
        if self.state == ControlState.STOPPED:
            if self._last_output is None:
                raise RuntimeError("no output available")
            return self._last_output

        now = float(now_s)
        dt = (
            self.config.control.period_s
            if dt_s is None
            else clamp(float(dt_s), 1e-4, 0.1)
        )
        events: list[str] = []
        age_s = max(0.0, now - frame.timestamp_s)

        if frame.sensor_fault_count > 0:
            output = self._enter_fault(
                frame, now, "sensor_fault", events=("sensor_fault",)
            )
            self._last_output = output
            return output

        if age_s > self.config.safety.stale_fault_s:
            output = self._enter_fault(
                frame, now, f"fatal stale frame ({age_s:.3f}s)", events=("stale_fault",)
            )
            self._last_output = output
            return output

        last_timestamp = self._force_estimator.last_timestamp
        if last_timestamp is None or frame.timestamp_s > last_timestamp:
            self._last_filtered = self._force_estimator.update(
                frame.force_vectors_n, frame.timestamp_s
            )
        filtered = self._last_filtered
        raw_local_force = self._force_estimator.raw_local_magnitudes(
            frame.force_vectors_n
        )

        fixed_side_peak = max(
            max(raw_local_force[index] for index in self.config.hand.thumb_sensor_indices),
            max(raw_local_force[index] for index in self.config.hand.palm_sensor_indices),
        )
        if (
            fixed_side_peak >= self.config.safety.fixed_side_hard_force_limit_n
            and not self._fixed_side_limit_latched
        ):
            self._fixed_side_limit_latched = True
            events.append("fixed_side_force_limit")
        if self._fixed_side_limit_latched:
            self.state = ControlState.FIXED_SIDE_FORCE_LIMIT
            self.reason = (
                "fixed-side local force limit latched at "
                f"{fixed_side_peak:.2f} N; motion stopped"
            )
            hold_angles = clamp_angles(
                frame.angles_deg,
                self.config.hand.safe_min_angles_deg,
                self.config.hand.safe_max_angles_deg,
            )
            self._angles_cmd = hold_angles
            self._fingers["index"].command_angles_deg = hold_angles[0:2]
            self._fingers["middle"].command_angles_deg = hold_angles[2:4]
            output = self._make_output(frame, filtered, now, events=tuple(events))
            self._last_output = output
            return output

        self._update_temperature(frame.motor_temperature_c, dt, events)
        if self._temperature_high_for_s >= self.config.safety.temperature_duration_s:
            output = self._enter_fault(
                frame, now, "motor temperature limit", events=("temperature_fault",)
            )
            self._last_output = output
            return output

        raw_index_force, raw_middle_force = self._raw_finger_forces(frame)
        peak_filtered_force = max(filtered.index_n, filtered.middle_n)
        peak_finger_force = max(
            peak_filtered_force, raw_index_force, raw_middle_force
        )
        if (
            peak_finger_force > self.config.safety.hard_force_limit_n
            and not self._hard_limit_latched
        ):
            self._hard_limit_latched = True
            events.append("hard_force_limit")
        if self._hard_limit_latched:
            self.state = ControlState.FORCE_LIMIT
            self.reason = (
                f"hard force limit latched at {peak_finger_force:.2f} N"
            )
            output = self._release_step(frame, filtered, now, dt, events)
            self._last_output = output
            return output

        if peak_finger_force >= self.config.safety.soft_force_limit_n:
            if not self._soft_warning_latched:
                events.append("soft_force_limit")
            self._soft_warning_latched = True

        baseline = self.baseline_vectors_n or ((0.0, 0.0, 0.0),) * 10
        peak_local_force = max(
            max(
                0.0,
                float(vector[2]) - float(baseline[index][2]),
            )
            for index, vector in enumerate(frame.force_vectors_n)
        )
        if (
            peak_local_force >= self.config.safety.local_soft_force_limit_n
            and "local_soft_force_limit" not in self._event_keys
        ):
            events.append("local_soft_force_limit")
            self._event_keys.add("local_soft_force_limit")

        if age_s > self.config.safety.stale_release_s:
            if self.state != ControlState.STALE_RELEASE:
                events.append("stale_release")
            self.state = ControlState.STALE_RELEASE
            self.reason = f"stale force data for {age_s:.3f}s"
            output = self._release_step(frame, filtered, now, dt, events)
            self._last_output = output
            return output

        if age_s > self.config.safety.stale_freeze_s:
            self.state = ControlState.STALE_HOLD
            self.reason = f"stale force data for {age_s:.3f}s; target frozen"
            output = self._make_output(frame, filtered, now, events=tuple(events))
            self._last_output = output
            return output

        if self.state == ControlState.HOLD:
            output = self._make_output(frame, filtered, now, events=tuple(events))
            self._last_output = output
            return output

        if self.state == ControlState.RELEASE or self.state == ControlState.STALE_RELEASE:
            output = self._release_step(frame, filtered, now, dt, events)
            self._last_output = output
            return output

        target_force = self._target_ramp.update(dt)
        if self.state == ControlState.APPROACH or self.state == ControlState.CONTROLLING:
            self._advance_fingers(
                frame,
                filtered,
                raw_local_force,
                (raw_index_force, raw_middle_force),
                now,
                dt,
                target_force,
                events,
            )
            self._state_from_fingers(frame, events)
        output = self._make_output(frame, filtered, now, events=tuple(events))
        self._last_output = output
        self._last_now_s = now
        return output

    def _new_approach_runtime(
        self, actual_angles_deg: tuple[float, float]
    ) -> _FingerRuntime:
        base = (float(actual_angles_deg[0]), float(actual_angles_deg[1]))
        return _FingerRuntime(
            phase=FingerPhase.APPROACH,
            base_angles_deg=base,
            command_angles_deg=base,
            previous_actual_angles_deg=base,
        )

    def _advance_fingers(
        self,
        frame: ContactFrame,
        filtered: FilteredForce,
        raw_local_force: tuple[float, ...],
        raw_force_n: tuple[float, float],
        now_s: float,
        dt_s: float,
        target_force_n: float,
        events: list[str],
    ) -> None:
        for name, force_n, raw_force, actual, sensor_indices in (
            (
                "index",
                filtered.index_n,
                raw_force_n[0],
                frame.angles_deg[0:2],
                self.config.hand.index_sensor_indices,
            ),
            (
                "middle",
                filtered.middle_n,
                raw_force_n[1],
                frame.angles_deg[2:4],
                self.config.hand.middle_sensor_indices,
            ),
        ):
            runtime = self._fingers[name]
            raw_local_max_force = max(
                raw_local_force[index] for index in sensor_indices
            )
            filtered_local_max_force = (
                filtered.index_local_max_n
                if name == "index"
                else filtered.middle_local_max_n
            )
            if (
                not runtime.local_relief
                and raw_local_max_force
                >= self.config.control.local_control_force_limit_n
            ):
                runtime.local_relief = True
                runtime.phase = FingerPhase.LOCAL_RELIEF
                runtime.local_clear_for_s = 0.0
                if (
                    runtime.last_local_relief_s is None
                    or now_s - runtime.last_local_relief_s
                    > self.config.control.local_control_relief_window_s
                ):
                    runtime.local_relief_count = 1
                else:
                    runtime.local_relief_count += 1
                runtime.last_local_relief_s = now_s
                runtime.hold_after_local_relief = (
                    runtime.local_relief_count
                    >= self.config.control.local_control_max_relief_count
                )
                events.append(f"{name}_local_relief")
            if runtime.phase == FingerPhase.LOCAL_RELIEF:
                if (
                    filtered_local_max_force
                    <= self.config.control.local_control_release_force_n
                ):
                    runtime.local_clear_for_s += dt_s
                else:
                    runtime.local_clear_for_s = 0.0
                if (
                    runtime.local_clear_for_s
                    >= self.config.control.local_control_clear_duration_s
                ):
                    runtime.local_relief = False
                    runtime.local_clear_for_s = 0.0
                    if runtime.hold_after_local_relief:
                        runtime.phase = FingerPhase.LOCAL_LIMIT_HOLD
                        runtime.command_angles_deg = actual
                        runtime.velocity_deg_s = 0.0
                        events.append(f"{name}_local_limit_hold")
                    else:
                        self._begin_force(name, runtime, actual, force_n)
                        runtime.local_cooldown_s = (
                            self.config.control.local_control_cooldown_s
                        )
                        events.append(f"{name}_local_relief_released")
                    continue
                delta = self.config.control.release_velocity_deg_s * dt_s
                runtime.command_angles_deg = (
                    clamp(
                        runtime.command_angles_deg[0]
                        - self.config.hand.closure_sign * delta,
                        *self._joint_limits(name, 0),
                    ),
                    clamp(
                        runtime.command_angles_deg[1]
                        - self.config.hand.closure_sign
                        * self.config.hand.distal_to_proximal_ratio
                        * delta,
                        *self._joint_limits(name, 1),
                    ),
                )
                runtime.velocity_deg_s = -self.config.control.release_velocity_deg_s
                continue
            if runtime.phase == FingerPhase.LOCAL_LIMIT_HOLD:
                runtime.command_angles_deg = actual
                runtime.velocity_deg_s = 0.0
                continue
            if runtime.phase in {FingerPhase.APPROACH, FingerPhase.REAPPROACH}:
                if (
                    max(force_n, raw_force)
                    >= self.config.control.activation_force_n
                ):
                    self._begin_force(name, runtime, actual, force_n)
                    events.append(f"{name}_contact")
                    continue

                velocity = (
                    self.profile.approach_velocity_deg_s
                    if runtime.phase == FingerPhase.APPROACH
                    else self.config.control.reapproach_velocity_deg_s
                )
                runtime.progress_deg += velocity * dt_s
                limit = (
                    self.profile.approach_limit_deg
                    if runtime.phase == FingerPhase.APPROACH
                    else self.config.control.reapproach_span_deg
                )
                bounded_progress = min(runtime.progress_deg, limit)
                candidate = closure_to_angles(
                    runtime.base_angles_deg,
                    bounded_progress,
                    self.config.hand.closure_sign,
                    self.config.hand.distal_to_proximal_ratio,
                    self._joint_limits(name, 0),
                    self._joint_limits(name, 1),
                )
                reached_search_limit = (
                    bounded_progress >= limit
                    or candidate[0]
                    in self._joint_limits(name, 0)
                    or candidate[1]
                    in self._joint_limits(name, 1)
                )
                runtime.command_angles_deg = candidate
                runtime.velocity_deg_s = velocity
                if reached_search_limit:
                    runtime.phase = FingerPhase.NO_CONTACT
                    runtime.velocity_deg_s = 0.0
                    events.append(f"{name}_no_contact")
                continue

            if runtime.phase == FingerPhase.FORCE:
                if force_n < self.config.control.contact_release_force_n:
                    runtime.lost_for_s += dt_s
                else:
                    runtime.lost_for_s = 0.0
                if (
                    runtime.lost_for_s
                    >= self.config.control.contact_loss_hold_s
                ):
                    runtime.phase = FingerPhase.REAPPROACH
                    runtime.base_angles_deg = runtime.command_angles_deg
                    runtime.progress_deg = 0.0
                    runtime.admittance = None
                    runtime.velocity_deg_s = 0.0
                    events.append(f"{name}_contact_lost")
                    continue
                if runtime.local_cooldown_s > 0:
                    runtime.local_cooldown_s = max(
                        0.0, runtime.local_cooldown_s - dt_s
                    )
                    runtime.command_angles_deg = actual
                    runtime.velocity_deg_s = 0.0
                    continue
                if runtime.admittance is None:
                    raise RuntimeError(f"{name} force phase has no admittance")
                result = runtime.admittance.update(
                    force_n=max(force_n, filtered_local_max_force),
                    target_force_n=target_force_n,
                    current_angles_deg=actual,
                    dt_s=dt_s,
                )
                runtime.command_angles_deg = result.angles_deg
                runtime.velocity_deg_s = result.velocity_deg_s
                runtime.closure_s_deg = result.closure_s_deg

    def _begin_force(
        self,
        name: str,
        runtime: _FingerRuntime,
        actual_angles_deg: tuple[float, float],
        force_n: float,
    ) -> None:
        del force_n
        runtime.phase = FingerPhase.FORCE
        runtime.local_relief = False
        runtime.base_angles_deg = (
            float(actual_angles_deg[0]),
            float(actual_angles_deg[1]),
        )
        runtime.command_angles_deg = runtime.base_angles_deg
        runtime.previous_actual_angles_deg = runtime.base_angles_deg
        runtime.progress_deg = 0.0
        runtime.lost_for_s = 0.0
        runtime.closure_s_deg = 0.0
        runtime.admittance = FingerAdmittance(
            base_angles_deg=runtime.base_angles_deg,
            previous_angles_deg=actual_angles_deg,
            proximal_limits=self._joint_limits(name, 0),
            distal_limits=self._joint_limits(name, 1),
            closure_sign=self.config.hand.closure_sign,
            ratio=self.config.hand.distal_to_proximal_ratio,
            force_to_velocity_gain=self.profile.force_to_velocity_gain,
            velocity_damping_gain=self.profile.velocity_damping_gain,
            maximum_velocity_deg_s=self.config.control.max_closure_velocity_deg_s,
            force_deadband_n=self.config.control.force_deadband_n,
        )

    def _state_from_fingers(
        self, frame: ContactFrame, events: list[str]
    ) -> None:
        phases = {runtime.phase for runtime in self._fingers.values()}
        if FingerPhase.LOCAL_LIMIT_HOLD in phases:
            self.state = ControlState.LOCAL_LIMIT_HOLD
            self.reason = (
                "repeated local overload; both fingers held; "
                "reposition object or change target force"
            )
            hold_angles = clamp_angles(
                frame.angles_deg,
                self.config.hand.safe_min_angles_deg,
                self.config.hand.safe_max_angles_deg,
            )
            self._angles_cmd = hold_angles
            self._fingers["index"].command_angles_deg = hold_angles[0:2]
            self._fingers["middle"].command_angles_deg = hold_angles[2:4]
        elif phases == {FingerPhase.NO_CONTACT}:
            self.state = ControlState.NO_CONTACT
            self.reason = "both fingers reached search limit without contact"
        elif (
            FingerPhase.FORCE in phases
            or FingerPhase.LOCAL_RELIEF in phases
            or FingerPhase.LOCAL_LIMIT_HOLD in phases
        ):
            self.state = ControlState.CONTROLLING
            if FingerPhase.LOCAL_LIMIT_HOLD in phases:
                self.reason = "local force limit hold; reduce target or reposition object"
            elif FingerPhase.LOCAL_RELIEF in phases:
                self.reason = "local force relief active"
            elif phases <= {FingerPhase.FORCE, FingerPhase.NO_CONTACT}:
                self.reason = "force admittance active"
            else:
                self.reason = "force admittance active; one finger searching"
        else:
            self.state = ControlState.APPROACH
            if FingerPhase.NO_CONTACT in phases:
                self.reason = "one finger reached search limit; other still approaching"
            else:
                self.reason = f"approaching with profile={self.profile_name}"
        del events

    def _release_step(
        self,
        frame: ContactFrame,
        filtered: FilteredForce,
        now_s: float,
        dt_s: float,
        events: list[str],
    ) -> ControlOutput:
        delta = self.config.control.release_velocity_deg_s * dt_s
        released: list[float] = []
        for current, opened in zip(self._angles_cmd, self.config.hand.open_angles_deg):
            if current > opened:
                released.append(max(opened, current - delta))
            else:
                released.append(min(opened, current + delta))
        self._angles_cmd = clamp_angles(
            released,
            self.config.hand.safe_min_angles_deg,
            self.config.hand.safe_max_angles_deg,
        )
        for name, runtime in self._fingers.items():
            indices = (0, 1) if name == "index" else (2, 3)
            runtime.command_angles_deg = (
                self._angles_cmd[indices[0]],
                self._angles_cmd[indices[1]],
            )
            runtime.velocity_deg_s = -self.config.control.release_velocity_deg_s
        return self._make_output(frame, filtered, now_s, events=tuple(events))

    def _enter_fault(
        self,
        frame: ContactFrame,
        now_s: float,
        reason: str,
        events: tuple[str, ...],
    ) -> ControlOutput:
        self.state = ControlState.FAULT
        self.reason = reason
        self._angles_cmd = self.config.hand.open_angles_deg
        if self._fingers:
            self._fingers["index"].command_angles_deg = (
                self._angles_cmd[0],
                self._angles_cmd[1],
            )
            self._fingers["middle"].command_angles_deg = (
                self._angles_cmd[2],
                self._angles_cmd[3],
            )
        filtered = self._last_filtered
        if filtered is None:
            raise RuntimeError("force estimator is not initialized")
        return self._make_output(frame, filtered, now_s, events=events)

    def _update_temperature(
        self,
        temperatures_c: tuple[int, int, int, int],
        dt_s: float,
        events: list[str],
    ) -> None:
        if max(temperatures_c) > self.config.safety.temperature_limit_c:
            self._temperature_high_for_s += dt_s
            if self._temperature_high_for_s > 0 and "temperature_high" not in self._event_keys:
                events.append("temperature_high")
                self._event_keys.add("temperature_high")
        else:
            self._temperature_high_for_s = 0.0
            self._event_keys.discard("temperature_high")

    def _joint_limits(
        self, finger: str, which: int
    ) -> tuple[float, float]:
        if finger == "index":
            index = 0 if which == 0 else 1
        elif finger == "middle":
            index = 2 if which == 0 else 3
        else:
            raise ValueError(f"unknown finger: {finger}")
        return (
            self.config.hand.safe_min_angles_deg[index],
            self.config.hand.safe_max_angles_deg[index],
        )

    def _raw_finger_forces(self, frame: ContactFrame) -> tuple[float, float]:
        baseline = self.baseline_vectors_n or ((0.0, 0.0, 0.0),) * 10
        index_force = sum(
            weight
            * max(
                0.0,
                float(frame.force_vectors_n[index][2]) - float(baseline[index][2]),
            )
            for index, weight in zip(
                self.config.hand.index_sensor_indices,
                self.config.hand.index_sensor_weights,
            )
        )
        middle_force = sum(
            weight
            * max(
                0.0,
                float(frame.force_vectors_n[index][2]) - float(baseline[index][2]),
            )
            for index, weight in zip(
                self.config.hand.middle_sensor_indices,
                self.config.hand.middle_sensor_weights,
            )
        )
        return index_force, middle_force

    def _finger_command(
        self, name: str, filtered: FilteredForce, target_force_n: float
    ) -> FingerCommand:
        runtime = self._fingers[name]
        force_n = filtered.index_n if name == "index" else filtered.middle_n
        return FingerCommand(
            phase=runtime.phase,
            force_n=force_n,
            target_force_n=target_force_n,
            closure_s_deg=runtime.closure_s_deg,
            velocity_deg_s=runtime.velocity_deg_s,
            angles_deg=runtime.command_angles_deg,
            local_force_n=(
                filtered.index_local_max_n
                if name == "index"
                else filtered.middle_local_max_n
            ),
            local_relief=runtime.local_relief,
        )

    def _make_output(
        self,
        frame: ContactFrame,
        filtered: FilteredForce,
        now_s: float,
        events: tuple[str, ...] = (),
    ) -> ControlOutput:
        if not self._fingers:
            raise RuntimeError("finger runtimes are not initialized")
        index_angles = self._fingers["index"].command_angles_deg
        middle_angles = self._fingers["middle"].command_angles_deg
        self._angles_cmd = clamp_angles(
            (
                index_angles[0],
                index_angles[1],
                middle_angles[0],
                middle_angles[1],
            ),
            self.config.hand.safe_min_angles_deg,
            self.config.hand.safe_max_angles_deg,
        )
        target = self._target_ramp.current_force_n
        return ControlOutput(
            timestamp_s=float(now_s),
            state=self.state,
            reason=self.reason,
            angles_deg=self._angles_cmd,
            index=self._finger_command("index", filtered, target),
            middle=self._finger_command("middle", filtered, target),
            sensor_force_n=tuple(
                vector[2] for vector in filtered.sensor_vectors_n
            ),
            filtered_force_n=filtered.sensor_vectors_n,
            local_force_n=filtered.local_magnitudes_n,
            temperature_c=tuple(int(value) for value in frame.motor_temperature_c),
            current_ma=tuple(int(value) for value in frame.motor_current_ma),
            events=events,
        )
