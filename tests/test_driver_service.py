from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from omen_fan_control._driver_service import (
    find_experimental_module_path,
    get_driver_status,
    init_experimental_driver,
    is_custom_driver_active,
    is_module_loaded,
    is_stock_driver_active,
    restore_stock_driver,
    rollback_system,
)


class TestDriverService(unittest.TestCase):
    def test_find_experimental_module_path_finds_file(self):
        with patch.object(Path, "is_file", return_value=True):
            found = find_experimental_module_path("/custom/path/hp-wmi.ko")
            self.assertEqual(found, Path("/custom/path/hp-wmi.ko"))

    def test_find_experimental_module_path_not_found(self):
        with patch.object(Path, "is_file", return_value=False):
            found = find_experimental_module_path(None)
            self.assertIsNone(found)

    def test_is_module_loaded(self):
        fake_modules = "hp_wmi 24576 0 - Live 0xffffffffc000\nwmi 36864 1 hp_wmi, Live 0xffffffffc010\n"
        with (
            patch.object(Path, "is_file", return_value=True),
            patch.object(Path, "read_text", return_value=fake_modules),
        ):
            self.assertTrue(is_module_loaded("hp_wmi"))
            self.assertTrue(is_module_loaded("hp-wmi"))
            self.assertFalse(is_module_loaded("nvidia"))

    def test_is_custom_driver_active(self):
        with (
            patch("omen_fan_control._driver_service.is_module_loaded", return_value=True),
            patch.object(Path, "exists", return_value=True),
        ):
            self.assertTrue(is_custom_driver_active())

    def test_is_stock_driver_active(self):
        with (
            patch("omen_fan_control._driver_service.is_module_loaded", return_value=True),
            patch("omen_fan_control._driver_service.is_custom_driver_active", return_value=False),
        ):
            self.assertTrue(is_stock_driver_active())

    def test_init_experimental_driver_requires_root(self):
        with patch("os.geteuid", return_value=1000):
            success, msg = init_experimental_driver()
            self.assertFalse(success)
            self.assertIn("Root privileges required", msg)

    def test_init_experimental_driver_already_active(self):
        with (
            patch("os.geteuid", return_value=0),
            patch("omen_fan_control._driver_service.is_custom_driver_active", return_value=True),
        ):
            success, msg = init_experimental_driver()
            self.assertTrue(success)
            self.assertIn("already loaded", msg)

    def test_init_experimental_driver_module_missing_falls_back_to_stock(self):
        with (
            patch("os.geteuid", return_value=0),
            patch("omen_fan_control._driver_service.is_custom_driver_active", return_value=False),
            patch("omen_fan_control._driver_service.find_experimental_module_path", return_value=None),
            patch("omen_fan_control._driver_service.is_module_loaded", return_value=False),
            patch("subprocess.run") as mock_run,
        ):
            success, msg = init_experimental_driver()
            self.assertFalse(success)
            self.assertIn("not found. Preserved stock driver", msg)
            mock_run.assert_called_with(["modprobe", "hp-wmi"], check=False)

    def test_init_experimental_driver_insmod_failure_restores_stock(self):
        def _fake_run(cmd, *args, **kwargs):
            res = MagicMock()
            if cmd and cmd[0] == "insmod":
                res.returncode = 1
                res.stderr = "Permission denied"
            else:
                res.returncode = 0
                res.stderr = ""
            return res

        with (
            patch("os.geteuid", return_value=0),
            patch("omen_fan_control._driver_service.is_custom_driver_active", return_value=False),
            patch("omen_fan_control._driver_service.find_experimental_module_path", return_value=Path("/tmp/hp-wmi.ko")),
            patch("omen_fan_control._driver_service.is_module_loaded", side_effect=[True, False, False]),
            patch("subprocess.run", side_effect=_fake_run),
        ):
            success, msg = init_experimental_driver()
            self.assertFalse(success)
            self.assertIn("Restored stock hp-wmi fallback", msg)

    def test_init_experimental_driver_success(self):
        fake_res = MagicMock()
        fake_res.returncode = 0

        with (
            patch("os.geteuid", return_value=0),
            patch("omen_fan_control._driver_service.is_custom_driver_active", return_value=False),
            patch("omen_fan_control._driver_service.find_experimental_module_path", return_value=Path("/tmp/hp-wmi.ko")),
            patch("omen_fan_control._driver_service.is_module_loaded", return_value=False),
            patch("subprocess.run", return_value=fake_res),
            patch.object(Path, "exists", return_value=True),
        ):
            success, msg = init_experimental_driver()
            self.assertTrue(success)
            self.assertIn("successfully loaded", msg)

    def test_restore_stock_driver(self):
        fake_res = MagicMock()
        fake_res.returncode = 0
        fake_res.stderr = ""

        with (
            patch("os.geteuid", return_value=0),
            patch("omen_fan_control._driver_service.is_module_loaded", return_value=True),
            patch("subprocess.run", return_value=fake_res),
            patch("omen_fan_control._driver_service.is_custom_driver_active", return_value=False),
        ):
            success, msg = restore_stock_driver()
            self.assertTrue(success)
            self.assertIn("successfully restored", msg)

    def test_rollback_system(self):
        with (
            patch("os.geteuid", return_value=0),
            patch("subprocess.run"),
            patch("omen_fan_control._driver_service.restore_stock_driver", return_value=(True, "Restored stock")),
            patch("omen_fan_control._driver_service.is_stock_driver_active", return_value=True),
        ):
            success, msg = rollback_system()
            self.assertTrue(success)
            self.assertIn("Rollback complete", msg)

    def test_get_driver_status(self):
        with (
            patch("omen_fan_control._driver_service.is_custom_driver_active", return_value=True),
            patch("omen_fan_control._driver_service.is_module_loaded", return_value=True),
            patch.object(Path, "exists", return_value=True),
            patch("omen_fan_control._driver_service.find_experimental_module_path", return_value=Path("/tmp/hp-wmi.ko")),
        ):
            status = get_driver_status()
            self.assertEqual(status["status"], "experimental_active")
            self.assertTrue(status["experimental_active"])
            self.assertTrue(status["speed_node_exists"])


if __name__ == "__main__":
    unittest.main()
