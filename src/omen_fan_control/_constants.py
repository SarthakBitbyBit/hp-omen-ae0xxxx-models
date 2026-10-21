from __future__ import annotations

import os
from pathlib import Path
from typing import Any


HWMON_PATH_PATTERN = "/sys/devices/platform/hp-wmi/hwmon/*/"
THERMAL_ZONE_PATH = "/sys/class/thermal/thermal_zone0/temp"
LOG_LEVELS: dict[str, int] = {
    "DEBUG": 0,
    "INFO": 1,
    "WARNING": 2,
    "ERROR": 3,
    "QUIET": 4,
}

if os.geteuid() == 0:
    CONFIG_DIR = Path("/etc/omen-fan-control")
    VOLATILE_CONFIG_DIR = Path("/run/omen-fan-control")
else:
    CONFIG_DIR = Path(os.path.expanduser("~/.config/omen-fan-control"))
    _xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
    if _xdg_runtime:
        VOLATILE_CONFIG_DIR = Path(_xdg_runtime) / "omen-fan-control"
    else:
        VOLATILE_CONFIG_DIR = CONFIG_DIR / "run"

CONFIG_FILE = CONFIG_DIR / "config.json"
VOLATILE_CONFIG_FILE = VOLATILE_CONFIG_DIR / "config.json"
SYSTEM_RUN_DIR = Path("/run/omen-fan-control")
SYSTEM_VOLATILE_CONFIG_FILE = SYSTEM_RUN_DIR / "config.json"
SYSTEM_STATUS_FILE = SYSTEM_RUN_DIR / "status.json"

DEFAULT_CALIBRATION_WAIT = 30
DEFAULT_WATCHDOG_INTERVAL = 90

WMI_CMD_GM = 0x20008
WMI_QUERY_FAN_COUNT = 0x10
WMI_QUERY_SET_PERFORMANCE_MODE = 0x1A
WMI_QUERY_GET_GPU_THERMAL_MODES = 0x21
WMI_QUERY_SET_GPU_THERMAL_MODES = 0x22
WMI_QUERY_FAN_SPEED_MAX_SET = 0x27
WMI_QUERY_GET_SYSTEM_DESIGN_DATA = 0x28
WMI_QUERY_FAN_SPEED_GET = 0x2D
WMI_QUERY_FAN_SPEED_SET = 0x2E
EXPERIMENTAL_MAX_KEEP_ALIVE_INTERVAL = 90
EXPERIMENTAL_WATCHDOG_REFRESH_INTERVAL = 60.0
MIN_SOFTWARE_AUTO_FAN_LEVEL = 5
FIRMWARE_AUTO_FAN_LEVEL = 0
DEFAULT_MIN_REQUIRED_RPM = 500
DEFAULT_FALLBACK_MIN_FAN_LEVEL = 15
DEFAULT_CALIBRATION_CANDIDATE_LEVELS: tuple[int, ...] = (10, 12, 14, 16, 18, 20, 22, 25, 28, 30, 35, 40)
MAX_RUNTIME_SAFETY_FLOOR = 50
EXPERIMENTAL_FAN_SPEED_PATH = Path("/sys/devices/platform/hp-wmi/experimental_fan_speed")
EXPERIMENTAL_FAN_MAX_PATH = Path("/sys/devices/platform/hp-wmi/experimental_fan_max")
EXPERIMENTAL_FAN_TELEMETRY_PATH = Path("/sys/devices/platform/hp-wmi/experimental_fan_telemetry")
EXPERIMENTAL_CONTROL_MODULE_PARAM = Path("/sys/module/hp_wmi/parameters/experimental_control")
EXPERIMENTAL_TELEMETRY_MODULE_PARAM = Path("/sys/module/hp_wmi/parameters/experimental_telemetry")

# 8CC0 Windows OMEN Auto Mode Breakpoints & Hysteresis Constants
AUTO_ACTIVATION_CPU_TEMP: float = 65.0
AUTO_ACTIVATION_GPU_TEMP: float = 55.0
AUTO_COOLDOWN_CPU_TEMP: float = 59.0
AUTO_COOLDOWN_GPU_TEMP: float = 49.0
ACTIVE_AUTO_MIN_RPM: int = 1800
ACTIVE_AUTO_MIN_FAN_LEVEL: int = 18

AUTO_CURVE_SOURCE_OBSERVED = "OBSERVED"
AUTO_CURVE_SOURCE_DERIVED = "DERIVED"
AUTO_CURVE_SOURCE_REVERSE_ENGINEERED = "REVERSE-ENGINEERED"
AUTO_CURVE_SOURCE_ESTIMATED = "ESTIMATED"
AUTO_CURVE_SOURCE_UNKNOWN = "UNKNOWN"

# Explicit 5°C thermal breakpoints for 8CC0 software AUTO:
# Breakpoints at: 65°C, 70°C, 75°C, 80°C, 85°C
# Sources:
#  65°C: CPU 1800 RPM (ESTIMATED), GPU 1800 RPM (ESTIMATED)
#  70°C: CPU 2600 RPM (DERIVED), GPU 2700 RPM (DERIVED)
#  75°C: CPU 3700 RPM (DERIVED), GPU 3900 RPM (DERIVED)
#  80°C: CPU 4700 RPM (DERIVED), GPU 5000 RPM (DERIVED)
#  85°C: CPU 5700 RPM (OBSERVED), GPU 6100 RPM (OBSERVED)
AUTO_BREAKPOINTS_TABLE: tuple[dict[str, Any], ...] = (
    {
        "temp": 65.0,
        "cpu_rpm": 1800,
        "gpu_rpm": 1800,
        "cpu_level": 18,
        "gpu_level": 18,
        "source_cpu": AUTO_CURVE_SOURCE_ESTIMATED,
        "source_gpu": AUTO_CURVE_SOURCE_ESTIMATED,
    },
    {
        "temp": 70.0,
        "cpu_rpm": 2600,
        "gpu_rpm": 2700,
        "cpu_level": 26,
        "gpu_level": 27,
        "source_cpu": AUTO_CURVE_SOURCE_DERIVED,
        "source_gpu": AUTO_CURVE_SOURCE_DERIVED,
    },
    {
        "temp": 75.0,
        "cpu_rpm": 3700,
        "gpu_rpm": 3900,
        "cpu_level": 37,
        "gpu_level": 39,
        "source_cpu": AUTO_CURVE_SOURCE_DERIVED,
        "source_gpu": AUTO_CURVE_SOURCE_DERIVED,
    },
    {
        "temp": 80.0,
        "cpu_rpm": 4700,
        "gpu_rpm": 5000,
        "cpu_level": 47,
        "gpu_level": 50,
        "source_cpu": AUTO_CURVE_SOURCE_DERIVED,
        "source_gpu": AUTO_CURVE_SOURCE_DERIVED,
    },
    {
        "temp": 85.0,
        "cpu_rpm": 5700,
        "gpu_rpm": 6100,
        "cpu_level": 57,
        "gpu_level": 61,
        "source_cpu": AUTO_CURVE_SOURCE_OBSERVED,
        "source_gpu": AUTO_CURVE_SOURCE_OBSERVED,
    },
)

# 8CC0 AUTO Thermal Recovery & Stagnation Boost Constants
AUTO_STAGNATION_ACTIVATION_TEMP: float = 77.0
AUTO_STAGNATION_WINDOW_SEC: float = 120.0
AUTO_STAGNATION_MIN_IMPROVEMENT_TEMP: float = 1.0
AUTO_STAGNATION_RECOVERY_TEMP: float = 75.0
AUTO_STAGNATION_INITIAL_BOOST_LEVEL: int = 4
AUTO_STAGNATION_STEP_BOOST_LEVEL: int = 2
AUTO_STAGNATION_STEP_INTERVAL_SEC: float = 30.0
AUTO_MAX_FAN_LEVEL_CPU: int = 57
AUTO_MAX_FAN_LEVEL_GPU: int = 61
AUTO_MAX_FAN_LEVEL: int = 61

# 8CC0 AUTO High-Temperature Spike Safety Barrier Constants
AUTO_EMERGENCY_TEMP_THRESHOLD: float = 85.0
AUTO_EMERGENCY_CONFIRMATION_SEC: float = 3.0
AUTO_EMERGENCY_RECOVERY_TEMP: float = 80.0

# 8CC0 Windows OMEN Performance Mode Breakpoints & Constants
ACTIVE_PERFORMANCE_MIN_RPM: int = 2000
ACTIVE_PERFORMANCE_MIN_FAN_LEVEL: int = 20
PERFORMANCE_ACTIVATION_CPU_TEMP: float = 55.0
PERFORMANCE_ACTIVATION_GPU_TEMP: float = 45.0
PERFORMANCE_COOLDOWN_CPU_TEMP: float = 50.0
PERFORMANCE_COOLDOWN_GPU_TEMP: float = 40.0
PERFORMANCE_CURVE_SOURCE_DEFINED = "DEFINED"
PERFORMANCE_CURVE_SOURCE_OBSERVED_CEILING = "OBSERVED-CEILING"

PERFORMANCE_BREAKPOINTS_TABLE: tuple[dict[str, Any], ...] = (
    {
        "temp": 55.0,
        "cpu_rpm": 2000,
        "gpu_rpm": 2000,
        "cpu_level": 20,
        "gpu_level": 20,
        "source_cpu": PERFORMANCE_CURVE_SOURCE_DEFINED,
        "source_gpu": PERFORMANCE_CURVE_SOURCE_DEFINED,
    },
    {
        "temp": 60.0,
        "cpu_rpm": 2500,
        "gpu_rpm": 2600,
        "cpu_level": 25,
        "gpu_level": 26,
        "source_cpu": PERFORMANCE_CURVE_SOURCE_DEFINED,
        "source_gpu": PERFORMANCE_CURVE_SOURCE_DEFINED,
    },
    {
        "temp": 65.0,
        "cpu_rpm": 3200,
        "gpu_rpm": 3400,
        "cpu_level": 32,
        "gpu_level": 34,
        "source_cpu": PERFORMANCE_CURVE_SOURCE_DEFINED,
        "source_gpu": PERFORMANCE_CURVE_SOURCE_DEFINED,
    },
    {
        "temp": 70.0,
        "cpu_rpm": 4000,
        "gpu_rpm": 4200,
        "cpu_level": 40,
        "gpu_level": 42,
        "source_cpu": PERFORMANCE_CURVE_SOURCE_DEFINED,
        "source_gpu": PERFORMANCE_CURVE_SOURCE_DEFINED,
    },
    {
        "temp": 75.0,
        "cpu_rpm": 4800,
        "gpu_rpm": 5100,
        "cpu_level": 48,
        "gpu_level": 51,
        "source_cpu": PERFORMANCE_CURVE_SOURCE_DEFINED,
        "source_gpu": PERFORMANCE_CURVE_SOURCE_DEFINED,
    },
    {
        "temp": 80.0,
        "cpu_rpm": 5400,
        "gpu_rpm": 5700,
        "cpu_level": 54,
        "gpu_level": 57,
        "source_cpu": PERFORMANCE_CURVE_SOURCE_DEFINED,
        "source_gpu": PERFORMANCE_CURVE_SOURCE_DEFINED,
    },
    {
        "temp": 85.0,
        "cpu_rpm": 5700,
        "gpu_rpm": 6100,
        "cpu_level": 57,
        "gpu_level": 61,
        "source_cpu": PERFORMANCE_CURVE_SOURCE_OBSERVED_CEILING,
        "source_gpu": PERFORMANCE_CURVE_SOURCE_OBSERVED_CEILING,
    },
)

# 8CC0 Performance High-Temperature Spike Safety Barrier Constants
PERFORMANCE_EMERGENCY_TEMP_THRESHOLD: float = 85.0
PERFORMANCE_EMERGENCY_CONFIRMATION_SEC: float = 3.0
PERFORMANCE_EMERGENCY_RECOVERY_TEMP: float = 80.0
PERFORMANCE_MAX_FAN_LEVEL_CPU: int = 57
PERFORMANCE_MAX_FAN_LEVEL_GPU: int = 61
PERFORMANCE_MAX_FAN_LEVEL: int = 61

# Thermal profile constants for OMEN v1 (modern OMEN)
HP_OMEN_V1_THERMAL_PROFILE_DEFAULT = 0x30
HP_OMEN_V1_THERMAL_PROFILE_PERFORMANCE = 0x31
HP_OMEN_V1_THERMAL_PROFILE_COOL = 0x50

# Thermal profile constants for OMEN v0 / Victus / Victus S
HP_OMEN_V0_THERMAL_PROFILE_DEFAULT = 0x00
HP_OMEN_V0_THERMAL_PROFILE_PERFORMANCE = 0x01
HP_OMEN_V0_THERMAL_PROFILE_COOL = 0x02

# Boards where temperature monitoring works but fan-control protocol is not
# yet hardware-validated.  The kernel driver stub (fan_profile=NULL) skips
# hwmon registration for these boards so no pwm* sysfs files appear.
# check_board_support() tests this set BEFORE SUPPORTED_BOARDS, ensuring
# 8CC0 is never returned as "SUPPORTED" despite appearing in that set.
# write_sys_file() reads the live DMI board name (not cached config) so the
# guard fires reliably even on first boot before the cache is populated.
MONITORING_ONLY_BOARDS: set[str] = {
    "8CC0",
}

# Boards where fan RPM telemetry exposed by the kernel driver (e.g. hp-wmi)
# is unsupported, unverified, or known to report incorrect/zero values despite
# spinning physical fans (such as board 8CC0 on HP OMEN 16-ae0xxx).
UNSUPPORTED_TELEMETRY_BOARDS: set[str] = {
    "8CC0",
}

# Default Auto curve: [temp_°C, fan_%].  These are the existing GUI curve
# editor defaults, reused for the service when no user curve is saved.
DEFAULT_AUTO_CURVE: list[list[float]] = [
    [40, 20],
    [55, 40],
    [70, 65],
    [85, 90],
    [95, 100],
]

# Performance reuses the Auto curve's validated breakpoints and increases
# each non-maximum target by 15 percentage points, capped at 100%.  It makes
# no claim about physical RPM; the result is verified mathematically in tests.
DEFAULT_PERFORMANCE_CURVE: list[list[float]] = [
    [temp, min(percent + 15, 100)] for temp, percent in DEFAULT_AUTO_CURVE
]

_OMEN_FAN_CONTROL_DIR = os.environ.get("OMEN_FAN_CONTROL_DIR")
if _OMEN_FAN_CONTROL_DIR:
    OMEN_FAN_DIR = Path(_OMEN_FAN_CONTROL_DIR) / "driver"
else:
    OMEN_FAN_DIR = Path(__file__).parent.absolute() / "data" / "driver"

CONFIG_VERSION = 1

SUPPORTED_BOARDS: set[str] = {
    "84DA", "84DB", "84DC",
    "8572", "8573", "8574", "8575",
    "8600", "8601", "8602", "8603", "8604", "8605", "8606", "8607", "860A",
    "8746", "8747", "8748", "8749", "874A", "8786", "8787", "8788", "878A",
    "878B", "878C", "87B5",
    "886B", "886C", "88C8", "88CB", "88D1", "88D2", "88F4", "88F5", "88F6",
    "88F7", "88FD", "88FE", "88FF",
    "8900", "8901", "8902", "8912", "8917", "8918", "8949", "894A", "89EB",
    "8A15", "8A42", "8BAD", "8E41", "8D2F",

    "88F8", "8A25",
    "8BAB", "8BB3", "8BBE", "8BCA", "8BD4", "8BD5", "8C76", "8C77", "8C78", "8BCD",
    "8C4D", "8C99", "8C9C", "8D26", "8D41", "8D87", "8A44", "8A4D", "8C58", "8BA9", "8BAA",
    "8A3D", "8A43", "8B2F", "8C2F", "8BC2", "8D88", "8DD6", "8E35",
    "8CC0",
}

POSSIBLY_SUPPPORTED_OMEN_BOARDS: set[str] = {
    "84DA", "84DB", "84DC", "8574", "8575", "860A", "87B5", "8572", "8573",
    "8600", "8601", "8602", "8605", "8606", "8607", "8746", "8747", "8749",
    "874A", "8603", "8604", "8748", "886B", "886C", "878A", "878B", "878C",
    "88C8", "88CB", "8786", "8787", "8788", "88D1", "88D2", "88F4", "88FD",
    "88F5", "88F6", "8A13", "8A14", "8A15", "8A16", "88F7", "88FE", "8A17",
    "8A18", "8A19", "8A1A", "8BAD", "8BB0", "88FF", "8900", "8901", "8902",
    "8912", "8917", "8918", "8A97", "8A96", "8D2C", "8949", "8A98", "894A",
    "8B1D", "89EB", "8A4C", "8A4D", "8A4E", "8A40", "8A41", "8A42", "8A43",
    "8A44", "8BA8", "8BA9", "8BAA", "8BAC", "8C76", "8C77", "8C78",
    "8BCA", "8BCB", "8BCF", "8C9B", "8BB4", "8C4E",
    "8C58", "8C75", "8C74", "8C73", "8CC1", "8CF1", "8CF2", "8CF3",
    "8CF4", "8D2F",
}
