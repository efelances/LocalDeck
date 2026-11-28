"""CLI and desktop entrypoint for LocalDeck."""

from __future__ import annotations

import argparse
import os
import sys

import uvicorn

from localdeck import __version__
from localdeck.config import get_data_dir, get_lan_ip, pick_available_port
from localdeck.core.deck_manager import DeckManager
from localdeck.server import create_app


def _run_headless(port: int | None = None) -> int:
    data_dir = get_data_dir()
    preferred_port = port or DeckManager(data_dir).get_config().settings.port
    chosen_port = pick_available_port(preferred_port)
    host_ip = get_lan_ip()
    app = create_app(data_dir=data_dir, host_ip=None, port=chosen_port)
    print(f"LocalDeck headless running at http://127.0.0.1:{chosen_port}/desktop")
    print(f"LAN address: http://{host_ip}:{chosen_port}")
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=chosen_port,
        log_level="warning",
        access_log=False,
        log_config=None,
    )
    return 0


def _ensure_stdio_for_windowed_exe() -> None:
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    _ensure_stdio_for_windowed_exe()
    parser = argparse.ArgumentParser(prog="localdeck", description="LocalDeck")
    parser.add_argument("--version", action="store_true", help="Print version and exit")
    parser.add_argument("--headless", action="store_true", help="Run HTTP/WS server without desktop window")
    parser.add_argument("--minimized", action="store_true", help="Start minimized to system tray")
    parser.add_argument("--port", type=int, default=None, help="Override listen port")
    args = parser.parse_args(argv)

    if args.version:
        print(__version__)
        return 0
    if args.headless:
        return _run_headless(port=args.port)

    from localdeck.desktop_app import run

    return run(port=args.port, minimized=args.minimized)


if __name__ == "__main__":
    raise SystemExit(main())
