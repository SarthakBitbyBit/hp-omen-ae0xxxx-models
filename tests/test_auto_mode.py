from __future__ import annotations

import json
import math
import time
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

import pytest
from click.testing import CliRunner

from omen_fan_control._cli.serve import serve
from omen_fan_control._constants import (
    AUTO_ACTIVATION_CPU_TEMP,
    AUTO_ACTIVATION_GPU_TEMP,
    AUTO_BREAKPOINTS_TABLE,
    AUTO_COOLDOWN_CPU_TEMP,
    AUTO_COOLDOWN_GPU_TEMP,
    AUTO_CURVE_SOURCE_DERIVED,
    AUTO_CURVE_SOURCE_ESTIMATED,
    AUTO_CURVE_SOURCE_OBSERVED,
    AUTO_EMERGENCY_CONFIRMATION_SEC,
    AUTO_EMERGENCY_RECOVERY_TEMP,
    AUTO_EMERGENCY_TEMP_THRESHOLD,
    AUTO_MAX_FAN_LEVEL_CPU,
    AUTO_MAX_FAN_LEVEL_GPU,
    AUTO_STAGNATION_ACTIVATION_TEMP,
    AUTO_STAGNATION_INITIAL_BOOST_LEVEL,
    AUTO_STAGNATION_MIN_IMPROVEMENT_TEMP,
    AUTO_STAGNATION_RECOVERY_TEMP,
    AUTO_STAGNATION_STEP_BOOST_LEVEL,
    AUTO_STAGNATION_STEP_INTERVAL_SEC,
    AUTO_STAGNATION_WINDOW_SEC,
    EXPERIMENTAL_WATCHDOG_REFRESH_INTERVAL,
    FIRMWARE_AUTO_FAN_LEVEL,
    WMI_CMD_GM,
    WMI_QUERY_FAN_COUNT,
    WMI_QUERY_FAN_SPEED_MAX_SET,
    WMI_QUERY_FAN_SPEED_SET,
)
from omen_fan_control.fan_controller import FanController


class BaseAutoTest:
    def setup_method(self) -> None:
        self.tmp_dir = Path("/tmp/test_omen_auto")
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self.cfg_path = self.tmp_dir / "config.json"
        self.cfg_path.write_text(
            json.dumps(
                {
                    "mode": "auto",
                    "ma_window": 1,
                    "enable_experimental_control": True,
                    "enable_experimental_telemetry": True,
                    "calibrated_min_cpu_level": 15,
                    "calibrated_min_gpu_level": 15,
                }
            )
        )

    def _make_ctrl(self, board: str = "8CC0", exp_ctl: bool = True, exp_telem: bool = True) -> FanController:
        with patch.object(FanController, "_get_live_board_name", return_value=board):
            ctrl = FanController(config_path=self.cfg_path)
            ctrl.config["enable_experimental_control"] = exp_ctl
            ctrl.config["enable_experimental_telemetry"] = exp_telem
            return ctrl


class TestAutoCurveBreakpoints(BaseAutoTest):
    """TASK 2: Explicit 5°C thermal breakpoints and interpolation."""

    def test_explicit_breakpoints_table_structure(self) -> None:
        ctrl = self._make_ctrl()
        table = ctrl.get_auto_curve_breakpoints()
        assert len(table) == 5

        expected = [
            (65.0, 1800, 1800, 18, 18, AUTO_CURVE_SOURCE_ESTIMATED, AUTO_CURVE_SOURCE_ESTIMATED),
            (70.0, 2600, 2700, 26, 27, AUTO_CURVE_SOURCE_DERIVED, AUTO_CURVE_SOURCE_DERIVED),
            (75.0, 3700, 3900, 37, 39, AUTO_CURVE_SOURCE_DERIVED, AUTO_CURVE_SOURCE_DERIVED),
            (80.0, 4700, 5000, 47, 50, AUTO_CURVE_SOURCE_DERIVED, AUTO_CURVE_SOURCE_DERIVED),
            (85.0, 5700, 6100, 57, 61, AUTO_CURVE_SOURCE_OBSERVED, AUTO_CURVE_SOURCE_OBSERVED),
        ]

        for i, (temp, c_rpm, g_rpm, c_lvl, g_lvl, s_cpu, s_gpu) in enumerate(expected):
            bp = table[i]
            assert bp["temp"] == temp
            assert bp["cpu_rpm"] == c_rpm
            assert bp["gpu_rpm"] == g_rpm
            assert bp["cpu_level"] == c_lvl
            assert bp["gpu_level"] == g_lvl
            assert bp["source_cpu"] == s_cpu
            assert bp["source_gpu"] == s_gpu

    def test_monotonic_progression(self) -> None:
        ctrl = self._make_ctrl()
        table = ctrl.get_auto_curve_breakpoints()
        for i in range(len(table) - 1):
            assert table[i]["temp"] < table[i + 1]["temp"]
            assert table[i]["cpu_rpm"] < table[i + 1]["cpu_rpm"]
            assert table[i]["gpu_rpm"] < table[i + 1]["gpu_rpm"]
            assert table[i]["cpu_level"] < table[i + 1]["cpu_level"]
            assert table[i]["gpu_level"] < table[i + 1]["gpu_level"]

    def test_exact_breakpoints(self) -> None:
        ctrl = self._make_ctrl()
        assert ctrl.interpolate_auto_curve(65.0) == (1800, 1800, "65°C")
        assert ctrl.interpolate_auto_curve(70.0) == (2600, 2700, "70°C")
        assert ctrl.interpolate_auto_curve(75.0) == (3700, 3900, "75°C")
        assert ctrl.interpolate_auto_curve(80.0) == (4700, 5000, "80°C")
        assert ctrl.interpolate_auto_curve(85.0) == (5700, 6100, "85°C")

    def test_interpolation_between_65_and_70(self) -> None:
        ctrl = self._make_ctrl()
        cpu_rpm, gpu_rpm, interval = ctrl.interpolate_auto_curve(67.5)
        assert cpu_rpm == 2200
        assert gpu_rpm == 2250
        assert interval == "65°C–70°C"

    def test_interpolation_between_70_and_75(self) -> None:
        ctrl = self._make_ctrl()
        cpu_rpm, gpu_rpm, interval = ctrl.interpolate_auto_curve(72.0)
        assert cpu_rpm == 3040
        assert gpu_rpm == 3180
        assert interval == "70°C–75°C"

    def test_interpolation_between_75_and_80(self) -> None:
        ctrl = self._make_ctrl()
        cpu_rpm, gpu_rpm, interval = ctrl.interpolate_auto_curve(77.5)
        assert cpu_rpm == 4200
        assert gpu_rpm == 4450
        assert interval == "75°C–80°C"

    def test_interpolation_between_80_and_85(self) -> None:
        ctrl = self._make_ctrl()
        cpu_rpm, gpu_rpm, interval = ctrl.interpolate_auto_curve(82.5)
        assert cpu_rpm == 5200
        assert gpu_rpm == 5550
        assert interval == "80°C–85°C"

    def test_boundary_clamps(self) -> None:
        ctrl = self._make_ctrl()
        # Below 65°C: clamps to 65°C breakpoint (1800 RPM)
        assert ctrl.interpolate_auto_curve(60.0) == (1800, 1800, "< 65°C")
        assert ctrl.interpolate_auto_curve(45.0) == (1800, 1800, "< 65°C")
        # Above 85°C: clamps to 85°C breakpoint (5700 / 6100 RPM)
        assert ctrl.interpolate_auto_curve(90.0) == (5700, 6100, "> 85°C")
        assert ctrl.interpolate_auto_curve(100.0) == (5700, 6100, "> 85°C")

    def test_rpm_to_fan_level_mapping(self) -> None:
        ctrl = self._make_ctrl()
        assert ctrl.rpm_to_fan_level(1600) == 16
        assert ctrl.rpm_to_fan_level(1800) == 18
        assert ctrl.rpm_to_fan_level(1849) == 18
        assert ctrl.rpm_to_fan_level(1850) == 19
        assert ctrl.rpm_to_fan_level(2600) == 26
        assert ctrl.rpm_to_fan_level(3700) == 37
        assert ctrl.rpm_to_fan_level(4700) == 47
        assert ctrl.rpm_to_fan_level(5700) == 57
        assert ctrl.rpm_to_fan_level(6100) == 61
        assert ctrl.rpm_to_fan_level(0) == 0
        with pytest.raises(TypeError):
            ctrl.rpm_to_fan_level("invalid")  # type: ignore


class TestAutoThermalActivationAndHysteresis(BaseAutoTest):
    """TASK 3 & TASK 4: Thermal state machine, OR-activation, and AND-cooldown."""

    def test_idle_when_both_below_activation(self) -> None:
        ctrl = self._make_ctrl()
        state, changed = ctrl.update_auto_thermal_state(cpu_temp=50.0, gpu_temp=40.0)
        assert state == "idle"
        assert not changed
        assert ctrl.get_auto_thermal_state() == "idle"

    def test_idle_just_below_activation_threshold(self) -> None:
        ctrl = self._make_ctrl()
        state, changed = ctrl.update_auto_thermal_state(cpu_temp=64.9, gpu_temp=54.9)
        assert state == "idle"
        assert not changed

    def test_activation_cpu_only_trigger(self) -> None:
        ctrl = self._make_ctrl()
        state, changed = ctrl.update_auto_thermal_state(cpu_temp=65.0, gpu_temp=40.0)
        assert state == "active"
        assert changed
        assert ctrl.get_auto_thermal_state() == "active"

    def test_activation_gpu_only_trigger(self) -> None:
        ctrl = self._make_ctrl()
        state, changed = ctrl.update_auto_thermal_state(cpu_temp=50.0, gpu_temp=55.0)
        assert state == "active"
        assert changed
        assert ctrl.get_auto_thermal_state() == "active"

    def test_activation_both_trigger(self) -> None:
        ctrl = self._make_ctrl()
        state, changed = ctrl.update_auto_thermal_state(cpu_temp=70.0, gpu_temp=60.0)
        assert state == "active"
        assert changed

    def test_activation_without_gpu_telemetry(self) -> None:
        ctrl = self._make_ctrl()
        # GPU is None / 0 -> triggers on CPU alone
        state, changed = ctrl.update_auto_thermal_state(cpu_temp=65.0, gpu_temp=None)
        assert state == "active"
        assert changed

    def test_cooldown_requires_both_below(self) -> None:
        ctrl = self._make_ctrl()
        # Trigger active first
        ctrl.update_auto_thermal_state(cpu_temp=70.0, gpu_temp=60.0)
        assert ctrl.get_auto_thermal_state() == "active"

        # CPU cools to 58°C, but GPU is still 52°C (>49°C) -> stays ACTIVE
        state, changed = ctrl.update_auto_thermal_state(cpu_temp=58.0, gpu_temp=52.0)
        assert state == "active"
        assert not changed

        # GPU cools to 45°C, but CPU is 62°C (>59°C) -> stays ACTIVE
        state, changed = ctrl.update_auto_thermal_state(cpu_temp=62.0, gpu_temp=45.0)
        assert state == "active"
        assert not changed

        # Both cool: CPU <= 59.0 AND GPU <= 49.0 -> transitions to IDLE
        state, changed = ctrl.update_auto_thermal_state(cpu_temp=59.0, gpu_temp=49.0)
        assert state == "idle"
        assert changed
        assert ctrl.get_auto_thermal_state() == "idle"

    def test_cooldown_without_gpu_telemetry(self) -> None:
        ctrl = self._make_ctrl()
        ctrl.update_auto_thermal_state(cpu_temp=70.0, gpu_temp=None)
        assert ctrl.get_auto_thermal_state() == "active"

        # Cooldown when GPU is None: CPU <= 59.0 triggers idle
        state, changed = ctrl.update_auto_thermal_state(cpu_temp=59.0, gpu_temp=None)
        assert state == "idle"
        assert changed

    def test_no_fan_chatter_in_hysteresis_band(self) -> None:
        ctrl = self._make_ctrl()
        # Starting in IDLE: 62°C / 52°C does NOT trigger active
        state, changed = ctrl.update_auto_thermal_state(cpu_temp=62.0, gpu_temp=52.0)
        assert state == "idle"
        assert not changed

        # Escalate to trigger active
        ctrl.update_auto_thermal_state(cpu_temp=66.0, gpu_temp=50.0)
        assert ctrl.get_auto_thermal_state() == "active"

        # Cool down into hysteresis band (62°C / 52°C): remains ACTIVE!
        state, changed = ctrl.update_auto_thermal_state(cpu_temp=62.0, gpu_temp=52.0)
        assert state == "active"
        assert not changed

        # Drop slightly more (60°C / 50°C): still in hysteresis band, remains ACTIVE!
        state, changed = ctrl.update_auto_thermal_state(cpu_temp=60.0, gpu_temp=50.0)
        assert state == "active"
        assert not changed


class TestAutoTargetCalculationAndReporting(BaseAutoTest):
    """TASK 2, 4, 5: Comprehensive reporting and level calculation."""

    def test_reporting_fields_in_idle(self) -> None:
        ctrl = self._make_ctrl()
        ctrl.reset_auto_state()
        report = ctrl.calculate_auto_target(cpu_temp=50.0, gpu_temp=40.0)

        assert report["current_temp"] == 50.0
        assert report["active_interval"] == "< 65°C"
        assert report["target_cpu_rpm"] == 0
        assert report["target_gpu_rpm"] == 0
        assert report["cpu_fan_level"] == FIRMWARE_AUTO_FAN_LEVEL
        assert report["gpu_fan_level"] == FIRMWARE_AUTO_FAN_LEVEL
        assert report["thermal_state"] == "idle"

        # Values below 65°C / 55°C still result in 0 RPM while idle
        report_near = ctrl.calculate_auto_target(cpu_temp=64.9, gpu_temp=54.9)
        assert report_near["target_cpu_rpm"] == 0
        assert report_near["target_gpu_rpm"] == 0
        assert report_near["cpu_fan_level"] == FIRMWARE_AUTO_FAN_LEVEL
        assert report_near["gpu_fan_level"] == FIRMWARE_AUTO_FAN_LEVEL
        assert report_near["thermal_state"] == "idle"

    def test_reporting_fields_in_active(self) -> None:
        ctrl = self._make_ctrl()
        ctrl.update_auto_thermal_state(cpu_temp=70.0, gpu_temp=50.0)

        report = ctrl.calculate_auto_target(cpu_temp=70.0, gpu_temp=50.0)
        assert report["current_temp"] == 70.0
        assert report["active_interval"] == "70°C"
        assert report["target_cpu_rpm"] == 2600
        assert report["target_gpu_rpm"] == 2700
        assert report["cpu_fan_level"] == 26
        assert report["gpu_fan_level"] == 27
        assert report["thermal_state"] == "active"

    def test_safety_floor_enforcement_in_active(self) -> None:
        ctrl = self._make_ctrl()
        ctrl.config["calibrated_min_cpu_level"] = 20
        ctrl.config["calibrated_min_gpu_level"] = 20
        ctrl.update_auto_thermal_state(cpu_temp=65.0, gpu_temp=40.0)

        # At 65°C, raw level would be 18, but calibrated floor is 20 -> clamped to 20
        report = ctrl.calculate_auto_target(cpu_temp=65.0, gpu_temp=40.0)
        assert report["target_cpu_rpm"] == 1800
        assert report["cpu_fan_level"] == 20
        assert report["gpu_fan_level"] == 20

    def test_active_floor_minimum_1800_rpm_when_calibrated_floor_is_lower(self) -> None:
        ctrl = self._make_ctrl()
        # Even if calibrated floor was as low as 10 (1000 RPM), the active product floor is 18 (1800 RPM)
        ctrl.config["calibrated_min_cpu_level"] = 10
        ctrl.config["calibrated_min_gpu_level"] = 10
        ctrl.update_auto_thermal_state(cpu_temp=65.0, gpu_temp=40.0)

        report = ctrl.calculate_auto_target(cpu_temp=65.0, gpu_temp=40.0)
        assert report["target_cpu_rpm"] == 1800
        assert report["target_gpu_rpm"] == 1800
        assert report["cpu_fan_level"] == 18
        assert report["gpu_fan_level"] == 18

    def test_calibrated_minimum_below_1800_rpm_does_not_lower_product_floor(self) -> None:
        ctrl = self._make_ctrl()
        # Hardware calibration stored: CPU level 12 (1180 RPM), GPU level 14 (1400 RPM)
        ctrl.config["calibrated_min_cpu_level"] = 12
        ctrl.config["calibrated_min_gpu_level"] = 14
        ctrl.update_auto_thermal_state(cpu_temp=65.0, gpu_temp=40.0)

        report = ctrl.calculate_auto_target(cpu_temp=65.0, gpu_temp=40.0)
        assert report["target_cpu_rpm"] == 1800
        assert report["target_gpu_rpm"] == 1800
        assert report["cpu_fan_level"] == 18
        assert report["gpu_fan_level"] == 18

    def test_uncapped_progressive_curve_reaches_hardware_maximum(self) -> None:
        ctrl = self._make_ctrl()
        ctrl.update_auto_thermal_state(cpu_temp=85.0, gpu_temp=60.0)

        report = ctrl.calculate_auto_target(cpu_temp=85.0, gpu_temp=60.0)
        # At 85°C, CPU reaches 5700 RPM and GPU reaches 6100 RPM
        assert report["target_cpu_rpm"] == 5700
        assert report["target_gpu_rpm"] == 6100
        assert report["cpu_fan_level"] == 57
        assert report["gpu_fan_level"] == 61

        # Above 85°C (e.g. 90°C), fans remain at maximum capability
        report_over = ctrl.calculate_auto_target(cpu_temp=90.0, gpu_temp=60.0)
        assert report_over["target_cpu_rpm"] == 5700
        assert report_over["target_gpu_rpm"] == 6100
        assert report_over["cpu_fan_level"] == 57
        assert report_over["gpu_fan_level"] == 61

    def test_reporting_with_live_telemetry(self) -> None:
        ctrl = self._make_ctrl()
        ctrl.update_auto_thermal_state(cpu_temp=75.0, gpu_temp=60.0)

        with patch.object(ctrl, "read_experimental_fan_telemetry", return_value=(3680, 3920)):
            report = ctrl.calculate_auto_target(cpu_temp=75.0, gpu_temp=60.0)
            assert report["actual_cpu_rpm"] == 3680
            assert report["actual_gpu_rpm"] == 3920

    def test_independent_fans_calculation(self) -> None:
        ctrl = self._make_ctrl()
        ctrl.update_auto_thermal_state(cpu_temp=85.0, gpu_temp=55.0)

        report = ctrl.calculate_auto_target(cpu_temp=85.0, gpu_temp=55.0, independent_fans=True)
        # CPU is at 85°C -> 5700 RPM
        assert report["target_cpu_rpm"] == 5700
        # GPU is at 55°C (normalized = 65°C) -> 1800 RPM
        assert report["target_gpu_rpm"] == 1800


class TestAutoFanControlSequenceAndRouting(BaseAutoTest):
    """TASK 5 & 8: 0x2E packet generation, WMI sequence, and duplicate suppression."""

    def test_8cc0_control_sequence_via_executor(self) -> None:
        ctrl = self._make_ctrl()
        calls: list[tuple[int, int, bytes, int]] = []

        def mock_executor(query: int, domain: int, payload: bytes, out_size: int) -> int:
            calls.append((query, domain, payload, out_size))
            return 0

        success = ctrl.set_experimental_fan_speed(18, 18, query_executor=mock_executor)
        assert success is True
        assert len(calls) == 3
        # 1. Query 0x10 (trigger)
        assert calls[0][0] == WMI_QUERY_FAN_COUNT
        assert calls[0][1] == WMI_CMD_GM
        assert calls[0][2] == b"\x00"
        # 2. Query 0x27(0) (disable max)
        assert calls[1][0] == WMI_QUERY_FAN_SPEED_MAX_SET
        assert calls[1][2] == b"\x00\x00\x00\x00"
        # 3. Query 0x2E (speed levels 18, 18)
        assert calls[2][0] == WMI_QUERY_FAN_SPEED_SET
        assert calls[2][2] == bytes([18, 18])

    def test_restore_firmware_auto_sequence(self) -> None:
        ctrl = self._make_ctrl()
        calls: list[tuple[int, int, bytes, int]] = []

        def mock_executor(query: int, domain: int, payload: bytes, out_size: int) -> int:
            calls.append((query, domain, payload, out_size))
            return 0

        success = ctrl.restore_firmware_auto(query_executor=mock_executor)
        assert success is True
        assert len(calls) == 3
        # Query 0x2E payload must be 0, 0
        assert calls[2][0] == WMI_QUERY_FAN_SPEED_SET
        assert calls[2][2] == bytes([0, 0])
        assert (ctrl._last_commanded_cpu_level, ctrl._last_commanded_gpu_level) == (0, 0)
        assert ctrl._current_control_mode == "firmware_auto"

    def test_duplicate_writes_suppressed_within_watchdog_window(self) -> None:
        ctrl = self._make_ctrl()
        call_count = 0

        def mock_executor(query: int, domain: int, payload: bytes, out_size: int) -> int:
            nonlocal call_count
            call_count += 1
            return 0

        # First write executes
        res1 = ctrl.set_experimental_fan_speed(19, 19, query_executor=mock_executor)
        assert res1 is True
        assert call_count == 3

        # Immediate duplicate write is suppressed (0 new queries executed)
        res2 = ctrl.set_experimental_fan_speed(19, 19, query_executor=mock_executor)
        assert res2 is True
        assert call_count == 3

    def test_watchdog_refresh_triggers_after_interval(self) -> None:
        ctrl = self._make_ctrl()
        call_count = 0

        def mock_executor(query: int, domain: int, payload: bytes, out_size: int) -> int:
            nonlocal call_count
            call_count += 1
            return 0

        ctrl.set_experimental_fan_speed(20, 20, query_executor=mock_executor)
        assert call_count == 3

        # Fast-forward time past 60s
        ctrl._last_control_time = time.time() - (EXPERIMENTAL_WATCHDOG_REFRESH_INTERVAL + 1.0)

        # Duplicate write must now execute to refresh watchdog
        res = ctrl.set_experimental_fan_speed(20, 20, query_executor=mock_executor)
        assert res is True
        assert call_count == 6


class TestAutoRpmFeedback(BaseAutoTest):
    """TASK 7: RPM telemetry feedback, shortfall detection, and bounded trim."""

    def test_shortfall_escalates_trim_conservatively(self) -> None:
        ctrl = self._make_ctrl()
        ctrl.reset_auto_state()

        # Cycle 1: CPU actual 1400 RPM vs target 1800 RPM (shortfall 400 > 350)
        trim_c, trim_g = ctrl.apply_auto_rpm_feedback(
            actual_cpu_rpm=1400, actual_gpu_rpm=1800, target_cpu_rpm=1800, target_gpu_rpm=1800
        )
        assert trim_c == 0  # Requires 2 consecutive cycles
        assert trim_g == 0

        # Cycle 2: Shortfall confirmed -> trim bumps to +1
        trim_c, trim_g = ctrl.apply_auto_rpm_feedback(
            actual_cpu_rpm=1400, actual_gpu_rpm=1800, target_cpu_rpm=1800, target_gpu_rpm=1800
        )
        assert trim_c == 1
        assert trim_g == 0

    def test_trim_bounded_at_maximum_ceiling(self) -> None:
        ctrl = self._make_ctrl()
        ctrl.reset_auto_state()
        ctrl._rpm_feedback_trim_cpu = 3
        ctrl._consecutive_rpm_shortfall_cpu = 1

        trim_c, _ = ctrl.apply_auto_rpm_feedback(
            actual_cpu_rpm=1200, actual_gpu_rpm=1800, target_cpu_rpm=1800, target_gpu_rpm=1800
        )
        assert trim_c == 3  # Capped at +3

    def test_trim_decays_when_rpm_satisfies_target(self) -> None:
        ctrl = self._make_ctrl()
        ctrl.reset_auto_state()
        ctrl._rpm_feedback_trim_cpu = 2

        # Actual RPM reaches 1700 RPM (within 200 of 1800) -> decays to 1
        trim_c, _ = ctrl.apply_auto_rpm_feedback(
            actual_cpu_rpm=1700, actual_gpu_rpm=1800, target_cpu_rpm=1800, target_gpu_rpm=1800
        )
        assert trim_c == 1

        # Second cycle within target -> decays to 0
        trim_c, _ = ctrl.apply_auto_rpm_feedback(
            actual_cpu_rpm=1800, actual_gpu_rpm=1800, target_cpu_rpm=1800, target_gpu_rpm=1800
        )
        assert trim_c == 0


class TestDaemonAutoIntegration(BaseAutoTest):
    """TASK 6: 5-second polling loop in serve.py for AUTO mode."""

    def test_daemon_idle_suppresses_repeated_fan_writes(self) -> None:
        ctrl = self._make_ctrl()
        ctrl.config["mode"] = "auto"

        cycle_count = [0]
        writes: list[tuple[int, int]] = []

        def _fake_sleep(sec: float) -> None:
            cycle_count[0] += 1
            if cycle_count[0] >= 3:
                raise KeyboardInterrupt()

        def _fake_set_speed(cpu: int, gpu: int, **kwargs: object) -> bool:
            writes.append((cpu, gpu))
            return True

        with (
            patch("omen_fan_control._cli.serve.get_controller", return_value=ctrl),
            patch.object(ctrl, "is_fan_control_ready", return_value=True),
            patch.object(ctrl, "get_reference_temp", return_value=50.0),
            patch.object(ctrl, "get_gpu_temp", return_value=40.0),
            patch("time.sleep", side_effect=_fake_sleep),
            patch.object(ctrl, "set_experimental_fan_speed", side_effect=_fake_set_speed),
            patch.object(ctrl, "restore_firmware_auto") as mock_restore,
        ):
            runner = CliRunner()
            res = runner.invoke(serve)
            assert res.exit_code == 0
            # While remaining idle below activation, 0 fan writes were issued
            assert len(writes) == 0
            # restore_firmware_auto called once on shutdown cleanup
            assert mock_restore.call_count >= 1

    def test_daemon_activates_software_auto_at_threshold(self) -> None:
        ctrl = self._make_ctrl()
        ctrl.config["mode"] = "auto"
        ctrl.config["ma_window"] = 1

        cycle_count = [0]
        writes: list[tuple[int, int]] = []

        def _fake_sleep(sec: float) -> None:
            cycle_count[0] += 1
            if cycle_count[0] >= 2:
                raise KeyboardInterrupt()

        def _fake_set_speed(cpu: int, gpu: int, **kwargs: object) -> bool:
            writes.append((cpu, gpu))
            return True

        with (
            patch("omen_fan_control._cli.serve.get_controller", return_value=ctrl),
            patch.object(ctrl, "is_fan_control_ready", return_value=True),
            patch.object(ctrl, "get_reference_temp", return_value=70.0),
            patch.object(ctrl, "get_gpu_temp", return_value=45.0),
            patch("time.sleep", side_effect=_fake_sleep),
            patch.object(ctrl, "set_experimental_fan_speed", side_effect=_fake_set_speed),
            patch.object(ctrl, "restore_firmware_auto"),
        ):
            runner = CliRunner()
            res = runner.invoke(serve)
            assert res.exit_code == 0
            assert "Auto thermal trigger reached" in res.output
            assert len(writes) >= 1
            # 70°C corresponds to CPU level 26, GPU level 27
            assert writes[0] == (26, 27)

    def test_daemon_cooldown_restores_firmware_auto(self) -> None:
        ctrl = self._make_ctrl()
        ctrl.config["mode"] = "auto"
        ctrl.config["ma_window"] = 1

        cycle_count = [0]
        temps = [(70.0, 50.0), (55.0, 45.0)]  # Cycle 1: active -> Cycle 2: cooled

        def _fake_sleep(sec: float) -> None:
            cycle_count[0] += 1
            if cycle_count[0] >= 2:
                raise KeyboardInterrupt()

        def _fake_cpu_temp() -> float:
            return temps[min(cycle_count[0], len(temps) - 1)][0]

        def _fake_gpu_temp() -> float:
            return temps[min(cycle_count[0], len(temps) - 1)][1]

        with (
            patch("omen_fan_control._cli.serve.get_controller", return_value=ctrl),
            patch.object(ctrl, "is_fan_control_ready", return_value=True),
            patch.object(ctrl, "get_reference_temp", side_effect=_fake_cpu_temp),
            patch.object(ctrl, "get_gpu_temp", side_effect=_fake_gpu_temp),
            patch("time.sleep", side_effect=_fake_sleep),
            patch.object(ctrl, "set_experimental_fan_speed"),
            patch.object(ctrl, "restore_firmware_auto") as mock_restore,
        ):
            runner = CliRunner()
            res = runner.invoke(serve)
            assert res.exit_code == 0
            assert "Auto thermal trigger reached" in res.output
            assert "Auto thermal cooldown reached" in res.output
            # Restored once during cooldown transition + once on shutdown
            assert mock_restore.call_count >= 2

    def test_daemon_invalid_temp_fallback(self) -> None:
        ctrl = self._make_ctrl()
        ctrl.config["mode"] = "auto"

        cycle_count = [0]

        def _fake_sleep(sec: float) -> None:
            cycle_count[0] += 1
            if cycle_count[0] >= 2:
                raise KeyboardInterrupt()

        with (
            patch("omen_fan_control._cli.serve.get_controller", return_value=ctrl),
            patch.object(ctrl, "is_fan_control_ready", return_value=True),
            patch.object(ctrl, "get_reference_temp", return_value=float("nan")),
            patch("time.sleep", side_effect=_fake_sleep),
            patch.object(ctrl, "restore_firmware_auto") as mock_restore,
        ):
            runner = CliRunner()
            res = runner.invoke(serve)
            assert res.exit_code == 0
            assert "Warning: Invalid or unavailable temperature" in res.output
            assert mock_restore.call_count >= 1


class TestAutoIntegrationBugFixesAndGating(BaseAutoTest):
    """TASK 11: Unit tests verifying AUTO integration bug fixes on board 8CC0 (11.A - 11.G)."""

    def test_11_a_8cc0_control_disabled_zero_writes(self) -> None:
        """11.A: 8CC0 + experimental control disabled: set_fan_pwm() performs zero WMI writes."""
        ctrl = self._make_ctrl(board="8CC0", exp_ctl=False)
        with patch.object(ctrl, "set_experimental_fan_speed") as mock_exp_speed, \
             patch.object(ctrl, "write_sys_file") as mock_write:
            ctrl.set_fan_pwm(128, 128)
            mock_exp_speed.assert_not_called()
            mock_write.assert_not_called()

    def test_11_b_8cc0_control_enabled_routes_0x2e(self) -> None:
        """11.B: 8CC0 + experimental control enabled: set_fan_pwm() routes through experimental_fan_speed / 0x2E."""
        ctrl = self._make_ctrl(board="8CC0", exp_ctl=True)
        with patch.object(ctrl, "get_calibrated_min_fan_levels", return_value=(15, 15)), \
             patch.object(ctrl, "set_experimental_fan_speed") as mock_exp_speed, \
             patch.object(ctrl, "write_sys_file") as mock_write:
            ctrl.set_fan_pwm(128, 150)
            mock_write.assert_not_called()
            mock_exp_speed.assert_called_once()
            cpu_lvl, gpu_lvl = mock_exp_speed.call_args[0]
            assert cpu_lvl == 50
            assert gpu_lvl == 59

    def test_11_c_8cc0_never_falls_through_to_legacy_hwmon(self) -> None:
        """11.C: 8CC0 must never fall through to legacy hwmon fan PWM, regardless of flags."""
        for ctl_enabled in (False, True):
            ctrl = self._make_ctrl(board="8CC0", exp_ctl=ctl_enabled)
            with patch.object(ctrl, "write_sys_file") as mock_write, \
                 patch.object(FanController, "fan_control_available", new_callable=PropertyMock, return_value=True), \
                 patch.object(ctrl, "set_experimental_fan_speed"):
                ctrl.set_fan_pwm(200, 200)
                mock_write.assert_not_called()

    def test_11_d_8cc0_telemetry_uses_0x2d_when_enabled(self) -> None:
        """11.D: 8CC0 telemetry uses 0x2D when telemetry is explicitly enabled, and 0 when disabled."""
        ctrl_enabled = self._make_ctrl(board="8CC0", exp_telem=True)
        with patch.object(ctrl_enabled, "read_experimental_fan_telemetry", return_value=(3500, 4200)):
            assert ctrl_enabled.get_fan_speed() == 4200
            assert ctrl_enabled.get_fan_speed_info() == (4200, False)
            both = ctrl_enabled.get_both_fan_speeds()
            assert both == [("Fan 1 (CPU)", 3500, False), ("Fan 2 (GPU)", 4200, False)]

        ctrl_disabled = self._make_ctrl(board="8CC0", exp_telem=False)
        with patch.object(ctrl_disabled, "read_experimental_fan_telemetry") as mock_read:
            assert ctrl_disabled.get_fan_speed() == 0
            assert ctrl_disabled.get_fan_speed_info() == (0, False)
            assert ctrl_disabled.get_both_fan_speeds() == [("Fan 1 (CPU)", 0, False), ("Fan 2 (GPU)", 0, False)]
            mock_read.assert_not_called()

    def test_11_e_configuration_persists_both_flags(self) -> None:
        """11.E: Configuration persists both flags correctly."""
        from omen_fan_control._config import load_config
        ctrl = self._make_ctrl(board="8CC0")

        ctrl.config["enable_experimental_control"] = True
        ctrl.config["enable_experimental_telemetry"] = True
        ctrl.save_config()
        loaded = load_config(self.cfg_path)
        assert loaded.get("enable_experimental_control") is True
        assert loaded.get("enable_experimental_telemetry") is True

        ctrl.config["enable_experimental_control"] = False
        ctrl.config["enable_experimental_telemetry"] = False
        ctrl.save_config()
        loaded2 = load_config(self.cfg_path)
        assert loaded2.get("enable_experimental_control") is False
        assert loaded2.get("enable_experimental_telemetry") is False

    def test_11_f_default_configuration_safe_disabled(self) -> None:
        """11.F: Default configuration remains safe with experimental control disabled."""
        from omen_fan_control._config import DEFAULT_CONFIG
        assert DEFAULT_CONFIG.get("enable_experimental_control") is False
        assert DEFAULT_CONFIG.get("enable_experimental_telemetry") is False
        assert DEFAULT_CONFIG.get("enable_experimental") is False

        with patch.object(FanController, "_get_live_board_name", return_value="8CC0"), \
             patch.object(FanController, "_read_module_param", return_value=False):
            empty_cfg = self.tmp_dir / "empty_config.json"
            ctrl = FanController(config_path=empty_cfg)
            support_status, board = ctrl.check_board_support()
            assert support_status == "MONITORING_ONLY"
            assert board == "8CC0"
            assert ctrl.is_experimental_control_enabled() is False
            assert ctrl.is_experimental_telemetry_enabled() is False
            assert ctrl.is_fan_control_ready() is False

    def test_11_g_daemon_readiness_reflects_experimental_interface(self) -> None:
        """11.G: Daemon readiness correctly reflects the actual experimental interface."""
        ctrl_dis = self._make_ctrl(board="8CC0", exp_ctl=False)
        assert ctrl_dis.is_fan_control_ready() is False

        ctrl_en = self._make_ctrl(board="8CC0", exp_ctl=True)
        missing_speed = MagicMock(spec=Path)
        missing_speed.exists.return_value = False
        with patch("omen_fan_control.fan_controller.EXPERIMENTAL_FAN_SPEED_PATH", missing_speed):
            assert ctrl_en.is_fan_control_ready() is False

        present_speed = MagicMock(spec=Path)
        present_speed.exists.return_value = True
        with patch("omen_fan_control.fan_controller.EXPERIMENTAL_FAN_SPEED_PATH", present_speed):
            assert ctrl_en.is_fan_control_ready() is True


class TestAutoStagnationRecoveryBoost(BaseAutoTest):
    """FEATURE 1: AUTO Thermal Recovery & Stagnation Boost (>= 77°C)."""

    def test_below_77_no_recovery_boost(self) -> None:
        """Temperatures below 77°C are not eligible for stagnation detection."""
        ctrl = self._make_ctrl()
        ctrl.reset_auto_state()

        # At 76.0°C for 200 seconds, boost remains 0
        b_cpu, b_gpu, changed = ctrl.update_auto_stagnation_state(cpu_temp=76.0, gpu_temp=60.0, now=0.0)
        assert (b_cpu, b_gpu) == (0, 0)
        assert not changed

        b_cpu, b_gpu, changed = ctrl.update_auto_stagnation_state(cpu_temp=76.5, gpu_temp=65.0, now=200.0)
        assert (b_cpu, b_gpu) == (0, 0)
        assert not changed
        assert ctrl.get_auto_stagnation_boost() == (0, 0)

    def test_77_plus_temperature_falling_no_boost(self) -> None:
        """When temperature is >= 77°C but meaningfully improving (>= 1°C), do not boost."""
        ctrl = self._make_ctrl()
        ctrl.reset_auto_state()

        # t=0: 80.0°C starts window
        ctrl.update_auto_stagnation_state(cpu_temp=80.0, gpu_temp=None, now=0.0)

        # t=60: 78.5°C (dropped by 1.5°C >= 1.0°C min improvement)
        b_cpu, _, changed = ctrl.update_auto_stagnation_state(cpu_temp=78.5, gpu_temp=None, now=60.0)
        assert b_cpu == 0
        assert not changed

        # t=150: (elapsed from reset=90s < 120s)
        b_cpu, _, changed = ctrl.update_auto_stagnation_state(cpu_temp=78.0, gpu_temp=None, now=150.0)
        assert b_cpu == 0
        assert not changed

    def test_77_plus_normal_cooling_no_boost(self) -> None:
        """When normal cooling is actively driving temperature down, do not boost."""
        ctrl = self._make_ctrl()
        ctrl.reset_auto_state()

        # Consistent cooling steps every 40s
        ctrl.update_auto_stagnation_state(cpu_temp=82.0, gpu_temp=None, now=0.0)
        ctrl.update_auto_stagnation_state(cpu_temp=80.8, gpu_temp=None, now=40.0)
        ctrl.update_auto_stagnation_state(cpu_temp=79.5, gpu_temp=None, now=80.0)
        b_cpu, _, _ = ctrl.update_auto_stagnation_state(cpu_temp=78.0, gpu_temp=None, now=120.0)
        assert b_cpu == 0

    def test_temperature_stagnant_less_than_threshold_no_boost(self) -> None:
        """Temperature stagnant for less than the 120s window does not boost."""
        ctrl = self._make_ctrl()
        ctrl.reset_auto_state()

        ctrl.update_auto_stagnation_state(cpu_temp=80.0, gpu_temp=None, now=0.0)
        # 119 seconds elapsed (< 120.0s)
        b_cpu, _, changed = ctrl.update_auto_stagnation_state(cpu_temp=80.1, gpu_temp=None, now=119.0)
        assert b_cpu == 0
        assert not changed

    def test_temperature_stagnant_for_threshold_boost_activates(self) -> None:
        """Temperature stagnant for >= 120s activates initial recovery boost (+4)."""
        ctrl = self._make_ctrl()
        ctrl.reset_auto_state()

        ctrl.update_auto_stagnation_state(cpu_temp=80.0, gpu_temp=None, now=0.0)
        # 120 seconds elapsed with temperature hovering around 80.0°C
        b_cpu, b_gpu, changed = ctrl.update_auto_stagnation_state(cpu_temp=79.8, gpu_temp=None, now=120.0)
        assert b_cpu == AUTO_STAGNATION_INITIAL_BOOST_LEVEL  # +4
        assert b_gpu == 0
        assert changed
        assert ctrl.get_auto_stagnation_boost() == (4, 0)

    def test_boost_increases_fan_level_incrementally(self) -> None:
        """Continued stagnation after re-evaluation interval applies incremental step boost (+2)."""
        ctrl = self._make_ctrl()
        ctrl.reset_auto_state()

        # t=0 to 120: triggers initial boost (+4)
        ctrl.update_auto_stagnation_state(cpu_temp=80.0, gpu_temp=None, now=0.0)
        b_cpu, _, _ = ctrl.update_auto_stagnation_state(cpu_temp=80.0, gpu_temp=None, now=120.0)
        assert b_cpu == 4

        # t=155 (35s after boost >= 30s step interval): still stagnant -> increments to 6 (+2)
        b_cpu, _, changed = ctrl.update_auto_stagnation_state(cpu_temp=80.0, gpu_temp=None, now=155.0)
        assert b_cpu == 6
        assert changed

        # t=190 (35s later): still stagnant -> increments to 8 (+2)
        b_cpu, _, changed = ctrl.update_auto_stagnation_state(cpu_temp=80.0, gpu_temp=None, now=190.0)
        assert b_cpu == 8
        assert changed

    def test_boost_never_exceeds_auto_ceiling(self) -> None:
        """Boost is strictly clamped to AUTO fan ceiling (57 CPU, 61 GPU)."""
        ctrl = self._make_ctrl()
        ctrl.reset_auto_state()
        ctrl.update_auto_thermal_state(cpu_temp=85.0, gpu_temp=85.0)

        # Force a large boost in controller state
        ctrl._auto_stagnation_cpu["boost_level"] = 25
        ctrl._auto_stagnation_gpu["boost_level"] = 25

        report = ctrl.calculate_auto_target(cpu_temp=85.0, gpu_temp=85.0)
        # Normal curve at 85°C is 57 CPU, 61 GPU.
        # Adding 25 must NOT exceed 57 CPU and 61 GPU!
        assert report["cpu_fan_level"] == AUTO_MAX_FAN_LEVEL_CPU  # 57
        assert report["gpu_fan_level"] == AUTO_MAX_FAN_LEVEL_GPU  # 61

    def test_boost_works_independently_for_cpu_and_gpu(self) -> None:
        """CPU at 80°C (stagnant) boosts CPU fan only; GPU at 60°C is unaffected."""
        ctrl = self._make_ctrl()
        ctrl.reset_auto_state()
        ctrl.update_auto_thermal_state(cpu_temp=80.0, gpu_temp=60.0)

        ctrl.update_auto_stagnation_state(cpu_temp=80.0, gpu_temp=60.0, now=0.0)
        b_cpu, b_gpu, changed = ctrl.update_auto_stagnation_state(cpu_temp=80.0, gpu_temp=60.0, now=120.0)

        assert b_cpu == 4
        assert b_gpu == 0
        assert changed

        # In target report, CPU level has +4 applied, GPU has 0 boost applied
        report = ctrl.calculate_auto_target(cpu_temp=80.0, gpu_temp=60.0, independent_fans=True)
        # At 80°C normal CPU level is 47 -> 47 + 4 = 51
        assert report["cpu_fan_level"] == 51
        assert report["stagnation_boost_cpu"] == 4
        assert report["stagnation_boost_gpu"] == 0

    def test_recovery_band_removes_boost(self) -> None:
        """When temperature reaches 74-75°C recovery band, boost is removed."""
        ctrl = self._make_ctrl()
        ctrl.reset_auto_state()

        # Engage boost
        ctrl.update_auto_stagnation_state(cpu_temp=80.0, gpu_temp=None, now=0.0)
        ctrl.update_auto_stagnation_state(cpu_temp=80.0, gpu_temp=None, now=120.0)
        assert ctrl.get_auto_stagnation_boost()[0] == 4

        # Temperature cools into hysteresis band (76.0°C): boost remains on!
        b_cpu, _, _ = ctrl.update_auto_stagnation_state(cpu_temp=76.0, gpu_temp=None, now=140.0)
        assert b_cpu == 4

        # Temperature reaches recovery band (74.8°C <= 75.0°C): boost REMOVED!
        b_cpu, _, changed = ctrl.update_auto_stagnation_state(cpu_temp=74.8, gpu_temp=None, now=160.0)
        assert b_cpu == 0
        assert changed
        assert ctrl.get_auto_stagnation_boost() == (0, 0)

    def test_controller_returns_to_normal_curve_after_recovery(self) -> None:
        """After recovery boost is removed, target returns to exact normal curve."""
        ctrl = self._make_ctrl()
        ctrl.reset_auto_state()
        ctrl.update_auto_thermal_state(cpu_temp=75.0, gpu_temp=50.0)

        # Baseline curve target at 75°C
        baseline_report = ctrl.calculate_auto_target(cpu_temp=75.0, gpu_temp=50.0)
        expected_cpu_level = baseline_report["cpu_fan_level"]

        # Simulate stagnation, then recovery
        ctrl.update_auto_stagnation_state(cpu_temp=80.0, gpu_temp=None, now=0.0)
        ctrl.update_auto_stagnation_state(cpu_temp=80.0, gpu_temp=None, now=120.0)
        assert ctrl.get_auto_stagnation_boost()[0] > 0

        # Cool to 74.5°C
        ctrl.update_auto_stagnation_state(cpu_temp=74.5, gpu_temp=None, now=160.0)
        assert ctrl.get_auto_stagnation_boost() == (0, 0)

        # Re-check target at 75°C
        new_report = ctrl.calculate_auto_target(cpu_temp=75.0, gpu_temp=50.0)
        assert new_report["cpu_fan_level"] == expected_cpu_level

    def test_boost_does_not_permanently_modify_curve(self) -> None:
        """Stagnation boost does not modify the curve table or configured curve."""
        ctrl = self._make_ctrl()
        table_before = [dict(bp) for bp in ctrl.get_auto_curve_breakpoints()]

        ctrl.update_auto_stagnation_state(cpu_temp=80.0, gpu_temp=None, now=0.0)
        ctrl.update_auto_stagnation_state(cpu_temp=80.0, gpu_temp=None, now=120.0)
        ctrl.calculate_auto_target(cpu_temp=80.0, gpu_temp=None)

        table_after = [dict(bp) for bp in ctrl.get_auto_curve_breakpoints()]
        assert table_before == table_after

    def test_boost_does_not_ratchet_indefinitely(self) -> None:
        """Boost does not ratchet indefinitely and clears completely on cooldown."""
        ctrl = self._make_ctrl()
        ctrl.reset_auto_state()

        # Simulate 10 incremental steps at 80°C
        ctrl.update_auto_stagnation_state(cpu_temp=80.0, gpu_temp=None, now=0.0)
        ctrl.update_auto_stagnation_state(cpu_temp=80.0, gpu_temp=None, now=120.0)
        for t in range(150, 450, 30):
            ctrl.update_auto_stagnation_state(cpu_temp=80.0, gpu_temp=None, now=float(t))

        # Must never exceed AUTO ceiling
        b_cpu, _ = ctrl.get_auto_stagnation_boost()
        assert b_cpu <= AUTO_MAX_FAN_LEVEL_CPU

        # Cooldown resets boost to 0
        ctrl.update_auto_stagnation_state(cpu_temp=74.0, gpu_temp=None, now=500.0)
        assert ctrl.get_auto_stagnation_boost() == (0, 0)

    def test_repeated_heating_stagnation_triggers_another_boost(self) -> None:
        """After recovery, subsequent heating and stagnation legitimately triggers another boost."""
        ctrl = self._make_ctrl()
        ctrl.reset_auto_state()

        # First cycle: 80°C -> boost -> cool to 74°C -> clear
        ctrl.update_auto_stagnation_state(cpu_temp=80.0, gpu_temp=None, now=0.0)
        ctrl.update_auto_stagnation_state(cpu_temp=80.0, gpu_temp=None, now=120.0)
        assert ctrl.get_auto_stagnation_boost()[0] == 4

        ctrl.update_auto_stagnation_state(cpu_temp=74.0, gpu_temp=None, now=160.0)
        assert ctrl.get_auto_stagnation_boost() == (0, 0)

        # Re-heating to 81°C starts a new window
        ctrl.update_auto_stagnation_state(cpu_temp=81.0, gpu_temp=None, now=200.0)
        assert ctrl.get_auto_stagnation_boost() == (0, 0)

        # 120s later at t=320s, stagnation again -> boosts!
        b_cpu, _, changed = ctrl.update_auto_stagnation_state(cpu_temp=81.0, gpu_temp=None, now=320.0)
        assert b_cpu == 4
        assert changed

    def test_performance_max_manual_modes_unaffected(self) -> None:
        """Performance, max, and manual modes do not use stagnation boost."""
        ctrl = self._make_ctrl()
        ctrl.config["mode"] = "performance"
        pwm_c, pwm_g = ctrl.calculate_dual_target_pwm(cpu_temp=80, gpu_temp=80)
        # Verify calculation completes normally and returns valid PWM
        assert 0 <= pwm_c <= 255
        assert 0 <= pwm_g <= 255

        ctrl.config["mode"] = "manual"
        ctrl.config["manual_pwm"] = 180
        man_c, man_g = ctrl.get_dual_manual_pwm()
        assert (man_c, man_g) == (180, 180)


class TestAutoHighTemperatureSpikeSafetyBarrier(BaseAutoTest):
    """FEATURE 2: AUTO High-Temperature Spike Safety Barrier (>= 85°C)."""

    def test_84_celsius_no_emergency_state(self) -> None:
        """Temperatures below 85°C do not trigger emergency state."""
        ctrl = self._make_ctrl()
        ctrl.reset_auto_state()

        state, changed = ctrl.update_auto_emergency_state(cpu_temp=84.0, gpu_temp=70.0, now=0.0)
        assert state == "normal"
        assert not changed
        assert ctrl.get_auto_emergency_state() == "normal"

    def test_single_85_celsius_sample_no_immediate_max(self) -> None:
        """A single >= 85°C reading does not immediately command MAX (transitions to pending)."""
        ctrl = self._make_ctrl()
        ctrl.reset_auto_state()

        state, changed = ctrl.update_auto_emergency_state(cpu_temp=85.0, gpu_temp=None, now=0.0)
        assert state == "pending"
        assert changed
        assert ctrl.get_auto_emergency_state() == "pending"
        # Emergency MAX is NOT active during pending
        assert not ctrl._auto_emergency_max_active

    def test_94_celsius_transient_spike_no_max(self) -> None:
        """A 94°C reading for 1-2s cancels pending state upon return to 60°C; never commands MAX."""
        ctrl = self._make_ctrl()
        ctrl.reset_auto_state()

        # Spike at t=0
        state, _ = ctrl.update_auto_emergency_state(cpu_temp=94.0, gpu_temp=None, now=0.0)
        assert state == "pending"

        # Falls back to 60°C at t=1.5s
        state, changed = ctrl.update_auto_emergency_state(cpu_temp=60.0, gpu_temp=None, now=1.5)
        assert state == "normal"
        assert changed
        assert not ctrl._auto_emergency_max_active

    def test_transient_spike_clears_pending_cancelled(self) -> None:
        """Transient high reading clears pending emergency state cleanly."""
        ctrl = self._make_ctrl()
        ctrl.reset_auto_state()

        ctrl.update_auto_emergency_state(cpu_temp=90.0, gpu_temp=None, now=0.0)
        assert ctrl.get_auto_emergency_state() == "pending"

        ctrl.update_auto_emergency_state(cpu_temp=78.0, gpu_temp=None, now=2.0)
        assert ctrl.get_auto_emergency_state() == "normal"
        assert ctrl._auto_emergency_pending_start is None

    def test_sustained_85_celsius_activates_emergency_cooling(self) -> None:
        """High temperature persisting for >= 3s activates emergency cooling state."""
        ctrl = self._make_ctrl()
        ctrl.reset_auto_state()

        # t=0: enters pending
        ctrl.update_auto_emergency_state(cpu_temp=88.0, gpu_temp=None, now=0.0)

        # t=3.5: persists >= 3.0s confirmation window -> enters active!
        state, changed = ctrl.update_auto_emergency_state(cpu_temp=89.0, gpu_temp=None, now=3.5)
        assert state == "active"
        assert changed
        assert ctrl._auto_emergency_max_active is True

    def test_emergency_state_suppresses_duplicate_max_writes(self) -> None:
        """Subsequent sustained calls while in active emergency report state_changed=False."""
        ctrl = self._make_ctrl()
        ctrl.reset_auto_state()

        ctrl.update_auto_emergency_state(cpu_temp=90.0, gpu_temp=None, now=0.0)
        state1, changed1 = ctrl.update_auto_emergency_state(cpu_temp=90.0, gpu_temp=None, now=3.5)
        assert state1 == "active"
        assert changed1 is True

        # Subsequent cycles in emergency cooling: no duplicate write needed
        state2, changed2 = ctrl.update_auto_emergency_state(cpu_temp=90.0, gpu_temp=None, now=8.5)
        assert state2 == "active"
        assert changed2 is False

        state3, changed3 = ctrl.update_auto_emergency_state(cpu_temp=89.0, gpu_temp=None, now=13.5)
        assert state3 == "active"
        assert changed3 is False

    def test_emergency_state_has_recovery_hysteresis(self) -> None:
        """Emergency cooling requires temperature to drop <= 80.0°C to recover."""
        ctrl = self._make_ctrl()
        ctrl.reset_auto_state()

        ctrl.update_auto_emergency_state(cpu_temp=90.0, gpu_temp=None, now=0.0)
        ctrl.update_auto_emergency_state(cpu_temp=90.0, gpu_temp=None, now=3.5)
        assert ctrl.get_auto_emergency_state() == "active"

        # Drops below 85°C to 83°C: still above 80.0°C recovery temp -> remains ACTIVE!
        state, changed = ctrl.update_auto_emergency_state(cpu_temp=83.0, gpu_temp=None, now=8.5)
        assert state == "active"
        assert not changed

    def test_recovery_returns_to_normal_auto(self) -> None:
        """When temperature cools <= 80.0°C, emergency cooling state is cleared."""
        ctrl = self._make_ctrl()
        ctrl.reset_auto_state()

        ctrl.update_auto_emergency_state(cpu_temp=90.0, gpu_temp=None, now=0.0)
        ctrl.update_auto_emergency_state(cpu_temp=90.0, gpu_temp=None, now=3.5)

        # Cooldown below 80.0°C (e.g. 78.5°C)
        state, changed = ctrl.update_auto_emergency_state(cpu_temp=78.5, gpu_temp=None, now=12.0)
        assert state == "normal"
        assert changed
        assert ctrl.get_auto_emergency_state() == "normal"
        assert ctrl._auto_emergency_max_active is False

    def test_invalid_missing_sensor_data_does_not_clear_emergency(self) -> None:
        """Missing or NaN sensor data during active emergency does not falsely claim recovery."""
        ctrl = self._make_ctrl()
        ctrl.reset_auto_state()

        ctrl.update_auto_emergency_state(cpu_temp=90.0, gpu_temp=None, now=0.0)
        ctrl.update_auto_emergency_state(cpu_temp=90.0, gpu_temp=None, now=3.5)
        assert ctrl.get_auto_emergency_state() == "active"

        # None reading: must stay in active emergency
        state, changed = ctrl.update_auto_emergency_state(cpu_temp=None, gpu_temp=None, now=8.0)
        assert state == "active"
        assert not changed

        # NaN reading: must stay in active emergency
        state, changed = ctrl.update_auto_emergency_state(cpu_temp=float("nan"), gpu_temp=None, now=12.0)
        assert state == "active"
        assert not changed

    def test_performance_max_manual_modes_unaffected(self) -> None:
        """Non-auto modes do not invoke or depend on auto emergency state."""
        ctrl = self._make_ctrl()
        ctrl.config["mode"] = "max"
        assert ctrl.config.get("mode") == "max"
        ctrl.reset_auto_state()
        assert ctrl.get_auto_emergency_state() == "normal"


class TestDaemonAutoNewFeaturesIntegration(BaseAutoTest):
    """Daemon integration tests for spike barrier and stagnation boost."""

    def test_daemon_transient_spike_does_not_command_max(self) -> None:
        """Daemon handles 1-cycle spike without commanding MAX fan."""
        ctrl = self._make_ctrl()
        ctrl.config["mode"] = "auto"
        ctrl.config["ma_window"] = 1

        # Startup + Cycle 1: 94°C (pending) -> Cycle 2: 60°C (cleared)
        temps = [60.0, 94.0, 60.0]
        temp_idx = [0]
        max_commands: list[bool] = []

        def _get_temp() -> float:
            idx = min(temp_idx[0], len(temps) - 1)
            t = temps[idx]
            temp_idx[0] += 1
            return t

        def _fake_sleep(sec: float) -> None:
            if temp_idx[0] >= len(temps):
                raise KeyboardInterrupt()

        def _fake_set_max(enable: bool, **kwargs: object) -> bool:
            max_commands.append(bool(enable))
            return True

        with (
            patch("omen_fan_control._cli.serve.get_controller", return_value=ctrl),
            patch.object(ctrl, "is_fan_control_ready", return_value=True),
            patch.object(ctrl, "get_reference_temp", side_effect=_get_temp),
            patch.object(ctrl, "get_gpu_temp", return_value=45.0),
            patch("time.sleep", side_effect=_fake_sleep),
            patch.object(ctrl, "set_experimental_fan_max", side_effect=_fake_set_max),
            patch.object(ctrl, "set_experimental_fan_speed", return_value=True),
            patch.object(ctrl, "restore_firmware_auto"),
        ):
            runner = CliRunner()
            res = runner.invoke(serve)
            assert res.exit_code == 0
            # MAX was never commanded for transient 94°C spike!
            assert len(max_commands) == 0

    def test_daemon_sustained_high_temp_commands_max_and_recovers(self) -> None:
        """Daemon commands MAX fan on sustained high temp and recovers when cooled."""
        ctrl = self._make_ctrl()
        ctrl.config["mode"] = "auto"
        ctrl.config["ma_window"] = 1

        # Startup: 60°C
        # Cycle 1: 90°C (pending, t=0s)
        # Cycle 2: 90°C (confirmed -> MAX ON, t=5s >= 3.0s confirm_sec)
        # Cycle 3: 90°C (duplicate -> suppressed, t=10s)
        # Cycle 4: 75°C (recovered -> MAX OFF, t=15s <= 80.0°C recovery)
        temps = [60.0, 90.0, 90.0, 90.0, 75.0]
        temp_idx = [0]
        mono_time = [0.0]
        max_commands: list[bool] = []

        def _get_temp() -> float:
            idx = min(temp_idx[0], len(temps) - 1)
            t = temps[idx]
            temp_idx[0] += 1
            return t

        def _fake_sleep(sec: float) -> None:
            mono_time[0] += 5.0
            if temp_idx[0] >= len(temps):
                raise KeyboardInterrupt()

        def _fake_set_max(enable: bool, **kwargs: object) -> bool:
            max_commands.append(bool(enable))
            return True

        with (
            patch("omen_fan_control._cli.serve.get_controller", return_value=ctrl),
            patch.object(ctrl, "is_fan_control_ready", return_value=True),
            patch.object(ctrl, "get_reference_temp", side_effect=_get_temp),
            patch.object(ctrl, "get_gpu_temp", return_value=45.0),
            patch("time.sleep", side_effect=_fake_sleep),
            patch("time.monotonic", side_effect=lambda: mono_time[0]),
            patch("omen_fan_control.fan_controller.time.monotonic", side_effect=lambda: mono_time[0]),
            patch.object(ctrl, "set_experimental_fan_max", side_effect=_fake_set_max),
            patch.object(ctrl, "set_experimental_fan_speed", return_value=True),
            patch.object(ctrl, "restore_firmware_auto"),
        ):
            runner = CliRunner()
            res = runner.invoke(serve)
            assert res.exit_code == 0
            # Commanded MAX True once, then recovered MAX False
            assert max_commands == [True, False]
            assert "Emergency high-temperature detected" in res.output
            assert "Emergency cooling recovered" in res.output

    def test_daemon_stagnation_boost_integration(self) -> None:
        """Daemon detects stagnation and logs/applies stagnation boost."""
        ctrl = self._make_ctrl()
        ctrl.config["mode"] = "auto"
        ctrl.config["ma_window"] = 1

        # Startup: 60°C
        # Cycle 1: 80°C (t=0s, normal curve)
        # Cycle 2: 80°C (t=125s, stagnant >= 120s -> boost +4)
        temps = [60.0, 80.0, 80.0]
        temp_idx = [0]
        mono_time = [0.0]
        commanded_speeds: list[tuple[int, int]] = []

        def _get_temp() -> float:
            idx = min(temp_idx[0], len(temps) - 1)
            t = temps[idx]
            temp_idx[0] += 1
            return t

        def _fake_sleep(sec: float) -> None:
            mono_time[0] += 125.0
            if temp_idx[0] >= len(temps):
                raise KeyboardInterrupt()

        def _fake_set_speed(c_spd: int, g_spd: int, **kwargs: object) -> bool:
            commanded_speeds.append((c_spd, g_spd))
            return True

        with (
            patch("omen_fan_control._cli.serve.get_controller", return_value=ctrl),
            patch.object(ctrl, "is_fan_control_ready", return_value=True),
            patch.object(ctrl, "get_reference_temp", side_effect=_get_temp),
            patch.object(ctrl, "get_gpu_temp", return_value=50.0),
            patch("time.sleep", side_effect=_fake_sleep),
            patch("time.monotonic", side_effect=lambda: mono_time[0]),
            patch("omen_fan_control.fan_controller.time.monotonic", side_effect=lambda: mono_time[0]),
            patch.object(ctrl, "set_experimental_fan_speed", side_effect=_fake_set_speed),
            patch.object(ctrl, "restore_firmware_auto"),
        ):
            runner = CliRunner()
            res = runner.invoke(serve)
            assert res.exit_code == 0
            assert "Auto thermal stagnation boost engaged" in res.output
            assert len(commanded_speeds) >= 2
            # Second cycle speed reflects +4 boost over first cycle
            assert commanded_speeds[1][0] == commanded_speeds[0][0] + 4
