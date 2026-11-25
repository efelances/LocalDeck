"""Lightweight Windows WASAPI stereo & per-app peak meter over pure ctypes COM."""

from __future__ import annotations

import ctypes
import sys
import threading
import uuid
from abc import ABC, abstractmethod
from ctypes import wintypes
from pathlib import PureWindowsPath
from typing import Callable

_CLSCTX_ALL = 23
_ERENDER = 0
_EMULTIMEDIA = 1
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class AudioMeterBackend(ABC):
    """Abstract stereo output level meter returning `(peak, left, right)` in `[0.0, 1.0]`."""

    @abstractmethod
    def read_levels(self, target_exe: str | None = None) -> tuple[float, float, float]:
        """Read current output peak and stereo channel peaks (optionally filtered to one .exe)."""

    @abstractmethod
    def list_audio_apps(self) -> list[str]:
        """Return sorted executable names currently registered in Windows audio mixer."""

    @abstractmethod
    def close(self) -> None:
        """Release any underlying audio or COM handles."""


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def from_str(cls, raw: str) -> _GUID:
        u = uuid.UUID(raw)
        return cls(u.time_low, u.time_mid, u.time_hi_version, (ctypes.c_ubyte * 8)(*u.bytes[8:]))


_CLSID_ENUMERATOR = _GUID.from_str("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
_IID_ENUMERATOR = _GUID.from_str("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
_IID_SESSION_MGR2 = _GUID.from_str("{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}")
_IID_SESSION_CTRL2 = _GUID.from_str("{BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D}")
_IID_METER = _GUID.from_str("{C02216F6-8C67-4B5B-9D00-D008E73E0064}")


def _clamp01(value: float) -> float:
    if value <= 0.0:
        return 0.0
    if value >= 1.0:
        return 1.0
    return float(value)


def _vcall(ptr: ctypes.c_void_p, index: int, restype, *argtypes):
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtbl[index])


def _release_ptr(ptr: ctypes.c_void_p | None) -> None:
    if ptr and ptr.value:
        try:
            _vcall(ptr, 2, wintypes.ULONG)(ptr)
        except Exception:
            pass


def _exe_for_pid(pid: int, cache: dict[int, str] | None = None) -> str:
    if not pid:
        return "system"
    if cache is not None and pid in cache:
        return cache[pid]
    exe = ""
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if handle:
        try:
            size = wintypes.DWORD(512)
            buf = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                exe = PureWindowsPath(buf.value).name.lower()
        finally:
            kernel32.CloseHandle(handle)
    if cache is not None and exe:
        if len(cache) > 128:
            cache.clear()
        cache[pid] = exe
    return exe


def _read_meter_interface(meter_ptr: ctypes.c_void_p) -> tuple[float, float, float] | None:
    peak_val = ctypes.c_float()
    get_peak = _vcall(meter_ptr, 3, ctypes.c_long, ctypes.POINTER(ctypes.c_float))
    if get_peak(meter_ptr, ctypes.byref(peak_val)) != 0:
        return None
    peak = _clamp01(peak_val.value)
    ch_count = wintypes.UINT(0)
    get_ch_count = _vcall(meter_ptr, 4, ctypes.c_long, ctypes.POINTER(wintypes.UINT))
    if get_ch_count(meter_ptr, ctypes.byref(ch_count)) == 0 and ch_count.value >= 2:
        arr = (ctypes.c_float * ch_count.value)()
        get_ch_peaks = _vcall(
            meter_ptr, 5, ctypes.c_long, wintypes.UINT, ctypes.POINTER(ctypes.c_float)
        )
        if get_ch_peaks(meter_ptr, ch_count.value, arr) == 0:
            return (peak, _clamp01(arr[0]), _clamp01(arr[1]))
    return (peak, peak, peak)


def list_windows_audio_apps() -> list[str]:
    """Enumerate unique `.exe` names currently holding Windows audio sessions."""
    if sys.platform != "win32":
        return []
    ole32 = ctypes.OleDLL("ole32")
    need_uninit = False
    try:
        hr_init = ole32.CoInitializeEx(None, 0x2)
        need_uninit = hr_init in (0, 1)
    except OSError:
        pass

    enum_ptr = ctypes.c_void_p()
    dev_ptr = ctypes.c_void_p()
    mgr_ptr = ctypes.c_void_p()
    se_ptr = ctypes.c_void_p()
    apps: set[str] = set()
    try:
        if (
            ole32.CoCreateInstance(
                ctypes.byref(_CLSID_ENUMERATOR),
                None,
                _CLSCTX_ALL,
                ctypes.byref(_IID_ENUMERATOR),
                ctypes.byref(enum_ptr),
            )
            != 0
            or not enum_ptr.value
        ):
            return []
        get_default = _vcall(
            enum_ptr, 4, ctypes.c_long, wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p)
        )
        if get_default(enum_ptr, _ERENDER, _EMULTIMEDIA, ctypes.byref(dev_ptr)) != 0 or not dev_ptr.value:
            return []
        activate = _vcall(
            dev_ptr,
            3,
            ctypes.c_long,
            ctypes.POINTER(_GUID),
            wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        )
        if (
            activate(dev_ptr, ctypes.byref(_IID_SESSION_MGR2), _CLSCTX_ALL, None, ctypes.byref(mgr_ptr)) != 0
            or not mgr_ptr.value
        ):
            return []
        get_enum = _vcall(mgr_ptr, 5, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p))
        if get_enum(mgr_ptr, ctypes.byref(se_ptr)) != 0 or not se_ptr.value:
            return []
        cnt = ctypes.c_int(0)
        _vcall(se_ptr, 3, ctypes.c_long, ctypes.POINTER(ctypes.c_int))(se_ptr, ctypes.byref(cnt))
        get_session = _vcall(se_ptr, 4, ctypes.c_long, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))
        for idx in range(cnt.value):
            sc = ctypes.c_void_p()
            sc2 = ctypes.c_void_p()
            try:
                if get_session(se_ptr, idx, ctypes.byref(sc)) != 0 or not sc.value:
                    continue
                qi = _vcall(sc, 0, ctypes.c_long, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p))
                if qi(sc, ctypes.byref(_IID_SESSION_CTRL2), ctypes.byref(sc2)) != 0 or not sc2.value:
                    continue
                pid = wintypes.DWORD(0)
                if _vcall(sc2, 14, ctypes.c_long, ctypes.POINTER(wintypes.DWORD))(sc2, ctypes.byref(pid)) == 0:
                    exe = _exe_for_pid(int(pid.value))
                    if exe and exe != "system":
                        apps.add(exe)
            finally:
                _release_ptr(sc2)
                _release_ptr(sc)
    except OSError:
        return []
    finally:
        _release_ptr(se_ptr)
        _release_ptr(mgr_ptr)
        _release_ptr(dev_ptr)
        _release_ptr(enum_ptr)
        if need_uninit:
            try:
                ole32.CoUninitialize()
            except Exception:
                pass
    return sorted(apps)


class WindowsAudioMeterBackend(AudioMeterBackend):
    """Pure-ctypes WASAPI IAudioMeterInformation reader for default Windows render device."""

    def __init__(self) -> None:
        self._ole32 = ctypes.OleDLL("ole32")
        self._com_initialized = False
        self._enumerator: ctypes.c_void_p | None = None
        self._device: ctypes.c_void_p | None = None
        self._meter: ctypes.c_void_p | None = None
        self._session_mgr: ctypes.c_void_p | None = None
        self._pid_exe_cache: dict[int, str] = {}

    def _release_interfaces(self) -> None:
        _release_ptr(self._session_mgr)
        _release_ptr(self._meter)
        _release_ptr(self._device)
        _release_ptr(self._enumerator)
        self._session_mgr = None
        self._meter = None
        self._device = None
        self._enumerator = None
        self._pid_exe_cache.clear()

    def _ensure_ready(self) -> bool:
        if self._meter and self._meter.value and self._device and self._device.value:
            return True
        if not self._com_initialized:
            try:
                hr_init = self._ole32.CoInitializeEx(None, 0x2)
                self._com_initialized = hr_init in (0, 1)
            except OSError:
                self._com_initialized = False

        enum_ptr = ctypes.c_void_p()
        hr = self._ole32.CoCreateInstance(
            ctypes.byref(_CLSID_ENUMERATOR),
            None,
            _CLSCTX_ALL,
            ctypes.byref(_IID_ENUMERATOR),
            ctypes.byref(enum_ptr),
        )
        if hr != 0 or not enum_ptr.value:
            return False
        self._enumerator = enum_ptr

        dev_ptr = ctypes.c_void_p()
        get_default = _vcall(
            enum_ptr, 4, ctypes.c_long, wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p)
        )
        if get_default(enum_ptr, _ERENDER, _EMULTIMEDIA, ctypes.byref(dev_ptr)) != 0 or not dev_ptr.value:
            self._release_interfaces()
            return False
        self._device = dev_ptr

        activate = _vcall(
            dev_ptr,
            3,
            ctypes.c_long,
            ctypes.POINTER(_GUID),
            wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        )
        meter_ptr = ctypes.c_void_p()
        if (
            activate(dev_ptr, ctypes.byref(_IID_METER), _CLSCTX_ALL, None, ctypes.byref(meter_ptr)) != 0
            or not meter_ptr.value
        ):
            self._release_interfaces()
            return False
        self._meter = meter_ptr
        return True

    def _ensure_session_manager(self) -> ctypes.c_void_p | None:
        if not self._ensure_ready() or self._device is None:
            return None
        if self._session_mgr and self._session_mgr.value:
            return self._session_mgr
        mgr_ptr = ctypes.c_void_p()
        activate = _vcall(
            self._device,
            3,
            ctypes.c_long,
            ctypes.POINTER(_GUID),
            wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        )
        if (
            activate(self._device, ctypes.byref(_IID_SESSION_MGR2), _CLSCTX_ALL, None, ctypes.byref(mgr_ptr)) != 0
            or not mgr_ptr.value
        ):
            return None
        self._session_mgr = mgr_ptr
        return mgr_ptr

    def _read_session_levels(self, target_exe: str) -> tuple[float, float, float]:
        mgr = self._ensure_session_manager()
        if mgr is None:
            return (0.0, 0.0, 0.0)
        se_ptr = ctypes.c_void_p()
        max_p = 0.0
        max_l = 0.0
        max_r = 0.0
        try:
            get_enum = _vcall(mgr, 5, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p))
            if get_enum(mgr, ctypes.byref(se_ptr)) != 0 or not se_ptr.value:
                self._release_interfaces()
                return (0.0, 0.0, 0.0)
            cnt = ctypes.c_int(0)
            _vcall(se_ptr, 3, ctypes.c_long, ctypes.POINTER(ctypes.c_int))(se_ptr, ctypes.byref(cnt))
            get_session = _vcall(se_ptr, 4, ctypes.c_long, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))
            for idx in range(cnt.value):
                sc = ctypes.c_void_p()
                sc2 = ctypes.c_void_p()
                meter = ctypes.c_void_p()
                try:
                    if get_session(se_ptr, idx, ctypes.byref(sc)) != 0 or not sc.value:
                        continue
                    qi = _vcall(sc, 0, ctypes.c_long, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p))
                    if qi(sc, ctypes.byref(_IID_SESSION_CTRL2), ctypes.byref(sc2)) != 0 or not sc2.value:
                        continue
                    pid = wintypes.DWORD(0)
                    if _vcall(sc2, 14, ctypes.c_long, ctypes.POINTER(wintypes.DWORD))(sc2, ctypes.byref(pid)) != 0:
                        continue
                    if _exe_for_pid(int(pid.value), cache=self._pid_exe_cache) != target_exe:
                        continue
                    if qi(sc, ctypes.byref(_IID_METER), ctypes.byref(meter)) != 0 or not meter.value:
                        continue
                    levels = _read_meter_interface(meter)
                    if levels is not None:
                        p, l, r = levels
                        max_p = max(max_p, p)
                        max_l = max(max_l, l)
                        max_r = max(max_r, r)
                finally:
                    _release_ptr(meter)
                    _release_ptr(sc2)
                    _release_ptr(sc)
        finally:
            _release_ptr(se_ptr)
        return (max_p, max_l, max_r)

    def read_levels(self, target_exe: str | None = None) -> tuple[float, float, float]:
        try:
            cleaned = PureWindowsPath(target_exe.strip()).name.lower() if target_exe and target_exe.strip() else ""
            if cleaned:
                return self._read_session_levels(cleaned)
            if not self._ensure_ready() or self._meter is None:
                return (0.0, 0.0, 0.0)
            levels = _read_meter_interface(self._meter)
            if levels is None:
                self._release_interfaces()
                return (0.0, 0.0, 0.0)
            return levels
        except OSError:
            self._release_interfaces()
            return (0.0, 0.0, 0.0)

    def list_audio_apps(self) -> list[str]:
        return list_windows_audio_apps()

    def close(self) -> None:
        self._release_interfaces()
        if self._com_initialized:
            try:
                self._ole32.CoUninitialize()
            except Exception:
                pass
            self._com_initialized = False


class NoopAudioMeterBackend(AudioMeterBackend):
    """Configurable test double and non-Windows fallback audio meter."""

    def __init__(
        self,
        levels: tuple[float, float, float] = (0.0, 0.0, 0.0),
        per_app_levels: dict[str, tuple[float, float, float]] | None = None,
    ) -> None:
        self.levels = levels
        self.per_app_levels: dict[str, tuple[float, float, float]] = {
            k.lower(): v for k, v in (per_app_levels or {}).items()
        }
        self.closed = False

    def read_levels(self, target_exe: str | None = None) -> tuple[float, float, float]:
        if target_exe and target_exe.strip():
            p, l, r = self.per_app_levels.get(target_exe.strip().lower(), (0.0, 0.0, 0.0))
        else:
            p, l, r = self.levels
        return (_clamp01(p), _clamp01(l), _clamp01(r))

    def list_audio_apps(self) -> list[str]:
        return sorted(self.per_app_levels.keys())

    def close(self) -> None:
        self.closed = True


def create_default_audio_meter_backend() -> AudioMeterBackend:
    """Create platform-appropriate audio meter backend."""
    if sys.platform == "win32":
        return WindowsAudioMeterBackend()
    return NoopAudioMeterBackend()


class AudioMeterWatcher:
    """Background daemon polling audio levels only while an `audio` skin is active and viewed."""

    def __init__(
        self,
        backend: AudioMeterBackend,
        should_poll: Callable[[], bool],
        on_level: Callable[[float, float, float], None],
        get_target_exe: Callable[[], str | None] | None = None,
        poll_interval: float = 0.055,
    ) -> None:
        self._backend = backend
        self._should_poll = should_poll
        self._on_level = on_level
        self._get_target_exe = get_target_exe
        self._poll_interval = poll_interval
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_sent: tuple[float, float, float] | None = None
        self._last_target: str | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="LocalDeckAudioMeter", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.5)
        self._thread = None

    def _run(self) -> None:
        try:
            while not self._stop_event.is_set():
                if not self._should_poll():
                    self._last_sent = None
                    self._last_target = None
                    self._stop_event.wait(0.25)
                    continue

                target = self._get_target_exe() if self._get_target_exe else None
                if target != self._last_target:
                    self._last_target = target
                    self._last_sent = None

                peak, left, right = self._backend.read_levels(target_exe=target)
                if peak < 0.0006 and left < 0.0006 and right < 0.0006:
                    current = (0.0, 0.0, 0.0)
                else:
                    current = (peak, left, right)

                if self._last_sent is None or any(
                    abs(a - b) > 0.0012 for a, b in zip(current, self._last_sent)
                ):
                    self._last_sent = current
                    try:
                        self._on_level(*current)
                    except Exception:
                        pass

                self._stop_event.wait(self._poll_interval)
        finally:
            self._backend.close()
