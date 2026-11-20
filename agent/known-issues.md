# 已知缺陷（未修复）

2026-11-20 通读 Cap 全部代码后记录。这里是**确实错了、但还没改**的条目。

**记录基准**：`main` @ `3c42ec5` + 当天工作区的「采集输出上移到 `Paxini-Cap/Recordings/`」
改动。核对时该工作区正在被并发修改，所以下表行号以 2026-11-20 19:05 的文件状态为准；
之后再改代码，行号会漂，按函数名找（每条都写了函数名）。

与相邻两份文档的分工：

| 文档 | 回答什么 |
|---|---|
| [`待验证清单.md`](../Paxini-Cap/DOC/待验证清单.md) | 某项**测过没有**、实测值多少（功能可能是对的，只是没验） |
| 本文件 | 代码或文档**明确写错了**，还没改 |
| [`pitfalls.md`](pitfalls.md) | 已经踩过、知道怎么绕的坑 |

修完一条就把对应小节删掉，不要留"已修复"的僵尸条目；要留证据就在 commit 里写。
**编号不连续是正常的**——删掉的 K3/K5/K6/K7 已于 2026-11-20 修好，编号保留不复用。

## 严重度口径

| 级别 | 含义 |
|---|---|
| P0 | 会损坏数据或让采集直接失败。**当前没有 P0。** |
| P1 | 静默写错元数据；或者主动把人/模型引向错误改法；或者让新机器装不上 |
| P2 | 只影响边界场景、显示或说法，数据与可用性不受影响 |

**结论：没有 P0。** 采集链路、HDF5 布局、对齐逻辑都经得起读；问题集中在元数据、
注释和文档，属于"信错文档会浪费半天"而不是"数据会坏"。所以不急着停手修，
但要记着，别在它们上面再盖东西。

## 清单

| # | 级别 | 位置 | 一句话 |
|---|---|---|---|
| K1 | P1 | `Cap_Quest/capture/collect_cap.py:2268` | `--view --no-view-video` 时 manifest 的相机元数据变空 |
| K2 | P1 | `Cap_Quest/capture/collect_cap.py:8-16` | 模块 docstring 整段是 Dex 的配对策略，教人去做对齐 |
| K4 | P1 | `Paxini-Cap/sdk/README.md:47` | 第一条 `dpkg -i` 在 python3=3.12 的机器上必失败，与同文件 §3.1 冲突 |
| K8 | P2 | `Cap_Quest/capture/collect_cap.py:701` | `write_cap` 在锁外改 `_cap_pending`，与看门狗落盘有窄竞态 |

---

## K1（P1）`--view --no-view-video` 会让 manifest 丢掉相机元数据

**位置**：`Cap_Quest/capture/collect_cap.py` 的 `main()`，`if args.view:` 分支第一行
（2026-11-20 19:05 时为 2268 行）

```python
if args.view:
    try:
        camera_ids = tuple(camera.camera_id for camera in cameras if camera.preview_fps > 0)
```

`camera_ids` 是外层变量（`Hdf5Recorder(...)` 构造时用过），这里被**重新绑定**。
`preview_fps` 只在 `args.view and args.view_video` 时才 > 0，所以：

| 命令 | `camera_ids` 变成 | manifest |
|---|---|---|
| `--view`（默认带视频） | 两路都在 | 正常 |
| `--view --no-view-video` | `()` | `cameras.camera_ids=[]`、`cameras.per_camera={}`、`output.camera_ids=[]` |
| 不带 `--view` | 不进这个分支 | 正常 |

**后果**：`data.h5` 里 `cameras/<id>/` 组是齐的（recorder 在视图分支之前就用旧值建好），
时间戳也照写（`finally` 里按 `cameras` 列表遍历，不按 `camera_ids`）。所以**数据没坏，
只有 manifest 在撒谎**。连带的还有收尾那几条「camX 视频帧：N」日志不再打印，
采完看不出相机有没有真的录到。

**为什么危险**：manifest 是排障和验收的第一入口。空 `per_camera` 会让人以为
"这次没录相机"，进而去查 ffmpeg、查带宽、重采——而实际数据在文件里躺着。
`selftest` 覆盖不到：没有一项跑 `main()` 的 manifest 分支。

**修法**：把 2265 行改成局部变量，别碰外层 `camera_ids`：

```python
preview_ids = tuple(cam.camera_id for cam in cameras if cam.preview_fps > 0)
```

然后用 `preview_ids` 传给视图构造与 `attach_cameras`。

**验证**：`--view --no-view-video` 采 10 秒，`manifest.cameras.per_camera` 应有两路
且 `frames > 0`。要锁住回归，把 manifest 组装那段抽成可测函数，selftest 里喂一个
`preview_fps=0` 的假相机断言 `camera_ids` 非空。

---

## K2（P1）模块 docstring 是 Dex 的策略，教人去做对齐

**位置**：`Cap_Quest/capture/collect_cap.py:8-16`

```
Timing policy (identical to ``Dex_Quest/collect_hand_pose.py``):
- One host Unix-nanosecond axis. ...
- Pairing is nearest neighbour only: no interpolation, no averaging.
- Every skew is an absolute value. A frame outside tolerance is recorded as an
  unmatched row, never dropped.
```

这一段描述的是 **Dex 在采集时对齐**的行为（`aligned.jsonl` 那套）。Cap 的真实实现
是三条独立行轴、零配对，`manifest.alignment` 固定写
`none (raw per-source timestamps only)`。两者是相反的设计（约束 1 与约束 11）。

**为什么这是 P1**：docstring 是任何新读者（人或者模型）打开文件看到的第一段文字。
按它改代码，就会把历史上被整体删掉的对齐逻辑加回来，直接踩穿硬约束第 1 条，
而且是"看起来在修 bug"的姿势踩的。已经有人被它误导过一轮。

**修法**：换成真实策略（三轴独立、各带自己的时间戳、配对属于 `Paxini-Align`、
回调线程只拷贝），并显式写明"不要在这里做配对"。

**验证**：`grep -n "nearest neighbour\|unmatched row" capture/*.py` 应无命中。

---

## K4（P1）`sdk/README.md` 第一条安装命令在 3.12 机器上必失败

**位置**：`Paxini-Cap/sdk/README.md:47`（§3），与同文件 §3.1（63-78 行）冲突

§3 让你装原始包 `pxhandsdk_1.0.0.0.ubantu2204_amd64.deb`，§3.1 紧接着说这个包的
`Depends: python3 (<< 3.11)` 在 python3=3.12 的机器上必然不满足，要装本地重新打包的
`pxhandsdk_1.0.0.0.ubantu2204_amd64.py310fix.deb`。两个包当前都在 `sdk/` 里。

**后果**：新机器照 §3 顺序做，第一条 `dpkg -i` 失败；跟着 `apt-get install -f`
还会把包卸掉（README 自己警告了）。纯浪费一轮。

**修法**：§3 第一条直接写 `.py310fix.deb`，把原始包降级成"打补丁的来源"。

**验证**：`dpkg -L pxhandsdk | head`，再
`python3.10 -c "import pxhandsdk; print(hasattr(pxhandsdk,'PxCap3'))"` 为 `True`。

---

## K8（P2）`write_cap` 在锁外改 `_cap_pending`

**位置**：`Cap_Quest/capture/collect_cap.py` 的 `write_cap()`（2026-11-20 19:05 时为 701 行起）

`write_cap` 在**不持锁**的情况下 `self._cap_pending.append(hand)` 并更新首末时间戳；
看门狗线程的 `emergency_save` 持锁后执行 `batch = self._cap_pending; self._cap_pending = []`。
两者交错时，某一两行可能既不在 `batch` 里、也被随后清空——落盘少 1~n 行。

**后果**：只发生在"30 s 看门狗到点"或"第二次 Ctrl+C"的几百毫秒内，最多丢一两行，
**不会写坏文件**（写坏的路径被那把非重入锁挡住了）。与文档"写盘全部在主线程"的
说法有出入，但主线程之外的访问者只有紧急落盘一个。

**修法**（真要改）：`write_cap` 的 append 段也套 `self._lock`，或者让紧急落盘
只处理"已经 append 完"的批次。**建议先不动**——紧急路径的复杂度比丢一行值钱。

---

## 顺带记下：读代码时确认成立、不用改的点

避免以后重复怀疑：

- `distribute_counts` 一定等于真正写进数组的点数（`snapshot_cap_frame` 用
  `usable = min(count, len(points), 119)` 回写），444 合计由 `selftest` 锁住。
- `verify_rows` 只查 `cap/` 与 `quest/`，`cameras/` 刻意排除——它的两列等长由
  `write_camera_timestamps` 自己抛 `RuntimeError` 保证。
- WSL 后端锚不到 `epoch` 就整路失败，**没有**"假设 1.55 s"的兜底，这是刻意的。
- `resolve_delay` 用 cam0 的台账值给两路共用，是 2026-11-17 的明确决定，
  不是漏了 cam1（cam1 缺测见 `待验证清单.md` A11）。
