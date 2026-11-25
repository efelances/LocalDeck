"""Native pywebview desktop host for LocalDeck."""

from __future__ import annotations

import threading
import time
import urllib.error
import urllib.request

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


def run(port: int | None = None, minimized: bool = False) -> int:
    """Start the local FastAPI server and open the pywebview desktop window."""
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
        host_ip = get_lan_ip()

        app = create_app(data_dir=data_dir, host_ip=host_ip, port=chosen_port)
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

        window = webview.create_window(
            "LocalDeck",
            f"http://127.0.0.1:{chosen_port}/desktop",
            width=1220,
            height=780,
            min_size=(920, 620),
            hidden=minimized,
            background_color="#1C1B19" if settings.theme == "dark" else "#F7F5F0",
        )

        allow_quit = False
        tray_active = False
        open_label, quit_label = _TRAY_LABELS.get(settings.language, _TRAY_LABELS["en"])

        def _show_window() -> None:
            try:
                window.show()
                window.restore()
                window.on_top = True
                window.on_top = False
            except Exception:
                pass

        app.state.ctx.on_focus_window = _show_window

        def _quit_app() -> None:
            nonlocal allow_quit
            allow_quit = True
            tray.stop()
            try:
                window.destroy()
            except Exception:
                pass

        tray = TrayController(
            on_show=_show_window,
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
            webview.start()
        finally:
            tray.stop()
            clear_runtime_info(data_dir)
            server.should_exit = True
            app.state.ctx.executor.release_all()
            thread.join(timeout=3.0)
        return 0
    finally:
        guard.release()
