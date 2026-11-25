"""Pydantic models and validation rules for LocalDeck configuration and actions."""

from __future__ import annotations

from pathlib import PureWindowsPath
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator, model_validator

from localdeck.core.app_launcher import validate_safe_app_path
from localdeck.core.input_win import ALLOWED_KEYS
from localdeck.core.sound_player import validate_sound_filename

LanguageCode = Literal["en", "tr", "de", "es"]
ThemeMode = Literal["dark", "light"]
PageSkin = Literal["default", "truck", "blocky", "studio", "tactical", "audio"]
AudioVizMode = Literal["bars", "rainbow", "neon", "breathe"]
WidgetKind = Literal["clock", "cpu_ram", "ets2_speed", "ets2_fuel"]
ActionType = Literal[
    "hotkey",
    "hold_key",
    "media",
    "macro",
    "open_url",
    "switch_page",
    "play_sound",
    "launch_app",
    "obs_action",
    "widget",
    "noop",
]
ObsCommand = Literal[
    "switch_scene",
    "toggle_record",
    "toggle_stream",
    "save_replay",
    "toggle_mute",
    "toggle_studio_mode",
]
MediaAction = Literal["play_pause", "next", "prev", "stop", "vol_up", "vol_down", "mute"]
ButtonTone = Literal["default", "accent", "ink"]


def _normalize_keys(keys: list[str]) -> list[str]:
    if len(keys) > 6:
        raise ValueError("A hotkey chord cannot contain more than 6 keys")
    normalized: list[str] = []
    for raw in keys:
        key = raw.strip().lower()
        if key not in ALLOWED_KEYS:
            raise ValueError(f"Unsupported or disallowed key: {raw!r}")
        if key not in normalized:
            normalized.append(key)
    return normalized


class MacroStep(BaseModel):
    """A single validated step inside a macro sequence."""

    kind: Literal["hotkey", "media", "delay"]
    keys: list[str] = Field(default_factory=list)
    media_action: MediaAction | None = None
    delay_ms: int = Field(default=0, ge=0, le=2000)

    @field_validator("keys")
    @classmethod
    def validate_keys(cls, value: list[str]) -> list[str]:
        return _normalize_keys(value)


class ButtonAction(BaseModel):
    """Allowlisted deck button action specification."""

    type: ActionType = "noop"
    keys: list[str] = Field(default_factory=list)
    media_action: MediaAction | None = None
    url: str | None = None
    target_page_id: str | None = None
    sound_file: str | None = None
    app_path: str | None = None
    obs_command: ObsCommand | None = None
    obs_scene: str | None = None
    obs_input: str | None = None
    widget_kind: WidgetKind | None = None
    steps: list[MacroStep] = Field(default_factory=list)

    @field_validator("keys")
    @classmethod
    def validate_keys(cls, value: list[str]) -> list[str]:
        return _normalize_keys(value)

    @field_validator("steps")
    @classmethod
    def validate_steps(cls, value: list[MacroStep]) -> list[MacroStep]:
        if len(value) > 10:
            raise ValueError("A macro cannot contain more than 10 steps")
        return value

    @model_validator(mode="after")
    def validate_action_fields(self) -> ButtonAction:
        if self.type == "open_url":
            if not self.url:
                raise ValueError("URL is required for open_url action")
            parsed = urlparse(self.url.strip())
            if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
                raise ValueError("Only http:// and https:// URLs with a valid host are allowed")
            self.url = self.url.strip()
        elif self.type == "play_sound" and self.sound_file:
            self.sound_file = validate_sound_filename(self.sound_file)
        elif self.type == "launch_app" and self.app_path:
            self.app_path = validate_safe_app_path(self.app_path)
        elif self.type == "widget" and self.widget_kind is None:
            self.widget_kind = "clock"
        return self


class DeckButton(BaseModel):
    """A button placed at a grid cell on a deck page."""

    id: str = Field(min_length=1, max_length=64)
    row: int = Field(ge=0, le=5)
    col: int = Field(ge=0, le=5)
    row_span: int = Field(default=1, ge=1, le=2)
    col_span: int = Field(default=1, ge=1, le=2)
    label: str = Field(default="", max_length=32)
    label_i18n: dict[str, str] = Field(default_factory=dict)
    icon: str = Field(default="bolt", max_length=32)
    tone: ButtonTone = "default"
    action: ButtonAction = Field(default_factory=ButtonAction)


class DeckPage(BaseModel):
    """A deck page with grid dimensions and optional window auto-switch rules."""

    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=40)
    name_i18n: dict[str, str] = Field(default_factory=dict)
    skin: PageSkin = "default"
    audio_viz_enabled: bool = False
    audio_source_exe: str = ""
    audio_viz_mode: AudioVizMode = "bars"
    rows: int = Field(default=3, ge=2, le=6)
    cols: int = Field(default=4, ge=2, le=6)
    match_executables: list[str] = Field(default_factory=list)
    match_title_contains: list[str] = Field(default_factory=list)
    buttons: list[DeckButton] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def default_audio_viz_for_legacy_audio_pages(cls, data: object) -> object:
        if isinstance(data, dict) and data.get("skin") == "audio" and "audio_viz_enabled" not in data:
            return {**data, "audio_viz_enabled": True}
        return data

    @field_validator("audio_source_exe")
    @classmethod
    def normalize_audio_source_exe(cls, value: str) -> str:
        cleaned = value.strip()
        return PureWindowsPath(cleaned).name.lower() if cleaned else ""

    @field_validator("match_executables")
    @classmethod
    def normalize_executables(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        for item in values:
            cleaned = PureWindowsPath(item.strip()).name.lower()
            if cleaned and cleaned not in result:
                result.append(cleaned)
        return result

    @field_validator("match_title_contains")
    @classmethod
    def normalize_titles(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        for item in values:
            cleaned = item.strip()
            if cleaned and cleaned not in result:
                result.append(cleaned)
        return result


class AppSettings(BaseModel):
    """Global desktop and mobile deck preferences."""

    language: LanguageCode = "tr"
    theme: ThemeMode = "dark"
    high_contrast: bool = False
    auto_switch: bool = True
    haptics: bool = True
    port: int = Field(default=8765, ge=1024, le=65535)
    active_page_id: str = "general"
    start_with_windows: bool = False
    obs_port: int = Field(default=4455, ge=1024, le=65535)
    obs_password: str = Field(default="", max_length=128)


class DeckConfig(BaseModel):
    """Persisted root deck configuration."""

    version: int = 1
    settings: AppSettings = Field(default_factory=AppSettings)
    pages: list[DeckPage] = Field(default_factory=list)
