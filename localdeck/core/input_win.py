"""Safe hardware scan-code input backend for Windows SendInput and test doubles."""

from __future__ import annotations

import ctypes
import sys
import threading
from abc import ABC, abstractmethod
from ctypes import wintypes
from typing import Any

KEY_VK_MAP: dict[str, int] = {
    # Letters
    **{chr(c): c - 32 for c in range(ord("a"), ord("z") + 1)},
    # Digits
    **{str(d): 0x30 + d for d in range(10)},
    # Function keys F1-F24
    **{f"f{i}": 0x6F + i for i in range(1, 25)},
    # Numpad digits
    **{f"num{d}": 0x60 + d for d in range(10)},
    # Modifiers
    "ctrl": 0x11,
    "lctrl": 0xA2,
    "rctrl": 0xA3,
    "shift": 0x10,
    "lshift": 0xA0,
    "rshift": 0xA1,
    "alt": 0x12,
    "lalt": 0xA4,
    "ralt": 0xA5,
    "win": 0x5B,
    "lwin": 0x5B,
    "rwin": 0x5C,
    # Navigation & control
    "enter": 0x0D,
    "esc": 0x1B,
    "tab": 0x09,
    "space": 0x20,
    "backspace": 0x08,
    "delete": 0x2E,
    "insert": 0x2D,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    "printscreen": 0x2C,
    # Punctuation
    "minus": 0xBD,
    "equal": 0xBB,
    "lbracket": 0xDB,
    "rbracket": 0xDD,
    "semicolon": 0xBA,
    "apostrophe": 0xDE,
    "comma": 0xBC,
    "period": 0xBE,
    "slash": 0xBF,
    "backslash": 0xDC,
    "grave": 0xC0,
}

EXTENDED_KEYS: frozenset[str] = frozenset(
    {
        "rctrl",
        "ralt",
        "win",
        "lwin",
        "rwin",
        "insert",
        "delete",
        "home",
        "end",
        "pageup",
        "pagedown",
        "up",
        "down",
        "left",
        "right",
        "printscreen",
    }
)

MEDIA_VK_MAP: dict[str, int] = {
    "play_pause": 0xB3,
    "next": 0xB0,
    "prev": 0xB1,
    "stop": 0xB2,
    "vol_up": 0xAF,
    "vol_down": 0xAE,
    "mute": 0xAD,
}

ALLOWED_KEYS: frozenset[str] = frozenset(KEY_VK_MAP.keys())

INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
MAPVK_VK_TO_VSC = 0


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _anonymous_ = ("_input",)
    _fields_ = [
        ("type", wintypes.DWORD),
        ("_input", _INPUT_UNION),
    ]


class InputBackend(ABC):
    """Abstract keyboard and media input backend."""

    @abstractmethod
    def tap_hotkey(self, keys: list[str]) -> None:
        """Press and release a hotkey chord."""

    @abstractmethod
    def hold_start(self, keys: list[str], owner_id: str = "default") -> None:
        """Hold keys down on behalf of `owner_id`."""

    @abstractmethod
    def hold_end(self, keys: list[str], owner_id: str = "default") -> None:
        """Release held keys for `owner_id`."""

    @abstractmethod
    def release_owner(self, owner_id: str) -> None:
        """Release all keys currently held by `owner_id`."""

    @abstractmethod
    def release_all(self) -> None:
        """Release all keys held by any owner."""

    @abstractmethod
    def tap_media(self, media_action: str) -> None:
        """Trigger a media key event."""


class WindowsInputBackend(InputBackend):
    """Windows SendInput backend using hardware scan codes for hotkeys and VKs for media."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._held_by_owner: dict[str, list[str]] = {}
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._user32.SendInput.argtypes = (
            wintypes.UINT,
            ctypes.POINTER(INPUT),
            ctypes.c_int,
        )
        self._user32.SendInput.restype = wintypes.UINT
        self._user32.MapVirtualKeyW.argtypes = (wintypes.UINT, wintypes.UINT)
        self._user32.MapVirtualKeyW.restype = wintypes.UINT

    def _is_held_elsewhere(self, key: str, exclude_owner: str | None = None) -> bool:
        for owner, held_keys in self._held_by_owner.items():
            if owner != exclude_owner and key in held_keys:
                return True
        return False

    def _build_key_input(self, key: str, key_up: bool = False) -> INPUT:
        vk = KEY_VK_MAP[key]
        scan = int(self._user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC))
        if scan:
            flags = KEYEVENTF_SCANCODE
            w_vk = 0
            w_scan = scan
        else:
            flags = 0
            w_vk = vk
            w_scan = 0
        if key in EXTENDED_KEYS:
            flags |= KEYEVENTF_EXTENDEDKEY
        if key_up:
            flags |= KEYEVENTF_KEYUP
        return INPUT(
            type=INPUT_KEYBOARD,
            ki=KEYBDINPUT(wVk=w_vk, wScan=w_scan, dwFlags=flags, time=0, dwExtraInfo=0),
        )

    def _send_inputs(self, inputs: list[INPUT]) -> None:
        if not inputs:
            return
        arr = (INPUT * len(inputs))(*inputs)
        self._user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))

    def tap_hotkey(self, keys: list[str]) -> None:
        normalized = [k.lower() for k in keys if k.lower() in KEY_VK_MAP]
        if not normalized:
            return
        inputs = [self._build_key_input(k, key_up=False) for k in normalized]
        inputs.extend(self._build_key_input(k, key_up=True) for k in reversed(normalized))
        self._send_inputs(inputs)

    def hold_start(self, keys: list[str], owner_id: str = "default") -> None:
        normalized = [k.lower() for k in keys if k.lower() in KEY_VK_MAP]
        if not normalized:
            return
        to_press: list[INPUT] = []
        with self._lock:
            owner_keys = self._held_by_owner.setdefault(owner_id, [])
            for key in normalized:
                already_held = key in owner_keys or self._is_held_elsewhere(key, exclude_owner=owner_id)
                if key not in owner_keys:
                    owner_keys.append(key)
                if not already_held:
                    to_press.append(self._build_key_input(key, key_up=False))
        self._send_inputs(to_press)

    def hold_end(self, keys: list[str], owner_id: str = "default") -> None:
        normalized = [k.lower() for k in keys if k.lower() in KEY_VK_MAP]
        if not normalized:
            return
        to_release: list[INPUT] = []
        with self._lock:
            owner_keys = self._held_by_owner.get(owner_id, [])
            for key in reversed(normalized):
                if key in owner_keys:
                    owner_keys.remove(key)
                if not self._is_held_elsewhere(key, exclude_owner=None):
                    to_release.append(self._build_key_input(key, key_up=True))
            if not owner_keys and owner_id in self._held_by_owner:
                del self._held_by_owner[owner_id]
        self._send_inputs(to_release)

    def release_owner(self, owner_id: str) -> None:
        to_release: list[INPUT] = []
        with self._lock:
            owner_keys = self._held_by_owner.pop(owner_id, [])
            for key in reversed(owner_keys):
                if not self._is_held_elsewhere(key, exclude_owner=None):
                    to_release.append(self._build_key_input(key, key_up=True))
        self._send_inputs(to_release)

    def release_all(self) -> None:
        to_release: list[INPUT] = []
        with self._lock:
            seen: set[str] = set()
            all_keys: list[str] = []
            for owner_keys in self._held_by_owner.values():
                for key in owner_keys:
                    if key not in seen:
                        seen.add(key)
                        all_keys.append(key)
            self._held_by_owner.clear()
            for key in reversed(all_keys):
                to_release.append(self._build_key_input(key, key_up=True))
        self._send_inputs(to_release)

    def tap_media(self, media_action: str) -> None:
        vk = MEDIA_VK_MAP.get(media_action)
        if vk is None:
            return
        down = INPUT(
            type=INPUT_KEYBOARD,
            ki=KEYBDINPUT(wVk=vk, wScan=0, dwFlags=KEYEVENTF_EXTENDEDKEY, time=0, dwExtraInfo=0),
        )
        up = INPUT(
            type=INPUT_KEYBOARD,
            ki=KEYBDINPUT(
                wVk=vk,
                wScan=0,
                dwFlags=KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP,
                time=0,
                dwExtraInfo=0,
            ),
        )
        self._send_inputs([down, up])


class NoopInputBackend(InputBackend):
    """In-memory recording backend for unit tests and non-Windows hosts."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.events: list[tuple[str, Any]] = []
        self.held_by_owner: dict[str, list[str]] = {}

    def _is_held_elsewhere(self, key: str, exclude_owner: str | None = None) -> bool:
        for owner, held_keys in self.held_by_owner.items():
            if owner != exclude_owner and key in held_keys:
                return True
        return False

    def tap_hotkey(self, keys: list[str]) -> None:
        normalized = [k.lower() for k in keys if k.lower() in KEY_VK_MAP]
        with self._lock:
            self.events.append(("tap_hotkey", list(normalized)))

    def hold_start(self, keys: list[str], owner_id: str = "default") -> None:
        normalized = [k.lower() for k in keys if k.lower() in KEY_VK_MAP]
        with self._lock:
            owner_keys = self.held_by_owner.setdefault(owner_id, [])
            for key in normalized:
                if key not in owner_keys:
                    owner_keys.append(key)
            self.events.append(("hold_start", list(normalized), owner_id))

    def hold_end(self, keys: list[str], owner_id: str = "default") -> None:
        normalized = [k.lower() for k in keys if k.lower() in KEY_VK_MAP]
        with self._lock:
            owner_keys = self.held_by_owner.get(owner_id, [])
            for key in normalized:
                if key in owner_keys:
                    owner_keys.remove(key)
            if not owner_keys and owner_id in self.held_by_owner:
                del self.held_by_owner[owner_id]
            self.events.append(("hold_end", list(normalized), owner_id))

    def release_owner(self, owner_id: str) -> None:
        with self._lock:
            released = self.held_by_owner.pop(owner_id, [])
            if released:
                self.events.append(("release_owner", released, owner_id))

    def release_all(self) -> None:
        with self._lock:
            all_keys = [k for keys in self.held_by_owner.values() for k in keys]
            self.held_by_owner.clear()
            if all_keys:
                self.events.append(("release_all", all_keys))

    def tap_media(self, media_action: str) -> None:
        if media_action in MEDIA_VK_MAP:
            with self._lock:
                self.events.append(("tap_media", media_action))


def create_default_input_backend() -> InputBackend:
    """Create the platform-appropriate input backend."""
    if sys.platform == "win32":
        return WindowsInputBackend()
    return NoopInputBackend()
