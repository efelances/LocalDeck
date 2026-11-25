"""Safe, argument-free application launcher and installed Windows app detector."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path, PureWindowsPath

BLOCKED_EXECUTABLES: frozenset[str] = frozenset(
    {
        "cmd.exe",
        "powershell.exe",
        "pwsh.exe",
        "wscript.exe",
        "cscript.exe",
        "mshta.exe",
        "rundll32.exe",
        "reg.exe",
        "regedit.exe",
        "msiexec.exe",
        "schtasks.exe",
        "net.exe",
        "net1.exe",
        "certutil.exe",
        "bitsadmin.exe",
        "curl.exe",
        "conhost.exe",
        "wt.exe",
        "bash.exe",
        "wsl.exe",
    }
)

_FORBIDDEN_CHARS = ('"', "'", "\n", "\r", "|", "&", ";", "`", "<", ">")


def validate_safe_app_path(raw_path: str) -> str:
    """Validate that `raw_path` points to a safe `.exe` or `.lnk` file without arguments."""
    cleaned = (raw_path or "").strip()
    if not cleaned:
        raise ValueError("Application path is required")
    if any(ch in cleaned for ch in _FORBIDDEN_CHARS):
        raise ValueError("Application path contains forbidden characters")
    win_path = PureWindowsPath(cleaned)
    suffix = win_path.suffix.lower()
    if suffix not in (".exe", ".lnk"):
        raise ValueError("Only .exe and .lnk files are allowed")
    basename = win_path.name.lower()
    if not basename or basename in BLOCKED_EXECUTABLES:
        raise ValueError(f"Executable is blocked for security: {basename}")
    return cleaned


def launch_safe_app(app_path: str) -> None:
    """Launch an allowlisted local `.exe` or `.lnk` with zero arguments and `shell=False`."""
    cleaned = validate_safe_app_path(app_path)
    target = Path(cleaned)
    if not target.is_file():
        raise FileNotFoundError(f"Application not found: {cleaned}")
    if target.suffix.lower() == ".lnk":
        if sys.platform != "win32":
            raise OSError("Shortcut launching is only supported on Windows")
        os.startfile(str(target))  # type: ignore[attr-defined]
        return
    subprocess.Popen([str(target)], shell=False, cwd=str(target.parent))


def detect_installed_apps() -> list[dict[str, str]]:
    """Return a list of common installed Windows applications found on disk."""
    if sys.platform != "win32":
        return []
    local = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    roaming = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    pf = Path(os.environ.get("ProgramFiles") or r"C:\Program Files")
    pf86 = Path(os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)")
    sys32 = Path(os.environ.get("SystemRoot") or r"C:\Windows") / "System32"

    candidates = [
        ("Spotify", [roaming / "Spotify" / "Spotify.exe", local / "Microsoft" / "WindowsApps" / "Spotify.exe"]),
        ("Discord", [local / "Discord" / "Update.exe", local / "Programs" / "Discord" / "Discord.exe", roaming / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Discord Inc" / "Discord.lnk"]),
        ("OBS Studio", [pf / "obs-studio" / "bin" / "64bit" / "obs64.exe", pf86 / "obs-studio" / "bin" / "64bit" / "obs64.exe"]),
        ("VS Code", [local / "Programs" / "Microsoft VS Code" / "Code.exe", pf / "Microsoft VS Code" / "Code.exe"]),
        ("Google Chrome", [pf / "Google" / "Chrome" / "Application" / "chrome.exe", pf86 / "Google" / "Chrome" / "Application" / "chrome.exe", local / "Google" / "Chrome" / "Application" / "chrome.exe"]),
        ("Microsoft Edge", [pf86 / "Microsoft" / "Edge" / "Application" / "msedge.exe", pf / "Microsoft" / "Edge" / "Application" / "msedge.exe"]),
        ("Brave", [pf / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe", local / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe"]),
        ("Steam", [pf86 / "Steam" / "steam.exe", pf / "Steam" / "steam.exe"]),
        ("Calculator", [sys32 / "calc.exe"]),
        ("Notepad", [sys32 / "notepad.exe"]),
        ("Paint", [sys32 / "mspaint.exe"]),
    ]

    found: list[dict[str, str]] = []
    for name, paths in candidates:
        for p in paths:
            if p.is_file():
                try:
                    safe = validate_safe_app_path(str(p))
                except ValueError:
                    continue
                found.append({"name": name, "path": safe})
                break
    return found
