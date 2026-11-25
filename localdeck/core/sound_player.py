"""Local soundboard storage and playback for validated .wav and .mp3 files."""

from __future__ import annotations

import ctypes
import os
import re
import secrets
import sys
import threading
from abc import ABC, abstractmethod
from pathlib import Path

SAFE_SOUND_RE = re.compile(r"^[A-Za-z0-9_.-]+\.(wav|mp3)$", re.IGNORECASE)
MAX_SOUND_BYTES = 10 * 1024 * 1024


def validate_sound_filename(filename: str | None) -> str:
    """Validate that a sound filename is a strict basename ending in .wav or .mp3."""
    cleaned = (filename or "").strip()
    if not cleaned or "/" in cleaned or "\\" in cleaned or ".." in cleaned:
        raise ValueError("Invalid sound filename")
    if not SAFE_SOUND_RE.match(cleaned):
        raise ValueError("Sound filename must match [A-Za-z0-9_.-]+.(wav|mp3)")
    return cleaned


class SoundLibrary:
    """Manages uploaded soundboard clips inside `<data_dir>/sounds`."""

    def __init__(self, data_dir: Path) -> None:
        self._sounds_dir = Path(data_dir) / "sounds"
        self._sounds_dir.mkdir(parents=True, exist_ok=True)

    @property
    def sounds_dir(self) -> Path:
        return self._sounds_dir

    def save_sound(self, original_name: str, data: bytes) -> str:
        if len(data) == 0:
            raise ValueError("Sound file is empty")
        if len(data) > MAX_SOUND_BYTES:
            raise ValueError("Sound file exceeds 10 MB limit")

        base = Path((original_name or "").strip()).name
        ext = Path(base).suffix.lower()
        if ext not in (".wav", ".mp3"):
            raise ValueError("Only .wav and .mp3 files are supported")

        raw_stem = Path(base).stem
        safe_stem = re.sub(r"[^A-Za-z0-9_-]+", "_", raw_stem).strip("_-") or "sound"
        safe_stem = safe_stem[:40]
        filename = f"{safe_stem}-{secrets.token_hex(3)}{ext}"
        validate_sound_filename(filename)

        target = (self._sounds_dir / filename).resolve()
        if target.parent != self._sounds_dir.resolve():
            raise ValueError("Unsafe sound path")

        tmp_path = self._sounds_dir / f"{filename}.tmp"
        tmp_path.write_bytes(data)
        os.replace(tmp_path, target)
        return filename

    def list_sounds(self) -> list[str]:
        if not self._sounds_dir.exists():
            return []
        items = [
            p.name
            for p in self._sounds_dir.iterdir()
            if p.is_file() and SAFE_SOUND_RE.match(p.name)
        ]
        return sorted(items, key=str.lower)

    def resolve_sound(self, filename: str) -> Path:
        cleaned = validate_sound_filename(filename)
        root = self._sounds_dir.resolve()
        candidate = (root / cleaned).resolve()
        if candidate.parent != root or not candidate.is_file():
            raise FileNotFoundError(f"Sound clip not found: {cleaned}")
        return candidate


class SoundPlayer(ABC):
    """Abstract soundboard playback backend."""

    @abstractmethod
    def play(self, path: Path) -> None:
        """Play the sound file asynchronously."""


class WindowsSoundPlayer(SoundPlayer):
    """Plays .wav via winsound and .mp3 via winmm MCI without external dependencies."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def play(self, path: Path) -> None:
        ext = path.suffix.lower()
        if ext == ".wav":
            import winsound

            winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC)
            return
        if ext == ".mp3":
            with self._lock:
                winmm = ctypes.windll.winmm
                alias = "localdeck_sfx"
                winmm.mciSendStringW(f"close {alias}", None, 0, None)
                escaped = str(path).replace('"', "")
                rc = winmm.mciSendStringW(f'open "{escaped}" type mpegvideo alias {alias}', None, 0, None)
                if rc != 0:
                    raise RuntimeError(f"Failed to open MP3 (MCI error {rc})")
                winmm.mciSendStringW(f"play {alias} from 0", None, 0, None)
            return
        raise ValueError(f"Unsupported audio format: {ext}")


class NoopSoundPlayer(SoundPlayer):
    """Recording sound player for tests and non-Windows hosts."""

    def __init__(self) -> None:
        self.played: list[Path] = []

    def play(self, path: Path) -> None:
        self.played.append(path)


def create_default_sound_player() -> SoundPlayer:
    if sys.platform == "win32":
        return WindowsSoundPlayer()
    return NoopSoundPlayer()
