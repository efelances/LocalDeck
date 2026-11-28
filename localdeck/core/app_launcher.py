"""Safe, argument-free application launcher and installed Windows app detector."""

from __future__ import annotations

import os
import struct
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


_LNK_CLSID = bytes.fromhex("0114020000000000c000000000000046")


def _read_c_string(raw: bytes, offset: int, wide: bool) -> str:
    if offset <= 0 or offset >= len(raw):
        return ""
    if wide:
        end = raw.find(b"\x00\x00", offset)
        while end != -1 and (end - offset) % 2 != 0:
            end = raw.find(b"\x00\x00", end + 1)
        slice_bytes = raw[offset:] if end == -1 else raw[offset:end]
        return slice_bytes.decode("utf-16-le", errors="ignore").rstrip("\x00")
    end = raw.find(b"\x00", offset)
    slice_bytes = raw[offset:] if end == -1 else raw[offset:end]
    return slice_bytes.decode("mbcs" if sys.platform == "win32" else "latin-1", errors="ignore").rstrip("\x00")


def parse_lnk_target_and_args(lnk_path: Path) -> tuple[str, str]:
    """Parse target path and arguments from a Windows .lnk (MS-SHLLINK) file."""
    raw = lnk_path.read_bytes()
    if len(raw) < 76:
        raise ValueError("Invalid shortcut file")
    hdr_size = struct.unpack_from("<I", raw, 0)[0]
    if hdr_size != 0x4C or raw[4:20] != _LNK_CLSID:
        raise ValueError("Invalid shortcut header")

    flags = struct.unpack_from("<I", raw, 20)[0]
    pos = hdr_size
    if flags & 0x01:
        if pos + 2 > len(raw):
            raise ValueError("Truncated shortcut IDList")
        id_size = struct.unpack_from("<H", raw, pos)[0]
        pos += 2 + id_size

    target = ""
    if flags & 0x02:
        if pos + 12 > len(raw):
            raise ValueError("Truncated shortcut LinkInfo")
        li_size, li_hdr, li_flags = struct.unpack_from("<III", raw, pos)
        if li_flags & 0x01:
            if li_hdr >= 0x24 and pos + 32 <= len(raw):
                uoff = struct.unpack_from("<I", raw, pos + 28)[0]
                if uoff:
                    target = _read_c_string(raw, pos + uoff, wide=True)
            if not target and pos + 20 <= len(raw):
                aoff = struct.unpack_from("<I", raw, pos + 16)[0]
                if aoff:
                    target = _read_c_string(raw, pos + aoff, wide=False)
        pos += li_size

    is_uni = bool(flags & 0x80)
    args = ""
    rel_path = ""
    for bit, field_name in (
        (0x04, "name"),
        (0x08, "relative_path"),
        (0x10, "working_dir"),
        (0x20, "arguments"),
        (0x40, "icon_location"),
    ):
        if flags & bit:
            if pos + 2 > len(raw):
                break
            char_cnt = struct.unpack_from("<H", raw, pos)[0]
            pos += 2
            byte_len = char_cnt * (2 if is_uni else 1)
            val = raw[pos : pos + byte_len].decode(
                "utf-16-le" if is_uni else ("mbcs" if sys.platform == "win32" else "latin-1"),
                errors="ignore",
            )
            pos += byte_len
            if field_name == "arguments":
                args = val
            elif field_name == "relative_path":
                rel_path = val

    env_target = ""
    while pos + 8 <= len(raw):
        blk_size, blk_sig = struct.unpack_from("<II", raw, pos)
        if blk_size < 4 or pos + blk_size > len(raw):
            break
        if blk_sig == 0xA0000001 and blk_size >= 788:
            env_target = _read_c_string(raw, pos + 268, wide=True) or _read_c_string(raw, pos + 8, wide=False)
        pos += blk_size

    resolved_target = target.strip() or env_target.strip() or rel_path.strip()
    return (resolved_target, args.strip())


def validate_lnk_file(lnk_path: Path) -> None:
    """Validate a Windows .lnk shortcut file ensures safe target and zero arguments."""
    target_path, arguments = parse_lnk_target_and_args(lnk_path)
    if arguments:
        raise ValueError("Shortcut arguments are not allowed for security")
    if not target_path:
        raise ValueError("Shortcut target path is empty")
    validate_safe_app_path(os.path.expandvars(target_path))


def launch_safe_app(app_path: str) -> None:
    """Launch an allowlisted local `.exe` or `.lnk` with zero arguments and `shell=False`."""
    cleaned = validate_safe_app_path(app_path)
    target = Path(cleaned)
    if not target.is_file():
        raise FileNotFoundError(f"Application not found: {cleaned}")
    if target.suffix.lower() == ".lnk":
        if sys.platform != "win32":
            raise OSError("Shortcut launching is only supported on Windows")
        validate_lnk_file(target)
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
