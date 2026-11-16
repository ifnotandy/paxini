# Paxini

PXCap III 数据手套与 PXDex III 灵巧手的开发工作区。采集上位机、执行器 SDK 示例、厂商资料集中在一个仓库里管理。

## 目录

| 路径 | 内容 |
|---|---|
| `Paxini-Cap/` | Cap 采集上位机：手套 + 双路相机 + Quest 3 同步采集，输出单个 HDF5。详见 [`Paxini-Cap/README.md`](Paxini-Cap/README.md) |
| `Paxini-Dex/` | Dex 执行器：SDK 封装、`Dex_Quest` 采集流程、`Dex_Admittance` 导纳控制 |
| `厂商资料/` | 厂商 PDF 说明书、SDK 使用手册、PXCap / PXDex 的 URDF 与 STL |
| `厂商数据采集/` | 厂商上位机产出的原始录制数据（**不入库**，仅本地保留） |
| `Quest3_功能与接口清单.xlsx` | Quest 3 功能与接口梳理 |
| `帕西尼PXHandSDK功能清单_amd64_1.0.0.0.xlsx` | PXHand SDK 接口清单 |
| `厂商上位机判分标准.txt` | 上位机能力对照标准 |
| `启动PXCap.bat` / `PXCap Studio.lnk` | 本地启动入口 |

## 安装包与二进制

单个文件 8–171 MB，不纳入 Git，统一放在 [Releases](../../releases) 的 `vendor-binaries-v1.0`。克隆后按下表放回：

| 文件 | 放回位置 |
|---|---|
| `ffmpeg.exe` | `Paxini-Cap/sdk/ffmpeg-win/` |
| `pxhandsdk_*.deb` | `Paxini-Cap/sdk/` |
| `XRoboToolkit_PC_Service_*.deb` | `Paxini-Cap/sdk/` |
| `XRoboToolkit-Quest-*.apk` | `Paxini-Cap/sdk/xrobotoolkit/` |
| `pxcap-studio_*.deb` | 本机安装即可，不必放回仓库 |

原始 deb 的校验值见 `Paxini-Cap/sdk/SHA256SUMS`。

## 环境

- Python 3.10+，依赖见 `Paxini-Cap/requirements.txt`
- Ubuntu 22.04（相机 V4L2 直采、SDK 运行环境）或 Windows + WSL2
- Quest 3 需 `adb` 连接，设备 IP 记录在 `Cap_Quest/.quest_ip`（不入库）

## 版本库说明

本仓库 2026-11-16 由三个来源合并而成，历史不连续：

- `Paxini-Cap/` 原仓库 `ifnotandy/Paxini-Cap`（冻结在 72c6058）
- `Paxini-Dex/` 原仓库 `Dongqiao6/Paxini`（同步至 fb224f2）
- `厂商资料/` 与根目录文档为直接归档

原两个 `.git` 目录备份在 `C:\Users\Andy\Desktop\paxini-git备份\`，需要拆回独立仓库时可用。
