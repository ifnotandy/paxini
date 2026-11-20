#!/usr/bin/env python3
"""Record PXCap III glove data, two cameras and a Quest 3 6D pose in one HDF5.

PXCap III is a data glove: it has no motors, so this program cannot move
anything. It connects the glove, reads its identity, firmware, fault state and
SN, then starts the SDK collection callback.

Timing policy (identical to ``Dex_Quest/collect_hand_pose.py``):

- One host Unix-nanosecond axis. The glove frame carries
  ``timestamp_unix_ns`` from the SDK; Quest samples are stamped with the
  midpoint of the host-side SDK polling call; video frames carry the pts that
  ffmpeg took from the host wall clock at capture time.
- Pairing is nearest neighbour only: no interpolation, no averaging.
- Every skew is an absolute value. A frame outside tolerance is recorded as an
  unmatched row, never dropped.

The glove callback runs on an SDK-owned thread and must stay short: it copies
the frame into numpy arrays and pushes them into a queue. All file writing
happens on the main thread.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import queue
import re
import shlex
import signal
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence

try:
    import h5py
    import numpy as np
    import yaml
except ImportError as exc:  # pragma: no cover - dependency guard
    print(
        f"[错误] 缺少依赖：{exc}。请运行 "
        "python3.10 -m pip install --user numpy h5py pyyaml",
        file=sys.stderr,
    )
    raise SystemExit(2)

# 实时视图在 ../viewer 下。采集程序单独成目录后要把它加进搜索路径，
# 否则 --view 时函数内的 `from live_view import ...` 会 ImportError
# （延迟导入，--help 测不出来）。
_VIEWER_DIR = Path(__file__).resolve().parent.parent / "viewer"
if str(_VIEWER_DIR) not in sys.path:
    sys.path.insert(0, str(_VIEWER_DIR))

from camera_ffmpeg import (
    FfmpegCamera,
    discover_video_nodes,
    ffmpeg_version,
    find_ffmpeg,
    interrupt_running_cameras,
    wait_for_cameras_ready,
)
from quest3 import (
    BUTTON_METHODS,
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
# 采集程序在 capture/ 下，配置放在上一层（Cap_Quest 根）：
# 配置被采集、延时测量、手套标定三处共用，放根目录避免各自维护一份。
QUEST_ROOT = SCRIPT_DIR.parent
# 输出放在再上一层（Paxini-Cap 根）：采集和 Paxini-Align 都要读同一批目录，
# 放在 Cap_Quest 里面会让下游只能写死一个相对路径绕回来取数据。
CAP_ROOT = QUEST_ROOT.parent
DEFAULT_OUTPUT_ROOT = CAP_ROOT / "Recordings"
DEFAULT_CONFIG_PATH = QUEST_ROOT / "cap_config.yaml"
# PXCap III has no motor/encoder bottleneck, so the default matches the product
# data rate instead of PXDex's 30 Hz (PXDex is limited by its 78 Hz encoder).
DEFAULT_HAND_RATE_HZ = 120
CAP_MAX_RATE_HZ = 300
CAP_USB_ID = "28e9:018a"
CAP_MAX_POINTS = 119
QUEST_JOINT_CAPACITY = 26
# get_body_joints_timestamp() 返回 24 个关节的 IMU 时间戳
BODY_JOINT_TIMESTAMP_CAPACITY = 24
CAMERA_SLOTS = ("cam0", "cam1")
HAND_SIDES = ("left", "right")
POSE_SIDE_CHOICES = ("auto",) + HAND_SIDES
ANGLE_FIELDS = [
    "index_proximal",
    "index_distal",
    "middle_proximal",
    "middle_distal",
]
# 产品命名：index_proximal=FF2, index_distal=FF1, middle_proximal=MF2,
# middle_distal=MF1（顺序不能改，和 SDK 结构体字段顺序一致）
CAP_SLOTS = [
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
CAP_SLOT_POINTS = {
    "thumb_one": 49,
    "thumb_two": 20,
    "index_zero": 49,
    "index_one": 20,
    "middle_zero": 49,
    "middle_one": 20,
    "middle_two": 49,
    "middle_three": 20,
    "palm_zero": 119,
    "palm_one": 49,
}
BUTTON_NAMES = tuple(BUTTON_METHODS)
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
STOP_EVENT = threading.Event()
FORCE_EXIT_WATCHDOG_ARMED = threading.Event()
#: How long the watchdog may spend trying to save the file before giving up.
EMERGENCY_SAVE_TIMEOUT_S = 5.0

# The recorder writing right now, so a signal handler or the shutdown watchdog can
# still save it. Module level because a signal handler only receives (signum, frame).
ACTIVE_RECORDER: "Hdf5Recorder | None" = None
SHUTDOWN_TIMEOUT_S = 30.0
# 粗检阈值：相机/Quest 的时间窗与手套差太多就是录错了对象，只提示不纠正
SESSION_START_TOLERANCE_S = 5.0
FLUSH_ROWS = 100


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


def emergency_save_and_exit(reason: str, exit_code: int = 130) -> None:
    """Save whatever the recorder still holds, then leave.

    Used by the shutdown watchdog (cleanup hung past the budget) and by a second
    Ctrl+C. Only the pending rows are at stake here, but they are the difference
    between a readable file and a 48-byte stub, so it is worth a few milliseconds.
    """
    recorder = ACTIVE_RECORDER
    if recorder is None:
        error(f"{reason}：没有活动中的写入器，直接退出")
    else:
        log(f"[INFO] {reason}：先把内存里未落盘的行写进 data.h5 …")
        if recorder.emergency_save(EMERGENCY_SAVE_TIMEOUT_S):
            warn("已落盘（含未满一批的余数），但 manifest.json 没写、写标志可能未清")
        else:
            warn(
                "主线程卡在 HDF5 调用里，抢不到锁，无法落盘：数据到最后一个已 flush 的批；"
                "强行并发写只会弄坏文件，所以这里直接退出"
            )
    interrupt_running_cameras()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(exit_code)


def force_exit_after_watchdog() -> None:
    time.sleep(SHUTDOWN_TIMEOUT_S)
    if not STOP_EVENT.is_set():
        return
    emergency_save_and_exit(f"安全清理超过 {SHUTDOWN_TIMEOUT_S:g} 秒")


@dataclass(frozen=True, slots=True)
class TimedSample:
    data: dict[str, Any]


class TimedStream:
    """Thread-safe hand-off for Quest samples.

    The collector only stores them; pairing with glove frames happens in a
    separate processing project, so this buffer just preserves order and stays
    bounded if the writer falls behind.
    """

    def __init__(self, max_samples: int) -> None:
        self._samples: deque[TimedSample] = deque(maxlen=max_samples)
        self._lock = threading.Lock()
        self.dropped = 0

    def push(self, sample: TimedSample) -> None:
        with self._lock:
            if len(self._samples) == self._samples.maxlen:
                self.dropped += 1
            self._samples.append(sample)

    def latest(self) -> TimedSample | None:
        with self._lock:
            return self._samples[-1] if self._samples else None

    def drain(self) -> list[TimedSample]:
        """Take everything buffered, oldest first."""
        with self._lock:
            items = list(self._samples)
            self._samples.clear()
            return items


def finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def force_vector(values: Any) -> tuple[int, int, int]:
    return int(values.x), int(values.y), int(values.z)


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
        "sensor_fault_count": int(getattr(fault, "sensor_fault_count", 0)),
        "magnetic_encoder_fault_count": int(
            getattr(fault, "magnetic_encoder_fault_count", 0)
        ),
        "sensor_faults": sensor_faults,
        "magnetic_encoder_faults": magnetic_encoder_faults,
    }


def snapshot_cap_frame(
    frame: Any,
    include_distribution: bool = True,
) -> dict[str, Any]:
    """Copy a PxCap3 callback frame into numpy arrays.

    The SDK pointer is only valid for the duration of the callback, so every
    field is copied here. Distribution force is read through ``count``: the
    public array always has 119 slots but only ``count`` hold valid points.
    """
    encoder = frame.encoder_raw
    angles = frame.joint_angles
    resultant = frame.resultant_force
    distribution = frame.distribute_force

    encoder_raw = np.array(
        [int(getattr(encoder, name)) for name in ANGLE_FIELDS],
        dtype=np.int32,
    )
    joint_angles = np.full(len(ANGLE_FIELDS), math.nan, dtype=np.float64)
    for index, name in enumerate(ANGLE_FIELDS):
        value = finite_float(getattr(angles, name))
        if value is not None:
            joint_angles[index] = value

    resultant_force = np.zeros((len(CAP_SLOTS), 3), dtype=np.int16)
    distribute_force = (
        np.zeros((len(CAP_SLOTS), CAP_MAX_POINTS, 3), dtype=np.int16)
        if include_distribution
        else None
    )
    distribute_counts = np.zeros(len(CAP_SLOTS), dtype=np.uint32)

    for index, slot in enumerate(CAP_SLOTS):
        resultant_force[index] = force_vector(getattr(resultant, slot))

        slot_data = getattr(distribution, slot)
        count = int(getattr(slot_data, "count", 0))
        if distribute_force is None:
            # 不写数组时仍然记下 SDK 报的有效点数
            distribute_counts[index] = min(count, CAP_MAX_POINTS)
            continue
        points = getattr(slot_data, "values", ()) or ()
        # counts 必须等于真正写进数组的点数，否则读的人会按它去取补齐区的 0
        usable = max(0, min(count, len(points), CAP_MAX_POINTS))
        distribute_counts[index] = usable
        for point_index in range(usable):
            point = points[point_index]
            distribute_force[index, point_index, 0] = int(point.x)
            distribute_force[index, point_index, 1] = int(point.y)
            distribute_force[index, point_index, 2] = int(point.z)

    return {
        "timestamp_unix_ns": int(frame.timestamp_unix_ns),
        "timestamp_monotonic_ns": int(frame.timestamp_monotonic_ns),
        "encoder_raw": encoder_raw,
        "joint_angles_deg": joint_angles,
        "resultant_force": resultant_force,
        "distribute_force": distribute_force,
        "distribute_counts": distribute_counts,
    }


def pose_array(values: Any) -> np.ndarray:
    """Pad or trim a pose to 7 finite floats, missing components become NaN."""
    pose = np.full(7, math.nan, dtype=np.float64)
    if not isinstance(values, (list, tuple)) or len(values) < 7:
        return pose
    for index in range(7):
        number = finite_float(values[index])
        if number is not None:
            pose[index] = number
    return pose


def axis_array(values: Any) -> np.ndarray:
    axis = np.full(2, math.nan, dtype=np.float64)
    if not isinstance(values, (list, tuple)):
        return axis
    for index in range(min(2, len(values))):
        number = finite_float(values[index])
        if number is not None:
            axis[index] = number
    return axis


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
    }


def quest_sample_to_row(
    sample: dict[str, Any],
    pose_source: str,
    pose_side: str,
    joint_capacity: int = QUEST_JOINT_CAPACITY,
) -> dict[str, Any]:
    """Flatten one Quest sample into one row of the quest group.

    Every sample becomes exactly one row; nothing is matched against the glove
    here. Missing values are NaN for floats, ``False`` for flags, ``0`` for
    counts and -1 for "SDK did not provide it".
    """
    body_joints = sample.get("body_joints_timestamp_ns")
    if isinstance(body_joints, list) and body_joints:
        joint_stamps = np.full(
            BODY_JOINT_TIMESTAMP_CAPACITY, -1, dtype=np.int64
        )
        for index in range(
            min(len(body_joints), BODY_JOINT_TIMESTAMP_CAPACITY)
        ):
            value = body_joints[index]
            if isinstance(value, int):
                joint_stamps[index] = value
    else:
        joint_stamps = np.full(BODY_JOINT_TIMESTAMP_CAPACITY, -1, dtype=np.int64)

    row: dict[str, Any] = {
        "quest/sdk_timestamp_ns": int(sample.get("sdk_timestamp_ns") or -1),
        "quest/body_timestamp_ns": int(sample.get("body_timestamp_ns") or -1),
        "quest/motion_timestamp_ns": int(sample.get("motion_timestamp_ns") or -1),
        "quest/body_joints_timestamp_ns": joint_stamps,
        "quest/sequence": int(sample.get("sequence") or 0),
        "quest/pose_6d": np.full(7, math.nan, dtype=np.float64),
        "quest/head/pose_xyzw": np.full(7, math.nan, dtype=np.float64),
        "quest/head/tracked": False,
        "quest/head/available": False,
        "quest/tracked": False,
    }
    for side in ("left", "right"):
        row[f"quest/controller_{side}/pose_xyzw"] = np.full(
            7, math.nan, dtype=np.float64
        )
        row[f"quest/controller_{side}/grip"] = math.nan
        row[f"quest/controller_{side}/trigger"] = math.nan
        row[f"quest/controller_{side}/axis"] = np.full(
            2, math.nan, dtype=np.float64
        )
        row[f"quest/hand_{side}/joints_pose_xyzw"] = np.full(
            (joint_capacity, 7), math.nan, dtype=np.float64
        )
        row[f"quest/hand_{side}/joint_count"] = 0
    for name in BUTTON_NAMES:
        row[f"quest/buttons/{name}"] = False

    row["quest/tracked"] = bool(sample.get("tracked"))
    head = sample.get("head") or {}
    row["quest/head/pose_xyzw"] = pose_array(head.get("pose_xyzw"))
    row["quest/head/tracked"] = bool(head.get("tracked"))
    row["quest/head/available"] = bool(head.get("available"))

    controllers = sample.get("controllers") or {}
    for side in ("left", "right"):
        controller = controllers.get(side) or {}
        pose = controller.get("pose") or {}
        row[f"quest/controller_{side}/pose_xyzw"] = pose_array(
            pose.get("pose_xyzw")
        )
        grip = finite_float(controller.get("grip"))
        trigger = finite_float(controller.get("trigger"))
        row[f"quest/controller_{side}/grip"] = (
            grip if grip is not None else math.nan
        )
        row[f"quest/controller_{side}/trigger"] = (
            trigger if trigger is not None else math.nan
        )
        row[f"quest/controller_{side}/axis"] = axis_array(controller.get("axis"))

    hands = sample.get("hands") or {}
    for side in ("left", "right"):
        hand = hands.get(side) or {}
        joints = hand.get("joints_pose_xyzw")
        count = int(hand.get("joint_count") or 0)
        row[f"quest/hand_{side}/joint_count"] = min(count, joint_capacity)
        if not isinstance(joints, list):
            continue
        joints_pose = np.full((joint_capacity, 7), math.nan, dtype=np.float64)
        for index in range(min(len(joints), joint_capacity)):
            joints_pose[index] = pose_array(joints[index])
        row[f"quest/hand_{side}/joints_pose_xyzw"] = joints_pose

    buttons = sample.get("buttons") or {}
    for name in BUTTON_NAMES:
        row[f"quest/buttons/{name}"] = bool(buttons.get(name, False))

    selected = extract_selected_pose(sample, pose_source, pose_side)
    row["quest/pose_6d"] = pose_array(selected["pose_xyzw"])
    return row


class Hdf5Recorder:
    """Append-only HDF5 writer with two independent row axes.

    ``/cap`` follows the glove frames (N rows), ``/quest`` follows the Quest
    samples (Q rows), ``/cameras`` only stores each stream's own timestamps.
    Nothing is matched here on purpose: pairing belongs to the downstream
    processing project, and the collector must not invent relationships.
    """

    CAP_DATASETS = (
        ("timestamp_unix_ns", np.int64, 256),
        ("timestamp_monotonic_ns", np.int64, 256),
        ("encoder_raw", np.int32, 256),
        ("joint_angles_deg", np.float64, 256),
        ("resultant_force", np.int16, 256),
        ("distribute_force", np.int16, 16),
        ("distribute_counts", np.uint32, 256),
    )

    def __init__(
        self,
        output_dir: Path,
        pose_source: str,
        pose_side: str,
        cap_side: str,
        camera_ids: Sequence[str],
        include_distribution: bool,
        joint_capacity: int = QUEST_JOINT_CAPACITY,
        flush_rows: int = FLUSH_ROWS,
    ) -> None:
        self.output_dir = output_dir
        self.pose_source = pose_source
        self.pose_side = pose_side
        self.cap_side = cap_side
        self.camera_ids = tuple(camera_ids)
        self.cameras_enabled = bool(self.camera_ids)
        self.include_distribution = include_distribution
        self.joint_capacity = joint_capacity
        self.flush_rows = max(1, flush_rows)

        self.file: h5py.File | None = None
        # Serialises every HDF5 mutation. The shutdown watchdog runs in its own
        # thread and may try to save the file while the main thread is mid-write;
        # without this lock a reentrant HDF5 call from two threads is undefined.
        self._lock = threading.Lock()
        self.cap_rows = 0
        self.quest_rows = 0
        self._cap_pending: list[dict[str, Any]] = []
        self._quest_pending: list[dict[str, Any]] = []

        self._first_cap_unix_ns: int | None = None
        self._last_cap_unix_ns: int | None = None
        self._first_cap_mono_ns: int | None = None
        self._last_cap_mono_ns: int | None = None

        self._cap_datasets: dict[str, h5py.Dataset] = {}
        self._quest_datasets: dict[str, h5py.Dataset] = {}
        self.started_at = datetime.now().astimezone()
        self.started_monotonic_ns = time.monotonic_ns()

    # -- lifecycle ---------------------------------------------------------
    def __enter__(self) -> "Hdf5Recorder":
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.file = h5py.File(self.output_dir / "data.h5", "w", libver="latest")

        for name, dtype, chunk_rows in self.CAP_DATASETS:
            if name == "distribute_force" and not self.include_distribution:
                continue
            self._cap_datasets[name] = self._dataset(
                f"cap/{name}",
                self._cap_shape(name),
                dtype,
                chunk_rows=chunk_rows,
                compression="gzip" if name == "distribute_force" else None,
            )

        for path, dtype in self._quest_layout():
            self._quest_datasets[path] = self._dataset(
                path,
                self._quest_shape(path),
                dtype,
                chunk_rows=256,
            )

        for camera_id in self.camera_ids:
            self.file.create_group(f"cameras/{camera_id}")
        self.file.create_group("meta")
        return self

    def _cap_shape(self, name: str) -> tuple[int, ...]:
        if name in ("encoder_raw", "joint_angles_deg"):
            return (4,)
        if name == "resultant_force":
            return (len(CAP_SLOTS), 3)
        if name == "distribute_force":
            return (len(CAP_SLOTS), CAP_MAX_POINTS, 3)
        if name == "distribute_counts":
            return (len(CAP_SLOTS),)
        return ()

    def _quest_layout(self) -> list[tuple[str, Any]]:
        specs: list[tuple[str, Any]] = [
            ("quest/sdk_timestamp_ns", np.int64),
            ("quest/body_timestamp_ns", np.int64),
            ("quest/motion_timestamp_ns", np.int64),
            ("quest/body_joints_timestamp_ns", np.int64),
            ("quest/sequence", np.int32),
            ("quest/pose_6d", np.float64),
            ("quest/head/pose_xyzw", np.float64),
            ("quest/head/tracked", bool),
            ("quest/head/available", bool),
        ]
        for side in ("left", "right"):
            specs += [
                (f"quest/controller_{side}/pose_xyzw", np.float64),
                (f"quest/controller_{side}/grip", np.float64),
                (f"quest/controller_{side}/trigger", np.float64),
                (f"quest/controller_{side}/axis", np.float64),
                (f"quest/hand_{side}/joints_pose_xyzw", np.float64),
                (f"quest/hand_{side}/joint_count", np.int32),
            ]
        for name in BUTTON_NAMES:
            specs.append((f"quest/buttons/{name}", bool))
        specs.append(("quest/tracked", bool))
        return specs

    def _quest_shape(self, path: str) -> tuple[int, ...]:
        if path.endswith("body_joints_timestamp_ns"):
            return (BODY_JOINT_TIMESTAMP_CAPACITY,)
        if path.endswith("joints_pose_xyzw"):
            return (self.joint_capacity, 7)
        if path.endswith("/axis"):
            return (2,)
        if path.endswith("pose_6d") or path.endswith("pose_xyzw"):
            return (7,)
        return ()

    def _dataset(
        self,
        path: str,
        trailing: tuple[int, ...],
        dtype: Any,
        *,
        chunk_rows: int,
        compression: str | None = None,
    ) -> h5py.Dataset:
        assert self.file is not None
        options: dict[str, Any] = {
            "shape": (0, *trailing),
            "maxshape": (None, *trailing),
            "dtype": dtype,
            "chunks": (chunk_rows, *trailing),
        }
        if compression is not None:
            options["compression"] = compression
            options["compression_opts"] = 1
        return self.file.create_dataset(path, **options)

    # -- writing -----------------------------------------------------------
    def write_cap(self, hand: dict[str, Any]) -> None:
        unix_ns = int(hand["timestamp_unix_ns"])
        mono_ns = int(hand["timestamp_monotonic_ns"])
        if self._first_cap_unix_ns is None:
            self._first_cap_unix_ns = unix_ns
            self._first_cap_mono_ns = mono_ns
        self._last_cap_unix_ns = unix_ns
        self._last_cap_mono_ns = mono_ns
        self._cap_pending.append(hand)
        if len(self._cap_pending) >= self.flush_rows:
            self.flush_cap()

    def write_quest(self, row: dict[str, Any]) -> None:
        self._quest_pending.append(row)
        if len(self._quest_pending) >= self.flush_rows:
            self.flush_quest()

    def flush_cap(self) -> None:
        with self._lock:
            self._flush_cap_locked()

    def _flush_cap_locked(self) -> None:
        """Caller must hold ``self._lock``."""
        if self.file is None or not self._cap_pending:
            return
        batch = self._cap_pending
        self._cap_pending = []
        start = self.cap_rows
        rows = start + len(batch)
        # Build every array before touching the file. If one dataset's values
        # cannot be built (missing key, bad dtype), no dataset has been resized
        # yet, so the file stays at a consistent row count instead of half-written.
        prepared: list[tuple[h5py.Dataset, np.ndarray]] = [
            (dataset, np.array([item[name] for item in batch], dtype=dataset.dtype))
            for name, dataset in self._cap_datasets.items()
        ]
        for dataset, values in prepared:
            dataset.resize((rows, *dataset.shape[1:]))
            dataset[start:rows] = values
        self.cap_rows = rows
        # Flush after every batch, not only in close(): until HDF5 pushes its
        # metadata to the file, a killed process leaves a data.h5 with no tree at
        # all (root group header is never written) and the whole capture is lost.
        # Measured cost on this machine: 0.07 ms per 100-row batch (~0.8% of the
        # 10 ms write). Note this is flush(), not fsync() — it survives a killed
        # process but not a power cut.
        self.file.flush()

    def flush_quest(self) -> None:
        with self._lock:
            self._flush_quest_locked()

    def _flush_quest_locked(self) -> None:
        """Caller must hold ``self._lock``."""
        if self.file is None or not self._quest_pending:
            return
        batch = self._quest_pending
        self._quest_pending = []
        start = self.quest_rows
        rows = start + len(batch)
        prepared: list[tuple[h5py.Dataset, np.ndarray]] = [
            (dataset, np.array([item[path] for item in batch], dtype=dataset.dtype))
            for path, dataset in self._quest_datasets.items()
        ]
        for dataset, values in prepared:
            dataset.resize((rows, *dataset.shape[1:]))
            dataset[start:rows] = values
        self.quest_rows = rows
        self.file.flush()

    def write_camera_timestamps(
        self,
        camera_id: str,
        timestamps_ns: Sequence[int],
        monotonic_ns: Sequence[int] | None = None,
    ) -> dict[str, Any]:
        """Store one camera's own frame timestamps; no pairing is done here.

        ``monotonic_ns`` is currently always ``None``: the V4L2 path stamps
        frames with the host wall clock, so the ``timestamp_monotonic_ns``
        dataset is left absent. Readers detect that with
        ``"timestamp_monotonic_ns" in group`` or via
        ``manifest.cameras.per_camera.<id>.monotonic_available``.
        """
        timestamps = np.asarray(timestamps_ns, dtype=np.int64)
        if timestamps.size > 1 and np.any(np.diff(timestamps) < 0):
            raise RuntimeError(
                f"PXCap {camera_id} 摄像头时间戳非单调递增，数据不可用"
            )
        mono: np.ndarray | None = None
        if monotonic_ns is not None and len(monotonic_ns) > 0:
            mono = np.asarray(monotonic_ns, dtype=np.int64)
            # Both columns index the same frames. A length mismatch would give the
            # group two datasets with different first dimensions, which nothing
            # downstream checks for (cameras/ is excluded from verify_rows) and
            # which silently breaks frame pairing in Paxini-Align.
            if mono.size != timestamps.size:
                raise RuntimeError(
                    f"PXCap {camera_id} 墙钟 {timestamps.size} 个、单调钟 {mono.size} 个，"
                    "同一路相机的两列长度必须相等"
                )
            if mono.size > 1 and np.any(np.diff(mono) < 0):
                raise RuntimeError(
                    f"PXCap {camera_id} 摄像头单调钟时间戳非单调递增，数据不可用"
                )
        with self._lock:
            assert self.file is not None
            group = self.file["cameras"][camera_id]
            if "timestamp_unix_ns" in group:
                del group["timestamp_unix_ns"]
            group.create_dataset("timestamp_unix_ns", data=timestamps, dtype=np.int64)
            info: dict[str, Any] = {
                "frames": int(timestamps.size),
                "first_timestamp_unix_ns": (
                    int(timestamps[0]) if timestamps.size else None
                ),
                "last_timestamp_unix_ns": (
                    int(timestamps[-1]) if timestamps.size else None
                ),
            }
            if mono is not None:
                if "timestamp_monotonic_ns" in group:
                    del group["timestamp_monotonic_ns"]
                group.create_dataset(
                    "timestamp_monotonic_ns", data=mono, dtype=np.int64
                )
                info["monotonic_available"] = True
                info["first_timestamp_monotonic_ns"] = (
                    int(mono[0]) if mono.size else None
                )
                info["last_timestamp_monotonic_ns"] = (
                    int(mono[-1]) if mono.size else None
                )
            else:
                if "timestamp_monotonic_ns" in group:
                    del group["timestamp_monotonic_ns"]
                info["monotonic_available"] = False
            self.file.flush()
            return info

    # -- derived values ----------------------------------------------------
    @property
    def cap_total(self) -> int:
        return self.cap_rows + len(self._cap_pending)

    @property
    def quest_total(self) -> int:
        return self.quest_rows + len(self._quest_pending)

    @property
    def sample_span_s(self) -> float:
        """Span of the recorded glove frames on the monotonic clock."""
        if self._first_cap_mono_ns is None or self._last_cap_mono_ns is None:
            return 0.0
        return (self._last_cap_mono_ns - self._first_cap_mono_ns) / 1e9

    @property
    def capture_rate_hz(self) -> float:
        """Frame rate measured from the recorded timestamps, not wall time."""
        span = self.sample_span_s
        total = self.cap_total
        if span <= 0.0 or total < 2:
            return 0.0
        return (total - 1) / span

    def cap_window_unix_ns(self) -> tuple[int, int] | None:
        if self._first_cap_unix_ns is None or self._last_cap_unix_ns is None:
            return None
        return self._first_cap_unix_ns, self._last_cap_unix_ns

    # -- verification ------------------------------------------------------
    def verify_rows(self) -> dict[str, Any]:
        """Each group's datasets must agree with that group's own row count."""
        with self._lock:
            assert self.file is not None
            self._flush_cap_locked()
            self._flush_quest_locked()
            mismatches: list[dict[str, Any]] = []
            checked = 0

            def visit(name: str, obj: Any) -> None:
                nonlocal checked
                if not isinstance(obj, h5py.Dataset):
                    return
                if name.startswith("cap/"):
                    expected = self.cap_rows
                elif name.startswith("quest/"):
                    expected = self.quest_rows
                else:
                    return
                checked += 1
                if obj.shape[0] != expected:
                    mismatches.append(
                        {
                            "dataset": name,
                            "rows": int(obj.shape[0]),
                            "expected": expected,
                        }
                    )

            self.file.visititems(visit)
            return {
                "cap_rows": self.cap_rows,
                "quest_rows": self.quest_rows,
                "datasets_checked": checked,
                "row_mismatch": mismatches,
            }

    def write_meta(self, metadata: dict[str, Any]) -> None:
        with self._lock:
            if self.file is None:
                # A shutdown watchdog may have closed the file between manifest()
                # and here; losing meta attributes is better than a traceback in
                # the last second of the run.
                warn("data.h5 已关闭，meta 属性未写入")
                return
            group = self.file["meta"]
            for key, value in metadata.items():
                group.attrs[key] = json.dumps(
                    value,
                    ensure_ascii=False,
                    allow_nan=False,
                )

    def manifest(self, extra: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._flush_cap_locked()
            self._flush_quest_locked()
            return self._manifest_locked(extra)

    def _manifest_locked(self, extra: dict[str, Any]) -> dict[str, Any]:
        ended_at = datetime.now().astimezone()
        elapsed_s = (time.monotonic_ns() - self.started_monotonic_ns) / 1e9
        return {
            "format_version": 4,
            "started_at": self.started_at.isoformat(timespec="milliseconds"),
            "ended_at": ended_at.isoformat(timespec="milliseconds"),
            "cap_side": self.cap_side,
            "rows": self.cap_rows,
            "quest_rows": self.quest_rows,
            "actual_rate_hz": self.capture_rate_hz,
            "duration_s": elapsed_s,
            "sample_span_s": self.sample_span_s,
            "alignment": "none (raw per-source timestamps only)",
            "pose": {
                "source": self.pose_source,
                "side": None if self.pose_source == "head" else self.pose_side,
                "order": ["x", "y", "z", "qx", "qy", "qz", "qw"],
                "coordinate_frame": dict(QUEST_COORDINATE_FRAME),
            },
            "output": {
                "hdf5": "data.h5",
                "cameras": "cameras/" if self.cameras_enabled else None,
                "camera_ids": list(self.camera_ids),
            },
            **extra,
        }

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def _close_locked(self) -> None:
        """Caller must hold ``self._lock``."""
        if self.file is None:
            return
        self._flush_cap_locked()
        self._flush_quest_locked()
        for dataset in self._cap_datasets.values():
            if dataset.shape[0] != self.cap_rows:
                dataset.resize((self.cap_rows, *dataset.shape[1:]))
        for dataset in self._quest_datasets.values():
            if dataset.shape[0] != self.quest_rows:
                dataset.resize((self.quest_rows, *dataset.shape[1:]))
        self.file.flush()
        self.file.close()
        self.file = None

    def emergency_save(self, timeout_s: float = 5.0) -> bool:
        """Last-chance save from another thread (shutdown watchdog / second Ctrl+C).

        Takes the recorder lock with a timeout so it never blocks forever: if the
        main thread is stuck *inside* HDF5, grabbing the lock would deadlock, and
        a concurrent write would corrupt the file — in that case return False and
        let the caller hard-exit, which keeps the data already flushed.

        Returns True when the file was closed cleanly (pending rows included).
        """
        if self.file is None:
            return True  # a normal shutdown already finished the job
        if not self._lock.acquire(timeout=max(0.1, timeout_s)):
            return False
        try:
            self._close_locked()
            return True
        except Exception as exc:  # a broken file is still better than no file
            warn(f"紧急落盘失败：{exc.__class__.__name__}: {exc}")
            return False
        finally:
            self._lock.release()


class HandCollector:
    """PxCap3 session: connect, read identity, then stream through a callback."""

    def __init__(
        self,
        port: str,
        frequency_hz: int,
        include_distribution: bool,
    ) -> None:
        import pxhandsdk

        if not hasattr(pxhandsdk, "PxCap3"):
            raise RuntimeError(
                "当前解释器加载不到 PxCap3 绑定：系统 python3 是 3.12 时 "
                "import 能过但类不存在。请一律用 python3.10 运行"
            )
        self.pxhandsdk = pxhandsdk
        self.port = port
        self.frequency_hz = frequency_hz
        self.include_distribution = include_distribution
        self.hand = pxhandsdk.PxCap3()
        self.frames: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=4096)
        self.collection_started = False
        self.snapshot_errors = 0
        self.queue_overflows = 0
        self.callback_count = 0
        self.callback_total_ns = 0
        self.callback_max_ns = 0
        self.sdk_version: str | None = None
        self.controller_version: str | None = None
        self.sensor_versions: list[str] = []
        self.sn = None
        self.fault: dict[str, Any] | None = None
        self.fault_error: str | None = None
        self.info_errors: dict[str, str] = {}

    def connect(self) -> None:
        rc = self.hand.connect_device(self.port)
        if rc != 0:
            code, message = self.hand.get_last_error()
            raise RuntimeError(
                f"PXCap 连接失败 rc={rc}, code={code}, message={message}"
            )
        log(f"[INFO] PXCap 已连接：{self.port}")

        # Everything except is_connected/stop/disconnect returns 4000 once
        # collection starts, so identity must be read here, before start().
        rc, sdk_version = self.hand.get_sdk_version()
        if rc == 0:
            self.sdk_version = str(sdk_version)
            log(f"[INFO] PXCap SDK：{self.sdk_version}")
        else:
            self._note_error("get_sdk_version")

        rc, firmware = self.hand.get_firmware_versions()
        if rc == 0 and firmware is not None:
            self.controller_version = str(firmware.controller_version)
            self.sensor_versions = [
                str(value) for value in firmware.sensor_versions
            ][: int(firmware.sensor_version_count)]
            log(f"[INFO] PXCap 固件：{self.controller_version}")
            if self.sensor_versions:
                log(
                    f"[INFO] PXCap 传感器模组："
                    f"{len(self.sensor_versions)} 个 "
                    f"({', '.join(self.sensor_versions)})"
                )
        else:
            self._note_error("get_firmware_versions")

        rc, sn = self.hand.get_sn()
        if rc == 0:
            self.sn = str(sn)
            log(f"[INFO] PXCap SN：{self.sn}")
        else:
            self._note_error("get_sn")

        rc, fault = self.hand.get_fault_code()
        if rc == 0 and fault is not None:
            self.fault = fault_to_dict(fault)
            log(
                "[INFO] PXCap 故障："
                f"传感器 {self.fault['sensor_fault_count']} 项，"
                f"磁编码器 {self.fault['magnetic_encoder_fault_count']} 项"
            )
        else:
            code, message = self.hand.get_last_error()
            self.fault_error = f"rc={rc}, code={code}, message={message}"
            warn(f"读取 PXCap 故障信息失败：{self.fault_error}")

    def _note_error(self, method: str) -> None:
        code, message = self.hand.get_last_error()
        self.info_errors[method] = f"code={code}, message={message}"
        warn(f"读取 {method} 失败：code={code}, message={message}")

    def _on_frame(self, frame: Any) -> None:
        """SDK thread: copy, enqueue, return. No I/O, no other SDK calls."""
        started_ns = time.perf_counter_ns()
        try:
            sample = snapshot_cap_frame(
                frame,
                self.include_distribution,
            )
        except Exception as exc:
            self.snapshot_errors += 1
            if self.snapshot_errors <= 3:
                warn(f"PXCap 采集帧复制失败：{exc}")
            return
        try:
            self.frames.put_nowait(sample)
        except queue.Full:
            self.queue_overflows += 1
            if self.queue_overflows <= 3:
                warn("PXCap 数据队列已满，出现掉帧")
        elapsed_ns = time.perf_counter_ns() - started_ns
        self.callback_count += 1
        self.callback_total_ns += elapsed_ns
        if elapsed_ns > self.callback_max_ns:
            self.callback_max_ns = elapsed_ns

    @property
    def callback_mean_ms(self) -> float:
        if self.callback_count == 0:
            return 0.0
        return self.callback_total_ns / self.callback_count / 1e6

    @property
    def callback_max_ms(self) -> float:
        return self.callback_max_ns / 1e6

    def start(self) -> None:
        rc = self.hand.start_collection(self.frequency_hz, self._on_frame)
        if rc != 0:
            code, message = self.hand.get_last_error()
            raise RuntimeError(
                f"PXCap 连续采集启动失败 rc={rc}, code={code}, message={message}"
            )
        self.collection_started = True
        log(f"[INFO] PXCap 连续采集已启动：{self.frequency_hz} Hz")

    def stop(self) -> None:
        if self.collection_started:
            rc = self.hand.stop_collection()
            if rc != 0:
                code, message = self.hand.get_last_error()
                warn(
                    "停止 PXCap 连续采集失败："
                    f"rc={rc}, code={code}, message={message}"
                )
            self.collection_started = False

    def disconnect(self) -> None:
        rc = self.hand.disconnect_device()
        if rc != 0:
            warn(f"断开 PXCap 返回错误码 {rc}")


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
                # 时间戳由 quest3.XrtReader.collect_sample 一并返回，这里只负责
                # 入队，不追加任何主机侧时刻
                self.stream.push(TimedSample(data=self.reader.collect_sample(sequence)))
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "采集 PXCap III 连续帧、左右摄像头视频和 Quest 3 样本，写成单个 HDF5。"
            "只存原始数据与各自时间戳，不做跨源对齐（对齐另有下游工程）。"
            "手套没有电机，本程序不会产生任何运动。"
        )
    )
    parser.add_argument(
        "--hand-port",
        default="auto",
        help=(
            "PXCap 串口；默认 auto，按 USB VID:PID 28e9:018a 自动发现"
        ),
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="列出自动发现的串口、视频节点和 ffmpeg 版本后退出",
    )
    parser.add_argument(
        "--hand-rate",
        type=int,
        default=DEFAULT_HAND_RATE_HZ,
        help=(
            f"PXCap 连续采集目标频率，默认 {DEFAULT_HAND_RATE_HZ} Hz；"
            f"SDK 建议不超过 {CAP_MAX_RATE_HZ} Hz"
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
        choices=POSE_SIDE_CHOICES,
        default="auto",
        help=(
            "controller/hand pose 的左右手；auto 跟随 --cap-side，"
            "left/right 可显式覆盖"
        ),
    )
    parser.add_argument(
        "--cap-side",
        choices=HAND_SIDES,
        default="right",
        help="当前单只 PXCap 的手别，默认 right；决定相机命名和 Quest 默认手别",
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
        help="开始采集手套前要求连续收到的有效 Quest pose 数，默认 3",
    )
    parser.add_argument(
        "--no-cameras",
        action="store_true",
        help="不录制摄像头视频；等价于 --camera-mode none",
    )
    parser.add_argument(
        "--camera-mode",
        choices=("both", "cam0", "cam1", "none"),
        default="both",
        help="相机：both、cam0、cam1 或 none，默认 both",
    )
    parser.add_argument(
        "--camera-0",
        default="auto",
        help="cam0 设备节点；默认 auto，按 sysfs 设备路径排序取第一个",
    )
    parser.add_argument(
        "--camera-1",
        default="auto",
        help="cam1 设备节点；默认 auto，按 sysfs 设备路径排序取第二个",
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
        "--camera-start-timeout",
        type=float,
        default=15.0,
        help="等待所选摄像头出帧的超时秒数，默认 15",
    )
    parser.add_argument(
        "--camera-ready-samples",
        type=int,
        default=3,
        help="开始采集手套前每路摄像头要求的连续新帧数，默认 3",
    )
    parser.add_argument(
        "--video-codec",
        choices=("copy", "h264", "hevc", "mjpeg"),
        default="copy",
        help=(
            "视频编码，默认 copy（MJPEG 直通，CPU 占用最低）；"
            "h264/hevc 省空间但吃 CPU，mjpeg 重新编码"
        ),
    )
    parser.add_argument(
        "--output-dir",
        help="输出目录；默认 <Paxini-Cap>/Recordings/data_<日期>_<序号>",
    )
    parser.add_argument(
        "--omit-distribution",
        action="store_true",
        help="不写 444 点分布力，以减小文件体积",
    )
    parser.add_argument(
        "--view",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "采集时开一个实时窗口（关节角 / 合力曲线 / 10 路合力数值表）。"
            "视图只读内存里的帧，不碰 data.h5；窗口卡住或关掉都不影响录制。"
            "配置文件里写 \"view\": true 可默认打开，命令行 --no-view 可临时关掉"
        ),
    )
    parser.add_argument(
        "--view-fps",
        type=float,
        default=20.0,
        help="实时窗口刷新率，默认 20；跑在子进程里，不占采集主循环",
    )
    parser.add_argument(
        "--view-mode",
        choices=("auto", "html", "tk"),
        default="auto",
        help=(
            "实时视图的形式。auto（默认）= 有 X/Wayland 会话用 tk、没有则用 html；"
            "html = 本地页面 http://127.0.0.1:8765/（浏览器看，不依赖窗口管理器）；"
            "tk = matplotlib 独立窗口"
        ),
    )
    parser.add_argument(
        "--view-video",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "实时窗口里显示相机关键帧（默认开）。ffmpeg 在录 mkv 的同时多输出"
            "一路缩小、抽帧的 MJPEG 到管道，主进程只取最新一帧，不参与编码，"
            "所以不影响录制；没有相机时自动忽略。--no-view-video 关掉"
        ),
    )
    parser.add_argument(
        "--view-video-fps",
        type=float,
        default=5.0,
        help="实时预览的抽帧帧率，默认 5；调高会多占 ffmpeg 一点 CPU",
    )
    parser.add_argument(
        "--view-video-width",
        type=int,
        default=360,
        help="实时预览的宽度（像素），默认 360，高度按比例",
    )
    parser.add_argument(
        "--no-quest",
        action="store_true",
        help=(
            "不采集 Quest：跳过 PC Service/ADB 与 pose 等待，quest 数据全部按"
            "缺失写入（NaN/False），用于只录手套与相机的场景"
        ),
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
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help=(
            "YAML 配置文件路径，默认 Cap_Quest/cap_config.yaml（存在即加载）；"
            "命令行参数优先于配置文件"
        ),
    )
    parser.add_argument(
        "--no-pc-service-check",
        action="store_true",
        help="不检查 PC Service 进程",
    )
    return parser


def validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if not 1 <= args.hand_rate <= CAP_MAX_RATE_HZ:
        parser.error(
            f"--hand-rate 必须在 1..{CAP_MAX_RATE_HZ} Hz 范围内："
            "SDK 建议连续采集不超过 300 Hz"
        )
    if not 0 < args.quest_rate <= 240:
        parser.error("--quest-rate 必须在 (0, 240] Hz 范围内")
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
    if args.camera_start_timeout <= 0:
        parser.error("--camera-start-timeout 必须大于 0")
    if args.camera_ready_samples < 1:
        parser.error("--camera-ready-samples 必须大于或等于 1")
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
    if args.no_quest and args.start_pc_service:
        parser.error("--no-quest 与 --start-pc-service 不能同时使用")


def resolve_pose_side(cap_side: str, requested_pose_side: str) -> str:
    """Resolve the Quest side without changing either source's raw fields."""
    if requested_pose_side == "auto":
        return cap_side
    if requested_pose_side in HAND_SIDES:
        return requested_pose_side
    raise ValueError(f"未知 pose side：{requested_pose_side!r}")


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


def create_output_dir(path: Path) -> None:
    """Create the recording directory; never reuse an existing one."""
    try:
        path.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise RuntimeError(
            f"输出目录已存在：{path}。请删除旧目录或换一个 --output-dir"
        ) from exc


def write_startup_failure(
    output_dir: Path,
    error: Exception | None,
    *,
    started_at: datetime,
    cap_side: str,
    pose_source: str,
    pose_side: str,
    pose_side_requested: str,
    hand_port: str,
    camera_devices: dict[str, str],
    cameras: Sequence[FfmpegCamera],
    ffmpeg: str | None,
) -> Path:
    """Preserve diagnostics when startup fails before data.h5 is created.

    The normal run deletes a partially-created output directory. Keeping a
    failed directory would otherwise look like a recording, so this writes an
    explicit status file and never creates a fake HDF5 file.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    camera_info: dict[str, Any] = {}
    for camera in cameras:
        command: list[str] | None = None
        command_error: str | None = None
        try:
            command = camera.command()
        except Exception as exc:  # diagnostics must survive a broken backend
            command_error = f"{exc.__class__.__name__}: {exc}"
        camera_info[camera.camera_id] = {
            "device": camera.device,
            "command": command,
            "command_text": shlex.join(command) if command is not None else None,
            "command_error": command_error,
            "video": str(camera.video_path),
            "log": str(camera.log_path),
            "log_tail": camera.log_tail(),
            "failure": (
                str(camera.failure) if camera.failure is not None else None
            ),
        }

    payload = {
        "status": "startup_failed",
        "started_at": started_at.isoformat(timespec="milliseconds"),
        "ended_at": datetime.now().astimezone().isoformat(
            timespec="milliseconds"
        ),
        "error": str(error) if error is not None else "采集启动未完成",
        "cap_side": cap_side,
        "pose": {
            "source": pose_source,
            "side": pose_side,
            "side_requested": pose_side_requested,
        },
        "hand_port": hand_port,
        "camera_devices": dict(camera_devices),
        "cameras": camera_info,
        "env": environment_info(ffmpeg),
    }
    path = output_dir / "startup_failure.json"
    with path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2, allow_nan=False)
        file.write("\n")
    return path


def load_config_file(path: Path) -> dict[str, Any]:
    """Read the optional YAML config.

    Keys are the long option names with underscores (``camera_mode``). YAML is a
    superset of JSON, so an old ``cap_config.json`` still parses; comments are
    real ``#`` lines now, but keys starting with ``_`` are still skipped so that
    older files keep working.
    """
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"无法读取配置文件：{path}（{exc}）") from exc
    try:
        payload = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f"第 {mark.line + 1} 行第 {mark.column + 1} 列：" if mark else ""
        raise RuntimeError(f"配置文件不是合法 YAML：{path}（{where}{exc}）") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"配置文件顶层必须是映射（键: 值）：{path}")
    return {
        str(key): value
        for key, value in payload.items()
        if not str(key).startswith("_") and value is not None
    }


def apply_config(
    parser: argparse.ArgumentParser,
    config: dict[str, Any],
    path: Path,
) -> None:
    """Apply config entries as parser defaults, so CLI arguments still win."""
    actions = {action.dest: action for action in parser._actions}
    unknown = sorted(key for key in config if key not in actions)
    if unknown:
        raise RuntimeError(
            f"配置文件 {path} 含未知配置项：{', '.join(unknown)}"
        )

    defaults: dict[str, Any] = {}
    for key, value in config.items():
        action = actions[key]
        if action.choices is not None and value not in action.choices:
            raise RuntimeError(
                f"配置项 {key} 必须是 {'/'.join(map(str, action.choices))} "
                f"之一，实际是 {value!r}"
            )
        if isinstance(action.default, bool):
            if not isinstance(value, bool):
                raise RuntimeError(
                    f"配置项 {key} 必须是 true 或 false，实际是 {value!r}"
                )
            defaults[key] = value
            continue
        try:
            if action.type is int:
                defaults[key] = int(value)
            elif action.type is float:
                defaults[key] = float(value)
            elif action.type is str:
                defaults[key] = str(value)
            else:
                defaults[key] = value
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"配置项 {key} 的值无效：{value!r}") from exc
    parser.set_defaults(**defaults)


@dataclass(frozen=True, slots=True)
class SerialPort:
    path: str
    by_id: str | None
    usb_id: str | None


def usb_ids_for_tty(name: str, sysfs_root: Path = Path("/sys/class/tty")) -> str | None:
    """Walk up sysfs from a tty node to the USB device's idVendor:idProduct."""
    directory = sysfs_root / name / "device"
    try:
        directory = directory.resolve(strict=True)
    except OSError:
        return None
    for _ in range(8):
        vendor = directory / "idVendor"
        product = directory / "idProduct"
        if vendor.is_file() and product.is_file():
            try:
                return (
                    f"{vendor.read_text(encoding='utf-8').strip()}:"
                    f"{product.read_text(encoding='utf-8').strip()}"
                ).lower()
            except OSError:
                return None
        if directory.parent == directory:
            return None
        directory = directory.parent
    return None


def discover_hand_ports(dev_root: Path = Path("/dev")) -> list[SerialPort]:
    """Discover USB/ACM serial ports without depending on tty numbering."""
    found: dict[str, SerialPort] = {}
    by_id = dev_root / "serial" / "by-id"
    if by_id.is_dir():
        for entry in sorted(by_id.iterdir()):
            try:
                resolved = entry.resolve(strict=True)
            except OSError:
                continue
            if not re.fullmatch(r"tty(?:USB|ACM)\d+", resolved.name):
                continue
            found.setdefault(
                str(resolved),
                SerialPort(
                    path=str(resolved),
                    by_id=entry.name,
                    usb_id=usb_ids_for_tty(resolved.name),
                ),
            )

    for pattern in ("ttyUSB*", "ttyACM*"):
        for entry in sorted(dev_root.glob(pattern)):
            try:
                resolved = entry.resolve(strict=True)
            except OSError:
                continue
            found.setdefault(
                str(resolved),
                SerialPort(
                    path=str(resolved),
                    by_id=None,
                    usb_id=usb_ids_for_tty(resolved.name),
                ),
            )
    return [found[key] for key in sorted(found)]


def resolve_hand_port(
    requested: str,
    dev_root: Path = Path("/dev"),
) -> str:
    if requested != "auto":
        path = Path(requested).expanduser()
        if not path.exists():
            raise RuntimeError(f"PXCap 串口不存在：{path}")
        return str(path)

    ports = discover_hand_ports(dev_root)
    if not ports:
        raise RuntimeError(
            "未发现可用的串口。请检查手套 USB 线缆、dialout 权限，"
            "或用 --hand-port /dev/ttyACM0 指定"
        )

    matches = [port for port in ports if port.usb_id == CAP_USB_ID]
    if len(matches) == 1:
        log(f"[INFO] 自动发现 PXCap 串口：{matches[0].path}（USB {CAP_USB_ID}）")
        return matches[0].path
    if len(matches) > 1:
        formatted = "\n".join(f"  {port.path}" for port in matches)
        raise RuntimeError(
            f"检测到多个 USB {CAP_USB_ID} 串口，无法判断哪一个是 PXCap：\n"
            f"{formatted}\n请使用 --hand-port 指定"
        )
    if len(ports) == 1:
        warn(
            f"未发现 USB {CAP_USB_ID} 的设备，按唯一串口继续：{ports[0].path}"
        )
        return ports[0].path

    formatted = "\n".join(
        f"  {port.path}（USB {port.usb_id or '未知'}）" for port in ports
    )
    raise RuntimeError(
        f"检测到多个串口，但没有匹配 USB {CAP_USB_ID} 的设备：\n"
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
                f"自动发现 PXCap {slot} 摄像头失败。请用 "
                "--camera-0/--camera-1 指定设备节点"
            )
        devices[slot] = discovered[index]
    if len(set(devices.values())) != len(devices):
        raise RuntimeError("两路摄像头解析到了同一个设备，请手动指定")
    return devices


def selected_camera_slots(args: argparse.Namespace) -> tuple[str, ...]:
    mode = "none" if args.no_cameras else args.camera_mode
    if mode == "both":
        return CAMERA_SLOTS
    if mode in CAMERA_SLOTS:
        return (mode,)
    return ()


def list_devices() -> int:
    ports = discover_hand_ports()

    log("[INFO] USB/ACM 串口：")
    if not ports:
        log("  未发现")
    for port in ports:
        marker = "PXCap" if port.usb_id == CAP_USB_ID else "-"
        by_id = f" by-id={port.by_id}" if port.by_id else ""
        log(f"  {port.path} USB={port.usb_id or '未知'} {marker}{by_id}")

    nodes = discover_video_nodes()
    log("[INFO] V4L2 视频采集节点（video-index 0）：")
    if not nodes:
        log("  未发现")
    for node in nodes:
        log(f"  {node}")

    try:
        ffmpeg = find_ffmpeg()
    except RuntimeError as exc:
        warn(str(exc))
        log("[INFO] ffmpeg：未找到")
    else:
        log(f"[INFO] ffmpeg：{ffmpeg_version(ffmpeg)}（{ffmpeg}）")
    return 0


def handle_stop_signal(signum: int, _frame: Any) -> None:
    if STOP_EVENT.is_set():
        emergency_save_and_exit("再次收到停止信号，不再等安全清理", exit_code=130)
        return
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


def pump_and_record(
    recorder: Hdf5Recorder,
    hand_collector: HandCollector,
    quest_stream: TimedStream,
    started_monotonic_ns: int,
    deadline: float | None = None,
    cameras: Sequence[FfmpegCamera] = (),
    final: bool = False,
    view: Any | None = None,
) -> None:
    """Move both queues into the HDF5, each on its own row axis.

    ``final=False`` is the recording loop: as soon as a stop is requested it
    returns, so the glove can be stopped before the tail is drained. ``final``
    runs after ``stop_collection()`` and keeps going until the queues are empty
    (1 s safety net), which is what keeps the last frames on disk.
    """
    last_progress = time.monotonic()
    stop_drain_deadline: float | None = None
    while True:
        if deadline is not None and time.monotonic() >= deadline:
            STOP_EVENT.set()
        if STOP_EVENT.is_set():
            if not final:
                break
            if stop_drain_deadline is None:
                stop_drain_deadline = time.monotonic() + 1.0
            elif time.monotonic() >= stop_drain_deadline:
                pending = hand_collector.frames.qsize()
                if pending > 0:
                    warn(f"停止时手套队列仍有约 {pending} 帧未写入")
                for sample in quest_stream.drain():
                    recorder.write_quest(
                        quest_sample_to_row(
                            sample.data,
                            recorder.pose_source,
                            recorder.pose_side,
                            recorder.joint_capacity,
                        )
                    )
                break

        for sample in quest_stream.drain():
            recorder.write_quest(
                quest_sample_to_row(
                    sample.data,
                    recorder.pose_source,
                    recorder.pose_side,
                    recorder.joint_capacity,
                )
            )

        try:
            hand = hand_collector.frames.get(timeout=0.05)
        except queue.Empty:
            if STOP_EVENT.is_set():
                break
            if (
                time.monotonic() - last_progress >= 1.0
                and recorder.cap_total == 0
            ):
                warn("尚未收到 PXCap 连续采集帧")
                last_progress = time.monotonic()
            continue

        recorder.write_cap(hand)
        if view is not None:
            view.push(hand)

        now = time.monotonic()
        if now - last_progress >= 1.0:
            for camera in cameras:
                camera.poll_health()
            elapsed = (time.monotonic_ns() - started_monotonic_ns) / 1e9
            rate = recorder.cap_total / elapsed if elapsed > 0.0 else 0.0
            log(
                f"[采集] 手套帧={recorder.cap_total} "
                f"Quest样本={recorder.quest_total} 速率={rate:.1f}Hz"
            )
            last_progress = now


def environment_info(ffmpeg: str | None) -> dict[str, Any]:
    info: dict[str, Any] = {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "numpy": np.__version__,
        "h5py": h5py.__version__,
    }
    if ffmpeg is not None:
        info["ffmpeg"] = ffmpeg_version(ffmpeg)
        info["ffmpeg_path"] = ffmpeg
    return info


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    # Read --config first, then turn the file into defaults, then parse again so
    # an explicit command-line argument still overrides the file.
    pre_args, _ = parser.parse_known_args(argv)
    config_path = Path(pre_args.config).expanduser()
    try:
        config = load_config_file(config_path)
        if config:
            apply_config(parser, config, config_path)
    except RuntimeError as exc:
        error(str(exc))
        return 2
    args = parser.parse_args(argv)
    if config:
        log(f"[INFO] 已加载配置：{config_path}（{len(config)} 项）")
    elif config_path == DEFAULT_CONFIG_PATH:
        # 默认文件不存在时，检查是不是还留着改名前的那份 JSON：静默忽略会让人
        # 以为配置生效了，实际跑的是内置默认值。
        legacy = DEFAULT_CONFIG_PATH.with_suffix(".json")
        if legacy.is_file():
            warn(
                f"磁盘上还有 {legacy.name}，但它已不再自动加载；"
                f"改名为 {DEFAULT_CONFIG_PATH.name} 才会生效"
            )
    validate_args(parser, args)
    pose_side_requested = args.pose_side
    args.pose_side = resolve_pose_side(args.cap_side, pose_side_requested)
    if pose_side_requested == "auto":
        log(f"[INFO] Quest 手别跟随手套：{args.cap_side}")
    elif args.pose_side != args.cap_side:
        warn(
            f"手套手别为 {args.cap_side}，Quest 显式使用 "
            f"{args.pose_side}；两路数据不会交换或镜像"
        )
    signal.signal(signal.SIGINT, handle_stop_signal)
    signal.signal(signal.SIGTERM, handle_stop_signal)
    STOP_EVENT.clear()
    FORCE_EXIT_WATCHDOG_ARMED.clear()

    if args.list_devices:
        return list_devices()

    reader: XrtReader | None = None
    poller: QuestPoller | None = None
    quest_setup: Any = None
    quest_ready_sample: TimedSample | None = None
    quest_ready_wait_s: float | None = None
    cameras: list[FfmpegCamera] = []
    camera_ready_wait_s: float | None = None
    camera_devices: dict[str, str] = {}
    camera_videos: dict[str, Any] = {}
    camera_errors: dict[str, str] = {}
    hand_collector: HandCollector | None = None
    recorder: Hdf5Recorder | None = None
    live_view: Any | None = None
    recorder_entered = False
    poller_started = False
    output_dir_created = False
    exit_code = 0
    startup_error: Exception | None = None
    session_started_at = datetime.now().astimezone()
    started_monotonic_ns = time.monotonic_ns()
    output_dir = make_output_dir(args.output_dir)
    output_dir_preexisting = output_dir.exists()
    camera_slots = selected_camera_slots(args)
    camera_ids = tuple(f"{args.cap_side}_{slot}" for slot in camera_slots)
    slot_by_camera = dict(zip(camera_ids, camera_slots))
    resolved_hand_port = args.hand_port
    ffmpeg: str | None = None
    quest_stream = TimedStream(max_samples=max(600, int(args.quest_rate * 10)))

    try:
        resolved_hand_port = resolve_hand_port(args.hand_port)
        if args.no_quest:
            warn(
                "已禁用 Quest（--no-quest）：只采集手套与相机，"
                "quest 组全部按缺失写入（NaN/False）"
            )
        else:
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
                    "程序仍会等待有效 Quest 数据，收到前不会启动 PXCap 采集。"
                )

            xrt = import_sdk()
            reader = XrtReader(xrt)
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
            ffmpeg = find_ffmpeg()
            needs_discovery = any(
                (args.camera_0 if slot == "cam0" else args.camera_1) == "auto"
                for slot in camera_slots
            )
            discovered = discover_video_nodes() if needs_discovery else []
            if discovered:
                log("[INFO] 自动发现视频节点：" + ", ".join(discovered))
            camera_devices = resolve_camera_devices(
                args,
                camera_slots,
                discovered,
            )
            log(
                "[INFO] PXCap 摄像头映射："
                + ", ".join(
                    f"{args.cap_side}_{slot}={camera_devices[slot]}"
                    for slot in camera_slots
                )
            )
            create_output_dir(output_dir)
            output_dir_created = True
            camera_dir = output_dir / "cameras"
            camera_dir.mkdir(parents=True, exist_ok=True)
            cameras = [
                FfmpegCamera(
                    camera_id,
                    camera_devices[slot],
                    camera_dir,
                    args.camera_width,
                    args.camera_height,
                    args.camera_fps,
                    args.video_codec,
                    ffmpeg,
                    preview_fps=(
                        args.view_video_fps
                        if args.view and args.view_video
                        else 0.0
                    ),
                    preview_width=args.view_video_width,
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
                STOP_EVENT,
            )

        hand_collector = HandCollector(
            resolved_hand_port,
            args.hand_rate,
            not args.omit_distribution,
        )
        hand_collector.connect()

        if not output_dir_created:
            create_output_dir(output_dir)
            output_dir_created = True

        recorder = Hdf5Recorder(
            output_dir,
            args.pose_source,
            args.pose_side,
            args.cap_side,
            camera_ids,
            not args.omit_distribution,
        )
        recorder.__enter__()
        recorder_entered = True
        global ACTIVE_RECORDER
        ACTIVE_RECORDER = recorder
        log(f"[INFO] 数据写入：{output_dir}")

        if args.view:
            try:
                camera_ids = tuple(
                    camera.camera_id for camera in cameras if camera.preview_fps > 0
                )
                title = f"PXCap III 实时 · {output_dir.name}"
                from live_view import default_view_mode

                view_mode = (
                    args.view_mode if args.view_mode != "auto" else default_view_mode()
                )
                if view_mode == "html":
                    from live_view import HtmlLiveView

                    live_view = HtmlLiveView(
                        CAP_SLOTS,
                        fps=args.view_video_fps,
                        title=title,
                        camera_ids=camera_ids,
                    )
                    live_view.attach_cameras(cameras)
                    log(f"[INFO] 实时视图（浏览器版）：{live_view.url}")
                else:
                    from live_view import LiveView

                    live_view = LiveView(
                        CAP_SLOTS,
                        fps=args.view_fps,
                        title=title,
                        camera_ids=camera_ids,
                    )
                    live_view.attach_cameras(cameras)
                    log(
                        "[INFO] 实时视图已在独立进程打开"
                        "（关窗或按 q 只关视图，录制继续）"
                    )
            except Exception as exc:
                live_view = None
                warn(f"实时视图打不开：{exc}；采集继续，可以用 --html 事后回放")

        started_monotonic_ns = time.monotonic_ns()
        hand_collector.start()
        deadline = (
            time.monotonic() + args.duration if args.duration > 0.0 else None
        )
        log("开始同步采集，按 Ctrl+C 停止")
        while not STOP_EVENT.is_set():
            pump_and_record(
                recorder,
                hand_collector,
                quest_stream,
                started_monotonic_ns,
                deadline,
                cameras,
                view=live_view,
            )

    except (QuestSetupError, RuntimeError, OSError, ImportError) as exc:
        startup_error = exc
        error(str(exc))
        exit_code = 2
    finally:
        STOP_EVENT.set()
        hand_stopped = True
        if hand_collector is not None:
            log("[INFO] 停止 PXCap 连续采集 ...")
            hand_stopped, _ = call_with_timeout(
                "停止 PXCap 连续采集",
                hand_collector.stop,
                2.5,
            )
        if (
            recorder is not None
            and recorder_entered
            and hand_collector is not None
        ):
            pump_and_record(
                recorder,
                hand_collector,
                quest_stream,
                started_monotonic_ns,
                final=True,
                view=live_view,
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

        # Stop the glove first, then the cameras: ffmpeg only needs SIGINT after
        # no more frames will be paired against it.
        for camera in cameras:
            log(f"[INFO] 停止 PXCap {camera.camera_id} 摄像头 ...")
            call_with_timeout(
                f"停止 PXCap {camera.camera_id} 摄像头",
                camera.stop,
                6.0,
            )
            if camera.failure is not None and startup_error is None:
                startup_error = RuntimeError(
                    f"PXCap {camera.camera_id} 摄像头失败：{camera.failure}"
                )
                exit_code = 2

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
            for camera in cameras:
                try:
                    video = camera.probe()
                except RuntimeError as exc:
                    camera_errors[camera.camera_id] = str(exc)
                    error(str(exc))
                    if startup_error is None:
                        startup_error = exc
                    exit_code = 2
                    continue
                try:
                    info = recorder.write_camera_timestamps(
                        camera.camera_id,
                        video.timestamps_ns,
                        monotonic_ns=video.timestamps_monotonic_ns,
                    )
                except RuntimeError as exc:
                    camera_errors[camera.camera_id] = str(exc)
                    error(str(exc))
                    if startup_error is None:
                        startup_error = exc
                    exit_code = 2
                    continue
                log(
                    f"[INFO] PXCap {camera.camera_id} 视频帧：{info['frames']}，"
                    f"实际 {video.actual_fps:.1f} fps"
                )
                camera_videos[camera.camera_id] = (video, info)

            cap_window = recorder.cap_window_unix_ns()
            if cap_window is not None:
                cap_first, cap_last = cap_window
                for camera_id, entry in camera_videos.items():
                    video, info = entry
                    first = info["first_timestamp_unix_ns"]
                    if first is None:
                        continue
                    gap_s = (first - cap_first) / 1e9
                    if abs(gap_s) > SESSION_START_TOLERANCE_S:
                        warn(
                            f"{camera_id} 首帧与手套首帧相差 {gap_s:+.1f} 秒，"
                            "两者可能不在同一段录制里（仅提示，文件里不做对齐）"
                        )
                # Quest 只剩 SDK 时间戳，域未知，无法与手套的单调钟比较，
                # 因此这里不做覆盖检查；两路是否同场只能靠行数是否都非 0 粗判。
                # --no-quest 时 quest 组本来就是 0 行，别报一条假警告。
                if not args.no_quest and recorder.quest_rows == 0:
                    warn("本次没有采到任何 Quest 样本（quest 组为 0 行）")

            verify = recorder.verify_rows()
            if verify["row_mismatch"]:
                error(
                    "HDF5 行数自检失败："
                    + ", ".join(
                        f"{item['dataset']}={item['rows']}"
                        f"(应为 {item['expected']})"
                        for item in verify["row_mismatch"]
                    )
                )
                if startup_error is None:
                    startup_error = RuntimeError("HDF5 行数自检失败")
                exit_code = 2

            actual_hand_rate = recorder.capture_rate_hz
            camera_metadata: dict[str, Any] = {}
            camera_by_id = {item.camera_id: item for item in cameras}
            for camera_id in camera_ids:
                entry: dict[str, Any] = {
                    "device": camera_devices.get(slot_by_camera[camera_id]),
                    "backend": "linux",
                    "file": f"cameras/{camera_id}.mkv",
                    "requested_width": args.camera_width,
                    "requested_height": args.camera_height,
                    "requested_fps": args.camera_fps,
                    "codec": args.video_codec,
                    "error": camera_errors.get(camera_id),
                }
                video_entry = camera_videos.get(camera_id)
                if isinstance(video_entry, tuple):
                    video, info = video_entry
                    entry.update(
                        {
                            "frames": info["frames"],
                            "width": video.width,
                            "height": video.height,
                            "avg_fps": video.avg_fps,
                            "actual_fps": video.actual_fps,
                            "duration_s": video.duration_s,
                            "first_timestamp_unix_ns": (
                                info["first_timestamp_unix_ns"]
                            ),
                            "last_timestamp_unix_ns": (
                                info["last_timestamp_unix_ns"]
                            ),
                            "monotonic_available": info.get(
                                "monotonic_available", False
                            ),
                            "first_timestamp_monotonic_ns": info.get(
                                "first_timestamp_monotonic_ns"
                            ),
                            "last_timestamp_monotonic_ns": info.get(
                                "last_timestamp_monotonic_ns"
                            ),
                        }
                    )
                camera_metadata[camera_id] = entry

            metadata = {
                "hand": {
                    "port": resolved_hand_port,
                    "side": args.cap_side,
                    "pose_side_requested": pose_side_requested,
                    "target_rate_hz": args.hand_rate,
                    "actual_rate_hz": actual_hand_rate,
                    "sdk_version": hand_collector.sdk_version,
                    "controller_version": hand_collector.controller_version,
                    "sensor_versions": hand_collector.sensor_versions,
                    "sn": hand_collector.sn,
                    "fault": hand_collector.fault,
                    "fault_error": hand_collector.fault_error,
                    "info_errors": hand_collector.info_errors,
                    "snapshot_errors": hand_collector.snapshot_errors,
                    "queue_overflows": hand_collector.queue_overflows,
                    "callback_calls": hand_collector.callback_count,
                    "callback_mean_ms": hand_collector.callback_mean_ms,
                    "callback_max_ms": hand_collector.callback_max_ms,
                    "distribution_included": not args.omit_distribution,
                },
                "quest": {
                    "enabled": not args.no_quest,
                    "timestamp_source": "sdk_provided",
                    "timestamp_fields": [
                        "sdk_timestamp_ns",
                        "body_timestamp_ns",
                        "motion_timestamp_ns",
                        "body_joints_timestamp_ns",
                    ],
                    "error": (
                        str(poller.failure)
                        if poller is not None and poller.failure is not None
                        else None
                    ),
                    "rows": recorder.quest_rows,
                    "samples_dropped": quest_stream.dropped,
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
                    "required_consecutive_samples": args.quest_ready_samples,
                    "poll_rate_hz": args.quest_rate,
                    "polls": poller.samples if poller is not None else 0,
                    "poller_overruns": (
                        poller.overruns if poller is not None else 0
                    ),
                },
                "cameras": {
                    "enabled": bool(camera_slots),
                    "backend": "linux",
                    "timestamp_clock": "host_unix_ns",
                    "mode": (
                        "none"
                        if not camera_slots
                        else "both"
                        if len(camera_slots) == 2
                        else camera_slots[0]
                    ),
                    "cap_side": args.cap_side,
                    "slots": list(camera_slots),
                    "camera_ids": list(camera_ids),
                    "ready_before_hand_start": (
                        bool(camera_slots) and camera_ready_wait_s is not None
                    ),
                    "ready_wait_seconds": camera_ready_wait_s,
                    "per_camera": camera_metadata,
                },
                "verify": verify,
                "env": environment_info(ffmpeg),
                "error": (
                    str(startup_error) if startup_error is not None else None
                ),
            }
            manifest = recorder.manifest(metadata)
            recorder.write_meta(
                {
                    key: manifest[key]
                    for key in (
                        "format_version",
                        "started_at",
                        "ended_at",
                        "rows",
                        "actual_rate_hz",
                        "duration_s",
                        "alignment",
                        "pose",
                        "hand",
                        "quest",
                        "cameras",
                        "verify",
                        "env",
                    )
                }
            )
            recorder.__exit__(None, None, None)
            if live_view is not None:
                live_view.close()
                if live_view.dropped:
                    log(
                        f"[INFO] 实时视图丢弃 {live_view.dropped} 帧"
                        "（视图跟不上，录制不受影响）"
                    )
            manifest_path = output_dir / "manifest.json"
            with manifest_path.open("x", encoding="utf-8") as file:
                json.dump(
                    manifest,
                    file,
                    ensure_ascii=False,
                    indent=2,
                    allow_nan=False,
                )
                file.write("\n")

            if hand_stopped:
                call_with_timeout(
                    "断开 PXCap",
                    hand_collector.disconnect,
                    2.0,
                )
            else:
                warn("PXCap 停止超时，跳过断开调用以避免再次阻塞")
            log(
                f"[INFO] 采集结束：手套 {recorder.cap_rows} 帧，"
                f"Quest {recorder.quest_rows} 样本"
            )
            for camera_id in camera_ids:
                video_entry = camera_videos.get(camera_id)
                if isinstance(video_entry, tuple):
                    video, _ = video_entry
                    log(f"[INFO] {camera_id}：{video.frames} 帧")
            for camera in cameras:
                if camera.preview_fps > 0:
                    average = (
                        camera.preview_bytes / camera.preview_frames / 1024.0
                        if camera.preview_frames
                        else 0.0
                    )
                    log(
                        f"[INFO] {camera.camera_id} 实时预览："
                        f"{camera.preview_frames} 帧，平均 {average:.1f} KB/帧"
                    )
            log(f"[INFO] manifest：{manifest_path}")
        elif hand_collector is not None:
            if hand_stopped:
                call_with_timeout(
                    "断开 PXCap",
                    hand_collector.disconnect,
                    2.0,
                )

        if not recorder_entered and (startup_error is not None or exit_code != 0):
            failure_dir = output_dir
            if output_dir_preexisting:
                stamp = session_started_at.strftime("%Y%m%d_%H%M%S")
                base = output_dir.with_name(
                    f"{output_dir.name}_startup_failure_{stamp}"
                )
                failure_dir = base
                suffix = 2
                while failure_dir.exists():
                    failure_dir = base.with_name(f"{base.name}_{suffix}")
                    suffix += 1
            failure_path = write_startup_failure(
                failure_dir,
                startup_error,
                started_at=session_started_at,
                cap_side=args.cap_side,
                pose_source=args.pose_source,
                pose_side=args.pose_side,
                pose_side_requested=pose_side_requested,
                hand_port=resolved_hand_port,
                camera_devices=camera_devices,
                cameras=cameras,
                ffmpeg=ffmpeg,
            )
            log(f"[INFO] 启动失败诊断已保留：{failure_path}")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
