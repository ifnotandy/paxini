"""latency_records.py —— 相机延迟测量的结果台账（一台机器一个文件）

为什么要单独记一份
------------------
``latency_measure/<时间戳>_*/`` 每次都是一份独立产物，目录名带时间戳，下游没法
直接回答「这台机器该用哪个 Δ」。本模块把每次测量的结论追加进一份固定文件，
文件名就是「电脑用户-平台」：

    latency/records/<user>-ubuntu.yaml  原生 Ubuntu 采集路径

一台机器一个文件，换机器就多一个文件，互不覆盖。一条记录长这样，键是测量序号：

    1:
      at: '2026-11-16T21:04:54+08:00'
      camera: 0
      median_ms: 189.712      # 修正量：时间轴减掉这么多
      iqr_ms: 5.17            # 抖动
      samples: 213            # 有效样本
      ok: true                # 三个判据都过

下游只关心四个字段：``median_ms``、``camera``、``ok``、``at``。

读法
----
    import sys
    sys.path.insert(0, "<repo>/Paxini-Cap/Cap_Quest/latency")
    from latency_records import load_records, pick_latest, to_rows

    entry = pick_latest(load_records(), camera=0)   # 本机最近一次合格的 cam0
    print(entry["median_ms"])                       # 189.712

    rows = to_rows(load_records())                  # 摊平成列表，可直接喂 pandas
"""

from __future__ import annotations

import getpass
import os
import socket
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
QUEST_ROOT = HERE.parent
DEFAULT_RECORD_DIR = HERE / "records"

# 判据与测量脚本保持一致：三条都过才算 ok。
MIN_SAMPLES = 30
MAX_IQR_MS = 40.0
MAX_DRIFT_MS = 10.0
# 与上一次同类测量差超过这个值就提示：链路整体变了（驱动缓冲、曝光、换 USB 口）。
DELTA_WARN_MS = 20.0

PLATFORMS = ("ubuntu",)
PLATFORM_BACKEND = {"ubuntu": "linux"}
PLATFORM_LABEL = {"ubuntu": "原生 Ubuntu 采集路径（V4L2）"}

# 每次重写都带上这段注释，手工改文件后也不会丢说明。
HEADER = """\
# 相机延迟测量记录 —— 一台机器一个文件，按测量顺序追加，不要手工改序号
#
# 文件名   <电脑用户>-ubuntu.yaml   例：ifnot-ubuntu.yaml
# 序号    同一台机器上的第几次测量，从 1 起递增；cam0 与 cam1 混排，看 camera 字段
# 结果    median_ms 是推荐使用的 Δ（含显示链路，单位毫秒）
# 合格    ok: true = 样本 >= 30、IQR <= 40 ms、前后漂移 <= 10 ms
# 偏差    delta_prev_ms = 与上一次同参数测量的差；超过 20 ms 说明整条链路变了
#
# 下游读数
#   import sys; sys.path.insert(0, "<repo>/Paxini-Cap/Cap_Quest/latency")
#   from latency_records import load_records, pick_latest
#   entry = pick_latest(load_records(), camera=0)     # 不给路径 = 本机这份
#   median = entry["median_ms"]
#
"""


# ---------------------------------------------------------------- 路径与命名


def _local_platform() -> str:
    """本机属于哪条采集路径。现在只有一条：原生 Ubuntu。"""
    return "ubuntu"


def machine_key(platform: str, user: str | None = None) -> str:
    """``ifnot-ubuntu`` 这种「电脑用户-平台」标识，同时用作文件名主干。"""
    if platform not in PLATFORMS:
        raise RuntimeError(f"platform 只能是 {PLATFORMS}，收到 {platform!r}")
    name = (user or getpass.getuser() or "unknown").strip()
    return f"{name}-{platform}".lower()


def record_path(
    platform: str | None = None,
    record_dir: Path | str | None = None,
    machine: str | None = None,
) -> Path:
    """算出这份台账该写在哪。"""
    platform = platform or _local_platform()
    directory = Path(record_dir) if record_dir else DEFAULT_RECORD_DIR
    stem = machine or machine_key(platform)
    return directory / f"{stem}.yaml"


def _relative(path: Path | str | None) -> str | None:
    """输出目录记成相对 Cap_Quest 的路径，换机器/换盘符也不会指错。"""
    if path is None:
        return None
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(QUEST_ROOT).as_posix()
    except ValueError:
        return resolved.as_posix()


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------- 读


def _default_record_file() -> Path:
    """不传路径时读哪一份。

    先按「本机平台」找（就是 ``<user>-ubuntu.yaml``）；找不到再看目录里是不是
    只有一份——换了用户名、或者从别人机器拷了一份台账过来时，只有一份就一定是它。
    """
    primary = record_path()
    if primary.is_file():
        return primary
    candidates = sorted(DEFAULT_RECORD_DIR.glob("*.yaml")) if DEFAULT_RECORD_DIR.is_dir() else []
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        names = "、".join(item.name for item in candidates)
        raise RuntimeError(
            f"records/ 下有多份台账（{names}），本机这份还没建，请显式传文件路径"
        )
    return primary


def load_records(path: Path | str | None = None) -> dict[str, Any]:
    """读一份台账；path 省略时读本机这份（不存在就返回空骨架）。"""
    target = Path(path) if path else _default_record_file()
    if not target.is_file():
        platform = target.stem.rsplit("-", 1)[-1]
        payload: dict[str, Any] = {"machine": target.stem, "measurements": {}}
        if platform in PLATFORMS:
            payload["platform"] = platform
            payload["backend"] = PLATFORM_BACKEND[platform]
        return payload
    try:
        payload = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeError(f"台账读不出来：{target}（{exc}）") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"台账顶层不是映射：{target}")
    payload.setdefault("machine", target.stem)
    measurements = payload.get("measurements") or {}
    # YAML 可能把 1 读成 int，也可能因为引号读成 "1"，统一成 int。
    normalized: dict[int, dict] = {}
    for key, value in measurements.items():
        try:
            normalized[int(key)] = value
        except (TypeError, ValueError):
            continue
    payload["measurements"] = dict(sorted(normalized.items()))
    return payload


def load_all(record_dir: Path | str | None = None) -> dict[str, dict[str, Any]]:
    """读整个 records/ 目录，返回 ``{文件名主干: 台账}``，用于跨机器汇总。"""
    directory = Path(record_dir) if record_dir else DEFAULT_RECORD_DIR
    if not directory.is_dir():
        return {}
    return {
        item.stem: load_records(item)
        for item in sorted(directory.glob("*.yaml"))
    }


def pick_latest(
    data: dict[str, Any] | None = None,
    camera: int | None = None,
    only_ok: bool = True,
) -> dict[str, Any] | None:
    """取最近一次记录。

    camera 给定时只看那一路（两路相机延迟不同，不能混用）；only_ok 默认跳过
    不合格的测量，避免下游拿到一个 IQR 60 ms 的数。
    """
    payload = data if data is not None else load_records()
    entries = (payload.get("measurements") or {}).items()
    chosen: tuple[int, dict[str, Any]] | None = None
    for seq, entry in entries:
        if camera is not None and entry.get("camera") != camera:
            continue
        if only_ok and not entry.get("ok"):
            continue
        if chosen is None or seq > chosen[0]:
            chosen = (seq, entry)
    return chosen[1] if chosen else None


def latest_median(
    camera: int | None = None,
    data: dict[str, Any] | None = None,
    only_ok: bool = True,
) -> float | None:
    """下游最常用的那一句：拿到该用的 Δ（毫秒）。"""
    entry = pick_latest(data, camera=camera, only_ok=only_ok)
    return None if entry is None else float(entry["median_ms"])


def to_rows(data: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """摊平成一行一条记录，方便 ``pandas.DataFrame(to_rows(...))``。"""
    payload = data if data is not None else load_records()
    machine = payload.get("machine")
    rows: list[dict[str, Any]] = []
    for seq, entry in (payload.get("measurements") or {}).items():
        row = {"machine": machine, "seq": seq}
        row.update(entry)
        rows.append(row)
    return rows


# ---------------------------------------------------------------- 判定与写


def judge(stats: dict[str, Any]) -> tuple[bool, list[str]]:
    """按判据给结论，返回 (是否合格, 不合格原因)。

    ``drift_ms`` 缺失时跳过那一条（早期记录可能没有这个字段）。
    """
    reasons: list[str] = []
    samples = int(stats.get("samples") or 0)
    if samples < MIN_SAMPLES:
        reasons.append(f"样本 {samples} < {MIN_SAMPLES}")
    iqr = float(stats.get("iqr_ms") or 0.0)
    if iqr > MAX_IQR_MS:
        reasons.append(f"IQR {iqr:.1f} ms > {MAX_IQR_MS:g}")
    drift = abs(float(stats.get("drift_ms") or 0.0))
    if drift > MAX_DRIFT_MS:
        reasons.append(f"前后漂移 {drift:.1f} ms > {MAX_DRIFT_MS:g}")
    return (not reasons, reasons)


def _comparable_previous(
    measurements: dict[int, dict[str, Any]], entry: dict[str, Any], camera: int
) -> dict[str, Any] | None:
    """找上一次可直接对比的测量：同相机、合格、采集参数没变。

    延迟会整体跳（驱动缓冲从 1 帧涨到 3~4 帧就是 +100 ms），跟上一次比一下最
    容易发现「链路变了但数据看起来都正常」。
    """
    keys = ("width", "height", "codec")
    for seq in sorted(measurements, reverse=True):
        prev = measurements[seq]
        if prev.get("camera") != camera or not prev.get("ok"):
            continue
        if prev.get("median_ms") is None:
            continue
        if any(prev.get(key) != entry.get(key) for key in keys):
            continue
        return prev
    return None


def record_measurement(
    *,
    platform: str,
    camera: int,
    stats: dict[str, Any],
    meta: dict[str, Any] | None = None,
    output_dir: Path | str | None = None,
    record_dir: Path | str | None = None,
    machine: str | None = None,
    detection_rate: float | None = None,
    note: str = "",
    clock_domain: str = "",
    at: str | None = None,
) -> tuple[Path, int, dict[str, Any]]:
    """追加一条测量记录，返回 (文件路径, 序号, 记录内容)。

    只追加，不覆盖：历史读数留着才能看出换了 USB 口/改了分辨率之后有没有变化。
    ``at`` 只在补录历史数据时给，正常测量留空用当前时间。
    """
    target = record_path(platform, record_dir, machine)
    try:
        payload = load_records(target) if target.is_file() else {}
    except RuntimeError:
        # 文件坏了就备份再重开，不让一次写坏的历史挡住这次测量。
        broken = target.with_suffix(f".broken-{datetime.now():%Y%m%d_%H%M%S}.yaml")
        target.replace(broken)
        payload = {}

    measurements = payload.get("measurements") or {}
    seq = (max(measurements) if measurements else 0) + 1
    stats = stats or {}
    ok, reasons = judge(stats)

    entry: dict[str, Any] = {
        "at": at or _now(),
        "camera": camera,
        "median_ms": round(float(stats.get("median_ms") or 0.0), 3)
        if stats.get("samples")
        else None,
        "iqr_ms": round(float(stats.get("iqr_ms") or 0.0), 3) if stats.get("samples") else None,
        "samples": int(stats.get("samples") or 0),
        "ok": ok,
    }
    if not ok:
        entry["reject_reason"] = "; ".join(reasons)
    if stats.get("samples"):
        entry["mean_ms"] = round(float(stats.get("mean_ms") or 0.0), 3)
        entry["std_ms"] = round(float(stats.get("std_ms") or 0.0), 3)
        if "drift_ms" in stats:
            # 只在测量脚本真的算过漂移时才写，不拿 0 冒充「测过且没漂」。
            entry["drift_ms"] = round(float(stats.get("drift_ms") or 0.0), 3)
    if detection_rate is not None:
        entry["detection_rate"] = round(float(detection_rate), 3)

    meta = meta or {}
    for key in ("width", "height", "fps", "codec", "seconds", "display_latency_ms", "device_name"):
        if meta.get(key) is not None:
            entry[key] = meta[key]
    if output_dir is not None:
        entry["output_dir"] = _relative(output_dir)
    if note:
        entry["note"] = note

    previous = _comparable_previous(measurements, entry, camera)
    if previous and entry.get("median_ms") is not None:
        entry["delta_prev_ms"] = round(
            float(entry["median_ms"]) - float(previous["median_ms"]), 3
        )

    measurements[seq] = entry
    head: dict[str, Any] = {
        "machine": machine or machine_key(platform),
        "platform": platform,
        "backend": PLATFORM_BACKEND[platform],
        "collect_path": PLATFORM_LABEL[platform],
        "host": socket.gethostname(),
        "created_at": payload.get("created_at") or at or _now(),
        "updated_at": _now(),
        "measurements": measurements,
    }
    if clock_domain:
        head["clock_domain"] = clock_domain

    target.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(head, allow_unicode=True, sort_keys=False, default_flow_style=False)
    # 先写临时文件再替换，测量中途断电也不会留下半份 YAML。
    temp = target.with_suffix(".yaml.tmp")
    temp.write_text(HEADER + body, encoding="utf-8", newline="\n")
    temp.replace(target)
    return target, seq, entry


def describe(data: dict[str, Any]) -> list[str]:
    """把一份台账压成几行，命令行里直接打印。"""
    lines = [
        f"机器     {data.get('machine')}（{data.get('platform')} / {data.get('backend')}）"
    ]
    entries = data.get("measurements") or {}
    if not entries:
        lines.append("记录     （空）")
        return lines
    lines.append(f"共       {len(entries)} 次测量")
    for seq, entry in entries.items():
        value = entry.get("median_ms")
        result = f"{value:8.2f} ms" if value is not None else "    无样本"
        flag = "OK" if entry.get("ok") else "x "
        delta = entry.get("delta_prev_ms")
        # 只是陈述「与上一条合格记录差多少」，不代替判断：链路偶发整体跳一档时，
        # 跳的那条和回来的那条都会显示出大差值。
        trend = f"{delta:+7.1f}" if delta is not None else "      -"
        lines.append(
            f"  {seq:>2}.  cam{entry.get('camera')}  {result}   "
            f"IQR {entry.get('iqr_ms') or 0:5.2f}  n={entry.get('samples') or 0:<4} "
            f"{flag} Δ{trend}  {entry.get('at')}"
        )
    return lines
