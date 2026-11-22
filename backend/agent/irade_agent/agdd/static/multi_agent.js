// Multi-agent team canvas: design agent teams as a node graph (inputs, data sources, agents, outputs),
// configure what each agent receives (input kinds + media modalities) and its default prompt, attach images /
// video / audio / PDF / text files, then run the team and watch agents collaborate. Edge semantics cover the
// production multi-agent patterns: review loops, handoffs (OpenAI), orchestrator-worker spawn (Anthropic),
// shared state via output_key (Google ADK) and guardrail tripwires.
(() => {
  const G = window.AGDD;
  const { $, esc, api, toast, run, stale } = G;
  const MA = G.MA = G.MA || {};
  Object.assign(MA, {
    teams: MA.teams || [], presets: MA.presets || {}, teamId: MA.teamId || "", name: MA.name || "새 팀",
    graph: MA.graph || null, sel: MA.sel || null, dirty: !!MA.dirty, json: false,
    run: MA.run || null, poll: null, flashes: MA.flashes || [], vm: false,
    undo: MA.undo || [], redo: MA.redo || [],
    presetInfo: MA.presetInfo || {}, attLib: MA.attLib || {}, runAtt: MA.runAtt || [], uploads: MA.uploads || [],
    limits: MA.limits || { chunk_bytes: 4 * 1024 * 1024, max_attachment_bytes: 50 * 1024 * 1024 }, full: !!MA.full,
  });

  const NODE_W = 210, NODE_H = 96, PORT_Y = 48;
  const KIND = {
    input: { color: "#3b82f6", icon: "⤷", name: "입력" },
    data: { color: "#8b5cf6", icon: "▤", name: "데이터" },
    agent: { color: "#2f7d64", icon: "◉", name: "에이전트" },
    mock: { color: "#53648a", icon: "◇", name: "모의 에이전트" },
    output: { color: "#c17d00", icon: "⚑", name: "출력" },
  };
  const ACCEPTS = [["request", "사용자 요청"], ["data", "데이터 소스"], ["agent", "동료 결과"], ["feedback", "리뷰 피드백"], ["message", "직접 메시지"]];
  const SOURCES = [["files", "첨부 파일 (이미지·영상·음성·PDF·문서)"], ["spec_scope", "명세 범위 (ACCEPT_NOW)"], ["ux_layout", "UX 2D 레이아웃"], ["repo_files", "저장소 파일"], ["cells", "노트북 셀"], ["collab_history", "협업 대화 기록"], ["text", "고정 텍스트"]];
  const MODALITIES = [["text", "텍스트", "📝"], ["image", "이미지", "🖼"], ["video", "동영상", "🎬"], ["audio", "오디오", "🎧"], ["pdf", "PDF", "📄"], ["file", "기타 파일", "📦"]];
  const MOD_ICON = Object.fromEntries(MODALITIES.map(m => [m[0], m[2]]));
  const WHEN = { always: ["항상", "var(--muted)"], approve: ["승인 시", "var(--ok)"], revise: ["수정 요청 시", "var(--warn)"],
    handoff: ["핸드오프", "#0ea5e9"], spawn: ["하위 작업 배정", "#db2777"] };
  const AGENT_ONLY_WHEN = ["handoff", "spawn"];
  const STATUS = { idle: "대기", running: "실행 중", waiting: "워커 대기", done: "완료", failed: "실패", skipped: "건너뜀" };

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
      .ma-node.s-waiting { border-color:#db2777; } .ma-node.s-waiting .ma-st { background:#db2777; color:#fff; }
      .ma-wrap { grid-template-columns:minmax(0,1fr) 340px; }
      .ma-wrap.ma-full { position:fixed; inset:10px; z-index:60; height:auto !important; border-radius:10px; border:1px solid var(--line); box-shadow:0 12px 48px rgba(0,0,0,.28); }
      .ma-wrap.ma-full .ma-fullbar { display:flex; }
      .ma-fullbar { display:none; position:absolute; left:12px; top:10px; z-index:2; gap:6px; }
      .ma-stagebox { position:relative; min-width:0; min-height:0; display:flex; } .ma-stagebox .ma-stage { flex:1; }
      .ma-resize { height:10px; cursor:ns-resize; display:flex; align-items:center; justify-content:center; }
      .ma-resize::after { content:""; width:56px; height:4px; border-radius:4px; background:var(--line); }
      .ma-resize:hover::after { background:var(--accent); }
      .ma-att { display:flex; flex-wrap:wrap; gap:6px; }
      .ma-achip { display:inline-flex; align-items:center; gap:6px; max-width:100%; border:1px solid var(--line); border-radius:8px; padding:3px 6px 3px 3px; background:var(--bg); font-size:12px; }
      .ma-achip .th { width:34px; height:34px; border-radius:6px; background:var(--chip); display:flex; align-items:center; justify-content:center; font-size:17px; overflow:hidden; flex:none; }
      .ma-achip .th img { width:100%; height:100%; object-fit:cover; }
      .ma-achip .nm { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:170px; }
      .ma-achip .sz { color:var(--muted); font-size:10.5px; }
      .ma-achip button { border:0; background:none; color:var(--muted); cursor:pointer; padding:0 2px; font-size:14px; }
      .ma-achip .bar { height:3px; background:var(--accent); border-radius:3px; }
      .ma-drop { border:1.5px dashed var(--line); border-radius:10px; padding:10px; font-size:12px; color:var(--muted); text-align:center; cursor:pointer; }
      .ma-drop.over { border-color:var(--accent); background:color-mix(in srgb,var(--accent) 8%,transparent); color:var(--ink); }
      .ma-presets { min-width:220px; }
    `;
    document.head.appendChild(st);
  }

  const WSP = () => `/workspaces/${G.getWS()}`;
  const node = id => MA.graph.nodes.find(n => n.id === id);
  const edgeKey = e => `${e.from}>${e.to}:${e.when || "always"}`;
  const touch = () => {
    MA.dirty = true;
    const b = $("#ma-save");
    if (b) b.textContent = "저장 *";
    if ($("#ma-undo")) $("#ma-undo").disabled = !MA.undo.length;
    if ($("#ma-redo")) $("#ma-redo").disabled = !MA.redo.length;
    clearTimeout(MA._monT); MA._monT = setTimeout(() => syncMonitor(), 300);  // agents added/removed/renamed
  };
  let maLastUndoKey = "", maLastUndoAt = 0;
  function snapshotMaUndo() {
    if (!MA.graph) return;
    MA.undo.push(JSON.stringify(MA.graph));
    if (MA.undo.length > 60) MA.undo.shift();
    MA.redo = [];
    maLastUndoKey = "";
  }
  function snapshotMaUndoOnce(key) {
    const now = Date.now();
    if (maLastUndoKey === key && now - maLastUndoAt < 1200) return;
    snapshotMaUndo();
    maLastUndoKey = key;
    maLastUndoAt = now;
  }
  function performMaUndo() {
    if (!MA.undo.length) return;
    MA.redo.push(JSON.stringify(MA.graph));
    MA.graph = JSON.parse(MA.undo.pop());
    MA.sel = null;
    touch();
    MA.json ? G.render() : draw(true);
  }
  function performMaRedo() {
    if (!MA.redo.length) return;
    MA.undo.push(JSON.stringify(MA.graph));
    MA.graph = JSON.parse(MA.redo.pop());
    MA.sel = null;
    touch();
    MA.json ? G.render() : draw(true);
  }
  function duplicateSelectedMaNode() {
    if (MA.sel?.type !== "node") return;
    const orig = node(MA.sel.id);
    if (!orig) return;
    snapshotMaUndo();
    const copy = JSON.parse(JSON.stringify(orig));
    const base = orig.id.replace(/_\d+$/, "");
    let i = 2;
    while (node(`${base}_${i}`)) i++;
    copy.id = `${base}_${i}`;
    copy.label = `${orig.label || orig.id} (복사)`;
    copy.x = (orig.x || 80) + 30;
    copy.y = (orig.y || 80) + 30;
    MA.graph.nodes.push(copy);
    MA.sel = { type: "node", id: copy.id };
    touch();
    draw(true);
  }
  const nodeState = id => MA.run?.node_state?.[id] || null;

  // ------------------------------------------------------------ attachments (multimodal inputs)
  const fmtSize = n => n < 1024 ? `${n}B` : n < 1048576 ? `${(n / 1024).toFixed(1)}KB` : `${(n / 1048576).toFixed(1)}MB`;
  const modalityOf = (mime, name) => {
    mime = (mime || "").toLowerCase(); name = (name || "").toLowerCase();
    if (mime === "application/pdf" || name.endsWith(".pdf")) return "pdf";
    for (const k of ["image", "video", "audio"]) if (mime.startsWith(k + "/")) return k;
    if (mime.startsWith("text/") || /json|xml|yaml|javascript|sql|toml/.test(mime) || /\.(txt|md|csv|tsv|json|jsonl|xml|ya?ml|toml|log|py|js|ts|tsx|jsx|java|kt|go|rs|c|h|cpp|cs|rb|php|sh|sql|html|css|tex|ipynb|srt|vtt)$/.test(name)) return "text";
    return "file";
  };
  const THUMBS = MA.thumbs = MA.thumbs || {};
  function thumb(meta) {
    if (meta.modality !== "image" || !meta.id) return null;
    if (THUMBS[meta.id]) return THUMBS[meta.id] === "pending" ? null : THUMBS[meta.id];
    THUMBS[meta.id] = "pending";
    fetch(`${G.getBASE()}${WSP()}/team-attachments/${meta.id}/raw`, { headers: G.authHeaders() })
      .then(r => r.ok ? r.blob() : Promise.reject()).then(b => { THUMBS[meta.id] = URL.createObjectURL(b); document.querySelectorAll(`[data-thumb="${meta.id}"]`).forEach(x => x.innerHTML = `<img src="${THUMBS[meta.id]}" alt="">`); })
      .catch(() => { THUMBS[meta.id] = ""; });
    return null;
  }
  function chip(meta, removeAttr) {
    const t = meta.preview || thumb(meta);
    const prog = meta.progress != null && meta.progress < 1 ? `<div class="bar" style="width:${Math.round(meta.progress * 100)}%"></div>` : "";
    return `<span class="ma-achip" title="${esc(meta.name)} · ${esc(meta.mime || "")}">
      <span class="th" data-thumb="${esc(meta.id || "")}">${t ? `<img src="${esc(t)}" alt="">` : MOD_ICON[meta.modality] || "📦"}</span>
      <span style="min-width:0"><div class="nm">${esc(meta.name)}</div><div class="sz">${esc(meta.error ? "업로드 실패: " + meta.error : meta.progress != null && meta.progress < 1 ? `업로드 ${Math.round(meta.progress * 100)}%` : fmtSize(meta.size || 0))}</div>${prog}</span>
      ${removeAttr ? `<button ${removeAttr} title="제거">×</button>` : ""}</span>`;
  }
  const readB64 = blob => new Promise((ok, bad) => { const r = new FileReader(); r.onload = () => ok(String(r.result).split(",", 2)[1] || ""); r.onerror = () => bad(r.error); r.readAsDataURL(blob); });

  // Chunked upload (every request stays under the gateway body limit); returns the READY attachment meta.
  async function uploadFile(file, onProgress) {
    if (file.size > MA.limits.max_attachment_bytes) throw new Error(`최대 ${fmtSize(MA.limits.max_attachment_bytes)}`);
    const init = await api("POST", `${WSP()}/team-attachments`, { name: file.name, mime: file.type || "application/octet-stream", size: file.size }, { noInvalidate: true });
    const CH = init.chunk_size;
    for (let idx = 0; idx < init.chunks; idx++) {
      const data = await readB64(file.slice(idx * CH, Math.min(file.size, (idx + 1) * CH)));
      await api("POST", `${WSP()}/team-attachments/${init.id}/chunks/${idx}`, { data }, { noInvalidate: true });
      onProgress?.(Math.min(0.99, (idx + 1) / init.chunks));
    }
    const meta = await api("POST", `${WSP()}/team-attachments/${init.id}/complete`, {}, { noInvalidate: true });
    MA.attLib[meta.id] = meta;
    return meta;
  }

  // Upload many files into a list of ids; pending entries are shown with progress until they are READY.
  async function addFiles(files, target, rerender) {
    const jobs = [...files].map(f => {
      const pend = { tmp: Math.random().toString(36).slice(2), name: f.name, mime: f.type, size: f.size, modality: modalityOf(f.type, f.name), progress: 0,
        preview: f.type.startsWith("image/") && f.size < 20e6 ? URL.createObjectURL(f) : null, target };
      MA.uploads.push(pend); rerender();
      return uploadFile(f, p => { pend.progress = p; rerender(); })
        .then(meta => { MA.uploads = MA.uploads.filter(x => x !== pend); target.add(meta.id); if (pend.preview) THUMBS[meta.id] = pend.preview; rerender(); })
        .catch(e => { pend.error = e.message; rerender(); setTimeout(() => { MA.uploads = MA.uploads.filter(x => x !== pend); rerender(); }, 6000); });
    });
    await Promise.allSettled(jobs);
  }

  // A reusable attachment field: chips + "add files" + pick from the workspace library.
  function attField(key, ids, hint) {
    const pending = MA.uploads.filter(u => u.target?.key === key);
    const lib = Object.values(MA.attLib).filter(m => !ids.includes(m.id)).sort((a, b) => String(b.created_at).localeCompare(String(a.created_at))).slice(0, 80);
    return `<div class="fld"><span>첨부 파일 ${ids.length ? `(${ids.length})` : ""}</span>
      <div class="ma-att">${ids.map(id => chip(MA.attLib[id] || { id, name: id.slice(0, 8), modality: "file" }, `data-arm="${key}" data-id="${esc(id)}"`)).join("")}${pending.map(u => chip(u)).join("")}</div>
      <div class="ma-drop" data-drop="${key}">파일을 끌어다 놓거나 클릭해서 선택 — 이미지·동영상·오디오·PDF·문서 (최대 ${fmtSize(MA.limits.max_attachment_bytes)})<input type="file" multiple hidden data-file="${key}"></div>
      ${lib.length ? `<select data-lib="${key}"><option value="">보관함에서 추가…</option>${lib.map(m => `<option value="${esc(m.id)}">${MOD_ICON[m.modality] || ""} ${esc(m.name)} · ${fmtSize(m.size)}</option>`).join("")}</select>` : ""}
      ${hint ? `<div class="ma-hint">${hint}</div>` : ""}</div>`;
  }
  function bindAttField(key, getIds, setIds, rerender) {
    const target = { key, add: id => { const ids = getIds(); if (!ids.includes(id)) setIds([...ids, id]); } };
    const drop = document.querySelector(`[data-drop="${key}"]`), input = document.querySelector(`[data-file="${key}"]`);
    if (drop && input) {
      drop.onclick = () => input.click();
      input.onchange = () => { if (input.files.length) addFiles(input.files, target, rerender); };
      drop.ondragover = e => { e.preventDefault(); drop.classList.add("over"); };
      drop.ondragleave = () => drop.classList.remove("over");
      drop.ondrop = e => { e.preventDefault(); drop.classList.remove("over"); if (e.dataTransfer.files.length) addFiles(e.dataTransfer.files, target, rerender); };
    }
    document.querySelectorAll(`[data-arm="${key}"]`).forEach(b => b.onclick = () => { setIds(getIds().filter(x => x !== b.dataset.id)); rerender(); });
    const lib = document.querySelector(`[data-lib="${key}"]`);
    if (lib) lib.onchange = () => { if (lib.value) { target.add(lib.value); rerender(); } };
    return target;
  }

  function newId(kind) {
    let i = 1; const base = kind === "agent" ? "agent" : kind;
    while (node(`${base}_${i}`)) i++;
    return `${base}_${i}`;
  }

  function addNode(type, x, y) {
    snapshotMaUndo();
    const kind = type === "mock" ? "agent" : type;
    const id = newId(type);
    const n = { id, kind, label: { input: "요청", data: "데이터", agent: `에이전트 ${id.split("_")[1]}`, output: "결과" }[kind], x: x ?? 80, y: y ?? 80 };
    if (kind === "agent") Object.assign(n, { role: "", system_prompt: "", prompt_template: "", accepts: ACCEPTS.map(a => a[0]), modalities: MODALITIES.map(m => m[0]), input_mode: "all", mode: "think", cli: "", model: "", effort: "", max_runs: 3, can_message: "*", blackboard: false, output_key: "", guardrail: false });
    if (type === "mock") Object.assign(n, { mode: "mock", label: `모의 에이전트 ${id.split("_")[1]}` });
    if (kind === "data") Object.assign(n, { source: "files", params: { attachments: [], note: "" } });
    if (kind === "input") Object.assign(n, { default_request: "", attachments: [] });
    MA.graph.nodes.push(n); MA.sel = { type: "node", id }; touch(); draw(true);
  }

  function addEdge(from, to) {
    const a = node(from), b = node(to);
    if (!a || !b || from === to) return;
    if (["input", "data"].includes(b.kind)) return toast(`${KIND[b.kind].name} 노드는 입력을 받을 수 없습니다`, true);
    if (a.kind === "output") return toast("출력 노드에서는 연결할 수 없습니다", true);
    const e = { id: `e${Date.now().toString(36)}`, from, to, when: "always", label: "" };
    if (e.when === "always" && a.kind === "agent" && b.kind === "agent" && MA.graph.edges.some(x => x.to === to && x.when === "spawn")) e.when = "spawn";  // workers only take spawn edges
    if (MA.graph.edges.some(x => edgeKey(x) === edgeKey(e))) return toast("이미 연결되어 있습니다", true);
    snapshotMaUndo();
    MA.graph.edges.push(e); MA.sel = { type: "edge", id: e.id }; touch(); draw(true);
  }

  function removeSelected() {
    if (!MA.sel) return;
    snapshotMaUndo();
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
    snapshotMaUndo();
    node(oldId).id = nextId;
    MA.graph.edges.forEach(e => { if (e.from === oldId) e.from = nextId; if (e.to === oldId) e.to = nextId; });
    MA.graph.nodes.forEach(n => { if (Array.isArray(n.can_message)) n.can_message = n.can_message.map(x => x === oldId ? nextId : x); });
    MA.sel = { type: "node", id: nextId }; touch(); return true;
  }

  // ------------------------------------------------------------ canvas drawing
  function nodeBody(n) {
    if (n.kind === "agent") {
      const mods = n.modalities || MODALITIES.map(m => m[0]);
      const media = mods.length === MODALITIES.length ? "모든 미디어" : mods.length ? mods.map(m => MOD_ICON[m]).join("") : "미디어 없음";
      const worker = MA.graph.edges.some(e => e.to === n.id && e.when === "spawn"), orch = MA.graph.edges.some(e => e.from === n.id && e.when === "spawn");
      const execution = n.mode === "mock" ? ["모의 응답"] : [n.mode === "code" ? "💻 code" : "💭 think", n.cli || "기본 CLI", n.model];
      const chips = [...execution, media, n.input_mode === "any" ? "any" : null,
        n.guardrail ? "🛡 가드레일" : null, n.output_key ? `state.${n.output_key}` : null, orch ? "오케스트레이터" : null, worker ? "워커" : null].filter(Boolean);
      return `${esc(n.role || "역할 미지정")}<div class="chips">${chips.map(c => `<span class="chip">${esc(c)}</span>`).join("")}</div>`;
    }
    const attCount = n.kind === "input" ? (n.attachments || []).length : n.kind === "data" && n.source === "files" ? (n.params?.attachments || []).length : 0;
    if (attCount) {
      const ids = n.kind === "input" ? n.attachments : n.params.attachments;
      const icons = [...new Set(ids.map(id => MOD_ICON[MA.attLib[id]?.modality] || "📦"))].join("");
      const head = n.kind === "input" ? esc(n.default_request ? n.default_request.slice(0, 60) : "실행 시 요청 + 첨부") : esc((n.params?.note || "첨부 파일").slice(0, 60));
      return `${head}<div class="chips"><span class="chip">📎 ${attCount} ${icons}</span></div>`;
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
    const W = Math.max(2400, ...MA.graph.nodes.map(n => n.x + NODE_W + 600));
    const H = Math.max(1400, ...MA.graph.nodes.map(n => n.y + NODE_H + 500));
    const edges = MA.graph.edges.map(e => {
      const a = node(e.from), b = node(e.to); if (!a || !b) return "";
      const p = pathFor(a, b), w = e.when || "always", on = MA.sel?.type === "edge" && MA.sel.id === e.id;
      const color = WHEN[w][1], dash = w === "revise" ? ` stroke-dasharray="7 5"` : w === "spawn" ? ` stroke-dasharray="2 5"` : w === "handoff" ? ` stroke-dasharray="10 4"` : "";
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
    const markers = Object.entries({ always: "var(--muted)", approve: "var(--ok)", revise: "var(--warn)", handoff: "#0ea5e9", spawn: "#db2777", msg: "#a855f7" })
      .map(([k, c]) => `<marker id="ma-arr-${k}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="${c}"/></marker>`).join("");
    const nodes = MA.graph.nodes.map(n => {
      const style = KIND[n.kind === "agent" && n.mode === "mock" ? "mock" : n.kind];
      const s = nodeState(n.id), on = MA.sel?.type === "node" && MA.sel.id === n.id;
      const stTxt = s ? `${STATUS[s.status] || s.status}${s.runs > 1 ? ` ×${s.runs}` : ""}${s.verdict && s.verdict !== "none" ? ` · ${s.verdict}` : ""}` : "";
      return `<div class="ma-node ${on ? "on" : ""} ${s ? "s-" + esc(s.status) : ""}" data-node="${esc(n.id)}" style="left:${n.x}px;top:${n.y}px">
        <div class="hd" style="background:${style.color}"><span>${style.icon}</span><span>${esc(n.label)}</span><span class="id">${esc(n.id)}</span></div>
        <div class="bd">${nodeBody(n)}</div>
        ${n.kind !== "input" && n.kind !== "data" ? `<div class="ma-port in" data-in="${esc(n.id)}"></div>` : ""}
        ${n.kind !== "output" ? `<div class="ma-port out" data-out="${esc(n.id)}" title="드래그해서 다른 노드에 연결"></div>` : ""}
        ${stTxt ? `<span class="ma-st">${esc(stTxt)}</span>` : ""}
      </div>`;
    }).join("");
    if (MA.fitPending && stage.clientWidth) {  // first draw of a graph: zoom so the whole team is visible
      const maxX = Math.max(...MA.graph.nodes.map(n => n.x + NODE_W)) + 60, maxY = Math.max(...MA.graph.nodes.map(n => n.y + NODE_H)) + 140;
      MA.zoom = Math.max(0.4, Math.min(1.2, (stage.clientWidth - 20) / maxX, (stage.clientHeight - 20) / maxY));
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
        const p = pt(e); drag = { mode: "move", n, el, dx: p.x - n.x, dy: p.y - n.y, moved: false, before: JSON.stringify(MA.graph) }; MA.dragging = true;
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
      } else { if (d.moved) { if (d.before) { MA.undo.push(d.before); if (MA.undo.length > 60) MA.undo.shift(); MA.redo = []; } touch(); } draw(); }
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
      const toAgent = node(e.to)?.kind === "agent";
      const whenOpts = Object.entries(WHEN).filter(([k]) => !AGENT_ONLY_WHEN.includes(k) || toAgent);
      el.innerHTML = `<div class="ide-head">연결 · ${esc(e.from)} → ${esc(e.to)}</div><div class="g-form">
        <label class="fld"><span>전달 조건</span><select id="ma-e-when" ${fromAgent ? "" : "disabled"}>${whenOpts.map(([k, [l]]) => opt(k, e.when, l)).join("")}</select></label>
        <div class="ma-hint">• <b>항상</b>: 완료될 때마다 결과 전달<br>• <b>승인 시</b>: 에이전트가 approve(또는 판정 없음)일 때<br>• <b>수정 요청 시</b>: revise일 때 — 대상은 결과를 <i>리뷰 피드백</i>으로 받아 다시 작업 (평가자-최적화 / LoopAgent)<br>
          • <b>핸드오프</b>: 에이전트가 <code>handoff</code>로 이 대상을 고를 때만 — 대화(첨부 포함)를 넘기고 다른 연결은 발동하지 않음 (OpenAI handoffs · ADK transfer)<br>
          • <b>하위 작업 배정</b>: 오케스트레이터가 <code>tasks</code>로 이 워커에게 작업을 여러 개 배정 → 병렬 실행 후 결과가 오케스트레이터에게 돌아옴 (Anthropic orchestrator-workers · agents-as-tools)</div>
        <label class="fld"><span>레이블</span><input id="ma-e-label" value="${esc(e.label || "")}" placeholder="예: 수정 요청"></label>
        <button class="g bad" id="ma-e-del">연결 삭제</button></div>`;
      $("#ma-e-when").onchange = ev => { snapshotMaUndo(); e.when = ev.target.value; touch(); draw(); };
      $("#ma-e-label").onchange = ev => { snapshotMaUndo(); e.label = ev.target.value; touch(); draw(); };
      $("#ma-e-del").onclick = removeSelected;
      return;
    }
    if (sel?.type === "node") {
      const n = node(sel.id); if (!n) { MA.sel = null; return drawInspector(); }
      const style = KIND[n.kind === "agent" && n.mode === "mock" ? "mock" : n.kind];
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
          <div class="ma-hint">비우면 <code>{{auto}}</code> — 받은 입력을 종류별 섹션으로 자동 구성. 사용 가능: <code>{{request}}</code> <code>{{attachments}}</code> <code>{{data}}</code> <code>{{agents}}</code> <code>{{subtasks}}</code> <code>{{feedback}}</code> <code>{{messages}}</code> <code>{{blackboard}}</code> <code>{{state}}</code> <code>{{state:키}}</code> <code>{{input:노드id}}</code></div>
          <div class="fld"><span>받아들일 입력 종류</span><div class="ma-checks">${ACCEPTS.map(([k, l]) => `<label><input type="checkbox" data-acc="${k}" ${n.accepts?.includes(k) ? "checked" : ""}> ${l}</label>`).join("")}</div></div>
          <div class="fld"><span>받아들일 미디어 (첨부 파일 모달리티)</span><div class="ma-checks">${MODALITIES.map(([k, l, ic]) => `<label><input type="checkbox" data-mod="${k}" ${(n.modalities || MODALITIES.map(m => m[0])).includes(k) ? "checked" : ""}> ${ic} ${l}</label>`).join("")}</div>
            <div class="ma-hint">선택하지 않은 형식의 첨부는 이 에이전트에게 전달되지 않습니다. VM에서 PDF는 텍스트·페이지 이미지로, 동영상은 키프레임·음성으로 변환되고, Claude(Read)·Codex(--image)·Gemini(@파일)에 맞게 전달됩니다.</div></div>
          <div class="row">
            <label class="fld" style="flex:1"><span>실행 시점</span><select id="ma-a-imode">${opt("all", n.input_mode, "모든 상위 입력 대기")}${opt("any", n.input_mode, "입력 올 때마다")}</select></label>
            <label class="fld" style="flex:1"><span>모드</span><select id="ma-a-mode">${opt("think", n.mode, "💭 think (답변)")}${opt("code", n.mode, "💻 code (저장소 작업)")}${opt("mock", n.mode, "◇ 모의 응답")}</select></label>
          </div>
          ${n.mode === "mock" ? `
          <label class="fld"><span>실행 순서별 모의 응답 (JSON 배열)</span><textarea id="ma-a-mock" rows="5" class="mono">${esc(typeof n.mock_responses === "string" ? n.mock_responses : JSON.stringify(n.mock_responses ?? [{ content: `${n.label} 모의 응답` }], null, 2))}</textarea></label>
          <div class="ma-hint">응답 객체에 <code>content</code>를 입력하세요. <code>verdict</code>, <code>messages</code>, <code>handoff</code>, <code>tasks</code>, <code>tripwire</code>도 설정할 수 있습니다. 배열을 모두 사용하면 마지막 응답을 반복합니다.</div>` : ""}
          ${n.mode !== "mock" ? `<div class="row">
            <label class="fld" style="flex:1"><span>CLI</span><select id="ma-a-cli">${opt("", n.cli, "워크스페이스 기본")}${["claude", "codex", "agy"].map(c => opt(c, n.cli)).join("")}</select></label>
            <label class="fld" style="flex:1"><span>노력</span><select id="ma-a-eff">${opt("", n.effort, "기본")}${["low", "medium", "high", "xhigh", "max"].map(c => opt(c, n.effort)).join("")}</select></label>
          </div>` : ""}
          <div class="row">
            ${n.mode !== "mock" ? `<label class="fld" style="flex:2"><span>모델 (비우면 기본)</span><input id="ma-a-model" class="mono" value="${esc(n.model || "")}" placeholder="opus / gpt-5.5 …"></label>` : ""}
            <label class="fld" style="flex:1"><span>최대 실행</span><input id="ma-a-max" type="number" min="1" max="10" value="${n.max_runs ?? 3}"></label>
          </div>
          <div class="fld"><span>직접 메시지를 보낼 수 있는 동료</span><div class="ma-checks">
            <label><input type="checkbox" id="ma-a-cmall" ${all ? "checked" : ""}> 모두</label>
            ${others.map(o => `<label><input type="checkbox" data-cm="${esc(o.id)}" ${all || n.can_message.includes(o.id) ? "checked" : ""} ${all ? "disabled" : ""}> ${esc(o.label)}</label>`).join("") || `<span class="muted">다른 에이전트 없음</span>`}
          </div></div>
          <label class="row" style="font-size:12.5px"><input type="checkbox" id="ma-a-bb" ${n.blackboard ? "checked" : ""}> <span>팀 공유 보드·공유 상태(모든 에이전트의 최근 결과) 참조</span></label>
          <label class="fld"><span>결과를 공유 상태에 저장 (output_key)</span><input id="ma-a-okey" class="mono" value="${esc(n.output_key || "")}" placeholder="예: draft"></label>
          <div class="ma-hint">Google ADK식 세션 상태: 이 에이전트의 결과가 <code>state.키</code>로 저장되고, 다른 에이전트의 프롬프트·템플릿에서 <code>{{state:키}}</code>로 읽습니다.</div>
          <label class="row" style="font-size:12.5px"><input type="checkbox" id="ma-a-guard" ${n.guardrail ? "checked" : ""}> <span>🛡 가드레일 — 정책 위반 시 <code>tripwire</code>로 팀 실행 중단 (BLOCKED)</span></label>`;
      } else if (n.kind === "data") {
        const p = n.params || {};
        const fields = {
          spec_scope: `<label class="fld"><span>도메인 (비우면 전체)</span><input data-p="domain" value="${esc(p.domain || "")}"></label>`,
          ux_layout: `<label class="fld"><span>UX 뷰</span><input data-p="view_key" class="mono" value="${esc(p.view_key || "home")}"></label>`,
          repo_files: `<label class="fld"><span>파일 경로 (쉼표/줄바꿈, 최대 8개)</span><textarea data-p="paths" class="mono" rows="3">${esc((p.paths || []).join("\n"))}</textarea></label>
            <label class="fld"><span>저장소 (비우면 팀 기본)</span><input data-p="repo_key" class="mono" value="${esc(p.repo_key || "")}"></label>`,
          cells: `<div class="ma-hint">VM의 노트북 셀 전체를 JSON으로 전달합니다.</div>`,
          files: `${attField(`data:${n.id}`, p.attachments || [], "팀에 저장되어 실행할 때마다 연결된 에이전트에게 전달됩니다 (각 에이전트의 미디어 설정으로 걸러짐).")}
            <label class="fld"><span>설명 (에이전트에게 함께 전달)</span><textarea data-p="note" rows="2">${esc(p.note || "")}</textarea></label>`,
          collab_history: `<label class="fld"><span>최근 메시지 수</span><input data-p="limit" type="number" min="1" max="100" value="${p.limit ?? 20}"></label>`,
          text: `<label class="fld"><span>텍스트</span><textarea data-p="text" rows="6">${esc(p.text || "")}</textarea></label>`,
        };
        body = `<label class="fld"><span>데이터 소스</span><select id="ma-d-src">${SOURCES.map(([k, l]) => opt(k, n.source, l)).join("")}</select></label>${fields[n.source] || ""}
          <div class="ma-hint">실행 시작 시 한 번 조회되어 연결된 에이전트에 <i>데이터</i> 입력으로 전달됩니다.</div>`;
      } else if (n.kind === "input") {
        body = `<label class="fld"><span>기본 요청 (실행 시 비워두면 사용)</span><textarea id="ma-i-def" rows="4">${esc(n.default_request || "")}</textarea></label>
          ${attField(`input:${n.id}`, n.attachments || [], "항상 요청과 함께 전달되는 기본 첨부입니다. 실행할 때마다 다른 파일은 아래 '팀 실행'에서 첨부하세요.")}`;
      } else {
        const outs = MA.run?.result_json?.outputs?.[n.id] || [];
        body = outs.length ? `<div class="fld"><span>최근 결과</span><pre class="mono" style="white-space:pre-wrap;max-height:320px;overflow:auto">${esc(outs[outs.length - 1])}</pre></div>` : `<div class="ma-hint">연결된 에이전트의 결과가 여기에 모입니다.</div>`;
      }
      el.innerHTML = `<div class="ide-head">${style.icon} ${style.name} 노드</div><div class="g-form">${common}${body}<div class="row"><button class="g" id="ma-n-dup">복제</button><button class="g bad" id="ma-n-del">노드 삭제</button></div></div>`;
      $("#ma-n-id").onchange = ev => { if (!renameNode(n.id, ev.target.value.trim())) ev.target.value = n.id; else draw(true); };
      $("#ma-n-label").oninput = ev => { snapshotMaUndoOnce(`lbl:${n.id}`); n.label = ev.target.value; touch(); const h = document.querySelector(`[data-node="${CSS.escape(n.id)}"] .hd span:nth-child(2)`); if (h) h.textContent = n.label; };
      $("#ma-n-dup").onclick = duplicateSelectedMaNode;
      $("#ma-n-del").onclick = removeSelected;
      const bind = (id, fn, evt = "onchange") => { const x = $(id); if (x) x[evt] = ev => { if (evt === "oninput") snapshotMaUndoOnce(`${id}:${n.id}`); else snapshotMaUndo(); fn(ev.target); touch(); }; };
      if (n.kind === "agent") {
        bind("#ma-a-role", t => { n.role = t.value; draw(); });
        bind("#ma-a-sys", t => { n.system_prompt = t.value; }, "oninput");
        bind("#ma-a-tpl", t => { n.prompt_template = t.value; }, "oninput");
        bind("#ma-a-imode", t => { n.input_mode = t.value; draw(); });
        bind("#ma-a-mode", t => { n.mode = t.value; draw(true); });
        bind("#ma-a-mock", t => {
          try {
            const value = JSON.parse(t.value);
            n.mock_responses = Array.isArray(value) ? value : t.value;
          } catch {
            n.mock_responses = t.value;
          }
        }, "oninput");
        bind("#ma-a-cli", t => { n.cli = t.value; draw(); });
        bind("#ma-a-eff", t => { n.effort = t.value; });
        bind("#ma-a-model", t => { n.model = t.value.trim(); draw(); });
        bind("#ma-a-max", t => { n.max_runs = Math.max(1, Math.min(10, parseInt(t.value, 10) || 3)); });
        bind("#ma-a-bb", t => { n.blackboard = t.checked; });
        bind("#ma-a-okey", t => { n.output_key = t.value.trim(); draw(); });
        bind("#ma-a-guard", t => { n.guardrail = t.checked; draw(); });
        document.querySelectorAll("[data-mod]").forEach(c => c.onchange = () => { snapshotMaUndo(); n.modalities = MODALITIES.map(m => m[0]).filter(k => document.querySelector(`[data-mod="${k}"]`).checked); touch(); draw(); });
        document.querySelectorAll("[data-acc]").forEach(c => c.onchange = () => { snapshotMaUndo(); n.accepts = ACCEPTS.map(a => a[0]).filter(k => document.querySelector(`[data-acc="${k}"]`).checked); touch(); });
        bind("#ma-a-cmall", t => { n.can_message = t.checked ? "*" : MA.graph.nodes.filter(x => x.kind === "agent" && x.id !== n.id).map(x => x.id); drawInspector(); });
        document.querySelectorAll("[data-cm]").forEach(c => c.onchange = () => { snapshotMaUndo(); n.can_message = [...document.querySelectorAll("[data-cm]")].filter(x => x.checked).map(x => x.dataset.cm); touch(); });
      } else if (n.kind === "data") {
        bind("#ma-d-src", t => { n.source = t.value; n.params = {}; draw(true); });
        document.querySelectorAll("[data-p]").forEach(f => f.onchange = () => {
          snapshotMaUndo();
          n.params = n.params || {};
          const k = f.dataset.p;
          n.params[k] = k === "paths" ? f.value.split(/[\r\n,]+/).map(s => s.trim()).filter(Boolean) : k === "limit" ? parseInt(f.value, 10) || 20 : f.value;
          touch(); draw();
        });
        if (n.source === "files") bindAttField(`data:${n.id}`, () => n.params.attachments || [], ids => { snapshotMaUndo(); n.params.attachments = ids; touch(); }, () => { if (MA.sel?.id === n.id) drawInspector(); draw(); });
      } else if (n.kind === "input") {
        bind("#ma-i-def", t => { n.default_request = t.value; draw(); });
        bindAttField(`input:${n.id}`, () => n.attachments || [], ids => { snapshotMaUndo(); n.attachments = ids; touch(); }, () => { if (MA.sel?.id === n.id) drawInspector(); draw(); });
      }
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
        • 툴바 버튼 또는 빈 곳 <b>더블클릭</b>: 노드 추가 · <b>⛶</b>: 전체 화면 · 캔버스 아래 막대를 끌어 높이 조절<br>
        • 노드 오른쪽 <b>●</b>을 끌어 다른 노드에 놓으면 연결<br>
        • 연결을 클릭해 조건 변경 — <b>수정 요청 시</b>(리뷰 루프), <b>핸드오프</b>(대화 넘기기), <b>하위 작업 배정</b>(오케스트레이터→워커 병렬)<br>
        • 에이전트는 실행 중 동료에게 <b>직접 메시지</b>를 보내 질문·답변하며 협업합니다 (보라색 점선)<br>
        • <b>output_key</b>로 공유 상태에 결과를 남기고 <code>{{state:키}}</code>로 읽기 · <b>가드레일</b> 에이전트는 tripwire로 실행 중단<br>
        • <kbd>Ctrl+Z</kbd>/<kbd>Ctrl+Shift+Z</kbd>: 실행 취소/다시 · <kbd>Ctrl+D</kbd>: 복제 · <kbd>Ctrl+S</kbd>: 저장 · <kbd>Delete</kbd>: 삭제
      </div></div>`;
    $("#ma-s-steps").onchange = e => { snapshotMaUndo(); st.max_steps = Math.max(1, Math.min(100, parseInt(e.target.value, 10) || 24)); touch(); };
    $("#ma-s-par").onchange = e => { snapshotMaUndo(); st.parallelism = Math.max(1, Math.min(6, parseInt(e.target.value, 10) || 3)); touch(); };
    $("#ma-s-repo").onchange = e => { snapshotMaUndo(); st.repo_key = e.target.value.trim(); touch(); };
  }

  // ------------------------------------------------------------ run panel
  const EV = {
    run_started: ["▶", "실행 시작"], data_resolved: ["▤", "데이터 조회"], data_failed: ["⚠", "데이터 조회 실패"],
    node_started: ["⏳", "작업 시작"], node_finished: ["✅", "완료"], node_failed: ["❌", "실패"], message: ["✉", "메시지"],
    message_rejected: ["⚠", "메시지 거부"], output: ["⚑", "결과 도착"], dropped: ["⚠", "입력 무시"], node_skipped: ["⏭", "건너뜀"],
    run_finished: ["■", "실행 종료"], delivery: ["→", "전달"], handoff: ["⇢", "핸드오프"], handoff_rejected: ["⚠", "핸드오프 거부"],
    task_assigned: ["⑂", "하위 작업 배정"], task_rejected: ["⚠", "하위 작업 거부"], subtasks_returned: ["⑃", "하위 작업 결과 회수"],
    state_set: ["◆", "공유 상태 저장"], guardrail_tripped: ["🛡", "가드레일 작동 — 실행 중단"],
    media_preparing: ["📎", "첨부 준비 중"], media_ready: ["📎", "첨부 준비 완료"], media_failed: ["⚠", "첨부 처리 실패"],
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
      const who = ["message", "handoff", "task_assigned"].includes(e.type) ? `<b>${esc(label(e.node))}</b> → <b>${esc(label(e.target))}</b>`
        : e.type === "output" ? `<b>${esc(label(e.node))}</b> → ⚑ ${esc(label(e.target))}`
        : e.node ? `<b>${esc(label(e.node))}</b>` : "";
      const extra = [m.verdict && m.verdict !== "none" ? `<span class="chip">${esc(m.verdict)}</span>` : "", m.run_no > 1 ? `<span class="chip">#${m.run_no}</span>` : "",
        m.ms ? `<span class="muted">${(m.ms / 1000).toFixed(1)}s</span>` : "", m.status ? `<span class="chip ${G.statusCls(m.status)}">${esc(m.status)}</span>` : "",
        m.changed_files?.length ? `<span class="chip">파일 ${m.changed_files.length}</span>` : "",
        m.attachments ? `<span class="chip">📎 ${m.attachments}</span>` : "", m.subtask ? `<span class="chip">워커 #${(m.index ?? 0) + 1}</span>` : "",
        m.key ? `<span class="chip mono">state.${esc(m.key)}</span>` : "", m.tasks ? `<span class="chip">작업 ${m.tasks}개 배정</span>` : "",
        m.modalities?.length ? `<span class="chip">${m.modalities.map(x => MOD_ICON[x] || x).join("")}</span>` : ""].join(" ");
      const cls = ["message", "handoff", "task_assigned"].includes(e.type) ? "msg" : ["node_failed", "data_failed", "message_rejected", "guardrail_tripped", "media_failed", "task_rejected", "handoff_rejected"].includes(e.type) ? "bad" : e.type === "run_finished" ? "fin" : "";
      const head = `${ic} ${name} ${who} ${extra}`;
      return `<div class="ma-ev ${cls}">${e.content ? `<details ${e.type === "message" ? "open" : ""}><summary>${head}</summary><pre>${esc(e.content)}</pre></details>` : head}</div>`;
    }).join("") || `<div class="muted">이벤트 대기 중…</div>`;
  }

  function drawRunHead() {
    const el = $("#ma-runhead"); if (!el) return;
    const r = MA.run;
    el.innerHTML = r ? `<span class="chip ${G.statusCls(r.status === "PARTIAL" ? "DEFER" : r.status === "BLOCKED" ? "FAILED" : r.status)}">${esc(r.status)}</span> <span class="muted mono">${esc(String(r.team_run_id).slice(0, 8))}</span>
      ${r.result_json?.blocked ? `<div class="bad" style="font-size:12.5px;margin-top:4px">🛡 ${esc(label(r.result_json.blocked.node))}: ${esc(r.result_json.blocked.reason)}</div>` : ""}
      ${r.result_json?.state && Object.keys(r.result_json.state).length ? `<details style="margin-top:6px;font-size:12.5px"><summary>공유 상태 (${Object.keys(r.result_json.state).length})</summary>${Object.entries(r.result_json.state).map(([k, v]) => `<div style="margin-top:4px"><code>${esc(k)}</code><pre class="mono" style="white-space:pre-wrap;max-height:160px;overflow:auto;margin:2px 0">${esc(String(v).slice(0, 4000))}</pre></div>`).join("")}</details>` : ""}
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
      if (e.type === "task_assigned") MA.flashes.push({ kind: "edge", from: e.node, to: e.target, t: now });
    }
    return fresh.length;
  }

  // ------------------------------------------------------------ agent monitor (one read-only VM PTY per agent)
  // Each agent call runs in its own automation PTY `agdd-ma-<node>` (orchestrator workers: `-t<index>`) and the
  // CLI output is mirrored there, so the console can watch every agent live. A PTY is opened per call and closed
  // afterwards; while the run is RUNNING a pane keeps re-attaching to the next session of the same name.
  const termOf = (id, idx) => (`agdd-ma-${id}` + (idx == null ? "" : `-t${idx}`)).slice(0, 64);
  const isWorker = id => MA.graph.edges.some(e => e.to === id && e.when === "spawn");
  const runActive = () => MA.run?.status === "RUNNING";
  const ST_KO = { idle: "대기", pending: "대기", running: "작업 중", done: "완료", failed: "실패", skipped: "건너뜀", waiting: "입력 대기" };
  const ST_CLS = { running: "warn", done: "ok", failed: "bad", waiting: "warn" };
  function subtaskStates() {
    const m = {};
    for (const e of MA.run?.events || []) {
      const x = e.meta_json || {};
      if (!x.subtask || x.index == null || !e.node) continue;
      const status = e.type === "node_started" ? "running" : e.type === "node_finished" ? "done" : e.type === "node_failed" ? "failed" : "";
      if (status) m[termOf(e.node, x.index)] = { node: e.node, index: x.index, status };
    }
    return m;
  }
  const paneStatus = p => (p.index != null ? MA._subs?.[p.name]?.status : nodeState(p.node)?.status) || "idle";

  function buildMonitor() {
    if (MA.mon) { MA.mon.stop(); MA.mon = null; }
    const host = $("#ma-mon"); if (!host) return;
    if (!MA.vm) { host.innerHTML = `<div class="muted" style="font-size:12.5px">에이전트별 실시간 터미널은 워크스페이스 VM이 연결된 경우에만 사용할 수 있습니다 (CLI 에이전트가 VM PTY에서 실행됨).</div>`; return; }
    const agents = MA.graph.nodes.filter(n => n.kind === "agent").length;
    MA.mon = G.TermGrid(host, {
      storeKey: "agdd.ma.mon", layout: agents > 2 ? "4" : "2", closable: false, fontSize: 12,
      height: Math.max(360, Math.round(innerHeight * 0.55)),
      connect: () => runActive(), rearm: () => runActive(),
      idleText: () => "팀이 실행되면 이 에이전트의 CLI 출력이 실시간으로 표시됩니다.",
      busy: p => paneStatus(p) === "running",
      meta: p => {
        const s = paneStatus(p), ns = p.index == null ? nodeState(p.node) : null, n = node(p.node);
        return `<span class="chip ${ST_CLS[s] || ""}" style="font-size:10.5px;padding:0 6px">${esc(ST_KO[s] || s)}</span>`
          + (ns?.runs ? `<span class="chip" style="font-size:10.5px;padding:0 6px">#${ns.runs}</span>` : "")
          + (n ? `<span class="chip" style="font-size:10.5px;padding:0 6px">${esc(n.mode || "think")}${n.cli ? " · " + esc(n.cli) : ""}</span>` : "");
      },
      onActive: p => { if (p?.node && node(p.node)) { MA.sel = { type: "node", id: p.node }; draw(true); } },
      empty: "팀에 에이전트 노드가 없습니다.",
    });
    syncMonitor();
  }

  function syncMonitor() {
    const mon = MA.mon; if (!mon || !MA.graph || G.getTAB() !== "team") return;
    MA._subs = subtaskStates();
    const want = MA.graph.nodes.filter(n => n.kind === "agent" && !isWorker(n.id))
      .map(n => ({ name: termOf(n.id), node: n.id, index: null, label: `🤖 ${n.label || n.id}` }));
    for (const [name, v] of Object.entries(MA._subs)) want.push({ name, node: v.node, index: v.index, label: `⑂ ${label(v.node)} · 워커 #${v.index + 1}` });
    const names = new Set(want.map(w => w.name));
    for (const p of [...mon.panes]) if (!names.has(p.name)) mon.remove(p.name);
    for (const w of want) { const p = mon.find(w.name); if (p) Object.assign(p, w); else mon.add({ ...w, ro: true, banner: false }, { focus: false }); }
    if (runActive()) for (const p of mon.panes) if (!p.term || p.state === "ended") mon.connect(p.name, { fresh: true });
    mon.refresh();
    const c = $("#ma-mon-count");
    if (c) { const busy = mon.panes.filter(p => paneStatus(p) === "running").length; c.textContent = `${mon.panes.length}개 세션${busy ? ` · ${busy}개 작업 중` : ""}`; }
  }

  async function poll() {
    clearTimeout(MA.poll);
    if (!MA.run || G.getTAB() !== "team") return;
    const after = MA.run.events.length ? Math.max(...MA.run.events.map(e => e.seq)) : 0;
    try {
      const data = await api("GET", `${WSP()}/team-runs/${MA.run.team_run_id}?after=${after}`, undefined, { fresh: true });
      if (G.getTAB() !== "team") return;
      ingest(data);
      draw(); drawFeed(); drawRunHead(); syncMonitor();
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
    if (MA.uploads.some(u => u.target?.key === "run" && !u.error)) throw new Error("첨부 파일 업로드가 끝난 뒤 실행하세요");
    const res = await api("POST", `${WSP()}/team-runs`, { team_id: MA.teamId || null, graph: MA.graph, request, attachments: MA.runAtt }, { noInvalidate: true });
    MA.run = { team_run_id: res.team_run_id, status: "RUNNING", events: [], node_state: {}, result_json: {}, active: true };
    toast("팀 실행을 시작했습니다");
    draw(); drawFeed(); drawRunHead(); refreshHistory(); syncMonitor(); poll();
  }

  // ------------------------------------------------------------ team persistence
  function loadGraph(g, teamId, name) {
    MA.graph = JSON.parse(JSON.stringify(g));
    MA.graph.settings = MA.graph.settings || { max_steps: 24, parallelism: 3 };
    MA.graph.edges.forEach((e, i) => { e.id = e.id || `e${i + 1}`; e.when = e.when || "always"; });
    MA.graph.nodes.forEach(n => {
      if (n.kind === "input") n.attachments = n.attachments || [];
      if (n.kind === "agent") Object.assign(n, { modalities: n.modalities || MODALITIES.map(m => m[0]), output_key: n.output_key || "", guardrail: !!n.guardrail, accepts: n.accepts || ACCEPTS.map(a => a[0]), input_mode: n.input_mode || "all", mode: n.mode || "think", cli: n.cli || "", model: n.model || "", effort: n.effort || "", max_runs: n.max_runs ?? 3, can_message: n.can_message ?? "*", blackboard: !!n.blackboard, role: n.role || "", system_prompt: n.system_prompt || "", prompt_template: n.prompt_template || "" });
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

  try { MA.h = MA.h || parseInt(localStorage.getItem("agdd.ma.h") || "0", 10) || 0; } catch {}

  G.VIEWS.team = async function teamView() {
    const res = await api("GET", `${WSP()}/teams`, undefined, { fresh: true });
    if (stale("team")) return;
    MA.teams = res.teams || []; MA.presets = res.presets || {}; MA.vm = !!res.vm_enabled; MA.presetInfo = res.preset_info || {};
    if (res.limits) MA.limits = res.limits;
    for (const m of res.attachments || []) MA.attLib[m.id] = m;
    if (MA.graph && MA.wsKey !== G.getWS()) {
      MA.graph = null; MA.run = null; MA.teamId = ""; MA.sel = null; MA.dirty = false;
      MA.runAtt = []; MA.attLib = {}; MA.uploads = []; MA.flashes = [];
      MA.undo = []; MA.redo = []; MA.lastRequest = "";
      clearTimeout(MA.poll);
    }
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
      ${MA.vm ? "" : `<div class="card" style="border-color:var(--warn)">VM 런타임이 연결되지 않아 think 모드 에이전트만 LLM 게이트웨이로 실행됩니다. code 모드·저장소/셀 데이터 소스와 동영상·오디오·PDF 첨부 처리는 VM이 필요합니다 (이미지·텍스트 첨부는 게이트웨이로 직접 전달).</div>`}
      <div class="g-toolbar">
        <span class="muted" style="font-size:12px">노드 추가</span>
        ${Object.entries(KIND).map(([k, v]) => `<button class="g-kind" data-add="${k}"><i style="background:${v.color}"></i>${v.name}</button>`).join("")}
        <span class="g-sep"></span>
        <select id="ma-preset" class="ma-presets" title="Anthropic · OpenAI · Google ADK에서 쓰는 멀티 에이전트 구조"><option value="">패턴 템플릿 불러오기…</option>${Object.entries(MA.presetInfo).map(([k, v]) => `<option value="${esc(k)}" title="${esc(v.desc)}">${esc(v.title)} — ${esc(v.vendor)}</option>`).join("")}</select>
        <span class="g-sep"></span>
        <button class="g" id="ma-undo" title="실행 취소 (Ctrl+Z)" ${MA.undo.length ? "" : "disabled"}>↶</button><button class="g" id="ma-redo" title="다시 실행 (Ctrl+Shift+Z)" ${MA.redo.length ? "" : "disabled"}>↷</button>
        <span class="g-sep"></span>
        <button class="g" id="ma-zout" title="축소">−</button><span class="mono muted" id="ma-zoomlbl" style="min-width:38px;text-align:center">100%</span><button class="g" id="ma-zin" title="확대">＋</button><button class="g" id="ma-fit">맞춤</button>
        <button class="g ${MA.full ? "on" : ""}" id="ma-full" title="전체 화면 (Esc로 닫기)">⛶</button>
        <span class="g-sep"></span>
        <button class="g" id="ma-validate">검증</button>
        <button class="g ${MA.json ? "on" : ""}" id="ma-json">{ } JSON</button>
      </div>
      <div class="g-wrap ma-wrap ${MA.full ? "ma-full" : ""}" id="ma-wrap" style="height:${Math.round(MA.h || Math.max(640, innerHeight - 170))}px;min-height:420px">
        ${MA.json ? `<div class="g-jsonbox"><textarea id="ma-raw" spellcheck="false">${esc(JSON.stringify(MA.graph, null, 2))}</textarea><div class="row" style="padding:8px"><button class="b" id="ma-apply-json">JSON 적용</button></div></div>`
          : `<div class="ma-stagebox"><div class="ma-fullbar"><button class="g" id="ma-full-x">⛶ 전체 화면 닫기</button><button class="b" id="ma-run2">▶ 팀 실행</button></div><div id="ma-stage" class="ma-stage"></div></div>`}
        <div id="ma-insp" class="g-insp"></div>
      </div>
      <div class="ma-resize" id="ma-resize" title="드래그해서 캔버스 높이 조절 (더블클릭: 기본값)"></div>
      <div class="ma-run">
        <div class="card" style="margin:0">
          <b>팀 실행</b>
          <textarea id="ma-request" rows="5" style="width:100%;box-sizing:border-box;margin-top:8px" placeholder="팀에게 맡길 요청 (비우면 입력 노드의 기본 요청) — 이미지는 붙여넣기(Ctrl+V)도 됩니다">${esc(MA.lastRequest || "")}</textarea>
          <div id="ma-runatt" style="margin-top:6px"></div>
          <div class="row" style="margin-top:8px"><button class="b" id="ma-run">▶ 팀 실행</button><select id="ma-history" style="flex:1;min-width:0"><option value="">실행 기록…</option></select></div>
          <div id="ma-runhead" style="margin-top:8px"></div>
        </div>
        <div class="card" style="margin:0"><b>협업 타임라인</b> <span class="muted" style="font-size:12px">에이전트 작업·메시지·결과가 실시간으로 표시됩니다</span><div id="ma-feed" class="ma-feed" style="margin-top:8px"></div></div>
      </div>
      <div class="card" style="margin:12px 0 0"><div class="row" style="margin-bottom:6px"><b>에이전트 모니터</b> <span class="muted" style="font-size:12px" id="ma-mon-count"></span>
        <span class="muted" style="font-size:12px">— 에이전트마다 VM 터미널(<span class="mono">agdd-ma-*</span>)의 CLI 출력을 읽기 전용으로 실시간 표시합니다. 패널을 누르면 캔버스에서 해당 노드가 선택됩니다.</span></div>
        <div id="ma-mon"></div></div>`);

    $("#ma-team").onchange = e => {
      if (MA.dirty && !confirm("저장하지 않은 변경 사항이 있습니다. 계속할까요?")) { e.target.value = MA.teamId; return; }
      const t = MA.teams.find(x => x.team_id === e.target.value);
      MA.undo = []; MA.redo = [];
      if (t) loadGraph(t.graph_json, t.team_id, t.name); else loadGraph(MA.presets.review_loop, "", "새 팀");
      MA.run = null; G.render();
    };
    $("#ma-name").oninput = () => { MA.name = $("#ma-name").value; touch(); };
    $("#ma-save").onclick = run(save);
    if ($("#ma-del")) $("#ma-del").onclick = run(async () => {
      if (!confirm(`팀 '${MA.name}'을(를) 삭제할까요?`)) return;
      await api("DELETE", `${WSP()}/teams/${MA.teamId}`); MA.graph = null; MA.teamId = ""; MA.run = null; MA.undo = []; MA.redo = []; G.render();
    });
    document.querySelectorAll("[data-add]").forEach(b => b.onclick = () => {
      const st = $("#ma-stage"); addNode(b.dataset.add, (st?.scrollLeft || 0) + 120 + Math.random() * 120 | 0, (st?.scrollTop || 0) + 80 + Math.random() * 120 | 0);
    });
    $("#ma-preset").onchange = e => {
      const key = e.target.value; if (!key) return;
      const info = MA.presetInfo[key] || {};
      if ((MA.dirty || MA.graph.nodes.length > 0) && !confirm(`'${info.title}' 템플릿으로 캔버스를 바꿀까요?\n${info.vendor}: ${info.desc}\n(저장된 팀은 저장 전까지 바뀌지 않습니다)`)) { e.target.value = ""; return; }
      snapshotMaUndo();
      const prevUndo = MA.undo, prevRedo = MA.redo;
      loadGraph(MA.presets[key], MA.teamId, MA.name);
      MA.undo = prevUndo; MA.redo = prevRedo;
      touch(); G.render();
    };
    const setFull = on => { MA.full = on; $("#ma-wrap")?.classList.toggle("ma-full", on); $("#ma-full")?.classList.toggle("on", on); MA.fitPending = true; setTimeout(() => draw(), 30); };
    MA._setFull = setFull;
    $("#ma-undo").onclick = performMaUndo;
    $("#ma-redo").onclick = performMaRedo;
    $("#ma-full").onclick = () => setFull(!MA.full);
    if ($("#ma-full-x")) $("#ma-full-x").onclick = () => setFull(false);
    if ($("#ma-run2")) $("#ma-run2").onclick = run(async () => { setFull(false); await startRun(); });
    const rz = $("#ma-resize");
    rz.onpointerdown = e => {
      const wrap = $("#ma-wrap"), y0 = e.clientY, h0 = wrap.getBoundingClientRect().height;
      rz.setPointerCapture(e.pointerId);
      rz.onpointermove = ev => { MA.h = Math.max(420, Math.min(2400, h0 + ev.clientY - y0)); wrap.style.height = MA.h + "px"; };
      rz.onpointerup = () => { rz.onpointermove = rz.onpointerup = null; try { localStorage.setItem("agdd.ma.h", String(MA.h)); } catch {} draw(); };
    };
    rz.ondblclick = () => { MA.h = 0; try { localStorage.removeItem("agdd.ma.h"); } catch {} G.render(); };
    const drawRunAtt = () => {
      const box = $("#ma-runatt"); if (!box) return;
      box.innerHTML = attField("run", MA.runAtt, "이번 실행에만 요청과 함께 전달됩니다 (입력 노드에 연결된 에이전트가 받음).");
      bindAttField("run", () => MA.runAtt, ids => { MA.runAtt = ids; }, drawRunAtt);
    };
    drawRunAtt();
    $("#ma-request").onpaste = e => {
      const files = [...(e.clipboardData?.files || [])];
      if (files.length) { e.preventDefault(); addFiles(files, { key: "run", add: id => { if (!MA.runAtt.includes(id)) MA.runAtt = [...MA.runAtt, id]; } }, drawRunAtt); }
    };
    $("#ma-validate").onclick = run(async () => {
      const r = await api("POST", `${WSP()}/teams/validate`, { graph: MA.graph }, { noInvalidate: true });
      r.ok ? toast("그래프가 유효합니다 ✓") : toast(r.error, true);
    });
    $("#ma-json").onclick = () => { MA.json = !MA.json; G.render(); };
    const zoomTo = z => { MA.zoom = Math.max(0.3, Math.min(1.6, Math.round(z * 10) / 10)); draw(); };
    $("#ma-zin").onclick = () => zoomTo((MA.zoom || 1) + 0.1);
    $("#ma-zout").onclick = () => zoomTo((MA.zoom || 1) - 0.1);
    $("#ma-fit").onclick = () => { MA.fitPending = true; draw(); };
    if (MA.json) $("#ma-apply-json").onclick = run(async () => {
      snapshotMaUndo();
      const prevUndo = MA.undo, prevRedo = MA.redo;
      loadGraph(JSON.parse($("#ma-raw").value), MA.teamId, MA.name);
      MA.undo = prevUndo; MA.redo = prevRedo;
      touch(); MA.json = false; G.render();
    });
    $("#ma-request").oninput = e => { MA.lastRequest = e.target.value; };
    $("#ma-run").onclick = run(startRun);
    $("#ma-history").onchange = run(async e => {
      if (!e.target.value) return;
      const data = await api("GET", `${WSP()}/team-runs/${e.target.value}`, undefined, { fresh: true });
      ingest(data, true); MA.flashes = [];
      if (data.graph_json && confirm("이 실행에 사용된 그래프를 캔버스에 불러올까요? (취소: 현재 캔버스 유지)")) { snapshotMaUndo(); const u = MA.undo, r = MA.redo; loadGraph(data.graph_json, MA.teamId, MA.name); MA.undo = u; MA.redo = r; touch(); }
      draw(); drawFeed(); drawRunHead(); syncMonitor(); if (data.status === "RUNNING") poll();
    });
    const vw = $("#view");
    vw.onkeydown = null;
    if (!MA._keyBound) {
      MA._keyBound = true;
      document.addEventListener("keydown", e => {
        if (G.getTAB() !== "team" || e.isComposing || !MA.graph) return;
        const mod = e.ctrlKey || e.metaKey;
        const code = e.code || "";
        if (e.key === "Escape" && MA.full) { e.preventDefault(); MA._setFull?.(false); return; }
        if (mod && code === "KeyS") { e.preventDefault(); run(save)(); return; }
        const ae = document.activeElement;
        const tag = (ae?.tagName || "").toUpperCase();
        const isText = ae?.isContentEditable || tag === "TEXTAREA" || tag === "SELECT" ||
          (tag === "INPUT" && !["checkbox", "radio", "range", "color", "button", "submit", "reset"].includes((ae.type || "text").toLowerCase()));
        if (MA.json || isText) return;
        if (mod && code === "KeyZ") { e.preventDefault(); if (e.shiftKey) performMaRedo(); else performMaUndo(); return; }
        if (mod && code === "KeyY") { e.preventDefault(); performMaRedo(); return; }
        if (mod && code === "KeyD" && MA.sel?.type === "node") { e.preventDefault(); duplicateSelectedMaNode(); return; }
        if ((code === "Delete" || code === "Backspace") && MA.sel) { e.preventDefault(); removeSelected(); }
      });
    }
    if (!MA.json) draw(true); else drawInspector();
    drawFeed(); drawRunHead(); refreshHistory(); buildMonitor();
    if (MA.run?.status === "RUNNING") poll();
    clearInterval(MA.anim);
    MA.anim = setInterval(() => { if (G.getTAB() !== "team") return clearInterval(MA.anim); if (MA.flashes.length) draw(); }, 900);
  };
})();
