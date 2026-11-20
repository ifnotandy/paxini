#!/usr/bin/env python3.10
"""measure_camera_latency.py —— 测量相机采集链路的延迟（原生 Ubuntu / V4L2）

相机由 ffmpeg 直读 ``/dev/videoN``（V4L2），Δ 与设备、分辨率、帧率、codec、USB
口都有关，换任何一种都要重测。

问题
----
采集端写进 HDF5 的相机时间戳，含义是「ffmpeg 从驱动队列取到这一帧的时刻」，
不是「这一帧被拍下的时刻」。两者之间隔着：

    曝光 + 传感器读出 + MJPEG 编码 + USB 传输 + 驱动缓冲

这段差值就是本程序要测的 Δ。拿到之后下游减掉它，时间轴才回到接近曝光时刻。

时间戳从哪来
------------
本脚本用 ``-use_wallclock_as_timestamps 1``，ffmpeg 在出帧时直接写 ``av_gettime()``，
所以 **mkv 里的 pts 本身就是绝对墙钟**，不需要任何锚点：

    timestamp_unix_ns[i] = pts[i]

Δ 通常几十毫秒。单轮就够；需要更稳的数时用 ``--repeat`` 合并多轮。

做法
----
1. 屏幕上以最高速刷新二维码，内容是生成那一刻的 ``time.time_ns()``。
   QR 时刻和 pts 都取自同一台机器的 ``CLOCK_REALTIME``，差值里没有时钟域问题。
2. 用 ``camera_ffmpeg.FfmpegCamera`` 录一段 mkv，参数与 collect_cap.py 的
   linux 分支完全一致 —— 只有同一条链路测出来的数才能拿去修采集数据。
3. 停止后 ffprobe 取每帧 pts（已经是墙钟纳秒），并校验首帧落在合理年份区间。
4. 逐帧解码二维码，得到该帧画面上那份内容的生成时刻。
5. Δ = 帧时刻 − 码内时刻，逐帧落盘，输出中位数。

测出来的 Δ 包含两段
------------------
- **相机链路**：曝光 + MJPEG 编码 + USB 传输 + 驱动缓冲 + ffmpeg 取帧
- **显示链路**：窗口合成 + 屏幕扫描输出 + 刷新等待

真实场景（相机拍实物）不需要显示那一段，所以填参数时可以用
``--display-latency-ms`` 扣掉它。显示段本程序测不到绝对值，只给可观测下界
（QR 编码耗时、imshow 耗时）；屏幕刷新等待的理论均值是半个刷新周期，
60 Hz 屏约 8.3 ms。

用法
----
    cd <repo>/Paxini-Cap/Cap_Quest
    export PATH="$HOME/bin:$PATH"          # 静态 ffmpeg/ffprobe
    sudo timedatectl set-ntp false          # 测前关 NTP，见下方注意
    python3.10 latency/measure_camera_latency.py --list
    python3.10 latency/measure_camera_latency.py            # 两路各 20 秒

测量结论自动追加进 ``latency/records/<电脑用户>-ubuntu.yaml``，下游读最近的合格值。

把二维码窗口拖到相机正前方，占画面四分之一以上；按 q 可提前结束。

注意：pts 来自 ``CLOCK_REALTIME``，测量期间若 NTP 步进一次系统时间，那段时间
的样本会整体偏掉。关掉 NTP 再测；测完记得 ``sudo timedatectl set-ntp true``。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
import yaml

# 采集模块在 ../capture：延时测量单独成目录后要显式补搜索路径
_CAPTURE_DIR = Path(__file__).resolve().parent.parent / "capture"
if str(_CAPTURE_DIR) not in sys.path:
    sys.path.insert(0, str(_CAPTURE_DIR))

from camera_ffmpeg import (
    VIDEO_CODECS,
    FfmpegCamera,
    discover_video_nodes,
    ffmpeg_version,
    find_ffmpeg,
    log,
    warn,
)
from latency_records import DELTA_WARN_MS, record_measurement

HERE = Path(__file__).resolve().parent
# 配置与输出都放在 Cap_Quest 根目录，和采集程序共用同一份 cap_config.yaml
QUEST_ROOT = HERE.parent
CONFIG_PATH = QUEST_ROOT / "cap_config.yaml"
DEFAULT_OUTPUT_ROOT = QUEST_ROOT / "latency_measure"

# 采集只接两路外接相机（cam0 / cam1）；电脑自带摄像头不参与采集，不测。
CAPTURE_SLOTS = 2
# 一次测量至少要有这么多有效样本，否则中位数不可信。
MIN_SAMPLES = 30
# 有效样本低于这个数就提示调整二维码大小/距离。
COMFORTABLE_SAMPLES = 100
# 二维码内容合法长度：19 位纳秒整数（约 1.79e18）。
QR_TEXT_MIN_LEN = 18
QR_TEXT_MAX_LEN = 20
# 延迟落在窗口外的样本视为解码错误或时钟异常，直接丢弃。
# 下界放到 -50 ms：Linux 上真实值可能低到 20~30 ms，抖动会更早碰线。
PLAUSIBLE_LATENCY_MS = (-50.0, 3000.0)
# 前后半段中位数差超过这个值就认为测量期间环境变了（NTP 步进、CPU 抢占）。
DRIFT_WARN_MS = 10.0


# ---------------------------------------------------------------- 配置与设备


def load_cap_config() -> dict:
    """读 cap_config.yaml；解析失败就退回内置默认值，让测量能继续。"""
    if not CONFIG_PATH.is_file():
        return {}
    try:
        raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        warn(f"读取 {CONFIG_PATH.name} 失败，改用内置默认值：{exc}")
        return {}
    return {str(key): value for key, value in raw.items() if not str(key).startswith("_")}


def pick(value, fallback):
    """命令行没给（None）就用配置值，配置也没有就用内置默认。"""
    return fallback if value is None else value


def node_label(node: str) -> str:
    """给 /dev/videoN 配一个可读名字，方便对上物理相机。"""
    name_path = Path("/sys/class/video4linux") / Path(node).name / "name"
    try:
        card = name_path.read_text(encoding="utf-8").strip()
    except OSError:
        card = ""
    return f"{node}  {card}".rstrip()


def resolve_devices() -> list[tuple[str, str]]:
    """返回 [(显示名, -i 参数)]，顺序即相机编号顺序。

    只看 index==0 的采集节点，UVC 设备旁边那个 metadata 节点读不出画面。
    """
    return [(node_label(node), node) for node in discover_video_nodes()]


def check_display() -> None:
    """没有图形会话时提前报错，别等到 imshow 才崩在 cv2 里。"""
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        return
    raise RuntimeError(
        "当前会话既没有 DISPLAY 也没有 WAYLAND_DISPLAY，开不了二维码窗口。"
        "本程序必须在本机桌面上跑（不能用 ssh 直接跑，也不能纯 tty）"
    )


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
    timestamps_ns: list[int]
    samples: list[ShowSample] = field(default_factory=list)
    elapsed_s: float = 0.0

    @property
    def first_ts_ns(self) -> int:
        return self.timestamps_ns[0] if self.timestamps_ns else 0


def record_with_qr(
    camera: FfmpegCamera,
    seconds: float,
    size_px: int,
    target_hz: float,
    window: str,
) -> tuple[list[ShowSample], float]:
    """开窗刷码，同时让相机录 seconds 秒。

    返回 (屏幕上刷过的所有二维码, 实际刷码时长)。

    cv2 的窗口操作全部留在主线程：Qt/GTK 后端在子线程里 imshow 会偶发段错误。
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
    last_progress = 0
    ready_streak = 0
    while time.monotonic() < deadline_wait:
        progress = camera.progress_signal()
        if progress > last_progress:
            last_progress = progress
            ready_streak += 1
            if ready_streak >= 3:
                break
        # 等相机的这段时间也要让窗口重绘，否则窗口显示成空白/未响应。
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
    ffmpeg_exe: str,
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
        backend="linux",
        preview_fps=0.0,  # 不要预览流：第二个输出会改变 ffmpeg 的写出节奏
    )
    camera.open()
    camera.start()
    try:
        samples, elapsed = record_with_qr(camera, seconds, size_px, target_hz, camera_id)
    finally:
        camera.stop()

    # probe() 在原生 Ubuntu 路径下直接返回墙钟 pts，并自带「首帧是否像墙钟」的校验。
    video = camera.probe()
    # 再加一道时间合理区间检查：pts 若是设备自己的计数器，会落在 1970 年附近。
    first_year = datetime.fromtimestamp(video.timestamps_ns[0] / 1e9).year
    if not 2020 <= first_year <= 2100:
        raise RuntimeError(
            f"首帧时间戳对应 {first_year} 年，明显不是当前墙钟。"
            "pts 不是 av_gettime()，请核对 ffmpeg 命令里的 -use_wallclock_as_timestamps 1"
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
    # 前后半段分开算中位数：两者差得多，说明测量期间环境变了（NTP 步进、
    # CPU 抢占、相机自动曝光在调整），这时整个数都不能用。
    half = values.size // 2
    drift = float(np.median(values[half:]) - np.median(values[:half])) if half else 0.0
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
        "drift_ms": drift,
    }


def verdict(stats: dict, detection_rate: float, samples_shown: int) -> list[str]:
    """把统计翻译成人话：哪些数能用、哪些要重测。"""
    lines: list[str] = []
    n = stats.get("samples", 0)
    if n < MIN_SAMPLES:
        lines.append(
            f"x 有效样本只有 {n} 个（要 >= {MIN_SAMPLES}）：二维码没被稳定拍到。"
            "把窗口放大、相机靠近、调好焦距后重测"
        )
        return lines
    if n < COMFORTABLE_SAMPLES:
        lines.append(f"~ 样本 {n} 个，够用但偏少；中位数误差约 +/-{stats['iqr_ms'] / 2:.0f} ms")
    else:
        lines.append(f"OK 样本充足（{n} 个）")

    iqr = stats["iqr_ms"]
    if iqr <= 15:
        lines.append(f"OK 抖动小（IQR {iqr:.1f} ms），分布集中，结果可直接用")
    elif iqr <= 40:
        lines.append(f"~ 抖动中等（IQR {iqr:.1f} ms），建议重测一次看是否稳定")
    else:
        lines.append(
            f"x 抖动大（IQR {iqr:.1f} ms）：通常是 CPU 抢占或自动曝光在变化，"
            "关掉其他程序、锁死曝光后再测"
        )

    drift = abs(stats.get("drift_ms", 0.0))
    if drift > DRIFT_WARN_MS:
        lines.append(
            f"x 前后半段中位数差 {drift:.1f} ms（阈值 {DRIFT_WARN_MS:g} ms）："
            "测量期间系统时钟被步进过，或相机在自动调整。关 NTP（timedatectl set-ntp false）"
            "并锁曝光后重测"
        )
    else:
        lines.append(f"OK 前后半段一致（差 {drift:.1f} ms），时间轴没有漂移")

    if detection_rate < 0.3:
        lines.append(
            f"~ 解码率只有 {detection_rate * 100:.1f}%：二维码在画面里太小或失焦，"
            "样本可能有偏"
        )
    else:
        lines.append(f"OK 解码率 {detection_rate * 100:.1f}%")

    if samples_shown:
        lines.append(
            f"- 本次共刷出 {samples_shown} 个不同二维码，"
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
            "backend": "linux",
            "width": meta.get("width"),
            "height": meta.get("height"),
            "fps_target": meta.get("fps"),
            "codec": meta.get("codec"),
            "frames": result.frames,
            "actual_fps": round(result.fps, 3),
            "first_frame_unix_ns": result.first_ts_ns,
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
        description="测量相机延迟（原生 Ubuntu / V4L2 采集路径）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python3.10 latency/measure_camera_latency.py --list\n"
            "  python3.10 latency/measure_camera_latency.py               # 两路都测，各 20 秒\n"
            "  python3.10 latency/measure_camera_latency.py --camera 1 --seconds 20 --qr-size 1000\n"
            "  python3.10 latency/measure_camera_latency.py --camera 0 --device /dev/video4\n"
        ),
    )
    parser.add_argument("--list", action="store_true", help="只枚举相机，不测量")
    parser.add_argument(
        "--camera",
        type=int,
        default=None,
        help="只测第几路；不填则测 cam0 和 cam1（两路外接相机，默认都测）",
    )
    parser.add_argument("--seconds", type=float, default=20.0, help="录制时长，默认 20 秒")
    parser.add_argument("--qr-size", type=int, default=800, help="二维码窗口边长（像素），默认 800")
    parser.add_argument("--qr-hz", type=float, default=100.0, help="目标刷新率，默认 100 Hz")
    parser.add_argument(
        "--display-latency-ms",
        type=float,
        default=0.0,
        help="手动扣除的显示链路延迟（毫秒）；只影响 summary 里的修正值，不改原始统计",
    )
    parser.add_argument("--output-dir", type=Path, default=None, help="测量输出目录")
    parser.add_argument("--ffmpeg", default=None, help="ffmpeg 路径，默认查 PATH / $FFMPEG")
    parser.add_argument("--width", type=int, default=None, help="默认取 cap_config.yaml")
    parser.add_argument("--height", type=int, default=None, help="默认取 cap_config.yaml")
    parser.add_argument("--fps", type=float, default=None, help="默认取 cap_config.yaml")
    parser.add_argument("--video-codec", default=None, choices=VIDEO_CODECS,
                        help="默认取 cap_config.yaml，通常 copy")
    parser.add_argument("--device", default=None,
                        help="直接指定采集节点，形如 /dev/video4")

    group = parser.add_argument_group("测量记录（写 latency/records/<机器>.yaml）")
    group.add_argument("--machine", default=None,
                       help="台账里的机器标识，默认 <用户名>-ubuntu")
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
    """测一路相机：录制 → 解码 → 落盘 → 记账，返回这一路的结论。

    逐路独立起停：某一路失败不影响另一路出结果，最后按最严重的那一路给退出码。
    """
    if args.output_dir:
        # 多路时每路一个子目录，否则两路的产物会混在一起
        output_dir = args.output_dir / f"cam{index}" if multi else args.output_dir
    else:
        output_dir = DEFAULT_OUTPUT_ROOT / f"{stamp}_cam{index}"
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 62)
    print(f"相机延迟测量（Linux / V4L2）第 {index + 1} 路")
    print("=" * 62)
    print(f"相机       [{index}] {device_name}")
    print(f"采集参数   {width}x{height}@{fps:g}  codec={codec}")
    print(f"录多久     {args.seconds:g} 秒")
    print(f"二维码     {args.qr_size} px / 目标 {args.qr_hz:g} Hz")
    print(f"输出       {output_dir}")
    print("-" * 62)
    print("把二维码窗口拖到相机正前方，占画面四分之一以上。")
    print("=" * 62)
    time.sleep(1.0)

    try:
        result = run_camera(
            index,
            device_name,
            device_input,
            output_dir,
            ffmpeg_exe,
            width,
            height,
            fps,
            codec,
            args.seconds,
            args.qr_size,
            args.qr_hz,
        )
    except (RuntimeError, OSError) as exc:
        warn(f"第 {index} 路采集失败：{exc}")
        return {"camera": index, "stats": {"samples": 0}, "code": 3}

    print("-" * 62)
    log("[INFO] 开始逐帧解码二维码，这一段没有进度条，耐心等")
    try:
        hits, total, decoded = decode_video(result)
    except RuntimeError as exc:
        warn(f"第 {index} 路解码失败：{exc}")
        return {"camera": index, "stats": {"samples": 0}, "code": 4}

    stats = summarize(hits)
    detection_rate = decoded / total if total else 0.0
    meta = {
        "device_name": device_name,
        "width": width,
        "height": height,
        "fps": fps,
        "codec": codec,
        "display_latency_ms": args.display_latency_ms,
    }
    paths = write_outputs(output_dir, result, hits, stats, meta)

    # 把这次结论追加进台账：下游按机器读一个固定文件，不用翻时间戳目录。
    recorded = None
    if not args.no_record:
        try:
            recorded = record_measurement(
                platform="ubuntu",
                camera=index,
                stats=stats,
                meta={**meta, "seconds": args.seconds},
                output_dir=output_dir,
                record_dir=args.record_dir,
                machine=args.machine,
                detection_rate=detection_rate,
                note=args.note,
                clock_domain="CLOCK_REALTIME（V4L2 墙钟 pts，无 epoch 锚点）",
            )
        except (OSError, RuntimeError) as exc:
            warn(f"第 {index} 路测量记录没写成功，本次产物不受影响：{exc}")

    print("=" * 62)
    print(f"第 {index} 路结果")
    print("=" * 62)
    print(f"录制       {result.frames} 帧 / {result.elapsed_s:.1f} s / {result.fps:.1f} fps")
    refresh = len(result.samples) / result.elapsed_s if result.elapsed_s > 0 else 0.0
    print(f"屏幕       刷出 {len(result.samples)} 个二维码，实际 {refresh:.1f} Hz")
    print(f"解码       {decoded} / {total} 帧（{detection_rate * 100:.1f}%）")
    print(f"有效样本   {stats.get('samples', 0)}")
    print(f"首帧时刻   {datetime.fromtimestamp(result.first_ts_ns / 1e9).isoformat(timespec='milliseconds')}"
          "（来自 V4L2 墙钟 pts，无需锚点）")
    print("-" * 62)

    if stats.get("samples"):
        print("Δ = 帧时间戳 - 拍摄时刻（含显示链路）")
        print(f"  中位数    {stats['median_ms']:8.2f} ms   <- 建议填这个")
        print(f"  均值      {stats['mean_ms']:8.2f} ms")
        print(f"  标准差    {stats['std_ms']:8.2f} ms")
        print(f"  P5 / P95  {stats['p05_ms']:8.2f} / {stats['p95_ms']:.2f} ms")
        print(f"  最小/最大 {stats['min_ms']:8.2f} / {stats['max_ms']:.2f} ms")
        print(f"  IQR       {stats['iqr_ms']:8.2f} ms")
        print(f"  前后漂移  {stats['drift_ms']:8.2f} ms")
        print("-" * 62)
        if result.samples:
            print("显示链路可观测开销（下界）")
            print(f"  QR 编码+转图  {statistics.fmean([s.encode_ms for s in result.samples]):5.2f} ms")
            print(f"  imshow+事件   {statistics.fmean([s.imshow_ms for s in result.samples]):5.2f} ms")
        if args.display_latency_ms:
            print(f"扣掉 {args.display_latency_ms:g} ms 显示链路后："
                  f"{stats['median_ms'] - args.display_latency_ms:.2f} ms")
        print("-" * 62)
        print("判读")
        for line in verdict(stats, detection_rate, len(result.samples)):
            print(f"  {line}")
    else:
        print("没有有效样本。常见原因：")
        print("  - 二维码没进画面：窗口拖到相机正前方，放大到画面 1/4 以上")
        print("  - 失焦或过曝：手动对焦，避开屏幕反光")
        print("  - 相机权限：当前用户要在 video 组里（id -nG | grep video）")
        if not result.samples:
            print("  - 一个二维码都没刷出：窗口开不起来，检查 DISPLAY")

    print()
    print("输出文件")
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
            warn(
                f"cam{index} 与上一条合格记录差 {delta:+.1f} ms"
                f"（{before:.2f} → {entry['median_ms']:.2f}）。环境没变的话重测一次确认："
                "驱动缓冲、自动曝光、换 USB 口都会让整条时间轴整体平移"
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

    # 采集参数必须和 collect_cap.py 的 linux 分支一致，否则测的不是同一条链路。
    width = pick(args.width, config.get("camera_width", 1280))
    height = pick(args.height, config.get("camera_height", 720))
    fps = pick(args.fps, config.get("camera_fps", 30))
    codec = pick(args.video_codec, config.get("video_codec", "copy"))

    try:
        ffmpeg_exe = find_ffmpeg(args.ffmpeg)
    except RuntimeError as exc:
        warn(str(exc))
        return 2

    devices = resolve_devices()

    if args.list:
        print("采集路径  V4L2（-use_wallclock_as_timestamps 1）")
        print(f"ffmpeg    {ffmpeg_exe}  ({ffmpeg_version(ffmpeg_exe)})")
        print(f"采集参数  {width}x{height}@{fps:g}  codec={codec}")
        print("相机：")
        if not devices:
            print("  （没有 /sys/class/video4linux 节点：先确认相机已插上、"
                  "用户已加入 video 组）")
        for index, (name, device_input) in enumerate(devices):
            print(f"  [{index}] {name}")
            print(f"      {device_input}")
        if devices:
            print("默认      测 cam0 和 cam1（前两路外接相机）；只想测一路用 --camera N")
        return 0

    if not devices:
        warn(
            "没有枚举到任何相机。检查：① 相机是否插上（ls /dev/video*）"
            "② 当前用户是否在 video 组（id -nG）③ 虚拟机/容器里是否把 USB 直通了"
        )
        return 2

    # 只测采集在用的那两路（cam0 / cam1）。电脑自带的摄像头即使被枚举到也不测：
    # 它不参与采集，测出来的数没有用处，还白占一段录制时间。
    if args.camera is None:
        targets = list(range(min(CAPTURE_SLOTS, len(devices))))
        if len(devices) > CAPTURE_SLOTS:
            log(f"[INFO] 枚举到 {len(devices)} 路，只测前两路 cam0/cam1；"
                f"要测别的用 --camera N")
    else:
        if args.camera < 0 or args.camera >= len(devices):
            warn(f"--camera {args.camera} 超出范围，当前只有 {len(devices)} 路")
            return 2
        targets = [args.camera]

    if args.device and len(targets) > 1:
        warn("--device 只能配合 --camera 单路使用；本次忽略它，按枚举到的相机逐路测")

    try:
        check_display()
    except RuntimeError as exc:
        warn(str(exc))
        return 2

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    multi = len(targets) > 1
    if multi:
        print(f"本次逐路测量 {len(targets)} 路相机：" + "、".join(f"cam{i}" for i in targets))

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
    print("  timestamp_unix_ns -= 中位数(ms) * 1e6      # 原生 Ubuntu 只有这一列")
    print("  （linux 的 pts 已是墙钟，没有 timestamp_monotonic_ns 这一列）")
    print("  之后再用就甭重测了，直接从 latency/records/ 读最近一次合格值")
    codes = [item["code"] for item in results]
    for bad in (4, 3, 5):
        if bad in codes:
            return bad
    return 0


if __name__ == "__main__":
    sys.exit(main())
