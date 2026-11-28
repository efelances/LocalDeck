(() => {
  const { t, resolveLocalized } = window.LocalDeckI18n;
  const Icons = window.LocalDeckIcons;

  let deckState = null;
  let liveState = { system: { cpu_pct: 0, ram_pct: 0 }, obs: {}, ets2: { connected: false }, master_muted: false };
  let mixerState = null;
  let ws = null;
  let reconnectMs = 500;
  let latencyMs = null;
  let wakeLock = null;
  let wakeLockWanted = false;
  let draggingTarget = null;
  const activeHolds = new Map();

  const $ = (id) => document.getElementById(id);
  const audioViz = window.LocalDeckAudioViz.create($("mobileAudioViz"));

  function escapeHtml(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function releaseActiveHolds() {
    if (!activeHolds.size) return;
    activeHolds.forEach((held) => {
      sendWs({ type: "hold_end", page_id: held.page_id, button_id: held.button_id });
    });
    activeHolds.clear();
  }

  function lang() {
    return (deckState && deckState.settings && deckState.settings.language) || "tr";
  }

  function activePage() {
    if (!deckState) return null;
    const pages = deckState.pages || [];
    return pages.find((p) => p.id === deckState.active_page_id) || pages[0] || null;
  }

  function setStatus(connected) {
    const l = lang();
    $("mobileStatusDot").className = "status-dot" + (connected ? " online" : "");
    if (!connected) {
      $("mobileStatusText").textContent = t(l, "mobileReconnecting");
      return;
    }
    $("mobileStatusText").textContent = latencyMs !== null
      ? `${t(l, "mobileConnected")} · ${latencyMs}ms`
      : t(l, "mobileConnected");
  }

  function vibrateIfEnabled() {
    if (deckState?.settings?.haptics && navigator.vibrate) {
      navigator.vibrate(15);
    }
  }

  function sendWs(payload) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(payload));
    }
  }

  function formatClock() {
    const d = new Date();
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  function formatDate() {
    const d = new Date();
    return d.toLocaleDateString([], { weekday: "short", day: "2-digit", month: "short" });
  }

  function buildWidgetInner(btn, l) {
    const kind = btn.action?.widget_kind || "clock";
    const lbl = escapeHtml(resolveLocalized(btn, "label", l) || t(l, `widget_${kind}`));
    const ets2 = liveState.ets2 || {};
    const sys = liveState.system || {};

    if (kind === "clock") {
      return `
        <div class="widget-tile" data-widget-kind="clock">
          <div class="widget-kicker">${lbl}</div>
          <div class="widget-value" data-w-val>${formatClock()}</div>
          <div class="widget-sub" data-w-sub>${formatDate()}</div>
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
    const barW = ets2.connected ? (ets2.fuel_pct || 0) : 0;
    return `
      <div class="widget-tile" data-widget-kind="ets2_fuel">
        <div class="widget-kicker">${lbl}</div>
        <div class="widget-value" data-w-val>${fuel}</div>
        <div class="widget-sub" data-w-sub>${rpmSub}</div>
        <div class="widget-bar"><div class="widget-bar-fill" style="width:${barW}%"></div></div>
      </div>
    `;
  }

  function updateWidgetsInPlace() {
    const l = lang();
    document.querySelectorAll(".widget-tile[data-widget-kind]").forEach((tile) => {
      const kind = tile.getAttribute("data-widget-kind");
      const valEl = tile.querySelector("[data-w-val]");
      const subEl = tile.querySelector("[data-w-sub]");
      const fillEl = tile.querySelector(".widget-bar-fill");
      const sys = liveState.system || {};
      const ets2 = liveState.ets2 || {};
      if (kind === "clock") {
        if (valEl) valEl.textContent = formatClock();
        if (subEl) subEl.textContent = formatDate();
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
  }

  function applyLiveIndicator(el, btn, l) {
    el.classList.remove("live-active-danger", "live-active-ok");
    const oldBadge = el.querySelector(".live-badge");
    if (oldBadge) oldBadge.remove();

    const act = btn.action || {};
    const obs = liveState.obs || {};
    let badgeText = "";
    if (act.type === "obs_action" && obs.connected) {
      if (act.obs_command === "toggle_record" && obs.recording) {
        el.classList.add("live-active-danger");
        badgeText = t(l, "liveRec");
      } else if (act.obs_command === "toggle_stream" && obs.streaming) {
        el.classList.add("live-active-danger");
        badgeText = t(l, "liveStream");
      } else if (act.obs_command === "switch_scene" && act.obs_scene && obs.scene === act.obs_scene) {
        el.classList.add("live-active-ok");
      } else if (act.obs_command === "toggle_studio_mode" && obs.studio_mode) {
        el.classList.add("live-active-ok");
      }
    } else if (act.type === "media" && act.media_action === "mute" && liveState.master_muted) {
      el.classList.add("live-active-danger");
      badgeText = t(l, "liveMuted");
    }

    if (badgeText) {
      const b = document.createElement("span");
      b.className = "live-badge";
      b.textContent = badgeText;
      el.appendChild(b);
    }
  }

  function updateAllLiveIndicators() {
    const page = activePage();
    if (!page) return;
    const l = lang();
    document.querySelectorAll(".mobile-btn[data-btn-id]").forEach((el) => {
      const btn = page.buttons.find((b) => b.id === el.getAttribute("data-btn-id"));
      if (btn) applyLiveIndicator(el, btn, l);
    });
  }

  function renderMixerDrawer() {
    const l = lang();
    $("mobileMixerTitle").textContent = t(l, "mixerTitle");
    $("mobileMixerRefreshBtn").textContent = t(l, "mixerRefresh");
    $("mobileMixerCloseBtn").textContent = t(l, "closeModal");

    const list = $("mobileMixerList");
    if (!mixerState) return;
    if (draggingTarget) return; // Keep active touch slider smooth while dragging

    list.innerHTML = "";
    const entries = [
      { target: null, title: t(l, "mixerMaster"), ...mixerState.master },
      ...(mixerState.apps || []).map((a) => ({ target: a.exe, title: a.exe, ...a })),
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
      muteBtn.onclick = () => {
        vibrateIfEnabled();
        sendWs({ type: "set_mute", exe: item.target, muted: !item.muted });
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
      const keyId = item.target || "__master__";
      slider.addEventListener("pointerdown", () => { draggingTarget = keyId; });
      const endDrag = () => {
        if (draggingTarget === keyId) draggingTarget = null;
      };
      slider.addEventListener("pointerup", endDrag);
      slider.addEventListener("pointercancel", endDrag);
      slider.addEventListener("input", () => {
        const val = Number(slider.value);
        pct.textContent = `${val}%`;
        if (timer) clearTimeout(timer);
        timer = setTimeout(() => {
          sendWs({ type: "set_volume", exe: item.target, volume: val / 100 });
        }, 40);
      });
      slider.addEventListener("change", () => {
        endDrag();
        sendWs({ type: "set_volume", exe: item.target, volume: Number(slider.value) / 100 });
      });

      controls.append(slider, pct);
      row.append(head, controls);
      list.appendChild(row);
    });

    if (!(mixerState.apps || []).length) {
      const hint = document.createElement("div");
      hint.className = "hint-text";
      hint.textContent = t(l, "mixerNoApps");
      list.appendChild(hint);
    }
  }

  function render() {
    if (!deckState) return;
    releaseActiveHolds();
    const l = lang();
    document.documentElement.lang = l;
    document.documentElement.setAttribute("data-theme", deckState.settings.theme || "dark");
    document.documentElement.setAttribute(
      "data-contrast",
      deckState.settings.high_contrast ? "high" : "normal"
    );

    const pagesEl = $("mobilePages");
    pagesEl.innerHTML = "";
    const page = activePage();
    (deckState.pages || []).forEach((p) => {
      const pill = document.createElement("button");
      pill.type = "button";
      pill.className = "mobile-page-pill" + (page && p.id === page.id ? " active" : "");
      pill.textContent = resolveLocalized(p, "name", l);
      pill.onclick = () => sendWs({ type: "select_page", page_id: p.id });
      pagesEl.appendChild(pill);
    });

    const grid = $("mobileGrid");
    grid.innerHTML = "";
    if (!page) {
      document.body.setAttribute("data-skin", "default");
      audioViz.setActive(false);
      return;
    }
    const skin = page.skin || "default";
    const vizOn = Boolean(page.audio_viz_enabled);
    document.body.setAttribute("data-skin", skin);
    document.body.setAttribute("data-audio-viz", vizOn ? "on" : "off");
    audioViz.setMode(page.audio_viz_mode || "bars");
    audioViz.setActive(vizOn);
    grid.style.gridTemplateColumns = `repeat(${page.cols}, minmax(0, 1fr))`;
    grid.style.gridTemplateRows = `repeat(${page.rows}, minmax(0, 1fr))`;

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
        const tier = r === 0 ? "top" : (r === page.rows - 1 ? "deep" : "mid");
        if (!btn) {
          const empty = document.createElement("div");
          empty.className = "mobile-btn-empty";
          empty.dataset.tier = tier;
          empty.style.gridColumn = `${c + 1} / span 1`;
          empty.style.gridRow = `${r + 1} / span 1`;
          grid.appendChild(empty);
          continue;
        }

        const rs = Math.max(1, Math.min(2, btn.row_span || 1));
        const cs = Math.max(1, Math.min(2, btn.col_span || 1));
        const isWidget = btn.action && btn.action.type === "widget";
        const el = document.createElement(isWidget ? "div" : "button");
        if (!isWidget) el.type = "button";
        el.dataset.tier = tier;
        el.dataset.btnId = btn.id;
        el.style.gridColumn = `${c + 1} / span ${cs}`;
        el.style.gridRow = `${r + 1} / span ${rs}`;
        el.className = `mobile-btn tone-${btn.tone || "default"}${cs > 1 ? " span-wide" : ""}`;

        if (isWidget) {
          el.classList.add("mobile-widget");
          el.innerHTML = buildWidgetInner(btn, l);
        } else {
          el.innerHTML = `
            <span class="live-led"></span>
            <div class="mobile-btn-icon">${Icons.svg(btn.icon)}</div>
            <div class="mobile-btn-label"></div>
          `;
          el.querySelector(".mobile-btn-label").textContent = resolveLocalized(btn, "label", l) || "";
          applyLiveIndicator(el, btn, l);

          const isHold = btn.action && btn.action.type === "hold_key";
          el.addEventListener("pointerdown", (e) => {
            e.preventDefault();
            el.classList.add("pressed");
            vibrateIfEnabled();
            if (isHold) {
              try { el.setPointerCapture(e.pointerId); } catch (_) {}
              activeHolds.set(e.pointerId, { page_id: page.id, button_id: btn.id });
              sendWs({ type: "hold_start", page_id: page.id, button_id: btn.id });
            } else {
              sendWs({ type: "tap", page_id: page.id, button_id: btn.id });
            }
          });

          const endHold = (e) => {
            el.classList.remove("pressed");
            const held = activeHolds.get(e.pointerId);
            if (held) {
              activeHolds.delete(e.pointerId);
              sendWs({ type: "hold_end", page_id: held.page_id, button_id: held.button_id });
            }
          };
          el.addEventListener("pointerup", endHold);
          el.addEventListener("pointercancel", endHold);
          el.addEventListener("lostpointercapture", endHold);
        }

        grid.appendChild(el);
      }
    }
  }

  async function requestWakeLock() {
    if (!("wakeLock" in navigator)) return;
    try {
      wakeLock = await navigator.wakeLock.request("screen");
      wakeLockWanted = true;
      $("wakeLockBtn").classList.add("active");
      wakeLock.addEventListener("release", () => {
        wakeLock = null;
        if (!wakeLockWanted) $("wakeLockBtn").classList.remove("active");
      });
    } catch (_) {}
  }

  async function releaseWakeLock() {
    wakeLockWanted = false;
    $("wakeLockBtn").classList.remove("active");
    if (wakeLock) {
      try { await wakeLock.release(); } catch (_) {}
      wakeLock = null;
    }
  }

  function connect() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(`${proto}//${location.host}/ws/deck`);
    ws.onopen = () => {
      reconnectMs = 500;
      setStatus(true);
      sendWs({ type: "ping", ts: performance.now() });
    };
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.type === "state_sync" && msg.state) {
        if (
          msg.state.asset_version &&
          window.__LOCALDECK_ASSET_V__ &&
          msg.state.asset_version !== window.__LOCALDECK_ASSET_V__
        ) {
          location.reload();
          return;
        }
        deckState = msg.state;
        setStatus(true);
        render();
        if ($("mobileMixerDrawer").classList.contains("open")) renderMixerDrawer();
      } else if (msg.type === "pong" && typeof msg.ts === "number") {
        latencyMs = Math.max(1, Math.round(performance.now() - msg.ts));
        setStatus(true);
      } else if (msg.type === "audio_level") {
        audioViz.pushLevel(msg);
      } else if (msg.type === "live_state" && msg.live) {
        liveState = msg.live;
        updateWidgetsInPlace();
        updateAllLiveIndicators();
      } else if (msg.type === "mixer_state" && msg.mixer) {
        mixerState = msg.mixer;
        renderMixerDrawer();
      }
    };
    ws.onclose = (ev) => {
      if (ev.code === 4401 || ev.code === 4403) {
        location.reload();
        return;
      }
      setStatus(false);
      setTimeout(connect, reconnectMs);
      reconnectMs = Math.min(4000, reconnectMs * 1.5);
    };
  }

  $("mobileMixerBtn").onclick = () => {
    $("mobileMixerDrawer").classList.add("open");
    sendWs({ type: "get_mixer" });
    renderMixerDrawer();
  };
  $("mobileMixerRefreshBtn").onclick = () => {
    draggingTarget = null;
    sendWs({ type: "get_mixer" });
  };
  $("mobileMixerCloseBtn").onclick = () => {
    draggingTarget = null;
    $("mobileMixerDrawer").classList.remove("open");
  };
  $("mobileMixerDrawer").onclick = (e) => {
    if (e.target === $("mobileMixerDrawer")) {
      draggingTarget = null;
      $("mobileMixerDrawer").classList.remove("open");
    }
  };

  $("wakeLockBtn").onclick = () => {
    if (wakeLockWanted) {
      releaseWakeLock();
    } else {
      requestWakeLock();
    }
  };
  $("fullscreenBtn").onclick = () => {
    if (!document.fullscreenElement) {
      document.documentElement.requestFullscreen?.().catch(() => {});
    } else {
      document.exitFullscreen?.().catch(() => {});
    }
  };

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && wakeLockWanted && !wakeLock) {
      requestWakeLock();
    }
  });

  setInterval(() => {
    updateWidgetsInPlace();
  }, 1000);

  setInterval(() => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      sendWs({ type: "ping", ts: performance.now() });
    }
  }, 5000);

  connect();
})();
