from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import click

from .. import get_data_dir
from .._driver_service import (
    find_experimental_module_path,
    get_driver_status,
    init_experimental_driver,
    restore_stock_driver,
    rollback_system,
)
from . import get_controller

DESKTOP_ENTRY_PATH = Path("/usr/share/applications/omen-fan-control-gui.desktop")
ICON_DEST_PATH = Path("/usr/share/icons/hicolor/256x256/apps/omen-fan-control.png")
SYSTEM_MODULE_DEST = Path("/usr/lib/omen-fan-control/hp-wmi.ko")

DESKTOP_FILE_CONTENT = """[Desktop Entry]
Name=OMEN Fan Control
Comment=Fan control and monitoring for HP OMEN laptops
Exec=omen-fan-control-gui
Icon=omen-fan-control
Terminal=false
Type=Application
Categories=System;HardwareSettings;Settings;
StartupNotify=true
"""


@click.command(name="driver-init")
@click.option("--module-path", type=click.Path(exists=True), help="Optional path to custom hp-wmi.ko")
def driver_init(module_path: str | None) -> None:
    """Initialize verified experimental driver with safe stock fallback."""
    success, msg = init_experimental_driver(module_path)
    if success:
        click.echo(f"Success: {msg}")
    else:
        click.echo(f"Error: {msg}", err=True)
        sys.exit(1)


@click.command(name="driver-restore")
def driver_restore() -> None:
    """Unload custom module and restore stock distro hp-wmi driver."""
    success, msg = restore_stock_driver()
    if success:
        click.echo(f"Success: {msg}")
    else:
        click.echo(f"Error: {msg}", err=True)
        sys.exit(1)


@click.command(name="driver-status")
def driver_status() -> None:
    """Display current driver loading status and experimental readiness."""
    status = get_driver_status()
    click.echo("OMEN Fan Control — Driver & Hardware Status:")
    click.echo(f"  Driver State:        {status['status']}")
    click.echo(f"  Module Loaded:       {status['module_loaded']}")
    click.echo(f"  Experimental Active: {status['experimental_active']}")
    click.echo(f"  Stock Active:        {status['stock_active']}")
    click.echo(f"  Speed Sysfs Node:    {'Present' if status['speed_node_exists'] else 'Missing'}")
    click.echo(f"  Max Sysfs Node:      {'Present' if status['max_node_exists'] else 'Missing'}")
    click.echo(f"  Telemetry Node:      {'Present' if status['telemetry_node_exists'] else 'Missing'}")
    click.echo(f"  Module File:         {status['module_file_path'] or 'Not found'}")


@click.command(name="rollback")
def rollback() -> None:
    """Emergency rollback: stops services, unloads custom module, and restores stock hp-wmi."""
    click.echo("Initiating system rollback to stock Ubuntu driver...")
    success, msg = rollback_system()
    if success:
        click.echo(f"Success: {msg}")
        click.echo("\nStock driver verified active. Experimental fan writes are disabled.")
    else:
        click.echo(f"Rollback warning: {msg}", err=True)
        sys.exit(1)


@click.command(name="install-system")
@click.option("--force", is_flag=True, help="Overwrite existing installed files")
def install_system(force: bool) -> None:
    """Install experimental driver, systemd units, and desktop launcher."""
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        click.echo("Error: Root privileges required to install system files. Run with sudo.", err=True)
        sys.exit(1)

    controller = get_controller()
    source_ko = find_experimental_module_path()
    if not source_ko or not source_ko.is_file():
        click.echo("Error: Could not locate verified hp-wmi.ko module file.", err=True)
        sys.exit(1)

    click.echo(f"1. Installing custom module to {SYSTEM_MODULE_DEST}...")
    try:
        SYSTEM_MODULE_DEST.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_ko, SYSTEM_MODULE_DEST)
        SYSTEM_MODULE_DEST.chmod(0o644)
    except Exception as e:
        click.echo(f"Error copying module: {e}", err=True)
        sys.exit(1)

    click.echo("2. Installing systemd units (omen-fan-control-driver.service and omen-fan-control.service)...")
    success, msg = controller.create_service()
    if not success:
        click.echo(f"Error creating services: {msg}", err=True)
        sys.exit(1)

    click.echo("3. Installing desktop entry and icon...")
    try:
        DESKTOP_ENTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
        DESKTOP_ENTRY_PATH.write_text(DESKTOP_FILE_CONTENT)
        DESKTOP_ENTRY_PATH.chmod(0o644)

        logo_src = get_data_dir() / "assets" / "logo.png"
        if logo_src.is_file():
            ICON_DEST_PATH.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(logo_src, ICON_DEST_PATH)
            ICON_DEST_PATH.chmod(0o644)
    except Exception as e:
        click.echo(f"Warning: Could not install desktop files: {e}")

    click.echo("\nInstallation complete! Services enabled and started.")


@click.command(name="uninstall-system")
def uninstall_system() -> None:
    """Uninstall project systemd services, installed module, and desktop files."""
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        click.echo("Error: Root privileges required to uninstall system files. Run with sudo.", err=True)
        sys.exit(1)

    controller = get_controller()
    click.echo("1. Stopping and removing systemd services...")
    controller.remove_all_services()

    click.echo("2. Removing installed custom module file...")
    if SYSTEM_MODULE_DEST.exists():
        try:
            SYSTEM_MODULE_DEST.unlink()
        except Exception:
            pass

    click.echo("3. Removing desktop entry and icon...")
    if DESKTOP_ENTRY_PATH.exists():
        try:
            DESKTOP_ENTRY_PATH.unlink()
        except Exception:
            pass
    if ICON_DEST_PATH.exists():
        try:
            ICON_DEST_PATH.unlink()
        except Exception:
            pass

    click.echo("4. Ensuring stock distro hp-wmi driver is active...")
    restore_stock_driver()

    click.echo("\nUninstallation complete. Machine restored to clean stock state.")
