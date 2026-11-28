"""Native pywebview desktop host for LocalDeck with browser-app fallback."""

from __future__ import annotations

import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

import uvicorn
import webview

from localdeck.config import get_data_dir, get_lan_ip, pick_available_port
from localdeck.core.deck_manager import DeckManager
from localdeck.core.single_instance import (
    SingleInstanceGuard,
    clear_runtime_info,
    notify_existing_instance,
    write_runtime_info,
)
from localdeck.core.win_unblock import unblock_frozen_app_dir
from localdeck.server import create_app
from localdeck.tray import TrayController


_TRAY_LABELS = {
    "tr": ("LocalDeck'i Aç", "Çıkış"),
    "en": ("Open LocalDeck", "Quit"),
    "de": ("LocalDeck öffnen", "Beenden"),
    "es": ("Abrir LocalDeck", "Salir"),
}


class _ThreadSafeUvicornServer(uvicorn.Server):
    """Uvicorn server that skips signal handler registration when run in a daemon thread."""

    def install_signal_handlers(self) -> None:
        return None


def _wait_for_health(port: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/health"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=0.5) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            time.sleep(0.1)
    raise TimeoutError(f"LocalDeck server did not become ready on port {port} within {timeout}s")


def _open_desktop_browser_fallback(url: str) -> None:
    """Open desktop UI in an Edge/Chrome `--app` window (or system default browser)."""
    candidates = [
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
        / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
        / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "Google" / "Chrome" / "Application" / "chrome.exe",
    ]
    for browser in candidates:
        if browser.is_file():
            try:
                subprocess.Popen([str(browser), f"--app={url}"], shell=False)
                return
            except OSError:
                continue
    webbrowser.open(url)


def run(port: int | None = None, minimized: bool = False) -> int:
    """Start the local FastAPI server and open the pywebview desktop window."""
    unblock_frozen_app_dir()
    data_dir = get_data_dir()
    guard = SingleInstanceGuard()
    if not guard.acquire():
        notify_existing_instance(data_dir)
        guard.release()
        return 0

    try:
        settings = DeckManager(data_dir).get_config().settings
        preferred_port = port or settings.port
        chosen_port = pick_available_port(preferred_port)

        app = create_app(data_dir=data_dir, host_ip=None, port=chosen_port)
        config = uvicorn.Config(
            app,
            host="0.0.0.0",
            port=chosen_port,
            log_level="warning",
            access_log=False,
            log_config=None,
        )
        server = _ThreadSafeUvicornServer(config)

        thread = threading.Thread(target=server.run, name="LocalDeckServer", daemon=True)
        thread.start()
        _wait_for_health(chosen_port)
        write_runtime_info(data_dir, chosen_port, app.state.ctx.auth_manager.desktop_token)

        desktop_url = f"http://127.0.0.1:{chosen_port}/desktop"
        window = webview.create_window(
            "LocalDeck",
            desktop_url,
            width=1220,
            height=780,
            min_size=(920, 620),
            hidden=minimized,
            background_color="#1C1B19" if settings.theme == "dark" else "#F7F5F0",
        )

        allow_quit = False
        tray_active = False
        fallback_quit = threading.Event()
        open_label, quit_label = _TRAY_LABELS.get(settings.language, _TRAY_LABELS["en"])

        def _show_window() -> None:
            try:
                window.show()
                window.restore()
                window.on_top = True
                window.on_top = False
            except Exception:
                _open_desktop_browser_fallback(desktop_url)

        show_handler = [_show_window]

        def _on_tray_show() -> None:
            show_handler[0]()

        app.state.ctx.on_focus_window = _on_tray_show

        def _quit_app() -> None:
            nonlocal allow_quit
            allow_quit = True
            fallback_quit.set()
            tray.stop()
            try:
                window.destroy()
            except Exception:
                pass

        tray = TrayController(
            on_show=_on_tray_show,
            on_quit=_quit_app,
            open_label=open_label,
            quit_label=quit_label,
        )

        def _on_closing() -> bool | None:
            if not allow_quit and tray_active:
                try:
                    window.hide()
                    return False
                except Exception:
                    return None
            return None

        window.events.closing += _on_closing
        tray_active = tray.start()
        try:
            try:
                webview.start()
            except Exception:
                # Fallback if pythonnet/.NET/WebView2 cannot load on the host machine
                show_handler[0] = lambda: _open_desktop_browser_fallback(desktop_url)
                if not minimized:
                    _open_desktop_browser_fallback(desktop_url)
                if tray_active:
                    fallback_quit.wait()
                else:
                    while not allow_quit:
                        time.sleep(0.5)
        finally:
            tray.stop()
            clear_runtime_info(data_dir)
            server.should_exit = True
            app.state.ctx.executor.release_all()
            thread.join(timeout=3.0)
        return 0
    finally:
        guard.release()
