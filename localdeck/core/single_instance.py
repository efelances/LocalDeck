"""Single-instance OS mutex guard and existing-window focus notifier."""

from __future__ import annotations

import ctypes
import json
import os
import sys
import urllib.error
import urllib.request
from ctypes import wintypes
from pathlib import Path

_ERROR_ALREADY_EXISTS = 183


class SingleInstanceGuard:
    """Ensures only one desktop instance of LocalDeck runs at a time."""

    def __init__(self, mutex_name: str = "Local\\LocalDeckSingleInstanceMutex") -> None:
        self._mutex_name = mutex_name
        self._handle: wintypes.HANDLE | None = None
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True) if sys.platform == "win32" else None

    def acquire(self) -> bool:
        if sys.platform != "win32" or self._kernel32 is None:
            return True
        if self._handle:
            return True
        handle = self._kernel32.CreateMutexW(None, False, self._mutex_name)
        err = ctypes.get_last_error()
        if not handle:
            return False
        if err == _ERROR_ALREADY_EXISTS:
            self._kernel32.CloseHandle(handle)
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        if sys.platform == "win32" and self._kernel32 is not None and self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


def _runtime_path(data_dir: Path) -> Path:
    return Path(data_dir) / "runtime.json"


def write_runtime_info(data_dir: Path, port: int, desktop_token: str) -> Path:
    """Atomically persist the running instance's port and loopback token."""
    target = _runtime_path(data_dir)
    tmp = target.with_suffix(".json.tmp")
    payload = json.dumps({"pid": os.getpid(), "port": int(port), "token": desktop_token})
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, target)
    return target


def clear_runtime_info(data_dir: Path) -> None:
    """Remove runtime.json if it belongs to the current process."""
    target = _runtime_path(data_dir)
    if not target.exists():
        return
    try:
        info = json.loads(target.read_text(encoding="utf-8"))
        if int(info.get("pid", -1)) == os.getpid():
            target.unlink(missing_ok=True)
    except (OSError, ValueError, TypeError):
        pass


def notify_existing_instance(data_dir: Path, timeout: float = 1.5) -> bool:
    """Ask the currently running LocalDeck instance to show and focus its window."""
    target = _runtime_path(data_dir)
    if not target.is_file():
        return False
    try:
        info = json.loads(target.read_text(encoding="utf-8"))
        port = int(info["port"])
        token = str(info["token"])
    except (OSError, KeyError, ValueError, TypeError):
        return False

    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/desktop/focus",
        data=b"{}",
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-LocalDeck-Desktop-Token": token,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError):
        return False
