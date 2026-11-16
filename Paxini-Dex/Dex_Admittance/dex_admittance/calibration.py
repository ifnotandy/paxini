from __future__ import annotations

import csv
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import AppConfig
from .hardware import PXDexHardware
from .mapping import closure_to_angles
from .models import clamp_angles


def clamp_joint_step(
    current_angles_deg: tuple[float, float, float, float],
    joint_index: int,
    delta_deg: float,
    config: AppConfig,
) -> tuple[float, float, float, float]:
    if not 0 <= joint_index < 4:
        raise ValueError("joint_index must be in 0..3")
    target = list(current_angles_deg)
    target[joint_index] += delta_deg
    return clamp_angles(
        target,
        config.hand.safe_min_angles_deg,
        config.hand.safe_max_angles_deg,
    )


def joint_direction_record(
    hardware: PXDexHardware,
    config: AppConfig,
    joint_index: int,
    delta_deg: float,
    hold_s: float,
) -> dict[str, Any]:
    hardware.require_position_mode()
    hardware.home()
    import time

    time.sleep(1.0)
    hardware.require_position_mode()
    hardware.enable()
    before_angles = hardware.read_angles()
    before_current_ma = hardware.read_motor_current()
    before_positions = hardware.read_current_positions()
    before_target_positions = hardware.read_target_positions()
    target = clamp_joint_step(before_angles, joint_index, delta_deg, config)
    restore_target = clamp_angles(
        before_angles,
        config.hand.safe_min_angles_deg,
        config.hand.safe_max_angles_deg,
    )
    try:
        hardware.set_angles(target)
        time.sleep(max(0.1, hold_s))
        during_angles = hardware.read_angles()
        force_vectors = hardware.read_force_vectors()
        during_current_ma = hardware.read_motor_current()
        during_positions = hardware.read_current_positions()
        during_target_positions = hardware.read_target_positions()
        last_error = hardware.last_error()
    finally:
        hardware.set_angles(restore_target)
        time.sleep(0.5)
    restored_angles = hardware.read_angles()
    return {
        "joint_index": joint_index,
        "requested_delta_deg": delta_deg,
        "before_angles_deg": before_angles,
        "target_angles_deg": target,
        "during_angles_deg": during_angles,
        "restore_target_angles_deg": restore_target,
        "restored_angles_deg": restored_angles,
        "force_vectors_n": force_vectors,
        "before_current_ma": before_current_ma,
        "during_current_ma": during_current_ma,
        "before_motor_positions": before_positions,
        "during_motor_positions": during_positions,
        "before_target_positions": before_target_positions,
        "during_target_positions": during_target_positions,
        "last_error": last_error,
    }


def force_sweep(
    hardware: PXDexHardware,
    config: AppConfig,
    *,
    step_deg: float,
    maximum_closure_deg: float,
    settle_s: float,
    maximum_force_n: float,
) -> tuple[list[dict[str, Any]], Path]:
    if step_deg <= 0 or maximum_closure_deg <= 0 or settle_s < 0:
        raise ValueError("invalid sweep parameters")
    if maximum_force_n <= 0 or maximum_force_n >= config.safety.hard_force_limit_n:
        raise ValueError("sweep force must be positive and below the hard limit")
    hardware.require_position_mode()
    hardware.start_collection()
    hardware.open_to_open_pose(
        velocity_deg_s=config.control.release_velocity_deg_s,
        period_s=config.control.period_s,
    )
    start = hardware.wait_for_frame()
    base = start.angles_deg
    rows: list[dict[str, Any]] = []
    last_timestamp = start.timestamp_s
    closure = 0.0
    import time

    try:
        while closure <= maximum_closure_deg + 1e-9:
            index_angles = closure_to_angles(
                (base[0], base[1]),
                closure,
                config.hand.closure_sign,
                config.hand.distal_to_proximal_ratio,
                (config.hand.safe_min_angles_deg[0], config.hand.safe_max_angles_deg[0]),
                (config.hand.safe_min_angles_deg[1], config.hand.safe_max_angles_deg[1]),
            )
            middle_angles = closure_to_angles(
                (base[2], base[3]),
                closure,
                config.hand.closure_sign,
                config.hand.distal_to_proximal_ratio,
                (config.hand.safe_min_angles_deg[2], config.hand.safe_max_angles_deg[2]),
                (config.hand.safe_min_angles_deg[3], config.hand.safe_max_angles_deg[3]),
            )
            target = (
                index_angles[0],
                index_angles[1],
                middle_angles[0],
                middle_angles[1],
            )
            hardware.set_angles(target)
            time.sleep(settle_s)
            frame = hardware.wait_for_next_frame(last_timestamp)
            last_timestamp = frame.timestamp_s
            index_force = sum(
                max(0.0, frame.force_vectors_n[i][2])
                for i in config.hand.index_sensor_indices
            )
            middle_force = sum(
                max(0.0, frame.force_vectors_n[i][2])
                for i in config.hand.middle_sensor_indices
            )
            rows.append(
                {
                    "closure_s_deg": closure,
                    "target_angles_deg": target,
                    "actual_angles_deg": frame.angles_deg,
                    "index_force_n": index_force,
                    "middle_force_n": middle_force,
                }
            )
            if max(index_force, middle_force) >= maximum_force_n:
                rows[-1]["stopped_for_force_limit"] = True
                break
            closure += step_deg
    finally:
        try:
            hardware.open_to_open_pose(
                velocity_deg_s=config.control.release_velocity_deg_s,
                period_s=config.control.period_s,
            )
        except Exception:
            # The original sweep error is more useful than a cleanup failure.
            pass

    directory = (
        Path(__file__).resolve().parents[1]
        / "data"
        / "calibration"
        / f"force-sweep-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    )
    directory.mkdir(parents=True, exist_ok=False)
    csv_path = directory / "sweep.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=(
                "closure_s_deg",
                "target_angles_deg",
                "actual_angles_deg",
                "index_force_n",
                "middle_force_n",
                "stopped_for_force_limit",
            ),
        )
        writer.writeheader()
        writer.writerows(rows)
    (directory / "metadata.json").write_text(
        json.dumps(
            {
                "created_at": datetime.now().astimezone().isoformat(),
                "base_angles_deg": base,
                "step_deg": step_deg,
                "maximum_closure_deg": maximum_closure_deg,
                "settle_s": settle_s,
                "maximum_force_n": maximum_force_n,
                "hand": asdict(config.hand),
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return rows, csv_path
