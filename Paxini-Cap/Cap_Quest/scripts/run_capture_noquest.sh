#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# run_capture_noquest.sh —— 无 Quest 版一键采集（PXCap III 手套 + 双路相机）
#
# 【适用范围】只接了手套和相机、没有 Quest 3 头显的采集。脚本固定带 --no-quest，
#            不会去等 ADB / pose，也不会写半截 Quest 数据。有头显时请直接跑
#            collect_cap.py，不要用这个脚本。
#
# 【它做的事】等价于手工依次执行下面这些命令，只是每步都先检查、失败即停：
#             export PATH="$HOME/bin:$PATH"
#             sudo timedatectl set-ntp false
#             usbipd detach --busid 3-3 && usbipd attach --wsl --busid 3-3
#             python3.10 capture/collect_cap.py --no-quest --cap-side right
#             采完读 manifest.json 打印摘要并检查 verify.row_mismatch
#
#            usbipd 那步只在 /dev/ttyACM0 不存在时才做（WSL 空闲约 60 秒会关掉，
#            attach 随之失效）；busid 自动从 usbipd list 里按 28e9:018a 查，
#            也可以用 USBIPD_BUSID=3-3 覆盖。
#
# 【用法】（下面几条都在 Cap_Quest/ 根目录执行，脚本自己会 cd 到 capture/）
#   bash scripts/run_capture_noquest.sh                  一直采，Ctrl+C 停（只按一次）
#   bash scripts/run_capture_noquest.sh -d 60            采 60 秒
#   bash scripts/run_capture_noquest.sh -d 60 --view     边采边看（要 WSLg 或物理显示器）
#   bash scripts/run_capture_noquest.sh -d 10 --dry-run  只打印将要执行的命令，不启动
#   bash scripts/run_capture_noquest.sh -- --video-codec h264     透传未列出的参数
#
# 【退出码】0 正常；2 环境检查不过（没串口/没 ffmpeg/没 SDK）；
#          3 采集跑完了但 manifest 的行数校验没通过
# ---------------------------------------------------------------------------
set -uo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# 本脚本在 scripts/ 下，采集程序在 capture/、配置与输出在上一层
QUEST_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
cd "$QUEST_ROOT/capture" || { echo "[X] 无法进入采集目录"; exit 2; }

PY="${PYTHON:-python3.10}"
DURATION=""
OUT_DIR=""
VIEW=0
DRY=0
SKIP_NTP=0
PASS_ARGS=()

usage() {
  sed -n '2,26p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
  case "$1" in
    -d|--duration)   DURATION="${2:-}"; shift 2 || { echo "[X] -d 需要一个秒数"; exit 2; } ;;
    -o|--out)        OUT_DIR="${2:-}"; shift 2 || { echo "[X] -o 需要一个目录"; exit 2; } ;;
    --view)          VIEW=1; shift ;;
    --no-ntp-check)  SKIP_NTP=1; shift ;;
    --dry-run)       DRY=1; shift ;;
    -h|--help)       usage; exit 0 ;;
    --)              shift; PASS_ARGS=("$@"); break ;;
    *) echo "[X] 未知参数：$1（collect_cap.py 自身的参数请写在 -- 之后）" >&2; exit 2 ;;
  esac
done

step() { printf '\n[%s] %s\n' "$1" "$2"; }
fail() { printf '[X] %s\n' "$1" >&2; exit 2; }

# --- 1/6 Python 解释器与手套 SDK -------------------------------------------
step 1/6 "Python 与 PXCap SDK"
command -v "$PY" >/dev/null 2>&1 || fail "找不到 $PY。本工程必须用 python3.10（系统 3.12 里没有 PxCap3）"
if ! "$PY" -c 'import pxhandsdk; assert hasattr(pxhandsdk, "PxCap3")' >/dev/null 2>&1; then
  fail "$PY 里 import pxhandsdk 失败或没有 PxCap3：检查是否装了 pxhandsdk 1.0.0.0、是否误用了 python3.12"
fi
printf '  %s  %s\n' "$PY" "$("$PY" -c 'import sys; print(sys.version.split()[0])')"

# --- 2/6 ffmpeg / ffprobe ---------------------------------------------------
# 查找顺序与代码里一致：$FFMPEG > PATH > 常见安装位置。找到后把它的目录加到
# PATH 最前面，这样 collect_cap.py 里的 shutil.which("ffmpeg") 也能命中同一个。
step 2/6 "ffmpeg / ffprobe"
FFMPEG_BIN="${FFMPEG:-}"
if [ -z "$FFMPEG_BIN" ]; then
  FFMPEG_BIN="$(command -v ffmpeg 2>/dev/null || true)"
fi
if [ -z "$FFMPEG_BIN" ]; then
  for cand in "$HOME/bin/ffmpeg" /usr/local/bin/ffmpeg /usr/bin/ffmpeg /opt/ffmpeg/bin/ffmpeg; do
    if [ -x "$cand" ]; then FFMPEG_BIN="$cand"; break; fi
  done
fi
[ -n "$FFMPEG_BIN" ] || fail "找不到 ffmpeg。把静态版（含 ffprobe）放到 ~/bin/ 或 PATH 里，或用 FFMPEG=/path/to/ffmpeg 指定"
FFPROBE_BIN="$(dirname -- "$FFMPEG_BIN")/ffprobe"
[ -x "$FFPROBE_BIN" ] || FFPROBE_BIN="$(command -v ffprobe 2>/dev/null || true)"
[ -n "$FFPROBE_BIN" ] || fail "找不到 ffprobe（要和 ffmpeg 放在同一目录，或单独在 PATH 里）"
PATH="$(dirname -- "$FFMPEG_BIN"):$PATH"
export PATH
printf '  ffmpeg  %s\n  ffprobe %s\n' "$FFMPEG_BIN" "$FFPROBE_BIN"

# 相机后端预检。wsl 后端要一份 Windows 版 ffmpeg.exe（不入库）；linux 后端要
# /dev/video* 节点。这台机器的配置是 wsl，换机器/换目录时最容易在这里踩空。
BACKEND="$("$PY" -c 'import json,os,sys; p=sys.argv[1]; print((json.load(open(p)).get("camera_backend") or "linux") if os.path.isfile(p) else "linux")' "$QUEST_ROOT/cap_config.json" 2>/dev/null || echo linux)"
if [ "$BACKEND" = "wsl" ]; then
  WIN_FFMPEG="${FFMPEG_WIN:-$(cd "$QUEST_ROOT/.." && pwd)/sdk/ffmpeg-win/ffmpeg.exe}"
  if [ -f "$WIN_FFMPEG" ]; then
    printf '  相机后端 wsl，ffmpeg.exe %s\n' "$WIN_FFMPEG"
  else
    printf '  [!] 相机后端是 wsl，但没找到 %s\n' "$WIN_FFMPEG"
    printf '  [!] 先补上它（或用 FFMPEG_WIN 指定），否则会卡在相机起不来\n'
  fi
else
  printf '  相机后端 linux，/dev/video* 节点 %s 个\n' "$(ls /dev/video* 2>/dev/null | wc -l)"
fi

# --- 3/6 手套串口（必要时自动 detach/attach）--------------------------------
# 只转发手套；相机走 Windows DirectShow（camera_backend: wsl），不需要 usbipd。
# WSL 发行版空闲约 60 秒（vmIdleTimeout 默认）就会关掉，attach 跟着失效，而
# usbipd list 仍显示 Attached —— 所以这里不看状态列，只认 /dev/ttyACM0 在不在，
# 不在就当场重连，省掉每次手工 detach/attach。
step 3/6 "手套串口"

find_port() {
  local p
  for p in /dev/ttyACM* /dev/ttyUSB*; do
    [ -e "$p" ] && { printf '%s' "$p"; return 0; }
  done
  for p in /dev/serial/by-id/*Paxini*; do
    [ -e "$p" ] && { printf '%s' "$p"; return 0; }
  done
  return 1
}

PORT="$(find_port || true)"
if [ -z "$PORT" ]; then
  USBIPD="$(command -v usbipd.exe 2>/dev/null || true)"
  if [ -z "$USBIPD" ]; then
    fail "看不到手套串口，也没有 usbipd.exe。原生 Linux 主机请检查线缆与 dialout 权限"
  fi
  BUSID="${USBIPD_BUSID:-}"
  if [ -z "$BUSID" ]; then
    BUSID="$("$USBIPD" list 2>/dev/null | awk '/28e9:018a/ {print $1; exit}')"
  fi
  if [ -z "$BUSID" ]; then
    fail "usbipd 列表里没有 28e9:018a：手套没插好，或 USB 口换了（可用 USBIPD_BUSID 指定 busid）"
  fi
  echo "  WSL 里没有串口节点，重连 usbipd（busid $BUSID）..."
  # detach 之后设备会重新枚举，紧接着 attach 会报 "There is no device with
  # busid"，所以必须重试；每次 attach 后轮询节点最多 3 秒。
  "$USBIPD" detach --busid "$BUSID" >/dev/null 2>&1 || true
  for attempt in 1 2 3 4 5; do
    ATTACH_OUT="$("$USBIPD" attach --wsl --busid "$BUSID" 2>&1 || true)"
    for _ in 1 2 3 4 5 6; do
      PORT="$(find_port || true)"
      [ -n "$PORT" ] && break
      sleep 0.5
    done
    [ -n "$PORT" ] && break
    echo "  第 $attempt 次 attach 未生效：$(printf '%s' "$ATTACH_OUT" | tail -1)"
    sleep 1
  done
  if [ -z "$PORT" ]; then
    fail "自动重连失败，请手工执行：usbipd detach --busid $BUSID 再 usbipd attach --wsl --busid $BUSID"
  fi
  echo "  已重新连接"
fi
printf '  %s\n' "$PORT"

# --- 4/6 墙钟校时 -----------------------------------------------------------
# 采集中途跳秒会污染相机时间戳（实测不关时 20 秒跳 2201 ms）。
step 4/6 "墙钟校时（NTP）"
if [ "$SKIP_NTP" = 1 ]; then
  echo "  已按 --no-ntp-check 跳过检查"
elif ! command -v timedatectl >/dev/null 2>&1; then
  echo "  [!] 没有 timedatectl，跳过（自行确认系统时间不会中途跳）"
else
  NTP_STATE="$(timedatectl show -p NTP --value 2>/dev/null || echo unknown)"
  if [ "$NTP_STATE" = "no" ]; then
    echo "  NTP 已关闭（systemd-timesyncd inactive），可以采集"
  else
    echo "  NTP 状态：$NTP_STATE，尝试关闭 ..."
    if sudo -n timedatectl set-ntp false 2>/dev/null; then
      echo "  已关闭（采完记得 sudo timedatectl set-ntp true）"
    else
      echo "  [!] 关不掉（需要 sudo 密码）。请另开终端执行：sudo timedatectl set-ntp false"
      echo "  [!] 不关的话采集期间墙钟可能跳秒，相机的 epoch 时间戳会跟着跳"
    fi
  fi
fi

# --- 5/6 组装命令 -----------------------------------------------------------
CMD=("$PY" "collect_cap.py" "--no-quest")
[ -n "$DURATION" ] && CMD+=(--duration "$DURATION")
[ "$VIEW" = 1 ] && CMD+=(--view)
[ -n "$OUT_DIR" ] && CMD+=(--output-dir "$OUT_DIR")
if [ ${#PASS_ARGS[@]} -gt 0 ]; then
  CMD+=("${PASS_ARGS[@]}")
fi

step 5/6 "启动采集"
printf '  '
printf '%q ' "${CMD[@]}"
printf '\n'
if [ "$DRY" = 1 ]; then
  echo "  （--dry-run：到此为止，没有启动采集）"
  exit 0
fi
echo "  Ctrl+C 只按一次：程序会依次停手套、停两路 ffmpeg、写 manifest"

"${CMD[@]}"
RC=$?

# --- 6/6 结果摘要 -----------------------------------------------------------
step 6/6 "结果摘要"
if [ -n "$OUT_DIR" ]; then
  TARGET="$OUT_DIR"
else
  TARGET="$(ls -dt "$QUEST_ROOT"/recordings/data_* 2>/dev/null | head -1)"
fi

if [ -z "${TARGET:-}" ] || [ ! -f "$TARGET/manifest.json" ]; then
  echo "  [!] 没找到 manifest.json，采集可能没真正开始（退出码 $RC）"
  exit "$RC"
fi

"$PY" - "$TARGET" <<'PYEOF'
import json, os, sys
target = sys.argv[1]
with open(os.path.join(target, "manifest.json"), encoding="utf-8") as fh:
    m = json.load(fh)
print("  目录   %s" % target)
print("  时长   %.1f s" % (m.get("duration_s") or 0))
print("  手套   %d 帧  实际 %.2f Hz" % (m["rows"], m["hand"]["actual_rate_hz"]))
cams = m.get("cameras", {}).get("per_camera", {})
total_mb = 0.0
for name, cam in sorted(cams.items()):
    size = ""
    path = cam.get("file") or ""
    if path and not os.path.isabs(path):
        path = os.path.join(target, path)          # manifest 里存的是相对采集目录的路径
    if path and os.path.isfile(path):
        mb = os.path.getsize(path) / 1048576
        total_mb += mb
        size = "  %.1f MB" % mb
    print("  %-12s %d 帧  %.1f fps%s" % (name, cam.get("frames", 0), cam.get("actual_fps") or 0, size))
if total_mb:
    print("  视频合计 %.1f MB" % total_mb)
v = m.get("verify", {})
print("  校验   检查 %d 个数据集，行数不一致 %d 处" % (v.get("datasets_checked", 0), len(v.get("row_mismatch") or [])))
for item in v.get("row_mismatch") or []:
    print("     [X] %s 有 %d 行，应为 %d" % (item["dataset"], item["rows"], item["expected"]))
for warning in m.get("warnings") or []:
    print("  [!] %s" % warning)
sys.exit(3 if (v.get("row_mismatch") or []) else 0)
PYEOF
SUMMARY_RC=$?

if [ "$RC" -ne 0 ]; then
  echo "  [!] collect_cap.py 退出码 $RC（非 0 表示没采满或中途出错）"
  exit "$RC"
fi
if [ "$SUMMARY_RC" -ne 0 ]; then
  echo "  [X] manifest 行数校验不通过，这份数据不要直接用"
  exit 3
fi
echo "  回放：python3.10 visualize.py \"$TARGET\" --html"
exit 0
