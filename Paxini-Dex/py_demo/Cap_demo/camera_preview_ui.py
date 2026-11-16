"""双路实时画面与每路独立的相机参数面板。Tk 控件只在主线程访问。"""

from __future__ import annotations

import signal
import sys
import time

try:
    import tkinter as tk
    from tkinter import ttk
except ImportError as exc:
    raise RuntimeError(
        "参数窗口需要 Tkinter，可安装：sudo apt install python3-tk"
    ) from exc

from camera_controls import AUTO_SPECS, SLIDER_SPECS


class CameraPanel:
    def __init__(self, parent, stream, cv2, args):
        self.stream, self.cv2 = stream, cv2
        self.controls = {}
        self.rows, self.auto_widgets = {}, {}
        self.pending = {}
        self.restore_revision = 0
        self._updating = False
        self._controls_built = False
        self._last_frame_count = -1
        self._image_dirty = True
        self.photo = None
        self.failed = False

        self.frame = ttk.LabelFrame(parent, text=f"{stream.label}  ·  {stream.device.path}", padding=8)
        self.frame.columnconfigure(0, weight=1)
        self.frame.rowconfigure(0, weight=1)
        view_height = max(120, round(args.view_width * args.height / args.width))
        self.canvas = tk.Canvas(self.frame, width=args.view_width, height=view_height,
                                background="#161b22", highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.image_item = self.canvas.create_image(0, 0, anchor="center")
        self.wait_item = self.canvas.create_text(0, 0, text="等待相机画面...", fill="white", font=("", 14))
        self.canvas.bind("<Configure>", self._canvas_resized)
        self.frame_status = tk.StringVar(value="正在连接相机...")
        ttk.Label(self.frame, textvariable=self.frame_status).grid(row=1, column=0, sticky="w", pady=(4, 7))

        self.control_frame = ttk.LabelFrame(self.frame, text="独立相机参数", padding=6)
        self.control_frame.grid(row=2, column=0, sticky="ew")
        self.control_frame.columnconfigure(1, weight=1)
        self.auto_frame = ttk.Frame(self.control_frame)
        self.auto_frame.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 5))
        self.feedback = tk.StringVar(value="正在读取驱动参数...")
        self.feedback_label = ttk.Label(self.frame, textvariable=self.feedback, wraplength=args.view_width)
        self.feedback_label.grid(row=3, column=0, sticky="ew", pady=(5, 0))

    def _canvas_resized(self, event):
        self._image_dirty = True
        self.canvas.coords(self.wait_item, event.width // 2, event.height // 2)

    def _build_controls(self, controls):
        for key, label, _ in AUTO_SPECS:
            if key not in controls:
                continue
            control = controls[key]
            variable = tk.BooleanVar(value=control.value != control.manual_value)
            widget = ttk.Checkbutton(self.auto_frame, text=label, variable=variable,
                                     command=lambda key=key: self._auto_changed(key))
            widget.pack(side="left", padx=(0, 14))
            self.auto_widgets[key] = (variable, widget)
        self.restore_button = ttk.Button(self.auto_frame, text="恢复启动值", command=self.restore_initial)
        self.restore_button.pack(side="right")

        for row, (key, label, _) in enumerate(SLIDER_SPECS, 1):
            ttk.Label(self.control_frame, text=label, width=8).grid(row=row, column=0, sticky="w")
            if key not in controls:
                ttk.Label(self.control_frame, text="相机未提供此参数").grid(row=row, column=1, sticky="w")
                continue
            control = controls[key]
            variable = tk.IntVar(value=control.value)
            scale = tk.Scale(self.control_frame, from_=control.minimum, to=control.maximum,
                             resolution=control.step, orient="horizontal", variable=variable,
                             showvalue=False, borderwidth=0, highlightthickness=0, sliderlength=18)
            scale.grid(row=row, column=1, sticky="ew", padx=5)
            value_label = ttk.Label(self.control_frame, text=control.format_value(), width=13, anchor="e")
            value_label.grid(row=row, column=2, sticky="e")
            self.rows[key] = (variable, scale, value_label)
            # 初始赋值后才注册 trace，启动和显示驱动回读值时不会写入设置。
            variable.trace_add("write", lambda *unused, key=key: self._slider_changed(key))
        ttk.Label(self.control_frame, text="自动模式下，曝光 / 色温滑块暂时禁用。", foreground="#666666").grid(
            row=len(SLIDER_SPECS) + 1, column=0, columnspan=3, sticky="w", pady=(5, 0))
        self._controls_built = True

    def _request(self, key, value):
        if self._updating or self.failed:
            return
        control = self.controls[key]
        if not control.writable:
            return
        if key in self.pending and self.pending[key][1] == value:
            return
        if key not in self.pending and control.value == value:
            return
        revision = self.stream.request_control(key, value)
        self.pending[key] = (revision, value)
        self.feedback.set(f"正在设置{control.label}...")
        self.feedback_label.configure(foreground="#555555")

    def _slider_changed(self, key):
        variable, _, label = self.rows[key]
        control = self.controls[key]
        value = variable.get()
        label.configure(text=control.format_value(value))
        self._request(key, value)

    def _auto_changed(self, key):
        control = self.controls[key]
        enabled = self.auto_widgets[key][0].get()
        value = control.automatic_value if enabled else control.manual_value
        if value is not None:
            self._request(key, value)

    def restore_initial(self):
        if self.failed:
            return
        self.pending.clear()
        self.restore_revision = self.stream.request_restore()
        self.feedback.set("正在恢复此相机的启动值...")

    def _refresh_controls(self):
        controls, applied, message, error, ready = self.stream.control_snapshot()
        self.controls = controls
        if not ready:
            return
        if not self._controls_built:
            self._build_controls(controls)
        self.pending = {key: item for key, item in self.pending.items() if item[0] > applied}
        restoring = self.restore_revision > applied
        self._updating = True
        try:
            for key, (variable, scale, label) in self.rows.items():
                control = controls.get(key)
                if control is None:
                    scale.configure(state="disabled")
                    continue
                if key not in self.pending:
                    variable.set(control.value)
                    label.configure(text=control.format_value())
                scale.configure(state="normal" if control.writable and not restoring and not self.failed else "disabled")
            for key, (variable, widget) in self.auto_widgets.items():
                control = controls.get(key)
                if control is None:
                    widget.state(["disabled"])
                    continue
                if key not in self.pending:
                    variable.set(control.value != control.manual_value)
                enabled = control.writable and control.automatic_value is not None and not restoring and not self.failed
                widget.state(["!disabled"] if enabled else ["disabled"])
            self.restore_button.state(["!disabled"] if controls and not restoring and not self.failed else ["disabled"])
        finally:
            self._updating = False
        if not self.failed and not self.pending and not restoring:
            self.feedback.set(error or message or "驱动没有提供可调参数")
            self.feedback_label.configure(foreground="#bd2020" if error else "#24633b")

    def refresh(self):
        frame, count, received, error = self.stream.snapshot()
        if error:
            if not self.failed:
                print(error, file=sys.stderr, flush=True)
            self.failed = True
            self.feedback.set(error)
            self.feedback_label.configure(foreground="#bd2020")
            self.frame_status.set("此路采集已停止，请检查连接后重新运行。")
        elif frame is not None:
            self.frame_status.set(f"已采集 {count} 帧  ·  距最新画面 {time.monotonic() - received:.2f}s")
        if frame is not None and (count != self._last_frame_count or self._image_dirty):
            width = max(1, self.canvas.winfo_width())
            height = max(1, self.canvas.winfo_height())
            source_height, source_width = frame.shape[:2]
            scale = min(width / source_width, height / source_height)
            image = self.cv2.resize(frame, (max(1, round(source_width * scale)),
                                           max(1, round(source_height * scale))), interpolation=self.cv2.INTER_AREA)
            image = self.cv2.cvtColor(image, self.cv2.COLOR_BGR2RGB)
            # Tk 原生支持二进制 PPM，无需 Pillow 的 ImageTk 扩展。
            ppm = f"P6\n{image.shape[1]} {image.shape[0]}\n255\n".encode("ascii") + image.tobytes()
            photo = tk.PhotoImage(data=ppm, format="PPM", master=self.canvas)
            self.canvas.coords(self.image_item, width // 2, height // 2)
            self.canvas.itemconfigure(self.image_item, image=photo)
            self.canvas.itemconfigure(self.wait_item, state="hidden")
            self.photo = photo
            self._last_frame_count, self._image_dirty = count, False
        self._refresh_controls()


class PreviewWindow:
    def __init__(self, cv2, hand, streams, args):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            raise RuntimeError("无法打开预览窗口，请在图形桌面的终端运行并检查 DISPLAY。") from exc
        self.streams = streams
        self.exit_code = 0
        self._closed = False
        self.root.title(f"PXCap3 {'左' if hand == 'left' else '右'}手套 · 双腕部画面与独立参数调节")
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        for key in ("q", "Q", "<Escape>"):
            self.root.bind(key, lambda event: self.close())
        width = min(args.view_width * 2 + 64, self.root.winfo_screenwidth() - 40)
        height = min(round(args.view_width * args.height / args.width) + 440,
                     self.root.winfo_screenheight() - 70)
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(min(920, width), min(620, height))
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)
        toolbar = ttk.Frame(self.root, padding=8)
        toolbar.grid(row=0, column=0, sticky="ew")
        ttk.Label(toolbar, text="两组滑块分别控制对应相机；设置成功后显示驱动回读值。").pack(side="left")
        ttk.Button(toolbar, text="停止预览并退出", command=self.close).pack(side="right")
        container = ttk.Frame(self.root, padding=(8, 0, 8, 8))
        container.grid(row=1, column=0, sticky="nsew")
        container.rowconfigure(0, weight=1)
        self.panels = []
        for column, stream in enumerate(streams):
            container.columnconfigure(column, weight=1, uniform="camera")
            panel = CameraPanel(container, stream, cv2, args)
            panel.frame.grid(row=0, column=column, sticky="nsew", padx=(0, 4) if column == 0 else (4, 0))
            self.panels.append(panel)

    def _tick(self):
        if self._closed:
            return
        try:
            for panel in self.panels:
                panel.refresh()
                if panel.failed:
                    self.exit_code = 1
        except Exception as exc:
            print(f"预览窗口刷新失败：{exc}", file=sys.stderr, flush=True)
            self.exit_code = 1
            self.close()
            return
        self.root.after(33, self._tick)

    def close(self):
        if not self._closed:
            self._closed = True
            for stream in self.streams:
                stream.stop_requested.set()
            self.root.quit()

    def run(self):
        previous = signal.getsignal(signal.SIGINT)
        try:
            signal.signal(signal.SIGINT, lambda signum, frame: self.close())
            self.root.after(0, self._tick)
            self.root.mainloop()
        finally:
            signal.signal(signal.SIGINT, previous)
        return self.exit_code

    def destroy(self):
        self.root.destroy()
