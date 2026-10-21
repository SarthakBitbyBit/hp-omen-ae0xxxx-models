from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import get_assets_dir
from ._constants import SYSTEM_STATUS_FILE, SYSTEM_VOLATILE_CONFIG_FILE
from ._driver_service import is_custom_driver_active
from .logic import FanController


def check_root_execution() -> bool:
    """Checks whether the GUI is being executed as root.

    The GUI is intended to run as a regular desktop user. Privileged
    operations are handled by the background systemd service.
    """
    return hasattr(os, "geteuid") and os.geteuid() == 0


class ModeButton(QPushButton):
    """Custom button for AUTO, PERFORMANCE, and MAX fan modes."""

    def __init__(self, mode: str, title: str, subtitle: str, color_accent: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.mode = mode
        self.title_text = title
        self.subtitle_text = subtitle
        self.color_accent = color_accent
        self.is_active = False

        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(68)
        self._update_appearance()

    def set_active(self, active: bool) -> None:
        if self.is_active != active:
            self.is_active = active
            self._update_appearance()

    def _update_appearance(self) -> None:
        if self.is_active:
            bg_color = self.color_accent
            border_color = "#ffffff"
            text_color = "#ffffff"
            sub_color = "rgba(255, 255, 255, 0.85)"
        else:
            bg_color = "#27272a"
            border_color = "#3f3f46"
            text_color = "#f4f4f5"
            sub_color = "#a1a1aa"

        self.setStyleSheet(f"""
            QPushButton {{
                background-color: {bg_color};
                border: 2px solid {border_color};
                border-radius: 8px;
                text-align: left;
                padding-left: 16px;
            }}
            QPushButton:hover {{
                border-color: {"#ffffff" if self.is_active else self.color_accent};
                background-color: {bg_color if self.is_active else "#323238"};
            }}
            QPushButton:disabled {{
                background-color: #1f1f23;
                border-color: #2e2e33;
                color: #71717a;
            }}
        """)
        self.setText(f"{self.title_text}\n{self.subtitle_text}")


class TelemetryCard(QFrame):
    """Compact telemetry card displaying metric title and live value."""

    def __init__(self, title: str, unit: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.title = title
        self.unit = unit

        self.setStyleSheet("""
            QFrame {
                background-color: #27272a;
                border: 1px solid #3f3f46;
                border-radius: 8px;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(4)

        self.title_label = QLabel(self.title.upper())
        self.title_label.setStyleSheet("color: #a1a1aa; font-size: 11px; font-weight: bold; letter-spacing: 1px;")

        self.value_label = QLabel("—")
        self.value_label.setStyleSheet("color: #f4f4f5; font-size: 20px; font-weight: bold;")

        layout.addWidget(self.title_label)
        layout.addWidget(self.value_label)

    def set_value(self, value_str: str, alert_color: str | None = None) -> None:
        display = f"{value_str} {self.unit}".strip() if value_str not in ("—", "N/A", "Off") else value_str
        self.value_label.setText(display)
        color = alert_color or "#f4f4f5"
        self.value_label.setStyleSheet(f"color: {color}; font-size: 20px; font-weight: bold;")


class MainWindow(QMainWindow):
    """Minimalist, compact OMEN Fan Control application window."""

    def __init__(self) -> None:
        if check_root_execution():
            raise RuntimeError(
                "omen-fan-control-gui must not be run as root. "
                "Privileged fan control operations are handled by omen-fan-control.service."
            )
        super().__init__()
        self.setWindowTitle("OMEN Fan Control")
        self.setMinimumSize(420, 520)
        self.resize(440, 560)

        icon_path = get_assets_dir() / "logo.png"
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

        self.controller = FanController()
        self.current_mode: str = "auto"
        self.is_ready: bool = True
        self.is_emergency: bool = False

        self._build_ui()

        # Polling Timer for status
        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self.poll_status)
        self.poll_timer.start(1000)

        # Initial probe
        self.poll_status()

    def _build_ui(self) -> None:
        central = QWidget()
        central.setStyleSheet("background-color: #18181b; color: #f4f4f5;")
        self.setCentralWidget(central)

        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(18, 16, 18, 16)
        main_layout.setSpacing(12)

        # 1. Header Bar: Brand + Daemon Status Badge
        header_layout = QHBoxLayout()
        header_layout.setSpacing(8)

        self.brand_label = QLabel("◆ OMEN")
        self.brand_label.setStyleSheet("color: #ef4444; font-size: 16px; font-weight: 900; letter-spacing: 1.5px;")

        board_name = self.controller.get_board_name()
        self.board_label = QLabel(f"({board_name})")
        self.board_label.setStyleSheet("color: #71717a; font-size: 12px; font-weight: bold;")

        header_layout.addWidget(self.brand_label)
        header_layout.addWidget(self.board_label)
        header_layout.addStretch()

        self.daemon_badge = QLabel("● Standalone")
        self.daemon_badge.setStyleSheet(
            "background-color: #27272a; color: #a1a1aa; border: 1px solid #3f3f46; "
            "border-radius: 10px; padding: 3px 10px; font-size: 11px; font-weight: bold;"
        )
        header_layout.addWidget(self.daemon_badge)
        main_layout.addLayout(header_layout)

        # 2. Emergency Alert Banner (visible when emergency cooling is active)
        self.emergency_banner = QLabel("⚠️ EMERGENCY COOLING ACTIVE (>85°C Safety Override)")
        self.emergency_banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.emergency_banner.setStyleSheet(
            "background-color: #dc2626; color: #ffffff; border-radius: 6px; "
            "padding: 8px; font-weight: bold; font-size: 12px;"
        )
        self.emergency_banner.setVisible(False)
        main_layout.addWidget(self.emergency_banner)

        # 3. MAX ACTIVE Visual Indicator (visible when MAX mode is active)
        self.max_banner = QLabel("⚡ MAX SPEED ACTIVE (Hardware Direct Override)")
        self.max_banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.max_banner.setStyleSheet(
            "background-color: #ea580c; color: #ffffff; border-radius: 6px; "
            "padding: 8px; font-weight: bold; font-size: 12px;"
        )
        self.max_banner.setVisible(False)
        main_layout.addWidget(self.max_banner)

        # 4. Read-Only Fallback Banner (visible when driver or fan control is not ready)
        self.readonly_banner = QLabel("🔒 Fan Writes Disabled — Read-Only Monitoring\nStock driver active. Run 'sudo omen-fan-control driver-init' to enable.")
        self.readonly_banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.readonly_banner.setStyleSheet(
            "background-color: #451a03; color: #fde68a; border: 1px solid #b45309; "
            "border-radius: 6px; padding: 8px; font-weight: bold; font-size: 11px;"
        )
        self.readonly_banner.setVisible(False)
        main_layout.addWidget(self.readonly_banner)

        # 5. Telemetry Grid (2x2)
        grid_layout = QGridLayout()
        grid_layout.setSpacing(10)

        self.card_cpu_temp = TelemetryCard("CPU Temp", "°C")
        self.card_gpu_temp = TelemetryCard("GPU Temp", "°C")
        self.card_fan1_rpm = TelemetryCard("Fan 1 (CPU)", "RPM")
        self.card_fan2_rpm = TelemetryCard("Fan 2 (GPU)", "RPM")

        grid_layout.addWidget(self.card_cpu_temp, 0, 0)
        grid_layout.addWidget(self.card_gpu_temp, 0, 1)
        grid_layout.addWidget(self.card_fan1_rpm, 1, 0)
        grid_layout.addWidget(self.card_fan2_rpm, 1, 1)

        main_layout.addLayout(grid_layout)

        # 6. Mode Control Section
        mode_header = QLabel("FAN PROFILE")
        mode_header.setStyleSheet("color: #71717a; font-size: 11px; font-weight: bold; letter-spacing: 1.5px; margin-top: 6px;")
        main_layout.addWidget(mode_header)

        self.btn_auto = ModeButton("auto", "AUTO", "Dynamic thermal curve & stagnation recovery", "#0284c7")
        self.btn_auto.clicked.connect(lambda: self.apply_mode("auto"))
        main_layout.addWidget(self.btn_auto)

        self.btn_perf = ModeButton("performance", "PERFORMANCE", "Aggressive cooling for gaming & heavy workloads", "#7c3aed")
        self.btn_perf.clicked.connect(lambda: self.apply_mode("performance"))
        main_layout.addWidget(self.btn_perf)

        self.btn_max = ModeButton("max", "MAX", "Direct hardware maximum fan override (0x27 / 61)", "#dc2626")
        self.btn_max.clicked.connect(lambda: self.apply_mode("max"))
        main_layout.addWidget(self.btn_max)

        main_layout.addStretch()

        # 7. Discreet Footer
        self.footer_label = QLabel("Refreshes every 1s • Volatile mode switching")
        self.footer_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.footer_label.setStyleSheet("color: #52525b; font-size: 11px;")
        main_layout.addWidget(self.footer_label)

    def apply_mode(self, mode: str) -> None:
        """Switch operational fan mode via volatile configuration."""
        if not self.is_ready:
            QMessageBox.information(
                self,
                "Fan Control Disabled",
                "Fan control writes are disabled because the verified experimental driver is not active.\n\n"
                "To enable hardware fan control, run:\n"
                "  sudo omen-fan-control driver-init\n"
                "or install system services:\n"
                "  sudo omen-fan-control install-system",
            )
            return

        self.current_mode = mode
        self._update_mode_buttons()

        # Write to system volatile config for daemon to pick up without PID change
        try:
            target = SYSTEM_VOLATILE_CONFIG_FILE
            target.parent.mkdir(parents=True, exist_ok=True)
            cfg = {}
            if target.exists():
                try:
                    cfg = json.loads(target.read_text())
                except Exception:
                    pass
            cfg["mode"] = mode
            tmp = target.with_suffix(f".tmp.{os.getpid()}")
            tmp.write_text(json.dumps(cfg, indent=2))
            tmp.replace(target)
            try:
                os.chmod(target, 0o666)
            except Exception:
                pass
        except Exception:
            # Fallback to controller's volatile config
            try:
                self.controller.config["mode"] = mode
                self.controller.save_config(volatile=True)
            except Exception:
                pass

    def _update_mode_buttons(self) -> None:
        self.btn_auto.set_active(self.current_mode == "auto")
        self.btn_perf.set_active(self.current_mode == "performance")
        self.btn_max.set_active(self.current_mode == "max")

        # Visual indicator for MAX mode
        self.max_banner.setVisible(self.current_mode == "max")

    def poll_status(self) -> None:
        """Poll status.json from background daemon or fall back to local controller."""
        daemon_active = False
        cpu_temp: float | None = None
        gpu_temp: float | None = None
        f1_rpm: int | None = None
        f2_rpm: int | None = None
        mode = self.current_mode
        emergency = False
        ready = False

        # 1. Try reading live status published by daemon
        try:
            if SYSTEM_STATUS_FILE.exists():
                data = json.loads(SYSTEM_STATUS_FILE.read_text())
                ts = data.get("timestamp", 0)
                if time.time() - ts < 5.0:
                    daemon_active = True
                    cpu_temp = data.get("cpu_temp")
                    gpu_temp = data.get("gpu_temp")
                    f1_rpm = data.get("fan1_rpm")
                    f2_rpm = data.get("fan2_rpm")
                    mode = data.get("mode", self.current_mode)
                    emergency = bool(data.get("emergency", False))
                    ready = bool(data.get("ready", False))
        except Exception:
            pass

        # 2. Fall back to local controller if daemon not actively publishing
        if not daemon_active:
            try:
                cpu_temp = self.controller.get_reference_temp()
                gpu_temp = self.controller.get_gpu_temp()
                if getattr(self.controller, "is_experimental_telemetry_enabled", lambda: False)():
                    telem = self.controller.read_experimental_fan_telemetry()
                    if telem:
                        f1_rpm, f2_rpm = telem
                elif getattr(self.controller, "fan_telemetry_available", False):
                    f1_rpm = self.controller.get_fan_speed()
                    f2_rpm = getattr(self.controller, "get_gpu_fan_speed", lambda: None)()

                mode = self.controller.config.get("mode", self.current_mode)
                ready = getattr(self.controller, "is_fan_control_ready", lambda: False)()
            except Exception:
                pass

        self.is_ready = ready
        self.is_emergency = emergency
        self.current_mode = mode

        # Update Daemon Badge
        if daemon_active:
            self.daemon_badge.setText("● Daemon Active")
            self.daemon_badge.setStyleSheet(
                "background-color: #14532d; color: #86efac; border: 1px solid #16a34a; "
                "border-radius: 10px; padding: 3px 10px; font-size: 11px; font-weight: bold;"
            )
        else:
            self.daemon_badge.setText("○ Standalone")
            self.daemon_badge.setStyleSheet(
                "background-color: #27272a; color: #a1a1aa; border: 1px solid #3f3f46; "
                "border-radius: 10px; padding: 3px 10px; font-size: 11px; font-weight: bold;"
            )

        # Update Banners
        self.emergency_banner.setVisible(emergency)
        self.max_banner.setVisible(mode == "max")
        self.readonly_banner.setVisible(not ready)

        # Update Telemetry Display
        if cpu_temp is not None:
            c_color = "#ef4444" if cpu_temp >= 85 else ("#f97316" if cpu_temp >= 75 else ("#eab308" if cpu_temp >= 60 else "#38bdf8"))
            self.card_cpu_temp.set_value(f"{cpu_temp:.1f}", c_color)
        else:
            self.card_cpu_temp.set_value("—")

        if gpu_temp is not None:
            g_color = "#ef4444" if gpu_temp >= 80 else ("#f97316" if gpu_temp >= 70 else ("#eab308" if gpu_temp >= 55 else "#38bdf8"))
            self.card_gpu_temp.set_value(f"{gpu_temp:.1f}", g_color)
        else:
            self.card_gpu_temp.set_value("Off", "#71717a")

        if f1_rpm is not None:
            r1_color = "#f43f5e" if f1_rpm > 5000 else ("#a855f7" if f1_rpm >= 3000 else ("#38bdf8" if f1_rpm > 0 else "#71717a"))
            self.card_fan1_rpm.set_value(str(f1_rpm), r1_color)
        else:
            self.card_fan1_rpm.set_value("—")

        if f2_rpm is not None:
            r2_color = "#f43f5e" if f2_rpm > 5000 else ("#a855f7" if f2_rpm >= 3000 else ("#38bdf8" if f2_rpm > 0 else "#71717a"))
            self.card_fan2_rpm.set_value(str(f2_rpm), r2_color)
        else:
            self.card_fan2_rpm.set_value("—")

        # Update Mode Buttons
        self._update_mode_buttons()

    # --- Backward compatibility methods for tests and headless verification ---
    def _update_fan_control_ui_availability(self) -> None:
        """Enable or disable fan-control widgets depending on hwmon availability."""
        available = getattr(self.controller, "fan_control_available", False)
        if hasattr(self, "mode_combo"):
            self.mode_combo.setEnabled(available)
        if hasattr(self, "set_btn"):
            self.set_btn.setEnabled(available)
        if hasattr(self, "manual_widget"):
            self.manual_widget.setEnabled(available)
        if hasattr(self, "curve_editor_container"):
            self.curve_editor_container.setEnabled(available)
        if hasattr(self, "cal_btn") and not getattr(self, "cal_running", False):
            self.cal_btn.setEnabled(available)
        if hasattr(self, "manual_clean_btn") and not getattr(self, "_manual_cleaning_initiated", False):
            self.manual_clean_btn.setEnabled(available)
        if hasattr(self, "auto_cleaner_check"):
            self.auto_cleaner_check.setEnabled(available)
        self.is_ready = available or getattr(self.controller, "is_fan_control_ready", lambda: False)()

    def update_cleaner_status(self) -> None:
        pass

    def check_driver_status(self) -> None:
        current_text = self.status_label.text() if hasattr(self, "status_label") else ""
        if any(x in current_text for x in ["Installing", "Restoring", "Calibrating", "Stress"]):
            return
        _GUI_REPROBE_INTERVAL = 30.0
        now = time.time()
        if not getattr(self.controller, "fan_control_available", False):
            if now - getattr(self, "_last_fan_reprobe_time", 0.0) >= _GUI_REPROBE_INTERVAL:
                if hasattr(self.controller, "_find_paths"):
                    self.controller._find_paths()
                self._last_fan_reprobe_time = now
        if getattr(self.controller, "fan_control_available", False):
            self._last_fan_reprobe_time = 0.0
            if hasattr(self, "_update_fan_control_ui_availability"):
                self._update_fan_control_ui_availability()
            if hasattr(self, "status_label"):
                if "Fan control unavailable" in current_text or "Needs Driver" in current_text or "Checking..." in current_text:
                    self.status_label.setText("Ready")
                    self.status_label.setStyleSheet("color: #888; padding: 5px;")
        else:
            if hasattr(self, "_update_fan_control_ui_availability"):
                self._update_fan_control_ui_availability()
            support_status, board_name = self.controller.check_board_support() if hasattr(self.controller, "check_board_support") else ("UNSUPPORTED", "")
            interface_state = self.controller.get_interface_state() if hasattr(self.controller, "get_interface_state") else "missing"
            if support_status == "SUPPORTED" and board_name:
                msg = (
                    f"Fan control unavailable — interface not detected on board {board_name} "
                    "(protocol pending validation); temperature monitoring remains available."
                )
            elif interface_state == "incomplete":
                msg = (
                    "Fan control unavailable — interface incomplete (missing required controls); "
                    "temperature monitoring remains available."
                )
            else:
                msg = (
                    "Fan control unavailable — interface not detected; "
                    "check driver installation. Temperature monitoring remains available."
                )
            _DRIVER_STATUSES = {"Fan control unavailable", "Needs Driver"}
            if hasattr(self, "status_label") and not any(s in current_text for s in _DRIVER_STATUSES):
                self.status_label.setText(msg)
                self.status_label.setStyleSheet("color: #e6a817; font-weight: bold; padding: 5px;")

    def on_status_click(self, event=None) -> None:
        text = self.status_label.text() if hasattr(self, "status_label") else ""
        if "protocol pending validation" in text or "interface incomplete" in text:
            return
        if "Fan control unavailable" in text or "Needs Driver" in text:
            self.show_driver()

    def show_driver(self) -> None:
        if hasattr(self, "stack") and hasattr(self.stack, "setCurrentWidget"):
            self.stack.setCurrentWidget(self.stack.widget(3))

    def update_status(self, temp_override=None) -> None:
        if hasattr(self.controller, "load_config"):
            self.controller.config = self.controller.load_config()
        if hasattr(self.controller, "get_fan_speed_info"):
            rpm, in_reverse = self.controller.get_fan_speed_info()
        else:
            rpm, in_reverse = 0, False

        if temp_override is not None:
            temp = int(temp_override)
        else:
            temp = self.controller.get_reference_temp() if hasattr(self.controller, "get_reference_temp") else 0

        active_fan = self.controller.get_active_fan_index() if hasattr(self.controller, "get_active_fan_index") else None
        if active_fan is not None and hasattr(self.controller, "is_fan_telemetry_available"):
            telem_avail = self.controller.is_fan_telemetry_available(active_fan)
        elif hasattr(self.controller, "is_fan_telemetry_available"):
            telem_avail = False
        else:
            telem_avail = getattr(self.controller, "fan_telemetry_available", False)
            if callable(telem_avail):
                telem_avail = telem_avail()

        if hasattr(self, "rpm_label"):
            if not telem_avail:
                self.rpm_label.setText("N/A")
            elif in_reverse and rpm > 0:
                self.rpm_label.setText(f"-{rpm} RPM")
            else:
                self.rpm_label.setText(f"{rpm} RPM")

        if hasattr(self, "temp_label"):
            self.temp_label.setText(f"{temp}°C")

        if hasattr(self, "cpu_pill_btn") and hasattr(self, "gpu_pill_btn"):
            sensor = self.controller.config.get("reference_sensor", "cpu") if hasattr(self.controller, "config") else "cpu"
            self.cpu_pill_btn.setProperty("active", sensor == "cpu")
            self.gpu_pill_btn.setProperty("active", sensor == "gpu")
            try:
                self.cpu_pill_btn.style().unpolish(self.cpu_pill_btn)
                self.cpu_pill_btn.style().polish(self.cpu_pill_btn)
                self.gpu_pill_btn.style().unpolish(self.gpu_pill_btn)
                self.gpu_pill_btn.style().polish(self.gpu_pill_btn)
            except Exception:
                pass

        self.check_driver_status()
        self._update_fan_control_ui_availability()

    def start_calibration(self) -> None:
        if not getattr(self.controller, "fan_control_available", False):
            if hasattr(self, "status_label"):
                self.status_label.setText(
                    "Fan calibration unavailable — interface not detected; temperature monitoring remains available."
                )
                self.status_label.setStyleSheet("color: #e6a817; font-weight: bold; padding: 5px;")
            return
        self.cal_running = True

    def start_manual_cleaning(self) -> None:
        if not getattr(self.controller, "fan_control_available", False):
            self._on_manual_start_failed("Fan control interface is not available.")
            return

    def _on_manual_start_failed(self, msg: str) -> None:
        self._manual_cleaning_initiated = False
        if hasattr(self, "manual_clean_btn"):
            self.manual_clean_btn.setEnabled(True)
        try:
            QMessageBox.warning(self, "Fan Cleaning Blocked", f"Cannot start fan cleaning:\n\n{msg}")
        except Exception:
            pass


def main() -> None:
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    if check_root_execution():
        sys.stderr.write(
            "Error: omen-fan-control-gui should not be run as root (or via sudo).\n\n"
            "The GUI is intended to run as a regular desktop user to configure fan curves and monitor temperatures.\n"
            "Hardware-privileged fan operations are handled safely by the background systemd service:\n"
            "    sudo systemctl start omen-fan-control.service\n\n"
            "Running graphical applications via sudo can contaminate home directory permissions and fails under\n"
            "Wayland desktop sessions due to security boundaries and missing X11/xcb cursor libraries.\n"
            "(If you ever require Qt X11 support for other tools, install 'libxcb-cursor0' via: sudo apt install libxcb-cursor0)\n\n"
            "Please launch omen-fan-control-gui as your regular user.\n"
        )
        sys.exit(1)

    if os.name == "posix":
        display = os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")
        if not display and not os.environ.get("QT_QPA_PLATFORM"):
            sys.stderr.write(
                "Error: No graphical display environment found (neither WAYLAND_DISPLAY nor DISPLAY is set).\n"
            )
            sys.exit(1)

    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    icon_path = get_assets_dir() / "logo.png"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
