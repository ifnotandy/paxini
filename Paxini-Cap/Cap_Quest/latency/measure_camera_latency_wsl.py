#!/usr/bin/env python
"""measure_camera_latency_wsl.py —— 相机延迟测量（WSL 采集路径）

项目有两条采集路径，区别只在相机这一路怎么接：

- **原生 Ubuntu**：ffmpeg 直读 ``/dev/videoN``（V4L2），用
  ``latency/measure_camera_latency.py`` 测
- **WSL**：Python 仍跑在 Linux 里，但相机借 Windows 的 ``ffmpeg.exe`` 走
  DirectShow，就是本脚本

本脚本要在 Windows 侧执行，原因不是「Windows 是另一个版本」，而是 WSL 路径的
时间戳由两段拼成，必须在同一侧才能测：

    timestamp_unix_ns = epoch_unix_ns + pts     # DirectShow 的 pts 从 0 起

epoch 是「mkv 首次变大」时打的墙钟。所以

    Δ = (epoch + pts) − 码内时刻

这个减法里的两个时刻必须同源。本脚本把二维码生成、窗口显示、ffmpeg.exe 调用、
epoch 打点全放在 Windows 侧，都用同一个 ``time.time_ns()``，**跨系统时钟差自动
抵消**——不需要知道 WSL 和 Windows 差多少（本机实测 0.3 ms，历史文档里的 1.61 s
是 NTP 没同步时的旧值）。即使差一分钟，只要在同一侧取，结果一样。

顺带省掉两层开销：
- 显示不经过 WSLg 的 RDP 合成，窗口直接出在 Windows 桌面
- epoch 检测从跨 9p 的 1.1 ms 降到本地 NTFS 的 0.5 ms（p50）

测的是什么
---------
    Δ = 显示链路 + 曝光 + MJPEG 编码 + USB 传输 + 驱动缓冲 + ffmpeg 处理
        + 首帧编码落盘那一段

ffmpeg.exe 启动的那 1.4 秒**不在** Δ 里：WSL 路径没有用 ``-copyts``，ffmpeg 会把
首帧的 pts 归零，实测首帧 pts 恰好是 0.0 ms，epoch 就落在首帧写出那一刻。所以这个
Δ 是真的链路延迟，不是「锚点没对齐」的误差。

两个数不可互换的原因不是差一个常数，而是基准不同：原生 Ubuntu 的 pts 是绝对墙钟，
WSL 的 pts 是相对首帧、靠 epoch 平移。采集端写的
``timestamp_unix_ns = epoch_unix_ns + pts`` 整体偏晚这么多，下游减掉 Δ 就回到接近
曝光时刻。

用法
----
    python measure_camera_latency_wsl.py --list
    python measure_camera_latency_wsl.py                  # 两路各 20 秒
    python measure_camera_latency_wsl.py --repeat 3       # 各 20 秒 x 3 轮，取合并值

测量结论自动追加进 ``latency/records/<电脑用户>-win.yaml``，下游读最近的合格值。

依赖：opencv-python、qrcode、pillow、numpy（matplotlib 可选，用于画图）
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
import yaml

from latency_records import DELTA_WARN_MS, record_measurement

if hasattr(sys.stdout, "reconfigure"):
    # 不要强改成 UTF-8：PowerShell 5.1 / cmd 的控制台代码页是 936，按 UTF-8
    # 输出会整屏乱码。保持控制台原编码，只放宽错误处理，遇到编码不了的字符
    # 用 ? 代替而不是抛 UnicodeEncodeError 把测量打断。
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")

HERE = Path(__file__).resolve().parent
# 配置与输出放在 Cap_Quest 根；sdk 在再上一层的 Paxini-Cap 下
QUEST_ROOT = HERE.parent
CONFIG_PATH = QUEST_ROOT / "cap_config.yaml"
DEFAULT_OUTPUT_ROOT = QUEST_ROOT / "latency_measure"
DEFAULT_FFMPEG = QUEST_ROOT.parent / "sdk" / "ffmpeg-win" / "ffmpeg.exe"

# 采集只接两路外接相机（cam0 / cam1）；电脑自带摄像头不参与采集，不测。
CAPTURE_SLOTS = 2

MIN_SAMPLES = 30
COMFORTABLE_SAMPLES = 100
QR_TEXT_MIN_LEN = 18
QR_TEXT_MAX_LEN = 20
PLAUSIBLE_LATENCY_MS = (-50.0, 3000.0)
# 等 mkv 首次变大的上限；ffmpeg.exe 从启动到首字节落盘约 1.5 s。
EPOCH_TIMEOUT_S = 25.0
STOP_TIMEOUT_S = 6.0


# ---------------------------------------------------------------- 配置与设备


def load_cap_config() -> dict:
    if not CONFIG_PATH.is_file():
        return {}
    try:
        raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        print(f"[警告] 读取 {CONFIG_PATH.name} 失败，用内置默认值：{exc}", file=sys.stderr)
        return {}
    return {str(key): value for key, value in raw.items() if not str(key).startswith("_")}


def pick(value, fallback):
    return fallback if value is None else value


def find_ffmpeg(argument: str | None) -> str:
    """优先级：--ffmpeg > $FFMPEG_WIN > sdk/ffmpeg-win/ffmpeg.exe > PATH。"""
    candidates: list[Path] = []
    if argument:
        candidates.append(Path(argument).expanduser())
    from_env = os.environ.get("FFMPEG_WIN")
    if from_env:
        candidates.append(Path(from_env).expanduser())
    candidates.append(DEFAULT_FFMPEG)
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    found = None
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        probe = Path(directory) / "ffmpeg.exe"
        if probe.is_file():
            found = str(probe)
            break
    if found:
        return found
    raise RuntimeError(
        "找不到 ffmpeg.exe。用 --ffmpeg 指定，或放到 "
        f"{DEFAULT_FFMPEG}，或加进 PATH。"
    )


def discover_cameras(ffmpeg_exe: str) -> list[tuple[str, str]]:
    """枚举 DirectShow 视频输入，返回 [(名字, -i 参数)]。

    两路相机同名（都叫 MASTER Camera），必须用 Alternative name 里的 PnP
    实例 ID 才能区分，否则两路会打开同一台。
    """
    try:
        result = subprocess.run(
            [ffmpeg_exe, "-hide_banner", "-f", "dshow",
             "-list_devices", "true", "-i", "dummy"],
            check=False, capture_output=True, text=True, timeout=25.0,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"枚举 DirectShow 设备失败：{exc}") from exc

    devices: list[tuple[str, str]] = []
    pending: str | None = None
    for line in (result.stderr + result.stdout).splitlines():
        name_match = re.search(r'"([^"]+)"\s*\(video\)', line)
        if name_match:
            pending = name_match.group(1)
            continue
        alt_match = re.search(r'Alternative name "([^"]+)"', line)
        if alt_match and pending is not None:
            devices.append((pending, f"video={alt_match.group(1)}"))
            pending = None
            continue
        if pending is not None and "Alternative name" not in line and line.strip():
            devices.append((pending, f"video={pending}"))
            pending = None
    if pending is not None:
        devices.append((pending, f"video={pending}"))
    return devices


# ---------------------------------------------------------------- 二维码


def make_qr_gray(text: str, target_px: int) -> np.ndarray:
    """编二维码并按整数倍放大，避免模块边缘落在半个像素上导致解码率暴跌。"""
    import qrcode

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_Q,
        box_size=1,
        border=4,
    )
    qr.add_data(text)
    qr.make(fit=True)
    modules = qr.modules_count + 8
    scale = max(1, target_px // modules)
    image = qr.make_image(fill_color="black", back_color="white").convert("L")
    array = np.asarray(image, dtype=np.uint8)
    array = np.repeat(np.repeat(array, scale, axis=0), scale, axis=1)
    side = array.shape[0]
    if side < target_px:
        pad = target_px - side
        array = np.pad(
            array,
            ((pad // 2, pad - pad // 2), (pad // 2, pad - pad // 2)),
            mode="constant",
            constant_values=255,
        )
    return array


def parse_qr_time(text: str) -> int | None:
    text = text.strip()
    if not text.isdigit():
        return None
    if not (QR_TEXT_MIN_LEN <= len(text) <= QR_TEXT_MAX_LEN):
        return None
    return int(text)


def check_gui() -> None:
    """开窗前先探一下 GUI 后端。

    opencv-python 和 opencv-python-headless 装在同一个环境时，后装的那个
    会覆盖 cv2，表现为 ``cvNamedWindow ... is not implemented``。提前拦下来，
    省得跑完一轮采集才在开窗那一步炸。
    """
    import cv2

    try:
        cv2.namedWindow("__gui_probe__", cv2.WINDOW_NORMAL)
        cv2.destroyWindow("__gui_probe__")
        cv2.waitKey(1)
    except cv2.error as exc:
        raise RuntimeError(
            "当前 cv2 没有图形后端（装成了 opencv-python-headless）。修复：\n"
            "  python -m pip uninstall -y opencv-python-headless\n"
            "  python -m pip install --force-reinstall --no-deps opencv-python"
        ) from exc


# ---------------------------------------------------------------- 采集


def codec_args(codec: str) -> list[str]:
    if codec == "copy":
        return ["-c:v", "copy"]
    if codec == "mjpeg":
        return ["-c:v", "mjpeg", "-q:v", "3"]
    if codec == "h264":
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]
    if codec == "hevc":
        return ["-c:v", "libx265", "-preset", "veryfast", "-crf", "24", "-pix_fmt", "yuv420p"]
    raise RuntimeError(f"未知编码：{codec}")


def build_command(
    ffmpeg_exe: str, device: str, width: int, height: int,
    fps: float, codec: str, output: Path,
) -> list[str]:
    """与 collect_cap.py 的 wsl 后端逐项一致。

    DirectShow 不接受 -use_wallclock_as_timestamps（会 RenderStream 失败），
    所以 pts 从 0 起，绝对时刻只能靠 epoch 锚定 —— 这正是要测的量。
    """
    return [
        ffmpeg_exe, "-hide_banner", "-loglevel", "warning", "-nostats", "-y",
        "-f", "dshow",
        "-video_size", f"{width}x{height}",
        "-framerate", f"{fps:g}",
        "-i", device,
        "-map", "0:v:0",
        "-fps_mode", "passthrough",
        *codec_args(codec),
        str(output),
    ]


@dataclass
class ShowSample:
    seq: int
    text: str
    t_gen_ns: int
    t_ready_ns: int
    t_show_ns: int

    @property
    def encode_ms(self) -> float:
        return (self.t_ready_ns - self.t_gen_ns) / 1e6

    @property
    def imshow_ms(self) -> float:
        return (self.t_show_ns - self.t_ready_ns) / 1e6


@dataclass
class Recording:
    camera_id: str
    video_path: Path
    epoch_unix_ns: int
    start_unix_ns: int = 0
    timestamps_ns: list[int] = field(default_factory=list)
    samples: list[ShowSample] = field(default_factory=list)
    elapsed_s: float = 0.0
    log_tail: str = ""

    @property
    def frames(self) -> int:
        return len(self.timestamps_ns)

    @property
    def epoch_delay_ms(self) -> float:
        """从 ffmpeg 进程拉起，到 mkv 首次变大，用了多久。

        这个数直接进 Δ：epoch 打得越晚，Δ 越大。它的轮间波动就是测量结果
        的轮间波动来源，所以必须单独记下来做诊断。
        """
        if not self.start_unix_ns or not self.epoch_unix_ns:
            return 0.0
        return (self.epoch_unix_ns - self.start_unix_ns) / 1e6

    @property
    def actual_fps(self) -> float:
        if len(self.timestamps_ns) < 2:
            return 0.0
        span = (self.timestamps_ns[-1] - self.timestamps_ns[0]) / 1e9
        return (len(self.timestamps_ns) - 1) / span if span > 0 else 0.0


def wait_first_byte(path: Path, process: subprocess.Popen, timeout_s: float) -> int:
    """轮询直到文件首次变大，返回那一刻的墙钟（本脚本在 Windows 侧，取到的是 Windows 时钟）。

    这就是采集端的 epoch：ffmpeg 把首帧写进容器的时刻。它晚于首帧被拍下，
    晚的那一段正是本程序要测的 Δ 的一部分。
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"ffmpeg 提前退出（退出码 {process.returncode}），检查设备名与分辨率")
        try:
            if path.is_file() and path.stat().st_size > 0:
                return time.time_ns()
        except OSError:
            pass
        time.sleep(0.002)
    raise RuntimeError(
        f"{timeout_s:g} 秒内 mkv 没有变大：ffmpeg 没出图。"
        "检查设备是否被别的程序占用、分辨率/帧率是否被支持"
    )


def record(
    ffmpeg_exe: str, device: str, output_dir: Path, camera_id: str,
    width: int, height: int, fps: float, codec: str,
    seconds: float, size_px: int, target_hz: float, window: str,
) -> Recording:
    """启动 ffmpeg.exe 录一段，同时开窗刷二维码。"""
    import cv2

    video_path = output_dir / f"{camera_id}.mkv"
    log_path = output_dir / f"{camera_id}.log"
    for stale in (video_path, log_path):
        try:
            stale.unlink()
        except FileNotFoundError:
            pass

    command = build_command(ffmpeg_exe, device, width, height, fps, codec, video_path)
    log_handle = log_path.open("wb")
    start_ns = time.time_ns()
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,  # Windows 上只有 stdin 写 q 才能优雅收尾
        stdout=subprocess.DEVNULL,
        stderr=log_handle,
    )

    samples: list[ShowSample] = []
    window_ready = False
    try:
        # 先把窗口开起来并显示等待提示：相机启动约 1.5 s，这段时间窗口不能白着。
        cv2.namedWindow(window, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(window, size_px, size_px)
        cv2.moveWindow(window, 60, 60)
        window_ready = True
        blank = np.full((size_px, size_px), 255, dtype=np.uint8)
        cv2.putText(blank, "waiting", (24, size_px // 2), cv2.FONT_HERSHEY_SIMPLEX,
                    1.0, (0, 0, 0), 2, cv2.LINE_AA)
        cv2.imshow(window, blank)
        cv2.waitKey(1)

        epoch_ns = wait_first_byte(video_path, process, EPOCH_TIMEOUT_S)
        # DirectShow 起来后头几帧常常重复或忽长忽短，跳过这一段再取样。
        settle_deadline = time.monotonic() + 1.0
        while time.monotonic() < settle_deadline:
            cv2.imshow(window, blank)
            cv2.waitKey(1)
            time.sleep(0.02)

        print(f"[INFO] 首帧已落盘，开始刷码 {seconds:g} 秒（按 q 提前结束）", flush=True)
        period = 1.0 / target_hz if target_hz > 0 else 0.0
        started = time.monotonic()
        deadline = started + seconds
        next_at = time.perf_counter()
        seq = 0
        while time.monotonic() < deadline:
            t_gen = time.time_ns()
            text = str(t_gen)
            gray = make_qr_gray(text, size_px)
            t_ready = time.time_ns()
            cv2.imshow(window, gray)
            key = cv2.waitKey(1) & 0xFF
            t_show = time.time_ns()
            samples.append(ShowSample(seq, text, t_gen, t_ready, t_show))
            seq += 1
            if key == ord("q"):
                print("[INFO] 收到 q，提前结束", flush=True)
                break
            if period:
                next_at += period
                delay = next_at - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)
                else:
                    next_at = time.perf_counter()
        elapsed = time.monotonic() - started
    finally:
        if window_ready:
            cv2.destroyWindow(window)
            cv2.waitKey(1)
        if process.poll() is None:
            try:
                if process.stdin is not None:
                    process.stdin.write(b"q")
                    process.stdin.flush()
            except (OSError, ValueError):
                pass
            try:
                process.wait(timeout=STOP_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3.0)
        log_handle.close()

    tail = ""
    try:
        tail = log_path.read_text(encoding="utf-8", errors="replace").strip()[-400:]
    except OSError:
        pass
    return Recording(camera_id, video_path, epoch_ns, start_ns,
                     samples=samples, elapsed_s=elapsed, log_tail=tail)


# ---------------------------------------------------------------- pts 与解码


def probe_pts(ffmpeg_exe: str, path: Path) -> list[int]:
    """用 showinfo 把每帧 pts_time 抓出来。

    sdk 里只有 ffmpeg.exe、没有 ffprobe.exe，所以走解码到 null 这条路，
    stderr 每帧一行 ``... pts_time:1.234 ...``。
    """
    try:
        result = subprocess.run(
            [ffmpeg_exe, "-hide_banner", "-nostdin", "-i", str(path),
             "-vf", "showinfo", "-f", "null", "-"],
            check=False, capture_output=True, text=True, timeout=900.0,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"读取 pts 失败：{exc}") from exc
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg 解析 {path.name} 失败：{result.stderr.strip()[-300:]}")

    stamps: list[int] = []
    for line in result.stderr.splitlines():
        match = re.search(r"pts_time:\s*(-?[0-9.]+)", line)
        if match:
            try:
                stamps.append(int(round(float(match.group(1)) * 1e9)))
            except ValueError:
                continue
    return stamps


@dataclass
class FrameHit:
    index: int
    frame_ts_ns: int
    qr_ts_ns: int
    latency_ms: float


def decode_video(recording: Recording) -> tuple[list[FrameHit], int, int]:
    import cv2

    capability = cv2.VideoCapture(str(recording.video_path))
    if not capability.isOpened():
        raise RuntimeError(f"打不开视频：{recording.video_path}")
    reported = int(capability.get(cv2.CAP_PROP_FRAME_COUNT))
    if reported and reported != recording.frames:
        print(
            f"[警告] 容器报 {reported} 帧，showinfo 读到 {recording.frames} 帧；"
            "按较少的一边截断",
            file=sys.stderr,
        )

    detector = cv2.QRCodeDetector()
    hits: list[FrameHit] = []
    seen: set[int] = set()
    index = 0
    decoded = 0
    limit = min(recording.frames, reported) if reported else recording.frames

    while index < limit:
        ok, frame = capability.read()
        if not ok:
            break
        frame_ts = recording.timestamps_ns[index]
        text, _points, _straight = detector.detectAndDecode(frame)
        if text:
            decoded += 1
            qr_ts = parse_qr_time(text)
            if qr_ts is not None and qr_ts not in seen:
                # 同一个码被连续多帧拍到只留第一次：那一帧离上屏最近。
                seen.add(qr_ts)
                latency = (frame_ts - qr_ts) / 1e6
                low, high = PLAUSIBLE_LATENCY_MS
                if low <= latency <= high:
                    hits.append(FrameHit(index, frame_ts, qr_ts, latency))
        index += 1

    capability.release()
    return hits, index, decoded


def summarize(hits: list[FrameHit]) -> dict:
    values = np.array([hit.latency_ms for hit in hits], dtype=float)
    if values.size == 0:
        return {"samples": 0}
    return {
        "samples": int(values.size),
        "median_ms": float(np.median(values)),
        "mean_ms": float(values.mean()),
        "std_ms": float(values.std(ddof=1)) if values.size > 1 else 0.0,
        "min_ms": float(values.min()),
        "max_ms": float(values.max()),
        "p05_ms": float(np.percentile(values, 5)),
        "p25_ms": float(np.percentile(values, 25)),
        "p75_ms": float(np.percentile(values, 75)),
        "p95_ms": float(np.percentile(values, 95)),
        "iqr_ms": float(np.percentile(values, 75) - np.percentile(values, 25)),
    }


def verdict(stats: dict, detection_rate: float, shown: int) -> list[str]:
    # 只用 cp936 里存在的字符：Windows 控制台默认 GBK，✓ ✗ 会变成问号。
    lines: list[str] = []
    count = stats.get("samples", 0)
    if count < MIN_SAMPLES:
        return [
            f"[BAD] 有效样本只有 {count} 个（要 ≥ {MIN_SAMPLES}）：二维码没被稳定拍到。"
            "把窗口拖进相机视野、放大、手动对焦后重测"
        ]
    if count < COMFORTABLE_SAMPLES:
        lines.append(f"[WARN] 样本 {count} 个，够用但偏少")
    else:
        lines.append(f"[OK] 样本充足（{count} 个）")

    iqr = stats["iqr_ms"]
    if iqr <= 20:
        lines.append(f"[OK] 抖动小（IQR {iqr:.1f} ms），分布集中，可直接用")
    elif iqr <= 50:
        lines.append(f"[WARN] 抖动中等（IQR {iqr:.1f} ms），建议再测一次看是否稳定")
    else:
        lines.append(f"[BAD] 抖动大（IQR {iqr:.1f} ms）：关掉其他程序、别让屏幕反光后重测")

    if detection_rate < 0.3:
        lines.append(f"[WARN] 解码率只有 {detection_rate * 100:.1f}%，二维码在画面里太小或失焦")
    else:
        lines.append(f"[OK] 解码率 {detection_rate * 100:.1f}%")

    if shown:
        lines.append(
            f"- 刷出 {shown} 个不同二维码，被拍到 {count} 个"
            f"（{count / shown * 100:.1f}%）"
        )
    return lines


def write_outputs(
    output_dir: Path, recording: Recording, hits: list[FrameHit],
    stats: dict, meta: dict,
) -> dict:
    frame_csv = output_dir / f"{recording.camera_id}_frames.csv"
    with frame_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["frame_index", "frame_time_unix_ns", "qr_time_unix_ns", "latency_ms"])
        for hit in hits:
            writer.writerow([hit.index, hit.frame_ts_ns, hit.qr_ts_ns, f"{hit.latency_ms:.3f}"])

    show_csv = output_dir / f"{recording.camera_id}_screen.csv"
    with show_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["seq", "t_gen_unix_ns", "encode_ms", "imshow_ms"])
        for sample in recording.samples:
            writer.writerow([sample.seq, sample.t_gen_ns,
                             f"{sample.encode_ms:.3f}", f"{sample.imshow_ms:.3f}"])

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "clock_domain": "windows (QR 时间与 epoch 同为 time.time_ns，跨系统时钟差不参与运算)",
        "camera": {
            "id": recording.camera_id,
            "device": meta.get("device_name"),
            "width": meta.get("width"),
            "height": meta.get("height"),
            "fps_target": meta.get("fps"),
            "codec": meta.get("codec"),
            "frames": recording.frames,
            "actual_fps": round(recording.actual_fps, 3),
            "epoch_unix_ns": recording.epoch_unix_ns,
            "epoch_delay_ms": round(recording.epoch_delay_ms, 2),
            "recorded_s": round(recording.elapsed_s, 3),
        },
        "screen": {
            "qr_count": len(recording.samples),
            "refresh_hz": round(len(recording.samples) / recording.elapsed_s, 2)
            if recording.elapsed_s > 0 else 0.0,
            "encode_ms_mean": round(statistics.fmean([s.encode_ms for s in recording.samples]), 3)
            if recording.samples else 0.0,
            "imshow_ms_mean": round(statistics.fmean([s.imshow_ms for s in recording.samples]), 3)
            if recording.samples else 0.0,
        },
        "latency": stats,
        "correction": {
            "display_latency_ms_assumed": meta.get("display_latency_ms"),
            "median_minus_display_ms": round(
                stats["median_ms"] - meta.get("display_latency_ms", 0.0), 2
            ) if stats.get("samples") else None,
            "note": "timestamp_unix_ns 与 timestamp_monotonic_ns 都减这个值",
        },
    }
    json_path = output_dir / f"{recording.camera_id}_summary.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    result = {"frame_csv": frame_csv, "screen_csv": show_csv, "json": json_path, "plot": None}
    if not hits:
        return result

    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        values = [hit.latency_ms for hit in hits]
        figure, axes = plt.subplots(1, 2, figsize=(11, 4.2))
        axes[0].hist(values, bins=40, color="#3b7dd8", edgecolor="white")
        axes[0].axvline(stats["median_ms"], color="#d83b3b", linewidth=1.6, label="median")
        axes[0].set_xlabel("latency (ms)")
        axes[0].set_ylabel("frames")
        axes[0].set_title("latency distribution")
        axes[0].legend()
        axes[1].plot([hit.index for hit in hits], values, ".", markersize=3, color="#3b7dd8")
        axes[1].axhline(stats["median_ms"], color="#d83b3b", linewidth=1.2)
        axes[1].set_xlabel("frame index")
        axes[1].set_ylabel("latency (ms)")
        axes[1].set_title("latency over time")
        figure.tight_layout()
        plot_path = output_dir / f"{recording.camera_id}_hist.png"
        figure.savefig(plot_path, dpi=130)
        plt.close(figure)
        result["plot"] = plot_path
    except Exception as exc:
        print(f"[警告] 直方图没画出来：{exc}", file=sys.stderr)
    return result


def write_merged(
    output_dir: Path, camera_id: str, rounds: list[dict],
    hits: list[FrameHit], stats: dict, meta: dict,
) -> tuple[Path | None, Path | None]:
    """多轮合并结果。

    单轮内部很稳（IQR 几个 ms），轮与轮的差别主要来自 ffmpeg 启动到首字节
    那段（epoch 锚定），实测能差十几 ms。合并多轮把这段噪声压下去。
    """
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "clock_domain": "windows (QR 时间与 epoch 同为 time.time_ns)",
        "rounds": rounds,
        "merged_latency": stats,
        "correction": {
            "display_latency_ms_assumed": meta.get("display_latency_ms"),
            "median_minus_display_ms": round(
                stats["median_ms"] - meta.get("display_latency_ms", 0.0), 2
            ) if stats.get("samples") else None,
            "note": "timestamp_unix_ns 与 timestamp_monotonic_ns 都减这个值",
        },
    }
    json_path = output_dir / f"{camera_id}_merged.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    plot_path = None
    if not hits:
        return json_path, None
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        values = [hit.latency_ms for hit in hits]
        figure, axis = plt.subplots(figsize=(7.5, 4.2))
        axis.hist(values, bins=50, color="#3b7dd8", edgecolor="white")
        axis.axvline(stats["median_ms"], color="#d83b3b", linewidth=1.6, label="median")
        axis.set_xlabel("latency (ms)")
        axis.set_ylabel("frames")
        axis.set_title(f"merged latency, {len(rounds)} rounds, n={stats['samples']}")
        axis.legend()
        figure.tight_layout()
        plot_path = output_dir / f"{camera_id}_merged_hist.png"
        figure.savefig(plot_path, dpi=130)
        plt.close(figure)
    except Exception as exc:
        print(f"[警告] 合并直方图没画出来：{exc}", file=sys.stderr)
    return json_path, plot_path


# ---------------------------------------------------------------- 入口

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "测量相机延迟（WSL 采集路径：相机走 Windows 的 ffmpeg.exe + DirectShow，"
            "本脚本在 Windows 侧执行，让 QR 时刻与 epoch 同源）"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python measure_camera_latency_wsl.py --list\n"
            "  python measure_camera_latency_wsl.py               # 两路都测，各 20 秒\n"
        ),
    )
    parser.add_argument("--list", action="store_true", help="只枚举相机")
    parser.add_argument(
        "--camera",
        type=int,
        default=None,
        help="只测第几路；不填则测 cam0 和 cam1（两路延迟不同，默认都测）",
    )
    parser.add_argument("--seconds", type=float, default=20.0, help="录制时长，默认 20 秒")
    parser.add_argument("--qr-size", type=int, default=800, help="二维码窗口边长，默认 800 px")
    parser.add_argument("--qr-hz", type=float, default=100.0, help="目标刷新率，默认 100 Hz")
    parser.add_argument("--repeat", type=int, default=1,
                        help="跑几轮再合并统计，默认 1；推荐 3，轮间抖动靠它压下来")
    parser.add_argument("--display-latency-ms", type=float, default=0.0,
                        help="手动扣除的显示链路延迟，只影响 summary 里的修正值")
    parser.add_argument("--output-dir", type=Path, default=None, help="输出目录")
    parser.add_argument("--ffmpeg", default=None, help="ffmpeg.exe 路径")
    parser.add_argument("--width", type=int, default=None)
    parser.add_argument("--height", type=int, default=None)
    parser.add_argument("--fps", type=float, default=None)
    parser.add_argument("--video-codec", default=None, choices=("copy", "h264", "hevc", "mjpeg"))
    parser.add_argument("--device", default=None, help="直接指定 -i 参数")

    group = parser.add_argument_group("测量记录（写 latency/records/<机器>.yaml）")
    group.add_argument("--machine", default=None,
                       help="台账里的机器标识，默认 <用户名>-win")
    group.add_argument("--record-dir", type=Path, default=None,
                       help="台账目录，默认 latency/records")
    group.add_argument("--note", default="",
                       help="给这次测量写一句备注，比如「换了 USB 口」「锁死曝光」")
    group.add_argument("--no-record", action="store_true",
                       help="只出本次产物，不写台账（调试用）")
    return parser.parse_args(argv)


def measure_one(
    index: int,
    device_name: str,
    device_input: str,
    args: argparse.Namespace,
    width: int,
    height: int,
    fps: float,
    codec: str,
    ffmpeg_exe: str,
    stamp: str,
    multi: bool,
) -> dict:
    """测一路相机（含 --repeat 多轮）：录制 → 解码 → 落盘 → 记账。

    逐路独立起停：某一路失败不影响另一路出结果，最后按最严重的那一路给退出码。
    """
    if args.output_dir:
        # 多路时每路一个子目录，否则两路的产物会混在一起
        output_dir = args.output_dir / f"cam{index}" if multi else args.output_dir
    else:
        output_dir = DEFAULT_OUTPUT_ROOT / f"{stamp}_wsl_cam{index}"
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    camera_id = f"wsl_cam{index}"

    print("=" * 62)
    print(f"相机延迟测量（WSL 采集路径）第 {index + 1} 路")
    print("=" * 62)
    print(f"相机       [{index}] {device_name}")
    print(f"采集参数   {width}x{height}@{fps:g}  codec={codec}")
    print(f"录多久     {args.seconds:g} 秒")
    print(f"二维码     {args.qr_size} px / 目标 {args.qr_hz:g} Hz")
    print(f"时钟域     Windows（本脚本在此执行，QR 与 epoch 同源）")
    print(f"输出       {output_dir}")
    print("-" * 62)
    print("把二维码窗口对准相机，占画面四分之一以上。")
    print("=" * 62)

    meta = {
        "device_name": device_name, "width": width, "height": height,
        "fps": fps, "codec": codec, "display_latency_ms": args.display_latency_ms,
    }
    rounds: list[dict] = []
    all_hits: list[FrameHit] = []
    last_recording: Recording | None = None
    paths: dict = {}
    frames_total = 0
    decoded_total = 0
    shown_total = 0

    for round_index in range(max(1, args.repeat)):
        round_no = round_index + 1
        if round_index > 0:
            # DirectShow 设备是独占的：上一轮 ffmpeg.exe 退出后驱动还要一点时间
            # 才真正释放，紧接着开下一轮会拿到 E_ACCESSDENIED(-5)。
            print("[INFO] 等待相机设备释放…", flush=True)
            time.sleep(2.5)
        if args.repeat > 1:
            round_dir = output_dir / f"round{round_no}"
            round_dir.mkdir(parents=True, exist_ok=True)
            print(f"--- 第 {round_no}/{args.repeat} 轮 ---", flush=True)
        else:
            round_dir = output_dir

        recording: Recording | None = None
        last_error: BaseException | None = None
        for attempt in range(2):
            try:
                recording = record(
                    ffmpeg_exe, device_input, round_dir, camera_id,
                    width, height, fps, codec,
                    args.seconds, args.qr_size, args.qr_hz, camera_id,
                )
                break
            except (RuntimeError, OSError) as exc:
                last_error = exc
                if attempt == 0:
                    # E_ACCESSDENIED 多半是上一轮的 ffmpeg 还没彻底放开设备
                    print(f"[警告] 启动失败，3 秒后重试：{exc}", file=sys.stderr)
                    time.sleep(3.0)
            except Exception as exc:  # cv2.error 等
                print(f"[警告] 采集中断：{type(exc).__name__}: {exc}", file=sys.stderr)
                return {"camera": index, "stats": {"samples": 0}, "code": 3}
        if recording is None:
            print(f"[警告] 第 {index} 路采集失败：{last_error}", file=sys.stderr)
            return {"camera": index, "stats": {"samples": 0}, "code": 3}

        try:
            stamps = probe_pts(ffmpeg_exe, recording.video_path)
        except RuntimeError as exc:
            print(f"[警告] 第 {index} 路 pts 提取失败：{exc}", file=sys.stderr)
            return {"camera": index, "stats": {"samples": 0}, "code": 4}
        if not stamps:
            print(f"[警告] 第 {index} 路视频里没有可读帧", file=sys.stderr)
            return {"camera": index, "stats": {"samples": 0}, "code": 4}
        # DirectShow 的 pts 从 0 起，加 epoch 还原绝对时刻。
        recording.timestamps_ns = [recording.epoch_unix_ns + value for value in stamps]
        print(f"[INFO] {recording.frames} 帧 / {recording.actual_fps:.1f} fps", flush=True)
        print("[INFO] 逐帧解码二维码，稍等", flush=True)

        try:
            hits, total, decoded = decode_video(recording)
        except RuntimeError as exc:
            print(f"[警告] 第 {index} 路解码失败：{exc}", file=sys.stderr)
            return {"camera": index, "stats": {"samples": 0}, "code": 4}

        round_stats = summarize(hits)
        rounds.append({
            "round": round_no,
            "frames": recording.frames,
            "decoded": decoded,
            "epoch_delay_ms": round(recording.epoch_delay_ms, 2),
            "latency": round_stats,
        })
        if args.repeat > 1 and round_stats.get("samples"):
            print(f"[INFO] 本轮中位数 {round_stats['median_ms']:.2f} ms"
                  f"（{round_stats['samples']} 样本，epoch {recording.epoch_delay_ms:.0f} ms）",
                  flush=True)
        paths = write_outputs(round_dir, recording, hits, round_stats, meta)
        all_hits.extend(hits)
        frames_total += recording.frames
        decoded_total += decoded
        shown_total += len(recording.samples)
        last_recording = recording

    recording = last_recording
    hits = all_hits
    stats = summarize(hits)
    detection_rate = decoded_total / frames_total if frames_total else 0.0

    merged_json = None
    merged_plot = None
    if args.repeat > 1:
        merged_json, merged_plot = write_merged(output_dir, camera_id, rounds, hits, stats, meta)

    # 把这次结论追加进台账：下游按机器读一个固定文件，不用翻时间戳目录。
    recorded = None
    if not args.no_record:
        try:
            recorded = record_measurement(
                platform="win",
                camera=index,
                stats=stats,
                meta={**meta, "seconds": args.seconds},
                output_dir=output_dir,
                record_dir=args.record_dir,
                machine=args.machine,
                detection_rate=detection_rate,
                note=args.note,
                clock_domain="windows（QR 时间与 epoch 同为 time.time_ns）",
            )
        except (OSError, RuntimeError) as exc:
            print(f"[警告] 第 {index} 路测量记录没写成功，本次产物不受影响：{exc}",
                  file=sys.stderr)

    print("=" * 62)
    print(f"第 {index} 路结果")
    print("=" * 62)
    if args.repeat > 1:
        print(f"轮次       {args.repeat} 轮，共 {frames_total} 帧")
        print("各轮中位数")
        for item in rounds:
            lat = item["latency"]
            value = f"{lat['median_ms']:.2f} ms" if lat.get("samples") else "无样本"
            print(f"  round{item['round']}  {value:>12}"
                  f"   {lat.get('samples', 0)} 样本, epoch {item['epoch_delay_ms']:.0f} ms")
    else:
        print(f"录制       {recording.frames} 帧 / {recording.elapsed_s:.1f} s / "
              f"{recording.actual_fps:.1f} fps")
        print(f"epoch 锚定 进程启动→首字节 {recording.epoch_delay_ms:.1f} ms"
              "（不进 Δ：首帧 pts 归零，epoch 落在首帧写出那一刻）")
    print(f"解码       {decoded_total} / {frames_total} 帧（{detection_rate * 100:.1f}%）")
    print(f"有效样本   {stats.get('samples', 0)}")
    print("-" * 62)

    if stats.get("samples"):
        print("Δ = 帧时间戳 - 拍摄时刻（含显示链路）")
        print(f"  中位数    {stats['median_ms']:8.2f} ms   ← 建议填这个")
        print(f"  均值      {stats['mean_ms']:8.2f} ms")
        print(f"  标准差    {stats['std_ms']:8.2f} ms")
        print(f"  P5 / P95  {stats['p05_ms']:8.2f} / {stats['p95_ms']:.2f} ms")
        print(f"  最小/最大 {stats['min_ms']:8.2f} / {stats['max_ms']:.2f} ms")
        print(f"  IQR       {stats['iqr_ms']:8.2f} ms")
        print("-" * 62)
        if recording.samples:
            print("显示链路可观测开销（下界）")
            print(f"  QR 编码+转图  {statistics.fmean([s.encode_ms for s in recording.samples]):5.2f} ms")
            print(f"  imshow+事件   {statistics.fmean([s.imshow_ms for s in recording.samples]):5.2f} ms")
        if args.display_latency_ms:
            print(f"扣掉 {args.display_latency_ms:g} ms 显示链路后："
                  f"{stats['median_ms'] - args.display_latency_ms:.2f} ms")
        print("-" * 62)
        print("判读")
        for line in verdict(stats, detection_rate, shown_total):
            print(f"  {line}")
    else:
        print("没有有效样本。常见原因：")
        print("  - 二维码没进画面：窗口拖到相机正前方，放大到画面 1/4 以上")
        print("  - 失焦或过曝：手动对焦，避开屏幕反光")
        if recording.log_tail:
            print(f"  - ffmpeg 日志尾部：{recording.log_tail[-200:]}")

    print("-" * 62)
    print("输出文件")
    if args.repeat > 1:
        print(f"  每轮明细   {output_dir}\\round<N>")
        if merged_json:
            print(f"  合并汇总   {merged_json}")
        if merged_plot:
            print(f"  合并直方图 {merged_plot}")
    else:
        print(f"  逐帧明细   {paths['frame_csv']}")
        print(f"  屏幕刷新   {paths['screen_csv']}")
        print(f"  汇总       {paths['json']}")
        if paths["plot"]:
            print(f"  直方图     {paths['plot']}")
    if recorded:
        record_file, seq, entry = recorded
        verdict_text = "合格" if entry["ok"] else "不合格"
        print(f"  测量记录   {record_file}  第 {seq} 次（{verdict_text}）")
        if entry.get("median_ms") is not None:
            print(f"             下游用这个：{entry['median_ms']:.2f} ms")
        delta = entry.get("delta_prev_ms")
        if delta is not None and abs(delta) > DELTA_WARN_MS:
            before = entry["median_ms"] - delta
            print(
                f"[警告] cam{index} 与上一条合格记录差 {delta:+.1f} ms"
                f"（{before:.2f} → {entry['median_ms']:.2f}）。环境没变的话重测一次确认："
                "驱动缓冲、自动曝光、换 USB 口都会让整条时间轴整体平移",
                file=sys.stderr,
            )
    print("=" * 62)

    return {
        "camera": index,
        "stats": stats,
        "detection_rate": detection_rate,
        "recorded": recorded,
        "code": 0 if stats.get("samples", 0) >= MIN_SAMPLES else 5,
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_cap_config()

    width = pick(args.width, config.get("camera_width", 1280))
    height = pick(args.height, config.get("camera_height", 720))
    fps = pick(args.fps, config.get("camera_fps", 30))
    codec = pick(args.video_codec, config.get("video_codec", "copy"))

    try:
        ffmpeg_exe = find_ffmpeg(args.ffmpeg)
        devices = discover_cameras(ffmpeg_exe)
    except RuntimeError as exc:
        print(f"[警告] {exc}", file=sys.stderr)
        return 2

    if args.list:
        print(f"ffmpeg.exe  {ffmpeg_exe}")
        print(f"采集参数    {width}x{height}@{fps:g}  codec={codec}")
        print("相机：")
        for index, (name, device_input) in enumerate(devices):
            print(f"  [{index}] {name}")
            print(f"      {device_input}")
        if devices:
            print("默认        测 cam0 和 cam1（前两路外接相机）；只想测一路用 --camera N")
        return 0

    if not devices:
        print("[警告] 没有枚举到相机", file=sys.stderr)
        return 2

    # 只测采集在用的那两路（cam0 / cam1）。电脑自带的摄像头即使被枚举到也不测：
    # 它不参与采集，测出来的数没有用处，还白占一段录制时间。
    if args.camera is None:
        targets = list(range(min(CAPTURE_SLOTS, len(devices))))
        if len(devices) > CAPTURE_SLOTS:
            print(f"[INFO] 枚举到 {len(devices)} 路，只测前两路 cam0/cam1；"
                  f"要测别的用 --camera N")
    else:
        if args.camera < 0 or args.camera >= len(devices):
            print(f"[警告] --camera {args.camera} 超出范围，只有 {len(devices)} 路",
                  file=sys.stderr)
            return 2
        targets = [args.camera]

    if args.device and len(targets) > 1:
        print("[警告] --device 只能配合 --camera 单路使用；本次忽略它，按枚举到的相机逐路测",
              file=sys.stderr)

    try:
        check_gui()
    except RuntimeError as exc:
        print(f"[警告] {exc}", file=sys.stderr)
        return 2

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    multi = len(targets) > 1
    if multi:
        print("本次逐路测量 %d 路相机：%s" % (len(targets), "、".join(f"cam{i}" for i in targets)))

    results = []
    for index in targets:
        device_name, device_input = devices[index]
        if args.device and not multi:
            device_input = args.device
        results.append(
            measure_one(
                index, device_name, device_input, args,
                width, height, fps, codec, ffmpeg_exe, stamp, multi,
            )
        )

    if multi:
        print()
        print("=" * 62)
        print("各路汇总（两路是不同 USB 口，各用各的，不要取平均）")
        print("=" * 62)
        for item in results:
            stats = item["stats"]
            if stats.get("samples"):
                state = "合格" if item["code"] == 0 else "样本不足"
                print(f"  cam{item['camera']}  {stats['median_ms']:8.2f} ms   "
                      f"IQR {stats['iqr_ms']:5.2f}   n={stats['samples']:<4} {state}")
            else:
                print(f"  cam{item['camera']}  没有有效样本（原因见上方）")

    print("=" * 62)
    print("怎么用：")
    print("  时间轴减掉**对应那一路**的中位数：cam0 用 cam0 的数，cam1 用 cam1 的数")
    print("  timestamp_unix_ns       -= 中位数(ms) * 1e6")
    print("  timestamp_monotonic_ns  同样减这个量")
    print("  之后再用就甭重测了，直接从 latency/records/ 读最近一次合格值")
    codes = [item["code"] for item in results]
    for bad in (4, 3, 5):
        if bad in codes:
            return bad
    return 0


if __name__ == "__main__":
    sys.exit(main())
