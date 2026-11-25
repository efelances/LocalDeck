"""Foreground Windows application watcher for automatic deck page switching."""

from __future__ import annotations

import ctypes
import os
import sys
import threading
from ctypes import wintypes
from pathlib import PureWindowsPath
from typing import Callable

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def get_foreground_window_info() -> tuple[str, str, int] | None:
    """Return `(exe_basename_lower, window_title, pid)` for the active foreground window."""
    if sys.platform != "win32":
        return None
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None

        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return None

        title_len = user32.GetWindowTextLengthW(hwnd)
        title_buf = ctypes.create_unicode_buffer(max(title_len + 1, 1))
        user32.GetWindowTextW(hwnd, title_buf, len(title_buf))
        title = title_buf.value.strip()

        exe_name = ""
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if handle:
            try:
                size = wintypes.DWORD(512)
                path_buf = ctypes.create_unicode_buffer(size.value)
                if kernel32.QueryFullProcessImageNameW(handle, 0, path_buf, ctypes.byref(size)):
                    exe_name = PureWindowsPath(path_buf.value).name.lower()
            finally:
                kernel32.CloseHandle(handle)

        return (exe_name, title, int(pid.value))
    except OSError:
        return None


class WindowWatcher:
    """Daemon polling thread notifying on foreground executable/title changes."""

    def __init__(
        self,
        on_foreground_change: Callable[[str, str], None],
        poll_interval: float = 0.35,
    ) -> None:
        self._on_change = on_foreground_change
        self._poll_interval = poll_interval
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._own_pid = os.getpid()
        self._last_seen: tuple[str, str] | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="LocalDeckWindowWatcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.5)
        self._thread = None

    def _run(self) -> None:
        while not self._stop_event.is_set():
            info = get_foreground_window_info()
            if info is not None:
                exe_name, title, pid = info
                if pid != self._own_pid and exe_name:
                    current = (exe_name, title)
                    if current != self._last_seen:
                        self._last_seen = current
                        try:
                            self._on_change(exe_name, title)
                        except Exception:
                            pass
            self._stop_event.wait(self._poll_interval)
