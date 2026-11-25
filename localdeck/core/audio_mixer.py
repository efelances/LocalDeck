"""Thread-safe Windows WASAPI master & per-app volume mixer over pure ctypes COM."""

from __future__ import annotations

import ctypes
import sys
import threading
from abc import ABC, abstractmethod
from ctypes import wintypes
from pathlib import PureWindowsPath
from typing import Any

from localdeck.core.audio_meter import (
    _CLSCTX_ALL,
    _CLSID_ENUMERATOR,
    _EMULTIMEDIA,
    _ERENDER,
    _GUID,
    _IID_ENUMERATOR,
    _IID_SESSION_CTRL2,
    _IID_SESSION_MGR2,
    _clamp01,
    _exe_for_pid,
    _release_ptr,
    _vcall,
)

_IID_ENDPOINT_VOLUME = _GUID.from_str("{5CDF2C82-841E-4546-9722-0CF74078229A}")
_IID_SIMPLE_AUDIO_VOLUME = _GUID.from_str("{87CE5498-68D6-44E5-9215-6DA47EF883D8}")


def _is_master_target(exe: str | None) -> bool:
    if exe is None:
        return True
    cleaned = exe.strip().lower()
    return not cleaned or cleaned == "__master__"


def _normalize_exe_target(exe: str | None) -> str:
    if _is_master_target(exe):
        return "__master__"
    return PureWindowsPath(str(exe).strip()).name.lower()


class AudioMixerBackend(ABC):
    """Abstract master & per-application audio volume/mute controller."""

    @abstractmethod
    def get_mixer_state(self) -> dict[str, Any]:
        """Return `{"master": {"volume": float, "muted": bool}, "apps": [...]}`."""

    @abstractmethod
    def set_volume(self, exe: str | None, volume: float) -> dict[str, Any]:
        """Set volume (`0.0..1.0`) for master or a target `.exe` and return updated state."""

    @abstractmethod
    def set_mute(self, exe: str | None, muted: bool) -> dict[str, Any]:
        """Set mute state for master or a target `.exe` and return updated state."""

    @abstractmethod
    def is_master_muted(self) -> bool:
        """Return whether the default playback endpoint is currently muted."""


class WindowsAudioMixerBackend(AudioMixerBackend):
    """Pure-ctypes WASAPI endpoint & session volume mixer (COM init/uninit per call)."""

    @staticmethod
    def _open_endpoint_device(ole32):
        enum_ptr = ctypes.c_void_p()
        dev_ptr = ctypes.c_void_p()
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
            _release_ptr(enum_ptr)
            return None, None
        get_default = _vcall(
            enum_ptr, 4, ctypes.c_long, wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p)
        )
        if get_default(enum_ptr, _ERENDER, _EMULTIMEDIA, ctypes.byref(dev_ptr)) != 0 or not dev_ptr.value:
            _release_ptr(dev_ptr)
            _release_ptr(enum_ptr)
            return None, None
        return enum_ptr, dev_ptr

    @staticmethod
    def _activate_interface(dev_ptr: ctypes.c_void_p, iid: _GUID) -> ctypes.c_void_p | None:
        out_ptr = ctypes.c_void_p()
        activate = _vcall(
            dev_ptr,
            3,
            ctypes.c_long,
            ctypes.POINTER(_GUID),
            wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        )
        if activate(dev_ptr, ctypes.byref(iid), _CLSCTX_ALL, None, ctypes.byref(out_ptr)) != 0 or not out_ptr.value:
            _release_ptr(out_ptr)
            return None
        return out_ptr

    def _run_with_com(self, fn):
        if sys.platform != "win32":
            return fn(None, None)
        ole32 = ctypes.OleDLL("ole32")
        need_uninit = False
        try:
            hr_init = ole32.CoInitializeEx(None, 0x2)
            need_uninit = hr_init in (0, 1)
        except OSError:
            pass

        enum_ptr = None
        dev_ptr = None
        try:
            enum_ptr, dev_ptr = self._open_endpoint_device(ole32)
            return fn(ole32, dev_ptr)
        except OSError:
            return None
        finally:
            _release_ptr(dev_ptr)
            _release_ptr(enum_ptr)
            if need_uninit:
                try:
                    ole32.CoUninitialize()
                except Exception:
                    pass

    @staticmethod
    def _read_master_on_dev(dev_ptr: ctypes.c_void_p | None) -> dict[str, Any]:
        if not dev_ptr:
            return {"volume": 1.0, "muted": False}
        ep = WindowsAudioMixerBackend._activate_interface(dev_ptr, _IID_ENDPOINT_VOLUME)
        if not ep:
            return {"volume": 1.0, "muted": False}
        try:
            vol = ctypes.c_float(1.0)
            muted = wintypes.BOOL(False)
            _vcall(ep, 9, ctypes.c_long, ctypes.POINTER(ctypes.c_float))(ep, ctypes.byref(vol))
            _vcall(ep, 15, ctypes.c_long, ctypes.POINTER(wintypes.BOOL))(ep, ctypes.byref(muted))
            return {"volume": round(_clamp01(vol.value), 3), "muted": bool(muted.value)}
        finally:
            _release_ptr(ep)

    @staticmethod
    def _iter_sessions(dev_ptr: ctypes.c_void_p | None):
        if not dev_ptr:
            return
        mgr = WindowsAudioMixerBackend._activate_interface(dev_ptr, _IID_SESSION_MGR2)
        if not mgr:
            return
        se_ptr = ctypes.c_void_p()
        try:
            if _vcall(mgr, 5, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p))(mgr, ctypes.byref(se_ptr)) != 0 or not se_ptr.value:
                return
            cnt = ctypes.c_int(0)
            _vcall(se_ptr, 3, ctypes.c_long, ctypes.POINTER(ctypes.c_int))(se_ptr, ctypes.byref(cnt))
            get_session = _vcall(se_ptr, 4, ctypes.c_long, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))
            for idx in range(cnt.value):
                sc = ctypes.c_void_p()
                sc2 = ctypes.c_void_p()
                sav = ctypes.c_void_p()
                try:
                    if get_session(se_ptr, idx, ctypes.byref(sc)) != 0 or not sc.value:
                        continue
                    qi = _vcall(sc, 0, ctypes.c_long, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p))
                    if qi(sc, ctypes.byref(_IID_SESSION_CTRL2), ctypes.byref(sc2)) != 0 or not sc2.value:
                        continue
                    pid = wintypes.DWORD(0)
                    if _vcall(sc2, 14, ctypes.c_long, ctypes.POINTER(wintypes.DWORD))(sc2, ctypes.byref(pid)) != 0:
                        continue
                    exe = _exe_for_pid(int(pid.value))
                    if not exe or exe == "system":
                        continue
                    if qi(sc, ctypes.byref(_IID_SIMPLE_AUDIO_VOLUME), ctypes.byref(sav)) != 0 or not sav.value:
                        continue
                    yield exe, sav
                finally:
                    _release_ptr(sav)
                    _release_ptr(sc2)
                    _release_ptr(sc)
        finally:
            _release_ptr(se_ptr)
            _release_ptr(mgr)

    def _collect_state_on_dev(self, dev_ptr: ctypes.c_void_p | None) -> dict[str, Any]:
        master = self._read_master_on_dev(dev_ptr)
        grouped: dict[str, dict[str, Any]] = {}
        for exe, sav in self._iter_sessions(dev_ptr):
            vol = ctypes.c_float(1.0)
            muted = wintypes.BOOL(False)
            if _vcall(sav, 4, ctypes.c_long, ctypes.POINTER(ctypes.c_float))(sav, ctypes.byref(vol)) != 0:
                continue
            _vcall(sav, 6, ctypes.c_long, ctypes.POINTER(wintypes.BOOL))(sav, ctypes.byref(muted))
            v = round(_clamp01(vol.value), 3)
            m = bool(muted.value)
            if exe not in grouped:
                grouped[exe] = {"exe": exe, "volume": v, "muted": m}
            else:
                grouped[exe]["volume"] = max(grouped[exe]["volume"], v)
                grouped[exe]["muted"] = grouped[exe]["muted"] and m
        apps = sorted(grouped.values(), key=lambda item: item["exe"])
        return {"master": master, "apps": apps}

    def get_mixer_state(self) -> dict[str, Any]:
        res = self._run_with_com(lambda _ole, dev: self._collect_state_on_dev(dev))
        return res or {"master": {"volume": 1.0, "muted": False}, "apps": []}

    def set_volume(self, exe: str | None, volume: float) -> dict[str, Any]:
        level = _clamp01(volume)
        target = _normalize_exe_target(exe)

        def _apply(_ole, dev):
            if dev:
                if target == "__master__":
                    ep = self._activate_interface(dev, _IID_ENDPOINT_VOLUME)
                    if ep:
                        try:
                            _vcall(ep, 7, ctypes.c_long, ctypes.c_float, ctypes.c_void_p)(ep, ctypes.c_float(level), None)
                        finally:
                            _release_ptr(ep)
                else:
                    for sess_exe, sav in self._iter_sessions(dev):
                        if sess_exe == target:
                            _vcall(sav, 3, ctypes.c_long, ctypes.c_float, ctypes.c_void_p)(
                                sav, ctypes.c_float(level), None
                            )
            return self._collect_state_on_dev(dev)

        return self._run_with_com(_apply) or {"master": {"volume": 1.0, "muted": False}, "apps": []}

    def set_mute(self, exe: str | None, muted: bool) -> dict[str, Any]:
        flag = wintypes.BOOL(bool(muted))
        target = _normalize_exe_target(exe)

        def _apply(_ole, dev):
            if dev:
                if target == "__master__":
                    ep = self._activate_interface(dev, _IID_ENDPOINT_VOLUME)
                    if ep:
                        try:
                            _vcall(ep, 14, ctypes.c_long, wintypes.BOOL, ctypes.c_void_p)(ep, flag, None)
                        finally:
                            _release_ptr(ep)
                else:
                    for sess_exe, sav in self._iter_sessions(dev):
                        if sess_exe == target:
                            _vcall(sav, 5, ctypes.c_long, wintypes.BOOL, ctypes.c_void_p)(sav, flag, None)
            return self._collect_state_on_dev(dev)

        return self._run_with_com(_apply) or {"master": {"volume": 1.0, "muted": False}, "apps": []}

    def is_master_muted(self) -> bool:
        res = self._run_with_com(lambda _ole, dev: self._read_master_on_dev(dev)["muted"])
        return bool(res)


class NoopAudioMixerBackend(AudioMixerBackend):
    """In-memory audio mixer mock for unit/integration tests and non-Windows platforms."""

    def __init__(
        self,
        master_volume: float = 0.8,
        master_muted: bool = False,
        apps: dict[str, tuple[float, bool]] | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self.master = {"volume": round(_clamp01(master_volume), 3), "muted": bool(master_muted)}
        initial_apps = apps if apps is not None else {"spotify.exe": (0.7, False), "discord.exe": (0.9, False)}
        self.apps: dict[str, dict[str, Any]] = {
            k.lower(): {"volume": round(_clamp01(v), 3), "muted": bool(m)}
            for k, (v, m) in initial_apps.items()
        }

    def get_mixer_state(self) -> dict[str, Any]:
        with self._lock:
            return {
                "master": dict(self.master),
                "apps": [
                    {"exe": exe, "volume": info["volume"], "muted": info["muted"]}
                    for exe, info in sorted(self.apps.items())
                ],
            }

    def set_volume(self, exe: str | None, volume: float) -> dict[str, Any]:
        level = round(_clamp01(volume), 3)
        target = _normalize_exe_target(exe)
        with self._lock:
            if target == "__master__":
                self.master["volume"] = level
            elif target in self.apps:
                self.apps[target]["volume"] = level
        return self.get_mixer_state()

    def set_mute(self, exe: str | None, muted: bool) -> dict[str, Any]:
        target = _normalize_exe_target(exe)
        with self._lock:
            if target == "__master__":
                self.master["muted"] = bool(muted)
            elif target in self.apps:
                self.apps[target]["muted"] = bool(muted)
        return self.get_mixer_state()

    def is_master_muted(self) -> bool:
        with self._lock:
            return bool(self.master["muted"])


def create_default_audio_mixer_backend() -> AudioMixerBackend:
    """Create platform-appropriate audio mixer backend."""
    if sys.platform == "win32":
        return WindowsAudioMixerBackend()
    return NoopAudioMixerBackend()
