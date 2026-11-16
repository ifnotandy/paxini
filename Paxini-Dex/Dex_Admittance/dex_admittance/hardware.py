from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from .config import AppConfig
from .models import ContactFrame


class HardwareError(RuntimeError):
    pass


@dataclass(frozen=True)
class HardwareStatus:
    connected: bool
    sdk_version: str
    firmware_version: str
    control_mode: str
    motor_enabled: bool
    sensor_fault_count: int
    position_min: tuple[int, int, int, int] | None
    position_max: tuple[int, int, int, int] | None
    angles_deg: tuple[float, float, float, float] | None


class PXDexHardware:
    """Thin, defensive adapter around the official pxhandsdk Python package."""

    def __init__(self, config: AppConfig, port: str) -> None:
        self.config = config
        self.port = port
        self._hand: Any = None
        self._latest_frame: ContactFrame | None = None
        self._frame_lock = threading.Lock()
        self._collection_started = False
        self._cached_mode = "unknown"
        self._cached_enabled = False
        self._firmware = "unknown"
        self._sdk_version = "unknown"
        self._sensor_fault_count = 0

    def connect(self) -> HardwareStatus:
        if self._hand is not None:
            return self.status()
        try:
            import pxhandsdk
        except ImportError as exc:
            raise HardwareError("pxhandsdk is not installed") from exc

        self._hand = pxhandsdk.PxDex3()
        rc = self._hand.connect_device(self.port)
        if rc != 0:
            raise HardwareError(f"connect failed: {self._last_error()}")
        rc, version = self._hand.get_firmware_versions()
        if rc == 0:
            self._firmware = str(version.firmware_version)
        rc, sdk_version = self._hand.get_sdk_version()
        if rc == 0:
            self._sdk_version = str(sdk_version)
        self._refresh_state()
        return self.status()

    def _last_error(self) -> str:
        if self._hand is None:
            return "device is not connected"
        try:
            code, message = self._hand.get_last_error()
            return f"{code}: {message}"
        except Exception as exc:  # pragma: no cover - defensive SDK boundary
            return f"unable to read SDK error: {exc}"

    def _refresh_state(self) -> None:
        if self._hand is None:
            raise HardwareError("device is not connected")
        rc, mode = self._hand.get_motor_control_mode()
        if rc == 0:
            self._cached_mode = str(getattr(mode, "name", mode))
        rc, enabled = self._hand.is_motor_enabled()
        if rc == 0:
            self._cached_enabled = bool(enabled)

    def status(self) -> HardwareStatus:
        if self._hand is None:
            return HardwareStatus(
                connected=False,
                sdk_version=self._sdk_version,
                firmware_version=self._firmware,
                control_mode="unknown",
                motor_enabled=False,
                sensor_fault_count=0,
                position_min=None,
                position_max=None,
                angles_deg=None,
            )
        self._refresh_state()
        min_position = None
        max_position = None
        rc, limits = self._hand.get_position_limit()
        if rc == 0:
            min_position = (
                int(limits.min_position.index_proximal_pos),
                int(limits.min_position.index_distal_pos),
                int(limits.min_position.middle_proximal_pos),
                int(limits.min_position.middle_distal_pos),
            )
            max_position = (
                int(limits.max_position.index_proximal_pos),
                int(limits.max_position.index_distal_pos),
                int(limits.max_position.middle_proximal_pos),
                int(limits.max_position.middle_distal_pos),
            )
        rc, fault = self._hand.get_fault_code()
        if rc == 0 and fault is not None:
            self._sensor_fault_count = int(fault.sensor_fault_count)
        angles_deg = None
        rc, angles = self._hand.get_motor_angles()
        if rc == 0:
            angles_deg = (
                float(angles.index_proximal),
                float(angles.index_distal),
                float(angles.middle_proximal),
                float(angles.middle_distal),
            )
        return HardwareStatus(
            connected=True,
            sdk_version=self._sdk_version,
            firmware_version=self._firmware,
            control_mode=self._cached_mode,
            motor_enabled=self._cached_enabled,
            sensor_fault_count=self._sensor_fault_count,
            position_min=min_position,
            position_max=max_position,
            angles_deg=angles_deg,
        )

    def require_position_mode(self) -> None:
        status = self.status()
        if not status.motor_enabled:
            raise HardwareError("motor is not enabled; refusing position targets")
        if status.control_mode != "MOTOR_MODE_POSITION":
            raise HardwareError(
                "motor is not in MOTOR_MODE_POSITION "
                f"(current={status.control_mode}); not switching automatically"
            )

    def enable(self) -> None:
        if self._hand is None:
            raise HardwareError("device is not connected")
        rc = self._hand.enable_motor()
        if rc != 0:
            raise HardwareError(f"enable failed: {self._last_error()}")
        self._refresh_state()

    def disable(self) -> None:
        if self._hand is None:
            raise HardwareError("device is not connected")
        rc = self._hand.disable_motor()
        if rc != 0:
            raise HardwareError(f"disable failed: {self._last_error()}")
        self._refresh_state()

    def home(self) -> None:
        if self._hand is None:
            raise HardwareError("device is not connected")
        rc = self._hand.set_magnetic_encoder_zero()
        if rc != 0:
            raise HardwareError(f"homing failed: {self._last_error()}")
        self._refresh_state()

    def calibrate_sensors(self) -> None:
        if self._hand is None:
            raise HardwareError("device is not connected")
        rc = self._hand.set_sensor_calibration()
        if rc != 0:
            raise HardwareError(
                f"tactile sensor calibration failed: {self._last_error()}"
            )

    def set_position_mode(self) -> None:
        if self._hand is None:
            raise HardwareError("device is not connected")
        try:
            import pxhandsdk
        except ImportError as exc:
            raise HardwareError("pxhandsdk is not installed") from exc
        mode = pxhandsdk.pxdex3.ControlMode.MOTOR_MODE_POSITION
        rc = self._hand.set_motor_control_mode(mode)
        if rc != 0:
            raise HardwareError(f"position mode switch failed: {self._last_error()}")
        self._refresh_state()
        if self._cached_mode != "MOTOR_MODE_POSITION":
            raise HardwareError(
                f"position mode was not confirmed (reported {self._cached_mode})"
            )

    def open_to_open_pose(
        self,
        *,
        velocity_deg_s: float,
        period_s: float,
        timeout_s: float = 30.0,
    ) -> ContactFrame:
        """Ramp all joints to the configured open pose without using homing."""

        from .models import clamp_angles

        self.require_position_mode()
        self.start_collection()
        frame = self.wait_for_frame()
        deadline = time.monotonic() + timeout_s
        last_timestamp = frame.timestamp_s
        target = clamp_angles(
            frame.angles_deg,
            self.config.hand.safe_min_angles_deg,
            self.config.hand.safe_max_angles_deg,
        )
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now - frame.timestamp_s > self.config.safety.stale_release_s:
                raise HardwareError("tactile frame became stale while opening")
            if frame.sensor_fault_count:
                raise HardwareError("sensor fault detected while opening")
            delta = max(0.01, velocity_deg_s * period_s)
            target = clamp_angles(
                [
                    (
                        max(open_angle, command - delta)
                        if command >= open_angle
                        else min(open_angle, command + delta)
                    )
                    for command, open_angle in zip(
                        target, self.config.hand.open_angles_deg
                    )
                ],
                self.config.hand.safe_min_angles_deg,
                self.config.hand.safe_max_angles_deg,
            )
            self.set_angles(target)
            time.sleep(period_s)
            frame = self.wait_for_next_frame(last_timestamp)
            last_timestamp = frame.timestamp_s
            error = max(
                abs(actual - desired)
                for actual, desired in zip(
                    frame.angles_deg, self.config.hand.open_angles_deg
                )
            )
            if error <= 0.5:
                return frame
        last_error = max(
            abs(actual - desired)
            for actual, desired in zip(
                frame.angles_deg, self.config.hand.open_angles_deg
            )
        )
        raise HardwareError(
            "hand did not reach the open pose within "
            f"{timeout_s:.1f}s; actual={list(frame.angles_deg)}, "
            f"target={list(target)}, open={list(self.config.hand.open_angles_deg)}, "
            f"max_error={last_error:.3f}deg"
        )

    def set_angles(self, angles_deg: tuple[float, float, float, float]) -> None:
        if self._hand is None:
            raise HardwareError("device is not connected")
        rc = self._hand.set_motor_angles(angles_deg)
        if rc != 0:
            raise HardwareError(
                f"angle target rejected for {list(angles_deg)}: {self._last_error()}"
            )

    def read_angles(self) -> tuple[float, float, float, float]:
        if self._hand is None:
            raise HardwareError("device is not connected")
        rc, angles = self._hand.get_motor_angles()
        if rc != 0:
            raise HardwareError(f"angle read failed: {self._last_error()}")
        return (
            float(angles.index_proximal),
            float(angles.index_distal),
            float(angles.middle_proximal),
            float(angles.middle_distal),
        )

    def read_force_vectors(self) -> tuple[tuple[float, float, float], ...]:
        if self._hand is None:
            raise HardwareError("device is not connected")
        rc, points = self._hand.get_sensor_resultant_force()
        if rc != 0:
            raise HardwareError(f"tactile read failed: {self._last_error()}")
        if len(points) < 10:
            raise HardwareError(
                f"tactile read returned {len(points)} channels; expected 10"
            )
        return tuple(
            (float(point.x) / 10.0, float(point.y) / 10.0, float(point.z) / 10.0)
            for point in points[:10]
        )

    def read_motor_current(self) -> tuple[int, int, int, int]:
        if self._hand is None:
            raise HardwareError("device is not connected")
        rc, current = self._hand.get_motor_current()
        if rc != 0:
            raise HardwareError(f"motor current read failed: {self._last_error()}")
        return tuple(int(value) for value in current)  # type: ignore[return-value]

    def read_current_positions(self) -> tuple[int, int, int, int]:
        if self._hand is None:
            raise HardwareError("device is not connected")
        rc, position = self._hand.get_current_motor_position()
        if rc != 0:
            raise HardwareError(f"motor position read failed: {self._last_error()}")
        return (
            int(position.index_proximal_pos),
            int(position.index_distal_pos),
            int(position.middle_proximal_pos),
            int(position.middle_distal_pos),
        )

    def read_target_positions(self) -> tuple[int, int, int, int]:
        if self._hand is None:
            raise HardwareError("device is not connected")
        rc, position = self._hand.get_target_position()
        if rc != 0:
            raise HardwareError(f"motor target read failed: {self._last_error()}")
        return (
            int(position.index_proximal_pos),
            int(position.index_distal_pos),
            int(position.middle_proximal_pos),
            int(position.middle_distal_pos),
        )

    def last_error(self) -> tuple[int, str | None]:
        if self._hand is None:
            return 122, "device is not connected"
        return self._hand.get_last_error()

    def start_collection(self) -> None:
        if self._hand is None:
            raise HardwareError("device is not connected")
        if self._collection_started:
            return
        rc = self._hand.start_collection(
            int(self.config.control.collection_hz), self._on_frame
        )
        if rc != 0:
            raise HardwareError(f"collection start failed: {self._last_error()}")
        self._collection_started = True

    def stop_collection(self) -> None:
        if self._hand is None or not self._collection_started:
            return
        rc = self._hand.stop_collection()
        self._collection_started = False
        if rc != 0:
            raise HardwareError(f"collection stop failed: {self._last_error()}")

    def _on_frame(self, data: Any) -> None:
        try:
            angles = data.angles_deg
            vectors = tuple(
                (float(point.x) / 10.0, float(point.y) / 10.0, float(point.z) / 10.0)
                for point in data.resultant_force[:10]
            )
            if len(vectors) != 10:
                return
            raw_angles = data.encoder_raw
            frame = ContactFrame(
                timestamp_s=float(data.timestamp_monotonic_ns) / 1_000_000_000.0,
                force_vectors_n=vectors,
                angles_deg=(
                    float(angles.index_proximal),
                    float(angles.index_distal),
                    float(angles.middle_proximal),
                    float(angles.middle_distal),
                ),
                motor_current_ma=tuple(int(value) for value in data.motor_current),
                motor_temperature_c=tuple(
                    int(value) for value in data.motor_temperature
                ),
                sensor_fault_count=int(data.fault_info.sensor_fault_count),
                control_mode=self._cached_mode,
                motor_enabled=self._cached_enabled,
                raw_angles=(
                    int(raw_angles.index_proximal),
                    int(raw_angles.index_distal),
                    int(raw_angles.middle_proximal),
                    int(raw_angles.middle_distal),
                ),
            )
        except Exception:
            return
        with self._frame_lock:
            self._latest_frame = frame

    def latest_frame(self) -> ContactFrame | None:
        with self._frame_lock:
            return self._latest_frame

    def clear_latest_frame(self) -> None:
        with self._frame_lock:
            self._latest_frame = None

    def wait_for_frame(self, timeout_s: float = 2.0) -> ContactFrame:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            frame = self.latest_frame()
            if frame is not None:
                return frame
            time.sleep(0.01)
        raise HardwareError(f"no complete frame received within {timeout_s:.1f}s")

    def wait_for_next_frame(
        self, after_timestamp_s: float, timeout_s: float = 2.0
    ) -> ContactFrame:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            frame = self.latest_frame()
            if frame is not None and frame.timestamp_s > after_timestamp_s:
                return frame
            time.sleep(0.005)
        raise HardwareError(f"no newer frame received within {timeout_s:.1f}s")

    def close(self) -> None:
        if self._hand is None:
            return
        try:
            try:
                self.stop_collection()
            except KeyboardInterrupt:
                pass
        finally:
            try:
                self._hand.disconnect_device()
            except KeyboardInterrupt:
                pass
            finally:
                self._hand = None
                self._collection_started = False
