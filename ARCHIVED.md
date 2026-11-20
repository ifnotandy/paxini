# 已归档：WSL 采集路径

**冻结于 2026-11-20**，对应 `main` 的 `69cbc94`（采集输出已上移到 `Paxini-Cap/Recordings`）。
`archive/wsl-backend` 分支**不再维护**，只作为"某天必须回到 Windows 采集"时的可运行快照。

`main` 已删除全部 WSL 支持，只剩原生 Ubuntu（V4L2）路径。需要 WSL 时 checkout 该分支，
不要把它 merge 回 main。

> 这份文件在 `archive/wsl-backend` 分支上同样存在（那份写的是分支自身状态）。
> `main` 上保留一份，是为了让读 `main` 的人知道删了什么、以及真要回去时怎么做。
> 下文凡是写"分支上"的数值，都是 2026-11-20 快照的实测值，**不代表 `main` 现状**。

## 归档分支里有什么

| 内容 | 位置 |
|---|---|
| 相机后端选择（`linux` / `wsl`） | `Cap_Quest/capture/camera_ffmpeg.py` 的 `backend` 参数与 `FfmpegCamera` |
| Windows 相机枚举（DirectShow） | `camera_ffmpeg.py` 的 `discover_windows_cameras()` |
| `usbipd` 自动 attach 与重连 | `Cap_Quest/scripts/run_capture_noquest.sh` |
| WSL 专用延迟测量 | `Cap_Quest/latency/measure_camera_latency_wsl.py` |
| Windows 侧 ffmpeg | `Paxini-Cap/sdk/ffmpeg-win/ffmpeg.exe`（不入库，见 `sdk/README.md`） |
| WSL 实测的延迟台账 | 分支内 `Cap_Quest/latency/records/andy-win.yaml`；`main` 已移到 `latency/records/archive/andy-win.yaml` |

## 这台机器的原始配置

- Windows 11 + WSL2（Ubuntu 22.04），手套 SDK 装在 WSL 内的 `python3.10`
- 相机**全程留在 Windows**：WSL 里调 `ffmpeg.exe`，`-f dshow` 输入
- 手套经 `usbipd` 转发进 WSL（`28e9:018a`），`usbipd list` 显示 Attached 也可能已失效，
  脚本里必须重试而不是判一次就放弃
- Quest 3 走 XRoboToolKit：Windows 侧 `RoboticsServiceProcess` + 局域网，WSL 只是客户端
- 关 NTP（`sudo timedatectl set-ntp false`），否则墙钟跳变会让时间轴整段偏

## 当时的实测值

| 项 | 值 | 来源 |
|---|---|---|
| 相机延迟 Δ（cam0） | **192.3 ms**（IQR 6.12，271 样本） | `records/andy-win.yaml` 第 3 条，1280×720@30 `copy` |
| 相机延迟 Δ（cam1） | **未单独测**，与 cam0 共用同一值 | `DOC/待验证清单.md` A11 |
| 手套实际频率 | 115.9~119 Hz 波动，中位间隔 8.34 ms | `DOC/待验证清单.md` B8 |
| `selftest` | 136 项（分支上；`main` 删了 WSL 用例，项数以运行输出为准） | `Cap_Quest/selftest/selftest.py` |

同名目录 `right_cam0` / `right_cam1` 是因为当时相机 ID 前缀跟手别走；`main` 现在用
`<cap_side>_camN`，切回来时注意别和下采样脚本的路径混。

## 恢复步骤

1. `git checkout archive/wsl-backend`
2. Windows 侧确认 `usbipd list` 能看到 `28e9:018a`，必要时
   `usbipd bind --busid 3-3` 再 `usbipd attach --wsl --busid 3-3`
3. 把 Windows 版 ffmpeg 放到 `Paxini-Cap/sdk/ffmpeg-win/ffmpeg.exe`（不入库）
4. WSL 内装 `pxhandsdk`、`numpy`/`h5py`/`xrobotoolkit-sdk`，用 `python3.10 -m pip install --user`
5. `cd Paxini-Cap/Cap_Quest && python3.10 selftest/selftest.py`，应全过（分支上 136 项）
6. `python3.10 capture/collect_cap.py --list-devices`，确认串口末尾是 `PXCap`
7. 采一枪前先 `sudo timedatectl set-ntp false`，否则 `DOC/时间戳说明.md` §4.1 的坑会重演

## 不要做的事

- **不要**把本分支 merge 回 `main`。`main` 的 `camera_ffmpeg.py` 已经没有 `wsl` 分支，
  硬合会带回半套后端判定。
- **不要**用本分支的数据去验证 `main` 的行为：`main` 的 align 只看原生路径的时间语义。
- **不要**删 `andy-win.yaml`。它是这台机器 WSL 路径唯一的 Δ 证据；`main` 已把它归档到
  `latency/records/archive/`。
