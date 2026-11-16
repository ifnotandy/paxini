#!/usr/bin/env python3
"""自动选择 PXCap3 的两路腕部相机，在同一个窗口并排实时显示。

运行：/usr/bin/python3 camera_preview.py
列出设备：/usr/bin/python3 camera_preview.py --list
每路画面下方提供独立的相机参数滑块，设置后读取驱动返回值。
依赖：系统 Python 的 python3-opencv、python3-tk。
按 Q、Esc、Ctrl+C 或关闭窗口退出。
仅选择 PXCap3 相机，不自动使用笔记本摄像头；不占用手套串口，不保存图像。
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock, Thread

from camera_controls import V4L2Controls

# 与 PXCap Studio 的 CAMERA_PRODUCT_IDS_BY_HANDEDNESS 一致。
# 通道分别表示大拇指侧、非大拇指侧，不是左手/右手两只手套。
CAMERA_VENDOR_ID = "0c45"
HAND_PRODUCT_IDS = {"left": ("c000", "cc00"), "right": ("000c", "00cc")}
CAMERA_LABELS = ("大拇指侧腕部相机", "非大拇指侧腕部相机")


@dataclass(frozen=True)
class CameraDevice:
    path: str
    name: str
    vendor_id: str
    product_id: str
    index: int
    usb_path: str


def discover_cameras(sysfs_root: Path = Path("/sys/class/video4linux")) -> list[CameraDevice]:
    devices = []
    for node in sorted(sysfs_root.glob("video*")):
        try:
            device_path = (node / "device").resolve()
            usb = next((parent for parent in (device_path, *device_path.parents)
                        if (parent / "idVendor").is_file()), None)
            if usb is None:
                continue
            devices.append(CameraDevice(
                path=f"/dev/{node.name}",
                name=(node / "name").read_text().strip(),
                vendor_id=(usb / "idVendor").read_text().strip().lower(),
                product_id=(usb / "idProduct").read_text().strip().lower(),
                index=int((node / "index").read_text().strip()),
                usb_path=str(usb),
            ))
        except (OSError, ValueError):
            # 枚举期间设备被移除时忽略该条目。
            continue
    return devices


def _device_summary(devices: list[CameraDevice]) -> str:
    if not devices:
        return "未发现 USB 视频设备"
    return "\n".join(
        f"  {device.path}: {device.vendor_id}:{device.product_id}, "
        f"index={device.index}, {device.name}"
        for device in devices
    )


def select_cameras(devices: list[CameraDevice], args) -> tuple[str, tuple[CameraDevice, CameraDevice]]:
    if args.thumb_device or args.other_device:
        if not (args.thumb_device and args.other_device):
            raise RuntimeError("手动指定时，需要同时提供 --thumb-device 和 --other-device。")
        selected = []
        for supplied in (args.thumb_device, args.other_device):
            resolved = str(Path(supplied).resolve())
            match = next((device for device in devices if device.path == resolved), None)
            if match is None:
                raise RuntimeError(f"{supplied} 不是当前可用的视频节点。\n{_device_summary(devices)}")
            if match.index != 0:
                raise RuntimeError(f"{supplied} 不是该相机的 index=0 图像接口，请用 --list 查看。")
            selected.append(match)
        if selected[0].usb_path == selected[1].usb_path:
            raise RuntimeError("两路必须来自两个不同的腕部相机，不能选择同一相机的两个接口。")
        hand = next((hand for hand, ids in HAND_PRODUCT_IDS.items()
                     if all(device.vendor_id == CAMERA_VENDOR_ID for device in selected)
                     and tuple(device.product_id for device in selected) == ids), None)
        if hand is None or args.hand not in ("auto", hand):
            raise RuntimeError("指定的两路不符合所选 PXCap3 手套的大拇指侧/非大拇指侧 USB 标识。")
        return hand, (selected[0], selected[1])

    hands = HAND_PRODUCT_IDS if args.hand == "auto" else {args.hand: HAND_PRODUCT_IDS[args.hand]}
    pairs = []
    for hand, product_ids in hands.items():
        candidates = [
            [device for device in devices if device.vendor_id == CAMERA_VENDOR_ID
             and device.product_id == product_id and device.index == 0]
            for product_id in product_ids
        ]
        if all(len(group) == 1 for group in candidates):
            pairs.append((hand, (candidates[0][0], candidates[1][0])))
        elif all(candidates):
            raise RuntimeError("发现多组相同 USB 标识的腕部相机，请手动指定两路视频节点。")
    if len(pairs) == 1:
        return pairs[0]
    if len(pairs) > 1:
        raise RuntimeError("左右手套的两组相机均已连接，请通过 --hand left 或 --hand right 选择。")
    raise RuntimeError(
        "未找到完整的 PXCap3 双腕部相机。请检查相机 USB 连接，并使用 --list 查看。\n"
        f"当前视频节点：\n{_device_summary(devices)}"
    )


class CameraStream(Thread):
    """每路独立读取，只保留最新帧；相机句柄由所属线程创建和释放。"""

    def __init__(self, cv2, device: CameraDevice, label: str, args):
        super().__init__(name=f"camera-{Path(device.path).name}", daemon=True)
        self.cv2, self.device, self.label, self.args = cv2, device, label, args
        self.stop_requested = Event()
        self.lock = Lock()
        self.frame = None
        self.frame_count = 0
        self.last_received = 0.0
        self.error = ""
        self.controls = {}
        self.initial_controls = {}
        self.controls_ready = False
        self.control_error = ""
        self.control_message = "正在读取相机参数..."
        self.applied_revision = 0
        self.request_revision = 0
        self.pending_changes = {}
        self.pending_restore = False
        self._last_controls_poll = 0.0

    def snapshot(self):
        with self.lock:
            return self.frame, self.frame_count, self.last_received, self.error

    def control_snapshot(self):
        with self.lock:
            return (self.controls.copy(), self.applied_revision, self.control_message,
                    self.control_error, self.controls_ready)

    def request_control(self, key: str, value: int) -> int:
        with self.lock:
            self.pending_changes[key] = value
            self.request_revision += 1
            return self.request_revision

    def request_restore(self) -> int:
        with self.lock:
            self.pending_changes.clear()
            self.pending_restore = True
            self.request_revision += 1
            return self.request_revision

    def _service_controls(self, controller):
        with self.lock:
            changes, restore = self.pending_changes, self.pending_restore
            self.pending_changes, self.pending_restore = {}, False
            revision = self.request_revision
        if not changes and not restore and time.monotonic() - self._last_controls_poll < 1.0:
            return
        message, error = "", ""
        try:
            if restore:
                controller.restore(self.initial_controls)
                message = "已恢复启动值"
            # 自动模式先应用，依赖的手动参数随后再写入。
            for key in sorted(changes, key=lambda key: not key.startswith("auto_")):
                controller.set_value(key, changes[key])
            if changes:
                message = "设置已生效（已回读确认）"
        except Exception as exc:
            error = f"参数设置失败：{exc}"
        try:
            actual = controller.read_all()
            with self.lock:
                self.controls = actual
        except Exception as exc:
            error = f"参数读取失败：{exc}"
        self._last_controls_poll = time.monotonic()
        with self.lock:
            if changes or restore:
                self.applied_revision = revision
                self.control_error = error
                self.control_message = message
            elif error:
                self.control_error = error

    def run(self):
        cv2, args = self.cv2, self.args
        capture = None
        controller = None
        try:
            capture = cv2.VideoCapture(self.device.path, cv2.CAP_V4L2)
            if not capture.isOpened():
                raise RuntimeError("无法打开，请确认访问权限，并关闭占用该相机的上位机预览。")
            requests = [
                (cv2.CAP_PROP_FRAME_WIDTH, args.width, "宽度"),
                (cv2.CAP_PROP_FRAME_HEIGHT, args.height, "高度"),
                (cv2.CAP_PROP_FPS, args.fps, "帧率"),
            ]
            if args.pixel_format != "auto":
                requests.insert(0, (cv2.CAP_PROP_FOURCC,
                                    cv2.VideoWriter_fourcc(*args.pixel_format), "输入格式"))
            for property_id, value, label in requests:
                if not capture.set(property_id, value):
                    print(f"{self.label}: 设备未接受请求的{label}，使用实际协商值。", file=sys.stderr)
            print(
                f"{self.label}: {self.device.path}, "
                f"{capture.get(cv2.CAP_PROP_FRAME_WIDTH):.0f}x"
                f"{capture.get(cv2.CAP_PROP_FRAME_HEIGHT):.0f}, "
                f"帧率参数 {capture.get(cv2.CAP_PROP_FPS):g} fps",
                flush=True,
            )
            try:
                controller = V4L2Controls(self.device.path)
                initial = controller.read_all()
                with self.lock:
                    self.controls = initial
                    self.initial_controls = initial.copy()
                    self.control_message = "参数已读取，拖动滑块即可调节"
            except Exception as exc:
                if controller is not None:
                    controller.close()
                    controller = None
                with self.lock:
                    self.control_error = f"无法读取调节参数：{exc}"
            finally:
                with self.lock:
                    self.controls_ready = True
            deadline = time.monotonic() + 8.0
            while not self.stop_requested.is_set():
                if controller is not None:
                    self._service_controls(controller)
                ok, frame = capture.read()
                if self.stop_requested.is_set():
                    break
                if ok and frame is not None:
                    now = time.monotonic()
                    with self.lock:
                        self.frame = frame
                        self.frame_count += 1
                        self.last_received = now
                        first_frame = self.frame_count == 1
                    if first_frame:
                        print(f"{self.label}: 已收到首帧 {frame.shape[1]}x{frame.shape[0]}", flush=True)
                    deadline = now + 5.0
                elif time.monotonic() >= deadline:
                    raise RuntimeError("持续读取不到有效画面，请检查 USB 连接、带宽或尝试降低分辨率。")
                else:
                    self.stop_requested.wait(0.02)
        except Exception as exc:
            with self.lock:
                self.error = f"{self.label} ({self.device.path}): {exc}"
        finally:
            try:
                if controller is not None:
                    controller.close()
            finally:
                if capture is not None:
                    capture.release()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PXCap3 双腕部实时预览，每路提供独立的相机参数滑块")
    parser.add_argument("--list", action="store_true", help="仅列出视频节点及 USB 标识，不打开相机")
    parser.add_argument("--hand", choices=("auto", "left", "right"), default="auto", help="选择手套，默认自动识别")
    parser.add_argument("--thumb-device", help="手动指定大拇指侧腕部相机，例如 /dev/video2")
    parser.add_argument("--other-device", help="手动指定非大拇指侧腕部相机，例如 /dev/video6")
    parser.add_argument("--width", type=int, default=1280, help="每路采集宽度，默认 1280")
    parser.add_argument("--height", type=int, default=720, help="每路采集高度，默认 720")
    parser.add_argument("--fps", type=int, default=30, help="每路目标采集帧率，默认 30")
    parser.add_argument("--view-width", type=int, default=640, help="窗口中每路画面的显示宽度，默认 640")
    parser.add_argument("--pixel-format", choices=("MJPG", "YUYV", "auto"), default="MJPG")
    args = parser.parse_args(argv)
    if min(args.width, args.height, args.fps, args.view_width) <= 0:
        parser.error("分辨率、显示宽度和帧率必须为正整数")
    devices = discover_cameras()
    if args.list:
        print(_device_summary(devices))
        return 0
    hand, selected = select_cameras(devices, args)
    print(f"已选择{'左' if hand == 'left' else '右'}手套的双腕部相机：")
    for label, device in zip(CAMERA_LABELS, selected):
        print(f"  {label}: {device.path} ({device.vendor_id}:{device.product_id})")

    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("缺少 OpenCV，请为系统 Python 安装 python3-opencv。") from exc
    from camera_preview_ui import PreviewWindow

    streams = [CameraStream(cv2, device, label, args) for device, label in zip(selected, CAMERA_LABELS)]
    window = None
    try:
        window = PreviewWindow(cv2, hand, streams, args)
        for stream in streams:
            stream.start()
        print("双路实时预览已启动；每路画面下方有独立滑块和自动模式开关。", flush=True)
        return window.run()
    finally:
        for stream in streams:
            stream.stop_requested.set()
        deadline = time.monotonic() + 3.0
        for stream in streams:
            if stream.ident is not None:
                stream.join(timeout=max(0.0, deadline - time.monotonic()))
                if stream.is_alive():
                    print(f"{stream.label}: 驱动仍在等待返回，进程退出时将释放设备。", file=sys.stderr)
            print(f"{stream.label}: 已采集 {stream.snapshot()[1]} 帧。")
        if window is not None:
            window.destroy()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已停止双路预览。")
        sys.exit(130)
    except Exception as exc:
        print(f"摄像头读取失败：{exc}", file=sys.stderr)
        sys.exit(1)
