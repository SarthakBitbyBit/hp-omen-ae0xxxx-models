from __future__ import annotations

from typing import Any

import click

from .._constants import DEFAULT_MIN_REQUIRED_RPM
from . import get_controller


def run_min_calibration(
    controller: Any,
    min_rpm: int = DEFAULT_MIN_REQUIRED_RPM,
    settle_time: float = 6.0,
    sample_count: int = 5,
    sample_interval: float = 1.2,
) -> None:
    """Execute minimum stable fan level calibration and display detailed progress."""
    click.echo("=======================================================")
    click.echo("  HP OMEN 16 (8CC0) Minimum Fan Level Calibration")
    click.echo("=======================================================")
    click.echo("Testing candidate fan levels sequentially to detect physical pulsing/stalling.")
    click.echo(f"Required minimum sustained RPM threshold: {min_rpm} RPM.")
    click.echo(f"Settling time: {settle_time}s | Samples per step: {sample_count} (every {sample_interval}s).\n")

    def _progress(step: dict[str, Any]) -> None:
        cand = step["candidate"]
        cpu = step["cpu"]
        gpu = step["gpu"]
        click.echo(f"Candidate Level: {cand}% (CPU cmd: {step['cmd_cpu']}%, GPU cmd: {step['cmd_gpu']}%)")

        c_status = cpu.get("status", "N/A")
        c_samples = cpu.get("samples", [])
        c_min = cpu.get("min", 0)
        c_max = cpu.get("max", 0)
        c_avg = cpu.get("avg", 0.0)
        c_med = cpu.get("median", 0)
        c_var = cpu.get("variance", 0.0)
        click.echo(
            f"  • CPU Fan: {c_status} | Samples: {c_samples} RPM | "
            f"Min: {c_min}, Max: {c_max}, Avg: {c_avg:.1f}, Med: {c_med}, Var: {c_var:.1f}"
        )

        g_status = gpu.get("status", "N/A")
        g_samples = gpu.get("samples", [])
        g_min = gpu.get("min", 0)
        g_max = gpu.get("max", 0)
        g_avg = gpu.get("avg", 0.0)
        g_med = gpu.get("median", 0)
        g_var = gpu.get("variance", 0.0)
        click.echo(
            f"  • GPU Fan: {g_status} | Samples: {g_samples} RPM | "
            f"Min: {g_min}, Max: {g_max}, Avg: {g_avg:.1f}, Med: {g_med}, Var: {g_var:.1f}\n"
        )

    res = controller.calibrate_minimum_fan_levels(
        min_required_rpm=min_rpm,
        settle_time=settle_time,
        sample_count=sample_count,
        sample_interval=sample_interval,
        progress_callback=_progress,
    )

    if res.get("success"):
        click.echo("Calibration SUCCEEDED:")
        click.echo(f"  • Calibrated CPU Minimum Level: {res['calibrated_min_cpu_level']}% (~{res['calibrated_min_cpu_rpm']} RPM)")
        click.echo(f"  • Calibrated GPU Minimum Level: {res['calibrated_min_gpu_level']}% (~{res['calibrated_min_gpu_rpm']} RPM)")
        click.echo("Saved to persistent configuration. Native firmware AUTO restored.")
    else:
        click.echo(f"Calibration FAILED: {res.get('error', 'Unknown error')}")
        click.echo("Restored native firmware AUTO fan control. Unstable levels were not persisted.")


@click.command("calibrate")
@click.option("--min-levels", "--min", "min_levels", is_flag=True, help="Calibrate minimum stable fan levels to prevent pulsing.")
@click.option("--min-rpm", default=DEFAULT_MIN_REQUIRED_RPM, type=int, help="Minimum sustained RPM threshold (default: 500).")
@click.option("--settle-time", default=6.0, type=float, help="Settling duration in seconds per candidate level.")
@click.option("--sample-count", default=5, type=int, help="Number of RPM samples to collect per candidate level.")
@click.option("--sample-interval", default=1.2, type=float, help="Interval between samples in seconds.")
@click.option("--experimental-control/--no-experimental-control", default=None, help="Explicitly enable/disable experimental fan control.")
@click.option("--experimental-telemetry/--no-experimental-telemetry", default=None, help="Explicitly enable/disable experimental telemetry.")
def calibrate(
    min_levels: bool,
    min_rpm: int,
    settle_time: float,
    sample_count: int,
    sample_interval: float,
    experimental_control: bool | None = None,
    experimental_telemetry: bool | None = None,
) -> None:
    """Run fan calibration to determine max RPM or minimum stable fan levels."""
    controller = get_controller()
    if experimental_control is not None:
        controller.config["enable_experimental_control"] = experimental_control
    if experimental_telemetry is not None:
        controller.config["enable_experimental_telemetry"] = experimental_telemetry

    if min_levels:
        run_min_calibration(
            controller,
            min_rpm=min_rpm,
            settle_time=settle_time,
            sample_count=sample_count,
            sample_interval=sample_interval,
        )
        return

    if not controller.fan_control_available:
        click.echo("Error: Fan control interface is not available.")
        return

    if click.confirm("This will spin fans at max speed for calibration. Continue?"):
        prev_mode = controller.config.get("mode", "auto")
        controller.config["mode"] = "calibration"
        controller.save_config()

        try:
            gen = controller.calibrate()
            max_rpm = 0
            try:
                while True:
                    progress = next(gen)
                    click.echo(f"Calibrating... {progress}%", nl=False)
                    click.echo("\r", nl=False)
            except StopIteration as e:
                result = e.value

            if isinstance(result, (tuple, list)):
                rpm1, rpm2 = result
                click.echo(f"\nCalibration finished.\n• Fan 1 (CPU) Max: {rpm1} RPM\n• Fan 2 (GPU) Max: {rpm2} RPM")
            else:
                click.echo(f"\nCalibration finished. Max RPM: {result}")

        finally:
            controller.config["mode"] = prev_mode
            controller.save_config()


@click.command("calibrate-min")
@click.option("--min-rpm", default=DEFAULT_MIN_REQUIRED_RPM, type=int, help="Minimum sustained RPM threshold (default: 500).")
@click.option("--settle-time", default=6.0, type=float, help="Settling duration in seconds per candidate level.")
@click.option("--sample-count", default=5, type=int, help="Number of RPM samples to collect per candidate level.")
@click.option("--sample-interval", default=1.2, type=float, help="Interval between samples in seconds.")
@click.option("--experimental-control/--no-experimental-control", default=None, help="Explicitly enable/disable experimental fan control.")
@click.option("--experimental-telemetry/--no-experimental-telemetry", default=None, help="Explicitly enable/disable experimental telemetry.")
def calibrate_min(
    min_rpm: int,
    settle_time: float,
    sample_count: int,
    sample_interval: float,
    experimental_control: bool | None = None,
    experimental_telemetry: bool | None = None,
) -> None:
    """Calibrate lowest stable software-controlled fan levels on board 8CC0."""
    controller = get_controller()
    if experimental_control is not None:
        controller.config["enable_experimental_control"] = experimental_control
    if experimental_telemetry is not None:
        controller.config["enable_experimental_telemetry"] = experimental_telemetry
    run_min_calibration(
        controller,
        min_rpm=min_rpm,
        settle_time=settle_time,
        sample_count=sample_count,
        sample_interval=sample_interval,
    )
