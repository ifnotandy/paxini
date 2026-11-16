# agent/

给 AI 助手用的项目认知文档。目标不是"读完变专家"，是**动手前 3 分钟内知道该改哪个
文件、不能碰什么、改完怎么验**。

人类读者也欢迎，但正式说明在 [`../README.md`](../README.md) 和
[`../Paxini-Cap/README.md`](../Paxini-Cap/README.md)，那两份讲得更完整。

## 这套文档解决什么

AI 在新会话里最容易犯的四个错，各有一份对应：

| 文件 | 治什么 |
|---|---|
| [`code-map.md`](code-map.md) | 不知道改哪个文件，凭文件名瞎猜 |
| [`constraints.md`](constraints.md) | 不知道哪些"优化"会破坏下游，好心办坏事 |
| [`playbook.md`](playbook.md) | 知道要改什么，但步骤顺序错，跑一半卡住 |
| [`pitfalls.md`](pitfalls.md) | 重复踩已经踩过的坑，浪费一整轮 |

## 项目 30 秒

`paxini` 是 PXCap III 数据手套与 PXDex III 灵巧手的开发工作区，两条独立的线：

```text
Paxini-Cap/   只记录、不运动
              PXCap III 手套(串口 28e9:018a, 120Hz)
            + 2× UVC 相机(1280x720@30)
            + Quest 3(ADB + XRoboToolkit, 120Hz 轮询)
            → 单个 data.h5，三条独立行轴，各自时间戳，不做对齐

Paxini-Dex/   会运动
              PXDex III 灵巧手 + 导纳控制 + Dex_Quest 采集
              输出 aligned.jsonl + hand_pose.csv + 逐帧 JPG（采集时就对齐）
```

两条线的代码骨架同源：`Cap_Quest` 对应 `Dex_Quest`。但 `quest3.py` 已经分叉，
差异在 `collect_sample` 和模块 docstring，**不要为了"一致"去同步它们**。

## 现在什么能用

| 能力 | 状态 |
|---|---|
| Cap 采集（手套 + 双路相机） | 可用，2026-11-13 真机验证 |
| Cap 回放（Tk / HTML） | 可用 |
| Cap 实时视图（`--view`） | 可用 |
| 手套零点标定 | 代码有，**无真机记录** |
| 相机延迟测量 | 可用，实测约 191 ms（见 `latency/`） |
| Quest 采集 | **未测**（无头显） |
| 相机内参/外参 | **不存在** |
| 跨源对齐 | **不存在**（设计给了下游 `Paxini-Align/`，未开工） |

一句话：**能采、能看，不能算三维、不能跨源配。**

## 文档权威矩阵

同一个问题往往有好几份文档写过，按这张表找**唯一权威**，别读三份再自己猜：

| 想知道 | 看这份 | 其他文档的角色 |
|---|---|---|
| `data.h5` 里某个字段是什么 | `Paxini-Cap/DOC/HDF5结构说明.md` | 其他都是摘要 |
| 某列时间戳是什么时钟、怎么换算 | `Paxini-Cap/DOC/时间戳说明.md` | 其他都是摘要 |
| 某项测过没有、实测值多少 | `Paxini-Cap/DOC/待验证清单.md` | README 只给结论 |
| 标定要采哪几组量 | `Paxini-Cap/DOC/标定.md` | — |
| 某个参数什么意思 | `Paxini-Cap/Cap_Quest/README.md` §6.2 | `--help` 也是权威 |
| 怎么开发、不能违反什么 | `Paxini-Cap/README.md` §0 | 本目录是补充 |
| 怎么跑起来 | `Paxini-Cap/Cap_Quest/README.md` §1 | — |

改代码前先查这张表，能省掉一半的翻找。

## 三个最常迷路的地方

**1. 以为有个"主程序"能一把跑完。**
没有。`capture/collect_cap.py` 只负责录制，回放在 `viewer/visualize.py`，标定在
`calib/hand_calib.py`，延迟测量在 `latency/`。四个入口互不调用。

**2. 以为采集时会对齐。**
不会，而且**这是刻意设计**，不是没做完。三条行轴行数永远不相等，配对属于下游。
历史上有过一版对齐代码，被整体删除了。

**3. 以为 WSL 里能直接读相机。**
不能。WSL 里相机要走 Windows 的 `ffmpeg.exe` + DirectShow；`camera_backend: auto` 已经
按环境判好了，日志会打印落点。usbipd 转发 UVC 会让 720p 从 30 fps 掉到 5 fps。
