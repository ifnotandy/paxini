# 任务手册

按"我要干成什么"组织，直接照做。命令都在 `Paxini-Cap/Cap_Quest/` 下执行。

## 1. 跑一次采集（从零）

```bash
# 0) 环境（只做一次，已装可跳）
sudo dpkg -i ../sdk/pxhandsdk_1.0.0.0.ubantu2204_amd64.deb
python3.10 -m pip install --user -r ../requirements.txt

# 1) 确认设备在
export PATH="$HOME/bin:$PATH"
python3.10 capture/collect_cap.py --list-devices    # 串口末尾应是 PXCap

# 2) 不接硬件先自检
python3.10 selftest/selftest.py                     # 107 项

# 3) 关 NTP（必须）
sudo timedatectl set-ntp false

# 4) 采
python3.10 capture/collect_cap.py --cap-side right --no-quest
```

**成功判据**：结束时会打印 `verify.row_mismatch` 为空。非空说明某组数据集首维与
自己的行数不一致，这个文件能打开但按行取数会整体错位。

产物在 `recordings/data_<YYYYMMDD>_<n>/`。

## 2. 采集起不来，按现象查

| 现象 | 先查这个 |
|---|---|
| `SERIAL_BUSY(102)` | 串口被占：关掉 PXCap Studio、`hand_calib.py`、旧进程 |
| 找不到串口 | `ls /dev/ttyACM*`；空就重跑 `usbipd attach`；再不行查 `dialout` 组 |
| `import pxhandsdk` 成功但没 `PxCap3` | 用成 3.12 了，换 `python3.10` |
| 相机 0 fps / 5 fps | 日志里的「相机后端」是不是被强制成 `linux` 了 |
| 相机 20 秒不出图 | 先 `--camera-mode cam0` 排除带宽；再验分辨率是否被支持 |
| 等 Quest 超时 120 s | 没有头显就加 `--no-quest` |
| 采完没有 `manifest.json` | 被强杀了。数据落在最后一个满批之前（每批已 flush），先 `--clear-write-flag` 再读；2026-11-13 那批老文件没这个保护，全废 |

## 3. 改 HDF5 字段

三步，一步不能少：

1. 改 `capture/collect_cap.py` 的 `CAP_DATASETS`（手套侧）或 `_quest_layout()`
   （Quest 侧），同时把 `format_version` 从 4 加一
2. 改 `DOC/HDF5结构说明.md` 的字段表 + 版本变更表（**定点修改，不要整份重写**）
3. 在 `selftest/selftest.py` 加一条用例，跑通 102→103 项

漏第 3 步的后果：下次别人改坏这个字段，没人发现。

## 4. 加一个命令行参数

1. `capture/collect_cap.py` 的 `build_parser()` 加 `add_argument`
2. `cap_config.yaml` 加同名字段 + 一行注释
3. `load_config_file()` / `apply_config()` 确认这个键会被读取（命令行优先于文件）
4. `Cap_Quest/README.md` §6.2 参数表加一行
5. `selftest` 的"配置"组加用例

验证：`python3.10 capture/collect_cap.py --help` 能看到，且
`python3.10 -c "import yaml;print(yaml.safe_load(open('cap_config.yaml')))"` 不报错。

## 5. 测相机延迟

**用哪个脚本取决于你走哪条采集路径。** 两路的时间戳构成不同，测出来的数不可互换：

| 采集路径 | 脚本 | 帧时间戳的构成 | 实测量级 |
|---|---|---|---|
| WSL（相机借 Windows 的 ffmpeg.exe / DirectShow） | `latency/measure_camera_latency_wsl.py` | `epoch_unix_ns + 相对 pts` | 约 190 ms，主要是显示与 DirectShow 缓冲 |
| 原生 Ubuntu（ffmpeg / V4L2） | `latency/measure_camera_latency.py` | 墙钟 pts 本身，无锚点 | 几十毫秒 |

Windows：

```powershell
cd C:\Users\Andy\Desktop\paxini\Paxini-Cap\Cap_Quest
& "C:\Users\Andy\AppData\Local\Programs\Python\Python312\python.exe" `
  latency\measure_camera_latency_wsl.py --repeat 3
```

Ubuntu：

```bash
cd <repo>/Paxini-Cap/Cap_Quest
export PATH="$HOME/bin:$PATH"
sudo timedatectl set-ntp false          # pts 走 CLOCK_REALTIME，测完记得开回来
python3.10 latency/measure_camera_latency.py
```

**默认逐路测 cam0 和 cam1、每路录 20 秒**，所以上面两条命令都不带参数。枚举到的第 3 路
（笔记本自带的 `USB2.0 HD UVC WebCam`）不参与采集，脚本会自动跳过；只想测一路加
`--camera 0`，想改时长加 `--seconds N`。

两路是不同 USB 口，延迟也不同，**各用各的，不要取平均**。跑完会打一张各路汇总表；
某一路没有有效样本时，说明那一路没拍到二维码窗口，得调整相机朝向或单独重测。

把弹出的二维码窗口移到两路都能看到的屏幕位置，占各自画面 1/4 以上。

**成功判据**：解码率 > 30%、样本 ≥ 100、IQR < 20 ms。Ubuntu 版还要看「前后漂移」
< 10 ms —— 超了说明测的时候系统时钟被步进过，或者相机在自动调曝光，结果不能用。

**测完自动记账，不用手抄**：`latency/records/<电脑用户>-<win|ubuntu>.yaml`，
一台机器一个文件，键是测量序号；日志末尾会打印「测量记录 … 第 N 次（合格）」。
`--no-record` 可以跳过，用来调试。

同一台机器不同时间的读数会**整体跳一档**：本机 2026-11-16 连测三次是
189.7 → 287.2 → 192.3 ms，而 IQR 一直只有 5~6 ms。所以下游取数只许从台账读
最近一次的合格值，别抄某个固定数字：

```python
import sys; sys.path.insert(0, "Paxini-Cap/Cap_Quest/latency")
from latency_records import load_records, pick_latest

entry = pick_latest(load_records(), camera=0)   # 跳过 ok: false，两路相机要分开取
median = entry["median_ms"]
```

台账里每条带 `delta_prev_ms`（与上一条同参数的差），超过 20 ms 时测量当场警告，
说明链路变了 —— 先重测确认，别直接拿去修时间轴。

结果怎么用：

- Windows：`timestamp_unix_ns` 和 `timestamp_monotonic_ns` **两列都减**
- Ubuntu：只有 `timestamp_unix_ns` 一列，减它

## 6. 看采到的数据

```bash
python3.10 viewer/visualize.py recordings/data_20261113_9            # Tk 窗口
python3.10 viewer/visualize.py recordings/data_20261113_9 --html     # 导出浏览器版
python3.10 viewer/visualize.py recordings/data_20261113_9 --save-png a.png
```

没有 `python3.10-tk` 时用 `--html`。浏览器版支持悬停读数、保峰抽稀、双路视频面板。

## 7. 提交前

```bash
cd Paxini-Cap/Cap_Quest
python3.10 -m py_compile capture/*.py viewer/*.py selftest/*.py latency/*.py calib/*.py
python3.10 -m pyflakes  capture/*.py viewer/*.py selftest/*.py latency/*.py calib/*.py
python3.10 selftest/selftest.py                      # 107 项（加过用例会更多）
```

三关都过再提交。改过行为或数据结构，`selftest` 必须加用例，不能只靠手跑过一遍。

## 8. 判断一个改动安不安全

按顺序问自己：

1. **它会不会让 `cap/` `quest/` `cameras/` 的某一维变长或变短？** → 会，就要升
   `format_version` 并改文档
2. **它是不是在采集端做了配对或插值？** → 是，违反约束 1，停下
3. **它有没有引入一个新的时钟读数？** → 有，确认和原有列同源，否则违反约束 2
4. **它会不会让回调线程做 IO？** → 会，挪到主线程
5. **它改的是不是 `DOC/HDF5结构说明.md`？** → 整份重写会覆盖用户手动编辑的内容

五条都过了，再动手。
