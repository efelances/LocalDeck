"""Executes safe allowlisted deck button actions (hotkeys, hold, media, macro, URL, sound, app, OBS, page switch)."""

from __future__ import annotations

import time
import webbrowser
from typing import Any, Callable
from urllib.parse import urlparse

from localdeck.core.app_launcher import launch_safe_app
from localdeck.core.input_win import InputBackend
from localdeck.core.obs_client import ObsBackend
from localdeck.core.sound_player import SoundLibrary, SoundPlayer
from localdeck.models import ButtonAction


def _is_safe_http_url(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url.strip())
    return parsed.scheme.lower() in ("http", "https") and bool(parsed.netloc)


class ActionExecutor:
    """Validates and dispatches button actions through safe backends."""

    def __init__(
        self,
        input_backend: InputBackend,
        on_switch_page: Callable[[str], None] | None = None,
        open_url_fn: Callable[[str], Any] | None = None,
        sound_library: SoundLibrary | None = None,
        sound_player: SoundPlayer | None = None,
        launch_app_fn: Callable[[str], Any] | None = None,
        obs_backend: ObsBackend | None = None,
        get_obs_settings: Callable[[], tuple[int, str]] | None = None,
    ) -> None:
        self._input = input_backend
        self._on_switch_page = on_switch_page
        self._open_url = open_url_fn or webbrowser.open
        self._sound_library = sound_library
        self._sound_player = sound_player
        self._launch_app = launch_app_fn or launch_safe_app
        self._obs_backend = obs_backend
        self._get_obs_settings = get_obs_settings or (lambda: (4455, ""))

    def set_switch_page_callback(self, callback: Callable[[str], None] | None) -> None:
        self._on_switch_page = callback

    def execute_tap(self, action: ButtonAction, owner_id: str = "default") -> dict[str, Any]:
        del owner_id
        kind = action.type
        if kind == "hotkey":
            self._input.tap_hotkey(action.keys)
            return {"executed": "hotkey"}
        if kind == "hold_key":
            self._input.tap_hotkey(action.keys)
            return {"executed": "hold_key_tap"}
        if kind == "media":
            if action.media_action:
                self._input.tap_media(action.media_action)
            return {"executed": "media"}
        if kind == "macro":
            for step in action.steps:
                if step.kind == "hotkey" and step.keys:
                    self._input.tap_hotkey(step.keys)
                elif step.kind == "media" and step.media_action:
                    self._input.tap_media(step.media_action)
                elif step.kind == "delay" and step.delay_ms > 0:
                    time.sleep(step.delay_ms / 1000.0)
            return {"executed": "macro", "steps": len(action.steps)}
        if kind == "open_url":
            if not _is_safe_http_url(action.url):
                raise ValueError("Blocked unsafe URL")
            assert action.url is not None
            self._open_url(action.url.strip())
            return {"executed": "open_url"}
        if kind == "switch_page":
            if action.target_page_id and self._on_switch_page:
                self._on_switch_page(action.target_page_id)
            return {"switched_page_id": action.target_page_id}
        if kind == "play_sound":
            if not action.sound_file or self._sound_library is None or self._sound_player is None:
                raise ValueError("Soundboard is not configured")
            clip_path = self._sound_library.resolve_sound(action.sound_file)
            self._sound_player.play(clip_path)
            return {"executed": "play_sound", "sound_file": action.sound_file}
        if kind == "launch_app":
            if not action.app_path:
                raise ValueError("Application path is required")
            self._launch_app(action.app_path)
            return {"executed": "launch_app", "app_path": action.app_path}
        if kind == "obs_action":
            if not action.obs_command or self._obs_backend is None:
                raise ValueError("OBS command is not configured")
            port, password = self._get_obs_settings()
            res = self._obs_backend.execute(
                command=action.obs_command,
                scene=action.obs_scene,
                input_name=action.obs_input,
                port=port,
                password=password,
            )
            return {"executed": "obs_action", "obs": res}
        if kind == "widget":
            return {"executed": "widget", "widget_kind": action.widget_kind}
        return {"executed": "noop"}

    def execute_hold_start(self, action: ButtonAction, owner_id: str = "default") -> dict[str, Any]:
        if action.type in ("hold_key", "hotkey") and action.keys:
            self._input.hold_start(action.keys, owner_id=owner_id)
            return {"holding": list(action.keys)}
        return self.execute_tap(action, owner_id=owner_id)

    def execute_hold_end(self, action: ButtonAction, owner_id: str = "default") -> dict[str, Any]:
        if action.type in ("hold_key", "hotkey") and action.keys:
            self._input.hold_end(action.keys, owner_id=owner_id)
            return {"released": list(action.keys)}
        return {"released": []}

    def release_owner(self, owner_id: str) -> None:
        self._input.release_owner(owner_id)

    def release_all(self) -> None:
        self._input.release_all()
