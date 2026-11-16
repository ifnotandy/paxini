# Paxini

本仓库包含 PXDex III 的触觉导纳控制、Quest 同步采集和 SDK 示例。
GitHub 只提交源码和说明文档；SDK 安装包作为
[`sdk-v1.0.0` Release](https://github.com/Dongqiao6/Paxini/releases/tag/sdk-v1.0.0)
的附件提供。`.venv/`、`Dex_Quest/recordings/`、`PXDex/` 和 `PXCap/`
保留在本机，不纳入版本控制。

## 目录

```text
Paxini/
├── .venv/           # 三个代码目录共用的 Python 环境
├── Dex_Admittance/  # PXDex 触觉力导纳控制
├── Dex_Quest/       # PXDex 手/双相机 + Quest 3 6D pose 同步采集
├── py_demo/         # PXDex/PXCap3 示例与相机工具
├── sdk/
│   ├── xrobotoolkit/# XRoboToolKit APK 与 PC Service 安装包
│   └── pxhandsdk/   # PXDex SDK 安装包
├── PXDex/           # PXDex 产品资料、SDK 文档和 URDF
└── PXCap/           # PXCap 产品资料和 URDF
```

`sdk/` 中的安装包从 GitHub Release 下载到对应子目录后使用，不会直接提交
到 Git 仓库。`.venv/`、`PXDex/`、`PXCap/`、采集数据和实验结果仅保留在本机。

## 触觉导纳控制

[`Dex_Admittance/`](Dex_Admittance/) 在 PXDex III 的 `MOTOR_MODE_POSITION`
下运行。十路触觉帧以 60 Hz 输入，上位机以 40 Hz 外环为食指和中指分别计算
一维抓取力导纳，最后调用 `set_motor_angles(q_target)`；不直接发送力矩或电流。

```text
10 路局部触觉 + 四路实际关节角 + 目标力
  -> 基线校正、5 Hz 低通、手指力聚合
  -> 食指/中指独立导纳
  -> 四路关节位置目标
  -> PXDex 固件位置内环
```

输入与输出：

| 类型 | 内容 |
|---|---|
| 输入 | 10 个局部三轴力向量（N）；四路实际角 `[IP, ID, MP, MD]`；目标力、导纳参数 |
| 监控输入 | 电机电流、温度、传感器故障、帧时间戳和控制模式 |
| 输出 | 四路位置目标 `q_target=[q_IP,q_ID,q_MP,q_MD]`（°），以及每指状态、速度、力和事件日志 |

单指代码实际采用的一阶导纳律为：

```text
α           = 1 - exp(-2π f_c dt)
tilde f[k]  = α f[k] + (1 - α) tilde f[k-1]
F           = Σ_j w_j max(0, tilde z_j)
L_max       = max_j ||tilde f_j||
F_c         = max(F, L_max)
e           = Fd - F_c                 # |e| <= 0.15 N 时置零
v_m         = 1/2 * [σ Δq_IP/dt + σ Δq_ID/(r dt)]
v           = clamp(Kp e - Kd v_m, -v_max, v_max)
s[k+1]      = clamp(s[k] + v dt, s_min, s_max)

q_IP_target = base_IP + σ s
q_ID_target = base_ID + σ r s
```

`s` 是等效 PIP 闭合量。默认 `Fd=1.2 N`、`Kp=3.0 deg/(s·N)`、
`Kd=0.45`、`v_max=15 deg/s`、`r=0.724`、`σ=+1`。阻尼项使用实际关节角差分
得到的闭合速度，避免直接微分触觉力。接触后以当前姿态为 `base`，目标力按
`1 N/s` 平滑变化。

安全逻辑：单指加权力达到 `4 N` 时锁存并释放；活动指任一局部力达到 `1.2 N`
时先局部松开，重复触发则保持；拇指/掌心局部力达到 `5 N` 时停止运动并保持；
触觉断帧、传感器故障或电机过热时停止自动导纳。

```bash
cd Paxini
source .venv/bin/activate
cd Dex_Admittance

python -m dex_admittance simulate \
  --object soft --profile soft --duration 8

python -m unittest discover -s tests -v
```

实机标定和运行见 [Dex_Admittance/README.md](Dex_Admittance/README.md)。

## SDK 安装包

三个安装包统一发布在
[`sdk-v1.0.0` Release](https://github.com/Dongqiao6/Paxini/releases/tag/sdk-v1.0.0)。

| 安装包 | 用途 | 仓库内放置位置 | 使用该包的程序 |
|---|---|---|---|
| [pxhandsdk_1.0.0.0.ubantu2204_amd64.deb](https://github.com/Dongqiao6/Paxini/releases/download/sdk-v1.0.0/pxhandsdk_1.0.0.0.ubantu2204_amd64.deb) | PXDex III 的 C++/Python 驱动和系统级 Python 包 | `sdk/pxhandsdk/` | `Dex_Admittance/`、`py_demo/`、`Dex_Quest/` |
| [XRoboToolkit_PC_Service_1.0.0_ubuntu_22.04_amd64.deb](https://github.com/Dongqiao6/Paxini/releases/download/sdk-v1.0.0/XRoboToolkit_PC_Service_1.0.0_ubuntu_22.04_amd64.deb) | 在 Ubuntu PC 上接收 Quest 跟踪数据，供 Python SDK 查询 | `sdk/xrobotoolkit/` | `Dex_Quest/quest3.py`、`Dex_Quest/collect_hand_pose.py` |
| [XRoboToolkit-Quest-1.0.1.apk](https://github.com/Dongqiao6/Paxini/releases/download/sdk-v1.0.0/XRoboToolkit-Quest-1.0.1.apk) | 安装在 Quest 3 中，采集并发送手柄和手部 6D pose | `sdk/xrobotoolkit/` | `Dex_Quest/collect_hand_pose.py` 的 Quest 数据源 |

下载后先校验文件完整性：

```bash
cd Paxini
sha256sum -c sdk/SHA256SUMS
```

在 Ubuntu 22.04 PC 上安装两个 DEB。`apt` 会自动处理系统依赖：

```bash
sudo apt install \
  ./sdk/xrobotoolkit/XRoboToolkit_PC_Service_1.0.0_ubuntu_22.04_amd64.deb \
  ./sdk/pxhandsdk/pxhandsdk_1.0.0.0.ubantu2204_amd64.deb

sudo /usr/share/pxhandsdk/scripts/setup-serial-access.sh
```

`pxhandsdk` 必须安装到系统 Python，不能从 PyPI 安装同名包。安装串口权限脚本
后需要重新登录，`py_demo/` 才能访问 `/dev/ttyUSB*`。

通过 USB ADB 将 Quest APK 安装到头显：

```bash
adb devices
adb install -r sdk/xrobotoolkit/XRoboToolkit-Quest-1.0.1.apk
```

`Dex_Quest/` 还需要通过 pip 安装 Python 侧的 `xrobotoolkit-sdk`；它是代码依赖，
与上面的 PC Service 安装包不同。

## Python 环境

三个代码目录共用根目录下的 `.venv`：

```bash
cd Paxini
source .venv/bin/activate

python -c "import pxhandsdk, xrobotoolkit_sdk, cv2, numpy, yaml; print('environment ok')"
```

该环境使用 `--system-site-packages`，因此可以访问通过 DEB 安装的
`pxhandsdk`。`xrobotoolkit-sdk`、OpenCV、NumPy 和 PyYAML 位于共享虚拟环境中。

如果以后需要重建环境：

```bash
cd Paxini
/usr/bin/python3 -m venv --system-site-packages .venv
source .venv/bin/activate
python -m pip install --upgrade xrobotoolkit-sdk opencv-python numpy PyYAML
sudo dpkg -i sdk/pxhandsdk/pxhandsdk_1.0.0.0.ubantu2204_amd64.deb
```

不要通过 pip 安装 `pxhandsdk`，应使用官方提供的 DEB。

## 同步采集

```bash
cd Paxini
source .venv/bin/activate
cd Dex_Quest

python collect_hand_pose.py \
  --pose-source controller \
  --pose-side right
```

详细说明见 [Dex_Quest/README.md](Dex_Quest/README.md)。

## PXDex 示例

```bash
cd Paxini
source .venv/bin/activate
cd py_demo

python test.py
```

`py_demo/Cap_demo/` 还包含 zw 提供的 PXCap3 触觉、编码器实时显示和双腕部
相机预览示例。详细说明见 [py_demo/README.md](py_demo/README.md)。
