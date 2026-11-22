// Multi-terminal grid: several VM PTYs side by side (tabs, 2 columns or a 2×2 grid). Each pane keeps its own
// xterm instance alive while the grid exists, so switching tabs/layouts never drops a session. Used by the VM tab
// (user shells + automation watchers) and by the multi-agent team monitor (one read-only pane per agent PTY).
(() => {
  const G = window.AGDD;
  const { esc, openTerminal } = G;
  const NAME_RX = /^[A-Za-z0-9_-]{1,64}$/;
  const STATE_LABEL = { connecting: "연결 중", live: "실시간", waiting: "대기", ended: "종료", idle: "대기" };

  if (!document.getElementById("tg-style")) {
    const st = document.createElement("style");
    st.id = "tg-style";
    st.textContent = `
      .tg-bar { display:flex; gap:4px; align-items:center; flex-wrap:wrap; margin-bottom:6px; }
      .tg-tab { display:inline-flex; align-items:center; gap:6px; font:inherit; font-size:12.5px; border:1px solid var(--line); background:var(--panel); color:var(--ink); border-radius:6px; padding:3px 6px 3px 9px; cursor:pointer; max-width:260px; }
      .tg-tab.on { border-color:var(--accent); box-shadow:inset 0 -2px 0 var(--accent); font-weight:600; }
      .tg-tab .tg-nm { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .tg-tab .tg-x, .tg-ph .tg-x { border:0; background:none; color:var(--muted); cursor:pointer; padding:0 3px; font-size:13px; line-height:1; }
      .tg-tab .tg-x:hover, .tg-ph .tg-x:hover { color:var(--bad); }
      .tg-dot { width:7px; height:7px; border-radius:50%; background:var(--muted); flex:none; }
      .tg-dot.live { background:var(--ok); } .tg-dot.connecting, .tg-dot.waiting { background:var(--warn); } .tg-dot.ended { background:var(--bad); }
      .tg-dot.busy { background:var(--ok); box-shadow:0 0 0 3px color-mix(in srgb, var(--ok) 30%, transparent); animation:tg-pulse 1.2s infinite; }
      @keyframes tg-pulse { 50% { box-shadow:0 0 0 1px color-mix(in srgb, var(--ok) 10%, transparent); } }
      .tg-sp { flex:1; }
      .tg-seg { display:inline-flex; border:1px solid var(--line); border-radius:6px; overflow:hidden; }
      .tg-seg button { font:inherit; font-size:12px; border:0; background:var(--panel); color:var(--ink); padding:3px 9px; cursor:pointer; }
      .tg-seg button + button { border-left:1px solid var(--line); }
      .tg-seg button.on { background:var(--chip); font-weight:600; }
      .tg-grid { display:grid; gap:8px; }
      .tg-pane { display:flex; flex-direction:column; min-width:0; min-height:0; border:1px solid var(--line); border-radius:7px; overflow:hidden; background:#111; }
      .tg-pane.on { border-color:var(--accent); }
      .tg-pane.busy { border-color:color-mix(in srgb, var(--ok) 70%, var(--line)); }
      .tg-pane[hidden] { display:none; }
      .tg-ph { display:flex; align-items:center; gap:6px; padding:3px 8px; font-size:12px; background:var(--panel); border-bottom:1px solid var(--line); cursor:pointer; min-width:0; }
      .tg-ph .tg-nm { font-weight:600; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .tg-ph .tg-meta { display:flex; gap:4px; align-items:center; overflow:hidden; white-space:nowrap; }
      .tg-ph .tg-st { color:var(--muted); font-size:11px; white-space:nowrap; }
      .tg-term { flex:1; min-height:0; padding:4px 6px; }
      .tg-empty { color:#9a998f; font-size:12.5px; padding:14px; }
      .tg-ro { font-size:10.5px; border:1px solid var(--line); border-radius:4px; padding:0 4px; color:var(--muted); }
    `;
    document.head.appendChild(st);
  }

  /**
   * host: element to render into.
   * o.height: total grid height in px (default 520). o.layout: "1" | "2" | "4" (default "1").
   * o.storeKey: persist panes/layout/active under this key (VM tab). o.closable (default true).
   * o.rearm(pane): keep waiting for the next session instead of ending (automation PTYs).
   * o.connect(pane): false = create the pane but don't attach yet (connect later with grid.connect()).
   * o.meta(pane): extra header html. o.busy(pane): pulse indicator. o.onActive(pane). o.onClose(pane).
   * o.empty: html shown when no panes. o.fontSize.
   */
  G.TermGrid = function TermGrid(host, o = {}) {
    let saved = null;
    if (o.storeKey) {
      try {
        const raw = G.store.get(o.storeKey);
        const parsed = raw ? JSON.parse(raw) : null;
        if (parsed && typeof parsed === "object") saved = parsed;
      } catch { saved = null; }
    }
    const st = { panes: [], active: "", layout: (saved && saved.layout) || o.layout || "1", stopped: false };
    const H = () => Math.max(260, o.height || 520);
    host.innerHTML = `<div class="tg"><div class="tg-bar"></div><div class="tg-grid"></div></div>`;
    const bar = host.querySelector(".tg-bar"), grid = host.querySelector(".tg-grid");

    const find = name => st.panes.find(p => p.name === name);
    const persist = () => {
      if (!o.storeKey) return;
      G.store.set(o.storeKey, JSON.stringify({ layout: st.layout, active: st.active,
        panes: st.panes.filter(p => !p.ephemeral).map(p => ({ name: p.name, ro: p.ro, label: p.label || "" })) }));
    };

    function drawBar() {
      bar.innerHTML = st.panes.map(p => `<span class="tg-tab ${p.name === st.active ? "on" : ""}" data-tab="${esc(p.name)}" title="${esc(p.name)}">
          <i class="tg-dot ${o.busy?.(p) ? "busy" : p.state}"></i><span class="tg-nm">${esc(p.label || p.name)}</span>${p.ro ? `<span class="tg-ro">RO</span>` : ""}
          ${o.closable !== false ? `<button class="tg-x" data-x="${esc(p.name)}" title="패널 닫기 (VM 세션은 유지)">×</button>` : ""}</span>`).join("")
        + `<span class="tg-sp"></span>${o.extraBar || ""}
        <span class="tg-seg" title="레이아웃">${[["1", "▭ 탭"], ["2", "◫ 2열"], ["4", "⊞ 그리드"]].map(([k, t]) => `<button data-lay="${k}" class="${st.layout === k ? "on" : ""}">${t}</button>`).join("")}</span>`;
      bar.querySelectorAll("[data-tab]").forEach(b => b.onclick = e => { if (!e.target.closest("[data-x]")) api.activate(b.dataset.tab, true); });
      bar.querySelectorAll("[data-x]").forEach(b => b.onclick = () => api.remove(b.dataset.x));
      bar.querySelectorAll("[data-lay]").forEach(b => b.onclick = () => api.setLayout(b.dataset.lay));
      o.onBar?.(bar);
    }

    function drawHead(p) {
      const busy = !!o.busy?.(p);
      p.el.classList.toggle("busy", busy);
      p.el.classList.toggle("on", p.name === st.active && st.layout !== "1");
      p.head.innerHTML = `<i class="tg-dot ${busy ? "busy" : p.state}"></i><span class="tg-nm" title="${esc(p.name)}">${esc(p.label || p.name)}</span>
        ${p.label && p.label !== p.name ? `<span class="tg-st mono">${esc(p.name)}</span>` : ""}
        <span class="tg-meta">${o.meta?.(p) || ""}</span><span class="tg-sp"></span>
        <span class="tg-st">${esc(STATE_LABEL[p.state] || p.state)}</span>${p.ro ? `<span class="tg-ro">읽기 전용</span>` : ""}
        ${!p.ro && p.term ? `<button class="tg-x" data-kill title="VM 셸 세션 종료(프로세스 종료)">⏻</button>` : ""}
        ${o.closable !== false ? `<button class="tg-x" data-x title="패널 닫기 (VM 세션은 유지)">×</button>` : ""}`;
      const x = p.head.querySelector("[data-x]"); if (x) x.onclick = e => { e.stopPropagation(); api.remove(p.name); };
      const k = p.head.querySelector("[data-kill]");
      if (k) k.onclick = e => {
        e.stopPropagation();
        if (!confirm(`'${p.name}' 셸 세션을 종료할까요? 실행 중인 프로세스가 모두 종료됩니다.`)) return;
        p.term?.close?.().catch(err => G.toast(err.message, true));
      };
    }

    function layout() {
      const n = st.panes.length, h = H();
      const cols = st.layout === "1" || n <= 1 ? 1 : 2;
      const rowH = st.layout === "4" && n > 2 ? Math.max(220, Math.floor((h - 8) / 2)) : h;
      grid.style.gridTemplateColumns = `repeat(${cols}, minmax(0, 1fr))`;
      grid.style.gridAutoRows = `${rowH}px`;
      if (!n) grid.innerHTML = `<div class="tg-pane"><div class="tg-empty">${o.empty || "열린 터미널이 없습니다."}</div></div>`;
      else grid.querySelector(".tg-pane:not([data-n])")?.remove();
      for (const p of st.panes) {
        p.el.hidden = st.layout === "1" && p.name !== st.active;
        drawHead(p);
      }
      requestAnimationFrame(() => st.panes.forEach(p => { if (!p.el.hidden) p.term?.fit(); }));
    }

    function connectPane(p) {
      if (p.term || st.stopped) return;
      p.host.innerHTML = "";
      p.term = openTerminal(p.host, p.name, p.ro, {
        rearm: o.rearm ? () => !!o.rearm(p) : null,
        fontSize: o.fontSize, banner: p.banner,
        onState: s => { p.state = s; drawHead(p); const t = bar.querySelector(`[data-tab="${CSS.escape(p.name)}"] .tg-dot`); if (t) t.className = `tg-dot ${o.busy?.(p) ? "busy" : s}`; },
      });
    }

    const api = {
      get panes() { return st.panes; },
      get layout() { return st.layout; },
      find,
      active: () => find(st.active) || null,
      /** pane: { name, ro?, label?, ephemeral?, banner?, ...anything (passed back to callbacks) } */
      add(pane, { focus = true } = {}) {
        if (!pane || !NAME_RX.test(pane.name || "")) { G.toast("터미널 이름은 영문/숫자/_/- 1~64자입니다", true); return null; }
        let p = find(pane.name);
        if (p) { Object.assign(p, pane); if (focus) api.activate(p.name, true); else layout(); return p; }
        p = { ro: false, state: "idle", ...pane, term: null };
        p.el = document.createElement("div");
        p.el.className = "tg-pane"; p.el.dataset.n = p.name;
        p.el.innerHTML = `<div class="tg-ph"></div><div class="tg-term"></div>`;
        p.head = p.el.firstChild; p.host = p.el.lastChild;
        p.el.addEventListener("pointerdown", () => { if (st.active !== p.name) api.activate(p.name, false); });
        grid.appendChild(p.el);
        st.panes.push(p);
        if (o.connect?.(p) === false) p.host.innerHTML = `<div class="tg-empty">${o.idleText?.(p) || "연결 대기"}</div>`;
        else connectPane(p);
        if (focus || !st.active) st.active = p.name;
        drawBar(); layout(); persist();
        if (focus) setTimeout(() => p.term?.focus(), 30);
        return p;
      },
      remove(name) {
        const p = find(name); if (!p) return;
        try { p.term?.stop(); } catch {}
        p.el.remove();
        st.panes = st.panes.filter(x => x !== p);
        if (st.active === name) st.active = st.panes[Math.max(0, st.panes.length - 1)]?.name || "";
        o.onClose?.(p);
        drawBar(); layout(); persist();
      },
      activate(name, focus) {
        if (!find(name)) return;
        const changed = st.active !== name;
        st.active = name;
        if (changed) { drawBar(); layout(); persist(); o.onActive?.(find(name)); }
        if (focus) setTimeout(() => find(name)?.term?.focus(), 20);
      },
      setLayout(l) { if (!["1", "2", "4"].includes(l)) return; st.layout = l; drawBar(); layout(); persist(); },
      /** attach a pane created with connect() === false (or re-attach one that ended). */
      connect(name, { fresh = false } = {}) {
        const p = find(name); if (!p) return;
        if (fresh && p.term && p.state === "ended") { try { p.term.stop(); } catch {} p.term = null; }
        connectPane(p);
      },
      refresh() { drawBar(); st.panes.forEach(drawHead); },
      type(text) { const p = api.active(); if (!p || p.ro || !p.term) return false; p.term.type(text); return true; },
      stop() { st.stopped = true; st.panes.forEach(p => { try { p.term?.stop(); } catch {} p.term = null; }); },
      restore(defaults = []) {
        const list = saved && Array.isArray(saved.panes) && saved.panes.length ? saved.panes : defaults;
        for (const p of list) if (p && NAME_RX.test(p.name || "")) api.add({ name: p.name, ro: !!p.ro || p.name.startsWith("agdd-"), label: p.label || "" }, { focus: false });
        if (saved?.active && find(saved.active)) api.activate(saved.active, false);
        drawBar(); layout();
      },
      nextName(prefix = "t") {
        if (!find("main")) return "main";
        for (let i = 2; ; i++) if (!find(prefix + i)) return prefix + i;
      },
    };
    drawBar(); layout();
    return api;
  };
})();
