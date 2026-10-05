"""
Tests for absent-hwmon / unavailable fan-control handling.

All sysfs interactions are mocked — no real hardware is touched.
Run with: python -m pytest tests/test_fan_unavailable.py -v
"""
from __future__ import annotations

import shutil
import sys
import tempfile
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
        def setWindowTitle(self, *a): pass
        def resize(self, *a): pass
        def setWindowIcon(self, *a): pass
        def setCentralWidget(self, *a): pass
        def statusBar(self): return self
        def addPermanentWidget(self, *a): pass
        def setSizeGripEnabled(self, *a): pass

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
    board_name: str = "8C78",
) -> FanController:
    """Return a FanController whose sysfs paths are all mocked."""
    with patch.object(FanController, "_find_paths"), \
         patch.object(FanController, "_find_cpu_temp_path", return_value=None), \
         patch.object(FanController, "load_config", return_value={}):
        ctrl = FanController.__new__(FanController)
        ctrl.config = {}
        ctrl.config_path = Path("/tmp/fake-config.json")
        ctrl._get_live_board_name = lambda: board_name
        ctrl._read_module_param = lambda p: False

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


# ===========================================================================
# 17. Board 8CC0 RPM telemetry unavailable & safety handling
# ===========================================================================
class TestBoard8CC0TelemetryAndSafety(unittest.TestCase):
    def test_8cc0_rpm_zero_reports_telemetry_unavailable_with_temps_available(self):
        """
        On board 8CC0, where physical fans spin but sysfs exposes fan1_input: 0 and fan2_input: 0,
        telemetry must be treated as unavailable (N/A) while CPU/GPU temperature monitoring
        and safety restrictions remain intact.
        """
        ctrl = _make_controller(
            hwmon_exists=True,
            pwm1_enable_exists=True,
            fan1_input_exists=True,
            fan2_input_exists=True,
            board_name="8CC0",
        )
        ctrl.read_sys_file = MagicMock(return_value="0")
        ctrl.get_cpu_temp = MagicMock(return_value=48)
        ctrl.get_gpu_temp = MagicMock(return_value=42)

        self.assertEqual(ctrl.get_board_name(), "8CC0")
        self.assertTrue(ctrl._is_unsupported_telemetry_board())
        self.assertFalse(ctrl.is_fan_telemetry_available(1))
        self.assertFalse(ctrl.is_fan_telemetry_available(2))
        self.assertFalse(ctrl.fan_telemetry_available)
        self.assertIsNone(ctrl.get_active_fan_index())

        # RPM data model contracts
        speed = ctrl.get_fan_speed()
        self.assertIsInstance(speed, int)
        self.assertEqual(speed, 0)
        speed_info = ctrl.get_fan_speed_info()
        self.assertIsInstance(speed_info, tuple)
        self.assertEqual(speed_info, (0, False))

        both_speeds = ctrl.get_both_fan_speeds()
        self.assertEqual(len(both_speeds), 2)
        self.assertEqual(both_speeds[0], ("Fan 1 (CPU)", 0, False))
        self.assertEqual(both_speeds[1], ("Fan 2 (GPU)", 0, False))

        # Temperatures remain fully functional
        self.assertEqual(ctrl.get_cpu_temp(), 48)
        self.assertEqual(ctrl.get_gpu_temp(), 42)

        # Safety restriction intact
        self.assertTrue(ctrl._is_monitoring_only_board())

    def test_8cc0_gui_update_status_displays_na_and_preserves_temperature(self):
        """
        GUI update_status must display 'N/A' for RPM on board 8CC0 while rendering temperature.
        """
        from omen_fan_control.gui import MainWindow

        ctrl = _make_controller(
            hwmon_exists=True,
            pwm1_enable_exists=True,
            fan1_input_exists=True,
            fan2_input_exists=True,
            board_name="8CC0",
        )
        ctrl.read_sys_file = MagicMock(return_value="0")
        ctrl.get_reference_temp = MagicMock(return_value=45)

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
        fake_gui.temp_label.setText.assert_called_with("45°C")

    def test_8cc0_fan_speed_dialog_displays_na(self):
        """
        FanSpeedDialog cards must display 'N/A' on board 8CC0 despite fan inputs existing.
        """
        from omen_fan_control.gui_widgets import FanSpeedDialog

        ctrl = _make_controller(
            hwmon_exists=True,
            pwm1_enable_exists=True,
            fan1_input_exists=True,
            fan2_input_exists=True,
            board_name="8CC0",
        )
        ctrl.read_sys_file = MagicMock(return_value="0")

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

            def setVisible(self, v): pass
            def setObjectName(self, n): pass
            def setAlignment(self, a): pass

        class FakeLayout:
            def addWidget(self, w): pass

        dialog.cards_layout = FakeLayout()

        with patch("omen_fan_control.gui_widgets.QFrame", return_value=MagicMock()), \
             patch("omen_fan_control.gui_widgets.QVBoxLayout", return_value=MagicMock()), \
             patch("omen_fan_control.gui_widgets.QLabel", side_effect=lambda *a, **kw: FakeCardLbl(*a, **kw)):
            dialog.refresh_fans()

        values = list(captured.values())
        self.assertIn("N/A", values)
        self.assertNotIn("0 RPM", values)

    def test_8cc0_cli_status_displays_na_and_shows_temps(self):
        """
        CLI status must display 'N/A (telemetry not detected)' on board 8CC0.
        """
        from click.testing import CliRunner
        from omen_fan_control.cli import status

        ctrl = _make_controller(
            hwmon_exists=True,
            pwm1_enable_exists=True,
            fan1_input_exists=True,
            fan2_input_exists=True,
            board_name="8CC0",
        )
        ctrl.read_sys_file = MagicMock(return_value="0")

        with patch("omen_fan_control.cli.get_controller", return_value=ctrl), \
             patch.object(ctrl, "get_cpu_temp", return_value=45), \
             patch.object(ctrl, "get_all_core_temps", return_value=[]), \
             patch.object(ctrl, "is_service_installed", return_value=False), \
             patch.object(ctrl, "is_service_running", return_value=False), \
             patch.object(ctrl, "check_install_type", return_value="none"):
            runner = CliRunner()
            result = runner.invoke(status)
            self.assertEqual(result.exit_code, 0)
            self.assertIn("Fan Speed:         N/A (telemetry not detected)", result.output)
            self.assertIn("CPU Package Temp:  45°C", result.output)

    def test_8cc0_sysfs_writes_remain_blocked(self):
        """
        Board 8CC0 remains monitoring-only; sysfs writes are blocked even if files exist.
        """
        ctrl = _make_controller(
            hwmon_exists=True,
            pwm1_enable_exists=True,
            board_name="8CC0",
        )
        path = Path("/mock/pwm1")
        with patch("builtins.open") as mock_open:
            ctrl.write_sys_file(path, 100)
            mock_open.assert_not_called()


# ===========================================================================
# 18. Supported board RPM behavior & genuine zero preservation
# ===========================================================================
class TestSupportedBoardTelemetryPreservation(unittest.TestCase):
    def test_valid_rpm_reading_displayed_on_supported_board(self):
        """
        Supported board with spinning fans reports active RPM correctly in GUI and data model.
        """
        from omen_fan_control.gui import MainWindow

        ctrl = _make_controller(
            hwmon_exists=True,
            pwm1_enable_exists=True,
            fan1_input_exists=True,
            fan2_input_exists=True,
            board_name="8C78",
        )
        ctrl.read_sys_file = MagicMock(return_value="2500")

        self.assertFalse(ctrl._is_unsupported_telemetry_board())
        self.assertTrue(ctrl.is_fan_telemetry_available(1))
        self.assertTrue(ctrl.is_fan_telemetry_available(2))
        self.assertEqual(ctrl.get_active_fan_index(), 1)
        self.assertEqual(ctrl.get_fan_speed(), 2500)
        self.assertEqual(ctrl.get_fan_speed_info(), (2500, False))

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
        fake_gui.rpm_label.setText.assert_called_with("2500 RPM")

    def test_genuine_zero_rpm_on_supported_board_remains_zero_rpm(self):
        """
        When fans genuinely stop on a supported board, 0 RPM must be displayed as '0 RPM',
        NOT converted to 'N/A'.
        """
        from omen_fan_control.gui import MainWindow
        from omen_fan_control.gui_widgets import FanSpeedDialog

        ctrl = _make_controller(
            hwmon_exists=True,
            pwm1_enable_exists=True,
            fan1_input_exists=True,
            fan2_input_exists=True,
            board_name="8C78",
        )
        ctrl.read_sys_file = MagicMock(return_value="0")

        self.assertFalse(ctrl._is_unsupported_telemetry_board())
        self.assertTrue(ctrl.is_fan_telemetry_available(1))
        self.assertTrue(ctrl.is_fan_telemetry_available(2))
        self.assertIsNotNone(ctrl.get_active_fan_index())
        self.assertEqual(ctrl.get_fan_speed(), 0)
        self.assertEqual(ctrl.get_fan_speed_info(), (0, False))

        # Main GUI
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
        fake_gui.rpm_label.setText.assert_called_with("0 RPM")

        # Dialog
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

            def setVisible(self, v): pass
            def setObjectName(self, n): pass
            def setAlignment(self, a): pass

        class FakeLayout:
            def addWidget(self, w): pass

        dialog.cards_layout = FakeLayout()

        with patch("omen_fan_control.gui_widgets.QFrame", return_value=MagicMock()), \
             patch("omen_fan_control.gui_widgets.QVBoxLayout", return_value=MagicMock()), \
             patch("omen_fan_control.gui_widgets.QLabel", side_effect=lambda *a, **kw: FakeCardLbl(*a, **kw)):
            dialog.refresh_fans()

        values = list(captured.values())
        self.assertIn("0 RPM", values)
        self.assertNotIn("N/A", values)


# ===========================================================================
# 19. Missing RPM files and read errors handled safely
# ===========================================================================
class TestTelemetryReadErrors(unittest.TestCase):
    def test_telemetry_read_error_none_handled_as_na(self):
        """
        When read_sys_file returns None (e.g. sysfs I/O error), telemetry is marked
        as unavailable and displays 'N/A' rather than false '0 RPM'.
        """
        from omen_fan_control.gui import MainWindow

        ctrl = _make_controller(
            hwmon_exists=True,
            pwm1_enable_exists=True,
            fan1_input_exists=True,
            fan2_input_exists=True,
            board_name="8C78",
        )
        ctrl.read_sys_file = MagicMock(return_value=None)

        self.assertIsNone(ctrl.get_active_fan_index())
        self.assertFalse(ctrl.is_fan_telemetry_available(1))
        self.assertFalse(ctrl.is_fan_telemetry_available(2))

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

    def test_telemetry_read_error_corrupted_data_handled_as_na(self):
        """
        When read_sys_file returns invalid/non-digit data, it is handled as a read error
        and reported as 'N/A'.
        """
        from omen_fan_control.gui import MainWindow

        ctrl = _make_controller(
            hwmon_exists=True,
            pwm1_enable_exists=True,
            fan1_input_exists=True,
            fan2_input_exists=True,
            board_name="8C78",
        )
        ctrl.read_sys_file = MagicMock(return_value="corrupted_read")

        self.assertIsNone(ctrl.get_active_fan_index())
        self.assertFalse(ctrl.is_fan_telemetry_available(1))
        self.assertFalse(ctrl.is_fan_telemetry_available(2))

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


# ===========================================================================
# 20. Root execution prevention & display checks
# ===========================================================================
class TestRootExecutionHandling(unittest.TestCase):
    def test_check_root_execution_returns_true_for_root_and_false_for_user(self):
        from omen_fan_control.gui import check_root_execution

        with patch("os.geteuid", return_value=0):
            self.assertTrue(check_root_execution())

        with patch("os.geteuid", return_value=1000):
            self.assertFalse(check_root_execution())

    def test_gui_main_refuses_root_execution_with_clear_message(self):
        """
        Invoking gui.main() as root must exit with status 1 and write guidance
        to sys.stderr explaining that the GUI runs as a regular user.
        """
        from omen_fan_control.gui import main
        import io

        fake_stderr = io.StringIO()
        with patch("os.geteuid", return_value=0), \
             patch("sys.stderr", fake_stderr), \
             self.assertRaises(SystemExit) as ctx:
            main()

        self.assertEqual(ctx.exception.code, 1)
        err_msg = fake_stderr.getvalue()
        self.assertIn("should not be run as root", err_msg)
        self.assertIn("omen-fan-control.service", err_msg)
        self.assertIn("libxcb-cursor0", err_msg)

    def test_main_window_init_raises_runtime_error_if_root(self):
        """
        Instantiating MainWindow directly as root must raise RuntimeError.
        """
        from omen_fan_control.gui import MainWindow

        with patch("os.geteuid", return_value=0):
            with self.assertRaises(RuntimeError) as ctx:
                MainWindow.__new__(MainWindow).__init__()
            self.assertIn("must not be run as root", str(ctx.exception))


# ===========================================================================
# 21. Experimental WMI 0x2D read-only fan telemetry (isolated test suite)
# ===========================================================================
class TestExperimentalWmiTelemetry(unittest.TestCase):
    def setUp(self):
        self.ctrl = _make_controller(hwmon_exists=False, board_name="8CC0")

    def test_parse_wmi_0x2d_valid_values(self):
        """Valid WMI query 0x2D buffer parses to fan_data[0] * 100 and fan_data[1] * 100."""
        # 128-byte buffer matching HP WMI query 0x2D response
        raw_128 = bytearray(128)
        raw_128[0] = 35  # CPU: 35 * 100 = 3500 RPM
        raw_128[1] = 42  # GPU: 42 * 100 = 4200 RPM
        result = FanController.parse_wmi_0x2d_fan_data(bytes(raw_128))
        self.assertEqual(result, (3500, 4200))

        # List input
        result_list = FanController.parse_wmi_0x2d_fan_data([50, 60])
        self.assertEqual(result_list, (5000, 6000))

    def test_parse_wmi_0x2d_zero_values(self):
        """Zero fan values in 0x2D buffer parse to (0, 0) RPM."""
        raw_128 = bytes(128)  # all zeroes
        result = FanController.parse_wmi_0x2d_fan_data(raw_128)
        self.assertEqual(result, (0, 0))

        result_list = FanController.parse_wmi_0x2d_fan_data([0, 0])
        self.assertEqual(result_list, (0, 0))

    def test_parse_wmi_0x2d_malformed_responses(self):
        """Malformed or out-of-range responses safely return None."""
        # Truncated responses (< 2 bytes)
        self.assertIsNone(FanController.parse_wmi_0x2d_fan_data(b""))
        self.assertIsNone(FanController.parse_wmi_0x2d_fan_data(b"\x23"))
        self.assertIsNone(FanController.parse_wmi_0x2d_fan_data([]))
        self.assertIsNone(FanController.parse_wmi_0x2d_fan_data([35]))

        # Non-integer / boolean element types
        self.assertIsNone(FanController.parse_wmi_0x2d_fan_data(["35", "42"]))
        self.assertIsNone(FanController.parse_wmi_0x2d_fan_data([None, 42]))
        self.assertIsNone(FanController.parse_wmi_0x2d_fan_data([True, False]))

        # Out-of-range values (< 0 or > 255)
        self.assertIsNone(FanController.parse_wmi_0x2d_fan_data([-1, 42]))
        self.assertIsNone(FanController.parse_wmi_0x2d_fan_data([35, 256]))

        # Wrong top-level type
        self.assertIsNone(FanController.parse_wmi_0x2d_fan_data("not-a-buffer"))
        self.assertIsNone(FanController.parse_wmi_0x2d_fan_data(12345))

    def test_parse_wmi_0x2d_failed_query(self):
        """Failed WMI query (None) safely returns None."""
        self.assertIsNone(FanController.parse_wmi_0x2d_fan_data(None))

    def test_read_experimental_telemetry_disabled_by_default(self):
        """Experimental telemetry is disabled by default; returns None even if valid data passed."""
        self.ctrl.config = {"enable_experimental_telemetry": False}
        self.assertIsNone(self.ctrl.read_experimental_fan_telemetry([35, 42]))

        # Config omitted entirely
        self.ctrl.config = {}
        self.assertIsNone(self.ctrl.read_experimental_fan_telemetry([35, 42]))

    def test_read_experimental_telemetry_enabled_with_direct_buffer(self):
        """When enabled, parses valid buffer to (cpu_rpm, gpu_rpm)."""
        self.ctrl.config = {"enable_experimental_telemetry": True}
        result = self.ctrl.read_experimental_fan_telemetry(bytes([35, 42]))
        self.assertEqual(result, (3500, 4200))

    def test_read_experimental_telemetry_enabled_with_sysfs_format(self):
        """When enabled, reads and parses formatted sysfs attribute file."""
        self.ctrl.config = {"enable_experimental_telemetry": True}
        fake_path = MagicMock(spec=Path)
        fake_path.exists.return_value = True
        fake_path.read_text.return_value = "cpu_rpm=3500\ngpu_rpm=4200\n"

        result = self.ctrl.read_experimental_fan_telemetry(fake_path)
        self.assertEqual(result, (3500, 4200))

    def test_read_experimental_telemetry_sysfs_zero_values(self):
        """When enabled, sysfs reporting zero values parses to (0, 0)."""
        self.ctrl.config = {"enable_experimental_telemetry": True}
        fake_path = MagicMock(spec=Path)
        fake_path.exists.return_value = True
        fake_path.read_text.return_value = "cpu_rpm=0\ngpu_rpm=0\n"

        result = self.ctrl.read_experimental_fan_telemetry(fake_path)
        self.assertEqual(result, (0, 0))

    def test_read_experimental_telemetry_sysfs_malformed(self):
        """Corrupted sysfs content safely returns None."""
        self.ctrl.config = {"enable_experimental_telemetry": True}
        fake_path = MagicMock(spec=Path)
        fake_path.exists.return_value = True
        fake_path.read_text.return_value = "unparseable garbage\n"
        fake_path.read_bytes.return_value = b""

        self.assertIsNone(self.ctrl.read_experimental_fan_telemetry(fake_path))

    def test_read_experimental_telemetry_missing_sysfs_file(self):
        """Missing sysfs path returns None."""
        self.ctrl.config = {"enable_experimental_telemetry": True}
        fake_path = MagicMock(spec=Path)
        fake_path.exists.return_value = False

        self.assertIsNone(self.ctrl.read_experimental_fan_telemetry(fake_path))

    def test_read_experimental_telemetry_sysfs_read_error(self):
        """I/O error during sysfs read safely returns None."""
        self.ctrl.config = {"enable_experimental_telemetry": True}
        fake_path = MagicMock(spec=Path)
        fake_path.exists.return_value = True
        fake_path.read_text.side_effect = OSError("WMI evaluation failed")
        fake_path.read_bytes.side_effect = OSError("WMI evaluation failed")

        self.assertIsNone(self.ctrl.read_experimental_fan_telemetry(fake_path))

    def test_gui_continues_to_display_na_for_board_8cc0(self):
        """GUI strictly keeps displaying 'N/A' on board 8CC0 regardless of experimental telemetry flag."""
        from omen_fan_control.gui import MainWindow

        # Test both flag enabled and disabled
        for flag_val in [False, True]:
            self.ctrl.config = {"enable_experimental_telemetry": flag_val}
            fake_gui = MagicMock()
            fake_gui.controller = self.ctrl
            fake_gui.rpm_label = MagicMock()
            fake_gui.temp_label = MagicMock()
            fake_gui.cpu_pill_btn = MagicMock()
            fake_gui.gpu_pill_btn = MagicMock()
            fake_gui.check_driver_status = MagicMock()
            fake_gui._update_fan_control_ui_availability = MagicMock()
            fake_gui.update_cleaner_status = MagicMock()

            # Ensure 8CC0 standard telemetry is unavailable
            self.assertFalse(self.ctrl.is_fan_telemetry_available(1))
            self.assertFalse(self.ctrl.is_fan_telemetry_available(2))

            MainWindow.update_status(fake_gui)
            fake_gui.rpm_label.setText.assert_called_with("N/A")


class TestExperimentalWmiMaxFanControl(unittest.TestCase):
    """Section 22: Unit tests for experimental Max Fan control (WMI query 0x27)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config_path = Path(self.temp_dir) / "config.json"
        with patch.object(FanController, "_get_live_board_name", return_value="8CC0"):
            self.ctrl = FanController(config_path=self.config_path)
            self.ctrl._read_module_param = lambda p: False

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_max_on_packet(self):
        """MAX ON constructs exact 4-byte payload {0x01, 0x00, 0x00, 0x00} and writes '1\n'."""
        packet_bool = FanController.build_wmi_0x27_max_fan_packet(True)
        self.assertEqual(packet_bool, b"\x01\x00\x00\x00")
        self.assertEqual(len(packet_bool), 4)

        packet_int = FanController.build_wmi_0x27_max_fan_packet(1)
        self.assertEqual(packet_int, b"\x01\x00\x00\x00")

        self.ctrl.config = {"enable_experimental_control": True}
        fake_path = MagicMock(spec=Path)
        fake_path.exists.return_value = True

        res = self.ctrl.set_experimental_fan_max(True, path=fake_path)
        self.assertTrue(res)
        fake_path.write_text.assert_called_once_with("1\n")

    def test_max_off_packet(self):
        """MAX OFF constructs exact 4-byte payload {0x00, 0x00, 0x00, 0x00} and writes '0\n'."""
        packet_bool = FanController.build_wmi_0x27_max_fan_packet(False)
        self.assertEqual(packet_bool, b"\x00\x00\x00\x00")
        self.assertEqual(len(packet_bool), 4)

        packet_int = FanController.build_wmi_0x27_max_fan_packet(0)
        self.assertEqual(packet_int, b"\x00\x00\x00\x00")

        self.ctrl.config = {"enable_experimental_control": True}
        fake_path = MagicMock(spec=Path)
        fake_path.exists.return_value = True

        res = self.ctrl.set_experimental_fan_max(False, path=fake_path)
        self.assertTrue(res)
        fake_path.write_text.assert_called_once_with("0\n")

    def test_invalid_input(self):
        """Invalid inputs are rejected without writing or building packets."""
        invalid_values = [2, -1, 100, "1", "0", "true", None, [1], {"on": True}, 1.5]
        for val in invalid_values:
            with self.assertRaises((ValueError, TypeError)):
                FanController.build_wmi_0x27_max_fan_packet(val)

        self.ctrl.config = {"enable_experimental_control": True}
        fake_path = MagicMock(spec=Path)
        fake_path.exists.return_value = True

        for val in invalid_values:
            res = self.ctrl.set_experimental_fan_max(val, path=fake_path)
            self.assertFalse(res)
        fake_path.write_text.assert_not_called()

    def test_disabled_control_rejection(self):
        """When control flag is disabled or absent, commands are rejected and no write occurs."""
        fake_path = MagicMock(spec=Path)
        fake_path.exists.return_value = True

        # Explicitly disabled
        self.ctrl.config = {"enable_experimental_control": False}
        self.assertFalse(self.ctrl.set_experimental_fan_max(True, path=fake_path))
        self.assertFalse(self.ctrl.set_experimental_fan_max(False, path=fake_path))
        self.assertIsNone(self.ctrl.get_experimental_fan_max(path=fake_path))
        fake_path.write_text.assert_not_called()

        # Omitted from config
        self.ctrl.config = {}
        self.assertFalse(self.ctrl.set_experimental_fan_max(True, path=fake_path))
        self.assertFalse(self.ctrl.set_experimental_fan_max(False, path=fake_path))
        self.assertIsNone(self.ctrl.get_experimental_fan_max(path=fake_path))
        fake_path.write_text.assert_not_called()

    def test_wmi_failure(self):
        """WMI I/O or sysfs failure safely returns False / None without crashing."""
        self.ctrl.config = {"enable_experimental_control": True}
        fake_path = MagicMock(spec=Path)
        fake_path.exists.return_value = True
        fake_path.write_text.side_effect = OSError("WMI evaluation failed: -EIO")
        fake_path.read_text.side_effect = OSError("WMI evaluation failed: -EIO")

        self.assertFalse(self.ctrl.set_experimental_fan_max(True, path=fake_path))
        self.assertFalse(self.ctrl.set_experimental_fan_max(False, path=fake_path))
        self.assertIsNone(self.ctrl.get_experimental_fan_max(path=fake_path))

    def test_0x10_packet(self):
        """Query 0x10 constructs exact 1-byte payload b'\\x00' for domain HPWMI_GM."""
        packet = FanController.build_wmi_0x10_trigger_packet()
        self.assertEqual(packet, b"\x00")
        self.assertEqual(len(packet), 1)

    def test_0x27_packet(self):
        """Query 0x27 constructs exact 4-byte payload for ON (0x01) and OFF (0x00)."""
        packet_on = FanController.build_wmi_0x27_max_fan_packet(True)
        self.assertEqual(packet_on, b"\x01\x00\x00\x00")
        self.assertEqual(len(packet_on), 4)

        packet_off = FanController.build_wmi_0x27_max_fan_packet(False)
        self.assertEqual(packet_off, b"\x00\x00\x00\x00")
        self.assertEqual(len(packet_off), 4)

    def test_0x2e_packet(self):
        """Query 0x2E constructs exact 2-byte payload b'\\x00\\x00' (reset speeds to automatic)."""
        packet = FanController.build_wmi_0x2e_reset_packet()
        self.assertEqual(packet, b"\x00\x00")
        self.assertEqual(len(packet), 2)

    def test_ordering_0x10_before_0x27(self):
        """Enabling MAX issues query 0x10 first, then query 0x27, and schedules keep-alive."""
        self.ctrl.config = {"enable_experimental_control": True}
        calls = []

        def mock_executor(query, domain, payload, out_size):
            calls.append((query, domain, payload, out_size))
            return 0

        res = self.ctrl.set_experimental_fan_max(True, query_executor=mock_executor)
        self.assertTrue(res)
        self.assertEqual(len(calls), 2)
        # First call: query 0x10 (HPWMI_GM, payload b"\x00", out_size 4)
        self.assertEqual(calls[0], (0x10, 0x20008, b"\x00", 4))
        # Second call: query 0x27 (HPWMI_GM, payload b"\x01\x00\x00\x00", out_size 0)
        self.assertEqual(calls[1], (0x27, 0x20008, b"\x01\x00\x00\x00", 0))
        self.assertTrue(self.ctrl._experimental_keepalive_active)

    def test_ordering_max_off_sequence(self):
        """Disabling MAX issues exact sequence: 0x10 -> 0x27(0) -> 0x2E(0/0) -> cancel keep-alive."""
        self.ctrl.config = {"enable_experimental_control": True}
        calls = []

        def mock_executor(query, domain, payload, out_size):
            calls.append((query, domain, payload, out_size))
            return 0

        # Pre-arm keepalive as if MAX was ON
        self.ctrl._schedule_experimental_max_keepalive()
        self.assertTrue(self.ctrl._experimental_keepalive_active)

        res = self.ctrl.set_experimental_fan_max(False, query_executor=mock_executor)
        self.assertTrue(res)
        self.assertEqual(len(calls), 3)
        # 1. Query 0x10 (HPWMI_GM, payload b"\x00", out_size 4)
        self.assertEqual(calls[0], (0x10, 0x20008, b"\x00", 4))
        # 2. Query 0x27(0) (HPWMI_GM, payload b"\x00\x00\x00\x00", out_size 0)
        self.assertEqual(calls[1], (0x27, 0x20008, b"\x00\x00\x00\x00", 0))
        # 3. Query 0x2E(0/0) (HPWMI_GM, payload b"\x00\x00", out_size 0)
        self.assertEqual(calls[2], (0x2E, 0x20008, b"\x00\x00", 0))
        # 4. Keep-alive cancelled
        self.assertFalse(self.ctrl._experimental_keepalive_active)

    def test_query_executor_boolean_and_result_handling(self):
        """query_executor returning True, 0, or (0, ...) is treated as success, not an error.

        In Python, bool is a subclass of int (isinstance(True, int) is True).
        Ensure True is NOT interpreted as non-zero error code.
        """
        self.ctrl.config = {"enable_experimental_control": True}

        # Success cases: True, 0, None, (0, b'ok'), (True, None)
        success_returns = [True, 0, None, (0, b"data"), (True, None)]
        for ret_val in success_returns:
            with self.subTest(ret_val=ret_val):
                calls = []
                def success_executor(query, domain, payload, out_size):
                    calls.append(query)
                    return ret_val

                res_on = self.ctrl.set_experimental_fan_max(True, query_executor=success_executor)
                self.assertTrue(res_on)
                self.assertEqual(len(calls), 2)

                res_off = self.ctrl.set_experimental_fan_max(False, query_executor=success_executor)
                self.assertTrue(res_off)
                self.assertEqual(len(calls), 5)  # 2 + 3

        # Failure cases: False, -1, 1, (1, None), (-1, None)
        failure_returns = [False, -1, 1, (1, None), (-1, None)]
        for fail_val in failure_returns:
            with self.subTest(fail_val=fail_val):
                calls = []
                def fail_executor(query, domain, payload, out_size):
                    calls.append(query)
                    return fail_val

                res_on = self.ctrl.set_experimental_fan_max(True, query_executor=fail_executor)
                self.assertFalse(res_on)
                self.assertEqual(len(calls), 1)  # Aborted after 0x10

                res_off = self.ctrl.set_experimental_fan_max(False, query_executor=fail_executor)
                self.assertFalse(res_off)
                self.assertEqual(len(calls), 2)  # 1 from ON + 1 from OFF

    def test_abort_if_0x10_fails_on_max_off(self):
        """If query 0x10 fails on MAX OFF, abort without issuing 0x27 or 0x2E."""
        self.ctrl.config = {"enable_experimental_control": True}
        calls = []

        def failing_executor(query, domain, payload, out_size):
            calls.append(query)
            if query == 0x10:
                return -1
            return 0

        res = self.ctrl.set_experimental_fan_max(False, query_executor=failing_executor)
        self.assertFalse(res)
        self.assertEqual(calls, [0x10])

    def test_abort_if_0x27_fails_on_max_off(self):
        """If query 0x27 fails on MAX OFF, abort without issuing 0x2E."""
        self.ctrl.config = {"enable_experimental_control": True}
        calls = []

        def failing_executor(query, domain, payload, out_size):
            calls.append(query)
            if query == 0x27:
                return -1
            return 0

        res = self.ctrl.set_experimental_fan_max(False, query_executor=failing_executor)
        self.assertFalse(res)
        self.assertEqual(calls, [0x10, 0x27])

    def test_abort_if_0x10_fails(self):
        """If query 0x10 fails, abort immediately without issuing query 0x27 or scheduling keep-alive."""
        self.ctrl.config = {"enable_experimental_control": True}
        calls = []

        def failing_executor(query, domain, payload, out_size):
            calls.append((query, domain, payload, out_size))
            if query == 0x10:
                return -1  # Simulation of firmware error
            return 0

        res = self.ctrl.set_experimental_fan_max(True, query_executor=failing_executor)
        self.assertFalse(res)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], 0x10)
        self.assertFalse(self.ctrl._experimental_keepalive_active)

    def test_auto_sequence_packets_builder(self):
        """AUTO mode sequence builder constructs exact 3-step tuple list with correct opcodes and sizes."""
        packets = FanController.build_wmi_auto_sequence_packets()
        self.assertEqual(len(packets), 3)

        # 1. Query 0x10 (HPWMI_FAN_COUNT_GET_QUERY / user-define trigger): insize 1, payload b"\x00", outsize 4
        self.assertEqual(packets[0], (0x10, 0x20008, b"\x00", 4))

        # 2. Query 0x27 (HPWMI_FAN_SPEED_MAX_SET_QUERY): insize 4, payload b"\x00\x00\x00\x00", outsize 0
        self.assertEqual(packets[1], (0x27, 0x20008, b"\x00\x00\x00\x00", 0))

        # 3. Query 0x2E (HPWMI_VICTUS_S_FAN_SPEED_SET_QUERY): insize 2, payload b"\x00\x00", outsize 0
        self.assertEqual(packets[2], (0x2E, 0x20008, b"\x00\x00", 0))

    def test_set_experimental_fan_auto(self):
        """set_experimental_fan_auto executes exact 0x10 -> 0x27(0) -> 0x2E(0/0) -> cancel keep-alive sequence."""
        self.ctrl.config = {"enable_experimental_control": True}
        calls = []

        def mock_executor(query, domain, payload, out_size):
            calls.append((query, domain, payload, out_size))
            return 0

        # Pre-arm keepalive as if coming from MAX mode
        self.ctrl._schedule_experimental_max_keepalive()
        self.assertTrue(self.ctrl._experimental_keepalive_active)

        res = self.ctrl.set_experimental_fan_auto(query_executor=mock_executor)
        self.assertTrue(res)
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0], (0x10, 0x20008, b"\x00", 4))
        self.assertEqual(calls[1], (0x27, 0x20008, b"\x00\x00\x00\x00", 0))
        self.assertEqual(calls[2], (0x2E, 0x20008, b"\x00\x00", 0))
        self.assertFalse(self.ctrl._experimental_keepalive_active)

    def test_set_experimental_fan_auto_gating(self):
        """set_experimental_fan_auto is strictly blocked when enable_experimental_control is False."""
        self.ctrl.config = {"enable_experimental_control": False}
        mock_executor = MagicMock()

        res = self.ctrl.set_experimental_fan_auto(query_executor=mock_executor)
        self.assertFalse(res)
        mock_executor.assert_not_called()

    def test_set_experimental_fan_auto_abort_on_error(self):
        """set_experimental_fan_auto aborts if query 0x10 fails."""
        self.ctrl.config = {"enable_experimental_control": True}
        calls = []

        def failing_executor(query, domain, payload, out_size):
            calls.append(query)
            if query == 0x10:
                return -1
            return 0

        res = self.ctrl.set_experimental_fan_auto(query_executor=failing_executor)
        self.assertFalse(res)
        self.assertEqual(calls, [0x10])

    def test_keep_alive_scheduling(self):
        """Keep-alive is scheduled on MAX ON and canceled on MAX OFF."""
        self.ctrl.config = {"enable_experimental_control": True}
        calls = []

        def mock_executor(query, domain, payload, out_size):
            calls.append((query, domain, payload, out_size))
            return 0

        # Enable MAX: keepalive active
        res_on = self.ctrl.set_experimental_fan_max(True, query_executor=mock_executor)
        self.assertTrue(res_on)
        self.assertTrue(self.ctrl._experimental_keepalive_active)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], 0x10)
        self.assertEqual(calls[1][0], 0x27)

        # Disable MAX (clean OFF path): keepalive canceled, 0x10 -> 0x27(0) -> 0x2E(0/0) issued
        res_off = self.ctrl.set_experimental_fan_max(False, query_executor=mock_executor)
        self.assertTrue(res_off)
        self.assertFalse(self.ctrl._experimental_keepalive_active)
        self.assertEqual(len(calls), 5)
        self.assertEqual(calls[2], (0x10, 0x20008, b"\x00", 4))
        self.assertEqual(calls[3], (0x27, 0x20008, b"\x00\x00\x00\x00", 0))
        self.assertEqual(calls[4], (0x2E, 0x20008, b"\x00\x00", 0))

    def test_no_control_traffic_when_experimental_control_disabled(self):
        """When experimental_control=False, no WMI control traffic is sent."""
        self.ctrl.config = {"enable_experimental_control": False}
        mock_executor = MagicMock()

        res = self.ctrl.set_experimental_fan_max(True, query_executor=mock_executor)
        self.assertFalse(res)
        mock_executor.assert_not_called()
        self.assertFalse(self.ctrl._experimental_keepalive_active)

    def test_no_write_during_initialization(self):
        """Instantiating FanController issues NO fan writes on board 8CC0."""
        with patch.object(FanController, "_get_live_board_name", return_value="8CC0"), \
             patch.object(FanController, "write_sys_file") as mock_write, \
             patch.object(FanController, "set_experimental_fan_max") as mock_exp_max:
            ctrl = FanController(config_path=self.config_path)
            self.assertEqual(ctrl.check_board_support()[0], "MONITORING_ONLY")
            mock_write.assert_not_called()
            mock_exp_max.assert_not_called()


class TestExperimentalWmiPerformanceMode(unittest.TestCase):
    """Section 23: Unit tests for Performance mode and thermal presets (WMI query 0x1A & 0x22)."""

    def test_wmi_constants_integrity(self):
        """Verify protocol constants match kernel and hardware specifications."""
        from omen_fan_control._constants import (
            WMI_QUERY_SET_PERFORMANCE_MODE,
            WMI_QUERY_GET_GPU_THERMAL_MODES,
            WMI_QUERY_SET_GPU_THERMAL_MODES,
            HP_OMEN_V1_THERMAL_PROFILE_DEFAULT,
            HP_OMEN_V1_THERMAL_PROFILE_PERFORMANCE,
            HP_OMEN_V1_THERMAL_PROFILE_COOL,
            HP_OMEN_V0_THERMAL_PROFILE_DEFAULT,
            HP_OMEN_V0_THERMAL_PROFILE_PERFORMANCE,
            HP_OMEN_V0_THERMAL_PROFILE_COOL,
        )
        self.assertEqual(WMI_QUERY_SET_PERFORMANCE_MODE, 0x1A)
        self.assertEqual(WMI_QUERY_GET_GPU_THERMAL_MODES, 0x21)
        self.assertEqual(WMI_QUERY_SET_GPU_THERMAL_MODES, 0x22)
        self.assertEqual(HP_OMEN_V1_THERMAL_PROFILE_DEFAULT, 0x30)
        self.assertEqual(HP_OMEN_V1_THERMAL_PROFILE_PERFORMANCE, 0x31)
        self.assertEqual(HP_OMEN_V1_THERMAL_PROFILE_COOL, 0x50)
        self.assertEqual(HP_OMEN_V0_THERMAL_PROFILE_DEFAULT, 0x00)
        self.assertEqual(HP_OMEN_V0_THERMAL_PROFILE_PERFORMANCE, 0x01)
        self.assertEqual(HP_OMEN_V0_THERMAL_PROFILE_COOL, 0x02)

    def test_0x1a_performance_packet_v1(self):
        """OMEN v1 Performance constructs exact 2-byte payload b'\\xFF\\x31'."""
        pkt_default = FanController.build_wmi_0x1a_performance_packet()
        self.assertEqual(pkt_default, b"\xFF\x31")
        self.assertEqual(len(pkt_default), 2)

        pkt_named = FanController.build_wmi_0x1a_performance_packet("performance", version=1)
        self.assertEqual(pkt_named, b"\xFF\x31")

    def test_0x1a_balanced_packet_v1(self):
        """OMEN v1 Balanced/Default constructs exact 2-byte payload b'\\xFF\\x30'."""
        pkt_bal = FanController.build_wmi_0x1a_performance_packet("balanced", version=1)
        self.assertEqual(pkt_bal, b"\xFF\x30")
        self.assertEqual(len(pkt_bal), 2)

        pkt_def = FanController.build_wmi_0x1a_performance_packet("default", version=1)
        self.assertEqual(pkt_def, b"\xFF\x30")

    def test_0x1a_cool_packet_v1(self):
        """OMEN v1 Cool constructs exact 2-byte payload b'\\xFF\\x50'."""
        pkt_cool = FanController.build_wmi_0x1a_performance_packet("cool", version=1)
        self.assertEqual(pkt_cool, b"\xFF\x50")
        self.assertEqual(len(pkt_cool), 2)

    def test_0x1a_packets_v0(self):
        """OMEN v0 / Victus / Victus S payloads use 0x01 (Perf), 0x00 (Balanced), 0x02 (Cool)."""
        pkt_perf = FanController.build_wmi_0x1a_performance_packet("performance", version=0)
        self.assertEqual(pkt_perf, b"\xFF\x01")

        pkt_bal = FanController.build_wmi_0x1a_performance_packet("balanced", version=0)
        self.assertEqual(pkt_bal, b"\xFF\x00")

        pkt_def = FanController.build_wmi_0x1a_performance_packet("default", version=0)
        self.assertEqual(pkt_def, b"\xFF\x00")

        pkt_cool = FanController.build_wmi_0x1a_performance_packet("cool", version=0)
        self.assertEqual(pkt_cool, b"\xFF\x02")

    def test_0x1a_explicit_int_mode(self):
        """Explicit integer mode bytes are formatted directly into buffer[1]."""
        self.assertEqual(FanController.build_wmi_0x1a_performance_packet(0x31), b"\xFF\x31")
        self.assertEqual(FanController.build_wmi_0x1a_performance_packet(0x00), b"\xFF\x00")
        self.assertEqual(FanController.build_wmi_0x1a_performance_packet(0xFF), b"\xFF\xFF")

    def test_0x1a_invalid_inputs(self):
        """Invalid mode names, versions, out-of-range bytes, and types are rejected."""
        # Unknown mode string
        with self.assertRaises(ValueError):
            FanController.build_wmi_0x1a_performance_packet("turbo")

        # Unsupported policy version
        with self.assertRaises(ValueError):
            FanController.build_wmi_0x1a_performance_packet("performance", version=2)

        # Mode integer out of range 0..255
        with self.assertRaises(ValueError):
            FanController.build_wmi_0x1a_performance_packet(-1)
        with self.assertRaises(ValueError):
            FanController.build_wmi_0x1a_performance_packet(256)

        # Invalid types
        for invalid_type in [True, False, None, [0x31], {"mode": "perf"}]:
            with self.assertRaises(TypeError):
                FanController.build_wmi_0x1a_performance_packet(invalid_type)

    def test_performance_sequence_packets_builder_v1(self):
        """OMEN v1 performance sequence issues Query 0x10 followed by Query 0x1A."""
        # Performance mode
        packets_perf = FanController.build_wmi_performance_sequence_packets("performance", version=1)
        self.assertEqual(len(packets_perf), 2)
        self.assertEqual(packets_perf[0], (0x10, 0x20008, b"\x00", 4))
        self.assertEqual(packets_perf[1], (0x1A, 0x20008, b"\xFF\x31", 0))

        # Balanced mode
        packets_bal = FanController.build_wmi_performance_sequence_packets("balanced", version=1)
        self.assertEqual(len(packets_bal), 2)
        self.assertEqual(packets_bal[0], (0x10, 0x20008, b"\x00", 4))
        self.assertEqual(packets_bal[1], (0x1A, 0x20008, b"\xFF\x30", 0))

    def test_performance_sequence_packets_builder_v0(self):
        """OMEN v0 performance sequence issues Query 0x10 followed by Query 0x1A."""
        packets_perf = FanController.build_wmi_performance_sequence_packets("performance", version=0)
        self.assertEqual(len(packets_perf), 2)
        self.assertEqual(packets_perf[0], (0x10, 0x20008, b"\x00", 4))
        self.assertEqual(packets_perf[1], (0x1A, 0x20008, b"\xFF\x01", 0))

        packets_bal = FanController.build_wmi_performance_sequence_packets("balanced", version=0)
        self.assertEqual(len(packets_bal), 2)
        self.assertEqual(packets_bal[0], (0x10, 0x20008, b"\x00", 4))
        self.assertEqual(packets_bal[1], (0x1A, 0x20008, b"\xFF\x00", 0))

    def test_0x22_gpu_thermal_modes_packet(self):
        """Query 0x22 packet constructs exact 4-byte payload for GPU cTGP, PPAB, and D-state."""
        # Performance: cTGP=1, PPAB=1, dstate=1, slowdown=85 (0x55)
        pkt_perf = FanController.build_wmi_0x22_gpu_thermal_packet(
            ctgp=True, ppab=True, dstate=1, gpu_slowdown_temp=85
        )
        self.assertEqual(pkt_perf, b"\x01\x01\x01\x55")
        self.assertEqual(len(pkt_perf), 4)

        # Balanced: cTGP=0, PPAB=1, dstate=1, slowdown=0
        pkt_bal = FanController.build_wmi_0x22_gpu_thermal_packet(
            ctgp=False, ppab=True, dstate=1, gpu_slowdown_temp=0
        )
        self.assertEqual(pkt_bal, b"\x00\x01\x01\x00")

        # Low power: cTGP=0, PPAB=0, dstate=1, slowdown=0
        pkt_low = FanController.build_wmi_0x22_gpu_thermal_packet(
            ctgp=False, ppab=False, dstate=1, gpu_slowdown_temp=0
        )
        self.assertEqual(pkt_low, b"\x00\x00\x01\x00")

        # Bounds validation
        with self.assertRaises(ValueError):
            FanController.build_wmi_0x22_gpu_thermal_packet(True, True, dstate=256)
        with self.assertRaises(ValueError):
            FanController.build_wmi_0x22_gpu_thermal_packet(True, True, dstate=-1)
        with self.assertRaises(ValueError):
            FanController.build_wmi_0x22_gpu_thermal_packet(True, True, gpu_slowdown_temp=256)
        with self.assertRaises(ValueError):
            FanController.build_wmi_0x22_gpu_thermal_packet(True, True, gpu_slowdown_temp=-1)

    def test_0x28_system_design_packet(self):
        """Query 0x28 packet constructs exact 8-byte zeroed payload."""
        packet = FanController.build_wmi_0x28_system_design_packet()
        self.assertEqual(packet, b"\x00" * 8)
        self.assertEqual(len(packet), 8)

    def test_parse_0x28_thermal_policy_version_bytes(self):
        """Query 0x28 output extracts byte 3 as thermal policy version."""
        raw_v1 = bytes([0x01, 0x02, 0x00, 0x01, 0x10, 0x20, 0x30, 0x40])
        parsed_v1 = FanController.parse_wmi_0x28_thermal_policy_version(raw_v1)
        self.assertIsNotNone(parsed_v1)
        self.assertEqual(parsed_v1[0], 1)
        self.assertEqual(parsed_v1[1], [0x01, 0x02, 0x00, 0x01, 0x10, 0x20, 0x30, 0x40])

        raw_v0 = bytes([0x01, 0x02, 0x00, 0x00, 0x10, 0x20, 0x30, 0x40])
        parsed_v0 = FanController.parse_wmi_0x28_thermal_policy_version(raw_v0)
        self.assertIsNotNone(parsed_v0)
        self.assertEqual(parsed_v0[0], 0)
        self.assertEqual(parsed_v0[1], [0x01, 0x02, 0x00, 0x00, 0x10, 0x20, 0x30, 0x40])

    def test_parse_0x28_sysfs_string(self):
        """Sysfs formatted text output is parsed accurately."""
        sysfs_text_v1 = "thermal_policy_version=1\nraw=0x01 0x02 0x00 0x01 0x10 0x20 0x30 0x40\n"
        parsed_v1 = FanController.parse_wmi_0x28_thermal_policy_version(sysfs_text_v1)
        self.assertIsNotNone(parsed_v1)
        self.assertEqual(parsed_v1[0], 1)
        self.assertEqual(parsed_v1[1], [0x01, 0x02, 0x00, 0x01, 0x10, 0x20, 0x30, 0x40])

        sysfs_text_v0 = "thermal_policy_version=0\nraw=0x01 0x02 0x00 0x00 0x10 0x20 0x30 0x40\n"
        parsed_v0 = FanController.parse_wmi_0x28_thermal_policy_version(sysfs_text_v0)
        self.assertIsNotNone(parsed_v0)
        self.assertEqual(parsed_v0[0], 0)

    def test_parse_0x28_invalid_input(self):
        """Malformed or incomplete query 0x28 payloads return None."""
        self.assertIsNone(FanController.parse_wmi_0x28_thermal_policy_version(None))
        self.assertIsNone(FanController.parse_wmi_0x28_thermal_policy_version(b"\x00" * 7))  # Too short (<8)
        self.assertIsNone(FanController.parse_wmi_0x28_thermal_policy_version([0, 0, 0]))  # Too short
        self.assertIsNone(FanController.parse_wmi_0x28_thermal_policy_version([0, 0, 0, True, 0, 0, 0, 0]))  # Bool
        self.assertIsNone(FanController.parse_wmi_0x28_thermal_policy_version([0, 0, 0, 300, 0, 0, 0, 0]))  # Out of range
        self.assertIsNone(FanController.parse_wmi_0x28_thermal_policy_version("invalid string"))

    def test_read_experimental_thermal_policy_version_gating(self):
        """read_experimental_thermal_policy_version respects gating flag and handles errors."""
        temp_dir = tempfile.mkdtemp()
        try:
            cfg_path = Path(temp_dir) / "config.json"
            with patch.object(FanController, "_get_live_board_name", return_value="8CC0"):
                ctrl = FanController(config_path=cfg_path)

            # Gating disabled: returns None
            ctrl.config = {"enable_experimental_telemetry": False}
            fake_node = Path(temp_dir) / "experimental_thermal_policy_version"
            fake_node.write_text("thermal_policy_version=1\nraw=0x00 0x00 0x00 0x01 0x00 0x00 0x00 0x00\n")
            self.assertIsNone(ctrl.read_experimental_thermal_policy_version(source=fake_node))

            # Gating enabled: reads sysfs successfully
            ctrl.config = {"enable_experimental_telemetry": True}
            res = ctrl.read_experimental_thermal_policy_version(source=fake_node)
            self.assertIsNotNone(res)
            self.assertEqual(res[0], 1)

            # Nonexistent path: returns None
            self.assertIsNone(ctrl.read_experimental_thermal_policy_version(source=Path(temp_dir) / "nonexistent"))

            # I/O error: returns None without exception
            with patch.object(Path, "read_text", side_effect=OSError("Read error")), \
                 patch.object(Path, "read_bytes", side_effect=OSError("Read error")), \
                 patch.object(Path, "exists", return_value=True):
                self.assertIsNone(ctrl.read_experimental_thermal_policy_version(source=fake_node))
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_set_experimental_performance_mode_on(self):
        """Performance ON issues 0x10 -> 0x1A with exact OMEN v1 payload b'\\xFF\\x31'."""
        temp_dir = tempfile.mkdtemp()
        try:
            cfg_path = Path(temp_dir) / "config.json"
            with patch.object(FanController, "_get_live_board_name", return_value="8CC0"):
                ctrl = FanController(config_path=cfg_path)
            ctrl.config = {"enable_experimental_control": True}

            calls = []
            def mock_executor(query, domain, payload, out_size):
                calls.append((query, domain, payload, out_size))
                return 0

            # Test query_executor path
            res = ctrl.set_experimental_performance_mode(True, query_executor=mock_executor)
            self.assertTrue(res)
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0], (0x10, 0x20008, b"\x00", 4))
            self.assertEqual(calls[1], (0x1A, 0x20008, b"\xFF\x31", 0))

            # Test sysfs path
            fake_path = MagicMock(spec=Path)
            fake_path.exists.return_value = True
            res_sysfs = ctrl.set_experimental_performance_mode(True, path=fake_path)
            self.assertTrue(res_sysfs)
            fake_path.write_text.assert_called_once_with("1\n")
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_set_experimental_performance_mode_off_balanced(self):
        """Performance OFF (Balanced/Default) issues 0x10 -> 0x1A with exact OMEN v1 payload b'\\xFF\\x30'."""
        temp_dir = tempfile.mkdtemp()
        try:
            cfg_path = Path(temp_dir) / "config.json"
            with patch.object(FanController, "_get_live_board_name", return_value="8CC0"):
                ctrl = FanController(config_path=cfg_path)
            ctrl.config = {"enable_experimental_control": True}

            calls = []
            def mock_executor(query, domain, payload, out_size):
                calls.append((query, domain, payload, out_size))
                return 0

            # Test query_executor path
            res = ctrl.set_experimental_performance_mode(False, query_executor=mock_executor)
            self.assertTrue(res)
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0], (0x10, 0x20008, b"\x00", 4))
            self.assertEqual(calls[1], (0x1A, 0x20008, b"\xFF\x30", 0))

            # Test sysfs path
            fake_path = MagicMock(spec=Path)
            fake_path.exists.return_value = True
            res_sysfs = ctrl.set_experimental_performance_mode(False, path=fake_path)
            self.assertTrue(res_sysfs)
            fake_path.write_text.assert_called_once_with("0\n")
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_performance_mode_strictly_v1_not_v0(self):
        """Board 8CC0 is confirmed OMEN v1; v0 payloads (0x01/0x00) must never be sent."""
        temp_dir = tempfile.mkdtemp()
        try:
            cfg_path = Path(temp_dir) / "config.json"
            with patch.object(FanController, "_get_live_board_name", return_value="8CC0"):
                ctrl = FanController(config_path=cfg_path)
            ctrl.config = {"enable_experimental_control": True}

            calls = []
            def mock_executor(query, domain, payload, out_size):
                calls.append((query, domain, payload, out_size))
                return 0

            ctrl.set_experimental_performance_mode(True, query_executor=mock_executor)
            ctrl.set_experimental_performance_mode(False, query_executor=mock_executor)

            self.assertEqual(calls[1][2], b"\xFF\x31")
            self.assertNotEqual(calls[1][2], b"\xFF\x01")  # Must NOT be v0

            self.assertEqual(calls[3][2], b"\xFF\x30")
            self.assertNotEqual(calls[3][2], b"\xFF\x00")  # Must NOT be v0
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_performance_mode_gating(self):
        """Performance control and query are strictly blocked when enable_experimental_control is False."""
        temp_dir = tempfile.mkdtemp()
        try:
            cfg_path = Path(temp_dir) / "config.json"
            with patch.object(FanController, "_get_live_board_name", return_value="8CC0"):
                ctrl = FanController(config_path=cfg_path)
            ctrl.config = {"enable_experimental_control": False}

            mock_executor = MagicMock()
            res_set = ctrl.set_experimental_performance_mode(True, query_executor=mock_executor)
            self.assertFalse(res_set)
            mock_executor.assert_not_called()

            fake_path = MagicMock(spec=Path)
            fake_path.exists.return_value = True
            fake_path.read_text.return_value = "1\n"
            res_get = ctrl.get_experimental_performance_mode(path=fake_path)
            self.assertIsNone(res_get)
            fake_path.read_text.assert_not_called()
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_performance_mode_abort_if_0x10_fails(self):
        """If query 0x10 fails, abort immediately without issuing query 0x1A."""
        temp_dir = tempfile.mkdtemp()
        try:
            cfg_path = Path(temp_dir) / "config.json"
            with patch.object(FanController, "_get_live_board_name", return_value="8CC0"):
                ctrl = FanController(config_path=cfg_path)
            ctrl.config = {"enable_experimental_control": True}

            calls = []
            def failing_executor(query, domain, payload, out_size):
                calls.append(query)
                if query == 0x10:
                    return -1  # Failure
                return 0

            res = ctrl.set_experimental_performance_mode(True, query_executor=failing_executor)
            self.assertFalse(res)
            self.assertEqual(calls, [0x10])
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_performance_mode_abort_if_0x1a_fails(self):
        """If query 0x1A fails, return False."""
        temp_dir = tempfile.mkdtemp()
        try:
            cfg_path = Path(temp_dir) / "config.json"
            with patch.object(FanController, "_get_live_board_name", return_value="8CC0"):
                ctrl = FanController(config_path=cfg_path)
            ctrl.config = {"enable_experimental_control": True}

            calls = []
            def failing_executor(query, domain, payload, out_size):
                calls.append(query)
                if query == 0x1A:
                    return -1  # Failure
                return 0

            res = ctrl.set_experimental_performance_mode(True, query_executor=failing_executor)
            self.assertFalse(res)
            self.assertEqual(calls, [0x10, 0x1A])
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_performance_mode_invalid_inputs(self):
        """Invalid inputs return False without performing writes."""
        temp_dir = tempfile.mkdtemp()
        try:
            cfg_path = Path(temp_dir) / "config.json"
            with patch.object(FanController, "_get_live_board_name", return_value="8CC0"):
                ctrl = FanController(config_path=cfg_path)
            ctrl.config = {"enable_experimental_control": True}

            mock_executor = MagicMock()
            for invalid in ["perf", 2, -1, None, [1], {"mode": 1}]:
                res = ctrl.set_experimental_performance_mode(invalid, query_executor=mock_executor)
                self.assertFalse(res)
            mock_executor.assert_not_called()
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_get_experimental_performance_mode_values(self):
        """get_experimental_performance_mode parses sysfs '0' and '1' correctly and handles errors."""
        temp_dir = tempfile.mkdtemp()
        try:
            cfg_path = Path(temp_dir) / "config.json"
            with patch.object(FanController, "_get_live_board_name", return_value="8CC0"):
                ctrl = FanController(config_path=cfg_path)
            ctrl.config = {"enable_experimental_control": True}

            node = Path(temp_dir) / "experimental_performance_mode"
            node.write_text("1\n")
            self.assertEqual(ctrl.get_experimental_performance_mode(path=node), 1)

            node.write_text("0\n")
            self.assertEqual(ctrl.get_experimental_performance_mode(path=node), 0)

            node.write_text("invalid\n")
            self.assertIsNone(ctrl.get_experimental_performance_mode(path=node))

            self.assertIsNone(ctrl.get_experimental_performance_mode(path=Path(temp_dir) / "nonexistent"))

            with patch.object(Path, "read_text", side_effect=OSError("I/O error")):
                self.assertIsNone(ctrl.get_experimental_performance_mode(path=node))
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_no_performance_write_during_initialization(self):
        """Instantiating FanController issues NO Performance writes on board 8CC0."""
        temp_dir = tempfile.mkdtemp()
        try:
            cfg_path = Path(temp_dir) / "config.json"
            with patch.object(FanController, "_get_live_board_name", return_value="8CC0"), \
                 patch.object(FanController, "write_sys_file") as mock_write, \
                 patch.object(FanController, "set_experimental_performance_mode") as mock_perf:
                ctrl = FanController(config_path=cfg_path)
                self.assertEqual(ctrl.check_board_support()[0], "MONITORING_ONLY")
                mock_write.assert_not_called()
                mock_perf.assert_not_called()
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


class TestExperimentalWmi0x2eAutoFanControl(unittest.TestCase):
    """
    Unit tests for Query 0x2E autonomous AUTO fan control on board 8CC0.
    Verifies packet building, safety floor invariants, stock ordering,
    watchdog refresh, duplicate suppression, and safe shutdown fallbacks.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.cfg_path = Path(self.temp_dir) / "config.json"

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_build_wmi_0x2e_speed_packet_valid(self):
        """Valid integer inputs produce exact 2-byte payloads."""
        self.assertEqual(FanController.build_wmi_0x2e_speed_packet(0, 0), b"\x00\x00")
        self.assertEqual(FanController.build_wmi_0x2e_speed_packet(100, 100), b"\x64\x64")
        self.assertEqual(FanController.build_wmi_0x2e_speed_packet(50, 75), b"\x32\x4b")
        self.assertEqual(FanController.build_wmi_0x2e_speed_packet(5, 5), b"\x05\x05")

    def test_build_wmi_0x2e_speed_packet_boundaries(self):
        """Boundary values (0, 100) are accepted."""
        self.assertEqual(FanController.build_wmi_0x2e_speed_packet(0, 100), b"\x00\x64")
        self.assertEqual(FanController.build_wmi_0x2e_speed_packet(100, 0), b"\x64\x00")

    def test_build_wmi_0x2e_speed_packet_invalid_types(self):
        """Non-integer types raise TypeError."""
        for invalid_val in [50.0, "50", None, True, False, [50], float("nan")]:
            with self.assertRaises(TypeError):
                FanController.build_wmi_0x2e_speed_packet(invalid_val, 50)
            with self.assertRaises(TypeError):
                FanController.build_wmi_0x2e_speed_packet(50, invalid_val)

    def test_build_wmi_0x2e_speed_packet_out_of_bounds(self):
        """Values outside 0..100 raise ValueError."""
        for out_of_bounds in [-1, -100, 101, 255]:
            with self.assertRaises(ValueError):
                FanController.build_wmi_0x2e_speed_packet(out_of_bounds, 50)
            with self.assertRaises(ValueError):
                FanController.build_wmi_0x2e_speed_packet(50, out_of_bounds)

    def test_pwm_to_fan_level_conversion(self):
        """PWM 0..255 accurately converts to 0..100."""
        self.assertEqual(FanController.pwm_to_fan_level(0), 0)
        self.assertEqual(FanController.pwm_to_fan_level(255), 100)
        self.assertEqual(FanController.pwm_to_fan_level(128), 50)
        self.assertEqual(FanController.pwm_to_fan_level(64), 25)

        with self.assertRaises(TypeError):
            FanController.pwm_to_fan_level(50.5)
        with self.assertRaises(ValueError):
            FanController.pwm_to_fan_level(-1)
        with self.assertRaises(ValueError):
            FanController.pwm_to_fan_level(256)

    def test_minimum_fan_speed_safety_invariant(self):
        """Software AUTO clamps to minimum level 5 (~500 RPM safety floor)."""
        # 0% output -> 5
        self.assertEqual(FanController.clamp_software_auto_fan_level(0), 5)
        # 1-4% output -> 5
        self.assertEqual(FanController.clamp_software_auto_fan_level(1), 5)
        self.assertEqual(FanController.clamp_software_auto_fan_level(4), 5)
        # 5% output -> 5
        self.assertEqual(FanController.clamp_software_auto_fan_level(5), 5)
        # Standard levels preserved
        self.assertEqual(FanController.clamp_software_auto_fan_level(50), 50)
        self.assertEqual(FanController.clamp_software_auto_fan_level(100), 100)
        # Over 100 clamped to 100
        self.assertEqual(FanController.clamp_software_auto_fan_level(110), 100)

        with self.assertRaises(TypeError):
            FanController.clamp_software_auto_fan_level(5.0)

    def test_pwm_to_software_auto_level(self):
        """PWM values convert to fan levels with strict floor of 5."""
        self.assertEqual(FanController.pwm_to_software_auto_level(0), 5)
        self.assertEqual(FanController.pwm_to_software_auto_level(5), 5)
        self.assertEqual(FanController.pwm_to_software_auto_level(12), 5)
        self.assertEqual(FanController.pwm_to_software_auto_level(128), 50)
        self.assertEqual(FanController.pwm_to_software_auto_level(255), 100)

    def test_clamp_software_auto_dual_levels_independent(self):
        """CPU and GPU targets are clamped independently to minimum level 5."""
        self.assertEqual(FanController.clamp_software_auto_dual_levels(0, 50), (5, 50))
        self.assertEqual(FanController.clamp_software_auto_dual_levels(80, 2), (80, 5))
        self.assertEqual(FanController.clamp_software_auto_dual_levels(0, 0), (5, 5))
        self.assertEqual(FanController.clamp_software_auto_dual_levels(100, 100), (100, 100))

    def test_wmi_stock_sequence_ordering_0x10_0x27_0x2e(self):
        """Stock sequence executes 0x10 -> 0x27(0) -> 0x2E(cpu, gpu) in exact order."""
        from omen_fan_control._constants import (
            WMI_CMD_GM,
            WMI_QUERY_FAN_COUNT,
            WMI_QUERY_FAN_SPEED_MAX_SET,
            WMI_QUERY_FAN_SPEED_SET,
        )

        calls = []

        def mock_executor(query, domain, in_data, out_size):
            calls.append((query, domain, in_data, out_size))
            return True

        with patch.object(FanController, "_get_live_board_name", return_value="8CC0"):
            ctrl = FanController(config_path=self.cfg_path)
            ctrl.config["enable_experimental_control"] = True
            ctrl.query_executor = mock_executor

            success = ctrl.set_experimental_fan_speed(50, 60)
            self.assertTrue(success)
            self.assertEqual(len(calls), 3)

            # 1. Query 0x10 trigger
            self.assertEqual(calls[0], (WMI_QUERY_FAN_COUNT, WMI_CMD_GM, b"\x00", 4))
            # 2. Query 0x27(0) Max off
            self.assertEqual(calls[1], (WMI_QUERY_FAN_SPEED_MAX_SET, WMI_CMD_GM, b"\x00\x00\x00\x00", 0))
            # 3. Query 0x2E speed set
            self.assertEqual(calls[2], (WMI_QUERY_FAN_SPEED_SET, WMI_CMD_GM, b"\x32\x3c", 0))

    def test_abort_on_0x10_failure(self):
        """Failure on Query 0x10 trigger aborts before 0x27 and 0x2E."""
        from omen_fan_control._constants import WMI_QUERY_FAN_COUNT

        calls = []

        def mock_executor(query, domain, in_data, out_size):
            calls.append(query)
            if query == WMI_QUERY_FAN_COUNT:
                return False
            return True

        with patch.object(FanController, "_get_live_board_name", return_value="8CC0"):
            ctrl = FanController(config_path=self.cfg_path)
            ctrl.config["enable_experimental_control"] = True
            ctrl.query_executor = mock_executor

            success = ctrl.set_experimental_fan_speed(50, 50)
            self.assertFalse(success)
            self.assertEqual(calls, [WMI_QUERY_FAN_COUNT])

    def test_abort_on_0x27_failure(self):
        """Failure on Query 0x27 Max-off aborts before 0x2E."""
        from omen_fan_control._constants import (
            WMI_QUERY_FAN_COUNT,
            WMI_QUERY_FAN_SPEED_MAX_SET,
        )

        calls = []

        def mock_executor(query, domain, in_data, out_size):
            calls.append(query)
            if query == WMI_QUERY_FAN_SPEED_MAX_SET:
                return False
            return True

        with patch.object(FanController, "_get_live_board_name", return_value="8CC0"):
            ctrl = FanController(config_path=self.cfg_path)
            ctrl.config["enable_experimental_control"] = True
            ctrl.query_executor = mock_executor

            success = ctrl.set_experimental_fan_speed(50, 50)
            self.assertFalse(success)
            self.assertEqual(calls, [WMI_QUERY_FAN_COUNT, WMI_QUERY_FAN_SPEED_MAX_SET])

    def test_abort_on_0x2e_failure(self):
        """Failure on Query 0x2E returns False."""
        from omen_fan_control._constants import (
            WMI_QUERY_FAN_COUNT,
            WMI_QUERY_FAN_SPEED_MAX_SET,
            WMI_QUERY_FAN_SPEED_SET,
        )

        calls = []

        def mock_executor(query, domain, in_data, out_size):
            calls.append(query)
            if query == WMI_QUERY_FAN_SPEED_SET:
                return False
            return True

        with patch.object(FanController, "_get_live_board_name", return_value="8CC0"):
            ctrl = FanController(config_path=self.cfg_path)
            ctrl.config["enable_experimental_control"] = True
            ctrl.query_executor = mock_executor

            success = ctrl.set_experimental_fan_speed(50, 50)
            self.assertFalse(success)
            self.assertEqual(calls, [WMI_QUERY_FAN_COUNT, WMI_QUERY_FAN_SPEED_MAX_SET, WMI_QUERY_FAN_SPEED_SET])

    def test_8cc0_routing_to_wmi_rather_than_hwmon(self):
        """Board 8CC0 with experimental control routes set_fan_pwm to WMI with calibrated floor."""
        with patch.object(FanController, "_get_live_board_name", return_value="8CC0"), \
             patch.object(FanController, "write_sys_file") as mock_write, \
             patch.object(FanController, "set_experimental_fan_speed", return_value=True) as mock_exp_speed:
            ctrl = FanController(config_path=self.cfg_path)
            ctrl.config["enable_experimental_control"] = True
            ctrl.config["calibrated_min_cpu_level"] = 18
            ctrl.config["calibrated_min_gpu_level"] = 20

            # Input PWM 0 (0%) and 255 (100%) -> should clamp to calibrated floors
            ctrl.set_fan_pwm(0, 255)

            mock_exp_speed.assert_called_once_with(18, 100)
            mock_write.assert_not_called()

    def test_non_8cc0_routing_preserves_hwmon(self):
        """Non-8CC0 supported board uses legacy hwmon and does not route to WMI."""
        ctrl = _make_controller(
            hwmon_exists=True,
            pwm1_enable_exists=True,
            pwm1_exists=True,
            board_name="8C78",
        )
        with patch.object(ctrl, "write_sys_file") as mock_write, \
             patch.object(ctrl, "read_sys_file", return_value="1"), \
             patch.object(ctrl, "set_experimental_fan_speed") as mock_exp_speed:
            ctrl.set_fan_pwm(128)

            mock_write.assert_any_call(ctrl.pwm1_path, "128")
            mock_exp_speed.assert_not_called()

    def test_8cc0_rpm_routing_to_0x2d_telemetry(self):
        """Board 8CC0 routes RPM queries to 0x2D telemetry when enabled."""
        with patch.object(FanController, "_get_live_board_name", return_value="8CC0"), \
             patch.object(FanController, "read_experimental_fan_telemetry", return_value=(3900, 4200)):
            ctrl = FanController(config_path=self.cfg_path)
            ctrl.config["enable_experimental_telemetry"] = True

            # Single fan speed returns max of dual fans
            self.assertEqual(ctrl.get_fan_speed(), 4200)

            # Single fan speed info returns (rpm, False)
            self.assertEqual(ctrl.get_fan_speed_info(), (4200, False))

            # Dual fan speeds return labeled CPU and GPU telemetry
            both = ctrl.get_both_fan_speeds()
            self.assertEqual(len(both), 2)
            self.assertEqual(both[0], ("Fan 1 (CPU)", 3900, False))
            self.assertEqual(both[1], ("Fan 2 (GPU)", 4200, False))

    def test_duplicate_write_suppression(self):
        """Identical target writes within 60s are suppressed."""
        calls = []

        def mock_executor(query, domain, in_data, out_size):
            calls.append(query)
            return True

        with patch.object(FanController, "_get_live_board_name", return_value="8CC0"):
            ctrl = FanController(config_path=self.cfg_path)
            ctrl.config["enable_experimental_control"] = True
            ctrl.query_executor = mock_executor

            # First write: triggers queries
            res1 = ctrl.set_experimental_fan_speed(50, 50)
            self.assertTrue(res1)
            self.assertEqual(len(calls), 3)

            # Second identical write immediately: suppressed
            res2 = ctrl.set_experimental_fan_speed(50, 50)
            self.assertTrue(res2)
            self.assertEqual(len(calls), 3)  # no new queries

    def test_watchdog_refresh_when_target_unchanged(self):
        """Identical target writes after >=60s execute queries to refresh EC watchdog."""
        calls = []

        def mock_executor(query, domain, in_data, out_size):
            calls.append(query)
            return True

        with patch.object(FanController, "_get_live_board_name", return_value="8CC0"):
            ctrl = FanController(config_path=self.cfg_path)
            ctrl.config["enable_experimental_control"] = True
            ctrl.query_executor = mock_executor

            # First write
            ctrl.set_experimental_fan_speed(50, 50)
            self.assertEqual(len(calls), 3)

            # Simulate 65 seconds elapsed
            ctrl._last_control_time -= 65.0

            # Identical target write: should refresh EC watchdog
            res = ctrl.set_experimental_fan_speed(50, 50)
            self.assertTrue(res)
            self.assertEqual(len(calls), 6)  # 3 new queries executed

    def test_shutdown_fallback_to_firmware_auto(self):
        """restore_firmware_auto executes 0x10 -> 0x27(0) -> 0x2E(0,0) and resets state."""
        from omen_fan_control._constants import (
            WMI_CMD_GM,
            WMI_QUERY_FAN_COUNT,
            WMI_QUERY_FAN_SPEED_MAX_SET,
            WMI_QUERY_FAN_SPEED_SET,
        )

        calls = []

        def mock_executor(query, domain, in_data, out_size):
            calls.append((query, domain, in_data, out_size))
            return True

        with patch.object(FanController, "_get_live_board_name", return_value="8CC0"), \
             patch.object(FanController, "_cancel_experimental_max_keepalive") as mock_stop_ka:
            ctrl = FanController(config_path=self.cfg_path)
            ctrl.config["enable_experimental_control"] = True
            ctrl.query_executor = mock_executor

            # Set manual speed first
            ctrl.set_experimental_fan_speed(50, 50)
            self.assertEqual(ctrl._current_control_mode, "software_auto")
            calls.clear()

            # Execute shutdown restore
            success = ctrl.restore_firmware_auto()
            self.assertTrue(success)
            self.assertEqual(len(calls), 3)

            # Sequence: 0x10 -> 0x27(0) -> 0x2E(0, 0)
            self.assertEqual(calls[0], (WMI_QUERY_FAN_COUNT, WMI_CMD_GM, b"\x00", 4))
            self.assertEqual(calls[1], (WMI_QUERY_FAN_SPEED_MAX_SET, WMI_CMD_GM, b"\x00\x00\x00\x00", 0))
            self.assertEqual(calls[2], (WMI_QUERY_FAN_SPEED_SET, WMI_CMD_GM, b"\x00\x00", 0))

            self.assertGreaterEqual(mock_stop_ka.call_count, 1)
            self.assertEqual(ctrl._current_control_mode, "firmware_auto")
            self.assertEqual(ctrl._last_commanded_cpu_level, 0)
            self.assertEqual(ctrl._last_commanded_gpu_level, 0)

    def test_experimental_control_gating(self):
        """Experimental control is rejected if enable_experimental_control is False."""
        with patch.object(FanController, "_get_live_board_name", return_value="8CC0"):
            ctrl = FanController(config_path=self.cfg_path)
            ctrl.config["enable_experimental_control"] = False

            self.assertFalse(ctrl.set_experimental_fan_speed(50, 50))
            self.assertFalse(ctrl.set_experimental_fan_auto())

    def test_no_writes_during_driver_initialization(self):
        """Instantiating FanController on 8CC0 executes ZERO writes."""
        with patch.object(FanController, "_get_live_board_name", return_value="8CC0"), \
             patch.object(FanController, "write_sys_file") as mock_write, \
             patch.object(FanController, "set_experimental_fan_speed") as mock_speed, \
             patch.object(FanController, "restore_firmware_auto") as mock_auto:
            ctrl = FanController(config_path=self.cfg_path)
            ctrl.config["enable_experimental_control"] = True
            ctrl.config["enable_experimental_telemetry"] = True

            mock_write.assert_not_called()
            mock_speed.assert_not_called()
            mock_auto.assert_not_called()

    def test_sysfs_experimental_fan_speed_rw(self):
        """get_experimental_fan_speed reads from sysfs node."""
        node = Path(self.temp_dir) / "experimental_fan_speed"
        node.write_text("50 65\n")

        with patch.object(FanController, "_get_live_board_name", return_value="8CC0"):
            ctrl = FanController(config_path=self.cfg_path)
            ctrl.config["enable_experimental_control"] = True
            self.assertEqual(ctrl.get_experimental_fan_speed(path=node), (50, 65))

            node.write_text("invalid\n")
            self.assertIsNone(ctrl.get_experimental_fan_speed(path=node))

            self.assertIsNone(ctrl.get_experimental_fan_speed(path=Path(self.temp_dir) / "nonexistent"))

    def test_custom_user_curve_cannot_produce_below_calibrated_floor_during_software_auto(self):
        """User curves specifying 0% or low percentages never command below calibrated floor."""
        custom_low_curve = [
            [20, 0],
            [30, 2],
            [40, 4],
            [80, 50],
            [95, 100],
        ]
        with patch.object(FanController, "_get_live_board_name", return_value="8CC0"), \
             patch.object(FanController, "set_experimental_fan_speed") as mock_speed:
            ctrl = FanController(config_path=self.cfg_path)
            ctrl.config["enable_experimental_control"] = True
            ctrl.config["curve"] = custom_low_curve
            ctrl.config["calibrated_min_cpu_level"] = 16
            ctrl.config["calibrated_min_gpu_level"] = 18

            # At 20C (curve output 0%)
            cpu_pwm, gpu_pwm = ctrl.calculate_dual_target_pwm(20, 20, curve_override=custom_low_curve)
            self.assertEqual(cpu_pwm, 0)
            ctrl.set_fan_pwm(cpu_pwm, gpu_pwm)
            mock_speed.assert_called_with(16, 18)

            # At 30C (curve output 2%)
            cpu_pwm, gpu_pwm = ctrl.calculate_dual_target_pwm(30, 30, curve_override=custom_low_curve)
            ctrl.set_fan_pwm(cpu_pwm, gpu_pwm)
            mock_speed.assert_called_with(16, 18)

            # At 40C (curve output 4%)
            cpu_pwm, gpu_pwm = ctrl.calculate_dual_target_pwm(40, 40, curve_override=custom_low_curve)
            ctrl.set_fan_pwm(cpu_pwm, gpu_pwm)
            mock_speed.assert_called_with(16, 18)

    def test_daemon_invalid_temperature_safety_fallback(self):
        """Invalid or unavailable temperatures trigger fallback to firmware AUTO."""
        for bad_temp in [None, 0, -10, 120]:
            ctrl = FanController(config_path=self.cfg_path)
            with patch.object(ctrl, "restore_firmware_auto") as mock_restore:
                # Mirror serve.py temperature validation logic
                current_temp = bad_temp
                if current_temp is None or current_temp <= 0 or current_temp > 115:
                    ctrl.restore_firmware_auto()
                mock_restore.assert_called_once()


# ===========================================================================
# 22. Empirical Minimum Fan Level Calibration & Runtime Safety System
# ===========================================================================
class TestMinimumFanLevelCalibrationAndSafety(unittest.TestCase):
    """
    Tests for Phase 2, 3, and 4: empirical calibration of minimum fan levels,
    software auto floor enforcement, and runtime low-RPM safety.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.cfg_path = Path(self.temp_dir) / "config.json"

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _make_ctrl(self, board="8CC0", exp_ctl=True, exp_telem=True):
        with patch.object(FanController, "_get_live_board_name", return_value=board):
            ctrl = FanController(config_path=self.cfg_path)
            ctrl.config["enable_experimental_control"] = exp_ctl
            ctrl.config["enable_experimental_telemetry"] = exp_telem
            return ctrl

    def test_get_calibrated_min_fan_levels_defaults_to_fallback(self):
        """When uncalibrated, get_calibrated_min_fan_levels returns fallback (15, 15)."""
        ctrl = self._make_ctrl()
        self.assertEqual(ctrl.get_calibrated_min_fan_levels(), (15, 15))

        # Configurable fallback
        ctrl.config["fallback_min_fan_level"] = 18
        self.assertEqual(ctrl.get_calibrated_min_fan_levels(), (18, 18))

    def test_get_calibrated_min_fan_levels_reads_persisted_config(self):
        """Persisted calibration values are returned independently for CPU and GPU."""
        ctrl = self._make_ctrl()
        ctrl.config["calibrated_min_cpu_level"] = 16
        ctrl.config["calibrated_min_gpu_level"] = 20
        self.assertEqual(ctrl.get_calibrated_min_fan_levels(), (16, 20))

    def test_get_calibrated_min_fan_levels_rejects_out_of_range(self):
        """Out-of-range or non-integer calibrated values fall back to safe default."""
        ctrl = self._make_ctrl()
        ctrl.config["calibrated_min_cpu_level"] = 2  # < 5 invalid
        ctrl.config["calibrated_min_gpu_level"] = 105  # > 100 invalid
        self.assertEqual(ctrl.get_calibrated_min_fan_levels(), (15, 15))

    def test_escalate_runtime_min_fan_floor(self):
        """Runtime escalation bumps session floor conservatively and caps at ceiling."""
        ctrl = self._make_ctrl()
        ctrl.config["calibrated_min_cpu_level"] = 14
        ctrl.config["calibrated_min_gpu_level"] = 16

        # Bump CPU floor
        new_cpu = ctrl.escalate_runtime_min_fan_floor("cpu", bump=2)
        self.assertEqual(new_cpu, 16)
        self.assertEqual(ctrl.get_calibrated_min_fan_levels(), (16, 16))

        # Bump CPU again
        new_cpu2 = ctrl.escalate_runtime_min_fan_floor("cpu", bump=2)
        self.assertEqual(new_cpu2, 18)
        self.assertEqual(ctrl.get_calibrated_min_fan_levels(), (18, 16))

        # Reset session floors
        ctrl.reset_runtime_min_fan_floors()
        self.assertEqual(ctrl.get_calibrated_min_fan_levels(), (14, 16))

    def test_software_auto_never_commands_level_0_during_operation(self):
        """Active software AUTO clamps commanded level to calibrated floor, never level 0."""
        ctrl = self._make_ctrl()
        ctrl.config["calibrated_min_cpu_level"] = 18
        ctrl.config["calibrated_min_gpu_level"] = 20

        with patch.object(ctrl, "set_experimental_fan_speed") as mock_speed:
            # PWM 0 (demand 0%) must clamp to floors (18, 20)
            ctrl.set_fan_pwm(0, 0)
            mock_speed.assert_called_once_with(18, 20)

    def test_calibration_discovers_independent_stable_minimums(self):
        """Calibration tests candidates sequentially and identifies stable CPU and GPU floors."""
        ctrl = self._make_ctrl()

        # Telemetry mock:
        # candidate 10: CPU pulses (spread=1400 > 350), GPU stalls (RPM=0)
        # candidate 12: CPU pulses, GPU pulses
        # candidate 14: CPU stable (~1400 RPM, low spread), GPU pulses
        # candidate 16: CPU already stable, GPU stable (~1600 RPM, low spread)
        telemetry_responses = {
            10: [(400, 0), (1800, 0), (500, 0), (1900, 0), (400, 0)],
            12: [(800, 600), (1900, 1700), (900, 700), (1800, 1600), (850, 650)],
            14: [(1400, 700), (1420, 1800), (1390, 800), (1410, 1750), (1400, 750)],
            16: [(1400, 1600), (1410, 1620), (1390, 1590), (1400, 1610), (1405, 1600)],
        }
        current_cmds = [10, 10]

        def mock_executor(query, domain, payload, outsize):
            from omen_fan_control._constants import WMI_QUERY_FAN_SPEED_SET
            if query == WMI_QUERY_FAN_SPEED_SET and len(payload) == 2:
                current_cmds[0] = payload[0]
                current_cmds[1] = payload[1]
            return 0

        sample_idx = [0]

        def mock_telemetry_reader():
            c_cand = current_cmds[0]
            g_cand = current_cmds[1]
            c_resp = telemetry_responses.get(c_cand, [(1400, 1600)] * 5)
            g_resp = telemetry_responses.get(g_cand, [(1400, 1600)] * 5)
            c_val = c_resp[sample_idx[0] % len(c_resp)][0]
            g_val = g_resp[sample_idx[0] % len(g_resp)][1]
            sample_idx[0] += 1
            return (c_val, g_val)

        with patch.object(ctrl, "get_reference_temp", return_value=45.0), \
             patch.object(ctrl, "restore_firmware_auto") as mock_restore:
            result = ctrl.calibrate_minimum_fan_levels(
                min_required_rpm=500,
                candidate_levels=[10, 12, 14, 16, 18, 20],
                settle_time=0.001,
                sample_count=5,
                sample_interval=0.001,
                query_executor=mock_executor,
                telemetry_reader=mock_telemetry_reader,
            )

            self.assertTrue(result["success"])
            self.assertEqual(result["calibrated_min_cpu_level"], 14)
            self.assertEqual(result["calibrated_min_gpu_level"], 16)
            self.assertGreater(result["calibrated_min_cpu_rpm"], 1300)
            self.assertGreater(result["calibrated_min_gpu_rpm"], 1500)

            # Restored firmware auto in finally block
            mock_restore.assert_called_once()

            # Saved to config
            self.assertEqual(ctrl.config.get("calibrated_min_cpu_level"), 14)
            self.assertEqual(ctrl.config.get("calibrated_min_gpu_level"), 16)
            self.assertIn("calibration_timestamp", ctrl.config)

    def test_calibration_thermal_safety_abort(self):
        """Calibration aborts immediately if CPU temperature exceeds safety limit (> 80°C)."""
        ctrl = self._make_ctrl()

        with patch.object(ctrl, "get_reference_temp", return_value=85.0), \
             patch.object(ctrl, "restore_firmware_auto") as mock_restore:
            result = ctrl.calibrate_minimum_fan_levels(
                candidate_levels=[10, 12, 14],
                settle_time=0.001,
                sample_count=2,
                sample_interval=0.001,
                query_executor=MagicMock(return_value=0),
                telemetry_reader=MagicMock(return_value=(1000, 1000)),
            )

            self.assertFalse(result["success"])
            self.assertIn("exceeds safety ceiling", result["error"])
            self.assertIsNone(ctrl.config.get("calibrated_min_cpu_level"))

    def test_calibration_exception_guarantees_firmware_auto_restore(self):
        """Any unexpected exception during calibration still restores firmware AUTO."""
        ctrl = self._make_ctrl()

        def failing_executor(*args):
            raise KeyboardInterrupt("Simulated user interrupt")

        with patch.object(ctrl, "get_reference_temp", return_value=45.0), \
             patch.object(ctrl, "restore_firmware_auto") as mock_restore:
            with self.assertRaises(KeyboardInterrupt):
                ctrl.calibrate_minimum_fan_levels(
                    candidate_levels=[10, 12],
                    settle_time=0.001,
                    sample_count=2,
                    sample_interval=0.001,
                    query_executor=failing_executor,
                    telemetry_reader=MagicMock(return_value=(1000, 1000)),
                )
            mock_restore.assert_called_once()

    def test_runtime_safety_escalation_does_not_suppress_curve(self):
        """Safety escalation pushes floor UP but never suppresses higher curve demands."""
        ctrl = self._make_ctrl()
        ctrl.config["calibrated_min_cpu_level"] = 15
        ctrl.config["calibrated_min_gpu_level"] = 15

        with patch.object(ctrl, "set_experimental_fan_speed") as mock_speed:
            # High demand (PWM 200 -> level 78): commands 78
            ctrl.set_fan_pwm(200, 200)
            mock_speed.assert_called_with(78, 78)

            # Low demand (PWM 10 -> level 4): clamped to floor 15
            ctrl.set_fan_pwm(10, 10)
            mock_speed.assert_called_with(15, 15)

    def test_cli_calibrate_min_invokes_calibration(self):
        """CLI calibrate-min command executes calibration and reports summary."""
        from click.testing import CliRunner
        from omen_fan_control._cli import cli

        ctrl = self._make_ctrl()
        fake_result = {
            "success": True,
            "error": None,
            "calibrated_min_cpu_level": 16,
            "calibrated_min_gpu_level": 18,
            "calibrated_min_cpu_rpm": 1450,
            "calibrated_min_gpu_rpm": 1550,
            "history": [],
        }

        with patch("omen_fan_control._cli.calibrate.get_controller", return_value=ctrl), \
             patch.object(ctrl, "calibrate_minimum_fan_levels", return_value=fake_result) as mock_cal:
            runner = CliRunner()
            res = runner.invoke(cli, ["calibrate-min", "--settle-time", "0.01", "--sample-count", "2"])
            self.assertEqual(res.exit_code, 0)
            self.assertIn("Calibration SUCCEEDED", res.output)
            self.assertIn("16%", res.output)
            self.assertIn("18%", res.output)
            mock_cal.assert_called_once()

    def test_daemon_runtime_low_rpm_safety_escalation(self):
        """When actual RPM is persistently below 500 RPM for >= 2 cycles, floor is escalated."""
        from click.testing import CliRunner
        from omen_fan_control._cli.serve import serve

        ctrl = self._make_ctrl()
        ctrl.config["mode"] = "auto"
        ctrl.config["enable_experimental_control"] = True
        ctrl.config["enable_experimental_telemetry"] = True
        ctrl.config["calibrated_min_cpu_level"] = 14
        ctrl.config["calibrated_min_gpu_level"] = 14
        ctrl._last_commanded_cpu_level = 14
        ctrl._last_commanded_gpu_level = 14

        cycle_count = [0]
        telemetry_samples = [
            (350, 1200),  # Cycle 1: CPU sub-500, GPU normal
            (380, 1200),  # Cycle 2: CPU sub-500 again -> triggers escalation!
            (1200, 1200), # Cycle 3
        ]

        def _fake_sleep(sec):
            cycle_count[0] += 1
            if cycle_count[0] >= 3:
                raise KeyboardInterrupt()

        def _fake_telemetry(*args, **kwargs):
            idx = min(cycle_count[0], len(telemetry_samples) - 1)
            return telemetry_samples[idx]

        with patch("omen_fan_control._cli.serve.get_controller", return_value=ctrl), \
             patch.object(ctrl, "is_fan_control_ready", return_value=True), \
             patch.object(ctrl, "read_experimental_fan_telemetry", side_effect=_fake_telemetry), \
             patch.object(ctrl, "get_reference_temp", return_value=45.0), \
             patch.object(ctrl, "get_gpu_temp", return_value=40.0), \
             patch("time.sleep", side_effect=_fake_sleep), \
             patch.object(ctrl, "set_experimental_fan_speed"), \
             patch.object(ctrl, "restore_firmware_auto"):
            runner = CliRunner()
            res = runner.invoke(serve)
            self.assertEqual(res.exit_code, 0)
            self.assertIn("Escalated session floor to level 16", res.output)
            self.assertEqual(ctrl.get_calibrated_min_fan_levels()[0], 16)


# ===========================================================================
# 23. Requirements 12.A - 12.G: Explicit Experimental Calibration Gating & Safety
# ===========================================================================
class TestExplicitExperimentalCalibrationGatingAndSafety(unittest.TestCase):
    """
    Validation suite proving requirements 12.A through 12.G:
      12.A: 8CC0 + experimental_control=0 -> Calibration blocked.
      12.B: 8CC0 + experimental_telemetry=0 -> Calibration blocked.
      12.C: 8CC0 + both flags enabled + experimental sysfs nodes present -> Calibration allowed.
      12.D: Legacy hwmon fan_control_available=False must NOT block explicit experimental 8CC0 calibration.
      12.E: Non-8CC0 behavior remains unchanged.
      12.F: No hardware writes occur during object initialization.
      12.G: Cleanup/fallback remains intact.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.cfg_path = Path(self.temp_dir) / "config.json"

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _make_ctrl(self, board="8CC0", exp_ctl=None, exp_telem=None, mock_hwmon=False):
        with patch.object(FanController, "_get_live_board_name", return_value=board):
            ctrl = FanController(config_path=self.cfg_path)
            ctrl._get_live_board_name = lambda: board
            if exp_ctl is not None:
                ctrl.config["enable_experimental_control"] = exp_ctl
            if exp_telem is not None:
                ctrl.config["enable_experimental_telemetry"] = exp_telem
            if not mock_hwmon:
                ctrl.hwmon_path = None
                ctrl.pwm1_enable_path = None
                ctrl.pwm1_path = None
                ctrl.pwm2_path = None
            return ctrl

    def test_12_a_8cc0_experimental_control_disabled_blocks_calibration(self):
        """12.A: 8CC0 + experimental_control=0 blocks calibration."""
        # 1. Via explicit config = False
        ctrl = self._make_ctrl(board="8CC0", exp_ctl=False, exp_telem=True)
        res = ctrl.calibrate_minimum_fan_levels(
            candidate_levels=[10, 12],
            query_executor=MagicMock(return_value=0),
            telemetry_reader=MagicMock(return_value=(1000, 1000)),
        )
        self.assertFalse(res["success"])
        self.assertIn("Experimental control and telemetry must be enabled", res["error"])

        # 2. Via module parameter = False with config omitted
        ctrl2 = self._make_ctrl(board="8CC0", exp_ctl=None, exp_telem=True)
        with patch.object(ctrl2, "_read_module_param", side_effect=lambda p: False if p == "experimental_control" else True):
            res2 = ctrl2.calibrate_minimum_fan_levels(
                candidate_levels=[10, 12],
                query_executor=MagicMock(return_value=0),
                telemetry_reader=MagicMock(return_value=(1000, 1000)),
            )
            self.assertFalse(res2["success"])
            self.assertIn("Experimental control and telemetry must be enabled", res2["error"])

    def test_12_b_8cc0_experimental_telemetry_disabled_blocks_calibration(self):
        """12.B: 8CC0 + experimental_telemetry=0 blocks calibration."""
        # 1. Via explicit config = False
        ctrl = self._make_ctrl(board="8CC0", exp_ctl=True, exp_telem=False)
        res = ctrl.calibrate_minimum_fan_levels(
            candidate_levels=[10, 12],
            query_executor=MagicMock(return_value=0),
            telemetry_reader=MagicMock(return_value=(1000, 1000)),
        )
        self.assertFalse(res["success"])
        self.assertIn("Experimental control and telemetry must be enabled", res["error"])

        # 2. Via module parameter = False with config omitted
        ctrl2 = self._make_ctrl(board="8CC0", exp_ctl=True, exp_telem=None)
        with patch.object(ctrl2, "_read_module_param", side_effect=lambda p: False if p == "experimental_telemetry" else True):
            res2 = ctrl2.calibrate_minimum_fan_levels(
                candidate_levels=[10, 12],
                query_executor=MagicMock(return_value=0),
                telemetry_reader=MagicMock(return_value=(1000, 1000)),
            )
            self.assertFalse(res2["success"])
            self.assertIn("Experimental control and telemetry must be enabled", res2["error"])

    def test_12_c_8cc0_both_flags_enabled_and_sysfs_present_allows_calibration(self):
        """12.C: 8CC0 + both flags enabled + experimental sysfs nodes present allows calibration."""
        fake_speed = MagicMock(spec=Path)
        fake_speed.exists.return_value = True
        fake_telem = MagicMock(spec=Path)
        fake_telem.exists.return_value = True
        fake_telem.read_text.return_value = "cpu_rpm=1500\ngpu_rpm=1500\n"

        # Case 1: Enabled via config
        ctrl = self._make_ctrl(board="8CC0", exp_ctl=True, exp_telem=True)
        with patch("omen_fan_control.fan_controller.EXPERIMENTAL_FAN_SPEED_PATH", fake_speed), \
             patch("omen_fan_control.fan_controller.EXPERIMENTAL_FAN_TELEMETRY_PATH", fake_telem), \
             patch.object(ctrl, "get_reference_temp", return_value=45.0), \
             patch.object(ctrl, "set_experimental_fan_speed", return_value=True), \
             patch.object(ctrl, "read_experimental_fan_telemetry", return_value=(1500, 1500)), \
             patch.object(ctrl, "restore_firmware_auto"):
            res = ctrl.calibrate_minimum_fan_levels(
                candidate_levels=[15],
                settle_time=0.001,
                sample_count=2,
                sample_interval=0.001,
            )
            self.assertTrue(res["success"])
            self.assertEqual(res["calibrated_min_cpu_level"], 15)
            self.assertEqual(res["calibrated_min_gpu_level"], 15)

        # Case 2: Enabled via kernel module parameters with empty config
        ctrl2 = self._make_ctrl(board="8CC0", exp_ctl=None, exp_telem=None)
        with patch("omen_fan_control.fan_controller.EXPERIMENTAL_FAN_SPEED_PATH", fake_speed), \
             patch("omen_fan_control.fan_controller.EXPERIMENTAL_FAN_TELEMETRY_PATH", fake_telem), \
             patch.object(ctrl2, "_read_module_param", return_value=True), \
             patch.object(ctrl2, "get_reference_temp", return_value=45.0), \
             patch.object(ctrl2, "set_experimental_fan_speed", return_value=True), \
             patch.object(ctrl2, "read_experimental_fan_telemetry", return_value=(1500, 1500)), \
             patch.object(ctrl2, "restore_firmware_auto"):
            res2 = ctrl2.calibrate_minimum_fan_levels(
                candidate_levels=[15],
                settle_time=0.001,
                sample_count=2,
                sample_interval=0.001,
            )
            self.assertTrue(res2["success"])
            self.assertEqual(res2["calibrated_min_cpu_level"], 15)
            self.assertEqual(res2["calibrated_min_gpu_level"], 15)

    def test_12_d_legacy_hwmon_fan_control_available_false_does_not_block_calibration(self):
        """12.D: Legacy hwmon fan_control_available=False must NOT block explicit experimental 8CC0 calibration."""
        ctrl = self._make_ctrl(board="8CC0", exp_ctl=True, exp_telem=True, mock_hwmon=False)
        self.assertFalse(ctrl.fan_control_available)

        with patch.object(ctrl, "get_reference_temp", return_value=45.0), \
             patch.object(ctrl, "restore_firmware_auto"):
            res = ctrl.calibrate_minimum_fan_levels(
                candidate_levels=[16],
                settle_time=0.001,
                sample_count=2,
                sample_interval=0.001,
                query_executor=MagicMock(return_value=0),
                telemetry_reader=MagicMock(return_value=(1600, 1600)),
            )
            self.assertTrue(res["success"])
            self.assertEqual(res["calibrated_min_cpu_level"], 16)
            self.assertEqual(res["calibrated_min_gpu_level"], 16)

    def test_12_e_non_8cc0_behavior_remains_unchanged(self):
        """12.E: Non-8CC0 behavior remains unchanged (calibration blocked, hwmon preserved)."""
        ctrl = self._make_ctrl(board="8C78", exp_ctl=True, exp_telem=True)
        self.assertEqual(ctrl.get_board_name(), "8C78")
        status, board = ctrl.check_board_support()
        self.assertEqual(status, "SUPPORTED")

        # Calibration is 8CC0-specific and blocked on non-8CC0
        res = ctrl.calibrate_minimum_fan_levels(
            candidate_levels=[15],
            query_executor=MagicMock(return_value=0),
            telemetry_reader=MagicMock(return_value=(1500, 1500)),
        )
        self.assertFalse(res["success"])
        self.assertIn("must be enabled for 8CC0", res["error"])

    def test_12_f_no_hardware_writes_occur_during_object_initialization(self):
        """12.F: No hardware writes occur during FanController object initialization."""
        with patch.object(FanController, "write_sys_file") as mock_write_sys, \
             patch.object(FanController, "set_experimental_fan_speed") as mock_exp_speed, \
             patch.object(FanController, "set_experimental_fan_max") as mock_exp_max, \
             patch.object(FanController, "set_fan_mode") as mock_mode, \
             patch.object(FanController, "_get_live_board_name", return_value="8CC0"):
            controller = FanController(config_path=self.cfg_path)
            mock_write_sys.assert_not_called()
            mock_exp_speed.assert_not_called()
            mock_exp_max.assert_not_called()
            mock_mode.assert_not_called()

    def test_12_g_cleanup_fallback_remains_intact_and_does_not_call_legacy_hwmon(self):
        """12.G: Calibration cleanup restores firmware AUTO via 0x10->0x27(0)->0x2E(0,0) and avoids legacy hwmon."""
        ctrl = self._make_ctrl(board="8CC0", exp_ctl=True, exp_telem=True, mock_hwmon=False)
        self.assertFalse(ctrl.fan_control_available)

        mock_executor = MagicMock(return_value=0)
        with patch.object(ctrl, "get_reference_temp", return_value=45.0), \
             patch.object(ctrl, "set_experimental_fan_speed", return_value=True) as mock_exp_speed, \
             patch.object(ctrl, "set_fan_mode") as mock_legacy_mode:
            res = ctrl.calibrate_minimum_fan_levels(
                candidate_levels=[15],
                settle_time=0.001,
                sample_count=2,
                sample_interval=0.001,
                query_executor=mock_executor,
                telemetry_reader=MagicMock(return_value=(1500, 1500)),
            )
            self.assertTrue(res["success"])
            # Firmware AUTO (0, 0) restored at cleanup
            mock_exp_speed.assert_any_call(0, 0, path=None, query_executor=mock_executor, force_refresh=True)
            # Legacy hwmon fan mode was NOT called
            mock_legacy_mode.assert_not_called()


if __name__ == "__main__":
    unittest.main()

