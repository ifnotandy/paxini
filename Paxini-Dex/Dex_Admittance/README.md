# Dex_Admittance

基于 PXDex III 位置模式和十路触觉反馈，实现食指、中指独立的一维抓取力导纳。
PXDex 固件只执行位置内环；导纳计算在上位机完成，最终通过
`set_motor_angles(q_target)` 下发四路目标角。

## 控制结构

```text
10 路局部触觉 + 四路实际关节角 + 目标力
  -> 基线校正、低通、手指力聚合
  -> 食指/中指独立力导纳
  -> [PIP, DIP] 位置目标
  -> PXDex MOTOR_MODE_POSITION
```

默认触觉帧率为 60 Hz，导纳外环周期为 25 ms（40 Hz）。两根活动手指共享目标力，
但分别维护接触姿态、闭合量、速度和状态。

### 输入

| 输入 | 内容 |
|---|---|
| 触觉 | 10 个局部三轴力向量（N） |
| 关节角 | `[index_PIP, index_DIP, middle_PIP, middle_DIP]`（°） |
| 控制参数 | 目标力、`soft/semi_rigid/rigid` 导纳配置 |
| 安全监控 | 时间戳、电机电流、温度、传感器故障、控制模式 |

默认传感器分配：

| 部位 | 槽位 | 力聚合权重 |
|---|---|---|
| 食指 | `[2, 3]` | `[1.00, 0.41]` |
| 中指 | `[4, 5, 6, 7]` | `[1.00, 0.41, 1.00, 0.41]` |
| 拇指 | `[0, 1]` | 仅参与固定侧安全 |
| 掌心 | `[8, 9]` | 仅参与固定侧安全 |

### 输出

| 输出 | 内容 |
|---|---|
| 位置目标 | `q_target=[q_IP,q_ID,q_MP,q_MD]`（°） |
| 每指状态 | `APPROACH/FORCE/LOCAL_RELIEF/LOCAL_LIMIT_HOLD/HOLD/NO_CONTACT`、`s`、速度和目标力 |
| 日志 | 滤波后十路力、局部力、温度、电流、控制状态和事件 |

## 控制律

每个触觉点先减去空载基线，再使用一阶低通：

```text
f_j         = (raw_x-bx, raw_y-by, max(0, raw_z-bz))
alpha       = 1 - exp(-2*pi*f_c*dt)
tilde_f_j[k] = alpha*f_j[k] + (1-alpha)*tilde_f_j[k-1]
```

食指、中指分别按权重聚合法向力，并取局部三轴力模长的最大值参加控制：

```text
F_i       = sum_j(w_j * max(0, tilde_z_j))
L_i,max   = max_j(||tilde_f_j||)
F_c       = max(F_i, L_i,max)
e         = Fd - F_c
```

`|e| <= 0.15 N` 时力误差置零。单指一阶导纳律为：

```text
v_measured = 1/2 * [sigma*dq_PIP/dt + sigma*dq_DIP/(r*dt)]
v_c        = clamp(Kp*e - Kd*v_measured, -v_max, v_max)
s[k+1]     = clamp(s[k] + v_c*dt, s_min, s_max)

q_PIP_target = base_PIP + sigma*s
q_DIP_target = base_DIP + sigma*r*s
```

其中 `s` 是等效 PIP 闭合量，`sigma=+1`，`r=0.724`。
`Kp` 将力误差转换为闭合速度，`Kd` 对实际关节角差分得到的闭合速度做阻尼。
最终目标统一裁剪到配置的安全角度范围；触达上下限时该周期速度置零。

默认 `Fd=1.2 N`、`Kp=3.0 deg/(s*N)`、`Kd=0.45`、`v_max=15 deg/s`、
低通截止频率 `5 Hz`。接触后以当前姿态作为 `base`，目标力按最大 `1 N/s`
平滑变化。

## 状态与安全

每根活动指独立执行：

```text
APPROACH --力>=0.3 N--> FORCE --力<0.1 N持续0.3 s--> REAPPROACH
                         |
                         +--局部力>=1.2 N--> LOCAL_RELIEF
```

- `APPROACH`：从当前姿态慢速闭合，达到 `0.3 N` 后记录接触姿态。
- `FORCE`：运行上述导纳闭环；重接触每次最多搜索 `10°`。
- `LOCAL_RELIEF`：对应手指先松开；恢复后冷却 `0.3 s`。
- `LOCAL_LIMIT_HOLD`：同一手指 6 秒内触发 3 次局部松开后，两指同步保持。
- `FORCE_LIMIT`：单指力达到 `4 N`，锁存并向张开方向释放。
- `FIXED_SIDE_FORCE_LIMIT`：拇指/掌心任一局部力达到 `5 N`，停止运动并保持。
- 触觉超过 `0.1/0.3/1.0 s` 未更新时分别冻结、释放、故障；温度超过 `80°C`
  持续 `5 s` 进入故障。

## 快速开始

需要 Python 3.10+、`pxhandsdk` 和 PyYAML。仓库根目录的 `.venv` 已配置好共享环境：

```bash
cd Dex_Admittance
source ../.venv/bin/activate

# 无硬件仿真
python -m dex_admittance simulate \
  --object soft --profile soft --duration 8

# 自动测试
python -m unittest discover -s tests -v
```

## 实机流程

先按仓库根 README 安装 SDK、配置 `/dev/ttyUSB*` 串口权限并清空手部空间。
连接设备会使能电机，因此所有连接命令都显式要求
`--ack-connect-enables-motors`。

```bash
# 1. 只读检查
python -m dex_admittance check \
  --port /dev/ttyUSB0 \
  --ack-connect-enables-motors

# 2. 空载触觉归零和残余基线
python -m dex_admittance calibrate-sensors \
  --port /dev/ttyUSB0 --confirm-zero \
  --ack-connect-enables-motors

python -m dex_admittance calibrate-baseline \
  --port /dev/ttyUSB0 --duration 5 \
  --ack-connect-enables-motors
```

首次使用还应空载检查四路关节方向。关节顺序为
`0=食指PIP, 1=食指DIP, 2=中指PIP, 3=中指DIP`：

```bash
python -m dex_admittance calibrate-joint \
  --port /dev/ttyUSB0 --joint 0 --switch-position-mode \
  --ack-connect-enables-motors
```

启动交互控制，并显式加载最新基线：

```bash
python -m dex_admittance device-shell \
  --port /dev/ttyUSB0 \
  --profile soft \
  --baseline "$(ls -t data/calibration/baseline-*.json | head -1)" \
  --ack-connect-enables-motors
```

进入 shell 后依次执行：

```text
mode-position
MODE
prepare
start
```

`start` 会先以 `15 deg/s` 预张开。根据提示轻放物体后输入 `GRASP`；
运行中可用 `force 1.0` 修改目标力，用 `hold`/`release` 保持或释放。
结束时输入 `release`、`stop`、`quit`。`stop` 保持电机状态，`disable` 才会失能。
位置模式不会跨连接保持。

## 配置与日志

默认参数见 [config/default.yaml](config/default.yaml)，完整设计见
[DESIGN.md](DESIGN.md)。

每次实验写入 `data/<时间>-<物体>-<目标力>/`：

```text
metadata.json  samples.csv  events.csv  config.yaml
```

`data/*` 已被 Git 忽略。需要持续刷新终端遥测时添加 `--live-telemetry`；
SDK 原始日志默认写入 `data/sdk-logs/`。

常见故障：`MOTOR_MODE_IDLE` 需重新执行 `mode-position`；`SERIAL_NOT_EXIST`
检查串口、供电和 USB-RS485；`NO_CONTACT` 先 `release` 再重新放物体；
`FORCE_LIMIT` 和 `FIXED_SIDE_FORCE_LIMIT` 应停止实验并检查接触位置。

## 范围

当前只实现固定台架上的双指一维力导纳。不包含机械臂笛卡尔阻抗、
六维力控、滑移预测、视觉或 PXDex 低层力矩/电流控制。
