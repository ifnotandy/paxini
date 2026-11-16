"""PXCap3 相机的 V4L2 控制。直接使用驱动单位，避免 OpenCV 后端的数值映射。"""

from __future__ import annotations

import errno
import fcntl
import os
import struct
from dataclasses import dataclass


QUERY_FORMAT = "=II32siiiiIII"
QUERY_CTRL = (3 << 30) | (struct.calcsize(QUERY_FORMAT) << 16) | (ord("V") << 8) | 36
GET_CTRL = (3 << 30) | (8 << 16) | (ord("V") << 8) | 27
SET_CTRL = (3 << 30) | (8 << 16) | (ord("V") << 8) | 28
QUERY_MENU = (3 << 30) | (44 << 16) | (ord("V") << 8) | 37
UNWRITABLE_FLAGS = 0x01 | 0x02 | 0x04 | 0x10  # disabled / grabbed / read-only / inactive

# 顺序也作为界面滑块的顺序。
SLIDER_SPECS = (
    ("brightness", "亮度", 0x00980900),
    ("contrast", "对比度", 0x00980901),
    ("saturation", "饱和度", 0x00980902),
    ("gain", "增益", 0x00980913),
    ("gamma", "Gamma", 0x00980910),
    ("sharpness", "锐度", 0x0098091B),
    ("white_balance", "色温", 0x0098091A),
    ("exposure", "曝光", 0x009A0902),
)
AUTO_SPECS = (
    ("auto_exposure", "自动曝光", 0x009A0901),
    ("auto_white_balance", "自动白平衡", 0x0098090C),
)
CONTROL_SPECS = SLIDER_SPECS + AUTO_SPECS


@dataclass(frozen=True)
class CameraControl:
    key: str
    label: str
    control_id: int
    minimum: int
    maximum: int
    step: int
    default: int
    value: int
    flags: int
    choices: tuple[tuple[int, str], ...] = ()

    @property
    def writable(self) -> bool:
        return not self.flags & UNWRITABLE_FLAGS

    @property
    def manual_value(self) -> int:
        return 1 if self.key == "auto_exposure" else 0

    @property
    def automatic_value(self) -> int | None:
        if self.key == "auto_white_balance":
            return 1
        options = dict(self.choices)
        return next((value for value in (3, 0) if value in options), None)

    def format_value(self, value: int | None = None) -> str:
        value = self.value if value is None else value
        if self.key == "exposure":
            return f"{value / 10:.1f} ms"
        if self.key == "white_balance":
            return f"{value} K"
        return str(value)


class V4L2Controls:
    def __init__(self, device_path: str):
        self.fd = os.open(device_path, os.O_RDWR | os.O_NONBLOCK)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def query(self, key: str) -> CameraControl | None:
        _, label, control_id = next(spec for spec in CONTROL_SPECS if spec[0] == key)
        buffer = bytearray(struct.pack(QUERY_FORMAT, control_id, 0, b"", 0, 0, 0, 0, 0, 0, 0))
        try:
            fcntl.ioctl(self.fd, QUERY_CTRL, buffer, True)
        except OSError as exc:
            if exc.errno == errno.EINVAL:
                return None
            raise
        _, kind, _, minimum, maximum, step, default, flags, _, _ = struct.unpack(QUERY_FORMAT, buffer)
        if flags & 1:
            return None
        current = bytearray(struct.pack("=Ii", control_id, 0))
        fcntl.ioctl(self.fd, GET_CTRL, current, True)
        value = struct.unpack("=Ii", current)[1]
        choices = []
        if kind == 3:
            for index in range(minimum, maximum + 1):
                menu = bytearray(struct.pack("=II32sI", control_id, index, b"", 0))
                try:
                    fcntl.ioctl(self.fd, QUERY_MENU, menu, True)
                except OSError as exc:
                    if exc.errno == errno.EINVAL:
                        continue
                    raise
                name = struct.unpack("=II32sI", menu)[2].split(b"\0", 1)[0].decode(errors="replace")
                choices.append((index, name))
        return CameraControl(key, label, control_id, minimum, maximum, max(1, step),
                             default, value, flags, tuple(choices))

    def read_all(self) -> dict[str, CameraControl]:
        result = {}
        for key, _, _ in CONTROL_SPECS:
            control = self.query(key)
            if control is not None:
                result[key] = control
        return result

    def set_value(self, key: str, value: int):
        control = self.query(key)
        if control is None:
            raise ValueError(f"相机不支持参数 {key}")
        if not control.writable:
            raise ValueError(f"{control.label}当前不可调；请先关闭对应的自动模式。")
        if not control.minimum <= value <= control.maximum:
            raise ValueError(f"{control.label}超出范围 {control.minimum}~{control.maximum}")
        if control.choices:
            if value not in dict(control.choices):
                raise ValueError(f"{control.label}不支持模式 {value}")
        elif (value - control.minimum) % control.step:
            raise ValueError(f"{control.label}的调节步长必须为 {control.step}")
        fcntl.ioctl(self.fd, SET_CTRL, bytearray(struct.pack("=Ii", control.control_id, value)), True)
        actual = self.query(key)
        if actual is None or actual.value != value:
            raise RuntimeError(f"{control.label}设置未生效，请求 {value}，回读 {actual.value if actual else '不可用'}")

    def restore(self, initial: dict[str, CameraControl]):
        # 先进入手动模式，恢复手动值，最后恢复原来的自动模式。
        for auto_key, manual_key in (("auto_exposure", "exposure"),
                                     ("auto_white_balance", "white_balance")):
            if auto_key in initial and manual_key in initial:
                self.set_value(auto_key, initial[auto_key].manual_value)
        for key, _, _ in SLIDER_SPECS:
            if key in initial:
                self.set_value(key, initial[key].value)
        for key, _, _ in AUTO_SPECS:
            if key in initial:
                self.set_value(key, initial[key].value)
