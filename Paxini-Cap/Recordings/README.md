# Recordings

采集输出目录。**这些数据不入库**（`.gitignore` 里只有本文件被放行），全部由
`Cap_Quest/capture/collect_cap.py` 写入，默认位置就是这里。

一份采集是一个 `data_<YYYYMMDD>_<序号>/` 目录，结构见
[`../DOC/HDF5结构说明.md`](../DOC/HDF5结构说明.md)。

## 当前内容

| 目录 | 手别 | 相机 | 手套 | 时长 | Quest | 来历 |
|---|---|---|---|---|---|---|
| `data_20261117_1` | 右手 | 双路 1280×720@30 | 2379 行 / 114.2 Hz | 24.5 s | 无（`quest_rows=0`） | 样例，来自标签 `data-sample-20261117` @ `52f63ce`（源分支已删，标签保留溯源） |
| `data_20261117_2` | 右手 | 双路 1280×720@30 | 2587 行 / 115.1 Hz | 26.3 s | 无（`quest_rows=0`） | 同上 |
| `data_20261117_3` | 右手 | 双路 1280×720@30 | 2620 行 / 117.1 Hz | 26.3 s | 无（`quest_rows=0`） | 同上 |
| `data_20261118_1` | 右手 | 双路 1280×720@30 | 2396 行 / 115.9 Hz | 24.3 s | 无（`quest_rows=0`） | 本机现采 |

四份的行数、频率、时长与各自的 `manifest.json` 逐项核对一致（2026-11-20，按
`manifest.rows` / `actual_rate_hz` / `duration_s` 读，不按目录名猜）；`data.h5` 均为
`format_version=4`，可直接喂 [`../Paxini-Align/align_capture.py`](../Paxini-Align/align_capture.py)。

**四份都没有 Quest 行**（`quest_rows=0`、`quest.enabled=false`）。表格早先给三份
20261117 样例标过「有」，那是因为 `data.h5` 里有空的 `quest/` 组——组在、行是 0，
不等于采到了数据；C8 与 Quest 对齐在这批数据上无从验证。

## 已知限制

**这四份都是旧采集路径（WSL2，相机经 Windows）下产生的**（`manifest.env.platform` 全为
`...microsoft-standard-WSL2`，相机 device 是 Windows 的 `@device_pnp_\\?\usb#vid_...` 串），
当前 `main` 只有原生 Ubuntu（V4L2）一条路。读这批数据时注意：

- 相机 ID 是 `right_cam0` / `right_cam1`（当时前缀跟手别走），当前代码写 `<cap_side>_camN`；
- 对齐这批数据要用当时的 Δ **192.3 ms**（1280×720@30 `copy`；原始台账在
  `archive/wsl-backend` 分支的 `Cap_Quest/latency/records/andy-win.yaml`）。**当前路径必须
  重测，不能沿用它**；2026-11-20 起 Align 的默认 Δ 是 0（未标定），所以对齐这批老数据
  必须显式给 `--delay-ms 192.3`，否则拿到的是一份没做延迟补偿的产物；
- 四份的 `data.h5` 里都有 `quest/` 组但 0 行，C8 时钟域从未验证，`--quest-align sdk_unix`
  没有可对齐的样本，会被门控拒绝。这是预期行为，不是数据损坏。

另外 `data_20261118_1` 与 `DOC/待验证清单.md` C8/A12 引用的同名文件**不是同一份**
（那份是 Quest 左手、640×480@15、`left_cam0` 245 帧，原始文件已不在本工作区）。

## 下载

数据本体不进 git，通过下面两处获取（2026-11-20 起）：

- ModelScope 数据集：`（待补链接）`
- GitHub Release（只放 20261117 三份样例的 zip，tag 指向数据源提交 `52f63ce`）：
  <https://github.com/yang-robotics/paxini/releases/tag/data-sample-20261117>

仓库是私有的，Release 资产只有组织成员能下；对外分发走 ModelScope 那条。

## 新增采集

不需要手动建目录：`collect_cap.py` 会按当天序号自动递增。要指定位置用
`--output-dir`，但注意 `align_capture.py` 的默认批量路径是 `../Recordings`。
