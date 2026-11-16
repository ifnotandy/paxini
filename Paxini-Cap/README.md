# Paxini-Cap

PXCap III 数据手套 + 双路相机 + Quest 3 的同步采集工程，对应
`Paxini-Dex`（PXDex 执行器）那一套。区别是这里没有电机、不产生任何运动，
输出是单个 HDF5。

```text
Paxini-Cap/
├── Cap_Quest/          # 主程序（对应 Dex_Quest）
│   ├── collect_cap.py      # 主程序：参数、就绪等待、HDF5、manifest（不做对齐）
│   ├── camera_ffmpeg.py    # ffmpeg 直采 V4L2 + ffprobe 回填时间戳
│   ├── quest3.py           # Quest 3 ADB/SDK 封装；样本与四个 SDK 时间戳在这里采
│   ├── selftest.py         # 不接硬件的自检，102 项
│   ├── visualize.py        # 回放采集结果：Tk 窗口，或 --html 导出浏览器版
│   ├── live_view.py        # 采集时的实时窗口（独立子进程，不占采集主循环）
│   ├── hand_calib.py       # 手套零点标定：SDK 标零/静态磁检测 + 闸门 + 复读验证
│   ├── cap_config.json     # 可选配置（存在即加载，命令行优先）
│   └── README.md           # 怎么用：参数表、输出、回放、实时视图、FAQ、验证状态
├── DOC/                # 文档（格式说明、时钟说明、待验证清单、标定）
│   ├── README.md           # 文档索引 + 阅读顺序 + 维护约定
│   ├── HDF5结构说明.md     # 逐字段说明 + 完整目录树（data.h5 的唯一权威文档）
│   ├── 时间戳说明.md       # 三个来源的时钟域、换算公式、四个坑（先读这份）
│   ├── 待验证清单.md       # 真机/Quest 待验证项 + 已决定不做的理由 + 标定（G 组）
│   ├── 标定.md             # 两类标定：手套零点（已实现）+ 相机标定（未实现）
│   └── figures/            # 时间戳图解（SVG，供 HDF5结构说明 / 时间戳说明 引用）
├── calib/              # 标定留证：hand/ 下手套标零报告（相机标定结果将来的落点）
├── py_demo/            # 独立小脚本，不参与主流程，用来确认「连得上、采得到」
│   ├── test.py             # 最小连通性：SDK 版本、SN、故障、一次读数
│   ├── stream_demo.py      # 连续采集，打印帧率与帧间隔分布
│   └── camera.py           # 单路相机录 N 秒，ffprobe 回读；复用 camera_ffmpeg.py
├── sdk/                # deb 包 + SHA256SUMS + 安装说明（二进制不入库）
│   ├── xrobotoolkit/       # Quest 端 APK
│   └── ffmpeg-win/         # Windows 版 ffmpeg.exe（camera_backend: wsl 用）
├── requirements.txt
└── README.md
```

## 0. 接手必读

**当前状态**：真机（手套 + 双路相机）已跑通，但 **Quest 整组未测、标定功能不存在**
——这两块补上之前，跨源对齐与任何"距离 / 位置 / 三维"的结论都不成立。逐项状态见
§6 与 [`DOC/待验证清单.md`](DOC/待验证清单.md)。102 项 `selftest.py` 只覆盖纯逻辑
（帧解析、行轴、配置、视图挂钩）；下游 `Paxini-Align/` 还没开工，仓库里没有配对代码。

### 0.1 不能违反的约定

1. **采集端不做对齐。** 三条行轴各写各的，配对属于下游。曾经有过一版对齐代码，
   已整体删除，别加回来。
2. **只记 SDK 给的时间戳。** `cap/` 的 unix + monotonic 由 pxhandsdk 帧结构自带；
   `quest/` 的四个由 `quest3.collect_sample` 取。不要补主机侧读数。
3. **分布力的 119 是坑位数，不是点数。** 每槽位按 `distribute_counts` 读，
   该值等于真正写进数组的点数。
4. **回调线程只拷贝。** `snapshot_cap_frame` 跑在 SDK 线程里，只做 numpy 拷贝入队；
   写盘全部在主线程 `pump_and_record`。
5. **`quest3.py` 已与 `Dex_Quest/quest3.py` 分叉。** 差异只在 `collect_sample`
   和模块 docstring，不要再追求两份一致。
6. **`DOC/HDF5结构说明.md` 被用户手动编辑过**，只能定点修改，禁止整份重写。
7. **改 `cap/` 或 `quest/` 的布局必须升 `format_version`**（当前 `4`），并在
   `HDF5结构说明.md` 的版本变更表里写清改了什么。下游按版本号判断怎么读。
8. **WSL 里相机必须用 `camera_backend: wsl`。** usbipd 转发 UVC 会丢等时包，
   720p 从 30 fps 掉到 5 fps（驱动日志刷 `Dropping payload (out of sync)`）。
   这不是参数问题，调分辨率、关厂商软件、改曝光都无效。

### 0.2 代码地图

| 想改什么 | 改哪里 |
|---|---|
| 增删 HDF5 字段 | `collect_cap.py` 的 `CAP_DATASETS` / `_quest_layout()`，再同步文档和 selftest |
| 加命令行参数 | `build_parser()` + `cap_config.json` + `Cap_Quest/README.md` 参数表 |
| 回放窗口 / HTML | `visualize.py`（`collect_payload` 读+抽稀，`run_window`/`write_viewer` 两个出口） |
| 采集时实时视图 | `live_view.py`：`HtmlLiveView`（fork 子进程 + 队列，WSL 默认）／`LiveView`（fork 子进程的 Tk 窗口，原生 Linux 默认） |
| 视频采集与 pts 回填 | `camera_ffmpeg.py` |
| Quest 连接与样本读取 | `quest3.py` |
| 采集主循环节拍 | `collect_cap.py` 的 `pump_and_record` |
| 手套零点标定 / 静态磁检测 | `hand_calib.py`（闸门在 `check_*_gate`，判据见 `DOC/标定.md` §1） |

数据写入是批量的：`Hdf5Recorder.write_cap` 攒满 `FLUSH_ROWS = 100`（120 Hz 下约
0.83 s）才 `resize` + 批量赋值，`file.flush()` 只在 `close()` 时调。所以采集中途
读 `data.h5` 会看到一跳一跳的数据，实时视图因此只读内存、不读文件。

### 0.3 环境坑（都踩过）

- **Git Bash 会把 `/` 开头的参数改写成 Windows 路径**：调 WSL 时加
  `MSYS_NO_PATHCONV=1`，否则 `/mnt/c/...` 会变成 `G:/WorkBuddy/...`。
- **`sudo` 要密码**，本机没配免密。`wsl.exe` 里用 `echo <pw> | sudo -S`。
- **matplotlib 只认 `Noto Sans CJK JP`**，没有 Mono 版。等宽中文会整片变方框，
  表格类布局别用 `family="monospace"`。
- **Write 工具产出 CRLF**，仓库其余文件都是 LF，提交前统一一次。
- **WSL 的 `/tmp` 和 Windows 临时目录不是一回事**，跨端传文件走 `/mnt/c/...`。
- 没有 `python3.10-tk` 时开不了窗口，用 `visualize.py --html`。

### 0.4 改完必须过的三关

```bash
cd Cap_Quest
python3.10 -m py_compile *.py
python3.10 -m pyflakes *.py
python3.10 selftest.py          # 应为 102 项全过
```

- 改数据结构或行为，**必须加 selftest 用例**，不能只靠手跑
- 改字段，三处同步：`DOC/HDF5结构说明.md`、`Cap_Quest/README.md`、本文件
- 临时脚本用 `_tmp_` 前缀，任务结束删掉
- 用户文档与提交信息用中文；代码注释和 docstring 用英文（与 Dex 保持一致）

## 1. 环境要求

| 项 | 要求 |
|---|---|
| 系统 | WSL Ubuntu 24.04（SDK 标称 22.04，24.04 实测可用） |
| Python | **`python3.10`**。系统 `python3` 是 3.12，此时 `import pxhandsdk` 能过但 `hasattr(pxhandsdk,"PxCap3")` 为 False |
| 手套 SDK | `pxhandsdk 1.0.0.0`，`dpkg -i` 装到 `/usr/lib/python3/pxhandsdk` |
| Python 包 | `numpy` / `h5py` / `xrobotoolkit-sdk`，用 `python3.10 -m pip install --user` |
| 回放 | `matplotlib`（随 requirements 装）；窗口模式还要 `python3.10-tk`，`--html` 不需要 |
| ffmpeg | 需要带 `v4l2` 输入和 `-fps_mode` 的版本（5.0+）。系统没装也没 sudo 权限时，把静态版放到 `~/bin/ffmpeg`（含 `ffprobe`） |
| ffmpeg（Windows） | `sdk/ffmpeg-win/ffmpeg.exe`，只在 `camera_backend: wsl` 时用。不入库，见 [`sdk/README.md`](sdk/README.md) |
| USB 转发 | Windows 侧 `usbipd` 把设备 attach 进 WSL |

不要用 `python3.10 -m venv`：这个环境里 ensurepip 会失败，直接用系统 3.10 加
`--user` 装包。

## 2. 硬件接入

```powershell
usbipd list
usbipd attach --wsl --hardware-id 28e9:018a          # 手套 PXCap III
usbipd attach --wsl --busid <BUSID>                   # 相机 1（首次要 bind）
usbipd attach --wsl --busid <BUSID>                   # 相机 2
```

- 手套串口按 **USB VID:PID `28e9:018a`** 识别，不依赖 `/dev/ttyACM0` 编号
  （重插会变）。程序同时扫 `ttyACM*`、`ttyUSB*` 和 `/dev/serial/by-id`。
- 相机按 `/sys/class/video4linux/video*` 的 `index == 0` 节点发现（UVC 还会
  多一个 metadata 节点），按 `device` 的 realpath 去重、排序后映射 cam0/cam1。
- 两路 UVC 同时走 usbipd 是同步传输，720p30 两路约 20 MB/s，是整条链路最容易
  出问题的地方：先用 `--camera-fps 15` 或 640x480 验证。

## 3. 快速开始

```bash
# 1) 装 SDK（只做一次）
sudo dpkg -i sdk/pxhandsdk_1.0.0.0.ubantu2204_amd64.deb
sudo /usr/share/pxhandsdk/scripts/setup-serial-access.sh
# 重新登录让 dialout 生效

# 2) 装 Python 依赖（只做一次）
python3.10 -m pip install --user -r requirements.txt

# 3) 自检（不接硬件）
cd Cap_Quest && python3.10 selftest.py

# 4) 看设备
export PATH="$HOME/bin:$PATH"
python3.10 collect_cap.py --list-devices

# 5) 采集（没有 Quest 头显时加 --no-quest）
#    采前务必关 NTP：实测 20 秒内墙钟跳了 2201 ms，关掉后为 0
sudo timedatectl set-ntp false
export PATH="$HOME/bin:$PATH"
python3.10 collect_cap.py --cap-side right --pose-side right

# 5b) 采集时实时看（窗口在独立进程，关掉不影响录制）
python3.10 collect_cap.py --cap-side right --pose-side right --view

# 5c) 没有头显时用一键脚本（固定 --no-quest，采完自动核对 manifest）
bash run_capture_noquest.sh -d 60          # 见 Cap_Quest/README.md「一键脚本（无 Quest 版）」

# 6) 回放（没显示器就加 --html 导出浏览器版）
python3.10 visualize.py recordings/data_<日期>_<序号>
```

两种可视化分工：`--view` 是采集时的实时滚动窗口，只有最近几秒；`visualize.py` 是
采完之后对整段数据的回放，可以拖时间轴、导 HTML。

采集中途不要校时，命令见上面第 5 步；跳变的原因与发现方法见
[`DOC/时间戳说明.md`](DOC/时间戳说明.md)。

数据落在 `Cap_Quest/recordings/data_<YYYYMMDD>_<n>/`，详见
[`Cap_Quest/README.md`](Cap_Quest/README.md)。

## 4. 数据与时间戳

**采集工程只存原始数据，不做对齐。** 三条独立行轴，各带自己的时间戳：

- 手套：`cap/`（N 行），每帧有墙钟 + 单调钟两个读数
- Quest：`quest/`（Q 行），每次轮询一行，只存 SDK 给的四个时间戳 + `sequence`
- 相机：`cameras/<id>/timestamp_unix_ns`（M 行），ffmpeg 打的墙钟

配对（谁和谁、什么阈值、要不要插值）属于下游处理工程，采集端不写也不猜。
细节见 `DOC/HDF5结构说明.md`，时钟域与换算见 `DOC/时间戳说明.md`。

## 5. 已知限制

完整清单见 [`Cap_Quest/README.md`](Cap_Quest/README.md) §8，要点：只支持一只手套；
强杀进程可能丢 `manifest.json` 和未 flush 的尾部行（≤100 行）；Matroska 时间基 1 ms、
量化误差 ±0.5 ms；**时间对齐不等于坐标对**——相机标定功能还不存在，四组量的设计见
[`DOC/标定.md`](DOC/标定.md) §2。

## 6. 验证状态

**真机（手套 + 双路相机，2026-11-13）已跑通**：连采 5 次（25~75 s），手套
115.4~118.3 Hz、两路相机恒 30.0 fps、37 个数据集行数零不一致；相机逐帧 pts 无空洞
（`copy` 与 `h264` 双档，后者约 2.2 逻辑核、体积减半），关 NTP 后时钟零跳变，
`verify.row_mismatch` 全部为空。回放（浏览器版 + 实时窗口，"纯数字"口径）与双路视频
面板逐项通过，交互层在 Chromium 里按真实事件实测过；**Tk 回放的交互没人在真窗口里
点过**，且还没有一次真正有接触、有动作、带 Quest 的完整场景。

**不接硬件也过**：`selftest.py` 102 项、`py_compile` / `pyflakes`、`--list-devices`、
无设备退出码 2 且不留目录、假 SDK 驱动真实主循环、ffmpeg 合成源全链路、实时视图
子进程推送 130 µs/帧。

**还没有的**：

- **Quest 整组**（无头显）：C3~C8 未测，其中 C8（`sdk_timestamp_ns` 属于哪个时钟）是
  跨源对齐的前置条件。前置已备齐：PC Service 能起（监听 63901/60061），APK 已入库
  并校验。
- **相机标定**：功能不存在——无代码、无数据、`meta/` 里没有标定出处，四组量的设计见
  [`DOC/标定.md`](DOC/标定.md) §2。在此之前任何"距离 / 位置 / 三维"的结论都没有依据。
  **手套零点标定已实现**（`Cap_Quest/hand_calib.py`，见 [`DOC/标定.md`](DOC/标定.md) §1），
  但还没有真机执行记录（清单 B9）。

**逐项状态、实测值与判据全在 [`DOC/待验证清单.md`](DOC/待验证清单.md)**：
A 相机 / B 手套 / C Quest / D 时间 / E 不做 / F 可视化 / G 标定；G 组不是"待验证"
而是"待实现"（设计见 [`DOC/标定.md`](DOC/标定.md) §2），E 组记录了 7 条
"决定不做/已删除"的理由，避免重复讨论。

## 7. 后续

1. **标定**（优先级最高，因为它是"空间"这件事的前提）：相机内参、双相机外参、
   手套→相机、Quest→相机，四组量的判据与落盘格式写在 [`DOC/标定.md`](DOC/标定.md) §2。
   G1/G2 只需要 mkv 和一块标定板，不依赖手套与 Quest，可以先做。
2. **`Paxini-Align/`**：读 `data.h5` + `cameras/*.mkv`，做跨源配对。阈值、插值、丢帧
   策略由它决定，采集端只保证原始时间戳齐全。时间配对不依赖标定，空间融合依赖。
3. **可视化**：`Cap_Quest/visualize.py` 已经能回放——关节角、10 路合力数值表、
   共享时间轴、空格播放/逐帧/点曲线跳转/滑块/倍速，浏览器版还有悬停读数、保峰抽稀、
   时钟漂移统计、空负载与跳变警告、**双路相机视频面板（按时间轴精确同步）**；
   采集时 `--view` 实时出 4 路关节角与 10 路合力表（纯数字，不画曲线）加**相机关键帧**。
   还缺两块：
   - Quest 轨迹叠加（要 C 组 + G4 标定）
   - 时间质量**曲线图**：现在只有数值（漂移 + >10 ms 跳变次数），画成
     `unix − monotonic` 偏移曲线更直观
