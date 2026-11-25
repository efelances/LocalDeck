"""Windows auto-start registration via HKCU Run registry key."""

from __future__ import annotations

import os
import sys
from pathlib import Path

_RUN_SUBKEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = "LocalDeck"


def get_autostart_command() -> str:
    """Build the command registered under HKCU Run to launch LocalDeck minimized to tray."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --minimized'
    local_app = os.environ.get("LOCALAPPDATA")
    if local_app:
        installed_exe = Path(local_app) / "LocalDeck" / "app" / "LocalDeck.exe"
        if installed_exe.is_file():
            return f'"{installed_exe}" --minimized'
    return f'"{sys.executable}" -m localdeck --minimized'


def is_autostart_enabled() -> bool:
    """Return True if LocalDeck is configured to start automatically with Windows."""
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_SUBKEY, 0, winreg.KEY_READ) as key:
            val, _ = winreg.QueryValueEx(key, _VALUE_NAME)
            return bool(str(val).strip())
    except OSError:
        return False


def set_autostart(enabled: bool) -> bool:
    """Enable or disable HKCU Windows startup entry for LocalDeck."""
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _RUN_SUBKEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, _VALUE_NAME, 0, winreg.REG_SZ, get_autostart_command())
                return True
            try:
                winreg.DeleteValue(key, _VALUE_NAME)
            except FileNotFoundError:
                pass
            return False
    except OSError:
        return is_autostart_enabled()
