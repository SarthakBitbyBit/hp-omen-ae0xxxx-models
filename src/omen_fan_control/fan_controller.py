from __future__ import annotations

import glob
import math
import os
import statistics
import struct
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Generator, Sequence

from ._cleaner import FanCleanerMixin
from ._config import ConfigDict, load_config as _load_config, save_config as _save_config
from ._constants import (
    CONFIG_FILE,
    DEFAULT_CALIBRATION_WAIT,
    HWMON_PATH_PATTERN,
    SUPPORTED_BOARDS,
    POSSIBLY_SUPPPORTED_OMEN_BOARDS,
    MONITORING_ONLY_BOARDS,
    UNSUPPORTED_TELEMETRY_BOARDS,
    WMI_CMD_GM,
    WMI_QUERY_FAN_COUNT,
    WMI_QUERY_SET_PERFORMANCE_MODE,
    WMI_QUERY_GET_GPU_THERMAL_MODES,
    WMI_QUERY_SET_GPU_THERMAL_MODES,
    WMI_QUERY_FAN_SPEED_MAX_SET,
    WMI_QUERY_GET_SYSTEM_DESIGN_DATA,
    WMI_QUERY_FAN_SPEED_SET,
    HP_OMEN_V1_THERMAL_PROFILE_DEFAULT,
    HP_OMEN_V1_THERMAL_PROFILE_PERFORMANCE,
    HP_OMEN_V1_THERMAL_PROFILE_COOL,
    HP_OMEN_V0_THERMAL_PROFILE_DEFAULT,
    HP_OMEN_V0_THERMAL_PROFILE_PERFORMANCE,
    HP_OMEN_V0_THERMAL_PROFILE_COOL,
    MIN_SOFTWARE_AUTO_FAN_LEVEL,
    FIRMWARE_AUTO_FAN_LEVEL,
    DEFAULT_MIN_REQUIRED_RPM,
    DEFAULT_FALLBACK_MIN_FAN_LEVEL,
    DEFAULT_CALIBRATION_CANDIDATE_LEVELS,
    MAX_RUNTIME_SAFETY_FLOOR,
    EXPERIMENTAL_FAN_SPEED_PATH,
    EXPERIMENTAL_FAN_TELEMETRY_PATH,
    EXPERIMENTAL_CONTROL_MODULE_PARAM,
    EXPERIMENTAL_TELEMETRY_MODULE_PARAM,
    EXPERIMENTAL_WATCHDOG_REFRESH_INTERVAL,
    AUTO_ACTIVATION_CPU_TEMP,
    AUTO_ACTIVATION_GPU_TEMP,
    AUTO_COOLDOWN_CPU_TEMP,
    AUTO_COOLDOWN_GPU_TEMP,
    ACTIVE_AUTO_MIN_RPM,
    ACTIVE_AUTO_MIN_FAN_LEVEL,
    AUTO_BREAKPOINTS_TABLE,
    AUTO_EMERGENCY_CONFIRMATION_SEC,
    AUTO_EMERGENCY_RECOVERY_TEMP,
    AUTO_EMERGENCY_TEMP_THRESHOLD,
    AUTO_MAX_FAN_LEVEL,
    AUTO_MAX_FAN_LEVEL_CPU,
    AUTO_MAX_FAN_LEVEL_GPU,
    AUTO_STAGNATION_ACTIVATION_TEMP,
    AUTO_STAGNATION_INITIAL_BOOST_LEVEL,
    AUTO_STAGNATION_MIN_IMPROVEMENT_TEMP,
    AUTO_STAGNATION_RECOVERY_TEMP,
    AUTO_STAGNATION_STEP_BOOST_LEVEL,
    AUTO_STAGNATION_STEP_INTERVAL_SEC,
    AUTO_STAGNATION_WINDOW_SEC,
    ACTIVE_PERFORMANCE_MIN_RPM,
    ACTIVE_PERFORMANCE_MIN_FAN_LEVEL,
    PERFORMANCE_ACTIVATION_CPU_TEMP,
    PERFORMANCE_ACTIVATION_GPU_TEMP,
    PERFORMANCE_COOLDOWN_CPU_TEMP,
    PERFORMANCE_COOLDOWN_GPU_TEMP,
    PERFORMANCE_CURVE_SOURCE_DEFINED,
    PERFORMANCE_CURVE_SOURCE_OBSERVED_CEILING,
    PERFORMANCE_BREAKPOINTS_TABLE,
    PERFORMANCE_EMERGENCY_TEMP_THRESHOLD,
    PERFORMANCE_EMERGENCY_CONFIRMATION_SEC,
    PERFORMANCE_EMERGENCY_RECOVERY_TEMP,
    PERFORMANCE_MAX_FAN_LEVEL,
    PERFORMANCE_MAX_FAN_LEVEL_CPU,
    PERFORMANCE_MAX_FAN_LEVEL_GPU,
)
from ._driver import DriverInstallerMixin
from ._service import ServiceManagerMixin


class EmergencySafetyStateMachine:
    """State machine for high-temperature spike filtering and emergency MAX cooling.

    Prevents transient (1-2s) sensor spikes from immediately commanding MAX.
    Requires persistence >= confirm_sec (default 3.0s / 4.0s) before escalating.
    Once active, remains in emergency cooling until temperature recovers below
    recovery_temp (default 80.0°C) to prevent MAX -> idle oscillation.
    Invalid/missing readings (None, NaN, Inf, <= 0) never clear an active emergency.
    """

    def __init__(
        self,
        threshold: float = AUTO_EMERGENCY_TEMP_THRESHOLD,
        recovery_temp: float = AUTO_EMERGENCY_RECOVERY_TEMP,
        confirm_sec: float = AUTO_EMERGENCY_CONFIRMATION_SEC,
    ) -> None:
        self.threshold: float = float(threshold)
        self.recovery_temp: float = float(recovery_temp)
        self.confirm_sec: float = float(confirm_sec)
        self.state: str = "normal"
        self.pending_start: float | None = None
        self._max_active: bool = False

    @property
    def max_active(self) -> bool:
        return self._max_active

    @max_active.setter
    def max_active(self, val: bool) -> None:
        self._max_active = bool(val)

    @property
    def is_max_active(self) -> bool:
        return self._max_active

    def reset(self) -> None:
        self.state = "normal"
        self.pending_start = None
        self._max_active = False

    def update(
        self,
        cpu_temp: float | None,
        gpu_temp: float | None = None,
        now: float | None = None,
    ) -> tuple[str, bool]:
        cur_time = time.monotonic() if now is None else float(now)

        if (
            cpu_temp is None
            or math.isnan(cpu_temp)
            or math.isinf(cpu_temp)
            or cpu_temp <= 0
        ):
            if self.state == "active":
                return "active", False
            return self.state, False

        has_gpu = (
            gpu_temp is not None
            and not math.isnan(gpu_temp)
            and not math.isinf(gpu_temp)
            and gpu_temp > 0
        )
        is_high = (cpu_temp >= self.threshold) or (has_gpu and gpu_temp >= self.threshold)

        if self.state == "normal":
            if is_high:
                self.state = "pending"
                self.pending_start = cur_time
                return "pending", True
            return "normal", False

        elif self.state == "pending":
            if is_high:
                start = self.pending_start if self.pending_start is not None else cur_time
                elapsed = cur_time - start
                if elapsed >= self.confirm_sec:
                    self.state = "active"
                    self._max_active = True
                    return "active", True
                return "pending", False
            else:
                self.state = "normal"
                self.pending_start = None
                return "normal", True

        else:  # active
            recovered = (cpu_temp <= self.recovery_temp) and (not has_gpu or gpu_temp <= self.recovery_temp)
            if recovered:
                self.state = "normal"
                self.pending_start = None
                self._max_active = False
                return "normal", True
            return "active", False


class FanController(
    FanCleanerMixin,
    DriverInstallerMixin,
    ServiceManagerMixin,
):
    hwmon_path: Path | None = None
    pwm1_enable_path: Path | None = None
    pwm2_enable_path: Path | None = None
    pwm1_path: Path | None = None
    pwm2_path: Path | None = None
    fan1_input_path: Path | None = None
    fan2_input_path: Path | None = None
    fan1_max_path: Path | None = None
    fan2_max_path: Path | None = None
    cpu_temp_path: Path | None = None
    config_path: Path = CONFIG_FILE

    _current_control_mode: str = "firmware_auto"
    _last_commanded_cpu_level: int | None = None
    _last_commanded_gpu_level: int | None = None
    _last_control_time: float = 0.0
    _auto_thermal_state: str = "idle"
    _consecutive_rpm_shortfall_cpu: int = 0
    _consecutive_rpm_shortfall_gpu: int = 0
    _rpm_feedback_trim_cpu: int = 0
    _rpm_feedback_trim_gpu: int = 0

    _performance_thermal_state: str = "idle"
    _performance_consecutive_rpm_shortfall_cpu: int = 0
    _performance_consecutive_rpm_shortfall_gpu: int = 0
    _performance_rpm_feedback_trim_cpu: int = 0
    _performance_rpm_feedback_trim_gpu: int = 0

    @property
    def _auto_emergency_state(self) -> str:
        if not hasattr(self, "_auto_emergency_sm"):
            self._auto_emergency_sm = EmergencySafetyStateMachine()
        return self._auto_emergency_sm.state

    @_auto_emergency_state.setter
    def _auto_emergency_state(self, val: str) -> None:
        if not hasattr(self, "_auto_emergency_sm"):
            self._auto_emergency_sm = EmergencySafetyStateMachine()
        self._auto_emergency_sm.state = val

    @property
    def _auto_emergency_pending_start(self) -> float | None:
        if not hasattr(self, "_auto_emergency_sm"):
            self._auto_emergency_sm = EmergencySafetyStateMachine()
        return self._auto_emergency_sm.pending_start

    @_auto_emergency_pending_start.setter
    def _auto_emergency_pending_start(self, val: float | None) -> None:
        if not hasattr(self, "_auto_emergency_sm"):
            self._auto_emergency_sm = EmergencySafetyStateMachine()
        self._auto_emergency_sm.pending_start = val

    @property
    def _auto_emergency_max_active(self) -> bool:
        if not hasattr(self, "_auto_emergency_sm"):
            self._auto_emergency_sm = EmergencySafetyStateMachine()
        return self._auto_emergency_sm.max_active

    @_auto_emergency_max_active.setter
    def _auto_emergency_max_active(self, val: bool) -> None:
        if not hasattr(self, "_auto_emergency_sm"):
            self._auto_emergency_sm = EmergencySafetyStateMachine()
        self._auto_emergency_sm.max_active = val

    @property
    def _performance_emergency_state(self) -> str:
        if not hasattr(self, "_perf_emergency_sm"):
            self._perf_emergency_sm = EmergencySafetyStateMachine(
                threshold=PERFORMANCE_EMERGENCY_TEMP_THRESHOLD,
                recovery_temp=PERFORMANCE_EMERGENCY_RECOVERY_TEMP,
                confirm_sec=PERFORMANCE_EMERGENCY_CONFIRMATION_SEC,
            )
        return self._perf_emergency_sm.state

    @_performance_emergency_state.setter
    def _performance_emergency_state(self, val: str) -> None:
        if not hasattr(self, "_perf_emergency_sm"):
            self._perf_emergency_sm = EmergencySafetyStateMachine(
                threshold=PERFORMANCE_EMERGENCY_TEMP_THRESHOLD,
                recovery_temp=PERFORMANCE_EMERGENCY_RECOVERY_TEMP,
                confirm_sec=PERFORMANCE_EMERGENCY_CONFIRMATION_SEC,
            )
        self._perf_emergency_sm.state = val

    @property
    def _performance_emergency_pending_start(self) -> float | None:
        if not hasattr(self, "_perf_emergency_sm"):
            self._perf_emergency_sm = EmergencySafetyStateMachine(
                threshold=PERFORMANCE_EMERGENCY_TEMP_THRESHOLD,
                recovery_temp=PERFORMANCE_EMERGENCY_RECOVERY_TEMP,
                confirm_sec=PERFORMANCE_EMERGENCY_CONFIRMATION_SEC,
            )
        return self._perf_emergency_sm.pending_start

    @_performance_emergency_pending_start.setter
    def _performance_emergency_pending_start(self, val: float | None) -> None:
        if not hasattr(self, "_perf_emergency_sm"):
            self._perf_emergency_sm = EmergencySafetyStateMachine(
                threshold=PERFORMANCE_EMERGENCY_TEMP_THRESHOLD,
                recovery_temp=PERFORMANCE_EMERGENCY_RECOVERY_TEMP,
                confirm_sec=PERFORMANCE_EMERGENCY_CONFIRMATION_SEC,
            )
        self._perf_emergency_sm.pending_start = val

    @property
    def _performance_emergency_max_active(self) -> bool:
        if not hasattr(self, "_perf_emergency_sm"):
            self._perf_emergency_sm = EmergencySafetyStateMachine(
                threshold=PERFORMANCE_EMERGENCY_TEMP_THRESHOLD,
                recovery_temp=PERFORMANCE_EMERGENCY_RECOVERY_TEMP,
                confirm_sec=PERFORMANCE_EMERGENCY_CONFIRMATION_SEC,
            )
        return self._perf_emergency_sm.max_active

    @_performance_emergency_max_active.setter
    def _performance_emergency_max_active(self, val: bool) -> None:
        if not hasattr(self, "_perf_emergency_sm"):
            self._perf_emergency_sm = EmergencySafetyStateMachine(
                threshold=PERFORMANCE_EMERGENCY_TEMP_THRESHOLD,
                recovery_temp=PERFORMANCE_EMERGENCY_RECOVERY_TEMP,
                confirm_sec=PERFORMANCE_EMERGENCY_CONFIRMATION_SEC,
            )
        self._perf_emergency_sm.max_active = val

    def __init__(self, config_path: str | Path | None = None) -> None:
        self._find_paths()
        if config_path:
            self.config_path = Path(config_path)
        else:
            self.config_path = CONFIG_FILE

        self.config = self.load_config()

        self._auto_emergency_sm = EmergencySafetyStateMachine(
            threshold=float(self.config.get("auto_emergency_temp_threshold", AUTO_EMERGENCY_TEMP_THRESHOLD)),
            recovery_temp=float(self.config.get("auto_emergency_recovery_temp", AUTO_EMERGENCY_RECOVERY_TEMP)),
            confirm_sec=float(self.config.get("auto_emergency_confirmation_sec", AUTO_EMERGENCY_CONFIRMATION_SEC)),
        )
        self._perf_emergency_sm = EmergencySafetyStateMachine(
            threshold=float(self.config.get("performance_emergency_temp_threshold", PERFORMANCE_EMERGENCY_TEMP_THRESHOLD)),
            recovery_temp=float(self.config.get("performance_emergency_recovery_temp", PERFORMANCE_EMERGENCY_RECOVERY_TEMP)),
            confirm_sec=float(self.config.get("performance_emergency_confirmation_sec", PERFORMANCE_EMERGENCY_CONFIRMATION_SEC)),
        )
        self._performance_thermal_state = "idle"
        self._performance_consecutive_rpm_shortfall_cpu = 0
        self._performance_consecutive_rpm_shortfall_gpu = 0
        self._performance_rpm_feedback_trim_cpu = 0
        self._performance_rpm_feedback_trim_gpu = 0
        self.reset_auto_stagnation_state()

        if self.config.get("cleaner_in_progress", False):
            start_ts = self.config.get("cleaner_start_time", 0)
            if not start_ts or (time.time() - start_ts >= 30):
                self.emergency_stop_fan_cleaning()

    def _get_live_board_name(self) -> str | None:
        try:
            with open("/sys/class/dmi/id/board_name") as f:
                return f.read().strip()
        except Exception:
            return None

    def get_board_name(self) -> str | None:
        """Returns the live board name if readable, otherwise cached board name."""
        name = self._get_live_board_name()
        if name:
            return name
        cached = self.config.get("cached_board_name") if hasattr(self, "config") and isinstance(self.config, dict) else None
        return str(cached) if cached else None

    def _is_monitoring_only_board(self) -> bool:
        """Whether the live board (or, if unavailable, cached board) forbids writes."""
        board_name = self.get_board_name() or ""
        return str(board_name) in MONITORING_ONLY_BOARDS

    def _is_unsupported_telemetry_board(self) -> bool:
        """Whether the board has unsupported/unreliable fan telemetry."""
        board_name = self.get_board_name() or ""
        return str(board_name) in UNSUPPORTED_TELEMETRY_BOARDS

    def _read_module_param(self, param_name: str) -> bool:
        """Read boolean parameter from /sys/module/hp_wmi/parameters/<param_name>."""
        candidates = [
            EXPERIMENTAL_CONTROL_MODULE_PARAM if param_name == "experimental_control" else EXPERIMENTAL_TELEMETRY_MODULE_PARAM,
            Path(f"/sys/module/hp_wmi/parameters/{param_name}"),
            Path(f"/sys/module/hp_wmi_omen/parameters/{param_name}"),
        ]
        for path in candidates:
            try:
                if path.exists():
                    val = path.read_text().strip().lower()
                    return val in ("y", "1", "true")
            except Exception:
                pass
        return False

    def is_experimental_control_enabled(self) -> bool:
        """True if experimental control (queries 0x27 Max / 0x2E Speed) is explicitly enabled in config or module param."""
        cfg = getattr(self, "config", {})
        if isinstance(cfg, dict):
            if "enable_experimental_control" in cfg and cfg["enable_experimental_control"] is not None:
                return bool(cfg["enable_experimental_control"])
            if "experimental_control" in cfg and cfg["experimental_control"] is not None:
                return bool(cfg["experimental_control"])
        return self._read_module_param("experimental_control")

    def is_experimental_telemetry_enabled(self) -> bool:
        """True if experimental fan telemetry (query 0x2D) is explicitly enabled in config or module param."""
        cfg = getattr(self, "config", {})
        if isinstance(cfg, dict):
            if "enable_experimental_telemetry" in cfg and cfg["enable_experimental_telemetry"] is not None:
                return bool(cfg["enable_experimental_telemetry"])
            if "experimental_telemetry" in cfg and cfg["experimental_telemetry"] is not None:
                return bool(cfg["experimental_telemetry"])
        return self._read_module_param("experimental_telemetry")

    def check_board_support(self) -> tuple[str, str]:
        board_name = self._get_live_board_name()
        if board_name:
            if board_name != self.config.get("cached_board_name"):
                self.config["cached_board_name"] = board_name
                self.save_config()
        else:
            cached = self.config.get("cached_board_name")
            if not cached:
                print("Error reading board name")
                return "UNSUPPORTED", "Unknown"
            board_name = str(cached)

        if board_name in MONITORING_ONLY_BOARDS:
            return "MONITORING_ONLY", board_name
        if board_name in SUPPORTED_BOARDS:
            return "SUPPORTED", board_name
        elif board_name in POSSIBLY_SUPPPORTED_OMEN_BOARDS:
            return "POSSIBLY_SUPPORTED", board_name
        return "UNSUPPORTED", board_name

    def _find_paths(self) -> None:
        self.cpu_temp_path = self._find_cpu_temp_path()
        paths = glob.glob(HWMON_PATH_PATTERN)
        if not paths:
            self.hwmon_path = None
            self.pwm1_enable_path = None
            self.pwm2_enable_path = None
            self.pwm1_path = None
            self.pwm2_path = None
            self.fan1_input_path = None
            self.fan2_input_path = None
            return

        self.hwmon_path = Path(paths[0])
        self.pwm1_enable_path = self.hwmon_path / "pwm1_enable"
        self.pwm2_enable_path = self.hwmon_path / "pwm2_enable"
        self.pwm1_path = self.hwmon_path / "pwm1"
        self.pwm2_path = self.hwmon_path / "pwm2"
        self.fan1_input_path = self.hwmon_path / "fan1_input"
        self.fan2_input_path = self.hwmon_path / "fan2_input"
        self.fan1_max_path = self.hwmon_path / "fan1_max"
        self.fan2_max_path = self.hwmon_path / "fan2_max"
        self.cpu_temp_path = self._find_cpu_temp_path()

    def _find_cpu_temp_path(self) -> Path | None:
        for hwmon in Path("/sys/class/hwmon").glob("hwmon*"):
            try:
                name_path = hwmon / "name"
                if not name_path.exists():
                    continue
                with open(name_path) as f:
                    name = f.read().strip()
                if name in ["coretemp", "k10temp"]:
                    temp_path = hwmon / "temp1_input"
                    if temp_path.exists():
                        return temp_path
            except Exception:
                continue

        if Path("/sys/class/thermal/thermal_zone0/temp").exists():
            return Path("/sys/class/thermal/thermal_zone0/temp")
        return None

    def load_config(self) -> ConfigDict:
        return _load_config(self.config_path)

    def save_config(self, volatile: bool = False, source: str | None = None) -> None:
        _save_config(self.config, self.config_path, volatile=volatile, source=source)

    def write_sys_file(self, path: Path | str | None, value: int | str) -> None:
        if not path or not self.fan_control_available:
            return
        if self._is_monitoring_only_board():
            print("Fan write blocked: board is monitoring-only.")
            return
        try:
            with open(path, "w") as f:
                f.write(str(value))
        except PermissionError:
            print(f"Permission denied writing to {path}. Are you running as root?")
        except Exception as e:
            print(f"Error writing to {path}: {e}")

    def read_sys_file(self, path: Path | None) -> str | None:
        if not path or not path.exists():
            return None
        try:
            with open(path) as f:
                return f.read().strip()
        except Exception as e:
            print(f"Error reading {path}: {e}")
            return None

    def parse_hwmon_rpm(self, val: str | None) -> tuple[int, bool]:
        if not val:
            return 0, False
        try:
            raw_rpm = int(val)
            if raw_rpm >= 12800:
                reverse_bit_speed = raw_rpm // 100
                actual_speed = (reverse_bit_speed & 0x7F) * 100
                return actual_speed, True
            return raw_rpm, False
        except Exception:
            return 0, False

    @staticmethod
    def parse_wmi_0x2d_fan_data(
        raw_data: bytes | bytearray | list[int] | tuple[int, ...] | None,
    ) -> tuple[int, int] | None:
        """Parse raw buffer from WMI query 0x2D (HPWMI_VICTUS_S_FAN_SPEED_GET_QUERY).

        Returns (cpu_rpm, gpu_rpm) where:
          cpu_rpm = fan_data[0] * 100
          gpu_rpm = fan_data[1] * 100
        Returns None if raw_data is None, malformed (< 2 elements, non-int or out of 0..255 byte range).
        """
        if raw_data is None:
            return None
        if not isinstance(raw_data, (bytes, bytearray, list, tuple)):
            return None
        if len(raw_data) < 2:
            return None
        c, g = raw_data[0], raw_data[1]
        if not isinstance(c, int) or not isinstance(g, int) or isinstance(c, bool) or isinstance(g, bool):
            return None
        if not (0 <= c <= 255 and 0 <= g <= 255):
            return None
        return c * 100, g * 100

    def read_experimental_fan_telemetry(
        self,
        source: bytes | bytearray | list[int] | tuple[int, ...] | Path | str | None = None,
        path: Path | str | None = None,
    ) -> tuple[int, int] | None:
        """Isolated experimental read-only fan telemetry for query 0x2D.

        Guarded strictly behind is_experimental_telemetry_enabled().
        Returns (cpu_rpm, gpu_rpm) or None if disabled, unavailable, or failed.
        """
        if not self.is_experimental_telemetry_enabled():
            return None

        actual_source = path if path is not None else source

        # Direct raw buffer / sequence input (e.g. from low-level WMI query or mock)
        if isinstance(actual_source, (bytes, bytearray, list, tuple)):
            return self.parse_wmi_0x2d_fan_data(actual_source)

        # Sysfs path input (or default platform device sysfs node)
        if hasattr(actual_source, "read_text") or isinstance(actual_source, Path):
            target_path = actual_source
        elif actual_source:
            target_path = Path(actual_source)
        else:
            target_path = EXPERIMENTAL_FAN_TELEMETRY_PATH

        if not target_path.exists():
            return None

        try:
            content = target_path.read_text().strip()
            cpu, gpu = None, None
            for line in content.splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    k, v = k.strip().lower(), v.strip()
                    if k == "cpu_rpm" and v.isdigit():
                        cpu = int(v)
                    elif k == "gpu_rpm" and v.isdigit():
                        gpu = int(v)
            if cpu is not None and gpu is not None:
                return cpu, gpu
        except Exception:
            pass

        try:
            raw_bytes = target_path.read_bytes()
            return self.parse_wmi_0x2d_fan_data(raw_bytes)
        except Exception:
            return None

    @staticmethod
    def build_wmi_0x28_system_design_packet() -> bytes:
        """Build the 8-byte input payload for WMI query 0x28 (HPWMI_GET_SYSTEM_DESIGN_DATA).

        Domain: HPWMI_GM (0x20008), insize: 8, payload: 8 zero bytes, outsize: 8.
        """
        return b"\x00" * 8

    @staticmethod
    def parse_wmi_0x28_thermal_policy_version(
        raw_data: bytes | bytearray | list[int] | tuple[int, ...] | str | None,
    ) -> tuple[int, list[int]] | None:
        """Parse response from WMI query 0x28 (HPWMI_GET_SYSTEM_DESIGN_DATA).

        Returns (thermal_policy_version, raw_bytes) where:
          thermal_policy_version = raw_bytes[3]
            0 -> OMEN v0 / Victus / Victus S (Performance = 0x01, Default = 0x00)
            1 -> OMEN v1 (Performance = 0x31, Default = 0x30)
            >= 2 -> Unknown / future policy version
        Returns None on None, malformed (< 8 bytes, non-integer, or out of 0..255 range).
        """
        if raw_data is None:
            return None

        # Handle formatted text output from sysfs
        if isinstance(raw_data, str):
            version = None
            raw_bytes: list[int] = []
            for line in raw_data.strip().splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    k, v = k.strip().lower(), v.strip()
                    if k == "thermal_policy_version" and v.isdigit():
                        version = int(v)
                    elif k == "raw":
                        tokens = v.split()
                        for tok in tokens:
                            try:
                                raw_bytes.append(int(tok, 16) if tok.startswith("0x") else int(tok))
                            except ValueError:
                                pass
            if version is not None:
                if len(raw_bytes) >= 8:
                    return version, raw_bytes[:8]
                return version, [0, 0, 0, version, 0, 0, 0, 0]
            return None

        if not isinstance(raw_data, (bytes, bytearray, list, tuple)):
            return None
        if len(raw_data) < 8:
            return None

        bytes_list: list[int] = []
        for b in raw_data[:8]:
            if not isinstance(b, int) or isinstance(b, bool) or not (0 <= b <= 255):
                return None
            bytes_list.append(b)

        version = bytes_list[3]
        return version, bytes_list

    def read_experimental_thermal_policy_version(
        self,
        source: bytes | bytearray | list[int] | tuple[int, ...] | Path | str | None = None,
    ) -> tuple[int, list[int]] | None:
        """Isolated experimental read-only thermal policy version probe via query 0x28.

        Guarded strictly behind the 'enable_experimental_telemetry' config flag.
        Returns (thermal_policy_version, raw_bytes) or None if disabled, unavailable, or failed.
        """
        if not self.is_experimental_telemetry_enabled():
            return None

        if isinstance(source, (bytes, bytearray, list, tuple)):
            return self.parse_wmi_0x28_thermal_policy_version(source)

        if hasattr(source, "read_text") or isinstance(source, Path):
            path = source
        elif source:
            path = Path(source)
        else:
            path = Path("/sys/devices/platform/hp-wmi/experimental_thermal_policy_version")

        if not path.exists():
            return None

        try:
            content = path.read_text().strip()
            parsed = self.parse_wmi_0x28_thermal_policy_version(content)
            if parsed is not None:
                return parsed
        except Exception:
            pass

        try:
            raw_bytes = path.read_bytes()
            return self.parse_wmi_0x28_thermal_policy_version(raw_bytes)
        except Exception:
            return None

    @staticmethod
    def build_wmi_0x10_trigger_packet() -> bytes:
        """Build the 1-byte payload for WMI query 0x10 (HPWMI_FAN_COUNT_GET_QUERY / user-define trigger).

        Domain: HPWMI_GM (0x20008), input size: 1, payload: b"\x00", output size: 4.
        """
        return b"\x00"

    @staticmethod
    def build_wmi_0x27_max_fan_packet(enable: bool | int) -> bytes:
        """Build the 4-byte payload for WMI query 0x27 (HPWMI_FAN_SPEED_MAX_SET_QUERY).

        Input:
          enable=True / 1:  b"\x01\x00\x00\x00" (Max fans ON)
          enable=False / 0: b"\x00\x00\x00\x00" (Max fans OFF)
        Raises ValueError or TypeError on invalid input.
        """
        if isinstance(enable, bool):
            val = 1 if enable else 0
        elif isinstance(enable, int) and enable in (0, 1):
            val = enable
        else:
            raise ValueError(f"Invalid max fan value: {enable!r}. Must be True/False or 1/0.")
        return struct.pack("<I", val)

    @staticmethod
    def build_wmi_0x2e_reset_packet() -> bytes:
        """Build the 2-byte payload for WMI query 0x2E (HPWMI_VICTUS_S_FAN_SPEED_SET_QUERY).

        Domain: HPWMI_GM (0x20008), input size: 2, payload: b"\x00\x00", output size: 0.
        Restores automatic EC fan control curves for both CPU and GPU fans.
        """
        return b"\x00\x00"

    @staticmethod
    def build_wmi_0x2e_speed_packet(cpu_level: int, gpu_level: int) -> bytes:
        """Build the 2-byte payload for WMI query 0x2E (HPWMI_VICTUS_S_FAN_SPEED_SET_QUERY).

        Domain: HPWMI_GM (0x20008), input size: 2, payload: b"{cpu_level}{gpu_level}", output size: 0.
        Byte 0: CPU fan speed level (0 = firmware auto, 1..100 = manual speed level).
        Byte 1: GPU fan speed level (0 = firmware auto, 1..100 = manual speed level).
        """
        if type(cpu_level) is not int or type(gpu_level) is not int:
            raise TypeError(
                f"Fan levels must be integers, got cpu={type(cpu_level).__name__}, "
                f"gpu={type(gpu_level).__name__}"
            )
        if not (0 <= cpu_level <= 100) or not (0 <= gpu_level <= 100):
            raise ValueError(
                f"Fan levels must be in range 0..100, got cpu={cpu_level}, gpu={gpu_level}"
            )
        return bytes([cpu_level, gpu_level])

    @staticmethod
    def pwm_to_fan_level(pwm: int) -> int:
        """Convert Linux PWM (0..255) to WMI fan level (0..100)."""
        if type(pwm) is not int:
            raise TypeError(f"PWM value must be an integer, got {type(pwm).__name__}")
        if not (0 <= pwm <= 255):
            raise ValueError(f"PWM value must be in range 0..255, got {pwm}")
        return int(round((pwm / 255.0) * 100.0))

    @classmethod
    def clamp_software_auto_fan_level(cls, level: int, min_floor: int | None = None) -> int:
        """Centralized safety invariant: SOFTWARE AUTO fan level must be in [floor, 100].

        In SOFTWARE AUTO mode, fans must NEVER intentionally be commanded below
        the calibrated stable minimum (or safe fallback floor, default >= 5)
        to prevent stalling and to prevent sending level 0 (which releases manual
        control back to native firmware AUTO).
        Level 0 may ONLY be used during explicit transitions to native firmware AUTO.
        """
        if type(level) is not int:
            raise TypeError(f"Fan level must be an integer, got {type(level).__name__}")
        floor = min_floor if min_floor is not None else MIN_SOFTWARE_AUTO_FAN_LEVEL
        floor = max(MIN_SOFTWARE_AUTO_FAN_LEVEL, min(100, floor))
        return max(floor, min(100, level))

    @classmethod
    def clamp_software_auto_dual_levels(
        cls,
        cpu_level: int,
        gpu_level: int,
        min_cpu_floor: int | None = None,
        min_gpu_floor: int | None = None,
    ) -> tuple[int, int]:
        """Clamp CPU and GPU fan levels independently to the SOFTWARE AUTO safety floor."""
        return (
            cls.clamp_software_auto_fan_level(cpu_level, min_floor=min_cpu_floor),
            cls.clamp_software_auto_fan_level(gpu_level, min_floor=min_gpu_floor),
        )

    @classmethod
    def pwm_to_software_auto_level(cls, pwm: int, min_floor: int | None = None) -> int:
        """Convert Linux PWM (0..255) to SOFTWARE AUTO fan level clamped to safety floor."""
        raw_level = cls.pwm_to_fan_level(pwm)
        return cls.clamp_software_auto_fan_level(raw_level, min_floor=min_floor)

    @classmethod
    def build_wmi_auto_sequence_packets(cls) -> list[tuple[int, int, bytes, int]]:
        """Build the complete ordered WMI packet sequence required to transition to AUTO mode.

        Returns a list of tuples: (query_id, command_domain, payload_bytes, expected_outsize)
          1. Query 0x10 (HPWMI_FAN_COUNT_GET_QUERY / user-define trigger):
             Domain: HPWMI_GM (0x20008), insize: 1, payload: b"\x00", outsize: 4
          2. Query 0x27 (HPWMI_FAN_SPEED_MAX_SET_QUERY / disable Max):
             Domain: HPWMI_GM (0x20008), insize: 4, payload: b"\x00\x00\x00\x00", outsize: 0
          3. Query 0x2E (HPWMI_VICTUS_S_FAN_SPEED_SET_QUERY / restore auto curve):
             Domain: HPWMI_GM (0x20008), insize: 2, payload: b"\x00\x00", outsize: 0
        """
        return [
            (WMI_QUERY_FAN_COUNT, WMI_CMD_GM, cls.build_wmi_0x10_trigger_packet(), 4),
            (WMI_QUERY_FAN_SPEED_MAX_SET, WMI_CMD_GM, cls.build_wmi_0x27_max_fan_packet(False), 0),
            (WMI_QUERY_FAN_SPEED_SET, WMI_CMD_GM, cls.build_wmi_0x2e_reset_packet(), 0),
        ]

    @staticmethod
    def build_wmi_0x1a_performance_packet(mode: int | str = "performance", version: int = 1) -> bytes:
        """Build the 2-byte payload for WMI query 0x1A (HPWMI_SET_PERFORMANCE_MODE).

        Buffer layout:
          buffer[0] = 0xFF (-1 signed char)
          buffer[1] = mode byte

        Modes supported:
          If version == 1 (OMEN v1 default):
            'performance' -> 0x31 (HP_OMEN_V1_THERMAL_PROFILE_PERFORMANCE)
            'default' / 'balanced' -> 0x30 (HP_OMEN_V1_THERMAL_PROFILE_DEFAULT)
            'cool' -> 0x50 (HP_OMEN_V1_THERMAL_PROFILE_COOL)
          If version == 0 (OMEN v0 / Victus / Victus S):
            'performance' -> 0x01 (HP_OMEN_V0_THERMAL_PROFILE_PERFORMANCE)
            'default' / 'balanced' -> 0x00 (HP_OMEN_V0_THERMAL_PROFILE_DEFAULT)
            'cool' -> 0x02 (HP_OMEN_V0_THERMAL_PROFILE_COOL)
          Or an explicit integer mode byte (0 <= mode <= 255).
        """
        if isinstance(mode, str):
            normalized = mode.strip().lower()
            if version == 1:
                mapping = {
                    "performance": HP_OMEN_V1_THERMAL_PROFILE_PERFORMANCE,
                    "default": HP_OMEN_V1_THERMAL_PROFILE_DEFAULT,
                    "balanced": HP_OMEN_V1_THERMAL_PROFILE_DEFAULT,
                    "cool": HP_OMEN_V1_THERMAL_PROFILE_COOL,
                }
            elif version == 0:
                mapping = {
                    "performance": HP_OMEN_V0_THERMAL_PROFILE_PERFORMANCE,
                    "default": HP_OMEN_V0_THERMAL_PROFILE_DEFAULT,
                    "balanced": HP_OMEN_V0_THERMAL_PROFILE_DEFAULT,
                    "cool": HP_OMEN_V0_THERMAL_PROFILE_COOL,
                }
            else:
                raise ValueError(f"Unsupported thermal profile policy version: {version}. Expected 0 or 1.")

            if normalized not in mapping:
                raise ValueError(f"Unknown performance mode: {mode!r}. Valid names: {list(mapping.keys())}")
            mode_byte = mapping[normalized]
        elif isinstance(mode, int) and not isinstance(mode, bool):
            if not 0 <= mode <= 255:
                raise ValueError(f"Mode byte out of range 0..255: {mode}")
            mode_byte = mode
        else:
            raise TypeError(f"Invalid mode type: {type(mode).__name__}. Expected str or int.")

        return bytes([0xFF, mode_byte])

    @classmethod
    def build_wmi_performance_sequence_packets(
        cls,
        mode: int | str = "performance",
        version: int = 1,
    ) -> list[tuple[int, int, bytes, int]]:
        """Build the ordered WMI packet sequence required to set thermal/performance mode.

        Sequence:
          1. Query 0x10 (HPWMI_FAN_COUNT_GET_QUERY / user-define trigger):
             Domain: HPWMI_GM (0x20008), insize: 1, payload: b"\\x00", outsize: 4
          2. Query 0x1A (HPWMI_SET_PERFORMANCE_MODE):
             Domain: HPWMI_GM (0x20008), insize: 2, payload: b"\\xFF<mode>", outsize: 0
        """
        packet_0x1a = cls.build_wmi_0x1a_performance_packet(mode=mode, version=version)
        return [
            (WMI_QUERY_FAN_COUNT, WMI_CMD_GM, cls.build_wmi_0x10_trigger_packet(), 4),
            (WMI_QUERY_SET_PERFORMANCE_MODE, WMI_CMD_GM, packet_0x1a, 0),
        ]

    @staticmethod
    def build_wmi_0x22_gpu_thermal_packet(
        ctgp: bool | int,
        ppab: bool | int,
        dstate: int = 1,
        gpu_slowdown_temp: int = 0,
    ) -> bytes:
        """Build the 4-byte payload for WMI query 0x22 (HPWMI_SET_GPU_THERMAL_MODES_QUERY).

        Payload structure:
          byte 0: ctgp_enable (1 or 0)
          byte 1: ppab_enable (1 or 0)
          byte 2: dstate (1 = 100%, 2 = 50%, 3 = 25%, 4 = 12.5%)
          byte 3: gpu_slowdown_temp (preserved or 0)
        """
        ctgp_byte = 1 if ctgp else 0
        ppab_byte = 1 if ppab else 0
        if not 0 <= dstate <= 255:
            raise ValueError(f"dstate out of range 0..255: {dstate}")
        if not 0 <= gpu_slowdown_temp <= 255:
            raise ValueError(f"gpu_slowdown_temp out of range 0..255: {gpu_slowdown_temp}")
        return bytes([ctgp_byte, ppab_byte, dstate, gpu_slowdown_temp])

    @staticmethod
    def _is_query_executor_success(res: Any) -> bool:
        """Evaluate query_executor return value safely.

        In Python, bool is a subclass of int (isinstance(True, int) is True).
        True, 0, (0, ...), and (True, ...) indicate SUCCESS.
        False, non-zero int, (non-zero int, ...), and (False, ...) indicate FAILURE.
        """
        if isinstance(res, bool):
            return res
        if isinstance(res, (int, float)):
            return res == 0
        if isinstance(res, (tuple, list)):
            if len(res) == 0:
                return True
            first = res[0]
            if isinstance(first, bool):
                return first
            if isinstance(first, (int, float)):
                return first == 0
            return bool(first)
        if res is None:
            return True
        return bool(res)

    _experimental_keepalive_active: bool = False

    def _schedule_experimental_max_keepalive(self) -> None:
        self._experimental_keepalive_active = True

    def _cancel_experimental_max_keepalive(self) -> None:
        self._experimental_keepalive_active = False

    def set_experimental_fan_max(
        self,
        enable: bool | int,
        path: Path | str | None = None,
        query_executor: Any = None,
    ) -> bool:
        """Isolated experimental control for Max Fan (query 0x27) with 0x10 arming and keep-alive.

        Guarded strictly behind is_experimental_control_enabled().
        Returns True on success, False if disabled, invalid input, missing node, or write error.
        """
        if not self.is_experimental_control_enabled():
            return False

        try:
            packet_0x27 = self.build_wmi_0x27_max_fan_packet(enable)
        except (ValueError, TypeError):
            return False

        is_max_on = (packet_0x27 == b"\x01\x00\x00\x00")

        if query_executor is not None:
            if is_max_on:
                # 1. Before 0x27(1), issue query 0x10 (user-define trigger):
                packet_0x10 = self.build_wmi_0x10_trigger_packet()
                res_0x10 = query_executor(0x10, 0x20008, packet_0x10, 4)
                if not self._is_query_executor_success(res_0x10):
                    return False

                # 2. Only after successful 0x10, issue 0x27 (Max fan ON):
                res_0x27 = query_executor(0x27, 0x20008, packet_0x27, 0)
                if not self._is_query_executor_success(res_0x27):
                    return False

                # 3. Schedule 90-second keep-alive worker while MAX is active
                self._schedule_experimental_max_keepalive()
                return True
            else:
                # Clean MAX OFF path:
                # Verified sequence: 0x10 -> 0x27(0) -> 0x2E(0/0) -> cancel keep-alive

                # 1. Query 0x10 (user-define trigger / re-arm for clean transition)
                packet_0x10 = self.build_wmi_0x10_trigger_packet()
                res_0x10 = query_executor(0x10, 0x20008, packet_0x10, 4)
                if not self._is_query_executor_success(res_0x10):
                    return False

                # 2. Query 0x27(0) (disable Max fan mode)
                res_0x27 = query_executor(0x27, 0x20008, packet_0x27, 0)
                if not self._is_query_executor_success(res_0x27):
                    return False

                # 3. Query 0x2E({0x00, 0x00}) (restore auto EC fan curve)
                packet_0x2e = self.build_wmi_0x2e_reset_packet()
                res_0x2e = query_executor(0x2E, 0x20008, packet_0x2e, 0)
                if not self._is_query_executor_success(res_0x2e):
                    return False

                # 4. Cancel keep-alive worker
                self._cancel_experimental_max_keepalive()
                return True

        val_str = "1" if is_max_on else "0"

        if hasattr(path, "write_text") or isinstance(path, Path):
            target = path
        elif path:
            target = Path(path)
        else:
            target = Path("/sys/devices/platform/hp-wmi/experimental_fan_max")

        if not target.exists():
            return False

        try:
            target.write_text(f"{val_str}\n")
            if is_max_on:
                self._schedule_experimental_max_keepalive()
            else:
                self._cancel_experimental_max_keepalive()
            return True
        except Exception:
            return False

    def set_experimental_fan_speed(
        self,
        cpu_level: int,
        gpu_level: int,
        path: Path | str | None = None,
        query_executor: Any = None,
        force_refresh: bool = False,
    ) -> bool:
        """Isolated experimental control for variable fan speed (query 0x2E) with 0x10 arming.

        Sequence for 8CC0:
          1. Query 0x10: domain 0x20008, insize 1, payload b"\\x00", outsize 4
          2. Query 0x27(0): domain 0x20008, insize 4, payload b"\\x00\\x00\\x00\\x00", outsize 0
          3. Query 0x2E: domain 0x20008, insize 2, payload b"{cpu}{gpu}", outsize 0

        Guarded strictly behind is_experimental_control_enabled().
        Returns True on success, False if disabled, invalid input, missing node, or write error.
        """
        if not self.is_experimental_control_enabled():
            return False

        try:
            packet_0x2e = self.build_wmi_0x2e_speed_packet(cpu_level, gpu_level)
        except (ValueError, TypeError):
            return False

        now = time.time()
        is_duplicate = (
            self._last_commanded_cpu_level == cpu_level
            and self._last_commanded_gpu_level == gpu_level
        )
        watchdog_due = (now - self._last_control_time >= EXPERIMENTAL_WATCHDOG_REFRESH_INTERVAL)

        # Suppress duplicate writes unless watchdog refresh is due or force_refresh is True
        if is_duplicate and not watchdog_due and not force_refresh:
            return True

        executor = query_executor if query_executor is not None else getattr(self, "query_executor", None)
        if executor is not None:
            # 1. Query 0x10 (trigger / arm)
            packet_0x10 = self.build_wmi_0x10_trigger_packet()
            res_0x10 = executor(WMI_QUERY_FAN_COUNT, WMI_CMD_GM, packet_0x10, 4)
            if not self._is_query_executor_success(res_0x10):
                return False

            # 2. Query 0x27(0) (disable MAX)
            packet_0x27 = self.build_wmi_0x27_max_fan_packet(0)
            res_0x27 = executor(WMI_QUERY_FAN_SPEED_MAX_SET, WMI_CMD_GM, packet_0x27, 0)
            if not self._is_query_executor_success(res_0x27):
                return False

            # 3. Query 0x2E (set speed levels)
            res_0x2e = executor(WMI_QUERY_FAN_SPEED_SET, WMI_CMD_GM, packet_0x2e, 0)
            if not self._is_query_executor_success(res_0x2e):
                return False

            self._last_commanded_cpu_level = cpu_level
            self._last_commanded_gpu_level = gpu_level
            self._last_control_time = now
            self._current_control_mode = "firmware_auto" if (cpu_level == 0 and gpu_level == 0) else "software_auto"
            self._cancel_experimental_max_keepalive()
            return True

        if hasattr(path, "write_text") or isinstance(path, Path):
            target = path
        elif path:
            target = Path(path)
        else:
            target = EXPERIMENTAL_FAN_SPEED_PATH

        if not target.exists():
            return False

        try:
            target.write_text(f"{cpu_level} {gpu_level}\n")
            self._last_commanded_cpu_level = cpu_level
            self._last_commanded_gpu_level = gpu_level
            self._last_control_time = now
            self._current_control_mode = "firmware_auto" if (cpu_level == 0 and gpu_level == 0) else "software_auto"
            self._cancel_experimental_max_keepalive()
            return True
        except Exception:
            return False

    def get_experimental_fan_speed(
        self,
        path: Path | str | None = None,
    ) -> tuple[int, int] | None:
        """Read last commanded CPU and GPU fan speed levels from sysfs."""
        if not self.is_experimental_control_enabled():
            return None

        if hasattr(path, "read_text") or isinstance(path, Path):
            target = path
        elif path:
            target = Path(path)
        else:
            target = EXPERIMENTAL_FAN_SPEED_PATH

        if not target.exists():
            return None

        try:
            txt = target.read_text().strip()
            parts = txt.split()
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                return int(parts[0]), int(parts[1])
            return None
        except Exception:
            return None

    def restore_firmware_auto(
        self,
        path: Path | str | None = None,
        query_executor: Any = None,
    ) -> bool:
        """Safely restore native firmware AUTO fan control and stop keep-alives.

        Validated sequence: 0x10 -> 0x27(0) -> 0x2E(0, 0).
        Used on daemon shutdown, unrecoverable failures, and explicit release.
        """
        self._cancel_experimental_max_keepalive()
        self._current_control_mode = "firmware_auto"
        self._last_commanded_cpu_level = FIRMWARE_AUTO_FAN_LEVEL
        self._last_commanded_gpu_level = FIRMWARE_AUTO_FAN_LEVEL
        self._last_control_time = time.time()

        success = True
        if self.is_experimental_control_enabled():
            success = self.set_experimental_fan_speed(
                FIRMWARE_AUTO_FAN_LEVEL,
                FIRMWARE_AUTO_FAN_LEVEL,
                path=path,
                query_executor=query_executor,
                force_refresh=True,
            )
        if self.get_board_name() != "8CC0" and self.fan_control_available:
            self.set_fan_mode("auto")

        return success

    def set_experimental_fan_auto(
        self,
        path: Path | str | None = None,
        query_executor: Any = None,
    ) -> bool:
        """Transition fan control to firmware AUTO mode.

        Guarded strictly behind is_experimental_control_enabled().
        Executes the verified reverse-engineered sequence:
          0x10 -> 0x27(0) -> 0x2E(0/0) -> cancel keep-alive.
        Returns True on success, False otherwise.
        """
        if not self.is_experimental_control_enabled():
            return False
        return self.restore_firmware_auto(path=path, query_executor=query_executor)

    def get_experimental_fan_max(
        self,
        path: Path | str | None = None,
    ) -> int | None:
        """Read state of experimental fan max speed sysfs node.

        Returns 0 (OFF), 1 (ON), or None if disabled, unavailable, or failed.
        """
        if not self.is_experimental_control_enabled():
            return None

        if hasattr(path, "read_text") or isinstance(path, Path):
            target = path
        elif path:
            target = Path(path)
        else:
            target = Path("/sys/devices/platform/hp-wmi/experimental_fan_max")

        if not target.exists():
            return None

        try:
            txt = target.read_text().strip()
            if txt in ("0", "1"):
                return int(txt)
            return None
        except Exception:
            return None

    def set_experimental_performance_mode(
        self,
        enable: bool | int,
        path: Path | str | None = None,
        query_executor: Any = None,
    ) -> bool:
        """Isolated experimental control for Performance mode (query 0x1A) with 0x10 arming.

        Sequence for 8CC0 (confirmed OMEN v1):
          1. Query 0x10: domain 0x20008, insize 1, payload b"\\x00", outsize 4
          2. Query 0x1A: domain 0x20008, insize 2, payload b"\\xFF\\x31" (ON) or b"\\xFF\\x30" (OFF/Balanced), outsize 0

        Guarded strictly behind is_experimental_control_enabled().
        Returns True on success, False if disabled, invalid input, missing node, or write error.
        """
        if not self.is_experimental_control_enabled():
            return False

        if isinstance(enable, bool):
            is_perf = enable
        elif isinstance(enable, int) and enable in (0, 1):
            is_perf = bool(enable)
        else:
            return False

        if query_executor is not None:
            # 1. Before 0x1A, issue query 0x10 (user-define trigger):
            packet_0x10 = self.build_wmi_0x10_trigger_packet()
            res_0x10 = query_executor(WMI_QUERY_FAN_COUNT, WMI_CMD_GM, packet_0x10, 4)
            if not self._is_query_executor_success(res_0x10):
                return False

            # 2. Issue query 0x1A with confirmed OMEN v1 payload:
            mode_str = "performance" if is_perf else "balanced"
            packet_0x1a = self.build_wmi_0x1a_performance_packet(mode_str, version=1)
            res_0x1a = query_executor(WMI_QUERY_SET_PERFORMANCE_MODE, WMI_CMD_GM, packet_0x1a, 0)
            if not self._is_query_executor_success(res_0x1a):
                return False

            return True

        if hasattr(path, "write_text") or isinstance(path, Path):
            target = path
        elif path:
            target = Path(path)
        else:
            target = Path("/sys/devices/platform/hp-wmi/experimental_performance_mode")

        if not target.exists():
            return False

        try:
            target.write_text("1\n" if is_perf else "0\n")
            return True
        except Exception:
            return False

    def get_experimental_performance_mode(
        self,
        path: Path | str | None = None,
    ) -> int | None:
        """Read state of experimental performance mode sysfs node.

        Returns 0 (Balanced/Default), 1 (Performance), or None if disabled, unavailable, or failed.
        """
        if not self.is_experimental_control_enabled():
            return None

        if hasattr(path, "read_text") or isinstance(path, Path):
            target = path
        elif path:
            target = Path(path)
        else:
            target = Path("/sys/devices/platform/hp-wmi/experimental_performance_mode")

        if not target.exists():
            return None

        try:
            txt = target.read_text().strip()
            if txt in ("0", "1"):
                return int(txt)
            return None
        except Exception:
            return None

    def get_active_fan_index(self) -> int | None:
        """Returns the 1-based index (1 for Fan 1/CPU, 2 for Fan 2/GPU) of the fan
        whose telemetry value is reported by get_fan_speed / get_fan_speed_info,
        or None if neither fan's telemetry is available.
        """
        f1_avail = self.is_fan_telemetry_available(1)
        f2_avail = self.is_fan_telemetry_available(2)
        if not f1_avail and not f2_avail:
            return None
        if f1_avail and not f2_avail:
            val1 = self.read_sys_file(self.fan1_input_path)
            if val1 is None or not val1.strip().lstrip("-").isdigit():
                self._fan1_read_error = True
                return None
            return 1
        if f2_avail and not f1_avail:
            val2 = self.read_sys_file(self.fan2_input_path)
            if val2 is None or not val2.strip().lstrip("-").isdigit():
                self._fan2_read_error = True
                return None
            return 2

        val1 = self.read_sys_file(self.fan1_input_path)
        val2 = self.read_sys_file(self.fan2_input_path)

        ok1 = val1 is not None and val1.strip().lstrip("-").isdigit()
        ok2 = val2 is not None and val2.strip().lstrip("-").isdigit()

        self._fan1_read_error = not ok1
        self._fan2_read_error = not ok2

        if not ok1 and not ok2:
            return None
        if ok1 and not ok2:
            return 1
        if ok2 and not ok1:
            return 2

        rpm1, _ = self.parse_hwmon_rpm(val1)
        rpm2, _ = self.parse_hwmon_rpm(val2)
        return 2 if rpm2 > rpm1 else 1

    def get_fan_speed(self) -> int:
        if self.get_board_name() == "8CC0":
            if self.is_experimental_telemetry_enabled():
                telemetry = self.read_experimental_fan_telemetry()
                if telemetry is not None:
                    return max(telemetry[0], telemetry[1])
            return 0

        idx = self.get_active_fan_index()
        if idx is None:
            return 0

        f1_avail = self.is_fan_telemetry_available(1)
        f2_avail = self.is_fan_telemetry_available(2)

        val1 = self.read_sys_file(self.fan1_input_path) if f1_avail else ""
        val2 = self.read_sys_file(self.fan2_input_path) if f2_avail else ""

        rpm1, is_rev1 = self.parse_hwmon_rpm(val1)
        rpm2, is_rev2 = self.parse_hwmon_rpm(val2)

        rpm = rpm2 if idx == 2 else rpm1
        is_rev = is_rev2 if idx == 2 else (is_rev1 or (is_rev2 if f2_avail else False))

        if val1 or val2:
            raw_log = f"sysfs fan1={val1 or 'N/A'}, fan2={val2 or 'N/A'}"
            if not hasattr(self, "_last_logged_raw_rpm") or self._last_logged_raw_rpm != raw_log:
                self._last_logged_raw_rpm = raw_log
                self.log_cleaner(f"HWMON SYSFS READINGS -> {raw_log} => Parsed RPM: {rpm} (Reverse: {is_rev})")

        return rpm

    def get_fan_speed_info(self) -> tuple[int, bool]:
        if self.get_board_name() == "8CC0":
            if self.is_experimental_telemetry_enabled():
                telemetry = self.read_experimental_fan_telemetry()
                if telemetry is not None:
                    return max(telemetry[0], telemetry[1]), False
            return 0, False

        idx = self.get_active_fan_index()
        if idx is None:
            return 0, False

        f1_avail = self.is_fan_telemetry_available(1)
        f2_avail = self.is_fan_telemetry_available(2)

        val1 = self.read_sys_file(self.fan1_input_path) if f1_avail else ""
        val2 = self.read_sys_file(self.fan2_input_path) if f2_avail else ""

        rpm1, is_rev1 = self.parse_hwmon_rpm(val1)
        rpm2, is_rev2 = self.parse_hwmon_rpm(val2)

        if idx == 2:
            return rpm2, is_rev2
        else:
            is_rev = is_rev1 or (is_rev2 if f2_avail else False)
            return rpm1, is_rev

    def get_both_fan_speeds(self) -> list[tuple[str, int, bool]]:
        if self.get_board_name() == "8CC0":
            if self.is_experimental_telemetry_enabled():
                telemetry = self.read_experimental_fan_telemetry()
                if telemetry is not None:
                    return [
                        ("Fan 1 (CPU)", telemetry[0], False),
                        ("Fan 2 (GPU)", telemetry[1], False),
                    ]
            return [
                ("Fan 1 (CPU)", 0, False),
                ("Fan 2 (GPU)", 0, False),
            ]

        can_read1 = (
            not self._is_unsupported_telemetry_board()
            and bool(self.hwmon_path and (not hasattr(self.hwmon_path, "exists") or self.hwmon_path.exists()))
            and bool(self.fan1_input_path and (not hasattr(self.fan1_input_path, "exists") or self.fan1_input_path.exists()))
        )
        can_read2 = (
            not self._is_unsupported_telemetry_board()
            and bool(self.hwmon_path and (not hasattr(self.hwmon_path, "exists") or self.hwmon_path.exists()))
            and bool(self.fan2_input_path and (not hasattr(self.fan2_input_path, "exists") or self.fan2_input_path.exists()))
        )

        val1 = self.read_sys_file(self.fan1_input_path) if can_read1 else ""
        val2 = self.read_sys_file(self.fan2_input_path) if can_read2 else ""

        if can_read1:
            self._fan1_read_error = (val1 is None or not val1.strip().lstrip("-").isdigit())
        else:
            self._fan1_read_error = False

        if can_read2:
            self._fan2_read_error = (val2 is None or not val2.strip().lstrip("-").isdigit())
        else:
            self._fan2_read_error = False

        rpm1, is_rev1 = self.parse_hwmon_rpm(val1)
        rpm2, is_rev2 = self.parse_hwmon_rpm(val2)

        return [
            ("Fan 1 (CPU)", rpm1, is_rev1),
            ("Fan 2 (GPU)", rpm2, is_rev2),
        ]

    def get_cpu_temp(self) -> int:
        if self.cpu_temp_path:
            val = self.read_sys_file(self.cpu_temp_path)
            return int(val) // 1000 if val else 0
        return 0

    def has_gpu(self) -> bool:
        if hasattr(self, "_has_gpu_cache"):
            return self._has_gpu_cache
        self._has_gpu_cache = self.get_gpu_temp() > 0
        return self._has_gpu_cache

    def get_gpu_temp(self) -> int:
        try:
            res = subprocess.run(["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True)
            if res.returncode == 0 and res.stdout.strip():
                return int(res.stdout.strip())
        except Exception:
            pass

        try:
            for hwmon in Path("/sys/class/hwmon").glob("hwmon*"):
                name_path = hwmon / "name"
                if name_path.exists():
                    with open(name_path) as f:
                        name = f.read().strip()
                    if "amdgpu" in name.lower():
                        temp_path = hwmon / "temp1_input"
                        if temp_path.exists():
                            val = self.read_sys_file(temp_path)
                            return int(val) // 1000 if val else 0
        except Exception:
            pass

        return 0

    @staticmethod
    def calculate_percentile(values: Sequence[float], percentile: float) -> float:
        """Calculate percentile using linear interpolation between closest ranks.

        Formula:
          k = (n - 1) * p / 100
          i = int(k), f = k - i
          res = s[i] + f * (s[i + 1] - s[i])
        """
        if not values:
            return 0.0
        sorted_vals = sorted(float(v) for v in values)
        n = len(sorted_vals)
        if n == 1:
            return sorted_vals[0]
        p = max(0.0, min(100.0, float(percentile)))
        k = (n - 1) * p / 100.0
        i = int(k)
        f = k - i
        if i >= n - 1:
            return sorted_vals[-1]
        return sorted_vals[i] + f * (sorted_vals[i + 1] - sorted_vals[i])

    def get_valid_core_temperatures(self) -> list[float]:
        """Return list of valid CPU core temperatures, excluding Package id 0."""
        override = getattr(self, "_core_temps_override", None)
        if override is not None:
            raw_temps = override
        else:
            raw_temps = []
            for label, temp in self.get_all_core_temps():
                if "package" in label.lower():
                    continue
                raw_temps.append(temp)

        valid: list[float] = []
        for x in raw_temps:
            if x is None:
                continue
            try:
                fx = float(x)
                if not math.isnan(fx) and not math.isinf(fx) and fx > 0:
                    valid.append(fx)
            except (ValueError, TypeError):
                continue
        return valid

    def get_cpu_normal_temp(self) -> float | None:
        """Authoritative normal CPU temperature: 75th percentile of valid cores."""
        cores = self.get_valid_core_temperatures()
        if cores:
            return self.calculate_percentile(cores, 75.0)
        return None

    def get_cpu_emergency_temp(self) -> float | None:
        """Authoritative emergency CPU temperature: maximum of valid cores."""
        cores = self.get_valid_core_temperatures()
        if cores:
            return float(max(cores))
        return None

    def get_reference_temp(self) -> int:
        sensor = self.config.get("reference_sensor", "cpu")
        if sensor == "gpu":
            gpu_temp = self.get_gpu_temp()
            if gpu_temp > 0:
                return gpu_temp
        normal_cpu = self.get_cpu_normal_temp()
        if normal_cpu is not None and normal_cpu > 0:
            return int(round(normal_cpu))
        return self.get_cpu_temp()

    def get_all_core_temps(self) -> list[tuple[str, int]]:
        core_temps: list[tuple[int, str, int]] = []
        package_temps: list[tuple[str, int]] = []

        if not self.cpu_temp_path:
            return []

        hwmon_dir = self.cpu_temp_path.parent

        for f in hwmon_dir.glob("temp*_input"):
            try:
                label_file = f.with_name(f.name.replace("input", "label"))
                if label_file.exists():
                    label = self.read_sys_file(label_file)
                else:
                    label = f.name

                if not label:
                    continue

                val = self.read_sys_file(f)
                if not val:
                    continue
                temp = int(val) // 1000

                if "Core" in label:
                    try:
                        idx = int(label.split()[-1])
                        core_temps.append((idx, label, temp))
                    except Exception:
                        core_temps.append((999, label, temp))
                elif "Package" in label:
                    package_temps.append((label, temp))
            except Exception:
                continue

        core_temps.sort(key=lambda x: x[0])

        params: list[tuple[str, int]] = []
        for p in package_temps:
            params.append(p)
        for c in core_temps:
            params.append((c[1], c[2]))

        return params

    def get_interface_state(self) -> str:
        """Returns 'available', 'incomplete', or 'absent'.

        'absent': hwmon path does not exist.
        'incomplete': hwmon path exists, but one or more required control files
                      (pwm1_enable, pwm1, pwm2) are missing.
        'available': all required control sysfs files are present (write operations
                     still require appropriate root/elevation privileges).
        """
        if not self.hwmon_path:
            return "absent"
        if hasattr(self.hwmon_path, "exists") and not self.hwmon_path.exists():
            return "absent"
        required = [self.pwm1_enable_path, self.pwm1_path, self.pwm2_path]
        if any(p is None or (hasattr(p, "exists") and not p.exists()) for p in required):
            return "incomplete"
        return "available"

    @property
    def fan_control_available(self) -> bool:
        """True only when all required control files exist (does not guarantee write permissions)."""
        return self.get_interface_state() == "available"

    def is_fan_control_ready(self) -> bool:
        """True if the fan control interface is available for the active platform/board."""
        if self.get_board_name() == "8CC0":
            if self.is_experimental_control_enabled():
                return EXPERIMENTAL_FAN_SPEED_PATH.exists()
            return False
        return self.fan_control_available

    def is_fan_telemetry_available(self, fan: int = 1) -> bool:
        """True if the RPM sysfs input file for the specified fan exists, is supported,
        and has not encountered a read error.
        """
        if self.get_board_name() == "8CC0":
            if self.is_experimental_telemetry_enabled():
                return EXPERIMENTAL_FAN_TELEMETRY_PATH.exists()
            return False
        if self._is_unsupported_telemetry_board():
            return False
        if not self.hwmon_path or (hasattr(self.hwmon_path, "exists") and not self.hwmon_path.exists()):
            return False
        path = self.fan1_input_path if fan == 1 else self.fan2_input_path
        if not path or (hasattr(path, "exists") and not path.exists()):
            return False
        if getattr(self, f"_fan{fan}_read_error", False):
            return False
        return True

    @property
    def fan_telemetry_available(self) -> bool:
        """True if at least one fan RPM telemetry input path exists."""
        return self.is_fan_telemetry_available(1) or self.is_fan_telemetry_available(2)

    def set_fan_mode(self, mode: str) -> None:
        if self.get_board_name() == "8CC0" and self.is_experimental_control_enabled():
            if mode == "max":
                self.set_experimental_fan_max(True)
            elif mode == "auto":
                self.restore_firmware_auto()
            return

        if not self.fan_control_available:
            return
        if mode == "max":
            # Existing hwmon interface convention: enable=0 requests full speed.
            # This requests, but does not claim to verify, a particular RPM.
            self.write_sys_file(self.pwm1_enable_path, 0)
            if self.pwm2_enable_path and self.pwm2_enable_path.exists():
                self.write_sys_file(self.pwm2_enable_path, 0)
        elif mode == "auto":
            self.write_sys_file(self.pwm1_enable_path, 2)
            if self.pwm2_enable_path and self.pwm2_enable_path.exists():
                self.write_sys_file(self.pwm2_enable_path, 2)

    def get_hardware_fan_limits(self) -> tuple[int, int]:
        c_max = 0
        g_max = 0
        if getattr(self, "fan1_max_path", None) and self.fan1_max_path.exists():
            val = self.read_sys_file(self.fan1_max_path)
            if val and val.strip().isdigit():
                c_max = int(val.strip())
        if getattr(self, "fan2_max_path", None) and self.fan2_max_path.exists():
            val = self.read_sys_file(self.fan2_max_path)
            if val and val.strip().isdigit():
                g_max = int(val.strip())
        return c_max, g_max

    def get_effective_fan_limits(self) -> tuple[int, int]:
        cal_c = int(self.config.get("fan1_max", 0)) or int(self.config.get("fan_max", 0))
        cal_g = int(self.config.get("fan2_max", 0)) or int(self.config.get("fan_max", 0))
        hw_c, hw_g = self.get_hardware_fan_limits()

        if self.config.get("use_advanced_fan_control", False):
            strat = self.config.get("max_fan_speed_strategy", "calibration")
            if strat == "omen_defaults":
                cpu_m = int(self.config.get("windows_cpu_max_rpm") or 0)
                gpu_m = int(self.config.get("windows_gpu_max_rpm") or 0)
                if cpu_m > 0 and gpu_m > 0:
                    return cpu_m, gpu_m
                if cal_c > 0 or cal_g > 0:
                    return cal_c or cal_g, cal_g or cal_c
                if hw_c > 0 or hw_g > 0:
                    return hw_c or hw_g, hw_g or hw_c
                return cpu_m or 0, gpu_m or 0
            elif strat == "custom":
                cpu_m = int(self.config.get("manual_cpu_max_rpm") or 0)
                gpu_m = int(self.config.get("manual_gpu_max_rpm") or 0)
                if cpu_m > 0 and gpu_m > 0:
                    return cpu_m, gpu_m
                if cal_c > 0 or cal_g > 0:
                    return cal_c or cal_g, cal_g or cal_c
                if hw_c > 0 or hw_g > 0:
                    return hw_c or hw_g, hw_g or hw_c
                return cpu_m or 0, gpu_m or 0
            else: # calibration
                if cal_c > 0 or cal_g > 0:
                    return cal_c or cal_g, cal_g or cal_c
                if hw_c > 0 or hw_g > 0:
                    return hw_c or hw_g, hw_g or hw_c
                return 0, 0
        elif self.config.get("use_manual_max_rpm", False) or self.config.get("bypass_calibration", False):
            cpu_m = int(self.config.get("manual_cpu_max_rpm") or self.config.get("manual_max_rpm", 0))
            gpu_m = int(self.config.get("manual_gpu_max_rpm") or self.config.get("manual_max_rpm", 0))
            if cpu_m > 0 and gpu_m > 0:
                return cpu_m, gpu_m

        # Default behavior: use calibrated value if exists, else hardware reported max
        if cal_c > 0 or cal_g > 0:
            return cal_c or cal_g, cal_g or cal_c
        if hw_c > 0 or hw_g > 0:
            return hw_c or hw_g, hw_g or hw_c
        return 0, 0

    def get_effective_fan_max(self) -> int:
        cpu_m, gpu_m = self.get_effective_fan_limits()
        return max(cpu_m, gpu_m)

    _runtime_min_cpu_floor: int | None = None
    _runtime_min_gpu_floor: int | None = None

    def get_calibrated_min_fan_levels(self) -> tuple[int, int]:
        """Return the minimum stable fan level floor for (CPU, GPU).

        Reads persisted calibration from config ('calibrated_min_cpu_level' and
        'calibrated_min_gpu_level'). If missing, uncalibrated, or out-of-range,
        falls back to 'fallback_min_fan_level' (default 15).
        Also honors any runtime safety escalation floors for the active session.
        """
        cfg = getattr(self, "config", {})
        fallback = int(cfg.get("fallback_min_fan_level", DEFAULT_FALLBACK_MIN_FAN_LEVEL))
        fallback = max(MIN_SOFTWARE_AUTO_FAN_LEVEL, min(100, fallback))

        raw_cpu = cfg.get("calibrated_min_cpu_level")
        raw_gpu = cfg.get("calibrated_min_gpu_level")

        if isinstance(raw_cpu, int) and MIN_SOFTWARE_AUTO_FAN_LEVEL <= raw_cpu <= 100:
            cpu_floor = raw_cpu
        else:
            cpu_floor = fallback

        if isinstance(raw_gpu, int) and MIN_SOFTWARE_AUTO_FAN_LEVEL <= raw_gpu <= 100:
            gpu_floor = raw_gpu
        else:
            gpu_floor = fallback

        runtime_cpu = getattr(self, "_runtime_min_cpu_floor", None)
        if isinstance(runtime_cpu, int):
            cpu_floor = max(cpu_floor, runtime_cpu)

        runtime_gpu = getattr(self, "_runtime_min_gpu_floor", None)
        if isinstance(runtime_gpu, int):
            gpu_floor = max(gpu_floor, runtime_gpu)

        return cpu_floor, gpu_floor

    def escalate_runtime_min_fan_floor(self, fan: str, bump: int = 2) -> int:
        """Conservatively escalate session floor for a fan showing sub-500 RPM near-stall."""
        fan_key = str(fan).strip().lower()
        cpu_floor, gpu_floor = self.get_calibrated_min_fan_levels()

        if fan_key in ("cpu", "1", "fan1"):
            new_floor = min(MAX_RUNTIME_SAFETY_FLOOR, cpu_floor + bump)
            self._runtime_min_cpu_floor = new_floor
            return new_floor
        elif fan_key in ("gpu", "2", "fan2"):
            new_floor = min(MAX_RUNTIME_SAFETY_FLOOR, gpu_floor + bump)
            self._runtime_min_gpu_floor = new_floor
            return new_floor
        else:
            new_floor = min(MAX_RUNTIME_SAFETY_FLOOR, max(cpu_floor, gpu_floor) + bump)
            self._runtime_min_cpu_floor = new_floor
            self._runtime_min_gpu_floor = new_floor
            return new_floor

    def reset_runtime_min_fan_floors(self) -> None:
        """Reset temporary runtime safety escalation floors."""
        self._runtime_min_cpu_floor = None
        self._runtime_min_gpu_floor = None

    def set_fan_pwm(self, value: int, gpu_value: int | None = None) -> None:
        # Route 8CC0 to experimental 0x2E path if enabled; never fall through to legacy hwmon.
        if self.get_board_name() == "8CC0":
            if self.is_experimental_control_enabled():
                cpu_floor, gpu_floor = self.get_calibrated_min_fan_levels()
                raw_cpu_level = self.pwm_to_fan_level(int(value))
                gpu_val = int(gpu_value) if gpu_value is not None else int(value)
                raw_gpu_level = self.pwm_to_fan_level(gpu_val)
                cpu_level = self.clamp_software_auto_fan_level(raw_cpu_level, min_floor=cpu_floor)
                gpu_level = self.clamp_software_auto_fan_level(raw_gpu_level, min_floor=gpu_floor)
                self.set_experimental_fan_speed(cpu_level, gpu_level)
            return

        if not self.fan_control_available:
            return
        current_enable = self.read_sys_file(self.pwm1_enable_path)
        if current_enable != "1":
            self.write_sys_file(self.pwm1_enable_path, 1)
        if self.pwm2_enable_path and self.pwm2_enable_path.exists():
            current_gpu_enable = self.read_sys_file(self.pwm2_enable_path)
            if current_gpu_enable != "1":
                self.write_sys_file(self.pwm2_enable_path, 1)
        val_str = str(int(value))
        self.write_sys_file(self.pwm1_path, val_str)
        if self.pwm2_path and self.pwm2_path.exists():
            gpu_val_str = str(int(gpu_value)) if gpu_value is not None else val_str
            self.write_sys_file(self.pwm2_path, gpu_val_str)

    @staticmethod
    def is_valid_curve(curve: object) -> bool:
        """Return whether a curve can safely be passed to the interpolation engine."""
        if not isinstance(curve, list) or not curve:
            return False
        for point in curve:
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                return False
            temp, percent = point
            if not isinstance(temp, (int, float)) or not isinstance(percent, (int, float)):
                return False
            if not 0 <= percent <= 100:
                return False
        return True

    def calculate_target_pwm(self, current_temp: int, curve: list[list[float]] | None = None) -> int | None:
        if curve is None:
            curve = self.config.get("curve", [])
        if not curve:
            return None

        sorted_curve = sorted(curve, key=lambda p: p[0])

        target_speed_percent = 0

        if current_temp <= sorted_curve[0][0]:
            target_speed_percent = sorted_curve[0][1]
        elif current_temp >= sorted_curve[-1][0]:
            target_speed_percent = sorted_curve[-1][1]
        else:
            for i in range(len(sorted_curve) - 1):
                p1 = sorted_curve[i]
                p2 = sorted_curve[i + 1]
                if p1[0] <= current_temp <= p2[0]:
                    interp_mode = self.config.get("curve_interpolation", "smooth")
                    if interp_mode == "discrete":
                        target_speed_percent = p1[1]
                    else:
                        denom = p2[0] - p1[0]
                        if denom == 0:
                            target_speed_percent = p2[1]
                        else:
                            ratio = (current_temp - p1[0]) / denom
                            target_speed_percent = p1[1] + ratio * (p2[1] - p1[1])
                    break

        return int(round(target_speed_percent / 100 * 255))

    def calculate_dual_target_pwm(
        self,
        cpu_temp: int,
        gpu_temp: int | None = None,
        curve_override: list[list[float]] | None = None,
    ) -> tuple[int, int]:
        method = self.config.get("fan_control_method", "percentage") if self.config.get("use_advanced_fan_control", False) else "percentage"
        cpu_pwm = self.calculate_target_pwm(cpu_temp, curve=curve_override)
        if cpu_pwm is None:
            cpu_pwm = 0

        if method == "percentage":
            return cpu_pwm, cpu_pwm

        elif method == "asymmetrical":
            cpu_max, gpu_max = self.get_effective_fan_limits()
            if cpu_max <= 0 or gpu_max <= 0:
                return cpu_pwm, cpu_pwm

            cpu_rpm = (cpu_pwm / 255) * cpu_max
            offset = int(self.config.get("asymmetrical_offset_rpm", 200))
            target_gpu_rpm = max(0, min(gpu_max, cpu_rpm + offset))
            gpu_pwm = int(round((target_gpu_rpm / gpu_max) * 255))
            gpu_pwm = max(0, min(255, gpu_pwm))
            return cpu_pwm, gpu_pwm

        elif method == "custom_gpu":
            use_gpu_sensor = self.config.get("gpu_curve_use_gpu_temp", True)
            if use_gpu_sensor and gpu_temp is not None and gpu_temp > 0:
                effective_gpu_temp = gpu_temp
            else:
                ref_sensor = self.config.get("reference_sensor", "cpu")
                if ref_sensor == "gpu" and gpu_temp is not None and gpu_temp > 0:
                    effective_gpu_temp = gpu_temp
                else:
                    effective_gpu_temp = cpu_temp

            gpu_curve = curve_override if curve_override is not None else self.config.get("gpu_curve", [])
            if not gpu_curve:
                gpu_pwm = cpu_pwm
            else:
                gpu_pwm = self.calculate_target_pwm(effective_gpu_temp, curve=gpu_curve)
                if gpu_pwm is None:
                    gpu_pwm = cpu_pwm
            return cpu_pwm, gpu_pwm

        return cpu_pwm, cpu_pwm

    def get_dual_manual_pwm(self) -> tuple[int, int]:
        method = self.config.get("fan_control_method", "percentage") if self.config.get("use_advanced_fan_control", False) else "percentage"
        cpu_pwm = max(0, min(255, int(self.config.get("manual_pwm", 128))))

        if method == "percentage":
            return cpu_pwm, cpu_pwm

        elif method == "asymmetrical":
            cpu_max, gpu_max = self.get_effective_fan_limits()
            if cpu_max <= 0 or gpu_max <= 0:
                return cpu_pwm, cpu_pwm
            cpu_rpm = (cpu_pwm / 255) * cpu_max
            offset = int(self.config.get("asymmetrical_offset_rpm", 200))
            target_gpu_rpm = max(0, min(gpu_max, cpu_rpm + offset))
            gpu_pwm = int(round((target_gpu_rpm / gpu_max) * 255))
            return cpu_pwm, max(0, min(255, gpu_pwm))

        elif method == "custom_gpu":
            gpu_pwm = max(0, min(255, int(self.config.get("gpu_manual_pwm", cpu_pwm))))
            return cpu_pwm, gpu_pwm

        return cpu_pwm, cpu_pwm

    def get_auto_curve_breakpoints(self) -> tuple[dict[str, Any], ...]:
        """Return the explicit 5°C thermal breakpoint table for 8CC0 AUTO mode."""
        return AUTO_BREAKPOINTS_TABLE

    def interpolate_auto_curve(self, temp: float) -> tuple[int, int, str]:
        """Interpolate CPU and GPU target RPMs from the explicit 65–85°C AUTO curve.

        Returns (cpu_target_rpm, gpu_target_rpm, active_interval_str).

        Breakpoints:
          65°C: 1800 RPM CPU / 1800 RPM GPU
          70°C: 2600 RPM CPU / 2700 RPM GPU
          75°C: 3700 RPM CPU / 3900 RPM GPU
          80°C: 4700 RPM CPU / 5000 RPM GPU
          85°C: 5700 RPM CPU / 6100 RPM GPU
        """
        breakpoints = self.get_auto_curve_breakpoints()
        first_bp = breakpoints[0]
        last_bp = breakpoints[-1]

        if temp <= first_bp["temp"]:
            interval = f"{int(first_bp['temp'])}°C" if temp == first_bp["temp"] else f"< {int(first_bp['temp'])}°C"
            return first_bp["cpu_rpm"], first_bp["gpu_rpm"], interval

        if temp >= last_bp["temp"]:
            interval = f"{int(last_bp['temp'])}°C" if temp == last_bp["temp"] else f"> {int(last_bp['temp'])}°C"
            return last_bp["cpu_rpm"], last_bp["gpu_rpm"], interval

        for i in range(len(breakpoints) - 1):
            bp_low = breakpoints[i]
            bp_high = breakpoints[i + 1]
            t_low = bp_low["temp"]
            t_high = bp_high["temp"]
            if t_low <= temp <= t_high:
                if temp == t_low:
                    return bp_low["cpu_rpm"], bp_low["gpu_rpm"], f"{int(t_low)}°C"
                if temp == t_high:
                    return bp_high["cpu_rpm"], bp_high["gpu_rpm"], f"{int(t_high)}°C"
                ratio = (temp - t_low) / (t_high - t_low)
                cpu_rpm = int(round(bp_low["cpu_rpm"] + ratio * (bp_high["cpu_rpm"] - bp_low["cpu_rpm"])))
                gpu_rpm = int(round(bp_low["gpu_rpm"] + ratio * (bp_high["gpu_rpm"] - bp_low["gpu_rpm"])))
                interval = f"{int(t_low)}°C–{int(t_high)}°C"
                return cpu_rpm, gpu_rpm, interval

        return last_bp["cpu_rpm"], last_bp["gpu_rpm"], f"{int(last_bp['temp'])}°C"

    def rpm_to_fan_level(self, rpm: int | float) -> int:
        """Convert target RPM to 0x2E fan level (1..100) based on verified 100 RPM/level scaling."""
        if not isinstance(rpm, (int, float)):
            raise TypeError(f"RPM must be numeric, got {type(rpm).__name__}")
        if rpm <= 0:
            return 0
        level = int(math.floor(float(rpm) / 100.0 + 0.5))
        return max(1, min(100, level))

    def get_auto_thermal_state(self) -> str:
        """Return current software AUTO thermal state: 'idle' or 'active'."""
        return getattr(self, "_auto_thermal_state", "idle")

    def update_auto_thermal_state(
        self, cpu_temp: float, gpu_temp: float | None = None
    ) -> tuple[str, bool]:
        """Update the AUTO thermal state using verified hysteresis thresholds.

        Activation (IDLE -> ACTIVE):
          CPU >= 65°C OR GPU >= 55°C
          (Triggered when either thermal domain exceeds its activation threshold)

        Cooldown (ACTIVE -> IDLE):
          CPU <= 59°C AND (GPU <= 49°C or GPU is None/unavailable)
          (Released only when both thermal domains cool below their off-thresholds)

        Returns (new_state, state_changed).
        """
        current = self.get_auto_thermal_state()
        has_gpu = (gpu_temp is not None and gpu_temp > 0)

        if current == "idle":
            triggered = (cpu_temp >= AUTO_ACTIVATION_CPU_TEMP) or (has_gpu and gpu_temp >= AUTO_ACTIVATION_GPU_TEMP)
            if triggered:
                self._auto_thermal_state = "active"
                return "active", True
            return "idle", False
        else:
            cooled = (cpu_temp <= AUTO_COOLDOWN_CPU_TEMP) and (not has_gpu or gpu_temp <= AUTO_COOLDOWN_GPU_TEMP)
            if cooled:
                self._auto_thermal_state = "idle"
                self._consecutive_rpm_shortfall_cpu = 0
                self._consecutive_rpm_shortfall_gpu = 0
                self._rpm_feedback_trim_cpu = 0
                self._rpm_feedback_trim_gpu = 0
                self.reset_auto_stagnation_state()
                return "idle", True
            return "active", False

    def calculate_auto_target(
        self,
        cpu_temp: float,
        gpu_temp: float | None = None,
        independent_fans: bool = False,
    ) -> dict[str, Any]:
        """Calculate target RPMs, fan levels, and active intervals for AUTO mode.

        Returns a dictionary reporting:
          - current_temp: effective temperature
          - active_interval: string describing the breakpoint interval
          - target_cpu_rpm: target CPU fan speed
          - target_gpu_rpm: target GPU fan speed
          - cpu_fan_level: commanded 0x2E CPU level (with safety floor applied)
          - gpu_fan_level: commanded 0x2E GPU level (with safety floor applied)
          - actual_cpu_rpm: measured 0x2D CPU RPM if telemetry available
          - actual_gpu_rpm: measured 0x2D GPU RPM if telemetry available
          - thermal_state: 'idle' or 'active'
          - stagnation_boost_cpu: active stagnation boost added to CPU level
          - stagnation_boost_gpu: active stagnation boost added to GPU level
          - emergency_state: 'normal', 'pending', or 'active'
        """
        state = self.get_auto_thermal_state()
        has_gpu = (gpu_temp is not None and gpu_temp > 0)

        gpu_normalized = (gpu_temp + (AUTO_ACTIVATION_CPU_TEMP - AUTO_ACTIVATION_GPU_TEMP)) if has_gpu else 0.0
        effective_temp = max(float(cpu_temp), gpu_normalized)

        if independent_fans and has_gpu:
            target_cpu_rpm, _, interval_cpu = self.interpolate_auto_curve(float(cpu_temp))
            _, target_gpu_rpm, interval_gpu = self.interpolate_auto_curve(gpu_normalized)
            interval = f"CPU:{interval_cpu} GPU:{interval_gpu}"
        else:
            target_cpu_rpm, target_gpu_rpm, interval = self.interpolate_auto_curve(effective_temp)

        boost_cpu, boost_gpu = self.get_auto_stagnation_boost()

        if state == "idle":
            target_cpu_rpm = 0
            target_gpu_rpm = 0
            cpu_level = FIRMWARE_AUTO_FAN_LEVEL
            gpu_level = FIRMWARE_AUTO_FAN_LEVEL
            boost_cpu = 0
            boost_gpu = 0
        else:
            raw_cpu_level = self.rpm_to_fan_level(target_cpu_rpm)
            raw_gpu_level = self.rpm_to_fan_level(target_gpu_rpm)
            cpu_floor, gpu_floor = self.get_calibrated_min_fan_levels()
            active_cpu_floor = max(cpu_floor, ACTIVE_AUTO_MIN_FAN_LEVEL)
            active_gpu_floor = max(gpu_floor, ACTIVE_AUTO_MIN_FAN_LEVEL)
            cpu_level = self.clamp_software_auto_fan_level(raw_cpu_level, min_floor=active_cpu_floor)
            gpu_level = self.clamp_software_auto_fan_level(raw_gpu_level, min_floor=active_gpu_floor)

            # Apply stagnation recovery boost (strictly bounded by AUTO fan ceiling)
            if boost_cpu > 0:
                cpu_ceiling = int(self.config.get("auto_max_fan_level_cpu", AUTO_MAX_FAN_LEVEL_CPU))
                cpu_level = min(cpu_ceiling, cpu_level + boost_cpu)
            if boost_gpu > 0:
                gpu_ceiling = int(self.config.get("auto_max_fan_level_gpu", AUTO_MAX_FAN_LEVEL_GPU))
                gpu_level = min(gpu_ceiling, gpu_level + boost_gpu)

        actual_cpu_rpm = None
        actual_gpu_rpm = None
        if self.is_experimental_telemetry_enabled():
            telemetry = self.read_experimental_fan_telemetry()
            if telemetry is not None:
                actual_cpu_rpm, actual_gpu_rpm = telemetry

        return {
            "current_temp": effective_temp,
            "active_interval": interval,
            "target_cpu_rpm": target_cpu_rpm,
            "target_gpu_rpm": target_gpu_rpm,
            "cpu_fan_level": cpu_level,
            "gpu_fan_level": gpu_level,
            "actual_cpu_rpm": actual_cpu_rpm,
            "actual_gpu_rpm": actual_gpu_rpm,
            "thermal_state": state,
            "stagnation_boost_cpu": boost_cpu,
            "stagnation_boost_gpu": boost_gpu,
            "emergency_state": self.get_auto_emergency_state(),
        }

    def _ensure_auto_emergency_sm(self) -> None:
        if not hasattr(self, "_auto_emergency_sm") or self._auto_emergency_sm is None:
            self._auto_emergency_sm = EmergencySafetyStateMachine(
                threshold=float(self.config.get("auto_emergency_temp_threshold", AUTO_EMERGENCY_TEMP_THRESHOLD)),
                recovery_temp=float(self.config.get("auto_emergency_recovery_temp", AUTO_EMERGENCY_RECOVERY_TEMP)),
                confirm_sec=float(self.config.get("auto_emergency_confirmation_sec", AUTO_EMERGENCY_CONFIRMATION_SEC)),
            )

    def get_auto_emergency_state(self) -> str:
        """Return current AUTO emergency cooling state: 'normal', 'pending', or 'active'."""
        self._ensure_auto_emergency_sm()
        return self._auto_emergency_sm.state

    def reset_auto_emergency_state(self) -> None:
        """Reset AUTO emergency cooling state machine."""
        self._ensure_auto_emergency_sm()
        self._auto_emergency_sm.reset()

    def update_auto_emergency_state(
        self,
        cpu_temp: float | None,
        gpu_temp: float | None = None,
        now: float | None = None,
    ) -> tuple[str, bool]:
        """Evaluate high-temperature spike barrier and emergency MAX cooling for AUTO mode."""
        self._ensure_auto_emergency_sm()
        self._auto_emergency_sm.threshold = float(
            self.config.get("auto_emergency_temp_threshold", AUTO_EMERGENCY_TEMP_THRESHOLD)
        )
        self._auto_emergency_sm.recovery_temp = float(
            self.config.get("auto_emergency_recovery_temp", AUTO_EMERGENCY_RECOVERY_TEMP)
        )
        self._auto_emergency_sm.confirm_sec = float(
            self.config.get("auto_emergency_confirmation_sec", AUTO_EMERGENCY_CONFIRMATION_SEC)
        )
        return self._auto_emergency_sm.update(cpu_temp, gpu_temp, now)

    def get_auto_stagnation_boost(self) -> tuple[int, int]:
        """Return current (cpu_boost, gpu_boost) fan levels."""
        cpu_data = getattr(self, "_auto_stagnation_cpu", None)
        gpu_data = getattr(self, "_auto_stagnation_gpu", None)
        cpu_boost = int(cpu_data.get("boost_level", 0)) if isinstance(cpu_data, dict) else 0
        gpu_boost = int(gpu_data.get("boost_level", 0)) if isinstance(gpu_data, dict) else 0
        return cpu_boost, gpu_boost

    def reset_auto_stagnation_state(self) -> None:
        """Reset AUTO stagnation tracking and boost levels."""
        self._auto_stagnation_cpu = {
            "start_time": None,
            "baseline_temp": None,
            "boost_level": 0,
            "last_boost_time": None,
        }
        self._auto_stagnation_gpu = {
            "start_time": None,
            "baseline_temp": None,
            "boost_level": 0,
            "last_boost_time": None,
        }

    def update_auto_stagnation_state(
        self,
        cpu_temp: float | None,
        gpu_temp: float | None = None,
        now: float | None = None,
    ) -> tuple[int, int, bool]:
        """Evaluate thermal stagnation and compute incremental recovery boost.

        Monitors whether temperature is stagnant (fails to improve by >= min_imp
        over window_sec) at or above activation_temp (>= 77°C).
        Applies incremental boost (+3..+5 initial, +2 additional if stagnant),
        bounded by AUTO ceiling.
        Removes boost when temperature cools into recovery band (<= 75°C).

        Returns (cpu_boost, gpu_boost, state_changed).
        """
        cur_time = time.monotonic() if now is None else float(now)

        act_temp = float(self.config.get("auto_stagnation_activation_temp", AUTO_STAGNATION_ACTIVATION_TEMP))
        rec_temp = float(self.config.get("auto_stagnation_recovery_temp", AUTO_STAGNATION_RECOVERY_TEMP))
        win_sec = float(self.config.get("auto_stagnation_window_sec", AUTO_STAGNATION_WINDOW_SEC))
        min_imp = float(self.config.get("auto_stagnation_min_improvement_temp", AUTO_STAGNATION_MIN_IMPROVEMENT_TEMP))
        init_boost = int(self.config.get("auto_stagnation_initial_boost", AUTO_STAGNATION_INITIAL_BOOST_LEVEL))
        step_boost = int(self.config.get("auto_stagnation_step_boost", AUTO_STAGNATION_STEP_BOOST_LEVEL))
        step_interval = float(self.config.get("auto_stagnation_step_interval_sec", AUTO_STAGNATION_STEP_INTERVAL_SEC))

        if not hasattr(self, "_auto_stagnation_cpu") or not isinstance(self._auto_stagnation_cpu, dict):
            self.reset_auto_stagnation_state()

        def _update_domain(data: dict[str, Any], temp: float | None, ceiling: int) -> bool:
            if temp is None or math.isnan(temp) or math.isinf(temp) or temp <= 0:
                return False

            prev_boost = data.get("boost_level", 0)

            # Case 1: Boost is currently ACTIVE (> 0)
            if prev_boost > 0:
                # Recovery condition: temp reached <= recovery band (74-75°C)
                if temp <= rec_temp:
                    data["boost_level"] = 0
                    data["start_time"] = None
                    data["baseline_temp"] = None
                    data["last_boost_time"] = None
                    return True

                # Hysteresis band: rec_temp < temp < act_temp (75°C to 77°C)
                if temp < act_temp:
                    # Temperature is cooling down; maintain existing boost
                    return False

                # temp >= act_temp (>= 77°C): still hot despite boost
                baseline = data.get("baseline_temp")
                if baseline is not None and (baseline - temp) >= min_imp:
                    # Meaningfully decreasing (improving by >= 1°C)!
                    data["baseline_temp"] = temp
                    data["start_time"] = cur_time
                    return False
                else:
                    # Stagnant despite boost. Check if step_interval elapsed since last boost
                    last_b = data.get("last_boost_time")
                    if last_b is not None and (cur_time - last_b) >= step_interval:
                        new_boost = min(ceiling, prev_boost + step_boost)
                        if new_boost != prev_boost:
                            data["boost_level"] = new_boost
                            data["last_boost_time"] = cur_time
                            data["baseline_temp"] = temp
                            data["start_time"] = cur_time
                            return True
                    return False

            # Case 2: Boost is NOT currently active (== 0)
            else:
                if temp < act_temp:
                    # Below 77°C: not eligible for stagnation detection
                    data["start_time"] = None
                    data["baseline_temp"] = None
                    return False

                # temp >= act_temp (>= 77°C)
                if data.get("start_time") is None:
                    # Start stagnation observation window
                    data["start_time"] = cur_time
                    data["baseline_temp"] = temp
                    return False

                baseline = data.get("baseline_temp")
                if baseline is not None and (baseline - temp) >= min_imp:
                    # Meaningfully decreasing (cooling working normally)
                    data["baseline_temp"] = temp
                    data["start_time"] = cur_time
                    return False

                # If temperature rose above baseline, update baseline so we measure future drop from peak
                if baseline is not None and temp > baseline:
                    data["baseline_temp"] = temp

                elapsed = cur_time - data["start_time"]
                if elapsed >= win_sec:
                    # Stagnation duration threshold met!
                    data["boost_level"] = min(ceiling, init_boost)
                    data["last_boost_time"] = cur_time
                    data["baseline_temp"] = temp
                    data["start_time"] = cur_time
                    return True

                return False

        cpu_ceil = int(self.config.get("auto_max_fan_level_cpu", AUTO_MAX_FAN_LEVEL_CPU))
        gpu_ceil = int(self.config.get("auto_max_fan_level_gpu", AUTO_MAX_FAN_LEVEL_GPU))

        changed_cpu = _update_domain(self._auto_stagnation_cpu, cpu_temp, cpu_ceil)
        changed_gpu = _update_domain(self._auto_stagnation_gpu, gpu_temp, gpu_ceil)

        return (
            self._auto_stagnation_cpu["boost_level"],
            self._auto_stagnation_gpu["boost_level"],
            (changed_cpu or changed_gpu),
        )

    def apply_auto_rpm_feedback(
        self,
        actual_cpu_rpm: int,
        actual_gpu_rpm: int,
        target_cpu_rpm: int,
        target_gpu_rpm: int,
        lag_threshold: int = 350,
    ) -> tuple[int, int]:
        """Track RPM deviations against target RPM and conservatively compensate.

        If actual RPM lags below target by >= lag_threshold for 2 consecutive cycles,
        applies a conservative bounded trim (max +3 fan levels = +300 RPM) to avoid
        uncontrolled oscillations while ensuring fan speed satisfies target.
        Returns (trim_cpu, trim_gpu).
        """
        if actual_cpu_rpm < target_cpu_rpm - lag_threshold and target_cpu_rpm > 0:
            self._consecutive_rpm_shortfall_cpu += 1
            if self._consecutive_rpm_shortfall_cpu >= 2:
                self._rpm_feedback_trim_cpu = min(3, self._rpm_feedback_trim_cpu + 1)
                self._consecutive_rpm_shortfall_cpu = 0
        elif actual_cpu_rpm >= target_cpu_rpm - 200:
            self._consecutive_rpm_shortfall_cpu = 0
            self._rpm_feedback_trim_cpu = max(0, self._rpm_feedback_trim_cpu - 1)

        if actual_gpu_rpm < target_gpu_rpm - lag_threshold and target_gpu_rpm > 0:
            self._consecutive_rpm_shortfall_gpu += 1
            if self._consecutive_rpm_shortfall_gpu >= 2:
                self._rpm_feedback_trim_gpu = min(3, self._rpm_feedback_trim_gpu + 1)
                self._consecutive_rpm_shortfall_gpu = 0
        elif actual_gpu_rpm >= target_gpu_rpm - 200:
            self._consecutive_rpm_shortfall_gpu = 0
            self._rpm_feedback_trim_gpu = max(0, self._rpm_feedback_trim_gpu - 1)

        return self._rpm_feedback_trim_cpu, self._rpm_feedback_trim_gpu

    def reset_auto_state(self) -> None:
        """Reset internal AUTO mode state machine and feedback counters."""
        self._auto_thermal_state = "idle"
        self._consecutive_rpm_shortfall_cpu = 0
        self._consecutive_rpm_shortfall_gpu = 0
        self._rpm_feedback_trim_cpu = 0
        self._rpm_feedback_trim_gpu = 0
        self.reset_auto_emergency_state()
        self.reset_auto_stagnation_state()

    # --------------------------------------------------------------------------
    # PERFORMANCE Mode (8CC0 Software Dual-Fan Speed Control)
    # --------------------------------------------------------------------------

    def get_performance_curve_breakpoints(self) -> tuple[dict[str, Any], ...]:
        """Return the explicit 5°C thermal breakpoints table for PERFORMANCE mode."""
        return PERFORMANCE_BREAKPOINTS_TABLE

    def interpolate_performance_curve(self, temp: float) -> tuple[int, int, str]:
        """Linearly interpolate CPU and GPU target RPMs between explicit breakpoints.

        Table:
          55°C: 2000 / 2000 RPM (DEFINED)
          60°C: 2500 / 2600 RPM (DEFINED)
          65°C: 3200 / 3400 RPM (DEFINED)
          70°C: 4000 / 4200 RPM (DEFINED)
          75°C: 4800 / 5100 RPM (DEFINED)
          80°C: 5400 / 5700 RPM (DEFINED)
          85°C: 5700 / 6100 RPM (OBSERVED-CEILING)

        Below 55°C clamps to (2000, 2000, '< 55°C').
        Above 85°C clamps to (5700, 6100, '> 85°C').
        """
        table = self.get_performance_curve_breakpoints()
        t = float(temp)
        if t <= table[0]["temp"]:
            interval = f"< {int(table[0]['temp'])}°C" if t < table[0]["temp"] else f"{int(table[0]['temp'])}°C"
            return table[0]["cpu_rpm"], table[0]["gpu_rpm"], interval

        if t >= table[-1]["temp"]:
            interval = f"> {int(table[-1]['temp'])}°C" if t > table[-1]["temp"] else f"{int(table[-1]['temp'])}°C"
            return table[-1]["cpu_rpm"], table[-1]["gpu_rpm"], interval

        for i in range(len(table) - 1):
            bp1 = table[i]
            bp2 = table[i + 1]
            if bp1["temp"] <= t <= bp2["temp"]:
                if t == bp1["temp"]:
                    return bp1["cpu_rpm"], bp1["gpu_rpm"], f"{int(bp1['temp'])}°C"
                if t == bp2["temp"]:
                    return bp2["cpu_rpm"], bp2["gpu_rpm"], f"{int(bp2['temp'])}°C"
                ratio = (t - bp1["temp"]) / (bp2["temp"] - bp1["temp"])
                cpu_rpm = int(round(bp1["cpu_rpm"] + ratio * (bp2["cpu_rpm"] - bp1["cpu_rpm"])))
                gpu_rpm = int(round(bp1["gpu_rpm"] + ratio * (bp2["gpu_rpm"] - bp1["gpu_rpm"])))
                interval = f"{int(bp1['temp'])}°C–{int(bp2['temp'])}°C"
                return cpu_rpm, gpu_rpm, interval

        return table[-1]["cpu_rpm"], table[-1]["gpu_rpm"], f"{int(table[-1]['temp'])}°C"

    def get_performance_thermal_state(self) -> str:
        """Return current PERFORMANCE thermal state: 'idle' or 'active'."""
        return getattr(self, "_performance_thermal_state", "idle")

    def update_performance_thermal_state(
        self,
        cpu_temp: float,
        gpu_temp: float | None = None,
    ) -> tuple[str, bool]:
        """Update PERFORMANCE mode thermal state machine using dual-domain hysteresis.

        Activation:
          CPU >= 55.0°C OR GPU >= 45.0°C (transitions idle -> active)
        Cooldown:
          CPU <= 50.0°C AND (GPU <= 40.0°C or GPU is None/unavailable) (transitions active -> idle)
        Hysteresis:
          5.0°C on both CPU and GPU boundaries.
        Returns (new_state, state_changed).
        """
        current_state = self.get_performance_thermal_state()
        has_gpu = (gpu_temp is not None and not math.isnan(gpu_temp) and not math.isinf(gpu_temp) and gpu_temp > 0)

        is_hot = (cpu_temp >= PERFORMANCE_ACTIVATION_CPU_TEMP) or (has_gpu and gpu_temp >= PERFORMANCE_ACTIVATION_GPU_TEMP)
        is_cool = (cpu_temp <= PERFORMANCE_COOLDOWN_CPU_TEMP) and (not has_gpu or gpu_temp <= PERFORMANCE_COOLDOWN_GPU_TEMP)

        if current_state == "idle":
            if is_hot:
                self._performance_thermal_state = "active"
                return "active", True
            return "idle", False
        else:  # active
            if is_cool:
                self._performance_thermal_state = "idle"
                return "idle", True
            return "active", False

    def calculate_performance_target(
        self,
        cpu_temp: float,
        gpu_temp: float | None = None,
        independent_fans: bool = False,
    ) -> dict[str, Any]:
        """Calculate target RPMs, fan levels, and active intervals for PERFORMANCE mode.

        Returns dictionary reporting:
          - current_temp: effective temperature
          - active_interval: string describing breakpoint interval
          - target_cpu_rpm: target CPU fan speed
          - target_gpu_rpm: target GPU fan speed
          - cpu_fan_level: commanded 0x2E CPU level (with active floor applied)
          - gpu_fan_level: commanded 0x2E GPU level (with active floor applied)
          - actual_cpu_rpm: measured 0x2D CPU RPM if telemetry available
          - actual_gpu_rpm: measured 0x2D GPU RPM if telemetry available
          - thermal_state: 'idle' or 'active'
          - emergency_state: 'normal', 'pending', or 'active'
        """
        state = self.get_performance_thermal_state()
        has_gpu = (gpu_temp is not None and gpu_temp > 0)

        gpu_normalized = (gpu_temp + (PERFORMANCE_ACTIVATION_CPU_TEMP - PERFORMANCE_ACTIVATION_GPU_TEMP)) if has_gpu else 0.0
        effective_temp = max(float(cpu_temp), gpu_normalized)

        if independent_fans and has_gpu:
            target_cpu_rpm, _, interval_cpu = self.interpolate_performance_curve(float(cpu_temp))
            _, target_gpu_rpm, interval_gpu = self.interpolate_performance_curve(gpu_normalized)
            interval = f"CPU:{interval_cpu} GPU:{interval_gpu}"
        else:
            target_cpu_rpm, target_gpu_rpm, interval = self.interpolate_performance_curve(effective_temp)

        if state == "idle":
            target_cpu_rpm = 0
            target_gpu_rpm = 0
            cpu_level = FIRMWARE_AUTO_FAN_LEVEL
            gpu_level = FIRMWARE_AUTO_FAN_LEVEL
        else:
            raw_cpu_level = self.rpm_to_fan_level(target_cpu_rpm)
            raw_gpu_level = self.rpm_to_fan_level(target_gpu_rpm)
            cpu_floor, gpu_floor = self.get_calibrated_min_fan_levels()
            active_cpu_floor = max(cpu_floor, ACTIVE_PERFORMANCE_MIN_FAN_LEVEL)
            active_gpu_floor = max(gpu_floor, ACTIVE_PERFORMANCE_MIN_FAN_LEVEL)
            cpu_level = self.clamp_software_auto_fan_level(raw_cpu_level, min_floor=active_cpu_floor)
            gpu_level = self.clamp_software_auto_fan_level(raw_gpu_level, min_floor=active_gpu_floor)

            cpu_ceiling = int(self.config.get("performance_max_fan_level_cpu", PERFORMANCE_MAX_FAN_LEVEL_CPU))
            gpu_ceiling = int(self.config.get("performance_max_fan_level_gpu", PERFORMANCE_MAX_FAN_LEVEL_GPU))
            cpu_level = min(cpu_ceiling, cpu_level)
            gpu_level = min(gpu_ceiling, gpu_level)

        actual_cpu_rpm = None
        actual_gpu_rpm = None
        if self.is_experimental_telemetry_enabled():
            telemetry = self.read_experimental_fan_telemetry()
            if telemetry is not None:
                actual_cpu_rpm, actual_gpu_rpm = telemetry

        return {
            "current_temp": effective_temp,
            "active_interval": interval,
            "target_cpu_rpm": target_cpu_rpm,
            "target_gpu_rpm": target_gpu_rpm,
            "cpu_fan_level": cpu_level,
            "gpu_fan_level": gpu_level,
            "actual_cpu_rpm": actual_cpu_rpm,
            "actual_gpu_rpm": actual_gpu_rpm,
            "thermal_state": state,
            "emergency_state": self.get_performance_emergency_state(),
        }

    def _ensure_perf_emergency_sm(self) -> None:
        if not hasattr(self, "_perf_emergency_sm") or self._perf_emergency_sm is None:
            self._perf_emergency_sm = EmergencySafetyStateMachine(
                threshold=float(self.config.get("performance_emergency_temp_threshold", PERFORMANCE_EMERGENCY_TEMP_THRESHOLD)),
                recovery_temp=float(self.config.get("performance_emergency_recovery_temp", PERFORMANCE_EMERGENCY_RECOVERY_TEMP)),
                confirm_sec=float(self.config.get("performance_emergency_confirmation_sec", PERFORMANCE_EMERGENCY_CONFIRMATION_SEC)),
            )

    def get_performance_emergency_state(self) -> str:
        """Return current PERFORMANCE emergency cooling state: 'normal', 'pending', or 'active'."""
        self._ensure_perf_emergency_sm()
        return self._perf_emergency_sm.state

    def reset_performance_emergency_state(self) -> None:
        """Reset PERFORMANCE emergency cooling state machine."""
        self._ensure_perf_emergency_sm()
        self._perf_emergency_sm.reset()

    def update_performance_emergency_state(
        self,
        cpu_temp: float | None,
        gpu_temp: float | None = None,
        now: float | None = None,
    ) -> tuple[str, bool]:
        """Evaluate high-temperature spike barrier for PERFORMANCE mode."""
        self._ensure_perf_emergency_sm()
        self._perf_emergency_sm.threshold = float(
            self.config.get("performance_emergency_temp_threshold", PERFORMANCE_EMERGENCY_TEMP_THRESHOLD)
        )
        self._perf_emergency_sm.recovery_temp = float(
            self.config.get("performance_emergency_recovery_temp", PERFORMANCE_EMERGENCY_RECOVERY_TEMP)
        )
        self._perf_emergency_sm.confirm_sec = float(
            self.config.get("performance_emergency_confirmation_sec", PERFORMANCE_EMERGENCY_CONFIRMATION_SEC)
        )
        return self._perf_emergency_sm.update(cpu_temp, gpu_temp, now)

    def apply_performance_rpm_feedback(
        self,
        actual_cpu_rpm: int,
        actual_gpu_rpm: int,
        target_cpu_rpm: int,
        target_gpu_rpm: int,
        lag_threshold: int = 350,
    ) -> tuple[int, int]:
        """Track RPM deviations against target RPM for PERFORMANCE mode and conservatively compensate.
        Returns (trim_cpu, trim_gpu).
        """
        if actual_cpu_rpm < target_cpu_rpm - lag_threshold and target_cpu_rpm > 0:
            self._performance_consecutive_rpm_shortfall_cpu += 1
            if self._performance_consecutive_rpm_shortfall_cpu >= 2:
                self._performance_rpm_feedback_trim_cpu = min(3, self._performance_rpm_feedback_trim_cpu + 1)
                self._performance_consecutive_rpm_shortfall_cpu = 0
        elif actual_cpu_rpm >= target_cpu_rpm - 200:
            self._performance_consecutive_rpm_shortfall_cpu = 0
            self._performance_rpm_feedback_trim_cpu = max(0, self._performance_rpm_feedback_trim_cpu - 1)

        if actual_gpu_rpm < target_gpu_rpm - lag_threshold and target_gpu_rpm > 0:
            self._performance_consecutive_rpm_shortfall_gpu += 1
            if self._performance_consecutive_rpm_shortfall_gpu >= 2:
                self._performance_rpm_feedback_trim_gpu = min(3, self._performance_rpm_feedback_trim_gpu + 1)
                self._performance_consecutive_rpm_shortfall_gpu = 0
        elif actual_gpu_rpm >= target_gpu_rpm - 200:
            self._performance_consecutive_rpm_shortfall_gpu = 0
            self._performance_rpm_feedback_trim_gpu = max(0, self._performance_rpm_feedback_trim_gpu - 1)

        return self._performance_rpm_feedback_trim_cpu, self._performance_rpm_feedback_trim_gpu

    def reset_performance_state(self) -> None:
        """Reset internal PERFORMANCE mode state machine, feedback counters, and emergency state."""
        self._performance_thermal_state = "idle"
        self._performance_consecutive_rpm_shortfall_cpu = 0
        self._performance_consecutive_rpm_shortfall_gpu = 0
        self._performance_rpm_feedback_trim_cpu = 0
        self._performance_rpm_feedback_trim_gpu = 0
        self.reset_performance_emergency_state()

    def transition_mode(self, from_mode: str, to_mode: str) -> None:
        """Coherent transition between operational modes without restarting process.

        Exits outgoing mode, resets transient state, releases hardware ownership if required,
        and initializes destination mode.
        """
        f = (from_mode or "").lower()
        t = (to_mode or "").lower()
        if f == t:
            return

        # 1. Exit outgoing mode and release previous hardware ownership
        if f == "max":
            if self.get_board_name() == "8CC0" and self.is_experimental_control_enabled():
                self.set_experimental_fan_max(False)
            self._cancel_experimental_max_keepalive()
        elif f == "auto":
            self.reset_auto_state()
        elif f == "performance":
            self.reset_performance_state()

        # 2. Enter destination mode and establish hardware ownership
        if t == "max":
            self.reset_auto_emergency_state()
            self.reset_performance_emergency_state()
            if self.get_board_name() == "8CC0" and self.is_experimental_control_enabled():
                self.set_experimental_fan_max(True)
            else:
                self.set_fan_mode("max")
        elif t == "auto":
            self.reset_auto_state()
        elif t == "performance":
            self.reset_performance_state()

    def handle_resume(self) -> None:
        """Revalidate sysfs interfaces and safely restore active mode on system resume."""
        self._find_paths()
        self._last_control_time = 0.0
        self.reset_runtime_min_fan_floors()
        self.reset_auto_emergency_state()
        self.reset_performance_emergency_state()
        self.reset_auto_stagnation_state()
        mode = self.config.get("mode", "auto")
        if mode == "max":
            if self.get_board_name() == "8CC0" and self.is_experimental_control_enabled():
                self.set_experimental_fan_max(True)
            else:
                self.set_fan_mode("max")
        elif mode in ("auto", "performance"):
            self.restore_firmware_auto()

    def handle_suspend(self) -> None:
        """Safely release hardware to firmware control before system suspend."""
        try:
            self.restore_firmware_auto()
        except Exception:
            pass

    def calibrate(self) -> Generator[int, None, tuple[int, int]]:
        if not self.fan_control_available:
            raise RuntimeError("Fan control interface unavailable. Calibration aborted.")

        print("Starting calibration...")

        try:
            prev_enable = self.read_sys_file(self.pwm1_enable_path) or "2"
            prev_pwm = self.read_sys_file(self.pwm1_path) or "0"
            prev_pwm2 = self.read_sys_file(self.pwm2_path) if (self.pwm2_path and self.pwm2_path.exists()) else None
        except Exception:
            prev_enable = "2"
            prev_pwm = "0"
            prev_pwm2 = None

        self.set_fan_mode('max')

        try:
            wait_time = self.config.get("calibration_wait", DEFAULT_CALIBRATION_WAIT)
            steps = 10
            for i in range(steps):
                time.sleep(wait_time / steps)
                yield int((i + 1) / steps * 100)

            val1 = self.read_sys_file(self.fan1_input_path)
            val2 = self.read_sys_file(self.fan2_input_path)
            rpm1, _ = self.parse_hwmon_rpm(val1)
            rpm2, _ = self.parse_hwmon_rpm(val2)

            self.config["fan1_max"] = rpm1
            self.config["fan2_max"] = rpm2
            self.save_config()

            print(f"Calibration finished: Fan 1={rpm1} RPM, Fan 2={rpm2} RPM")
            return rpm1, rpm2
        except GeneratorExit:
            print("Stopping calibration...")
            raise
        finally:
            try:
                if prev_enable:
                    self.write_sys_file(self.pwm1_enable_path, prev_enable)
                if prev_pwm and str(prev_enable).strip() == "1":
                    self.write_sys_file(self.pwm1_path, prev_pwm)
                    if prev_pwm2 and self.pwm2_path and self.pwm2_path.exists():
                        self.write_sys_file(self.pwm2_path, prev_pwm2)
            except Exception as e:
                print(f"Error restoring fan state: {e}")

    def calibrate_minimum_fan_levels(
        self,
        min_required_rpm: int = DEFAULT_MIN_REQUIRED_RPM,
        candidate_levels: list[int] | tuple[int, ...] | None = None,
        settle_time: float = 6.0,
        sample_count: int = 5,
        sample_interval: float = 1.2,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        query_executor: Any = None,
        telemetry_reader: Callable[[], tuple[int, int] | None] | None = None,
        telemetry_path: Path | str | None = None,
        max_safe_temp: float = 80.0,
        save_to_config: bool = True,
    ) -> dict[str, Any]:
        """Empirically determine the lowest stable software-controlled fan levels for 8CC0.

        Tests candidate fan levels sequentially, collects multi-sample RPM telemetry via
        WMI query 0x2D, evaluates rotational stability and pulsing/cogging limits, and
        determines independent minimum stable operational floors for CPU and GPU fans.

        Guarantees restoration of native firmware AUTO fan control on completion, failure,
        or interrupt.
        """
        is_8cc0 = (self.get_board_name() == "8CC0")
        cfg = getattr(self, "config", {})
        exp_ctl = self.is_experimental_control_enabled()
        exp_telem = self.is_experimental_telemetry_enabled()

        has_mock = (
            query_executor is not None
            or telemetry_reader is not None
            or getattr(self, "query_executor", None) is not None
        )
        has_sysfs = (
            EXPERIMENTAL_FAN_SPEED_PATH.exists()
            and EXPERIMENTAL_FAN_TELEMETRY_PATH.exists()
        )

        if not (is_8cc0 and exp_ctl and exp_telem and (has_sysfs or has_mock)):
            return {
                "success": False,
                "error": "Experimental control and telemetry must be enabled for 8CC0 fan minimum calibration.",
                "calibrated_min_cpu_level": None,
                "calibrated_min_gpu_level": None,
                "calibrated_min_cpu_rpm": None,
                "calibrated_min_gpu_rpm": None,
                "history": [],
            }

        if candidate_levels is None:
            candidates = list(DEFAULT_CALIBRATION_CANDIDATE_LEVELS)
        else:
            candidates = [int(x) for x in candidate_levels if 5 <= int(x) <= 100]
            candidates.sort()

        if not candidates:
            raise ValueError("No valid candidate fan levels provided (must be in range 5..100).")

        cur_temp = self.get_reference_temp()
        if cur_temp is not None and cur_temp > max_safe_temp:
            return {
                "success": False,
                "error": f"Starting CPU temperature {cur_temp:.1f}°C exceeds safety ceiling ({max_safe_temp}°C).",
                "calibrated_min_cpu_level": None,
                "calibrated_min_gpu_level": None,
                "calibrated_min_cpu_rpm": None,
                "calibrated_min_gpu_rpm": None,
                "history": [],
            }

        prev_mode = self.config.get("mode", "auto")
        self.config["mode"] = "calibration"
        prev_exp_ctl = cfg.get("enable_experimental_control") if isinstance(cfg, dict) else None
        prev_exp_telem = cfg.get("enable_experimental_telemetry") if isinstance(cfg, dict) else None
        if isinstance(cfg, dict):
            cfg["enable_experimental_control"] = True
            cfg["enable_experimental_telemetry"] = True

        stable_cpu_level: int | None = None
        stable_gpu_level: int | None = None
        stable_cpu_rpm: int | None = None
        stable_gpu_rpm: int | None = None
        history: list[dict[str, Any]] = []
        abort_reason: str | None = None

        try:
            for candidate in candidates:
                step_temp = self.get_reference_temp()
                if step_temp is not None and step_temp > max_safe_temp:
                    abort_reason = f"Thermal safety ceiling exceeded: CPU reached {step_temp:.1f}°C (> {max_safe_temp}°C)"
                    break

                cmd_cpu = stable_cpu_level if stable_cpu_level is not None else candidate
                cmd_gpu = stable_gpu_level if stable_gpu_level is not None else candidate

                speed_ok = self.set_experimental_fan_speed(
                    cmd_cpu,
                    cmd_gpu,
                    query_executor=query_executor,
                    force_refresh=True,
                )
                if not speed_ok:
                    abort_reason = f"Failed to command fan speed levels CPU={cmd_cpu}, GPU={cmd_gpu}"
                    break

                if settle_time > 0:
                    time.sleep(settle_time)

                cpu_samples: list[int] = []
                gpu_samples: list[int] = []
                for s_idx in range(sample_count):
                    if s_idx > 0 and sample_interval > 0:
                        time.sleep(sample_interval)

                    if telemetry_reader is not None:
                        telemetry = telemetry_reader()
                    else:
                        telemetry = self.read_experimental_fan_telemetry(path=telemetry_path)

                    if telemetry is not None:
                        cpu_samples.append(telemetry[0])
                        gpu_samples.append(telemetry[1])

                if len(cpu_samples) < max(2, sample_count // 2):
                    abort_reason = f"Insufficient telemetry samples collected at candidate level {candidate}"
                    break

                # Evaluate CPU fan stability
                cpu_stable = False
                if stable_cpu_level is not None:
                    cpu_stable = True
                    cpu_stats = {
                        "level": stable_cpu_level,
                        "samples": cpu_samples,
                        "min": min(cpu_samples),
                        "max": max(cpu_samples),
                        "avg": sum(cpu_samples) / len(cpu_samples),
                        "median": statistics.median(cpu_samples),
                        "variance": statistics.variance(cpu_samples) if len(cpu_samples) > 1 else 0.0,
                        "spread": max(cpu_samples) - min(cpu_samples),
                        "stable": True,
                        "status": "ALREADY_STABLE",
                    }
                else:
                    c_min = min(cpu_samples)
                    c_max = max(cpu_samples)
                    c_avg = sum(cpu_samples) / len(cpu_samples)
                    c_med = statistics.median(cpu_samples)
                    c_var = statistics.variance(cpu_samples) if len(cpu_samples) > 1 else 0.0
                    c_spread = c_max - c_min
                    max_allowed_spread = max(350, int(0.25 * c_avg))
                    is_spinning = all(s > 0 for s in cpu_samples)
                    meets_rpm = (c_min >= min_required_rpm and c_avg >= min_required_rpm)
                    no_pulsing = (c_spread <= max_allowed_spread)
                    cpu_stable = (is_spinning and meets_rpm and no_pulsing)
                    status_str = "STABLE" if cpu_stable else ("STALL" if not is_spinning or not meets_rpm else "PULSING")
                    cpu_stats = {
                        "level": candidate,
                        "samples": cpu_samples,
                        "min": c_min,
                        "max": c_max,
                        "avg": c_avg,
                        "median": c_med,
                        "variance": c_var,
                        "spread": c_spread,
                        "stable": cpu_stable,
                        "status": status_str,
                    }
                    if cpu_stable:
                        stable_cpu_level = candidate
                        stable_cpu_rpm = int(round(c_avg))

                # Evaluate GPU fan stability
                gpu_stable = False
                if stable_gpu_level is not None:
                    gpu_stable = True
                    gpu_stats = {
                        "level": stable_gpu_level,
                        "samples": gpu_samples,
                        "min": min(gpu_samples),
                        "max": max(gpu_samples),
                        "avg": sum(gpu_samples) / len(gpu_samples),
                        "median": statistics.median(gpu_samples),
                        "variance": statistics.variance(gpu_samples) if len(gpu_samples) > 1 else 0.0,
                        "spread": max(gpu_samples) - min(gpu_samples),
                        "stable": True,
                        "status": "ALREADY_STABLE",
                    }
                else:
                    g_min = min(gpu_samples)
                    g_max = max(gpu_samples)
                    g_avg = sum(gpu_samples) / len(gpu_samples)
                    g_med = statistics.median(gpu_samples)
                    g_var = statistics.variance(gpu_samples) if len(gpu_samples) > 1 else 0.0
                    g_spread = g_max - g_min
                    max_allowed_spread = max(350, int(0.25 * g_avg))
                    is_spinning = all(s > 0 for s in gpu_samples)
                    meets_rpm = (g_min >= min_required_rpm and g_avg >= min_required_rpm)
                    no_pulsing = (g_spread <= max_allowed_spread)
                    gpu_stable = (is_spinning and meets_rpm and no_pulsing)
                    status_str = "STABLE" if gpu_stable else ("STALL" if not is_spinning or not meets_rpm else "PULSING")
                    gpu_stats = {
                        "level": candidate,
                        "samples": gpu_samples,
                        "min": g_min,
                        "max": g_max,
                        "avg": g_avg,
                        "median": g_med,
                        "variance": g_var,
                        "spread": g_spread,
                        "stable": gpu_stable,
                        "status": status_str,
                    }
                    if gpu_stable:
                        stable_gpu_level = candidate
                        stable_gpu_rpm = int(round(g_avg))

                step_info = {
                    "candidate": candidate,
                    "cmd_cpu": cmd_cpu,
                    "cmd_gpu": cmd_gpu,
                    "cpu": cpu_stats,
                    "gpu": gpu_stats,
                }
                history.append(step_info)

                if progress_callback is not None:
                    try:
                        progress_callback(step_info)
                    except Exception:
                        pass

                if stable_cpu_level is not None and stable_gpu_level is not None:
                    break

        finally:
            self.restore_firmware_auto(query_executor=query_executor)
            self.config["mode"] = prev_mode
            if isinstance(cfg, dict):
                if prev_exp_ctl is None:
                    cfg.pop("enable_experimental_control", None)
                else:
                    cfg["enable_experimental_control"] = prev_exp_ctl
                if prev_exp_telem is None:
                    cfg.pop("enable_experimental_telemetry", None)
                else:
                    cfg["enable_experimental_telemetry"] = prev_exp_telem

        success = (
            stable_cpu_level is not None
            and stable_gpu_level is not None
            and abort_reason is None
        )

        if success and save_to_config:
            self.config["calibrated_min_cpu_level"] = stable_cpu_level
            self.config["calibrated_min_gpu_level"] = stable_gpu_level
            self.config["calibrated_min_cpu_rpm"] = stable_cpu_rpm
            self.config["calibrated_min_gpu_rpm"] = stable_gpu_rpm
            self.config["calibration_timestamp"] = datetime.now(timezone.utc).isoformat()
            self.config["calibration_context"] = {
                "kernel": os.uname().release if hasattr(os, "uname") else "unknown",
                "board": self.get_board_name(),
                "min_required_rpm": min_required_rpm,
            }
            self.save_config()

        return {
            "success": success,
            "error": abort_reason,
            "calibrated_min_cpu_level": stable_cpu_level,
            "calibrated_min_gpu_level": stable_gpu_level,
            "calibrated_min_cpu_rpm": stable_cpu_rpm,
            "calibrated_min_gpu_rpm": stable_gpu_rpm,
            "history": history,
        }

    def start_stress_test(self, duration_sec: int, core_count: int | None = None) -> bool:
        if core_count is None:
            core_count = os.cpu_count() or 4

        self.stop_stress_test()

        self.stress_processes = []
        cmd = [sys.executable, "-c", "while True: 9999**9999"]

        print(f"Starting stress test on {core_count} cores...")
        try:
            for _ in range(core_count):
                p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.stress_processes.append(p)
            return True
        except Exception as e:
            print(f"Error starting stress test: {e}")
            self.stop_stress_test()
            return False

    def stop_stress_test(self) -> None:
        if hasattr(self, 'stress_processes') and self.stress_processes:
            print("Stopping stress test...")
            for p in self.stress_processes:
                try:
                    p.terminate()
                except Exception:
                    pass

            for p in self.stress_processes:
                try:
                    p.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    p.kill()

            self.stress_processes = []
            print("Stopped stress test.")

    def set_bios_control(self, enabled: bool) -> bool:
        try:
            subprocess.run(["modprobe", "ec_sys", "write_support=1"], check=True)
        except Exception as e:
            print(f"Failed to load ec_sys: {e}")
            return False

        ECIO_FILE = "/sys/kernel/debug/ec/ec0/io"
        BIOS_OFFSET = 98
        TIMER_OFFSET = 99
        FAN1_OFFSET = 52
        FAN2_OFFSET = 53

        try:
            with open(ECIO_FILE, "r+b") as ec:
                if not enabled:
                    ec.seek(BIOS_OFFSET)
                    ec.write(bytes([6]))
                    time.sleep(0.1)
                    ec.seek(TIMER_OFFSET)
                    ec.write(bytes([0]))
                else:
                    ec.seek(BIOS_OFFSET)
                    ec.write(bytes([0]))
                    ec.seek(FAN1_OFFSET)
                    ec.write(bytes([0]))
                    ec.seek(FAN2_OFFSET)
                    ec.write(bytes([0]))
            return True
        except Exception as e:
            print(f"Error setting BIOS control: {e}")
            return False
