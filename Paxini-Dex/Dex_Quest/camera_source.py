"""Timestamped PXDex camera capture for the synchronized recorder."""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import Any


def discover_pxdex_cameras(
    sysfs_root: Path = Path("/sys/class/video4linux"),
    dev_root: Path = Path("/dev"),
) -> list[str]:
    """Find one capture node for each PXDex MASTER Camera."""
    cameras: dict[str, str] = {}
    if not sysfs_root.is_dir():
        return []

    for entry in sorted(sysfs_root.glob("video*")):
        try:
            name = (entry / "name").read_text(
                encoding="utf-8",
                errors="replace",
            ).strip()
            index_text = (entry / "index").read_text(
                encoding="utf-8"
            ).strip()
            index = int(index_text)
        except (OSError, ValueError):
            continue
        lowered = name.lower()
        if "master camera" not in lowered and "pxdex" not in lowered:
            continue
        if index != 0:
            continue

        device_path = entry / "device"
        try:
            device_key = os.path.realpath(device_path)
        except OSError:
            device_key = str(device_path)
        cameras[device_key] = str(dev_root / entry.name)

    return [cameras[key] for key in sorted(cameras)]


def v4l2_device_index(device: str) -> int | None:
    """Return the integer index required by OpenCV's V4L2 backend."""
    name = Path(device).resolve(strict=False).name
    match = re.fullmatch(r"video(\d+)", name)
    return int(match.group(1)) if match else None


@dataclass(slots=True)
class CameraFrame:
    timestamp_ns: int
    frame_index: int
    image: Any
    read_start_ns: int
    read_end_ns: int


class CameraStream:
    """Bounded newest-frame buffer for one camera."""

    def __init__(self, max_frames: int = 10) -> None:
        self._frames: deque[CameraFrame] = deque(maxlen=max_frames)
        self._condition = threading.Condition()

    def push(self, frame: CameraFrame) -> bool:
        """Append a frame and report whether the oldest frame was dropped."""
        with self._condition:
            dropped = len(self._frames) == self._frames.maxlen
            self._frames.append(frame)
            self._condition.notify_all()
            return dropped

    def latest(self) -> CameraFrame | None:
        with self._condition:
            return self._frames[-1] if self._frames else None

    def nearest(
        self,
        target_ns: int,
        max_skew_ns: int,
    ) -> tuple[CameraFrame | None, int | None]:
        with self._condition:
            if not self._frames:
                return None, None
            best = min(
                self._frames,
                key=lambda frame: abs(frame.timestamp_ns - target_ns),
            )
        skew_ns = abs(best.timestamp_ns - target_ns)
        if skew_ns > max_skew_ns:
            return None, skew_ns
        return best, skew_ns


class PxDexCamera(threading.Thread):
    """Continuously read one PXDex RGB camera with host receive timestamps."""

    def __init__(
        self,
        camera_id: str,
        device: str,
        width: int,
        height: int,
        fps: float,
        stop_event: threading.Event,
    ) -> None:
        super().__init__(name=f"pxdex-camera-{camera_id}", daemon=True)
        self.camera_id = camera_id
        self.device = device
        self.width = width
        self.height = height
        self.fps = fps
        self.stop_event = stop_event
        self.stream = CameraStream()
        self.cap: Any = None
        self.failure: Exception | None = None
        self.frames = 0
        self.dropped = 0
        self.actual_width = 0
        self.actual_height = 0
        self.actual_fps = 0.0

    def open(self) -> None:
        try:
            import cv2
        except ImportError as exc:
            raise RuntimeError(
                "缺少 OpenCV，请在共享 .venv 中安装 opencv-python"
            ) from exc

        device_index = v4l2_device_index(self.device)
        source: int | str = (
            device_index if device_index is not None else self.device
        )
        cap = cv2.VideoCapture(source, cv2.CAP_V4L2)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_FPS, self.fps)
        if not cap.isOpened():
            cap.release()
            raise RuntimeError(
                f"无法打开 PXDex {self.camera_id} 摄像头：{self.device}"
            )
        self.cap = cap
        self.actual_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.actual_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.actual_fps = float(cap.get(cv2.CAP_PROP_FPS))
        print(
            f"[INFO] PXDex {self.camera_id} 摄像头已打开："
            f"{self.actual_width}x{self.actual_height}@{self.actual_fps:g} "
            f"device={self.device}",
            flush=True,
        )

    def run(self) -> None:
        period_s = 1.0 / self.fps
        next_deadline = time.monotonic()
        failures = 0
        try:
            while not self.stop_event.is_set():
                if self.cap is None:
                    raise RuntimeError(f"摄像头 {self.camera_id} 尚未打开")
                started_ns = time.monotonic_ns()
                ok, image = self.cap.read()
                ended_ns = time.monotonic_ns()
                if not ok or image is None:
                    failures += 1
                    if failures >= 30:
                        raise RuntimeError(
                            f"PXDex {self.camera_id} 摄像头连续读取失败"
                        )
                    self.stop_event.wait(0.05)
                    continue

                failures = 0
                self.frames += 1
                if self.stream.push(
                    CameraFrame(
                        timestamp_ns=(started_ns + ended_ns) // 2,
                        frame_index=self.frames,
                        image=image,
                        read_start_ns=started_ns,
                        read_end_ns=ended_ns,
                    )
                ):
                    self.dropped += 1

                next_deadline += period_s
                now = time.monotonic()
                if next_deadline < now - period_s:
                    next_deadline = now
                self.stop_event.wait(max(0.0, next_deadline - time.monotonic()))
        except BaseException as exc:
            self.failure = exc
            self.stop_event.set()

    def close(self) -> None:
        if self.cap is not None:
            self.cap.release()
            self.cap = None


def save_jpeg(path: Path, image: Any, quality: int) -> None:
    import cv2

    ok, encoded = cv2.imencode(
        ".jpg",
        image,
        [cv2.IMWRITE_JPEG_QUALITY, quality],
    )
    if not ok:
        raise RuntimeError("OpenCV JPEG 编码失败")
    path.write_bytes(encoded.tobytes())
