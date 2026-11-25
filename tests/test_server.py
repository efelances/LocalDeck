"""Integration tests for FastAPI routes, QR pairing, desktop API, and WebSockets."""

from __future__ import annotations

import base64
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from localdeck.core.audio_meter import NoopAudioMeterBackend
from localdeck.core.audio_mixer import NoopAudioMixerBackend
from localdeck.core.auth import AuthManager
from localdeck.core.input_win import NoopInputBackend
from localdeck.core.obs_client import NoopObsBackend
from localdeck.core.sound_player import NoopSoundPlayer
from localdeck.core.telemetry import Ets2TelemetryReader, SystemStatsReader
from localdeck.server import create_app


def _make_client(tmp_path: Path):
    backend = NoopInputBackend()
    app = create_app(
        data_dir=tmp_path,
        input_backend=backend,
        enable_window_watcher=False,
        enable_live_state=False,
        host_ip="192.168.1.50",
        port=8765,
    )
    return app, backend


def test_unauthenticated_gates_and_desktop_auth(tmp_path: Path):
    app, _ = _make_client(tmp_path)
    with TestClient(app) as client:
        # Unauthenticated mobile home shows pairing gate, never deck buttons
        home = client.get("/")
        assert home.status_code == 200
        assert "LocalDeck" in home.text
        assert "mobileGrid" not in home.text

        # Desktop API requires desktop token
        assert client.get("/api/desktop/state").status_code == 401
        token = app.state.ctx.auth_manager.desktop_token
        ok_state = client.get("/api/desktop/state", headers={"X-LocalDeck-Desktop-Token": token})
        assert ok_state.status_code == 200

        # Unauthenticated /ws/deck closes with 4401
        try:
            with client.websocket_connect("/ws/deck") as ws:
                ws.receive_json()
                assert False, "Expected WebSocketDisconnect"
        except WebSocketDisconnect as exc:
            assert exc.code == 4401


def test_full_pairing_websocket_settings_swap_and_revoke(tmp_path: Path):
    app, backend = _make_client(tmp_path)
    token = app.state.ctx.auth_manager.desktop_token
    headers = {"X-LocalDeck-Desktop-Token": token}

    with TestClient(app) as desktop_client, TestClient(app) as mobile_client:
        # 1. Desktop fetches state and pairing QR URL
        state_resp = desktop_client.get("/api/desktop/state", headers=headers)
        assert state_resp.status_code == 200
        pairing_url = state_resp.json()["pairing"]["url"]
        parsed = urlparse(pairing_url)
        pair_token = parse_qs(parsed.query)["t"][0]

        # 2. Mobile redeems pairing token once -> receives HttpOnly session cookie
        pair_resp = mobile_client.get(f"/pair?t={pair_token}", follow_redirects=False)
        assert pair_resp.status_code == 302
        assert pair_resp.headers["location"] == "/"
        set_cookie = pair_resp.headers.get("set-cookie", "").lower()
        assert AuthManager.COOKIE_NAME in set_cookie
        assert "httponly" in set_cookie

        # Re-using the same token must fail with 401
        reuse_resp = desktop_client.get(f"/pair?t={pair_token}", follow_redirects=False)
        assert reuse_resp.status_code == 401

        # Paired mobile home renders the PWA deck
        mobile_home = mobile_client.get("/")
        assert mobile_home.status_code == 200
        assert "mobileGrid" in mobile_home.text

        # 3. Mobile connects /ws/deck -> receives state_sync -> executes tap, hold, ping, select_page
        with mobile_client.websocket_connect("/ws/deck") as ws:
            sync_msg = ws.receive_json()
            assert sync_msg["type"] == "state_sync"
            assert sync_msg["state"]["active_page_id"] == "general"
            assert "obs_password" not in sync_msg["state"]["settings"]
            assert "start_with_windows" not in sync_msg["state"]["settings"]

            ws.send_json({"type": "ping", "ts": 123})
            pong = ws.receive_json()
            assert pong == {"type": "pong", "ts": 123}

            ws.send_json({"type": "tap", "page_id": "general", "button_id": "gen-play"})
            ack = ws.receive_json()
            assert ack == {"type": "ack", "button_id": "gen-play"}
            assert ("tap_media", "play_pause") in backend.events

            ws.send_json({"type": "hold_start", "page_id": "cockpit", "button_id": "cockpit-ptt"})
            ws.send_json({"type": "ping", "ts": 124})
            ws.receive_json()
            assert len(backend.held_by_owner) == 1

            ws.send_json({"type": "hold_end", "page_id": "cockpit", "button_id": "cockpit-ptt"})
            ws.send_json({"type": "ping", "ts": 125})
            ws.receive_json()
            assert backend.held_by_owner == {}

            # 4. Desktop updates settings (language -> en, theme -> light, high_contrast -> True) -> mobile receives state_sync
            assert sync_msg["state"]["settings"]["theme"] == "dark"
            upd = desktop_client.put(
                "/api/desktop/settings",
                headers=headers,
                json={"language": "en", "theme": "light", "high_contrast": True},
            )
            assert upd.status_code == 200
            pushed = ws.receive_json()
            assert pushed["type"] == "state_sync"
            assert pushed["state"]["settings"]["language"] == "en"
            assert pushed["state"]["settings"]["theme"] == "light"
            assert pushed["state"]["settings"]["high_contrast"] is True

            # 5. Desktop swaps buttons on general page -> mobile receives updated state_sync
            swap_resp = desktop_client.post(
                "/api/desktop/pages/general/swap",
                headers=headers,
                json={"from_row": 0, "from_col": 0, "to_row": 0, "to_col": 1},
            )
            assert swap_resp.status_code == 200
            pushed_swap = ws.receive_json()
            assert pushed_swap["type"] == "state_sync"
            gen_page = next(p for p in pushed_swap["state"]["pages"] if p["id"] == "general")
            coords = {b["id"]: (b["row"], b["col"]) for b in gen_page["buttons"]}
            assert coords["gen-play"] == (0, 1)
            assert coords["gen-prev"] == (0, 0)

            # Hold key while revoking -> disconnect/revoke cleans up held keys and closes WS with 4403
            ws.send_json({"type": "hold_start", "page_id": "cockpit", "button_id": "cockpit-ptt"})
            ws.send_json({"type": "ping", "ts": 126})
            ws.receive_json()
            assert len(backend.held_by_owner) == 1

            sessions = desktop_client.get("/api/desktop/state", headers=headers).json()["devices"]
            session_id = sessions[0]["session_id"]
            rev_resp = desktop_client.delete(f"/api/desktop/devices/{session_id}", headers=headers)
            assert rev_resp.status_code == 200

            try:
                ws.receive_json()
                assert False, "Expected WebSocketDisconnect after revoke"
            except WebSocketDisconnect as exc:
                assert exc.code == 4403

        assert backend.held_by_owner == {}


def test_templates_skin_sync_and_json_import_export(tmp_path: Path):
    app, _ = _make_client(tmp_path)
    token = app.state.ctx.auth_manager.desktop_token
    headers = {"X-LocalDeck-Desktop-Token": token}

    with TestClient(app) as desktop_client, TestClient(app) as mobile_client:
        # Pair mobile client
        pairing_url = desktop_client.get("/api/desktop/state", headers=headers).json()["pairing"]["url"]
        pair_token = parse_qs(urlparse(pairing_url).query)["t"][0]
        assert mobile_client.get(f"/pair?t={pair_token}", follow_redirects=False).status_code == 302

        with mobile_client.websocket_connect("/ws/deck") as ws:
            ws.receive_json()  # initial state_sync

            # List templates & install ets2 + minecraft
            tpl_res = desktop_client.get("/api/desktop/templates", headers=headers)
            assert tpl_res.status_code == 200
            assert len(tpl_res.json()["templates"]) == 7

            ets_res = desktop_client.post("/api/desktop/templates/ets2/install", headers=headers)
            assert ets_res.status_code == 200
            sync_ets = ws.receive_json()
            assert sync_ets["state"]["active_page_id"] == "ets2"
            ets_page = next(p for p in sync_ets["state"]["pages"] if p["id"] == "ets2")
            assert ets_page["skin"] == "truck"

            mc_res = desktop_client.post("/api/desktop/templates/minecraft/install", headers=headers)
            assert mc_res.status_code == 200
            sync_mc = ws.receive_json()
            assert sync_mc["state"]["active_page_id"] == "minecraft"
            mc_page = next(p for p in sync_mc["state"]["pages"] if p["id"] == "minecraft")
            assert mc_page["skin"] == "blocky"

            # Export and re-import (collision generates unique id)
            exported = desktop_client.get("/api/desktop/pages/minecraft/export", headers=headers)
            assert exported.status_code == 200
            imp = desktop_client.post("/api/desktop/pages/import", headers=headers, json=exported.json())
            assert imp.status_code == 200
            assert imp.json()["active_page_id"] == "minecraft-2"
            ws.receive_json()

            # Unsafe URL in imported page must be rejected by Pydantic validation
            bad_page = exported.json()
            bad_page["buttons"][0]["action"] = {"type": "open_url", "url": "javascript:alert(1)"}
            bad_imp = desktop_client.post("/api/desktop/pages/import", headers=headers, json=bad_page)
            assert bad_imp.status_code == 422

        # Blocky underground strata SVG asset must be served
        strata = desktop_client.get("/static/skin-blocky-strata.svg")
        assert strata.status_code == 200
        assert "<svg" in strata.text


def test_audio_level_broadcasts_on_audio_skin(tmp_path: Path):
    backend = NoopInputBackend()
    audio_backend = NoopAudioMeterBackend(
        levels=(0.018, 0.016, 0.018),
        per_app_levels={"spotify.exe": (0.42, 0.40, 0.44), "discord.exe": (0.09, 0.09, 0.09)},
    )
    app = create_app(
        data_dir=tmp_path,
        input_backend=backend,
        enable_window_watcher=False,
        enable_audio_meter=True,
        enable_live_state=False,
        audio_meter_backend=audio_backend,
        host_ip="192.168.1.50",
        port=8765,
    )
    token = app.state.ctx.auth_manager.desktop_token
    headers = {"X-LocalDeck-Desktop-Token": token}

    with TestClient(app) as desktop_client, TestClient(app) as mobile_client:
        desk_html = desktop_client.get("/desktop")
        assert desk_html.status_code == 200
        assert desk_html.headers.get("cache-control") == "no-store"

        js_resp = desktop_client.get("/static/audio_viz.js")
        assert js_resp.status_code == 200
        assert js_resp.headers.get("cache-control") == "no-store"

        sources_resp = desktop_client.get("/api/desktop/audio-sources", headers=headers)
        assert sources_resp.status_code == 200
        assert sources_resp.json()["sources"] == ["discord.exe", "spotify.exe"]

        pairing_url = desktop_client.get("/api/desktop/state", headers=headers).json()["pairing"]["url"]
        pair_token = parse_qs(urlparse(pairing_url).query)["t"][0]
        assert mobile_client.get(f"/pair?t={pair_token}", follow_redirects=False).status_code == 302

        home = mobile_client.get("/")
        assert home.status_code == 200
        assert home.headers.get("cache-control") == "no-store"

        with mobile_client.websocket_connect("/ws/deck") as ws:
            ws.receive_json()  # initial state_sync (general page)
            # Install spotify template (skin='audio') -> triggers state_sync then audio_level
            assert desktop_client.post("/api/desktop/templates/spotify/install", headers=headers).status_code == 200
            seen_audio = None
            for _ in range(10):
                msg = ws.receive_json()
                if msg.get("type") == "audio_level":
                    seen_audio = msg
                    break
            assert seen_audio is not None
            assert seen_audio["peak"] == 0.018

            # Switch page audio source to spotify.exe + rainbow visualizer -> receives spotify.exe levels
            spotify_page = desktop_client.get("/api/desktop/pages/spotify/export", headers=headers).json()
            spotify_page["audio_source_exe"] = "spotify.exe"
            spotify_page["audio_viz_mode"] = "rainbow"
            assert desktop_client.put("/api/desktop/pages/spotify", headers=headers, json=spotify_page).status_code == 200

            seen_mode = None
            seen_sp_audio = None
            for _ in range(20):
                msg = ws.receive_json()
                if msg.get("type") == "state_sync":
                    sp = next(p for p in msg["state"]["pages"] if p["id"] == "spotify")
                    seen_mode = sp["audio_viz_mode"]
                elif msg.get("type") == "audio_level" and msg.get("peak") == 0.42:
                    seen_sp_audio = msg
                    break
            assert seen_mode == "rainbow"
            assert seen_sp_audio is not None
            assert seen_sp_audio["left"] == 0.4
            assert seen_sp_audio["right"] == 0.44


def test_soundboard_launch_app_obs_and_any_page_audio_viz(tmp_path: Path):
    backend = NoopInputBackend()
    audio_backend = NoopAudioMeterBackend(levels=(0.25, 0.24, 0.26))
    sound_player = NoopSoundPlayer()
    obs_backend = NoopObsBackend()
    launched_apps: list[str] = []

    app = create_app(
        data_dir=tmp_path,
        input_backend=backend,
        enable_window_watcher=False,
        enable_audio_meter=True,
        enable_live_state=False,
        audio_meter_backend=audio_backend,
        sound_player=sound_player,
        launch_app_fn=launched_apps.append,
        obs_backend=obs_backend,
        host_ip="192.168.1.50",
        port=8765,
    )
    token = app.state.ctx.auth_manager.desktop_token
    headers = {"X-LocalDeck-Desktop-Token": token}

    with TestClient(app) as desktop_client, TestClient(app) as mobile_client:
        # 1. Upload sound clip
        b64_wav = base64.b64encode(b"RIFF0000WAVE").decode("ascii")
        up_res = desktop_client.post(
            "/api/desktop/sounds/upload",
            headers=headers,
            json={"filename": "ding.wav", "data_base64": f"data:audio/wav;base64,{b64_wav}"},
        )
        assert up_res.status_code == 200
        clip_name = up_res.json()["filename"]
        assert clip_name in desktop_client.get("/api/desktop/sounds", headers=headers).json()["sounds"]

        # 2. Detected apps & OBS scenes
        assert desktop_client.get("/api/desktop/detected-apps", headers=headers).status_code == 200
        obs_res = desktop_client.get("/api/desktop/obs/scenes", headers=headers)
        assert obs_res.status_code == 200
        assert obs_res.json()["connected"] is True
        assert "Gameplay" in obs_res.json()["scenes"]

        # 3. Add play_sound, launch_app, obs_action buttons on general page AND enable audio_viz_enabled on general
        general = desktop_client.get("/api/desktop/pages/general/export", headers=headers).json()
        general["audio_viz_enabled"] = True
        general["audio_viz_mode"] = "neon"
        assert desktop_client.put("/api/desktop/pages/general", headers=headers, json=general).status_code == 200

        for btn_id, col, action in (
            ("b-snd", 0, {"type": "play_sound", "sound_file": clip_name}),
            ("b-app", 1, {"type": "launch_app", "app_path": r"C:\Apps\Spotify.exe"}),
            ("b-obs", 2, {"type": "obs_action", "obs_command": "switch_scene", "obs_scene": "Gameplay"}),
        ):
            r = desktop_client.put(
                f"/api/desktop/pages/general/buttons/{btn_id}",
                headers=headers,
                json={"id": btn_id, "row": 0, "col": col, "label": btn_id, "action": action},
            )
            assert r.status_code == 200

        # 4. Pair mobile and trigger buttons + verify audio_level works on non-audio skin when audio_viz_enabled=True
        pairing_url = desktop_client.get("/api/desktop/state", headers=headers).json()["pairing"]["url"]
        pair_token = parse_qs(urlparse(pairing_url).query)["t"][0]
        assert mobile_client.get(f"/pair?t={pair_token}", follow_redirects=False).status_code == 302

        with mobile_client.websocket_connect("/ws/deck") as ws:
            sync_msg = ws.receive_json()
            gen = next(p for p in sync_msg["state"]["pages"] if p["id"] == "general")
            assert gen["audio_viz_enabled"] is True
            assert gen["audio_viz_mode"] == "neon"

            for btn_id in ("b-snd", "b-app", "b-obs"):
                ws.send_json({"type": "tap", "page_id": "general", "button_id": btn_id})
                while True:
                    m = ws.receive_json()
                    if m.get("type") == "ack" and m.get("button_id") == btn_id:
                        break

            assert len(sound_player.played) == 1
            assert launched_apps == [r"C:\Apps\Spotify.exe"]
            assert obs_backend.calls[-1]["command"] == "switch_scene"
            assert obs_backend.calls[-1]["scene"] == "Gameplay"

            seen_audio = False
            for _ in range(15):
                m = ws.receive_json()
                if m.get("type") == "audio_level" and m.get("peak") == 0.25:
                    seen_audio = True
                    break
            assert seen_audio is True


def test_desktop_focus_endpoint(tmp_path: Path):
    focused: list[bool] = []
    app = create_app(
        data_dir=tmp_path,
        input_backend=NoopInputBackend(),
        enable_window_watcher=False,
        enable_audio_meter=False,
        on_focus_window=lambda: focused.append(True),
        host_ip="192.168.1.50",
        port=8765,
    )
    token = app.state.ctx.auth_manager.desktop_token
    with TestClient(app) as client:
        assert client.post("/api/desktop/focus").status_code == 401
        res = client.post("/api/desktop/focus", headers={"X-LocalDeck-Desktop-Token": token})
        assert res.status_code == 200
        assert res.json() == {"ok": True}
        assert focused == [True]


class _FakeStatsReader(SystemStatsReader):
    def read(self) -> dict[str, int]:
        return {"cpu_pct": 37, "ram_pct": 64}


class _FakeEts2Reader(Ets2TelemetryReader):
    def read(self):
        return {
            "connected": True,
            "speed_kmh": 84,
            "gear": "D11",
            "rpm": 1280,
            "fuel_pct": 62,
            "cruise_kmh": 85,
            "park_brake": False,
        }


def test_v2_mixer_autostart_widgets_and_live_state(tmp_path: Path):
    mixer = NoopAudioMixerBackend(master_volume=0.6, master_muted=False)
    obs = NoopObsBackend()
    autostart_flag = [False]

    def _set_auto(val: bool) -> bool:
        autostart_flag[0] = bool(val)
        return autostart_flag[0]

    app = create_app(
        data_dir=tmp_path,
        input_backend=NoopInputBackend(),
        enable_window_watcher=False,
        enable_audio_meter=False,
        audio_mixer_backend=mixer,
        obs_backend=obs,
        enable_live_state=True,
        live_state_poll_interval=0.05,
        system_stats_reader=_FakeStatsReader(),
        ets2_telemetry_reader=_FakeEts2Reader(),
        set_autostart_fn=_set_auto,
        get_autostart_fn=lambda: autostart_flag[0],
        host_ip="192.168.1.50",
        port=8765,
    )
    token = app.state.ctx.auth_manager.desktop_token
    headers = {"X-LocalDeck-Desktop-Token": token}

    with TestClient(app) as desktop_client, TestClient(app) as mobile_client:
        # 1. Start with Windows setting hook + Desktop mixer REST endpoints
        s_res = desktop_client.put(
            "/api/desktop/settings",
            headers=headers,
            json={"start_with_windows": True},
        )
        assert s_res.status_code == 200
        assert autostart_flag[0] is True
        assert s_res.json()["config"]["settings"]["start_with_windows"] is True

        m_res = desktop_client.get("/api/desktop/mixer", headers=headers)
        assert m_res.status_code == 200
        assert m_res.json()["master"]["volume"] == 0.6

        v_res = desktop_client.post(
            "/api/desktop/mixer/volume",
            headers=headers,
            json={"exe": "spotify.exe", "volume": 0.35},
        )
        assert v_res.status_code == 200
        sp = next(a for a in v_res.json()["apps"] if a["exe"] == "spotify.exe")
        assert sp["volume"] == 0.35

        # 2. Place a 1x2 wide cpu_ram widget + obs record toggle + ets2_speed widget on general
        assert desktop_client.put(
            "/api/desktop/pages/general/buttons/w-cpu",
            headers=headers,
            json={
                "id": "w-cpu",
                "row": 0,
                "col": 0,
                "row_span": 1,
                "col_span": 2,
                "label": "Sistem",
                "action": {"type": "widget", "widget_kind": "cpu_ram"},
            },
        ).status_code == 200
        assert desktop_client.put(
            "/api/desktop/pages/general/buttons/obs-rec",
            headers=headers,
            json={
                "id": "obs-rec",
                "row": 0,
                "col": 2,
                "label": "Rec",
                "action": {"type": "obs_action", "obs_command": "toggle_record"},
            },
        ).status_code == 200
        assert desktop_client.put(
            "/api/desktop/pages/general/buttons/w-ets",
            headers=headers,
            json={
                "id": "w-ets",
                "row": 1,
                "col": 2,
                "label": "Hız",
                "action": {"type": "widget", "widget_kind": "ets2_speed"},
            },
        ).status_code == 200

        # 3. Pair mobile, connect WS, test get_mixer, set_volume, set_mute & OBS tap live_state
        pairing_url = desktop_client.get("/api/desktop/state", headers=headers).json()["pairing"]["url"]
        pair_token = parse_qs(urlparse(pairing_url).query)["t"][0]
        assert mobile_client.get(f"/pair?t={pair_token}", follow_redirects=False).status_code == 302

        with mobile_client.websocket_connect("/ws/deck") as ws:
            ws.receive_json()  # state_sync

            ws.send_json({"type": "get_mixer"})
            got_mixer = None
            for _ in range(10):
                msg = ws.receive_json()
                if msg.get("type") == "mixer_state":
                    got_mixer = msg["mixer"]
                    break
            assert got_mixer is not None

            ws.send_json({"type": "set_volume", "exe": None, "volume": 0.42})
            ws.send_json({"type": "set_mute", "exe": None, "muted": True})
            ws.send_json({"type": "tap", "page_id": "general", "button_id": "obs-rec"})

            live_snapshot = None
            for _ in range(25):
                msg = ws.receive_json()
                if msg.get("type") == "live_state" and msg["live"]["obs"].get("recording") and msg["live"]["master_muted"]:
                    live_snapshot = msg["live"]
                    break
            assert live_snapshot is not None
            assert live_snapshot["system"] == {"cpu_pct": 37, "ram_pct": 64}
            assert live_snapshot["ets2"]["speed_kmh"] == 84
            assert live_snapshot["ets2"]["gear"] == "D11"
            assert mixer.get_mixer_state()["master"] == {"volume": 0.42, "muted": True}


def test_host_header_and_origin_guards(tmp_path: Path):
    app, _ = _make_client(tmp_path)
    token = app.state.ctx.auth_manager.desktop_token
    headers = {"X-LocalDeck-Desktop-Token": token}

    with TestClient(app) as client, TestClient(app) as mobile_client:
        assert client.get("/api/desktop/state", headers={**headers, "Host": "evil.example:8765"}).status_code == 403
        assert client.get("/desktop", headers={"Host": "evil.example"}).status_code == 403

        ok = client.get("/api/desktop/state", headers=headers)
        assert ok.status_code == 200
        assert ok.headers.get("x-frame-options") == "DENY"
        assert ok.headers.get("x-content-type-options") == "nosniff"
        assert ok.headers.get("referrer-policy") == "no-referrer"

        pairing_url = ok.json()["pairing"]["url"]
        pair_token = parse_qs(urlparse(pairing_url).query)["t"][0]
        assert mobile_client.get(f"/pair?t={pair_token}", follow_redirects=False).status_code == 302

        try:
            with mobile_client.websocket_connect("/ws/deck", headers={"origin": "http://evil.example"}) as ws:
                ws.receive_json()
                assert False, "Expected WebSocketDisconnect"
        except WebSocketDisconnect as exc:
            assert exc.code == 4403
