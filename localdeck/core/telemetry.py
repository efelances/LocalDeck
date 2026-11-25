"""System resource metrics and ETS 2 / ATS live telemetry readers."""

from __future__ import annotations

import ctypes
import json
import math
import struct
import sys
import time
import urllib.error
import urllib.request
from ctypes import wintypes
from typing import Any

_FILE_MAP_READ = 0x0004
_SCS_SHM_NAME = "Local\\SCSTelemetry"
_SCS_READ_BYTES = 2048
_FUNBIT_URL = "http://127.0.0.1:25555/api/ets2/telemetry"


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    def to_int(self) -> int:
        return (int(self.dwHighDateTime) << 32) | int(self.dwLowDateTime)


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_uint64),
        ("ullAvailPhys", ctypes.c_uint64),
        ("ullTotalPageFile", ctypes.c_uint64),
        ("ullAvailPageFile", ctypes.c_uint64),
        ("ullTotalVirtual", ctypes.c_uint64),
        ("ullAvailVirtual", ctypes.c_uint64),
        ("ullAvailExtendedVirtual", ctypes.c_uint64),
    ]


class SystemStatsReader:
    """Zero-dependency Windows CPU% & RAM% reader (delta GetSystemTimes + GlobalMemoryStatusEx)."""

    def __init__(self) -> None:
        self._prev_times: tuple[int, int, int] | None = None

    def read(self) -> dict[str, int]:
        if sys.platform != "win32":
            return {"cpu_pct": 0, "ram_pct": 0}
        kernel32 = ctypes.windll.kernel32
        ram_pct = 0
        mem = _MEMORYSTATUSEX()
        mem.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        if kernel32.GlobalMemoryStatusEx(ctypes.byref(mem)):
            ram_pct = max(0, min(100, int(mem.dwMemoryLoad)))

        cpu_pct = 0
        idle, kernel, user = _FILETIME(), _FILETIME(), _FILETIME()
        if kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            curr = (idle.to_int(), kernel.to_int(), user.to_int())
            if self._prev_times is not None:
                d_idle = curr[0] - self._prev_times[0]
                d_kernel = curr[1] - self._prev_times[1]
                d_user = curr[2] - self._prev_times[2]
                total = d_kernel + d_user
                if total > 0:
                    busy = max(0, total - d_idle)
                    cpu_pct = max(0, min(100, round((busy / total) * 100)))
            self._prev_times = curr

        return {"cpu_pct": cpu_pct, "ram_pct": ram_pct}


def disconnected_ets2_telemetry() -> dict[str, Any]:
    return {
        "connected": False,
        "speed_kmh": 0,
        "gear": "N",
        "rpm": 0,
        "fuel_pct": 0,
        "cruise_kmh": 0,
        "park_brake": False,
    }


def _format_gear(gear: int) -> str:
    if gear == 0:
        return "N"
    if gear > 0:
        return f"D{gear}"
    return f"R{abs(gear)}"


def parse_funbit_telemetry_json(data: dict[str, Any]) -> dict[str, Any]:
    """Parse Funbit ETS2 Telemetry HTTP JSON payload into normalized cockpit telemetry."""
    if not isinstance(data, dict) or not data.get("game", {}).get("connected"):
        return disconnected_ets2_telemetry()
    truck = data.get("truck") or {}
    speed_raw = float(truck.get("speed") or 0.0)
    # Funbit already reports speed in km/h (positive/negative for reverse)
    speed_kmh = max(0, min(350, round(abs(speed_raw))))
    gear_raw = int(truck.get("displayedGear", truck.get("gear", 0)) or 0)
    rpm = max(0, min(6500, round(float(truck.get("engineRpm") or 0.0))))
    fuel = float(truck.get("fuel") or 0.0)
    fuel_cap = float(truck.get("fuelCapacity") or 0.0)
    fuel_pct = max(0, min(100, round((fuel / fuel_cap) * 100))) if fuel_cap > 1.0 else 0
    cruise_kmh = max(0, min(250, round(float(truck.get("cruiseControlSpeed") or 0.0))))
    park_brake = bool(truck.get("parkBrakeOn", False))
    return {
        "connected": True,
        "speed_kmh": speed_kmh,
        "gear": _format_gear(gear_raw),
        "rpm": rpm,
        "fuel_pct": fuel_pct,
        "cruise_kmh": cruise_kmh,
        "park_brake": park_brake,
    }


def parse_scs_shared_memory(raw: bytes) -> dict[str, Any]:
    """Parse a RenCloud `Local\\SCSTelemetry` binary buffer with sanity validation."""
    if len(raw) < 1600 or raw[0] == 0:
        return disconnected_ets2_telemetry()
    try:
        gear = struct.unpack_from("<i", raw, 508)[0]
        if not (-12 <= gear <= 24):
            alt_gear = struct.unpack_from("<i", raw, 504)[0]
            gear = alt_gear if -12 <= alt_gear <= 24 else 0

        fuel_cap = struct.unpack_from("<f", raw, 704)[0]
        speed_ms = struct.unpack_from("<f", raw, 948)[0]
        rpm = struct.unpack_from("<f", raw, 952)[0]
        cruise_ms = struct.unpack_from("<f", raw, 980)[0]
        fuel = struct.unpack_from("<f", raw, 1004)[0]
    except struct.error:
        return disconnected_ets2_telemetry()

    if not all(math.isfinite(v) for v in (fuel_cap, speed_ms, rpm, cruise_ms, fuel)):
        return disconnected_ets2_telemetry()
    if abs(speed_ms) > 95.0 or not (0.0 <= rpm <= 6500.0) or not (0.0 <= fuel <= 5000.0):
        return disconnected_ets2_telemetry()

    fuel_pct = max(0, min(100, round((fuel / fuel_cap) * 100))) if fuel_cap > 1.0 else 0
    return {
        "connected": True,
        "speed_kmh": max(0, min(350, round(abs(speed_ms) * 3.6))),
        "gear": _format_gear(gear),
        "rpm": round(rpm),
        "fuel_pct": fuel_pct,
        "cruise_kmh": max(0, min(250, round(max(0.0, cruise_ms) * 3.6))),
        "park_brake": raw[1568] != 0,
    }


class Ets2TelemetryReader:
    """Reads ETS 2 / ATS telemetry from `Local\\SCSTelemetry` SHM or Funbit HTTP fallback."""

    def __init__(
        self,
        shm_name: str = _SCS_SHM_NAME,
        funbit_url: str = _FUNBIT_URL,
        http_backoff_seconds: float = 4.0,
    ) -> None:
        self._shm_name = shm_name
        self._funbit_url = funbit_url
        self._http_backoff = http_backoff_seconds
        self._next_http_try = 0.0

    def _read_shared_memory_windows(self) -> dict[str, Any] | None:
        if sys.platform != "win32":
            return None
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenFileMappingW.restype = wintypes.HANDLE
        kernel32.OpenFileMappingW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.MapViewOfFile.restype = ctypes.c_void_p
        kernel32.MapViewOfFile.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_size_t,
        ]
        kernel32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        handle = kernel32.OpenFileMappingW(_FILE_MAP_READ, False, self._shm_name)
        if not handle:
            return None
        view = None
        try:
            view = kernel32.MapViewOfFile(handle, _FILE_MAP_READ, 0, 0, _SCS_READ_BYTES)
            if not view:
                return None
            raw = ctypes.string_at(view, _SCS_READ_BYTES)
            parsed = parse_scs_shared_memory(raw)
            return parsed if parsed["connected"] else None
        except OSError:
            return None
        finally:
            if view:
                kernel32.UnmapViewOfFile(view)
            kernel32.CloseHandle(handle)

    def _read_funbit_http(self) -> dict[str, Any] | None:
        now = time.monotonic()
        if now < self._next_http_try:
            return None
        try:
            with urllib.request.urlopen(self._funbit_url, timeout=0.2) as resp:
                if resp.status != 200:
                    self._next_http_try = now + self._http_backoff
                    return None
                payload = json.loads(resp.read().decode("utf-8"))
                return parse_funbit_telemetry_json(payload)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            self._next_http_try = now + self._http_backoff
            return None

    def read(self) -> dict[str, Any]:
        shm = self._read_shared_memory_windows()
        if shm is not None:
            return shm
        http_data = self._read_funbit_http()
        if http_data is not None:
            return http_data
        return disconnected_ets2_telemetry()
