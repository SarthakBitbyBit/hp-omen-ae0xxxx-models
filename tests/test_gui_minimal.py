from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PyQt6.QtWidgets import QApplication

os.environ["QT_QPA_PLATFORM"] = "offscreen"
_qapp = QApplication.instance() or QApplication([])

from omen_fan_control.gui import MainWindow, ModeButton, TelemetryCard, check_root_execution


class TestMinimalGui(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.status_file = Path(self.temp_dir) / "status.json"
        self.volatile_cfg = Path(self.temp_dir) / "config.json"

    def test_check_root_execution(self):
        with patch("os.geteuid", return_value=0):
            self.assertTrue(check_root_execution())
        with patch("os.geteuid", return_value=1000):
            self.assertFalse(check_root_execution())

    def test_mode_button_state_toggle(self):
        btn = ModeButton("auto", "AUTO", "Thermal curve", "#0284c7")
        self.assertFalse(btn.is_active)
        btn.set_active(True)
        self.assertTrue(btn.is_active)
        btn.set_active(False)
        self.assertFalse(btn.is_active)

    def test_telemetry_card_display(self):
        card = TelemetryCard("CPU Temp", "°C")
        self.assertEqual(card.title, "CPU Temp")
        card.set_value("62.5", "#38bdf8")
        self.assertEqual(card.value_label.text(), "62.5 °C")

        card.set_value("—")
        self.assertEqual(card.value_label.text(), "—")

    @patch("omen_fan_control.gui.check_root_execution", return_value=False)
    def test_main_window_init(self, mock_root):
        with (
            patch("omen_fan_control.gui.SYSTEM_STATUS_FILE", self.status_file),
            patch("omen_fan_control.gui.SYSTEM_VOLATILE_CONFIG_FILE", self.volatile_cfg),
            patch("omen_fan_control.logic.FanController.get_board_name", return_value="8CC0"),
        ):
            window = MainWindow()
            window.show()
            self.assertEqual(window.windowTitle(), "OMEN Fan Control")
            self.assertEqual(window.current_mode, "auto")
            self.assertIn("8CC0", window.board_label.text())

    @patch("omen_fan_control.gui.check_root_execution", return_value=False)
    def test_apply_mode_writes_volatile_config(self, mock_root):
        with (
            patch("omen_fan_control.gui.SYSTEM_STATUS_FILE", self.status_file),
            patch("omen_fan_control.gui.SYSTEM_VOLATILE_CONFIG_FILE", self.volatile_cfg),
        ):
            window = MainWindow()
            window.show()
            window.is_ready = True
            window.apply_mode("performance")

            self.assertEqual(window.current_mode, "performance")
            self.assertTrue(window.btn_perf.is_active)
            self.assertFalse(window.btn_auto.is_active)

            # Check that volatile config was written
            self.assertTrue(self.volatile_cfg.exists())
            data = json.loads(self.volatile_cfg.read_text())
            self.assertEqual(data["mode"], "performance")

    @patch("omen_fan_control.gui.check_root_execution", return_value=False)
    def test_poll_status_reads_daemon_status(self, mock_root):
        import time

        with (
            patch("omen_fan_control.gui.SYSTEM_STATUS_FILE", self.status_file),
            patch("omen_fan_control.gui.SYSTEM_VOLATILE_CONFIG_FILE", self.volatile_cfg),
        ):
            window = MainWindow()
            window.show()

            # Simulate daemon writing status.json
            payload = {
                "timestamp": time.time(),
                "pid": 12345,
                "mode": "max",
                "cpu_temp": 78.4,
                "gpu_temp": 64.2,
                "fan1_rpm": 4500,
                "fan2_rpm": 4800,
                "emergency": False,
                "board": "8CC0",
                "ready": True,
            }
            self.status_file.write_text(json.dumps(payload))

            window.poll_status()

            self.assertEqual(window.current_mode, "max")
            self.assertFalse(window.max_banner.isHidden())
            self.assertTrue(window.emergency_banner.isHidden())
            self.assertTrue(window.readonly_banner.isHidden())
            self.assertIn("Daemon Active", window.daemon_badge.text())
            self.assertEqual(window.card_cpu_temp.value_label.text(), "78.4 °C")
            self.assertEqual(window.card_gpu_temp.value_label.text(), "64.2 °C")
            self.assertEqual(window.card_fan1_rpm.value_label.text(), "4500 RPM")
            self.assertEqual(window.card_fan2_rpm.value_label.text(), "4800 RPM")

    @patch("omen_fan_control.gui.check_root_execution", return_value=False)
    def test_emergency_banner_visibility(self, mock_root):
        import time

        with (
            patch("omen_fan_control.gui.SYSTEM_STATUS_FILE", self.status_file),
            patch("omen_fan_control.gui.SYSTEM_VOLATILE_CONFIG_FILE", self.volatile_cfg),
        ):
            window = MainWindow()
            window.show()
            payload = {
                "timestamp": time.time(),
                "mode": "auto",
                "cpu_temp": 88.0,
                "gpu_temp": 82.0,
                "fan1_rpm": 5700,
                "fan2_rpm": 6100,
                "emergency": True,
                "ready": True,
            }
            self.status_file.write_text(json.dumps(payload))
            window.poll_status()

            self.assertFalse(window.emergency_banner.isHidden())

    @patch("omen_fan_control.gui.check_root_execution", return_value=False)
    def test_readonly_banner_when_not_ready(self, mock_root):
        import time

        with (
            patch("omen_fan_control.gui.SYSTEM_STATUS_FILE", self.status_file),
            patch("omen_fan_control.gui.SYSTEM_VOLATILE_CONFIG_FILE", self.volatile_cfg),
        ):
            window = MainWindow()
            window.show()
            payload = {
                "timestamp": time.time(),
                "mode": "auto",
                "cpu_temp": 50.0,
                "gpu_temp": 45.0,
                "fan1_rpm": None,
                "fan2_rpm": None,
                "emergency": False,
                "ready": False,
            }
            self.status_file.write_text(json.dumps(payload))
            window.poll_status()

            self.assertFalse(window.readonly_banner.isHidden())


if __name__ == "__main__":
    unittest.main()
