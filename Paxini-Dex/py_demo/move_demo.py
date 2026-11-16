#!/usr/bin/env python3
"""PXDex III 小幅度运动演示（含食指、中指远节独立动作）。

用法：python move_demo.py
动作顺序：回零 -> 位置模式 + 使能 -> 食指/中指整体小角度弯曲
       -> 食指/中指远节独立小角度弯曲 -> 回到张开端 -> 失能断开。

关节命名与角度范围（产品说明书 3.3 节 + URDF + SDK 对应关系）：
  产品命名  SDK 字段          URDF 关节                结构          范围
  FF2      index_proximal    right_index_joint_0     食指 PIP 近节  0..58°
  FF1      index_distal      right_index_joint_1     食指 DIP 远节  0..42°
  MF2      middle_proximal   right_middle_joint_0    中指 PIP 近节  0..58°
  MF1      middle_distal     right_middle_joint_1    中指 DIP 远节  0..42°
0 为张开端，数值越大越弯曲。SDK 角度列表顺序固定为 [FF2, FF1, MF2, MF1]。

运行前请确认：手指全行程内没有人手、线缆和障碍物，且具备急停/断电手段。
"""
from __future__ import annotations

import sys
import time

import pxhandsdk


PORT = "/dev/ttyUSB0"

# 整体弯曲：四路都给小角度，顺序固定为 [FF2, FF1, MF2, MF1]
WHOLE_FINGER_ANGLES = [15.0, 0.0, 15.0, 0.0]
# 远节独立弯曲：近节(FF2/MF2)保持 0，只让远节(FF1/MF1)各动 12°
DISTAL_ONLY_ANGLES = [15.0, 20.0, 15.0, 20.0]
OPEN_ANGLES = [0.0, 0.0, 0.0, 0.0]

HOLD_SECONDS = 3.0

# 四个关节的最大角度限制，顺序固定为 [FF2, FF1, MF2, MF1]。
# 远节(DIP)上限 42°，近节(PIP)上限 58°，下限均为 0°（张开端）。
JOINT_LABELS = ["FF2(食指PIP近节)", "FF1(食指DIP远节)", "MF2(中指PIP近节)", "MF1(中指DIP远节)"]
MAX_ANGLE_DEG = [58.0, 42.0, 58.0, 42.0]
MIN_ANGLE_DEG = [0.0, 0.0, 0.0, 0.0]


def clamp_angles(angles: list[float]) -> list[float]:
    """把四个关节的目标角度截断到各自允许范围内，越界时打印警告。"""
    out = list(angles)
    for i in range(4):
        if out[i] < MIN_ANGLE_DEG[i]:
            print(f"警告: {JOINT_LABELS[i]} 目标 {out[i]} 低于下限 {MIN_ANGLE_DEG[i]}，已截断")
            out[i] = MIN_ANGLE_DEG[i]
        elif out[i] > MAX_ANGLE_DEG[i]:
            print(f"警告: {JOINT_LABELS[i]} 目标 {out[i]} 超过上限 {MAX_ANGLE_DEG[i]}，已截断")
            out[i] = MAX_ANGLE_DEG[i]
    return out


def move_to(hand: pxhandsdk.PxDex3, label: str, angles: list[float], hold: float) -> int:
    """下发一组角度目标，保持 hold 秒后读回实际角度。"""
    angles = clamp_angles(angles)
    print(f"\n[{label}] 目标角度:", angles)
    rc = hand.set_motor_angles(angles)
    print(f"[{label}] 下发:", rc, "" if rc == 0 else hand.get_last_error())
    if rc != 0:
        return rc
    time.sleep(hold)
    rc, current = hand.get_motor_angles()
    if rc == 0:
        print(f"[{label}] 当前角度:",
              current.index_proximal,
              current.index_distal,
              current.middle_proximal,
              current.middle_distal)
    else:
        print(f"[{label}] 读角度失败:", hand.get_last_error())
    return rc


def main() -> int:
    hand = pxhandsdk.PxDex3()

    if hand.connect_device(PORT) != 0:
        print("连接失败:", hand.get_last_error(), file=sys.stderr)
        return 1
    print("连接成功:", PORT)

    rc, version = hand.get_firmware_versions()
    if rc == 0:
        print("主固件版本:", version.firmware_version)

    rc, limits = hand.get_position_limit()
    if rc == 0:
        print("位置限位 min:",
              limits.min_position.index_proximal_pos,
              limits.min_position.index_distal_pos,
              limits.min_position.middle_proximal_pos,
              limits.min_position.middle_distal_pos)
        print("位置限位 max:",
              limits.max_position.index_proximal_pos,
              limits.max_position.index_distal_pos,
              limits.max_position.middle_proximal_pos,
              limits.max_position.middle_distal_pos)

    # 上电后位置/角度控制前先回零（会产生机械运动）。
    print("开始回零（手指会动）...")
    rc = hand.set_magnetic_encoder_zero()
    print("回零:", rc, "" if rc == 0 else hand.get_last_error())
    if rc != 0:
        return 1
    # 回零动作刚结束，留 1 秒稳定，再切模式和下发目标
    time.sleep(1.0)

    print("切换到位置模式:", hand.set_motor_control_mode(
        pxhandsdk.pxdex3.ControlMode.MOTOR_MODE_POSITION))
    print("使能电机:", hand.enable_motor())

    if move_to(hand, "整体小角度弯曲", WHOLE_FINGER_ANGLES, HOLD_SECONDS) != 0:
        return 1

    if move_to(hand, "远节独立小角度弯曲", DISTAL_ONLY_ANGLES, HOLD_SECONDS) != 0:
        return 1

    move_to(hand, "回到张开端", OPEN_ANGLES, HOLD_SECONDS)

    print("失能电机:", hand.disable_motor())
    print("断开连接:", hand.disconnect_device())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
