window.LocalDeckAudioViz = (() => {
  const BAR_COUNT = 16;
  const REST_SCALE = 0.03;
  const WEIGHTS = [
    0.72, 0.88, 1.00, 0.94, 0.82, 0.96, 1.05, 0.90,
    0.92, 1.04, 0.96, 0.84, 0.95, 1.00, 0.86, 0.70,
  ];
  const NEON_HUES = [38, 18, 6, 152, 42, 14, 158, 34, 8, 150, 44, 16, 155, 36, 10, 148];

  function create(container) {
    if (!container) {
      return {
        setActive() {},
        setMode() {},
        pushLevel() {},
      };
    }

    container.innerHTML = "";
    const breatheEl = document.createElement("div");
    breatheEl.className = "audio-breathe-glow";
    container.appendChild(breatheEl);

    const bars = [];
    const current = new Array(BAR_COUNT).fill(REST_SCALE);
    const caps = new Array(BAR_COUNT).fill(REST_SCALE);
    for (let i = 0; i < BAR_COUNT; i++) {
      const bar = document.createElement("span");
      bar.className = "audio-bar";
      container.appendChild(bar);
      bars.push(bar);
    }

    let active = false;
    let mode = "bars";
    let rafId = 0;
    let targetLeft = 0;
    let targetRight = 0;
    let targetPeak = 0;
    let rollingMax = 0.06;
    let phase = 0;
    let breatheEnergy = 0;

    function resetBars() {
      if (rafId) {
        cancelAnimationFrame(rafId);
        rafId = 0;
      }
      targetLeft = 0;
      targetRight = 0;
      targetPeak = 0;
      rollingMax = 0.06;
      breatheEnergy = 0;
      for (let i = 0; i < BAR_COUNT; i++) {
        current[i] = REST_SCALE;
        caps[i] = REST_SCALE;
        bars[i].style.transform = `scaleY(${REST_SCALE})`;
        bars[i].style.background = "";
        bars[i].style.boxShadow = "";
      }
      breatheEl.style.transform = "scale(0.92)";
      breatheEl.style.opacity = "0.45";
    }

    function step() {
      rafId = 0;
      if (!active) {
        resetBars();
        return;
      }

      if (targetPeak > rollingMax) {
        rollingMax += (targetPeak - rollingMax) * 0.45;
      } else {
        rollingMax = Math.max(0.035, rollingMax * 0.992);
      }

      phase += mode === "breathe" ? 0.045 : 0.19;
      const denom = Math.max(0.03, rollingMax);

      if (mode === "breathe") {
        const norm = Math.min(1, targetPeak / denom);
        const targetE = targetPeak < 0.0008 ? 0 : Math.pow(norm, 0.55);
        const speed = targetE > breatheEnergy ? 0.25 : 0.08;
        breatheEnergy += (targetE - breatheEnergy) * speed;
        const idlePulse = 0.5 + 0.5 * Math.sin(phase);
        const scale = 0.88 + idlePulse * 0.12 + breatheEnergy * 0.26;
        const opacity = 0.36 + idlePulse * 0.22 + breatheEnergy * 0.42;
        breatheEl.style.transform = `scale(${scale.toFixed(3)})`;
        breatheEl.style.opacity = Math.min(1, opacity).toFixed(3);
        rafId = requestAnimationFrame(step);
        return;
      }

      let maxCurrent = REST_SCALE;
      for (let i = 0; i < BAR_COUNT; i++) {
        const pan = i / (BAR_COUNT - 1);
        const stereo = targetLeft * (1 - pan) + targetRight * pan;
        const energy = Math.max(stereo, targetPeak * 0.7);
        const norm = Math.min(1, energy / denom);
        const shaped = Math.pow(norm, 0.52);
        const wave = 0.78 + 0.22 * Math.sin(phase * (0.9 + (i % 4) * 0.15) + i * 0.85);
        const desired = targetPeak < 0.0008
          ? REST_SCALE
          : Math.min(0.98, Math.max(REST_SCALE, shaped * WEIGHTS[i] * wave));

        const speed = desired > current[i] ? 0.42 : 0.16;
        current[i] += (desired - current[i]) * speed;
        if (current[i] > maxCurrent) maxCurrent = current[i];

        if (mode === "rainbow") {
          const hue = Math.round((i * 22 + phase * 18) % 360);
          bars[i].style.background =
            `linear-gradient(to top, hsla(${hue}, 88%, 56%, 0.84) 0%, hsla(${(hue + 28) % 360}, 86%, 60%, 0.50) 50%, hsla(${(hue + 52) % 360}, 84%, 64%, 0) 100%)`;
          bars[i].style.boxShadow = "";
        } else if (mode === "neon") {
          caps[i] = Math.max(current[i], caps[i] - 0.012);
          const hue = NEON_HUES[i % NEON_HUES.length];
          bars[i].style.background =
            `linear-gradient(to top, hsla(${hue}, 96%, 58%, 0.88) 0%, hsla(${hue}, 92%, 62%, 0.52) 52%, hsla(${hue}, 90%, 66%, 0) 100%)`;
          bars[i].style.boxShadow =
            current[i] > 0.12 ? `0 0 14px hsla(${hue}, 95%, 58%, 0.45)` : "none";
        } else {
          bars[i].style.background = "";
          bars[i].style.boxShadow = "";
        }

        bars[i].style.transform = `scaleY(${current[i].toFixed(3)})`;
      }

      if (targetPeak >= 0.0008 || maxCurrent > REST_SCALE + 0.004) {
        rafId = requestAnimationFrame(step);
      }
    }

    return {
      setActive(isAudioSkin) {
        active = Boolean(isAudioSkin);
        if (!active) {
          resetBars();
        } else if (mode === "breathe" && !rafId) {
          rafId = requestAnimationFrame(step);
        }
      },
      setMode(nextMode) {
        const resolved = ["bars", "rainbow", "neon", "breathe"].includes(nextMode) ? nextMode : "bars";
        if (mode === resolved && container.dataset.vizMode === resolved) return;
        mode = resolved;
        container.dataset.vizMode = resolved;
        for (let i = 0; i < BAR_COUNT; i++) {
          bars[i].style.background = "";
          bars[i].style.boxShadow = "";
        }
        if (active && mode === "breathe" && !rafId) {
          rafId = requestAnimationFrame(step);
        }
      },
      pushLevel(msg) {
        if (!active) return;
        targetPeak = Math.max(0, Math.min(1, Number(msg.peak) || 0));
        targetLeft = Math.max(0, Math.min(1, Number(msg.left) || targetPeak));
        targetRight = Math.max(0, Math.min(1, Number(msg.right) || targetPeak));
        if (!rafId) {
          rafId = requestAnimationFrame(step);
        }
      },
    };
  }

  return { create };
})();
