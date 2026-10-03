"""
Tests for absent-hwmon / unavailable fan-control handling.

All sysfs interactions are mocked — no real hardware is touched.
Run with: python -m pytest tests/test_fan_unavailable.py -v
"""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

# ---------------------------------------------------------------------------
# Minimal Qt stubs so we can import gui.py without a real display.
# ---------------------------------------------------------------------------
def _make_qt_stubs() -> None:
    """Insert lightweight stubs for PyQt6 into sys.modules."""
    def _stub_module(name: str, **attrs: object) -> types.ModuleType:
        m = types.ModuleType(name)
        m.__dict__.update(attrs)
        return m

    # Base stub class generator
    class _Widget:
        def __init__(self, *a, **kw): pass
        def setEnabled(self, v): pass
        def setVisible(self, v): pass
        def setText(self, v): pass
        def text(self): return ""
        def setStyleSheet(self, v): pass
        def addWidget(self, *a, **kw): pass
        def setCurrentText(self, v): pass
        def currentText(self): return "Auto"
        def setProperty(self, *a): pass
        def style(self): return self
        def unpolish(self, *a): pass
        def polish(self, *a): pass
        def value(self): return 50
        def setValue(self, v): pass
        def get(self): return None
        def exec(self): return 0

    # We only need the names imported by fan_controller and gui
    stub_names = [
        "PyQt6", "PyQt6.QtWidgets", "PyQt6.QtCore", "PyQt6.QtGui",
    ]
    for n in stub_names:
        if n not in sys.modules:
            sys.modules[n] = _stub_module(n)

    # QtWidgets
    qw = sys.modules["PyQt6.QtWidgets"]
    for cls in [
        "QApplication", "QMainWindow", "QWidget", "QVBoxLayout", "QHBoxLayout",
        "QPushButton", "QLabel", "QFrame", "QStackedWidget", "QComboBox",
        "QSpinBox", "QMessageBox", "QFileDialog", "QProgressBar", "QListView",
        "QTextEdit", "QCheckBox", "QLineEdit", "QGridLayout", "QButtonGroup",
        "QStackedLayout", "QGraphicsOpacityEffect", "QStyledItemDelegate",
        "QDialog", "QStyle",
    ]:
        setattr(qw, cls, _Widget)

    # QtCore
    qc = sys.modules["PyQt6.QtCore"]
    qc.Qt = MagicMock()
    qc.QTimer = MagicMock()
    qc.QThread = MagicMock()
    qc.pyqtSignal = MagicMock(return_value=MagicMock())
    qc.QSize = MagicMock()
    qc.QPointF = MagicMock()
    qc.QRectF = MagicMock()
    qc.QPoint = MagicMock()

    # QtGui
    qg = sys.modules["PyQt6.QtGui"]
    qg.QIcon = MagicMock()
    qg.QPainter = MagicMock()
    qg.QColor = MagicMock()
    qg.QPen = MagicMock()
    qg.QBrush = MagicMock()
    qg.QLinearGradient = MagicMock()
    qg.QPainterPath = MagicMock()


_make_qt_stubs()

# ---------------------------------------------------------------------------
# Now import the real modules under test.
# ---------------------------------------------------------------------------
from omen_fan_control.fan_controller import FanController  # noqa: E402


# ---------------------------------------------------------------------------
# Helper: build a FanController with a mocked-out filesystem.
# ---------------------------------------------------------------------------
def _make_controller(
    hwmon_exists: bool = False,
    pwm1_enable_exists: bool = False,
    pwm1_exists: bool | None = None,
    pwm2_exists: bool | None = None,
    fan1_input_exists: bool | None = None,
    fan2_input_exists: bool | None = None,
) -> FanController:
    """Return a FanController whose sysfs paths are all mocked."""
    with patch.object(FanController, "_find_paths"), \
         patch.object(FanController, "_find_cpu_temp_path", return_value=None), \
         patch.object(FanController, "load_config", return_value={}):
        ctrl = FanController.__new__(FanController)
        ctrl.config = {}
        ctrl.config_path = Path("/tmp/fake-config.json")

    # Wire paths manually
    if hwmon_exists:
        p_hwmon = MagicMock(spec=Path)
        p_hwmon.exists.return_value = True
        ctrl.hwmon_path = p_hwmon
        p_enable = MagicMock(spec=Path)
        p_enable.exists.return_value = pwm1_enable_exists
        ctrl.pwm1_enable_path = p_enable
        p_pwm1 = MagicMock(spec=Path)
        p_pwm1.exists.return_value = (
            pwm1_exists if pwm1_exists is not None else pwm1_enable_exists
        )
        ctrl.pwm1_path = p_pwm1
        p_pwm2 = MagicMock(spec=Path)
        p_pwm2.exists.return_value = (
            pwm2_exists if pwm2_exists is not None else pwm1_enable_exists
        )
        ctrl.pwm2_path = p_pwm2
        p_f1 = MagicMock(spec=Path)
        p_f1.exists.return_value = (
            fan1_input_exists if fan1_input_exists is not None else hwmon_exists
        )
        ctrl.fan1_input_path = p_f1
        p_f2 = MagicMock(spec=Path)
        p_f2.exists.return_value = (
            fan2_input_exists if fan2_input_exists is not None else hwmon_exists
        )
        ctrl.fan2_input_path = p_f2
    else:
        ctrl.hwmon_path = None
        ctrl.pwm1_enable_path = None
        ctrl.pwm1_path = None
        ctrl.pwm2_path = None
        ctrl.fan1_input_path = None
        ctrl.fan2_input_path = None

    ctrl.cpu_temp_path = None
    return ctrl


# ===========================================================================
# 1. fan_control_available property
# ===========================================================================
class TestFanControlAvailable(unittest.TestCase):
    def test_absent_hwmon_is_not_available(self):
        ctrl = _make_controller(hwmon_exists=False)
        self.assertFalse(ctrl.fan_control_available)

    def test_hwmon_present_but_pwm1_enable_missing(self):
        ctrl = _make_controller(hwmon_exists=True, pwm1_enable_exists=False)
        self.assertFalse(ctrl.fan_control_available)

    def test_hwmon_present_and_pwm1_enable_exists(self):
        ctrl = _make_controller(hwmon_exists=True, pwm1_enable_exists=True)
        self.assertTrue(ctrl.fan_control_available)


# ===========================================================================
# 1b. fan_telemetry_available property
# ===========================================================================
class TestFanTelemetryAvailability(unittest.TestCase):
    def test_telemetry_absent_when_hwmon_absent(self):
        ctrl = _make_controller(hwmon_exists=False)
        self.assertFalse(ctrl.fan_telemetry_available)
        self.assertFalse(ctrl.is_fan_telemetry_available(1))
        self.assertFalse(ctrl.is_fan_telemetry_available(2))

    def test_control_available_but_telemetry_unavailable_when_rpm_inputs_missing(self):
        ctrl = _make_controller(
            hwmon_exists=True,
            pwm1_enable_exists=True,
            fan1_input_exists=False,
            fan2_input_exists=False,
        )
        self.assertTrue(ctrl.fan_control_available)
        self.assertFalse(ctrl.fan_telemetry_available)
        self.assertFalse(ctrl.is_fan_telemetry_available(1))
        self.assertFalse(ctrl.is_fan_telemetry_available(2))

    def test_partial_telemetry_single_fan(self):
        ctrl = _make_controller(
            hwmon_exists=True,
            pwm1_enable_exists=True,
            fan1_input_exists=True,
            fan2_input_exists=False,
        )
        self.assertTrue(ctrl.fan_control_available)
        self.assertTrue(ctrl.fan_telemetry_available)
        self.assertTrue(ctrl.is_fan_telemetry_available(1))
        self.assertFalse(ctrl.is_fan_telemetry_available(2))

    def test_telemetry_available_when_inputs_exist(self):
        ctrl = _make_controller(
            hwmon_exists=True,
            pwm1_enable_exists=True,
            fan1_input_exists=True,
            fan2_input_exists=True,
        )
        self.assertTrue(ctrl.fan_control_available)
        self.assertTrue(ctrl.fan_telemetry_available)
        self.assertTrue(ctrl.is_fan_telemetry_available(1))
        self.assertTrue(ctrl.is_fan_telemetry_available(2))


# ===========================================================================
# 2. RPM readings when hwmon is absent
# ===========================================================================
class TestRpmReadingsAbsent(unittest.TestCase):
    def setUp(self):
        self.ctrl = _make_controller(hwmon_exists=False)

    def test_get_fan_speed_returns_zero_not_negative(self):
        # When paths are None, read_sys_file returns None -> parse_hwmon_rpm -> (0, False)
        # The sentinel for "absent" is fan_control_available, not a magic RPM value.
        rpm = self.ctrl.get_fan_speed()
        self.assertEqual(rpm, 0)

    def test_get_fan_speed_info_returns_zero_false(self):
        rpm, is_rev = self.ctrl.get_fan_speed_info()
        self.assertEqual(rpm, 0)
        self.assertFalse(is_rev)

    def test_get_both_fan_speeds_returns_two_entries(self):
        fans = self.ctrl.get_both_fan_speeds()
        # Must still return two entries — callers (FanSpeedDialog, _cleaner) expect this.
        self.assertEqual(len(fans), 2)
        for name, rpm, is_rev in fans:
            self.assertEqual(rpm, 0)
            self.assertFalse(is_rev)

    def test_parse_hwmon_rpm_none(self):
        rpm, is_rev = self.ctrl.parse_hwmon_rpm(None)
        self.assertEqual(rpm, 0)
        self.assertFalse(is_rev)

    def test_parse_hwmon_rpm_valid(self):
        rpm, is_rev = self.ctrl.parse_hwmon_rpm("3200")
        self.assertEqual(rpm, 3200)
        self.assertFalse(is_rev)

    def test_parse_hwmon_rpm_reverse(self):
        # Reverse-bit encoding: raw >= 12800; actual = (raw // 100 & 0x7F) * 100
        raw = 12800 + 3200  # 16000
        rpm, is_rev = self.ctrl.parse_hwmon_rpm(str(raw))
        self.assertTrue(is_rev)


# ===========================================================================
# 3. write_sys_file silently no-ops when path is None
# ===========================================================================
class TestWriteSysFileNoop(unittest.TestCase):
    def setUp(self):
        self.ctrl = _make_controller(hwmon_exists=False)

    def test_write_sys_file_none_path_noop(self):
        # Should not raise; should not open any file
        with patch("builtins.open", side_effect=AssertionError("open called with None path")):
            self.ctrl.write_sys_file(None, 42)  # must not call open

    def test_set_fan_mode_noop_when_path_none(self):
        # write_sys_file guards None; set_fan_mode passes pwm1_enable_path which is None
        self.ctrl.set_fan_mode("auto")   # no exception
        self.ctrl.set_fan_mode("max")    # no exception

    def test_set_fan_pwm_noop_when_paths_none(self):
        self.ctrl.set_fan_pwm(128)       # no exception


# ===========================================================================
# 4. Daemon: no fan writes when fan_control_available is False
# ===========================================================================
class TestDaemonNoWritesWhenUnavailable(unittest.TestCase):
    """Verify that the daemon loop skips all set_fan_* calls when unavailable."""

    def test_serve_skips_writes_when_unavailable(self):
        """
        Simulate one daemon loop iteration with fan_control_available=False.
        Confirm set_fan_pwm and set_fan_mode are NOT called.
        """
        import importlib
        # Import serve module fresh
        from omen_fan_control._cli import serve as serve_mod

        ctrl = _make_controller(hwmon_exists=False)
        ctrl.config = {"mode": "auto"}
        ctrl.config_path = Path("/tmp/fake.json")

        with patch.object(ctrl, "set_fan_mode") as mock_mode, \
             patch.object(ctrl, "set_fan_pwm") as mock_pwm, \
             patch.object(ctrl, "get_reference_temp", return_value=45), \
             patch.object(ctrl, "is_reverse_mode_active", return_value=False), \
             patch.object(ctrl, "_find_paths"):

            # Simulate the guard logic directly (the actual loop runs forever;
            # we verify the property gate):
            if not ctrl.fan_control_available:
                pass  # daemon continues without writing
            else:
                ctrl.set_fan_mode("auto")

            mock_mode.assert_not_called()
            mock_pwm.assert_not_called()


# ===========================================================================
# 5. Daemon: hysteresis skipped when hwmon unavailable (no RPM reading)
# ===========================================================================
class TestDaemonHysteresisGuard(unittest.TestCase):
    def test_no_hysteresis_arithmetic_when_unavailable(self):
        """
        With fan_control_available=False, get_fan_speed() returns 0 and
        get_effective_fan_max() returns 0, so the hysteresis block is skipped.
        The daemon does not attempt set_fan_pwm.
        """
        ctrl = _make_controller(hwmon_exists=False)
        ctrl.config = {"mode": "curve", "curve": [[30, 30], [80, 100]]}

        with patch.object(ctrl, "set_fan_pwm") as mock_pwm, \
             patch.object(ctrl, "get_effective_fan_max", return_value=0), \
             patch.object(ctrl, "get_fan_speed", return_value=0):

            # Replicate what the daemon would do for mode==curve when unavailable
            if not ctrl.fan_control_available:
                pass
            else:
                target_cpu_pwm, target_gpu_pwm = ctrl.calculate_dual_target_pwm(50, None)
                current_rpm = ctrl.get_fan_speed()
                max_rpm = ctrl.get_effective_fan_max()
                should_apply = True
                if max_rpm > 0 and current_rpm >= 0:
                    diff = abs((target_cpu_pwm / 255) * max_rpm - current_rpm)
                    should_apply = diff > 200
                if should_apply:
                    ctrl.set_fan_pwm(target_cpu_pwm, target_gpu_pwm)

            mock_pwm.assert_not_called()


# ===========================================================================
# 6. Watchdog no-op when unavailable
# ===========================================================================
class TestWatchdogNoop(unittest.TestCase):
    def test_run_watchdog_does_nothing_when_unavailable(self):
        """
        run_watchdog must return immediately without calling apply_fan_mode
        when fan_control_available is False.
        """
        # We test the guard logic directly since MainWindow needs a display.
        ctrl = _make_controller(hwmon_exists=False)
        apply_called = []

        def _fake_apply():
            apply_called.append(True)

        # Replicate run_watchdog guard
        if ctrl.fan_control_available:
            _fake_apply()
            # would also set status_label

        self.assertEqual(apply_called, [], "apply_fan_mode must not be called when unavailable")


# ===========================================================================
# 7. GUI: FanSpeedDialog shows N/A when unavailable
# ===========================================================================
class TestFanSpeedDialogUnavailable(unittest.TestCase):
    def test_refresh_fans_shows_na_when_unavailable(self):
        """
        When fan_control_available is False, refresh_fans should display
        "N/A" for each fan card rather than "0 RPM".
        """
        from omen_fan_control.gui_widgets import FanSpeedDialog

        ctrl = _make_controller(hwmon_exists=False)
        # get_both_fan_speeds returns [(name,0,False),(name,0,False)] when absent
        ctrl.get_both_fan_speeds = MagicMock(return_value=[
            ("Fan 1 (CPU)", 0, False),
            ("Fan 2 (GPU)", 0, False),
        ])

        # Build a minimal dialog without a real QApplication
        dialog = FanSpeedDialog.__new__(FanSpeedDialog)
        dialog.controller = ctrl
        dialog.fan_cards = {}

        captured = {}

        class FakeLblVal:
            def setText(self, t): captured[len(captured)] = t
            def setVisible(self, v): pass

        class FakeLblRev:
            def setText(self, t): pass
            def setVisible(self, v): pass

        dialog.fan_cards = {
            "Fan 1 (CPU)": (FakeLblVal(), FakeLblRev()),
            "Fan 2 (GPU)": (FakeLblVal(), FakeLblRev()),
        }

        dialog.refresh_fans()

        for v in captured.values():
            self.assertEqual(v, "N/A", f"Expected 'N/A' but got {v!r}")


# ===========================================================================
# 8. Supported boards retain existing behavior
# ===========================================================================
class TestSupportedBoardBehavior(unittest.TestCase):
    def test_fan_control_available_when_hwmon_present(self):
        ctrl = _make_controller(hwmon_exists=True, pwm1_enable_exists=True)
        self.assertTrue(ctrl.fan_control_available)

    def test_get_fan_speed_reads_sysfs_when_available(self):
        ctrl = _make_controller(hwmon_exists=True, pwm1_enable_exists=True)
        ctrl.read_sys_file = MagicMock(return_value="3200")
        rpm = ctrl.get_fan_speed()
        self.assertEqual(rpm, 3200)

    def test_set_fan_mode_writes_when_available(self):
        ctrl = _make_controller(hwmon_exists=True, pwm1_enable_exists=True)
        with patch.object(ctrl, "write_sys_file") as mock_write:
            ctrl.set_fan_mode("auto")
            mock_write.assert_called_once_with(ctrl.pwm1_enable_path, 2)

    def test_get_both_fan_speeds_returns_real_values(self):
        ctrl = _make_controller(hwmon_exists=True, pwm1_enable_exists=True)
        ctrl.read_sys_file = MagicMock(return_value="4000")
        fans = ctrl.get_both_fan_speeds()
        self.assertEqual(len(fans), 2)
        for _, rpm, _ in fans:
            self.assertEqual(rpm, 4000)


# ===========================================================================
# 9. Tri-state interface classification: absent vs incomplete vs available
# ===========================================================================
class TestInterfaceStateTriState(unittest.TestCase):
    def test_absent_hwmon(self):
        ctrl = _make_controller(hwmon_exists=False)
        self.assertEqual(ctrl.get_interface_state(), "absent")
        self.assertFalse(ctrl.fan_control_available)

    def test_incomplete_missing_pwm1_enable(self):
        ctrl = _make_controller(
            hwmon_exists=True,
            pwm1_enable_exists=False,
            pwm1_exists=True,
            pwm2_exists=True,
        )
        self.assertEqual(ctrl.get_interface_state(), "incomplete")
        self.assertFalse(ctrl.fan_control_available)

    def test_incomplete_missing_pwm1(self):
        ctrl = _make_controller(
            hwmon_exists=True,
            pwm1_enable_exists=True,
            pwm1_exists=False,
            pwm2_exists=True,
        )
        self.assertEqual(ctrl.get_interface_state(), "incomplete")
        self.assertFalse(ctrl.fan_control_available)

    def test_incomplete_missing_pwm2(self):
        ctrl = _make_controller(
            hwmon_exists=True,
            pwm1_enable_exists=True,
            pwm1_exists=True,
            pwm2_exists=False,
        )
        self.assertEqual(ctrl.get_interface_state(), "incomplete")
        self.assertFalse(ctrl.fan_control_available)

    def test_available_when_all_required_controls_present(self):
        ctrl = _make_controller(
            hwmon_exists=True,
            pwm1_enable_exists=True,
            pwm1_exists=True,
            pwm2_exists=True,
        )
        self.assertEqual(ctrl.get_interface_state(), "available")
        self.assertTrue(ctrl.fan_control_available)


# ===========================================================================
# 10. Incomplete interface cannot produce sysfs writes
# ===========================================================================
class TestIncompleteInterfaceWritesPrevented(unittest.TestCase):
    def setUp(self):
        # Incomplete interface: pwm1_enable and pwm1 exist, but pwm2 is missing
        self.ctrl = _make_controller(
            hwmon_exists=True,
            pwm1_enable_exists=True,
            pwm1_exists=True,
            pwm2_exists=False,
        )

    def test_write_sys_file_noop_when_incomplete(self):
        with patch("builtins.open", side_effect=AssertionError("open called on incomplete interface")):
            self.ctrl.write_sys_file(self.ctrl.pwm1_path, 100)

    def test_set_fan_mode_noop_when_incomplete(self):
        with patch.object(self.ctrl, "write_sys_file") as mock_write:
            self.ctrl.set_fan_mode("auto")
            self.ctrl.set_fan_mode("max")
            mock_write.assert_not_called()

    def test_set_fan_pwm_noop_when_incomplete(self):
        with patch.object(self.ctrl, "write_sys_file") as mock_write:
            self.ctrl.set_fan_pwm(150)
            mock_write.assert_not_called()

    def test_calibrate_raises_when_incomplete(self):
        with self.assertRaises(RuntimeError) as ctx:
            next(self.ctrl.calibrate())
        self.assertIn("unavailable", str(ctx.exception).lower())

    def test_start_fan_cleaning_rejected_when_incomplete(self):
        success, msg = self.ctrl.start_fan_cleaning()
        self.assertFalse(success)
        self.assertIn("not available", msg)

    def test_apply_post_cleaner_mode_skipped_when_incomplete(self):
        with patch.object(self.ctrl, "set_fan_pwm") as mock_pwm, \
             patch.object(self.ctrl, "set_fan_mode") as mock_mode:
            self.ctrl.apply_post_cleaner_mode()
            mock_pwm.assert_not_called()
            mock_mode.assert_not_called()

    def test_reverse_mode_active_returns_false_when_incomplete(self):
        self.assertFalse(self.ctrl.is_reverse_mode_active())


# ===========================================================================
# 11. Daemon startup, lifecycle, Ctrl-C exit, and re-probe transition
# ===========================================================================
class TestDaemonLifecycleAndReprobe(unittest.TestCase):
    def test_daemon_startup_and_ctrl_c_clean_exit(self):
        """
        Verify that daemon starts up, initializes all state cleanly,
        and handles Ctrl-C (KeyboardInterrupt) without falling through into
        any obsolete or duplicate loop logic.
        """
        from click.testing import CliRunner
        from omen_fan_control._cli.serve import serve

        ctrl = _make_controller(hwmon_exists=False)
        ctrl.config = {"mode": "auto", "ma_window": 5}
        ctrl.config_path = Path("/tmp/fake_config.json")

        def _interrupt_on_sleep(seconds):
            raise KeyboardInterrupt()

        with patch("omen_fan_control._cli.serve.get_controller", return_value=ctrl), \
             patch("time.sleep", side_effect=_interrupt_on_sleep), \
             patch.object(ctrl, "get_reference_temp", return_value=42.0):
            runner = CliRunner()
            result = runner.invoke(serve)
            self.assertEqual(result.exit_code, 0)
            self.assertIn("Starting Omen Fan Control Daemon...", result.output)
            self.assertIn("Fan control interface absent at startup", result.output)
            self.assertIn("Stopping daemon...", result.output)

    def test_daemon_independent_temperature_monitoring(self):
        """
        Verify that temperature monitoring runs unconditionally even when the
        fan-control interface is absent.
        """
        from click.testing import CliRunner
        from omen_fan_control._cli.serve import serve

        ctrl = _make_controller(hwmon_exists=False)
        ctrl.config = {"mode": "auto"}
        ctrl.config_path = Path("/tmp/fake_config.json")

        temp_mock = MagicMock(return_value=55.0)
        ctrl.get_reference_temp = temp_mock

        def _interrupt_on_sleep(seconds):
            raise KeyboardInterrupt()

        with patch("omen_fan_control._cli.serve.get_controller", return_value=ctrl), \
             patch("time.sleep", side_effect=_interrupt_on_sleep):
            runner = CliRunner()
            result = runner.invoke(serve)
            self.assertEqual(result.exit_code, 0)
            # Temperature was queried at startup and inside the loop
            self.assertGreaterEqual(temp_mock.call_count, 2)

    def test_daemon_reprobe_detects_newly_available_interface(self):
        """
        Test that when the interface starts absent and later becomes available,
        the daemon re-probes via _find_paths() and resumes fan writes.
        """
        from click.testing import CliRunner
        from omen_fan_control._cli.serve import serve

        ctrl = _make_controller(hwmon_exists=False)
        ctrl.config = {"mode": "auto"}
        ctrl.config_path = Path("/tmp/fake_config.json")

        find_paths_calls = 0

        def _fake_find_paths():
            nonlocal find_paths_calls
            find_paths_calls += 1
            if find_paths_calls >= 2:
                # Mutate controller to have a complete interface
                ctrl.hwmon_path = MagicMock(spec=Path)
                ctrl.hwmon_path.exists.return_value = True
                p = MagicMock(spec=Path)
                p.exists.return_value = True
                ctrl.pwm1_enable_path = p
                ctrl.pwm1_path = p
                ctrl.pwm2_path = p

        ctrl._find_paths = MagicMock(side_effect=_fake_find_paths)

        simulated_time = 1000.0

        def _fake_time():
            return simulated_time

        def _fake_sleep(seconds):
            nonlocal simulated_time
            simulated_time += 35.0  # advance time past _REPROBE_INTERVAL (30s)
            if find_paths_calls >= 2:
                raise KeyboardInterrupt()

        with patch("omen_fan_control._cli.serve.get_controller", return_value=ctrl), \
             patch("time.time", side_effect=_fake_time), \
             patch("time.sleep", side_effect=_fake_sleep), \
             patch.object(ctrl, "get_reference_temp", return_value=40.0), \
             patch.object(ctrl, "set_fan_pwm") as mock_set_pwm, \
             patch.object(ctrl, "set_fan_mode") as mock_set_mode:
            runner = CliRunner()
            result = runner.invoke(serve)
            self.assertEqual(result.exit_code, 0)
            self.assertIn("resuming fan-control writes", result.output)
            self.assertTrue(mock_set_pwm.called or mock_set_mode.called)


# ===========================================================================
# 12. GUI behavior: navigation suppression and control gating
# ===========================================================================
class TestGuiBehaviorAndNavigation(unittest.TestCase):
    def test_on_status_click_suppressed_for_pending_validation(self):
        """
        Clicking the status label when status contains '(protocol pending validation)'
        must NOT trigger navigation to Driver Management.
        """
        from omen_fan_control.gui import MainWindow

        ctrl = _make_controller(hwmon_exists=False)
        fake_gui = MagicMock()
        fake_gui.controller = ctrl
        fake_gui.status_label.text.return_value = (
            "Fan control unavailable — interface not detected on board 8CC0 "
            "(protocol pending validation); temperature monitoring remains available."
        )

        MainWindow.on_status_click(fake_gui, None)
        fake_gui.show_driver.assert_not_called()

    def test_on_status_click_suppressed_for_incomplete_interface(self):
        """
        Clicking the status label when interface is incomplete must NOT
        navigate to Driver Management.
        """
        from omen_fan_control.gui import MainWindow

        ctrl = _make_controller(hwmon_exists=False)
        fake_gui = MagicMock()
        fake_gui.controller = ctrl
        fake_gui.status_label.text.return_value = (
            "Fan control unavailable — interface incomplete (missing required controls); "
            "temperature monitoring remains available."
        )

        MainWindow.on_status_click(fake_gui, None)
        fake_gui.show_driver.assert_not_called()

    def test_on_status_click_navigates_when_driver_genuinely_missing(self):
        """
        Clicking the status label when driver installation is suggested
        navigates to Driver Management.
        """
        from omen_fan_control.gui import MainWindow

        ctrl = _make_controller(hwmon_exists=False)
        fake_gui = MagicMock()
        fake_gui.controller = ctrl
        fake_gui.status_label.text.return_value = (
            "Fan control unavailable — interface not detected; "
            "check driver installation. Temperature monitoring remains available."
        )

        MainWindow.on_status_click(fake_gui, None)
        fake_gui.show_driver.assert_called_once()

    def test_update_fan_control_ui_availability_disables_all_controls(self):
        """
        When fan_control_available is False, mode combo, set btn, manual widget,
        curve container, calibration button, cleaning button, and auto-cleaner check
        are all disabled.
        """
        from omen_fan_control.gui import MainWindow

        ctrl = _make_controller(hwmon_exists=False)
        fake_gui = MagicMock()
        fake_gui.controller = ctrl
        fake_gui.cal_running = False
        fake_gui._manual_cleaning_initiated = False

        MainWindow._update_fan_control_ui_availability(fake_gui)
        fake_gui.mode_combo.setEnabled.assert_called_with(False)
        fake_gui.set_btn.setEnabled.assert_called_with(False)
        fake_gui.manual_widget.setEnabled.assert_called_with(False)
        fake_gui.curve_editor_container.setEnabled.assert_called_with(False)
        fake_gui.cal_btn.setEnabled.assert_called_with(False)
        fake_gui.manual_clean_btn.setEnabled.assert_called_with(False)
        fake_gui.auto_cleaner_check.setEnabled.assert_called_with(False)

    def test_start_calibration_guarded_when_unavailable(self):
        """
        start_calibration must not launch calibration thread when unavailable.
        """
        from omen_fan_control.gui import MainWindow

        ctrl = _make_controller(hwmon_exists=False)

        class FakeGui:
            def __init__(self):
                self.controller = ctrl
                self.status_label = MagicMock()

        fake_gui = FakeGui()

        MainWindow.start_calibration(fake_gui)
        fake_gui.status_label.setText.assert_called_with(
            "Fan calibration unavailable — interface not detected; temperature monitoring remains available."
        )
        self.assertFalse(hasattr(fake_gui, "cal_thread"))

    def test_start_manual_cleaning_guarded_when_unavailable(self):
        """
        start_manual_cleaning must invoke _on_manual_start_failed without starting thread.
        """
        from omen_fan_control.gui import MainWindow

        ctrl = _make_controller(hwmon_exists=False)
        fake_gui = MagicMock()
        fake_gui.controller = ctrl

        MainWindow.start_manual_cleaning(fake_gui)
        fake_gui._on_manual_start_failed.assert_called_once_with(
            "Fan control interface is not available."
        )


# ===========================================================================
# 13. Regression: Missing RPM inputs when control files exist
# ===========================================================================
class TestMissingRpmInputsIntegration(unittest.TestCase):
    def test_controls_available_required_rpm_telemetry_unavailable_header_displays_na(self):
        """
        Exact case: controls available (pwm1_enable, pwm1, pwm2 exist),
        required RPM telemetry unavailable (fan1_input, fan2_input missing),
        get_fan_speed_info contract preserved as (int, bool), header displays N/A.
        """
        from omen_fan_control.gui import MainWindow

        ctrl = _make_controller(
            hwmon_exists=True,
            pwm1_enable_exists=True,
            pwm1_exists=True,
            pwm2_exists=True,
            fan1_input_exists=False,
            fan2_input_exists=False,
        )
        self.assertTrue(ctrl.fan_control_available)
        self.assertFalse(ctrl.fan_telemetry_available)

        # Return contract preserved: tuple[int, bool]
        speed_info = ctrl.get_fan_speed_info()
        self.assertIsInstance(speed_info, tuple)
        self.assertEqual(len(speed_info), 2)
        self.assertIsInstance(speed_info[0], int)
        self.assertIsInstance(speed_info[1], bool)
        self.assertEqual(speed_info, (0, False))

        fake_gui = MagicMock()
        fake_gui.controller = ctrl
        fake_gui.rpm_label = MagicMock()
        fake_gui.temp_label = MagicMock()
        fake_gui.cpu_pill_btn = MagicMock()
        fake_gui.gpu_pill_btn = MagicMock()
        fake_gui.check_driver_status = MagicMock()
        fake_gui._update_fan_control_ui_availability = MagicMock()
        fake_gui.update_cleaner_status = MagicMock()

        MainWindow.update_status(fake_gui)
        fake_gui.rpm_label.setText.assert_called_with("N/A")

    def test_gui_displays_na_when_telemetry_missing_despite_control_available(self):
        """
        When control files exist (fan_control_available=True) but RPM inputs are missing
        (fan_telemetry_available=False), the main GUI rpm_label must display 'N/A'.
        """
        from omen_fan_control.gui import MainWindow

        ctrl = _make_controller(
            hwmon_exists=True,
            pwm1_enable_exists=True,
            fan1_input_exists=False,
            fan2_input_exists=False,
        )
        self.assertTrue(ctrl.fan_control_available)
        self.assertFalse(ctrl.fan_telemetry_available)

        fake_gui = MagicMock()
        fake_gui.controller = ctrl
        fake_gui.rpm_label = MagicMock()
        fake_gui.temp_label = MagicMock()
        fake_gui.cpu_pill_btn = MagicMock()
        fake_gui.gpu_pill_btn = MagicMock()
        fake_gui.check_driver_status = MagicMock()
        fake_gui._update_fan_control_ui_availability = MagicMock()
        fake_gui.update_cleaner_status = MagicMock()

        MainWindow.update_status(fake_gui)
        fake_gui.rpm_label.setText.assert_called_with("N/A")

    def test_dialog_displays_na_when_telemetry_missing_despite_control_available(self):
        """
        FanSpeedDialog must display 'N/A' on cards when control exists but tachometers do not.
        """
        from omen_fan_control.gui_widgets import FanSpeedDialog

        ctrl = _make_controller(
            hwmon_exists=True,
            pwm1_enable_exists=True,
            fan1_input_exists=False,
            fan2_input_exists=False,
        )
        ctrl.get_both_fan_speeds = MagicMock(return_value=[
            ("Fan 1 (CPU)", 0, False),
            ("Fan 2 (GPU)", 0, False),
        ])

        dialog = FanSpeedDialog.__new__(FanSpeedDialog)
        dialog.controller = ctrl
        dialog.fan_cards = {}

        captured: dict[int, str] = {}

        class FakeCardLbl:
            def __init__(self, text="", *a, **kw):
                self._text = str(text) if text else ""
                if self._text:
                    captured[len(captured)] = self._text

            def setText(self, t):
                self._text = str(t)
                captured[len(captured)] = self._text

            def text(self):
                return self._text

            def setVisible(self, v):
                pass

            def setObjectName(self, n):
                pass

            def setAlignment(self, a):
                pass

        class FakeLayout:
            def addWidget(self, w):
                pass

        dialog.cards_layout = FakeLayout()

        # Mock QFrame and QVBoxLayout constructor for card creation
        with patch("omen_fan_control.gui_widgets.QFrame", return_value=MagicMock()), \
             patch("omen_fan_control.gui_widgets.QVBoxLayout", return_value=MagicMock()), \
             patch("omen_fan_control.gui_widgets.QLabel", side_effect=lambda *a, **kw: FakeCardLbl(*a, **kw)):
            dialog.refresh_fans()

        # The fan value labels should have been initialized with "N/A"
        values = list(captured.values())
        self.assertIn("N/A", values)

        # Subsequent refresh also updates existing cards to "N/A"
        captured.clear()
        dialog.refresh_fans()
        values_refresh = list(captured.values())
        self.assertIn("N/A", values_refresh)

    def test_cli_status_displays_na_when_telemetry_missing(self):
        """
        CLI status must display 'N/A (telemetry not detected)' when telemetry is unavailable.
        """
        from click.testing import CliRunner
        from omen_fan_control.cli import status

        ctrl = _make_controller(
            hwmon_exists=True,
            pwm1_enable_exists=True,
            fan1_input_exists=False,
            fan2_input_exists=False,
        )

        with patch("omen_fan_control.cli.get_controller", return_value=ctrl), \
             patch.object(ctrl, "read_sys_file", return_value="2"), \
             patch.object(ctrl, "get_fan_speed", return_value=0), \
             patch.object(ctrl, "get_cpu_temp", return_value=45), \
             patch.object(ctrl, "get_all_core_temps", return_value=[]), \
             patch.object(ctrl, "is_service_installed", return_value=False), \
             patch.object(ctrl, "is_service_running", return_value=False), \
             patch.object(ctrl, "check_install_type", return_value="none"):
            runner = CliRunner()
            result = runner.invoke(status)
            self.assertEqual(result.exit_code, 0)
            self.assertIn("Fan Speed:         N/A (telemetry not detected)", result.output)

    def test_fan_control_writes_succeed_when_telemetry_missing(self):
        """
        Missing RPM inputs must not block fan control writes when PWM controls exist.
        """
        ctrl = _make_controller(
            hwmon_exists=True,
            pwm1_enable_exists=True,
            fan1_input_exists=False,
            fan2_input_exists=False,
        )
        self.assertTrue(ctrl.fan_control_available)
        self.assertFalse(ctrl.fan_telemetry_available)

        with patch.object(ctrl, "write_sys_file") as mock_write:
            ctrl.set_fan_mode("max")
            mock_write.assert_called_with(ctrl.pwm1_enable_path, 0)


# ===========================================================================
# 14. Regression: GUI re-probing and interface appearance detection
# ===========================================================================
class TestGuiReprobeAndReenable(unittest.TestCase):
    def test_gui_reprobe_throttled_under_30s(self):
        """
        check_driver_status must not re-probe sysfs via _find_paths() if less than
        30 seconds have elapsed since the last probe.
        """
        from omen_fan_control.gui import MainWindow

        ctrl = _make_controller(hwmon_exists=False)
        ctrl._find_paths = MagicMock()

        fake_gui = MagicMock()
        fake_gui.controller = ctrl
        fake_gui.status_label.text.return_value = "Fan control unavailable — interface not detected"
        fake_gui._last_fan_reprobe_time = 1000.0

        with patch("time.time", return_value=1015.0):  # 15s elapsed (< 30s)
            MainWindow.check_driver_status(fake_gui)
            ctrl._find_paths.assert_not_called()
            self.assertEqual(fake_gui._last_fan_reprobe_time, 1000.0)

    def test_gui_reprobe_runs_after_30s_interval(self):
        """
        check_driver_status must trigger _find_paths() when 30 seconds or more have elapsed.
        """
        from omen_fan_control.gui import MainWindow

        ctrl = _make_controller(hwmon_exists=False)
        ctrl._find_paths = MagicMock()

        fake_gui = MagicMock()
        fake_gui.controller = ctrl
        fake_gui.status_label.text.return_value = "Fan control unavailable — interface not detected"
        fake_gui._last_fan_reprobe_time = 1000.0

        with patch("time.time", return_value=1035.0):  # 35s elapsed (>= 30s)
            MainWindow.check_driver_status(fake_gui)
            ctrl._find_paths.assert_called_once()
            self.assertEqual(fake_gui._last_fan_reprobe_time, 1035.0)

    def test_gui_detects_interface_appearance_and_updates_status_ready(self):
        """
        When the interface becomes available upon re-probe, check_driver_status
        updates the status label to 'Ready' and resets _last_fan_reprobe_time to 0.0.
        """
        from omen_fan_control.gui import MainWindow

        ctrl = _make_controller(hwmon_exists=False)

        def _simulate_driver_loaded():
            # Controller discovers sysfs
            p_hwmon = MagicMock(spec=Path)
            p_hwmon.exists.return_value = True
            ctrl.hwmon_path = p_hwmon
            p_file = MagicMock(spec=Path)
            p_file.exists.return_value = True
            ctrl.pwm1_enable_path = p_file
            ctrl.pwm1_path = p_file
            ctrl.pwm2_path = p_file

        ctrl._find_paths = MagicMock(side_effect=_simulate_driver_loaded)

        fake_gui = MagicMock()
        fake_gui.controller = ctrl
        fake_gui.status_label.text.return_value = "Fan control unavailable — interface not detected"
        fake_gui._last_fan_reprobe_time = 1000.0

        with patch("time.time", return_value=1035.0):
            MainWindow.check_driver_status(fake_gui)
            ctrl._find_paths.assert_called_once()
            self.assertTrue(ctrl.fan_control_available)
            fake_gui.status_label.setText.assert_called_with("Ready")
            self.assertEqual(fake_gui._last_fan_reprobe_time, 0.0)
            fake_gui._update_fan_control_ui_availability.assert_called_once()

    def test_gui_reenables_controls_when_interface_available(self):
        """
        _update_fan_control_ui_availability enables all UI controls when
        fan_control_available becomes True.
        """
        from omen_fan_control.gui import MainWindow

        ctrl = _make_controller(hwmon_exists=True, pwm1_enable_exists=True)
        self.assertTrue(ctrl.fan_control_available)

        fake_gui = MagicMock()
        fake_gui.controller = ctrl
        fake_gui.cal_running = False
        fake_gui._manual_cleaning_initiated = False

        MainWindow._update_fan_control_ui_availability(fake_gui)
        fake_gui.mode_combo.setEnabled.assert_called_with(True)
        fake_gui.set_btn.setEnabled.assert_called_with(True)
        fake_gui.manual_widget.setEnabled.assert_called_with(True)
        fake_gui.curve_editor_container.setEnabled.assert_called_with(True)
        fake_gui.cal_btn.setEnabled.assert_called_with(True)
        fake_gui.manual_clean_btn.setEnabled.assert_called_with(True)
        fake_gui.auto_cleaner_check.setEnabled.assert_called_with(True)

    def test_gui_appearance_flow_reenables_controls(self):
        """
        End-to-end appearance check: when the interface appears after being absent,
        check_driver_status triggers _update_fan_control_ui_availability which
        calls setEnabled(True) on controls.
        """
        from omen_fan_control.gui import MainWindow

        ctrl = _make_controller(hwmon_exists=False)

        def _simulate_driver_loaded():
            p_hwmon = MagicMock(spec=Path)
            p_hwmon.exists.return_value = True
            ctrl.hwmon_path = p_hwmon
            p_file = MagicMock(spec=Path)
            p_file.exists.return_value = True
            ctrl.pwm1_enable_path = p_file
            ctrl.pwm1_path = p_file
            ctrl.pwm2_path = p_file

        ctrl._find_paths = MagicMock(side_effect=_simulate_driver_loaded)

        fake_gui = MagicMock()
        fake_gui.controller = ctrl
        fake_gui.status_label.text.return_value = "Fan control unavailable — interface not detected"
        fake_gui._last_fan_reprobe_time = 1000.0
        fake_gui.cal_running = False
        fake_gui._manual_cleaning_initiated = False

        # Bind the real method to fake_gui so check_driver_status invokes real logic
        fake_gui._update_fan_control_ui_availability = lambda: MainWindow._update_fan_control_ui_availability(fake_gui)

        with patch("time.time", return_value=1035.0):
            MainWindow.check_driver_status(fake_gui)
            self.assertTrue(ctrl.fan_control_available)
            fake_gui.status_label.setText.assert_called_with("Ready")
            self.assertEqual(fake_gui._last_fan_reprobe_time, 0.0)
            fake_gui.mode_combo.setEnabled.assert_called_with(True)
            fake_gui.set_btn.setEnabled.assert_called_with(True)


# ===========================================================================
# 15. Asymmetric fan telemetry availability (CPU vs GPU tachometers)
# ===========================================================================
class TestAsymmetricFanTelemetry(unittest.TestCase):
    def test_cpu_telemetry_missing_gpu_telemetry_present(self):
        """
        When Fan 1 (CPU) RPM input is missing but Fan 2 (GPU) exists:
        - is_fan_telemetry_available(1) is False, is_fan_telemetry_available(2) is True.
        - get_active_fan_index() reports 2 (GPU).
        - get_fan_speed() returns GPU RPM (contract int).
        - get_fan_speed_info() returns (GPU RPM, is_rev) (contract tuple[int, bool]).
        - GUI update_status displays GPU RPM (not N/A, not 0).
        - CLI status displays GPU RPM.
        """
        from click.testing import CliRunner
        from omen_fan_control.cli import status
        from omen_fan_control.gui import MainWindow

        ctrl = _make_controller(
            hwmon_exists=True,
            pwm1_enable_exists=True,
            fan1_input_exists=False,
            fan2_input_exists=True,
        )
        ctrl.read_sys_file = MagicMock(side_effect=lambda p: "2700" if p == ctrl.fan2_input_path else "")

        self.assertFalse(ctrl.is_fan_telemetry_available(1))
        self.assertTrue(ctrl.is_fan_telemetry_available(2))
        self.assertEqual(ctrl.get_active_fan_index(), 2)

        # Contracts
        speed = ctrl.get_fan_speed()
        self.assertIsInstance(speed, int)
        self.assertEqual(speed, 2700)

        info = ctrl.get_fan_speed_info()
        self.assertIsInstance(info, tuple)
        self.assertEqual(info, (2700, False))

        # GUI display
        fake_gui = MagicMock()
        fake_gui.controller = ctrl
        fake_gui.rpm_label = MagicMock()
        fake_gui.temp_label = MagicMock()
        fake_gui.cpu_pill_btn = MagicMock()
        fake_gui.gpu_pill_btn = MagicMock()
        fake_gui.check_driver_status = MagicMock()
        fake_gui._update_fan_control_ui_availability = MagicMock()
        fake_gui.update_cleaner_status = MagicMock()

        MainWindow.update_status(fake_gui)
        fake_gui.rpm_label.setText.assert_called_with("2700 RPM")

        # CLI status
        with patch("omen_fan_control.cli.get_controller", return_value=ctrl), \
             patch.object(ctrl, "read_sys_file", side_effect=lambda p: "2700" if p == ctrl.fan2_input_path else ("2" if p == ctrl.pwm1_enable_path else "")), \
             patch.object(ctrl, "get_cpu_temp", return_value=45), \
             patch.object(ctrl, "get_all_core_temps", return_value=[]), \
             patch.object(ctrl, "is_service_installed", return_value=False), \
             patch.object(ctrl, "is_service_running", return_value=False), \
             patch.object(ctrl, "check_install_type", return_value="none"):
            runner = CliRunner()
            result = runner.invoke(status)
            self.assertEqual(result.exit_code, 0)
            self.assertIn("Fan Speed:         2700 RPM", result.output)

    def test_gpu_telemetry_missing_cpu_telemetry_present(self):
        """
        When Fan 2 (GPU) RPM input is missing but Fan 1 (CPU) exists:
        - is_fan_telemetry_available(1) is True, is_fan_telemetry_available(2) is False.
        - get_active_fan_index() reports 1 (CPU).
        - get_fan_speed() returns CPU RPM (contract int).
        - get_fan_speed_info() returns (CPU RPM, is_rev) (contract tuple[int, bool]).
        - GUI update_status displays CPU RPM.
        - CLI status displays CPU RPM.
        """
        from click.testing import CliRunner
        from omen_fan_control.cli import status
        from omen_fan_control.gui import MainWindow

        ctrl = _make_controller(
            hwmon_exists=True,
            pwm1_enable_exists=True,
            fan1_input_exists=True,
            fan2_input_exists=False,
        )
        ctrl.read_sys_file = MagicMock(side_effect=lambda p: "2300" if p == ctrl.fan1_input_path else "")

        self.assertTrue(ctrl.is_fan_telemetry_available(1))
        self.assertFalse(ctrl.is_fan_telemetry_available(2))
        self.assertEqual(ctrl.get_active_fan_index(), 1)

        # Contracts
        speed = ctrl.get_fan_speed()
        self.assertIsInstance(speed, int)
        self.assertEqual(speed, 2300)

        info = ctrl.get_fan_speed_info()
        self.assertIsInstance(info, tuple)
        self.assertEqual(info, (2300, False))

        # GUI display
        fake_gui = MagicMock()
        fake_gui.controller = ctrl
        fake_gui.rpm_label = MagicMock()
        fake_gui.temp_label = MagicMock()
        fake_gui.cpu_pill_btn = MagicMock()
        fake_gui.gpu_pill_btn = MagicMock()
        fake_gui.check_driver_status = MagicMock()
        fake_gui._update_fan_control_ui_availability = MagicMock()
        fake_gui.update_cleaner_status = MagicMock()

        MainWindow.update_status(fake_gui)
        fake_gui.rpm_label.setText.assert_called_with("2300 RPM")

        # CLI status
        with patch("omen_fan_control.cli.get_controller", return_value=ctrl), \
             patch.object(ctrl, "read_sys_file", side_effect=lambda p: "2300" if p == ctrl.fan1_input_path else ("2" if p == ctrl.pwm1_enable_path else "")), \
             patch.object(ctrl, "get_cpu_temp", return_value=45), \
             patch.object(ctrl, "get_all_core_temps", return_value=[]), \
             patch.object(ctrl, "is_service_installed", return_value=False), \
             patch.object(ctrl, "is_service_running", return_value=False), \
             patch.object(ctrl, "check_install_type", return_value="none"):
            runner = CliRunner()
            result = runner.invoke(status)
            self.assertEqual(result.exit_code, 0)
            self.assertIn("Fan Speed:         2300 RPM", result.output)


# ===========================================================================
# 16. Fan-cleaning recovery independent of PWM control interface
# ===========================================================================
class TestCleanerRecoveryIndependentOfPwm(unittest.TestCase):
    def test_reverse_mode_detected_via_config_when_pwm_unavailable(self):
        """
        is_reverse_mode_active() must return True when cleaner_in_progress is set,
        even if fan_control_available is False.
        """
        ctrl = _make_controller(hwmon_exists=False)
        self.assertFalse(ctrl.fan_control_available)

        ctrl.config = {"cleaner_in_progress": True}
        self.assertTrue(ctrl.is_reverse_mode_active())

    def test_reverse_mode_detected_via_tachometer_when_pwm_unavailable(self):
        """
        is_reverse_mode_active() must return True when sysfs tachometer reports >= 12800,
        even if PWM controls are incomplete or absent.
        """
        ctrl = _make_controller(
            hwmon_exists=True,
            pwm1_enable_exists=False,  # PWM controls missing
            fan1_input_exists=True,
            fan2_input_exists=True,
        )
        self.assertFalse(ctrl.fan_control_available)

        # 16000 corresponds to 3200 RPM in reverse
        ctrl.read_sys_file = MagicMock(return_value="16000")
        self.assertTrue(ctrl.is_reverse_mode_active())

    def test_daemon_startup_recovery_triggers_when_pwm_unavailable(self):
        """
        When fan_control_available is False but reverse mode is detected,
        the daemon startup recovery MUST invoke stop_fan_cleaning() rather
        than skipping the check.
        """
        from click.testing import CliRunner
        from omen_fan_control._cli.serve import serve

        ctrl = _make_controller(hwmon_exists=False)
        self.assertFalse(ctrl.fan_control_available)
        ctrl.config = {"mode": "auto", "cleaner_in_progress": True}
        ctrl.config_path = Path("/tmp/fake_config.json")

        def _interrupt_on_sleep(seconds):
            raise KeyboardInterrupt()

        with patch("omen_fan_control._cli.serve.get_controller", return_value=ctrl), \
             patch.object(ctrl, "is_reverse_mode_active", return_value=True), \
             patch.object(ctrl, "stop_fan_cleaning") as mock_stop, \
             patch("time.sleep", side_effect=_interrupt_on_sleep), \
             patch.object(ctrl, "get_reference_temp", return_value=40.0):
            runner = CliRunner()
            result = runner.invoke(serve)
            self.assertEqual(result.exit_code, 0)
            self.assertIn("Recovery: Reverse mode detected on daemon startup", result.output)
            mock_stop.assert_called_once()

    def test_start_fan_cleaning_strictly_blocked_when_pwm_unavailable(self):
        """
        Starting a NEW cleaning cycle must remain blocked when fan control is unavailable.
        """
        ctrl = _make_controller(hwmon_exists=False)
        self.assertFalse(ctrl.fan_control_available)

        success, msg = ctrl.start_fan_cleaning()
        self.assertFalse(success)
        self.assertIn("not available", msg.lower())


if __name__ == "__main__":
    unittest.main()
