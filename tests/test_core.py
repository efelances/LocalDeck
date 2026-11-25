"""Core unit tests for DeckManager, ActionExecutor, AuthManager, QR, and WindowsInputBackend."""

from __future__ import annotations

import ctypes
import struct
import sys
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from localdeck.core.actions import ActionExecutor
from localdeck.core.app_launcher import validate_safe_app_path
from localdeck.core.audio_meter import (
    AudioMeterWatcher,
    NoopAudioMeterBackend,
    WindowsAudioMeterBackend,
    list_windows_audio_apps,
)
from localdeck.core.audio_mixer import NoopAudioMixerBackend, WindowsAudioMixerBackend
from localdeck.core.auth import AuthManager
from localdeck.core.branding import LOGO_SVG, render_logo_image
from localdeck.core.deck_manager import DeckManager
from localdeck.core.input_win import INPUT, NoopInputBackend, WindowsInputBackend
from localdeck.core.live_state import LiveStateWatcher
from localdeck.core.obs_client import NoopObsBackend
from localdeck.core.qr import generate_qr_svg
from localdeck.core.single_instance import (
    SingleInstanceGuard,
    clear_runtime_info,
    write_runtime_info,
)
from localdeck.core.sound_player import NoopSoundPlayer, SoundLibrary
from localdeck.core.telemetry import (
    Ets2TelemetryReader,
    SystemStatsReader,
    parse_funbit_telemetry_json,
    parse_scs_shared_memory,
)
from localdeck.core.templates import build_template_page, list_templates
from localdeck.models import ButtonAction, DeckButton, DeckPage, MacroStep
from localdeck.tray import create_tray_image


def test_deck_manager_starter_persistence_and_crud(tmp_path: Path):
    dm = DeckManager(tmp_path)
    cfg = dm.get_config()
    assert [p.id for p in cfg.pages] == ["general", "productivity", "cockpit"]
    assert (tmp_path / "deck.json").exists()

    # Upsert custom page + button and verify atomic persistence across reload
    custom_page = DeckPage(
        id="sim",
        name="Sim Cockpit",
        rows=2,
        cols=3,
        match_executables=[r"C:\Games\eurotrucks2.exe"],
        match_title_contains=["Euro Truck"],
    )
    dm.upsert_page(custom_page)
    assert dm.get_page("sim").match_executables == ["eurotrucks2.exe"]

    btn = DeckButton(
        id="sim-horn",
        row=0,
        col=0,
        label="Horn",
        icon="bolt",
        action=ButtonAction(type="hold_key", keys=["H"]),
    )
    dm.upsert_button("sim", btn)

    reloaded = DeckManager(tmp_path)
    reloaded_page = reloaded.get_page("sim")
    assert reloaded_page is not None
    assert len(reloaded_page.buttons) == 1
    assert reloaded_page.buttons[0].action.keys == ["h"]

    # Window matching by exe and title substring
    assert reloaded.match_window("eurotrucks2.exe", "Driving") == "sim"
    assert reloaded.match_window("other.exe", "Euro Truck Simulator 2") == "sim"
    assert reloaded.match_window("notepad.exe", "Untitled") is None

    # Drag-and-drop swap: move to empty cell, then swap two occupied cells
    reloaded.swap_buttons("sim", 0, 0, 1, 2)
    moved = reloaded.get_page("sim").buttons[0]
    assert (moved.row, moved.col) == (1, 2)

    btn2 = DeckButton(
        id="sim-lights",
        row=0,
        col=1,
        label="Lights",
        action=ButtonAction(type="hotkey", keys=["l"]),
    )
    reloaded.upsert_button("sim", btn2)
    reloaded.swap_buttons("sim", 1, 2, 0, 1)
    by_id = {b.id: (b.row, b.col) for b in reloaded.get_page("sim").buttons}
    assert by_id["sim-horn"] == (0, 1)
    assert by_id["sim-lights"] == (1, 2)

    # Delete button and pages, forbidding deletion of last page
    reloaded.delete_button("sim", "sim-lights")
    assert len(reloaded.get_page("sim").buttons) == 1
    reloaded.delete_page("sim")
    reloaded.delete_page("productivity")
    reloaded.delete_page("cockpit")
    with pytest.raises(ValueError, match="last remaining page"):
        reloaded.delete_page("general")


def test_button_action_validation_and_execution():
    with pytest.raises(ValidationError):
        ButtonAction(type="hotkey", keys=["not_a_real_key"])

    for bad_url in ("javascript:alert(1)", "file:///C:/Windows/cmd.exe", "cmd:calc"):
        with pytest.raises(ValidationError):
            ButtonAction(type="open_url", url=bad_url)

    backend = NoopInputBackend()
    opened_urls: list[str] = []
    switched_pages: list[str] = []
    executor = ActionExecutor(
        input_backend=backend,
        on_switch_page=switched_pages.append,
        open_url_fn=opened_urls.append,
    )

    executor.execute_tap(ButtonAction(type="hotkey", keys=["CTRL", "C"]))
    executor.execute_tap(ButtonAction(type="media", media_action="play_pause"))
    executor.execute_tap(
        ButtonAction(
            type="macro",
            steps=[
                MacroStep(kind="hotkey", keys=["ctrl", "s"]),
                MacroStep(kind="delay", delay_ms=1),
                MacroStep(kind="media", media_action="next"),
            ],
        )
    )
    executor.execute_tap(ButtonAction(type="open_url", url="https://example.com/docs"))
    executor.execute_tap(ButtonAction(type="switch_page", target_page_id="cockpit"))

    assert ("tap_hotkey", ["ctrl", "c"]) in backend.events
    assert ("tap_media", "play_pause") in backend.events
    assert ("tap_hotkey", ["ctrl", "s"]) in backend.events
    assert ("tap_media", "next") in backend.events
    assert opened_urls == ["https://example.com/docs"]
    assert switched_pages == ["cockpit"]

    # Hold lifecycle + owner cleanup + release_all
    hold_action = ButtonAction(type="hold_key", keys=["v"])
    executor.execute_hold_start(hold_action, owner_id="client-1")
    assert backend.held_by_owner["client-1"] == ["v"]
    executor.release_owner("client-1")
    assert "client-1" not in backend.held_by_owner

    executor.execute_hold_start(hold_action, owner_id="client-2")
    executor.release_all()
    assert backend.held_by_owner == {}


def test_auth_manager_single_use_qr_and_session_lifecycle(tmp_path: Path):
    auth = AuthManager(tmp_path)
    pt = auth.create_pairing_token(ttl_seconds=300)
    assert pt.is_valid()

    redeemed = auth.redeem_pairing_token(pt.token, user_agent="Mozilla/5.0 (iPhone)", client_ip="192.168.1.44")
    assert redeemed is not None
    session, raw_cookie = redeemed
    assert session.device_name == "iPhone"

    # Second attempt on same token must fail
    assert auth.redeem_pairing_token(pt.token, user_agent="Android", client_ip="192.168.1.45") is None

    # Expired token must fail
    expired = auth.create_pairing_token(ttl_seconds=-1)
    assert auth.redeem_pairing_token(expired.token, user_agent="Android", client_ip="192.168.1.45") is None

    # Session persists across AuthManager reload and revokes cleanly
    reloaded = AuthManager(tmp_path)
    validated = reloaded.validate_session_cookie(raw_cookie)
    assert validated is not None
    assert validated.session_id == session.session_id
    assert reloaded.validate_session_cookie("bad.cookie") is None

    assert reloaded.revoke_session(session.session_id) is True
    assert reloaded.validate_session_cookie(raw_cookie) is None


def test_qr_svg_output():
    svg = generate_qr_svg("http://192.168.1.10:8765/pair?t=abc123")
    assert "<svg" in svg
    assert "#1C1B19" in svg


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only SendInput struct check")
def test_windows_sendinput_struct_and_scancodes():
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        assert ctypes.sizeof(INPUT) == 40
    backend = WindowsInputBackend()
    for key in ("a", "enter", "space", "ctrl", "up"):
        inp = backend._build_key_input(key, key_up=False)
        assert inp.ki.wScan != 0


def test_templates_skins_priority_and_tray_icon(tmp_path: Path):
    templates = list_templates()
    ids = [item["id"] for item in templates]
    assert ids == ["ets2", "minecraft", "discord", "spotify", "vscode", "obs", "fps_helper"]

    for tid in ids:
        page = build_template_page(tid)
        assert page is not None
        assert len(page.buttons) > 0

    assert build_template_page("ets2").skin == "truck"
    assert build_template_page("minecraft").skin == "blocky"
    assert build_template_page("obs").skin == "studio"
    assert build_template_page("fps_helper").skin == "tactical"
    assert build_template_page("spotify").skin == "audio"

    dm = DeckManager(tmp_path)
    vscode_page = dm.import_page(build_template_page("vscode"))
    mc_page = dm.import_page(build_template_page("minecraft"))

    # Newly installed vscode template takes priority over starter productivity page
    assert dm.match_window("code.exe", "main.py - Visual Studio Code") == vscode_page.id
    # Minecraft matches both Windows exe and Java window title
    assert dm.match_window("javaw.exe", "Minecraft 1.21.1 - Singleplayer") == mc_page.id

    img = create_tray_image()
    assert img.size == (64, 64)
    assert img.mode == "RGBA"

    assert "<svg" in LOGO_SVG
    assert "#1C1B19" in LOGO_SVG
    assert "#2A6E53" in LOGO_SVG
    logo_img = render_logo_image(64)
    assert logo_img.size == (64, 64)
    ico_path = Path(__file__).resolve().parent.parent / "localdeck" / "web" / "static" / "localdeck.ico"
    assert ico_path.is_file()
    assert ico_path.read_bytes()[:4] == b"\x00\x00\x01\x00"


def test_audio_meter_watcher_respects_should_poll_and_per_app_filter():
    backend = NoopAudioMeterBackend(
        levels=(0.55, 0.50, 0.60),
        per_app_levels={"spotify.exe": (0.31, 0.30, 0.31), "discord.exe": (0.12, 0.12, 0.12)},
    )
    assert backend.list_audio_apps() == ["discord.exe", "spotify.exe"]
    assert backend.read_levels("spotify.exe") == (0.31, 0.30, 0.31)
    assert backend.read_levels("unknown.exe") == (0.0, 0.0, 0.0)

    polling_enabled = False
    target_exe: str | None = None
    received: list[tuple[float, float, float]] = []

    watcher = AudioMeterWatcher(
        backend=backend,
        should_poll=lambda: polling_enabled,
        on_level=lambda p, l, r: received.append((p, l, r)),
        get_target_exe=lambda: target_exe,
        poll_interval=0.02,
    )
    watcher.start()
    try:
        time.sleep(0.08)
        assert received == []
        polling_enabled = True
        deadline = time.monotonic() + 1.0
        while not received and time.monotonic() < deadline:
            time.sleep(0.02)
        assert received[-1] == (0.55, 0.50, 0.60)

        target_exe = "spotify.exe"
        deadline = time.monotonic() + 1.0
        while received[-1] != (0.31, 0.30, 0.31) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert received[-1] == (0.31, 0.30, 0.31)
    finally:
        watcher.stop()
    assert backend.closed is True

    # DeckPage normalizes audio_source_exe and validates audio_viz_mode
    page = DeckPage(id="sp", name="Spotify", skin="audio", audio_source_exe=r"C:\Apps\Spotify.EXE", audio_viz_mode="neon")
    assert page.audio_source_exe == "spotify.exe"
    assert page.audio_viz_mode == "neon"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only WASAPI COM test")
def test_windows_audio_meter_backend_reads_and_closes():
    apps = list_windows_audio_apps()
    assert isinstance(apps, list)
    backend = WindowsAudioMeterBackend()
    try:
        peak, left, right = backend.read_levels()
        assert 0.0 <= peak <= 1.0
        assert 0.0 <= left <= 1.0
        assert 0.0 <= right <= 1.0
    finally:
        backend.close()


def test_soundboard_safe_launcher_obs_and_optional_audio_viz(tmp_path: Path):
    # SoundLibrary validation + atomic save
    lib = SoundLibrary(tmp_path)
    saved_wav = lib.save_sound("Air Horn!.wav", b"RIFF0000WAVE")
    assert saved_wav.endswith(".wav")
    assert lib.list_sounds() == [saved_wav]
    assert lib.resolve_sound(saved_wav).is_file()

    for bad_name in ("evil.exe", "script.bat", "foo.wav.exe"):
        with pytest.raises(ValueError):
            lib.save_sound(bad_name, b"data")
    with pytest.raises(ValueError):
        lib.resolve_sound("../escape.wav")
    with pytest.raises(ValueError):
        lib.save_sound("empty.wav", b"")

    # Safe app launcher blocks shell interpreters & scripts
    assert validate_safe_app_path(r"C:\Program Files\Spotify\Spotify.exe") == r"C:\Program Files\Spotify\Spotify.exe"
    assert validate_safe_app_path(r"C:\Links\App.lnk") == r"C:\Links\App.lnk"
    for blocked in (
        r"C:\Windows\System32\cmd.exe",
        "powershell.exe",
        "pwsh.exe",
        r"C:\Temp\evil.bat",
        r"C:\Temp\run.ps1",
        r'C:\App\foo.exe" --eval',
    ):
        with pytest.raises(ValueError):
            validate_safe_app_path(blocked)

    # ActionExecutor executes play_sound, launch_app, obs_action cleanly
    sound_player = NoopSoundPlayer()
    launched: list[str] = []
    obs = NoopObsBackend()
    executor = ActionExecutor(
        input_backend=NoopInputBackend(),
        sound_library=lib,
        sound_player=sound_player,
        launch_app_fn=launched.append,
        obs_backend=obs,
        get_obs_settings=lambda: (4455, "secret"),
    )

    executor.execute_tap(ButtonAction(type="play_sound", sound_file=saved_wav))
    assert len(sound_player.played) == 1
    assert sound_player.played[0].name == saved_wav

    executor.execute_tap(ButtonAction(type="launch_app", app_path=r"C:\Apps\OBS\obs64.exe"))
    assert launched == [r"C:\Apps\OBS\obs64.exe"]

    executor.execute_tap(ButtonAction(type="obs_action", obs_command="switch_scene", obs_scene="Gameplay"))
    assert obs.calls[-1] == {
        "command": "switch_scene",
        "scene": "Gameplay",
        "input_name": None,
        "port": 4455,
        "password": "secret",
    }

    # Optional audio_viz_enabled works on ANY skin (e.g., blocky or default)
    mc_viz = DeckPage(id="mc", name="MC", skin="blocky", audio_viz_enabled=True, audio_viz_mode="breathe")
    assert mc_viz.audio_viz_enabled is True
    assert mc_viz.audio_viz_mode == "breathe"

    # Runtime pointer file lifecycle
    rt = write_runtime_info(tmp_path, port=8765, desktop_token="tok-123")
    assert rt.is_file()
    clear_runtime_info(tmp_path)
    assert not rt.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only mutex check")
def test_single_instance_mutex_blocks_duplicate():
    name = f"Local\\LocalDeckTestMutex_{int(time.time() * 1000)}"
    g1 = SingleInstanceGuard(mutex_name=name)
    g2 = SingleInstanceGuard(mutex_name=name)
    try:
        assert g1.acquire() is True
        assert g2.acquire() is False
    finally:
        g2.release()
        g1.release()

    g3 = SingleInstanceGuard(mutex_name=name)
    try:
        assert g3.acquire() is True
    finally:
        g3.release()


def test_v2_spans_audio_mixer_telemetry_and_live_watcher(tmp_path: Path):
    # 1. Button spans + overlap validation + out-of-bounds guard
    dm = DeckManager(tmp_path)
    wide = DeckButton(
        id="wide-widget",
        row=0,
        col=1,
        row_span=1,
        col_span=2,
        label="Clock",
        action=ButtonAction(type="widget", widget_kind="clock"),
    )
    updated_page = dm.upsert_button("general", wide)
    ids = {b.id for b in updated_page.buttons}
    # Replaced gen-prev (0,1) and gen-next (0,2), while keeping gen-play (0,0) and gen-mute (0,3)
    assert "wide-widget" in ids
    assert "gen-prev" not in ids and "gen-next" not in ids
    assert "gen-play" in ids and "gen-mute" in ids

    # Out-of-bounds span must raise ValueError
    with pytest.raises(ValueError):
        dm.upsert_button(
            "general",
            DeckButton(id="bad-span", row=0, col=3, col_span=2, label="Bad"),
        )
    # Moving 1x2 wide button into col 3 must fail space check
    with pytest.raises(ValueError):
        dm.swap_buttons("general", 0, 1, 0, 3)

    # 2. Audio mixer backend mock
    mixer = NoopAudioMixerBackend(master_volume=0.5, master_muted=False)
    st = mixer.set_volume(None, 0.75)
    assert st["master"]["volume"] == 0.75
    st = mixer.set_mute("spotify.exe", True)
    sp = next(a for a in st["apps"] if a["exe"] == "spotify.exe")
    assert sp["muted"] is True
    mixer.set_mute("__master__", True)
    assert mixer.is_master_muted() is True

    # 3. Funbit JSON & RenCloud SCSTelemetry SHM parsers
    funbit = parse_funbit_telemetry_json(
        {
            "game": {"connected": True},
            "truck": {
                "speed": -22.4,
                "displayedGear": -2,
                "engineRpm": 1420.5,
                "fuel": 300.0,
                "fuelCapacity": 600.0,
                "cruiseControlSpeed": 80.0,
                "parkBrakeOn": True,
            },
        }
    )
    assert funbit["connected"] is True
    assert funbit["speed_kmh"] == 22
    assert funbit["gear"] == "R2"
    assert funbit["rpm"] == 1420
    assert funbit["fuel_pct"] == 50
    assert funbit["cruise_kmh"] == 80
    assert funbit["park_brake"] is True

    buf = bytearray(2048)
    buf[0] = 1
    struct.pack_into("<i", buf, 508, 12)
    struct.pack_into("<f", buf, 704, 800.0)
    struct.pack_into("<f", buf, 948, 25.0)  # 90 km/h
    struct.pack_into("<f", buf, 952, 1350.0)
    struct.pack_into("<f", buf, 980, 22.22)  # ~80 km/h
    struct.pack_into("<f", buf, 1004, 600.0)  # 75%
    buf[1568] = 1
    shm = parse_scs_shared_memory(bytes(buf))
    assert shm == {
        "connected": True,
        "speed_kmh": 90,
        "gear": "D12",
        "rpm": 1350,
        "fuel_pct": 75,
        "cruise_kmh": 80,
        "park_brake": True,
    }
    buf[0] = 0
    assert parse_scs_shared_memory(bytes(buf))["connected"] is False

    # 4. System stats reader & LiveStateWatcher
    stats = SystemStatsReader().read()
    assert 0 <= stats["cpu_pct"] <= 100
    assert 0 <= stats["ram_pct"] <= 100

    obs = NoopObsBackend()
    obs.execute("toggle_record")
    page = DeckPage(
        id="w",
        name="W",
        skin="truck",
        buttons=[
            DeckButton(id="b1", row=0, col=0, action=ButtonAction(type="widget", widget_kind="cpu_ram")),
            DeckButton(id="b2", row=0, col=1, action=ButtonAction(type="obs_action", obs_command="toggle_record")),
            DeckButton(id="b3", row=0, col=2, action=ButtonAction(type="media", media_action="mute")),
        ],
    )
    snapshots: list[dict] = []
    watcher = LiveStateWatcher(
        should_poll=lambda: True,
        get_active_page=lambda: page,
        get_obs_settings=lambda: (4455, ""),
        obs_backend=obs,
        mixer_backend=mixer,
        system_stats=SystemStatsReader(),
        ets2_reader=Ets2TelemetryReader(shm_name="Local\\NonExistentSCS_Test", funbit_url="http://127.0.0.1:9/none"),
        on_live_state=snapshots.append,
        poll_interval=0.05,
    )
    watcher.start()
    try:
        deadline = time.monotonic() + 1.5
        while not snapshots and time.monotonic() < deadline:
            time.sleep(0.02)
        assert snapshots
        assert snapshots[-1]["obs"]["recording"] is True
        assert snapshots[-1]["master_muted"] is True
    finally:
        watcher.stop()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only WASAPI mixer COM smoke check")
def test_windows_audio_mixer_backend_reads():
    state = WindowsAudioMixerBackend().get_mixer_state()
    assert 0.0 <= state["master"]["volume"] <= 1.0
    assert isinstance(state["master"]["muted"], bool)
    assert isinstance(state["apps"], list)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only Mark-of-the-Web ADS test")
def test_win_unblock_removes_zone_identifier(tmp_path: Path):
    from localdeck.core.win_unblock import unblock_downloaded_binaries

    dll = tmp_path / "Python.Runtime.dll"
    dll.write_bytes(b"MZ")
    ads = f"{dll}:Zone.Identifier"
    with open(ads, "w", encoding="utf-8") as fh:
        fh.write("[ZoneTransfer]\r\nZoneId=3\r\n")

    assert unblock_downloaded_binaries(tmp_path) == 1
    with pytest.raises(FileNotFoundError):
        open(ads, "r", encoding="utf-8")
