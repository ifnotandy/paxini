#!/usr/bin/env python3
"""PxCap III 手套标定与维护（非相机部分）。

四个命令，全部围绕 SDK 的三个写接口：

- ``info``    只读：SN / 固件 / 故障 / 4 路编码器与 10 路合力现状
- ``encoder`` 磁编码器标零（``set_encoder_calibration``）
- ``sensor``  传感器标零（``set_sensor_calibration``）
- ``magnet``  静态磁检测（``set_static_magnet_check`` + 轮询 ``get_static_magnet_check``）
- ``selftest`` 离线自检：不连设备，只验闸门与统计逻辑
- ``ports``   列出发现的串口

**这些操作写的是设备，不是主机的数**：标零结果落进手套自己的存储，之后
``get_encoder_raw_data()`` 返回的展开编码值就换成新的零点。主机侧不保存偏移量、
不做二次修正，所以换一台电脑、换一个采集程序，零点照样生效；反过来，标错一次
也只能重标，没有"撤销"。

正因为是设备端持久化，写之前必须先挡住两种废标定：

- 手没静止 → 编码器零点带进运动量
- 手上还有外力 / 有人捏着 → 传感器把受力当成"空载零点"

两个闸门都在本程序里做成硬性前置检查，不通过就不写设备（``--force`` 可跳过，
但报告里会记下来）。

相机标定不在这里，见 ``DOC/标定.md`` §2。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, NoReturn, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
# 标定程序本身在 Cap_Quest/calib/ 下：配置在上一层（Cap_Quest 根），
# 报告写在本目录的 hand/，和程序放在一起，不再往 Paxini-Cap 顶层丢。
QUEST_ROOT = SCRIPT_DIR.parent
REPORT_DIR = SCRIPT_DIR / "hand"
DEFAULT_CONFIG_PATH = QUEST_ROOT / "cap_config.yaml"
CAP_USB_ID = "28e9:018a"

# 顺序与 SDK 结构体字段一致，不能改。
ANGLE_FIELDS = (
    "index_proximal",
    "index_distal",
    "middle_proximal",
    "middle_distal",
)
ANGLE_LABELS = {
    "index_proximal": "食指 指腹(FF2)",
    "index_distal": "食指 指尖(FF1)",
    "middle_proximal": "中指 指腹(MF2)",
    "middle_distal": "中指 指尖(MF1)",
}
CAP_SLOTS = (
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
)

# 失败调用的错误码来自 SDK；4000 是"采集中禁止调用"这一类状态互斥。
RC_NOT_CONNECTED_MSG = "请检查手套是否已在采集状态；标定期间不得并发轮询"

FORCE_UNIT = 0.1  # PxCap3ForceVector 单位：0.1 N


def log(message: str) -> None:
    print(message, flush=True)


def warn(message: str) -> None:
    print(f"[警告] {message}", file=sys.stderr, flush=True)


def fail(message: str) -> NoReturn:
    raise SystemExit(f"[错误] {message}")


# ---------------------------------------------------------------------------
# 串口发现：只依赖 /dev 与 sysfs，不引 numpy / h5py
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SerialPort:
    path: str
    by_id: str | None
    usb_id: str | None


def usb_ids_for_tty(name: str, sysfs_root: Path = Path("/sys/class/tty")) -> str | None:
    """Walk sysfs from a tty node up to the USB device's idVendor:idProduct."""
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
        parent = directory.parent
        if parent == directory:
            break
        directory = parent
    return None


def discover_hand_ports(dev_root: Path = Path("/dev")) -> list[SerialPort]:
    """Discover USB/ACM serial ports without relying on tty numbering."""
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


def resolve_hand_port(requested: str, dev_root: Path = Path("/dev")) -> str:
    if requested != "auto":
        path = Path(requested).expanduser()
        if not path.exists():
            fail(f"PXCap 串口不存在：{path}")
        return str(path)

    ports = discover_hand_ports(dev_root)
    if not ports:
        fail(
            "未发现可用串口。请检查手套 USB 线缆、usbipd 转发与 dialout 权限，"
            "或用 --hand-port /dev/ttyACM0 指定"
        )

    matches = [port for port in ports if port.usb_id == CAP_USB_ID]
    if len(matches) == 1:
        log(f"[INFO] 自动发现 PXCap 串口：{matches[0].path}（USB {CAP_USB_ID}）")
        return matches[0].path
    if len(matches) > 1:
        formatted = "\n".join(f"  {port.path}" for port in matches)
        fail(
            f"检测到多个 USB {CAP_USB_ID} 串口，无法判断哪一个是 PXCap：\n"
            f"{formatted}\n请用 --hand-port 指定"
        )
    if len(ports) == 1:
        warn(f"未发现 USB {CAP_USB_ID} 的设备，按唯一串口继续：{ports[0].path}")
        return ports[0].path

    formatted = "\n".join(
        f"  {port.path}（USB {port.usb_id or '未知'}）" for port in ports
    )
    fail(
        f"检测到多个串口，但没有匹配 USB {CAP_USB_ID} 的设备：\n"
        f"{formatted}\n请用 --hand-port 指定"
    )


# ---------------------------------------------------------------------------
# SDK 会话
# ---------------------------------------------------------------------------


def import_sdk() -> Any:
    try:
        import pxhandsdk
    except ImportError:
        fail(
            "找不到 pxhandsdk。请在装有 SDK 的系统 python3.10 里运行"
            "（python3.12 import 能过但类不存在）"
        )
    if not hasattr(pxhandsdk, "PxCap3"):
        fail(
            "当前解释器加载不到 PxCap3 绑定：系统 python3 是 3.12 时 import 能过"
            "但类不存在。请一律用 python3.10 运行"
        )
    return pxhandsdk


def force_tuple(vector: Any) -> tuple[int, int, int]:
    return (int(vector.x), int(vector.y), int(vector.z))


def angles_dict(angles: Any) -> dict[str, float]:
    return {name: float(getattr(angles, name)) for name in ANGLE_FIELDS}


def raw_dict(raw: Any) -> dict[str, int]:
    return {name: int(getattr(raw, name)) for name in ANGLE_FIELDS}


def forces_dict(resultant: Any) -> dict[str, tuple[int, int, int]]:
    return {slot: force_tuple(getattr(resultant, slot)) for slot in CAP_SLOTS}


class HandSession:
    """Connect, read identity, and expose the calibration writes."""

    def __init__(self, port: str) -> None:
        self.pxhandsdk = import_sdk()
        self.port = port
        self.hand = self.pxhandsdk.PxCap3()
        self.sn: str | None = None
        self.sdk_version: str | None = None
        self.controller_version: str | None = None
        self.sensor_versions: list[str] = []
        self.connected = False

    def __enter__(self) -> "HandSession":
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    # -- lifecycle ---------------------------------------------------------
    def connect(self) -> None:
        rc = self.hand.connect_device(self.port)
        if rc != 0:
            code, message = self.hand.get_last_error()
            fail(f"PXCap 连接失败 rc={rc}, code={code}, message={message}")
        self.connected = True
        log(f"[INFO] PXCap 已连接：{self.port}")

        rc, sdk_version = self.hand.get_sdk_version()
        if rc == 0:
            self.sdk_version = str(sdk_version)
            log(f"[INFO] SDK 版本：{self.sdk_version}")

        rc, firmware = self.hand.get_firmware_versions()
        if rc == 0 and firmware is not None:
            self.controller_version = str(firmware.controller_version)
            self.sensor_versions = [
                str(value) for value in firmware.sensor_versions
            ][: int(firmware.sensor_version_count)]
            log(f"[INFO] 固件：{self.controller_version}")
            if self.sensor_versions:
                log(
                    f"[INFO] 传感器模组：{len(self.sensor_versions)} 个 "
                    f"({', '.join(self.sensor_versions)})"
                )

        rc, sn = self.hand.get_sn()
        if rc == 0:
            self.sn = str(sn)
            log(f"[INFO] SN：{self.sn}")

    def close(self) -> None:
        if not self.connected:
            return
        try:
            self.hand.disconnect_device()
        except Exception as exc:  # pragma: no cover - 断开失败不该掩盖主结果
            warn(f"断开设备时出错：{exc}")
        self.connected = False

    # -- reads -------------------------------------------------------------
    def fault_summary(self) -> dict[str, Any]:
        rc, fault = self.hand.get_fault_code()
        if rc != 0 or fault is None:
            code, message = self.hand.get_last_error()
            return {"available": False, "error": f"rc={rc}, code={code}, message={message}"}
        return {
            "available": True,
            "sensor_faults": [
                {"index": int(item.index), "code": int(item.code)}
                for item in fault.sensor_faults
            ],
            "magnetic_encoder_faults": [
                {"index": int(item.index), "code": int(item.code)}
                for item in fault.magnetic_encoder_faults
            ],
        }

    def read_encoders(self) -> dict[str, dict[str, float | int]]:
        rc, raw = self.hand.get_encoder_raw_data()
        if rc != 0:
            code, message = self.hand.get_last_error()
            fail(f"读取编码器原始值失败 rc={rc}, code={code}, message={message}（{RC_NOT_CONNECTED_MSG}）")
        rc, angles = self.hand.get_encoder_angles()
        if rc != 0:
            code, message = self.hand.get_last_error()
            fail(f"读取编码器角度失败 rc={rc}, code={code}, message={message}（{RC_NOT_CONNECTED_MSG}）")
        raw_values = raw_dict(raw)
        angle_values = angles_dict(angles)
        return {
            name: {"raw": raw_values[name], "deg": angle_values[name]}
            for name in ANGLE_FIELDS
        }

    def read_forces(self) -> dict[str, tuple[int, int, int]]:
        rc, resultant = self.hand.get_sensor_resultant_force()
        if rc != 0:
            code, message = self.hand.get_last_error()
            fail(f"读取合力失败 rc={rc}, code={code}, message={message}（{RC_NOT_CONNECTED_MSG}）")
        return forces_dict(resultant)

    def read_static_magnet(self) -> list[int]:
        rc, result = self.hand.get_static_magnet_check()
        if rc != 0:
            code, message = self.hand.get_last_error()
            fail(f"读取静态磁检测结果失败 rc={rc}, code={code}, message={message}")
        return [int(value) for value in result.status]

    # -- writes ------------------------------------------------------------
    def set_encoder_calibration(self) -> None:
        rc = self.hand.set_encoder_calibration()
        if rc != 0:
            code, message = self.hand.get_last_error()
            fail(f"磁编码器标零失败 rc={rc}, code={code}, message={message}（{RC_NOT_CONNECTED_MSG}）")

    def set_sensor_calibration(self) -> None:
        rc = self.hand.set_sensor_calibration()
        if rc != 0:
            code, message = self.hand.get_last_error()
            fail(f"传感器标零失败 rc={rc}, code={code}, message={message}（{RC_NOT_CONNECTED_MSG}）")

    def set_static_magnet_check(self) -> None:
        rc = self.hand.set_static_magnet_check()
        if rc != 0:
            code, message = self.hand.get_last_error()
            fail(f"触发起始静态磁检测失败 rc={rc}, code={code}, message={message}（{RC_NOT_CONNECTED_MSG}）")


# ---------------------------------------------------------------------------
# 采样与闸门
# ---------------------------------------------------------------------------


def sample_encoders(
    session: HandSession,
    samples: int,
    interval_s: float,
) -> list[dict[str, dict[str, float | int]]]:
    """Poll the encoders ``samples`` times at ~1/interval_s."""
    frames: list[dict[str, dict[str, float | int]]] = []
    for index in range(samples):
        frames.append(session.read_encoders())
        if index + 1 < samples:
            time.sleep(interval_s)
    return frames


def sample_forces(
    session: HandSession,
    samples: int,
    interval_s: float,
) -> list[dict[str, tuple[int, int, int]]]:
    frames: list[dict[str, tuple[int, int, int]]] = []
    for index in range(samples):
        frames.append(session.read_forces())
        if index + 1 < samples:
            time.sleep(interval_s)
    return frames


def encoder_jitter(frames: Sequence[dict[str, dict[str, float | int]]]) -> dict[str, float]:
    """Peak-to-peak angle spread per channel; a moving hand shows up here."""
    spread: dict[str, float] = {}
    for name in ANGLE_FIELDS:
        values = [frame[name]["deg"] for frame in frames]
        spread[name] = max(values) - min(values)
    return spread


def force_stats(
    frames: Sequence[dict[str, tuple[int, int, int]]],
) -> tuple[dict[str, tuple[float, float, float]], dict[str, tuple[int, int, int]]]:
    """Per-slot mean offset and peak-to-peak swing over the sampling window.

    The absolute value alone cannot answer "is something pressing on it": the
    stored zero point drifts, so an untouched glove may still read 1 N. Swing is
    what a hand shows; a steady large offset is just a stale zero.
    """
    mean: dict[str, tuple[float, float, float]] = {}
    swing: dict[str, tuple[int, int, int]] = {}
    if not frames:
        raise ValueError("采样窗口为空，无法给出偏置与摆动")
    for slot in CAP_SLOTS:
        axes = list(zip(*(frame[slot] for frame in frames)))
        mean[slot] = tuple(sum(axis) / len(axis) for axis in axes)  # type: ignore[assignment]
        swing[slot] = tuple(max(axis) - min(axis) for axis in axes)  # type: ignore[assignment]
    return mean, swing


def check_encoder_gate(
    spread: dict[str, float],
    limit_deg: float,
) -> list[str]:
    return [
        f"{ANGLE_LABELS[name]} 抖动 {spread[name]:.2f}° > {limit_deg:.2f}°"
        for name in ANGLE_FIELDS
        if spread[name] > limit_deg
    ]


def check_sensor_gate(
    mean: dict[str, tuple[float, float, float]],
    swing: dict[str, tuple[int, int, int]],
    max_swing_raw: int,
    max_offset_raw: int,
) -> tuple[list[str], list[str]]:
    """Return (blocking problems, drift warnings)."""
    problems: list[str] = []
    warnings: list[str] = []
    for slot in CAP_SLOTS:
        worst_swing = max(swing[slot])
        worst_offset = max(abs(value) for value in mean[slot])
        shown = tuple(round(value, 1) for value in mean[slot])
        if worst_swing > max_swing_raw:
            problems.append(
                f"{slot} 窗口内摆动 {swing[slot]} > {max_swing_raw} (0.1N)：手上还在动或受压"
            )
        elif worst_offset > max_offset_raw:
            problems.append(
                f"{slot} 偏置 {shown} 超过 {max_offset_raw} (0.1N)：明显有负载"
            )
        elif worst_offset > max_swing_raw:
            warnings.append(
                f"{slot} 偏置 {shown} = {worst_offset * FORCE_UNIT:.1f} N，更像零点漂移，建议标零"
            )
    return problems, warnings


def confirm(action: str, details: Sequence[str], assume_yes: bool) -> None:
    if assume_yes:
        return
    log("")
    for line in details:
        log(f"  {line}")
    log("")
    answer = input(f"确认执行「{action}」？写入设备后不可撤销，输入 yes 继续：")
    if answer.strip().lower() not in ("yes", "y"):
        fail("已取消，未写入设备")


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------


def write_report(
    path: Path | None,
    payload: dict[str, Any],
    enabled: bool,
) -> Path | None:
    if not enabled:
        return None
    if path is None:
        stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
        sn = str(payload.get("sn") or "unknown").replace("/", "_")
        path = REPORT_DIR / f"hand_{sn}_{stamp}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def report_base(session: HandSession, command: str) -> dict[str, Any]:
    return {
        "tool": "hand_calib.py",
        "command": command,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "port": session.port,
        "sn": session.sn,
        "sdk_version": session.sdk_version,
        "controller_version": session.controller_version,
        "sensor_versions": session.sensor_versions,
        "note": "标定写在设备端并持久化；本文件只是操作留证，不是主机侧的偏移量",
    }


# ---------------------------------------------------------------------------
# 命令
# ---------------------------------------------------------------------------


def format_encoder_table(values: dict[str, dict[str, float | int]]) -> list[str]:
    lines = [f"  {'通道':<18}{'原始值':>12}{'角度(°)':>12}"]
    for name in ANGLE_FIELDS:
        lines.append(
            f"  {ANGLE_LABELS[name]:<18}"
            f"{values[name]['raw']:>12d}{values[name]['deg']:>12.3f}"
        )
    return lines


def format_force_table(values: dict[str, tuple[int, int, int]]) -> list[str]:
    lines = [f"  {'槽位':<15}{'x':>6}{'y':>6}{'z':>6}   (0.1 N)"]
    for slot in CAP_SLOTS:
        x, y, z = values[slot]
        lines.append(f"  {slot:<15}{x:>6d}{y:>6d}{z:>6d}")
    return lines


def command_info(args: argparse.Namespace) -> int:
    port = resolve_hand_port(args.hand_port)
    with HandSession(port) as session:
        fault = session.fault_summary()
        encoders = session.read_encoders()
        forces = session.read_forces()

        log("")
        log("编码器：")
        for line in format_encoder_table(encoders):
            log(line)
        log("")
        log("10 路合力：")
        for line in format_force_table(forces):
            log(line)
        log("")
        if fault.get("available"):
            log(
                f"故障：传感器 {len(fault['sensor_faults'])} 项，"
                f"磁编码器 {len(fault['magnetic_encoder_faults'])} 项"
            )
        else:
            log(f"故障：读取失败（{fault.get('error')}）")

        payload = report_base(session, "info")
        payload.update(
            {
                "encoders": encoders,
                "forces_0p1n": {slot: list(forces[slot]) for slot in CAP_SLOTS},
                "fault": fault,
            }
        )
        path = write_report(args.report, payload, not args.no_report)
        if path:
            log(f"\n报告：{path}")
    return 0


def command_encoder(args: argparse.Namespace) -> int:
    port = resolve_hand_port(args.hand_port)
    with HandSession(port) as session:
        log(f"[INFO] 采样 {args.samples} 帧检查静止状态（{args.interval_ms} ms 间隔）...")
        before_frames = sample_encoders(session, args.samples, args.interval_ms / 1000.0)
        spread = encoder_jitter(before_frames)
        problems = check_encoder_gate(spread, args.max_jitter_deg)

        log("")
        log("标零前编码器：")
        for line in format_encoder_table(before_frames[-1]):
            log(line)
        log("")
        log(
            "静止检查（峰峰值）："
            + "，".join(f"{ANGLE_LABELS[n]} {spread[n]:.2f}°" for n in ANGLE_FIELDS)
        )

        if problems:
            for problem in problems:
                warn(problem)
            if not args.force:
                fail(
                    "手没有静止，标零会把运动量写进零点。保持手套不动（建议平放桌面）"
                    "后重试；确实要用当前姿态标零请加 --force"
                )
            warn("--force 生效：忽略静止检查，继续标零")

        before = before_frames[-1]
        confirm(
            "磁编码器标零",
            [
                "磁编码器标零后，当前姿态成为零点，之后的角度以它为基准。",
                "该零点写入手套存储并持久化，本程序无法撤销，只能重新标零。",
                "执行前请确认：手套已平放/静止，且没有其他进程在采集。",
            ],
            args.yes or args.dry_run,
        )

        if args.dry_run:
            log("[INFO] --dry-run：跳过写设备，只输出前置检查结果")
            payload = report_base(session, "encoder")
            payload.update(
                {
                    "dry_run": True,
                    "jitter_deg": spread,
                    "before": before,
                    "gate_passed": not problems,
                    "result": "dry-run",
                }
            )
            path = write_report(args.report, payload, not args.no_report)
            if path:
                log(f"报告：{path}")
            return 0

        session.set_encoder_calibration()
        log("[INFO] 标零指令已下发，SDK 处理约 2 秒...")
        time.sleep(args.settle_s)

        after_frames = sample_encoders(session, max(3, args.samples // 4), args.interval_ms / 1000.0)
        after = after_frames[-1]
        residuals = {name: abs(after[name]["deg"]) for name in ANGLE_FIELDS}
        worst = max(residuals.values())

        log("")
        log("标零后编码器：")
        for line in format_encoder_table(after):
            log(line)
        log("")
        log(f"零点残差：最大 {worst:.2f}°（判据 ≤ {args.max_residual_deg:.2f}°）")

        payload = report_base(session, "encoder")
        payload.update(
            {
                "dry_run": False,
                "jitter_deg": spread,
                "before": before,
                "after": after,
                "residual_deg": residuals,
                "gate_passed": not problems,
                "forced": bool(args.force and problems),
                "result": "ok" if worst <= args.max_residual_deg else "residual-too-large",
            }
        )
        path = write_report(args.report, payload, not args.no_report)
        if path:
            log(f"报告：{path}")

        if worst > args.max_residual_deg:
            warn(
                f"零点残差 {worst:.2f}° 超过 {args.max_residual_deg:.2f}°："
                "可能是标零瞬间手在动，或该路磁编码器异常（跑 magnet 看静态磁检测）"
            )
            return 1
        log("[INFO] 磁编码器标零完成")
    return 0


def command_sensor(args: argparse.Namespace) -> int:
    port = resolve_hand_port(args.hand_port)
    with HandSession(port) as session:
        log(f"[INFO] 采样 {args.samples} 帧检查空载状态（{args.interval_ms} ms 间隔）...")
        before_frames = sample_forces(session, args.samples, args.interval_ms / 1000.0)
        mean, swing = force_stats(before_frames)
        problems, drift = check_sensor_gate(
            mean, swing, args.max_swing_raw, args.max_offset_raw
        )

        log("")
        log("标零前合力（窗口均值 / 摆动峰峰值，单位 0.1 N）：")
        log(f"  {'槽位':<15}{'均值x':>7}{'均值y':>7}{'均值z':>7}  {'Δx':>5}{'Δy':>5}{'Δz':>5}")
        for slot in CAP_SLOTS:
            mx, my, mz = mean[slot]
            sx, sy, sz = swing[slot]
            log(
                f"  {slot:<15}{mx:>7.1f}{my:>7.1f}{mz:>7.1f}"
                f"  {sx:>5d}{sy:>5d}{sz:>5d}"
            )

        for line in drift:
            warn(line)

        if problems:
            for problem in problems:
                warn(problem)
            if not args.force:
                fail(
                    "传感器标零会把当前受力当成空载零点。请摘下手套、清空接触面后"
                    "重试；确实要带载标零请加 --force（不推荐）"
                )
            warn("--force 生效：忽略空载检查，继续标零")

        before = before_frames[-1]
        confirm(
            "传感器标零",
            [
                "传感器标零后，当前受力成为零点，之后 10 路合力以它为基准。",
                "该零点写入手套存储并持久化，本程序无法撤销，只能重新标零。",
                "执行前请确认：手套空载、无任何接触，且没有其他进程在采集。",
            ],
            args.yes or args.dry_run,
        )

        if args.dry_run:
            log("[INFO] --dry-run：跳过写设备，只输出前置检查结果")
            payload = report_base(session, "sensor")
            payload.update(
                {
                    "dry_run": True,
                    "mean_0p1n": {slot: [round(v, 2) for v in mean[slot]] for slot in CAP_SLOTS},
                    "swing_0p1n": {slot: list(swing[slot]) for slot in CAP_SLOTS},
                    "before_0p1n": {slot: list(before[slot]) for slot in CAP_SLOTS},
                    "problems": problems,
                    "drift_warnings": drift,
                    "gate_passed": not problems,
                    "result": "dry-run",
                }
            )
            path = write_report(args.report, payload, not args.no_report)
            if path:
                log(f"报告：{path}")
            return 0

        session.set_sensor_calibration()
        log("[INFO] 标零指令已下发，SDK 处理约 2 秒...")
        time.sleep(args.settle_s)

        after_frames = sample_forces(session, max(3, args.samples // 4), args.interval_ms / 1000.0)
        after = after_frames[-1]
        mean_after, _swing_after = force_stats(after_frames)
        worst = max(
            abs(value) for values in mean_after.values() for value in values
        )

        log("")
        log("标零后合力：")
        for line in format_force_table(after):
            log(line)
        log("")
        log(
            f"空载残差：最大 |偏置| {worst:.1f} (0.1N) = {worst * FORCE_UNIT:.1f} N"
            f"（判据 ≤ {args.max_residual_raw}）"
        )

        payload = report_base(session, "sensor")
        payload.update(
            {
                "dry_run": False,
                "mean_before_0p1n": {
                    slot: [round(v, 2) for v in mean[slot]] for slot in CAP_SLOTS
                },
                "swing_before_0p1n": {slot: list(swing[slot]) for slot in CAP_SLOTS},
                "after_0p1n": {slot: list(after[slot]) for slot in CAP_SLOTS},
                "residual_0p1n": {
                    slot: [round(v, 2) for v in mean_after[slot]] for slot in CAP_SLOTS
                },
                "problems": problems,
                "drift_warnings": drift,
                "gate_passed": not problems,
                "forced": bool(args.force and problems),
                "result": "ok" if worst <= args.max_residual_raw else "residual-too-large",
            }
        )
        path = write_report(args.report, payload, not args.no_report)
        if path:
            log(f"报告：{path}")

        if worst > args.max_residual_raw:
            warn(
                f"空载残差 {worst:.1f} (0.1N) 超过判据：可能标零时手套仍被接触，"
                "或该路传感器异常（看 info 里的故障项）"
            )
            return 1
        log("[INFO] 传感器标零完成")
    return 0


def command_magnet(args: argparse.Namespace) -> int:
    port = resolve_hand_port(args.hand_port)
    with HandSession(port) as session:
        confirm(
            "静态磁检测",
            [
                "静态磁检测会让 4 路磁编码器依次进入检测状态，约数秒到十几秒。",
                "检测期间手套必须静止，且不能有其他进程轮询同一个串口。",
            ],
            args.yes or args.dry_run,
        )

        if args.dry_run:
            log("[INFO] --dry-run：跳过写设备")
            return 0

        session.set_static_magnet_check()
        log("[INFO] 静态磁检测已触发，等待结果...")

        deadline = time.monotonic() + args.timeout_s
        status: list[int] = []
        while time.monotonic() < deadline:
            time.sleep(0.5)
            status = session.read_static_magnet()
            if status and all(value != 2 for value in status):
                break

        log("")
        log(f"  {'通道':<18}{'状态':>8}")
        labels = {0: "正常", 1: "异常", 2: "检测中"}
        for name, value in zip(ANGLE_FIELDS, status):
            log(f"  {ANGLE_LABELS[name]:<18}{labels.get(value, value):>8}")

        abnormal = [name for name, value in zip(ANGLE_FIELDS, status) if value == 1]
        pending = [name for name, value in zip(ANGLE_FIELDS, status) if value == 2]

        payload = report_base(session, "magnet")
        payload.update(
            {
                "status_by_channel": dict(zip(ANGLE_FIELDS, status)),
                "abnormal": abnormal,
                "pending": pending,
                "result": "timeout" if pending else ("abnormal" if abnormal else "ok"),
            }
        )
        path = write_report(args.report, payload, not args.no_report)
        if path:
            log(f"\n报告：{path}")

        if pending:
            warn(f"超时仍有通道在检测中：{', '.join(pending)}")
            return 1
        if abnormal:
            warn(f"磁编码器异常：{', '.join(abnormal)}，请检查磁铁位置与排线")
            return 1
        log("[INFO] 4 路静态磁检测正常")
    return 0


# ---------------------------------------------------------------------------
# 离线自检
# ---------------------------------------------------------------------------


def _frames_for_force(
    value: int,
    swing: int,
    ticks: int = 6,
) -> list[dict[str, tuple[int, int, int]]]:
    return [
        {slot: (value + (tick % 2) * swing, value, value) for slot in CAP_SLOTS}
        for tick in range(ticks)
    ]


def _rejects_empty() -> bool:
    try:
        force_stats([])
    except (ValueError, IndexError, ZeroDivisionError):
        return True
    return False


def command_selftest(args: argparse.Namespace) -> int:
    """Exercise the gates on synthetic frames; no device and no SDK needed."""
    checks: list[tuple[str, bool, str]] = []

    still = [
        {name: {"raw": 0, "deg": 10.0 + (tick % 2) * 0.1} for name in ANGLE_FIELDS}
        for tick in range(6)
    ]
    spread_still = encoder_jitter(still)
    checks.append(
        (
            "静止手通过编码器闸门",
            not check_encoder_gate(spread_still, 0.3),
            f"峰峰值 {max(spread_still.values()):.2f}°",
        )
    )

    moving = [
        {name: {"raw": 0, "deg": 10.0 + tick * 1.0} for name in ANGLE_FIELDS}
        for tick in range(6)
    ]
    encoder_problems = check_encoder_gate(encoder_jitter(moving), 0.3)
    checks.append(
        (
            "运动手被编码器闸门拦住",
            len(encoder_problems) == len(ANGLE_FIELDS),
            f"{len(encoder_problems)} 路告警",
        )
    )

    mean, swing = force_stats(_frames_for_force(7, 0))
    sensor_problems, drift = check_sensor_gate(mean, swing, 8, 20)
    checks.append(
        (
            "空载小偏置直接放行",
            not sensor_problems and not drift,
            f"偏置 {mean['thumb_one']}",
        )
    )

    mean, swing = force_stats(_frames_for_force(12, 0))
    sensor_problems, drift = check_sensor_gate(mean, swing, 8, 20)
    checks.append(
        (
            "零点漂移只给建议不阻断",
            not sensor_problems and bool(drift),
            f"{len(drift)} 路提示",
        )
    )

    mean, swing = force_stats(_frames_for_force(44, 0))
    sensor_problems, _ = check_sensor_gate(mean, swing, 8, 20)
    checks.append(
        ("带载被传感器闸门拦住", bool(sensor_problems), f"{len(sensor_problems)} 路告警")
    )

    mean, swing = force_stats(_frames_for_force(10, 20))
    sensor_problems, _ = check_sensor_gate(mean, swing, 8, 20)
    checks.append(
        ("窗口内摆动被拦住", bool(sensor_problems), f"{len(sensor_problems)} 路告警")
    )

    checks.append(
        ("空窗口不被静默接受", _rejects_empty(), "空帧列表应抛异常而非给出 0 摆动")
    )

    failed = [name for name, ok, _detail in checks if not ok]
    for name, ok, detail in checks:
        log(f"  [{'PASS' if ok else 'FAIL'}] {name}（{detail}）")
    log("")
    if failed:
        warn(f"{len(failed)} 项失败：{', '.join(failed)}")
        return 1
    log(f"[INFO] hand_calib 自检通过：{len(checks)} 项")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "PxCap III 手套标定与维护：磁编码器标零 / 传感器标零 / 静态磁检测。"
            "写操作直接改设备内持久化的零点，请先看 DOC/标定.md §1（手套标定）"
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    def add_global_options(target: argparse.ArgumentParser, *, in_subcommand: bool) -> None:
        """Global switches, accepted before and after the subcommand.

        The copies inside subparsers use ``SUPPRESS`` so an omitted flag there
        keeps whatever the top-level parser already parsed instead of
        shadowing it with a default.
        """
        if in_subcommand:
            hide = argparse.SUPPRESS
            target.add_argument(
                "--hand-port", default=hide,
                help="PXCap 串口；auto = 按 USB 28e9:018a 自动发现",
            )
            target.add_argument(
                "--yes", action="store_true", default=hide,
                help="跳过交互确认（自动化用）",
            )
            target.add_argument(
                "--dry-run", action="store_true", default=hide,
                help="只跑前置检查与采样，不写设备",
            )
            target.add_argument(
                "--report", type=Path, default=hide,
                help="报告落盘路径；默认 calib/hand/hand_<SN>_<时间>.json",
            )
            target.add_argument(
                "--no-report", action="store_true", default=hide,
                help="不写报告文件",
            )
            return
        target.add_argument(
            "--hand-port",
            default="auto",
            help="PXCap 串口；auto = 按 USB 28e9:018a 自动发现",
        )
        target.add_argument("--yes", action="store_true", help="跳过交互确认（自动化用）")
        target.add_argument(
            "--dry-run",
            action="store_true",
            help="只跑前置检查与采样，不写设备",
        )
        target.add_argument(
            "--report",
            type=Path,
            default=None,
            help="报告落盘路径；默认 calib/hand/hand_<SN>_<时间>.json",
        )
        target.add_argument("--no-report", action="store_true", help="不写报告文件")

    add_global_options(parser, in_subcommand=False)

    global_parent = argparse.ArgumentParser(add_help=False)
    add_global_options(global_parent, in_subcommand=True)

    gate = argparse.ArgumentParser(add_help=False, parents=[global_parent])
    gate.add_argument("--samples", type=int, default=30, help="采样帧数")
    gate.add_argument(
        "--interval-ms", type=float, default=20.0, help="采样间隔（毫秒）"
    )
    gate.add_argument(
        "--settle-s", type=float, default=2.5, help="写指令后等待设备处理的时间（秒）"
    )
    gate.add_argument(
        "--force", action="store_true", help="跳过静止/空载闸门（不推荐，会记进报告）"
    )

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("info", help="只读：现状、故障、编码器与合力", parents=[global_parent])

    enc = sub.add_parser("encoder", help="磁编码器标零（写设备）", parents=[gate])
    enc.add_argument(
        "--max-jitter-deg",
        type=float,
        default=0.3,
        help="允许的静止抖动峰峰值；超过即拒绝标零",
    )
    enc.add_argument(
        "--max-residual-deg",
        type=float,
        default=2.0,
        help="标零后 |角度| 的判据",
    )

    sen = sub.add_parser("sensor", help="传感器标零（写设备）", parents=[gate])
    sen.add_argument(
        "--max-swing-raw",
        type=int,
        default=8,
        help="采样窗内允许的摆动峰峰值（0.1N）；超过视为手在动/受压，拒绝标零",
    )
    sen.add_argument(
        "--max-offset-raw",
        type=int,
        default=20,
        help="允许的偏置上限（0.1N）；超过视为明显负载，拒绝标零",
    )
    sen.add_argument(
        "--max-residual-raw",
        type=int,
        default=5,
        help="标零后 |偏置| 的判据，单位 0.1N",
    )

    mag = sub.add_parser("magnet", help="静态磁检测（写设备）", parents=[global_parent])
    mag.add_argument("--timeout-s", type=float, default=30.0, help="等待检测结果的上限")

    sub.add_parser("ports", help="列出发现的串口后退出")
    sub.add_parser("selftest", help="离线自检：不连设备，只验闸门逻辑")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "ports":
        ports = discover_hand_ports()
        if not ports:
            log("未发现 ttyUSB / ttyACM 串口")
            return 1
        for port in ports:
            marker = "  <-- PXCap" if port.usb_id == CAP_USB_ID else ""
            log(f"{port.path}  USB {port.usb_id or '未知'}  {port.by_id or ''}{marker}")
        return 0

    handlers = {
        "info": command_info,
        "encoder": command_encoder,
        "sensor": command_sensor,
        "magnet": command_magnet,
        "selftest": command_selftest,
    }
    try:
        return handlers[args.command](args)
    except KeyboardInterrupt:
        warn("已中断；如果写指令已经下发，设备端状态可能已改变")
        return 130
    except SystemExit:
        raise
    except Exception as exc:  # pragma: no cover - 兜底，避免吞掉 traceback 之外的上下文
        fail(f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    sys.exit(main())
