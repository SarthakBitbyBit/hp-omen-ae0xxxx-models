from __future__ import annotations

import threading
import time

import click

from .._constants import (
    DEFAULT_AUTO_CURVE,
    DEFAULT_PERFORMANCE_CURVE,
    VOLATILE_CONFIG_FILE,
)
from . import get_controller

# Bounded exponential backoff when the hwmon interface is absent (seconds).
_REPROBE_INTERVALS: tuple[int, ...] = (30, 60, 120, 300)
_POLL_INTERVAL = 5


@click.command()
def serve() -> None:
    """Run the fan control daemon (foreground). Used by systemd service."""
    controller = get_controller()
    click.echo("Starting Omen Fan Control Daemon...")

    # Reverse-mode recovery at startup — independent of hp-wmi PWM interface.
    try:
        if controller.is_reverse_mode_active():
            click.echo("Recovery: Reverse mode detected on daemon startup. Initiating stop sequence...")
            controller.stop_fan_cleaning()
    except Exception as e:
        click.echo(f"Startup reverse mode check failed: {e}")

    if not controller.fan_control_available:
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

    while True:
        try:
            # --- Config reload (on-disk change detection) ---
            try:
                current_mtime = controller.config_path.stat().st_mtime
                current_v_mtime = 0.0
                if VOLATILE_CONFIG_FILE.exists():
                    current_v_mtime = VOLATILE_CONFIG_FILE.stat().st_mtime
                if current_mtime > last_config_mtime or current_v_mtime > last_volatile_mtime:
                    controller.config = controller.load_config()
                    last_config_mtime = controller.config_path.stat().st_mtime if controller.config_path.exists() else 0
                    last_volatile_mtime = VOLATILE_CONFIG_FILE.stat().st_mtime if VOLATILE_CONFIG_FILE.exists() else 0
                    print("Configuration reloaded from disk.")
            except Exception:
                pass

            mode = controller.config.get("mode", "auto")
            if mode == "calibration":
                time.sleep(1)
                continue

            if time.time() - last_watchdog_time > watchdog_interval:
                last_watchdog_time = time.time()

            # --- Temperature monitoring: always runs, independent of fan-control availability ---
            current_temp = controller.get_reference_temp()

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
            if not controller.fan_control_available:
                now = time.time()
                reprobe_interval = _REPROBE_INTERVALS[reprobe_backoff_index]
                if now - last_reprobe_time >= reprobe_interval:
                    controller._find_paths()
                    last_reprobe_time = now
                    if controller.fan_control_available:
                        reprobe_backoff_index = 0
                    else:
                        reprobe_backoff_index = min(
                            reprobe_backoff_index + 1, len(_REPROBE_INTERVALS) - 1
                        )

            if not controller.fan_control_available:
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

                if should_apply:
                    controller.set_fan_pwm(target_cpu_pwm, target_gpu_pwm)
                    hysteresis_start_time = None

            elif mode == "manual":
                cpu_pwm, gpu_pwm = controller.get_dual_manual_pwm()
                controller.set_fan_pwm(cpu_pwm, gpu_pwm)

            elif mode == "max":
                controller.set_fan_mode("max")

            elif mode in ("auto", "performance"):
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

            time.sleep(_POLL_INTERVAL)

        except KeyboardInterrupt:
            click.echo("Stopping daemon...")
            break
        except Exception as e:
            click.echo(f"Error in daemon loop: {e}")
            time.sleep(5)
