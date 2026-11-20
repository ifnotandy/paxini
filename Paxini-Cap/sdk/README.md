# SDK 安装包

二进制不入库（`.deb` / `.apk` 都被 `.gitignore` 排除），只保留校验和与本说明。

## 1. 下载

Release 地址：

```text
https://github.com/Dongqiao6/Paxini/releases/tag/sdk-v1.0.0
```

三个文件，缺一不可：

| 文件 | 大小 | 用途 |
|---|---|---|
| `pxhandsdk_1.0.0.0.ubantu2204_amd64.deb` | 11.5 MB | 手套/执行器 SDK（含 `pxhandsdk.pxcap3` 绑定） |
| `XRoboToolkit-Quest-1.0.1.apk` | 55.1 MB | Quest 3 端 APK，`Cap_Quest/quest3.py` 会用它 |
| `XRoboToolkit_PC_Service_1.0.0_ubuntu_22.04_amd64.deb` | 100.0 MB | Quest 数据落地的 PC Service |

```bash
export HTTPS_PROXY=http://127.0.0.1:7897    # gh/curl 不读 Windows 系统代理
gh release download sdk-v1.0.0 --repo Dongqiao6/Paxini -D sdk/
```

不设代理时实测只有 20~44 KB/s，设了约 3.9 MB/s。

## 2. 校验

```bash
cd sdk
sha256sum -c SHA256SUMS
```

三个都是 `OK` 才继续。手工核对期望值：

```text
9596ba0f94310b8a9fecc0643f291200250400d8cad02cdfe25889f562304d7f  pxhandsdk_1.0.0.0.ubantu2204_amd64.deb
c8a077451e7f6b66ca688668d7b32f5a6a6c19a8c0fa4657f2e1344493c174df  XRoboToolkit-Quest-1.0.1.apk
61961067eb4b41f81ed7cae35f4690dbb0ddfefb329a12b24e0b90ebc46ada91  XRoboToolkit_PC_Service_1.0.0_ubuntu_22.04_amd64.deb
```

## 3. 安装

```bash
# 手套 SDK
sudo dpkg -i pxhandsdk_1.0.0.0.ubantu2204_amd64.deb
sudo /usr/share/pxhandsdk/scripts/setup-serial-access.sh
# 注销后重新登录，dialout 组才生效

# PC Service（Quest 数据链路）
sudo dpkg -i XRoboToolkit_PC_Service_1.0.0_ubuntu_22.04_amd64.deb

# APK 不用手动装到设备：quest3.py 会通过 ADB 安装并启动。
# 但文件必须放在代码默认位置，否则要加 --apk 指定
mkdir -p xrobotoolkit
mv XRoboToolkit-Quest-1.0.1.apk xrobotoolkit/
```

`quest3.py` 默认从 `Paxini-Cap/sdk/xrobotoolkit/XRoboToolkit-Quest-1.0.1.apk` 找 APK，
用 `--apk` 可以指向别处。

### 系统 python3 是 3.12 时报依赖不满足

官方 `pxhandsdk` deb 声明 `Depends: python3 (<< 3.11)`，而 Ubuntu 22.04 上
`python3` 已经是 **3.12**，所以：

- `sudo dpkg -i` 直接失败（依赖不满足）
- 跟着跑 `sudo apt-get install -f` 更糟：它会把刚装的包**卸掉**

这不是包坏了，是依赖声明写死了版本区间。包里 4 个扩展全是 `cpython-310` ABI，
本来也只能被 `python3.10` 加载，所以把依赖改成 `python3.10` 是让声明对齐事实。

修复包是**本地生成**的，不入库（`.deb` 已被 `.gitignore` 排除）。已生成过就直接装：

```bash
sudo dpkg -i pxhandsdk_1.0.0.0.ubantu2204_amd64.py310fix.deb
```

换机器或包丢了，按下面重做：

```bash
WORK=$(mktemp -d)
dpkg-deb -R pxhandsdk_1.0.0.0.ubantu2204_amd64.deb "$WORK"
sed -i 's/^Depends: python3 ([^)]*)/Depends: python3.10/' "$WORK/DEBIAN/control"
chmod 755 "$WORK"/DEBIAN/*inst          # 官方包里是 664，--build 会报 bad permissions
dpkg-deb --build --root-owner-group "$WORK" pxhandsdk_1.0.0.0.ubantu2204_amd64.py310fix.deb
```

**不要动系统的 `python3`（3.12）和 `/usr/bin/python3.10`，两个都要留着。**
装完 `apt-get check` 返回 0 就说明依赖树没被弄坏。

### PC Service 怎么起

这个 deb **不是库，是常驻进程**：装完 `/opt/apps/roboticsservice/` 下有
`RoboticsServiceProcess`（服务本体）、`SDK/x64/libPXREARobotSDK.so`（Python SDK
底层链接的 C++ 库），以及 4 个启动脚本。官方没提供 systemd 单元，只放了一个桌面
菜单项（指向 `run3D.sh`），所以要自己启动。

| 脚本 | 做什么 | 用不用 |
|---|---|---|
| `runService.sh` | 只后台启动 `RoboticsServiceProcess` | **用这个** |
| `run2D.sh` | 服务 + 2D 演示程序 | 不用 |
| `run3D.sh` | 服务 + Unity 三维演示（桌面菜单入口） | 不用，WSL 无显示会报错 |
| `runRobotDataRecorder.sh` | 服务 + 录制演示 | 不用 |

```bash
# 启动（脚本末尾是 ./RoboticsServiceProcess &，命令会立即返回）
/opt/apps/roboticsservice/runService.sh

# 确认在跑
ps -eo pid,cmd | grep [R]oboticsServiceProcess
```

或者让采集程序代劳（内部用 `start_new_session=True`，不受终端关闭影响）：

```bash
python3.10 collect_cap.py --start-pc-service ...
```

不加 `--start-pc-service` 时，检测不到服务只会打警告，不会退出（`--no-quest`
本来就不需要它）。

## 4. 装完确认

```bash
python3.10 -c "import pxhandsdk; print(pxhandsdk.PxCap3)"
dpkg -L pxhandsdk | grep -E 'pxcap3|PxCap3SDK'
```

第一条必须打印出类对象。系统默认 `python3`（3.12）会 `import` 成功但拿不到
`PxCap3`——那是 Python 版本不匹配，不是安装失败。

## 5. Windows 版 ffmpeg（**已归档，main 不需要**）

> 2026-11-20 起 `main` 只用原生 V4L2，本节仅对 `archive/wsl-backend` 分支有效。
> 当时的做法是 WSL 调 Windows 的 `ffmpeg.exe` 走 DirectShow：usbipd 转发 UVC 会丢
等时包，720p 从 30 fps 掉到 5 fps。原生 Linux 不需要这一节。

放这里即可，代码默认读这个路径：

```text
Paxini-Cap/sdk/ffmpeg-win/ffmpeg.exe
```

`*.exe` 已被 `.gitignore` 排除，换机器要自己放一份。来源二选一：

- 从 gyan.dev / BtbN 下载 Windows 静态版，解出 `ffmpeg.exe`
  （实测可用：`ffmpeg 9.0.2-essentials_build`，约 100 MB）
- 装过 `imageio-ffmpeg` 的 Python 环境里直接拷：
  `<Python>/Lib/site-packages/imageio_ffmpeg/binaries/ffmpeg-win-x86_64-*.exe`

只需要 `ffmpeg.exe`，不需要 `ffprobe.exe`：回填 pts 用的是 Linux 侧 ffprobe，
mkv 在 `/mnt/c` 下两边都能读。

确认能用：

```bash
Paxini-Cap/sdk/ffmpeg-win/ffmpeg.exe -hide_banner -version | head -1
Paxini-Cap/sdk/ffmpeg-win/ffmpeg.exe -hide_banner -f dshow -list_devices true -i dummy
```

第二条会列出 DirectShow 视频设备。相机重名时以 `Alternative name` 为准，
采集程序用的就是它。
