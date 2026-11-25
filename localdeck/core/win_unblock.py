"""Remove Windows Mark-of-the-Web (Zone.Identifier) streams from extracted binaries."""

from __future__ import annotations

import os
import sys
from pathlib import Path

_BINARY_SUFFIXES = frozenset({".dll", ".pyd", ".exe"})


def unblock_downloaded_binaries(root_dir: Path) -> int:
    """Strip `:Zone.Identifier` alternate data streams under `root_dir` on Windows."""
    if sys.platform != "win32":
        return 0
    root = Path(root_dir)
    if not root.exists():
        return 0

    unblocked = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            dot_idx = name.rfind(".")
            if dot_idx == -1 or name[dot_idx:].lower() not in _BINARY_SUFFIXES:
                continue
            ads_path = f"{os.path.join(dirpath, name)}:Zone.Identifier"
            try:
                os.unlink(ads_path)
                unblocked += 1
            except FileNotFoundError:
                continue
            except OSError:
                continue
    return unblocked


def unblock_frozen_app_dir() -> int:
    """Unblock PyInstaller bundle binaries before pythonnet/WebView2 initializes."""
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return 0
    exe_dir = Path(sys.executable).resolve().parent
    total = unblock_downloaded_binaries(exe_dir)
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        mei_dir = Path(meipass).resolve()
        if mei_dir != exe_dir and exe_dir not in mei_dir.parents:
            total += unblock_downloaded_binaries(mei_dir)
    return total
