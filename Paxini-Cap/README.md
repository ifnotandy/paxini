# Paxini-Cap

PXCap III 数据手套 + 双路相机 + Quest 3 的同步采集工程，对应
`Paxini-Dex`（PXDex 执行器）那一套。区别是这里没有电机、不产生任何运动，
输出是单个 HDF5。

```text
Paxini-Cap/
├── Cap_Quest/          # 主程序（对应 Dex_Quest）；内部结构与命令见其 README
│   ├── capture/            # 采集主程序与设备驱动层
│   ├── calib/              # 手套零点标定
│   ├── latency/            # 相机延迟测量
│   ├── viewer/             # 回放与实时视图
│   ├── selftest/           # 不接硬件的自检，102 项
│   ├── scripts/            # 一键采集脚本
│   ├── recordings/         # 采集输出（data_<日期>_<序号>/）
│   └── cap_config.yaml     # 可选配置（存在即加载，命令行优先）
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

1. **采集端不做对齐。** 三条行轴 `cap/`(N) / `quest/`(Q) / `cameras/*`(M) 互相独立、
   行数永不相等，谁都不能当索引去切另一条。配对属于下游 `Paxini-Align/`。曾经有过
   一版对齐代码，已整体删除，别加回来。
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
8. **WSL 里相机不能走 V4L2。** usbipd 转发 UVC 会丢等时包，720p 从 30 fps 掉到
   5 fps，且与曝光、分辨率、厂商软件都无关。`camera_backend: auto` 已经按运行环境
   判好了（WSL → wsl），除非要强制，不用手工填。两条路径的技术对比见
   [`Cap_Quest/README.md` §6.3](Cap_Quest/README.md)。

### 0.2 代码地图

| 想改什么 | 改哪里 |
|---|---|
| 增删 HDF5 字段 | `collect_cap.py` 的 `CAP_DATASETS` / `_quest_layout()`，再同步文档和 selftest |
| 加命令行参数 | `build_parser()` + `cap_config.yaml` + `Cap_Quest/README.md` 参数表 |
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
python3.10 -m py_compile capture/*.py viewer/*.py selftest/*.py latency/*.py calib/*.py
python3.10 -m pyflakes capture/*.py viewer/*.py selftest/*.py latency/*.py calib/*.py
python3.10 selftest/selftest.py          # 应为 102 项全过
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

## 3. 安装（只做一次）

```bash
sudo dpkg -i sdk/pxhandsdk_1.0.0.0.ubantu2204_amd64.deb
sudo /usr/share/pxhandsdk/scripts/setup-serial-access.sh
# 重新登录让 dialout 生效

python3.10 -m pip install --user -r requirements.txt
```

装完先跑 §0.4 的三关：`selftest` 102 项全过说明环境没问题，此时不需要插硬件。

**验证设备与日常运行不在本文档**：`--list-devices`、采集、`--view`、一键脚本、回放、
参数表全在 [`Cap_Quest/README.md` §1](Cap_Quest/README.md)。采前关 NTP、
`export PATH="$HOME/bin:$PATH"`、没有头显加 `--no-quest` 这三条纪律，在该文件的
【快速开始】与【§2 时间戳】里各有完整说明，这里不再重列。

## 4. 验证状态

**结论：采集链路可用，跨源分析不可用。**

真机（手套 + 双路相机）2026-11-13 已跑通，逐帧无丢帧、关 NTP 后时钟零跳变；不接
硬件时 `selftest` 102 项全过。但三处缺口没补上之前，任何跨源结论都不成立：

| 缺口 | 状态 | 后果 |
|---|---|---|
| Quest 整组 | 无头显，C3~C8 未测 | `sdk_timestamp_ns` 属于哪个时钟未定，跨源对齐缺前置 |
| 相机标定 | **功能不存在**（无代码、无数据） | 没有内参/外参，"距离 / 位置 / 三维"全无依据 |
| 手套零点标定 | 代码已有，无真机记录 | 关节角零点可能是偏的，默认阈值待 `info` 噪声底复核 |

逐项实测值、判据、以及 7 条"决定不做"的理由（E 组）全在
[`DOC/待验证清单.md`](DOC/待验证清单.md)。使用侧的明细表见
[`Cap_Quest/README.md` §10](Cap_Quest/README.md)。

## 5. 后续

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
