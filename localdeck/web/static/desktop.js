(() => {
  const { t, resolveLocalized, LANGUAGES } = window.LocalDeckI18n;
  const Icons = window.LocalDeckIcons;
  const TOKEN = window.__LOCALDECK_TOKEN__ || "";

  let state = null;
  let liveState = { system: { cpu_pct: 0, ram_pct: 0 }, obs: {}, ets2: { connected: false }, master_muted: false };
  let desktopMixerState = null;
  let selectedCell = null; // { row, col, buttonId }
  let draftKeys = [];
  let draftMacro = [];
  let selectedIcon = "bolt";
  let selectedTone = "default";
  let selectedSize = "1x1";
  let recordingKeys = false;
  let qrCountdown = 0;
  let pageSettingsOpen = false;
  let dragSource = null; // { row, col }
  let ws = null;

  const $ = (id) => document.getElementById(id);
  const audioViz = window.LocalDeckAudioViz.create($("desktopAudioViz"));

  function escapeHtml(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  const KEY_CODE_MAP = {
    ControlLeft: "ctrl",
    ControlRight: "rctrl",
    ShiftLeft: "shift",
    ShiftRight: "rshift",
    AltLeft: "alt",
    AltRight: "ralt",
    MetaLeft: "win",
    MetaRight: "rwin",
    Enter: "enter",
    NumpadEnter: "enter",
    Escape: "esc",
    Tab: "tab",
    Space: "space",
    Backspace: "backspace",
    Delete: "delete",
    Insert: "insert",
    Home: "home",
    End: "end",
    PageUp: "pageup",
    PageDown: "pagedown",
    ArrowUp: "up",
    ArrowDown: "down",
    ArrowLeft: "left",
    ArrowRight: "right",
    PrintScreen: "printscreen",
    Minus: "minus",
    Equal: "equal",
    BracketLeft: "lbracket",
    BracketRight: "rbracket",
    Semicolon: "semicolon",
    Quote: "apostrophe",
    Comma: "comma",
    Period: "period",
    Slash: "slash",
    Backslash: "backslash",
    Backquote: "grave",
  };

  function mapBrowserKey(e) {
    if (KEY_CODE_MAP[e.code]) return KEY_CODE_MAP[e.code];
    if (/^Key([A-Z])$/.test(e.code)) return e.code.slice(3).toLowerCase();
    if (/^Digit([0-9])$/.test(e.code)) return e.code.slice(5);
    if (/^F([1-9]|1[0-9]|2[0-4])$/.test(e.code)) return e.code.toLowerCase();
    if (/^Numpad([0-9])$/.test(e.code)) return "num" + e.code.slice(6);
    const k = (e.key || "").toLowerCase();
    if (state && state.allowed_keys && state.allowed_keys.includes(k)) return k;
    return null;
  }

  async function api(method, path, body) {
    const res = await fetch(path, {
      method,
      headers: {
        "Content-Type": "application/json",
        "X-LocalDeck-Desktop-Token": TOKEN,
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || "Request failed");
    }
    return res.json();
  }

  function lang() {
    return (state && state.config && state.config.settings && state.config.settings.language) || "tr";
  }

  function currentPage() {
    if (!state) return null;
    const pages = state.config.pages || [];
    return pages.find((p) => p.id === state.active_page_id) || pages[0] || null;
  }

  function applyStaticTranslations() {
    const l = lang();
    document.documentElement.lang = l;
    const theme = state?.config?.settings?.theme || "dark";
    document.documentElement.setAttribute("data-theme", theme);
    const contrast = state?.config?.settings?.high_contrast ? "high" : "normal";
    document.documentElement.setAttribute("data-contrast", contrast);

    document.querySelectorAll("[data-i18n]").forEach((el) => {
      el.textContent = t(l, el.getAttribute("data-i18n"));
    });
  }

  function renderTopBar() {
    const l = lang();
    const settings = state.config.settings;
    $("lanBadge").textContent = state.lan_url;
    $("connDot").className = "status-dot" + (state.connected_count > 0 ? " online" : "");
    $("connCount").textContent = `${state.connected_count} ${t(l, "connectedCount")}`;

    const autoBtn = $("autoSwitchToggle");
    autoBtn.textContent = `${t(l, "autoSwitchLabel")}: ${settings.auto_switch ? "ON" : "OFF"}`;
    autoBtn.className = "toggle-btn" + (settings.auto_switch ? " active" : "");

    const winBtn = $("autostartToggle");
    winBtn.textContent = t(l, "startWithWindows");
    winBtn.className = "toggle-btn" + (settings.start_with_windows ? " active" : "");

    const themeBtn = $("themeToggle");
    const isDark = (settings.theme || "dark") === "dark";
    themeBtn.textContent = isDark ? t(l, "themeDark") : t(l, "themeLight");
    themeBtn.className = "toggle-btn" + (isDark ? " active" : "");

    const hcBtn = $("contrastToggle");
    hcBtn.textContent = t(l, "highContrastLabel");
    hcBtn.className = "toggle-btn" + (settings.high_contrast ? " active" : "");

    const langGroup = $("langGroup");
    langGroup.innerHTML = "";
    LANGUAGES.forEach((code) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "lang-btn" + (code === l ? " active" : "");
      btn.textContent = code.toUpperCase();
      btn.onclick = () => updateSettings({ language: code });
      langGroup.appendChild(btn);
    });
  }

  function renderPagesSidebar() {
    const l = lang();
    const list = $("pageList");
    list.innerHTML = "";
    const active = currentPage();
    state.config.pages.forEach((page) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "page-item" + (active && page.id === active.id ? " active" : "");
      const nameSpan = document.createElement("span");
      nameSpan.textContent = resolveLocalized(page, "name", l);
      const dimSpan = document.createElement("small");
      dimSpan.textContent = `${page.rows}×${page.cols}`;
      btn.append(nameSpan, dimSpan);
      btn.onclick = () => updateSettings({ active_page_id: page.id });
      list.appendChild(btn);
    });
  }

  function renderForegroundBox() {
    const l = lang();
    const fg = state.foreground || { exe: "", title: "" };
    $("fgExe").textContent = fg.exe || "—";
    $("fgTitle").textContent = fg.exe ? fg.title || "" : t(l, "noForeground");
    const bindBtn = $("bindExeBtn");
    const page = currentPage();
    if (fg.exe && page && !page.match_executables.includes(fg.exe.toLowerCase())) {
      bindBtn.style.display = "inline-flex";
      bindBtn.textContent = `${t(l, "addExeToPage")} (${fg.exe})`;
    } else {
      bindBtn.style.display = "none";
    }
  }

  function renderQrAndDevices() {
    const l = lang();
    if (state.pairing) {
      $("qrBox").innerHTML = state.pairing.svg || "";
      if (typeof state.pairing.expires_in === "number") {
        qrCountdown = state.pairing.expires_in;
      }
    }
    $("qrMeta").textContent = `${t(l, "qrHint")} ${qrCountdown}s`;

    const devList = $("deviceList");
    devList.innerHTML = "";
    const devices = state.devices || [];
    if (!devices.length) {
      const empty = document.createElement("div");
      empty.className = "hint-text";
      empty.textContent = t(l, "noDevices");
      devList.appendChild(empty);
      return;
    }
    devices.forEach((dev) => {
      const row = document.createElement("div");
      row.className = "device-row";
      const info = document.createElement("div");
      info.className = "device-info";
      const dot = document.createElement("span");
      dot.className = "status-dot" + (dev.connected ? " online" : "");
      const txt = document.createElement("div");
      const name = document.createElement("strong");
      name.textContent = dev.device_name;
      const meta = document.createElement("span");
      meta.className = "hint-text";
      meta.textContent = `${dev.client_ip} · ${dev.connected ? t(l, "online") : t(l, "offline")}`;
      txt.append(name, meta);
      info.append(dot, txt);

      const revokeBtn = document.createElement("button");
      revokeBtn.type = "button";
      revokeBtn.className = "btn btn-sm";
      revokeBtn.textContent = t(l, "revoke");
      revokeBtn.onclick = async () => {
        state = await api("DELETE", `/api/desktop/devices/${dev.session_id}`);
        renderAll();
      };
      row.append(info, revokeBtn);
      devList.appendChild(row);
    });
  }

  function buildDesktopWidgetHtml(btn, l) {
    const kind = btn.action?.widget_kind || "clock";
    const lbl = escapeHtml(resolveLocalized(btn, "label", l) || t(l, `widget_${kind}`));
    const sys = liveState.system || {};
    const ets2 = liveState.ets2 || {};
    if (kind === "clock") {
      const now = new Date();
      return `
        <div class="widget-tile" data-widget-kind="clock">
          <div class="widget-kicker">${lbl}</div>
          <div class="widget-value" data-w-val>${now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</div>
          <div class="widget-sub" data-w-sub>${now.toLocaleDateString([], { weekday: "short", day: "2-digit", month: "short" })}</div>
        </div>
      `;
    }
    if (kind === "cpu_ram") {
      const cpu = sys.cpu_pct ?? 0;
      const ram = sys.ram_pct ?? 0;
      return `
        <div class="widget-tile" data-widget-kind="cpu_ram">
          <div class="widget-kicker">CPU · RAM</div>
          <div class="widget-value" data-w-val>${cpu}%</div>
          <div class="widget-sub" data-w-sub>RAM ${ram}%</div>
          <div class="widget-bar"><div class="widget-bar-fill" style="width:${cpu}%"></div></div>
        </div>
      `;
    }
    if (kind === "ets2_speed") {
      const val = ets2.connected ? `${ets2.speed_kmh || 0}` : "—";
      const sub = escapeHtml(ets2.connected
        ? `km/h · ${ets2.gear || "N"}${ets2.cruise_kmh ? ` · CC ${ets2.cruise_kmh}` : ""}${ets2.park_brake ? " · (P)" : ""}`
        : t(l, "ets2Offline"));
      return `
        <div class="widget-tile" data-widget-kind="ets2_speed">
          <div class="widget-kicker">${lbl}</div>
          <div class="widget-value" data-w-val>${val}</div>
          <div class="widget-sub" data-w-sub>${sub}</div>
        </div>
      `;
    }
    const fuel = ets2.connected ? `${ets2.fuel_pct || 0}%` : "—";
    const rpmSub = escapeHtml(ets2.connected ? `${ets2.rpm || 0} RPM` : t(l, "ets2Offline"));
    return `
      <div class="widget-tile" data-widget-kind="ets2_fuel">
        <div class="widget-kicker">${lbl}</div>
        <div class="widget-value" data-w-val>${fuel}</div>
        <div class="widget-sub" data-w-sub>${rpmSub}</div>
        <div class="widget-bar"><div class="widget-bar-fill" style="width:${ets2.connected ? (ets2.fuel_pct || 0) : 0}%"></div></div>
      </div>
    `;
  }

  function applyDesktopLiveIndicator(cell, btn, l) {
    cell.classList.remove("live-active-danger", "live-active-ok");
    const oldBadge = cell.querySelector(".live-badge");
    if (oldBadge) oldBadge.remove();
    const act = btn.action || {};
    const obs = liveState.obs || {};
    let badgeText = "";
    if (act.type === "obs_action" && obs.connected) {
      if (act.obs_command === "toggle_record" && obs.recording) {
        cell.classList.add("live-active-danger");
        badgeText = t(l, "liveRec");
      } else if (act.obs_command === "toggle_stream" && obs.streaming) {
        cell.classList.add("live-active-danger");
        badgeText = t(l, "liveStream");
      } else if (act.obs_command === "switch_scene" && act.obs_scene && obs.scene === act.obs_scene) {
        cell.classList.add("live-active-ok");
      } else if (act.obs_command === "toggle_studio_mode" && obs.studio_mode) {
        cell.classList.add("live-active-ok");
      }
    } else if (act.type === "media" && act.media_action === "mute" && liveState.master_muted) {
      cell.classList.add("live-active-danger");
      badgeText = t(l, "liveMuted");
    }
    if (badgeText) {
      const b = document.createElement("span");
      b.className = "live-badge";
      b.textContent = badgeText;
      cell.appendChild(b);
    }
  }

  function updateDesktopLiveElements() {
    const l = lang();
    const sys = liveState.system || {};
    const ets2 = liveState.ets2 || {};
    const now = new Date();
    document.querySelectorAll("#deckGrid .widget-tile[data-widget-kind]").forEach((tile) => {
      const kind = tile.getAttribute("data-widget-kind");
      const valEl = tile.querySelector("[data-w-val]");
      const subEl = tile.querySelector("[data-w-sub]");
      const fillEl = tile.querySelector(".widget-bar-fill");
      if (kind === "clock") {
        if (valEl) valEl.textContent = now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
        if (subEl) subEl.textContent = now.toLocaleDateString([], { weekday: "short", day: "2-digit", month: "short" });
      } else if (kind === "cpu_ram") {
        const cpu = sys.cpu_pct ?? 0;
        if (valEl) valEl.textContent = `${cpu}%`;
        if (subEl) subEl.textContent = `RAM ${sys.ram_pct ?? 0}%`;
        if (fillEl) fillEl.style.width = `${cpu}%`;
      } else if (kind === "ets2_speed") {
        if (valEl) valEl.textContent = ets2.connected ? `${ets2.speed_kmh || 0}` : "—";
        if (subEl) {
          subEl.textContent = ets2.connected
            ? `km/h · ${ets2.gear || "N"}${ets2.cruise_kmh ? ` · CC ${ets2.cruise_kmh}` : ""}${ets2.park_brake ? " · (P)" : ""}`
            : t(l, "ets2Offline");
        }
      } else if (kind === "ets2_fuel") {
        if (valEl) valEl.textContent = ets2.connected ? `${ets2.fuel_pct || 0}%` : "—";
        if (subEl) subEl.textContent = ets2.connected ? `${ets2.rpm || 0} RPM` : t(l, "ets2Offline");
        if (fillEl) fillEl.style.width = `${ets2.connected ? (ets2.fuel_pct || 0) : 0}%`;
      }
    });
    const page = currentPage();
    if (!page) return;
    document.querySelectorAll("#deckGrid .deck-cell[data-btn-id]").forEach((cell) => {
      const btn = page.buttons.find((b) => b.id === cell.getAttribute("data-btn-id"));
      if (btn) applyDesktopLiveIndicator(cell, btn, l);
    });
  }

  function renderGridStage() {
    const l = lang();
    const page = currentPage();
    if (!page) return;
    $("stagePageTitle").textContent = resolveLocalized(page, "name", l);

    const dims = ["2x3", "3x4", "3x5", "4x4", "4x5"];
    const currentDim = `${page.rows}x${page.cols}`;
    if (!dims.includes(currentDim)) dims.push(currentDim);
    const dimSelect = $("gridDimensionSelect");
    dimSelect.innerHTML = "";
    dims.forEach((d) => {
      const opt = document.createElement("option");
      opt.value = d;
      opt.textContent = d.replace("x", " × ");
      opt.selected = d === currentDim;
      dimSelect.appendChild(opt);
    });

    $("pageSettingsCard").style.display = pageSettingsOpen ? "flex" : "none";
    $("pageNameInput").value = resolveLocalized(page, "name", l);
    $("pageExeInput").value = (page.match_executables || []).join(", ");
    $("deletePageBtn").disabled = state.config.pages.length <= 1;

    const skinSelect = $("pageSkinSelect");
    skinSelect.innerHTML = "";
    ["default", "truck", "blocky", "studio", "tactical", "audio"].forEach((sk) => {
      const opt = document.createElement("option");
      opt.value = sk;
      opt.textContent = t(l, `skin_${sk}`);
      opt.selected = (page.skin || "default") === sk;
      skinSelect.appendChild(opt);
    });

    const skin = page.skin || "default";
    const vizOn = Boolean(page.audio_viz_enabled);
    $("deckStageFrame").setAttribute("data-skin", skin);
    $("deckStageFrame").setAttribute("data-audio-viz", vizOn ? "on" : "off");
    $("audioVizEnabledCheck").checked = vizOn;
    audioViz.setMode(page.audio_viz_mode || "bars");
    audioViz.setActive(vizOn);

    renderAudioSettingsRow(page, vizOn);

    const grid = $("deckGrid");
    grid.style.gridTemplateColumns = `repeat(${page.cols}, minmax(0, 1fr))`;
    grid.innerHTML = "";

    const covered = new Set();
    page.buttons.forEach((b) => {
      const rs = Math.max(1, Math.min(2, b.row_span || 1));
      const cs = Math.max(1, Math.min(2, b.col_span || 1));
      for (let dr = 0; dr < rs; dr++) {
        for (let dc = 0; dc < cs; dc++) {
          if (dr !== 0 || dc !== 0) covered.add(`${b.row + dr},${b.col + dc}`);
        }
      }
    });

    const btnAt = (r, c) => page.buttons.find((b) => b.row === r && b.col === c) || null;

    for (let r = 0; r < page.rows; r++) {
      for (let c = 0; c < page.cols; c++) {
        if (covered.has(`${r},${c}`)) continue;
        const btn = btnAt(r, c);
        const cell = document.createElement("button");
        cell.type = "button";
        const tier = r === 0 ? "top" : (r === page.rows - 1 ? "deep" : "mid");
        cell.dataset.tier = tier;
        const rs = btn ? Math.max(1, Math.min(2, btn.row_span || 1)) : 1;
        const cs = btn ? Math.max(1, Math.min(2, btn.col_span || 1)) : 1;
        cell.style.gridColumn = `${c + 1} / span ${cs}`;
        cell.style.gridRow = `${r + 1} / span ${rs}`;

        const isSelected = selectedCell && selectedCell.row === r && selectedCell.col === c;
        cell.className = "deck-cell";
        if (btn) {
          cell.dataset.btnId = btn.id;
          cell.classList.add("filled", `tone-${btn.tone || "default"}`);
          if (cs > 1) cell.classList.add("span-wide");
          cell.draggable = true;
        }
        if (isSelected) cell.classList.add("selected");

        if (btn && btn.action?.type === "widget") {
          cell.innerHTML = buildDesktopWidgetHtml(btn, l);
        } else if (btn) {
          const led = document.createElement("span");
          led.className = "live-led";
          const iconEl = document.createElement("div");
          iconEl.className = "deck-cell-icon";
          iconEl.innerHTML = Icons.svg(btn.icon);
          const lbl = document.createElement("div");
          lbl.className = "deck-cell-label";
          lbl.textContent = resolveLocalized(btn, "label", l) || t(l, `act_${btn.action.type}`);
          const sub = document.createElement("div");
          sub.className = "deck-cell-sub";
          sub.textContent = btn.action.type === "hotkey" || btn.action.type === "hold_key"
            ? (btn.action.keys || []).join("+")
            : t(l, `act_${btn.action.type}`);
          cell.append(led, iconEl, lbl, sub);
          applyDesktopLiveIndicator(cell, btn, l);
        } else {
          const plus = document.createElement("span");
          plus.className = "empty-plus";
          plus.textContent = "+";
          cell.appendChild(plus);
        }

        cell.onclick = () => selectCell(r, c, btn);

        cell.addEventListener("dragstart", (e) => {
          if (!btn) {
            e.preventDefault();
            return;
          }
          dragSource = { row: r, col: c };
          e.dataTransfer.effectAllowed = "move";
          e.dataTransfer.setData("text/plain", `${r},${c}`);
        });
        cell.addEventListener("dragover", (e) => {
          if (!dragSource) return;
          e.preventDefault();
          cell.classList.add("drag-over");
        });
        cell.addEventListener("dragleave", () => cell.classList.remove("drag-over"));
        cell.addEventListener("drop", async (e) => {
          e.preventDefault();
          cell.classList.remove("drag-over");
          if (!dragSource) return;
          const src = dragSource;
          dragSource = null;
          if (src.row === r && src.col === c) return;
          try {
            state = await api("POST", `/api/desktop/pages/${page.id}/swap`, {
              from_row: src.row,
              from_col: src.col,
              to_row: r,
              to_col: c,
            });
            selectedCell = { row: r, col: c, buttonId: btnAtAfter(r, c) };
            renderAll();
          } catch (err) {
            showSavedToast(err.message);
          }
        });

        grid.appendChild(cell);
      }
    }
  }

  function renderAudioSettingsRow(page, vizEnabled, liveSources) {
    const l = lang();
    const row = $("audioSettingsRow");
    row.style.display = vizEnabled ? "grid" : "none";

    const vizSelect = $("audioVizModeSelect");
    vizSelect.innerHTML = "";
    ["bars", "rainbow", "neon", "breathe"].forEach((mode) => {
      const opt = document.createElement("option");
      opt.value = mode;
      opt.textContent = t(l, `viz_${mode}`);
      opt.selected = (page.audio_viz_mode || "bars") === mode;
      vizSelect.appendChild(opt);
    });

    const srcSelect = $("audioSourceSelect");
    const currentSrc = (page.audio_source_exe || "").toLowerCase();
    const sources = Array.isArray(liveSources) ? liveSources : (state.audio_sources || []);
    const merged = [...new Set(currentSrc ? [currentSrc, ...sources] : sources)];
    srcSelect.innerHTML = "";
    const allOpt = document.createElement("option");
    allOpt.value = "";
    allOpt.textContent = t(l, "audioSourceAll");
    allOpt.selected = !currentSrc;
    srcSelect.appendChild(allOpt);
    merged.forEach((exe) => {
      const opt = document.createElement("option");
      opt.value = exe;
      opt.textContent = exe;
      opt.selected = exe === currentSrc;
      srcSelect.appendChild(opt);
    });
  }

  async function refreshAudioSources() {
    const page = currentPage();
    if (!page) return;
    try {
      const res = await api("GET", "/api/desktop/audio-sources");
      state.audio_sources = res.sources || [];
      renderAudioSettingsRow(page, $("audioVizEnabledCheck").checked, state.audio_sources);
    } catch (_) {}
  }

  async function refreshDetectedApps() {
    try {
      const res = await api("GET", "/api/desktop/detected-apps");
      state.detected_apps = res.apps || [];
      renderDetectedAppSelect();
    } catch (_) {}
  }

  function renderDetectedAppSelect() {
    const l = lang();
    const sel = $("detectedAppSelect");
    if (!sel) return;
    sel.innerHTML = "";
    const ph = document.createElement("option");
    ph.value = "";
    ph.textContent = t(l, "detectedAppsPlaceholder");
    sel.appendChild(ph);
    (state.detected_apps || []).forEach((app) => {
      const opt = document.createElement("option");
      opt.value = app.path;
      opt.textContent = app.name;
      sel.appendChild(opt);
    });
  }

  function renderSoundSelect(selectedSound) {
    const l = lang();
    const sel = $("soundFileSelect");
    sel.innerHTML = "";
    const sounds = state.sounds || [];
    if (!sounds.length) {
      const empty = document.createElement("option");
      empty.value = "";
      empty.textContent = t(l, "noSoundsYet");
      sel.appendChild(empty);
      return;
    }
    sounds.forEach((name) => {
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name;
      opt.selected = name === selectedSound;
      sel.appendChild(opt);
    });
  }

  function updateObsVisibility() {
    const cmd = $("obsCommandSelect").value;
    $("obsSceneWrap").style.display = cmd === "switch_scene" ? "flex" : "none";
    $("obsInputWrap").style.display = cmd === "toggle_mute" ? "flex" : "none";
  }

  async function fetchObsResources(saveFirst = true) {
    const l = lang();
    try {
      if (saveFirst) {
        const obs_port = Number($("obsPortInput").value) || 4455;
        const obs_password = $("obsPasswordInput").value || "";
        state = await api("PUT", "/api/desktop/settings", { obs_port, obs_password });
      }
      const res = await api("GET", "/api/desktop/obs/scenes");
      const sceneList = $("obsSceneList");
      const inputList = $("obsInputList");
      sceneList.innerHTML = "";
      inputList.innerHTML = "";
      (res.scenes || []).forEach((s) => {
        const opt = document.createElement("option");
        opt.value = s;
        sceneList.appendChild(opt);
      });
      (res.inputs || []).forEach((inp) => {
        const opt = document.createElement("option");
        opt.value = inp;
        inputList.appendChild(opt);
      });
      $("obsStatusText").textContent = res.connected
        ? `${t(l, "obsConnected")} (${(res.scenes || []).length})`
        : t(l, "obsDisconnected");
    } catch (_) {
      $("obsStatusText").textContent = t(l, "obsDisconnected");
    }
  }

  function btnAtAfter(r, c) {
    const page = currentPage();
    const b = page && page.buttons.find((x) => x.row === r && x.col === c);
    return b ? b.id : null;
  }

  function selectCell(row, col, btn) {
    recordingKeys = false;
    $("keyRecorderBox").style.display = "none";
    selectedCell = { row, col, buttonId: btn ? btn.id : null };
    const l = lang();
    $("emptyInspectorHint").style.display = "none";
    $("inspectorForm").style.display = "flex";

    $("btnLabelInput").value = btn ? resolveLocalized(btn, "label", l) : "";
    selectedIcon = btn ? btn.icon : "bolt";
    selectedTone = btn ? btn.tone : "default";
    selectedSize = btn ? `${btn.row_span || 1}x${btn.col_span || 1}` : "1x1";
    const action = btn ? btn.action : { type: "hotkey", keys: [], steps: [] };
    draftKeys = [...(action.keys || [])];
    draftMacro = (action.steps || []).map((s) => ({ ...s, keys: [...(s.keys || [])] }));

    renderInspectorPickers(action);
    renderGridStage();
  }

  function renderInspectorPickers(actionOverride) {
    const l = lang();
    const page = currentPage();
    const existing = selectedCell && page
      ? page.buttons.find((b) => b.row === selectedCell.row && b.col === selectedCell.col)
      : null;
    const actionType = actionOverride ? actionOverride.type : $("actionTypeSelect").value || "hotkey";

    const iconPicker = $("iconPicker");
    iconPicker.innerHTML = "";
    Icons.NAMES.forEach((name) => {
      const el = document.createElement("button");
      el.type = "button";
      el.className = "icon-choice" + (name === selectedIcon ? " active" : "");
      el.innerHTML = Icons.svg(name);
      el.onclick = () => {
        selectedIcon = name;
        renderInspectorPickers();
      };
      iconPicker.appendChild(el);
    });

    const toneRow = $("toneRow");
    toneRow.innerHTML = "";
    [
      ["default", "toneDefault", "background:var(--surface);color:var(--ink)"],
      ["accent", "toneAccent", "background:var(--accent);color:var(--accent-ink)"],
      ["ink", "toneInk", "background:var(--tile-ink);color:var(--tile-ink-text)"],
    ].forEach(([tone, key, style]) => {
      const el = document.createElement("button");
      el.type = "button";
      el.className = "tone-btn" + (selectedTone === tone ? " active" : "");
      el.setAttribute("style", style);
      el.textContent = t(l, key);
      el.onclick = () => {
        selectedTone = tone;
        renderInspectorPickers();
      };
      toneRow.appendChild(el);
    });

    const sizeSelect = $("btnSizeSelect");
    sizeSelect.innerHTML = "";
    ["1x1", "1x2", "2x1"].forEach((sz) => {
      const opt = document.createElement("option");
      opt.value = sz;
      opt.textContent = t(l, `size_${sz}`);
      opt.selected = sz === selectedSize;
      sizeSelect.appendChild(opt);
    });

    const actSelect = $("actionTypeSelect");
    actSelect.innerHTML = "";
    [
      "hotkey",
      "hold_key",
      "media",
      "play_sound",
      "launch_app",
      "obs_action",
      "widget",
      "macro",
      "open_url",
      "switch_page",
      "noop",
    ].forEach((kind) => {
      const opt = document.createElement("option");
      opt.value = kind;
      opt.textContent = t(l, `act_${kind}`);
      opt.selected = kind === actionType;
      actSelect.appendChild(opt);
    });

    const mediaSelect = $("mediaActionSelect");
    const prevMedia = mediaSelect.value;
    const widgetSelect = $("widgetKindSelect");
    const prevWidget = widgetSelect.value;
    const targetSelect = $("targetPageSelect");
    const prevTarget = targetSelect.value;
    const obsCmdSelect = $("obsCommandSelect");
    const prevObsCmd = obsCmdSelect.value;

    widgetSelect.innerHTML = "";
    ["clock", "cpu_ram", "ets2_speed", "ets2_fuel"].forEach((wk) => {
      const opt = document.createElement("option");
      opt.value = wk;
      opt.textContent = t(l, `widget_${wk}`);
      opt.selected = actionOverride ? (actionOverride.widget_kind || "clock") === wk : prevWidget === wk;
      widgetSelect.appendChild(opt);
    });

    mediaSelect.innerHTML = "";
    (state.media_actions || []).forEach((m) => {
      const opt = document.createElement("option");
      opt.value = m;
      opt.textContent = t(l, `media_${m}`);
      opt.selected = actionOverride ? actionOverride.media_action === m : prevMedia === m;
      mediaSelect.appendChild(opt);
    });

    targetSelect.innerHTML = "";
    state.config.pages.forEach((p) => {
      const opt = document.createElement("option");
      opt.value = p.id;
      opt.textContent = resolveLocalized(p, "name", l);
      if (actionOverride ? actionOverride.target_page_id === p.id : prevTarget === p.id) opt.selected = true;
      targetSelect.appendChild(opt);
    });

    obsCmdSelect.innerHTML = "";
    [
      "switch_scene",
      "toggle_record",
      "toggle_stream",
      "save_replay",
      "toggle_mute",
      "toggle_studio_mode",
    ].forEach((cmd) => {
      const opt = document.createElement("option");
      opt.value = cmd;
      opt.textContent = t(l, `obs_${cmd}`);
      opt.selected = actionOverride ? actionOverride.obs_command === cmd : prevObsCmd === cmd;
      obsCmdSelect.appendChild(opt);
    });

    if (actionOverride) {
      $("urlInput").value = actionOverride.url || "";
      $("appPathInput").value = actionOverride.app_path || "";
      $("obsSceneInput").value = actionOverride.obs_scene || "";
      $("obsInputName").value = actionOverride.obs_input || "";
      renderSoundSelect(actionOverride.sound_file || "");
    } else {
      renderSoundSelect($("soundFileSelect").value || "");
    }
    renderDetectedAppSelect();
    $("obsPortInput").value = state.config.settings.obs_port || 4455;
    $("obsPasswordInput").value = state.config.settings.obs_password || "";

    $("keysSection").style.display = ["hotkey", "hold_key", "macro"].includes(actionType) ? "flex" : "none";
    $("mediaSection").style.display = actionType === "media" ? "flex" : "none";
    $("urlSection").style.display = actionType === "open_url" ? "flex" : "none";
    $("switchPageSection").style.display = actionType === "switch_page" ? "flex" : "none";
    $("soundSection").style.display = actionType === "play_sound" ? "flex" : "none";
    $("launchAppSection").style.display = actionType === "launch_app" ? "flex" : "none";
    $("obsSection").style.display = actionType === "obs_action" ? "flex" : "none";
    $("widgetSection").style.display = actionType === "widget" ? "flex" : "none";
    $("macroSection").style.display = actionType === "macro" ? "flex" : "none";
    updateObsVisibility();

    if (actionType === "launch_app" && !state.detected_apps) refreshDetectedApps();
    if (actionType === "obs_action") fetchObsResources(false);

    $("testButtonBtn").disabled = !existing;
    $("deleteButtonBtn").disabled = !existing;

    renderKeyChips();
    renderMacroList();
  }

  function renderKeyChips() {
    const wrap = $("keyChips");
    wrap.innerHTML = "";
    draftKeys.forEach((k) => {
      const chip = document.createElement("span");
      chip.className = "key-chip";
      chip.textContent = k.toUpperCase();
      wrap.appendChild(chip);
    });
    ["ctrl", "shift", "alt", "win", "space", "enter"].forEach((quick) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn btn-sm";
      btn.textContent = `+ ${quick}`;
      btn.onclick = () => {
        if (!draftKeys.includes(quick) && draftKeys.length < 6) {
          draftKeys.push(quick);
          renderKeyChips();
        }
      };
      wrap.appendChild(btn);
    });
  }

  function renderMacroList() {
    const list = $("macroList");
    list.innerHTML = "";
    draftMacro.forEach((step, idx) => {
      const row = document.createElement("div");
      row.className = "macro-row";
      const label = document.createElement("span");
      label.textContent = step.kind === "delay"
        ? `Delay ${step.delay_ms} ms`
        : `${step.kind}: ${(step.keys || []).join("+")}`;
      const rm = document.createElement("button");
      rm.type = "button";
      rm.className = "btn btn-sm";
      rm.textContent = "×";
      rm.onclick = () => {
        draftMacro.splice(idx, 1);
        renderMacroList();
      };
      row.append(label, rm);
      list.appendChild(row);
    });
  }

  function renderAll() {
    if (!state) return;
    applyStaticTranslations();
    renderTopBar();
    renderPagesSidebar();
    renderForegroundBox();
    renderQrAndDevices();
    renderGridStage();
  }

  async function updateSettings(patch) {
    state = await api("PUT", "/api/desktop/settings", patch);
    selectedCell = null;
    $("inspectorForm").style.display = "none";
    $("emptyInspectorHint").style.display = "block";
    renderAll();
  }

  function showSavedToast(msg) {
    const el = $("saveStatus");
    el.textContent = msg || t(lang(), "savedToast");
    setTimeout(() => {
      if (el.textContent === (msg || t(lang(), "savedToast"))) el.textContent = "";
    }, 1800);
  }

  function connectDesktopWs() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(`${proto}//${location.host}/ws/desktop?token=${encodeURIComponent(TOKEN)}`);
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.type === "state_sync" && msg.state) {
        state = msg.state;
        renderAll();
      } else if (msg.type === "devices_updated" && state) {
        state.devices = msg.devices;
        state.connected_count = msg.connected_count;
        renderTopBar();
        renderQrAndDevices();
      } else if (msg.type === "foreground_updated" && state) {
        state.foreground = msg.foreground;
        renderForegroundBox();
      } else if (msg.type === "pairing_consumed" && state) {
        state.pairing = msg.pairing;
        state.devices = msg.devices;
        state.connected_count = msg.connected_count;
        renderTopBar();
        renderQrAndDevices();
      } else if (msg.type === "audio_level") {
        audioViz.pushLevel(msg);
      } else if (msg.type === "live_state" && msg.live) {
        liveState = msg.live;
        updateDesktopLiveElements();
      } else if (msg.type === "mixer_state" && msg.mixer) {
        desktopMixerState = msg.mixer;
        if ($("mixerModal").classList.contains("open")) renderDesktopMixer();
      }
    };
    ws.onclose = () => setTimeout(connectDesktopWs, 1500);
  }

  function renderDesktopMixer() {
    const l = lang();
    const list = $("desktopMixerList");
    if (!list || !desktopMixerState) return;
    list.innerHTML = "";
    const entries = [
      { target: null, title: t(l, "mixerMaster"), ...desktopMixerState.master },
      ...(desktopMixerState.apps || []).map((a) => ({ target: a.exe, title: a.exe, ...a })),
    ];
    entries.forEach((item) => {
      const row = document.createElement("div");
      row.className = "mixer-row";
      const head = document.createElement("div");
      head.className = "mixer-row-head";
      const name = document.createElement("span");
      name.className = "mixer-app-name";
      name.textContent = item.title;
      const muteBtn = document.createElement("button");
      muteBtn.type = "button";
      muteBtn.className = "btn btn-sm" + (item.muted ? " btn-muted" : "");
      muteBtn.textContent = item.muted ? t(l, "liveMuted") : t(l, "media_mute");
      muteBtn.onclick = async () => {
        desktopMixerState = await api("POST", "/api/desktop/mixer/mute", {
          exe: item.target,
          muted: !item.muted,
        });
        renderDesktopMixer();
      };
      head.append(name, muteBtn);

      const controls = document.createElement("div");
      controls.className = "mixer-controls";
      const slider = document.createElement("input");
      slider.type = "range";
      slider.min = "0";
      slider.max = "100";
      slider.value = String(Math.round((item.volume || 0) * 100));
      slider.className = "mixer-slider";
      const pct = document.createElement("span");
      pct.className = "mixer-pct";
      pct.textContent = `${slider.value}%`;

      let timer = null;
      slider.addEventListener("input", () => {
        const val = Number(slider.value);
        pct.textContent = `${val}%`;
        if (timer) clearTimeout(timer);
        timer = setTimeout(() => {
          api("POST", "/api/desktop/mixer/volume", { exe: item.target, volume: val / 100 }).catch(() => {});
        }, 45);
      });
      slider.addEventListener("change", async () => {
        desktopMixerState = await api("POST", "/api/desktop/mixer/volume", {
          exe: item.target,
          volume: Number(slider.value) / 100,
        });
      });

      controls.append(slider, pct);
      row.append(head, controls);
      list.appendChild(row);
    });
    if (!(desktopMixerState.apps || []).length) {
      const empty = document.createElement("div");
      empty.className = "hint-text";
      empty.textContent = t(l, "mixerNoApps");
      list.appendChild(empty);
    }
  }

  function bindEvents() {
    $("openMixerBtn").onclick = async () => {
      $("mixerModal").classList.add("open");
      desktopMixerState = await api("GET", "/api/desktop/mixer");
      renderDesktopMixer();
    };
    $("refreshDesktopMixerBtn").onclick = async () => {
      desktopMixerState = await api("GET", "/api/desktop/mixer");
      renderDesktopMixer();
    };
    $("closeMixerModalBtn").onclick = () => $("mixerModal").classList.remove("open");
    $("mixerModal").onclick = (e) => {
      if (e.target === $("mixerModal")) $("mixerModal").classList.remove("open");
    };
    $("autostartToggle").onclick = () =>
      updateSettings({ start_with_windows: !state.config.settings.start_with_windows });
    $("autoSwitchToggle").onclick = () =>
      updateSettings({ auto_switch: !state.config.settings.auto_switch });
    $("themeToggle").onclick = () =>
      updateSettings({ theme: (state.config.settings.theme || "dark") === "dark" ? "light" : "dark" });
    $("contrastToggle").onclick = () =>
      updateSettings({ high_contrast: !state.config.settings.high_contrast });

    $("newPageBtn").onclick = async () => {
      const id = "page-" + Date.now().toString(36);
      const newPage = {
        id,
        name: t(lang(), "newPageDefault"),
        name_i18n: { [lang()]: t(lang(), "newPageDefault") },
        skin: "default",
        rows: 3,
        cols: 4,
        match_executables: [],
        match_title_contains: [],
        buttons: [],
      };
      state = await api("POST", "/api/desktop/pages", newPage);
      state = await api("PUT", "/api/desktop/settings", { active_page_id: id });
      selectedCell = null;
      renderAll();
    };

    $("openTemplatesBtn").onclick = async () => {
      const l = lang();
      const res = await api("GET", "/api/desktop/templates");
      const grid = $("templateGrid");
      grid.innerHTML = "";
      (res.templates || []).forEach((tpl) => {
        const card = document.createElement("div");
        card.className = "template-card";
        const exes = [...(tpl.match_executables || []), ...(tpl.match_title_contains || [])].join(", ");
        card.innerHTML = `
          <div>
            <div style="display:flex;align-items:center;justify-content:space-between;gap:6px">
              <strong></strong>
              <span class="skin-tag">${t(l, `skin_${tpl.skin || "default"}`)}</span>
            </div>
            <p class="hint-text" style="margin:6px 0 4px"></p>
            <div class="hint-text" style="font-family:monospace;font-size:11px"></div>
          </div>
          <button type="button" class="btn btn-sm btn-accent">${t(l, "installTemplate")}</button>
        `;
        card.querySelector("strong").textContent = resolveLocalized(tpl, "name", l);
        card.querySelectorAll("p")[0].textContent = resolveLocalized(tpl, "description", l);
        card.querySelectorAll("div")[2].textContent = exes;
        card.querySelector("button").onclick = async () => {
          state = await api("POST", `/api/desktop/templates/${tpl.id}/install`);
          $("templateModal").classList.remove("open");
          selectedCell = null;
          renderAll();
        };
        grid.appendChild(card);
      });
      $("templateModal").classList.add("open");
    };

    $("closeTemplatesBtn").onclick = () => $("templateModal").classList.remove("open");
    $("templateModal").onclick = (e) => {
      if (e.target === $("templateModal")) $("templateModal").classList.remove("open");
    };

    $("bindExeBtn").onclick = async () => {
      const page = currentPage();
      const exe = state?.foreground?.exe;
      if (!page || !exe) return;
      const updated = {
        ...page,
        match_executables: [...new Set([...(page.match_executables || []), exe.toLowerCase()])],
      };
      state = await api("PUT", `/api/desktop/pages/${page.id}`, updated);
      renderAll();
    };

    $("refreshQrBtn").onclick = async () => {
      state.pairing = await api("POST", "/api/desktop/pairing/refresh");
      renderQrAndDevices();
    };

    $("copyQrUrlBtn").onclick = async () => {
      if (!state?.pairing?.url) return;
      await navigator.clipboard?.writeText(state.pairing.url).catch(() => {});
      const btn = $("copyQrUrlBtn");
      btn.textContent = t(lang(), "copied");
      setTimeout(() => (btn.textContent = t(lang(), "copyLink")), 1500);
    };

    $("togglePageSettingsBtn").onclick = () => {
      pageSettingsOpen = !pageSettingsOpen;
      renderGridStage();
      if (pageSettingsOpen && $("audioVizEnabledCheck").checked) {
        refreshAudioSources();
      }
    };

    $("audioVizEnabledCheck").onchange = (e) => {
      const page = currentPage();
      if (!page) return;
      const on = e.target.checked;
      renderAudioSettingsRow(page, on);
      if (on) refreshAudioSources();
    };

    $("pageSkinSelect").onchange = (e) => {
      const nextSkin = e.target.value;
      if (nextSkin === "audio" && !$("audioVizEnabledCheck").checked) {
        $("audioVizEnabledCheck").checked = true;
        renderAudioSettingsRow(currentPage(), true);
        refreshAudioSources();
      }
    };

    $("refreshAudioSourcesBtn").onclick = () => refreshAudioSources();

    $("gridDimensionSelect").onchange = async (e) => {
      const page = currentPage();
      if (!page) return;
      const [rows, cols] = e.target.value.split("x").map(Number);
      state = await api("PUT", `/api/desktop/pages/${page.id}`, { ...page, rows, cols });
      renderAll();
    };

    $("savePageSettingsBtn").onclick = async () => {
      const page = currentPage();
      if (!page) return;
      const name = $("pageNameInput").value.trim() || page.name;
      const exes = $("pageExeInput").value
        .split(",")
        .map((s) => s.trim().toLowerCase())
        .filter(Boolean);
      const nextI18n = { ...(page.name_i18n || {}), [lang()]: name };
      state = await api("PUT", `/api/desktop/pages/${page.id}`, {
        ...page,
        name,
        name_i18n: nextI18n,
        skin: $("pageSkinSelect").value || "default",
        audio_viz_enabled: $("audioVizEnabledCheck").checked,
        audio_source_exe: $("audioSourceSelect").value || "",
        audio_viz_mode: $("audioVizModeSelect").value || "bars",
        match_executables: exes,
      });
      pageSettingsOpen = false;
      renderAll();
      showSavedToast();
    };

    $("exportPageBtn").onclick = async () => {
      const page = currentPage();
      if (!page) return;
      const exported = await api("GET", `/api/desktop/pages/${page.id}/export`);
      const blob = new Blob([JSON.stringify(exported, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${page.id}.localdeck.json`;
      a.click();
      URL.revokeObjectURL(url);
    };

    $("importPageBtn").onclick = () => $("importFileInput").click();
    $("importFileInput").onchange = async (e) => {
      const file = e.target.files?.[0];
      e.target.value = "";
      if (!file) return;
      try {
        const parsed = JSON.parse(await file.text());
        state = await api("POST", "/api/desktop/pages/import", parsed);
        pageSettingsOpen = false;
        selectedCell = null;
        renderAll();
        showSavedToast(t(lang(), "importedToast"));
      } catch (_) {
        showSavedToast(t(lang(), "importFailed"));
      }
    };

    $("deletePageBtn").onclick = async () => {
      const page = currentPage();
      if (!page || state.config.pages.length <= 1) return;
      state = await api("DELETE", `/api/desktop/pages/${page.id}`);
      pageSettingsOpen = false;
      selectedCell = null;
      renderAll();
    };

    $("btnSizeSelect").onchange = (e) => { selectedSize = e.target.value || "1x1"; };
    $("actionTypeSelect").onchange = () => renderInspectorPickers();
    $("obsCommandSelect").onchange = () => updateObsVisibility();
    $("fetchObsBtn").onclick = () => fetchObsResources(true);
    $("detectedAppSelect").onchange = (e) => {
      if (e.target.value) $("appPathInput").value = e.target.value;
    };
    $("uploadSoundBtn").onclick = () => $("soundUploadInput").click();
    $("soundUploadInput").onchange = async (e) => {
      const file = e.target.files?.[0];
      e.target.value = "";
      if (!file) return;
      const reader = new FileReader();
      reader.onload = async () => {
        try {
          const res = await api("POST", "/api/desktop/sounds/upload", {
            filename: file.name,
            data_base64: String(reader.result || ""),
          });
          state.sounds = res.sounds || [];
          renderSoundSelect(res.filename);
          showSavedToast();
        } catch (err) {
          showSavedToast(err.message);
        }
      };
      reader.readAsDataURL(file);
    };

    $("recordKeysBtn").onclick = () => {
      recordingKeys = true;
      draftKeys = [];
      renderKeyChips();
      $("keyRecorderBox").style.display = "block";
    };

    $("clearKeysBtn").onclick = () => {
      draftKeys = [];
      renderKeyChips();
    };

    window.addEventListener("keydown", (e) => {
      if (!recordingKeys) return;
      e.preventDefault();
      e.stopPropagation();
      if (e.code === "Escape") {
        recordingKeys = false;
        $("keyRecorderBox").style.display = "none";
        return;
      }
      const mapped = mapBrowserKey(e);
      if (mapped && !draftKeys.includes(mapped) && draftKeys.length < 6) {
        draftKeys.push(mapped);
        renderKeyChips();
      }
      if (mapped && !["ctrl", "rctrl", "shift", "rshift", "alt", "ralt", "win", "rwin"].includes(mapped)) {
        recordingKeys = false;
        $("keyRecorderBox").style.display = "none";
      }
    }, true);

    $("addMacroHotkeyBtn").onclick = () => {
      if (!draftKeys.length || draftMacro.length >= 10) return;
      draftMacro.push({ kind: "hotkey", keys: [...draftKeys], delay_ms: 0 });
      renderMacroList();
    };

    $("addMacroDelayBtn").onclick = () => {
      if (draftMacro.length >= 10) return;
      draftMacro.push({ kind: "delay", keys: [], delay_ms: 100 });
      renderMacroList();
    };

    $("inspectorForm").onsubmit = async (e) => {
      e.preventDefault();
      const page = currentPage();
      if (!page || !selectedCell) return;
      const existing = page.buttons.find(
        (b) => b.row === selectedCell.row && b.col === selectedCell.col
      );
      const buttonId = (existing && existing.id) || `btn-${Date.now().toString(36)}`;
      const label = $("btnLabelInput").value.trim() || "Key";
      const actionType = $("actionTypeSelect").value;
      const [rowSpan, colSpan] = ($("btnSizeSelect").value || "1x1").split("x").map(Number);
      selectedSize = `${rowSpan}x${colSpan}`;
      const payload = {
        id: buttonId,
        row: selectedCell.row,
        col: selectedCell.col,
        row_span: rowSpan || 1,
        col_span: colSpan || 1,
        label,
        label_i18n: { ...(existing?.label_i18n || {}), [lang()]: label },
        icon: selectedIcon,
        tone: selectedTone,
        action: {
          type: actionType,
          keys: ["hotkey", "hold_key"].includes(actionType) ? draftKeys : [],
          media_action: actionType === "media" ? $("mediaActionSelect").value : null,
          url: actionType === "open_url" ? $("urlInput").value.trim() : null,
          target_page_id: actionType === "switch_page" ? $("targetPageSelect").value : null,
          sound_file: actionType === "play_sound" ? ($("soundFileSelect").value || null) : null,
          app_path: actionType === "launch_app" ? ($("appPathInput").value.trim() || null) : null,
          obs_command: actionType === "obs_action" ? $("obsCommandSelect").value : null,
          obs_scene: actionType === "obs_action" ? ($("obsSceneInput").value.trim() || null) : null,
          obs_input: actionType === "obs_action" ? ($("obsInputName").value.trim() || null) : null,
          widget_kind: actionType === "widget" ? ($("widgetKindSelect").value || "clock") : null,
          steps: actionType === "macro" ? draftMacro : [],
        },
      };
      try {
        state = await api(
          "PUT",
          `/api/desktop/pages/${page.id}/buttons/${buttonId}`,
          payload
        );
        selectedCell.buttonId = buttonId;
        renderAll();
        showSavedToast();
      } catch (err) {
        showSavedToast(err.message);
      }
    };

    $("testButtonBtn").onclick = async () => {
      const page = currentPage();
      if (!page || !selectedCell?.buttonId) return;
      const res = await api("POST", `/api/desktop/pages/${page.id}/test-button/${selectedCell.buttonId}`);
      if (res && res.ok === false && res.error) showSavedToast(res.error);
    };

    $("deleteButtonBtn").onclick = async () => {
      const page = currentPage();
      if (!page || !selectedCell?.buttonId) return;
      state = await api("DELETE", `/api/desktop/pages/${page.id}/buttons/${selectedCell.buttonId}`);
      selectedCell = null;
      $("inspectorForm").style.display = "none";
      $("emptyInspectorHint").style.display = "block";
      renderAll();
    };
  }

  setInterval(() => {
    if (state) updateDesktopLiveElements();
  }, 1000);

  setInterval(async () => {
    if (!state) return;
    if (qrCountdown > 0) {
      qrCountdown -= 1;
      $("qrMeta").textContent = `${t(lang(), "qrHint")} ${qrCountdown}s`;
    } else {
      try {
        state.pairing = await api("POST", "/api/desktop/pairing/refresh");
        renderQrAndDevices();
      } catch (_) {}
    }
  }, 1000);

  async function init() {
    bindEvents();
    state = await api("GET", "/api/desktop/state");
    renderAll();
    connectDesktopWs();
  }

  init().catch(console.error);
})();
