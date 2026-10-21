"""Driver lifecycle management for HP OMEN board 8CC0.

Manages loading the verified custom experimental driver (hp-wmi.ko) with
experimental_control=1 and experimental_telemetry=1 while preserving the
stock Ubuntu hp_wmi driver as a safe fallback.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
import subprocess
from typing import Any

from . import get_data_dir
from ._constants import (
    EXPERIMENTAL_FAN_MAX_PATH,
    EXPERIMENTAL_FAN_SPEED_PATH,
    EXPERIMENTAL_FAN_TELEMETRY_PATH,
)

logger = logging.getLogger(__name__)

# Standard installation paths for custom experimental module
SYSTEM_MODULE_PATHS: tuple[Path, ...] = (
    Path("/usr/lib/omen-fan-control/hp-wmi.ko"),
    Path("/usr/local/share/omen-fan-control/hp-wmi.ko"),
    Path("/usr/local/share/omen-fan-control/driver/hp-wmi-omen/hp-wmi.ko"),
)


def find_experimental_module_path(custom_path: Path | str | None = None) -> Path | None:
    """Locate the verified experimental hp-wmi.ko module file."""
    if custom_path is not None:
        p = Path(custom_path)
        if p.is_file():
            return p

    # 1. Check system installation locations
    for sys_path in SYSTEM_MODULE_PATHS:
        if sys_path.is_file():
            return sys_path

    # 2. Check repository data directory
    repo_driver = get_data_dir() / "driver" / "hp-wmi-omen" / "hp-wmi.ko"
    if repo_driver.is_file():
        return repo_driver

    # 3. Check relative to current source tree
    fallback_driver = Path(__file__).resolve().parent / "data" / "driver" / "hp-wmi-omen" / "hp-wmi.ko"
    if fallback_driver.is_file():
        return fallback_driver

    return None


def is_module_loaded(module_name: str = "hp_wmi") -> bool:
    """Check if a kernel module is currently loaded in memory."""
    target = module_name.replace("-", "_")
    try:
        modules_file = Path("/proc/modules")
        if modules_file.is_file():
            for line in modules_file.read_text().splitlines():
                parts = line.split()
                if parts and parts[0] == target:
                    return True
            return False
    except Exception:
        pass
    try:
        res = subprocess.run(["lsmod"], capture_output=True, text=True, check=False)
        for line in res.stdout.splitlines():
            if line.split() and line.split()[0] == target:
                return True
    except Exception:
        pass
    return False


def is_custom_driver_active() -> bool:
    """Check if the custom experimental driver is currently active with sysfs nodes."""
    return (
        is_module_loaded("hp_wmi")
        and EXPERIMENTAL_FAN_SPEED_PATH.exists()
        and EXPERIMENTAL_FAN_MAX_PATH.exists()
        and EXPERIMENTAL_FAN_TELEMETRY_PATH.exists()
    )


def is_stock_driver_active() -> bool:
    """Check if the stock hp_wmi driver is loaded without experimental sysfs attributes."""
    return is_module_loaded("hp_wmi") and not EXPERIMENTAL_FAN_SPEED_PATH.exists()


def get_driver_status() -> dict[str, Any]:
    """Return structured status of current kernel driver and experimental readiness."""
    loaded = is_module_loaded("hp_wmi")
    custom_active = is_custom_driver_active()
    stock_active = is_stock_driver_active()

    status = "not_loaded"
    if custom_active:
        status = "experimental_active"
    elif stock_active:
        status = "stock_fallback_active"
    elif loaded:
        status = "loaded_without_nodes"

    module_file = find_experimental_module_path()

    return {
        "status": status,
        "module_loaded": loaded,
        "experimental_active": custom_active,
        "stock_active": stock_active,
        "speed_node_exists": EXPERIMENTAL_FAN_SPEED_PATH.exists(),
        "max_node_exists": EXPERIMENTAL_FAN_MAX_PATH.exists(),
        "telemetry_node_exists": EXPERIMENTAL_FAN_TELEMETRY_PATH.exists(),
        "module_file_found": module_file is not None,
        "module_file_path": str(module_file) if module_file else None,
    }


def init_experimental_driver(module_path: Path | str | None = None) -> tuple[bool, str]:
    """Idempotently initialize verified experimental hp-wmi.ko driver.

    If experimental driver is already active with sysfs attributes, succeeds immediately.
    If stock driver is loaded, safely unloads it and inserts custom module with
    experimental_control=1 experimental_telemetry=1.
    If insertion or verification fails, automatically restores stock hp-wmi as fallback.
    """
    # 1. Root permission check
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        return False, "Root privileges required to initialize kernel modules."

    # 2. Idempotent check: if custom driver is already loaded and ready, exit clean
    if is_custom_driver_active():
        return True, "Experimental driver is already loaded and verified."

    # 3. Locate custom module file
    ko_file = find_experimental_module_path(module_path)
    if ko_file is None:
        # Custom module missing; ensure stock is loaded as safe fallback
        if not is_module_loaded("hp_wmi"):
            subprocess.run(["modprobe", "hp-wmi"], check=False)
        return False, "Verified experimental module (hp-wmi.ko) not found. Preserved stock driver."

    # 4. Safely unload existing module (whether stock or partial custom)
    if is_module_loaded("hp_wmi"):
        unload_res = subprocess.run(["modprobe", "-r", "hp-wmi"], capture_output=True, text=True, check=False)
        if unload_res.returncode != 0:
            # Fallback to rmmod
            subprocess.run(["rmmod", "hp_wmi"], capture_output=True, text=True, check=False)

        if is_module_loaded("hp_wmi"):
            return False, "Failed to unload existing hp_wmi module. Cannot safely insert experimental driver."

    # 5. Pre-load module dependencies
    deps = ("wmi", "platform_profile", "sparse_keymap")
    for dep in deps:
        subprocess.run(["modprobe", dep], check=False, capture_output=True)

    # 6. Insert verified custom module with explicit experimental parameters
    cmd = [
        "insmod",
        str(ko_file),
        "experimental_control=1",
        "experimental_telemetry=1",
    ]
    insmod_res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if insmod_res.returncode != 0:
        err = insmod_res.stderr.strip() or "Unknown insmod error"
        # Immediate fallback: restore stock driver
        subprocess.run(["modprobe", "hp-wmi"], check=False)
        return False, f"Failed to insert experimental driver ({err}). Restored stock hp-wmi fallback."

    # 7. Post-load verification of sysfs attributes
    if not (
        EXPERIMENTAL_FAN_SPEED_PATH.exists()
        and EXPERIMENTAL_FAN_MAX_PATH.exists()
        and EXPERIMENTAL_FAN_TELEMETRY_PATH.exists()
    ):
        # Nodes failed to register: roll back to stock immediately
        subprocess.run(["rmmod", "hp_wmi"], check=False, capture_output=True)
        subprocess.run(["modprobe", "hp-wmi"], check=False)
        return False, "Experimental sysfs nodes failed to register. Restored stock hp-wmi fallback."

    return True, f"Experimental driver successfully loaded from {ko_file} and verified."


def restore_stock_driver() -> tuple[bool, str]:
    """Safely unload custom module and restore stock distro hp-wmi driver."""
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        return False, "Root privileges required to restore kernel driver."

    if is_module_loaded("hp_wmi"):
        # Unload custom driver
        subprocess.run(["rmmod", "hp_wmi"], check=False, capture_output=True)

    # Load stock driver
    res = subprocess.run(["modprobe", "hp-wmi"], capture_output=True, text=True, check=False)
    if res.returncode != 0:
        return False, f"Failed to modprobe stock hp-wmi: {res.stderr.strip()}"

    if is_custom_driver_active():
        return False, "Warning: Custom driver still active after attempted restore."

    return True, "Stock Ubuntu hp-wmi driver successfully restored."


def rollback_system() -> tuple[bool, str]:
    """Perform emergency rollback to stock driver and stop project services."""
    actions: list[str] = []

    # 1. Stop fan control daemon service
    try:
        subprocess.run(["systemctl", "stop", "omen-fan-control.service"], check=False, capture_output=True)
        actions.append("Stopped omen-fan-control.service")
    except Exception:
        pass

    # 2. Stop driver service
    try:
        subprocess.run(["systemctl", "stop", "omen-fan-control-driver.service"], check=False, capture_output=True)
        actions.append("Stopped omen-fan-control-driver.service")
    except Exception:
        pass

    # 3. Restore stock driver
    success, msg = restore_stock_driver()
    actions.append(msg)

    # 4. Clean up volatile config and status files in /run
    from ._constants import VOLATILE_CONFIG_FILE
    try:
        if VOLATILE_CONFIG_FILE.exists():
            VOLATILE_CONFIG_FILE.unlink()
            actions.append("Removed volatile config")
    except Exception:
        pass

    status_file = Path("/run/omen-fan-control/status.json")
    try:
        if status_file.exists():
            status_file.unlink()
            actions.append("Removed daemon status file")
    except Exception:
        pass

    summary = " | ".join(actions)
    if success:
        return True, f"Rollback complete: {summary}"
    return False, f"Rollback encountered errors: {summary}"
