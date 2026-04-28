/* ========================================================================
 * Whale Shadow — dashboard JS
 *
 * Pulls /api endpoints, renders:
 *   - the strike × time OI heatmap (D3 SVG)
 *   - top walls list (CE & PE)
 *   - timing context strip (session, hora, max-pain)
 *   - flows panel (FII/DII)
 *
 * Polls every 15 s while a tab is active. Pauses when the tab is hidden.
 * ====================================================================== */

(() => {
  "use strict";

  const POLL_MS = 15000;
  const SNAPSHOT_LIMIT = 80;

  const state = {
    symbol: "NIFTY",
    expiry: null,
    timer: null,
    lastHeatmap: null,
  };

  const $ = (id) => document.getElementById(id);

  // ---------- formatters ----------
  const fmtNum = (n) =>
    n == null
      ? "—"
      : new Intl.NumberFormat("en-IN", {
          maximumFractionDigits: 2,
        }).format(n);

  const fmtCompact = (n) => {
    if (n == null) return "—";
    if (Math.abs(n) >= 1e7) return (n / 1e7).toFixed(1) + "Cr";
    if (Math.abs(n) >= 1e5) return (n / 1e5).toFixed(1) + "L";
    if (Math.abs(n) >= 1000) return (n / 1000).toFixed(0) + "K";
    return String(n);
  };

  const fmtTimeIST = (utcStr) => {
    if (!utcStr) return "—";
    try {
      const d = new Date(utcStr);
      return d.toLocaleTimeString("en-IN", {
        hour: "2-digit",
        minute: "2-digit",
        timeZone: "Asia/Kolkata",
      });
    } catch {
      return "—";
    }
  };

  // ---------- API ----------
  async function api(path) {
    const res = await fetch(path, { headers: { Accept: "application/json" } });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    return res.json();
  }

  // ---------- timing strip ----------
  async function refreshTiming() {
    try {
      const t = await api("/api/timing");
      $("ctxSession").textContent = phaseLabel(t.session_phase);
      $("ctxClose").textContent =
        t.minutes_to_close > 0 ? `${t.minutes_to_close}m` : "—";
      $("ctxHora").textContent = `${t.hora.lord} · ${t.hora.start_ist}–${t.hora.end_ist}`;
    } catch (e) {
      console.warn("timing failed", e);
    }
  }

  function phaseLabel(p) {
    const map = {
      pre_open: "Pre-Open",
      opening: "Opening",
      mid_session: "Mid",
      pre_close: "Pre-Close",
      post_close: "Post-Close",
      closed: "Closed",
    };
    return map[p] || p;
  }

  // ---------- heatmap ----------
  function renderHeatmap(data) {
    state.lastHeatmap = data;
    const container = $("heatmap");
    container.innerHTML = "";

    if (!data.timestamps.length || !data.strikes.length) {
      container.innerHTML =
        '<div style="padding:42px 16px;text-align:center;color:var(--ink-muted);font-style:italic">collecting snapshots… first heatmap appears after ~2 polls</div>';
      return;
    }

    const T = data.timestamps.length; // x
    const K = data.strikes.length; // y
    const cellW = Math.max(6, Math.min(14, Math.floor((window.innerWidth - 90) / T)));
    const cellH = 10;
    const labelW = 56;
    const padTop = 6;
    const padBottom = 6;
    const padRight = 12;

    const width = labelW + T * cellW + padRight;
    const height = padTop + K * cellH + padBottom;

    const svg = d3
      .select(container)
      .append("svg")
      .attr("width", width)
      .attr("height", height)
      .attr("viewBox", `0 0 ${width} ${height}`);

    const g = svg.append("g").attr("transform", `translate(${labelW}, ${padTop})`);

    // ----- compute color per cell from CE+PE side combined -----
    // We render two bands per row: CE (left half) + PE (right half)?
    // Better for mobile: dominate-by-side rendering — show the larger of CE/PE
    // at that strike with side-coded hue. But to mirror the reel's look more
    // closely, we render a single bar whose color is the combined grade and
    // whose intensity reflects the higher of the two OIs.
    const intensity = (norm) => {
      // norm in [0..1]
      const a = Math.min(1, Math.max(0.05, norm));
      return a;
    };

    // figure out a max OI for normalisation
    let maxOI = 1;
    for (let r = 0; r < K; r++) {
      for (let c = 0; c < T; c++) {
        maxOI = Math.max(maxOI, data.ce_oi[c][r] || 0, data.pe_oi[c][r] || 0);
      }
    }

    const colorFor = (grade, side, normVal) => {
      const a = intensity(normVal);
      if (grade === "whale") return `rgba(217, 70, 239, ${0.55 + 0.45 * a})`;
      if (grade === "large") return `rgba(245, 158, 11, ${0.45 + 0.5 * a})`;
      // normal — shoal — softer, side-tinted
      if (side === "CE") return `rgba(216, 212, 182, ${0.10 + 0.35 * a})`;
      return `rgba(189, 220, 220, ${0.10 + 0.35 * a})`;
    };

    // ----- strike labels (every Nth to avoid clutter) -----
    const stride = K > 60 ? 6 : K > 30 ? 3 : 2;
    svg
      .append("g")
      .attr("transform", `translate(0, ${padTop})`)
      .selectAll("text")
      .data(data.strikes)
      .enter()
      .append("text")
      .attr("x", labelW - 6)
      .attr("y", (d, i) => i * cellH + cellH - 2)
      .attr("text-anchor", "end")
      .attr("font-family", "JetBrains Mono, monospace")
      .attr("font-size", "9px")
      .attr("fill", (d, i) => (i % stride === 0 ? "#b5b59a" : "rgba(181,181,154,0.0)"))
      .text((d) => d);

    // ----- cells -----
    // Reverse Y so higher strikes are at the top (like the reel).
    const yIndex = (r) => K - 1 - r;

    for (let r = 0; r < K; r++) {
      for (let c = 0; c < T; c++) {
        const ceOI = data.ce_oi[c][r] || 0;
        const peOI = data.pe_oi[c][r] || 0;
        const ceGrade = data.ce_grade[c][r];
        const peGrade = data.pe_grade[c][r];

        const dominant = ceOI >= peOI ? "CE" : "PE";
        const grade = dominant === "CE" ? ceGrade : peGrade;
        const oi = Math.max(ceOI, peOI);
        const norm = oi / maxOI;

        g.append("rect")
          .attr("x", c * cellW)
          .attr("y", yIndex(r) * cellH)
          .attr("width", cellW - 1)
          .attr("height", cellH - 1)
          .attr("fill", colorFor(grade, dominant, norm))
          .attr("data-strike", data.strikes[r])
          .attr("data-time", data.timestamps[c])
          .attr("data-ce", ceOI)
          .attr("data-pe", peOI)
          .on("mousemove touchstart", (ev) => showTip(ev, data.strikes[r], data.timestamps[c], ceOI, peOI))
          .on("mouseout touchend", hideTip);
      }
    }

    // ----- spot price line across time -----
    const yScaleStrike = (k) => {
      const idx = data.strikes.indexOf(k);
      if (idx < 0) {
        // interpolate
        let i = 0;
        while (i < data.strikes.length - 1 && data.strikes[i + 1] < k) i++;
        const lo = data.strikes[i];
        const hi = data.strikes[i + 1];
        const t = (k - lo) / (hi - lo || 1);
        return yIndex(i + t) * cellH + cellH / 2;
      }
      return yIndex(idx) * cellH + cellH / 2;
    };

    const linePts = data.underlying.map((u, i) => [i * cellW + cellW / 2, yScaleStrike(u)]);
    g.append("path")
      .attr("class", "spot-line")
      .attr("d", d3.line()(linePts));

    // max pain line
    const mpPts = data.max_pain
      .map((m, i) => (m == null ? null : [i * cellW + cellW / 2, yScaleStrike(m)]))
      .filter(Boolean);
    if (mpPts.length > 1) {
      g.append("path").attr("class", "maxpain-line").attr("d", d3.line()(mpPts));
    }

    // update titles
    $("heatmapSymbol").textContent = state.symbol;
    $("heatmapExpiry").textContent = data.expiry || "—";
    if (data.max_pain && data.max_pain.length) {
      const mp = data.max_pain[data.max_pain.length - 1];
      $("ctxMaxPain").textContent = mp ? fmtNum(mp) : "—";
    }
  }

  let _tip;
  function showTip(ev, strike, time, ce, pe) {
    if (!_tip) {
      _tip = document.createElement("div");
      _tip.className = "cell-tip";
      document.body.appendChild(_tip);
    }
    const x = ev.touches ? ev.touches[0].clientX : ev.clientX;
    const y = ev.touches ? ev.touches[0].clientY : ev.clientY;
    _tip.style.left = Math.min(window.innerWidth - 220, x + 14) + "px";
    _tip.style.top = Math.max(8, y - 60) + "px";
    _tip.innerHTML = `
      <div style="color:var(--ink);font-weight:500">${strike}</div>
      <div style="color:var(--ink-muted)">${fmtTimeIST(time)} IST</div>
      <div style="color:var(--shark);margin-top:4px">CE OI: ${fmtCompact(ce)}</div>
      <div style="color:var(--kelp)">PE OI: ${fmtCompact(pe)}</div>
    `;
  }
  function hideTip() {
    if (_tip) _tip.remove();
    _tip = null;
  }

  // ---------- walls list ----------
  function renderWalls(walls) {
    const ce = walls.walls.filter((w) => w.side === "CE").slice(0, 8);
    const pe = walls.walls.filter((w) => w.side === "PE").slice(0, 8);

    const draw = (list, where) => {
      where.innerHTML = "";
      if (!list.length) {
        where.innerHTML =
          '<li style="color:var(--ink-muted);font-style:italic;font-family:var(--display);font-size:12px;padding:6px 0">no walls — distribution flat</li>';
        return;
      }
      list.forEach((w) => {
        const li = document.createElement("li");
        li.className = "wall-item is-" + w.grade;
        const dir = w.chg_oi > 0 ? "up" : w.chg_oi < 0 ? "down" : "";
        const arrow = w.chg_oi > 0 ? "▲" : w.chg_oi < 0 ? "▼" : "·";
        li.innerHTML = `
          <div class="wall-strike">${w.strike}</div>
          <div class="wall-oi">${fmtCompact(w.oi)}</div>
          <div class="wall-chg ${dir}">${arrow} ${fmtCompact(Math.abs(w.chg_oi))}</div>
        `;
        where.appendChild(li);
      });
    };
    draw(ce, $("ceWallsList"));
    draw(pe, $("peWallsList"));
  }

  // ---------- expiry select ----------
  function rebuildExpiryOptions(expiries) {
    const sel = $("expirySelect");
    const current = state.expiry;
    sel.innerHTML = "";
    expiries.forEach((e) => {
      const opt = document.createElement("option");
      opt.value = e;
      opt.textContent = e;
      sel.appendChild(opt);
    });
    if (current && expiries.includes(current)) {
      sel.value = current;
    } else if (expiries.length) {
      sel.value = expiries[0];
      state.expiry = expiries[0];
    }
  }

  // ---------- flows ----------
  async function refreshFlows() {
    try {
      const { flows } = await api("/api/flows");
      const box = $("flowsBox");
      if (!flows.length) return;
      box.innerHTML = "";
      flows.slice(0, 6).forEach((f) => {
        const dir = f.net >= 0 ? "up" : "down";
        const sign = f.net >= 0 ? "+" : "";
        const row = document.createElement("div");
        row.className = "flow-row";
        row.innerHTML = `
          <div class="flow-cat">${f.category || "—"}<br><span style="color:var(--ink-muted);font-size:10px">${f.date_str || ""}</span></div>
          <div style="color:var(--ink-soft)">B ${fmtCompact(f.buy)}<br>S ${fmtCompact(f.sell)}</div>
          <div class="flow-net ${dir}">${sign}${fmtCompact(f.net)}</div>
        `;
        box.appendChild(row);
      });
    } catch (e) {
      console.warn("flows failed", e);
    }
  }

  // ---------- main refresh ----------
  async function refresh() {
    try {
      const heat = await api(
        `/api/heatmap/${state.symbol}?snapshots=${SNAPSHOT_LIMIT}` +
          (state.expiry ? `&expiry=${encodeURIComponent(state.expiry)}` : "")
      );

      // sync expiries
      const wallsRes = await api(`/api/walls/${state.symbol}` +
        (state.expiry ? `?expiry=${encodeURIComponent(state.expiry)}` : ""));
      if (wallsRes.expiries && wallsRes.expiries.length) {
        rebuildExpiryOptions(wallsRes.expiries);
      }
      if (wallsRes.underlying != null) {
        $("spot" + state.symbol).textContent = fmtNum(wallsRes.underlying);
      }
      renderWalls(wallsRes);

      // pick expiry from heatmap if state.expiry not set
      if (!state.expiry && heat.expiry) state.expiry = heat.expiry;
      renderHeatmap(heat);

      const ts = heat.timestamps[heat.timestamps.length - 1];
      $("lastUpdate").textContent = fmtTimeIST(ts) + " IST";
    } catch (e) {
      console.warn("refresh failed", e);
    }
  }

  function startPolling() {
    if (state.timer) clearInterval(state.timer);
    refresh();
    refreshTiming();
    refreshFlows();
    state.timer = setInterval(() => {
      if (document.visibilityState === "visible") {
        refresh();
        refreshTiming();
      }
    }, POLL_MS);
  }

  // ---------- wire UI ----------
  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll(".tab").forEach((btn) => {
      btn.addEventListener("click", () => {
        document
          .querySelectorAll(".tab")
          .forEach((b) => b.classList.remove("is-active"));
        btn.classList.add("is-active");
        state.symbol = btn.dataset.symbol;
        state.expiry = null;
        startPolling();
      });
    });

    $("expirySelect").addEventListener("change", (e) => {
      state.expiry = e.target.value;
      refresh();
    });

    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") refresh();
    });

    startPolling();
    setInterval(refreshFlows, 5 * 60 * 1000); // flows every 5 min
  });
})();
