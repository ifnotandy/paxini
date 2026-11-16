from __future__ import annotations

import csv
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from .config import AppConfig
from .models import ContactFrame, ControlOutput


class SessionLogger:
    def __init__(
        self,
        root: str | Path,
        metadata: dict[str, Any],
        config: AppConfig,
        object_name: str,
        target_force_n: float,
    ) -> None:
        root = Path(root)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe_object = "".join(
            character if character.isalnum() or character in "-_" else "_"
            for character in object_name
        )
        self.directory = (
            root / f"{stamp}-{safe_object}-{target_force_n:.1f}N"
        )
        suffix = 1
        while self.directory.exists():
            self.directory = (
                root
                / f"{stamp}-{safe_object}-{target_force_n:.1f}N-{suffix:02d}"
            )
            suffix += 1
        self.directory.mkdir(parents=True, exist_ok=False)
        (self.directory / "metadata.json").write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        (self.directory / "config.yaml").write_text(
            yaml.safe_dump(
                {
                    "hand": asdict(config.hand),
                    "control": asdict(config.control),
                    "safety": asdict(config.safety),
                    "profiles": {
                        name: asdict(profile)
                        for name, profile in config.profiles.items()
                    },
                },
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        self._samples_file = (self.directory / "samples.csv").open(
            "w", newline="", encoding="utf-8"
        )
        self._events_file = (self.directory / "events.csv").open(
            "w", newline="", encoding="utf-8"
        )
        self._sample_writer = csv.DictWriter(
            self._samples_file, fieldnames=self._sample_fields()
        )
        self._sample_writer.writeheader()
        self._event_writer = csv.DictWriter(
            self._events_file, fieldnames=("timestamp_s", "state", "event", "reason")
        )
        self._event_writer.writeheader()
        self._closed = False

    @staticmethod
    def _sample_fields() -> list[str]:
        fields = [
            "timestamp_s",
            "state",
            "reason",
            "index_force_n",
            "middle_force_n",
            "index_target_force_n",
            "middle_target_force_n",
            "index_phase",
            "middle_phase",
            "index_closure_s_deg",
            "middle_closure_s_deg",
            "index_velocity_deg_s",
            "middle_velocity_deg_s",
            "index_local_force_n",
            "middle_local_force_n",
            "index_local_relief",
            "middle_local_relief",
            "q_index_proximal_target_deg",
            "q_index_distal_target_deg",
            "q_middle_proximal_target_deg",
            "q_middle_distal_target_deg",
            "q_index_proximal_actual_deg",
            "q_index_distal_actual_deg",
            "q_middle_proximal_actual_deg",
            "q_middle_distal_actual_deg",
            "current_index_ma",
            "current_index_distal_ma",
            "current_middle_ma",
            "current_middle_distal_ma",
            "temperature_index_c",
            "temperature_index_distal_c",
            "temperature_middle_c",
            "temperature_middle_distal_c",
        ]
        fields.extend(f"sensor_{index:02d}_z_n" for index in range(10))
        fields.extend(f"sensor_{index:02d}_magnitude_n" for index in range(10))
        return fields

    def record(self, frame: ContactFrame, output: ControlOutput) -> None:
        row: dict[str, Any] = {
            "timestamp_s": f"{output.timestamp_s:.6f}",
            "state": output.state.value,
            "reason": output.reason,
            "index_force_n": f"{output.index.force_n:.4f}",
            "middle_force_n": f"{output.middle.force_n:.4f}",
            "index_target_force_n": f"{output.index.target_force_n:.4f}",
            "middle_target_force_n": f"{output.middle.target_force_n:.4f}",
            "index_phase": output.index.phase.value,
            "middle_phase": output.middle.phase.value,
            "index_closure_s_deg": f"{output.index.closure_s_deg:.4f}",
            "middle_closure_s_deg": f"{output.middle.closure_s_deg:.4f}",
            "index_velocity_deg_s": f"{output.index.velocity_deg_s:.4f}",
            "middle_velocity_deg_s": f"{output.middle.velocity_deg_s:.4f}",
            "index_local_force_n": f"{output.index.local_force_n:.4f}",
            "middle_local_force_n": f"{output.middle.local_force_n:.4f}",
            "index_local_relief": int(output.index.local_relief),
            "middle_local_relief": int(output.middle.local_relief),
            "q_index_proximal_target_deg": f"{output.angles_deg[0]:.4f}",
            "q_index_distal_target_deg": f"{output.angles_deg[1]:.4f}",
            "q_middle_proximal_target_deg": f"{output.angles_deg[2]:.4f}",
            "q_middle_distal_target_deg": f"{output.angles_deg[3]:.4f}",
            "q_index_proximal_actual_deg": f"{frame.angles_deg[0]:.4f}",
            "q_index_distal_actual_deg": f"{frame.angles_deg[1]:.4f}",
            "q_middle_proximal_actual_deg": f"{frame.angles_deg[2]:.4f}",
            "q_middle_distal_actual_deg": f"{frame.angles_deg[3]:.4f}",
            "current_index_ma": frame.motor_current_ma[0],
            "current_index_distal_ma": frame.motor_current_ma[1],
            "current_middle_ma": frame.motor_current_ma[2],
            "current_middle_distal_ma": frame.motor_current_ma[3],
            "temperature_index_c": frame.motor_temperature_c[0],
            "temperature_index_distal_c": frame.motor_temperature_c[1],
            "temperature_middle_c": frame.motor_temperature_c[2],
            "temperature_middle_distal_c": frame.motor_temperature_c[3],
        }
        for index, value in enumerate(output.sensor_force_n):
            row[f"sensor_{index:02d}_z_n"] = f"{value:.4f}"
        for index, value in enumerate(output.local_force_n):
            row[f"sensor_{index:02d}_magnitude_n"] = f"{value:.4f}"
        self._sample_writer.writerow(row)
        for event in output.events:
            self._event_writer.writerow(
                {
                    "timestamp_s": f"{output.timestamp_s:.6f}",
                    "state": output.state.value,
                    "event": event,
                    "reason": output.reason,
                }
            )

    def close(self) -> None:
        if self._closed:
            return
        self._samples_file.flush()
        self._events_file.flush()
        self._samples_file.close()
        self._events_file.close()
        self._closed = True

    def __enter__(self) -> "SessionLogger":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
