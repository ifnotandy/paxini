"""Timestamped ffmpeg capture for the synchronized recorder.

Two backends share one class:

- ``linux`` — ffmpeg reads ``/dev/videoN`` through V4L2 and stamps each frame
  with the host wall clock (``-use_wallclock_as_timestamps 1``).  The pts in the
  container *is* Unix epoch nanoseconds.
- ``wsl`` — the camera is attached to Windows, and WSL runs ``ffmpeg.exe``
  against DirectShow.  This exists because usbipd loses isochronous packets: the
  same camera delivers 30 fps natively on Windows but ~5 fps after being
  forwarded into WSL.  DirectShow rejects ``-use_wallclock_as_timestamps``, so
  pts restarts at 0 and the absolute epoch has to be recovered separately — see
  ``FfmpegCamera.epoch_unix_ns``.

OpenCV ``read()`` only reports when the call returned; the frame may have waited
in the driver queue for up to one frame period.  Both backends therefore keep
ffmpeg's own container timestamps rather than re-stamping after the fact.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

# Any sane wall-clock stamp is far above this; a pts here means the capture
# fell back to a relative time base and must not be treated as epoch time.
WALLCLOCK_MIN_NS = 1_500_000_000_000
VIDEO_CODECS = ("copy", "h264", "hevc", "mjpeg")
CAMERA_BACKENDS = ("linux", "wsl")
# ``auto`` is resolved once at startup by ``resolve_backend``; nothing downstream
# ever stores it, because the manifest has to record which path actually ran.
CAMERA_BACKEND_CHOICES = ("auto",) + CAMERA_BACKENDS
PROGRESS_POLL_S = 0.01
STOP_TIMEOUT_S = 4.0
# How long to watch the output file before declaring that the first frame never
# landed.  There is deliberately no "assume 1.55 s" fallback any more: a guessed
# epoch writes plausible-looking but wrong absolute times, and wrong data is
# worse than a missing camera.
EPOCH_WATCH_TIMEOUT_S = 20.0

# Live preview: a second ffmpeg output writes small JPEGs to stdout.  Kept at a
# few fps and a few hundred px on purpose — this rides along with the recording
# and must not compete for encoder time (the mkv output is the copy encoder, so
# the cost here is the scaler plus mjpeg).
PREVIEW_FPS = 5.0
PREVIEW_WIDTH = 360
PREVIEW_QUALITY = 7
PREVIEW_MAX_BUFFER = 4 * 1024 * 1024

# ffmpeg runs in its own session so only this program's SIGINT reaches it. On a
# forced exit that session would be orphaned, so keep handles here and ask them
# to finalise before the process disappears.
RUNNING_PROCESSES: list[subprocess.Popen[bytes]] = []


def interrupt_running_cameras() -> None:
    """Best-effort shutdown for cameras that are still recording.

    Linux ffmpeg understands SIGINT; ``ffmpeg.exe`` does not, so the WSL
    backend is asked to finalise through its stdin instead.
    """
    for process in list(RUNNING_PROCESSES):
        if process.poll() is None:
            try:
                if process.stdin is not None:
                    process.stdin.write(b"q")
                    process.stdin.flush()
                else:
                    process.send_signal(signal.SIGINT)
            except (ProcessLookupError, OSError, ValueError):
                pass
        if process in RUNNING_PROCESSES:
            RUNNING_PROCESSES.remove(process)


def to_windows_path(path: Path | str) -> str:
    """Translate ``/mnt/c/dir/file`` into ``C:\\dir\\file``."""
    text = str(path)
    match = re.match(r"^/mnt/([a-zA-Z])/(.*)$", text)
    if match:
        return f"{match.group(1).upper()}:\\" + match.group(2).replace("/", "\\")
    return text


def find_windows_ffmpeg(argument: str | None = None) -> str:
    """Locate ``ffmpeg.exe``. Explicit path beats $FFMPEG_WIN beats sdk/."""
    candidates: list[Path] = []
    if argument:
        candidates.append(Path(argument).expanduser())
    from_env = os.environ.get("FFMPEG_WIN")
    if from_env:
        candidates.append(Path(from_env).expanduser())
    module_dir = Path(__file__).resolve().parent
    # capture/ 的上一层是 Cap_Quest，再上一层才是 Paxini-Cap（sdk 在那里）
    candidates.append(module_dir.parent.parent / "sdk" / "ffmpeg-win" / "ffmpeg.exe")

    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise RuntimeError(
        "找不到 ffmpeg.exe。请把 Windows 静态版放到 "
        "Paxini-Cap/sdk/ffmpeg-win/ffmpeg.exe，或用 --ffmpeg-win 指定路径。"
    )


def discover_windows_cameras(ffmpeg_exe: str) -> list[dict[str, str]]:
    """List DirectShow video inputs as ``{name, input}``.

    ``input`` is the exact ``-i`` argument, using the PnP alternative name when
    available: several cameras in this rig share the friendly name "MASTER
    Camera", and the plain name would always open the first one.
    """
    try:
        result = subprocess.run(
            [ffmpeg_exe, "-hide_banner", "-f", "dshow",
             "-list_devices", "true", "-i", "dummy"],
            check=False, capture_output=True, text=True, timeout=20.0,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"枚举 DirectShow 设备失败：{exc}") from exc

    devices: list[dict[str, str]] = []
    pending: str | None = None
    for line in (result.stderr + result.stdout).splitlines():
        name_match = re.search(r'"([^"]+)"\s*\(video\)', line)
        if name_match:
            pending = name_match.group(1)
            continue
        alt_match = re.search(r'Alternative name "([^"]+)"', line)
        if alt_match and pending is not None:
            devices.append({"name": pending, "input": f"video={alt_match.group(1)}"})
            pending = None
            continue
        if pending is not None and "Alternative name" not in line and line.strip():
            devices.append({"name": pending, "input": f"video={pending}"})
            pending = None
    if pending is not None:
        devices.append({"name": pending, "input": f"video={pending}"})
    return devices



def log(message: str) -> None:
    print(message, flush=True)


def warn(message: str) -> None:
    print(f"[警告] {message}", file=sys.stderr, flush=True)


def running_in_wsl() -> bool:
    """True when this Linux is a WSL distro rather than a native install.

    Three independent signals, because none of them is set in every launch
    path: ``WSL_DISTRO_NAME`` comes from ``wsl.exe``, ``WSL_INTEROP`` from the
    interop server, and the kernel release string carries ``microsoft`` on both
    WSL1 and WSL2 even when the distro was started by systemd.
    """
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        release = Path("/proc/sys/kernel/osrelease").read_text(encoding="utf-8")
    except OSError:
        return False
    return "microsoft" in release.lower()


def resolve_backend(requested: str | None = None) -> str:
    """Turn ``auto`` into a concrete backend; leave explicit choices alone.

    WSL keeps the camera on the Windows side: usbipd forwards UVC over USB/IP,
    which drops isochronous packets, and a camera that does 30 fps natively
    collapses to ~5 fps after the detour.  Native Linux has no such hop, so V4L2
    is both simpler and the only path whose pts is already epoch time.

    An explicit ``linux`` in WSL is still honoured — someone may have a working
    passthrough and prefer it — but it is never chosen automatically.
    """
    value = (requested or "auto").strip().lower()
    if value == "auto":
        return "wsl" if running_in_wsl() else "linux"
    if value not in CAMERA_BACKENDS:
        raise RuntimeError(
            f"未知相机后端：{requested!r}"
            f"（可选 {' / '.join(CAMERA_BACKEND_CHOICES)}）"
        )
    return value


def discover_video_nodes(
    sysfs_root: Path = Path("/sys/class/video4linux"),
    dev_root: Path = Path("/dev"),
) -> list[str]:
    """Find one capture node per UVC device.

    A UVC camera exposes a metadata node next to its capture node; only
    ``index == 0`` is a frame source. Nodes are de-duplicated by the real path of
    their parent device so a device that exposes several index-0 nodes still
    yields a single entry.
    """
    if not sysfs_root.is_dir():
        return []

    devices: dict[str, str] = {}
    for entry in sorted(sysfs_root.glob("video*")):
        try:
            index = int((entry / "index").read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue
        if index != 0:
            continue
        try:
            device_key = os.path.realpath(entry / "device")
        except OSError:
            device_key = str(entry / "device")
        devices[device_key] = str(dev_root / entry.name)
    return [devices[key] for key in sorted(devices)]


def find_ffmpeg(argument: str | None = None) -> str:
    """Resolve the ffmpeg binary; ``--ffmpeg`` beats ``$FFMPEG`` beats ``PATH``."""
    requested = argument or os.environ.get("FFMPEG") or "ffmpeg"
    path = Path(requested).expanduser()
    if path.is_absolute() or path.parent != Path("."):
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        raise RuntimeError(f"ffmpeg 不存在或不可执行：{path}")
    found = shutil.which(requested)
    if not found:
        raise RuntimeError(
            "找不到 ffmpeg（需要 5.0+，带 ffprobe，且支持 v4l2 与 -fps_mode）。"
            "装到 PATH 里即可，或用 --ffmpeg 指定路径、FFMPEG 环境变量。"
        )
    return found


def find_ffprobe(ffmpeg: str) -> str:
    sibling = Path(ffmpeg).with_name("ffprobe")
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return str(sibling)
    found = shutil.which("ffprobe")
    if not found:
        raise RuntimeError(
            "找不到 ffprobe。请把它和 ffmpeg 放在同一目录，或加入 PATH。"
        )
    return found


def ffmpeg_version(ffmpeg: str) -> str:
    """Return the first line of ``ffmpeg -version`` or a readable fallback."""
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "未知"
    lines = result.stdout.splitlines()
    return lines[0].strip() if lines else "未知"


@dataclass(slots=True)
class CameraVideo:
    """Result of probing one recorded file."""

    camera_id: str
    path: Path
    frames: int
    width: int
    height: int
    avg_fps: float
    timestamps_ns: list[int]
    # WSL backend only: same frames on the monotonic clock, anchored to
    # ``epoch_monotonic_ns``. Linux backend leaves this None.
    timestamps_monotonic_ns: list[int] | None = None

    @property
    def duration_s(self) -> float:
        if len(self.timestamps_ns) < 2:
            return 0.0
        return (self.timestamps_ns[-1] - self.timestamps_ns[0]) / 1e9

    @property
    def actual_fps(self) -> float:
        span = self.duration_s
        return (self.frames - 1) / span if span > 0.0 else 0.0


def probe_frames(ffprobe: str, path: Path) -> list[int]:
    """Read every decoded frame pts and convert it to absolute Unix nanoseconds."""
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "frame=pts_time",
                "-of",
                "csv=p=0",
                str(path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=300.0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"ffprobe 读取 {path.name} 时间戳失败：{exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or "无输出"
        raise RuntimeError(f"ffprobe 读取 {path.name} 失败：{detail}")

    timestamps: list[int] = []
    for line in result.stdout.splitlines():
        text = line.strip().split(",")[0].strip()
        if not text or text.upper() == "N/A":
            continue
        try:
            seconds = float(text)
        except ValueError:
            continue
        timestamps.append(int(round(seconds * 1e9)))
    return timestamps


def probe_stream(ffprobe: str, path: Path) -> dict[str, object]:
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height,avg_frame_rate,r_frame_rate",
                "-of",
                "json",
                str(path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30.0,
        )
        payload = json.loads(result.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return {}
    streams = payload.get("streams") or []
    return streams[0] if streams else {}


def parse_rate(text: object) -> float:
    """Parse ffprobe's ``num/den`` frame-rate string."""
    if not isinstance(text, str) or "/" not in text:
        return 0.0
    numerator, _, denominator = text.partition("/")
    try:
        top = float(numerator)
        bottom = float(denominator)
    except ValueError:
        return 0.0
    return top / bottom if bottom else 0.0


def extract_jpeg_frames(buffer: bytearray) -> list[bytes]:
    """Pull complete JPEGs out of ``buffer``, consuming them in place.

    ``image2pipe`` is a bare concatenation with no framing of its own, so the
    only reliable delimiters are the SOI/EOI markers.  A partial frame at the
    tail is left in the buffer for the next read; anything before the first SOI
    (a stray marker from a torn write) is dropped.
    """
    frames: list[bytes] = []
    while True:
        start = buffer.find(b"\xff\xd8")
        if start < 0:
            buffer.clear()
            break
        end = buffer.find(b"\xff\xd9", start + 2)
        if end < 0:
            if start:
                del buffer[:start]
            break
        frames.append(bytes(buffer[start:end + 2]))
        del buffer[:end + 2]
    return frames


class FfmpegCamera:
    """Record one camera through ffmpeg, on either backend."""

    def __init__(
        self,
        camera_id: str,
        device: str,
        output_dir: Path,
        width: int,
        height: int,
        fps: float,
        codec: str,
        ffmpeg: str,
        backend: str = "linux",
        windows_ffmpeg: str | None = None,
        preview_fps: float = 0.0,
        preview_width: int = PREVIEW_WIDTH,
        preview_quality: int = PREVIEW_QUALITY,
    ) -> None:
        if backend not in CAMERA_BACKENDS:
            raise RuntimeError(f"未知相机后端：{backend}")
        self.camera_id = camera_id
        self.device = device
        self.output_dir = output_dir
        self.width = width
        self.height = height
        self.fps = fps
        self.codec = codec
        self.backend = backend
        # ``ffmpeg`` always points at the Linux build: ffprobe and the version
        # banner come from it.  On the WSL backend the capture itself runs the
        # Windows binary, kept separately.
        self.ffmpeg = ffmpeg
        self.capture_ffmpeg = (
            find_windows_ffmpeg(windows_ffmpeg) if backend == "wsl" else ffmpeg
        )
        self.ffprobe = find_ffprobe(ffmpeg)
        self.video_path = output_dir / f"{camera_id}.mkv"
        self.progress_path = output_dir / f"{camera_id}.progress"
        self.log_path = output_dir / f"{camera_id}.log"
        self.process: subprocess.Popen[bytes] | None = None
        self.failure: Exception | None = None
        # Absolute Unix ns that pts=0 corresponds to. Only the WSL backend
        # needs it; on Linux the pts already is epoch time.
        self.epoch_unix_ns: int | None = None
        self.start_unix_ns: int | None = None
        # Same idea on the monotonic clock. Lets the camera time axis line up
        # with cap/timestamp_monotonic_ns without going through the wall clock
        # (which NTP can step mid-recording).
        self.epoch_monotonic_ns: int | None = None
        self.start_monotonic_ns: int | None = None
        self._log_handle = None
        self._epoch_stop = threading.Event()
        self._epoch_thread: threading.Thread | None = None
        # Live preview (display only; nothing here reaches the HDF5).
        self.preview_fps = max(0.0, float(preview_fps))
        self.preview_width = int(preview_width)
        self.preview_quality = int(preview_quality)
        self.preview_frames = 0
        self.preview_bytes = 0
        self._preview_stop = threading.Event()
        self._preview_thread: threading.Thread | None = None
        self._preview_lock = threading.Lock()
        self._preview_latest: tuple[int, bytes] | None = None

    def _codec_args(self) -> list[str]:
        if self.codec == "copy":
            return ["-c:v", "copy"]
        if self.codec == "mjpeg":
            return ["-c:v", "mjpeg", "-q:v", "3"]
        if self.codec == "h264":
            return [
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-pix_fmt", "yuv420p",
            ]
        if self.codec == "hevc":
            return [
                "-c:v", "libx265", "-preset", "veryfast", "-crf", "24",
                "-pix_fmt", "yuv420p",
            ]
        raise RuntimeError(f"未知视频编码：{self.codec}")

    def command(self) -> list[str]:
        command = [self.capture_ffmpeg, "-hide_banner"]
        if self.backend == "linux":
            command += [
                "-nostdin",
                "-loglevel",
                "warning",
                "-nostats",
                "-y",
                "-f",
                "v4l2",
                "-use_wallclock_as_timestamps",
                "1",
                "-thread_queue_size",
                "1024",
                "-input_format",
                "mjpeg",
                "-video_size",
                f"{self.width}x{self.height}",
                "-framerate",
                f"{self.fps:g}",
                "-i",
                self.device,
                "-map",
                "0:v:0",
                "-fps_mode",
                "passthrough",
                "-copyts",
                "-muxdelay",
                "0",
                "-muxpreload",
                "0",
            ]
            progress = str(self.progress_path)
            output = str(self.video_path)
        else:
            # DirectShow rejects -use_wallclock_as_timestamps ("Could not
            # RenderStream"), so pts restarts at 0 and epoch_unix_ns supplies
            # the missing origin. -nostdin is left out on purpose: sending "q"
            # on stdin is the only graceful stop ffmpeg.exe understands.
            command += [
                "-loglevel",
                "warning",
                "-nostats",
                "-y",
                "-f",
                "dshow",
                "-video_size",
                f"{self.width}x{self.height}",
                "-framerate",
                f"{self.fps:g}",
                "-i",
                self.device,
                "-map",
                "0:v:0",
                "-fps_mode",
                "passthrough",
            ]
            progress = to_windows_path(self.progress_path)
            output = to_windows_path(self.video_path)
        command += self._codec_args()
        command += ["-progress", progress, output]
        if self.preview_fps > 0:
            # Second output on the same input: a decimated MJPEG stream on
            # stdout.  ``-an`` keeps audio away, and ``-f image2pipe`` is what
            # makes ffmpeg concatenate JPEGs instead of writing one file.
            command += [
                "-map", "0:v:0",
                "-an",
                "-vf", f"fps={self.preview_fps:g},scale={self.preview_width}:-2",
                "-c:v", "mjpeg",
                "-q:v", str(self.preview_quality),
                "-f", "image2pipe",
                "pipe:1",
            ]
        return command

    def open(self) -> None:
        """Validate inputs and clear stale artifacts before start()."""
        if self.codec not in VIDEO_CODECS:
            raise RuntimeError(f"未知视频编码：{self.codec}")
        if self.backend == "linux" and not Path(self.device).exists():
            raise RuntimeError(
                f"PXCap {self.camera_id} 摄像头节点不存在：{self.device}"
            )
        self.output_dir.mkdir(parents=True, exist_ok=True)
        for path in (self.progress_path, self.log_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def _watch_first_frame(self) -> None:
        """Stamp the moment the container starts growing.

        DirectShow gives no absolute time, so pts=0 has to be anchored to the
        host clock.  ffmpeg.exe takes ~1.55 s from launch to the first byte on
        disk; watching the file catches that per run instead of trusting a
        constant.
        """
        deadline = time.monotonic() + EPOCH_WATCH_TIMEOUT_S
        while not self._epoch_stop.is_set() and time.monotonic() < deadline:
            try:
                if self.video_path.is_file() and self.video_path.stat().st_size > 0:
                    # Stamp both clocks at the same instant: downstream can
                    # pick whichever aligns with its source.
                    self.epoch_unix_ns = time.time_ns()
                    self.epoch_monotonic_ns = time.monotonic_ns()
                    return
            except OSError:
                pass
            time.sleep(0.002)

    def start(self) -> None:
        if self.process is not None:
            raise RuntimeError(f"PXCap {self.camera_id} 摄像头已启动")
        self._log_handle = self.log_path.open("wb")
        self.epoch_unix_ns = None
        self.epoch_monotonic_ns = None
        self.start_unix_ns = time.time_ns()
        self.start_monotonic_ns = time.monotonic_ns()
        self._epoch_stop.clear()
        try:
            self.process = subprocess.Popen(
                self.command(),
                stdin=(
                    subprocess.PIPE
                    if self.backend == "wsl"
                    else subprocess.DEVNULL
                ),
                stdout=(
                    subprocess.PIPE
                    if self.preview_fps > 0
                    else subprocess.DEVNULL
                ),
                stderr=self._log_handle,
                start_new_session=True,
            )
        except OSError as exc:
            self._close_log()
            raise RuntimeError(
                f"PXCap {self.camera_id} 摄像头启动失败：{exc}"
            ) from exc
        RUNNING_PROCESSES.append(self.process)
        if self.preview_fps > 0:
            self._preview_stop.clear()
            self._preview_thread = threading.Thread(
                target=self._preview_reader,
                name=f"preview-{self.camera_id}",
                daemon=True,
            )
            self._preview_thread.start()
        if self.backend == "wsl":
            self._epoch_thread = threading.Thread(
                target=self._watch_first_frame,
                name=f"epoch-{self.camera_id}",
                daemon=True,
            )
            self._epoch_thread.start()
        log(
            f"[INFO] PXCap {self.camera_id} 摄像头已启动："
            f"{self.width}x{self.height}@{self.fps:g} "
            f"codec={self.codec} backend={self.backend} device={self.device}"
        )

    def _preview_reader(self) -> None:
        """Split the stdout MJPEG stream into frames and keep only the newest.

        The pipe has to be drained continuously or ffmpeg blocks on write and
        the *recording* stalls, so this thread reads even when nobody is
        watching the preview; the last frame simply gets overwritten.
        """
        process = self.process
        if process is None or process.stdout is None:
            return
        stream = process.stdout
        buffer = bytearray()
        while not self._preview_stop.is_set():
            try:
                chunk = stream.read(65536)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            buffer += chunk
            for frame in extract_jpeg_frames(buffer):
                stamp = time.time_ns()
                with self._preview_lock:
                    self._preview_latest = (stamp, frame)
                self.preview_frames += 1
                self.preview_bytes += len(frame)
            if len(buffer) > PREVIEW_MAX_BUFFER:
                buffer.clear()
        try:
            stream.close()
        except (OSError, ValueError):
            pass

    def latest_preview(self) -> tuple[int, bytes] | None:
        """Newest ``(unix_ns, jpeg_bytes)``, or None before the first frame."""
        with self._preview_lock:
            return self._preview_latest

    def progress_signal(self) -> int:
        """Return a monotonically growing frame-count estimate.

        ffmpeg omits ``frame=`` for some stream-copy paths, even though it
        still reports ``out_time_us`` and ``total_size``. Readiness checks need
        evidence that new frames landed, not a specific progress key.
        """
        try:
            text = self.progress_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return 0
        frames = 0
        out_time_us = 0
        total_size = 0
        for line in text.splitlines():
            key, _, value = line.partition("=")
            key = key.strip()
            if key not in ("frame", "out_time_us", "total_size"):
                continue
            try:
                number = int(value.strip())
            except ValueError:
                continue
            if key == "frame":
                frames = number
            elif key == "out_time_us":
                out_time_us = number
            else:
                total_size = number
        if frames > 0:
            return frames
        if out_time_us > 0:
            return max(1, int(round(out_time_us * self.fps / 1e6)))
        # A growing container is enough to prove the input produced a frame.
        return max(0, total_size // 65536)

    def poll_health(self) -> None:
        """Raise if ffmpeg exited while the recording is running."""
        if self.failure is not None:
            raise RuntimeError(
                f"PXCap {self.camera_id} 摄像头失败：{self.failure}"
            )
        if self.process is None:
            return
        code = self.process.poll()
        if code is None:
            return
        detail = self.log_tail()
        self.failure = RuntimeError(
            f"ffmpeg 退出码 {code}" + (f"，日志：{detail}" if detail else "")
        )
        raise RuntimeError(
            f"PXCap {self.camera_id} 摄像头失败：{self.failure}"
        )

    def log_tail(self, limit: int = 3) -> str:
        try:
            text = self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return " | ".join(lines[-limit:])

    def stop(self) -> None:
        """Finalise the container: SIGINT on Linux, ``q`` on stdin on Windows."""
        process = self.process
        if process is None:
            return
        self._epoch_stop.set()
        if self._epoch_thread is not None:
            self._epoch_thread.join(timeout=1.0)
            self._epoch_thread = None
        if process.poll() is None:
            try:
                if self.backend == "wsl" and process.stdin is not None:
                    process.stdin.write(b"q")
                    process.stdin.flush()
                else:
                    process.send_signal(signal.SIGINT)
            except (ProcessLookupError, OSError, ValueError):
                pass
            try:
                process.wait(timeout=STOP_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                warn(
                    f"PXCap {self.camera_id} 摄像头未在 "
                    f"{STOP_TIMEOUT_S:g} 秒内退出，强制结束"
                )
                process.kill()
                try:
                    process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    warn(f"PXCap {self.camera_id} 摄像头进程无法结束")
        if process.returncode not in (0, -signal.SIGINT, 130):
            detail = self.log_tail()
            message = f"ffmpeg 退出码 {process.returncode}" + (
                f"，日志：{detail}" if detail else ""
            )
            self.failure = self.failure or RuntimeError(message)
            warn(f"PXCap {self.camera_id} 摄像头异常结束：{message}")
        # Only now is it safe to stop the reader: ffmpeg must be allowed to
        # finish writing to stdout, otherwise it blocks on a full pipe and never
        # gets to finalise the mkv.
        self._preview_stop.set()
        if self._preview_thread is not None:
            self._preview_thread.join(timeout=2.0)
            self._preview_thread = None
        if process in RUNNING_PROCESSES:
            RUNNING_PROCESSES.remove(process)
        self.process = None
        self._close_log()

    def close(self) -> None:
        self.stop()

    def _close_log(self) -> None:
        if self._log_handle is not None:
            try:
                self._log_handle.close()
            except OSError:
                pass
            self._log_handle = None

    def probe(self) -> CameraVideo:
        """Recover per-frame absolute timestamps and stream metadata."""
        if not self.video_path.is_file():
            raise RuntimeError(
                f"PXCap {self.camera_id} 摄像头未生成视频文件：{self.video_path}"
            )
        raw_pts = probe_frames(self.ffprobe, self.video_path)
        if not raw_pts:
            detail = self.log_tail()
            raise RuntimeError(
                f"PXCap {self.camera_id} 摄像头视频没有可读帧"
                + (f"（日志：{detail}）" if detail else "")
            )
        timestamps_monotonic: list[int] | None = None
        if self.backend == "linux":
            if raw_pts[0] <= WALLCLOCK_MIN_NS:
                raise RuntimeError(
                    f"PXCap {self.camera_id} 摄像头时间戳不是墙钟时间"
                    f"（首帧 pts_time={raw_pts[0] / 1e9:.6f} 秒）。"
                    "-use_wallclock_as_timestamps 未生效，请检查 ffmpeg 版本与命令"
                )
            timestamps = raw_pts
        else:
            # pts restarts at 0 on DirectShow; shift it onto both host clocks.
            # If the first byte was never observed there is nothing honest to
            # shift by: guessing a start delay would silently write wrong
            # absolute times, so fail this camera instead.
            if self.epoch_unix_ns is None or self.epoch_monotonic_ns is None:
                raise RuntimeError(
                    f"PXCap {self.camera_id} 没有捕捉到首帧墙钟时刻"
                    f"（开始录制后 {EPOCH_WATCH_TIMEOUT_S:g} 秒内视频文件未增长），"
                    "无法把 pts 锚到主机时钟。本路不写时间戳，也不参与对齐；"
                    "mkv 文件本身仍然可用，重采一次即可"
                )
            epoch_unix = self.epoch_unix_ns
            epoch_mono = self.epoch_monotonic_ns
            timestamps = [epoch_unix + value for value in raw_pts]
            timestamps_monotonic = [epoch_mono + value for value in raw_pts]
        stream = probe_stream(self.ffprobe, self.video_path)
        return CameraVideo(
            camera_id=self.camera_id,
            path=self.video_path,
            frames=len(timestamps),
            width=int(stream.get("width") or 0),
            height=int(stream.get("height") or 0),
            avg_fps=parse_rate(stream.get("avg_frame_rate"))
            or parse_rate(stream.get("r_frame_rate")),
            timestamps_ns=timestamps,
            timestamps_monotonic_ns=timestamps_monotonic,
        )


def wait_for_cameras_ready(
    cameras: Sequence[FfmpegCamera],
    timeout_s: float,
    required_samples: int,
    stop_event=None,
) -> float:
    """Wait until every camera keeps reporting new frames in its progress file."""
    started = time.monotonic()
    deadline = started + timeout_s
    counts = {camera.camera_id: 0 for camera in cameras}
    last_progress = {camera.camera_id: 0 for camera in cameras}
    camera_text = "/".join(camera.camera_id for camera in cameras)
    log(
        f"[INFO] 等待 PXCap {camera_text} 摄像头画面；"
        f"每路需要连续 {required_samples} 帧，超时 {timeout_s:g} 秒"
    )

    while time.monotonic() < deadline:
        if stop_event is not None and stop_event.is_set():
            raise RuntimeError("等待 PXCap 摄像头时收到停止信号")
        for camera in cameras:
            camera.poll_health()
            progress = camera.progress_signal()
            if progress > last_progress[camera.camera_id]:
                last_progress[camera.camera_id] = progress
                counts[camera.camera_id] += 1
        if all(count >= required_samples for count in counts.values()):
            elapsed = time.monotonic() - started
            log(
                f"[INFO] PXCap {camera_text} 摄像头已就绪，"
                f"等待 {elapsed:.1f} 秒"
            )
            return elapsed
        time.sleep(PROGRESS_POLL_S)

    details = "，".join(
        f"{camera_id}={progress}"
        for camera_id, progress in last_progress.items()
    )
    raise RuntimeError(
        f"等待 PXCap {camera_text} 摄像头画面超时（已出帧：{details}）。"
        "请检查 --list-devices 的节点、--camera-width/--camera-height 是否被"
        "设备支持，以及两路 UVC 同时走 usbipd 时的带宽"
    )
