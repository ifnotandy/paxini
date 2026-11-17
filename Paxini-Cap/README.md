# Paxini-Cap

PXCap III 数据手套 + 双路相机 + Quest 3 的同步采集工程，对应
`Paxini-Dex`（PXDex 执行器）那一套。区别是这里没有电机、不产生任何运动，
输出是单个 HDF5。

```text
Paxini-Cap/
├── Cap_Quest/          # 主程序（对应 Dex_Quest）；内部结构与命令见其 README
│   ├── capture/            # 采集主程序与设备驱动层
│   ├── calib/              # 手套零点标定（hand/ 放标零报告，不入库）
│   ├── latency/            # 延迟测量：两个脚本 + records/ 台账（一台机器一个文件）
│   ├── viewer/             # 回放与实时视图
│   ├── selftest/           # 不接硬件的自检，107 项
│   ├── scripts/            # 一键采集脚本
│   ├── recordings/         # 采集输出（data_<日期>_<序号>/）
│   └── cap_config.yaml     # 可选配置（存在即加载，命令行优先）
├── Paxini-Align/       # 下游：把手套流重采样到相机帧时刻（→ README）
│   ├── align_capture.py    # 主程序：读 data.h5，输出 aligned_<cam>.h5
│   └── selftest.py         # 合成数据自检 12 项，不接硬件
├── DOC/                # 文档（格式说明、时钟说明、待验证清单、标定）
│   ├── README.md           # 文档索引 + 阅读顺序 + 维护约定
│   ├── HDF5结构说明.md     # 逐字段说明 + 完整目录树（data.h5 的唯一权威文档）
│   ├── 时间戳说明.md       # 三个来源的时钟域、换算公式、四个坑（先读这份）
│   ├── 待验证清单.md       # 真机/Quest 待验证项 + 已决定不做的理由 + 标定（G 组）
│   ├── 标定.md             # 两类标定：手套零点（已实现）+ 相机标定（未实现）
│   └── figures/            # 时间戳图解（SVG，供 HDF5结构说明 / 时间戳说明 引用）
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

**当前状态**：真机（手套 + 双路相机）已跑通，但 **Quest 整组未测**；相机标定**已决定不做**
（E8，下游扩散模型只吃图像、不解算位姿）。Quest 补上之前，跨源对齐做不了；标定不做，
任何"距离 / 位置 / 三维"的结论都不成立。逐项状态见 §6 与
[`DOC/待验证清单.md`](DOC/待验证清单.md)。107 项 `selftest.py` 只覆盖纯逻辑
（帧解析、行轴、配置、视图挂钩）；**下游配对已有代码**：`Paxini-Align/` 把手套流按相机
帧时刻重采样（见 §5）。

**数据现状（2026-11-17 核对）**：`recordings/` 里 13 份采集中只有 4 份可用
（`data_20261113_2 / _5 / _11`、`data_20261116_1`）。另外 8 份是采集中途被强杀的文件，
写的时候 `file.flush()` 还只在 `close()` 里调，强杀时 HDF5 元数据树从未落盘，`data.h5`
只剩 **48 字节超级块**，整份作废。这个坑 2026-11-17 已修（每批落盘，见 `agent/pitfalls.md`），
修好之后强杀最多丢最后不满 100 行的那一批。`data_20261113_1` 能打开但没录相机。

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
0.83 s）才 `resize` + 批量赋值，**每批结束 `file.flush()`**（2026-11-17 起；之前只在
`close()` 里 flush，强杀会连元数据一起丢）。所以采集中途读 `data.h5` 会看到一跳一跳的
数据，实时视图因此只读内存、不读文件。

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
python3.10 selftest/selftest.py          # 应为 107 项全过
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

装完先跑 §0.4 的三关：`selftest` 107 项全过说明环境没问题，此时不需要插硬件。

**验证设备与日常运行不在本文档**：`--list-devices`、采集、`--view`、一键脚本、回放、
参数表全在 [`Cap_Quest/README.md` §1](Cap_Quest/README.md)。采前关 NTP、
`export PATH="$HOME/bin:$PATH"`、没有头显加 `--no-quest` 这三条纪律，在该文件的
【快速开始】与【§2 时间戳】里各有完整说明，这里不再重列。

## 4. 验证状态

**结论：采集链路可用，跨源分析不可用。**

真机（手套 + 双路相机）2026-11-13 已跑通，逐帧无丢帧、关 NTP 后时钟零跳变；不接
硬件时 `selftest` 107 项全过。但三处缺口没补上之前，任何跨源结论都不成立：

| 缺口 | 状态 | 后果 |
|---|---|---|
| Quest 整组 | 无头显，C3~C8 未测 | `sdk_timestamp_ns` 属于哪个时钟未定，跨源对齐缺前置 |
| 相机标定 | **已划出范围**（E8） | 下游扩散模型只吃图像、不算位姿，因此不做。代价是不做任何"距离 / 位置 / 三维"结论 |
| 手套零点标定 | 代码已有，无真机记录 | 关节角零点可能是偏的，默认阈值待 `info` 噪声底复核 |

逐项实测值、判据、以及 7 条"决定不做"的理由（E 组）全在
[`DOC/待验证清单.md`](DOC/待验证清单.md)。使用侧的明细表见
[`Cap_Quest/README.md` §10](Cap_Quest/README.md)。

## 5. 后续

1. **`Paxini-Align/`（已实现，2026-11-17）**：相机时间戳为采样网格，减去实测 Δ 后
   把手套流线性插值到该时刻，一行一个训练样本；输出 `aligned_<cam>.h5`，含取图用的
   `frame_index` 与插值跨度质量列。用法与字段见 [`Paxini-Align/README.md`](Paxini-Align/README.md)。
   **Quest 暂不处理**；两路相机不同步（cam1 最近帧偏差半帧），要严格同帧就得各跑一次。
2. **相机延迟 Δ 现场实测**（不是标定，别混）：改分辨率、帧率、codec、USB 口、机器之后
   重跑 `latency/`，下游从台账取最近一次合格值。扩散模型对观测-动作错帧比人眼敏感得多，
   便宜但漏了要重采。
3. **可视化**：`Cap_Quest/visualize.py` 已经能回放——关节角、10 路合力数值表、
   共享时间轴、空格播放/逐帧/点曲线跳转/滑块/倍速，浏览器版还有悬停读数、保峰抽稀、
   时钟漂移统计、空负载与跳变警告、**双路相机视频面板（按时间轴精确同步）**；
   采集时 `--view` 实时出 4 路关节角与 10 路合力表（纯数字，不画曲线）加**相机关键帧**。
   还缺两块：
   - Quest 轨迹叠加（要 C 组 + Quest 外参，即 E8 启封后的 G4）
   - 时间质量**曲线图**：现在只有数值（漂移 + >10 ms 跳变次数），画成
     `unix − monotonic` 偏移曲线更直观
