"""Windows system tray integration for keeping LocalDeck running silently in background."""

from __future__ import annotations

from typing import Callable

from PIL import Image

from localdeck.core.branding import render_logo_image


def create_tray_image() -> Image.Image:
    """Generate a crisp 64x64 LocalDeck icon matching the desktop and web logo."""
    return render_logo_image(64)


class TrayController:
    """Manages the pystray icon and Open/Quit menu items."""

    def __init__(
        self,
        on_show: Callable[[], None],
        on_quit: Callable[[], None],
        open_label: str = "Open LocalDeck",
        quit_label: str = "Quit",
    ) -> None:
        self._on_show = on_show
        self._on_quit = on_quit
        self._open_label = open_label
        self._quit_label = quit_label
        self._icon = None

    def start(self) -> bool:
        try:
            import pystray

            menu = pystray.Menu(
                pystray.MenuItem(self._open_label, lambda _icon, _item: self._on_show(), default=True),
                pystray.MenuItem(self._quit_label, lambda _icon, _item: self._on_quit()),
            )
            self._icon = pystray.Icon("LocalDeck", create_tray_image(), "LocalDeck", menu)
            self._icon.run_detached()
            return True
        except Exception:
            self._icon = None
            return False

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass
            self._icon = None
