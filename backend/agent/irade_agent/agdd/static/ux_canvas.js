(() => {
  const G = window.AGDD;
  const { $, esc, api, toast, run, stale } = G;

  const UXC = G.UXC;
  UXC.view = UXC.view || "home";
  UXC.vp = UXC.vp || { width: 1200, height: 720 };
  UXC.items = UXC.items || {};
  UXC.removed = UXC.removed || new Set();
  UXC.sel = UXC.sel || "";
  UXC.connectMode = !!UXC.connectMode;
  UXC.json = !!UXC.json;
  UXC.undo = UXC.undo || [];
  UXC.redo = UXC.redo || [];
  UXC.pendingReq = UXC.pendingReq || null;
  UXC.decisionId = UXC.decisionId || null;

  const PALETTE = [
    ["header", "Header", 960, 72],
    ["sidebar", "Sidebar", 220, 480],
    ["section", "Section", 560, 260],
    ["card", "Card", 280, 160],
    ["button", "Button", 140, 44],
    ["input", "Input", 260, 44],
    ["chart", "Chart", 420, 240],
    ["text", "Text", 320, 80],
  ];

  function snapshotUndo() {
    UXC.undo.push(JSON.stringify({ items: UXC.items, removed: [...UXC.removed] }));
    if (UXC.undo.length > 60) UXC.undo.shift();
    UXC.redo = [];
  }

  function makeBox(selector, kind, label, x, y, w, h, note = "") {
    return {
      locked: true,
      config: {
        layout: { x, y, z: Object.keys(UXC.items).length + 1, direction: "column", columns: 1 },
        size: { w, h },
        spacing: { padding: 14, gap: 10 },
        tokens: { bg: "#1f2937", accent: "#2f5d50", radius: 10 },
        visibility: "visible",
        order: Object.keys(UXC.items).length + 1,
        props: { kind, label, note, connects_to: [] },
      },
    };
  }

  function ensureBoxFields(sel, item, idx) {
    const c = item.config || {};
    const layout = { x: 40 + (idx % 3) * 260, y: 40 + Math.floor(idx / 3) * 160, z: idx + 1, direction: "column", columns: 1, ...(c.layout || {}) };
    const size = { w: 240, h: 120, ...(c.size || {}) };
    const spacing = { padding: 12, gap: 8, ...(c.spacing || {}) };
    const tokens = { bg: "#1f2937", accent: "#2f5d50", radius: 10, ...(c.tokens || {}) };
    const props = { kind: "section", label: sel, note: "", connects_to: [], ...(c.props || {}) };
    return {
      locked: item.locked !== false,
      config: { layout, size, spacing, tokens, visibility: c.visibility || "visible", order: c.order ?? layout.z, props },
    };
  }

  function loadPendingIntoCanvas(req) {
    if (!req) return;
    if (req.viewport) UXC.vp = { width: req.viewport.width || 1200, height: req.viewport.height || 720 };
    for (const el of req.elements || []) {
      if (!UXC.items[el.selector]) {
        UXC.items[el.selector] = makeBox(el.selector, el.kind || "section", el.label || el.selector, el.x ?? 40, el.y ?? 40, el.w ?? 240, el.h ?? 120, el.note || "");
      }
    }
  }

  function drawStage() {
    const stage = $("#uxc-stage");
    if (!stage) return;
    const vp = UXC.vp;
    const scale = Math.min(1, Math.max(0.35, (stage.clientWidth - 24) / vp.width));
    const arrows = [];
    for (const [sel, item] of Object.entries(UXC.items)) {
      const c = item.config;
      for (const tgt of c.props?.connects_to || []) {
        const b = UXC.items[tgt]; if (!b) continue;
        const x1 = c.layout.x + c.size.w / 2, y1 = c.layout.y + c.size.h / 2;
        const x2 = b.config.layout.x + b.config.size.w / 2, y2 = b.config.layout.y + b.config.size.h / 2;
        arrows.push(`<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="#7fc0a9" stroke-width="2.5" marker-end="url(#ux-arr)"/>`);
      }
    }

    const boxes = Object.entries(UXC.items).map(([sel, item]) => {
      const c = item.config;
      const on = UXC.sel === sel ? "on" : "";
      const lk = item.locked ? "locked" : "";
      return `<div class="uxc-box ${on} ${lk}" data-usel="${esc(sel)}" style="left:${c.layout.x}px;top:${c.layout.y}px;width:${c.size.w}px;height:${c.size.h}px;z-index:${c.layout.z || 1};border-radius:${c.tokens?.radius ?? 10}px;border-color:${esc(c.tokens?.accent || "#2f5d50")}">
        <div class="uxc-box-head">
          <span class="mono">${esc(sel)}</span>
          <span>${item.locked ? "🔒" : "🔓"} ${esc(c.props?.kind || "section")}</span>
        </div>
        <div class="uxc-box-body"><b>${esc(c.props?.label || sel)}</b>${c.props?.note ? `<small>${esc(c.props.note)}</small>` : ""}</div>
        <div class="uxc-box-dim mono">${c.layout.x},${c.layout.y} · ${c.size.w}×${c.size.h}</div>
        <div class="uxc-resize" data-uresize="${esc(sel)}" title="드래그해 크기 조절"></div>
      </div>`;
    }).join("");

    stage.innerHTML = `<div class="uxc-board" id="uxc-board" style="width:${vp.width}px;height:${vp.height}px;transform:scale(${scale});transform-origin:0 0">
      <svg class="uxc-arrows" width="${vp.width}" height="${vp.height}"><defs><marker id="ux-arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="#7fc0a9"/></marker></defs>${arrows.join("")}</svg>
      ${boxes}
    </div>`;
    stage.style.height = Math.max(420, Math.round(vp.height * scale + 28)) + "px";
    bindStage(scale);
  }

  function bindStage(scale) {
    const board = $("#uxc-board"); if (!board) return;
    let drag = null;
    const snap = n => Math.round(n / 8) * 8;
    board.onpointerdown = e => {
      const rh = e.target.closest("[data-uresize]");
      const bx = e.target.closest("[data-usel]");
      if (rh) {
        e.stopPropagation();
        const sel = rh.dataset.uresize, it = UXC.items[sel];
        snapshotUndo();
        board.setPointerCapture(e.pointerId);
        drag = { mode: "resize", sel, sx: e.clientX, sy: e.clientY, w: it.config.size.w, h: it.config.size.h, el: board.querySelector(`[data-usel="${CSS.escape(sel)}"]`) };
        return;
      }
      if (bx) {
        const sel = bx.dataset.usel;
        if (UXC.connectMode && UXC.sel && UXC.sel !== sel) {
          snapshotUndo();
          const arr = UXC.items[UXC.sel].config.props.connects_to || [];
          const idx = arr.indexOf(sel);
          if (idx >= 0) arr.splice(idx, 1); else arr.push(sel);
          UXC.items[UXC.sel].config.props.connects_to = arr;
          UXC.connectMode = false;
          drawStage(); drawInspector();
          return;
        }
        UXC.sel = sel;
        drawInspector();
        board.querySelectorAll(".uxc-box").forEach(n => n.classList.toggle("on", n === bx));
        const it = UXC.items[sel];
        board.setPointerCapture(e.pointerId);
        drag = { mode: "move", sel, sx: e.clientX, sy: e.clientY, x: it.config.layout.x, y: it.config.layout.y, moved: false, before: JSON.stringify(UXC.items), el: bx };
        return;
      }
      UXC.sel = "";
      drawInspector();
      board.querySelectorAll(".uxc-box").forEach(n => n.classList.remove("on"));
    };
    board.onpointermove = e => {
      if (!drag) return;
      const dx = (e.clientX - drag.sx) / scale, dy = (e.clientY - drag.sy) / scale;
      const c = UXC.items[drag.sel].config;
      if (drag.mode === "move") {
        c.layout.x = Math.max(0, Math.min(UXC.vp.width - c.size.w, snap(drag.x + dx)));
        c.layout.y = Math.max(0, Math.min(UXC.vp.height - c.size.h, snap(drag.y + dy)));
        drag.moved = true;
        drag.el.style.left = c.layout.x + "px"; drag.el.style.top = c.layout.y + "px";
      } else {
        c.size.w = Math.max(48, Math.min(UXC.vp.width - c.layout.x, snap(drag.w + dx)));
        c.size.h = Math.max(32, Math.min(UXC.vp.height - c.layout.y, snap(drag.h + dy)));
        drag.el.style.width = c.size.w + "px"; drag.el.style.height = c.size.h + "px";
      }
      const dim = drag.el.querySelector(".uxc-box-dim");
      if (dim) dim.textContent = `${c.layout.x},${c.layout.y} · ${c.size.w}×${c.size.h}`;
    };
    board.onpointerup = () => {
      if (!drag) return;
      if (drag.mode === "move" && drag.moved) {
        UXC.undo.push(JSON.stringify({ items: JSON.parse(drag.before), removed: [...UXC.removed] }));
        UXC.redo = [];
      }
      drag = null;
      drawStage(); drawInspector();
    };
    board.ondblclick = e => {
      if (e.target.closest("[data-usel]")) return;
      const r = board.getBoundingClientRect();
      addElement("card", "Card", 280, 160, snap((e.clientX - r.left) / scale), snap((e.clientY - r.top) / scale));
    };
  }

  function addElement(kind, label, w, h, x = 40, y = 40) {
    snapshotUndo();
    let i = 1; while (UXC.items[`#${kind}-${i}`]) i++;
    const sel = `#${kind}-${i}`;
    UXC.removed.delete(sel);
    UXC.items[sel] = makeBox(sel, kind, `${label} ${i}`, Math.max(0, x), Math.max(0, y), w, h);
    UXC.sel = sel;
    drawStage(); drawInspector();
  }

  function drawInspector() {
    const el = $("#uxc-insp"); if (!el) return;
    if (!UXC.sel || !UXC.items[UXC.sel]) {
      const count = Object.keys(UXC.items).length;
      el.innerHTML = `<div class="ide-head">캔버스 속성</div><div class="g-form">
        <div class="muted" style="font-size:12.5px">뷰 <b class="mono">${esc(UXC.view)}</b> · ${count}개 요소 · ${UXC.vp.width}×${UXC.vp.height}</div>
        <div class="muted" style="font-size:12px;line-height:1.6">• 팔레트 클릭 또는 빈 곳 더블클릭: UI 요소 추가<br>• 드래그: 2D 위치 이동 / 우하단 핸들: 크기 조절<br>• <b>연결</b> 버튼 후 대상 클릭: 요소 간 화살표 연결<br>• 🔒 잠금된 요소는 AGDD Audit이 수정을 차단합니다.</div>
        <label class="fld"><span>에이전트에 전달할 메모</span><textarea id="uxc-note" rows="3" style="min-height:70px" placeholder="예: 상단 헤더 아래 좌측 사이드바와 2열 대시보드 카드로 구성하세요."></textarea></label>
        <button class="b" id="uxc-send-agent">${UXC.decisionId ? "2D 구성을 에이전트에 전달 & 재개" : "이 2D 구성으로 에이전트 실행 →"}</button>
      </div>`;
      $("#uxc-send-agent").onclick = run(() => submitToAgent(!UXC.decisionId));
      return;
    }

    const sel = UXC.sel, it = UXC.items[sel], c = it.config;
    el.innerHTML = `<div class="ide-head">요소 속성 <button class="g" id="uxc-conn" style="padding:2px 8px;font-size:11px">${UXC.connectMode ? "연결 대상 클릭…" : "↗ 연결"}</button></div>
      <div class="g-form">
        <label class="fld"><span>셀렉터 (ID)</span><input id="ux-sel" class="mono" value="${esc(sel)}"></label>
        <div class="row">
          <label class="fld" style="flex:1"><span>종류</span><input id="ux-kind" value="${esc(c.props.kind || "section")}"></label>
          <label class="fld" style="flex:1"><span>레이블</span><input id="ux-label" value="${esc(c.props.label || "")}"></label>
        </div>
        <div class="row">
          <label class="fld" style="flex:1"><span>X</span><input id="ux-x" type="number" value="${c.layout.x}"></label>
          <label class="fld" style="flex:1"><span>Y</span><input id="ux-y" type="number" value="${c.layout.y}"></label>
          <label class="fld" style="flex:1"><span>W</span><input id="ux-w" type="number" value="${c.size.w}"></label>
          <label class="fld" style="flex:1"><span>H</span><input id="ux-h" type="number" value="${c.size.h}"></label>
        </div>
        <div class="row">
          <label class="fld" style="flex:1"><span>배치 방향</span><select id="ux-dir"><option value="column" ${c.layout.direction === "column" ? "selected" : ""}>column</option><option value="row" ${c.layout.direction === "row" ? "selected" : ""}>row</option><option value="grid" ${c.layout.direction === "grid" ? "selected" : ""}>grid</option></select></label>
          <label class="fld" style="flex:1"><span>열 수</span><input id="ux-cols" type="number" min="1" max="12" value="${c.layout.columns ?? 1}"></label>
          <label class="fld" style="flex:1"><span>Gap</span><input id="ux-gap" type="number" min="0" value="${c.spacing.gap ?? 10}"></label>
        </div>
        <label class="fld"><span>2D 의도/동작 설명 (에이전트 전달)</span><textarea id="ux-enote" rows="2" style="min-height:60px">${esc(c.props.note || "")}</textarea></label>
        <label class="row" style="font-size:12.5px"><input type="checkbox" id="ux-lock" ${it.locked ? "checked" : ""}> <span>🔒 사용자 잠금 (에이전트 덮어쓰기 차단)</span></label>
        ${(c.props.connects_to || []).length ? `<div class="muted" style="font-size:12px">연결: ${(c.props.connects_to || []).map(t => `<span class="chip">${esc(sel)} → ${esc(t)}</span>`).join(" ")}</div>` : ""}
        <div class="row">
          <button class="g" id="ux-dup">복제</button>
          <button class="g bad" id="ux-del">삭제</button>
        </div>
      </div>`;

    const bindNum = (id, fn) => { $(id).onchange = e => { snapshotUndo(); fn(parseInt(e.target.value, 10) || 0); drawStage(); }; };
    $("#ux-sel").onchange = e => {
      const next = e.target.value.trim(); if (!next || next === sel || UXC.items[next]) return;
      snapshotUndo(); UXC.items[next] = UXC.items[sel]; delete UXC.items[sel]; UXC.removed.add(sel); UXC.removed.delete(next);
      UXC.sel = next; drawStage(); drawInspector();
    };
    $("#ux-kind").onchange = e => { snapshotUndo(); c.props.kind = e.target.value.trim() || "section"; drawStage(); };
    $("#ux-label").oninput = e => { c.props.label = e.target.value; drawStage(); };
    bindNum("#ux-x", v => { c.layout.x = Math.max(0, v); });
    bindNum("#ux-y", v => { c.layout.y = Math.max(0, v); });
    bindNum("#ux-w", v => { c.size.w = Math.max(48, v); });
    bindNum("#ux-h", v => { c.size.h = Math.max(32, v); });
    $("#ux-dir").onchange = e => { snapshotUndo(); c.layout.direction = e.target.value; };
    bindNum("#ux-cols", v => { c.layout.columns = Math.max(1, v); });
    bindNum("#ux-gap", v => { c.spacing.gap = Math.max(0, v); });
    $("#ux-enote").oninput = e => { c.props.note = e.target.value; };
    $("#ux-lock").onchange = e => { snapshotUndo(); it.locked = e.target.checked; drawStage(); };
    $("#uxc-conn").onclick = () => { UXC.connectMode = !UXC.connectMode; drawInspector(); };
    $("#ux-dup").onclick = () => addElement(c.props.kind || "card", c.props.label || "Copy", c.size.w, c.size.h, c.layout.x + 24, c.layout.y + 24);
    $("#ux-del").onclick = () => { snapshotUndo(); delete UXC.items[sel]; UXC.removed.add(sel); UXC.sel = ""; drawStage(); drawInspector(); };
  }

  async function submitToAgent(launchRun) {
    const WS = G.getWS();
    const res = await api("POST", `/workspaces/${WS}/ux/${encodeURIComponent(UXC.view)}/submit-to-agent`, {
      elements: UXC.items,
      removed: [...UXC.removed],
      viewport: UXC.vp,
      decision_id: UXC.decisionId,
      note: ($("#uxc-note")?.value || "").trim(),
      launch_run: !!launchRun,
    }, { noInvalidate: true });
    UXC.removed.clear();
    UXC.pendingReq = null; UXC.decisionId = null;
    toast(res.resumed_run_id ? "2D UX 구성을 에이전트에 전달하고 실행을 시작했습니다" : "2D UX 구성 저장됨");
    if (res.resumed_run_id) G.openTab("collab", { collabMode: "chat" });
    else G.render();
  }

  G.VIEWS.ux = async function uxView() {
    const WS = G.getWS();
    const [viewsRes, fullRes, pendRes] = await Promise.all([
      api("GET", `/workspaces/${WS}/ux`, undefined, { fresh: true }),
      api("GET", `/workspaces/${WS}/ux/${encodeURIComponent(UXC.view)}?full=1`, undefined, { fresh: true }),
      api("GET", `/workspaces/${WS}/ux/pending-request`, undefined, { fresh: true }),
    ]);
    if (stale("ux")) return;

    const views = viewsRes.views || [];
    const viewNames = [...new Set([UXC.view, "home", ...views.map(v => v.view_key)])];
    UXC.items = {};
    (fullRes.items || []).forEach((row, idx) => {
      UXC.items[row.component_selector] = ensureBoxFields(row.component_selector, { locked: row.locked_by_user, config: row.config_json }, idx);
    });
    UXC.removed = new Set();

    const pending = (pendRes.pending || [])[0];
    if (pending && (!UXC.pendingReq || pending.ux_request?.view_key === UXC.view)) {
      UXC.pendingReq = pending.ux_request;
      UXC.decisionId = pending.decision_id;
    }
    if (UXC.pendingReq && UXC.pendingReq.view_key === UXC.view) loadPendingIntoCanvas(UXC.pendingReq);

    G.V(`<div class="row" style="justify-content:space-between;margin-bottom:10px">
        <div><h2>UX 2D 캔버스 · 오버라이드</h2><p class="sub" style="margin:0">2차원 공간에 화면 요소를 배치·연결·잠금하면 선언형 JSON과 공간 관계로 변환되어 에이전트에 전달됩니다.</p></div>
        <div class="row">
          <select id="uxc-view">${viewNames.map(v => `<option value="${esc(v)}" ${v === UXC.view ? "selected" : ""}>${esc(v)}</option>`).join("")}</select>
          <button class="g" id="uxc-new-view">+ 새 뷰</button>
          <select id="uxc-vp"><option value="1200x720" ${UXC.vp.width === 1200 ? "selected" : ""}>Desktop 1200×720</option><option value="768x1024" ${UXC.vp.width === 768 ? "selected" : ""}>Tablet 768×1024</option><option value="390x844" ${UXC.vp.width === 390 ? "selected" : ""}>Mobile 390×844</option></select>
          <button class="g" id="uxc-save">저장</button>
          <button class="b" id="uxc-deliver">${UXC.decisionId ? "2D 전달 & 재개 →" : "에이전트에 2D 전달 →"}</button>
        </div>
      </div>
      ${UXC.pendingReq ? `<div class="card" style="border-color:var(--warn);background:color-mix(in srgb,var(--warn) 8%,var(--panel))">
        <b>🎨 에이전트 시각 배치 요청 (${esc(UXC.pendingReq.view_key)})</b>: ${esc(UXC.pendingReq.prompt || "요소를 배치하고 전달하세요.")}
      </div>` : ""}
      <div class="g-toolbar">
        <span class="muted" style="font-size:12px">요소 추가</span>
        ${PALETTE.map(([k, l, w, h]) => `<button class="g-kind" data-uadd="${k}" data-ul="${l}" data-uw="${w}" data-uh="${h}">+ ${l}</button>`).join("")}
        <span class="g-sep"></span>
        <button class="g" id="uxc-undo">↶</button>
        <button class="g" id="uxc-redo">↷</button>
        <button class="g ${UXC.json ? "on" : ""}" id="uxc-json">{ } JSON</button>
      </div>
      <div class="g-wrap">
        ${UXC.json
          ? `<div class="g-jsonbox"><textarea id="uxc-raw" spellcheck="false">${esc(JSON.stringify(UXC.items, null, 2))}</textarea><div class="row" style="padding:8px"><button class="b" id="uxc-apply-json">JSON 적용</button></div></div>`
          : `<div id="uxc-stage" class="uxc-stage"></div>`}
        <div id="uxc-insp" class="g-insp"></div>
      </div>`);

    $("#uxc-view").onchange = () => { UXC.view = $("#uxc-view").value; UXC.sel = ""; G.render(); };
    $("#uxc-new-view").onclick = () => {
      const v = prompt("새 뷰 이름 (예: dashboard):", "dashboard");
      if (v && /^[A-Za-z0-9_.-]{1,64}$/.test(v.trim())) { UXC.view = v.trim(); UXC.sel = ""; G.render(); }
    };
    $("#uxc-vp").onchange = () => {
      const [w, h] = $("#uxc-vp").value.split("x").map(Number);
      UXC.vp = { width: w, height: h }; drawStage();
    };
    document.querySelectorAll("[data-uadd]").forEach(b => b.onclick = () => addElement(b.dataset.uadd, b.dataset.ul, +b.dataset.uw, +b.dataset.uh));
    $("#uxc-undo").onclick = () => {
      if (!UXC.undo.length) return;
      UXC.redo.push(JSON.stringify({ items: UXC.items, removed: [...UXC.removed] }));
      const p = JSON.parse(UXC.undo.pop()); UXC.items = p.items; UXC.removed = new Set(p.removed); UXC.sel = "";
      drawStage(); drawInspector();
    };
    $("#uxc-redo").onclick = () => {
      if (!UXC.redo.length) return;
      UXC.undo.push(JSON.stringify({ items: UXC.items, removed: [...UXC.removed] }));
      const p = JSON.parse(UXC.redo.pop()); UXC.items = p.items; UXC.removed = new Set(p.removed); UXC.sel = "";
      drawStage(); drawInspector();
    };
    $("#uxc-json").onclick = () => { UXC.json = !UXC.json; G.render(); };
    if (UXC.json) {
      $("#uxc-apply-json").onclick = run(async () => {
        const parsed = JSON.parse($("#uxc-raw").value);
        UXC.items = {};
        Object.entries(parsed).forEach(([sel, v], i) => { UXC.items[sel] = ensureBoxFields(sel, v, i); });
        UXC.json = false; G.render();
      });
    } else {
      drawStage();
    }
    drawInspector();
    $("#uxc-save").onclick = run(() => submitToAgent(false));
    $("#uxc-deliver").onclick = run(() => submitToAgent(!UXC.decisionId));
  };
})();
