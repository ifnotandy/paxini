# 踩坑记录

按"你会看到什么"组织。每条给现象、根因、对策。`Paxini-Cap/README.md` §0.3 有
一份精简版，这里是完整版，含 2026-11-16 新踩的。

## 一、看起来像 bug，其实不是

### 采集时读 `data.h5`，数据一跳一跳的

**现象**：程序在跑，用另一个进程读文件，只看到几百毫秒才多一块数据。

**根因**：`Hdf5Recorder.write_cap` 攒满 `FLUSH_ROWS = 100` 行才 resize+批量赋值，
`file.flush()` 只在 `close()` 时调。120 Hz 下约 0.83 s 一块。

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

**对策**：两轮之间等 2~3 秒。`latency/measure_camera_latency_wsl.py` 的 `--repeat`
已内置 2.5 s 等待 + 一次重试。

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

**对策**：跑对应采集路径的测量脚本（原生 Ubuntu → `measure_camera_latency.py`，WSL →
`measure_camera_latency_wsl.py`），数从 `latency/records/` 里取最近一次的合格值
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

### 强杀进程丢了 `manifest.json`

**根因**：manifest 在正常收尾时写。强杀会跳过。

**后果**：`data.h5` 尾部最多丢 100 行（未 flush 的），且没有 manifest 说明采到了什么。

**对策**：Ctrl+C 只按一次，等它自己收尾（有 30 s 看门狗）。

## 四、重构相关的坑（2026-11-16 目录拆分后）

### 移动文件后 `--help` 仍通过，但某个功能炸了

**根因**：`collect_cap.py` 里的 `from live_view import ...` 是**函数内延迟导入**，
只有 `--view` 时才执行。`--help` 测不出来。

**对策**：改了目录结构，除了 `--help`，还要跑一遍会用到的功能路径和
`python3.10 selftest/selftest.py`（102 项能覆盖绝大部分 import 链）。

### 外面的脚本引用了里面的模块

**根因**：`Paxini-Cap/py_demo/camera.py` 通过 `sys.path.insert` 复用
`Cap_Quest/capture/camera_ffmpeg.py`。Cap_Quest 内部一拆目录，这种外部引用就断。

**对策**：改结构前先 `grep -rn "camera_ffmpeg\|Cap_Quest" --include=*.py ..`，
把仓内所有跨目录引用列出来。
