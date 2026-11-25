"""FastAPI HTTP server and WebSocket hub for LocalDeck desktop and mobile clients."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import json
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from localdeck import __version__
from localdeck.config import get_data_dir, get_lan_ip, get_web_dir
from localdeck.core.actions import ActionExecutor
from localdeck.core.app_launcher import detect_installed_apps
from localdeck.core.audio_meter import (
    AudioMeterBackend,
    AudioMeterWatcher,
    create_default_audio_meter_backend,
)
from localdeck.core.audio_mixer import AudioMixerBackend, create_default_audio_mixer_backend
from localdeck.core.auth import AuthManager
from localdeck.core.autostart import is_autostart_enabled, set_autostart
from localdeck.core.branding import LOGO_SVG
from localdeck.core.deck_manager import DeckManager
from localdeck.core.input_win import ALLOWED_KEYS, MEDIA_VK_MAP, InputBackend, create_default_input_backend
from localdeck.core.live_state import LiveStateWatcher
from localdeck.core.obs_client import ObsBackend, ObsWebSocketClient
from localdeck.core.qr import generate_qr_svg
from localdeck.core.sound_player import (
    MAX_SOUND_BYTES,
    SoundLibrary,
    SoundPlayer,
    create_default_sound_player,
)
from localdeck.core.telemetry import Ets2TelemetryReader, SystemStatsReader
from localdeck.core.templates import build_template_page, list_templates
from localdeck.core.window_watcher import WindowWatcher
from localdeck.models import DeckButton, DeckPage, LanguageCode, ThemeMode

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})
_LOOPBACK_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "::1", "testserver"})

_PAIR_GATE_TEXT: dict[str, dict[str, str]] = {
    "tr": {
        "pair_title": "Eşleştirme Gerekli",
        "pair_body": "Bu cihaz henüz LocalDeck ile eşleştirilmedi. PC'deki LocalDeck uygulamasından QR kodu okutun.",
        "expired_title": "Bağlantı Geçersiz veya Süresi Dolmuş",
        "expired_body": "Bu tek kullanımlık QR bağlantısı kullanılmış veya süresi dolmuş. PC'de QR kodu yenileyip tekrar okutun.",
        "hint": "Güvenlik için her QR kod yalnızca bir kez kullanılabilir.",
    },
    "en": {
        "pair_title": "Pairing Required",
        "pair_body": "This device is not paired with LocalDeck yet. Scan the QR code shown in the LocalDeck desktop app.",
        "expired_title": "Link Invalid or Expired",
        "expired_body": "This single-use QR link has already been used or expired. Refresh the QR code on your PC and scan again.",
        "hint": "For LAN security, each QR code can only be redeemed once.",
    },
    "de": {
        "pair_title": "Kopplung erforderlich",
        "pair_body": "Dieses Gerät ist noch nicht mit LocalDeck gekoppelt. Scanne den QR-Code in der LocalDeck-Desktop-App.",
        "expired_title": "Link ungültig oder abgelaufen",
        "expired_body": "Dieser Einmal-QR-Link wurde bereits verwendet oder ist abgelaufen. Erneuere den QR-Code am PC.",
        "hint": "Aus Sicherheitsgründen ist jeder QR-Code nur einmal gültig.",
    },
    "es": {
        "pair_title": "Emparejamiento necesario",
        "pair_body": "Este dispositivo aún no está vinculado con LocalDeck. Escanea el código QR en la app de escritorio.",
        "expired_title": "Enlace inválido o caducado",
        "expired_body": "Este enlace QR de un solo uso ya fue utilizado o caducó. Actualiza el código QR en tu PC.",
        "hint": "Por seguridad, cada código QR solo se puede usar una vez.",
    },
}

def is_loopback(host: str | None) -> bool:
    return (host or "").lower() in _LOOPBACK_HOSTS


def _host_header_is_loopback(host_header: str | None) -> bool:
    h = (host_header or "").strip().lower()
    if h.startswith("["):
        h = h[1:].split("]", 1)[0]
    else:
        h = h.rsplit(":", 1)[0] if h.count(":") == 1 else h
    return h in _LOOPBACK_HOSTNAMES


class SettingsPatch(BaseModel):
    language: LanguageCode | None = None
    theme: ThemeMode | None = None
    high_contrast: bool | None = None
    auto_switch: bool | None = None
    haptics: bool | None = None
    start_with_windows: bool | None = None
    active_page_id: str | None = None
    obs_port: int | None = Field(default=None, ge=1024, le=65535)
    obs_password: str | None = Field(default=None, max_length=128)


class MixerVolumeRequest(BaseModel):
    exe: str | None = None
    volume: float = Field(ge=0.0, le=1.0)


class MixerMuteRequest(BaseModel):
    exe: str | None = None
    muted: bool


class SoundUploadRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=128)
    data_base64: str = Field(min_length=1)


class SwapRequest(BaseModel):
    from_row: int = Field(ge=0, le=5)
    from_col: int = Field(ge=0, le=5)
    to_row: int = Field(ge=0, le=5)
    to_col: int = Field(ge=0, le=5)


class AppContext:
    """Shared runtime state attached to FastAPI `app.state.ctx`."""

    def __init__(
        self,
        data_dir: Path,
        web_dir: Path,
        input_backend: InputBackend,
        audio_backend: AudioMeterBackend,
        audio_mixer: AudioMixerBackend,
        sound_player: SoundPlayer,
        obs_backend: ObsBackend,
        launch_app_fn: Callable[[str], Any] | None,
        on_focus_window: Callable[[], None] | None,
        set_autostart_fn: Callable[[bool], bool],
        get_autostart_fn: Callable[[], bool],
        host_ip: str,
        port: int,
    ) -> None:
        self.data_dir = data_dir
        self.web_dir = web_dir
        self.deck_manager = DeckManager(data_dir)
        self.auth_manager = AuthManager(data_dir)
        self.sound_library = SoundLibrary(data_dir)
        self.sound_player = sound_player
        self.obs_backend = obs_backend
        self.audio_mixer = audio_mixer
        self.set_autostart_fn = set_autostart_fn
        self.get_autostart_fn = get_autostart_fn
        self.executor = ActionExecutor(
            input_backend=input_backend,
            sound_library=self.sound_library,
            sound_player=sound_player,
            launch_app_fn=launch_app_fn,
            obs_backend=obs_backend,
            get_obs_settings=self._get_obs_settings,
        )
        self.executor.set_switch_page_callback(self._handle_switch_page_sync)
        self.audio_backend = audio_backend
        self.host_ip = host_ip
        self.port = port
        self.loop: asyncio.AbstractEventLoop | None = None
        self.window_watcher: WindowWatcher | None = None
        self.audio_watcher: AudioMeterWatcher | None = None
        self.live_watcher: LiveStateWatcher | None = None
        self.mobile_clients: dict[str, tuple[str, WebSocket]] = {}
        self.desktop_clients: set[WebSocket] = set()
        self.current_foreground: dict[str, str] = {"exe": "", "title": ""}
        self.last_live_state: dict[str, Any] = {}
        self.auto_switched_from_general: bool = False
        self.asset_version: str = secrets.token_hex(4)
        self.on_focus_window = on_focus_window

    def lan_base_url(self) -> str:
        return f"http://{self.host_ip}:{self.port}"

    def pairing_payload(self, force_refresh: bool = False) -> dict[str, Any]:
        pt = (
            self.auth_manager.create_pairing_token()
            if force_refresh
            else self.auth_manager.get_or_create_pairing_token()
        )
        url = f"{self.lan_base_url()}/pair?t={pt.token}"
        return {
            "url": url,
            "svg": generate_qr_svg(url),
            "expires_in": pt.seconds_remaining(),
        }

    def devices_payload(self) -> list[dict[str, Any]]:
        connected_sessions = {session_id for session_id, _ in self.mobile_clients.values()}
        devices = self.auth_manager.list_sessions()
        for dev in devices:
            dev["connected"] = dev["session_id"] in connected_sessions
        return devices

    def mobile_state_payload(self) -> dict[str, Any]:
        config = self.deck_manager.get_config()
        settings = config.settings.model_dump(mode="json")
        settings.pop("obs_password", None)
        settings.pop("start_with_windows", None)
        return {
            "settings": settings,
            "pages": [p.model_dump(mode="json") for p in config.pages],
            "active_page_id": config.settings.active_page_id,
            "foreground": dict(self.current_foreground),
            "asset_version": self.asset_version,
        }

    def desktop_state_payload(self) -> dict[str, Any]:
        config = self.deck_manager.get_config()
        config_dict = config.model_dump(mode="json")
        config_dict["settings"]["start_with_windows"] = bool(self.get_autostart_fn())
        return {
            "version": __version__,
            "config": config_dict,
            "active_page_id": config.settings.active_page_id,
            "lan_url": self.lan_base_url(),
            "pairing": self.pairing_payload(),
            "devices": self.devices_payload(),
            "connected_count": len(self.mobile_clients),
            "foreground": dict(self.current_foreground),
            "allowed_keys": sorted(ALLOWED_KEYS),
            "media_actions": list(MEDIA_VK_MAP.keys()),
            "sounds": self.sound_library.list_sounds(),
        }

    def _get_obs_settings(self) -> tuple[int, str]:
        settings = self.deck_manager.get_config().settings
        return (settings.obs_port, settings.obs_password)

    def _handle_switch_page_sync(self, target_page_id: str) -> None:
        if self.deck_manager.get_page(target_page_id) is None:
            return
        self.deck_manager.set_active_page(target_page_id)
        self.auto_switched_from_general = False

    async def broadcast_mobile(self, message: dict[str, Any]) -> None:
        stale: list[str] = []
        for client_id, (_, ws) in list(self.mobile_clients.items()):
            try:
                await ws.send_json(message)
            except Exception:
                stale.append(client_id)
        for client_id in stale:
            self.mobile_clients.pop(client_id, None)
            self.executor.release_owner(client_id)

    async def broadcast_desktop(self, message: dict[str, Any]) -> None:
        stale: list[WebSocket] = []
        for ws in list(self.desktop_clients):
            try:
                await ws.send_json(message)
            except Exception:
                stale.append(ws)
        for ws in stale:
            self.desktop_clients.discard(ws)

    async def broadcast_state_sync(self) -> None:
        await self.broadcast_mobile({"type": "state_sync", "state": self.mobile_state_payload()})
        await self.broadcast_desktop({"type": "state_sync", "state": self.desktop_state_payload()})

    async def broadcast_devices_updated(self) -> None:
        await self.broadcast_desktop(
            {
                "type": "devices_updated",
                "devices": self.devices_payload(),
                "connected_count": len(self.mobile_clients),
            }
        )

    def on_foreground_change(self, exe: str, title: str) -> None:
        self.current_foreground = {"exe": exe, "title": title}
        loop = self.loop
        if loop is None or loop.is_closed():
            return

        async def _apply() -> None:
            await self.broadcast_desktop(
                {"type": "foreground_updated", "foreground": dict(self.current_foreground)}
            )
            config = self.deck_manager.get_config()
            if not config.settings.auto_switch:
                return
            matched_id = self.deck_manager.match_window(exe, title)
            active_id = config.settings.active_page_id
            default_id = config.pages[0].id if config.pages else "general"
            changed = False
            if matched_id and matched_id != active_id:
                self.deck_manager.set_active_page(matched_id)
                self.auto_switched_from_general = True
                changed = True
            elif matched_id is None and self.auto_switched_from_general and active_id != default_id:
                self.deck_manager.set_active_page(default_id)
                self.auto_switched_from_general = False
                changed = True
            if changed:
                await self.broadcast_state_sync()

        asyncio.run_coroutine_threadsafe(_apply(), loop)

    def should_poll_audio(self) -> bool:
        if not self.mobile_clients and not self.desktop_clients:
            return False
        return bool(self.deck_manager.get_active_page().audio_viz_enabled)

    def get_audio_target_exe(self) -> str | None:
        exe = self.deck_manager.get_active_page().audio_source_exe
        return exe or None

    def on_audio_level(self, peak: float, left: float, right: float) -> None:
        loop = self.loop
        if loop is None or loop.is_closed():
            return
        payload = {
            "type": "audio_level",
            "peak": round(peak, 4),
            "left": round(left, 4),
            "right": round(right, 4),
        }

        async def _send() -> None:
            await self.broadcast_mobile(payload)
            await self.broadcast_desktop(payload)

        asyncio.run_coroutine_threadsafe(_send(), loop)

    def should_poll_live(self) -> bool:
        return bool(self.mobile_clients or self.desktop_clients)

    def trigger_live_refresh(self) -> None:
        if self.live_watcher is not None:
            self.live_watcher.trigger_now()

    def on_live_state(self, live: dict[str, Any]) -> None:
        self.last_live_state = live
        loop = self.loop
        if loop is None or loop.is_closed():
            return
        payload = {"type": "live_state", "live": live}

        async def _send() -> None:
            await self.broadcast_mobile(payload)
            await self.broadcast_desktop(payload)

        asyncio.run_coroutine_threadsafe(_send(), loop)

    async def broadcast_mixer_state(self, mixer_state: dict[str, Any]) -> None:
        payload = {"type": "mixer_state", "mixer": mixer_state}
        await self.broadcast_mobile(payload)
        await self.broadcast_desktop(payload)


def create_app(
    data_dir: Path | None = None,
    input_backend: InputBackend | None = None,
    enable_window_watcher: bool = True,
    enable_audio_meter: bool = True,
    audio_meter_backend: AudioMeterBackend | None = None,
    audio_mixer_backend: AudioMixerBackend | None = None,
    sound_player: SoundPlayer | None = None,
    launch_app_fn: Callable[[str], Any] | None = None,
    obs_backend: ObsBackend | None = None,
    enable_live_state: bool = True,
    live_state_poll_interval: float = 1.0,
    system_stats_reader: SystemStatsReader | None = None,
    ets2_telemetry_reader: Ets2TelemetryReader | None = None,
    set_autostart_fn: Callable[[bool], bool] | None = None,
    get_autostart_fn: Callable[[], bool] | None = None,
    on_focus_window: Callable[[], None] | None = None,
    host_ip: str | None = None,
    port: int = 8765,
) -> FastAPI:
    """Build and configure the FastAPI application for LocalDeck."""
    resolved_data = data_dir or get_data_dir()
    resolved_web = get_web_dir()
    resolved_backend = input_backend or create_default_input_backend()
    resolved_audio = audio_meter_backend or create_default_audio_meter_backend()
    resolved_mixer = audio_mixer_backend or create_default_audio_mixer_backend()
    resolved_sound = sound_player or create_default_sound_player()
    resolved_obs = obs_backend or ObsWebSocketClient()
    resolved_stats = system_stats_reader or SystemStatsReader()
    resolved_ets2 = ets2_telemetry_reader or Ets2TelemetryReader()
    resolved_set_autostart = set_autostart_fn or set_autostart
    resolved_get_autostart = get_autostart_fn or is_autostart_enabled
    resolved_ip = host_ip or get_lan_ip()

    ctx = AppContext(
        data_dir=resolved_data,
        web_dir=resolved_web,
        input_backend=resolved_backend,
        audio_backend=resolved_audio,
        audio_mixer=resolved_mixer,
        sound_player=resolved_sound,
        obs_backend=resolved_obs,
        launch_app_fn=launch_app_fn,
        on_focus_window=on_focus_window,
        set_autostart_fn=resolved_set_autostart,
        get_autostart_fn=resolved_get_autostart,
        host_ip=resolved_ip,
        port=port,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        ctx.loop = asyncio.get_running_loop()
        if enable_window_watcher:
            ctx.window_watcher = WindowWatcher(ctx.on_foreground_change)
            ctx.window_watcher.start()
        if enable_audio_meter:
            ctx.audio_watcher = AudioMeterWatcher(
                backend=resolved_audio,
                should_poll=ctx.should_poll_audio,
                on_level=ctx.on_audio_level,
                get_target_exe=ctx.get_audio_target_exe,
            )
            ctx.audio_watcher.start()
        if enable_live_state:
            ctx.live_watcher = LiveStateWatcher(
                should_poll=ctx.should_poll_live,
                get_active_page=ctx.deck_manager.get_active_page,
                get_obs_settings=ctx._get_obs_settings,
                obs_backend=resolved_obs,
                mixer_backend=resolved_mixer,
                system_stats=resolved_stats,
                ets2_reader=resolved_ets2,
                on_live_state=ctx.on_live_state,
                poll_interval=live_state_poll_interval,
            )
            ctx.live_watcher.start()
        try:
            yield
        finally:
            if ctx.live_watcher is not None:
                ctx.live_watcher.stop()
            if ctx.audio_watcher is not None:
                ctx.audio_watcher.stop()
            if ctx.window_watcher is not None:
                ctx.window_watcher.stop()
            ctx.executor.release_all()

    app = FastAPI(title="LocalDeck", version=__version__, lifespan=lifespan)
    app.state.ctx = ctx

    @app.middleware("http")
    async def no_stale_cache(request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path in ("/", "/desktop") or path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    static_dir = resolved_web / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    def require_desktop_auth(request: Request) -> None:
        host = request.client.host if request.client else None
        if not is_loopback(host):
            raise HTTPException(status_code=403, detail="Desktop API is restricted to localhost")
        if not _host_header_is_loopback(request.headers.get("host")):
            raise HTTPException(status_code=403, detail="Invalid host header")
        token = request.headers.get("X-LocalDeck-Desktop-Token", "")
        if not hmac.compare_digest(token, ctx.auth_manager.desktop_token):
            raise HTTPException(status_code=401, detail="Invalid desktop token")

    def _render_pair_gate(expired: bool = False, status_code: int = 200) -> HTMLResponse:
        settings = ctx.deck_manager.get_config().settings
        lang = settings.language if settings.language in _PAIR_GATE_TEXT else "en"
        copy = _PAIR_GATE_TEXT[lang]
        title = copy["expired_title"] if expired else copy["pair_title"]
        body = copy["expired_body"] if expired else copy["pair_body"]
        template = (ctx.web_dir / "pair_gate.html").read_text(encoding="utf-8")
        html = (
            template.replace("__LANG__", lang)
            .replace("__THEME__", settings.theme)
            .replace("__CONTRAST__", "high" if settings.high_contrast else "normal")
            .replace("__TITLE__", title)
            .replace("__BODY__", body)
            .replace("__HINT__", copy["hint"])
        )
        return HTMLResponse(content=html, status_code=status_code)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/manifest.webmanifest")
    async def manifest() -> Response:
        payload = {
            "name": "LocalDeck",
            "short_name": "LocalDeck",
            "start_url": "/",
            "display": "standalone",
            "background_color": "#1C1B19",
            "theme_color": "#245C46",
            "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}],
        }
        return JSONResponse(content=payload, media_type="application/manifest+json")

    @app.get("/icon.svg")
    async def icon_svg() -> Response:
        return Response(content=LOGO_SVG, media_type="image/svg+xml")

    @app.get("/desktop", response_class=HTMLResponse)
    async def desktop_page(request: Request) -> HTMLResponse:
        host = request.client.host if request.client else None
        if not is_loopback(host):
            raise HTTPException(status_code=403, detail="Desktop UI is restricted to localhost")
        if not _host_header_is_loopback(request.headers.get("host")):
            raise HTTPException(status_code=403, detail="Invalid host header")
        settings = ctx.deck_manager.get_config().settings
        html = (ctx.web_dir / "desktop.html").read_text(encoding="utf-8")
        html = (
            html.replace("__LANG__", settings.language)
            .replace("__THEME__", settings.theme)
            .replace("__CONTRAST__", "high" if settings.high_contrast else "normal")
            .replace("__ASSET_V__", ctx.asset_version)
        )
        token_script = f"<script>window.__LOCALDECK_TOKEN__ = {json.dumps(ctx.auth_manager.desktop_token)};</script>"
        html = html.replace("<!--__DESKTOP_TOKEN__-->", token_script)
        return HTMLResponse(content=html)

    @app.get("/", response_class=HTMLResponse)
    async def mobile_home(request: Request) -> HTMLResponse:
        cookie = request.cookies.get(AuthManager.COOKIE_NAME)
        session = ctx.auth_manager.validate_session_cookie(cookie)
        if session is None:
            return _render_pair_gate(expired=False, status_code=200)
        settings = ctx.deck_manager.get_config().settings
        html = (ctx.web_dir / "mobile.html").read_text(encoding="utf-8")
        asset_script = f"<script>window.__LOCALDECK_ASSET_V__ = {json.dumps(ctx.asset_version)};</script>"
        html = (
            html.replace("__LANG__", settings.language)
            .replace("__THEME__", settings.theme)
            .replace("__CONTRAST__", "high" if settings.high_contrast else "normal")
            .replace("__ASSET_V__", ctx.asset_version)
            .replace("<!--__MOBILE_META__-->", asset_script)
        )
        return HTMLResponse(content=html)

    @app.get("/pair")
    async def pair_device(request: Request, t: str = "") -> Response:
        client_ip = request.client.host if request.client else "unknown"
        user_agent = request.headers.get("User-Agent", "")
        redeemed = ctx.auth_manager.redeem_pairing_token(t, user_agent=user_agent, client_ip=client_ip)
        if redeemed is None:
            return _render_pair_gate(expired=True, status_code=401)
        session, raw_cookie = redeemed
        response = RedirectResponse(url="/", status_code=302)
        response.set_cookie(
            key=AuthManager.COOKIE_NAME,
            value=raw_cookie,
            httponly=True,
            samesite="lax",
            path="/",
            max_age=60 * 60 * 24 * 30,
        )
        await ctx.broadcast_desktop(
            {
                "type": "pairing_consumed",
                "device_name": session.device_name,
                "pairing": ctx.pairing_payload(force_refresh=True),
                "devices": ctx.devices_payload(),
                "connected_count": len(ctx.mobile_clients),
            }
        )
        return response

    @app.get("/api/desktop/state", dependencies=[Depends(require_desktop_auth)])
    async def get_desktop_state() -> dict[str, Any]:
        return ctx.desktop_state_payload()

    @app.post("/api/desktop/focus", dependencies=[Depends(require_desktop_auth)])
    async def focus_desktop_window() -> dict[str, bool]:
        if ctx.on_focus_window is not None:
            ctx.on_focus_window()
        return {"ok": True}

    @app.get("/api/desktop/audio-sources", dependencies=[Depends(require_desktop_auth)])
    async def get_audio_sources() -> dict[str, list[str]]:
        return {"sources": ctx.audio_backend.list_audio_apps()}

    @app.get("/api/desktop/mixer", dependencies=[Depends(require_desktop_auth)])
    async def get_mixer() -> dict[str, Any]:
        return ctx.audio_mixer.get_mixer_state()

    @app.post("/api/desktop/mixer/volume", dependencies=[Depends(require_desktop_auth)])
    async def set_mixer_volume(req: MixerVolumeRequest) -> dict[str, Any]:
        mixer_state = ctx.audio_mixer.set_volume(req.exe, req.volume)
        await ctx.broadcast_mixer_state(mixer_state)
        return mixer_state

    @app.post("/api/desktop/mixer/mute", dependencies=[Depends(require_desktop_auth)])
    async def set_mixer_mute(req: MixerMuteRequest) -> dict[str, Any]:
        mixer_state = ctx.audio_mixer.set_mute(req.exe, req.muted)
        ctx.trigger_live_refresh()
        await ctx.broadcast_mixer_state(mixer_state)
        return mixer_state

    @app.get("/api/desktop/sounds", dependencies=[Depends(require_desktop_auth)])
    async def get_sounds() -> dict[str, list[str]]:
        return {"sounds": ctx.sound_library.list_sounds()}

    @app.post("/api/desktop/sounds/upload", dependencies=[Depends(require_desktop_auth)])
    async def upload_sound(req: SoundUploadRequest) -> dict[str, Any]:
        raw_b64 = req.data_base64.split(",", 1)[-1].strip()
        if len(raw_b64) > MAX_SOUND_BYTES * 2:
            raise HTTPException(status_code=400, detail="Sound file exceeds 10 MB limit")
        try:
            data = base64.b64decode(raw_b64, validate=True)
            saved = ctx.sound_library.save_sound(req.filename, data)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"filename": saved, "sounds": ctx.sound_library.list_sounds()}

    @app.get("/api/desktop/detected-apps", dependencies=[Depends(require_desktop_auth)])
    async def get_detected_apps() -> dict[str, list[dict[str, str]]]:
        return {"apps": detect_installed_apps()}

    @app.get("/api/desktop/obs/scenes", dependencies=[Depends(require_desktop_auth)])
    async def get_obs_scenes() -> dict[str, Any]:
        port_val, pw = ctx._get_obs_settings()
        return ctx.obs_backend.fetch_resources(port=port_val, password=pw)

    @app.post("/api/desktop/pairing/refresh", dependencies=[Depends(require_desktop_auth)])
    async def refresh_pairing() -> dict[str, Any]:
        return ctx.pairing_payload(force_refresh=True)

    @app.put("/api/desktop/settings", dependencies=[Depends(require_desktop_auth)])
    async def update_settings(patch: SettingsPatch) -> dict[str, Any]:
        updates = patch.model_dump(exclude_none=True)
        if "start_with_windows" in updates:
            updates["start_with_windows"] = bool(ctx.set_autostart_fn(bool(updates["start_with_windows"])))
        if "active_page_id" in updates:
            ctx.auto_switched_from_general = False
        ctx.deck_manager.update_settings(updates)
        await ctx.broadcast_state_sync()
        return ctx.desktop_state_payload()

    @app.post("/api/desktop/pages", dependencies=[Depends(require_desktop_auth)])
    async def create_page(page: DeckPage) -> dict[str, Any]:
        ctx.deck_manager.upsert_page(page)
        await ctx.broadcast_state_sync()
        return ctx.desktop_state_payload()

    @app.get("/api/desktop/templates", dependencies=[Depends(require_desktop_auth)])
    async def get_templates() -> dict[str, Any]:
        return {"templates": list_templates()}

    @app.post("/api/desktop/templates/{template_id}/install", dependencies=[Depends(require_desktop_auth)])
    async def install_template(template_id: str) -> dict[str, Any]:
        page = build_template_page(template_id)
        if page is None:
            raise HTTPException(status_code=404, detail="Template not found")
        ctx.deck_manager.import_page(page)
        await ctx.broadcast_state_sync()
        return ctx.desktop_state_payload()

    @app.get("/api/desktop/pages/{page_id}/export", dependencies=[Depends(require_desktop_auth)])
    async def export_page(page_id: str) -> dict[str, Any]:
        page = ctx.deck_manager.get_page(page_id)
        if page is None:
            raise HTTPException(status_code=404, detail="Page not found")
        return page.model_dump(mode="json")

    @app.post("/api/desktop/pages/import", dependencies=[Depends(require_desktop_auth)])
    async def import_page(page: DeckPage) -> dict[str, Any]:
        ctx.deck_manager.import_page(page)
        await ctx.broadcast_state_sync()
        return ctx.desktop_state_payload()

    @app.put("/api/desktop/pages/{page_id}", dependencies=[Depends(require_desktop_auth)])
    async def update_page(page_id: str, page: DeckPage) -> dict[str, Any]:
        fixed_page = page.model_copy(update={"id": page_id})
        ctx.deck_manager.upsert_page(fixed_page)
        await ctx.broadcast_state_sync()
        return ctx.desktop_state_payload()

    @app.delete("/api/desktop/pages/{page_id}", dependencies=[Depends(require_desktop_auth)])
    async def delete_page(page_id: str) -> dict[str, Any]:
        try:
            ctx.deck_manager.delete_page(page_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        await ctx.broadcast_state_sync()
        return ctx.desktop_state_payload()

    @app.put("/api/desktop/pages/{page_id}/buttons/{button_id}", dependencies=[Depends(require_desktop_auth)])
    async def upsert_button(page_id: str, button_id: str, button: DeckButton) -> dict[str, Any]:
        fixed_btn = button.model_copy(update={"id": button_id})
        try:
            ctx.deck_manager.upsert_button(page_id, fixed_btn)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await ctx.broadcast_state_sync()
        return ctx.desktop_state_payload()

    @app.delete("/api/desktop/pages/{page_id}/buttons/{button_id}", dependencies=[Depends(require_desktop_auth)])
    async def delete_button(page_id: str, button_id: str) -> dict[str, Any]:
        try:
            ctx.deck_manager.delete_button(page_id, button_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        await ctx.broadcast_state_sync()
        return ctx.desktop_state_payload()

    @app.post("/api/desktop/pages/{page_id}/swap", dependencies=[Depends(require_desktop_auth)])
    async def swap_buttons(page_id: str, req: SwapRequest) -> dict[str, Any]:
        try:
            ctx.deck_manager.swap_buttons(
                page_id,
                from_row=req.from_row,
                from_col=req.from_col,
                to_row=req.to_row,
                to_col=req.to_col,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await ctx.broadcast_state_sync()
        return ctx.desktop_state_payload()

    @app.post("/api/desktop/pages/{page_id}/test-button/{button_id}", dependencies=[Depends(require_desktop_auth)])
    async def test_button(page_id: str, button_id: str) -> dict[str, Any]:
        page = ctx.deck_manager.get_page(page_id)
        if page is None:
            raise HTTPException(status_code=404, detail="Page not found")
        btn = next((b for b in page.buttons if b.id == button_id), None)
        if btn is None:
            raise HTTPException(status_code=404, detail="Button not found")
        try:
            result = ctx.executor.execute_tap(btn.action, owner_id="desktop_test")
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        if btn.action.type == "obs_action" or (
            btn.action.type == "media" and btn.action.media_action == "mute"
        ):
            ctx.trigger_live_refresh()
        if "switched_page_id" in result:
            await ctx.broadcast_state_sync()
        return {"ok": True, "result": result}

    @app.delete("/api/desktop/devices/{session_id}", dependencies=[Depends(require_desktop_auth)])
    async def revoke_device(session_id: str) -> dict[str, Any]:
        removed = ctx.auth_manager.revoke_session(session_id)
        if not removed:
            raise HTTPException(status_code=404, detail="Device session not found")
        to_close = [
            (cid, ws) for cid, (sid, ws) in list(ctx.mobile_clients.items()) if sid == session_id
        ]
        for cid, ws in to_close:
            ctx.mobile_clients.pop(cid, None)
            ctx.executor.release_owner(cid)
            try:
                await ws.close(code=4403)
            except Exception:
                pass
        await ctx.broadcast_devices_updated()
        return ctx.desktop_state_payload()

    @app.websocket("/ws/desktop")
    async def ws_desktop(websocket: WebSocket) -> None:
        host = websocket.client.host if websocket.client else None
        token = websocket.query_params.get("token", "")
        if (
            not is_loopback(host)
            or not _host_header_is_loopback(websocket.headers.get("host"))
            or not hmac.compare_digest(token, ctx.auth_manager.desktop_token)
        ):
            await websocket.accept()
            await websocket.close(code=4401)
            return
        await websocket.accept()
        ctx.desktop_clients.add(websocket)
        try:
            await websocket.send_json({"type": "state_sync", "state": ctx.desktop_state_payload()})
            if enable_live_state and ctx.last_live_state:
                await websocket.send_json({"type": "live_state", "live": ctx.last_live_state})
            ctx.trigger_live_refresh()
            while True:
                raw = await websocket.receive_text()
                if raw == "ping":
                    await websocket.send_text("pong")
        except WebSocketDisconnect:
            pass
        finally:
            ctx.desktop_clients.discard(websocket)

    @app.websocket("/ws/deck")
    async def ws_deck(websocket: WebSocket) -> None:
        origin = websocket.headers.get("origin")
        if origin:
            if urlparse(origin).netloc.lower() != (websocket.headers.get("host") or "").lower():
                await websocket.accept()
                await websocket.close(code=4403)
                return
        cookie = websocket.cookies.get(AuthManager.COOKIE_NAME)
        session = ctx.auth_manager.validate_session_cookie(cookie)
        if session is None:
            await websocket.accept()
            await websocket.close(code=4401)
            return
        await websocket.accept()
        client_id = f"{session.session_id}:{secrets.token_hex(4)}"
        ctx.mobile_clients[client_id] = (session.session_id, websocket)
        await ctx.broadcast_devices_updated()
        try:
            await websocket.send_json({"type": "state_sync", "state": ctx.mobile_state_payload()})
            if enable_live_state and ctx.last_live_state:
                await websocket.send_json({"type": "live_state", "live": ctx.last_live_state})
            ctx.trigger_live_refresh()
            while True:
                msg = await websocket.receive_json()
                msg_type = msg.get("type")
                if msg_type == "ping":
                    await websocket.send_json({"type": "pong", "ts": msg.get("ts")})
                    continue
                if msg_type == "get_mixer":
                    await websocket.send_json({"type": "mixer_state", "mixer": ctx.audio_mixer.get_mixer_state()})
                    continue
                if msg_type == "set_volume":
                    try:
                        vol = max(0.0, min(1.0, float(msg.get("volume", 0.5))))
                    except (TypeError, ValueError):
                        continue
                    exe = msg.get("exe")
                    mixer_state = ctx.audio_mixer.set_volume(str(exe) if exe is not None else None, vol)
                    await ctx.broadcast_mixer_state(mixer_state)
                    continue
                if msg_type == "set_mute":
                    exe = msg.get("exe")
                    muted = bool(msg.get("muted", False))
                    mixer_state = ctx.audio_mixer.set_mute(str(exe) if exe is not None else None, muted)
                    ctx.trigger_live_refresh()
                    await ctx.broadcast_mixer_state(mixer_state)
                    continue
                if msg_type == "select_page":
                    page_id = str(msg.get("page_id") or "")
                    if ctx.deck_manager.get_page(page_id) is not None:
                        ctx.deck_manager.set_active_page(page_id)
                        ctx.auto_switched_from_general = False
                        ctx.trigger_live_refresh()
                        await ctx.broadcast_state_sync()
                    continue
                if msg_type in ("tap", "hold_start", "hold_end"):
                    page_id = str(msg.get("page_id") or "")
                    button_id = str(msg.get("button_id") or "")
                    page = ctx.deck_manager.get_page(page_id)
                    if page is None:
                        continue
                    btn = next((b for b in page.buttons if b.id == button_id), None)
                    if btn is None:
                        continue
                    if msg_type == "tap":
                        try:
                            result = ctx.executor.execute_tap(btn.action, owner_id=client_id)
                            await websocket.send_json({"type": "ack", "button_id": button_id})
                            if btn.action.type == "obs_action" or (
                                btn.action.type == "media" and btn.action.media_action == "mute"
                            ):
                                ctx.trigger_live_refresh()
                            if "switched_page_id" in result:
                                ctx.trigger_live_refresh()
                                await ctx.broadcast_state_sync()
                        except Exception as exc:
                            await websocket.send_json(
                                {"type": "error", "button_id": button_id, "error": str(exc)}
                            )
                    elif msg_type == "hold_start":
                        ctx.executor.execute_hold_start(btn.action, owner_id=client_id)
                    elif msg_type == "hold_end":
                        ctx.executor.execute_hold_end(btn.action, owner_id=client_id)
        except WebSocketDisconnect:
            pass
        finally:
            ctx.mobile_clients.pop(client_id, None)
            ctx.executor.release_owner(client_id)
            await ctx.broadcast_devices_updated()

    return app
