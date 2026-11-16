# PXDex III + Quest 3 + 相机同步采集

一只 PXDex 有一个串口、两路掌侧相机（`cam0`/`cam1`）。默认同时采集同一只
执行器的两路图像，并与 PXDex、Quest 6D pose 写入同一份时间对齐记录。

## 1. 快速开始

```bash
cd /home/carrie/my_project/Paxini
source .venv/bin/activate
cd Dex_Quest
```

默认会自动发现唯一的 PXDex 串口，以及名称为 `MASTER Camera` 的两路相机：

```text
当前 PXDex 手别 + cam0 + cam1
```

例如右手执行器输出到 `right_cam0/` 和 `right_cam1/`。
换机器后可先检查：

```bash
python collect_hand_pose.py --list-devices
```

发现正常时直接运行：

```bash
python collect_hand_pose.py \
  --pxdex-side right \
  --pose-source controller \
  --pose-side right
```

右手执行器可省略 `--pxdex-side right`；左手执行器使用
`--pxdex-side left`。

测试阶段只开同一只手的一路相机：

```bash
# 只采集 cam0
python collect_hand_pose.py \
  --pxdex-side right \
  --pose-source controller \
  --pose-side right \
  --camera-mode cam0

# 只采集 cam1
python collect_hand_pose.py \
  --pxdex-side right \
  --pose-source controller \
  --pose-side right \
  --camera-mode cam1
```

完整模式：

```bash
python collect_hand_pose.py --camera-mode both
python collect_hand_pose.py --camera-mode none
```

`--pxdex-side` 表示执行器手别，`cam0/cam1` 表示同一只执行器上的两个相机，
不能混为左右手。

串口自动发现只在系统恰好存在一个 USB/ACM 串口时生效；有多个串口时程序会列出
候选并要求用 `--hand-port` 明确指定，避免误连其他设备。

摄像头按 sysfs 中的设备名和 `video-index0` 自动发现；发现的两路按设备路径
排序映射为 `cam0/cam1`。每次启动都会重新发现，不依赖固定的 `/dev/videoN`
或主机 USB 端口。

当前程序管理一只 PXDex 手。如果将来同时接入左右两只执行器，应有四路图像：
`left_cam0/left_cam1/right_cam0/right_cam1`，并需要两个独立的 PXDex 串口与
采集回调。

只有自动发现失败时才需要手动覆盖；请使用 `--list-devices` 输出的真实路径，
不要复制占位符。例如当前机器两路节点可能是 `/dev/video4` 和 `/dev/video8`。

启动顺序：

```text
ADB 启动 Quest APK
→ Quest 通过 Wi-Fi 向 PC Service 发送数据
→ 收到连续 3 帧有效 Quest pose
→ 所选相机连续出图
→ 启动 PXDex 连续采集
→ PXDex、Quest、相机图像分别按主机单调时钟最近邻对齐
```

USB ADB 可以一直保留；Quest 跟踪数据仍需要局域网。PC Service 未运行时可以
增加 `--start-pc-service`。

## 2. 同步与频率

- PXDex 只有一个串口、一个 `PXDex3` handle 和一个连续采集回调流。
- 每个 PXDex 回调帧同时包含四关节、电流、温度、10 路合力、444 点分布力、
  编码器和故障，不是左右手或左右通道分别输出。
- PXDex 默认 `30 Hz`，是该完整帧回调目标频率。
- Quest 默认轮询 `120 Hz`，但每个 PXDex 帧只保存最近的一帧 Quest 样本。
- 相机约 `30 fps`；每个 PXDex 帧分别匹配所选相机最近的一帧图像。
- 默认最大时间偏差都是 `30 ms`，只做最近邻选择，不做平均或插值。
- PXDex 硬件标称触觉 `83.3 Hz`、编码器 `78 Hz`；30 Hz 回调不会保存全部内部
  高频样本，当前公开接口也未提供这些内部样本的独立时间戳。

`sync.aligned` 表示 Quest 已对齐，`sync.cameras_aligned` 表示所选相机全部
对齐，`sync.all_sources_aligned` 表示 PXDex、Quest 和当前启用相机全部对齐。

## 3. Quest 6D pose

`pose_6d` 顺序为：

```text
x, y, z, qx, qy, qz, qw
```

单位为米，四元数为 `xyzw`。它保留 XRoboToolKit 的 Unity tracking-origin
坐标：`+X` 右、`+Y` 上、`+Z` 前，左手系。它不是 PXDex、机械臂或电脑坐标系；
时间对齐不代表空间外参已经标定。

默认 `--pose-source controller --pose-side right`。`quest` 字段仍完整保存头显、
左右手柄和左右手；`pose_6d` 只是选定的单一主位姿。

## 4. 输出文件

```text
recordings/data_<YYYYMMDD>_<当天序号>/
├── aligned.jsonl
├── hand_pose.csv
├── cameras/right_cam0/*.jpg
├── cameras/right_cam1/*.jpg
└── manifest.json
```

若使用左手执行器，目录为 `left_cam0/` 和 `left_cam1/`。

`aligned.jsonl` 每行一帧，主要字段：

| 字段 | 含义 |
|---|---|
| `sync` | 各时间戳、对齐偏差、对齐状态 |
| `pose_6d` | 选定的 Quest 6D pose |
| `hand` | 完整 PXDex 数据 |
| `quest` | 对应时刻的完整 Quest 样本 |
| `cameras` | 每路相机键及其图像路径、时间戳和匹配信息 |
| `pxdex_hand_side` | 当前 PXDex 手别：`left` 或 `right` |

PXDex `hand` 包含四关节角度、电机位置、编码器、电流、温度、10 路合力、444
点分布力和故障信息。图像只在匹配成功时保存；完整数据以 `aligned.jsonl` 为准。

`hand_pose.csv` 是只含时间戳、6D pose、四关节和图像路径的摘要。
`manifest.json` 记录设备、频率、帧数、配对率和错误统计。

## 5. 停止

正常停止只按一次 `Ctrl+C`，等待程序保存文件并生成 manifest。最多等待约
10 秒；只有清理卡住时才按第二次，第二次会立即强制退出，可能没有 manifest。

## 6. 安全

程序不会主动回零、切换模式或下发运动目标，但 PXDex SDK 在
`connect_device()` 时会自动使能电机。运行前必须清理机械手运动空间，并保留
急停或断电手段。

## 7. 常见问题

```bash
# 串口被占用
ps -eo pid,ppid,stat,etime,cmd | grep "[c]ollect_hand_pose.py"
sudo fuser -v /dev/ttyUSB0

# 查看相机
v4l2-ctl --list-devices
ls -l /dev/v4l/by-path/

# 查看串口
ls -l /dev/serial/by-id/ /dev/ttyUSB* /dev/ttyACM* 2>/dev/null
```

- 一直“尚未收到 SDK 样本”：检查 Quest Wi-Fi、PC Service、APK 的 Tracking/Send。
- 收到样本但有效 pose 为 0：唤醒手柄，或检查 `--pose-source/--pose-side`。
- 相机打不开：确认节点存在、未被占用，并设置 `--camera-mode cam0|cam1`。
- 只诊断 Quest：运行 `python quest3.py` 或 `python quest3.py --skip-adb`。

完整参数：

```bash
python collect_hand_pose.py --help
```
