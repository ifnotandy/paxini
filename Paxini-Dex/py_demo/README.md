# PXDex III Python SDK 快速上手指南

本指南面向第一次接触 PXDex III 三指执行器的同学。跟着下面的步骤，你可以在半小时内完成环境搭建、连接设备、读取状态，并让手指安全地动起来。

目录中的主要入口：

- `test.py`：最小的只读示例，验证连接和状态读取。
- `move_demo.py`：完整的运动演示，包含回零、使能、两段角度运动和断开。
- `camera.py`：可选的双摄像头画面预览工具，与 PXDex 控制无关。
- `Cap_demo/`：zw 提供的 PXCap3 触觉、编码器和双腕部相机示例。

---

## 1. 你需要先准备好什么

### 1.1 硬件

- 一台装有 Ubuntu 22.04（x86_64）的电脑。
- PXDex III 三指执行器，已通过 RS485 转 USB 适配器接到电脑，设备节点通常是 `/dev/ttyUSB0`。
- 执行器已上电。

> 电脑上如果有官方上位机 PXCap-Studio 正在运行，请先关闭它。串口同一时间只能被一个程序占用。

### 1.2 软件

- 系统 Python 3.10（本机是 `/usr/bin/python3`）。
- 已安装官方 SDK：`pxhandsdk`。

如果 SDK 还没装，先从官方渠道获取安装包并放到 `sdk/pxhandsdk/`，然后安装：

```bash
sudo dpkg -i sdk/pxhandsdk/pxhandsdk_1.0.0.0.ubantu2204_amd64.deb
sudo /usr/share/pxhandsdk/scripts/setup-serial-access.sh
```

装完重新登录一次，让串口权限生效。

---

## 2. 准备 Python 虚拟环境

SDK 是通过 dpkg 装到系统 Python 里的，普通虚拟环境默认看不到它。所以要用系统 Python 创建一个带 `--system-site-packages` 的虚拟环境。

首次创建（在 `Paxini` 目录下执行）：

```bash
cd /home/carrie/my_project/Paxini
/usr/bin/python3 -m venv --system-site-packages .venv
```

以后每次使用前，先激活这个环境：

```bash
source /home/carrie/my_project/Paxini/.venv/bin/activate
```

验证 SDK 能否正常导入：

```bash
python -c "import pxhandsdk; print('SDK OK')"
```

看到 `SDK OK` 就说明环境没问题了。

> 注意：不要执行 `pip install pxhandsdk`。这个包不是从 PyPI 安装的，官方手册明确禁止安装同名第三方包。

---

## 3. 目录说明

| 文件 | 作用 | 是否会产生运动 |
|---|---|---|
| `test.py` | 连接设备，读取固件版本和当前位置 | 否 |
| `move_demo.py` | 回零、使能、执行两段角度运动后张开 | 是 |
| `camera.py` | 打开两个摄像头并拼接显示 | 否 |

### PXCap3 示例

`Cap_demo/` 提供 PXCap3 的实时数据采集和双腕部相机预览：

```bash
cd Cap_demo

# 终端 A：触觉、分布力点数、编码器和关节角
python pxcap3_demo.py /dev/ttyACM0 --frequency 100

# 终端 B：枚举并预览两路腕部相机
python camera_preview.py --list
python camera_preview.py
```

`camera_preview_ui.py` 和 `camera_controls.py` 由预览入口自动使用。串口和视频节点
会随设备插拔变化，完整说明见
[Cap_demo/PXCap3_设备启动与程序说明.md](Cap_demo/PXCap3_设备启动与程序说明.md)。

---

## 4. 第一步：先做只读测试

确认串口号。默认是 `/dev/ttyUSB0`，如果不是，先查一下：

```bash
ls /dev/ttyUSB* /dev/ttyACM*
```

然后运行：

```bash
python test.py
```

预期会看到类似输出：

```text
R_PXDex_III_EVT2_V2.0.42.0.4
0 2
```

第一行是固件版本，第二行两个数字是食指指腹、食指指尖的当前位置读数。这一步不驱动手指，只是确认连接和读取都正常。

如果报 `No module named 'pxhandsdk'`，回到第 2 节检查虚拟环境；如果报端口相关的错误，跳到第 8 节常见问题。

---

## 5. 第二步：让手指安全地动一下

运行运动演示：

```bash
python move_demo.py
```

脚本会依次执行：

1. 连接设备；
2. 回零（手指会动到张开端）；
3. 切换到位置模式并使能电机；
4. 整体小角度弯曲；
5. 远节独立小角度弯曲；
6. 回到张开端；
7. 失能并断开。

运行前请务必确认：手指全行程内没有人手、线缆和障碍物，旁边有急停或断电手段。

### 5.1 想改动作幅度

打开 `move_demo.py`，改文件顶部这几个变量即可：

```python
# 顺序固定为 [FF2, FF1, MF2, MF1]
WHOLE_FINGER_ANGLES = [15.0, 0.0, 15.0, 0.0]   # 整体弯曲
DISTAL_ONLY_ANGLES = [15.0, 20.0, 15.0, 20.0]   # 远节弯曲
OPEN_ANGLES = [0.0, 0.0, 0.0, 0.0]              # 张开
```

脚本里已经做了角度上限保护：超过范围的值会被自动截断，并在终端打印警告。

---

## 6. 关节命名与角度范围

PXDex III 的拇指、掌心、掌根是固定结构，食指和中指各有两个活动关节。SDK 里四个通道的固定顺序和官方命名对应关系如下：

| 产品命名 | SDK 字段 | 结构 | 角度范围 |
|---|---|---|---|
| FF2 | `index_proximal` | 食指 PIP 近节 | 0–58° |
| FF1 | `index_distal` | 食指 DIP 远节 | 0–42° |
| MF2 | `middle_proximal` | 中指 PIP 近节 | 0–58° |
| MF1 | `middle_distal` | 中指 DIP 远节 | 0–42° |

角度列表顺序固定为 `[FF2, FF1, MF2, MF1]`，也就是「食指指腹、食指指尖、中指指腹、中指指尖」。0° 是张开端，数值越大手指越弯曲。

---

## 7. 核心代码模式

不管写什么程序，基本的套路都是这六步：

```python
import pxhandsdk

hand = pxhandsdk.PxDex3()

# 1. 连接
if hand.connect_device("/dev/ttyUSB0") != 0:
    raise SystemExit(f"连接失败: {hand.get_last_error()}")

# 2. 读状态（版本、限位、位置等）
rc, version = hand.get_firmware_versions()
rc, limits = hand.get_position_limit()

# 3. 回零（会产生运动）
hand.set_magnetic_encoder_zero()

# 4. 切模式 + 使能
hand.set_motor_control_mode(pxhandsdk.pxdex3.ControlMode.MOTOR_MODE_POSITION)
hand.enable_motor()

# 5. 下发角度目标（顺序 [FF2, FF1, MF2, MF1]）
hand.set_motor_angles([15.0, 8.0, 15.0, 8.0])

# 6. 失能 + 断开
hand.disable_motor()
hand.disconnect_device()
```

几个要点：

- 每个接口返回 `0` 表示成功，非 `0` 表示失败。
- 失败后用 `hand.get_last_error()` 拿错误码和错误信息。
- `set_motor_angles()` 是「下达目标后立即返回」，返回 0 只代表目标被接受，不代表手指已经到位；要读 `get_motor_angles()` 反馈确认实际角度。
- 每次设备重新上电并连接后，运动前建议先回零。

---

## 8. 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| `No module named 'pxhandsdk'` | 用了 conda 或普通 venv，看不到系统包 | 用 `/usr/bin/python3` 或重建带 `--system-site-packages` 的 venv |
| 报端口不存在、被占用（错误码 100/101/102） | 串口号不对或上位机占用 | 先关 PXCap-Studio，`ls /dev/ttyUSB*` 核对端口 |
| 无权限打开串口 | 当前用户不在 `dialout` 组 | 执行 `sudo /usr/share/pxhandsdk/scripts/setup-serial-access.sh` 后重新登录 |
| 回零或下发角度返回非 0 | 限位、使能状态或机械问题 | 看 `get_last_error()` 的错误码，先确认机械无阻挡、电机已使能 |
| 手指不动但返回 0 | 目标已到当前位置，或保持时间太短 | 把目标改大、观察读回的角度变化 |

常用错误码速查：`118` 目标超出限位，`122` 未连接，`166` 回零失败，`164` 目标写入失败，`173` 电机未使能，`174` 设备未使能。

---

## 9. 可选：双摄像头预览

`camera.py` 是独立的画面预览工具，默认打开 `/dev/video4` 和 `/dev/video8` 两个摄像头，左右拼接显示，按 Q 退出。

它需要 OpenCV，先在虚拟环境里装：

```bash
pip install opencv-python
```

然后核对摄像头设备号并运行：

```bash
ls /dev/video*
python camera.py
```

如果摄像头节点不是 `video4`、`video8`，修改 `camera.py` 顶部的 `CAMERA_1`、`CAMERA_2`。

---

## 10. 更多资料

- SDK 手册：`/usr/share/doc/pxhandsdk/pxdex3使用手册.md`
- 官方 Python 示例：`/usr/share/pxhandsdk/examples/amd64/python/pxdex3_demo.py`
- 官方 C++ 示例：`/usr/share/pxhandsdk/examples/amd64/C++/pxdex3_demo.cpp`
