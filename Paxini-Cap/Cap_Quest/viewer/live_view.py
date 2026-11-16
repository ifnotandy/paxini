#!/usr/bin/env python3
"""Live readout for the glove stream — two interchangeable shells.

Three viewers exist in this project, on purpose:

- ``visualize.py`` replays a finished ``data.h5`` — full length, scrubbable,
  with curves, works after the fact.
- ``LiveView`` (here) shows the stream while it is being recorded, as a
  matplotlib/Tk window in a forked child process.
- ``HtmlLiveView`` (also here) shows the same numbers and camera previews as a
  page on ``http://127.0.0.1:8765/``, served from a forked child.

``--view-mode auto`` picks between the last two: WSL keeps its WSLg display
(a weston compositor with no window manager, where a Tk window can land
off-screen and cannot be raised from the taskbar), so WSL gets the page;
native Linux gets the window.  ``html`` / ``tk`` force either one.

Both show **numbers only**: four magnetic-encoder joint angles on the left, the
10-slot resultant-force table on the right, camera thumbnails below.  During a
take you are watching whether the fingers are in the right pose and whether
force has actually been applied; a curve that scrolls past in 6 s answers
neither question as directly as a large number does, and the curve is still
there afterwards in the replay page.

Why a child process instead of a matplotlib window in the capture loop: the
loop has ~8 ms per frame at 120 Hz, and a redraw costs tens of milliseconds.
Drawing in-process would steal frames from the recording.  With a child:

- ``push`` does one ``norm`` plus ``queue.put_nowait`` and returns in
  microseconds.
- The queue is bounded (240 frames ≈ 2 s); if the viewer falls behind, frames
  are dropped instead of blocking the recorder, and ``dropped`` counts them.
- A crashed or closed viewer cannot take the recording down with it.

The file is never read back.  The recorder batches 100 rows before touching
``data.h5``, so tailing it would show bursts, and a second reader on a
non-SWMR HDF5 risks seeing a half-written chunk.

Usage from ``collect_cap.py``::

    view = LiveView(slots=CAP_SLOTS, window_s=6.0)   # forks the child
    ...
    view.push(hand)     # per recorded frame
    ...
    view.close()        # sends the sentinel, joins the child
"""

from __future__ import annotations

import json
import multiprocessing
import os
import queue as queue_module
import sys
import time
from pathlib import Path
from typing import Any, Sequence

import numpy as np


SLOT_LABELS = {
    "thumb_one": "拇指末",
    "thumb_two": "拇指中",
    "index_zero": "食指根",
    "index_one": "食指中",
    "middle_zero": "中指根",
    "middle_one": "中指中1",
    "middle_two": "中指中2",
    "middle_three": "中指末",
    "palm_zero": "掌根",
    "palm_one": "掌中",
}

ANGLE_LABELS = ("FF2 食指近", "FF1 食指远", "MF2 中指近", "MF1 中指远")
ANGLE_COLORS = ("#4da3ff", "#38d39f", "#f5a524", "#f26d6d")

_SENTINEL = None
_QUEUE_FRAMES = 240


def running_under_wslg() -> bool:
    """True when the display is WSLg rather than a real X/Wayland session.

    WSLg 用的是 weston 的精简合成器，没有用户熟悉的窗口管理器：窗口位置由它
    自己决定（常在屏幕右下、比屏幕还高），被盖住后任务栏那条带
    ``[WARN:COPY MODE]`` 前缀的条目也拉不回来。原生 Linux 有正常 WM，没有这个问题。
    """
    return bool(os.environ.get("WSL_DISTRO_NAME")) or Path("/mnt/wslg").is_dir()


def default_view_mode() -> str:
    """``auto`` 的落点：WSL 用浏览器版，原生 Linux 用窗口版。"""
    return "html" if running_under_wslg() else "tk"


# --------------------------------------------------------------------------
# child process: drawing only
# --------------------------------------------------------------------------
class _Viewer:
    """The matplotlib half.  Lives in the child; never touches the HDF5."""

    def __init__(
        self,
        slots: Sequence[str],
        fps: float,
        title: str,
        camera_ids: Sequence[str] = (),
    ) -> None:
        import matplotlib

        matplotlib.use("TkAgg", force=True)
        import matplotlib.pyplot as plt

        plt.rcParams["font.sans-serif"] = [
            "Noto Sans CJK JP",
            "Noto Sans CJK SC",
            "WenQuanYi Micro Hei",
            "DejaVu Sans",
        ]
        plt.rcParams["axes.unicode_minus"] = False
        plt.style.use("dark_background")

        self.slots = list(slots)
        self.fps = float(fps)
        self.alive = True
        self.frames = 0
        self._started = time.monotonic()
        self._last_draw = 0.0

        # 实时窗口只出数字，不画曲线：曲线留给回放页去看。这里只有最新一帧，
        # 所以不留历史缓冲。
        self._latest_angles: np.ndarray | None = None
        self._latest_slot: np.ndarray | None = None
        self._latest_counts: np.ndarray | None = None

        # 左边磁编码器（4 路关节角），右边 10 路合力表，底部相机缩略图。
        # 上面给 1.45：11 行数字 + 4 路关节角要靠这个高度才撑得开字号；
        # 相机行按 16:9 算好高度再留一点余量，缩略图不会被挤到窗口下沿之外。
        self.fig = plt.figure(figsize=(11.5, 6.2 + (2.35 if camera_ids else 0.0)))
        self.fig.canvas.manager.set_window_title(title)
        gs = self.fig.add_gridspec(
            2 if camera_ids else 1, 2,
            width_ratios=[1.0, 1.95],
            height_ratios=[1.45, 1.0] if camera_ids else None,
            left=0.045, right=0.985, top=0.9, bottom=0.055,
            hspace=0.2, wspace=0.05,
        )
        self.ax_angle = self.fig.add_subplot(gs[0, 0])
        self.ax_table = self.fig.add_subplot(gs[0, 1])

        # 磁编码器：4 路关节角，纯数字，字号 30 pt。
        self.ax_angle.axis("off")
        self.ax_angle.set_title("磁编码器 / 关节角（deg）", fontsize=12, pad=8)
        self._angle_rows = []
        for k in range(4):
            y = 0.84 - k * 0.235
            self.ax_angle.text(
                0.02, y, ANGLE_LABELS[k], fontsize=13, va="center",
                color="#aab6c8", transform=self.ax_angle.transAxes,
            )
            self._angle_rows.append(
                self.ax_angle.text(
                    0.97, y, "—", fontsize=30, va="center", ha="right",
                    color=ANGLE_COLORS[k], transform=self.ax_angle.transAxes,
                )
            )

        # 10 路合力按磁编码器那种纯数字显示：槽位 / Fx / Fy / Fz / |F| / 点数。
        # 力值换算成 N 显示两位小数：文件里是 0.1 N 的整数，一位小数正好等值，
        # 而整数显示会把 |F| 的 1.73 舍成 2（小力段误差 15%），也看不出 0.2 N。
        self.ax_table.axis("off")
        self.ax_table.set_title("10 路合力（N）", fontsize=12, pad=8)
        columns = [
            (0.00, "槽位", "left"),
            (0.36, "Fx", "right"),
            (0.54, "Fy", "right"),
            (0.72, "Fz", "right"),
            (0.87, "|F|", "right"),
            (1.00, "点数", "right"),
        ]
        for fx, label, align in columns:
            self.ax_table.text(
                fx, 0.962, label, fontsize=10.5, color="#6f7c92", ha=align,
                transform=self.ax_table.transAxes,
            )
        self._slot_rows = []
        for row, name in enumerate(self.slots):
            y = 0.885 - row * 0.078
            self.ax_table.text(
                0.00, y, SLOT_LABELS.get(name, name), fontsize=13, va="center",
                color="#aab6c8", transform=self.ax_table.transAxes,
            )
            self._slot_rows.append([
                self.ax_table.text(
                    fx, y, "—", fontsize=15, va="center", ha=align,
                    color="#e7ecf5" if fx < 0.99 else "#7c8798",
                    transform=self.ax_table.transAxes,
                )
                for fx, _, align in columns[1:]
            ])
        total_y = 0.885 - len(self.slots) * 0.078 - 0.012
        self.ax_table.plot(
            [0, 1], [total_y + 0.036, total_y + 0.036], color="#2a3444", lw=0.9,
            transform=self.ax_table.transAxes, clip_on=False,
        )
        self.ax_table.text(
            0.00, total_y, "合计", fontsize=13, va="center", color="#aab6c8",
            transform=self.ax_table.transAxes,
        )
        self._slot_total = [
            self.ax_table.text(
                fx, total_y, "—", fontsize=15, va="center", ha=align,
                color="#f0c274" if fx < 0.99 else "#7c8798",
                transform=self.ax_table.transAxes,
            )
            for fx, _, align in columns[1:]
        ]

        self.status = self.fig.suptitle("等待数据 ...", fontsize=9, color="#c9d3e3")

        # Camera previews: thumbnails under the curves, fed by JPEG frames that
        # ride along with the recording (see camera_ffmpeg.PREVIEW_FPS).
        self.camera_ids = list(camera_ids)
        self.ax_cams: dict[str, Any] = {}
        self._cam_artists: dict[str, Any] = {}
        self._images: dict[str, Any] = {}
        if self.camera_ids:
            row = gs[1, :].subgridspec(1, len(self.camera_ids), wspace=0.05)
            for index, name in enumerate(self.camera_ids):
                ax = self.fig.add_subplot(row[0, index])
                # Transparent patch: only the photo itself should be visible,
                # not the box left over beside a 16:9 image in a wider axes.
                ax.set_facecolor("none")
                ax.patch.set_visible(False)
                ax.set_xticks([])
                ax.set_yticks([])
                # No frame: the image keeps its own aspect ratio, so a visible
                # box would outline empty space to the left and right of it.
                for spine in ax.spines.values():
                    spine.set_visible(False)
                # 贴顶放：axis 比 16:9 的画面高时，空出来的那条留在下面而不是上下各一半。
                ax.set_anchor("N")
                ax.set_title(name, fontsize=9, pad=3, color="#9fb0c8")
                self.ax_cams[name] = ax

        self.fig.canvas.mpl_connect("close_event", self._on_close)
        self.fig.canvas.mpl_connect("key_press_event", self._on_key)
        self.fig.show()
        # WSLg 里窗口经常开在别的窗口后面，看起来像"没打开"。置顶并抬一次，
        # 这样一开采集就能看见；关不掉也不影响录制（关闭只关视图）。
        # WSLg 里窗口默认会开在屏幕右下角，897 px 的高度把下半截（相机缩略图）
        # 直接推出屏幕，看起来就像"视图没打开"。这里显式给一个能放进屏幕的
        # 尺寸与位置；不要动 -topmost / withdraw，那会让窗口在无窗口管理器的
        # WSLg 里根本不映射。
        try:
            window = self.fig.canvas.manager.window
            screen_w = int(window.winfo_screenwidth())
            screen_h = int(window.winfo_screenheight())
            width = min(1160, max(640, screen_w - 120))
            height = min(900, max(420, screen_h - 140))
            window.geometry(f"{width}x{height}+48+40")
            window.deiconify()
            window.update_idletasks()
            print(
                f"[实时视图] 窗口已开：{width}x{height}+48+40"
                f"（屏幕 {screen_w}x{screen_h}；按 q 或关窗只关视图）",
                file=sys.stderr, flush=True,
            )
        except Exception as exc:  # pragma: no cover - 取决于窗口管理器
            print(f"[实时视图] 窗口定位失败（不影响显示）：{exc}",
                  file=sys.stderr, flush=True)
        self.fig.canvas.draw()
        self.fig.canvas.flush_events()

    def push(self, payload: tuple[Any, ...]) -> None:
        _stamp, angles, _magnitude, slot_xyz, slot_counts = payload[:5]
        self.frames += 1
        self._latest_angles = np.asarray(angles, dtype=np.float32)
        self._latest_slot = np.asarray(slot_xyz, dtype=np.float32)
        self._latest_counts = (
            None if slot_counts is None else np.asarray(slot_counts, dtype=np.int64)
        )
        images = payload[5] if len(payload) > 5 else None
        if images:
            for name, blob in images.items():
                if name not in self.ax_cams:
                    continue
                array = self._decode(blob)
                if array is not None:
                    self._images[name] = array

    @staticmethod
    def _decode(blob: bytes) -> Any:
        """JPEG bytes -> HxWx3 uint8; the only place PIL is needed."""
        try:
            import io

            from PIL import Image

            with Image.open(io.BytesIO(blob)) as handle:
                return np.asarray(handle.convert("RGB"))
        except Exception:  # pragma: no cover - a torn frame is not fatal
            return None

    def tick(self) -> None:
        if not self.alive:
            return
        now = time.monotonic()
        if now - self._last_draw < 1.0 / self.fps:
            return
        self._last_draw = now
        if self._latest_angles is None and self._latest_slot is None:
            return

        if self._latest_angles is not None:
            for k, label in enumerate(self._angle_rows):
                value = (
                    float(self._latest_angles[k])
                    if k < self._latest_angles.size
                    else float("nan")
                )
                label.set_text("—" if not np.isfinite(value) else f"{value:.2f}")

        if self._latest_slot is not None:
            latest = self._latest_slot
            counts = self._latest_counts
            total = latest.sum(axis=0)
            for row, cells in enumerate(self._slot_rows):
                if row >= latest.shape[0]:
                    break
                fx, fy, fz = (float(value) / 10.0 for value in latest[row])
                cells[0].set_text(f"{fx:.2f}")
                cells[1].set_text(f"{fy:.2f}")
                cells[2].set_text(f"{fz:.2f}")
                cells[3].set_text(f"{float(np.linalg.norm(latest[row])) / 10.0:.2f}")
                cells[4].set_text(
                    "—" if counts is None else f"{int(counts[row])}"
                )
            total_n = total / 10.0
            self._slot_total[0].set_text(f"{total_n[0]:.2f}")
            self._slot_total[1].set_text(f"{total_n[1]:.2f}")
            self._slot_total[2].set_text(f"{total_n[2]:.2f}")
            self._slot_total[3].set_text(f"{float(np.linalg.norm(total)) / 10.0:.2f}")
            self._slot_total[4].set_text(
                "—" if counts is None else f"{int(np.asarray(counts).sum())}"
            )

        for name, array in self._images.items():
            ax = self.ax_cams.get(name)
            if ax is None:
                continue
            artist = self._cam_artists.get(name)
            if artist is None:
                # Keep the frame's own aspect ratio: stretching a 16:9 image
                # into a wide strip makes hands look squashed, and this view
                # exists to see what the fingers are doing.
                self._cam_artists[name] = ax.imshow(
                    array, aspect="equal", interpolation="nearest"
                )
            else:
                artist.set_data(array)

        elapsed = now - self._started
        rate = self.frames / elapsed if elapsed > 0 else 0.0
        self.status.set_text(
            f"已收 {self.frames} 帧    {rate:5.1f} Hz    "
            f"(q 关闭视图，录制不受影响)"
        )
        self.fig.canvas.draw_idle()
        self.fig.canvas.flush_events()

    def _on_close(self, _event: Any) -> None:
        self.alive = False

    def _on_key(self, event: Any) -> None:
        if event.key in ("q", "escape"):
            self.alive = False

    def close(self) -> None:
        self.alive = False
        try:
            import matplotlib.pyplot as plt

            plt.close(self.fig)
        except Exception:  # pragma: no cover - shutdown is best effort
            pass


def _viewer_main(
    pipe: Any,
    slots: Sequence[str],
    fps: float,
    title: str,
    camera_ids: Sequence[str] = (),
) -> None:
    """Child entry point: drain the queue, redraw, exit on sentinel/close."""
    import signal

    # Ctrl+C belongs to the recorder in the parent; this process should not
    # die mid-redraw and leave a zombie window behind.
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    try:
        view = _Viewer(slots, fps, title, camera_ids)
    except Exception as exc:  # pragma: no cover - depends on the display
        print(f"[实时视图] 打开失败：{exc}", file=sys.stderr)
        return

    while view.alive:
        try:
            payload = pipe.get(timeout=0.05)
        except queue_module.Empty:
            view.tick()
            continue
        except (EOFError, OSError):
            return
        if payload is _SENTINEL:
            break
        view.push(payload)
        # Drain whatever else arrived while we were drawing.
        while True:
            try:
                more = pipe.get_nowait()
            except queue_module.Empty:
                break
            if more is _SENTINEL:
                view.close()
                return
            view.push(more)
        view.tick()

    view.close()


# --------------------------------------------------------------------------
# browser viewer: same numbers, but rendered where it is guaranteed visible
# --------------------------------------------------------------------------
_HTML_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>__TITLE__</title>
<style>
  :root { color-scheme: dark; }
  body { margin:0; background:#0d1117; color:#e7ecf5;
         font:14px/1.4 "Segoe UI","Noto Sans CJK SC",sans-serif; }
  header { padding:8px 14px; display:flex; gap:18px; align-items:baseline;
           border-bottom:1px solid #222a36; }
  header b { font-size:15px; font-weight:600; }
  header span { color:#8b97ab; font-variant-numeric:tabular-nums; }
  main { display:grid; grid-template-columns: 260px 1fr; gap:14px; padding:14px; }
  .card { background:#121821; border:1px solid #1e2632; border-radius:8px; padding:10px 12px; }
  h2 { margin:0 0 8px; font-size:12px; font-weight:600; color:#7f8ca3;
       letter-spacing:.04em; text-transform:uppercase; }
  .angles div { display:flex; justify-content:space-between; align-items:baseline;
                padding:5px 0; border-bottom:1px solid #171e28; }
  .angles div:last-child { border:0; }
  .angles em { font-style:normal; color:#9fb0c8; }
  .angles b { font-size:24px; font-variant-numeric:tabular-nums; }
  table { width:100%; max-width:520px; border-collapse:collapse; font-variant-numeric:tabular-nums; }
  th { text-align:right; font-weight:500; font-size:11px; color:#6f7c92; padding:2px 6px; }
  th:first-child, td:first-child { text-align:left; color:#9fb0c8; }
  td { text-align:right; padding:3px 6px; border-top:1px solid #171e28; font-size:15px; }
  tr.total td { color:#f0c274; border-top:1px solid #2a3444; }
  .cams { grid-column: 1 / -1; display:grid; grid-template-columns:1fr 1fr; gap:12px; }
  .cams figure { margin:0; }
  .cams img { width:100%; display:block; border-radius:6px; background:#0a0e14; }
  .cams figcaption { color:#7f8ca3; font-size:11px; padding-top:4px; }
  #stale { color:#f0c274; }
</style></head>
<body>
<header><b>__TITLE__</b><span id="rate">等待数据 …</span><span id="stale"></span></header>
<main>
  <section class="card"><h2>磁编码器 / 关节角（deg）</h2>
    <div class="angles" id="angles">__ANGLE_ROWS__</div>
  </section>
  <section class="card"><h2>10 路合力（N）</h2>
    <table><thead><tr><th>槽位</th><th>Fx</th><th>Fy</th><th>Fz</th><th>|F|</th><th>点数</th></tr></thead>
    <tbody id="rows">__FORCE_ROWS__</tbody></table>
  </section>
  <section class="cams" id="cams">__CAMERAS__</section>
</main>
<script>
const $ = id => document.getElementById(id);
function fmt(v) { return v === null || v === undefined ? '—' : v.toFixed(2); }
async function tick() {
  try {
    const r = await fetch('/state', {cache: 'no-store'});
    const s = await r.json();
    $('rate').textContent = `已收 ${s.frames} 帧 · ${s.rate.toFixed(1)} Hz`;
    $('stale').textContent = s.age_s > 2 ? `数据停了 ${s.age_s.toFixed(1)} s` : '';
    s.angles.forEach((v, i) => { const el = $('ang' + i); if (el) el.textContent = fmt(v); });
    s.forces.forEach((f, i) => {
      const cells = document.querySelectorAll(`#row${i} td`);
      if (!cells.length) return;
      cells[1].textContent = fmt(f[0]);
      cells[2].textContent = fmt(f[1]);
      cells[3].textContent = fmt(f[2]);
      cells[4].textContent = fmt(Math.hypot(f[0], f[1], f[2]));
      cells[5].textContent = s.counts ? s.counts[i] : '—';
    });
    if (s.total) {
      const cells = document.querySelectorAll('#totalRow td');
      cells[1].textContent = fmt(s.total[0]);
      cells[2].textContent = fmt(s.total[1]);
      cells[3].textContent = fmt(s.total[2]);
      cells[4].textContent = fmt(Math.hypot(s.total[0], s.total[1], s.total[2]));
      cells[5].textContent = s.total_count === null ? '—' : s.total_count;
    }
  } catch (e) { /* 采集停了：保留最后一帧，不刷红 */ }
}
tick();
setInterval(tick, 150);
</script>
</body></html>
"""

_ANGLE_ROW = '<div><em>{label}</em><b id="ang{i}">—</b></div>'
_FORCE_ROW = (
    '<tr id="row{i}"><td>{label}</td><td>—</td><td>—</td><td>—</td>'
    '<td>—</td><td>—</td></tr>'
)


class _HtmlServer:
    """Child side: own the page, the newest state and the HTTP server."""

    def __init__(
        self,
        slots: Sequence[str],
        fps: float,
        title: str,
        camera_ids: Sequence[str],
        host: str,
        port: int,
    ) -> None:
        import http.server
        import threading

        self.slots = list(slots)
        self.camera_ids = list(camera_ids)
        self.title = str(title)
        self.fps = float(fps)
        self._lock = threading.Lock()
        self._frames = 0
        self._started = time.monotonic()
        self._last_push = 0.0
        self._angles: list[float | None] = [None] * 4
        self._forces: list[tuple[int, int, int]] = [(0, 0, 0)] * len(self.slots)
        self._counts: list[int] | None = None
        self._images: dict[str, bytes] = {}
        self._closed = False

        page = (
            _HTML_PAGE
            .replace("__TITLE__", _escape(self.title))
            .replace(
                "__ANGLE_ROWS__",
                "".join(
                    _ANGLE_ROW.format(label=_escape(ANGLE_LABELS[i]), i=i)
                    for i in range(4)
                ),
            )
            .replace(
                "__FORCE_ROWS__",
                "".join(
                    _FORCE_ROW.format(label=_escape(SLOT_LABELS.get(name, name)), i=i)
                    for i, name in enumerate(self.slots)
                )
                + '<tr class="total" id="totalRow"><td>合计</td>'
                '<td>—</td><td>—</td><td>—</td><td>—</td><td>—</td></tr>',
            )
            .replace(
                "__CAMERAS__",
                "".join(
                    f'<figure><img src="/cam/{_escape(name)}" alt="{_escape(name)}">'
                    f"<figcaption>{_escape(name)}</figcaption></figure>"
                    for name in self.camera_ids
                )
                or "<p style='color:#7f8ca3'>未启用相机</p>",
            )
        )
        body = page.encode("utf-8")
        viewer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: Any) -> None:  # 别刷屏
                return

            def do_GET(self) -> None:
                if self.path in ("/", "/index.html"):
                    self._send(200, "text/html; charset=utf-8", body)
                elif self.path == "/state":
                    self._send(200, "application/json", viewer.state_json())
                elif self.path.startswith("/cam/"):
                    viewer.stream(self, self.path[5:])
                else:
                    self._send(404, "text/plain; charset=utf-8", b"not found")

            def _send(self, code: int, ctype: str, payload: bytes) -> None:
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(payload)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        server = None
        last_error: Exception | None = None
        for candidate in [port] + [port + i for i in range(1, 11)]:
            try:
                server = http.server.ThreadingHTTPServer((host, candidate), Handler)
                break
            except OSError as exc:  # 端口被占：往后顺延
                last_error = exc
        if server is None:
            raise RuntimeError(f"无法绑定端口 {port}~{port + 10}：{last_error}")
        self._server = server
        self._server.daemon_threads = True
        self.port = int(self._server.server_address[1])
        self.url = f"http://{host}:{self.port}/"

    # -- state -------------------------------------------------------------
    def update(self, payload: tuple[Any, ...]) -> None:
        angles, forces, counts, images = payload
        with self._lock:
            self._frames += 1
            self._last_push = time.monotonic()
            if angles is not None:
                values = [float(v) for v in angles][:4]
                self._angles = values + [None] * (4 - len(values))
            if forces is not None:
                self._forces = [tuple(int(v) for v in row) for row in forces]
            if counts is not None:
                self._counts = [int(v) for v in counts]
            if images:
                self._images.update(images)

    def state_json(self) -> bytes:
        with self._lock:
            age = time.monotonic() - self._last_push if self._last_push else 0.0
            elapsed = time.monotonic() - self._started
            forces = self._forces
            payload = {
                "frames": self._frames,
                "rate": self._frames / elapsed if elapsed > 0 else 0.0,
                "age_s": age,
                "angles": self._angles,
                "forces": [[round(v / 10.0, 2) for v in row] for row in forces],
                "counts": self._counts,
                "total": [
                    round(sum(row[k] for row in forces) / 10.0, 2) for k in range(3)
                ],
                "total_count": None if self._counts is None else sum(self._counts),
            }
        return json.dumps(payload).encode("utf-8")

    def stream(self, handler: Any, name: str) -> None:
        """最新 JPEG 以 multipart MJPEG 推给浏览器，直到连接断开。"""
        boundary = b"frame"
        try:
            handler.send_response(200)
            handler.send_header("Age", "0")
            handler.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            handler.send_header(
                "Content-Type", "multipart/x-mixed-replace; boundary=frame"
            )
            handler.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            return
        interval = 1.0 / max(self.fps, 1.0)
        last = None
        while not self._closed:
            with self._lock:
                blob = self._images.get(name)
            if blob is not None and blob is not last:
                last = blob
                try:
                    handler.wfile.write(b"--" + boundary + b"\r\n")
                    handler.wfile.write(b"Content-Type: image/jpeg\r\n")
                    handler.wfile.write(
                        f"Content-Length: {len(blob)}\r\n\r\n".encode()
                    )
                    handler.wfile.write(blob + b"\r\n")
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
            time.sleep(interval)

    # -- lifecycle ---------------------------------------------------------
    def serve(self) -> None:
        self._server.serve_forever()

    def stop(self) -> None:
        self._closed = True
        try:
            self._server.shutdown()
            self._server.server_close()
        except Exception:  # pragma: no cover - shutdown is best effort
            pass


def _html_viewer_main(
    pipe: Any,
    port_pipe: Any,
    slots: Sequence[str],
    fps: float,
    title: str,
    camera_ids: Sequence[str],
    host: str,
    port: int,
) -> None:
    """Child entry point: bind the port, report it, then serve frames.

    端口由子进程绑、再用 pipe 报回父进程——父进程拿到的永远是真实端口，
    不用猜 8765 到底有没有被占。
    """
    import queue as queue_module
    import signal
    import threading

    # Ctrl+C 归父进程；这里被打断只会留下半条连接，不如让父进程统一收尾。
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    try:
        viewer = _HtmlServer(slots, fps, title, camera_ids, host, port)
    except Exception as exc:  # pragma: no cover - 端口被占 / 资源不足
        try:
            port_pipe.send(("error", str(exc)))
        except (BrokenPipeError, OSError):
            pass
        return
    try:
        port_pipe.send(("ok", viewer.port))
    except (BrokenPipeError, OSError):
        return
    finally:
        try:
            port_pipe.close()
        except OSError:
            pass

    threading.Thread(target=viewer.serve, name="html-http", daemon=True).start()
    print(f"[实时视图] 浏览器版已就绪：{viewer.url}", file=sys.stderr, flush=True)

    while True:
        try:
            payload = pipe.get(timeout=0.05)
        except queue_module.Empty:
            continue
        except (EOFError, OSError):
            break
        if payload is _SENTINEL:
            break
        viewer.update(payload)
        while True:  # 积压的全吃掉，只保留最新状态
            try:
                more = pipe.get_nowait()
            except queue_module.Empty:
                break
            except (EOFError, OSError):
                viewer.stop()
                return
            if more is _SENTINEL:
                viewer.stop()
                return
            viewer.update(more)

    viewer.stop()


class HtmlLiveView:
    """Serve the same readout as a web page on 127.0.0.1.

    The Tk window is nicer to glance at, but WSLg has no window manager: the
    window can land partly off-screen, and once it is behind something the
    taskbar entry often cannot bring it back.  This viewer has no such
    problem — it is just a page in the browser the user already has open,
    with the camera previews streamed as MJPEG.

    Runs in a **forked child**, like ``LiveView``: the parent only builds a
    small tuple per frame and drops it into a bounded queue.  The child owns
    the HTTP server, the page and the JPEG buffers, so a stuck browser
    connection cannot slow the recorder down, and the recorder holds no lock
    the child could ever block on.
    """

    def __init__(
        self,
        slots: Sequence[str],
        fps: float = 10.0,
        title: str = "PXCap III 实时",
        camera_ids: Sequence[str] = (),
        host: str = "127.0.0.1",
        port: int = 8765,
    ) -> None:
        self.slots = list(slots)
        self.camera_ids = list(camera_ids)
        self.fps = float(fps)
        self.dropped = 0
        self.image_pushes = 0
        self.url = ""
        self.port = 0
        self.error: str | None = None
        self._closed = False
        self._cameras: list[Any] = []
        self._image_interval = 1.0 / self.fps if self.fps > 0 else 0.0
        self._last_image = 0.0

        context = multiprocessing.get_context("fork")
        self._queue = context.Queue(maxsize=_QUEUE_FRAMES)
        read_end, write_end = context.Pipe(duplex=False)
        self._process = context.Process(
            target=_html_viewer_main,
            args=(
                self._queue,
                write_end,
                self.slots,
                self.fps,
                title,
                self.camera_ids,
                host,
                port,
            ),
            daemon=True,
        )
        self._process.start()
        write_end.close()

        if read_end.poll(5.0):
            try:
                status, value = read_end.recv()
            except (EOFError, OSError):
                status, value = "error", "视图进程提前退出"
            if status == "ok":
                self.port = int(value)
                self.url = f"http://{host}:{self.port}/"
            else:
                self.error = str(value)
        else:
            self.error = "视图进程 5 s 内没有报回端口"
        read_end.close()
        if self.error:
            print(
                f"[实时视图] 浏览器版启动失败：{self.error}（录制继续）",
                file=sys.stderr,
                flush=True,
            )

    # -- feeding -----------------------------------------------------------
    def attach_cameras(self, cameras: Sequence[Any]) -> None:
        self._cameras = [cam for cam in cameras if getattr(cam, "preview_fps", 0) > 0]

    def push(self, hand: dict[str, Any]) -> None:
        if self._closed or not self._process.is_alive():
            return
        angles = hand.get("joint_angles_deg")
        force = hand.get("resultant_force")
        counts = hand.get("distribute_counts")
        payload = (
            None if angles is None else [float(v) for v in angles],
            None if force is None else [[int(v) for v in row] for row in force],
            None if counts is None else [int(v) for v in counts],
            self._collect_images(),
        )
        try:
            self._queue.put_nowait(payload)
        except queue_module.Full:
            self.dropped += 1

    def _collect_images(self) -> dict[str, bytes] | None:
        if not self._cameras:
            return None
        now = time.monotonic()
        if now - self._last_image < self._image_interval:
            return None
        self._last_image = now
        images: dict[str, bytes] = {}
        for camera in self._cameras:
            latest = camera.latest_preview()
            if latest is not None:
                images[camera.camera_id] = latest[1]
        if not images:
            return None
        self.image_pushes += 1
        return images

    # -- lifecycle ---------------------------------------------------------
    def alive(self) -> bool:
        return not self._closed and self._process.is_alive()

    def close(self, timeout: float = 2.0) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._queue.put_nowait(_SENTINEL)
        except (queue_module.Full, ValueError):
            pass
        self._process.join(timeout=timeout)
        if self._process.is_alive():
            self._process.terminate()
            self._process.join(timeout=1.0)


def _escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# --------------------------------------------------------------------------
# parent process: a thin, non-blocking façade
# --------------------------------------------------------------------------
class LiveView:
    """Fork a viewer process and feed it frames without ever blocking."""

    def __init__(
        self,
        slots: Sequence[str],
        fps: float = 20.0,
        title: str = "PXCap III 实时",
        camera_ids: Sequence[str] = (),
    ) -> None:
        self.slots = list(slots)
        self.dropped = 0
        self._closed = False
        self.camera_ids = list(camera_ids)
        self._cameras: list[Any] = []
        self._image_interval = 1.0 / float(fps) if fps > 0 else 0.0
        self._last_images = 0.0
        self.image_pushes = 0

        context = multiprocessing.get_context("fork")
        self._queue = context.Queue(maxsize=_QUEUE_FRAMES)
        self._process = context.Process(
            target=_viewer_main,
            args=(
                self._queue, self.slots, float(fps), title, self.camera_ids,
            ),
            daemon=True,
        )
        self._process.start()

    def attach_cameras(self, cameras: Sequence[Any]) -> None:
        """Hand over the recorders whose live preview should be drawn."""
        self._cameras = [cam for cam in cameras if getattr(cam, "preview_fps", 0) > 0]

    # -- feeding -----------------------------------------------------------
    def push(self, hand: dict[str, Any]) -> None:
        """Quantise one frame and hand it over.  Never blocks the caller."""
        if self._closed or not self._process.is_alive():
            return
        force = np.asarray(hand["resultant_force"], dtype=np.float32)
        magnitude = np.linalg.norm(force, axis=1).astype(np.float32)
        angles = np.asarray(hand["joint_angles_deg"], dtype=np.float32)

        slot_xyz = force.copy()
        counts = hand.get("distribute_counts")
        slot_counts = (
            None if counts is None
            else np.asarray(counts, dtype=np.int32)
        )
        try:
            self._queue.put_nowait(
                (
                    int(hand["timestamp_monotonic_ns"]),
                    angles,
                    magnitude,
                    slot_xyz,
                    slot_counts,
                    self._collect_images(),
                )
            )
        except queue_module.Full:
            self.dropped += 1

    def _collect_images(self) -> dict[str, bytes] | None:
        """Newest JPEG per camera, at most at the viewer's own frame rate.

        ``push`` runs 120 times a second; copying every preview frame into the
        queue that often would be pure waste, so images are attached only as
        fast as the window can draw them.
        """
        if not self._cameras:
            return None
        now = time.monotonic()
        if now - self._last_images < self._image_interval:
            return None
        self._last_images = now
        images: dict[str, bytes] = {}
        for camera in self._cameras:
            latest = camera.latest_preview()
            if latest is not None:
                images[camera.camera_id] = latest[1]
        if not images:
            return None
        self.image_pushes += 1
        return images

    # -- lifecycle ---------------------------------------------------------
    def alive(self) -> bool:
        return not self._closed and self._process.is_alive()

    def close(self, timeout: float = 2.0) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._queue.put_nowait(_SENTINEL)
        except (queue_module.Full, ValueError):
            pass
        self._process.join(timeout=timeout)
        if self._process.is_alive():
            self._process.terminate()
            self._process.join(timeout=1.0)
