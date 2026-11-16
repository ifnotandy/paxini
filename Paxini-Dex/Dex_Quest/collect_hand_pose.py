#!/usr/bin/env python3
"""Synchronously record PXDex hand/camera data and a Quest 3 6D pose.

This program does not explicitly home, change control mode, or command PXDex
motors. It only connects the hand and starts the SDK collection stream. Note
that the current PXDex SDK automatically enables the motors during
``connect_device()``, as reported by its own connection log.

PXDex collection frames carry a CLOCK_MONOTONIC timestamp. Quest samples are
stamped with the midpoint of the host-side SDK polling call. Pairing uses those
two host monotonic timestamps; the Quest SDK timestamp is retained for
diagnostics but is not assumed to share a clock with the hand.

Each PXDex camera is timestamped independently at the midpoint of its host-side
OpenCV read. Both cameras are nearest-neighbor matched to every PXDex frame.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import queue
import re
import signal
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence

from camera_source import (
    CameraFrame,
    CameraStream,
    PxDexCamera,
    discover_pxdex_cameras,
    save_jpeg,
)
from quest3 import (
    DEFAULT_APK,
    DEFAULT_PACKAGE,
    DEFAULT_PC_SERVICE_SCRIPT,
    QuestSetupError,
    XrtReader,
    import_sdk,
    maybe_start_pc_service,
    pose_info,
    prepare_quest,
)


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_ROOT = SCRIPT_DIR / "recordings"
DEFAULT_HAND_RATE_HZ = 30
PXDEX_TACTILE_SAMPLE_RATE_HZ = 83.3
PXDEX_ENCODER_SAMPLE_RATE_HZ = 78.0
QUEST_COORDINATE_FRAME = {
    "name": "xrobotoolkit_quest_tracking_origin",
    "convention": "Unity tracking-origin frame (no additional transform)",
    "handedness": "left-handed",
    "axes": "+X right, +Y up, +Z forward",
    "origin": (
        "XRoboToolKit APK tracking origin; system recentering can change it"
    ),
    "translation_unit": "m",
    "rotation": "quaternion xyzw",
}
ANGLE_ORDER = ["FF2", "FF1", "MF2", "MF1"]
CAMERA_SLOTS = ("cam0", "cam1")
ANGLE_FIELDS = [
    "index_proximal",
    "index_distal",
    "middle_proximal",
    "middle_distal",
]
POSITION_FIELDS = [f"{name}_pos" for name in ANGLE_FIELDS]
RESULTANT_FORCE_ROUTES = [
    "thumb_one",
    "thumb_two",
    "index_zero",
    "index_one",
    "middle_zero",
    "middle_one",
    "middle_two",
    "middle_three",
    "palm_zero",
    "palm_one",
]
STOP_EVENT = threading.Event()
FORCE_EXIT_WATCHDOG_ARMED = threading.Event()
SHUTDOWN_TIMEOUT_S = 10.0


def log(message: str) -> None:
    print(message, flush=True)


def warn(message: str) -> None:
    print(f"[警告] {message}", file=sys.stderr, flush=True)


def error(message: str) -> None:
    print(f"[错误] {message}", file=sys.stderr, flush=True)


def call_with_timeout(
    label: str,
    function: Callable[[], Any],
    timeout_s: float,
) -> tuple[bool, Exception | None]:
    """Run a cleanup call without allowing a vendor SDK to block shutdown."""
    finished = threading.Event()
    failure: list[Exception] = []

    def run() -> None:
        try:
            function()
        except Exception as exc:
            failure.append(exc)
        finally:
            finished.set()

    worker = threading.Thread(
        target=run,
        name=f"cleanup-{label}",
        daemon=True,
    )
    worker.start()
    if not finished.wait(timeout_s):
        warn(f"{label} 超过 {timeout_s:g} 秒未返回，跳过等待")
        return False, None
    if failure:
        warn(f"{label} 失败：{failure[0]}")
        return True, failure[0]
    return True, None


def force_exit_after_watchdog() -> None:
    time.sleep(SHUTDOWN_TIMEOUT_S)
    if not STOP_EVENT.is_set():
        return
    error(
        f"安全清理超过 {SHUTDOWN_TIMEOUT_S:g} 秒，强制退出进程"
    )
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(130)


@dataclass(frozen=True, slots=True)
class TimedSample:
    timestamp_ns: int
    data: dict[str, Any]


class TimedStream:
    """Small thread-safe buffer used to find a sample near a target time."""

    def __init__(self, max_samples: int) -> None:
        self._samples: deque[TimedSample] = deque(maxlen=max_samples)
        self._condition = threading.Condition()

    def push(self, sample: TimedSample) -> None:
        with self._condition:
            self._samples.append(sample)
            self._condition.notify_all()

    def latest(self) -> TimedSample | None:
        with self._condition:
            return self._samples[-1] if self._samples else None

    def nearest(
        self,
        target_ns: int,
        max_skew_ns: int,
    ) -> tuple[TimedSample | None, int | None]:
        """Return the nearest buffered sample, or no pair outside tolerance."""
        with self._condition:
            if not self._samples:
                return None, None
            best = min(
                self._samples,
                key=lambda sample: abs(sample.timestamp_ns - target_ns),
            )
        skew_ns = abs(best.timestamp_ns - target_ns)
        if skew_ns > max_skew_ns:
            return None, skew_ns
        return best, skew_ns


def finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def force_points_to_lists(points: Sequence[Any]) -> list[list[int]]:
    return [[int(point.x), int(point.y), int(point.z)] for point in points]


def fault_to_dict(fault: Any) -> dict[str, Any]:
    sensor_faults = []
    for item in getattr(fault, "sensor_faults", ()):
        sensor_faults.append(
            {
                "source_name": str(item.source_name),
                "source_index": int(item.source_index),
                "error_code": int(item.error_code),
            }
        )
    magnetic_encoder_faults = []
    for item in getattr(fault, "magnetic_encoder_faults", ()):
        magnetic_encoder_faults.append(
            {
                "source_name": str(item.source_name),
                "source_index": int(item.source_index),
                "error_code": int(item.error_code),
            }
        )
    return {
        "sensor_fault_count": int(fault.sensor_fault_count),
        "magnetic_encoder_fault_count": int(fault.magnetic_encoder_fault_count),
        "sensor_faults": sensor_faults,
        "magnetic_encoder_faults": magnetic_encoder_faults,
    }


def snapshot_hand_frame(frame: Any, include_distribution: bool) -> dict[str, Any]:
    """Copy a PXDex callback frame before returning from the callback."""
    timestamp_ns = int(frame.timestamp_monotonic_ns)
    unix_ns = int(frame.timestamp_unix_ns)
    angles = [finite_float(getattr(frame.angles_deg, name)) for name in ANGLE_FIELDS]
    positions = [
        int(getattr(frame.current_position, name)) for name in POSITION_FIELDS
    ]
    hand: dict[str, Any] = {
        "timestamp_monotonic_ns": timestamp_ns,
        "timestamp_unix_ns": unix_ns,
        "joint_angles_deg": {
            "order": ANGLE_ORDER,
            "fields": ANGLE_FIELDS,
            "values": angles,
        },
        "motor_position_raw": {
            "fields": POSITION_FIELDS,
            "values": positions,
        },
        "encoder_raw": {
            "fields": ANGLE_FIELDS,
            "values": [int(value) for value in frame.encoder_raw.values],
        },
        "motor_current_ma": [int(value) for value in frame.motor_current],
        "motor_temperature_c": [int(value) for value in frame.motor_temperature],
        "resultant_force": {
            "unit": "0.1 N",
            "routes": RESULTANT_FORCE_ROUTES,
            "values_xyz": force_points_to_lists(frame.resultant_force),
        },
        "fault": fault_to_dict(frame.fault_info),
    }
    if include_distribution:
        hand["distribution_force"] = {
            "unit": "0.1 N",
            "layout": "flat",
            "values_xyz": force_points_to_lists(frame.distribute_force),
        }
    return hand


def extract_selected_pose(
    sample: dict[str, Any],
    source: str,
    side: str,
) -> dict[str, Any]:
    """Extract one pose while retaining the complete Quest sample separately."""
    info: dict[str, Any]
    if source == "head":
        info = sample["head"]
        selected_side = None
    elif source == "controller":
        info = sample["controllers"][side]["pose"]
        selected_side = side
    elif source == "hand":
        hand = sample["hands"][side]
        joints = hand.get("joints_pose_xyzw")
        raw_pose = joints[0] if isinstance(joints, list) and joints else None
        info = pose_info(raw_pose, bool(hand.get("active")))
        selected_side = side
    else:
        raise ValueError(f"未知 pose source: {source}")

    values = info.get("pose_xyzw")
    pose_xyzw = None
    if isinstance(values, list) and len(values) >= 7:
        numeric = [finite_float(value) for value in values[:7]]
        if all(value is not None for value in numeric):
            pose_xyzw = [float(value) for value in numeric]
    return {
        "source": source,
        "side": selected_side,
        "available": bool(info.get("available")),
        "tracked": bool(info.get("tracked")),
        "pose_xyzw": pose_xyzw,
        "translation_m": pose_xyzw[:3] if pose_xyzw else None,
        "rotation_xyzw": pose_xyzw[3:7] if pose_xyzw else None,
    }


class AlignedRecorder:
    def __init__(
        self,
        output_dir: Path,
        pose_source: str,
        pose_side: str,
        max_skew_ns: int,
        hand_side: str,
        camera_ids: Sequence[str],
        camera_max_skew_ns: int,
        camera_jpeg_quality: int,
    ) -> None:
        self.output_dir = output_dir
        self.pose_source = pose_source
        self.pose_side = pose_side
        self.max_skew_ns = max_skew_ns
        self.hand_side = hand_side
        self.camera_ids = tuple(camera_ids)
        self.cameras_enabled = bool(self.camera_ids)
        self.camera_max_skew_ns = camera_max_skew_ns
        self.camera_jpeg_quality = camera_jpeg_quality
        self.jsonl_file = None
        self.csv_file = None
        self.csv_writer = None
        self.frame_count = 0
        self.paired_count = 0
        self.unpaired_count = 0
        self.pose_tracked_count = 0
        self.camera_aligned_count = 0
        self.all_sources_aligned_count = 0
        self.camera_aligned_counts = {
            camera_id: 0 for camera_id in self.camera_ids
        }
        self._saved_camera_frames: set[tuple[str, int]] = set()
        self.last_quest_skew_ns: int | None = None
        self.started_at = datetime.now().astimezone()
        self.started_monotonic_ns = time.monotonic_ns()

    def __enter__(self) -> "AlignedRecorder":
        self.output_dir.mkdir(parents=True, exist_ok=False)
        for camera_id in self.camera_ids:
            (self.output_dir / "cameras" / camera_id).mkdir(
                parents=True
            )
        self.jsonl_file = (self.output_dir / "aligned.jsonl").open(
            "x", encoding="utf-8", buffering=1024 * 1024
        )
        self.csv_file = (self.output_dir / "hand_pose.csv").open(
            "x", encoding="utf-8", newline=""
        )
        self.csv_writer = csv.DictWriter(
            self.csv_file,
            fieldnames=[
                "frame",
                "aligned",
                "hand_timestamp_monotonic_ns",
                "hand_timestamp_unix_ns",
                "quest_host_timestamp_ns",
                "alignment_skew_ms",
                "quest_sdk_timestamp_ns",
                *[
                    field
                    for camera_id in self.camera_ids
                    for field in (
                        f"{camera_id}_image",
                        f"{camera_id}_skew_ms",
                    )
                ],
                "pose_source",
                "pose_side",
                "pose_tracked",
                "x_m",
                "y_m",
                "z_m",
                "qx",
                "qy",
                "qz",
                "qw",
                "ff2_deg",
                "ff1_deg",
                "mf2_deg",
                "mf1_deg",
                "index_proximal_raw",
                "index_distal_raw",
                "middle_proximal_raw",
                "middle_distal_raw",
            ],
        )
        self.csv_writer.writeheader()
        return self

    def write(
        self,
        hand: dict[str, Any],
        quest_sample: TimedSample | None,
        skew_ns: int | None,
        camera_pairs: dict[str, tuple[CameraFrame | None, int | None]] | None = None,
    ) -> None:
        self.frame_count += 1
        aligned = quest_sample is not None
        if aligned:
            self.paired_count += 1
            pose = extract_selected_pose(
                quest_sample.data,
                self.pose_source,
                self.pose_side,
            )
            if pose["tracked"]:
                self.pose_tracked_count += 1
            self.last_quest_skew_ns = skew_ns
            quest_data = quest_sample.data
            quest_timestamp_ns = quest_sample.timestamp_ns
        else:
            self.unpaired_count += 1
            pose = {
                "source": self.pose_source,
                "side": None if self.pose_source == "head" else self.pose_side,
                "available": False,
                "tracked": False,
                "pose_xyzw": None,
                "translation_m": None,
                "rotation_xyzw": None,
            }
            quest_data = None
            quest_timestamp_ns = None

        cameras = self._camera_records(camera_pairs or {})
        camera_aligned = (
            all(camera["aligned"] for camera in cameras.values())
            if self.cameras_enabled
            else None
        )
        if camera_aligned:
            self.camera_aligned_count += 1
        if aligned and camera_aligned is not False:
            self.all_sources_aligned_count += 1
        for camera_id, camera in cameras.items():
            if camera["aligned"]:
                self.camera_aligned_counts[camera_id] += 1

        record = {
            "frame": self.frame_count,
            "sync": {
                "method": "nearest_host_monotonic",
                "aligned": aligned,
                "cameras_aligned": camera_aligned,
                "all_sources_aligned": bool(aligned and camera_aligned is not False),
                "hand_timestamp_monotonic_ns": hand["timestamp_monotonic_ns"],
                "hand_timestamp_unix_ns": hand["timestamp_unix_ns"],
                "quest_host_timestamp_monotonic_ns": quest_timestamp_ns,
                "alignment_skew_ns": skew_ns,
                "max_skew_ns": self.max_skew_ns,
                "camera_max_skew_ns": self.camera_max_skew_ns,
            },
            "pose_6d": pose,
            "quest_coordinate_frame": dict(QUEST_COORDINATE_FRAME),
            "pxdex_hand_side": self.hand_side,
            "hand": hand,
            "quest": quest_data,
            "cameras": cameras if self.cameras_enabled else None,
        }
        if self.jsonl_file is None or self.csv_writer is None:
            raise RuntimeError("recorder 尚未打开")
        self.jsonl_file.write(
            json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n"
        )
        self._write_csv(record)

    def _camera_records(
        self,
        camera_pairs: dict[str, tuple[CameraFrame | None, int | None]],
    ) -> dict[str, dict[str, Any]]:
        cameras: dict[str, dict[str, Any]] = {}
        if not self.cameras_enabled:
            return cameras
        for camera_id in self.camera_ids:
            frame, skew_ns = camera_pairs.get(camera_id, (None, None))
            image_path = None
            error_message = None
            if frame is not None:
                relative_path = (
                    Path("cameras")
                    / camera_id
                    / f"{frame.frame_index:09d}.jpg"
                )
                cache_key = (camera_id, frame.frame_index)
                if cache_key not in self._saved_camera_frames:
                    try:
                        save_jpeg(
                            self.output_dir / relative_path,
                            frame.image,
                            self.camera_jpeg_quality,
                        )
                        self._saved_camera_frames.add(cache_key)
                        image_path = relative_path.as_posix()
                    except Exception as exc:
                        error_message = str(exc)
                        warn(
                            f"保存 PXDex {camera_id} 摄像头图像失败：{exc}"
                        )
                else:
                    image_path = relative_path.as_posix()
            cameras[camera_id] = {
                "hand_side": self.hand_side,
                "aligned": frame is not None,
                "timestamp_monotonic_ns": (
                    frame.timestamp_ns if frame is not None else None
                ),
                "read_start_ns": (
                    frame.read_start_ns if frame is not None else None
                ),
                "read_end_ns": (
                    frame.read_end_ns if frame is not None else None
                ),
                "alignment_skew_ns": skew_ns,
                "frame_index": (
                    frame.frame_index if frame is not None else None
                ),
                "image": image_path,
                "width": (
                    int(frame.image.shape[1]) if frame is not None else None
                ),
                "height": (
                    int(frame.image.shape[0]) if frame is not None else None
                ),
                "error": error_message,
                "time_method": "host_monotonic_read_midpoint",
            }
        return cameras

    def _write_csv(self, record: dict[str, Any]) -> None:
        assert self.csv_writer is not None
        hand = record["hand"]
        pose = record["pose_6d"]
        sync = record["sync"]
        pose_xyzw = pose["pose_xyzw"] or [None] * 7
        angles = hand["joint_angles_deg"]["values"]
        positions = hand["motor_position_raw"]["values"]
        quest = record["quest"]
        cameras = record.get("cameras") or {}
        skew_ns = sync["alignment_skew_ns"]
        row = {
            "frame": record["frame"],
            "aligned": int(sync["aligned"]),
            "hand_timestamp_monotonic_ns": sync["hand_timestamp_monotonic_ns"],
            "hand_timestamp_unix_ns": sync["hand_timestamp_unix_ns"],
            "quest_host_timestamp_ns": sync["quest_host_timestamp_monotonic_ns"],
            "alignment_skew_ms": (
                f"{skew_ns / 1_000_000:.6f}" if skew_ns is not None else None
            ),
            "quest_sdk_timestamp_ns": (
                quest.get("sdk_timestamp_ns") if quest is not None else None
            ),
            "pose_source": pose["source"],
            "pose_side": pose["side"],
            "pose_tracked": int(pose["tracked"]),
            "x_m": pose_xyzw[0],
            "y_m": pose_xyzw[1],
            "z_m": pose_xyzw[2],
            "qx": pose_xyzw[3],
            "qy": pose_xyzw[4],
            "qz": pose_xyzw[5],
            "qw": pose_xyzw[6],
            "ff2_deg": angles[0],
            "ff1_deg": angles[1],
            "mf2_deg": angles[2],
            "mf1_deg": angles[3],
            "index_proximal_raw": positions[0],
            "index_distal_raw": positions[1],
            "middle_proximal_raw": positions[2],
            "middle_distal_raw": positions[3],
        }
        for camera_id in self.camera_ids:
            camera = cameras.get(camera_id) or {}
            row[f"{camera_id}_image"] = camera.get("image")
            row[f"{camera_id}_skew_ms"] = self._skew_ms(
                camera.get("alignment_skew_ns")
            )
        self.csv_writer.writerow(row)

    @staticmethod
    def _skew_ms(skew_ns: Any) -> str | None:
        if skew_ns is None:
            return None
        return f"{float(skew_ns) / 1_000_000:.6f}"

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.jsonl_file is not None:
            self.jsonl_file.flush()
            self.jsonl_file.close()
        if self.csv_file is not None:
            self.csv_file.flush()
            self.csv_file.close()

    def write_manifest(self, extra: dict[str, Any]) -> Path:
        ended_at = datetime.now().astimezone()
        elapsed_s = (time.monotonic_ns() - self.started_monotonic_ns) / 1e9
        manifest = {
            "format_version": 3,
            "started_at": self.started_at.isoformat(timespec="milliseconds"),
            "ended_at": ended_at.isoformat(timespec="milliseconds"),
            "elapsed_seconds": elapsed_s,
            "pxdex_hand_side": self.hand_side,
            "output": {
                "aligned_jsonl": "aligned.jsonl",
                "flat_csv": "hand_pose.csv",
                "camera_images": "cameras/" if self.cameras_enabled else None,
                "camera_ids": list(self.camera_ids),
            },
            "pose": {
                "source": self.pose_source,
                "side": None if self.pose_source == "head" else self.pose_side,
                "order": ["x", "y", "z", "qx", "qy", "qz", "qw"],
                "coordinate_frame": dict(QUEST_COORDINATE_FRAME),
            },
            "alignment": {
                "method": "nearest_host_monotonic",
                "max_skew_ns": self.max_skew_ns,
                "frames": self.frame_count,
                "paired_frames": self.paired_count,
                "unpaired_frames": self.unpaired_count,
                "pose_tracked_frames": self.pose_tracked_count,
                "last_quest_skew_ns": self.last_quest_skew_ns,
                "cameras_enabled": self.cameras_enabled,
                "camera_max_skew_ns": self.camera_max_skew_ns,
                "all_sources_aligned_frames": self.all_sources_aligned_count,
                "camera_aligned_frames": self.camera_aligned_counts,
            },
            "units": {
                "pose_translation": "m",
                "pose_rotation": "quaternion xyzw",
                "joint_angles": "degree",
                "force": "0.1 N",
                "motor_current": "mA",
                "motor_temperature": "degree C",
            },
            **extra,
        }
        path = self.output_dir / "manifest.json"
        with path.open("x", encoding="utf-8") as file:
            json.dump(manifest, file, ensure_ascii=False, indent=2, allow_nan=False)
            file.write("\n")
        return path


class HandCollector:
    def __init__(
        self,
        port: str,
        frequency_hz: int,
        include_distribution: bool,
    ) -> None:
        import pxhandsdk

        self.pxhandsdk = pxhandsdk
        self.port = port
        self.frequency_hz = frequency_hz
        self.include_distribution = include_distribution
        self.hand = pxhandsdk.PxDex3()
        self.frames: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=4096)
        self.collection_started = False
        self.snapshot_errors = 0
        self.queue_overflows = 0
        self.firmware_version: str | None = None
        self.sn: str | None = None

    def connect(self) -> None:
        rc = self.hand.connect_device(self.port)
        if rc != 0:
            code, message = self.hand.get_last_error()
            raise RuntimeError(
                f"PXDex 连接失败 rc={rc}, code={code}, message={message}"
            )
        log(f"[INFO] PXDex 已连接：{self.port}")

        rc, version = self.hand.get_firmware_versions()
        if rc == 0:
            self.firmware_version = str(version.firmware_version)
            log(f"[INFO] PXDex 固件：{self.firmware_version}")
        rc, sn = self.hand.get_sn()
        if rc == 0:
            self.sn = str(sn)
            log(f"[INFO] PXDex SN：{self.sn}")
        rc, enabled = self.hand.is_motor_enabled()
        if rc == 0:
            log(f"[INFO] PXDex 电机使能状态：{enabled}")

    def _on_frame(self, frame: Any) -> None:
        try:
            sample = snapshot_hand_frame(frame, self.include_distribution)
        except Exception as exc:
            self.snapshot_errors += 1
            if self.snapshot_errors <= 3:
                warn(f"PXDex 采集帧复制失败：{exc}")
            return
        try:
            self.frames.put_nowait(sample)
        except queue.Full:
            self.queue_overflows += 1
            if self.queue_overflows <= 3:
                warn("PXDex 数据队列已满，出现掉帧")

    def start(self) -> None:
        rc = self.hand.start_collection(self.frequency_hz, self._on_frame)
        if rc != 0:
            code, message = self.hand.get_last_error()
            raise RuntimeError(
                f"PXDex 连续采集启动失败 rc={rc}, code={code}, message={message}"
            )
        self.collection_started = True
        log(f"[INFO] PXDex 连续采集已启动：{self.frequency_hz} Hz")

    def stop(self) -> None:
        if self.collection_started:
            rc = self.hand.stop_collection()
            if rc != 0:
                code, message = self.hand.get_last_error()
                warn(
                    "停止 PXDex 连续采集失败："
                    f"rc={rc}, code={code}, message={message}"
                )
            self.collection_started = False

    def disconnect(self) -> None:
        rc = self.hand.disconnect_device()
        if rc != 0:
            warn(f"断开 PXDex 返回错误码 {rc}")


class QuestPoller(threading.Thread):
    def __init__(
        self,
        reader: XrtReader,
        stream: TimedStream,
        rate_hz: float,
    ) -> None:
        super().__init__(name="quest-poller", daemon=True)
        self.reader = reader
        self.stream = stream
        self.period_s = 1.0 / rate_hz
        self.failure: Exception | None = None
        self.samples = 0
        self.overruns = 0

    def run(self) -> None:
        next_deadline = time.monotonic()
        try:
            while not STOP_EVENT.is_set():
                sequence = self.samples + 1
                started_ns = time.monotonic_ns()
                sample = self.reader.collect_sample(sequence)
                ended_ns = time.monotonic_ns()
                sample["host_poll_start_ns"] = started_ns
                sample["host_poll_end_ns"] = ended_ns
                sample["host_timestamp_ns"] = (started_ns + ended_ns) // 2
                self.stream.push(
                    TimedSample(timestamp_ns=sample["host_timestamp_ns"], data=sample)
                )
                self.samples += 1

                next_deadline += self.period_s
                now = time.monotonic()
                if now - next_deadline > self.period_s:
                    skipped = int((now - next_deadline) / self.period_s)
                    self.overruns += max(1, skipped)
                    next_deadline = now
                STOP_EVENT.wait(max(0.0, next_deadline - time.monotonic()))
        except BaseException as exc:
            self.failure = exc
            STOP_EVENT.set()


def wait_for_quest_ready(
    stream: TimedStream,
    pose_source: str,
    pose_side: str,
    timeout_s: float,
    required_samples: int,
) -> tuple[TimedSample, float]:
    """Wait until Quest continuously supplies a valid selected 6D pose."""
    started = time.monotonic()
    deadline = started + timeout_s
    last_sequence = -1
    consecutive = 0
    last_report = started
    source_text = (
        "head"
        if pose_source == "head"
        else f"{pose_source}.{pose_side}"
    )
    log(
        f"[INFO] 等待 Quest PC Service 收到有效的 {source_text} 6D pose；"
        f"需要连续 {required_samples} 帧，超时 {timeout_s:g} 秒"
    )

    while not STOP_EVENT.is_set() and time.monotonic() < deadline:
        sample = stream.latest()
        if sample is not None and sample.data.get("sequence") != last_sequence:
            last_sequence = sample.data.get("sequence")
            pose = extract_selected_pose(sample.data, pose_source, pose_side)
            if pose["tracked"] and pose["pose_xyzw"] is not None:
                consecutive += 1
                if consecutive >= required_samples:
                    elapsed = time.monotonic() - started
                    log(
                        f"[INFO] Quest 数据链路已确认：收到连续 {consecutive} 帧"
                        f"有效 {source_text} pose，等待 {elapsed:.1f} 秒"
                    )
                    return sample, elapsed
            else:
                consecutive = 0

        now = time.monotonic()
        if now - last_report >= 5.0:
            latest_text = (
                f"最新 SDK 序号 {last_sequence}"
                if last_sequence >= 0
                else "尚未收到 SDK 样本"
            )
            log(
                f"[等待 Quest] {now - started:.1f}/{timeout_s:g} 秒，"
                f"{latest_text}，连续有效 pose={consecutive}"
            )
            last_report = now
        time.sleep(0.05)

    if STOP_EVENT.is_set():
        raise QuestSetupError("等待 Quest 数据时收到停止信号")
    raise QuestSetupError(
        f"等待 {source_text} 6D pose 超时。请确认：\n"
        "  1. Quest 与电脑在同一局域网，PC Service 正在运行；\n"
        "  2. XRoboToolKit APK 已由 ADB 启动，并开启 Tracking 和 Send；\n"
        "  3. 使用 controller 时手柄已唤醒；使用 hand 时手部追踪有效；\n"
        "  4. 当前选中侧确实被追踪。"
    )


def wait_for_cameras_ready(
    cameras: Sequence[PxDexCamera],
    timeout_s: float,
    required_samples: int,
) -> float:
    """Wait until both PXDex cameras continuously deliver new frames."""
    started = time.monotonic()
    deadline = started + timeout_s
    last_indices = {camera.camera_id: -1 for camera in cameras}
    counts = {camera.camera_id: 0 for camera in cameras}
    camera_text = "/".join(camera.camera_id for camera in cameras)
    log(
        f"[INFO] 等待 PXDex {camera_text} 摄像头画面；"
        f"每路需要连续 {required_samples} 帧，超时 {timeout_s:g} 秒"
    )

    while not STOP_EVENT.is_set() and time.monotonic() < deadline:
        for camera in cameras:
            if camera.failure is not None:
                raise RuntimeError(
                    f"PXDex {camera.camera_id} 摄像头失败：{camera.failure}"
                )
            sample = camera.stream.latest()
            if (
                sample is not None
                and sample.frame_index != last_indices[camera.camera_id]
            ):
                last_indices[camera.camera_id] = sample.frame_index
                counts[camera.camera_id] += 1
        if all(count >= required_samples for count in counts.values()):
            elapsed = time.monotonic() - started
            log(
                f"[INFO] PXDex {camera_text} 摄像头已就绪，"
                f"等待 {elapsed:.1f} 秒"
            )
            return elapsed
        time.sleep(0.01)

    if STOP_EVENT.is_set():
        raise RuntimeError("等待 PXDex 摄像头时收到停止信号")
    details = "，".join(
        f"{side}={count}" for side, count in counts.items()
    )
    raise RuntimeError(
        f"等待 PXDex {camera_text} 摄像头画面超时（已收新帧：{details}）。"
        "请检查 by-path 设备节点和摄像头占用情况"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "同步采集 PXDex III 连续帧、左右摄像头图像和 Quest 3 6D pose。"
            "程序不主动回零、切换模式或下发运动目标；"
            "但 PXDex SDK 的 connect_device() 会自动使能电机。"
        )
    )
    parser.add_argument(
        "--hand-port",
        default="auto",
        help="PXDex 串口；默认 auto，自动发现唯一 USB/ACM 串口",
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="列出自动发现的串口和 PXDex 摄像头后退出",
    )
    parser.add_argument(
        "--hand-rate",
        type=int,
        default=DEFAULT_HAND_RATE_HZ,
        help=(
            "PXDex 完整帧目标频率，默认 30 Hz。产品说明书标称触觉采样 "
            "83.3 Hz、关节角度采样 78 Hz"
        ),
    )
    parser.add_argument(
        "--quest-rate",
        type=float,
        default=120.0,
        help="Quest SDK 轮询频率，默认 120 Hz",
    )
    parser.add_argument(
        "--pose-source",
        choices=("controller", "hand", "head"),
        default="controller",
        help="主 6D pose 来源，默认 controller",
    )
    parser.add_argument(
        "--pose-side",
        choices=("left", "right"),
        default="right",
        help="controller/hand pose 的左右手，默认 right",
    )
    parser.add_argument(
        "--pxdex-side",
        choices=("left", "right"),
        default="right",
        help="当前 PXDex 执行器的手别，用于图像命名，默认 right",
    )
    parser.add_argument(
        "--max-skew-ms",
        type=float,
        default=30.0,
        help="允许的最大时间偏差，默认 30 ms；超过时保留手部帧但不配对",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=0.0,
        help="采集秒数；0 表示运行到 Ctrl+C，默认 0",
    )
    parser.add_argument(
        "--quest-start-timeout",
        type=float,
        default=120.0,
        help="等待 APK 启动并通过 PC Service 发来有效 pose 的超时秒数，默认 120",
    )
    parser.add_argument(
        "--quest-ready-samples",
        type=int,
        default=3,
        help="开始采集 PXDex 前要求连续收到的有效 Quest pose 数，默认 3",
    )
    parser.add_argument(
        "--no-cameras",
        action="store_true",
        help="不采集摄像头图像；等价于 --camera-mode none",
    )
    parser.add_argument(
        "--camera-mode",
        choices=("both", "cam0", "cam1", "none"),
        default="both",
        help="同一只 PXDex 的相机：both、cam0、cam1 或 none，默认 both",
    )
    parser.add_argument(
        "--camera-0",
        default="auto",
        help="PXDex cam0；默认 auto，按 MASTER Camera 名称发现",
    )
    parser.add_argument(
        "--camera-1",
        default="auto",
        help="PXDex cam1；默认 auto，按 MASTER Camera 名称发现",
    )
    parser.add_argument(
        "--camera-width",
        type=int,
        default=1280,
        help="摄像头请求宽度，默认 1280",
    )
    parser.add_argument(
        "--camera-height",
        type=int,
        default=720,
        help="摄像头请求高度，默认 720",
    )
    parser.add_argument(
        "--camera-fps",
        type=float,
        default=30.0,
        help="摄像头请求帧率，默认 30 Hz",
    )
    parser.add_argument(
        "--camera-max-skew-ms",
        type=float,
        default=30.0,
        help="PXDex 帧与每路摄像头画面的最大时间偏差，默认 30 ms",
    )
    parser.add_argument(
        "--camera-start-timeout",
        type=float,
        default=15.0,
        help="等待所选摄像头出图的超时秒数，默认 15",
    )
    parser.add_argument(
        "--camera-ready-samples",
        type=int,
        default=3,
        help="启动 PXDex 前每路摄像头要求的连续新帧数，默认 3",
    )
    parser.add_argument(
        "--camera-jpeg-quality",
        type=int,
        default=95,
        help="保存摄像头 JPEG 的质量，默认 95",
    )
    parser.add_argument(
        "--output-dir",
        help="输出目录；默认 recordings/data_<日期>_<序号>",
    )
    parser.add_argument(
        "--omit-distribution",
        action="store_true",
        help="不写 444 点分布力，以减小文件体积",
    )
    parser.add_argument(
        "--skip-adb",
        action="store_true",
        help="不安装/启动 Quest APK，也不配置无线 ADB",
    )
    parser.add_argument("--apk", default=str(DEFAULT_APK), help="Quest APK 路径")
    parser.add_argument("--package", default=DEFAULT_PACKAGE, help="Quest APK 包名")
    parser.add_argument("--adb", help="adb 可执行文件路径")
    parser.add_argument("--serial", help="Quest ADB 设备序列号")
    parser.add_argument("--quest-ip", help="Quest 局域网 IPv4 地址")
    parser.add_argument("--adb-port", type=int, default=5555, help="无线 ADB 端口")
    parser.add_argument(
        "--allow-usb-only",
        action="store_true",
        help="兼容旧参数；有 USB ADB 时本程序默认就保留 USB",
    )
    parser.add_argument(
        "--start-pc-service",
        action="store_true",
        help="PC Service 未运行时自动启动",
    )
    parser.add_argument(
        "--pc-service-script",
        default=str(DEFAULT_PC_SERVICE_SCRIPT),
        help="PC Service 启动脚本路径",
    )
    parser.add_argument(
        "--no-pc-service-check",
        action="store_true",
        help="不检查 PC Service 进程",
    )
    return parser


def validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if not 1 <= args.hand_rate <= int(PXDEX_ENCODER_SAMPLE_RATE_HZ):
        parser.error(
            "--hand-rate 必须在 1..78 Hz 范围内：产品说明书中关节角度采样"
            f"为 {PXDEX_ENCODER_SAMPLE_RATE_HZ:g} Hz，不能用回调目标频率"
            "制造高于硬件采样的数据"
        )
    if not 0 < args.quest_rate <= 240:
        parser.error("--quest-rate 必须在 (0, 240] Hz 范围内")
    if args.max_skew_ms <= 0:
        parser.error("--max-skew-ms 必须大于 0")
    if args.duration < 0:
        parser.error("--duration 必须大于或等于 0")
    if args.quest_start_timeout <= 0:
        parser.error("--quest-start-timeout 必须大于 0")
    if args.quest_ready_samples < 1:
        parser.error("--quest-ready-samples 必须大于或等于 1")
    if args.camera_width <= 0 or args.camera_height <= 0:
        parser.error("--camera-width 和 --camera-height 必须大于 0")
    if args.camera_fps <= 0:
        parser.error("--camera-fps 必须大于 0")
    if args.camera_max_skew_ms <= 0:
        parser.error("--camera-max-skew-ms 必须大于 0")
    if args.camera_start_timeout <= 0:
        parser.error("--camera-start-timeout 必须大于 0")
    if args.camera_ready_samples < 1:
        parser.error("--camera-ready-samples 必须大于或等于 1")
    if not 1 <= args.camera_jpeg_quality <= 100:
        parser.error("--camera-jpeg-quality 必须在 1..100 范围内")
    if args.no_cameras and args.camera_mode in ("cam0", "cam1"):
        parser.error("--no-cameras 不能与 --camera-mode 同时使用")
    camera_mode = "none" if args.no_cameras else args.camera_mode
    if (
        camera_mode == "both"
        and args.camera_0 != "auto"
        and args.camera_0 == args.camera_1
    ):
        parser.error("--camera-0 与 --camera-1 不能是同一设备")
    if not 1 <= args.adb_port <= 65535:
        parser.error("--adb-port 必须在 1..65535 范围内")


def make_output_dir(argument: str | None) -> Path:
    if argument:
        return Path(argument).expanduser().resolve()

    date_text = datetime.now().strftime("%Y%m%d")
    pattern = re.compile(rf"^data_{date_text}_(\d+)$")
    used_numbers: list[int] = []
    if DEFAULT_OUTPUT_ROOT.is_dir():
        for child in DEFAULT_OUTPUT_ROOT.iterdir():
            if not child.is_dir():
                continue
            match = pattern.fullmatch(child.name)
            if match is not None:
                used_numbers.append(int(match.group(1)))

    next_number = max(used_numbers, default=0) + 1
    return (DEFAULT_OUTPUT_ROOT / f"data_{date_text}_{next_number}").resolve()


def discover_hand_ports(dev_root: Path = Path("/dev")) -> list[str]:
    """Discover USB/ACM serial ports without depending on tty numbering."""
    candidates: dict[str, str] = {}
    by_id = dev_root / "serial" / "by-id"
    if by_id.is_dir():
        for entry in sorted(by_id.iterdir()):
            try:
                resolved = entry.resolve(strict=True)
            except OSError:
                continue
            if re.fullmatch(r"tty(?:USB|ACM)\d+", resolved.name):
                candidates.setdefault(str(resolved), str(entry))

    for pattern in ("ttyUSB*", "ttyACM*"):
        for entry in sorted(dev_root.glob(pattern)):
            try:
                resolved = entry.resolve(strict=True)
            except OSError:
                continue
            candidates.setdefault(str(resolved), str(entry))
    return sorted(candidates.values())


def resolve_hand_port(
    requested: str,
    dev_root: Path = Path("/dev"),
) -> str:
    if requested != "auto":
        path = Path(requested).expanduser()
        if not path.exists():
            raise RuntimeError(f"PXDex 串口不存在：{path}")
        return str(path)

    candidates = discover_hand_ports(dev_root)
    if len(candidates) == 1:
        log(f"[INFO] 自动发现 PXDex 串口：{candidates[0]}")
        return candidates[0]
    if not candidates:
        raise RuntimeError(
            "未发现 PXDex 串口。请检查 USB-RS485 转接器、dialout 权限，"
            "或使用 --hand-port /dev/ttyUSBx 指定"
        )
    formatted = "\n".join(f"  {path}" for path in candidates)
    raise RuntimeError(
        "检测到多个串口，无法安全判断哪一个属于 PXDex：\n"
        f"{formatted}\n请使用 --hand-port 指定"
    )


def resolve_camera_devices(
    args: argparse.Namespace,
    slots: Sequence[str],
    discovered: Sequence[str],
) -> dict[str, str]:
    if not slots:
        return {}
    devices: dict[str, str] = {}
    for slot in slots:
        configured = (
            args.camera_0 if slot == "cam0" else args.camera_1
        )
        if configured != "auto":
            devices[slot] = str(Path(configured).expanduser())
            continue
        if slot == "cam0" or len(discovered) == 1:
            index = 0
        else:
            index = 1
        if index >= len(discovered):
            raise RuntimeError(
                f"自动发现 PXDex {slot} 摄像头失败。请用 "
                "--camera-0/--camera-1 指定设备"
            )
        devices[slot] = discovered[index]
    if len(set(devices.values())) != len(devices):
        raise RuntimeError("同一只手的摄像头解析到了同一个设备，请手动指定")
    return devices


def selected_camera_slots(args: argparse.Namespace) -> tuple[str, ...]:
    mode = "none" if args.no_cameras else args.camera_mode
    if mode == "both":
        return CAMERA_SLOTS
    if mode in CAMERA_SLOTS:
        return (mode,)
    return ()


def handle_stop_signal(signum: int, _frame: Any) -> None:
    if STOP_EVENT.is_set():
        error("再次收到停止信号，立即强制退出")
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(130)
    log(
        f"\n[INFO] 收到信号 {signum}，正在安全停止；"
        f"{SHUTDOWN_TIMEOUT_S:g} 秒内未退出可再次按 Ctrl+C 强制结束"
    )
    STOP_EVENT.set()
    if not FORCE_EXIT_WATCHDOG_ARMED.is_set():
        FORCE_EXIT_WATCHDOG_ARMED.set()
        watchdog = threading.Thread(
            target=force_exit_after_watchdog,
            name="force-exit-watchdog",
            daemon=True,
        )
        watchdog.start()


def drain_and_record(
    recorder: AlignedRecorder,
    hand_collector: HandCollector,
    quest_stream: TimedStream,
    started_monotonic_ns: int,
    deadline: float | None = None,
    camera_streams: dict[str, CameraStream] | None = None,
    camera_max_skew_ns: int = 0,
) -> None:
    last_progress = time.monotonic()
    stop_drain_deadline: float | None = None
    while True:
        if deadline is not None and time.monotonic() >= deadline:
            STOP_EVENT.set()
        if STOP_EVENT.is_set() and stop_drain_deadline is None:
            stop_drain_deadline = time.monotonic() + 1.0
        if (
            stop_drain_deadline is not None
            and time.monotonic() >= stop_drain_deadline
        ):
            warn("停止时数据队列未在 1 秒内排空，剩余帧未写入")
            break
        try:
            hand = hand_collector.frames.get(timeout=0.05)
        except queue.Empty:
            if STOP_EVENT.is_set():
                break
            if (
                time.monotonic() - last_progress >= 1.0
                and recorder.frame_count == 0
            ):
                warn("尚未收到 PXDex 连续采集帧")
                last_progress = time.monotonic()
            continue

        quest, skew_ns = quest_stream.nearest(
            hand["timestamp_monotonic_ns"],
            recorder.max_skew_ns,
        )
        camera_pairs = {
            side: stream.nearest(
                hand["timestamp_monotonic_ns"],
                camera_max_skew_ns,
            )
            for side, stream in (camera_streams or {}).items()
        }
        recorder.write(hand, quest, skew_ns, camera_pairs)
        if recorder.frame_count % 100 == 0:
            recorder.jsonl_file.flush()  # type: ignore[union-attr]
            recorder.csv_file.flush()  # type: ignore[union-attr]
        now = time.monotonic()
        if now - last_progress >= 1.0:
            elapsed = (time.monotonic_ns() - started_monotonic_ns) / 1e9
            rate = recorder.frame_count / elapsed if elapsed > 0.0 else 0.0
            log(
                f"[采集] 帧={recorder.frame_count} 配对={recorder.paired_count} "
                f"pose有效={recorder.pose_tracked_count} 速率={rate:.1f}Hz"
            )
            last_progress = now


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    validate_args(parser, args)
    signal.signal(signal.SIGINT, handle_stop_signal)
    signal.signal(signal.SIGTERM, handle_stop_signal)
    STOP_EVENT.clear()
    FORCE_EXIT_WATCHDOG_ARMED.clear()

    if args.list_devices:
        ports = discover_hand_ports()
        cameras = discover_pxdex_cameras()
        log("[INFO] USB/ACM 串口：")
        for path in ports or ["  未发现"]:
            log(f"  {path}" if ports else path)
        log("[INFO] PXDex MASTER Camera：")
        for path in cameras or ["  未发现"]:
            log(f"  {path}" if cameras else path)
        return 0

    reader: XrtReader | None = None
    poller: QuestPoller | None = None
    quest_setup: Any = None
    quest_ready_sample: TimedSample | None = None
    quest_ready_wait_s: float | None = None
    cameras: list[PxDexCamera] = []
    camera_streams: dict[str, CameraStream] = {}
    camera_ready_wait_s: float | None = None
    hand_collector: HandCollector | None = None
    recorder: AlignedRecorder | None = None
    recorder_entered = False
    poller_started = False
    exit_code = 0
    startup_error: Exception | None = None
    started_monotonic_ns = time.monotonic_ns()
    output_dir = make_output_dir(args.output_dir)
    camera_slots = selected_camera_slots(args)
    camera_ids = tuple(
        f"{args.pxdex_side}_{slot}" for slot in camera_slots
    )
    resolved_hand_port = args.hand_port
    camera_devices: dict[str, str] = {}

    try:
        resolved_hand_port = resolve_hand_port(args.hand_port)
        maybe_start_pc_service(args)
        if not args.skip_adb:
            quest_setup = prepare_quest(args, keep_usb_adb=True)
            log(
                f"[INFO] Quest ADB 已就绪：{quest_setup.serial} "
                f"({'无线' if quest_setup.wireless else 'USB'})"
            )
        else:
            warn(
                "已跳过 ADB：必须自行确认 XRoboToolKit APK 已经启动。"
                "程序仍会等待有效 Quest 数据，收到前不会启动 PXDex 采集。"
            )

        xrt = import_sdk()
        reader = XrtReader(xrt)
        quest_stream = TimedStream(max_samples=max(600, int(args.quest_rate * 10)))
        poller = QuestPoller(reader, quest_stream, args.quest_rate)
        poller.start()
        poller_started = True
        quest_ready_sample, quest_ready_wait_s = wait_for_quest_ready(
            quest_stream,
            args.pose_source,
            args.pose_side,
            args.quest_start_timeout,
            args.quest_ready_samples,
        )

        if camera_slots:
            needs_discovery = any(
                (
                    args.camera_0
                    if slot == "cam0"
                    else args.camera_1
                )
                == "auto"
                for slot in camera_slots
            )
            discovered_cameras = (
                discover_pxdex_cameras() if needs_discovery else []
            )
            if discovered_cameras:
                log(
                    "[INFO] 自动发现 PXDex 摄像头："
                    + ", ".join(discovered_cameras)
                )
            camera_devices = resolve_camera_devices(
                args,
                camera_slots,
                discovered_cameras,
            )
            log(
                "[INFO] PXDex 摄像头映射："
                + ", ".join(
                    f"{args.pxdex_side}_{slot}="
                    f"{camera_devices[slot]}"
                    for slot in camera_slots
                )
            )
            cameras = [
                PxDexCamera(
                    camera_id,
                    camera_devices[slot],
                    args.camera_width,
                    args.camera_height,
                    args.camera_fps,
                    STOP_EVENT,
                )
                for slot, camera_id in zip(camera_slots, camera_ids)
            ]
            for camera in cameras:
                camera.open()
            for camera in cameras:
                camera.start()
            camera_ready_wait_s = wait_for_cameras_ready(
                cameras,
                args.camera_start_timeout,
                args.camera_ready_samples,
            )
            camera_streams = {
                camera.camera_id: camera.stream for camera in cameras
            }

        hand_collector = HandCollector(
            resolved_hand_port,
            args.hand_rate,
            not args.omit_distribution,
        )
        hand_collector.connect()

        recorder = AlignedRecorder(
            output_dir,
            args.pose_source,
            args.pose_side,
            int(args.max_skew_ms * 1_000_000),
            args.pxdex_side,
            camera_ids,
            int(args.camera_max_skew_ms * 1_000_000),
            args.camera_jpeg_quality,
        )
        recorder.__enter__()
        recorder_entered = True
        log(f"[INFO] 数据写入：{output_dir}")

        started_monotonic_ns = time.monotonic_ns()
        hand_collector.start()
        deadline = (
            time.monotonic() + args.duration if args.duration > 0.0 else None
        )
        log("开始同步采集，按 Ctrl+C 停止")
        while not STOP_EVENT.is_set():
            drain_and_record(
                recorder,
                hand_collector,
                quest_stream,
                started_monotonic_ns,
                deadline,
                camera_streams,
                int(args.camera_max_skew_ms * 1_000_000),
            )

    except (QuestSetupError, RuntimeError, OSError, ImportError) as exc:
        startup_error = exc
        error(str(exc))
        exit_code = 2
    finally:
        STOP_EVENT.set()
        hand_stopped = True
        if hand_collector is not None:
            log("[INFO] 停止 PXDex 连续采集 ...")
            hand_stopped, _ = call_with_timeout(
                "停止 PXDex 连续采集",
                hand_collector.stop,
                2.5,
            )
        if poller is not None and poller_started:
            log("[INFO] 停止 Quest SDK 轮询 ...")
            poller.join(timeout=3.0)
            if poller.is_alive():
                warn("Quest 轮询线程未能在 3 秒内退出")
            elif poller.failure is not None:
                error(f"Quest 轮询失败：{poller.failure}")
                if startup_error is None:
                    startup_error = RuntimeError(
                        f"Quest 轮询失败：{poller.failure}"
                    )
                exit_code = 2
        for camera in cameras:
            if camera.ident is not None:
                camera.join(timeout=1.0)
                if camera.is_alive():
                    warn(f"PXDex {camera.camera_id} 摄像头线程未及时退出")
                if camera.failure is not None:
                    error(
                        f"PXDex {camera.camera_id} 摄像头失败：{camera.failure}"
                    )
                    if startup_error is None:
                        startup_error = RuntimeError(
                            f"PXDex {camera.camera_id} 摄像头失败："
                            f"{camera.failure}"
                        )
                    exit_code = 2
        for camera in cameras:
            if not camera.is_alive():
                call_with_timeout(
                    f"关闭 PXDex {camera.camera_id} 摄像头",
                    camera.close,
                    1.0,
                )
        if reader is not None:
            if poller is not None and poller_started and poller.is_alive():
                warn(
                    "Quest SDK 轮询线程仍在 C++ 调用中，"
                    "跳过可能阻塞的 close()"
                )
            else:
                call_with_timeout("关闭 Quest SDK", reader.close, 2.0)

        if (
            recorder is not None
            and recorder_entered
            and hand_collector is not None
        ):
            if poller is not None:
                drain_and_record(
                    recorder,
                    hand_collector,
                    poller.stream,
                    started_monotonic_ns,
                    camera_streams=camera_streams,
                    camera_max_skew_ns=int(
                        args.camera_max_skew_ms * 1_000_000
                    ),
                )
            elapsed = (time.monotonic_ns() - started_monotonic_ns) / 1e9
            actual_hand_rate = (
                recorder.frame_count / elapsed if elapsed > 0.0 else 0.0
            )
            metadata = {
                "quest": {
                    "setup_adb_skipped": args.skip_adb,
                    "apk_package": args.package,
                    "adb_serial": (
                        quest_setup.serial if quest_setup is not None else None
                    ),
                    "adb_wireless": (
                        quest_setup.wireless if quest_setup is not None else None
                    ),
                    "ready_before_hand_start": quest_ready_sample is not None,
                    "ready_wait_seconds": quest_ready_wait_s,
                    "ready_sample_sequence": (
                        quest_ready_sample.data.get("sequence")
                        if quest_ready_sample is not None
                        else None
                    ),
                    "ready_host_timestamp_monotonic_ns": (
                        quest_ready_sample.timestamp_ns
                        if quest_ready_sample is not None
                        else None
                    ),
                    "hand_recording_started_monotonic_ns": (
                        started_monotonic_ns
                    ),
                    "required_consecutive_samples": (
                        args.quest_ready_samples
                    ),
                    "poll_rate_hz": args.quest_rate,
                    "polls": poller.samples if poller is not None else 0,
                    "poller_overruns": (
                        poller.overruns if poller is not None else 0
                    ),
                },
                "hand": {
                    "port": resolved_hand_port,
                    "target_rate_hz": args.hand_rate,
                    "spec_tactile_sample_rate_hz": (
                        PXDEX_TACTILE_SAMPLE_RATE_HZ
                    ),
                    "spec_encoder_sample_rate_hz": (
                        PXDEX_ENCODER_SAMPLE_RATE_HZ
                    ),
                    "actual_rate_hz": actual_hand_rate,
                    "firmware_version": hand_collector.firmware_version,
                    "sn": hand_collector.sn,
                    "snapshot_errors": hand_collector.snapshot_errors,
                    "queue_overflows": hand_collector.queue_overflows,
                    "distribution_included": not args.omit_distribution,
                },
                "camera": {
                    "enabled": bool(camera_slots),
                    "mode": (
                        "none"
                        if not camera_slots
                        else "both"
                        if len(camera_slots) == 2
                        else camera_slots[0]
                    ),
                    "pxdex_side": args.pxdex_side,
                    "slots": list(camera_slots),
                    "camera_ids": list(camera_ids),
                    "ready_before_hand_start": (
                        bool(camera_slots)
                        and camera_ready_wait_s is not None
                    ),
                    "ready_wait_seconds": camera_ready_wait_s,
                    "requested_width": args.camera_width,
                    "requested_height": args.camera_height,
                    "requested_fps": args.camera_fps,
                    "jpeg_quality": args.camera_jpeg_quality,
                    "devices": {
                        camera_id: camera_devices[slot]
                        for slot, camera_id in zip(
                            camera_slots,
                            camera_ids,
                        )
                    },
                    "capture": {
                        camera.camera_id: {
                            "actual_width": camera.actual_width,
                            "actual_height": camera.actual_height,
                            "actual_fps": camera.actual_fps,
                            "frames": camera.frames,
                            "dropped_buffered_frames": camera.dropped,
                            "failure": (
                                str(camera.failure)
                                if camera.failure is not None
                                else None
                            ),
                        }
                        for camera in cameras
                    },
                },
                "error": str(startup_error) if startup_error is not None else None,
            }
            recorder.__exit__(None, None, None)
            manifest_path = recorder.write_manifest(metadata)
            if hand_stopped:
                call_with_timeout(
                    "断开 PXDex",
                    hand_collector.disconnect,
                    2.0,
                )
            else:
                warn("PXDex 停止超时，跳过断开调用以避免再次阻塞")
            log(
                f"[INFO] 采集结束：{recorder.frame_count} 帧，"
                f"配对 {recorder.paired_count}，未配对 {recorder.unpaired_count}，"
                f"pose 有效 {recorder.pose_tracked_count}"
                + (
                    (
                        f"，同一只手的双相机均配对 "
                        f"{recorder.camera_aligned_count}"
                        if len(camera_slots) == 2
                        else f"，{camera_ids[0]} 图像配对 "
                        f"{recorder.camera_aligned_count}"
                    )
                    if camera_slots
                    else ""
                )
            )
            log(f"[INFO] manifest：{manifest_path}")
        elif hand_collector is not None:
            if hand_stopped:
                call_with_timeout(
                    "断开 PXDex",
                    hand_collector.disconnect,
                    2.0,
                )

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
