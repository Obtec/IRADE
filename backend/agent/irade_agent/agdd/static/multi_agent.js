// Multi-agent team canvas: design agent teams as a node graph (inputs, data sources, agents, outputs),
// configure what each agent receives and its default prompt, then run the team and watch agents collaborate.
(() => {
  const G = window.AGDD;
  const { $, esc, api, toast, run, stale } = G;
  const MA = G.MA = G.MA || {};
  Object.assign(MA, {
    teams: MA.teams || [], presets: MA.presets || {}, teamId: MA.teamId || "", name: MA.name || "새 팀",
    graph: MA.graph || null, sel: MA.sel || null, dirty: !!MA.dirty, json: false,
    run: MA.run || null, poll: null, flashes: MA.flashes || [], vm: false,
  });

  const NODE_W = 210, NODE_H = 96, PORT_Y = 48;
  const KIND = {
    input: { color: "#3b82f6", icon: "⤷", name: "입력" },
    data: { color: "#8b5cf6", icon: "▤", name: "데이터" },
    agent: { color: "#2f7d64", icon: "◉", name: "에이전트" },
    output: { color: "#c17d00", icon: "⚑", name: "출력" },
  };
  const ACCEPTS = [["request", "사용자 요청"], ["data", "데이터 소스"], ["agent", "동료 결과"], ["feedback", "리뷰 피드백"], ["message", "직접 메시지"]];
  const SOURCES = [["spec_scope", "명세 범위 (ACCEPT_NOW)"], ["ux_layout", "UX 2D 레이아웃"], ["repo_files", "저장소 파일"], ["cells", "노트북 셀"], ["collab_history", "협업 대화 기록"], ["text", "고정 텍스트"]];
  const WHEN = { always: ["항상", "var(--muted)"], approve: ["승인 시", "var(--ok)"], revise: ["수정 요청 시", "var(--warn)"] };
  const STATUS = { idle: "대기", running: "실행 중", done: "완료", failed: "실패", skipped: "건너뜀" };

  if (!document.getElementById("ma-style")) {
    const st = document.createElement("style"); st.id = "ma-style";
    st.textContent = `
      .ma-stage { position:relative; overflow:auto; background:
        radial-gradient(circle, color-mix(in srgb,var(--line) 90%,transparent) 1px, transparent 1px) 0 0/20px 20px, var(--bg); }
      .ma-board { position:relative; }
      .ma-svg { position:absolute; inset:0; overflow:visible; }
      .ma-edge { fill:none; stroke-width:2.2; }
      .ma-edge-hit { fill:none; stroke:transparent; stroke-width:14; cursor:pointer; pointer-events:stroke; }
      .ma-edge.on { stroke-width:3.6; }
      .ma-edge.flow { stroke-dasharray:8 6; animation:maflow .6s linear infinite; stroke-width:3.2; }
      .ma-msg { fill:none; stroke:#a855f7; stroke-width:2.4; stroke-dasharray:3 5; animation:maflow .5s linear infinite; }
      @keyframes maflow { to { stroke-dashoffset:-28; } }
      .ma-elabel { font:11px system-ui,sans-serif; fill:var(--muted); paint-order:stroke; stroke:var(--bg); stroke-width:4px; pointer-events:none; }
      .ma-node { position:absolute; width:${NODE_W}px; height:${NODE_H}px; box-sizing:border-box; border:1.5px solid var(--line); border-radius:10px;
        background:var(--panel); box-shadow:0 1px 3px rgba(0,0,0,.08); cursor:grab; user-select:none; display:flex; flex-direction:column; }
      .ma-node.on { box-shadow:0 0 0 3px color-mix(in srgb,var(--accent) 45%,transparent); }
      .ma-node .hd { display:flex; align-items:center; gap:6px; padding:6px 10px; border-radius:8px 8px 0 0; color:#fff; font-size:12.5px; font-weight:600; }
      .ma-node .hd .id { margin-left:auto; font:10.5px ui-monospace,Menlo,monospace; opacity:.85; font-weight:400; }
      .ma-node .bd { padding:6px 10px; font-size:11.5px; color:var(--muted); line-height:1.35; overflow:hidden; flex:1; }
      .ma-node .bd .chips { display:flex; gap:4px; flex-wrap:wrap; margin-top:3px; }
      .ma-node .bd .chip { font-size:10.5px; padding:0 6px; }
      .ma-port { position:absolute; top:${PORT_Y - 7}px; width:14px; height:14px; border-radius:50%; background:var(--panel); border:2px solid var(--muted); box-sizing:border-box; }
      .ma-port.in { left:-8px; } .ma-port.out { right:-8px; cursor:crosshair; }
      .ma-port.out:hover { background:var(--accent); border-color:var(--accent); }
      .ma-st { position:absolute; right:8px; bottom:6px; font-size:10.5px; padding:0 6px; border-radius:99px; background:var(--chip); color:var(--muted); }
      .ma-node.s-running { border-color:var(--warn); animation:mapulse 1.2s ease-in-out infinite; }
      .ma-node.s-running .ma-st { background:var(--warn); color:#fff; }
      .ma-node.s-done { border-color:var(--ok); } .ma-node.s-done .ma-st { background:var(--ok); color:#fff; }
      .ma-node.s-failed { border-color:var(--bad); } .ma-node.s-failed .ma-st { background:var(--bad); color:#fff; }
      .ma-node.s-skipped { opacity:.55; }
      @keyframes mapulse { 50% { box-shadow:0 0 0 6px color-mix(in srgb,var(--warn) 25%,transparent); } }
      .ma-run { display:grid; grid-template-columns: 340px minmax(0,1fr); gap:12px; margin-top:12px; }
      @media (max-width:1100px) { .ma-run { grid-template-columns:1fr; } }
      .ma-feed { max-height:420px; overflow:auto; display:flex; flex-direction:column; gap:6px; }
      .ma-ev { border:1px solid var(--line); border-radius:8px; padding:6px 10px; background:var(--panel); font-size:12.5px; }
      .ma-ev.msg { border-color:color-mix(in srgb,#a855f7 50%,var(--line)); background:color-mix(in srgb,#a855f7 6%,var(--panel)); }
      .ma-ev.bad { border-color:var(--bad); } .ma-ev.fin { border-color:var(--ok); }
      .ma-ev summary { cursor:pointer; } .ma-ev pre { white-space:pre-wrap; word-break:break-word; margin:6px 0 0; font-size:12px; max-height:300px; overflow:auto; }
      .ma-checks { display:flex; flex-wrap:wrap; gap:4px 12px; font-size:12.5px; color:var(--ink); }
      .ma-hint { font-size:11.5px; color:var(--muted); line-height:1.55; }
      .ma-hint code { font-size:11px; background:var(--chip); padding:0 4px; border-radius:4px; }
    `;
    document.head.appendChild(st);
  }

  const WSP = () => `/workspaces/${G.getWS()}`;
  const node = id => MA.graph.nodes.find(n => n.id === id);
  const edgeKey = e => `${e.from}>${e.to}:${e.when || "always"}`;
  const touch = () => { MA.dirty = true; const b = $("#ma-save"); if (b) b.textContent = "저장 *"; };
  const nodeState = id => MA.run?.node_state?.[id] || null;

  function newId(kind) {
    let i = 1; const base = kind === "agent" ? "agent" : kind;
    while (node(`${base}_${i}`)) i++;
    return `${base}_${i}`;
  }

  function addNode(kind, x, y) {
    const id = newId(kind);
    const n = { id, kind, label: { input: "요청", data: "데이터", agent: `에이전트 ${id.split("_")[1]}`, output: "결과" }[kind], x: x ?? 80, y: y ?? 80 };
    if (kind === "agent") Object.assign(n, { role: "", system_prompt: "", prompt_template: "", accepts: ACCEPTS.map(a => a[0]), input_mode: "all", mode: "think", cli: "", model: "", effort: "", max_runs: 3, can_message: "*", blackboard: false });
    if (kind === "data") Object.assign(n, { source: "spec_scope", params: { domain: "" } });
    if (kind === "input") n.default_request = "";
    MA.graph.nodes.push(n); MA.sel = { type: "node", id }; touch(); draw(true);
  }

  function addEdge(from, to) {
    const a = node(from), b = node(to);
    if (!a || !b || from === to) return;
    if (["input", "data"].includes(b.kind)) return toast(`${KIND[b.kind].name} 노드는 입력을 받을 수 없습니다`, true);
    if (a.kind === "output") return toast("출력 노드에서는 연결할 수 없습니다", true);
    const e = { id: `e${Date.now().toString(36)}`, from, to, when: "always", label: "" };
    if (MA.graph.edges.some(x => edgeKey(x) === edgeKey(e))) return toast("이미 연결되어 있습니다", true);
    MA.graph.edges.push(e); MA.sel = { type: "edge", id: e.id }; touch(); draw(true);
  }

  function removeSelected() {
    if (!MA.sel) return;
    if (MA.sel.type === "node") {
      const id = MA.sel.id;
      MA.graph.nodes = MA.graph.nodes.filter(n => n.id !== id);
      MA.graph.edges = MA.graph.edges.filter(e => e.from !== id && e.to !== id);
      MA.graph.nodes.forEach(n => { if (Array.isArray(n.can_message)) n.can_message = n.can_message.filter(x => x !== id); });
    } else MA.graph.edges = MA.graph.edges.filter(e => e.id !== MA.sel.id);
    MA.sel = null; touch(); draw(true);
  }

  function renameNode(oldId, nextId) {
    if (!/^[A-Za-z][A-Za-z0-9_-]{0,39}$/.test(nextId)) return toast("ID: 영문으로 시작, 영문/숫자/_/- 최대 40자", true), false;
    if (node(nextId)) return toast("이미 있는 ID입니다", true), false;
    node(oldId).id = nextId;
    MA.graph.edges.forEach(e => { if (e.from === oldId) e.from = nextId; if (e.to === oldId) e.to = nextId; });
    MA.graph.nodes.forEach(n => { if (Array.isArray(n.can_message)) n.can_message = n.can_message.map(x => x === oldId ? nextId : x); });
    MA.sel = { type: "node", id: nextId }; touch(); return true;
  }

  // ------------------------------------------------------------ canvas drawing
  function nodeBody(n) {
    if (n.kind === "agent") {
      const chips = [n.mode === "code" ? "💻 code" : "💭 think", n.cli || "기본 CLI", n.model || null, n.input_mode === "any" ? "any" : null].filter(Boolean);
      return `${esc(n.role || "역할 미지정")}<div class="chips">${chips.map(c => `<span class="chip">${esc(c)}</span>`).join("")}</div>`;
    }
    if (n.kind === "data") return `${esc((SOURCES.find(s => s[0] === n.source) || [, n.source])[1])}<div class="chips">${Object.entries(n.params || {}).filter(([, v]) => v && String(v).length).slice(0, 2).map(([k, v]) => `<span class="chip">${esc(k)}: ${esc(String(v).slice(0, 18))}</span>`).join("")}</div>`;
    if (n.kind === "input") return esc(n.default_request ? n.default_request.slice(0, 80) : "실행 시 입력한 요청이 전달됩니다");
    const outs = MA.run?.result_json?.outputs?.[n.id];
    return outs?.length ? esc(String(outs[outs.length - 1]).slice(0, 90)) : "팀의 최종 결과를 수집합니다";
  }

  function pathFor(a, b) {
    const x1 = a.x + NODE_W, y1 = a.y + PORT_Y, x2 = b.x, y2 = b.y + PORT_Y;
    if (x2 > x1 + 30) { const dx = Math.max(40, (x2 - x1) / 2); return { d: `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`, mx: (x1 + x2) / 2, my: (y1 + y2) / 2 }; }
    // backward edge (feedback loop): route under the nodes
    const low = Math.max(a.y, b.y) + NODE_H + 50;
    return { d: `M${x1},${y1} C${x1 + 80},${y1} ${x1 + 80},${low} ${(x1 + x2) / 2},${low} S${x2 - 80},${y2} ${x2},${y2}`, mx: (x1 + x2) / 2, my: low };
  }

  // withInsp: also rebuild the inspector (only when the selection changed, so typing is never interrupted)
  function draw(withInsp) {
    const stage = $("#ma-stage"); if (!stage || !MA.graph) return;
    if (withInsp) drawInspector();
    if (MA.dragging) return;  // the board is redrawn on pointerup
    const now = Date.now();
    MA.flashes = MA.flashes.filter(f => now - f.t < 2600);
    const flowing = new Set(MA.flashes.filter(f => f.kind === "edge").map(f => `${f.from}>${f.to}`));
    const W = Math.max(1400, ...MA.graph.nodes.map(n => n.x + NODE_W + 300));
    const H = Math.max(640, ...MA.graph.nodes.map(n => n.y + NODE_H + 220));
    const edges = MA.graph.edges.map(e => {
      const a = node(e.from), b = node(e.to); if (!a || !b) return "";
      const p = pathFor(a, b), w = e.when || "always", on = MA.sel?.type === "edge" && MA.sel.id === e.id;
      const color = WHEN[w][1], dash = w === "revise" ? ` stroke-dasharray="7 5"` : "";
      const lbl = e.label || (w !== "always" ? WHEN[w][0] : "");
      return `<path class="ma-edge ${on ? "on" : ""} ${flowing.has(`${e.from}>${e.to}`) ? "flow" : ""}" d="${p.d}" stroke="${color}"${dash} marker-end="url(#ma-arr-${w})"/>
        <path class="ma-edge-hit" data-edge="${esc(e.id)}" d="${p.d}"/>
        ${lbl ? `<text class="ma-elabel" x="${p.mx}" y="${p.my - 6}" text-anchor="middle">${esc(lbl)}</text>` : ""}`;
    }).join("");
    const msgs = MA.flashes.filter(f => f.kind === "msg").map(f => {
      const a = node(f.from), b = node(f.to); if (!a || !b) return "";
      const x1 = a.x + NODE_W / 2, y1 = a.y, x2 = b.x + NODE_W / 2, y2 = b.y;
      const top = Math.min(y1, y2) - 70;
      return `<path class="ma-msg" d="M${x1},${y1} C${x1},${top} ${x2},${top} ${x2},${y2}" marker-end="url(#ma-arr-msg)"/>`;
    }).join("");
    const markers = Object.entries({ always: "var(--muted)", approve: "var(--ok)", revise: "var(--warn)", msg: "#a855f7" })
      .map(([k, c]) => `<marker id="ma-arr-${k}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="${c}"/></marker>`).join("");
    const nodes = MA.graph.nodes.map(n => {
      const s = nodeState(n.id), on = MA.sel?.type === "node" && MA.sel.id === n.id;
      const stTxt = s ? `${STATUS[s.status] || s.status}${s.runs > 1 ? ` ×${s.runs}` : ""}${s.verdict && s.verdict !== "none" ? ` · ${s.verdict}` : ""}` : "";
      return `<div class="ma-node ${on ? "on" : ""} ${s ? "s-" + esc(s.status) : ""}" data-node="${esc(n.id)}" style="left:${n.x}px;top:${n.y}px">
        <div class="hd" style="background:${KIND[n.kind].color}"><span>${KIND[n.kind].icon}</span><span>${esc(n.label)}</span><span class="id">${esc(n.id)}</span></div>
        <div class="bd">${nodeBody(n)}</div>
        ${n.kind !== "input" && n.kind !== "data" ? `<div class="ma-port in" data-in="${esc(n.id)}"></div>` : ""}
        ${n.kind !== "output" ? `<div class="ma-port out" data-out="${esc(n.id)}" title="드래그해서 다른 노드에 연결"></div>` : ""}
        ${stTxt ? `<span class="ma-st">${esc(stTxt)}</span>` : ""}
      </div>`;
    }).join("");
    if (MA.fitPending && stage.clientWidth) {  // first draw of a graph: zoom so the whole team is visible
      const maxX = Math.max(...MA.graph.nodes.map(n => n.x + NODE_W)) + 60, maxY = Math.max(...MA.graph.nodes.map(n => n.y + NODE_H)) + 140;
      MA.zoom = Math.max(0.4, Math.min(1, (stage.clientWidth - 20) / maxX, (stage.clientHeight - 20) / maxY));
      MA.fitPending = false;
    }
    const z = MA.zoom || 1;
    const zl = $("#ma-zoomlbl"); if (zl) zl.textContent = Math.round(z * 100) + "%";
    stage.innerHTML = `<div style="width:${W * z}px;height:${H * z}px"><div class="ma-board" id="ma-board" style="width:${W}px;height:${H}px;transform:scale(${z});transform-origin:0 0">
      <svg class="ma-svg" width="${W}" height="${H}"><defs>${markers}</defs>${edges}${msgs}<path id="ma-temp" class="ma-edge" stroke="var(--accent)" stroke-dasharray="5 4" d=""/></svg>
      ${nodes}</div></div>`;
    bindBoard();
  }

  function bindBoard() {
    const board = $("#ma-board"); if (!board) return;
    let drag = null;
    const pt = e => { const r = board.getBoundingClientRect(), z = MA.zoom || 1; return { x: (e.clientX - r.left) / z, y: (e.clientY - r.top) / z }; };
    board.onpointerdown = e => {
      const out = e.target.closest("[data-out]");
      if (out) {
        e.stopPropagation(); board.setPointerCapture(e.pointerId);
        const n = node(out.dataset.out);
        drag = { mode: "link", from: n.id, x1: n.x + NODE_W, y1: n.y + PORT_Y }; MA.dragging = true;
        return;
      }
      const hit = e.target.closest("[data-edge]");
      if (hit) { MA.sel = { type: "edge", id: hit.dataset.edge }; draw(true); return; }
      const el = e.target.closest("[data-node]");
      if (el) {
        const n = node(el.dataset.node); MA.sel = { type: "node", id: n.id };
        board.querySelectorAll(".ma-node").forEach(x => x.classList.toggle("on", x === el));
        drawInspector();
        board.setPointerCapture(e.pointerId);
        const p = pt(e); drag = { mode: "move", n, el, dx: p.x - n.x, dy: p.y - n.y, moved: false }; MA.dragging = true;
        return;
      }
      if (MA.sel) { MA.sel = null; draw(true); }
    };
    board.onpointermove = e => {
      if (!drag) return;
      const p = pt(e);
      if (drag.mode === "move") {
        drag.n.x = Math.max(0, Math.round((p.x - drag.dx) / 10) * 10);
        drag.n.y = Math.max(0, Math.round((p.y - drag.dy) / 10) * 10);
        drag.el.style.left = drag.n.x + "px"; drag.el.style.top = drag.n.y + "px"; drag.moved = true;
        // live-update edges touching this node
        const svgEdges = MA.graph.edges.filter(x => x.from === drag.n.id || x.to === drag.n.id);
        if (svgEdges.length && !drag.raf) drag.raf = requestAnimationFrame(() => { drag && (drag.raf = 0); redrawEdgesOnly(); });
      } else {
        const dx = Math.max(40, Math.abs(p.x - drag.x1) / 2);
        $("#ma-temp").setAttribute("d", `M${drag.x1},${drag.y1} C${drag.x1 + dx},${drag.y1} ${p.x - dx},${p.y} ${p.x},${p.y}`);
      }
    };
    board.onpointerup = e => {
      if (!drag) return;
      const d = drag; drag = null; MA.dragging = false;
      if (d.mode === "link") {
        $("#ma-temp").setAttribute("d", "");
        const target = document.elementFromPoint(e.clientX, e.clientY)?.closest("[data-node]");
        if (target && target.dataset.node !== d.from) addEdge(d.from, target.dataset.node);
      } else { if (d.moved) touch(); draw(); }
    };
    board.onpointercancel = () => { drag = null; MA.dragging = false; draw(); };
    board.ondblclick = e => {
      if (e.target.closest("[data-node]")) return;
      const p = pt(e); addNode("agent", Math.round((p.x - NODE_W / 2) / 10) * 10, Math.round((p.y - 20) / 10) * 10);
    };
  }

  function redrawEdgesOnly() {
    const svg = $("#ma-board svg"); if (!svg) return;
    const parts = MA.graph.edges.map(e => { const a = node(e.from), b = node(e.to); return a && b ? pathFor(a, b) : null; });
    const paths = svg.querySelectorAll(".ma-edge:not(#ma-temp)"), hits = svg.querySelectorAll(".ma-edge-hit");
    parts.forEach((p, i) => { if (p && paths[i]) { paths[i].setAttribute("d", p.d); hits[i]?.setAttribute("d", p.d); } });
    svg.querySelectorAll(".ma-elabel").forEach(t => t.remove());
  }

  // ------------------------------------------------------------ inspector
  const opt = (v, cur, label) => `<option value="${esc(v)}" ${String(v) === String(cur ?? "") ? "selected" : ""}>${esc(label ?? (v || "기본값"))}</option>`;

  function drawInspector() {
    const el = $("#ma-insp"); if (!el || !MA.graph) return;
    const sel = MA.sel;
    if (sel?.type === "edge") {
      const e = MA.graph.edges.find(x => x.id === sel.id); if (!e) { MA.sel = null; return drawInspector(); }
      const fromAgent = node(e.from)?.kind === "agent";
      el.innerHTML = `<div class="ide-head">연결 · ${esc(e.from)} → ${esc(e.to)}</div><div class="g-form">
        <label class="fld"><span>전달 조건</span><select id="ma-e-when" ${fromAgent ? "" : "disabled"}>${Object.entries(WHEN).map(([k, [l]]) => opt(k, e.when, l)).join("")}</select></label>
        <div class="ma-hint">• <b>항상</b>: 완료될 때마다 결과 전달<br>• <b>승인 시</b>: 에이전트가 approve(또는 판정 없음)일 때<br>• <b>수정 요청 시</b>: revise일 때 — 대상은 결과를 <i>리뷰 피드백</i>으로 받아 다시 작업합니다 (리뷰 루프)</div>
        <label class="fld"><span>레이블</span><input id="ma-e-label" value="${esc(e.label || "")}" placeholder="예: 수정 요청"></label>
        <button class="g bad" id="ma-e-del">연결 삭제</button></div>`;
      $("#ma-e-when").onchange = ev => { e.when = ev.target.value; touch(); draw(); };
      $("#ma-e-label").onchange = ev => { e.label = ev.target.value; touch(); draw(); };
      $("#ma-e-del").onclick = removeSelected;
      return;
    }
    if (sel?.type === "node") {
      const n = node(sel.id); if (!n) { MA.sel = null; return drawInspector(); }
      const common = `<label class="fld"><span>ID (프롬프트·메시지 주소)</span><input id="ma-n-id" class="mono" value="${esc(n.id)}"></label>
        <label class="fld"><span>이름</span><input id="ma-n-label" value="${esc(n.label)}"></label>`;
      let body = "";
      if (n.kind === "agent") {
        const others = MA.graph.nodes.filter(x => x.kind === "agent" && x.id !== n.id);
        const all = n.can_message === "*";
        body = `
          <label class="fld"><span>역할 (팀원에게 보이는 한 줄 설명)</span><input id="ma-a-role" value="${esc(n.role || "")}" placeholder="예: 구현을 검토해 승인/수정 요청"></label>
          <label class="fld"><span>기본 프롬프트 (시스템 지침)</span><textarea id="ma-a-sys" rows="5" style="min-height:96px" placeholder="이 에이전트가 항상 따를 지침">${esc(n.system_prompt || "")}</textarea></label>
          <label class="fld"><span>입력 템플릿</span><textarea id="ma-a-tpl" rows="4" class="mono" style="min-height:76px" placeholder="{{auto}}">${esc(n.prompt_template || "")}</textarea></label>
          <div class="ma-hint">비우면 <code>{{auto}}</code> — 받은 입력을 종류별 섹션으로 자동 구성. 사용 가능: <code>{{request}}</code> <code>{{data}}</code> <code>{{agents}}</code> <code>{{feedback}}</code> <code>{{messages}}</code> <code>{{blackboard}}</code> <code>{{input:노드id}}</code></div>
          <div class="fld"><span>받아들일 입력 종류</span><div class="ma-checks">${ACCEPTS.map(([k, l]) => `<label><input type="checkbox" data-acc="${k}" ${n.accepts?.includes(k) ? "checked" : ""}> ${l}</label>`).join("")}</div></div>
          <div class="row">
            <label class="fld" style="flex:1"><span>실행 시점</span><select id="ma-a-imode">${opt("all", n.input_mode, "모든 상위 입력 대기")}${opt("any", n.input_mode, "입력 올 때마다")}</select></label>
            <label class="fld" style="flex:1"><span>모드</span><select id="ma-a-mode">${opt("think", n.mode, "💭 think (답변)")}${opt("code", n.mode, "💻 code (저장소 작업)")}</select></label>
          </div>
          <div class="row">
            <label class="fld" style="flex:1"><span>CLI</span><select id="ma-a-cli">${opt("", n.cli, "워크스페이스 기본")}${["claude", "codex", "agy"].map(c => opt(c, n.cli)).join("")}</select></label>
            <label class="fld" style="flex:1"><span>노력</span><select id="ma-a-eff">${opt("", n.effort, "기본")}${["low", "medium", "high", "xhigh", "max"].map(c => opt(c, n.effort)).join("")}</select></label>
          </div>
          <div class="row">
            <label class="fld" style="flex:2"><span>모델 (비우면 기본)</span><input id="ma-a-model" class="mono" value="${esc(n.model || "")}" placeholder="opus / gpt-5.5 …"></label>
            <label class="fld" style="flex:1"><span>최대 실행</span><input id="ma-a-max" type="number" min="1" max="10" value="${n.max_runs ?? 3}"></label>
          </div>
          <div class="fld"><span>직접 메시지를 보낼 수 있는 동료</span><div class="ma-checks">
            <label><input type="checkbox" id="ma-a-cmall" ${all ? "checked" : ""}> 모두</label>
            ${others.map(o => `<label><input type="checkbox" data-cm="${esc(o.id)}" ${all || n.can_message.includes(o.id) ? "checked" : ""} ${all ? "disabled" : ""}> ${esc(o.label)}</label>`).join("") || `<span class="muted">다른 에이전트 없음</span>`}
          </div></div>
          <label class="row" style="font-size:12.5px"><input type="checkbox" id="ma-a-bb" ${n.blackboard ? "checked" : ""}> <span>팀 공유 보드(모든 에이전트의 최근 결과) 참조</span></label>`;
      } else if (n.kind === "data") {
        const p = n.params || {};
        const fields = {
          spec_scope: `<label class="fld"><span>도메인 (비우면 전체)</span><input data-p="domain" value="${esc(p.domain || "")}"></label>`,
          ux_layout: `<label class="fld"><span>UX 뷰</span><input data-p="view_key" class="mono" value="${esc(p.view_key || "home")}"></label>`,
          repo_files: `<label class="fld"><span>파일 경로 (쉼표/줄바꿈, 최대 8개)</span><textarea data-p="paths" class="mono" rows="3">${esc((p.paths || []).join("\n"))}</textarea></label>
            <label class="fld"><span>저장소 (비우면 팀 기본)</span><input data-p="repo_key" class="mono" value="${esc(p.repo_key || "")}"></label>`,
          cells: `<div class="ma-hint">VM의 노트북 셀 전체를 JSON으로 전달합니다.</div>`,
          collab_history: `<label class="fld"><span>최근 메시지 수</span><input data-p="limit" type="number" min="1" max="100" value="${p.limit ?? 20}"></label>`,
          text: `<label class="fld"><span>텍스트</span><textarea data-p="text" rows="6">${esc(p.text || "")}</textarea></label>`,
        };
        body = `<label class="fld"><span>데이터 소스</span><select id="ma-d-src">${SOURCES.map(([k, l]) => opt(k, n.source, l)).join("")}</select></label>${fields[n.source] || ""}
          <div class="ma-hint">실행 시작 시 한 번 조회되어 연결된 에이전트에 <i>데이터</i> 입력으로 전달됩니다.</div>`;
      } else if (n.kind === "input") {
        body = `<label class="fld"><span>기본 요청 (실행 시 비워두면 사용)</span><textarea id="ma-i-def" rows="4">${esc(n.default_request || "")}</textarea></label>`;
      } else {
        const outs = MA.run?.result_json?.outputs?.[n.id] || [];
        body = outs.length ? `<div class="fld"><span>최근 결과</span><pre class="mono" style="white-space:pre-wrap;max-height:320px;overflow:auto">${esc(outs[outs.length - 1])}</pre></div>` : `<div class="ma-hint">연결된 에이전트의 결과가 여기에 모입니다.</div>`;
      }
      el.innerHTML = `<div class="ide-head">${KIND[n.kind].icon} ${KIND[n.kind].name} 노드</div><div class="g-form">${common}${body}<button class="g bad" id="ma-n-del">노드 삭제</button></div>`;
      $("#ma-n-id").onchange = ev => { if (!renameNode(n.id, ev.target.value.trim())) ev.target.value = n.id; else draw(true); };
      $("#ma-n-label").oninput = ev => { n.label = ev.target.value; touch(); const h = document.querySelector(`[data-node="${CSS.escape(n.id)}"] .hd span:nth-child(2)`); if (h) h.textContent = n.label; };
      $("#ma-n-del").onclick = removeSelected;
      const bind = (id, fn, evt = "onchange") => { const x = $(id); if (x) x[evt] = ev => { fn(ev.target); touch(); }; };
      if (n.kind === "agent") {
        bind("#ma-a-role", t => { n.role = t.value; draw(); });
        bind("#ma-a-sys", t => { n.system_prompt = t.value; }, "oninput");
        bind("#ma-a-tpl", t => { n.prompt_template = t.value; }, "oninput");
        bind("#ma-a-imode", t => { n.input_mode = t.value; draw(); });
        bind("#ma-a-mode", t => { n.mode = t.value; draw(); });
        bind("#ma-a-cli", t => { n.cli = t.value; draw(); });
        bind("#ma-a-eff", t => { n.effort = t.value; });
        bind("#ma-a-model", t => { n.model = t.value.trim(); draw(); });
        bind("#ma-a-max", t => { n.max_runs = Math.max(1, Math.min(10, parseInt(t.value, 10) || 3)); });
        bind("#ma-a-bb", t => { n.blackboard = t.checked; });
        document.querySelectorAll("[data-acc]").forEach(c => c.onchange = () => { n.accepts = ACCEPTS.map(a => a[0]).filter(k => document.querySelector(`[data-acc="${k}"]`).checked); touch(); });
        bind("#ma-a-cmall", t => { n.can_message = t.checked ? "*" : MA.graph.nodes.filter(x => x.kind === "agent" && x.id !== n.id).map(x => x.id); drawInspector(); });
        document.querySelectorAll("[data-cm]").forEach(c => c.onchange = () => { n.can_message = [...document.querySelectorAll("[data-cm]")].filter(x => x.checked).map(x => x.dataset.cm); touch(); });
      } else if (n.kind === "data") {
        bind("#ma-d-src", t => { n.source = t.value; n.params = {}; draw(true); });
        document.querySelectorAll("[data-p]").forEach(f => f.onchange = () => {
          n.params = n.params || {};
          const k = f.dataset.p;
          n.params[k] = k === "paths" ? f.value.split(/[\s,]+/).filter(Boolean) : k === "limit" ? parseInt(f.value, 10) || 20 : f.value;
          touch(); draw();
        });
      } else if (n.kind === "input") bind("#ma-i-def", t => { n.default_request = t.value; draw(); });
      return;
    }
    const st = MA.graph.settings || (MA.graph.settings = {});
    const agents = MA.graph.nodes.filter(n => n.kind === "agent").length;
    el.innerHTML = `<div class="ide-head">팀 설정</div><div class="g-form">
      <div class="muted" style="font-size:12.5px">노드 ${MA.graph.nodes.length}개 · 에이전트 ${agents}명 · 연결 ${MA.graph.edges.length}개</div>
      <div class="row">
        <label class="fld" style="flex:1"><span>최대 단계</span><input id="ma-s-steps" type="number" min="1" max="100" value="${st.max_steps ?? 24}"></label>
        <label class="fld" style="flex:1"><span>동시 실행</span><input id="ma-s-par" type="number" min="1" max="6" value="${st.parallelism ?? 3}"></label>
      </div>
      <label class="fld"><span>저장소 (code 모드, 비우면 기본)</span><input id="ma-s-repo" class="mono" value="${esc(st.repo_key || "")}"></label>
      <div class="ma-hint">
        • 툴바 버튼 또는 빈 곳 <b>더블클릭</b>: 노드 추가<br>
        • 노드 오른쪽 <b>●</b>을 끌어 다른 노드에 놓으면 연결<br>
        • 연결을 클릭해 조건 변경 — <b>수정 요청 시</b> 연결로 리뷰 루프를 만듭니다<br>
        • 에이전트는 실행 중 동료에게 <b>직접 메시지</b>를 보내 질문·답변하며 협업합니다 (보라색 점선)<br>
        • <kbd>Delete</kbd>: 선택 항목 삭제 · 모든 실행은 VM 안에서 각 에이전트별 CLI 세션으로 병렬 진행
      </div></div>`;
    $("#ma-s-steps").onchange = e => { st.max_steps = Math.max(1, Math.min(100, parseInt(e.target.value, 10) || 24)); touch(); };
    $("#ma-s-par").onchange = e => { st.parallelism = Math.max(1, Math.min(6, parseInt(e.target.value, 10) || 3)); touch(); };
    $("#ma-s-repo").onchange = e => { st.repo_key = e.target.value.trim(); touch(); };
  }

  // ------------------------------------------------------------ run panel
  const EV = {
    run_started: ["▶", "실행 시작"], data_resolved: ["▤", "데이터 조회"], data_failed: ["⚠", "데이터 조회 실패"],
    node_started: ["⏳", "작업 시작"], node_finished: ["✅", "완료"], node_failed: ["❌", "실패"], message: ["✉", "메시지"],
    message_rejected: ["⚠", "메시지 거부"], output: ["⚑", "결과 도착"], dropped: ["⚠", "입력 무시"], node_skipped: ["⏭", "건너뜀"],
    run_finished: ["■", "실행 종료"], delivery: ["→", "전달"],
  };
  const label = id => node(id)?.label || id || "";

  function drawFeed() {
    const el = $("#ma-feed"); if (!el) return;
    const r = MA.run;
    if (!r) { el.innerHTML = `<div class="muted" style="font-size:12.5px">아직 실행 기록이 없습니다. 요청을 입력하고 팀을 실행하세요.</div>`; return; }
    const evs = (r.events || []).filter(e => e.type !== "delivery");
    el.innerHTML = evs.slice().reverse().map(e => {
      const [ic, name] = EV[e.type] || ["•", e.type];
      const m = e.meta_json || {};
      const who = e.type === "message" ? `<b>${esc(label(e.node))}</b> → <b>${esc(label(e.target))}</b>`
        : e.type === "output" ? `<b>${esc(label(e.node))}</b> → ⚑ ${esc(label(e.target))}`
        : e.node ? `<b>${esc(label(e.node))}</b>` : "";
      const extra = [m.verdict && m.verdict !== "none" ? `<span class="chip">${esc(m.verdict)}</span>` : "", m.run_no > 1 ? `<span class="chip">#${m.run_no}</span>` : "",
        m.ms ? `<span class="muted">${(m.ms / 1000).toFixed(1)}s</span>` : "", m.status ? `<span class="chip ${G.statusCls(m.status)}">${esc(m.status)}</span>` : "",
        m.changed_files?.length ? `<span class="chip">파일 ${m.changed_files.length}</span>` : ""].join(" ");
      const cls = e.type === "message" ? "msg" : ["node_failed", "data_failed", "message_rejected"].includes(e.type) ? "bad" : e.type === "run_finished" ? "fin" : "";
      const head = `${ic} ${name} ${who} ${extra}`;
      return `<div class="ma-ev ${cls}">${e.content ? `<details ${e.type === "message" ? "open" : ""}><summary>${head}</summary><pre>${esc(e.content)}</pre></details>` : head}</div>`;
    }).join("") || `<div class="muted">이벤트 대기 중…</div>`;
  }

  function drawRunHead() {
    const el = $("#ma-runhead"); if (!el) return;
    const r = MA.run;
    el.innerHTML = r ? `<span class="chip ${G.statusCls(r.status === "PARTIAL" ? "DEFER" : r.status)}">${esc(r.status)}</span> <span class="muted mono">${esc(String(r.team_run_id).slice(0, 8))}</span>
      ${r.active ? `<button class="g" id="ma-cancel" style="padding:2px 8px;font-size:12px">중지</button>` : ""}
      ${r.result_json?.error ? `<div class="bad" style="font-size:12.5px;margin-top:4px">${esc(r.result_json.error)}</div>` : ""}` : "";
    const c = $("#ma-cancel"); if (c) c.onclick = run(async () => { await api("POST", `${WSP()}/team-runs/${r.team_run_id}/cancel`, {}, { noInvalidate: true }); toast("중지 요청됨 — 진행 중인 에이전트가 끝나면 멈춥니다"); });
  }

  function ingest(data, reset) {
    if (reset || !MA.run || MA.run.team_run_id !== data.team_run_id) MA.run = { ...data, events: [] };
    const known = new Set(MA.run.events.map(e => e.seq));
    const fresh = (data.events || []).filter(e => !known.has(e.seq));
    Object.assign(MA.run, { ...data, events: MA.run.events.concat(fresh) });
    const now = Date.now();
    for (const e of fresh) {
      if (e.type === "delivery" || e.type === "output") MA.flashes.push({ kind: "edge", from: e.node, to: e.target, t: now });
      if (e.type === "message") MA.flashes.push({ kind: "msg", from: e.node, to: e.target, t: now });
    }
    return fresh.length;
  }

  async function poll() {
    clearTimeout(MA.poll);
    if (!MA.run || G.getTAB() !== "team") return;
    const after = MA.run.events.length ? Math.max(...MA.run.events.map(e => e.seq)) : 0;
    try {
      const data = await api("GET", `${WSP()}/team-runs/${MA.run.team_run_id}?after=${after}`, undefined, { fresh: true });
      if (G.getTAB() !== "team") return;
      ingest(data);
      draw(); drawFeed(); drawRunHead();
      if (data.status !== "RUNNING") { refreshHistory(); return; }
    } catch (e) { toast(e.message, true); }
    MA.poll = setTimeout(poll, 1500);
  }

  async function refreshHistory() {
    const sel = $("#ma-history"); if (!sel) return;
    const res = await api("GET", `${WSP()}/team-runs`, undefined, { fresh: true }).catch(() => ({ runs: [] }));
    sel.innerHTML = `<option value="">실행 기록…</option>` + (res.runs || []).map(r => `<option value="${esc(r.team_run_id)}" ${MA.run?.team_run_id === r.team_run_id ? "selected" : ""}>${esc(r.status)} · ${esc(new Date(r.created_at).toLocaleString())} · ${esc((r.request || "").slice(0, 30))}</option>`).join("");
  }

  async function startRun() {
    const request = $("#ma-request").value.trim();
    const res = await api("POST", `${WSP()}/team-runs`, { team_id: MA.teamId || null, graph: MA.graph, request }, { noInvalidate: true });
    MA.run = { team_run_id: res.team_run_id, status: "RUNNING", events: [], node_state: {}, result_json: {}, active: true };
    toast("팀 실행을 시작했습니다");
    draw(); drawFeed(); drawRunHead(); refreshHistory(); poll();
  }

  // ------------------------------------------------------------ team persistence
  function loadGraph(g, teamId, name) {
    MA.graph = JSON.parse(JSON.stringify(g));
    MA.graph.settings = MA.graph.settings || { max_steps: 24, parallelism: 3 };
    MA.graph.edges.forEach((e, i) => { e.id = e.id || `e${i + 1}`; e.when = e.when || "always"; });
    MA.graph.nodes.forEach(n => {
      if (n.kind === "agent") Object.assign(n, { accepts: n.accepts || ACCEPTS.map(a => a[0]), input_mode: n.input_mode || "all", mode: n.mode || "think", cli: n.cli || "", model: n.model || "", effort: n.effort || "", max_runs: n.max_runs ?? 3, can_message: n.can_message ?? "*", blackboard: !!n.blackboard, role: n.role || "", system_prompt: n.system_prompt || "", prompt_template: n.prompt_template || "" });
      if (n.kind === "data") n.params = n.params || {};
    });
    MA.teamId = teamId || ""; MA.name = name || MA.name; MA.sel = null; MA.dirty = false; MA.fitPending = true;
  }

  async function save() {
    const name = ($("#ma-name").value || "").trim() || "새 팀";
    const row = await api("POST", `${WSP()}/teams`, { team_id: MA.teamId || null, name, graph: MA.graph });
    MA.teamId = row.team_id; MA.name = row.name; MA.dirty = false;
    loadGraph(row.graph_json, row.team_id, row.name);
    toast("팀 저장됨"); G.render();
  }

  G.VIEWS.team = async function teamView() {
    const res = await api("GET", `${WSP()}/teams`, undefined, { fresh: true });
    if (stale("team")) return;
    MA.teams = res.teams || []; MA.presets = res.presets || {}; MA.vm = !!res.vm_enabled;
    if (MA.graph && MA.wsKey !== G.getWS()) { MA.graph = null; MA.run = null; MA.teamId = ""; }
    MA.wsKey = G.getWS();
    if (!MA.graph) {
      const t = MA.teams.find(x => x.team_id === MA.teamId) || MA.teams[0];
      if (t) loadGraph(t.graph_json, t.team_id, t.name); else loadGraph(MA.presets.review_loop, "", "기획→구현→리뷰 팀");
    }
    G.V(`<div class="row" style="justify-content:space-between;margin-bottom:10px;gap:10px;flex-wrap:wrap">
        <div><h2>멀티 에이전트 · 팀 캔버스</h2><p class="sub" style="margin:0">에이전트를 노드로 배치하고 데이터·프롬프트 흐름을 연결하면, 실제 CLI 에이전트들이 그 그래프대로 결과를 주고받고 메시지로 협업합니다.</p></div>
        <div class="row" style="flex-wrap:wrap">
          <select id="ma-team">${opt("", MA.teamId, "＋ 저장 안 된 팀")}${MA.teams.map(t => opt(t.team_id, MA.teamId, t.name)).join("")}</select>
          <input id="ma-name" value="${esc(MA.name)}" style="width:160px" placeholder="팀 이름">
          <button class="b" id="ma-save">${MA.dirty ? "저장 *" : "저장"}</button>
          ${MA.teamId ? `<button class="g bad" id="ma-del">삭제</button>` : ""}
        </div>
      </div>
      ${MA.vm ? "" : `<div class="card" style="border-color:var(--warn)">VM 런타임이 연결되지 않아 think 모드 에이전트만 LLM 게이트웨이로 실행됩니다. code 모드·저장소/셀 데이터 소스는 VM이 필요합니다.</div>`}
      <div class="g-toolbar">
        <span class="muted" style="font-size:12px">노드 추가</span>
        ${Object.entries(KIND).map(([k, v]) => `<button class="g-kind" data-add="${k}"><i style="background:${v.color}"></i>${v.name}</button>`).join("")}
        <span class="g-sep"></span>
        <span class="muted" style="font-size:12px">템플릿</span>
        <button class="g-kind" data-preset="review_loop">기획→구현→리뷰 루프</button>
        <button class="g-kind" data-preset="parallel">병렬 분석→종합</button>
        <button class="g-kind" data-preset="debate">찬반 토론→심판</button>
        <span class="g-sep"></span>
        <button class="g" id="ma-zout" title="축소">−</button><span class="mono muted" id="ma-zoomlbl" style="min-width:38px;text-align:center">100%</span><button class="g" id="ma-zin" title="확대">＋</button><button class="g" id="ma-fit">맞춤</button>
        <span class="g-sep"></span>
        <button class="g" id="ma-validate">검증</button>
        <button class="g ${MA.json ? "on" : ""}" id="ma-json">{ } JSON</button>
      </div>
      <div class="g-wrap" style="height:clamp(420px, calc(100vh - 430px), 620px);min-height:420px">
        ${MA.json ? `<div class="g-jsonbox"><textarea id="ma-raw" spellcheck="false">${esc(JSON.stringify(MA.graph, null, 2))}</textarea><div class="row" style="padding:8px"><button class="b" id="ma-apply-json">JSON 적용</button></div></div>`
          : `<div id="ma-stage" class="ma-stage"></div>`}
        <div id="ma-insp" class="g-insp"></div>
      </div>
      <div class="ma-run">
        <div class="card" style="margin:0">
          <b>팀 실행</b>
          <textarea id="ma-request" rows="5" style="width:100%;box-sizing:border-box;margin-top:8px" placeholder="팀에게 맡길 요청 (비우면 입력 노드의 기본 요청)">${esc(MA.lastRequest || "")}</textarea>
          <div class="row" style="margin-top:8px"><button class="b" id="ma-run">▶ 팀 실행</button><select id="ma-history" style="flex:1;min-width:0"><option value="">실행 기록…</option></select></div>
          <div id="ma-runhead" style="margin-top:8px"></div>
        </div>
        <div class="card" style="margin:0"><b>협업 타임라인</b> <span class="muted" style="font-size:12px">에이전트 작업·메시지·결과가 실시간으로 표시됩니다</span><div id="ma-feed" class="ma-feed" style="margin-top:8px"></div></div>
      </div>`);

    $("#ma-team").onchange = e => {
      if (MA.dirty && !confirm("저장하지 않은 변경 사항이 있습니다. 계속할까요?")) { e.target.value = MA.teamId; return; }
      const t = MA.teams.find(x => x.team_id === e.target.value);
      if (t) loadGraph(t.graph_json, t.team_id, t.name); else loadGraph(MA.presets.review_loop, "", "새 팀");
      MA.run = null; G.render();
    };
    $("#ma-name").oninput = () => { MA.name = $("#ma-name").value; touch(); };
    $("#ma-save").onclick = run(save);
    if ($("#ma-del")) $("#ma-del").onclick = run(async () => {
      if (!confirm(`팀 '${MA.name}'을(를) 삭제할까요?`)) return;
      await api("DELETE", `${WSP()}/teams/${MA.teamId}`); MA.graph = null; MA.teamId = ""; MA.run = null; G.render();
    });
    document.querySelectorAll("[data-add]").forEach(b => b.onclick = () => {
      const st = $("#ma-stage"); addNode(b.dataset.add, (st?.scrollLeft || 0) + 120 + Math.random() * 120 | 0, (st?.scrollTop || 0) + 80 + Math.random() * 120 | 0);
    });
    document.querySelectorAll("[data-preset]").forEach(b => b.onclick = () => {
      if ((MA.dirty || MA.graph.nodes.length > 0) && !confirm("현재 캔버스를 템플릿으로 바꿀까요? (저장된 팀은 저장 전까지 바뀌지 않습니다)")) return;
      const keepId = MA.teamId, keepName = MA.name;
      loadGraph(MA.presets[b.dataset.preset], keepId, keepName); touch(); G.render();
    });
    $("#ma-validate").onclick = run(async () => {
      const r = await api("POST", `${WSP()}/teams/validate`, { graph: MA.graph }, { noInvalidate: true });
      r.ok ? toast("그래프가 유효합니다 ✓") : toast(r.error, true);
    });
    $("#ma-json").onclick = () => { MA.json = !MA.json; G.render(); };
    const zoomTo = z => { MA.zoom = Math.max(0.3, Math.min(1.6, Math.round(z * 10) / 10)); draw(); };
    $("#ma-zin").onclick = () => zoomTo((MA.zoom || 1) + 0.1);
    $("#ma-zout").onclick = () => zoomTo((MA.zoom || 1) - 0.1);
    $("#ma-fit").onclick = () => { MA.fitPending = true; draw(); };
    if (MA.json) $("#ma-apply-json").onclick = run(async () => { loadGraph(JSON.parse($("#ma-raw").value), MA.teamId, MA.name); touch(); MA.json = false; G.render(); });
    $("#ma-request").oninput = e => { MA.lastRequest = e.target.value; };
    $("#ma-run").onclick = run(startRun);
    $("#ma-history").onchange = run(async e => {
      if (!e.target.value) return;
      const data = await api("GET", `${WSP()}/team-runs/${e.target.value}`, undefined, { fresh: true });
      ingest(data, true); MA.flashes = [];
      if (data.graph_json && confirm("이 실행에 사용된 그래프를 캔버스에 불러올까요? (취소: 현재 캔버스 유지)")) { loadGraph(data.graph_json, MA.teamId, MA.name); touch(); }
      draw(); drawFeed(); drawRunHead(); if (data.status === "RUNNING") poll();
    });
    const vw = $("#view");
    vw.onkeydown = null;
    document.onkeydown = e => {
      if (G.getTAB() !== "team" || !MA.sel || ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)) return;
      if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); removeSelected(); }
    };
    if (!MA.json) draw(true); else drawInspector();
    drawFeed(); drawRunHead(); refreshHistory();
    if (MA.run?.status === "RUNNING") poll();
    clearInterval(MA.anim);
    MA.anim = setInterval(() => { if (G.getTAB() !== "team") return clearInterval(MA.anim); if (MA.flashes.length) draw(); }, 900);
  };
})();
