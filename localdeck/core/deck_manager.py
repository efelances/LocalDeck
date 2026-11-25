"""Deck layouts, per-app auto-switching profiles, and atomic JSON persistence."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from localdeck.models import (
    AppSettings,
    ButtonAction,
    DeckButton,
    DeckConfig,
    DeckPage,
    MacroStep,
)


def _btn(
    button_id: str,
    row: int,
    col: int,
    label: str,
    label_i18n: dict[str, str],
    icon: str,
    action: ButtonAction,
    tone: str = "default",
) -> DeckButton:
    return DeckButton(
        id=button_id,
        row=row,
        col=col,
        label=label,
        label_i18n=label_i18n,
        icon=icon,
        tone=tone,  # type: ignore[arg-type]
        action=action,
    )


def build_starter_config() -> DeckConfig:
    """Create the initial 3-page LocalDeck configuration."""
    general_page = DeckPage(
        id="general",
        name="Genel & Medya",
        name_i18n={
            "tr": "Genel & Medya",
            "en": "General & Media",
            "de": "Allgemein & Medien",
            "es": "General y Medios",
        },
        rows=3,
        cols=4,
        buttons=[
            _btn(
                "gen-play",
                0,
                0,
                "Oynat",
                {"tr": "Oynat / Duraklat", "en": "Play / Pause", "de": "Play / Pause", "es": "Reproducir"},
                "play",
                ButtonAction(type="media", media_action="play_pause"),
                tone="accent",
            ),
            _btn(
                "gen-prev",
                0,
                1,
                "Önceki",
                {"tr": "Önceki", "en": "Previous", "de": "Zurück", "es": "Anterior"},
                "prev",
                ButtonAction(type="media", media_action="prev"),
            ),
            _btn(
                "gen-next",
                0,
                2,
                "Sonraki",
                {"tr": "Sonraki", "en": "Next", "de": "Weiter", "es": "Siguiente"},
                "next",
                ButtonAction(type="media", media_action="next"),
            ),
            _btn(
                "gen-mute",
                0,
                3,
                "Sessiz",
                {"tr": "Sessiz", "en": "Mute", "de": "Stumm", "es": "Silenciar"},
                "mute",
                ButtonAction(type="media", media_action="mute"),
                tone="ink",
            ),
            _btn(
                "gen-voldown",
                1,
                0,
                "Ses -",
                {"tr": "Ses Azalt", "en": "Vol -", "de": "Leiser", "es": "Vol -"},
                "vol_down",
                ButtonAction(type="media", media_action="vol_down"),
            ),
            _btn(
                "gen-volup",
                1,
                1,
                "Ses +",
                {"tr": "Ses Artır", "en": "Vol +", "de": "Lauter", "es": "Vol +"},
                "vol_up",
                ButtonAction(type="media", media_action="vol_up"),
            ),
            _btn(
                "gen-snip",
                1,
                2,
                "Ekran Alıntı",
                {"tr": "Ekran Alıntısı", "en": "Screenshot", "de": "Screenshot", "es": "Captura"},
                "camera",
                ButtonAction(type="hotkey", keys=["win", "shift", "s"]),
            ),
            _btn(
                "gen-desktop",
                1,
                3,
                "Masaüstü",
                {"tr": "Masaüstü", "en": "Desktop", "de": "Desktop", "es": "Escritorio"},
                "desktop",
                ButtonAction(type="hotkey", keys=["win", "d"]),
            ),
            _btn(
                "gen-alttab",
                2,
                0,
                "Pencere Geç",
                {"tr": "Pencere Geç", "en": "Switch App", "de": "Fenster", "es": "Cambiar App"},
                "layers",
                ButtonAction(type="hotkey", keys=["alt", "tab"]),
            ),
            _btn(
                "gen-lock",
                2,
                1,
                "Kilitle",
                {"tr": "PC Kilitle", "en": "Lock PC", "de": "Sperren", "es": "Bloquear"},
                "lock",
                ButtonAction(type="hotkey", keys=["win", "l"]),
                tone="ink",
            ),
            _btn(
                "gen-copy",
                2,
                2,
                "Kopyala",
                {"tr": "Kopyala", "en": "Copy", "de": "Kopieren", "es": "Copiar"},
                "copy",
                ButtonAction(type="hotkey", keys=["ctrl", "c"]),
            ),
            _btn(
                "gen-paste",
                2,
                3,
                "Yapıştır",
                {"tr": "Yapıştır", "en": "Paste", "de": "Einfügen", "es": "Pegar"},
                "paste",
                ButtonAction(type="hotkey", keys=["ctrl", "v"]),
            ),
        ],
    )

    productivity_page = DeckPage(
        id="productivity",
        name="Tarayıcı & Editör",
        name_i18n={
            "tr": "Tarayıcı & Editör",
            "en": "Browser & Editor",
            "de": "Browser & Editor",
            "es": "Navegador y Editor",
        },
        rows=3,
        cols=4,
        match_executables=["chrome.exe", "msedge.exe", "firefox.exe", "code.exe"],
        buttons=[
            _btn(
                "prod-newtab",
                0,
                0,
                "Yeni Sekme",
                {"tr": "Yeni Sekme", "en": "New Tab", "de": "Neuer Tab", "es": "Nueva Pestaña"},
                "plus",
                ButtonAction(type="hotkey", keys=["ctrl", "t"]),
                tone="accent",
            ),
            _btn(
                "prod-closetab",
                0,
                1,
                "Sekme Kapat",
                {"tr": "Sekme Kapat", "en": "Close Tab", "de": "Tab schließen", "es": "Cerrar"},
                "close",
                ButtonAction(type="hotkey", keys=["ctrl", "w"]),
            ),
            _btn(
                "prod-reopen",
                0,
                2,
                "Geri Aç",
                {"tr": "Geri Aç", "en": "Reopen Tab", "de": "Wiederöffnen", "es": "Reabrir"},
                "undo",
                ButtonAction(type="hotkey", keys=["ctrl", "shift", "t"]),
            ),
            _btn(
                "prod-refresh",
                0,
                3,
                "Yenile",
                {"tr": "Yenile", "en": "Refresh", "de": "Aktualisieren", "es": "Recargar"},
                "refresh",
                ButtonAction(type="hotkey", keys=["ctrl", "r"]),
            ),
            _btn(
                "prod-back",
                1,
                0,
                "Geri",
                {"tr": "Geri", "en": "Back", "de": "Zurück", "es": "Atrás"},
                "arrow_left",
                ButtonAction(type="hotkey", keys=["alt", "left"]),
            ),
            _btn(
                "prod-forward",
                1,
                1,
                "İleri",
                {"tr": "İleri", "en": "Forward", "de": "Vorwärts", "es": "Adelante"},
                "arrow_right",
                ButtonAction(type="hotkey", keys=["alt", "right"]),
            ),
            _btn(
                "prod-find",
                1,
                2,
                "Ara",
                {"tr": "Sayfada Ara", "en": "Find", "de": "Suchen", "es": "Buscar"},
                "search",
                ButtonAction(type="hotkey", keys=["ctrl", "f"]),
            ),
            _btn(
                "prod-save",
                1,
                3,
                "Kaydet",
                {"tr": "Kaydet", "en": "Save", "de": "Speichern", "es": "Guardar"},
                "save",
                ButtonAction(type="hotkey", keys=["ctrl", "s"]),
                tone="accent",
            ),
            _btn(
                "prod-undo",
                2,
                0,
                "Geri Al",
                {"tr": "Geri Al", "en": "Undo", "de": "Rückgängig", "es": "Deshacer"},
                "undo",
                ButtonAction(type="hotkey", keys=["ctrl", "z"]),
            ),
            _btn(
                "prod-redo",
                2,
                1,
                "Yinele",
                {"tr": "Yinele", "en": "Redo", "de": "Wiederholen", "es": "Rehacer"},
                "redo",
                ButtonAction(type="hotkey", keys=["ctrl", "y"]),
            ),
            _btn(
                "prod-devtools",
                2,
                2,
                "DevTools",
                {"tr": "DevTools", "en": "DevTools", "de": "DevTools", "es": "DevTools"},
                "code",
                ButtonAction(type="hotkey", keys=["f12"]),
            ),
            _btn(
                "prod-home",
                2,
                3,
                "Genel Sayfa",
                {"tr": "Genel Sayfa", "en": "General", "de": "Allgemein", "es": "General"},
                "grid",
                ButtonAction(type="switch_page", target_page_id="general"),
                tone="ink",
            ),
        ],
    )

    cockpit_page = DeckPage(
        id="cockpit",
        name="Hızlı Kokpit",
        name_i18n={
            "tr": "Hızlı Kokpit",
            "en": "Quick Cockpit",
            "de": "Schnell-Cockpit",
            "es": "Cabina Rápida",
        },
        rows=3,
        cols=4,
        buttons=[
            _btn(
                "cockpit-ptt",
                0,
                0,
                "Bas-Konuş",
                {"tr": "Bas-Konuş (V)", "en": "Push-to-Talk (V)", "de": "Push-to-Talk (V)", "es": "Pulsar-Hablar (V)"},
                "mic",
                ButtonAction(type="hold_key", keys=["v"]),
                tone="accent",
            ),
            _btn(
                "cockpit-holdspace",
                0,
                1,
                "Boşluk Tut",
                {"tr": "Boşluk Basılı Tut", "en": "Hold Space", "de": "Leertaste halten", "es": "Mantener Espacio"},
                "keyboard",
                ButtonAction(type="hold_key", keys=["space"]),
            ),
            _btn(
                "cockpit-macro",
                0,
                2,
                "Kayıt Makro",
                {"tr": "Hızlı Makro", "en": "Quick Macro", "de": "Schnell-Makro", "es": "Macro Rápida"},
                "bolt",
                ButtonAction(
                    type="macro",
                    steps=[
                        MacroStep(kind="hotkey", keys=["ctrl", "s"]),
                        MacroStep(kind="delay", delay_ms=120),
                        MacroStep(kind="hotkey", keys=["f5"]),
                    ],
                ),
            ),
            _btn(
                "cockpit-f1",
                1,
                0,
                "F1",
                {"tr": "F1", "en": "F1", "de": "F1", "es": "F1"},
                "star",
                ButtonAction(type="hotkey", keys=["f1"]),
            ),
            _btn(
                "cockpit-f2",
                1,
                1,
                "F2",
                {"tr": "F2", "en": "F2", "de": "F2", "es": "F2"},
                "star",
                ButtonAction(type="hotkey", keys=["f2"]),
            ),
            _btn(
                "cockpit-f3",
                1,
                2,
                "F3",
                {"tr": "F3", "en": "F3", "de": "F3", "es": "F3"},
                "star",
                ButtonAction(type="hotkey", keys=["f3"]),
            ),
            _btn(
                "cockpit-f4",
                1,
                3,
                "F4",
                {"tr": "F4", "en": "F4", "de": "F4", "es": "F4"},
                "star",
                ButtonAction(type="hotkey", keys=["f4"]),
            ),
            _btn(
                "cockpit-home",
                2,
                3,
                "Genel Sayfa",
                {"tr": "Genel Sayfa", "en": "General", "de": "Allgemein", "es": "General"},
                "grid",
                ButtonAction(type="switch_page", target_page_id="general"),
                tone="ink",
            ),
        ],
    )

    return DeckConfig(
        version=1,
        settings=AppSettings(active_page_id="general"),
        pages=[general_page, productivity_page, cockpit_page],
    )


def _occupied_cells(btn: DeckButton) -> set[tuple[int, int]]:
    return {
        (r, c)
        for r in range(btn.row, btn.row + btn.row_span)
        for c in range(btn.col, btn.col + btn.col_span)
    }


def _sanitize_buttons_for_grid(buttons: list[DeckButton], rows: int, cols: int) -> list[DeckButton]:
    """Filter out-of-bounds buttons, clamp spans to grid limits, and prevent overlapping cells."""
    kept: list[DeckButton] = []
    used: set[tuple[int, int]] = set()
    for btn in buttons:
        if not (0 <= btn.row < rows and 0 <= btn.col < cols):
            continue
        clamped_r = min(btn.row_span, rows - btn.row)
        clamped_c = min(btn.col_span, cols - btn.col)
        candidate = btn.model_copy(update={"row_span": clamped_r, "col_span": clamped_c})
        cells = _occupied_cells(candidate)
        if cells & used:
            continue
        used |= cells
        kept.append(candidate)
    return kept


class DeckManager:
    """Thread-safe manager for LocalDeck pages, buttons, settings, and window matching."""

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._file_path = self._data_dir / "deck.json"
        self._lock = threading.RLock()
        self._config = self._load_or_init()

    def _load_or_init(self) -> DeckConfig:
        if self._file_path.exists():
            raw = self._file_path.read_text(encoding="utf-8").strip()
            if raw:
                config = DeckConfig.model_validate_json(raw)
                if config.pages:
                    if not any(p.id == config.settings.active_page_id for p in config.pages):
                        config.settings.active_page_id = config.pages[0].id
                    return config
        starter = build_starter_config()
        self._save_locked(starter)
        return starter

    def _save_locked(self, config: DeckConfig) -> None:
        tmp_path = self._data_dir / "deck.json.tmp"
        payload = json.dumps(config.model_dump(mode="json"), indent=2, ensure_ascii=False)
        tmp_path.write_text(payload, encoding="utf-8")
        os.replace(tmp_path, self._file_path)

    def get_config(self) -> DeckConfig:
        with self._lock:
            return self._config.model_copy(deep=True)

    def get_page(self, page_id: str) -> DeckPage | None:
        with self._lock:
            for page in self._config.pages:
                if page.id == page_id:
                    return page.model_copy(deep=True)
            return None

    def get_active_page(self) -> DeckPage:
        with self._lock:
            active_id = self._config.settings.active_page_id
            for page in self._config.pages:
                if page.id == active_id:
                    return page.model_copy(deep=True)
            return self._config.pages[0].model_copy(deep=True)

    def set_active_page(self, page_id: str) -> DeckPage:
        with self._lock:
            target = next((p for p in self._config.pages if p.id == page_id), None)
            if target is None:
                raise KeyError(f"Page not found: {page_id}")
            self._config.settings.active_page_id = page_id
            self._save_locked(self._config)
            return target.model_copy(deep=True)

    def update_settings(self, patch: dict) -> AppSettings:
        with self._lock:
            merged = self._config.settings.model_dump()
            merged.update(patch)
            updated = AppSettings.model_validate(merged)
            if not any(p.id == updated.active_page_id for p in self._config.pages):
                updated.active_page_id = self._config.pages[0].id
            self._config.settings = updated
            self._save_locked(self._config)
            return updated.model_copy(deep=True)

    def upsert_page(self, page: DeckPage) -> DeckPage:
        with self._lock:
            filtered_buttons = _sanitize_buttons_for_grid(page.buttons, page.rows, page.cols)
            clean_page = page.model_copy(update={"buttons": filtered_buttons})
            for idx, existing in enumerate(self._config.pages):
                if existing.id == clean_page.id:
                    self._config.pages[idx] = clean_page
                    break
            else:
                self._config.pages.append(clean_page)
            self._save_locked(self._config)
            return clean_page.model_copy(deep=True)

    def delete_page(self, page_id: str) -> None:
        with self._lock:
            if len(self._config.pages) <= 1:
                raise ValueError("Cannot delete the last remaining page")
            new_pages = [p for p in self._config.pages if p.id != page_id]
            if len(new_pages) == len(self._config.pages):
                raise KeyError(f"Page not found: {page_id}")
            self._config.pages = new_pages
            if self._config.settings.active_page_id == page_id:
                self._config.settings.active_page_id = new_pages[0].id
            self._save_locked(self._config)

    def import_page(self, page: DeckPage) -> DeckPage:
        """Install an imported or template page with a unique id and make it active."""
        with self._lock:
            existing_ids = {p.id for p in self._config.pages}
            target_id = page.id
            if target_id in existing_ids:
                suffix = 2
                while f"{page.id}-{suffix}" in existing_ids:
                    suffix += 1
                target_id = f"{page.id}-{suffix}"
            filtered_buttons = _sanitize_buttons_for_grid(page.buttons, page.rows, page.cols)
            clean_page = page.model_copy(update={"id": target_id, "buttons": filtered_buttons})
            self._config.pages.append(clean_page)
            self._config.settings.active_page_id = clean_page.id
            self._save_locked(self._config)
            return clean_page.model_copy(deep=True)

    def upsert_button(self, page_id: str, button: DeckButton) -> DeckPage:
        with self._lock:
            page = next((p for p in self._config.pages if p.id == page_id), None)
            if page is None:
                raise KeyError(f"Page not found: {page_id}")
            if not (
                0 <= button.row
                and 0 <= button.col
                and button.row + button.row_span <= page.rows
                and button.col + button.col_span <= page.cols
            ):
                raise ValueError("Button coordinates or span exceed grid bounds")
            target_cells = _occupied_cells(button)
            page.buttons = [
                b
                for b in page.buttons
                if b.id != button.id and not (_occupied_cells(b) & target_cells)
            ]
            page.buttons.append(button.model_copy(deep=True))
            self._save_locked(self._config)
            return page.model_copy(deep=True)

    def delete_button(self, page_id: str, button_id: str) -> DeckPage:
        with self._lock:
            page = next((p for p in self._config.pages if p.id == page_id), None)
            if page is None:
                raise KeyError(f"Page not found: {page_id}")
            before = len(page.buttons)
            page.buttons = [b for b in page.buttons if b.id != button_id]
            if len(page.buttons) == before:
                raise KeyError(f"Button not found: {button_id}")
            self._save_locked(self._config)
            return page.model_copy(deep=True)

    def swap_buttons(
        self,
        page_id: str,
        from_row: int,
        from_col: int,
        to_row: int,
        to_col: int,
    ) -> DeckPage:
        with self._lock:
            page = next((p for p in self._config.pages if p.id == page_id), None)
            if page is None:
                raise KeyError(f"Page not found: {page_id}")
            for r, c in ((from_row, from_col), (to_row, to_col)):
                if not (0 <= r < page.rows and 0 <= c < page.cols):
                    raise ValueError("Swap coordinates are out of grid bounds")
            if from_row == to_row and from_col == to_col:
                return page.model_copy(deep=True)

            src = next((b for b in page.buttons if (from_row, from_col) in _occupied_cells(b)), None)
            dst = next((b for b in page.buttons if (to_row, to_col) in _occupied_cells(b)), None)
            if src is not None and src is dst:
                return page.model_copy(deep=True)

            updates: list[tuple[DeckButton, int, int]] = []
            if src is not None:
                updates.append((src, to_row, to_col))
            if dst is not None:
                updates.append((dst, from_row, from_col))

            moving_ids = {b.id for b, _, _ in updates}
            fixed_cells: set[tuple[int, int]] = set()
            for existing in page.buttons:
                if existing.id not in moving_ids:
                    fixed_cells |= _occupied_cells(existing)

            new_cells_total: set[tuple[int, int]] = set(fixed_cells)
            for btn, nr, nc in updates:
                if nr + btn.row_span > page.rows or nc + btn.col_span > page.cols:
                    raise ValueError("Not enough space to move spanned button")
                moved_copy = btn.model_copy(update={"row": nr, "col": nc})
                cells = _occupied_cells(moved_copy)
                if cells & new_cells_total:
                    raise ValueError("Not enough space to move spanned button")
                new_cells_total |= cells

            for btn, nr, nc in updates:
                btn.row = nr
                btn.col = nc
            self._save_locked(self._config)
            return page.model_copy(deep=True)

    def match_window(self, exe_name: str, window_title: str) -> str | None:
        exe = exe_name.strip().lower()
        title = window_title.strip().lower()
        with self._lock:
            for page in reversed(self._config.pages[1:]):
                if exe and exe in page.match_executables:
                    return page.id
                if title and any(sub.lower() in title for sub in page.match_title_contains):
                    return page.id
            return None
