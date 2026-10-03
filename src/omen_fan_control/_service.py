from __future__ import annotations

import json
import shutil
import sys
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from ._constants import OMEN_FAN_DIR, VOLATILE_CONFIG_FILE

if TYPE_CHECKING:
    pass


def _restore_selinux_context(path: Path | str) -> None:
    """Restore SELinux security context on the file if restorecon is available."""
    if shutil.which("restorecon"):
        try:
            subprocess.run(["restorecon", "-v", str(path)], check=False, capture_output=True)
        except Exception:
            pass


class ServiceManagerMixin:
    def create_service(self) -> tuple[bool, str]:
        service_content = f"""[Unit]
Description=HP Omen Fan Control Service
After=multi-user.target

[Service]
Type=simple
ExecStart={sys.executable} -m omen_fan_control.cli serve
WorkingDirectory=/
Restart=on-failure
StartLimitBurst=5
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
"""
        service_path = Path("/etc/systemd/system/omen-fan-control.service")

        try:
            service_path.parent.mkdir(parents=True, exist_ok=True)
            service_path.write_text(service_content)
            try:
                service_path.chmod(0o644)
            except Exception:
                pass
            _restore_selinux_context(service_path)
            subprocess.run(["systemctl", "daemon-reload"], check=True)
            subprocess.run(["systemctl", "enable", "omen-fan-control.service"], check=True)
            subprocess.run(["systemctl", "start", "omen-fan-control.service"], check=True)
            return True, "Service created and started."
        except Exception as e:
            return False, f"Failed to create service: {e}"

    def remove_service(self) -> tuple[bool, str]:
        try:
            subprocess.run(["systemctl", "stop", "omen-fan-control.service"], check=False)
            subprocess.run(["systemctl", "disable", "omen-fan-control.service"], check=False)

            service_path = Path("/etc/systemd/system/omen-fan-control.service")
            if service_path.exists():
                try:
                    service_path.unlink()
                except Exception:
                    subprocess.run(["rm", "-f", str(service_path)], check=False)

            subprocess.run(["systemctl", "daemon-reload"], check=True)
            return True, "Service removed."
        except Exception as e:
            return False, f"Failed to remove service: {e}"

    def restart_service(self) -> tuple[bool, str]:
        try:
            subprocess.run(["systemctl", "restart", "omen-fan-control.service"], check=True)
            return True, "Service restarted."
        except Exception as e:
            return False, f"Failed to restart service: {e}"

    def start_service(self) -> tuple[bool, str]:
        try:
            subprocess.run(["systemctl", "start", "omen-fan-control.service"], check=True)
            return True, "Service started."
        except Exception as e:
            return False, f"Failed to start service: {e}"

    def stop_service(self) -> tuple[bool, str]:
        try:
            subprocess.run(["systemctl", "stop", "omen-fan-control.service"], check=True)
            return True, "Service stopped."
        except Exception as e:
            return False, f"Failed to stop service: {e}"

    @staticmethod
    def is_service_installed() -> bool:
        for p in (
            Path("/etc/systemd/system/omen-fan-control.service"),
            Path("/usr/lib/systemd/system/omen-fan-control.service"),
            Path("/lib/systemd/system/omen-fan-control.service"),
        ):
            if p.exists():
                return True
        return False

    @staticmethod
    def is_service_running() -> bool:
        try:
            res = subprocess.run(["systemctl", "is-active", "omen-fan-control.service"], capture_output=True, text=True)
            return res.stdout.strip() == "active"
        except Exception:
            return False


