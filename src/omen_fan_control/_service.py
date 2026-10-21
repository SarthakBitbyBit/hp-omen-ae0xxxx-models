from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from ._driver_service import restore_stock_driver

if TYPE_CHECKING:
    pass

DRIVER_SERVICE_UNIT_NAME = "omen-fan-control-driver.service"
DAEMON_SERVICE_UNIT_NAME = "omen-fan-control.service"

DRIVER_SERVICE_PATH = Path(f"/etc/systemd/system/{DRIVER_SERVICE_UNIT_NAME}")
DAEMON_SERVICE_PATH = Path(f"/etc/systemd/system/{DAEMON_SERVICE_UNIT_NAME}")


def _restore_selinux_context(path: Path | str) -> None:
    """Restore SELinux security context on the file if restorecon is available."""
    if shutil.which("restorecon"):
        try:
            subprocess.run(["restorecon", "-v", str(path)], check=False, capture_output=True)
        except Exception:
            pass


def get_driver_service_content(executable: str | None = None) -> str:
    """Generate systemd unit file content for the experimental driver initialization service."""
    exe = executable or sys.executable
    return f"""[Unit]
Description=HP Omen Fan Control - Experimental Driver Service
DefaultDependencies=no
After=systemd-modules-load.service local-fs.target
Before=omen-fan-control.service sysinit.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart={exe} -m omen_fan_control.cli driver-init
ExecStop={exe} -m omen_fan_control.cli driver-restore
TimeoutSec=30

[Install]
WantedBy=multi-user.target
"""


def get_daemon_service_content(executable: str | None = None) -> str:
    """Generate systemd unit file content for the fan control polling daemon."""
    exe = executable or sys.executable
    return f"""[Unit]
Description=HP Omen Fan Control Service
Requires={DRIVER_SERVICE_UNIT_NAME}
After={DRIVER_SERVICE_UNIT_NAME} multi-user.target

[Service]
Type=simple
ExecStart={exe} -m omen_fan_control.cli serve
WorkingDirectory=/
RuntimeDirectory=omen-fan-control
RuntimeDirectoryMode=0777
UMask=0000
Restart=on-failure
StartLimitBurst=5
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
"""


class ServiceManagerMixin:
    def create_service(self, executable: str | None = None) -> tuple[bool, str]:
        """Install and enable both driver and daemon systemd services."""
        exe = executable or sys.executable
        driver_unit = get_driver_service_content(exe)
        daemon_unit = get_daemon_service_content(exe)

        try:
            # 1. Install driver service
            DRIVER_SERVICE_PATH.parent.mkdir(parents=True, exist_ok=True)
            DRIVER_SERVICE_PATH.write_text(driver_unit)
            try:
                DRIVER_SERVICE_PATH.chmod(0o644)
            except Exception:
                pass
            _restore_selinux_context(DRIVER_SERVICE_PATH)

            # 2. Install daemon service
            DAEMON_SERVICE_PATH.write_text(daemon_unit)
            try:
                DAEMON_SERVICE_PATH.chmod(0o644)
            except Exception:
                pass
            _restore_selinux_context(DAEMON_SERVICE_PATH)

            # 3. Reload daemon and enable services
            subprocess.run(["systemctl", "daemon-reload"], check=True)
            subprocess.run(["systemctl", "enable", DRIVER_SERVICE_UNIT_NAME], check=True)
            subprocess.run(["systemctl", "enable", DAEMON_SERVICE_UNIT_NAME], check=True)

            # 4. Start services in correct dependency order
            subprocess.run(["systemctl", "start", DRIVER_SERVICE_UNIT_NAME], check=True)
            subprocess.run(["systemctl", "start", DAEMON_SERVICE_UNIT_NAME], check=True)
            return True, "Services created and started."
        except Exception as e:
            return False, f"Failed to create service: {e}"

    def remove_service(self, remove_driver: bool = False) -> tuple[bool, str]:
        """Stop, disable, and remove daemon service (and optionally driver service)."""
        try:
            # 1. Stop daemon first, then driver if requested
            subprocess.run(["systemctl", "stop", DAEMON_SERVICE_UNIT_NAME], check=False)
            if remove_driver:
                subprocess.run(["systemctl", "stop", DRIVER_SERVICE_UNIT_NAME], check=False)

            # 2. Disable units
            subprocess.run(["systemctl", "disable", DAEMON_SERVICE_UNIT_NAME], check=False)
            if remove_driver:
                subprocess.run(["systemctl", "disable", DRIVER_SERVICE_UNIT_NAME], check=False)

            # 3. Unlink daemon service file (and driver if requested)
            if DAEMON_SERVICE_PATH.exists():
                try:
                    DAEMON_SERVICE_PATH.unlink()
                except Exception:
                    subprocess.run(["rm", "-f", str(DAEMON_SERVICE_PATH)], check=False)

            if remove_driver and DRIVER_SERVICE_PATH.exists():
                try:
                    DRIVER_SERVICE_PATH.unlink()
                except Exception:
                    subprocess.run(["rm", "-f", str(DRIVER_SERVICE_PATH)], check=False)

            subprocess.run(["systemctl", "daemon-reload"], check=True)

            # 4. Restore stock driver if driver service was removed
            if remove_driver:
                restore_stock_driver()

            return True, "Service removed."
        except Exception as e:
            return False, f"Failed to remove service: {e}"

    def remove_driver_service(self) -> tuple[bool, str]:
        """Stop, disable, and remove driver service and restore stock driver."""
        return self.remove_service(remove_driver=True)

    def remove_all_services(self) -> tuple[bool, str]:
        """Stop, disable, and remove both driver and daemon services, restoring stock driver."""
        return self.remove_service(remove_driver=True)

    def restart_service(self) -> tuple[bool, str]:
        try:
            subprocess.run(["systemctl", "restart", DAEMON_SERVICE_UNIT_NAME], check=True)
            return True, "Service restarted."
        except Exception as e:
            return False, f"Failed to restart service: {e}"

    def start_service(self) -> tuple[bool, str]:
        try:
            subprocess.run(["systemctl", "start", DRIVER_SERVICE_UNIT_NAME], check=True)
            subprocess.run(["systemctl", "start", DAEMON_SERVICE_UNIT_NAME], check=True)
            return True, "Service started."
        except Exception as e:
            return False, f"Failed to start service: {e}"

    def stop_service(self) -> tuple[bool, str]:
        try:
            subprocess.run(["systemctl", "stop", DAEMON_SERVICE_UNIT_NAME], check=True)
            return True, "Service stopped."
        except Exception as e:
            return False, f"Failed to stop service: {e}"

    @staticmethod
    def is_service_installed() -> bool:
        for p in (
            DAEMON_SERVICE_PATH,
            Path("/usr/lib/systemd/system/omen-fan-control.service"),
            Path("/lib/systemd/system/omen-fan-control.service"),
        ):
            if p.exists():
                return True
        return False

    @staticmethod
    def is_driver_service_installed() -> bool:
        for p in (
            DRIVER_SERVICE_PATH,
            Path("/usr/lib/systemd/system/omen-fan-control-driver.service"),
            Path("/lib/systemd/system/omen-fan-control-driver.service"),
        ):
            if p.exists():
                return True
        return False

    @staticmethod
    def is_service_running() -> bool:
        try:
            res = subprocess.run(
                ["systemctl", "is-active", DAEMON_SERVICE_UNIT_NAME],
                capture_output=True,
                text=True,
                check=False,
            )
            return res.stdout.strip() == "active"
        except Exception:
            return False

    @staticmethod
    def is_driver_service_running() -> bool:
        try:
            res = subprocess.run(
                ["systemctl", "is-active", DRIVER_SERVICE_UNIT_NAME],
                capture_output=True,
                text=True,
                check=False,
            )
            return res.stdout.strip() == "active"
        except Exception:
            return False
