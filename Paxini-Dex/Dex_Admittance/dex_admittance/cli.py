from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import threading
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import AppConfig, load_config
from .calibration import force_sweep, joint_direction_record
from .engine import AdmittanceEngine
from .hardware import HardwareError, PXDexHardware
from .models import ContactFrame, ControlOutput, ControlState
from .session import SessionLogger
from .simulator import OBJECT_MODELS, SimulatedHand


DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config" / "default.yaml"
DEFAULT_DATA_ROOT = Path(__file__).resolve().parents[1] / "data"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dex-admittance",
        description="PXDex III force-admittance control prototype",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    subparsers = parser.add_subparsers(dest="command", required=True)

    simulate = subparsers.add_parser(
        "simulate", help="run the complete controller against a simulated hand"
    )
    simulate.add_argument(
        "--profile",
        choices=("soft", "semi_rigid", "rigid"),
        default="soft",
    )
    simulate.add_argument(
        "--object",
        choices=tuple(OBJECT_MODELS),
        default="soft",
    )
    simulate.add_argument("--target-force", type=float, default=1.2)
    simulate.add_argument("--duration", type=float, default=8.0)
    simulate.add_argument("--release-duration", type=float, default=3.0)
    simulate.add_argument("--seed", type=int, default=7)
    simulate.add_argument("--operator", default="local")
    simulate.add_argument("--notes", default="simulation")
    simulate.add_argument("--no-log", action="store_true")
    simulate.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    simulate.add_argument(
        "--stale-at",
        type=float,
        default=None,
        help="freeze simulated timestamps at this many seconds",
    )
    simulate.add_argument(
        "--sensor-fault-at",
        type=float,
        default=None,
        help="inject a sensor fault at this many seconds",
    )

    check = subparsers.add_parser(
        "check", help="connect and print read-only device status"
    )
    check.add_argument("--port", default="/dev/ttyUSB0")
    check.add_argument("--show-sdk-logs", action="store_true")
    check.add_argument(
        "--ack-connect-enables-motors",
        action="store_true",
        help="required because the vendor connect call writes a hold target and enables motors",
    )

    mode_position = subparsers.add_parser(
        "mode-position",
        help="explicitly switch the PXDex to position mode, then disconnect",
    )
    mode_position.add_argument("--port", default="/dev/ttyUSB0")
    mode_position.add_argument("--confirm-mode", action="store_true")
    mode_position.add_argument("--show-sdk-logs", action="store_true")
    mode_position.add_argument("--ack-connect-enables-motors", action="store_true")

    baseline = subparsers.add_parser(
        "calibrate-baseline", help="record an unloaded tactile baseline"
    )
    baseline.add_argument("--port", default="/dev/ttyUSB0")
    baseline.add_argument("--duration", type=float, default=5.0)
    baseline.add_argument(
        "--output",
        type=Path,
        default=None,
        help="baseline JSON path; defaults below Dex_Admittance/data/calibration",
    )
    baseline.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    baseline.add_argument("--show-sdk-logs", action="store_true")
    baseline.add_argument("--ack-connect-enables-motors", action="store_true")

    sensor_zero = subparsers.add_parser(
        "calibrate-sensors",
        help="calibrate the unloaded tactile sensor zero; no motion command is sent",
    )
    sensor_zero.add_argument("--port", default="/dev/ttyUSB0")
    sensor_zero.add_argument("--confirm-zero", action="store_true")
    sensor_zero.add_argument("--show-sdk-logs", action="store_true")
    sensor_zero.add_argument("--ack-connect-enables-motors", action="store_true")

    joint = subparsers.add_parser(
        "calibrate-joint",
        help="apply one small joint step to identify direction, then restore",
    )
    joint.add_argument("--port", default="/dev/ttyUSB0")
    joint.add_argument("--joint", type=int, choices=range(4), required=True)
    joint.add_argument("--delta-deg", type=float, default=10.0)
    joint.add_argument("--hold", type=float, default=3.0)
    joint.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    joint.add_argument("--switch-position-mode", action="store_true")
    joint.add_argument("--show-sdk-logs", action="store_true")
    joint.add_argument("--ack-connect-enables-motors", action="store_true")

    sweep = subparsers.add_parser(
        "calibrate-force-sweep",
        help="run a guarded open-loop closure sweep for force-angle identification",
    )
    sweep.add_argument("--port", default="/dev/ttyUSB0")
    sweep.add_argument("--step-deg", type=float, default=0.5)
    sweep.add_argument("--max-closure-deg", type=float, default=30.0)
    sweep.add_argument("--settle", type=float, default=0.6)
    sweep.add_argument("--max-force", type=float, default=1.5)
    sweep.add_argument("--switch-position-mode", action="store_true")
    sweep.add_argument("--show-sdk-logs", action="store_true")
    sweep.add_argument("--ack-connect-enables-motors", action="store_true")

    device = subparsers.add_parser(
        "device-shell",
        help="connect and use an explicit interactive motion-safety shell",
    )
    device.add_argument("--port", default="/dev/ttyUSB0")
    device.add_argument(
        "--profile",
        choices=("soft", "semi_rigid", "rigid"),
        default="soft",
    )
    device.add_argument("--target-force", type=float, default=1.2)
    device.add_argument("--baseline", type=Path, default=None)
    device.add_argument("--object", default="sponge")
    device.add_argument("--operator", default="local")
    device.add_argument("--notes", default="")
    device.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    device.add_argument(
        "--live-telemetry",
        action="store_true",
        help="print force and state every 0.5 s; disabled by default",
    )
    device.add_argument(
        "--show-sdk-logs",
        action="store_true",
        help="keep native SDK logs on stderr instead of data/sdk-logs",
    )
    device.add_argument("--ack-connect-enables-motors", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config(args.config)
    if args.command == "simulate":
        return run_simulation(args, config)
    if args.command == "device-shell":
        return run_device_shell(args, config)
    handlers = {
        "check": run_check,
        "mode-position": run_set_position_mode,
        "calibrate-sensors": run_sensor_calibration,
        "calibrate-baseline": run_baseline_calibration,
        "calibrate-joint": run_joint_calibration,
        "calibrate-force-sweep": run_force_sweep_calibration,
    }
    if args.command in handlers:
        handler = handlers[args.command]
        if args.show_sdk_logs:
            return handler(args, config)
        data_root = getattr(args, "data_root", DEFAULT_DATA_ROOT)
        path = (
            data_root
            / "sdk-logs"
            / f"{args.command}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
        )
        with _NativeConsoleCapture(path):
            result = handler(args, config)
        print(f"SDK native log: {path}")
        return result
    raise AssertionError(f"unhandled command {args.command}")


def _metadata(
    config: AppConfig,
    args: argparse.Namespace,
    *,
    mode: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "started_at": datetime.now().astimezone().isoformat(),
        "mode": mode,
        "hand_side": config.hand.side,
        "object": getattr(args, "object", None),
        "target_force_n": getattr(args, "target_force", None),
        "profile": getattr(args, "profile", None),
        "operator": getattr(args, "operator", "local"),
        "notes": getattr(args, "notes", ""),
        "sdk_version": "pxhandsdk 1.0.0.0",
    }
    if extra:
        metadata.update(extra)
    return metadata


def run_simulation(args: argparse.Namespace, config: AppConfig) -> int:
    if args.duration <= 0 or args.release_duration < 0:
        raise SystemExit("duration must be positive and release duration non-negative")
    plant = SimulatedHand(config, args.object, seed=args.seed)
    engine = AdmittanceEngine(config, args.profile, args.target_force)
    initial = plant.frame()
    output = engine.start(initial, 0.0)
    logger: SessionLogger | None = None
    if not args.no_log:
        logger = SessionLogger(
            args.data_root,
            _metadata(config, args, mode="simulation"),
            config,
            object_name=args.object,
            target_force_n=args.target_force,
        )
        print(f"session: {logger.directory}")

    try:
        output, clock_s = _run_simulated_phase(
            plant,
            engine,
            output,
            config.control.period_s,
            args.duration,
            logger,
            now_start_s=0.0,
            stale_at=args.stale_at,
            sensor_fault_at=args.sensor_fault_at,
        )
        latest = plant.frame()
        if output.state != ControlState.FAULT:
            output = engine.release(latest, clock_s)
            _print_sample(output)
            output, clock_s = _run_simulated_phase(
                plant,
                engine,
                output,
                config.control.period_s,
                args.release_duration,
                logger,
                now_start_s=clock_s,
            )
        final = engine.stop(plant.frame(), clock_s)
        if logger is not None:
            logger.record(plant.frame(), final)
        _print_sample(final)
        return 0
    finally:
        if logger is not None:
            logger.close()


def _run_simulated_phase(
    plant: SimulatedHand,
    engine: AdmittanceEngine,
    output: ControlOutput,
    period_s: float,
    duration_s: float,
    logger: SessionLogger | None,
    *,
    now_start_s: float,
    stale_at: float | None = None,
    sensor_fault_at: float | None = None,
) -> tuple[ControlOutput, float]:
    steps = max(0, int(round(duration_s / period_s)))
    phase_start_s = now_start_s
    next_print = now_start_s
    for step_index in range(steps):
        elapsed = (step_index + 1) * period_s
        if stale_at is not None and elapsed >= stale_at:
            plant.freeze_frames(True)
        if (
            sensor_fault_at is not None
            and elapsed >= sensor_fault_at
            and plant.sensor_fault_count == 0
        ):
            plant.set_sensor_fault(True)
        plant.command(output.angles_deg)
        plant.advance(period_s)
        current = plant.frame()
        # The wall clock continues even when the simulated sensor timestamp freezes.
        engine_now = phase_start_s + elapsed
        output = engine.step(current, engine_now, period_s)
        if logger is not None:
            logger.record(current, output)
        if output.timestamp_s >= next_print:
            _print_sample(output)
            next_print += 0.5
        if output.state in {ControlState.FAULT, ControlState.STOPPED}:
            break
    return output, phase_start_s + steps * period_s


def _print_sample(output: ControlOutput) -> None:
    events = f" events={','.join(output.events)}" if output.events else ""
    print(
        f"t={output.timestamp_s:7.3f}s state={output.state.value:<14} "
        f"F=({output.index.force_n:4.2f},{output.middle.force_n:4.2f})N "
        f"L=({output.index.local_force_n:4.2f},{output.middle.local_force_n:4.2f})N "
        f"Fd={output.index.target_force_n:4.2f}N "
        f"q=[{output.angles_deg[0]:5.2f},{output.angles_deg[1]:5.2f},"
        f"{output.angles_deg[2]:5.2f},{output.angles_deg[3]:5.2f}]deg "
        f"{output.reason}{events}",
        flush=True,
    )


def _require_connection_ack(args: argparse.Namespace) -> None:
    if not args.ack_connect_enables_motors:
        raise SystemExit(
            "refusing to connect: the vendor connect call writes a hold target and "
            "enables motors. Re-run with --ack-connect-enables-motors after clearing "
            "the workspace."
        )


def run_check(args: argparse.Namespace, config: AppConfig) -> int:
    _require_connection_ack(args)
    hardware = PXDexHardware(config, args.port)
    try:
        status = hardware.connect()
        print(json.dumps(asdict(status), indent=2, ensure_ascii=False))
        return 0
    except HardwareError as exc:
        print(f"device check failed: {exc}")
        return 2
    finally:
        hardware.close()


def run_set_position_mode(args: argparse.Namespace, config: AppConfig) -> int:
    _require_connection_ack(args)
    if not args.confirm_mode:
        raise SystemExit(
            "refusing to switch mode without --confirm-mode; mode switching can "
            "disable and re-enable the motors"
        )
    hardware = PXDexHardware(config, args.port)
    try:
        hardware.connect()
        hardware.set_position_mode()
        print(json.dumps(asdict(hardware.status()), indent=2, ensure_ascii=False))
        return 0
    except HardwareError as exc:
        print(f"position mode switch failed: {exc}")
        return 2
    finally:
        hardware.close()


def run_baseline_calibration(args: argparse.Namespace, config: AppConfig) -> int:
    _require_connection_ack(args)
    if args.duration <= 0:
        raise SystemExit("duration must be positive")
    hardware = PXDexHardware(config, args.port)
    samples: list[tuple[tuple[float, float, float], ...]] = []
    try:
        hardware.connect()
        status = hardware.status()
        if status.sensor_fault_count:
            raise HardwareError(
                f"cannot calibrate with {status.sensor_fault_count} sensor faults"
            )
        hardware.start_collection()
        deadline = time.monotonic() + args.duration
        last_timestamp: float | None = None
        while time.monotonic() < deadline:
            frame = hardware.wait_for_frame(timeout_s=0.25)
            if last_timestamp != frame.timestamp_s:
                samples.append(frame.force_vectors_n)
                last_timestamp = frame.timestamp_s
        if not samples:
            raise HardwareError("no samples collected")
        per_sensor = []
        for index in range(10):
            values = [sample[index][2] for sample in samples]
            per_sensor.append(
                {
                    "mean_n": statistics.fmean(values),
                    "std_n": statistics.pstdev(values) if len(values) > 1 else 0.0,
                    "minimum_n": min(values),
                    "maximum_n": max(values),
                }
            )
        output = args.output
        if output is None:
            output = (
                args.data_root
                / "calibration"
                / f"baseline-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
            )
        output.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "created_at": datetime.now().astimezone().isoformat(),
            "duration_s": args.duration,
            "sample_count": len(samples),
            "sensor_vectors_n": [
                [0.0, 0.0, item["mean_n"]] for item in per_sensor
            ],
            "sensor_stats": per_sensor,
        }
        output.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"baseline: {output}")
        print(f"samples: {len(samples)}")
        return 0
    except HardwareError as exc:
        print(f"baseline calibration failed: {exc}")
        return 2
    finally:
        hardware.close()


def run_sensor_calibration(args: argparse.Namespace, config: AppConfig) -> int:
    _require_connection_ack(args)
    if not args.confirm_zero:
        raise SystemExit(
            "refusing to calibrate tactile sensors without --confirm-zero; "
            "remove all contact and objects first"
        )
    hardware = PXDexHardware(config, args.port)
    try:
        hardware.connect()
        status = hardware.status()
        if status.sensor_fault_count:
            raise HardwareError(
                f"cannot calibrate {status.sensor_fault_count} faulty sensors"
            )
        hardware.calibrate_sensors()
        forces = hardware.read_force_vectors()
        print(
            "tactile zero completed; current z="
            f"{[round(vector[2], 3) for vector in forces]} N"
        )
        return 0
    except HardwareError as exc:
        print(f"sensor calibration failed: {exc}")
        return 2
    finally:
        hardware.close()


def run_joint_calibration(args: argparse.Namespace, config: AppConfig) -> int:
    _require_connection_ack(args)
    if args.delta_deg == 0:
        raise SystemExit("delta-deg must be non-zero")
    hardware = PXDexHardware(config, args.port)
    try:
        hardware.connect()
        if args.switch_position_mode:
            hardware.set_position_mode()
        hardware.require_position_mode()
        force_vectors = hardware.read_force_vectors()
        peak_force_n = max(vector[2] for vector in force_vectors)
        if peak_force_n >= config.control.activation_force_n:
            raise HardwareError(
                "contact detected before joint calibration "
                f"(peak sensor force={peak_force_n:.2f} N); remove the object "
                "and rerun the test"
            )
        if input(
            f"Apply {args.delta_deg:+.2f} deg to joint {args.joint}, "
            "observe direction, then restore? type MOVE: "
        ) != "MOVE":
            print("cancelled")
            return 0
        record = joint_direction_record(
            hardware, config, args.joint, args.delta_deg, args.hold
        )
        output = (
            args.data_root
            / "calibration"
            / f"joint-{args.joint}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(record, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"joint calibration record: {output}")
        return 0
    except HardwareError as exc:
        print(f"joint calibration failed: {exc}")
        return 2
    finally:
        hardware.close()


def run_force_sweep_calibration(
    args: argparse.Namespace, config: AppConfig
) -> int:
    _require_connection_ack(args)
    print(
        "This is open-loop calibration: confirm the object is lightly placed and "
        "the workspace and power cutoff are unobstructed."
    )
    if input("type SWEEP to continue: ") != "SWEEP":
        print("cancelled")
        return 0
    hardware = PXDexHardware(config, args.port)
    try:
        hardware.connect()
        if args.switch_position_mode:
            hardware.set_position_mode()
        rows, output = force_sweep(
            hardware,
            config,
            step_deg=args.step_deg,
            maximum_closure_deg=args.max_closure_deg,
            settle_s=args.settle,
            maximum_force_n=args.max_force,
        )
        print(f"force sweep: {output} ({len(rows)} rows)")
        return 0
    except (HardwareError, ValueError) as exc:
        print(f"force sweep failed: {exc}")
        return 2
    finally:
        hardware.close()


class _LiveControlLoop:
    def __init__(
        self,
        hardware: PXDexHardware,
        engine: AdmittanceEngine,
        logger: SessionLogger,
        period_s: float,
        live_telemetry: bool,
    ) -> None:
        self.hardware = hardware
        self.engine = engine
        self.logger = logger
        self.period_s = period_s
        self.live_telemetry = live_telemetry
        self.latest_output: ControlOutput | None = None
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.thread = threading.Thread(
            target=self._run, name="pxdex-admittance", daemon=True
        )
        self.error: Exception | None = None

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=2.0)
        if self.thread.is_alive():
            raise HardwareError("control thread did not stop")

    def _run(self) -> None:
        next_tick = time.monotonic()
        next_report = next_tick
        while not self.stop_event.is_set():
            try:
                frame = self.hardware.latest_frame()
                if frame is None:
                    time.sleep(self.period_s)
                    continue
                now = time.monotonic()
                with self.lock:
                    output = self.engine.step(frame, now, self.period_s)
                    self.latest_output = output
                if output.state in {
                    ControlState.APPROACH,
                    ControlState.CONTROLLING,
                    ControlState.HOLD,
                    ControlState.RELEASE,
                    ControlState.STALE_HOLD,
                    ControlState.STALE_RELEASE,
                    ControlState.FORCE_LIMIT,
                    ControlState.FIXED_SIDE_FORCE_LIMIT,
                    ControlState.LOCAL_LIMIT_HOLD,
                    ControlState.NO_CONTACT,
                    ControlState.FAULT,
                }:
                    self.hardware.set_angles(output.angles_deg)
                self.logger.record(frame, output)
                if output.events:
                    _print_sample(output)
                    next_report = now + 0.5
                elif self.live_telemetry and now >= next_report:
                    _print_sample(output)
                    next_report = now + 0.5
                if output.state in {
                    ControlState.FAULT,
                    ControlState.STOPPED,
                }:
                    self.stop_event.set()
                    break
            except Exception as exc:
                self.error = exc
                self.stop_event.set()
                break
            next_tick += self.period_s
            delay = next_tick - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_tick = time.monotonic()


def _load_baseline(path: Path | None) -> tuple[tuple[float, float, float], ...] | None:
    if path is None:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"baseline file not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read baseline file {path}: {exc}") from exc
    vectors = payload.get("sensor_vectors_n")
    if not isinstance(vectors, list) or len(vectors) != 10:
        raise ValueError("baseline JSON must contain 10 sensor_vectors_n entries")
    return tuple(
        tuple(float(value) for value in vector)  # type: ignore[misc]
        for vector in vectors
    )


class _NativeConsoleCapture:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._saved_stdout_fd: int | None = None
        self._saved_stderr_fd: int | None = None
        self._stream: Any = None
        self._python_stdout: Any = None
        self._previous_stdout: Any = None

    def __enter__(self) -> "_NativeConsoleCapture":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self.path.open("ab", buffering=0)
        self._saved_stdout_fd = os.dup(1)
        self._saved_stderr_fd = os.dup(2)
        self._previous_stdout = sys.stdout
        self._python_stdout = os.fdopen(
            os.dup(self._saved_stdout_fd),
            "w",
            buffering=1,
            encoding=getattr(sys.stdout, "encoding", "utf-8"),
        )
        os.dup2(self._stream.fileno(), 1)
        os.dup2(self._stream.fileno(), 2)
        sys.stdout = self._python_stdout
        return self

    def __exit__(self, *args: object) -> None:
        if self._python_stdout is not None:
            self._python_stdout.flush()
            sys.stdout = self._previous_stdout
            self._python_stdout.close()
            self._python_stdout = None
        if self._saved_stdout_fd is not None:
            os.dup2(self._saved_stdout_fd, 1)
            os.close(self._saved_stdout_fd)
            self._saved_stdout_fd = None
        if self._saved_stderr_fd is not None:
            os.dup2(self._saved_stderr_fd, 2)
            os.close(self._saved_stderr_fd)
            self._saved_stderr_fd = None
        if self._stream is not None:
            self._stream.close()
            self._stream = None


def run_device_shell(args: argparse.Namespace, config: AppConfig) -> int:
    if args.show_sdk_logs:
        return _run_device_shell(args, config)
    path = (
        args.data_root
        / "sdk-logs"
        / f"device-shell-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
    )
    with _NativeConsoleCapture(path):
        result = _run_device_shell(args, config)
    print(f"SDK native log: {path}")
    return result


def _run_device_shell(args: argparse.Namespace, config: AppConfig) -> int:
    _require_connection_ack(args)
    hardware = PXDexHardware(config, args.port)
    controller: _LiveControlLoop | None = None
    logger: SessionLogger | None = None
    engine: AdmittanceEngine | None = None
    try:
        baseline_vectors = _load_baseline(args.baseline)
        hardware.connect()
        print(json.dumps(asdict(hardware.status()), indent=2, ensure_ascii=False))
        print(
            "commands: status mode-position prepare home start force <N> hold release stop "
            "disable quit"
        )
        while True:
            command = input("dex> ").strip().lower()
            if not command:
                continue
            if command == "status":
                print(json.dumps(asdict(hardware.status()), indent=2, ensure_ascii=False))
                if engine is not None:
                    print(
                        f"controller_state={engine.state.value} "
                        f"reason={engine.reason}"
                    )
                if controller is not None and controller.latest_output is not None:
                    _print_sample(controller.latest_output)
            elif command == "prepare":
                status = hardware.status()
                print(json.dumps(asdict(status), indent=2, ensure_ascii=False))
                if not status.motor_enabled:
                    print("NOT READY: motor is disabled")
                elif status.control_mode != "MOTOR_MODE_POSITION":
                    print(
                        "NOT READY: run 'mode-position' and confirm with MODE"
                    )
                else:
                    print("READY: position mode and motor enable state are valid")
            elif command == "mode-position":
                if controller is not None:
                    raise HardwareError("stop the controller before changing mode")
                if input(
                    "mode switching can disable/re-enable the motors; type MODE: "
                ) != "MODE":
                    print("mode switch cancelled")
                    continue
                hardware.set_position_mode()
                print("position mode confirmed")
            elif command == "home":
                if controller is not None:
                    raise HardwareError("stop the controller before homing")
                if input("type HOME to confirm movement: ") != "HOME":
                    print("homing cancelled")
                    continue
                hardware.stop_collection()
                hardware.home()
                print("homing completed")
            elif command == "start":
                if controller is not None:
                    print("controller is already running")
                    continue
                try:
                    hardware.require_position_mode()
                    hardware.start_collection()
                    print("opening hand before starting admittance...")
                    frame = hardware.open_to_open_pose(
                        velocity_deg_s=config.control.prestart_open_velocity_deg_s,
                        period_s=config.control.period_s,
                    )
                    hardware.stop_collection()
                    print(
                        "hand opened at "
                        f"{[round(value, 3) for value in frame.angles_deg]} deg"
                    )
                    if input(
                        "place the object lightly between the fingers, "
                        "then type GRASP: "
                    ) != "GRASP":
                        print("controller start cancelled")
                        continue
                    hardware.clear_latest_frame()
                    hardware.start_collection()
                    frame = hardware.wait_for_frame()
                    engine = AdmittanceEngine(
                        config,
                        args.profile,
                        args.target_force,
                        baseline_vectors_n=baseline_vectors,
                    )
                    logger = SessionLogger(
                        args.data_root,
                        _metadata(config, args, mode="device"),
                        config,
                        object_name=args.object,
                        target_force_n=args.target_force,
                    )
                    output = engine.start(frame, time.monotonic())
                    hardware.set_angles(output.angles_deg)
                    controller = _LiveControlLoop(
                        hardware,
                        engine,
                        logger,
                        config.control.period_s,
                        args.live_telemetry,
                    )
                    controller.start()
                    print(f"controller started; session={logger.directory}")
                except HardwareError as exc:
                    print(f"start failed; device shell remains open: {exc}")
                    hardware.stop_collection()
                    if logger is not None:
                        logger.close()
                        logger = None
                    engine = None
                    controller = None
            elif command.startswith("force "):
                if engine is None:
                    print("start the controller first")
                    continue
                value = float(command.split(maxsplit=1)[1])
                with controller.lock:
                    engine.set_target_force(value)
                print(f"target force ramp requested: {value:.2f} N")
            elif command in {"hold", "release"}:
                if engine is None or controller is None:
                    print("start the controller first")
                    continue
                frame = hardware.wait_for_frame()
                with controller.lock:
                    if command == "hold":
                        output = engine.hold(frame, time.monotonic())
                    else:
                        output = engine.release(frame, time.monotonic())
                hardware.set_angles(output.angles_deg)
                print(command)
            elif command == "stop":
                if controller is not None:
                    controller.stop()
                    if controller.error:
                        print(f"controller error: {controller.error}")
                    controller = None
                hardware.stop_collection()
                if logger is not None:
                    logger.close()
                    logger = None
                engine = None
                print("controller stopped; motor state unchanged")
            elif command == "disable":
                if controller is not None:
                    controller.stop()
                    controller = None
                hardware.stop_collection()
                if logger is not None:
                    logger.close()
                    logger = None
                hardware.disable()
                engine = None
                print("motors disabled")
            elif command == "quit":
                break
            else:
                print("unknown command")
        return 0
    except KeyboardInterrupt:
        print(
            "\ninterrupted; stopping controller without auto-disabling motors",
            flush=True,
        )
        return 130
    except (HardwareError, ValueError) as exc:
        print(f"device session error: {exc}", flush=True)
        return 2
    finally:
        if controller is not None:
            try:
                controller.stop()
            except HardwareError:
                pass
        if logger is not None:
            logger.close()
        hardware.close()
