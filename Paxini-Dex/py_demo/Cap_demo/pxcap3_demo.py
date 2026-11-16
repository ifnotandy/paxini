#!/usr/bin/env python3
"""
PxCap3 连续采集与实时显示示例
功能：持续采集全部触觉、编码器和关节角数据，在独立窗口显示和滚动打印。
公开头文档随安装包 VERSION 同步，不在示例中维护独立版本号。
连接状态：is_connected() 在连续采集和固件升级期间保持可用，且不访问设备。
回调约束：回调对象仅在本次调用有效；耗时处理、停止任务和销毁句柄必须转交业务线程。
用法：python3 pxcap3_demo.py <串口> [--upgrade-online <HTTP地址> | --upgrade-offline <ZIP包路径>]
     [--with-side-effects] [--sn=序列号] [--frequency 100]
关闭窗口、点击停止按钮或按 Ctrl+C 结束采集。采集频率默认 100Hz，窗口和终端每 100ms 输出最新帧。
窗口输出保留最近 2000 行，不保存文件，也不逐帧记录高频采集历史。
分布力打印全部有效测点的 XYZ 原始码和换算力（N）；测点编号为每路从 0 开始的数组下标。
注意：带 --with-side-effects 会执行写入/控制操作（如写SN、校准等）
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from dataclasses import dataclass
from threading import Lock

import pxhandsdk

CAP3_SENSOR_MODULE_LABELS = [
    "thumb_one",
    "thumb_two",
    "index_zero_one",
    "middle_zero_one",
    "middle_two_three",
    "palm_zero",
    "palm_one",
]

CAP3_SENSOR_ROUTE_LABELS = [
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

CAP3_ENCODER_LABELS = [
    "index_mag1（食指指腹）",
    "index_mag2（食指指尖）",
    "middle_mag3（三指指腹）",
    "middle_mag4（三指指尖）",
]

CAP3_ENCODER_FIELDS = (
    "index_proximal", "index_distal", "middle_proximal", "middle_distal",
)


@dataclass(frozen=True)
class CollectionSnapshot:
    """仅持有 Python 数值，不能将 SDK 回调对象留给界面线程使用。"""

    timestamp_unix_ns: int
    timestamp_monotonic_ns: int
    resultant_forces: tuple
    distributed_forces: tuple
    encoder_raw: tuple
    joint_angles: tuple


def _copy_collection_frame(frame) -> CollectionSnapshot:
    def xyz(force):
        return (int(force.x), int(force.y), int(force.z))

    resultant_forces = []
    distributed_forces = []
    for label in CAP3_SENSOR_ROUTE_LABELS:
        resultant_forces.append(xyz(getattr(frame.resultant_force, label)))
        distribution = getattr(frame.distribute_force, label)
        values = distribution.values
        # values 是固定容量数组，只复制有效点；不截断成原示例的前几个点。
        distributed_forces.append(tuple(xyz(value) for value in values[:distribution.count]))
    return CollectionSnapshot(
        timestamp_unix_ns=int(frame.timestamp_unix_ns),
        timestamp_monotonic_ns=int(frame.timestamp_monotonic_ns),
        resultant_forces=tuple(resultant_forces),
        distributed_forces=tuple(distributed_forces),
        encoder_raw=tuple(int(getattr(frame.encoder_raw, field)) for field in CAP3_ENCODER_FIELDS),
        joint_angles=tuple(float(getattr(frame.joint_angles, field)) for field in CAP3_ENCODER_FIELDS),
    )


def _format_collection_frame(snapshot: CollectionSnapshot, frame_number: int) -> str:
    lines = [
        f"=== 第 {frame_number} 帧 | Unix(ns)={snapshot.timestamp_unix_ns} "
        f"| Monotonic(ns)={snapshot.timestamp_monotonic_ns} ===",
        "编码器原始值 / 关节角(度):",
    ]
    for label, raw, angle in zip(CAP3_ENCODER_LABELS, snapshot.encoder_raw, snapshot.joint_angles):
        lines.append(f"  {label}: {raw} / {angle:.3f}°")
    lines.append("传感器合力（X, Y, Z；原始码，1 码 = 0.1 N）:")
    lines.append("分布力测点按每路从 0 开始编号，同时显示 XYZ 原始码及换算力（N）:")
    for label, resultant, points in zip(
        CAP3_SENSOR_ROUTE_LABELS, snapshot.resultant_forces, snapshot.distributed_forces
    ):
        lines.append(f"  {label}: 合力={resultant}, 分布力点数={len(points)}")
        for point_index, point in enumerate(points):
            x, y, z = point
            lines.append(
                f"    {label}[{point_index}]: 原始码(X, Y, Z)={point}; "
                f"力(N): X={x / 10:.1f}, Y={y / 10:.1f}, Z={z / 10:.1f}"
            )
    return "\n".join(lines) + "\n\n"


class CollectionWindow:
    REFRESH_MS = 100
    MAX_LOG_LINES = 2000
    FRAME_TIMEOUT_SECONDS = 5.0

    def __init__(self, cap3: pxhandsdk.PxCap3, serial_port: str, frequency: int):
        try:
            import tkinter as tk
            from tkinter import ttk
            from tkinter.scrolledtext import ScrolledText
        except ImportError as exc:
            raise RuntimeError("实时窗口需要 tkinter，请为运行脚本的 Python 安装 Tk 支持。") from exc

        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            raise RuntimeError("无法打开实时窗口，请在有图形桌面的终端中运行并检查 DISPLAY。") from exc

        self.cap3 = cap3
        self.frequency = frequency
        self.exit_code = 0
        self._lock = Lock()
        self._latest = None
        self._frame_count = 0
        self._callback_error = ""
        self._last_received = time.monotonic()
        self._started = False
        self._closed = False
        self._failed = False
        self._last_health_check = 0.0
        self._rate_count = 0
        self._rate_time = time.monotonic()

        self.root.title(f"PxCap3 连续采集 — {serial_port}")
        self.root.geometry("1160x820")
        self.root.minsize(860, 650)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Control-c>", lambda event: self.close())

        toolbar = ttk.Frame(self.root, padding=8)
        toolbar.pack(fill="x")
        self.status = tk.StringVar(value="准备启动连续采集...")
        ttk.Label(toolbar, textvariable=self.status).pack(side="left")
        ttk.Button(toolbar, text="停止采集并退出", command=self.close).pack(side="right")
        self.timestamps = tk.StringVar(value="等待第一帧数据...")
        ttk.Label(self.root, textvariable=self.timestamps, padding=(8, 0)).pack(anchor="w")
        ttk.Label(
            self.root,
            text="窗口和终端每 100ms 输出最新帧；合力表单位：0.1N；测点输出含原始码及 N；角度单位：度。",
            padding=(8, 6),
        ).pack(anchor="w")

        tables = ttk.Frame(self.root, padding=8)
        tables.pack(fill="x")
        sensors = ttk.LabelFrame(tables, text="十路传感器合力", padding=6)
        sensors.pack(side="left", fill="both", expand=True, padx=(0, 8))
        self.sensor_table = ttk.Treeview(
            sensors, columns=("sensor", "x", "y", "z", "count"), show="headings", height=10
        )
        for column, title in zip(
            ("sensor", "x", "y", "z", "count"), ("传感器", "X", "Y", "Z", "分布力点数")
        ):
            self.sensor_table.heading(column, text=title)
            self.sensor_table.column(column, width=150 if column == "sensor" else 70, anchor="center")
        for label in CAP3_SENSOR_ROUTE_LABELS:
            self.sensor_table.insert("", "end", iid=label, values=(label, "—", "—", "—", "—"))
        self.sensor_table.pack(fill="both", expand=True)

        encoders = ttk.LabelFrame(tables, text="四路编码器与关节角", padding=6)
        encoders.pack(side="left", fill="both", expand=True)
        self.encoder_table = ttk.Treeview(
            encoders, columns=("encoder", "raw", "angle"), show="headings", height=4
        )
        for column, title in zip(("encoder", "raw", "angle"), ("编码器", "原始值", "角度(°)")):
            self.encoder_table.heading(column, text=title)
            self.encoder_table.column(column, width=180 if column == "encoder" else 85, anchor="center")
        for field, label in zip(CAP3_ENCODER_FIELDS, CAP3_ENCODER_LABELS):
            self.encoder_table.insert("", "end", iid=field, values=(label, "—", "—"))
        self.encoder_table.pack(fill="both", expand=True)

        output = ttk.LabelFrame(
            self.root, text="采集数据滚动输出（保留最近 2000 行）", padding=6
        )
        output.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.auto_scroll = tk.BooleanVar(value=True)
        ttk.Checkbutton(output, text="自动滚动到最新数据", variable=self.auto_scroll).pack(anchor="w")
        self.log = ScrolledText(output, wrap="word", height=22, state="disabled")
        self.log.pack(fill="both", expand=True)

    def _on_frame(self, frame) -> None:
        # SDK 工作线程：只复制数据，禁止在这里更新窗口或调用 stop_collection。
        try:
            snapshot = _copy_collection_frame(frame)
            with self._lock:
                # 仅保留最新帧，避免图形刷新速度低于采集速度时出现无限积压。
                self._latest = snapshot
                self._frame_count += 1
                self._last_received = time.monotonic()
        except Exception as exc:
            with self._lock:
                self._callback_error = f"采集帧复制失败: {exc}"

    def _append_log(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", message)
        line_count = int(self.log.index("end-1c").split(".")[0])
        if line_count > self.MAX_LOG_LINES:
            self.log.delete("1.0", f"{line_count - self.MAX_LOG_LINES + 1}.0")
        if self.auto_scroll.get():
            self.log.see("end")
        self.log.configure(state="disabled")

    def _stop_collection(self) -> None:
        if self._started:
            self._started = False
            stop_ret = self.cap3.stop_collection()
            print(f"pxcap3_stop_collection: 返回值 {stop_ret}")
            if stop_ret != 0:
                self.exit_code = 1
                _print_last_error(self.cap3)

    def _fail(self, message: str) -> None:
        self.exit_code = 1
        self._failed = True
        self.status.set(f"采集已停止：{message}")
        self._append_log(f"\n[错误] {message}\n请检查设备后关闭窗口，重新运行程序。\n")
        print(message, file=sys.stderr)
        self._stop_collection()

    def _refresh(self) -> None:
        if self._closed:
            return
        try:
            if not self._failed:
                self._refresh_data()
        except Exception as exc:
            self._fail(f"刷新采集数据失败: {exc}")
        # 定时回到 Python，也让 Ctrl+C 和故障后的窗口关闭保持可用。
        self.root.after(self.REFRESH_MS, self._refresh)

    def _refresh_data(self) -> None:
        now = time.monotonic()
        with self._lock:
            snapshot = self._latest
            self._latest = None
            count = self._frame_count
            last_received = self._last_received
            callback_error = self._callback_error
        if callback_error:
            self._fail(callback_error)
            return
        if now - self._last_health_check >= 1.0:
            self._last_health_check = now
            connected_ret, connected = self.cap3.is_connected()
            if connected_ret != 0 or not connected:
                self._fail(f"设备连接已失效: {self.cap3.get_last_error()}")
                return
        if now - last_received > self.FRAME_TIMEOUT_SECONDS:
            self._fail(f"连续 5 秒未收到采集帧: {self.cap3.get_last_error()}")
            return
        if snapshot is None:
            self.status.set(f"等待新数据 | 已采集 {count} 帧 | 目标 {self.frequency} Hz")
            return

        rate = (count - self._rate_count) / max(now - self._rate_time, 1e-9)
        self._rate_count, self._rate_time = count, now
        self.status.set(f"持续采集中 | {count} 帧 | 目标 {self.frequency} Hz | 实际约 {rate:.1f} Hz")
        self.timestamps.set(
            f"Unix(ns): {snapshot.timestamp_unix_ns}    Monotonic(ns): {snapshot.timestamp_monotonic_ns}"
        )
        for label, resultant, points in zip(
            CAP3_SENSOR_ROUTE_LABELS, snapshot.resultant_forces, snapshot.distributed_forces
        ):
            self.sensor_table.item(label, values=(label, *resultant, len(points)))
        for field, label, raw, angle in zip(
            CAP3_ENCODER_FIELDS, CAP3_ENCODER_LABELS, snapshot.encoder_raw, snapshot.joint_angles
        ):
            self.encoder_table.item(field, values=(label, raw, f"{angle:.3f}"))
        message = _format_collection_frame(snapshot, count)
        self._append_log(message)
        print(message, end="", flush=True)

    def close(self) -> None:
        self._closed = True
        self.root.quit()

    def run(self) -> int:
        previous_sigint = signal.getsignal(signal.SIGINT)
        try:
            signal.signal(signal.SIGINT, lambda signum, frame: self.close())
            self._last_received = self._rate_time = time.monotonic()
            start_ret = self.cap3.start_collection(self.frequency, self._on_frame)
            print(f"pxcap3_start_collection({self.frequency} Hz): 返回值 {start_ret}")
            if start_ret != 0:
                raise RuntimeError(f"无法启动连续采集: {self.cap3.get_last_error()}")
            self._started = True
            if not self._closed:
                self.root.after(self.REFRESH_MS, self._refresh)
                self.root.mainloop()
        finally:
            try:
                self._stop_collection()
            finally:
                signal.signal(signal.SIGINT, previous_sigint)
                self.root.destroy()
        return self.exit_code


def _static_mag_status_label(status: int) -> str:
    if status == 0:
        return "正常"
    if status == 1:
        return "异常"
    if status == 2:
        return "检测中"
    return "未知"


def _format_static_mag_result(statuses: list[int]) -> str:
    return ", ".join(
        f"{label}={_static_mag_status_label(status)}"
        for label, status in zip(CAP3_ENCODER_LABELS, statuses)
    )


def _static_mag_overview(statuses: list[int]) -> str:
    return "全部正常" if all(status == 0 for status in statuses) else "存在异常或检测中"


def _is_upgrade_terminal_phase(phase: int) -> bool:
    enum = pxhandsdk.pxcap3.PxCap3UpgradePhase
    return phase in {
        enum.kMainboardFailed,
        enum.kSensor1Failed,
        enum.kSensor2Failed,
        enum.kSensor3Failed,
        enum.kSensor4Failed,
        enum.kSensor5Failed,
        enum.kSensor6Failed,
        enum.kSensor7Failed,
        enum.kSuccess,
        enum.kCancelled,
    }


def _describe_upgrade_firmware(progress) -> str:
    if progress.firmware_name != "":
        return progress.firmware_name
    if progress.firmware_index == 1:
        return "mainboard"
    if progress.firmware_index >= 2:
        return f"sensor#{progress.firmware_index - 1}"
    return "none"


def _print_last_error(cap3: pxhandsdk.PxCap3) -> None:
    error_code, error_message = cap3.get_last_error()
    print(f"  last_error.code={error_code}, last_error.message={error_message}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="PxCap3 持续采集，在独立窗口实时显示全部传感器和编码器数据")
    parser.add_argument("serial_port", help="设备串口，例如 /dev/ttyACM0")
    parser.add_argument("--frequency", type=int, default=100, help="目标采集频率，1~300 Hz，默认 100")
    upgrade = parser.add_mutually_exclusive_group()
    upgrade.add_argument("--upgrade-online", default="", metavar="HTTP地址")
    upgrade.add_argument("--upgrade-offline", default="", metavar="ZIP包路径")
    parser.add_argument("--with-side-effects", action="store_true", help="采集前执行校准、静态磁检测和写 SN")
    parser.add_argument("--sn", default="PXCAP3_DEMO_SN", help="配合 --with-side-effects 写入的序列号")
    args = parser.parse_args(argv[1:])
    if not 1 <= args.frequency <= 300:
        parser.error("--frequency 必须是 1~300 之间的整数")
    serial_port = args.serial_port

    print("=== PxCap3 连续采集程序 ===")
    print(f"串口: {serial_port}")
    print(f"在线升级地址: {args.upgrade_online or '(无)'}")
    print(f"离线升级包: {args.upgrade_offline or '(无)'}")
    print(f"带副作用操作: {'是' if args.with_side_effects else '否'}")
    print(f"目标采集频率: {args.frequency} Hz（关闭实时窗口或按 Ctrl+C 停止）")
    exit_code = 0

    # 1. 创建会话句柄
    print("\n--- 1. 创建会话 ---")
    cap3 = pxhandsdk.PxCap3()
    print("pxcap3_create: 成功")

    # 2. 获取 SDK 版本
    sdk_ret, sdk_version = cap3.get_sdk_version()
    print(f"pxcap3_get_sdk_version: 返回值 {sdk_ret}", end="")
    if sdk_ret == 0:
        print(f", 版本 {sdk_version}")
    else:
        print()

    # 3. 连接设备
    print("\n--- 2. 连接设备 ---")
    connect_ret = cap3.connect_device(serial_port)
    print(f"pxcap3_connect: 返回值 {connect_ret}", end="")
    if connect_ret != 0:
        error_code, error_msg = cap3.get_last_error()
        print(f"\n连接失败, error_code={error_code}, 信息: {error_msg}")
        return 1
    print(", 成功")

    try:
        exit_code = _run_connected_demo(cap3, args)
    except KeyboardInterrupt:
        print("\n已请求停止采集。")
        exit_code = 130
    finally:
        print("\n--- 断开连接 ---")
        disconnect_ret = cap3.disconnect_device()
        print(f"pxcap3_disconnect: 返回值 {disconnect_ret}")
        if disconnect_ret != 0:
            _print_last_error(cap3)
            exit_code = 1
    print("\n=== 连续采集程序结束 ===")
    return exit_code


def _run_connected_demo(cap3: pxhandsdk.PxCap3, args: argparse.Namespace) -> int:
    upgrade_online_address = args.upgrade_online
    upgrade_offline_package = args.upgrade_offline
    with_side_effects = args.with_side_effects
    sn_value = args.sn
    exit_code = 0

    # 4. 查询连接状态
    connected_ret, connected = cap3.is_connected()
    print(f"pxcap3_is_connected: 返回值 {connected_ret}, 状态 {1 if connected else 0}")

    # 5. 读取固件版本
    print("\n--- 3. 读取设备信息 ---")
    version_ret, version_info = cap3.get_firmware_versions()
    print(f"pxcap3_get_firmware_versions: 返回值 {version_ret}", end="")
    if version_ret == 0:
        print(f"\n  控制器版本: {version_info.controller_version}")
        print(f"  传感器模组数量: {version_info.sensor_version_count}")
        for i in range(version_info.sensor_version_count):
            label = CAP3_SENSOR_MODULE_LABELS[i] if i < len(CAP3_SENSOR_MODULE_LABELS) else "unknown_sensor_module"
            print(f"    {label}: {version_info.sensor_versions[i]}")
    else:
        print()

    # 6. 读取设备 SN
    sn_ret, sn = cap3.get_sn()
    print(f"pxcap3_get_sn: 返回值 {sn_ret}", end="")
    if sn_ret == 0:
        print(f", SN {sn}")
    else:
        print()

    # 诊断接口与连续采集互斥，启动采集前读取一次。
    print("\n--- 4. 读取故障信息 ---")
    fault_ret, fault_info = cap3.get_fault_code()
    print(f"pxcap3_get_fault_code: 返回值 {fault_ret}", end="")
    if fault_ret == 0:
        print(f", 传感器故障 {fault_info.sensor_fault_count}, 磁编故障 {fault_info.magnetic_encoder_fault_count}")
        if fault_info.sensor_fault_count > 0:
            print("  传感器故障项:")
            for item in fault_info.sensor_faults:
                label = CAP3_SENSOR_ROUTE_LABELS[item.source_index] if item.source_index < len(CAP3_SENSOR_ROUTE_LABELS) else "unknown_sensor_slot"
                print(f"    {label}: 0x{item.error_code:02X}")
        if fault_info.magnetic_encoder_fault_count > 0:
            print("  磁编码器故障项:")
            for item in fault_info.magnetic_encoder_faults:
                label = CAP3_ENCODER_LABELS[item.source_index] if item.source_index < len(CAP3_ENCODER_LABELS) else "unknown_encoder_channel"
                print(f"    {label}: 0x{item.error_code:02X}")
    else:
        print()

    # 读取静态磁检测结果
    static_ret, static_result = cap3.get_static_magnet_check()
    print(f"pxcap3_get_static_magnet_check: 返回值 {static_ret}")
    if static_ret == 0:
        print(f"  静态磁检测详情: {_format_static_mag_result(static_result.status)}")
        print(f"  静态磁检测概览: {_static_mag_overview(static_result.status)}")

    # 9. 固件升级相关接口
    print("\n--- 5. 固件升级相关 ---")

    if upgrade_online_address != "" or upgrade_offline_package != "":
        online = upgrade_online_address != ""
        mode = (pxhandsdk.pxcap3.PxCap3UpgradeMode.kOnline if online
                else pxhandsdk.pxcap3.PxCap3UpgradeMode.kOffline)
        source_set_ret = (cap3.set_upgrade_online_address(upgrade_online_address) if online
                          else cap3.set_upgrade_offline_package(upgrade_offline_package))
        print(f"{'pxcap3_set_upgrade_online_address' if online else 'pxcap3_set_upgrade_offline_package'}: 返回值 {source_set_ret}")
        if source_set_ret != 0:
            _print_last_error(cap3)
            exit_code = 1

        if exit_code == 0:
            package_versions_ret, package_versions = cap3.get_upgrade_package_versions(mode)
            print(f"pxcap3_get_upgrade_package_versions: 返回值 {package_versions_ret}")
            if package_versions_ret != 0:
                _print_last_error(cap3)
                exit_code = 1

        if exit_code == 0:
            progress_state = {"terminal": False, "last": None}

            def upgrade_progress_callback(progress):
                print(
                    "  升级回调: "
                    f"phase={progress.phase} ({int(progress.phase)}), "
                    f"progress={progress.progress}%, overall={progress.overall_progress}%, "
                    f"firmware_name={_describe_upgrade_firmware(progress)}, "
                    f"firmware_index={progress.firmware_index}, firmware_count={progress.firmware_count}, "
                    f"message={progress.message}"
                )
                progress_state["last"] = progress
                progress_state["terminal"] = _is_upgrade_terminal_phase(progress.phase)

            upgrade_ret2 = cap3.upgrade_firmware(mode, upgrade_progress_callback)
            print(f"pxcap3_upgrade_firmware: 返回值 {upgrade_ret2}")
            if upgrade_ret2 != 0:
                _print_last_error(cap3)
                exit_code = 1
            else:
                print("等待升级回调终态（最多 10 分钟）...")
                deadline = time.time() + 600
                while time.time() < deadline and not progress_state["terminal"]:
                    time.sleep(0.1)
                if not progress_state["terminal"]:
                    print("升级等待超时，尝试请求取消...")
                    cancel_ret = cap3.cancel_upgrade()
                    print(f"pxcap3_cancel_upgrade: 返回值 {cancel_ret}")
                    if cancel_ret != 0:
                        _print_last_error(cap3)
                    exit_code = 1
                elif progress_state["last"] is not None:
                    final_progress = progress_state["last"]
                    print("最终升级状态：")
                    print(
                        "  升级回调: "
                        f"phase={final_progress.phase} ({int(final_progress.phase)}), "
                        f"progress={final_progress.progress}%, overall={final_progress.overall_progress}%, "
                        f"firmware_name={_describe_upgrade_firmware(final_progress)}, "
                        f"firmware_index={final_progress.firmware_index}, firmware_count={final_progress.firmware_count}, "
                        f"message={final_progress.message}"
                    )
                    if final_progress.phase != pxhandsdk.pxcap3.PxCap3UpgradePhase.kSuccess:
                        exit_code = 1
                else:
                    exit_code = 1
    else:
        print("提示: 使用 --upgrade-online <HTTP地址> 或 --upgrade-offline <ZIP包路径> 可以演示固件升级功能")

    # 10. 带副作用的操作（仅在 --with-side-effects 时执行）
    if with_side_effects:
        print("\n--- 6. 带副作用的操作 ---")

        # 编码器校准
        encoder_cal_ret = cap3.set_encoder_calibration()
        print(f"pxcap3_set_encoder_calibration: 返回值 {encoder_cal_ret}")

        # 传感器校准
        sensor_cal_ret = cap3.set_sensor_calibration()
        print(f"pxcap3_set_sensor_calibration: 返回值 {sensor_cal_ret}")

        # 触发静态磁检测
        static_set_ret = cap3.set_static_magnet_check()
        print(f"pxcap3_set_static_magnet_check: 返回值 {static_set_ret}")
        print("等待 6 秒检测完成...")
        time.sleep(6)

        # 读取检测结果
        static_ret2, static_result2 = cap3.get_static_magnet_check()
        print(f"pxcap3_get_static_magnet_check(检测后): 返回值 {static_ret2}")
        if static_ret2 == 0:
            print(f"  静态磁检测详情: {_format_static_mag_result(static_result2.status)}")

        # 写入 SN
        set_sn_ret = cap3.set_sn(sn_value)
        print(f"pxcap3_set_sn: 返回值 {set_sn_ret}")

    if exit_code != 0:
        return exit_code

    # 所有维护操作完成后才启动连续采集，运行至用户关闭窗口。
    print("\n--- 7. 持续采集与实时显示 ---")
    return CollectionWindow(cap3, args.serial_port, args.frequency).run()


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except KeyboardInterrupt:
        print("\n程序已中止。", file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        print(e, file=sys.stderr)
        sys.exit(2)
