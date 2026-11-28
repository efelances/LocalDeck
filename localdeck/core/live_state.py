"""Selective live-state daemon for widgets, OBS recording/scene LEDs, and ETS 2 telemetry."""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from localdeck.core.audio_mixer import AudioMixerBackend
from localdeck.core.obs_client import ObsBackend
from localdeck.core.telemetry import (
    Ets2TelemetryReader,
    SystemStatsReader,
    disconnected_ets2_telemetry,
)
from localdeck.models import DeckPage


def _default_obs_status() -> dict[str, Any]:
    return {
        "connected": False,
        "recording": False,
        "streaming": False,
        "studio_mode": False,
        "scene": "",
    }


class LiveStateWatcher:
    """Polls CPU/RAM, OBS status, master mute, and ETS 2 telemetry only when needed."""

    def __init__(
        self,
        should_poll: Callable[[], bool],
        get_active_page: Callable[[], DeckPage],
        get_obs_settings: Callable[[], tuple[int, str]],
        obs_backend: ObsBackend,
        mixer_backend: AudioMixerBackend,
        system_stats: SystemStatsReader,
        ets2_reader: Ets2TelemetryReader,
        on_live_state: Callable[[dict[str, Any]], None],
        poll_interval: float = 1.0,
    ) -> None:
        self._should_poll = should_poll
        self._get_active_page = get_active_page
        self._get_obs_settings = get_obs_settings
        self._obs_backend = obs_backend
        self._mixer_backend = mixer_backend
        self._system_stats = system_stats
        self._ets2_reader = ets2_reader
        self._on_live_state = on_live_state
        self._poll_interval = poll_interval
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_sent: dict[str, Any] | None = None
        self._next_obs_poll = 0.0
        self._force_obs_refresh = False

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._wake_event.clear()
        self._thread = threading.Thread(target=self._run, name="LocalDeckLiveState", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.5)
        self._thread = None

    def trigger_now(self) -> None:
        self._force_obs_refresh = True
        self._wake_event.set()

    def _run(self) -> None:
        last_obs = _default_obs_status()
        while not self._stop_event.is_set():
            self._wake_event.clear()
            if not self._should_poll():
                self._last_sent = None
                self._wake_event.wait(0.35)
                continue

            page = self._get_active_page()
            needs_system = False
            needs_ets2 = page.skin == "truck"
            needs_obs = False
            needs_master_mute = False

            for btn in page.buttons:
                act = btn.action
                if act.type == "widget":
                    if act.widget_kind == "cpu_ram":
                        needs_system = True
                    elif act.widget_kind in ("ets2_speed", "ets2_fuel"):
                        needs_ets2 = True
                elif act.type == "obs_action":
                    needs_obs = True
                elif act.type == "media" and act.media_action == "mute":
                    needs_master_mute = True

            system_data = self._system_stats.read() if needs_system else {"cpu_pct": 0, "ram_pct": 0}
            ets2_data = self._ets2_reader.read() if needs_ets2 else disconnected_ets2_telemetry()
            master_muted = self._mixer_backend.is_master_muted() if needs_master_mute else False

            if needs_obs:
                now = time.monotonic()
                if self._force_obs_refresh or now >= self._next_obs_poll:
                    self._force_obs_refresh = False
                    port, password = self._get_obs_settings()
                    last_obs = self._obs_backend.fetch_status(port=port, password=password)
                    self._next_obs_poll = now + (2.5 if last_obs.get("connected") else 5.0)
                obs_data = last_obs
            else:
                obs_data = _default_obs_status()

            snapshot = {
                "system": system_data,
                "obs": obs_data,
                "ets2": ets2_data,
                "master_muted": master_muted,
            }
            if snapshot != self._last_sent:
                self._last_sent = snapshot
                try:
                    self._on_live_state(snapshot)
                except Exception:
                    pass

            self._wake_event.wait(self._poll_interval)
