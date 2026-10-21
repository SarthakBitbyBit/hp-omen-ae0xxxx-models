from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from omen_fan_control._service import (
    DAEMON_SERVICE_UNIT_NAME,
    DRIVER_SERVICE_UNIT_NAME,
    ServiceManagerMixin,
    get_daemon_service_content,
    get_driver_service_content,
)


class TestSystemdUnits(unittest.TestCase):
    def test_driver_service_unit_content(self):
        content = get_driver_service_content("/usr/bin/python3")
        self.assertIn("Description=HP Omen Fan Control - Experimental Driver Service", content)
        self.assertIn("Type=oneshot", content)
        self.assertIn("RemainAfterExit=yes", content)
        self.assertIn("ExecStart=/usr/bin/python3 -m omen_fan_control.cli driver-init", content)
        self.assertIn("ExecStop=/usr/bin/python3 -m omen_fan_control.cli driver-restore", content)
        self.assertIn("Before=omen-fan-control.service sysinit.target", content)

    def test_daemon_service_unit_content(self):
        content = get_daemon_service_content("/usr/bin/python3")
        self.assertIn("Description=HP Omen Fan Control Service", content)
        self.assertIn(f"Requires={DRIVER_SERVICE_UNIT_NAME}", content)
        self.assertIn(f"After={DRIVER_SERVICE_UNIT_NAME}", content)
        self.assertIn("RuntimeDirectory=omen-fan-control", content)
        self.assertIn("RuntimeDirectoryMode=0777", content)
        self.assertIn("ExecStart=/usr/bin/python3 -m omen_fan_control.cli serve", content)
        self.assertIn("Restart=on-failure", content)

    def test_create_service_installs_both_units_in_order(self):
        mgr = ServiceManagerMixin()

        with (
            patch.object(Path, "mkdir"),
            patch.object(Path, "write_text") as mock_write,
            patch.object(Path, "chmod"),
            patch("subprocess.run") as mock_run,
        ):
            success, msg = mgr.create_service("/usr/bin/python3")
            self.assertTrue(success)
            self.assertIn("Services created", msg)

            # Check systemctl calls
            calls = [c[0][0] for c in mock_run.call_args_list]
            self.assertEqual(calls[0], ["systemctl", "daemon-reload"])
            self.assertEqual(calls[1], ["systemctl", "enable", DRIVER_SERVICE_UNIT_NAME])
            self.assertEqual(calls[2], ["systemctl", "enable", DAEMON_SERVICE_UNIT_NAME])
            self.assertEqual(calls[3], ["systemctl", "start", DRIVER_SERVICE_UNIT_NAME])
            self.assertEqual(calls[4], ["systemctl", "start", DAEMON_SERVICE_UNIT_NAME])

    def test_remove_service_clean_removal(self):
        mgr = ServiceManagerMixin()

        with (
            patch("subprocess.run") as mock_run,
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "unlink") as mock_unlink,
            patch("omen_fan_control._service.restore_stock_driver") as mock_restore,
        ):
            success, msg = mgr.remove_all_services()
            self.assertTrue(success)
            self.assertEqual(mock_unlink.call_count, 2)
            mock_restore.assert_called_once()


if __name__ == "__main__":
    unittest.main()
