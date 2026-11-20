# 踩坑记录

按"你会看到什么"组织。每条给现象、根因、对策。`Paxini-Cap/README.md` §0.3 有
一份精简版，这里是完整版，含 2026-11-16 新踩的。

## 一、看起来像 bug，其实不是

### 采集时读 `data.h5`，数据一跳一跳的

**现象**：程序在跑，用另一个进程读文件，只看到几百毫秒才多一块数据。

**根因**：`Hdf5Recorder.write_cap` 攒满 `FLUSH_ROWS = 100` 行才 resize+批量赋值，
落盘粒度就是这一批。120 Hz 下约 0.83 s 一块。（2026-11-17 起每批都 `flush()`，
但批内仍是全有全无，所以"一跳一跳"照样成立。）

**对策**：别中途读。实时视图只读内存不读文件，就是这个原因。

### 手套帧率 115~118 Hz，不是标称的 120

**现象**：`--hand-rate 120`，`manifest` 里的实际频率总是差一点，偶尔出现 12~35 ms
的帧间隔空洞。

**根因**：USB 调度与主机负载，与相机无关（关掉两路相机单采手套仍是 116.7 Hz）。
手套和两路相机共用总线，中间还有 CH340 集线器。

**对策**：中位间隔 8.34 ms 就是 120 Hz，看中位数别看均值。要压空洞就在 Windows 里
关掉「USB 选择性暂停」和「允许计算机关闭此设备以节约电源」。

### Tk 回放窗口点了没反应

**状态**：Tk 回放的交互**没人在真窗口里点过**，只验过 `--save-png`。浏览器版是实测
过的。要交互就用 `--html`。

## 二、环境

> **2026-11-20**：`main` 已删除 WSL 路径，本节里 usbipd / DirectShow 相关的条目
> 只对 `archive/wsl-backend` 分支有效，保留是为了那天真要回去时别再踩一遍。

### `usbipd attach` 报 `already attached to a client`，WSL 里却没有 `/dev/ttyACM*`

**现象**：`usbipd list` 手套那行是 `Attached`，`ls /dev/ttyACM*` 为空；再 `attach`
被直接拒绝：

```text
usbipd: info: Device with hardware-id '28e9:018a' found at busid '3-3'.
usbipd: error: Device with busid '3-3' is already attached to a client.
```

**根因**：WSL 空闲约 60 s 会自动关机（`vmIdleTimeout` 默认值），USB/IP 会话随之
断开，但 usbipd 的状态列不跟着更新。设备侧还记着那个已经死掉的客户端，于是
attach 被当成重复操作拒掉。

**对策**：

```powershell
usbipd detach --busid <BUSID>
usbipd attach --wsl --busid <BUSID>
```

`detach` 会让设备重新枚举，紧跟的 `attach` 常报 `There is no device with busid`，
等 1~2 秒重试同一条命令，别改参数。仍然拒绝就 `wsl --shutdown` 关掉全部实例再挂
——注意它会杀掉 WSL 里所有正在跑的任务。

**判据**：只看 `/dev/ttyACM*` 在不在。`usbipd list` 的 `Attached` 不能当证据。
`scripts/run_capture_noquest.sh` 已内置这套 detach→attach 重试（最多 5 轮），
手工跑就得自己来。

### `cv2.namedWindow` 报 "The function is not implemented"

**根因**：装的是 `opencv-python-headless`。它和 `opencv-python` 装在同一环境时，
后装的那个覆盖 `cv2`，`getBuildInformation()` 里 `GUI: NONE`。

**对策**：

```bash
python -m pip uninstall -y opencv-python-headless
python -m pip install --force-reinstall --no-deps opencv-python
```

装完 `GUI:` 应显示 `WIN32UI`（Windows）或 `QT5`（Linux）。

### DirectShow 报 `-5` / `E_ACCESSDENIED`，第二轮启动失败

**根因**：DirectShow 相机是独占的。上一轮 `ffmpeg.exe` 退出后驱动还要一点时间才
真正释放。

**对策**：两轮之间等 2~3 秒。`latency/measure_camera_latency.py` 的 `--repeat`
已内置等待与重试。

### `import pxhandsdk` 成功，但 `PxCap3` 不存在

**根因**：用成 `python3`（3.12）了。3.12 能导入包，但拿不到 C 扩展的类。

**对策**：一律 `python3.10`。自查：
`python3.10 -c "import pxhandsdk; print(hasattr(pxhandsdk,'PxCap3'))"` 应为 `True`。

### `python3.10 -m venv` 失败

**根因**：这个环境的 ensurepip 坏了。

**对策**：不用 venv，直接 `python3.10 -m pip install --user`。

### Git Bash 把 `/mnt/c/...` 改写成 `G:/WorkBuddy/...`

**根因**：MSYS 的路径自动转换。调 WSL 时尤其容易中招，报"文件不存在"但路径看着对。

**对策**：命令前加 `MSYS_NO_PATHCONV=1`，或用 `//mnt/c/...` 双斜杠。

### `sudo` 要密码，但脚本里没法交互

**对策**：`wsl.exe` 里用 `echo <pw> | sudo -S <cmd>`。

### WSL 的 `/tmp` 和 Windows 的临时目录不是一回事

**对策**：跨端传文件走 `/mnt/c/...`。WSL 侧生成的文件要让 Windows 程序读，就写到
`/mnt/c/` 下。

### 中文在 Windows 控制台显示成乱码或问号

**根因**：控制台代码页是 936(GBK)。Python 强制 `reconfigure(encoding="utf-8")`
会让 PowerShell 按 GBK 解码 UTF-8 字节，整屏乱码；反过来 `✓` `✗` 这类字符在 GBK
里没有，会变成 `?`。

**对策**：不要强改编码，保持控制台原编码只放宽 `errors="replace"`；输出只用 GBK
覆盖得到的字符（`[OK]` `[WARN]` 而不是 `✓` `✗`）。

### matplotlib 中文变方框

**根因**：本机只装了 `Noto Sans CJK JP`，没有 Mono 版。

**对策**：等宽中文会整片变方框，表格类布局别用 `family="monospace"`。

### 写文件工具产出 CRLF，仓库其余是 LF

**对策**：提交前统一一次。

## 三、会静默毁数据

### 时间轴整体偏几百毫秒，但没有任何报错

**根因**：没做相机延迟标定，或用的是别人机器上的默认值。

**对策**：跑 `latency/measure_camera_latency.py`（原生 V4L2 唯一路径），数从
`latency/records/` 里取最近一次的合格值
（`pick_latest(load_records(), camera=0)`），别抄文档里的数字。改了分辨率、帧率、
codec、USB 口、机器都要重测。

### 用了几个月前的 Δ，时间轴整体偏 100 ms

**根因**：同一台机器、同一套参数，读数也会整体跳一档。2026-11-16 连测三次：
189.7 → 287.2 → 192.3 ms，三次 IQR 都只有 5~6 ms。变的不是抖动，是 DirectShow
缓冲深度（那一次多了约 3 帧）。把某一次的读数当常量写死，隔几个月就对不上。

**识别**：和「偏几百毫秒」一样 —— 行数、间隔、单调性全正常，只有对齐错。

**对策**：下游现场读 `latency/records/<机器>.yaml` 取最近一次 `ok: true`；记录里的
`delta_prev_ms` 超过 20 ms 说明链路变过，别直接拿去修时间轴，先重测确认。

### 原生 Ubuntu 采的数据减了 WSL 测的 Δ，时间轴整体平移

**根因**：两条路径的 pts 基准不同。V4L2 的 pts 是绝对墙钟；DirectShow 的 pts 被
ffmpeg 归零到首帧，再靠 `epoch` 平移成绝对时间。Δ 也不是同一个量：原生 Ubuntu 通常
几十毫秒，WSL 约 190 ms，多的是虚拟机边界与 DirectShow 缓冲。

**识别**：拿错数只会让时间轴整体平移，行数、间隔、单调性全都正常，看不出异常。

**对策**：两条路径各测各的，别抄。判据是量级：差三到五倍就说明拿错了。

### 跨系统时钟相减，结果每次都不一样

**根因**：WSL 与 Windows 的墙钟差**不是常数**。历史记录 1610 ms，2026-11-16 实测
0.3 ms，取决于 NTP 同步状态。

**对策**：一个差值里的两个时刻必须在同一侧取。看到任何 `t_a - t_b`，确认两边同源。

### 采集中途校时，数据废了

**根因**：手套墙钟列和 ffmpeg pts 会被 NTP step 改写，实测采集中出现过 2~5 秒跳变。
采集端**不修正**，原样落盘。

**对策**：采前 `sudo timedatectl set-ntp false`。已经跳了的，对比 `timestamp_unix_ns`
与 `timestamp_monotonic_ns` 能看出来，数据要么剔除要么作废。

### 两路相机 + 手套同时工作时，整个 USB Hub 掉线

**现象**：ffmpeg 已写入几十帧，随后报 `ioctl(VIDIOC_DQBUF): No such device`；
`/dev/video*` 和 `/dev/ttyACM*` 一起消失。

**根因**：USB Hub 在设备取流后被复位。2026-11-18 实测两个 MASTER Camera 与 PXCap
共用的 `usb 3-1.3` Hub 整组 disconnect，几秒后重新枚举。这是供电/硬件拓扑问题，
不是分辨率不支持。

**对策**：相机和手套尽量直连电脑的不同 USB 控制器端口；必须使用 Hub 时选带独立
电源的 USB 3.0 Hub，并用 `sudo dmesg -wT` 确认没有再次整组掉线。

### stream-copy 的 `.progress` 没有 `frame=`，被误判为 0 帧

**现象**：MKV 已经在增长，但相机就绪检查一直显示 0 帧。

**根因**：ffmpeg 7 的 stream-copy 可能只输出 `out_time_us` 和 `total_size`。
只解析 `frame` 会误判。

**对策**：用 `FfmpegCamera.progress_signal()`，依次兼容 `frame`、`out_time_us × fps`
和文件大小；不要恢复成只读 `frame`。

### 强杀进程 → 整份采集作废（2026-11-17 已修）

**根因**：`Hdf5Recorder` 每 100 行 `resize` 一次，但 `file.flush()` 原本**只在 `close()`
里调** —— HDF5 的元数据树（根组对象头、数据集 B 树）一直留在写缓存里，进程一死就没了。

**后果（修复前）**：`data.h5` 只剩 48 字节的超级块。表现为 `file is already open for
write`（写标志没清），清掉标志、重算校验和之后仍然报 `truncated file: eof = 48`。
**2026-11-13 那批 8 份被强杀的采集全部如此，0 份可救。**

**已修**：`flush_cap()` / `flush_quest()` 末尾加 `self.file.flush()`，每批都落盘。
实测代价 **0.07 ms / 100 行批**（占这批写入 10 ms 的 0.8%）。WSL 真实环境强杀对照：

| 版本 | 强杀后文件 | 清标志后 |
|---|---|---|
| 修复前 | **48 字节**（只有超级块） | 打不开，`eof = 48` |
| 修复后 | 797 697 字节 | **读出 500 行**（5 个满批；最后 50 行未满批的丢失） |

**恢复步骤**：强杀后的 `data.h5` 一定带写标志，直接用只读方式打不开。先
`python Paxini-Align/align_capture.py <目录> --clear-write-flag`（改 1 字节 + 重算超级块
校验和，等价 `h5clear -s`），再读。**只翻字节不重算校验和会把好文件弄坏**，报
`incorrect metadata checksum`。

**注意 flush ≠ fsync**：进程被杀能保住，整机断电仍可能丢 page cache 里未落盘的部分。
写盘策略接受这个风险，不做 fsync（每 0.83 s 一次 fsync 代价太高）。

**已修（第二层保护）**：30 s 看门狗到点和第二次 `Ctrl+C` 不再直接 `os._exit`，
而是先调 `Hdf5Recorder.emergency_save()`（带超时抢锁 → flush 余数 → resize 到精确行数 →
close），拿到文件后再退出 130。抢不到锁（主线程正卡在 HDF5 里）就放弃落盘，因为并发写
会把文件结构弄坏。

锁是**普通 `Lock` 不是 `RLock`**，这是刻意的：Python 的信号处理器跑在主线程上，如果
Ctrl+C 正好落在一次批量写中间，`RLock` 会让同一线程重入成功、在半截写入里再 `close()`
一次，把数据集 resize 成混合行数。普通 `Lock` 在这种情况下超时失败，宁可不落盘。
代价是 HDF5 的写操作全都套这把锁，且内部调用只能用 `_flush_*_locked` / `_close_locked`
这类"已持锁"版本，不能在锁内再调公共方法。

**对策**：Ctrl+C 只按一次，等它自己收尾（有 30 s 看门狗，超时也会落盘）。

## 四、重构相关的坑（2026-11-16 目录拆分后）

### 移动文件后 `--help` 仍通过，但某个功能炸了

**根因**：`collect_cap.py` 里的 `from live_view import ...` 是**函数内延迟导入**，
只有 `--view` 时才执行。`--help` 测不出来。

**对策**：改了目录结构，除了 `--help`，还要跑一遍会用到的功能路径和
`python3.10 selftest/selftest.py`（全过说明 import 链没问题）。

### 外面的脚本引用了里面的模块

**根因**：`Paxini-Cap/py_demo/camera.py` 通过 `sys.path.insert` 复用
`Cap_Quest/capture/camera_ffmpeg.py`。Cap_Quest 内部一拆目录，这种外部引用就断。

**对策**：改结构前先 `grep -rn "camera_ffmpeg\|Cap_Quest" --include=*.py ..`，
把仓内所有跨目录引用列出来。
