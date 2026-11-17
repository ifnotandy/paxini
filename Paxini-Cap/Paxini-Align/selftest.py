#!/usr/bin/env python3
"""Offline self-test for align_capture.py. No hardware, no real recordings.

Builds synthetic HDF5 files whose signals are known linear functions of time, so
the correct interpolated value can be computed in closed form and compared
exactly. Run with any Python that has numpy + h5py::

    python selftest.py

Prints one line per check and a total; exit code 1 if anything fails.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import h5py
except ImportError:
    print("缺少 h5py，无法自检")
    raise SystemExit(2)

import align_capture as ac


CAP_SLOT_POINTS = (49, 20, 49, 20, 49, 20, 49, 20, 119, 49)
#: Synthetic recordings store monotonic = unix - this offset, mirroring a real host.
MONO_OFFSET = 1_000_000_000_000

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), detail))
    print(f"[{'OK' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def make_recording(
    root: Path,
    *,
    glove_rate: float = 120.0,
    cam_rate: float = 30.0,
    glove_seconds: float = 4.0,
    cam_offset_s: float = -0.5,
    cam_seconds: float = 4.5,
    with_mono: bool = True,
    with_cam1: bool = False,
    t0_ns: int = 1_700_000_000_000_000_000,
    libver: str = "earliest",
) -> Path:
    """Synthetic recording: joint angles are a linear ramp in time."""
    rec = root / "data_synthetic_1"
    (rec / "cameras").mkdir(parents=True, exist_ok=True)

    n = int(glove_seconds * glove_rate)
    glove_dt = int(1e9 / glove_rate)
    glove_t = t0_ns + np.arange(n, dtype=np.int64) * glove_dt

    # joint_angles_deg[i] = 10 + 30 * (t - t0) / 1e9  → linear, verifiable exactly
    seconds = (glove_t - t0_ns) / 1e9
    angles = np.stack([10.0 + 30.0 * seconds, 5.0 - 10.0 * seconds,
                       seconds * 2.0, np.full_like(seconds, -3.5)], axis=1)

    m = int(cam_seconds * cam_rate)
    cam_dt = int(1e9 / cam_rate)
    cam_t = t0_ns + int(cam_offset_s * 1e9) + np.arange(m, dtype=np.int64) * cam_dt

    with h5py.File(rec / "data.h5", "w", libver=libver) as f:
        cap = f.create_group("cap")
        cap.create_dataset("timestamp_unix_ns", data=glove_t)
        if with_mono:
            cap.create_dataset("timestamp_monotonic_ns", data=glove_t - MONO_OFFSET)
        cap.create_dataset("joint_angles_deg", data=angles)
        cap.create_dataset("encoder_raw", data=(np.arange(n)[:, None] * 4 + np.arange(4)).astype(np.int32))
        cap.create_dataset("resultant_force", data=np.zeros((n, 10, 3), dtype=np.int16))
        cap.create_dataset("distribute_force",
                           data=np.zeros((n, 10, 119, 3), dtype=np.int16))
        counts = np.tile(np.array(CAP_SLOT_POINTS, dtype=np.uint32), (n, 1))
        cap.create_dataset("distribute_counts", data=counts)

        cams = f.create_group("cameras")
        for slot, series in (("right_cam0", cam_t), ("right_cam1", cam_t + 15_000_000)):
            if slot == "right_cam1" and not with_cam1:
                continue
            group = cams.create_group(slot)
            group.create_dataset("timestamp_unix_ns", data=series)
            if with_mono:
                group.create_dataset("timestamp_monotonic_ns", data=series - MONO_OFFSET)

        f.create_group("meta").attrs["format_version"] = 4
    return rec


def run(rec: Path, **kwargs) -> dict:
    defaults = dict(reference=None, timebase="auto", delay_ms=0.0, omit_distribution=False,
                    interpolate_distribution=True, keep_outside=False)
    defaults.update(kwargs)
    return ac.align_recording(rec, **defaults)


def test_linear_exactness(tmp: Path) -> None:
    rec = make_recording(tmp)
    result = run(rec, delay_ms=192.3)
    ts = result["timestamp_ns"] + MONO_OFFSET  # 回到夹具用的时间基准
    t0 = 1_700_000_000_000_000_000
    expected = 10.0 + 30.0 * (ts - t0) / 1e9
    got = result["state"]["joint_angles_deg"][:, 0]
    err = float(np.abs(expected - got).max())
    check("线性信号精确还原（线性插值应无误差）", err < 1e-9, f"最大误差 {err:.2e}°")


def test_delay_applied(tmp: Path) -> None:
    rec = make_recording(tmp)
    result = run(rec, delay_ms=192.3)
    delta = result["timestamp_cam_raw_ns"] - result["timestamp_ns"]
    check("主时间轴 = 相机戳 − Δ（192.3 ms）",
          bool(np.all(delta == int(round(192.3 * 1e6)))), f"逐行差值 {delta[0]} ns")


def test_outside_dropped(tmp: Path) -> None:
    rec = make_recording(tmp)  # camera starts 0.5 s before glove
    result = run(rec)
    total = int(result["camera_rows"])
    kept = int(result["aligned_rows"])
    dropped = int(result["dropped_outside"])
    check("覆盖范围外的相机帧被丢弃且计数正确",
          dropped > 0 and kept + dropped == total, f"保留 {kept} / 丢弃 {dropped} / 合计 {total}")


def test_keep_outside(tmp: Path) -> None:
    rec = make_recording(tmp)
    result = run(rec, keep_outside=True)
    check("--keep-outside 保留全部相机帧",
          int(result["aligned_rows"]) == int(result["camera_rows"]),
          f"{result['aligned_rows']} 行")


def test_nearest_integer(tmp: Path) -> None:
    rec = make_recording(tmp)
    result = run(rec)
    left = result["glove_left"]
    right = result["glove_right"]
    w = result["weights"]
    got = result["state"]["encoder_raw"][:, 0]
    base = np.arange(result["glove_rows"]) * 4  # encoder_raw[i, 0] = i * 4
    expected = np.where(w >= 0.5, base[right], base[left])
    check("整数计数走最近邻（不做平均）", bool(np.array_equal(got, expected)))


def test_counts_nearest(tmp: Path) -> None:
    rec = make_recording(tmp)
    result = run(rec)
    totals = result["state"]["distribute_counts"].sum(axis=1)
    check("distribute_counts 合计恒为 444", bool(np.all(totals == 444)), f"{totals[0]} 点/行")


def test_omit_distribution(tmp: Path) -> None:
    rec = make_recording(tmp)
    result = run(rec, omit_distribution=True)
    check("--omit-distribution 时不产生分布力数组",
          ac.DISTRIBUTION_STATE not in result["state"], result["distribution"])


def test_timebase_fallback(tmp: Path) -> None:
    rec = make_recording(tmp)
    with h5py.File(rec / "data.h5") as f:
        tb = ac.choose_timebase(f, "auto")
    rec2 = make_recording(tmp / "second", with_mono=False)
    with h5py.File(rec2 / "data.h5") as f:
        tb2 = ac.choose_timebase(f, "auto")
    check("timebase auto：有单调钟选 mono，没有退回 unix", tb == "mono" and tb2 == "unix",
          f"{tb} / {tb2}")


def test_multi_camera_views(tmp: Path) -> None:
    rec = make_recording(tmp, with_cam1=True)
    result = run(rec)
    views = result["views"]
    ok = set(views) == {"right_cam0", "right_cam1"}
    dt = np.abs(views["right_cam1"]["dt_ms"]) if ok else np.array([999.0])
    check("两路相机各自给出最近帧号（cam1 误差 ≤ 半帧）",
          ok and float(dt.max()) <= 16.7 + 0.1, f"cam1 最大偏差 {float(dt.max()):.2f} ms")


def test_write_and_reopen(tmp: Path) -> None:
    rec = make_recording(tmp, with_cam1=True)
    result = run(rec)
    path = ac.write_aligned(result, tmp / "out")
    with h5py.File(path) as f:
        keys = set(f.keys())
        summary = json.loads(f.attrs["summary"])
        mono = bool(np.all(np.diff(f["timestamp_ns"][:]) > 0))
    check("输出文件可重开、结构完整、summary 是合法 JSON",
          {"timestamp_ns", "glove", "state", "cameras"} <= keys and mono
          and summary["rows"] == result["aligned_rows"], f"{path.name} {summary['rows']} 行")


def test_gates_pass(tmp: Path) -> None:
    rec = make_recording(tmp)
    result = run(rec)
    problems = ac.gates(result)
    check("质量判据对合成数据全过", not problems, "；".join(problems) or "无问题")


def test_rejects_unsorted(tmp: Path) -> None:
    rec = make_recording(tmp)
    with h5py.File(rec / "data.h5", "r+") as f:
        series = f["cameras/right_cam0/timestamp_monotonic_ns"][:]
        series[10], series[11] = series[11], series[10]
        del f["cameras/right_cam0/timestamp_monotonic_ns"]
        f["cameras/right_cam0"].create_dataset("timestamp_monotonic_ns", data=series)
    try:
        run(rec)
    except ac.AlignError as exc:
        check("相机时间戳乱序时报错而不是硬算", "严格递增" in str(exc), str(exc)[:40])
    else:
        check("相机时间戳乱序时报错而不是硬算", False, "没有抛错")


def test_write_flag_repair(tmp: Path) -> None:
    """Simulate a crashed-write flag, then repair it the way --clear-write-flag does.

    Setting the flag without recomputing the superblock checksum is exactly the bug
    that once made eight real recordings look corrupt; this test locks both halves.
    h5py writes v0 superblocks by default and the collector's files are v3, so both
    layouts are handled here.
    """
    rec = make_recording(tmp, libver="latest")  # 真实采集文件是 v3 超级块
    path = rec / "data.h5"
    with open(path, "r+b") as handle:
        head = bytearray(handle.read(48))
        version = head[8]
        if version in (0, 1):
            head[20:24] = b"\x01\x00\x00\x00"
            head = head[:24]
        else:
            head[11] = 1
            head[44:48] = ac.lookup3(bytes(head[:44])).to_bytes(4, "little")
        handle.seek(0)
        handle.write(bytes(head))
    refused = False
    try:
        h5py.File(path, "r").close()
    except OSError as exc:
        refused = "already open for write" in str(exc)
    patched = ac.clear_write_flag(path)
    reopened = False
    try:
        with h5py.File(path, "r") as f:
            reopened = f["cap/joint_angles_deg"].shape[0] > 0
    except Exception:
        reopened = False
    check("写标志修复：置位后 h5py 拒绝、清位并重算校验和后数据完好",
          refused and patched and reopened,
          f"v{version} refused={refused} patched={patched} reopened={reopened}")


def test_lookup3_vector(tmp: Path) -> None:
    """Pin the checksum implementation: 44 zero bytes must hash to a fixed value.

    The same function reproduces the stored checksum of a real recording's
    superblock, so this vector pins the behaviour if it is ever refactored.
    """
    value = ac.lookup3(b"\x00" * 44)
    check("lookup3 实现与 HDF5 一致（固定向量）", value == 0xB0B6DC57, hex(value))


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="align_selftest_"))
    try:
        tests = [
            test_linear_exactness, test_delay_applied, test_outside_dropped, test_keep_outside,
            test_nearest_integer, test_counts_nearest, test_omit_distribution,
            test_timebase_fallback, test_multi_camera_views, test_write_and_reopen,
            test_gates_pass, test_rejects_unsorted, test_write_flag_repair, test_lookup3_vector,
        ]
        for index, test in enumerate(tests):
            work = root / f"case{index:02d}"
            work.mkdir(parents=True, exist_ok=True)
            try:
                test(work)
            except Exception as exc:  # a broken test must be reported, not fatal
                check(test.__name__, False, f"{exc.__class__.__name__}: {exc}")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    failed = [name for name, ok, _ in CHECKS if not ok]
    print("")
    print(f"{len(CHECKS) - len(failed)}/{len(CHECKS)} 项通过")
    if failed:
        print("失败：" + "，".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
