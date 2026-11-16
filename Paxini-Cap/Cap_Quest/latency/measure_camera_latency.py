#!/usr/bin/env python3.10
"""measure_camera_latency.py —— 测量相机采集链路的延迟

问题
----
采集端给相机写的时间戳不是「这一帧被拍下的时刻」。wsl 后端（Windows 采集路径）里：

    timestamp_unix_ns[i] = epoch_unix_ns + pts[i]

``pts`` 是 DirectShow 的流相对时间，从 0 起；``epoch_unix_ns`` 是「mkv 文件
首次变大」时打下的 WSL 墙钟。这一帧真正被拍下，比 ``epoch + pts`` 早若干毫秒
到几百毫秒 —— 早的那段就是本程序要测的 Δ。

拿到 Δ 之后，下游把 ``timestamp_unix_ns`` 整体减掉它，时间轴就回到接近曝光时刻。

做法
----
1. 屏幕上以最高速刷新二维码，内容是生成那一刻的 ``time.time_ns()``。
   必须是 **WSL 墙钟**：Windows 与 WSL 的墙钟实测差 1.6 s，混用会得到一个
   一千多秒的假延迟（见 DOC/时间戳说明.md §4.2）。
2. 用 ``camera_ffmpeg.FfmpegCamera`` 录一段 mkv，参数与 collect_cap.py 完全
   一致 —— 只有同一条链路测出来的数才能拿去修采集数据。
3. 停止后 ffprobe 取每帧 pts，加 epoch 还原绝对时刻。
4. 逐帧解码二维码，得到该帧画面上那份内容的生成时刻。
5. Δ = 帧时刻 − 码内时刻，逐帧落盘，输出中位数。

测出来的 Δ 包含两段
------------------
- **相机链路**：曝光 + MJPEG 编码 + USB 传输 + 驱动缓冲 + ffmpeg 处理
- **显示链路**：WSLg 合成 + 窗口上屏 + 屏幕刷新等待

真实世界场景要的是前者，所以填参数时按 ``--display-latency-ms`` 扣掉后者。
显示那段本程序测不到绝对值，只给出可观测下界（QR 编码耗时、imshow 耗时）。
屏幕刷新等待的理论均值是半个刷新周期 —— 60 Hz 屏约 8.3 ms。

用法
----
    cd /mnt/c/Users/Andy/Desktop/paxini/Paxini-Cap/Cap_Quest
    python3.10 measure_camera_latency.py --list
    python3.10 measure_camera_latency.py --camera 0 --seconds 15

对着屏幕放相机，让二维码占画面四分之一以上；按 q 可提前结束。
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

# 采集模块在 ../capture：延时测量单独成目录后要显式补搜索路径
_CAPTURE_DIR = Path(__file__).resolve().parent.parent / "capture"
if str(_CAPTURE_DIR) not in sys.path:
    sys.path.insert(0, str(_CAPTURE_DIR))

from camera_ffmpeg import (
    CAMERA_BACKENDS,
    VIDEO_CODECS,
    FfmpegCamera,
    discover_video_nodes,
    discover_windows_cameras,
    ffmpeg_version,
    find_ffmpeg,
    find_windows_ffmpeg,
    log,
    warn,
)

HERE = Path(__file__).resolve().parent
# 配置与输出都放在 Cap_Quest 根目录，和采集程序共用同一份 cap_config.json
QUEST_ROOT = HERE.parent
CONFIG_PATH = QUEST_ROOT / "cap_config.json"
DEFAULT_OUTPUT_ROOT = QUEST_ROOT / "latency_measure"

# 一次测量至少要有这么多有效样本，否则中位数不可信。
MIN_SAMPLES = 30
# 有效样本低于这个数就提示调整二维码大小/距离。
COMFORTABLE_SAMPLES = 100
# 二维码内容合法长度：19 位纳秒整数（约 1.79e18）。
QR_TEXT_MIN_LEN = 18
QR_TEXT_MAX_LEN = 20
# 延迟落在窗口外的样本视为解码错误或时钟异常，直接丢弃。
PLAUSIBLE_LATENCY_MS = (-50.0, 3000.0)
# 二维码只比生成时刻晚这么久以内的，才算同一批样本。
QR_ACCEPT_WINDOW_NS = 3_000_000_000


# ---------------------------------------------------------------- 配置与设备


def load_cap_config() -> dict:
    """读 cap_config.json；下划线开头的键是注释，丢掉。"""
    if not CONFIG_PATH.is_file():
        return {}
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        warn(f"读取 {CONFIG_PATH.name} 失败，改用内置默认值：{exc}")
        return {}
    return {key: value for key, value in raw.items() if not key.startswith("_")}


def pick(value, fallback):
    """命令行没给（None）就用配置值，配置也没有就用内置默认。"""
    return fallback if value is None else value


def resolve_devices(backend: str, count: int, ffmpeg_exe: str) -> list[tuple[str, str]]:
    """返回 [(显示名, -i 参数)]，顺序即相机编号顺序。

    wsl 后端走 DirectShow 枚举；同名相机靠 PnP 实例 ID 区分，不能只取名字，
    否则两路都会打开第一台（见 camera_ffmpeg.discover_windows_cameras）。
    """
    if backend == "wsl":
        items = discover_windows_cameras(ffmpeg_exe)
        return [(item["name"], item["input"]) for item in items]
    nodes = discover_video_nodes()
    return [(node, node) for node in nodes]


# ---------------------------------------------------------------- 二维码


def make_qr_gray(text: str, target_px: int) -> np.ndarray:
    """把文本编成二维码，按整数倍放大到接近 target_px 的方图。

    整数倍放大是必须的：非整数倍会让模块边缘落在半个像素上，相机一拍就糊，
    解码率断崖式下跌。这里用 repeat 而不是 cv2.resize，省一次插值。
    """
    import qrcode

    qr = qrcode.QRCode(
        version=None,
        # 19 位数字在 v1（21 模块）Q 级纠错下容量刚好够；Q 级有 25% 容错，
        # 比 L 级（7%）抗失焦和摩尔纹。
        error_correction=qrcode.constants.ERROR_CORRECT_Q,
        box_size=1,
        border=4,
    )
    qr.add_data(text)
    qr.make(fit=True)
    modules = qr.modules_count + 8  # 含 4 模块白边
    scale = max(1, target_px // modules)
    image = qr.make_image(fill_color="black", back_color="white").convert("L")
    array = np.asarray(image, dtype=np.uint8)
    array = np.repeat(np.repeat(array, scale, axis=0), scale, axis=1)
    side = array.shape[0]
    if side < target_px:
        # 用白边补齐到目标尺寸，二维码本身仍保持整数倍，居中即可。
        pad = target_px - side
        array = np.pad(
            array,
            ((pad // 2, pad - pad // 2), (pad // 2, pad - pad // 2)),
            mode="constant",
            constant_values=255,
        )
    return array


def parse_qr_time(text: str) -> int | None:
    """把二维码内容还原成纳秒时刻；不是纯数字就返回 None。"""
    text = text.strip()
    if not text.isdigit():
        return None
    if not (QR_TEXT_MIN_LEN <= len(text) <= QR_TEXT_MAX_LEN):
        return None
    return int(text)


# ---------------------------------------------------------------- 采集与显示


@dataclass
class ShowSample:
    """屏幕上刷出的一帧二维码。"""

    seq: int
    text: str
    t_gen_ns: int  # 写进二维码的那个时刻
    t_ready_ns: int  # 图像编码完成
    t_show_ns: int  # imshow + waitKey 返回

    @property
    def encode_ms(self) -> float:
        return (self.t_ready_ns - self.t_gen_ns) / 1e6

    @property
    def imshow_ms(self) -> float:
        return (self.t_show_ns - self.t_ready_ns) / 1e6


@dataclass
class RecordResult:
    camera_id: str
    video_path: Path
    frames: int
    fps: float
    epoch_unix_ns: int
    timestamps_ns: list[int]
    samples: list[ShowSample] = field(default_factory=list)
    elapsed_s: float = 0.0


def record_with_qr(
    camera: FfmpegCamera,
    seconds: float,
    size_px: int,
    target_hz: float,
    window: str,
) -> tuple[list[ShowSample], float]:
    """开窗刷码，同时让相机录 seconds 秒。

    返回 (屏幕上刷过的所有二维码, 实际刷码时长)。

    cv2 的窗口操作全部留在主线程：Qt 后端在子线程里 imshow 会偶发段错误。
    相机那边是独立进程，不占主线程。
    """
    import cv2

    cv2.namedWindow(window, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(window, size_px, size_px)
    cv2.moveWindow(window, 60, 60)

    samples: list[ShowSample] = []
    period = 1.0 / target_hz if target_hz > 0 else 0.0
    # 先等相机出图，再开始计时：启动那几秒的帧里还没有二维码。
    deadline_wait = time.monotonic() + 20.0
    last_frames = 0
    ready_streak = 0
    while time.monotonic() < deadline_wait:
        frames = camera.progress_frames()
        if frames > last_frames:
            last_frames = frames
            ready_streak += 1
            if ready_streak >= 3:
                break
        # 等相机的这段时间也要让窗口重绘，否则 WSLg 会显示成空白/未响应。
        blank = np.full((size_px, size_px), 255, dtype=np.uint8)
        cv2.putText(
            blank,
            "waiting for camera",
            (20, size_px // 2),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            (0, 0, 0),
            2,
            cv2.LINE_AA,
        )
        cv2.imshow(window, blank)
        cv2.waitKey(1)
        time.sleep(0.02)
    else:
        cv2.destroyWindow(window)
        raise RuntimeError("相机 20 秒内没有连续出帧，检查 --camera 编号与分辨率")

    log(f"[INFO] 相机已出帧，开始刷码 {seconds:g} 秒（按 q 提前结束）")

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
            log("[INFO] 收到 q，提前结束")
            break
        if period:
            next_at += period
            delay = next_at - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            else:
                # 编码跟不上目标频率：重置基准，别让欠账累积成突发。
                next_at = time.perf_counter()

    elapsed = time.monotonic() - started
    cv2.destroyWindow(window)
    cv2.waitKey(1)
    return samples, elapsed


def run_camera(
    slot: int,
    device_name: str,
    device_input: str,
    output_dir: Path,
    backend: str,
    ffmpeg_exe: str,
    windows_ffmpeg: str | None,
    width: int,
    height: int,
    fps: float,
    codec: str,
    seconds: float,
    size_px: int,
    target_hz: float,
) -> RecordResult:
    camera_id = f"lat_cam{slot}"
    camera = FfmpegCamera(
        camera_id,
        device_input,
        output_dir,
        width,
        height,
        fps,
        codec,
        ffmpeg_exe,
        backend=backend,
        windows_ffmpeg=windows_ffmpeg,
        preview_fps=0.0,  # 不要预览流：第二个输出会改变 ffmpeg 的写出节奏
    )
    camera.open()
    camera.start()
    try:
        samples, elapsed = record_with_qr(camera, seconds, size_px, target_hz, camera_id)
    finally:
        camera.stop()

    video = camera.probe()
    if camera.epoch_unix_ns is None:
        raise RuntimeError(
            "没有捕捉到 mkv 首次变大的时刻，无法把 pts 锚到墙钟。"
            "重采一次；持续失败就把输出目录换到 WSL 本地盘试试。"
        )
    log(
        f"[INFO] {camera_id}（{device_name}）录到 {video.frames} 帧 / "
        f"{video.duration_s:.2f} s / {video.actual_fps:.1f} fps"
    )
    return RecordResult(
        camera_id=camera_id,
        video_path=video.path,
        frames=video.frames,
        fps=video.actual_fps,
        epoch_unix_ns=camera.epoch_unix_ns,
        timestamps_ns=list(video.timestamps_ns),
        samples=samples,
        elapsed_s=elapsed,
    )


# ---------------------------------------------------------------- 解码与统计


@dataclass
class FrameHit:
    index: int
    frame_ts_ns: int
    qr_ts_ns: int
    latency_ms: float


def decode_video(result: RecordResult) -> tuple[list[FrameHit], int, int]:
    """逐帧解码二维码，返回 (命中列表, 总帧数, 解出码的帧数)。"""
    import cv2

    capability = cv2.VideoCapture(str(result.video_path))
    if not capability.isOpened():
        raise RuntimeError(f"打不开视频：{result.video_path}")
    reported = int(capability.get(cv2.CAP_PROP_FRAME_COUNT))
    if reported and reported != result.frames:
        warn(
            f"{result.camera_id} 容器报 {reported} 帧，ffprobe 读到 {result.frames} 帧；"
            "按较少的一边截断，避免时间戳与画面错位"
        )

    detector = cv2.QRCodeDetector()
    hits: list[FrameHit] = []
    seen: set[int] = set()
    total = 0
    decoded = 0
    limit = min(result.frames, reported) if reported else result.frames

    while total < limit:
        ok, frame = capability.read()
        if not ok:
            break
        frame_ts = result.timestamps_ns[total]
        text, _points, _straight = detector.detectAndDecode(frame)
        if text:
            decoded += 1
            qr_ts = parse_qr_time(text)
            if qr_ts is not None and qr_ts not in seen:
                # 同一个码被连续几帧拍到只算一次：第一次出现的那帧离上屏最近，
                # 受「停留时间」污染最小。重复帧放进来只会把分布拉宽。
                seen.add(qr_ts)
                latency = (frame_ts - qr_ts) / 1e6
                low, high = PLAUSIBLE_LATENCY_MS
                if low <= latency <= high:
                    hits.append(FrameHit(total, frame_ts, qr_ts, latency))
        total += 1

    capability.release()
    return hits, total, decoded


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


def verdict(stats: dict, detection_rate: float, samples_shown: int) -> list[str]:
    """把统计翻译成人话：哪些数能用、哪些要重测。"""
    lines: list[str] = []
    n = stats.get("samples", 0)
    if n < MIN_SAMPLES:
        lines.append(
            f"✗ 有效样本只有 {n} 个（要 ≥ {MIN_SAMPLES}）：二维码没被稳定拍到。"
            "把窗口放大、相机靠近、调好焦距后重测"
        )
        return lines
    if n < COMFORTABLE_SAMPLES:
        lines.append(f"△ 样本 {n} 个，够用但偏少；中位数误差约 ±{stats['iqr_ms'] / 2:.0f} ms")
    else:
        lines.append(f"✓ 样本充足（{n} 个）")

    iqr = stats["iqr_ms"]
    if iqr <= 20:
        lines.append(f"✓ 抖动小（IQR {iqr:.1f} ms），分布集中，结果可直接用")
    elif iqr <= 50:
        lines.append(f"△ 抖动中等（IQR {iqr:.1f} ms），建议重测一次看是否稳定")
    else:
        lines.append(
            f"✗ 抖动大（IQR {iqr:.1f} ms）：通常是 CPU 抢占或屏幕刷新不同步，"
            "关掉其他程序再测"
        )

    if detection_rate < 0.3:
        lines.append(
            f"△ 解码率只有 {detection_rate * 100:.1f}%：二维码在画面里太小或失焦，"
            "样本可能有偏"
        )
    else:
        lines.append(f"✓ 解码率 {detection_rate * 100:.1f}%")

    if samples_shown:
        lines.append(
            f"· 本次共刷出 {samples_shown} 个不同二维码，"
            f"被拍到 {stats['samples']} 个（{stats['samples'] / samples_shown * 100:.1f}%）"
        )
    return lines


def write_outputs(
    output_dir: Path,
    result: RecordResult,
    hits: list[FrameHit],
    stats: dict,
    meta: dict,
) -> dict:
    """落盘：逐帧明细、汇总 JSON、直方图。"""
    frame_csv = output_dir / f"{result.camera_id}_frames.csv"
    with frame_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["frame_index", "frame_time_unix_ns", "qr_time_unix_ns", "latency_ms"])
        for hit in hits:
            writer.writerow([hit.index, hit.frame_ts_ns, hit.qr_ts_ns, f"{hit.latency_ms:.3f}"])

    show_csv = output_dir / f"{result.camera_id}_screen.csv"
    with show_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["seq", "t_gen_unix_ns", "encode_ms", "imshow_ms"])
        for sample in result.samples:
            writer.writerow(
                [sample.seq, sample.t_gen_ns, f"{sample.encode_ms:.3f}", f"{sample.imshow_ms:.3f}"]
            )

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "camera": {
            "id": result.camera_id,
            "device": meta.get("device_name"),
            "backend": meta.get("backend"),
            "width": meta.get("width"),
            "height": meta.get("height"),
            "fps_target": meta.get("fps"),
            "codec": meta.get("codec"),
            "frames": result.frames,
            "actual_fps": round(result.fps, 3),
            "epoch_unix_ns": result.epoch_unix_ns,
            "recorded_s": round(result.elapsed_s, 3),
        },
        "screen": {
            "qr_count": len(result.samples),
            "refresh_hz": round(len(result.samples) / result.elapsed_s, 2)
            if result.elapsed_s > 0
            else 0.0,
            "encode_ms_mean": round(
                statistics.fmean([s.encode_ms for s in result.samples]), 3
            )
            if result.samples
            else 0.0,
            "imshow_ms_mean": round(
                statistics.fmean([s.imshow_ms for s in result.samples]), 3
            )
            if result.samples
            else 0.0,
        },
        "latency": stats,
        "correction": {
            "display_latency_ms_assumed": meta.get("display_latency_ms"),
            "median_minus_display_ms": round(
                stats.get("median_ms", 0.0) - meta.get("display_latency_ms", 0.0), 2
            )
            if stats.get("samples")
            else None,
            "note": "从 timestamp_unix_ns 里减掉这个值，时间轴就回到接近曝光时刻",
        },
    }
    json_path = output_dir / f"{result.camera_id}_summary.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    plot_path = output_dir / f"{result.camera_id}_hist.png"
    if not hits:
        # 没有样本就没有分布可画，跳过而不是让整轮测量报错。
        return {
            "frame_csv": frame_csv,
            "screen_csv": show_csv,
            "json": json_path,
            "plot": None,
        }
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
        figure.savefig(plot_path, dpi=130)
        plt.close(figure)
    except Exception as exc:  # 画图失败不该让测量白跑
        warn(f"直方图没画出来：{exc}")
        plot_path = None

    return {
        "frame_csv": frame_csv,
        "screen_csv": show_csv,
        "json": json_path,
        "plot": plot_path,
    }


# ---------------------------------------------------------------- 命令行


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="测量相机（Windows/DirectShow 采集路径）的端到端延迟",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python3.10 measure_camera_latency.py --list\n"
            "  python3.10 measure_camera_latency.py --camera 0 --seconds 15\n"
            "  python3.10 measure_camera_latency.py --camera 0 --seconds 20 --qr-size 1000\n"
        ),
    )
    parser.add_argument("--list", action="store_true", help="只枚举相机，不测量")
    parser.add_argument("--camera", type=int, default=0, help="第几路相机，默认 0")
    parser.add_argument("--seconds", type=float, default=15.0, help="录制时长，默认 15 秒")
    parser.add_argument("--qr-size", type=int, default=800, help="二维码窗口边长（像素），默认 800")
    parser.add_argument("--qr-hz", type=float, default=100.0, help="目标刷新率，默认 100 Hz")
    parser.add_argument(
        "--display-latency-ms",
        type=float,
        default=0.0,
        help="手动扣除的显示链路延迟（毫秒）；只影响 summary 里的修正值，不改原始统计",
    )
    parser.add_argument("--output-dir", type=Path, default=None, help="测量输出目录")
    parser.add_argument("--ffmpeg", default=None, help="Linux ffmpeg（用于 ffprobe），默认查 PATH")
    parser.add_argument("--ffmpeg-win", default=None, help="Windows ffmpeg.exe 路径")
    parser.add_argument("--camera-backend", default=None, choices=CAMERA_BACKENDS,
                        help="wsl = Windows DirectShow 采集；linux = /dev/videoN")
    parser.add_argument("--width", type=int, default=None, help="默认取 cap_config.json")
    parser.add_argument("--height", type=int, default=None, help="默认取 cap_config.json")
    parser.add_argument("--fps", type=float, default=None, help="默认取 cap_config.json")
    parser.add_argument("--video-codec", default=None, choices=VIDEO_CODECS,
                        help="默认取 cap_config.json，通常 copy")
    parser.add_argument("--device", default=None,
                        help="直接指定 -i 参数（形如 video=@device_pnp_... 或 /dev/video0）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_cap_config()

    backend = pick(args.camera_backend, config.get("camera_backend", "wsl"))
    width = pick(args.width, config.get("camera_width", 1280))
    height = pick(args.height, config.get("camera_height", 720))
    fps = pick(args.fps, config.get("camera_fps", 30))
    codec = pick(args.video_codec, config.get("video_codec", "copy"))

    try:
        ffmpeg_exe = find_ffmpeg(args.ffmpeg)
        windows_ffmpeg = (
            find_windows_ffmpeg(args.ffmpeg_win) if backend == "wsl" else None
        )
    except RuntimeError as exc:
        warn(str(exc))
        return 2

    try:
        devices = resolve_devices(backend, args.camera, windows_ffmpeg or ffmpeg_exe)
    except RuntimeError as exc:
        warn(str(exc))
        return 2

    if args.list:
        print(f"后端      {backend}")
        print(f"ffmpeg    {ffmpeg_exe}  ({ffmpeg_version(ffmpeg_exe)})")
        if windows_ffmpeg:
            print(f"ffmpeg.exe {windows_ffmpeg}")
        print(f"采集参数  {width}x{height}@{fps:g}  codec={codec}")
        print("相机：")
        for index, (name, device_input) in enumerate(devices):
            print(f"  [{index}] {name}")
            print(f"      {device_input}")
        return 0

    if not devices:
        warn("没有枚举到任何相机，检查 USB 连接与 --camera-backend")
        return 2
    if args.camera >= len(devices):
        warn(f"--camera {args.camera} 超出范围，当前只有 {len(devices)} 路")
        return 2

    device_name, device_input = devices[args.camera]
    if args.device:
        device_input = args.device

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = (
        args.output_dir
        if args.output_dir
        else DEFAULT_OUTPUT_ROOT / f"{stamp}_cam{args.camera}"
    )
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 62)
    print("相机延迟测量")
    print("=" * 62)
    print(f"相机       [{args.camera}] {device_name}")
    print(f"后端       {backend}")
    print(f"采集参数   {width}x{height}@{fps:g}  codec={codec}")
    print(f"录多久     {args.seconds:g} 秒")
    print(f"二维码     {args.qr_size} px / 目标 {args.qr_hz:g} Hz")
    print(f"输出       {output_dir}")
    print("-" * 62)
    print("把二维码窗口对着相机，占画面四分之一以上；跑完按提示看结果。")
    print("=" * 62)
    time.sleep(1.0)

    try:
        result = run_camera(
            args.camera,
            device_name,
            device_input,
            output_dir,
            backend,
            ffmpeg_exe,
            windows_ffmpeg,
            width,
            height,
            fps,
            codec,
            args.seconds,
            args.qr_size,
            args.qr_hz,
        )
    except (RuntimeError, OSError) as exc:
        warn(f"采集失败：{exc}")
        return 3

    print("-" * 62)
    log("[INFO] 开始逐帧解码二维码，这一段没有进度条，耐心等")
    try:
        hits, total, decoded = decode_video(result)
    except RuntimeError as exc:
        warn(f"解码失败：{exc}")
        return 4

    stats = summarize(hits)
    detection_rate = decoded / total if total else 0.0
    meta = {
        "device_name": device_name,
        "backend": backend,
        "width": width,
        "height": height,
        "fps": fps,
        "codec": codec,
        "display_latency_ms": args.display_latency_ms,
    }
    paths = write_outputs(output_dir, result, hits, stats, meta)

    print("=" * 62)
    print("结果")
    print("=" * 62)
    print(f"录制       {result.frames} 帧 / {result.elapsed_s:.1f} s / {result.fps:.1f} fps")
    refresh = len(result.samples) / result.elapsed_s if result.elapsed_s > 0 else 0.0
    print(f"屏幕       刷出 {len(result.samples)} 个二维码，实际 {refresh:.1f} Hz")
    print(f"解码       {decoded} / {total} 帧解出码（{detection_rate * 100:.1f}%）")
    print(f"有效样本   {stats.get('samples', 0)}")
    print()

    if stats.get("samples"):
        print("Δ = 帧时间戳 − 拍摄时刻（含显示链路）")
        print(f"  中位数    {stats['median_ms']:8.2f} ms   ← 建议填这个")
        print(f"  均值      {stats['mean_ms']:8.2f} ms")
        print(f"  标准差    {stats['std_ms']:8.2f} ms")
        print(f"  P5 / P95  {stats['p05_ms']:8.2f} / {stats['p95_ms']:.2f} ms")
        print(f"  最小/最大 {stats['min_ms']:8.2f} / {stats['max_ms']:.2f} ms")
        print(f"  IQR       {stats['iqr_ms']:8.2f} ms")
        print()
        encode_mean = statistics.fmean([s.encode_ms for s in result.samples]) if result.samples else 0.0
        imshow_mean = statistics.fmean([s.imshow_ms for s in result.samples]) if result.samples else 0.0
        print("显示链路可观测开销（下界，实际还包含合成与屏幕刷新等待）")
        print(f"  QR 编码+转图  {encode_mean:5.2f} ms")
        print(f"  imshow+事件   {imshow_mean:5.2f} ms")
        print()
        if args.display_latency_ms:
            corrected = stats["median_ms"] - args.display_latency_ms
            print(f"扣掉 {args.display_latency_ms:g} ms 显示链路后：{corrected:.2f} ms")
            print()
        print("判读")
        for line in verdict(stats, detection_rate, len(result.samples)):
            print(f"  {line}")
    else:
        print("没有有效样本。常见原因：")
        print("  · 二维码没进画面 —— 把窗口拖到相机正前方，放大到画面 1/4 以上")
        print("  · 失焦或过曝 —— 手动对焦，别让屏幕反光糊成一片")
        print("  · 分辨率/帧率不被设备支持 —— 先跑 --list 看枚举结果")

    print()
    print("输出文件")
    print(f"  逐帧明细   {paths['frame_csv']}")
    print(f"  屏幕刷新   {paths['screen_csv']}")
    print(f"  汇总       {paths['json']}")
    if paths["plot"]:
        print(f"  直方图     {paths['plot']}")
    print("=" * 62)
    print("怎么用这个数：")
    print("  timestamp_corrected = timestamp_unix_ns − 中位数（纳秒）")
    print("  真实世界场景再扣掉显示链路那一段（--display-latency-ms）")
    return 0 if stats.get("samples", 0) >= MIN_SAMPLES else 5


if __name__ == "__main__":
    sys.exit(main())
