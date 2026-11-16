# 导纳与阻抗控制开源实现调研

调研日期：2026-11-10。以下结论直接来自官方开源仓库源码。

## ROS 2 admittance_controller

来源：

- <https://github.com/ros-controls/ros2_controllers/tree/3c23b1b453a39ac98437a2e65503dc7f2885abb7/admittance_controller>
- 参数：`src/admittance_controller_parameters.yaml`
- 实现：`include/admittance_controller/admittance_rule_impl.hpp`

可借鉴：

1. 测量力先转换到统一坐标，再进入导纳计算。
2. 力反馈使用指数滤波。
3. 使用质量、刚度和阻尼比；阻尼按
   `D = 2 * zeta * sqrt(M * S)` 计算。
4. 可独立选择控制轴。
5. 导纳输出是参考轨迹的位置/速度修正，再交给位置控制器。

## Franka franka_ros 示例

来源：

- <https://github.com/frankaemika/franka_ros/tree/ddd2fffd9de44b02ad15b4bbb2bfa2cec4d60d98/franka_example_controllers>
- `src/cartesian_impedance_example_controller.cpp`
- `src/teleop_joint_pd_example_controller.cpp`
- `src/joint_wall.cpp`

可借鉴：

1. 笛卡尔阻抗使用 `tau = J^T(-K e - D Jdq)` 并叠加动力学补偿。
2. 对力矩变化率限幅，避免模式切换和参数变化产生冲击。
3. 软限位区逐渐增加阻尼，硬限位区采用更强保护。
4. 接触判断使用外力向量模长与阈值，而不是单一分量。
5. 目标、速度和加速度均经过平滑和饱和。

## 本项目采用的策略

- 保留位置模式下的简化单指导纳；
- 食/中指连续力使用配置化传感器权重；
- 每个活动指的任何局部三轴向量超过 `1.2 N` 时优先松开；
- 拇指/掌心任一局部三轴向量达到 `5 N` 时全局锁存并停止运动目标；
- 固定侧锁存要求操作员执行 `release` 或 `disable`；
- 所有局部模长、加权力、状态和事件写入日志。

## 未采用

- 六维笛卡尔阻抗；
- 力矩控制与动力学补偿；
- 重力/工具负载补偿；
- 滑移估计。

这些依赖当前 PXDex SDK 未公开的力矩接口或完整接触几何。
