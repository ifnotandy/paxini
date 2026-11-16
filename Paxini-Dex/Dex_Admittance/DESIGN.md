# Dex Admittance 设计说明

## 1. 目标

在固定台架上，用 PXDex III 的十路触觉反馈实现两根活动手指的独立一维抓取力导纳。

输入：

- 食指、中指各自的触觉局部法向力；
- 四路关节实际角度；
- 电机电流、温度和传感器故障。

输出：

- 四路关节角目标 `[index_proximal, index_distal, middle_proximal, middle_distal]`；
- 控制状态、限幅原因和实验日志。

PXDex 保持在 `MOTOR_MODE_POSITION`；不调用力矩、电流或速度目标接口。

## 2. 分层

```text
pxhandsdk callback
  -> latest-frame snapshot
  -> ForceEstimator
  -> AdmittanceEngine
       -> index finger state
       -> middle finger state
       -> safety supervisor
  -> set_motor_angles(q_target)
```

模块：

- `hardware.py`：官方 SDK 适配和最新帧快照；
- `force.py`：十路触觉基线、低通和手指力聚合；
- `admittance.py`：单指导纳律和目标力斜坡；
- `mapping.py`：闭合量到 PIP/DIP 的映射；
- `engine.py`：接近、接触、力控、重接近、保持、释放和故障状态机；
- `simulator.py`：可重复的无硬件对象模型；
- `session.py`：元数据、样本和事件日志；
- `calibration.py`：单关节方向、静态扫描和基线工具；
- `cli.py`：仿真、检查和设备安全壳。

## 3. 触觉力

固定槽位：

| 手指 | 槽位 |
|---|---|
| 食指 | `index_zero`, `index_one` |
| 中指 | `middle_zero`, `middle_one`, `middle_two`, `middle_three` |
| 拇指 | `thumb_one`, `thumb_two` |
| 掌心 | `palm_zero`, `palm_one` |

食指和中指使用可配置权重合成局部 `z` 分量，单位 N：

```text
index  = 1.00 * index_zero  + 0.41 * index_one
middle = 1.00 * middle_zero + 0.41 * middle_one
       + 1.00 * middle_two  + 0.41 * middle_three
```

权重初始值按触觉点数量近似设置。拇指、掌心传感器不控制固定结构运动，但参与全局安全关断。

每个触觉点先减去空载基线，再做一阶低通。局部安全使用未加权三轴模长：

```text
local_j = sqrt((x_j-bx_j)^2 + (y_j-by_j)^2 + max(0, z_j-bz_j)^2)
```

- 活动指任一局部力达到 `1.2 N`：对应手指进入 `LOCAL_RELIEF` 并松开；
- 滤波局部力降到 `1.0 N` 以下并持续 `0.2 s`：再冷却 `0.3 s` 后恢复导纳，避免阈值附近往复开合；
- 同一手指在 6 秒内触发 3 次局部松开：进入 `LOCAL_LIMIT_HOLD`，停止继续找力，提示重新放置物体或降低目标力；
- 拇指/掌心任一局部力达到 `5.0 N`：锁存 `FIXED_SIDE_FORCE_LIMIT` 并停止运动目标。

## 4. 单指导纳

```text
v_c input:
F_c = max(F_filtered, local_filtered_max)
e_F = Fd - F_c
v_c = clamp(Kp * e_F - Kd * v_measured, -v_max, v_max)
s_next = clamp(s + v_c * dt, s_min, s_max)
```

`v_measured` 来自实际 PIP/DIP 角度差分，而不是触觉力的数值微分。这样可避免 0.1 N 量化噪声被 D 项放大。

`s` 是等效 PIP 闭合角度。默认：

```text
index_proximal  = base_proximal  + sign * s
index_distal    = base_distal    + sign * 0.724 * s
middle_proximal = base_proximal  + sign * s
middle_distal   = base_distal    + sign * 0.724 * s
```

最终目标统一裁剪到配置的安全角度范围。角度方向仍必须通过实机小步标定确认。

## 5. 手指状态机

每根活动手指独立经历：

```text
APPROACH
  -> FORCE
  -> LOCAL_RELIEF
  -> LOCAL_LIMIT_HOLD
  -> REAPPROACH
  -> NO_CONTACT
```

- `APPROACH`：从当前角度慢速闭合；
- 力达到 `0.3 N`：记录接触姿态，进入 `FORCE`；
- `FORCE`：导纳闭环；
- 力低于 `0.1 N` 持续 `0.3 s`：进入 `REAPPROACH`；
- 重接近最多 `10°`，仍无接触则进入 `NO_CONTACT`；
- 达到搜索上限后停止，而不是继续向机械限位闭合。

两指共享一个目标力，但分别维护基角、闭合量、速度和接触状态。

## 6. 总体状态

```text
IDLE
APPROACH
CONTROLLING
HOLD
RELEASE
NO_CONTACT
FORCE_LIMIT
FIXED_SIDE_FORCE_LIMIT
STALE_HOLD
STALE_RELEASE
FAULT
STOPPED
```

- `HOLD`：操作员主动冻结；
- `STALE_HOLD`：触觉数据超过 100 ms，被动冻结；
- `STALE_RELEASE`：超过 300 ms，朝张开方向释放；
- `FORCE_LIMIT`：超过 4 N，锁存并释放；
- `FIXED_SIDE_FORCE_LIMIT`：固定侧局部力超过 5 N，停止导纳和运动目标，保持最后位置；
- `FAULT`：传感器故障、超过 1 s 断帧或温度故障，输出张开目标并停止自动控制线程；
- `STOPPED`：停止控制循环，但 CLI 不自动失能。

## 7. 时序

- SDK 完整帧目标：60 Hz；
- 导纳外环：40 Hz，周期 25 ms；
- 触觉滤波：默认 5 Hz 一阶低通；
- SDK 内部位置 P+D：50 Hz，参数不可由当前公开 API 修改。

外环较低是因为 Python 调度、RS-485 通信和位置内环都会引入延迟。滤波、周期和增益必须在实机上重新验证。

## 8. 台架与实验

最低台架：

- 刚性底板或实验板；
- 台钳/C 型夹或型材支架；
- 原装/转接法兰和合适螺钉；
- 掌根或底座刚性固定；
- 急停/断电通道。

不使用机械臂。加载方法：

1. 操作员戴手套轻推、轻拉物体或活动指节；
2. 软绳连接物体，悬挂已知重物做静态载荷；
3. 第一轮最大静态载荷 500 g。

实验顺序：

1. 触觉基线；
2. 四路关节小步方向；
3. 海绵静态力-角度；
4. 海绵 `1/2/3 N` 阶跃；
5. 人工推拉；
6. 塑料杯；
7. 网球；
8. 重复性；
9. 丢帧、超时和故障注入；
10. 缓慢释放和断电。

纸杯、鸡蛋、玻璃球和滑移控制不在第一轮。

## 9. 验收

- 目标力 `1/2/3 N` 的响应方向正确；
- 稳态 3 秒内波动约 `±0.4 N`；
- 不持续振荡；
- 两指可有不同目标角；
- 外力推入时朝释放方向调整；
- 外力撤销后恢复目标力附近；
- 不超过单指 4 N；
- 丢帧或故障后不再继续闭合；
- 所有样本可复盘。

没有外部力计时，不宣称触觉读数已完成绝对牛顿标定。

## 10. 后续

低层力矩/电流、六维阻抗、滑移预测、局部压力优化、视觉和机械臂适配不属于本阶段。
