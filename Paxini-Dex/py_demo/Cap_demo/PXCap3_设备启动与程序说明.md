# PXCap3 设备启动与程序说明

适用文件：`pxcap3_demo.py`、`camera_preview.py`、`camera_preview_ui.py`、`camera_controls.py`。本文按 2026-10-28 项目中的实际实现整理，供设备交接、首次部署和日常启动使用。

示例项目路径为 `/home/cris/PaXini`；在其他电脑上使用时，请替换为实际目录。示例串口 `/dev/ttyACM0`、摄像头节点 `/dev/video2` 和 `/dev/video6` 都可能随插拔变化，应以现场枚举结果为准。

## 1. 已配置电脑的快速启动

适用于已安装 SDK 和 Python 依赖、设备已连接、访问权限已配置的电脑。首次部署请先完成第 3～5 节。

先关闭 PXCap Studio 中的设备连接及相机预览，释放对应设备。打开两个终端，分别执行：

**终端 A：打开传感器数据窗口。**

~~~bash
cd /home/cris/PaXini
/usr/bin/python3 pxcap3_demo.py /dev/ttyACM0 --frequency 100
~~~

**终端 B：打开双腕部摄像头窗口及参数滑块。**

~~~bash
cd /home/cris/PaXini
/usr/bin/python3 camera_preview.py
~~~

正常启动后的结果：

| 窗口 | 应看到的内容 |
|---|---|
| 传感器数据窗口 | 10 路合力、分布力点数、4 路编码器原始值与关节角、帧数及时间戳持续更新 |
| 双摄像头窗口 | 左侧为大拇指侧腕部画面，右侧为非大拇指侧腕部画面；每路下方各有独立参数滑块 |

两程序可以同时运行：数据程序使用手套串口，视频程序使用两路视频设备。关闭其中一个窗口，另一个程序仍可继续运行。

## 2. 四个文件的分工

| 文件 | 职责 | 是否作为启动入口 |
|---|---|---|
| [pxcap3_demo.py](./pxcap3_demo.py) | 通过 PXHandSDK 连接手套串口，持续采集并显示触觉、编码器、关节角数据 | 是，启动时必须提供实际串口 |
| [camera_preview.py](./camera_preview.py) | 自动识别两台腕部相机、创建独立采集线程、启动双画面窗口，并处理参数调节请求 | 是，通常直接运行即可 |
| [camera_preview_ui.py](./camera_preview_ui.py) | 创建 Tkinter 双画面窗口、两组滑块、自动模式开关及设置反馈 | 由视频入口自动导入 |
| [camera_controls.py](./camera_controls.py) | 通过 Linux V4L2 查询、写入、回读相机参数，并实现恢复启动值 | 由视频入口自动导入 |

将四个文件放在同一目录，推荐交接目录如下：

~~~text
PaXini/
├── pxcap3_demo.py
├── camera_preview.py
├── camera_preview_ui.py
├── camera_controls.py
├── pxhandsdk_1.0.0.0.ubantu2204_amd64.deb
├── pxcap3使用手册.md
└── PXCap3_设备启动与程序说明.md
~~~

相机预览使用标准 V4L2 视频接口和 OpenCV；PXHandSDK 用于传感器数据采集。相机程序的界面和控制模块需要与其入口文件一起交付。

### 2.1 数据程序的工作流程

1. 解析串口和采集频率。
2. 创建 SDK 会话，连接设备，查询版本、SN 和诊断信息。
3. 启动 SDK 连续采集，回调中复制 Python 数值。
4. Tkinter 主线程每 100ms 显示一次最新帧。
5. 用户退出或发生采集故障时停止采集；退出程序时断开串口。

诊断信息和设备版本在采集前查询，不会在连续采集期间反复调用。采集帧中的分布力仍被读取，但窗口只显示点数，不打印每个点的坐标。

### 2.2 视频程序的工作流程

1. 从 Linux 视频设备信息读取 USB VID、PID 和接口索引。
2. 找到同一只手套的大拇指侧与非大拇指侧相机。
3. 每台相机使用独立线程，通过 OpenCV 的 V4L2 后端持续读取图像。
4. Tkinter 主线程在一个窗口内显示两路最新图像。
5. 滑块请求交给对应采集线程，通过 V4L2 写入参数并回读。
6. 退出时通知两路线程停止并释放设备。

快速拖动同一个滑块时，程序合并尚未执行的请求，保留最新目标值。两路画面各自取最新帧，并未实现硬件同步曝光。

## 3. 首次部署：运行环境与依赖

### 3.1 使用匹配 SDK 的系统 Python

本项目随附的 `pxhandsdk_1.0.0.0.ubantu2204_amd64.deb` 对应厂家手册中的环境：

| 项目 | 环境 |
|---|---|
| 操作系统 | Ubuntu 22.04 LTS，64 位 Linux |
| CPU 架构 | x86_64 / amd64 |
| SDK Python 绑定 | 系统 CPython 3.10 |
| 图形环境 | 能打开桌面窗口的本地图形会话 |

检查当前电脑：

~~~bash
uname -m
/usr/bin/python3 --version
~~~

预期分别包含 `x86_64` 和 `Python 3.10.x`。其他系统或 Python 小版本需要匹配的厂家 SDK，不能直接沿用这份二进制绑定。

启动命令统一使用 `/usr/bin/python3`。即使终端前有 `(base)`，显式指定此路径仍会使用系统 Python。

### 3.2 安装图形和视频依赖

~~~bash
sudo apt update
sudo apt install python3-tk python3-opencv
~~~

OpenCV 的系统包会安装所需的 NumPy 依赖；当前窗口使用 Tk 原生图像显示，不需要 Pillow。

检查依赖：

~~~bash
/usr/bin/python3 -c "import tkinter, cv2, numpy; print('GUI 和视频依赖正常，OpenCV:', cv2.__version__)"
~~~

如需后文的设备占用和相机能力查询工具，可选安装：

~~~bash
sudo apt install lsof v4l-utils
~~~

### 3.3 安装 PXHandSDK
SDK 应使用厂家提供的匹配版本安装包，先把.deb包下载到你所指定的目录，在.deb文件所在目录执行：

~~~bash
cd /home/cris/PaXini
sudo dpkg -i ./pxhandsdk_1.0.0.0.ubantu2204_amd64.deb
~~~

如果提示缺少依赖，并且软件源可用：

~~~bash
sudo apt-get -f install
~~~

验证系统 Python 能找到 SDK：

~~~bash
/usr/bin/python3 -c "import pxhandsdk; print('PXHandSDK 导入成功:', pxhandsdk.PxCap3)"
~~~



## 4. 首次部署：设备权限与连接

连接手套的数据接口及两个腕部摄像头，确认供电、USB 连接稳定。

将操作人员账户加入串口和视频设备常用访问组：

~~~bash
sudo usermod -aG dialout,video "$USER"
~~~

执行后注销桌面并重新登录，再重新打开终端或 VS Code。仅在旧 VS Code 会话中增加一个终端，可能仍然沿用旧用户组权限。

重新登录后检查：

~~~bash
id
ls -l /dev/ttyACM* /dev/video* 2>/dev/null
~~~

也可按厂家手册使用 SDK 的串口权限脚本配置串口访问：

~~~bash
sudo /usr/share/pxhandsdk/scripts/setup-serial-access.sh
~~~

该脚本属于 SDK 安装内容；执行后同样需要使新的用户组权限生效。

## 5. 启动前识别设备

### 5.1 确认手套串口

~~~bash
ls -l /dev/ttyACM* 2>/dev/null
ls -l /dev/serial/by-id/ 2>/dev/null
~~~

将现场确认的手套串口填入数据启动命令。若连接多个串口设备，需要结合设备名称、USB 信息或插拔前后的变化确认目标，不能只取编号最小的设备。

### 5.2 识别两路腕部相机

使用程序的只读枚举命令：

~~~bash
cd /home/cris/PaXini
/usr/bin/python3 camera_preview.py --list
~~~

它会列出视频节点、USB VID:PID、接口索引和设备名称，不启动画面采集。

脚本采用的 PXCap3 相机配对规则：

| 手套 | USB VID | 大拇指侧 PID | 非大拇指侧 PID |
|---|---|---|---|
| 左手套 | 0c45 | c000 | cc00 |
| 右手套 | 0c45 | 000c | 00cc |

自动选择使用每台相机的 `index=0` 图像接口。两路应来自两个不同的物理相机，不是同一台相机的两个视频节点。

此前本项目电脑上识别到的右手套示例：

~~~text
/dev/video2: 0c45:000c, index=0, MASTER Camera: MASTER Camera
/dev/video6: 0c45:00cc, index=0, MASTER Camera: MASTER Camera
~~~

这里的 `video2`、`video6` 仅为示例，不是固定编号。多台相机可能具有相同名称、相同序列号，不能仅凭名称或 `/dev/v4l/by-id/` 链接数量判断相机数量。

默认启动会重新识别目标设备，不会自动改用笔记本摄像头。当前脚本也不会在仅找到一路腕部相机时切换为单路预览。

## 6. 常用启动指令集

本节均在项目目录执行：

~~~bash
cd /home/cris/PaXini
~~~

### 6.1 只启动数据窗口

默认 100Hz：

~~~bash
/usr/bin/python3 pxcap3_demo.py /dev/ttyACM0
~~~

指定目标频率，例如 50Hz：

~~~bash
/usr/bin/python3 pxcap3_demo.py /dev/ttyACM0 --frequency 50
~~~

频率允许范围为 1～300Hz。目标频率与实际获得的帧率可能不同，需看窗口内的实际帧率与帧数。

### 6.2 自动启动双腕部视频窗口

~~~bash
/usr/bin/python3 camera_preview.py
~~~

如果左右两只手套的完整相机组同时连接，明确选择手套：

~~~bash
/usr/bin/python3 camera_preview.py --hand right
~~~

或：

~~~bash
/usr/bin/python3 camera_preview.py --hand left
~~~

### 6.3 手动指定两路相机

先使用 `--list` 确认节点，再同时指定两个接口：

~~~bash
/usr/bin/python3 camera_preview.py --thumb-device /dev/video2 --other-device /dev/video6
~~~

`--thumb-device` 为大拇指侧，`--other-device` 为非大拇指侧。程序仍会验证 USB 标识、接口索引和物理设备，防止配错相机。

### 6.4 降低视频采集分辨率

当 USB 带宽、处理负载或画面流畅度需要调整时，可请求每路 640×480：

~~~bash
/usr/bin/python3 camera_preview.py --width 640 --height 480
~~~

请求 15fps：

~~~bash
/usr/bin/python3 camera_preview.py --fps 15
~~~

设备可能拒绝不支持的格式、分辨率或帧率。以终端输出的协商参数和实际更新情况为准。

### 6.5 调整窗口中的预览尺寸

每路显示宽度改为 480 像素：

~~~bash
/usr/bin/python3 camera_preview.py --view-width 480
~~~

这个参数调整显示尺寸，不会直接改变相机采集分辨率。窗口还可手动缩放。

### 6.6 让驱动选择默认输入格式

~~~bash
/usr/bin/python3 camera_preview.py --pixel-format auto
~~~

默认格式为 MJPEG，对应命令参数 `MJPG`；另一可选值为 `YUYV`。输入格式需要设备实际支持。

### 6.7 查看命令行帮助

~~~bash
/usr/bin/python3 pxcap3_demo.py --help
/usr/bin/python3 camera_preview.py --help
~~~

`pxcap3_demo.py` 会先导入 SDK，因此查看帮助也需要 SDK 已正确安装。

### 6.8 保存终端诊断日志

在 Bash 中执行以下设置，使管道保留前一个程序的错误状态：

~~~bash
set -o pipefail
~~~

数据程序日志：

~~~bash
/usr/bin/python3 -u pxcap3_demo.py /dev/ttyACM0 --frequency 100 2>&1 | tee -a pxcap3_data_console.log
~~~

视频程序日志：

~~~bash
/usr/bin/python3 -u camera_preview.py 2>&1 | tee -a camera_preview_console.log
~~~

两个带窗口的程序仍需分别在两个终端启动。`tee -a` 将终端输出追加到日志；这些日志包含启动、连接、错误等信息，不是完整的传感器采集文件，也不是录像。

## 7. 窗口操作

### 7.1 传感器数据窗口

窗口显示：

- 10 路传感器的 X、Y、Z 合力及分布力点数；
- 4 路编码器原始值与关节角；
- 累计回调帧数、目标频率与估算实际帧率；
- SDK 的 Unix 纳秒时间戳与单调纳秒时间戳；
- 最近 2000 行滚动输出，可切换自动滚动。

力值的原始单位为 `0.1N`，例如原始数值 `10` 对应 `1N`；关节角单位为度。时间戳为 SDK 记录的主机侧完整帧可用时间，不代表相机曝光时刻。

当前采集默认 100Hz，界面每秒约刷新 10 次最新帧。分布力仅显示点数，不逐点打印坐标。高频采集期间，中间帧不会全部写入窗口历史。

### 7.2 双摄像头与独立参数滑块

窗口左侧控制大拇指侧腕部相机，右侧控制非大拇指侧腕部相机。每侧均提供以下滑块：

| 参数 | 显示或调节含义 |
|---|---|
| 亮度 | 相机亮度控制 |
| 对比度 | 相机对比度控制 |
| 饱和度 | 相机颜色饱和度控制 |
| 增益 | 相机增益控制 |
| Gamma | 相机 Gamma 控制 |
| 锐度 | 相机锐度控制 |
| 色温 | 手动白平衡色温，单位 K |
| 曝光 | 手动曝光时间，界面显示为 ms |

滑块范围、步长及是否可调由当前驱动读取，程序不会对所有设备强制使用同一套范围。未提供的参数会在窗口中标明。

**手动调曝光：**

1. 在对应画面下取消“自动曝光”。
2. 等待模式切换生效、曝光滑块可用。
3. 拖动曝光滑块，观察图像和设置回读结果。

**手动调色温：**

1. 在对应画面下取消“自动白平衡”。
2. 等待色温滑块可用。
3. 拖动色温滑块，观察图像和设置回读结果。

驱动曝光值每一格为 100µs，界面已换算成毫秒，例如驱动值 `156` 显示为 `15.6ms`。长曝光可能降低实际帧率。

启动时只读取相机参数，不主动改变这些控制值。调节成功后显示驱动回读值；设置失败会显示原因，滑块回到驱动实际值。

### 7.3 恢复相机参数

每组的“恢复启动值”按钮只恢复对应相机在本次程序启动时读取到的值，包括自动模式和手动参数；它不是恢复出厂设置。

相机调节通过 V4L2 直接写入设备。退出窗口不会自动撤销已应用的调整；需要撤销时，应在退出前点击对应相机的“恢复启动值”。

## 8. 正常退出与重新启动

| 程序 | 退出方式 | 资源处理 |
|---|---|---|
| 数据窗口 | 点击“停止采集并退出”、关闭窗口，或在所属终端按 Ctrl+C | 停止 SDK 连续采集，断开串口 |
| 视频窗口 | 点击“停止预览并退出”、关闭窗口、窗口内按 Q / Esc，或在所属终端按 Ctrl+C | 通知两路采集线程停止，释放相机 |

等原程序退出、终端回到提示符后再重新运行，避免重复启动多个实例争用同一设备。硬件断开后，重新插入并再次确认设备节点，再重启相应程序。

## 9. 完整命令行参数

### 9.1 pxcap3_demo.py

| 参数 | 默认值 | 说明 |
|---|---|---|
| `serial_port` | 必填 | 手套串口，例如 `/dev/ttyACM0` |
| `--frequency` | `100` | 目标采集频率，整数 1～300Hz |
| `--upgrade-online` | 不启用 | 配置在线来源并执行固件升级，与离线升级互斥 |
| `--upgrade-offline` | 不启用 | 配置离线 ZIP 包并执行固件升级，与在线升级互斥 |
| `--with-side-effects` | 不启用 | 采集前执行编码器校准、传感器校准、静态磁检测和写入 SN |
| `--sn` | `PXCAP3_DEMO_SN` | 配合 `--with-side-effects` 写入的 SN；单独传入不写 SN |
| `-h / --help` | — | 查看帮助 |

日常启动使用串口和 `--frequency` 即可。升级选项会执行固件升级，不需要同时传入 `--with-side-effects`。后者会同时执行上述维护动作，并将 SN 写为 `--sn` 的值；未指定时会写入默认示例 SN。维护和升级应按厂家手册安排，不属于日常预览步骤。

### 9.2 camera_preview.py

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--list` | 不启用 | 仅列出视频节点与 USB 身份，不打开相机 |
| `--hand` | `auto` | 选择 `auto`、`left` 或 `right`；表示手套左右手 |
| `--thumb-device` | 自动识别 | 手动指定大拇指侧视频节点，需配合另一侧参数 |
| `--other-device` | 自动识别 | 手动指定非大拇指侧视频节点，需配合另一侧参数 |
| `--width` | `1280` | 每路请求的采集宽度，正整数 |
| `--height` | `720` | 每路请求的采集高度，正整数 |
| `--fps` | `30` | 每路请求的采集帧率，正整数 |
| `--view-width` | `640` | 每路预览显示宽度，正整数 |
| `--pixel-format` | `MJPG` | 输入格式，可选 `MJPG`、`YUYV`、`auto` |
| `-h / --help` | — | 查看帮助 |

`camera_preview_ui.py` 和 `camera_controls.py` 不提供独立启动参数，由入口自动加载。

## 10. 常见故障排查

### 10.1 SDK 无法导入或 Python 版本不匹配

常见表现：`No module named pxhandsdk`、找不到 SDK 类、绑定动态库加载失败。

检查：

~~~bash
/usr/bin/python3 --version
dpkg -l pxhandsdk
/usr/bin/python3 -c "import pxhandsdk; print(pxhandsdk.PxCap3)"
~~~

确认厂家 SDK 版本、操作系统、CPU 架构和 Python ABI 匹配。若之前使用了 Conda 的 `python`，改用本文的系统 Python 命令。

### 10.2 串口不存在或无权限

~~~bash
ls -l /dev/ttyACM* 2>/dev/null
id
~~~

- 不存在：检查供电、USB 连接及实际枚举结果。
- 无权限：检查 `dialout` 组及节点权限，配置后重新登录。
- 重插后编号变化：更新启动命令中的串口。

### 10.3 串口报 102 / SERIAL_BUSY

查询占用：

~~~bash
lsof -nP /dev/ttyACM0
~~~

将路径换成实际串口。若看到 PXCap Studio、串口工具或旧的数据程序持有设备，在对应程序中停止采集并断开连接，再启动本项目数据窗口。同一物理串口保留一个活动 SDK 会话。

### 10.4 摄像头无法打开或未找到完整相机组

先重新枚举：

~~~bash
/usr/bin/python3 camera_preview.py --list
ls -l /dev/video* 2>/dev/null
~~~

核对 USB VID:PID 是否符合第 5 节的配对规则、两路 `index=0` 接口是否均存在，以及当前用户的视频访问权限。

若仅找到笔记本相机、仅一路腕部相机，或左右两组同时存在，程序会给出相应选择错误；需补全连接或用 `--hand` 指定手套。

查询示例节点的占用：

~~~bash
lsof -nP /dev/video2 /dev/video6
~~~

这两个节点必须替换为 `--list` 中的实际图像节点。关闭占用对应相机的旧预览，再重试。

### 10.5 已打开设备，但没有有效画面或画面不流畅

可以查看设备支持的格式与参数控件：

~~~bash
v4l2-ctl --device /dev/video2 --list-formats-ext
v4l2-ctl --device /dev/video2 --list-ctrls-menus
~~~

以上也是示例节点，应替换为实际设备。两台相机分别检查。

再依次检查：

1. 是否都出现“已收到首帧”日志，窗口帧数是否增长。
2. USB 连接、供电和可用带宽是否稳定。
3. 先尝试第 6.4 节的低分辨率命令。
4. 如果手动曝光很长，缩短曝光或重新启用自动曝光。
5. 检查请求格式是否受支持，必要时尝试 `--pixel-format auto`。

终端的“帧率参数 30fps”是协商参数，不是实际性能测量；实际情况还需观察帧数增长和最新画面时间。

### 10.6 曝光或色温滑块无法拖动

优先检查对应自动模式。自动曝光开启时手动曝光不可调，自动白平衡开启时手动色温不可调；关闭后等待驱动回读刷新。

其他只读、暂时被驱动占用或设备不支持的参数，也可能被禁用。查看面板中的具体反馈；参数写入失败不会被当作成功显示。

### 10.7 窗口打不开

检查图形会话和依赖：

~~~bash
echo "$DISPLAY"
/usr/bin/python3 -c "import tkinter, cv2; print('图形模块导入正常')"
~~~

在本地桌面终端运行。无显示会话的纯 SSH 终端不能直接显示这些窗口；需要另行配置可用的图形会话。

### 10.8 数据窗口显示连续 5 秒未收到采集帧

数据程序会停止采集并保留错误提示。检查连接、供电、串口及 SDK 错误日志，关闭窗口，重新枚举并重启。不要将窗口中停留的旧数值当作设备仍在持续更新。

## 11. 当前实现边界与交接验证

当前四个文件提供实时查看和相机参数调节，不自动保存完整传感器数据、图片或录像，也没有将传感器与相机做硬件时间同步。

交接时可实际完成以下验证：

1. 数据窗口连接成功，帧数和时间戳持续增长；轻触传感器后相应合力发生变化。
2. 双视频窗口两路都收到首帧，且帧数持续增长。
3. 小幅调节一侧对比度，确认反馈成功、另一侧的参数数值保持原值；结束前按需恢复启动值。
4. 分别关闭两个窗口，确认程序退出后能再次启动。

若交接目录中还包含 `test_camera_controls.py`，维护人员可运行无需真实设备的参数逻辑回归检查：

~~~bash
/usr/bin/python3 -m unittest -v test_camera_controls.py
~~~

## 12. 相关文件

- [厂家 PxCap3 使用手册](./pxcap3使用手册.md)：SDK 安装、接口约束、设备维护及错误码。
- [摄像头调用说明](./摄像头调用说明.md)：配套上位机中的相机接入来源、USB 配对规则和历史实测记录。
