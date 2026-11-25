"""Runtime configuration, paths, and LAN networking helpers for LocalDeck."""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path


def get_data_dir() -> Path:
    """Return the persistent LocalDeck data directory, creating it if needed."""
    override = os.environ.get("LOCALDECK_DATA_DIR")
    if override:
        path = Path(override).expanduser().resolve()
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        path = base / "LocalDeck"
    else:
        path = Path.home() / ".local" / "share" / "localdeck"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_web_dir() -> Path:
    """Return the path to bundled web assets in dev or PyInstaller frozen mode."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        frozen_web = Path(meipass) / "localdeck" / "web"
        if frozen_web.exists():
            return frozen_web
    return Path(__file__).resolve().parent / "web"


def get_lan_ip() -> str:
    """Determine the primary local LAN IPv4 address without sending external traffic."""
    for target in (("10.255.255.255", 1), ("8.8.8.8", 80)):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(target)
            ip = sock.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
        except OSError:
            continue
        finally:
            sock.close()
    return "127.0.0.1"


def pick_available_port(preferred: int = 8765) -> int:
    """Find an available TCP port starting from `preferred`, falling back to OS choice."""
    for candidate in range(preferred, preferred + 21):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("0.0.0.0", candidate))
            return candidate
        except OSError:
            continue
        finally:
            sock.close()

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("0.0.0.0", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()
