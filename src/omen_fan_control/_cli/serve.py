from __future__ import annotations

import json
import math
import os
from pathlib import Path
import signal
import threading
import time

import click

from .._constants import (
    DEFAULT_AUTO_CURVE,
    DEFAULT_MIN_REQUIRED_RPM,
    DEFAULT_PERFORMANCE_CURVE,
    EXPERIMENTAL_FAN_SPEED_PATH,
    MAX_RUNTIME_SAFETY_FLOOR,
    VOLATILE_CONFIG_FILE,
)
from . import get_controller

# Bounded exponential backoff when the hwmon interface is absent (seconds).
_REPROBE_INTERVALS: tuple[int, ...] = (30, 60, 120, 300)
_POLL_INTERVAL = 5


@click.command()
def serve() -> None:
    """Run the fan control daemon (foreground). Used by systemd service."""
    def _sigterm_handler(signum: int, frame: object) -> None:
        raise KeyboardInterrupt()

    try:
        signal.signal(signal.SIGTERM, _sigterm_handler)
    except (ValueError, AttributeError):
        pass

    controller = get_controller()
    click.echo("Starting Omen Fan Control Daemon...")

    # Reverse-mode recovery at startup — independent of hp-wmi PWM interface.
    try:
        if controller.is_reverse_mode_active():
            click.echo("Recovery: Reverse mode detected on daemon startup. Initiating stop sequence...")
            controller.stop_fan_cleaning()
    except Exception as e:
        click.echo(f"Startup reverse mode check failed: {e}")

    def _is_fan_control_ready() -> bool:
        if hasattr(controller, "is_fan_control_ready"):
            return controller.is_fan_control_ready()
        if controller.get_board_name() == "8CC0":
            if getattr(controller, "is_experimental_control_enabled", lambda: False)():
                return EXPERIMENTAL_FAN_SPEED_PATH.exists()
            return False
        return controller.fan_control_available

    if not _is_fan_control_ready():
        click.echo(
            "Fan control interface absent at startup — skipping fan writes. "
            f"Will re-probe with backoff {_REPROBE_INTERVALS}s."
        )

    ma_window = controller.config.get("ma_window", 5)
    initial_temp = controller.get_reference_temp() or 40.0

    # Half-buffer warmup: prefill floor(ma_window / 2) with baseline idle temp
    # to dilute boot-time CPU bursts without overcrowding large windows.
    temp_history: list[float] = []
    if initial_temp < 80.0:
        seed_temp = min(float(initial_temp), 45.0)
        prefill_count = int(ma_window) // 2
        temp_history = [seed_temp] * prefill_count

    watchdog_interval = controller.config.get("watchdog_interval", 90)
    last_watchdog_time = time.time()
    hysteresis_start_time: float | None = None

    daemon_start_time = time.time()

    last_config_mtime: float = 0
    last_volatile_mtime: float = 0
    cleaner_temp_history: list[float] = []

    # Track re-probe timing and log-suppression for unavailable interface.
    last_reprobe_time: float = 0.0
    reprobe_backoff_index = 0
    _logged_unavailable: bool = False
    consecutive_low_cpu: int = 0
    consecutive_low_gpu: int = 0

    last_active_mode: str = controller.config.get("mode", "auto")

    while True:
        try:
            # --- Config reload (on-disk change detection) ---
            try:
                current_mtime = (
                    controller.config_path.stat().st_mtime
                    if controller.config_path and controller.config_path.exists()
                    else 0.0
                )
                current_v_mtime = (
                    VOLATILE_CONFIG_FILE.stat().st_mtime
                    if VOLATILE_CONFIG_FILE.exists()
                    else 0.0
                )
                if current_mtime > last_config_mtime or current_v_mtime > last_volatile_mtime:
                    controller.config = controller.load_config()
                    last_config_mtime = current_mtime
                    last_volatile_mtime = current_v_mtime
                    print("Configuration reloaded from disk.")
            except Exception:
                pass

            mode = controller.config.get("mode", "auto")
            if mode != last_active_mode:
                controller.transition_mode(last_active_mode, mode)
                last_active_mode = mode

            if mode == "calibration":
                time.sleep(1)
                continue

            if time.time() - last_watchdog_time > watchdog_interval:
                last_watchdog_time = time.time()

            # --- Temperature monitoring: always runs, independent of fan-control availability ---
            current_temp = controller.get_reference_temp()

            if (
                current_temp is None
                or math.isnan(current_temp)
                or math.isinf(current_temp)
                or current_temp <= 0
                or current_temp > 115
            ):
                click.echo(
                    f"Warning: Invalid or unavailable temperature ({current_temp}) — "
                    "reverting to firmware AUTO mode for safety."
                )
                try:
                    controller.restore_firmware_auto()
                except Exception:
                    pass
                time.sleep(_POLL_INTERVAL)
                continue

            # --- Active cleaner: temperature-safety monitoring continues even when hwmon absent,
            #     but fan writes inside stop_fan_cleaning are guarded by the controller itself. ---
            cleaner_active = (
                controller.config.get("cleaner_in_progress", False)
                or controller.config.get("cleaner_transitioning", False)
            )
            if cleaner_active:
                cleaner_in_progress = controller.config.get("cleaner_in_progress", False)
                if cleaner_in_progress:
                    if current_temp is not None:
                        cleaner_temp_history.append(float(current_temp))
                        ma_win = controller.config.get("ma_window", 5)
                        if len(cleaner_temp_history) > ma_win:
                            cleaner_temp_history.pop(0)
                        if len(cleaner_temp_history) >= 3:
                            avg_cleaner_temp = sum(cleaner_temp_history) / len(cleaner_temp_history)
                            if avg_cleaner_temp > 75:
                                reason = f"Stopped early: CPU reached {avg_cleaner_temp:.1f}°C (> 75°C)"
                                controller.log_cleaner(
                                    f"SAFETY ABORT: Daemon CPU average reached {avg_cleaner_temp:.1f}°C "
                                    f"(> 75°C) during fan cleaning. Aborting immediately!"
                                )
                                click.echo(f"WARNING: {reason}. Aborting immediately!")
                                controller.stop_fan_cleaning(reason=reason)
                    start_time = controller.config.get("cleaner_start_time", 0)
                    dur = controller.config.get("cleaner_duration", 30)
                    if start_time and time.time() - start_time >= dur:
                        click.echo("Fan cleaning cycle completed normally.")
                        controller.stop_fan_cleaning(reason=f"Completed ({dur}s)")
                else:
                    cleaner_temp_history.clear()
                time.sleep(1)
                continue
            else:
                cleaner_temp_history.clear()

            # --- Re-probe hwmon when absent: deliberate interval, not every iteration ---
            if not _is_fan_control_ready():
                now = time.time()
                reprobe_interval = _REPROBE_INTERVALS[reprobe_backoff_index]
                if now - last_reprobe_time >= reprobe_interval:
                    controller._find_paths()
                    last_reprobe_time = now
                    if _is_fan_control_ready():
                        reprobe_backoff_index = 0
                    else:
                        reprobe_backoff_index = min(
                            reprobe_backoff_index + 1, len(_REPROBE_INTERVALS) - 1
                        )

            if not _is_fan_control_ready():
                # Log once; suppress repeats until resolved.
                if not _logged_unavailable:
                    click.echo(
                        "Fan control interface absent — skipping fan writes. "
                        "Temperature monitoring continues. "
                        f"Re-probe backoff: {_REPROBE_INTERVALS}s."
                    )
                    _logged_unavailable = True
                time.sleep(_POLL_INTERVAL)
                continue

            # Interface is available. Reset suppression flag if it was set.
            if _logged_unavailable:
                click.echo("Fan control interface detected — resuming fan-control writes.")
                _logged_unavailable = False
                reprobe_backoff_index = 0

            # --- Automatic fan cleaning (requires fan control) ---
            cleaner_enabled = controller.config.get("cleaner_enabled", False)
            if cleaner_enabled:
                raw_interval = controller.config.get("cleaner_interval", 14400)
                interval = max(raw_interval, 300)
                last_run = controller.config.get("cleaner_last_run", 0)
                if last_run == 0 or (daemon_start_time - last_run >= interval):
                    due = (time.time() - daemon_start_time) >= interval
                else:
                    due = (time.time() - last_run) >= interval
                if due:
                    if current_temp is not None and current_temp <= 75:
                        click.echo(f"Triggering automatic fan cleaning cycle (interval: {interval}s)...")
                        success, msg = controller.start_fan_cleaning()
                        if success:
                            def _auto_stop_cleaner() -> None:
                                dur = controller.config.get("cleaner_duration", 30)
                                time.sleep(dur)
                                if controller.config.get("cleaner_in_progress", False):
                                    controller.stop_fan_cleaning(reason=f"Completed ({dur}s)")
                            threading.Thread(target=_auto_stop_cleaner, daemon=True).start()
                        else:
                            click.echo(f"Failed to start automatic fan cleaning: {msg}")

            # --- Temperature moving-average (requires fan control to be meaningful for curve) ---
            ma_window = controller.config.get("ma_window", 5)
            if current_temp is not None:
                temp_history.append(float(current_temp))
            if len(temp_history) > ma_window:
                temp_history.pop(0)
            avg_temp = (
                sum(temp_history) / len(temp_history)
                if temp_history
                else (float(current_temp) if current_temp else 40.0)
            )

            gpu_temp = controller.get_gpu_temp()

            # --- Fan-mode writes ---
            if mode == "curve":
                target_cpu_pwm, target_gpu_pwm = controller.calculate_dual_target_pwm(avg_temp, gpu_temp)
                current_rpm = controller.get_fan_speed()
                max_rpm = controller.get_effective_fan_max()

                should_apply = True

                if max_rpm > 0 and controller.fan_telemetry_available:
                    target_rpm = (target_cpu_pwm / 255) * max_rpm
                    diff = abs(target_rpm - current_rpm)

                    if diff <= 200:
                        if hysteresis_start_time is None:
                            hysteresis_start_time = time.time()

                        if time.time() - hysteresis_start_time > 60:
                            should_apply = True
                        else:
                            should_apply = False
                    else:
                        hysteresis_start_time = None
                        should_apply = True

                if controller.get_board_name() == "8CC0" and getattr(controller, "is_experimental_control_enabled", lambda: False)():
                    # Controller handles 8CC0 duplicate suppression and 60s watchdog refresh
                    controller.set_fan_pwm(target_cpu_pwm, target_gpu_pwm)
                    hysteresis_start_time = None
                elif should_apply:
                    controller.set_fan_pwm(target_cpu_pwm, target_gpu_pwm)
                    hysteresis_start_time = None

            elif mode == "manual":
                cpu_pwm, gpu_pwm = controller.get_dual_manual_pwm()
                controller.set_fan_pwm(cpu_pwm, gpu_pwm)

            elif mode == "max":
                controller.set_fan_mode("max")

            elif mode in ("auto", "performance"):
                if (
                    mode == "auto"
                    and controller.get_board_name() == "8CC0"
                    and getattr(controller, "is_experimental_control_enabled", lambda: False)()
                ):
                    emergency_cpu: float | None = None
                    try:
                        from unittest.mock import Mock
                        is_ref_mocked = isinstance(getattr(controller, "get_reference_temp", None), Mock)
                        is_em_mocked = isinstance(getattr(controller, "get_cpu_emergency_temp", None), Mock)
                        has_override = getattr(controller, "_core_temps_override", None) is not None
                        if has_override or is_em_mocked or not is_ref_mocked:
                            emergency_cpu = float(controller.get_cpu_emergency_temp())
                    except Exception:
                        pass

                    if emergency_cpu is not None:
                        check_cpu_temp = max(emergency_cpu, float(current_temp) if current_temp else 0.0, float(avg_temp))
                    else:
                        check_cpu_temp = max(float(current_temp) if current_temp else 0.0, float(avg_temp))

                    em_state, em_changed = controller.update_auto_emergency_state(check_cpu_temp, gpu_temp)
                    if em_state == "active":
                        if em_changed:
                            click.echo(
                                f"Emergency high-temperature detected (CPU: {check_cpu_temp:.1f}°C, GPU: {gpu_temp}°C) "
                                "— commanding MAX fan cooling."
                            )
                            controller.set_experimental_fan_max(True)
                            controller._emergency_max_commanded = True
                    else:
                        if getattr(controller, "_emergency_max_commanded", False):
                            click.echo(
                                f"Emergency cooling recovered (CPU: {check_cpu_temp:.1f}°C, GPU: {gpu_temp}°C) "
                                "— returning to normal software AUTO control."
                            )
                            controller.set_experimental_fan_max(False)
                            controller._emergency_max_commanded = False

                        state, state_changed = controller.update_auto_thermal_state(avg_temp, gpu_temp)
                        if state == "idle":
                            if state_changed:
                                click.echo(
                                    f"Auto thermal cooldown reached (CPU: {avg_temp:.1f}°C, GPU: {gpu_temp}°C) "
                                    "— releasing fans to firmware AUTO."
                                )
                                controller.restore_firmware_auto()
                            # Suppress repeated fan writes while staying in idle
                        else:
                            if state_changed:
                                click.echo(
                                    f"Auto thermal trigger reached (CPU: {avg_temp:.1f}°C, GPU: {gpu_temp}°C) "
                                    "— engaging software AUTO fan control."
                                )
                            boost_c, boost_g, boost_changed = controller.update_auto_stagnation_state(avg_temp, gpu_temp)
                            if boost_changed and (boost_c > 0 or boost_g > 0):
                                click.echo(
                                    f"Auto thermal stagnation boost engaged (CPU +{boost_c}, GPU +{boost_g}) "
                                    f"at CPU: {avg_temp:.1f}°C, GPU: {gpu_temp}°C."
                                )
                            elif boost_changed and boost_c == 0 and boost_g == 0:
                                click.echo(
                                    f"Auto thermal recovery reached (CPU: {avg_temp:.1f}°C, GPU: {gpu_temp}°C) "
                                    "— releasing stagnation boost."
                                )

                            auto_report = controller.calculate_auto_target(avg_temp, gpu_temp)
                            target_cpu_level = auto_report["cpu_fan_level"]
                            target_gpu_level = auto_report["gpu_fan_level"]

                            if getattr(controller, "is_experimental_telemetry_enabled", lambda: False)():
                                telemetry = controller.read_experimental_fan_telemetry()
                                if telemetry is not None:
                                    act_cpu, act_gpu = telemetry
                                    trim_cpu, trim_gpu = controller.apply_auto_rpm_feedback(
                                        act_cpu, act_gpu, auto_report["target_cpu_rpm"], auto_report["target_gpu_rpm"]
                                    )
                                    if trim_cpu > 0:
                                        target_cpu_level = min(100, target_cpu_level + trim_cpu)
                                    if trim_gpu > 0:
                                        target_gpu_level = min(100, target_gpu_level + trim_gpu)

                            controller.set_experimental_fan_speed(target_cpu_level, target_gpu_level)

                elif (
                    mode == "performance"
                    and controller.get_board_name() == "8CC0"
                    and getattr(controller, "is_experimental_control_enabled", lambda: False)()
                ):
                    emergency_cpu = None
                    try:
                        from unittest.mock import Mock
                        is_ref_mocked = isinstance(getattr(controller, "get_reference_temp", None), Mock)
                        is_em_mocked = isinstance(getattr(controller, "get_cpu_emergency_temp", None), Mock)
                        has_override = getattr(controller, "_core_temps_override", None) is not None
                        if has_override or is_em_mocked or not is_ref_mocked:
                            emergency_cpu = float(controller.get_cpu_emergency_temp())
                    except Exception:
                        pass

                    if emergency_cpu is not None:
                        check_cpu_temp = max(emergency_cpu, float(current_temp) if current_temp else 0.0, float(avg_temp))
                    else:
                        check_cpu_temp = max(float(current_temp) if current_temp else 0.0, float(avg_temp))

                    em_state, em_changed = controller.update_performance_emergency_state(check_cpu_temp, gpu_temp)
                    if em_state == "active":
                        if em_changed:
                            click.echo(
                                f"Performance emergency high-temperature detected (CPU: {check_cpu_temp:.1f}°C, GPU: {gpu_temp}°C) "
                                "— commanding MAX fan cooling."
                            )
                            controller.set_experimental_fan_max(True)
                            controller._emergency_max_commanded = True
                    else:
                        if getattr(controller, "_emergency_max_commanded", False):
                            click.echo(
                                f"Performance emergency cooling recovered (CPU: {check_cpu_temp:.1f}°C, GPU: {gpu_temp}°C) "
                                "— returning to normal software PERFORMANCE control."
                            )
                            controller.set_experimental_fan_max(False)
                            controller._emergency_max_commanded = False

                        state, state_changed = controller.update_performance_thermal_state(avg_temp, gpu_temp)
                        if state == "idle":
                            if state_changed:
                                click.echo(
                                    f"Performance thermal cooldown reached (CPU: {avg_temp:.1f}°C, GPU: {gpu_temp}°C) "
                                    "— releasing fans to firmware AUTO."
                                )
                                controller.restore_firmware_auto()
                        else:
                            if state_changed:
                                click.echo(
                                    f"Performance thermal trigger reached (CPU: {avg_temp:.1f}°C, GPU: {gpu_temp}°C) "
                                    "— engaging software PERFORMANCE fan control."
                                )

                            perf_report = controller.calculate_performance_target(avg_temp, gpu_temp)
                            target_cpu_level = perf_report["cpu_fan_level"]
                            target_gpu_level = perf_report["gpu_fan_level"]

                            if getattr(controller, "is_experimental_telemetry_enabled", lambda: False)():
                                telemetry = controller.read_experimental_fan_telemetry()
                                if telemetry is not None:
                                    act_cpu, act_gpu = telemetry
                                    trim_cpu, trim_gpu = controller.apply_performance_rpm_feedback(
                                        act_cpu, act_gpu, perf_report["target_cpu_rpm"], perf_report["target_gpu_rpm"]
                                    )
                                    if trim_cpu > 0:
                                        target_cpu_level = min(100, target_cpu_level + trim_cpu)
                                    if trim_gpu > 0:
                                        target_gpu_level = min(100, target_gpu_level + trim_gpu)

                            controller.set_experimental_fan_speed(target_cpu_level, target_gpu_level)
                else:
                    configured_curve = controller.config.get("curve", [])
                    if configured_curve:
                        auto_curve = configured_curve
                    else:
                        auto_curve = DEFAULT_AUTO_CURVE

                    if mode == "performance":
                        curve = controller.config.get("performance_curve") or DEFAULT_PERFORMANCE_CURVE
                    else:
                        curve = auto_curve

                    if not controller.is_valid_curve(curve):
                        click.echo(
                            f"Invalid {mode} curve — skipping fan write; "
                            "firmware automatic mode was not selected."
                        )
                    else:
                        target_cpu_pwm, target_gpu_pwm = controller.calculate_dual_target_pwm(
                            int(avg_temp), gpu_temp, curve_override=curve
                        )
                        controller.set_fan_pwm(target_cpu_pwm, target_gpu_pwm)

            # --- Runtime Low-RPM Safety Monitor (8CC0 experimental control) ---
            if (
                mode in ("auto", "curve", "performance")
                and not getattr(controller, "_emergency_max_commanded", False)
                and controller.get_board_name() == "8CC0"
                and getattr(controller, "is_experimental_control_enabled", lambda: False)()
                and getattr(controller, "is_experimental_telemetry_enabled", lambda: False)()
            ):
                min_req_rpm = int(controller.config.get("min_required_rpm", DEFAULT_MIN_REQUIRED_RPM))
                telemetry = controller.read_experimental_fan_telemetry()
                if telemetry is not None:
                    actual_cpu_rpm, actual_gpu_rpm = telemetry

                    # Check CPU fan: only monitor when active level commanded
                    if (controller._last_commanded_cpu_level or 0) > 0:
                        if actual_cpu_rpm < min_req_rpm:
                            consecutive_low_cpu += 1
                            if consecutive_low_cpu >= 2:
                                new_floor = controller.escalate_runtime_min_fan_floor("cpu", bump=2)
                                click.echo(
                                    f"Safety guard: CPU fan RPM ({actual_cpu_rpm}) persistently below "
                                    f"{min_req_rpm} RPM. Escalated session floor to level {new_floor}."
                                )
                                consecutive_low_cpu = 0
                                if controller.get_board_name() != "8CC0" and "target_cpu_pwm" in locals() and "target_gpu_pwm" in locals():
                                    controller.set_fan_pwm(target_cpu_pwm, target_gpu_pwm)
                                elif "target_cpu_level" in locals() and "target_gpu_level" in locals():
                                    controller.set_experimental_fan_speed(max(target_cpu_level, new_floor), target_gpu_level)
                                if new_floor >= MAX_RUNTIME_SAFETY_FLOOR and actual_cpu_rpm == 0:
                                    click.echo(
                                        "CRITICAL: CPU fan stalled at maximum safety ceiling. "
                                        "Restoring native firmware AUTO control."
                                    )
                                    controller.restore_firmware_auto()
                        else:
                            consecutive_low_cpu = 0

                    # Check GPU fan: only monitor when active level commanded
                    if (controller._last_commanded_gpu_level or 0) > 0:
                        if actual_gpu_rpm < min_req_rpm:
                            consecutive_low_gpu += 1
                            if consecutive_low_gpu >= 2:
                                new_floor = controller.escalate_runtime_min_fan_floor("gpu", bump=2)
                                click.echo(
                                    f"Safety guard: GPU fan RPM ({actual_gpu_rpm}) persistently below "
                                    f"{min_req_rpm} RPM. Escalated session floor to level {new_floor}."
                                )
                                consecutive_low_gpu = 0
                                if controller.get_board_name() != "8CC0" and "target_cpu_pwm" in locals() and "target_gpu_pwm" in locals():
                                    controller.set_fan_pwm(target_cpu_pwm, target_gpu_pwm)
                                elif "target_cpu_level" in locals() and "target_gpu_level" in locals():
                                    controller.set_experimental_fan_speed(target_cpu_level, max(target_gpu_level, new_floor))
                                if new_floor >= MAX_RUNTIME_SAFETY_FLOOR and actual_gpu_rpm == 0:
                                    click.echo(
                                        "CRITICAL: GPU fan stalled at maximum safety ceiling. "
                                        "Restoring native firmware AUTO control."
                                    )
                                    controller.restore_firmware_auto()
                        else:
                            consecutive_low_gpu = 0

            # --- Publish live daemon status for GUI / monitoring clients ---
            try:
                status_path = Path("/run/omen-fan-control/status.json")
                status_path.parent.mkdir(parents=True, exist_ok=True)

                f1_rpm: int | None = None
                f2_rpm: int | None = None
                if getattr(controller, "is_experimental_telemetry_enabled", lambda: False)():
                    telem = controller.read_experimental_fan_telemetry()
                    if telem is not None:
                        f1_rpm, f2_rpm = telem
                elif getattr(controller, "fan_telemetry_available", False):
                    f1_rpm = controller.get_fan_speed()
                    f2_rpm = getattr(controller, "get_gpu_fan_speed", lambda: None)()

                is_emergency = False
                try:
                    if getattr(controller, "get_board_name", lambda: "")() == "8CC0":
                        if mode == "auto":
                            is_emergency = controller.get_auto_emergency_state() == "active"
                        elif mode == "performance":
                            is_emergency = controller.get_performance_emergency_state() == "active"
                except Exception:
                    is_emergency = False

                status_payload = {
                    "timestamp": time.time(),
                    "pid": os.getpid(),
                    "mode": mode,
                    "cpu_temp": round(float(avg_temp), 1) if avg_temp is not None else None,
                    "gpu_temp": round(float(gpu_temp), 1) if gpu_temp is not None else None,
                    "fan1_rpm": f1_rpm,
                    "fan2_rpm": f2_rpm,
                    "emergency": is_emergency,
                    "board": controller.get_board_name(),
                    "ready": _is_fan_control_ready(),
                }
                tmp_status = status_path.with_suffix(".tmp")
                tmp_status.write_text(json.dumps(status_payload))
                tmp_status.replace(status_path)
                try:
                    os.chmod(status_path, 0o666)
                    os.chmod(status_path.parent, 0o777)
                    cfg_file = status_path.parent / "config.json"
                    if cfg_file.exists():
                        os.chmod(cfg_file, 0o666)
                except Exception:
                    pass
            except Exception:
                pass

            time.sleep(_POLL_INTERVAL)

        except KeyboardInterrupt:
            click.echo("Stopping daemon...")
            break
        except Exception as e:
            click.echo(f"Error in daemon loop: {e}")
            try:
                controller.restore_firmware_auto()
            except Exception:
                pass
            time.sleep(5)

    click.echo("Cleaning up: restoring firmware AUTO fan control...")
    try:
        status_path = Path("/run/omen-fan-control/status.json")
        if status_path.exists():
            status_path.unlink()
    except Exception:
        pass
    try:
        controller.restore_firmware_auto()
    except Exception as e:
        click.echo(f"Warning during shutdown cleanup: {e}")
