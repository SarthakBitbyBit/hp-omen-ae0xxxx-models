"""Focused mocked regression tests for supported fan-control modes.

No test accesses real sysfs or validates physical fan behavior.
"""
from __future__ import annotations

import builtins
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, mock_open, patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from omen_fan_control._constants import (  # noqa: E402
    DEFAULT_AUTO_CURVE,
    DEFAULT_PERFORMANCE_CURVE,
    MONITORING_ONLY_BOARDS,
)
from omen_fan_control.fan_controller import FanController  # noqa: E402


class TestCurveModes(unittest.TestCase):
    def setUp(self) -> None:
        self.controller = FanController.__new__(FanController)
        self.controller.config = {"curve_interpolation": "smooth"}

    def _pwm(self, curve: list[list[float]], temp: int) -> int:
        result = self.controller.calculate_target_pwm(temp, curve=curve)
        self.assertIsNotNone(result)
        return result  # type: ignore[return-value]

    def test_performance_is_at_least_auto_across_dense_temperature_range(self) -> None:
        for temp in range(DEFAULT_AUTO_CURVE[0][0], DEFAULT_AUTO_CURVE[-1][0] + 1):
            self.assertGreaterEqual(
                self._pwm(DEFAULT_PERFORMANCE_CURVE, temp),
                self._pwm(DEFAULT_AUTO_CURVE, temp),
                f"performance target below auto target at {temp}°C",
            )

    def test_performance_is_strictly_higher_before_maximum(self) -> None:
        for temp in range(DEFAULT_AUTO_CURVE[0][0], DEFAULT_AUTO_CURVE[-1][0]):
            self.assertGreater(
                self._pwm(DEFAULT_PERFORMANCE_CURVE, temp),
                self._pwm(DEFAULT_AUTO_CURVE, temp),
                f"performance target not above auto target at {temp}°C",
            )

    def test_curve_override_applies_to_both_targets(self) -> None:
        self.controller.config.update(
            {
                "use_advanced_fan_control": True,
                "fan_control_method": "custom_gpu",
                "gpu_curve_use_gpu_temp": True,
                "gpu_curve": [[40, 1]],
            }
        )
        auto_cpu, auto_gpu = self.controller.calculate_dual_target_pwm(
            60, 60, curve_override=DEFAULT_AUTO_CURVE
        )
        performance_cpu, performance_gpu = self.controller.calculate_dual_target_pwm(
            60, 60, curve_override=DEFAULT_PERFORMANCE_CURVE
        )
        self.assertGreater(performance_cpu, auto_cpu)
        self.assertGreater(performance_gpu, auto_gpu)

    def test_curve_validation_rejects_empty_and_malformed_data(self) -> None:
        self.assertFalse(FanController.is_valid_curve([]))
        self.assertFalse(FanController.is_valid_curve([[40]]))
        self.assertFalse(FanController.is_valid_curve([[40, 101]]))
        self.assertTrue(FanController.is_valid_curve(DEFAULT_AUTO_CURVE))


class TestMonitoringOnlyHardwareGuard(unittest.TestCase):
    def _controller(self) -> FanController:
        controller = FanController.__new__(FanController)
        controller.config = {"cached_board_name": "8D41"}
        controller.config_path = Path("/mock/config.json")
        return controller

    def test_8cc0_is_monitoring_only(self) -> None:
        self.assertIn("8CC0", MONITORING_ONLY_BOARDS)
        controller = self._controller()
        with patch.object(controller, "_get_live_board_name", return_value="8CC0"), \
             patch.object(controller, "save_config"):
            status, board = controller.check_board_support()
        self.assertEqual((status, board), ("MONITORING_ONLY", "8CC0"))

    def test_live_8cc0_blocks_real_write_method_with_hwmon_available(self) -> None:
        controller = self._controller()
        path = Path("/mock/pwm1")
        mocked_open = mock_open()
        with (
            patch.object(FanController, "fan_control_available", new_callable=PropertyMock, return_value=True),
            patch.object(controller, "_get_live_board_name", return_value="8CC0"),
            patch.object(builtins, "open", mocked_open),
        ):
            controller.write_sys_file(path, 255)
        mocked_open.assert_not_called()

    def test_non_monitoring_board_can_reach_mocked_write(self) -> None:
        controller = self._controller()
        path = Path("/mock/pwm1")
        mocked_open = mock_open()
        with (
            patch.object(FanController, "fan_control_available", new_callable=PropertyMock, return_value=True),
            patch.object(controller, "_get_live_board_name", return_value="8D41"),
            patch.object(builtins, "open", mocked_open),
        ):
            controller.write_sys_file(path, 255)
        mocked_open.assert_called_once_with(path, "w")
        mocked_open().write.assert_called_once_with("255")


class TestModeEnablePaths(unittest.TestCase):
    def _controller(self, pwm2_enable_exists: bool = True) -> FanController:
        controller = FanController.__new__(FanController)
        controller.pwm1_enable_path = Path("/mock/pwm1_enable")
        controller.pwm2_enable_path = MagicMock()
        controller.pwm2_enable_path.exists.return_value = pwm2_enable_exists
        return controller

    def test_max_writes_both_enable_paths_when_present(self) -> None:
        controller = self._controller()
        with (
            patch.object(FanController, "fan_control_available", new_callable=PropertyMock, return_value=True),
            patch.object(controller, "write_sys_file") as write,
        ):
            controller.set_fan_mode("max")
        write.assert_any_call(controller.pwm1_enable_path, 0)
        write.assert_any_call(controller.pwm2_enable_path, 0)

    def test_max_skips_missing_second_enable_path(self) -> None:
        controller = self._controller(pwm2_enable_exists=False)
        with (
            patch.object(FanController, "fan_control_available", new_callable=PropertyMock, return_value=True),
            patch.object(controller, "write_sys_file") as write,
        ):
            controller.set_fan_mode("max")
        write.assert_called_once_with(controller.pwm1_enable_path, 0)


class TestServiceManager(unittest.TestCase):
    def setUp(self) -> None:
        self.controller = FanController.__new__(FanController)

    def test_create_service_installs_unit_and_enables(self) -> None:
        written_content: dict[str, str] = {}

        def fake_write_text(content: str) -> None:
            written_content["text"] = content

        with (
            patch("pathlib.Path.mkdir"),
            patch("pathlib.Path.write_text", side_effect=fake_write_text),
            patch("pathlib.Path.chmod"),
            patch("omen_fan_control._service._restore_selinux_context"),
            patch("subprocess.run") as mock_subp,
        ):
            success, msg = self.controller.create_service()
            self.assertTrue(success)
            self.assertIn("created and started", msg)
            self.assertIn("ExecStart=", written_content["text"])
            self.assertIn("omen_fan_control.cli serve", written_content["text"])
            self.assertIn("Restart=on-failure", written_content["text"])
            self.assertIn("User=root", written_content["text"])
            self.assertIn("WantedBy=multi-user.target", written_content["text"])

            commands = [call[0][0] for call in mock_subp.call_args_list]
            self.assertIn(["systemctl", "daemon-reload"], commands)
            self.assertIn(["systemctl", "enable", "omen-fan-control.service"], commands)
            self.assertIn(["systemctl", "start", "omen-fan-control.service"], commands)

    def test_remove_service_stops_disables_and_removes(self) -> None:
        with (
            patch("pathlib.Path.exists", return_value=True),
            patch("pathlib.Path.unlink") as mock_unlink,
            patch("subprocess.run") as mock_subp,
        ):
            success, msg = self.controller.remove_service()
            self.assertTrue(success)
            self.assertIn("removed", msg.lower())
            mock_unlink.assert_called_once()

            commands = [call[0][0] for call in mock_subp.call_args_list]
            self.assertIn(["systemctl", "stop", "omen-fan-control.service"], commands)
            self.assertIn(["systemctl", "disable", "omen-fan-control.service"], commands)
            self.assertIn(["systemctl", "daemon-reload"], commands)

    def test_is_service_installed_checks_multiple_systemd_paths(self) -> None:
        with patch("pathlib.Path.exists", return_value=True):
            self.assertTrue(FanController.is_service_installed())

        def check_path(path_obj: Path) -> bool:
            return "/usr/lib/systemd/system" in str(path_obj)

        with patch("pathlib.Path.exists", autospec=True, side_effect=check_path):
            self.assertTrue(FanController.is_service_installed())

    def test_is_service_running_evaluates_systemctl_status(self) -> None:
        mock_res = MagicMock()
        mock_res.stdout = "active\n"
        with patch("subprocess.run", return_value=mock_res):
            self.assertTrue(FanController.is_service_running())

        mock_res.stdout = "inactive\n"
        with patch("subprocess.run", return_value=mock_res):
            self.assertFalse(FanController.is_service_running())

    def test_service_control_commands_call_systemctl(self) -> None:
        with patch("subprocess.run") as mock_subp:
            self.assertTrue(self.controller.start_service()[0])
            self.assertTrue(self.controller.stop_service()[0])
            self.assertTrue(self.controller.restart_service()[0])

            commands = [call[0][0] for call in mock_subp.call_args_list]
            self.assertIn(["systemctl", "start", "omen-fan-control.service"], commands)
            self.assertIn(["systemctl", "stop", "omen-fan-control.service"], commands)
            self.assertIn(["systemctl", "restart", "omen-fan-control.service"], commands)


class TestDaemonServiceLifecycle(unittest.TestCase):
    def test_daemon_handles_sigterm_gracefully(self) -> None:
        import os
        import signal
        from click.testing import CliRunner
        from omen_fan_control._cli.serve import serve

        controller = FanController.__new__(FanController)
        controller.config = {"mode": "auto", "ma_window": 5}
        controller.config_path = Path("/mock/config.json")

        def _raise_sigterm(seconds: float) -> None:
            os.kill(os.getpid(), signal.SIGTERM)

        with (
            patch("omen_fan_control._cli.serve.get_controller", return_value=controller),
            patch("time.sleep", side_effect=_raise_sigterm),
            patch.object(controller, "get_reference_temp", return_value=45.0),
            patch.object(controller, "is_reverse_mode_active", return_value=False),
        ):
            runner = CliRunner()
            result = runner.invoke(serve)
            self.assertEqual(result.exit_code, 0)
            self.assertIn("Stopping daemon...", result.output)

    def test_daemon_picks_up_persisted_mode_change(self) -> None:
        from click.testing import CliRunner
        from omen_fan_control._cli.serve import serve

        controller = FanController.__new__(FanController)
        controller.config = {"mode": "auto", "ma_window": 5}
        fake_config_path = MagicMock(spec=Path)
        fake_config_path.exists.return_value = True
        fake_config_path.stat.return_value.st_mtime = 100.0
        controller.config_path = fake_config_path

        iterations = 0

        def _fake_sleep(seconds: float) -> None:
            nonlocal iterations
            iterations += 1
            if iterations == 1:
                # Update config to simulate user updating mode to performance
                controller.config = {"mode": "performance", "ma_window": 5}
                fake_config_path.stat.return_value.st_mtime = 200.0
            elif iterations >= 2:
                raise KeyboardInterrupt()

        with (
            patch("omen_fan_control._cli.serve.get_controller", return_value=controller),
            patch("time.sleep", side_effect=_fake_sleep),
            patch.object(controller, "get_reference_temp", return_value=50.0),
            patch.object(controller, "is_reverse_mode_active", return_value=False),
            patch.object(controller, "calculate_dual_target_pwm", return_value=(100, 100)),
            patch.object(controller, "set_fan_pwm") as mock_set_pwm,
            patch.object(controller, "get_board_name", return_value="8C78"),
            patch.object(FanController, "fan_control_available", new_callable=PropertyMock, return_value=True),
        ):
            runner = CliRunner()
            result = runner.invoke(serve)
            self.assertEqual(result.exit_code, 0)
            self.assertGreaterEqual(mock_set_pwm.call_count, 1)


if __name__ == "__main__":
    unittest.main()
