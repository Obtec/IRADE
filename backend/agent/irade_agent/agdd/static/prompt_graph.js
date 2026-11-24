// Prompt Dependency Graph & Nested Pre-Prompt Group Canvas
// - Prompt nodes store: prompt, output, model, dependencies, extra, parent_group_id
// - Edges (A -> B): "A depends on B" (B runs first; A inspects B via real MCP server)
// - Dashed rectangles (Pre-Prompt Groups) wrap prompt nodes or other groups to arbitrary depth
(() => {
  const G = window.AGDD;
  const { $, esc, api, toast, run, stale } = G;
  const PG = G.PGRAPH = G.PGRAPH || {};

  Object.assign(PG, {
    graphs: PG.graphs || [],
    presets: PG.presets || {},
    presetInfo: PG.presetInfo || {},
    gateway: PG.gateway || {},
    defaultModel: PG.defaultModel || "mid",
    graphId: PG.graphId || "",
    name: PG.name || "새 프롬프트 그래프",
    graph: PG.graph || null,
    sel: PG.sel || null, // { type: "node"|"group"|"edge", id }
    dirty: !!PG.dirty,
    json: false,
    wrapMode: false,
    linkMode: false,
    showProfileOverlay: false,
    reuseOutputs: false,
    edgeDir: PG.edgeDir || (G.store.get("agdd.pg.edgedir") === "execution" ? "execution" : "dependency"),
    pendingLinkFrom: null,
    inspTab: PG.inspTab || "changes",
    changeSel: PG.changeSel instanceof Set ? PG.changeSel : new Set(),
    changeSeen: PG.changeSeen instanceof Set ? PG.changeSeen : new Set(),
    templates: PG.templates || [],
    analytics: PG.analytics || null,
    versionCache: PG.versionCache || {},
    searchQuery: PG.searchQuery || "",
    searchResults: PG.searchResults || [],
    run: PG.run || null,
    poll: null,
    flashes: PG.flashes || [],
    undo: PG.undo || [],
    redo: PG.redo || [],
    full: !!PG.full,
    zoom: PG.zoom || 1,
  });

  const STATUS_KO = {
    idle: "대기",
    pending: "순서 대기",
    running: "실행 중",
    done: "완료",
    reused: "결과 재사용",
    failed: "실패",
    skipped: "건너뜀",
  };

  if (!document.getElementById("pg-style")) {
    const st = document.createElement("style");
    st.id = "pg-style";
    st.textContent = `
      .pg-toolbar { display:flex; flex-wrap:wrap; align-items:center; gap:6px; padding:8px 10px;
        border:1px solid var(--line); border-bottom:0; border-radius:10px 10px 0 0; background:var(--panel); }
      .pg-wrap { display:grid; grid-template-columns:minmax(0,1fr) 390px; height:calc(100vh - 230px);
        min-height:480px; border:1px solid var(--line); border-radius:0 0 10px 10px; overflow:hidden; background:var(--panel); }
      .pg-wrap.pg-full { position:fixed; inset:10px; z-index:60; height:auto !important; border-radius:10px;
        box-shadow:0 12px 48px rgba(0,0,0,.28); }
      .pg-stagebox { position:relative; min-width:0; min-height:0; display:flex; }
      .pg-stage { flex:1; position:relative; overflow:auto; touch-action:none; user-select:none;
        background:radial-gradient(circle, color-mix(in srgb,var(--line) 85%,transparent) 1px, transparent 1px) 0 0/20px 20px, var(--bg); }
      .pg-stage.wrap-mode { cursor:crosshair; }
      .pg-stage.link-mode { cursor:crosshair; }
      .pg-stage.link-mode .pg-node, .pg-stage.link-mode .pg-group { cursor:crosshair; }
      .pg-board { position:relative; }
      .pg-svg { position:absolute; inset:0; overflow:visible; pointer-events:none; }
      .pg-edge { fill:none; stroke-width:2.2; pointer-events:none; }
      .pg-edge.grp { stroke-dasharray:6 4; }
      .pg-edge.cond { stroke-dasharray:5 3; }
      .pg-edge.loop { stroke-dasharray:7 3 2 3; }
      .pg-edge.crit { stroke-width:3.2; filter:drop-shadow(0 0 4px #ef4444); }
      .pg-edge.on { stroke-width:2.7; filter:drop-shadow(0 0 3px currentColor); }
      .pg-edge.flow { stroke-dasharray:8 6; animation:pgflow .6s linear infinite; stroke-width:3.2; }
      .pg-edge-hit { fill:none; stroke:transparent; stroke-width:14; cursor:pointer; pointer-events:stroke; }
      @keyframes pgflow { to { stroke-dashoffset:-28; } }
      .pg-elabel { font:11px system-ui,sans-serif; fill:var(--muted); paint-order:stroke; stroke:var(--bg); stroke-width:4px; pointer-events:none; }
      .pg-node.pg-crit { border-color:#ef4444; box-shadow:0 0 0 3px color-mix(in srgb,#ef4444 32%,transparent); }
      .pg-prof-badge { position:absolute; left:50%; top:calc(100% + 4px); transform:translateX(-50%); font:10px ui-monospace,Menlo,monospace;
        background:var(--panel); border:1px solid var(--line); color:var(--ink); padding:1px 6px; border-radius:99px; white-space:nowrap; pointer-events:none; }

      /* Dashed Pre-Prompt Group */
      .pg-group { position:absolute; box-sizing:border-box; border:2px dashed color-mix(in srgb,var(--accent) 65%,var(--line));
        border-radius:12px; background:transparent; cursor:grab; overflow:visible; }
      .pg-group.depth-1 { border-color:#8b5cf6; background:transparent; }
      .pg-group.depth-2 { border-color:#0ea5e9; background:transparent; }
      .pg-group.depth-3 { border-color:#d97706; background:transparent; }
      .pg-group.on { box-shadow:0 0 0 3px color-mix(in srgb,var(--accent) 35%,transparent); border-style:dashed; }
      .pg-group.linking { box-shadow:0 0 0 3px color-mix(in srgb,var(--warn) 45%,transparent); border-style:dashed; }
      .pg-group-title { position:absolute; left:2px; bottom:calc(100% + 4px); max-width:calc(100% - 4px);
        font-size:12px; font-weight:600; color:var(--ink); white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
        pointer-events:none; line-height:1.25; background:color-mix(in srgb,var(--bg) 82%,transparent); padding:0 4px; border-radius:4px; }
      .pg-resize-handle { position:absolute; right:2px; bottom:2px; width:14px; height:14px; cursor:nwse-resize;
        border-right:2.5px solid var(--muted); border-bottom:2.5px solid var(--muted); border-radius:0 0 8px 0; opacity:0; transition:opacity .12s; }
      .pg-group:hover .pg-resize-handle, .pg-group.on .pg-resize-handle { opacity:.75; }
      .pg-resize-handle:hover { opacity:1; border-color:var(--accent); }

      /* Prompt Node */
      .pg-node { position:absolute; box-sizing:border-box; border:1.5px solid var(--line); border-radius:10px;
        background:var(--panel); box-shadow:0 2px 6px rgba(0,0,0,.08); cursor:grab; display:flex; flex-direction:column; overflow:visible; }
      .pg-node.on { border-color:var(--accent); box-shadow:0 0 0 3px color-mix(in srgb,var(--accent) 40%,transparent); }
      .pg-node.linking { border-color:var(--warn); box-shadow:0 0 0 3px color-mix(in srgb,var(--warn) 45%,transparent); }
      .pg-node-title { position:absolute; left:50%; bottom:calc(100% + 4px); transform:translateX(-50%); max-width:calc(100% + 48px);
        font-size:12px; font-weight:600; color:var(--ink); white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
        pointer-events:none; line-height:1.25; text-align:center; background:color-mix(in srgb,var(--bg) 82%,transparent); padding:0 4px; border-radius:4px; }
      .pg-node .bd { padding:6px 10px; font-size:11px; color:var(--muted); line-height:1.35; overflow:hidden; flex:1; display:flex; flex-direction:column; gap:3px; }
      .pg-node .bd .pr { color:var(--ink); overflow:hidden; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; }
      .pg-node .bd .out { color:var(--ok); font-size:10.5px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
      .pg-chips { display:flex; gap:4px; flex-wrap:wrap; align-items:center; }
      .pg-chips .chip { font-size:10px; padding:0 5px; }
      .pg-chips .chip.dep { border:1px solid color-mix(in srgb,#0ea5e9 45%,var(--line)); color:#0ea5e9; background:transparent; }
      .pg-chips .chip.pre { border:1px solid color-mix(in srgb,#8b5cf6 45%,var(--line)); color:#8b5cf6; background:transparent; }
      .pg-chips .chip.temp { border:1px solid color-mix(in srgb,#d97706 55%,var(--line)); color:#d97706; background:color-mix(in srgb,#d97706 10%,transparent); font-weight:600; }
      .pg-chips .chip.done-ok { border:1px solid color-mix(in srgb,var(--ok) 55%,var(--line)); color:var(--ok); background:transparent; }
      .pg-chips .chip.done-no { color:var(--muted); background:var(--chip); }
      .pg-chips .chip.chg-mod { border:1px solid color-mix(in srgb,#d97706 60%,var(--line)); color:#d97706; background:color-mix(in srgb,#d97706 12%,transparent); font-weight:600; }
      .pg-chips .chip.chg-rerun { border:1px solid color-mix(in srgb,#ea580c 60%,var(--line)); color:#ea580c; background:color-mix(in srgb,#ea580c 12%,transparent); font-weight:600; }
      .pg-chips .chip.chg-new { border:1px solid color-mix(in srgb,#64748b 50%,var(--line)); color:#475569; background:color-mix(in srgb,#64748b 10%,transparent); }
      .pg-node.st-modified, .pg-group.st-modified { border-color:#d97706; }
      .pg-node.st-needs-rerun, .pg-group.st-needs-rerun { border-color:#ea580c; }
      .pg-group-temp { white-space:nowrap; overflow:hidden; text-overflow:ellipsis; color:#d97706; font-weight:600; }

      /* Right Inspector Tabs & Git-style Changes Panel */
      .pg-insp-tabs { display:flex; border-bottom:1px solid var(--line); background:var(--bg); position:sticky; top:0; z-index:4; }
      .pg-insp-tab { flex:1; border:0; border-bottom:2px solid transparent; background:transparent; color:var(--muted);
        padding:8px 4px; font-size:11.5px; font-weight:600; cursor:pointer; display:flex; align-items:center; justify-content:center; gap:4px; white-space:nowrap; }
      .pg-diff { font:11.5px ui-monospace,Menlo,monospace; background:var(--bg); border:1px solid var(--line); border-radius:8px;
        padding:6px 8px; max-height:220px; overflow:auto; white-space:pre-wrap; line-height:1.45; }
      .pg-diff-add { background:color-mix(in srgb,var(--ok) 15%,transparent); color:var(--ok); display:block; }
      .pg-diff-del { background:color-mix(in srgb,var(--bad) 15%,transparent); color:var(--bad); display:block; }
      .pg-insp-tab:hover { color:var(--ink); }
      .pg-insp-tab.on { color:var(--accent); border-bottom-color:var(--accent); background:var(--panel); }
      .pg-tab-cnt { font-size:11px; padding:0 6px; border-radius:99px; background:var(--chip); color:var(--muted); font-weight:600; }
      .pg-tab-cnt.has { background:color-mix(in srgb,#d97706 18%,transparent); color:#b45309; }
      .pg-chg-panel { padding:10px 12px; display:flex; flex-direction:column; gap:10px; }
      .pg-chg-summary { display:grid; grid-template-columns:repeat(4,1fr); gap:5px; text-align:center; }
      .pg-chg-stat { border:1px solid var(--line); border-radius:8px; padding:5px 4px; background:var(--bg); font-size:11px; color:var(--muted); }
      .pg-chg-stat b { display:block; font-size:14px; color:var(--ink); }
      .pg-chg-item { border:1px solid var(--line); border-radius:8px; padding:8px 9px; background:var(--panel); display:flex; flex-direction:column; gap:5px; }
      .pg-chg-item.st-modified { border-left:3px solid #d97706; }
      .pg-chg-item.st-needs-rerun { border-left:3px solid #ea580c; }
      .pg-chg-item .top { display:flex; align-items:center; gap:6px; font-size:12.5px; }
      .pg-chg-item .name { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-weight:600; flex:1; min-width:0; }
      .pg-chg-reason { font-size:11px; color:var(--muted); line-height:1.4; }
      .pg-chg-callout { border:1px solid var(--line); border-radius:8px; padding:7px 9px; font-size:12px; background:var(--bg); display:flex; flex-direction:column; gap:4px; }
      .pg-chg-callout.st-modified { border-color:#d97706; background:color-mix(in srgb,#d97706 7%,transparent); }
      .pg-chg-callout.st-needs-rerun { border-color:#ea580c; background:color-mix(in srgb,#ea580c 7%,transparent); }
      .pg-chg-callout.st-clean { border-color:color-mix(in srgb,var(--ok) 45%,var(--line)); }

      /* Git-style Destroy & Temp Prompt Modal */
      .pg-modal-backdrop { position:fixed; inset:0; z-index:90; background:rgba(0,0,0,.52); display:flex; align-items:center; justify-content:center; padding:16px; }
      .pg-modal { width:min(860px, 96vw); max-height:88vh; display:flex; flex-direction:column; background:var(--panel); color:var(--ink);
        border:1px solid var(--line); border-radius:12px; box-shadow:0 18px 54px rgba(0,0,0,.35); overflow:hidden; }
      .pg-modal-hd { display:flex; align-items:center; justify-content:space-between; gap:10px; padding:12px 16px; border-bottom:1px solid var(--line); }
      .pg-modal-hd h3 { margin:0; font-size:15px; }
      .pg-modal-grid { display:grid; grid-template-columns:minmax(260px, 1fr) minmax(300px, 1.25fr); min-height:320px; max-height:62vh; overflow:hidden; }
      @media (max-width:760px) { .pg-modal-grid { grid-template-columns:1fr; overflow:auto; } }
      .pg-modal-list { border-right:1px solid var(--line); padding:12px; overflow:auto; display:flex; flex-direction:column; gap:8px; background:var(--bg); }
      .pg-dep-item { border:1px solid var(--line); border-radius:8px; padding:8px 10px; background:var(--panel); display:flex; flex-direction:column; gap:5px; }
      .pg-dep-item.has-draft { border-color:#d97706; }
      .pg-dep-item .top { display:flex; align-items:center; gap:6px; font-size:12.5px; }
      .pg-dep-item .prev { font-size:11.5px; color:#d97706; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
      .pg-modal-comp { padding:12px 14px; display:flex; flex-direction:column; gap:10px; overflow:auto; }
      .pg-modal-ft { display:flex; align-items:center; justify-content:flex-end; gap:8px; padding:10px 16px; border-top:1px solid var(--line); background:var(--bg); flex-wrap:wrap; }

      .pg-st { position:absolute; right:8px; bottom:5px; font-size:10px; padding:0 6px; border-radius:99px; background:var(--chip); color:var(--muted); }
      .pg-node.s-running { border-color:var(--warn); animation:pgpulse 1.2s ease-in-out infinite; }
      .pg-node.s-running .pg-st { background:var(--warn); color:#fff; }
      .pg-node.s-done { border-color:var(--ok); }
      .pg-node.s-done .pg-st { background:var(--ok); color:#fff; }
      .pg-node.s-failed { border-color:var(--bad); }
      .pg-node.s-failed .pg-st { background:var(--bad); color:#fff; }
      .pg-node.s-skipped { opacity:.58; }
      @keyframes pgpulse { 50% { box-shadow:0 0 0 6px color-mix(in srgb,var(--warn) 25%,transparent); } }

      .pg-wrap-box { position:absolute; border:2px dashed var(--accent); background:color-mix(in srgb,var(--accent) 12%,transparent);
        border-radius:10px; pointer-events:none; display:none; z-index:30; }
      .pg-insp { border-left:1px solid var(--line); overflow:auto; }
      .pg-run { display:grid; grid-template-columns: 360px minmax(0,1fr); gap:12px; margin-top:12px; }
      @media (max-width:1100px) { .pg-wrap { grid-template-columns:1fr; } .pg-run { grid-template-columns:1fr; } }
      .pg-feed { max-height:380px; overflow:auto; display:flex; flex-direction:column; gap:6px; }
      .pg-ev { border:1px solid var(--line); border-radius:8px; padding:6px 10px; background:var(--panel); font-size:12.5px; }
      .pg-ev.mcp { border-color:color-mix(in srgb,#0ea5e9 50%,var(--line)); background:color-mix(in srgb,#0ea5e9 6%,var(--panel)); }
      .pg-ev.bad { border-color:var(--bad); }
      .pg-ev.fin { border-color:var(--ok); }
      .pg-ev summary { cursor:pointer; }
      .pg-ev pre { white-space:pre-wrap; word-break:break-word; margin:6px 0 0; font-size:12px; max-height:260px; overflow:auto; }
      .pg-hint { font-size:11.5px; color:var(--muted); line-height:1.5; }
      .pg-pre-preview { background:var(--bg); border:1px dashed var(--line); border-radius:6px; padding:6px 8px; font-size:11.5px; white-space:pre-wrap; max-height:140px; overflow:auto; }
      .pg-dep-pill { display:inline-flex; align-items:center; gap:5px; padding:2px 8px; border-radius:99px; border:1px solid var(--line); background:var(--bg); font-size:11.5px; }
      .pg-dep-pill button { border:0; background:none; color:var(--bad); cursor:pointer; padding:0; font-size:13px; line-height:1; }
      .pg-fullbar { display:none; position:absolute; left:12px; top:10px; z-index:40; gap:6px; }
      .pg-wrap.pg-full .pg-fullbar { display:flex; }
    `;
    document.head.appendChild(st);
  }

  const WSP = () => `/workspaces/${G.getWS()}`;
  const findNode = id => PG.graph?.nodes?.find(n => n.id === id) || null;
  const findGroup = id => PG.graph?.groups?.find(g => g.id === id) || null;
  const findEntity = id => findNode(id) || findGroup(id);
  const entityLabel = id => findEntity(id)?.label || id || "";
  const nodeState = id => PG.run?.node_state?.[id] || null;

  function defaultContract() {
    return { enabled: false, json_required: false, json_keys: [], must_include: [], must_exclude: [], regex: "", min_chars: 0, max_retries: 0, on_fail: "fail" };
  }
  function defaultArtifact() {
    return { kind: "none", repo: "workspace", path: "", mode: "read", last_content_hash: "", last_synced_at: "" };
  }
  function defaultCondition() {
    return { type: "always", value: "", case_sensitive: false };
  }

  function ensureArrays(g) {
    g.nodes = Array.isArray(g.nodes) ? g.nodes : [];
    g.groups = Array.isArray(g.groups) ? g.groups : [];
    g.edges = Array.isArray(g.edges) ? g.edges : [];
    g.settings = g.settings || { parallelism: 4, max_tool_steps: 8, default_model: "", auto_rerun_needs_rerun: false, edge_direction: "dependency", cross_graph_cache: false };
    g.settings.auto_rerun_needs_rerun = Boolean(g.settings.auto_rerun_needs_rerun);
    g.settings.cross_graph_cache = Boolean(g.settings.cross_graph_cache);
    g.settings.edge_direction = g.settings.edge_direction === "execution" ? "execution" : "dependency";
    return g;
  }

  // Canonical synchronization: immediately reflect edges -> dependencies and parent_group_id -> node_ids / child_group_ids
  function syncGraphRelations(g = PG.graph) {
    if (!g) return;
    ensureArrays(g);
    const nodeIds = new Set(g.nodes.map(n => n.id));
    const groupIds = new Set(g.groups.map(x => x.id));
    const allIds = new Set([...nodeIds, ...groupIds]);

    g.groups.forEach(grp => {
      if (grp.parent_group_id && (!groupIds.has(grp.parent_group_id) || grp.parent_group_id === grp.id)) {
        grp.parent_group_id = null;
      }
      grp.node_ids = [];
      grp.child_group_ids = [];
      grp.dependencies = [];
      grp.temp_pre_prompt = typeof grp.temp_pre_prompt === "string" ? grp.temp_pre_prompt : "";
      grp.completed = Boolean(grp.completed);
      grp.last_run_pre_prompt = typeof grp.last_run_pre_prompt === "string"
        ? grp.last_run_pre_prompt
        : (grp.completed ? (grp.pre_prompt || "") : null);
      grp.last_run_temp_pre_prompt = typeof grp.last_run_temp_pre_prompt === "string"
        ? grp.last_run_temp_pre_prompt
        : (grp.completed ? "" : null);
      grp.needs_rerun = grp.completed ? Boolean(grp.needs_rerun || grp.stale_by_dep) : false;
      grp.rerun_reason = grp.needs_rerun ? String(grp.rerun_reason || "") : "";
    });
    g.nodes.forEach(n => {
      if (n.parent_group_id && !groupIds.has(n.parent_group_id)) n.parent_group_id = null;
      n.dependencies = [];
      n.extra = n.extra && typeof n.extra === "object" && !Array.isArray(n.extra) ? n.extra : {};
      n.contract = Object.assign(defaultContract(), n.contract && typeof n.contract === "object" ? n.contract : {});
      n.artifact = Object.assign(defaultArtifact(), n.artifact && typeof n.artifact === "object" ? n.artifact : {});
      n.impact_history = Array.isArray(n.impact_history) ? n.impact_history.slice(0, 20) : [];
      n.temp_prompt = typeof n.temp_prompt === "string" ? n.temp_prompt : "";
      n.completed = Boolean(n.completed);
      n.last_run_prompt = typeof n.last_run_prompt === "string"
        ? n.last_run_prompt
        : (n.completed ? (n.prompt || "") : null);
      n.last_run_temp_prompt = typeof n.last_run_temp_prompt === "string"
        ? n.last_run_temp_prompt
        : (n.completed ? "" : null);
      n.needs_rerun = n.completed ? Boolean(n.needs_rerun || n.stale_by_dep) : false;
      n.rerun_reason = n.needs_rerun ? String(n.rerun_reason || "") : "";
    });

    // Break any accidental group containment cycle
    g.groups.forEach(grp => {
      const seen = new Set([grp.id]);
      let cur = grp.parent_group_id;
      while (cur) {
        if (seen.has(cur)) {
          grp.parent_group_id = null;
          break;
        }
        seen.add(cur);
        cur = findGroup(cur)?.parent_group_id || null;
      }
    });

    g.nodes.forEach(n => {
      if (n.parent_group_id) findGroup(n.parent_group_id)?.node_ids.push(n.id);
    });
    g.groups.forEach(grp => {
      if (grp.parent_group_id) findGroup(grp.parent_group_id)?.child_group_ids.push(grp.id);
    });

    const seenPairs = new Set();
    g.edges = g.edges.filter(e => {
      if (!e || !allIds.has(e.from) || !allIds.has(e.to)) return false;
      const isLoop = Boolean(e.is_loop);
      if (e.from === e.to && !isLoop) return false;
      const k = `${e.from}>${e.to}>${isLoop ? 1 : 0}`;
      if (seenPairs.has(k)) return false;
      seenPairs.add(k);
      return true;
    });
    g.edges.forEach((e, i) => {
      if (!e.id) e.id = `e${i + 1}`;
      e.is_loop = Boolean(e.is_loop);
      e.max_iterations = e.is_loop ? Math.max(1, Math.min(20, parseInt(e.max_iterations, 10) || 3)) : 0;
      e.condition = Object.assign(defaultCondition(), e.condition && typeof e.condition === "object" ? e.condition : {});
      e.label = typeof e.label === "string" ? e.label : "";
      e.temp_prompt = typeof e.temp_prompt === "string" ? e.temp_prompt : "";
      if (!e.is_loop) {
        const src = findEntity(e.from);
        if (src) src.dependencies.push(e.to);
      }
    });
    g.nodes.forEach(n => {
      if (!n.completed) {
        n.last_run_pre_prompts = null;
      } else if (!n.last_run_pre_prompts || typeof n.last_run_pre_prompts !== "object" || Array.isArray(n.last_run_pre_prompts)) {
        const mp = {};
        ancestorGroups(n.id, g).forEach(ag => { mp[ag.id] = { pre: ag.pre_prompt || "", temp: "" }; });
        n.last_run_pre_prompts = mp;
      }
    });
  }

  function ancestorGroups(entityId, g = PG.graph) {
    const out = [];
    const seen = new Set();
    let cur = (g.nodes.find(n => n.id === entityId) || g.groups.find(x => x.id === entityId))?.parent_group_id || null;
    while (cur && !seen.has(cur)) {
      seen.add(cur);
      const grp = g.groups.find(x => x.id === cur);
      if (!grp) break;
      out.push(grp);
      cur = grp.parent_group_id || null;
    }
    out.reverse();
    return out;
  }

  function groupDepth(gid, g = PG.graph) {
    return ancestorGroups(gid, g).length;
  }

  function groupTier(gid, g = PG.graph, seen = new Set()) {
    if (seen.has(gid)) return 1;
    const grp = (g?.groups || []).find(x => x.id === gid);
    if (!grp) return 1;
    seen.add(gid);
    const children = (grp.child_group_ids || []).filter(cid => cid !== gid && !seen.has(cid));
    if (!children.length) {
      seen.delete(gid);
      return 1;
    }
    let mx = 0;
    for (const cid of children) {
      mx = Math.max(mx, groupTier(cid, g, seen));
    }
    seen.delete(gid);
    return mx + 1;
  }

  function tierColor(tier) {
    const palette = ["var(--accent)", "#8b5cf6", "#0ea5e9", "#d97706", "#ec4899", "#14b8a6"];
    if (tier >= 1 && tier <= palette.length) return palette[tier - 1];
    const hue = ((tier - 1) * 57) % 360;
    return `hsl(${hue}, 72%, 48%)`;
  }

  function subtreeIds(entityId, g = PG.graph) {
    const out = new Set([entityId]);
    const grp = g.groups.find(x => x.id === entityId);
    if (!grp) return out;
    const stack = [grp];
    while (stack.length) {
      const cur = stack.pop();
      for (const nid of cur.node_ids || []) out.add(nid);
      for (const cgid of cur.child_group_ids || []) {
        if (!out.has(cgid)) {
          out.add(cgid);
          const cg = g.groups.find(x => x.id === cgid);
          if (cg) stack.push(cg);
        }
      }
    }
    return out;
  }

  function wouldCreateCycle(fromId, toId, g = PG.graph, isLoop = false) {
    if (fromId === toId) return isLoop ? null : "자기 자신을 의존할 수 없습니다.";
    const subFrom = subtreeIds(fromId, g);
    const subTo = subtreeIds(toId, g);
    for (const x of subFrom) {
      if (subTo.has(x)) return "상위 그룹과 내부 요소 간에는 의존 관계를 연결할 수 없습니다.";
    }
    if (isLoop) return null;
    const adj = new Map();
    const addAdj = (u, v) => {
      const sU = subtreeIds(u, g);
      const sV = subtreeIds(v, g);
      for (const x of sU) {
        if (!adj.has(x)) adj.set(x, new Set());
        for (const y of sV) adj.get(x).add(y);
      }
    };
    g.edges.filter(e => !e.is_loop).forEach(e => addAdj(e.from, e.to));
    addAdj(fromId, toId);

    // Check if any member in subtree(toId) can reach any member in subtree(fromId)
    const visited = new Set();
    const q = [...subTo];
    while (q.length) {
      const cur = q.shift();
      if (subFrom.has(cur)) return "순환 의존(Cycle)이 발생합니다. 조건부 순환 간선으로 추가할 수 있습니다.";
      if (visited.has(cur)) continue;
      visited.add(cur);
      for (const nxt of adj.get(cur) || []) {
        if (!visited.has(nxt)) q.push(nxt);
      }
    }
    return null;
  }

  function isEntityCompleted(entityId, g = PG.graph) {
    const n = (g.nodes || []).find(x => x.id === entityId);
    if (n) return Boolean(n.completed);
    const grp = (g.groups || []).find(x => x.id === entityId);
    return Boolean(grp?.completed);
  }

  function validateCompletedEdge(fromId, toId, g = PG.graph) {
    const subFrom = subtreeIds(fromId, g);
    const subTo = subtreeIds(toId, g);
    let compFrom = null;
    for (const x of subFrom) {
      if (isEntityCompleted(x, g)) { compFrom = x; break; }
    }
    let uncompTo = null;
    for (const y of subTo) {
      if (!isEntityCompleted(y, g)) { uncompTo = y; break; }
    }
    if (compFrom && uncompTo) {
      const lbl = id => ((g.nodes || []).find(x => x.id === id) || (g.groups || []).find(x => x.id === id))?.label || id;
      return `완료된 대상(${lbl(compFrom)})은 미실행(미완료) 대상(${lbl(uncompTo)})에 의존하도록 연결할 수 없습니다.`;
    }
    return null;
  }

  function computeChangeStatus(g = PG.graph) {
    if (!g) return { nodes: {}, groups: {} };
    const nodes = g.nodes || [];
    const groups = g.groups || [];
    const byId = new Map();
    nodes.forEach(n => byId.set(n.id, n));
    groups.forEach(grp => byId.set(grp.id, grp));
    const lbl = id => byId.get(id)?.label || id;

    const state = new Map();
    const reasons = new Map();
    const details = new Map();

    const addReason = (id, code, text) => {
      const r = reasons.get(id);
      if (!r.includes(code)) {
        r.push(code);
        details.get(id).push(text);
      }
    };

    for (const n of nodes) {
      reasons.set(n.id, []);
      details.set(n.id, []);
      if (!n.completed) {
        state.set(n.id, "unexecuted");
        addReason(n.id, "unexecuted", "미실행");
        continue;
      }
      const directCodes = [];
      if (n.last_run_prompt !== null && n.last_run_prompt !== undefined && (n.prompt || "") !== n.last_run_prompt) {
        directCodes.push(["prompt_changed", "프롬프트 변경됨"]);
      }
      const lastTemp = n.last_run_temp_prompt == null ? "" : n.last_run_temp_prompt;
      if ((n.temp_prompt || "") !== lastTemp) {
        directCodes.push(["temp_prompt_changed", "임시 프롬프트 변경됨"]);
      }
      const curPre = new Map();
      ancestorGroups(n.id, g).forEach(ag => {
        curPre.set(ag.id, { pre: ag.pre_prompt || "", temp: ag.temp_pre_prompt || "" });
      });
      const prevPre = n.last_run_pre_prompts;
      const preChangedGids = [];
      if (prevPre && typeof prevPre === "object" && !Array.isArray(prevPre)) {
        for (const [gid, vals] of curPre.entries()) {
          const pv = prevPre[gid];
          if (!pv || pv.pre !== vals.pre || pv.temp !== vals.temp) preChangedGids.push(gid);
        }
        for (const [gid, vals] of Object.entries(prevPre)) {
          if (!curPre.has(gid) && vals && (vals.pre || vals.temp)) preChangedGids.push(gid);
        }
      }
      const markedRerun = Boolean(n.needs_rerun);
      const rerunText = n.rerun_reason ? `에이전트 판단: ${n.rerun_reason}` : "에이전트가 재실행 필요로 표시함";
      if (directCodes.length) {
        state.set(n.id, "modified");
        directCodes.forEach(([c, d]) => addReason(n.id, c, d));
        preChangedGids.forEach(gid => addReason(n.id, `pre_prompt_changed:${gid}`, `상위 프리 프롬프트 변경: ${lbl(gid)}`));
        if (markedRerun) addReason(n.id, "agent_marked_rerun", rerunText);
      } else if (markedRerun || preChangedGids.length) {
        state.set(n.id, "needs_rerun");
        if (markedRerun) addReason(n.id, "agent_marked_rerun", rerunText);
        preChangedGids.forEach(gid => addReason(n.id, `pre_prompt_changed:${gid}`, `상위 프리 프롬프트 변경: ${lbl(gid)}`));
      } else {
        state.set(n.id, "clean");
      }
    }

    for (const grp of groups) {
      reasons.set(grp.id, []);
      details.set(grp.id, []);
      if (!grp.completed) {
        state.set(grp.id, "unexecuted");
        addReason(grp.id, "unexecuted", "미실행");
        continue;
      }
      const directCodes = [];
      if (grp.last_run_pre_prompt !== null && grp.last_run_pre_prompt !== undefined && (grp.pre_prompt || "") !== grp.last_run_pre_prompt) {
        directCodes.push(["pre_prompt_changed", "프리 프롬프트 변경됨"]);
      }
      const lastTPre = grp.last_run_temp_pre_prompt == null ? "" : grp.last_run_temp_pre_prompt;
      if ((grp.temp_pre_prompt || "") !== lastTPre) {
        directCodes.push(["temp_pre_prompt_changed", "임시 프리 프롬프트 변경됨"]);
      }
      const markedRerun = Boolean(grp.needs_rerun);
      const rerunText = grp.rerun_reason ? `에이전트 판단: ${grp.rerun_reason}` : "에이전트가 재실행 필요로 표시함";
      if (directCodes.length) {
        state.set(grp.id, "modified");
        directCodes.forEach(([c, d]) => addReason(grp.id, c, d));
        if (markedRerun) addReason(grp.id, "agent_marked_rerun", rerunText);
      } else if (markedRerun) {
        state.set(grp.id, "needs_rerun");
        addReason(grp.id, "agent_marked_rerun", rerunText);
      } else {
        state.set(grp.id, "clean");
      }
    }

    let changed = true;
    while (changed) {
      changed = false;
      for (const grp of groups) {
        const gid = grp.id;
        if (!state.has(gid)) continue;
        const children = [...(grp.node_ids || []), ...(grp.child_group_ids || [])];
        for (const cid of children) {
          if (state.has(cid) && state.get(cid) !== "clean") {
            if (state.get(gid) === "clean") {
              state.set(gid, "needs_rerun");
              changed = true;
            }
            if (state.get(gid) === "needs_rerun") {
              addReason(gid, `member_changed:${cid}`, `내부 항목 변경됨: ${lbl(cid)}`);
            }
          }
        }
      }
    }

    const outNodes = {}, outGroups = {};
    for (const n of nodes) {
      const st = state.get(n.id);
      outNodes[n.id] = { state: st, needs_rerun: st !== "clean", reasons: reasons.get(n.id), details: details.get(n.id), rerun_reason: n.rerun_reason || "" };
    }
    for (const grp of groups) {
      const st = state.get(grp.id);
      outGroups[grp.id] = { state: st, needs_rerun: st !== "clean", reasons: reasons.get(grp.id), details: details.get(grp.id), rerun_reason: grp.rerun_reason || "" };
    }
    return { nodes: outNodes, groups: outGroups };
  }

  let pgLastUndoKey = "", pgLastUndoAt = 0;
  function snapshotUndo() {
    if (!PG.graph) return;
    PG.undo.push(JSON.stringify(PG.graph));
    if (PG.undo.length > 60) PG.undo.shift();
    PG.redo = [];
    pgLastUndoKey = "";
  }
  function snapshotUndoOnce(key) {
    const now = Date.now();
    if (pgLastUndoKey === key && now - pgLastUndoAt < 1200) return;
    snapshotUndo();
    pgLastUndoKey = key;
    pgLastUndoAt = now;
  }
  function touch() {
    PG.dirty = true;
    const b = $("#pg-save");
    if (b) b.textContent = "저장 *";
    if ($("#pg-undo")) $("#pg-undo").disabled = !PG.undo.length;
    if ($("#pg-redo")) $("#pg-redo").disabled = !PG.redo.length;
  }
  function performUndo() {
    if (!PG.undo.length) return;
    PG.redo.push(JSON.stringify(PG.graph));
    PG.graph = JSON.parse(PG.undo.pop());
    syncGraphRelations();
    PG.sel = null;
    touch();
    PG.json ? G.render() : draw(true);
  }
  function performRedo() {
    if (!PG.redo.length) return;
    PG.undo.push(JSON.stringify(PG.graph));
    PG.graph = JSON.parse(PG.redo.pop());
    syncGraphRelations();
    PG.sel = null;
    touch();
    PG.json ? G.render() : draw(true);
  }

  function newId(prefix) {
    let i = 1;
    while (findEntity(`${prefix}_${i}`)) i++;
    return `${prefix}_${i}`;
  }

  function addPromptNode(x = 120, y = 100, parentGroupId = null) {
    snapshotUndo();
    const id = newId("node");
    const n = {
      id,
      label: `프롬프트 ${id.split("_")[1]}`,
      prompt: "",
      temp_prompt: "",
      output: "",
      model: "",
      dependencies: [],
      extra: {},
      completed: false,
      parent_group_id: parentGroupId,
      x: Math.round(x / 10) * 10,
      y: Math.round(y / 10) * 10,
      width: 250,
      height: 145,
    };
    PG.graph.nodes.push(n);
    syncGraphRelations();
    PG.sel = { type: "node", id };
    PG.inspTab = "edit";
    touch();
    draw(true);
    return n;
  }

  function addPrePromptGroup(x = 80, y = 80, width = 360, height = 240, opts = {}) {
    snapshotUndo();
    const id = newId("group");
    const grp = {
      id,
      label: opts.label || `프리 프롬프트 그룹 ${id.split("_")[1]}`,
      pre_prompt: opts.pre_prompt || "",
      temp_pre_prompt: opts.temp_pre_prompt || "",
      node_ids: [],
      child_group_ids: [],
      parent_group_id: opts.parent_group_id || null,
      dependencies: [],
      completed: false,
      x: Math.round(x / 10) * 10,
      y: Math.round(y / 10) * 10,
      width: Math.max(200, Math.round(width / 10) * 10),
      height: Math.max(130, Math.round(height / 10) * 10),
    };
    PG.graph.groups.push(grp);
    if (Array.isArray(opts.adoptNodes)) {
      opts.adoptNodes.forEach(nid => {
        const n = findNode(nid);
        if (n) n.parent_group_id = id;
      });
    }
    if (Array.isArray(opts.adoptGroups)) {
      opts.adoptGroups.forEach(cgid => {
        const cg = findGroup(cgid);
        if (cg && cg.id !== id) cg.parent_group_id = id;
      });
    }
    syncGraphRelations();
    PG.sel = { type: "group", id };
    PG.inspTab = "edit";
    touch();
    draw(true);
    return grp;
  }

  function isReversedDir() {
    return PG.edgeDir === "execution";
  }

  function connectVisual(startId, endId) {
    return isReversedDir() ? addEdge(endId, startId) : addEdge(startId, endId);
  }

  function visualEdgeEndpoints(e) {
    return isReversedDir() ? { from: e.to, to: e.from } : { from: e.from, to: e.to };
  }

  function addEdge(from, to, opts = {}) {
    if (!findEntity(from) || !findEntity(to)) return false;
    let isLoop = Boolean(opts.is_loop);
    if (!isLoop) {
      const err = wouldCreateCycle(from, to, PG.graph, false);
      if (err) {
        const subFrom = subtreeIds(from, PG.graph);
        const subTo = subtreeIds(to, PG.graph);
        const containmentClash = from !== to && [...subFrom].some(x => subTo.has(x));
        if (containmentClash) {
          toast(err, true);
          return false;
        }
        const okLoop = typeof confirm === "function"
          ? confirm("순환/자기참조가 감지되었습니다.\n조건부 재귀·순환 간선(최대 3회 반복)으로 추가할까요?")
          : false;
        if (!okLoop) {
          toast(err, true);
          return false;
        }
        isLoop = true;
      }
    } else {
      const loopErr = wouldCreateCycle(from, to, PG.graph, true);
      if (loopErr) {
        toast(loopErr, true);
        return false;
      }
    }
    if (PG.graph.edges.some(e => e.from === from && e.to === to && Boolean(e.is_loop) === isLoop)) {
      toast("이미 동일한 간선이 연결되어 있습니다.", true);
      return false;
    }
    if (!isLoop) {
      const compErr = validateCompletedEdge(from, to);
      if (compErr) {
        toast(compErr, true);
        return false;
      }
    }
    snapshotUndo();
    PG.graph.edges.push({
      id: `e${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 5)}`,
      from,
      to,
      is_loop: isLoop,
      max_iterations: isLoop ? (opts.max_iterations || 3) : 0,
      condition: opts.condition || (isLoop ? { type: "on_contract_fail", value: "", case_sensitive: false } : defaultCondition()),
      label: opts.label || (isLoop ? "조건부 순환" : ""),
      temp_prompt: typeof opts.temp_prompt === "string" ? opts.temp_prompt : "",
    });
    syncGraphRelations();
    PG.sel = { type: "edge", id: PG.graph.edges[PG.graph.edges.length - 1].id };
    PG.inspTab = "edit";
    touch();
    draw(true);
    return true;
  }

  function removeEdgeByPair(from, to) {
    snapshotUndo();
    PG.graph.edges = PG.graph.edges.filter(e => !(e.from === from && e.to === to));
    syncGraphRelations();
    touch();
    draw(true);
  }

  function executeDestroyEntity(targetId, tempPrompts = {}) {
    snapshotUndo();
    if (findNode(targetId)) {
      PG.graph.nodes = PG.graph.nodes.filter(n => n.id !== targetId);
    } else if (findGroup(targetId)) {
      const grp = findGroup(targetId);
      const fallbackParent = grp?.parent_group_id || null;
      PG.graph.nodes.forEach(n => { if (n.parent_group_id === targetId) n.parent_group_id = fallbackParent; });
      PG.graph.groups.forEach(g => { if (g.parent_group_id === targetId) g.parent_group_id = fallbackParent; });
      PG.graph.groups = PG.graph.groups.filter(g => g.id !== targetId);
    }
    PG.graph.edges = PG.graph.edges.filter(e => e.from !== targetId && e.to !== targetId);

    let appliedCount = 0;
    for (const [eid, rawText] of Object.entries(tempPrompts || {})) {
      const text = String(rawText || "");
      if (!text.trim()) continue;
      const n = findNode(eid);
      const g = findGroup(eid);
      if (n) {
        n.temp_prompt = text;
        appliedCount += 1;
      } else if (g) {
        g.temp_pre_prompt = text;
        appliedCount += 1;
      }
    }
    syncGraphRelations();
    PG.sel = null;
    touch();
    draw(true);
    if (appliedCount > 0) {
      toast(`${targetId} 제거 완료 · ${appliedCount}개 의존 대상에 1회성 임시 프롬프트 반영됨`);
    }
  }

  function closeDestroyModal() {
    $("#pg-destroy-modal")?.remove();
  }

  function openDestroyModal(target) {
    closeDestroyModal();
    const dependents = PG.graph.edges
      .filter(e => e.to === target.id)
      .map(e => findEntity(e.from))
      .filter(Boolean);
    if (!dependents.length) {
      executeDestroyEntity(target.id, {});
      return;
    }

    const drafts = {};
    const checked = new Set(dependents.map(d => d.id));
    const targetKind = findGroup(target.id) ? "프리 프롬프트 그룹" : "프롬프트 노드";

    const backdrop = document.createElement("div");
    backdrop.id = "pg-destroy-modal";
    backdrop.className = "pg-modal-backdrop";
    backdrop.innerHTML = `<div class="pg-modal" role="dialog" aria-modal="true" aria-label="노드 제거 및 임시 프롬프트 작성">
      <div class="pg-modal-hd">
        <h3>${esc(targetKind)} <code>${esc(target.id)}</code> 제거 — 의존 대상 임시 프롬프트 작성</h3>
        <button type="button" class="icon" id="pg-dm-x" title="닫기">✕</button>
      </div>
      <div style="padding:10px 16px; border-bottom:1px solid var(--line); font-size:12.5px; color:var(--muted)">
        <b>${esc(target.label || target.id)}</b>에 의존하던 <b>${dependents.length}개</b> 대상이 감지되었습니다.
        Git 커밋할 파일을 묶듯 원하는 대상을 체크해 같은 임시 프롬프트를 한 번에 적용하거나, 각각 따로 작성하거나, 작성하지 않고 바로 제거할 수 있습니다.
        (임시 프롬프트는 1회 실행 완료 후 자동 제거됩니다.)
      </div>
      <div class="pg-modal-grid">
        <div class="pg-modal-list">
          <div class="row" style="justify-content:space-between">
            <span style="font-size:12px;font-weight:600">의존하던 대상 목록</span>
            <div class="row" style="gap:4px">
              <button type="button" class="g" id="pg-dm-all" style="padding:2px 8px;font-size:11.5px">전체 선택</button>
              <button type="button" class="g" id="pg-dm-none" style="padding:2px 8px;font-size:11.5px">선택 해제</button>
            </div>
          </div>
          <div id="pg-dm-items" style="display:flex;flex-direction:column;gap:6px"></div>
        </div>
        <div class="pg-modal-comp">
          <div class="fld">
            <span>선택된 대상 (<span id="pg-dm-cnt">0</span>개)</span>
            <div class="pg-chips" id="pg-dm-selchips"></div>
          </div>
          <label class="fld" style="flex:1">
            <span>임시 프롬프트 / 임시 프리 프롬프트 내용 (기존 프롬프트와 별개로 추가 전달됨)</span>
            <textarea id="pg-dm-text" rows="7" style="flex:1;min-height:140px" placeholder="예: 제거된 B 노드가 수행하던 사전 검증 기준을 반영해 직접 검증하세요."></textarea>
          </label>
          <div class="row">
            <button type="button" class="g" id="pg-dm-apply">선택한 대상에 임시 프롬프트 적용 (묶기)</button>
          </div>
        </div>
      </div>
      <div class="pg-modal-ft">
        <button type="button" class="g" id="pg-dm-cancel">취소</button>
        <button type="button" class="g bad" id="pg-dm-skip">임시 프롬프트 없이 바로 제거</button>
        <button type="button" class="b" id="pg-dm-confirm">제거 및 임시 프롬프트 반영</button>
      </div>
    </div>`;
    document.body.appendChild(backdrop);

    const renderModalState = () => {
      const itemsEl = $("#pg-dm-items");
      if (!itemsEl) return;
      itemsEl.innerHTML = dependents.map(d => {
        const isGrp = !!findGroup(d.id);
        const draft = (drafts[d.id] || "").trim();
        const existing = isGrp ? (d.temp_pre_prompt || "") : (d.temp_prompt || "");
        const shown = draft || existing;
        return `<div class="pg-dep-item ${draft ? "has-draft" : ""}">
          <div class="top">
            <input type="checkbox" data-pg-dm-chk="${esc(d.id)}" ${checked.has(d.id) ? "checked" : ""}>
            <span>${isGrp ? "⬚" : "◈"}</span>
            <b style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(d.label || d.id)}</b>
            <span class="muted mono" style="font-size:11px">${esc(d.id)}</span>
            <span class="chip" style="margin-left:auto;font-size:10.5px">${draft ? "✓ 작성됨" : existing ? "기존 임시" : "미작성"}</span>
          </div>
          ${shown ? `<div class="prev">${esc(shown)}</div>` : ""}
          <div class="row" style="gap:4px">
            <button type="button" class="g" data-pg-dm-edit="${esc(d.id)}" style="padding:1px 7px;font-size:11px">개별 편집</button>
            ${draft ? `<button type="button" class="g bad" data-pg-dm-clr="${esc(d.id)}" style="padding:1px 7px;font-size:11px">초기화</button>` : ""}
          </div>
        </div>`;
      }).join("");

      $("#pg-dm-cnt").textContent = String(checked.size);
      $("#pg-dm-selchips").innerHTML = checked.size
        ? [...checked].map(id => `<span class="chip">${findGroup(id) ? "⬚" : "◈"} ${esc(entityLabel(id))} (${esc(id)})</span>`).join("")
        : `<span class="muted" style="font-size:12px">왼쪽에서 임시 프롬프트를 적용할 대상을 체크하세요.</span>`;
      $("#pg-dm-apply").disabled = checked.size === 0;

      itemsEl.querySelectorAll("[data-pg-dm-chk]").forEach(cb => {
        cb.onchange = () => {
          if (cb.checked) checked.add(cb.dataset.pgDmChk);
          else checked.delete(cb.dataset.pgDmChk);
          renderModalState();
        };
      });
      itemsEl.querySelectorAll("[data-pg-dm-edit]").forEach(btn => {
        btn.onclick = () => {
          const id = btn.dataset.pgDmEdit;
          checked.clear();
          checked.add(id);
          $("#pg-dm-text").value = drafts[id] || "";
          renderModalState();
          $("#pg-dm-text").focus();
        };
      });
      itemsEl.querySelectorAll("[data-pg-dm-clr]").forEach(btn => {
        btn.onclick = () => {
          delete drafts[btn.dataset.pgDmClr];
          renderModalState();
        };
      });
    };

    const applyCheckedText = () => {
      const txt = ($("#pg-dm-text")?.value || "").trim();
      if (!txt || checked.size === 0) return false;
      for (const id of checked) drafts[id] = txt;
      checked.clear();
      $("#pg-dm-text").value = "";
      renderModalState();
      return true;
    };

    $("#pg-dm-all").onclick = () => { dependents.forEach(d => checked.add(d.id)); renderModalState(); };
    $("#pg-dm-none").onclick = () => { checked.clear(); renderModalState(); };
    $("#pg-dm-apply").onclick = () => {
      if (!($("#pg-dm-text")?.value || "").trim()) return toast("적용할 임시 프롬프트를 입력하세요.", true);
      applyCheckedText();
    };
    $("#pg-dm-x").onclick = closeDestroyModal;
    $("#pg-dm-cancel").onclick = closeDestroyModal;
    backdrop.onclick = e => { if (e.target === backdrop) closeDestroyModal(); };
    $("#pg-dm-skip").onclick = () => { closeDestroyModal(); executeDestroyEntity(target.id, {}); };
    $("#pg-dm-confirm").onclick = () => {
      const pendingTxt = ($("#pg-dm-text")?.value || "").trim();
      if (pendingTxt && checked.size > 0) {
        for (const id of checked) drafts[id] = pendingTxt;
      }
      closeDestroyModal();
      executeDestroyEntity(target.id, drafts);
    };

    renderModalState();
    requestAnimationFrame(() => $("#pg-dm-text")?.focus());
  }

  function removeSelected() {
    if (!PG.sel) return;
    if (PG.sel.type === "edge") {
      snapshotUndo();
      PG.graph.edges = PG.graph.edges.filter(e => e.id !== PG.sel.id);
      syncGraphRelations();
      PG.sel = null;
      touch();
      draw(true);
      return;
    }
    const ent = findEntity(PG.sel.id);
    if (!ent) return;
    openDestroyModal(ent);
  }

  function renameEntity(oldId, nextId) {
    if (!/^[A-Za-z][A-Za-z0-9_-]{0,39}$/.test(nextId)) {
      toast("ID는 영문으로 시작하고 영문/숫자/_/- 만 사용 가능합니다 (최대 40자).", true);
      return false;
    }
    if (oldId === nextId) return true;
    if (findEntity(nextId)) {
      toast("이미 사용 중인 ID입니다.", true);
      return false;
    }
    snapshotUndo();
    const ent = findEntity(oldId);
    if (!ent) return false;
    ent.id = nextId;
    PG.graph.nodes.forEach(n => {
      if (n.parent_group_id === oldId) n.parent_group_id = nextId;
      if (n.last_run_pre_prompts && typeof n.last_run_pre_prompts === "object" && Object.prototype.hasOwnProperty.call(n.last_run_pre_prompts, oldId)) {
        n.last_run_pre_prompts[nextId] = n.last_run_pre_prompts[oldId];
        delete n.last_run_pre_prompts[oldId];
      }
    });
    PG.graph.groups.forEach(g => { if (g.parent_group_id === oldId) g.parent_group_id = nextId; });
    PG.graph.edges.forEach(e => {
      if (e.from === oldId) e.from = nextId;
      if (e.to === oldId) e.to = nextId;
    });
    syncGraphRelations();
    PG.sel = { type: PG.sel.type, id: nextId };
    touch();
    return true;
  }

  function shiftGraphIfNeeded(targetMinX, targetMinY, g = PG.graph) {
    const dx = targetMinX < 20 ? Math.ceil((20 - targetMinX) / 10) * 10 : 0;
    const dy = targetMinY < 32 ? Math.ceil((32 - targetMinY) / 10) * 10 : 0;
    if (!dx && !dy) return;
    for (const n of g.nodes || []) { n.x += dx; n.y += dy; }
    for (const grp of g.groups || []) { grp.x += dx; grp.y += dy; }
  }

  function fitGroupBounds(gid, g = PG.graph, seen = new Set()) {
    if (!gid || seen.has(gid)) return;
    seen.add(gid);
    const grp = (g.groups || []).find(x => x.id === gid);
    if (!grp) return;
    const children = [
      ...(grp.node_ids || []).map(id => (g.nodes || []).find(n => n.id === id)),
      ...(grp.child_group_ids || []).map(id => (g.groups || []).find(x => x.id === id)),
    ].filter(Boolean);
    if (children.length) {
      const PAD_X = 28, PAD_TOP = 36, PAD_BOT = 28;
      let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
      for (const ch of children) {
        minX = Math.min(minX, ch.x);
        minY = Math.min(minY, ch.y);
        maxX = Math.max(maxX, ch.x + (ch.width || 250));
        maxY = Math.max(maxY, ch.y + (ch.height || 140));
      }
      const needX = Math.round((minX - PAD_X) / 10) * 10;
      const needY = Math.round((minY - PAD_TOP) / 10) * 10;
      if (needX < 20 || needY < 32) {
        const sdx = needX < 20 ? Math.ceil((20 - needX) / 10) * 10 : 0;
        const sdy = needY < 32 ? Math.ceil((32 - needY) / 10) * 10 : 0;
        shiftGraphIfNeeded(needX, needY, g);
        minX += sdx; minY += sdy; maxX += sdx; maxY += sdy;
      }
      const nx = Math.max(20, Math.round((minX - PAD_X) / 10) * 10);
      const ny = Math.max(32, Math.round((minY - PAD_TOP) / 10) * 10);
      const nr = Math.round((maxX + PAD_X) / 10) * 10;
      const nb = Math.round((maxY + PAD_BOT) / 10) * 10;
      const oldR = grp.x + (grp.width || 360);
      const oldB = grp.y + (grp.height || 240);
      grp.x = Math.min(grp.x, nx);
      grp.y = Math.min(grp.y, ny);
      grp.width = Math.max(260, Math.max(oldR, nr) - grp.x);
      grp.height = Math.max(170, Math.max(oldB, nb) - grp.y);
    }
    if (grp.parent_group_id) fitGroupBounds(grp.parent_group_id, g, seen);
  }

  function fitAllAncestorBounds(entityId, g = PG.graph) {
    const ent = (g.nodes || []).find(n => n.id === entityId) || (g.groups || []).find(x => x.id === entityId);
    if (ent?.parent_group_id) fitGroupBounds(ent.parent_group_id, g);
  }

  function wrapEntityInParentGroup(entityId) {
    const ent = findEntity(entityId);
    if (!ent) return null;
    snapshotUndo();
    const oldParent = ent.parent_group_id || null;
    const isGrp = !!findGroup(entityId);
    const nextTier = isGrp ? groupTier(entityId) + 1 : 1;
    const id = newId("group");
    const grp = {
      id,
      label: `${nextTier}-그룹 (${ent.label || entityId})`,
      pre_prompt: "",
      temp_pre_prompt: "",
      node_ids: [],
      child_group_ids: [],
      parent_group_id: oldParent,
      dependencies: [],
      completed: false,
      x: Math.round((ent.x - 28) / 10) * 10,
      y: Math.round((ent.y - 36) / 10) * 10,
      width: Math.max(240, (ent.width || 250) + 56),
      height: Math.max(150, (ent.height || 140) + 64),
    };
    shiftGraphIfNeeded(grp.x, grp.y);
    grp.x = Math.max(20, grp.x);
    grp.y = Math.max(32, grp.y);
    PG.graph.groups.push(grp);
    ent.parent_group_id = id;
    syncGraphRelations();
    fitGroupBounds(id);
    PG.sel = { type: "group", id };
    PG.inspTab = "edit";
    touch();
    draw(true);
    toast(`${nextTier}-그룹(${grp.label})으로 감쌌습니다.`);
    return grp;
  }

  function addChildSubgroup(parentGid) {
    const parent = findGroup(parentGid);
    if (!parent) return null;
    const existingCount = (parent.child_group_ids || []).length;
    const childX = parent.x + 28 + existingCount * 40;
    const childY = parent.y + 36 + existingCount * 24;
    const child = addPrePromptGroup(childX, childY, 320, 210, {
      parent_group_id: parentGid,
      label: `하위 그룹 ${(PG.graph.groups.length || 0) + 1}`,
    });
    fitAllAncestorBounds(child.id);
    draw(true);
    return child;
  }

  function assignParentByGeometry(entityId, g = PG.graph) {
    const ent = (g.nodes || []).find(n => n.id === entityId) || (g.groups || []).find(x => x.id === entityId);
    if (!ent) return;
    const isGrp = !!(g.groups || []).find(x => x.id === entityId);
    const cx = ent.x + (ent.width || 250) / 2;
    const cy = ent.y + (ent.height || 140) / 2;
    const myArea = (ent.width || 250) * (ent.height || 140);
    const forbidden = isGrp ? subtreeIds(entityId, g) : new Set([entityId]);

    const candidates = (g.groups || []).filter(grp => {
      if (forbidden.has(grp.id)) return false;
      const inside = cx >= grp.x && cx <= grp.x + grp.width && cy >= grp.y && cy <= grp.y + grp.height;
      if (!inside) return false;
      if (isGrp && grp.width * grp.height <= myArea) return false;
      return true;
    });
    candidates.sort((a, b) => (a.width * a.height) - (b.width * b.height));
    ent.parent_group_id = candidates[0]?.id || null;
    if (g === PG.graph) syncGraphRelations();
  }

  function translateGroupTree(gid, dx, dy) {
    const sub = subtreeIds(gid);
    for (const id of sub) {
      const ent = findEntity(id);
      if (!ent) continue;
      ent.x = Math.max(0, ent.x + dx);
      ent.y = Math.max(0, ent.y + dy);
    }
  }

  // ------------------------------------------------------------ rendering
  function rectBorderPoint(box, tx, ty, pad = 0) {
    const w = box.width || 250;
    const h = box.height || 140;
    const cx = box.x + w / 2;
    const cy = box.y + h / 2;
    const hw = w / 2 + pad;
    const hh = h / 2 + pad;
    const dx = tx - cx;
    const dy = ty - cy;
    if (Math.abs(dx) < 1e-6 && Math.abs(dy) < 1e-6) {
      return { x: cx + hw, y: cy, nx: 1, ny: 0 };
    }
    const txScale = Math.abs(dx) > 1e-6 ? hw / Math.abs(dx) : Infinity;
    const tyScale = Math.abs(dy) > 1e-6 ? hh / Math.abs(dy) : Infinity;
    const t = Math.min(txScale, tyScale);
    const x = cx + dx * t;
    const y = cy + dy * t;
    if (txScale < tyScale) {
      return { x, y, nx: dx >= 0 ? 1 : -1, ny: 0 };
    }
    return { x, y, nx: 0, ny: dy >= 0 ? 1 : -1 };
  }

  function edgePath(a, b, opts = {}) {
    if (a.id === b.id) {
      const w = a.width || 250;
      const sx = a.x + w - 22;
      const sy = a.y;
      const ex = a.x + w;
      const ey = a.y + 26;
      return {
        d: `M${sx},${sy} C${sx + 8},${sy - 56} ${ex + 62},${sy - 46} ${ex},${ey}`,
        mx: ex + 26,
        my: sy - 26,
      };
    }
    const acx = a.x + (a.width || 250) / 2;
    const acy = a.y + (a.height || 140) / 2;
    const bcx = b.x + (b.width || 250) / 2;
    const bcy = b.y + (b.height || 140) / 2;
    const p1 = rectBorderPoint(a, bcx, bcy, 1);
    const p2 = rectBorderPoint(b, acx, acy, 3);
    const dx = p2.x - p1.x;
    const dy = p2.y - p1.y;
    const dist = Math.hypot(dx, dy) || 1;
    const bend = Math.min(90, Math.max(24, dist * 0.32));
    const offset = Number(opts.curveOffset || 0);
    const ux = -dy / dist;
    const uy = dx / dist;
    const c1x = p1.x + p1.nx * bend + ux * offset;
    const c1y = p1.y + p1.ny * bend + uy * offset;
    const c2x = p2.x + p2.nx * bend + ux * offset;
    const c2y = p2.y + p2.ny * bend + uy * offset;
    const mx = 0.125 * p1.x + 0.375 * c1x + 0.375 * c2x + 0.125 * p2.x;
    const my = 0.125 * p1.y + 0.375 * c1y + 0.375 * c2y + 0.125 * p2.y;
    return {
      d: `M${p1.x},${p1.y} C${c1x},${c1y} ${c2x},${c2y} ${p2.x},${p2.y}`,
      mx,
      my,
    };
  }

  function edgeCurveOffsets(edges) {
    const counts = new Map();
    for (const e of edges) {
      if (e.from === e.to) continue;
      const key = e.from < e.to ? `${e.from}|${e.to}` : `${e.to}|${e.from}`;
      counts.set(key, (counts.get(key) || 0) + 1);
    }
    const seen = new Map();
    const out = new Map();
    for (const e of edges) {
      if (e.from === e.to) { out.set(e.id, 0); continue; }
      const key = e.from < e.to ? `${e.from}|${e.to}` : `${e.to}|${e.from}`;
      const tot = counts.get(key) || 1;
      const idx = seen.get(key) || 0;
      seen.set(key, idx + 1);
      if (tot > 1) {
        out.set(e.id, e.is_loop ? 54 : (idx === 0 ? -24 : 24));
      } else {
        out.set(e.id, e.is_loop ? 46 : 0);
      }
    }
    return out;
  }

  function edgeTooltip(e) {
    const rev = isReversedDir();
    const a = rev ? entityLabel(e.to) : entityLabel(e.from);
    const b = rev ? entityLabel(e.from) : entityLabel(e.to);
    const cond = e.condition || {};
    const cText = cond.type && cond.type !== "always" ? ` · 조건: ${cond.type}${cond.value ? `(${cond.value})` : ""}` : "";
    const lText = e.is_loop ? ` · 순환 최대 ${e.max_iterations || 3}회` : "";
    const tText = (e.temp_prompt || "").trim() ? " · ⚡임시 프롬프트" : "";
    const modeText = rev ? "실행" : "의존";
    return `${a} → ${b} (${modeText}${lText}${cText}${tText})`;
  }

  function draw(withInsp = false) {
    const stage = $("#pg-stage");
    if (!stage || !PG.graph) return;
    syncGraphRelations();
    if (withInsp) drawInspector();
    if (PG.dragging) return;

    const now = Date.now();
    PG.flashes = PG.flashes.filter(f => now - f.t < 2600);
    const flowing = new Set(PG.flashes.map(f => `${f.from}>${f.to}`));
    const critNodes = new Set(PG.showProfileOverlay ? (PG.run?.result_json?.profile?.critical_path_nodes || []) : []);

    const allBoxes = [...PG.graph.nodes, ...PG.graph.groups];
    const W = Math.max(2400, ...allBoxes.map(b => b.x + (b.width || 300) + 500));
    const H = Math.max(1500, ...allBoxes.map(b => b.y + (b.height || 200) + 400));

    const chg = computeChangeStatus();

    // Render groups shallowest -> deepest so inner nested rectangles sit on top
    const sortedGroups = [...PG.graph.groups].sort((a, b) => groupDepth(a.id) - groupDepth(b.id));
    const groupsHtml = sortedGroups.map(g => {
      const tier = groupTier(g.id);
      const bColor = tierColor(tier);
      const on = PG.sel?.type === "group" && PG.sel.id === g.id;
      const linking = PG.pendingLinkFrom === g.id;
      const gst = chg.groups[g.id]?.state || (g.completed ? "clean" : "unexecuted");
      const stCls = gst === "modified" ? "st-modified" : gst === "needs_rerun" ? "st-needs-rerun" : "";
      const borderStyle = (gst === "modified" || gst === "needs_rerun") ? "" : `border-color:${bColor};`;
      return `<div class="pg-group ${stCls} ${on ? "on" : ""} ${linking ? "linking" : ""}" data-pg-group="${esc(g.id)}"
        style="left:${g.x}px;top:${g.y}px;width:${g.width}px;height:${g.height}px;${borderStyle}">
        <div class="pg-group-title">${esc(g.label)}</div>
        <div class="pg-resize-handle" data-pg-resize="${esc(g.id)}" title="드래그해 그룹 크기 조절"></div>
      </div>`;
    }).join("");

    const nodesHtml = PG.graph.nodes.map(n => {
      const s = nodeState(n.id);
      const on = PG.sel?.type === "node" && PG.sel.id === n.id;
      const linking = PG.pendingLinkFrom === n.id;
      const nst = chg.nodes[n.id]?.state || (n.completed ? "clean" : "unexecuted");
      const stCls = nst === "modified" ? "st-modified" : nst === "needs_rerun" ? "st-needs-rerun" : "";
      const isCrit = critNodes.has(n.id);
      const tok = s?.usage?.total_tokens || 0;
      const profBadge = PG.showProfileOverlay && s && (s.ms || tok || s.cache_hit)
        ? `<div class="pg-prof-badge">${s.cache_hit ? "CACHE 0ms" : `${s.ms || 0}ms · ${tok}t`}</div>`
        : "";
      return `<div class="pg-node ${stCls} ${on ? "on" : ""} ${linking ? "linking" : ""} ${isCrit ? "pg-crit" : ""} ${s ? "s-" + esc(s.status) : ""}" data-pg-node="${esc(n.id)}"
        style="left:${n.x}px;top:${n.y}px;width:${n.width || 250}px;height:${n.height || 145}px">
        <div class="pg-node-title">${esc(n.label)}</div>
        ${profBadge}
      </div>`;
    }).join("");

    const curveMap = edgeCurveOffsets(PG.graph.edges);
    const edgesHtml = PG.graph.edges.map(e => {
      const ep = visualEdgeEndpoints(e);
      const a = findEntity(ep.from), b = findEntity(ep.to);
      if (!a || !b) return "";
      const p = edgePath(a, b, { curveOffset: curveMap.get(e.id) || 0 });
      const isGrp = !!(findGroup(e.from) || findGroup(e.to));
      const isLoop = Boolean(e.is_loop);
      const isCond = !isLoop && (e.condition?.type && e.condition.type !== "always");
      const on = PG.sel?.type === "edge" && PG.sel.id === e.id;
      const isCritEdge = PG.showProfileOverlay && critNodes.has(e.from) && critNodes.has(e.to);
      const color = isLoop ? "#f59e0b" : isCond ? "#10b981" : isGrp ? "#8b5cf6" : "#0ea5e9";
      const marker = isLoop ? "pg-arr-loop" : isCond ? "pg-arr-cond" : isGrp ? "pg-arr-grp" : "pg-arr-node";
      const cls = ["pg-edge", isGrp ? "grp" : "", isCond ? "cond" : "", isLoop ? "loop" : "", isCritEdge ? "crit" : "", on ? "on" : "", flowing.has(`${e.from}>${e.to}`) ? "flow" : ""].filter(Boolean).join(" ");
      return `<path class="${cls}" d="${p.d}" stroke="${color}" style="color:${color}" marker-end="url(#${marker})"/>
        <path class="pg-edge-hit" data-pg-edge="${esc(e.id)}" d="${p.d}"><title>${esc(edgeTooltip(e))}</title></path>`;
    }).join("");

    const markers = [
      ["pg-arr-node", "#0ea5e9"],
      ["pg-arr-grp", "#8b5cf6"],
      ["pg-arr-cond", "#10b981"],
      ["pg-arr-loop", "#f59e0b"],
    ].map(([id, c]) => `<marker id="${id}" viewBox="0 0 10 10" refX="9" refY="5" markerUnits="userSpaceOnUse" markerWidth="14" markerHeight="14" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="${c}"/></marker>`).join("");

    if (PG.fitPending && stage.clientWidth && allBoxes.length) {
      const maxX = Math.max(...allBoxes.map(b => b.x + (b.width || 260))) + 80;
      const maxY = Math.max(...allBoxes.map(b => b.y + (b.height || 160))) + 80;
      PG.zoom = Math.max(0.35, Math.min(1.15, (stage.clientWidth - 20) / maxX, (stage.clientHeight - 20) / maxY));
      PG.fitPending = false;
    }
    const z = PG.zoom || 1;
    const zl = $("#pg-zoomlbl");
    if (zl) zl.textContent = `${Math.round(z * 100)}%`;
    updateInspTabBadges(chg);

    stage.classList.toggle("wrap-mode", !!PG.wrapMode);
    stage.classList.toggle("link-mode", !!PG.linkMode);
    stage.innerHTML = `<div style="width:${W * z}px;height:${H * z}px">
      <div class="pg-board" id="pg-board" style="width:${W}px;height:${H}px;transform:scale(${z});transform-origin:0 0">
        ${groupsHtml}
        ${nodesHtml}
        <svg class="pg-svg" width="${W}" height="${H}">
          <defs>${markers}</defs>
          ${edgesHtml}
          <path id="pg-temp" class="pg-edge" stroke="var(--accent)" stroke-dasharray="5 4" d=""/>
        </svg>
        <div id="pg-wrap-box" class="pg-wrap-box"></div>
      </div>
    </div>`;
    bindBoard();
  }

  function redrawEdgesOnly() {
    const svg = $("#pg-board svg");
    if (!svg) return;
    const curveMap = edgeCurveOffsets(PG.graph.edges);
    const parts = PG.graph.edges.map(e => {
      const ep = visualEdgeEndpoints(e);
      const a = findEntity(ep.from), b = findEntity(ep.to);
      return a && b ? edgePath(a, b, { curveOffset: curveMap.get(e.id) || 0 }) : null;
    });
    const paths = svg.querySelectorAll(".pg-edge:not(#pg-temp)");
    const hits = svg.querySelectorAll(".pg-edge-hit");
    parts.forEach((p, i) => {
      if (!p) return;
      paths[i]?.setAttribute("d", p.d);
      hits[i]?.setAttribute("d", p.d);
    });
  }

  function bindBoard() {
    const board = $("#pg-board");
    if (!board) return;
    let drag = null;
    const pt = e => {
      const r = board.getBoundingClientRect();
      const z = PG.zoom || 1;
      return { x: (e.clientX - r.left) / z, y: (e.clientY - r.top) / z };
    };

    board.onpointerdown = e => {
      const runNodeBtn = e.target.closest("[data-pg-run-node]");
      if (runNodeBtn) {
        e.stopPropagation();
        run(startRun)("node", runNodeBtn.dataset.pgRunNode);
        return;
      }
      const runGrpBtn = e.target.closest("[data-pg-run-group]");
      if (runGrpBtn) {
        e.stopPropagation();
        run(startRun)("group", runGrpBtn.dataset.pgRunGroup);
        return;
      }
      const wrapEntBtn = e.target.closest("[data-pg-wrap-entity]");
      if (wrapEntBtn) {
        e.stopPropagation();
        wrapEntityInParentGroup(wrapEntBtn.dataset.pgWrapEntity);
        return;
      }
      if (PG.pendingLinkFrom) {
        const targetEl = e.target.closest("[data-pg-node],[data-pg-group]");
        const targetId = targetEl?.dataset?.pgNode || targetEl?.dataset?.pgGroup;
        if (targetId && targetId !== PG.pendingLinkFrom) {
          e.stopPropagation();
          const src = PG.pendingLinkFrom;
          PG.pendingLinkFrom = null;
          if (!connectVisual(src, targetId)) draw(true);
          return;
        }
        if (targetId === PG.pendingLinkFrom) {
          e.stopPropagation();
          PG.pendingLinkFrom = null;
          draw(true);
          return;
        }
      }
      const outPort = e.target.closest("[data-pg-out]");
      if (outPort) {
        e.stopPropagation();
        board.setPointerCapture(e.pointerId);
        const ent = findEntity(outPort.dataset.pgOut);
        const p = pt(e);
        drag = {
          mode: "link",
          from: ent.id,
          ent,
          sx: p.x,
          sy: p.y,
          moved: false,
        };
        PG.dragging = true;
        return;
      }
      if ((PG.linkMode && !e.shiftKey) || e.altKey) {
        const linkEl = e.target.closest("[data-pg-node],[data-pg-group]");
        const linkId = linkEl?.dataset?.pgNode || linkEl?.dataset?.pgGroup;
        if (linkId) {
          e.stopPropagation();
          board.setPointerCapture(e.pointerId);
          const ent = findEntity(linkId);
          const p = pt(e);
          drag = {
            mode: "link",
            from: ent.id,
            ent,
            sx: p.x,
            sy: p.y,
            moved: false,
          };
          PG.dragging = true;
          return;
        }
      }
      const resizeHandle = e.target.closest("[data-pg-resize]");
      if (resizeHandle) {
        e.stopPropagation();
        board.setPointerCapture(e.pointerId);
        const grp = findGroup(resizeHandle.dataset.pgResize);
        const p = pt(e);
        PG.sel = { type: "group", id: grp.id };
        PG.inspTab = "edit";
        drag = {
          mode: "resize",
          grp,
          el: board.querySelector(`[data-pg-group="${CSS.escape(grp.id)}"]`),
          sx: p.x,
          sy: p.y,
          w0: grp.width,
          h0: grp.height,
          before: JSON.stringify(PG.graph),
          moved: false,
        };
        PG.dragging = true;
        return;
      }
      const edgeHit = e.target.closest("[data-pg-edge]");
      if (edgeHit) {
        PG.sel = { type: "edge", id: edgeHit.dataset.pgEdge };
        PG.inspTab = "edit";
        draw(true);
        return;
      }
      if (PG.wrapMode || e.shiftKey) {
        board.setPointerCapture(e.pointerId);
        const p = pt(e);
        drag = { mode: "wrap", sx: p.x, sy: p.y, cx: p.x, cy: p.y };
        PG.dragging = true;
        return;
      }
      const nodeEl = e.target.closest("[data-pg-node]");
      if (nodeEl) {
        const n = findNode(nodeEl.dataset.pgNode);
        PG.sel = { type: "node", id: n.id };
        PG.inspTab = "edit";
        board.querySelectorAll(".pg-node,.pg-group").forEach(x => x.classList.toggle("on", x === nodeEl));
        drawInspector();
        board.setPointerCapture(e.pointerId);
        const p = pt(e);
        drag = { mode: "move-node", n, el: nodeEl, dx: p.x - n.x, dy: p.y - n.y, moved: false, before: JSON.stringify(PG.graph) };
        PG.dragging = true;
        return;
      }
      const grpEl = e.target.closest("[data-pg-group]");
      if (grpEl) {
        const g = findGroup(grpEl.dataset.pgGroup);
        PG.sel = { type: "group", id: g.id };
        PG.inspTab = "edit";
        board.querySelectorAll(".pg-node,.pg-group").forEach(x => x.classList.toggle("on", x === grpEl));
        drawInspector();
        board.setPointerCapture(e.pointerId);
        const p = pt(e);
        drag = {
          mode: "move-group",
          g,
          lastX: g.x,
          lastY: g.y,
          dx: p.x - g.x,
          dy: p.y - g.y,
          moved: false,
          before: JSON.stringify(PG.graph),
        };
        PG.dragging = true;
        return;
      }

      if (PG.sel || PG.pendingLinkFrom) {
        PG.sel = null;
        PG.pendingLinkFrom = null;
        draw(true);
      }
    };

    board.onpointermove = e => {
      if (!drag) return;
      const p = pt(e);
      if (drag.mode === "move-node") {
        drag.n.x = Math.max(0, Math.round((p.x - drag.dx) / 10) * 10);
        drag.n.y = Math.max(0, Math.round((p.y - drag.dy) / 10) * 10);
        drag.el.style.left = `${drag.n.x}px`;
        drag.el.style.top = `${drag.n.y}px`;
        drag.moved = true;
        redrawEdgesOnly();
      } else if (drag.mode === "move-group") {
        const nx = Math.max(0, Math.round((p.x - drag.dx) / 10) * 10);
        const ny = Math.max(0, Math.round((p.y - drag.dy) / 10) * 10);
        const ddx = nx - drag.lastX;
        const ddy = ny - drag.lastY;
        if (ddx || ddy) {
          translateGroupTree(drag.g.id, ddx, ddy);
          drag.lastX = nx;
          drag.lastY = ny;
          drag.moved = true;
          for (const sid of subtreeIds(drag.g.id)) {
            const se = findEntity(sid);
            const sel = board.querySelector(`[data-pg-node="${CSS.escape(sid)}"],[data-pg-group="${CSS.escape(sid)}"]`);
            if (se && sel) { sel.style.left = `${se.x}px`; sel.style.top = `${se.y}px`; }
          }
          redrawEdgesOnly();
        }
      } else if (drag.mode === "resize") {
        drag.grp.width = Math.max(200, Math.round((drag.w0 + p.x - drag.sx) / 10) * 10);
        drag.grp.height = Math.max(130, Math.round((drag.h0 + p.y - drag.sy) / 10) * 10);
        if (drag.el) {
          drag.el.style.width = `${drag.grp.width}px`;
          drag.el.style.height = `${drag.grp.height}px`;
        }
        drag.moved = true;
        redrawEdgesOnly();
      } else if (drag.mode === "link") {
        if (!drag.moved && Math.hypot(p.x - drag.sx, p.y - drag.sy) >= 6) drag.moved = true;
        if (drag.moved) {
          const p1 = rectBorderPoint(drag.ent, p.x, p.y, 1);
          const dist = Math.hypot(p.x - p1.x, p.y - p1.y);
          const bend = Math.min(80, Math.max(20, dist * 0.32));
          const c1x = p1.x + p1.nx * bend;
          const c1y = p1.y + p1.ny * bend;
          const c2x = p.x - (p.x - p1.x) * 0.25;
          const c2y = p.y - (p.y - p1.y) * 0.25;
          $("#pg-temp")?.setAttribute("d", `M${p1.x},${p1.y} C${c1x},${c1y} ${c2x},${c2y} ${p.x},${p.y}`);
        }
      } else if (drag.mode === "wrap") {
        drag.cx = p.x;
        drag.cy = p.y;
        const box = $("#pg-wrap-box");
        if (box) {
          const x = Math.min(drag.sx, drag.cx), y = Math.min(drag.sy, drag.cy);
          const w = Math.abs(drag.cx - drag.sx), h = Math.abs(drag.cy - drag.sy);
          box.style.display = "block";
          box.style.left = `${x}px`;
          box.style.top = `${y}px`;
          box.style.width = `${w}px`;
          box.style.height = `${h}px`;
        }
      }
    };

    board.onpointerup = e => {
      if (!drag) return;
      const d = drag;
      drag = null;
      PG.dragging = false;

      if (d.mode === "link") {
        $("#pg-temp")?.setAttribute("d", "");
        if (d.moved) {
          const el = document.elementFromPoint(e.clientX, e.clientY)?.closest("[data-pg-node],[data-pg-group]");
          const targetId = el?.dataset?.pgNode || el?.dataset?.pgGroup;
          if (targetId && targetId !== d.from) connectVisual(d.from, targetId);
        } else {
          PG.pendingLinkFrom = PG.pendingLinkFrom === d.from ? null : d.from;
          draw(true);
        }
        return;
      }

      if (d.mode === "wrap") {
        const box = $("#pg-wrap-box");
        if (box) box.style.display = "none";
        const rx = Math.min(d.sx, d.cx), ry = Math.min(d.sy, d.cy);
        const rw = Math.max(240, Math.abs(d.cx - d.sx)), rh = Math.max(150, Math.abs(d.cy - d.sy));
        if (Math.abs(d.cx - d.sx) < 30 && Math.abs(d.cy - d.sy) < 30) {
          draw();
          return;
        }
        const inside = ent => {
          const cx = ent.x + (ent.width || 250) / 2;
          const cy = ent.y + (ent.height || 140) / 2;
          return cx >= rx && cx <= rx + rw && cy >= ry && cy <= ry + rh;
        };
        const enclosedGroups = PG.graph.groups.filter(g => inside(g) && g.width * g.height < rw * rh);
        const enclosedGroupSet = new Set(enclosedGroups.map(g => g.id));
        const topGroups = enclosedGroups.filter(g => !g.parent_group_id || !enclosedGroupSet.has(g.parent_group_id));
        const looseNodes = PG.graph.nodes.filter(n => inside(n) && (!n.parent_group_id || !enclosedGroupSet.has(n.parent_group_id)));

        // Determine smallest outer enclosing group if the rectangle is drawn inside larger groups
        const cx = rx + rw / 2, cy = ry + rh / 2;
        const parentCandidates = PG.graph.groups
          .filter(g => !enclosedGroupSet.has(g.id) && cx >= g.x && cx <= g.x + g.width && cy >= g.y && cy <= g.y + g.height && g.width * g.height > rw * rh)
          .sort((a, b) => (a.width * a.height) - (b.width * b.height));
        PG.wrapMode = false;
        const btn = $("#pg-wrap-btn");
        if (btn) btn.classList.remove("on");
        const created = addPrePromptGroup(rx, ry, rw, rh, {
          parent_group_id: parentCandidates[0]?.id || null,
          adoptNodes: looseNodes.map(n => n.id),
          adoptGroups: topGroups.map(g => g.id),
        });
        if (looseNodes.length || topGroups.length) {
          fitGroupBounds(created.id);
          draw(true);
        }
        return;
      }

      if (d.moved) {
        if (d.mode === "move-node") {
          assignParentByGeometry(d.n.id);
        } else if (d.mode === "move-group") {
          assignParentByGeometry(d.g.id);
        } else if (d.mode === "resize") {
          // Re-evaluate containment from largest outer groups inward to keep arbitrarily deep hierarchies intact
          const byAreaDesc = [...PG.graph.groups].sort((a, b) => (b.width * b.height) - (a.width * a.height));
          byAreaDesc.forEach(g => { if (g.id !== d.grp.id) assignParentByGeometry(g.id); });
          PG.graph.nodes.forEach(n => assignParentByGeometry(n.id));
        }
        if (d.before) {
          PG.undo.push(d.before);
          if (PG.undo.length > 60) PG.undo.shift();
          PG.redo = [];
        }
        touch();
      }
      draw(true);
    };

    board.onpointercancel = () => { drag = null; PG.dragging = false; draw(); };
    board.ondblclick = e => {
      if (e.target.closest("[data-pg-node]")) return;
      const grpEl = e.target.closest("[data-pg-group]");
      const p = pt(e);
      addPromptNode(p.x - 125, p.y - 70, grpEl?.dataset?.pgGroup || null);
    };
  }

  // ------------------------------------------------------------ inspector & changes
  function countActionable(chg) {
    const c = chg || computeChangeStatus();
    let total = 0;
    for (const v of Object.values(c.nodes || {})) if (v.needs_rerun) total += 1;
    for (const v of Object.values(c.groups || {})) if (v.needs_rerun) total += 1;
    return total;
  }

  function updateInspTabBadges(chg) {
    const badge = $("#pg-tab-chg-cnt");
    if (!badge) return;
    const cnt = countActionable(chg);
    badge.textContent = String(cnt);
    badge.classList.toggle("has", cnt > 0);
  }

  function manualSetCompleted(entityId, checked) {
    const ent = findEntity(entityId);
    if (!ent) return false;
    if (checked) {
      const sub = subtreeIds(entityId);
      for (const e of PG.graph.edges) {
        if (sub.has(e.from)) {
          for (const y of subtreeIds(e.to)) {
            if (!isEntityCompleted(y)) {
              toast(`선행 의존 대상(${entityLabel(y)})이 먼저 실행 완료되어야 합니다.`, true);
              return false;
            }
          }
        }
      }
      snapshotUndo();
      ent.completed = true;
      ent.needs_rerun = false;
      ent.rerun_reason = "";
      if (findNode(entityId)) {
        ent.last_run_prompt = ent.prompt || "";
        ent.last_run_temp_prompt = "";
        const mp = {};
        ancestorGroups(entityId).forEach(ag => { mp[ag.id] = { pre: ag.pre_prompt || "", temp: "" }; });
        ent.last_run_pre_prompts = mp;
      } else {
        ent.last_run_pre_prompt = ent.pre_prompt || "";
        ent.last_run_temp_pre_prompt = "";
      }
      touch();
      return true;
    }

    snapshotUndo();
    const cleared = [];
    const q = [entityId];
    const seen = new Set();
    while (q.length) {
      const cur = q.shift();
      if (seen.has(cur)) continue;
      seen.add(cur);
      const item = findEntity(cur);
      if (!item) continue;
      const wasComp = Boolean(item.completed);
      item.completed = false;
      item.needs_rerun = false;
      item.rerun_reason = "";
      if (findNode(cur)) {
        item.last_run_prompt = null;
        item.last_run_temp_prompt = null;
        item.last_run_pre_prompts = null;
      } else {
        item.last_run_pre_prompt = null;
        item.last_run_temp_pre_prompt = null;
      }
      if (cur !== entityId && wasComp) cleared.push(cur);
      for (const agrp of ancestorGroups(cur)) {
        if (agrp.completed && !seen.has(agrp.id)) q.push(agrp.id);
      }
      for (const ed of PG.graph.edges) {
        if (subtreeIds(ed.to).has(cur)) {
          for (const x of subtreeIds(ed.from)) {
            if (!seen.has(x) && isEntityCompleted(x)) q.push(x);
          }
        }
      }
    }
    touch();
    if (cleared.length) {
      toast(`완료 해제에 따라 의존/상위 대상 ${cleared.length}개도 미실행 상태로 전환되었습니다.`);
    }
    return true;
  }

  function renderChangeCallout(entry, clrBtnId = "") {
    if (!entry) return "";
    const clrHtml = (clrBtnId && entry.reasons?.includes("agent_marked_rerun"))
      ? `<div class="row" style="margin-top:2px"><button type="button" class="g" id="${esc(clrBtnId)}" style="padding:1px 7px;font-size:11px">재실행 필요 표시 해제</button></div>`
      : "";
    if (entry.state === "clean") {
      return `<div class="pg-chg-callout st-clean"><b>✓ 실행 완료 (최신 상태)</b><span class="pg-hint">변경 사항이 없어 전체/의존 실행 시 에이전트 호출이 생략됩니다.</span></div>`;
    }
    if (entry.state === "modified") {
      return `<div class="pg-chg-callout st-modified"><b>M 변경 감지됨 (재실행 대상)</b><span class="pg-hint">${esc((entry.details || []).join(" · "))}</span>${clrHtml}</div>`;
    }
    if (entry.state === "needs_rerun") {
      return `<div class="pg-chg-callout st-needs-rerun"><b>↻ 재실행 필요 (에이전트/상위 변경)</b><span class="pg-hint">${esc((entry.details || []).join(" · "))}</span>${clrHtml}</div>`;
    }
    return `<div class="pg-chg-callout"><b>○ 미실행</b><span class="pg-hint">아직 실행 완료되지 않은 대상입니다.</span></div>`;
  }

  function renderLineDiff(oldStr, newStr) {
    const a = String(oldStr || "").split("\n");
    const b = String(newStr || "").split("\n");
    const out = [];
    const maxLen = Math.max(a.length, b.length);
    for (let i = 0; i < Math.min(maxLen, 160); i += 1) {
      const la = i < a.length ? a[i] : null;
      const lb = i < b.length ? b[i] : null;
      if (la === lb) {
        out.push(`<div>  ${esc(la ?? "")}</div>`);
      } else {
        if (la !== null) out.push(`<span class="pg-diff-del">- ${esc(la)}</span>`);
        if (lb !== null) out.push(`<span class="pg-diff-add">+ ${esc(lb)}</span>`);
      }
    }
    if (maxLen > 160) out.push(`<div class="muted">… (${maxLen - 160}줄 생략)</div>`);
    return `<div class="pg-diff">${out.join("") || `<span class="muted">(내용 동일)</span>`}</div>`;
  }

  function drawChangesPanel(bodyEl, chg) {
    const items = [];
    const cleanItems = [];
    for (const g of PG.graph.groups) {
      const info = chg.groups[g.id] || { state: "unexecuted", needs_rerun: true, details: ["미실행"] };
      const rec = { kind: "group", ent: g, ...info };
      if (info.needs_rerun) items.push(rec);
      else cleanItems.push(rec);
    }
    for (const n of PG.graph.nodes) {
      const info = chg.nodes[n.id] || { state: "unexecuted", needs_rerun: true, details: ["미실행"] };
      const rec = { kind: "node", ent: n, ...info };
      if (info.needs_rerun) items.push(rec);
      else cleanItems.push(rec);
    }

    const activeIds = new Set(items.map(x => x.ent.id));
    for (const id of [...PG.changeSel]) {
      if (!activeIds.has(id)) PG.changeSel.delete(id);
    }
    for (const id of [...PG.changeSeen]) {
      if (!activeIds.has(id)) PG.changeSeen.delete(id);
    }
    for (const id of activeIds) {
      if (!PG.changeSeen.has(id)) {
        PG.changeSeen.add(id);
        PG.changeSel.add(id);
      }
    }

    const orderRank = { modified: 0, needs_rerun: 1, unexecuted: 2 };
    items.sort((a, b) => (orderRank[a.state] ?? 9) - (orderRank[b.state] ?? 9) || (a.kind === b.kind ? 0 : a.kind === "group" ? -1 : 1));

    const modCnt = items.filter(x => x.state === "modified").length;
    const rerunCnt = items.filter(x => x.state === "needs_rerun").length;
    const unexecCnt = items.filter(x => x.state === "unexecuted").length;
    const cleanCnt = cleanItems.length;
    const selectedIds = items.filter(x => PG.changeSel.has(x.ent.id)).map(x => x.ent.id);

    const badgeFor = st => st === "modified"
      ? `<span class="chip chg-mod">M 변경</span>`
      : st === "needs_rerun"
        ? `<span class="chip chg-rerun">↻ 재실행 필요</span>`
        : `<span class="chip done-no">○ 미실행</span>`;

    bodyEl.innerHTML = `<div class="pg-chg-panel">
      <div class="pg-chg-summary">
        <div class="pg-chg-stat"><b>${modCnt}</b>변경됨</div>
        <div class="pg-chg-stat"><b>${rerunCnt}</b>재실행</div>
        <div class="pg-chg-stat"><b>${unexecCnt}</b>미실행</div>
        <div class="pg-chg-stat"><b style="color:var(--ok)">${cleanCnt}</b>최신</div>
      </div>
      <div class="row" style="gap:6px">
        <button type="button" class="b" id="pg-chg-run-all" style="flex:1" ${items.length ? "" : "disabled"}>▶ 전체 실행 (${items.length})</button>
        <button type="button" class="g" id="pg-chg-run-sel" style="flex:1" ${selectedIds.length ? "" : "disabled"}>▶ 선택 부분 실행 (${selectedIds.length})</button>
      </div>
      <div class="row" style="justify-content:space-between;gap:4px">
        <button type="button" class="g" id="pg-chg-sync" style="padding:2px 8px;font-size:11px" title="연결된 Repo 파일/Spec 변경을 감지해 재실행 필요 상태로 반영">🔄 산출물 동기화</button>
        <div class="row" style="gap:4px">
          <button type="button" class="g" id="pg-chg-all" style="padding:2px 7px;font-size:11px">전체</button>
          <button type="button" class="g" id="pg-chg-none" style="padding:2px 7px;font-size:11px">해제</button>
          <button type="button" class="g" id="pg-chg-dirty" style="padding:2px 7px;font-size:11px">변경만</button>
        </div>
      </div>
      <label class="row" style="font-size:12px">
        <input type="checkbox" id="pg-chg-autorun" ${PG.graph.settings?.auto_rerun_needs_rerun ? "checked" : ""}>
        <span>재실행 필요 노드 자동 재실행</span>
      </label>
      <label class="row" style="font-size:12px">
        <input type="checkbox" id="pg-chg-cache" ${PG.graph.settings?.cross_graph_cache ? "checked" : ""}>
        <span>동일 입력 해시 크로스-그래프 캐시 사용</span>
      </label>
      ${items.length ? `<div style="display:flex;flex-direction:column;gap:6px">
        ${items.map(it => {
          const id = it.ent.id;
          const ic = it.kind === "group" ? "⬚" : "◈";
          const latestImp = Array.isArray(it.ent.impact_history) && it.ent.impact_history.length ? it.ent.impact_history[0] : null;
          return `<div class="pg-chg-item st-${esc(it.state)}">
            <div class="top">
              <input type="checkbox" data-pg-chg-chk="${esc(id)}" ${PG.changeSel.has(id) ? "checked" : ""}>
              <span>${ic}</span>
              <span class="name" title="${esc(it.ent.label)} (${esc(id)})">${esc(it.ent.label)}</span>
              ${badgeFor(it.state)}
            </div>
            <div class="pg-chg-reason"><code>${esc(id)}</code> · ${esc((it.details || []).join(" · "))}</div>
            ${latestImp?.reason ? `<div class="pg-hint" style="color:#b45309">근거 (${esc(latestImp.source_node_id || "")}): ${esc(latestImp.reason)}</div>` : ""}
            <div class="row" style="gap:4px;flex-wrap:wrap">
              <button type="button" class="g" data-pg-chg-run="${esc(it.kind)}:${esc(id)}" style="padding:1px 7px;font-size:11px">▶ 승인/실행</button>
              ${it.ent.needs_rerun ? `<button type="button" class="g" data-pg-chg-dismiss="${esc(id)}" style="padding:1px 7px;font-size:11px">✓ 무시(유지)</button>` : ""}
              <button type="button" class="g" data-pg-chg-edit="${esc(it.kind)}:${esc(id)}" style="padding:1px 7px;font-size:11px">속성</button>
            </div>
          </div>`;
        }).join("")}
      </div>` : `<div class="pg-chg-callout st-clean"><b>✓ 모든 노드와 그룹이 최신 상태입니다</b><span class="pg-hint">프롬프트·임시 프롬프트 변경이 없어 추가 실행이 필요하지 않습니다.</span></div>`}
      ${PG.graphId ? `<details style="border-top:1px solid var(--line);padding-top:8px">
        <summary style="cursor:pointer;font-size:12px;color:var(--muted)" id="pg-rev-sum">⚑ MCP 영향도 판단 감사 로그</summary>
        <div id="pg-rev-list" style="margin-top:6px;display:flex;flex-direction:column;gap:4px"><span class="muted" style="font-size:11.5px">클릭해 불러오기…</span></div>
      </details>` : ""}
      ${cleanItems.length ? `<details style="border-top:1px solid var(--line);padding-top:8px">
        <summary style="cursor:pointer;font-size:12px;color:var(--muted)">✓ 최신 완료 (${cleanItems.length}개 — 실행 시 자동 생략)</summary>
        <div style="display:flex;flex-direction:column;gap:5px;margin-top:6px">
          ${cleanItems.map(it => `<div class="pg-chg-item">
            <div class="top">
              <span>${it.kind === "group" ? "⬚" : "◈"}</span>
              <span class="name">${esc(it.ent.label)}</span>
              <span class="muted mono" style="font-size:10.5px">${esc(it.ent.id)}</span>
              <button type="button" class="g" data-pg-chg-edit="${esc(it.kind)}:${esc(it.ent.id)}" style="padding:1px 6px;font-size:11px">편집</button>
            </div>
          </div>`).join("")}
        </div>
      </details>` : ""}
    </div>`;

    $("#pg-chg-run-all").onclick = run(() => startRun("all", null));
    $("#pg-chg-run-sel").onclick = run(() => {
      const ids = [...PG.changeSel].filter(id => activeIds.has(id));
      if (!ids.length) return toast("실행할 대상을 선택하세요.", true);
      return startRun("subset", null, ids);
    });
    $("#pg-chg-sync").onclick = run(async () => {
      syncGraphRelations();
      const res = await api("POST", `${WSP()}/prompt-graphs/sync-artifacts`, { graph_id: PG.graphId || null, graph: PG.graph });
      PG.graph = ensureArrays(res.graph);
      const cnt = res.changed_nodes?.length || 0;
      toast(`산출물 ${res.checked || 0}건 확인 · 변경 감지 ${cnt}건`);
      draw(true);
    });
    $("#pg-chg-all").onclick = () => { items.forEach(x => PG.changeSel.add(x.ent.id)); drawChangesPanel(bodyEl, computeChangeStatus()); };
    $("#pg-chg-none").onclick = () => { PG.changeSel.clear(); drawChangesPanel(bodyEl, computeChangeStatus()); };
    $("#pg-chg-dirty").onclick = () => {
      PG.changeSel.clear();
      items.filter(x => x.state === "modified" || x.state === "needs_rerun").forEach(x => PG.changeSel.add(x.ent.id));
      drawChangesPanel(bodyEl, computeChangeStatus());
    };
    $("#pg-chg-autorun").onchange = ev => {
      snapshotUndo();
      (PG.graph.settings || (PG.graph.settings = {})).auto_rerun_needs_rerun = ev.target.checked;
      touch();
    };
    $("#pg-chg-cache").onchange = ev => {
      snapshotUndo();
      (PG.graph.settings || (PG.graph.settings = {})).cross_graph_cache = ev.target.checked;
      touch();
    };
    bodyEl.querySelectorAll("[data-pg-chg-chk]").forEach(cb => {
      cb.onchange = () => {
        if (cb.checked) PG.changeSel.add(cb.dataset.pgChgChk);
        else PG.changeSel.delete(cb.dataset.pgChgChk);
        const btn = $("#pg-chg-run-sel");
        const cnt = items.filter(x => PG.changeSel.has(x.ent.id)).length;
        if (btn) { btn.disabled = cnt === 0; btn.textContent = `▶ 선택 부분 실행 (${cnt})`; }
      };
    });
    bodyEl.querySelectorAll("[data-pg-chg-run]").forEach(btn => {
      btn.onclick = () => {
        const [kind, id] = btn.dataset.pgChgRun.split(":");
        run(startRun)(kind, id);
      };
    });
    bodyEl.querySelectorAll("[data-pg-chg-dismiss]").forEach(btn => {
      btn.onclick = run(async () => {
        const eid = btn.dataset.pgChgDismiss;
        snapshotUndo();
        const ent = findEntity(eid);
        if (ent) { ent.needs_rerun = false; ent.rerun_reason = ""; }
        if (PG.graphId) {
          await api("POST", `${WSP()}/prompt-graphs/${PG.graphId}/impact-reviews/dismiss`, { entity_id: eid }).catch(() => null);
        }
        touch();
        draw(true);
        toast(`${eid} 재실행 필요 표시를 무시(유지)했습니다.`);
      });
    });
    bodyEl.querySelectorAll("[data-pg-chg-edit]").forEach(btn => {
      btn.onclick = () => {
        const [kind, id] = btn.dataset.pgChgEdit.split(":");
        PG.sel = { type: kind, id };
        PG.inspTab = "edit";
        draw(true);
      };
    });
    if ($("#pg-rev-sum")) {
      $("#pg-rev-sum").onclick = async () => {
        const box = $("#pg-rev-list");
        if (!box || box.dataset.loaded) return;
        box.dataset.loaded = "1";
        const res = await api("GET", `${WSP()}/prompt-graphs/${PG.graphId}/impact-reviews`, undefined, { fresh: true }).catch(() => ({ reviews: [] }));
        box.innerHTML = (res.reviews || []).map(r => `<div class="pg-chg-item">
          <div class="top">
            <span class="chip ${r.needs_rerun ? "chg-rerun" : "done-ok"}">${r.needs_rerun ? "⚑ 재실행" : "✓ 영향 없음"}</span>
            <span class="name">${esc(entityLabel(r.source_node_id))} → ${esc(entityLabel(r.target_id))}</span>
            <span class="muted" style="font-size:10.5px">${esc(r.decision)}</span>
          </div>
          <div class="pg-chg-reason">${esc(r.reason || "")}</div>
        </div>`).join("") || `<span class="muted" style="font-size:11.5px">기록된 영향도 판단이 없습니다.</span>`;
      };
    }
  }

  function drawTemplatesPanel(bodyEl) {
    bodyEl.innerHTML = `<div class="pg-chg-panel">
      <div class="ide-head" style="padding:0">🔍 워크스페이스 노드 검색</div>
      <div class="row" style="gap:6px">
        <input id="pg-tpl-search-q" value="${esc(PG.searchQuery || "")}" placeholder="노드 이름·프롬프트·출력 검색…" style="flex:1;min-width:0">
        <button type="button" class="g" id="pg-tpl-search-btn">검색</button>
      </div>
      <div id="pg-tpl-search-res" style="display:flex;flex-direction:column;gap:5px">
        ${(PG.searchResults || []).map(h => `<div class="pg-chg-item">
          <div class="top">
            <span class="name" title="${esc(h.graph_name)}">${esc(h.node_label)}</span>
            <span class="chip">${esc(h.graph_name)}</span>
          </div>
          <div class="pg-chg-reason">${esc(h.prompt_preview || "(비어 있음)")}</div>
          <button type="button" class="g" data-pg-open-hit="${esc(h.graph_id)}:${esc(h.node_id)}" style="align-self:flex-start;padding:1px 7px;font-size:11px">이동</button>
        </div>`).join("")}
      </div>
      <hr style="border:0;border-top:1px solid var(--line);margin:2px 0">
      <div class="row" style="justify-content:space-between">
        <b style="font-size:13px">📦 N-그룹 컴포넌트 라이브러리</b>
        <button type="button" class="g" id="pg-tpl-refresh" style="padding:2px 7px;font-size:11px">새로고침</button>
      </div>
      <div id="pg-tpl-list" style="display:flex;flex-direction:column;gap:6px">
        ${(PG.templates || []).map(t => `<div class="pg-chg-item">
          <div class="top">
            <span class="name">${esc(t.name)}</span>
            <span class="chip">${t.tier_depth}-그룹 · ${t.node_count}노드</span>
          </div>
          ${t.description ? `<div class="pg-chg-reason">${esc(t.description)}</div>` : ""}
          <div class="row" style="gap:6px">
            <button type="button" class="b" data-pg-tpl-ins="${esc(t.template_id)}" style="padding:2px 8px;font-size:11px;flex:1">＋ 캔버스에 삽입</button>
            <button type="button" class="g bad" data-pg-tpl-del="${esc(t.template_id)}" style="padding:2px 8px;font-size:11px">삭제</button>
          </div>
        </div>`).join("") || `<div class="pg-hint">저장된 N-그룹 컴포넌트가 없습니다. 프리 프롬프트 그룹을 선택하고 [속성] → [📦 컴포넌트로 저장]을 누르세요.</div>`}
      </div>
    </div>`;

    const loadTpls = async () => {
      const r = await api("GET", `${WSP()}/prompt-graph-templates`, undefined, { fresh: true }).catch(() => ({ templates: [] }));
      PG.templates = r.templates || [];
      if (PG.inspTab === "templates") drawTemplatesPanel(bodyEl);
    };
    if (!PG._tplLoadedOnce) { PG._tplLoadedOnce = true; loadTpls(); }
    $("#pg-tpl-refresh").onclick = run(loadTpls);
    $("#pg-tpl-search-btn").onclick = run(async () => {
      PG.searchQuery = $("#pg-tpl-search-q").value.trim();
      const r = await api("GET", `${WSP()}/prompt-graphs/search?q=${encodeURIComponent(PG.searchQuery)}`, undefined, { fresh: true });
      PG.searchResults = r.results || [];
      drawTemplatesPanel(bodyEl);
    });
    bodyEl.querySelectorAll("[data-pg-open-hit]").forEach(btn => {
      btn.onclick = () => {
        const [gid, nid] = btn.dataset.pgOpenHit.split(":");
        const targetGraph = (PG.graphs || []).find(g => String(g.graph_id) === String(gid));
        if (targetGraph && String(PG.graphId) !== String(gid)) {
          loadGraph(targetGraph.graph_json, targetGraph.graph_id, targetGraph.name);
        }
        PG.sel = { type: "node", id: nid };
        PG.inspTab = "edit";
        G.render();
      };
    });
    bodyEl.querySelectorAll("[data-pg-tpl-ins]").forEach(btn => {
      btn.onclick = run(async () => {
        syncGraphRelations();
        const tid = btn.dataset.pgTplIns;
        const r = await api("POST", `${WSP()}/prompt-graph-templates/${tid}/instantiate`, { graph: PG.graph, offset_x: 90, offset_y: 90 });
        snapshotUndo();
        PG.graph = ensureArrays(r.graph);
        PG.sel = { type: "group", id: r.inserted_group_id };
        PG.inspTab = "edit";
        touch();
        draw(true);
        toast("컴포넌트를 캔버스에 삽입했습니다.");
      });
    });
    bodyEl.querySelectorAll("[data-pg-tpl-del]").forEach(btn => {
      btn.onclick = run(async () => {
        await api("DELETE", `${WSP()}/prompt-graph-templates/${btn.dataset.pgTplDel}`);
        await loadTpls();
        toast("컴포넌트를 삭제했습니다.");
      });
    });
  }

  function drawProfilePanel(bodyEl) {
    const prof = PG.run?.result_json?.profile || null;
    const hist = PG.analytics || null;
    bodyEl.innerHTML = `<div class="pg-chg-panel">
      <label class="row" style="font-size:12px">
        <input type="checkbox" id="pg-prof-overlay" ${PG.showProfileOverlay ? "checked" : ""}>
        <span>캔버스에 임계 경로(빨간 테두리) & 성능 배지 표시</span>
      </label>
      ${prof ? `<div class="pg-chg-summary" style="grid-template-columns:repeat(2,1fr)">
        <div class="pg-chg-stat"><b>$${Number(prof.total_cost_usd || 0).toFixed(5)}</b>실행 비용</div>
        <div class="pg-chg-stat"><b>${prof.total_tokens || 0}</b>총 토큰</div>
        <div class="pg-chg-stat"><b>${prof.critical_path_ms || 0}ms</b>임계 경로 (${prof.parallelism_speedup || 1}x)</div>
        <div class="pg-chg-stat"><b style="color:var(--ok)">${prof.estimated_saved_tokens || 0}t</b>절약 (캐시 ${prof.cache_hit_count || 0} · 생략 ${prof.reused_clean_count || 0})</div>
      </div>
      <div class="pg-hint">임계 경로: ${(prof.critical_path_nodes || []).map(x => `<b>${esc(entityLabel(x))}</b>`).join(" → ") || "(없음)"}</div>` : `<div class="pg-hint">최근 실행 결과가 있으면 비용·지연시간·절약 효과가 여기에 표시됩니다.</div>`}
      <div class="row" style="justify-content:space-between;border-top:1px solid var(--line);padding-top:8px">
        <b style="font-size:12.5px">노드별 누적 실행 통계 (DB)</b>
        ${PG.graphId ? `<button type="button" class="g" id="pg-prof-refresh" style="padding:2px 7px;font-size:11px">조회</button>` : ""}
      </div>
      ${hist ? `<div style="display:flex;flex-direction:column;gap:5px">
        <div class="pg-hint">총 실행 ${hist.summary?.total_executions || 0}회 · 누적 비용 $${Number(hist.summary?.total_cost_usd || 0).toFixed(5)} · 캐시 적중 ${hist.summary?.cache_hits || 0}회</div>
        ${(hist.nodes || []).map(r => `<div class="pg-chg-item">
          <div class="top">
            <span class="name">${esc(r.node_label || r.node_id)}</span>
            <span class="mono" style="font-size:11px">$${Number(r.total_cost_usd || 0).toFixed(5)}</span>
          </div>
          <div class="pg-chg-reason">${r.executions}회 · 평균 ${r.avg_ms}ms · 평균 ${r.avg_tokens}t · 캐시 ${r.cache_hits}</div>
        </div>`).join("")}
      </div>` : `<div class="pg-hint">${PG.graphId ? "조회 버튼을 눌러 DB 통계를 불러오세요." : "그래프를 저장한 뒤 DB 누적 통계를 볼 수 있습니다."}</div>`}
    </div>`;

    $("#pg-prof-overlay").onchange = ev => {
      PG.showProfileOverlay = ev.target.checked;
      $("#pg-prof-btn")?.classList.toggle("on", PG.showProfileOverlay);
      draw(false);
    };
    if ($("#pg-prof-refresh")) {
      $("#pg-prof-refresh").onclick = run(async () => {
        PG.analytics = await api("GET", `${WSP()}/prompt-graphs/${PG.graphId}/analytics`, undefined, { fresh: true });
        drawProfilePanel(bodyEl);
      });
    }
  }

  function drawInspector() {
    const el = $("#pg-insp");
    if (!el || !PG.graph) return;
    syncGraphRelations();
    const chg = computeChangeStatus();
    const actionCnt = countActionable(chg);

    el.innerHTML = `<div class="pg-insp-tabs">
      <button type="button" class="pg-insp-tab ${PG.inspTab === "changes" ? "on" : ""}" id="pg-tab-changes">
        변경·실행 <span class="pg-tab-cnt ${actionCnt > 0 ? "has" : ""}" id="pg-tab-chg-cnt">${actionCnt}</span>
      </button>
      <button type="button" class="pg-insp-tab ${PG.inspTab === "edit" ? "on" : ""}" id="pg-tab-edit">속성</button>
      <button type="button" class="pg-insp-tab ${PG.inspTab === "templates" ? "on" : ""}" id="pg-tab-templates">컴포넌트</button>
      <button type="button" class="pg-insp-tab ${PG.inspTab === "profile" ? "on" : ""}" id="pg-tab-profile">프로파일</button>
    </div><div id="pg-insp-body"></div>`;

    $("#pg-tab-changes").onclick = () => { PG.inspTab = "changes"; drawInspector(); };
    $("#pg-tab-edit").onclick = () => { PG.inspTab = "edit"; drawInspector(); };
    $("#pg-tab-templates").onclick = () => { PG.inspTab = "templates"; drawInspector(); };
    $("#pg-tab-profile").onclick = () => { PG.inspTab = "profile"; drawInspector(); };

    const bodyEl = $("#pg-insp-body");
    if (PG.inspTab === "changes") { drawChangesPanel(bodyEl, chg); return; }
    if (PG.inspTab === "templates") { drawTemplatesPanel(bodyEl); return; }
    if (PG.inspTab === "profile") { drawProfilePanel(bodyEl); return; }

    const sel = PG.sel;

    if (sel?.type === "edge") {
      const e = PG.graph.edges.find(x => x.id === sel.id);
      if (!e) { PG.sel = null; return drawInspector(); }
      const fromEnt = findEntity(e.from), toEnt = findEntity(e.to);
      const rev = isReversedDir();
      const cond = e.condition || defaultCondition();
      bodyEl.innerHTML = `<div class="ide-head">${e.is_loop ? "↺ 조건부 순환/재귀 간선" : (rev ? `실행 순서 · ${esc(e.to)} → ${esc(e.from)}` : `의존 연결 · ${esc(e.from)} → ${esc(e.to)}`)}</div>
        <div class="g-form">
          <div class="card" style="margin:0">
            <b>${esc(rev ? (toEnt?.label || e.to) : (fromEnt?.label || e.from))}</b> <span class="muted">(${esc(rev ? e.to : e.from)})</span>
            <div class="pg-hint" style="margin:6px 0">${e.is_loop
              ? (rev ? "↺ 실행 완료 후 조건 충족 시 아래 대상을 다시 실행:" : "↺ 아래 대상 완료 후 조건 충족 시 이 대상을 다시 실행:")
              : (rev ? "→ 먼저 실행된 후 아래 대상이 이어 실행됨:" : "→ 아래 대상에 의존 (선행 완료 후 실행):")}</div>
            <b>${esc(rev ? (fromEnt?.label || e.from) : (toEnt?.label || e.to))}</b> <span class="muted">(${esc(rev ? e.from : e.to)})</span>
          </div>
          <label class="row" style="font-size:12.5px">
            <input type="checkbox" id="pg-e-loop" ${e.is_loop ? "checked" : ""}>
            <span>조건부 순환 / 재귀 간선 (<code>is_loop</code>)</span>
          </label>
          <div class="row" style="gap:8px">
            <label class="fld" style="flex:1"><span>조건 유형</span>
              <select id="pg-e-ctype">
                ${[["always","항상 실행"],["on_contract_pass","출력 계약 통과 시"],["on_contract_fail","출력 계약 실패 시"],["contains","출력 포함 문자열"],["not_contains","출력 미포함 문자열"],["regex","정규식 매치"],["json_eq","JSON 키=값 일치"],["agent_decide","에이전트 MCP 판단"]].map(([k, lbl]) => `<option value="${k}" ${cond.type === k ? "selected" : ""}>${lbl} (${k})</option>`).join("")}
              </select>
            </label>
            <label class="fld" style="width:90px"><span>최대 반복</span>
              <input id="pg-e-maxiter" type="number" min="1" max="20" value="${e.max_iterations || 3}" ${e.is_loop ? "" : "disabled"}>
            </label>
          </div>
          <label class="fld"><span>조건 값 (문자열 / 정규식 / key=val)</span>
            <input id="pg-e-cval" value="${esc(cond.value || "")}" placeholder="예: REVISE 또는 status=retry">
          </label>
          <label class="row" style="font-size:12px">
            <input type="checkbox" id="pg-e-ccase" ${cond.case_sensitive ? "checked" : ""}>
            <span>조건 비교 대소문자 구분</span>
          </label>
          <div class="fld">
            <div class="row" style="justify-content:space-between">
              <span>⚡ 간선 임시 프롬프트 (실행/순환 시 자동 주입 · 1회성)</span>
              ${(e.temp_prompt || "").trim() ? `<button type="button" class="icon" id="pg-e-clrtemp">비우기</button>` : ""}
            </div>
            <textarea id="pg-e-temp" rows="3" style="min-height:72px;border-color:${(e.temp_prompt || "").trim() ? "#d97706" : "var(--line)"}" placeholder="대상이 실행·재귀될 때마다 1회성 임시 프롬프트로 자동 주입됩니다. (실행 후 대상에서는 제거되고 재실행 시 다시 주입)">${esc(e.temp_prompt || "")}</textarea>
            <span class="pg-hint">치환자: <code>{iteration}</code>, <code>{max_iterations}</code>, <code>{source_id}</code>, <code>{source_label}</code>, <code>{source_output}</code>, <code>{contract_errors}</code></span>
          </div>
          <label class="fld"><span>간선 메모 (툴팁 표시)</span><input id="pg-e-lbl" value="${esc(e.label || "")}" placeholder="간선 설명"></label>
          <div class="row">
            <button class="g" id="pg-e-rev">방향 반전 (⇄)</button>
            <button class="g bad" id="pg-e-del">연결 삭제</button>
          </div>
        </div>`;
      $("#pg-e-loop").onchange = ev => {
        if (!ev.target.checked) {
          const otherGraph = { ...PG.graph, edges: PG.graph.edges.filter(x => x.id !== e.id) };
          const cyc = wouldCreateCycle(e.from, e.to, otherGraph, false);
          if (cyc) { ev.target.checked = true; return toast(cyc, true); }
        }
        snapshotUndo();
        e.is_loop = ev.target.checked;
        e.max_iterations = e.is_loop ? Math.max(1, e.max_iterations || 3) : 0;
        syncGraphRelations();
        touch();
        draw(true);
      };
      $("#pg-e-ctype").onchange = ev => { snapshotUndo(); (e.condition || (e.condition = defaultCondition())).type = ev.target.value; touch(); draw(false); };
      $("#pg-e-maxiter").onchange = ev => { snapshotUndo(); e.max_iterations = Math.max(1, Math.min(20, parseInt(ev.target.value, 10) || 3)); touch(); draw(false); };
      $("#pg-e-cval").oninput = ev => { snapshotUndoOnce(`ecv:${e.id}`); (e.condition || (e.condition = defaultCondition())).value = ev.target.value; touch(); };
      $("#pg-e-ccase").onchange = ev => { snapshotUndo(); (e.condition || (e.condition = defaultCondition())).case_sensitive = ev.target.checked; touch(); };
      $("#pg-e-temp").oninput = ev => { snapshotUndoOnce(`etmp:${e.id}`); e.temp_prompt = ev.target.value; touch(); draw(false); };
      if ($("#pg-e-clrtemp")) $("#pg-e-clrtemp").onclick = () => { snapshotUndo(); e.temp_prompt = ""; touch(); draw(true); };
      $("#pg-e-lbl").oninput = ev => { snapshotUndoOnce(`elbl:${e.id}`); e.label = ev.target.value; touch(); };
      $("#pg-e-rev").onclick = () => {
        const otherGraph = { ...PG.graph, edges: PG.graph.edges.filter(x => x.id !== e.id) };
        const err = wouldCreateCycle(e.to, e.from, otherGraph, Boolean(e.is_loop));
        if (err) return toast(err, true);
        if (!e.is_loop) {
          const compErr = validateCompletedEdge(e.to, e.from, otherGraph);
          if (compErr) return toast(compErr, true);
        }
        snapshotUndo();
        [e.from, e.to] = [e.to, e.from];
        syncGraphRelations();
        touch();
        draw(true);
      };
      $("#pg-e-del").onclick = removeSelected;
      return;
    }

    if (sel?.type === "node") {
      const n = findNode(sel.id);
      if (!n) { PG.sel = null; return drawInspector(); }
      const ancestors = ancestorGroups(n.id);
      const preList = ancestors.filter(g => (g.pre_prompt || "").trim() || (g.temp_pre_prompt || "").trim());
      const depTargets = [...PG.graph.nodes, ...PG.graph.groups].filter(x => x.id !== n.id && !(n.dependencies || []).includes(x.id));
      const aliases = Object.entries(PG.gateway?.aliases || {});
      const s = nodeState(n.id);
      const ct = n.contract || defaultContract();
      const art = n.artifact || defaultArtifact();

      bodyEl.innerHTML = `<div class="ide-head">◈ 프롬프트 노드</div>
        <div class="g-form">
          <div class="row">
            <button class="b" id="pg-n-run" style="flex:1">▶ 이 노드 실행</button>
            <button class="g" id="pg-n-dup">복제</button>
            <button class="g bad" id="pg-n-del">삭제</button>
          </div>
          ${renderChangeCallout(chg.nodes[n.id], "pg-n-clrrerun")}
          <div class="row" style="justify-content:space-between;gap:6px">
            <label class="row" style="font-size:12px">
              <input type="checkbox" id="pg-n-completed" ${n.completed ? "checked" : ""}>
              <span>완료 (<code>${n.completed ? "True" : "False"}</code>)</span>
            </label>
            <button type="button" class="g" id="pg-n-selfloop" style="padding:2px 8px;font-size:11px">↺ 자가 순환 추가</button>
          </div>
          ${s ? `<div class="pg-hint">실행 상태: <b>${esc(STATUS_KO[s.status] || s.status)}</b>${s.model ? ` · 모델 <code>${esc(s.model)}</code>` : ""}${s.mcp_calls ? ` · MCP 조회 ${s.mcp_calls}회` : ""}${s.error ? `<div class="bad">${esc(s.error)}</div>` : ""}</div>` : ""}
          <label class="fld"><span>노드 ID</span><input id="pg-n-id" class="mono" value="${esc(n.id)}"></label>
          <label class="fld"><span>이름</span><input id="pg-n-label" value="${esc(n.label)}"></label>
          <label class="fld"><span>실행할 모델 (비우면 기본값: ${esc(PG.graph.settings?.default_model || PG.defaultModel)})</span>
            <input id="pg-n-model" class="mono" list="pg-model-list" value="${esc(n.model || "")}" placeholder="예: cheap / mid / advisor">
            <datalist id="pg-model-list">
              ${aliases.map(([k, v]) => `<option value="${esc(k)}">${esc(k)} (${esc(v)})</option>`).join("")}
            </datalist>
          </label>
          <label class="fld"><span>소속 프리 프롬프트 그룹</span>
            <div class="row" style="gap:6px">
              <select id="pg-n-parent" style="flex:1;min-width:0">
                <option value="">(없음 — 최상위)</option>
                ${PG.graph.groups.map(g => `<option value="${esc(g.id)}" ${n.parent_group_id === g.id ? "selected" : ""}>${"  ".repeat(groupDepth(g.id))}⬚ ${groupTier(g.id)}-그룹 · ${esc(g.label)} (${esc(g.id)})</option>`).join("")}
              </select>
              <button type="button" class="g" id="pg-n-wrap" title="이 노드를 감싸는 새 1-그룹 생성" style="padding:4px 8px;font-size:11.5px;white-space:nowrap">＋ ⬚ 감싸기</button>
            </div>
          </label>
          ${preList.length ? `<div class="fld"><span>적용되는 프리 프롬프트 (${preList.length}단계 중첩)</span>
            <div class="pg-pre-preview">${preList.map((g, idx) => {
              const parts = [];
              if ((g.pre_prompt || "").trim()) parts.push(`[#${idx + 1} ${esc(g.label)}]\n${esc(g.pre_prompt)}`);
              if ((g.temp_pre_prompt || "").trim()) parts.push(`[#${idx + 1} ⚡ 임시 프리: ${esc(g.label)}]\n${esc(g.temp_pre_prompt)}`);
              return parts.join("\n");
            }).join("\n\n")}</div>
          </div>` : ""}
          <div class="fld">
            <span>의존 노드 / 그룹 (dependencies)</span>
            <div class="pg-chips">${(n.dependencies || []).length
              ? n.dependencies.map(d => `<span class="pg-dep-pill"><span>→ ${esc(entityLabel(d))} <span class="muted">(${esc(d)})</span></span><button type="button" data-pg-rmdep="${esc(d)}" title="의존 제거">×</button></span>`).join("")
              : `<span class="muted" style="font-size:12px">의존 대상 없음</span>`}</div>
            ${depTargets.length ? `<select id="pg-n-adddep"><option value="">＋ 의존 대상 추가…</option>${depTargets.map(x => `<option value="${esc(x.id)}">${findGroup(x.id) ? "⬚ 그룹" : "◈ 노드"} · ${esc(x.label)} (${esc(x.id)})</option>`).join("")}</select>` : ""}
          </div>
          <label class="fld"><span>프롬프트</span><textarea id="pg-n-prompt" rows="5" style="min-height:95px" placeholder="에이전트가 실행할 프롬프트">${esc(n.prompt || "")}</textarea></label>
          <div class="fld">
            <div class="row" style="justify-content:space-between">
              <span>⚡ 임시 프롬프트 (1회성)</span>
              ${(n.temp_prompt || "").trim() ? `<button type="button" class="icon" id="pg-n-clrtemp">비우기</button>` : ""}
            </div>
            <textarea id="pg-n-temp" rows="2" style="min-height:58px;border-color:${(n.temp_prompt || "").trim() ? "#d97706" : "var(--line)"}" placeholder="1회성 추가 지시">${esc(n.temp_prompt || "")}</textarea>
          </div>
          <label class="fld"><span>추가 정보 (extra JSON)</span><textarea id="pg-n-extra" rows="2" class="mono" style="min-height:58px">${esc(JSON.stringify(n.extra || {}, null, 2))}</textarea></label>
          <div class="fld">
            <div class="row" style="justify-content:space-between"><span>출력 결과 (output)</span>${n.output ? `<button type="button" class="icon" id="pg-n-clrout">비우기</button>` : ""}</div>
            <textarea id="pg-n-output" rows="5" style="min-height:90px" placeholder="실행 결과">${esc(n.output || "")}</textarea>
          </div>

          <details style="border-top:1px solid var(--line);padding-top:6px" ${ct.enabled ? "open" : ""}>
            <summary style="cursor:pointer;font-size:12px;font-weight:600">📋 출력 계약 (Schema / Assertion)</summary>
            <div class="g-form" style="margin-top:6px">
              <label class="row" style="font-size:12px"><input type="checkbox" id="pg-ct-en" ${ct.enabled ? "checked" : ""}><span>계약 검증 활성화</span></label>
              <label class="row" style="font-size:12px"><input type="checkbox" id="pg-ct-json" ${ct.json_required ? "checked" : ""}><span>유효 JSON 필수</span></label>
              <label class="fld"><span>필수 JSON 키 (줄바꿈 구분)</span><textarea id="pg-ct-keys" rows="2" class="mono">${esc((ct.json_keys || []).join("\n"))}</textarea></label>
              <label class="fld"><span>필수 포함 문자열 (줄바꿈 구분)</span><textarea id="pg-ct-inc" rows="2">${esc((ct.must_include || []).join("\n"))}</textarea></label>
              <label class="fld"><span>금지 문자열 (줄바꿈 구분)</span><textarea id="pg-ct-exc" rows="2">${esc((ct.must_exclude || []).join("\n"))}</textarea></label>
              <div class="row" style="gap:6px">
                <label class="fld" style="flex:1"><span>정규식</span><input id="pg-ct-rx" class="mono" value="${esc(ct.regex || "")}"></label>
                <label class="fld" style="width:76px"><span>최소글자</span><input id="pg-ct-min" type="number" min="0" value="${ct.min_chars || 0}"></label>
              </div>
              <div class="row" style="gap:6px">
                <label class="fld" style="flex:1"><span>실패 시 재시도</span><input id="pg-ct-ret" type="number" min="0" max="5" value="${ct.max_retries || 0}"></label>
                <label class="fld" style="flex:1"><span>최종 실패 처리</span>
                  <select id="pg-ct-onfail"><option value="fail" ${ct.on_fail === "fail" ? "selected" : ""}>노드 실패(fail)</option><option value="continue" ${ct.on_fail === "continue" ? "selected" : ""}>계속/분기(continue)</option></select>
                </label>
              </div>
            </div>
          </details>

          <details style="border-top:1px solid var(--line);padding-top:6px" ${art.kind !== "none" ? "open" : ""}>
            <summary style="cursor:pointer;font-size:12px;font-weight:600">🔗 산출물 양방향 연결 (Repo 파일 / Spec)</summary>
            <div class="g-form" style="margin-top:6px">
              <div class="row" style="gap:6px">
                <label class="fld" style="flex:1"><span>대상 종류</span>
                  <select id="pg-art-kind">
                    <option value="none" ${art.kind === "none" ? "selected" : ""}>연결 안 함</option>
                    <option value="repo_file" ${art.kind === "repo_file" ? "selected" : ""}>Repo 파일</option>
                    <option value="spec_query" ${art.kind === "spec_query" ? "selected" : ""}>Spec SQL 조회</option>
                  </select>
                </label>
                <label class="fld" style="flex:1"><span>방향 모드</span>
                  <select id="pg-art-mode">
                    <option value="read" ${art.mode === "read" ? "selected" : ""}>읽기 (변경 감지)</option>
                    <option value="write" ${art.mode === "write" ? "selected" : ""}>쓰기 (출력 반영)</option>
                    <option value="sync" ${art.mode === "sync" ? "selected" : ""}>양방향 (sync)</option>
                  </select>
                </label>
              </div>
              <label class="fld"><span>Repo 이름</span><input id="pg-art-repo" class="mono" value="${esc(art.repo || "workspace")}"></label>
              <label class="fld"><span>상대 경로 또는 Spec SELECT</span><input id="pg-art-path" class="mono" value="${esc(art.path || "")}" placeholder="예: docs/ARCHITECTURE.md"></label>
              ${art.last_content_hash ? `<div class="pg-hint mono">hash: ${esc(art.last_content_hash.slice(0, 12))}…</div>` : ""}
            </div>
          </details>

          <details style="border-top:1px solid var(--line);padding-top:6px">
            <summary style="cursor:pointer;font-size:12px;font-weight:600" id="pg-ver-sum">🕒 버전 이력 · Diff · 롤백</summary>
            <div id="pg-ver-box" style="margin-top:6px;display:flex;flex-direction:column;gap:6px">
              <span class="muted" style="font-size:11.5px">${PG.graphId ? "클릭해 실행 버전 이력을 불러옵니다…" : "그래프를 저장하면 노드별 버전 이력이 기록됩니다."}</span>
            </div>
          </details>
        </div>`;

      $("#pg-n-run").onclick = run(() => startRun("node", n.id));
      $("#pg-n-del").onclick = removeSelected;
      $("#pg-n-selfloop").onclick = () => addEdge(n.id, n.id, { is_loop: true, max_iterations: 3, condition: { type: "on_contract_fail", value: "", case_sensitive: false }, label: "자가 순환 개선" });
      if ($("#pg-n-clrrerun")) $("#pg-n-clrrerun").onclick = () => { snapshotUndo(); n.needs_rerun = false; n.rerun_reason = ""; touch(); draw(true); };
      $("#pg-n-completed").onchange = ev => {
        if (!manualSetCompleted(n.id, ev.target.checked)) { ev.target.checked = n.completed; return; }
        draw(true);
      };
      $("#pg-n-dup").onclick = () => {
        snapshotUndo();
        const copy = JSON.parse(JSON.stringify(n));
        copy.id = newId("node");
        copy.label = `${n.label} (복사)`;
        copy.x += 30;
        copy.y += 30;
        copy.dependencies = [];
        copy.completed = false;
        copy.last_run_prompt = null;
        copy.last_run_temp_prompt = null;
        copy.last_run_pre_prompts = null;
        copy.needs_rerun = false;
        copy.rerun_reason = "";
        copy.impact_history = [];
        PG.graph.nodes.push(copy);
        syncGraphRelations();
        PG.sel = { type: "node", id: copy.id };
        touch();
        draw(true);
      };
      $("#pg-n-id").onchange = ev => { if (!renameEntity(n.id, ev.target.value.trim())) ev.target.value = n.id; else draw(true); };
      $("#pg-n-label").oninput = ev => { snapshotUndoOnce(`nlbl:${n.id}`); n.label = ev.target.value; touch(); draw(false); };
      $("#pg-n-model").onchange = ev => { snapshotUndo(); n.model = ev.target.value.trim(); touch(); draw(false); };
      $("#pg-n-parent").onchange = ev => { snapshotUndo(); n.parent_group_id = ev.target.value || null; syncGraphRelations(); fitAllAncestorBounds(n.id); touch(); draw(true); };
      $("#pg-n-wrap").onclick = () => wrapEntityInParentGroup(n.id);
      $("#pg-n-prompt").oninput = ev => { snapshotUndoOnce(`npr:${n.id}`); n.prompt = ev.target.value; touch(); draw(false); };
      $("#pg-n-temp").oninput = ev => { snapshotUndoOnce(`ntmp:${n.id}`); n.temp_prompt = ev.target.value; touch(); draw(false); };
      if ($("#pg-n-clrtemp")) $("#pg-n-clrtemp").onclick = () => { snapshotUndo(); n.temp_prompt = ""; touch(); draw(true); };
      $("#pg-n-output").oninput = ev => { snapshotUndoOnce(`nout:${n.id}`); n.output = ev.target.value; touch(); };
      if ($("#pg-n-clrout")) $("#pg-n-clrout").onclick = () => { snapshotUndo(); n.output = ""; touch(); draw(true); };
      $("#pg-n-extra").onchange = ev => {
        try {
          const parsed = JSON.parse(ev.target.value.trim() || "{}");
          if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error();
          snapshotUndo();
          n.extra = parsed;
          touch();
          draw(false);
        } catch {
          toast("extra는 유효한 JSON 객체여야 합니다 (예: {\"effort\":\"high\"}).", true);
        }
      };
      const lines = v => String(v || "").split("\n").map(x => x.trim()).filter(Boolean);
      $("#pg-ct-en").onchange = ev => { snapshotUndo(); ct.enabled = ev.target.checked; n.contract = ct; touch(); };
      $("#pg-ct-json").onchange = ev => { snapshotUndo(); ct.json_required = ev.target.checked; n.contract = ct; touch(); };
      $("#pg-ct-keys").onchange = ev => { snapshotUndo(); ct.json_keys = lines(ev.target.value); n.contract = ct; touch(); };
      $("#pg-ct-inc").onchange = ev => { snapshotUndo(); ct.must_include = lines(ev.target.value); n.contract = ct; touch(); };
      $("#pg-ct-exc").onchange = ev => { snapshotUndo(); ct.must_exclude = lines(ev.target.value); n.contract = ct; touch(); };
      $("#pg-ct-rx").onchange = ev => { snapshotUndo(); ct.regex = ev.target.value.trim(); n.contract = ct; touch(); };
      $("#pg-ct-min").onchange = ev => { snapshotUndo(); ct.min_chars = Math.max(0, parseInt(ev.target.value, 10) || 0); n.contract = ct; touch(); };
      $("#pg-ct-ret").onchange = ev => { snapshotUndo(); ct.max_retries = Math.max(0, Math.min(5, parseInt(ev.target.value, 10) || 0)); n.contract = ct; touch(); };
      $("#pg-ct-onfail").onchange = ev => { snapshotUndo(); ct.on_fail = ev.target.value; n.contract = ct; touch(); };
      $("#pg-art-kind").onchange = ev => { snapshotUndo(); art.kind = ev.target.value; n.artifact = art; touch(); };
      $("#pg-art-mode").onchange = ev => { snapshotUndo(); art.mode = ev.target.value; n.artifact = art; touch(); };
      $("#pg-art-repo").onchange = ev => { snapshotUndo(); art.repo = ev.target.value.trim(); n.artifact = art; touch(); };
      $("#pg-art-path").onchange = ev => { snapshotUndo(); art.path = ev.target.value.trim(); n.artifact = art; touch(); };
      if ($("#pg-ver-sum") && PG.graphId) {
        $("#pg-ver-sum").onclick = async () => {
          const box = $("#pg-ver-box");
          if (!box || box.dataset.loaded) return;
          box.dataset.loaded = "1";
          const res = await api("GET", `${WSP()}/prompt-graphs/${PG.graphId}/nodes/${encodeURIComponent(n.id)}/versions`, undefined, { fresh: true }).catch(() => ({ versions: [] }));
          const vers = res.versions || [];
          PG.versionCache[n.id] = vers;
          if (!vers.length) {
            box.innerHTML = `<span class="muted" style="font-size:11.5px">아직 기록된 실행 버전이 없습니다.</span>`;
            return;
          }
          box.innerHTML = `${vers.map((v, i) => `<div class="pg-chg-item">
            <div class="top">
              <span class="name">#${vers.length - i} (iter ${v.iteration || 1}) · ${esc(v.model || "")}</span>
              <span class="muted" style="font-size:10.5px">${v.duration_ms || 0}ms · ${v.total_tokens || 0}t</span>
            </div>
            <div class="pg-chg-reason">${esc((v.output || "").slice(0, 90))}</div>
            <div class="row" style="gap:4px">
              <button type="button" class="g" data-pg-ver-diff="${v.version_id}" style="padding:1px 7px;font-size:11px">현재와 Diff</button>
              <button type="button" class="g" data-pg-ver-chk="${v.version_id}" style="padding:1px 7px;font-size:11px">↩ 출력 롤백</button>
            </div>
          </div>`).join("")}<div id="pg-ver-diffbox"></div>`;
          box.querySelectorAll("[data-pg-ver-diff]").forEach(b => {
            b.onclick = () => {
              const v = vers.find(x => String(x.version_id) === b.dataset.pgVerDiff);
              if (v) $("#pg-ver-diffbox").innerHTML = `<div class="pg-hint" style="margin:4px 0">과거(#${v.version_id}) → 현재 출력 Diff</div>` + renderLineDiff(v.output, n.output);
            };
          });
          box.querySelectorAll("[data-pg-ver-chk]").forEach(b => {
            b.onclick = run(async () => {
              const r = await api("POST", `${WSP()}/prompt-graphs/${PG.graphId}/nodes/${encodeURIComponent(n.id)}/checkout`, { version_id: Number(b.dataset.pgVerChk) });
              PG.graph = ensureArrays(r.graph);
              toast("선택한 버전의 출력으로 롤백했습니다.");
              draw(true);
            });
          });
        };
      }
      document.querySelectorAll("[data-pg-rmdep]").forEach(b => b.onclick = () => removeEdgeByPair(n.id, b.dataset.pgRmdep));
      if ($("#pg-n-adddep")) $("#pg-n-adddep").onchange = ev => { if (ev.target.value) addEdge(n.id, ev.target.value); };
      return;
    }

    if (sel?.type === "group") {
      const g = findGroup(sel.id);
      if (!g) { PG.sel = null; return drawInspector(); }
      const forbiddenParents = subtreeIds(g.id);
      const parentOpts = PG.graph.groups.filter(x => !forbiddenParents.has(x.id));
      const depTargets = [...PG.graph.nodes, ...PG.graph.groups].filter(x => !forbiddenParents.has(x.id) && !(g.dependencies || []).includes(x.id));
      const directNodes = (g.node_ids || []).map(id => findNode(id)).filter(Boolean);
      const childGroups = (g.child_group_ids || []).map(id => findGroup(id)).filter(Boolean);

      const tier = groupTier(g.id);
      const ancestors = ancestorGroups(g.id);
      const subIds = subtreeIds(g.id);
      const totalSubGroups = Math.max(0, PG.graph.groups.filter(x => x.id !== g.id && subIds.has(x.id)).length);
      const totalSubNodes = PG.graph.nodes.filter(x => subIds.has(x.id)).length;

      bodyEl.innerHTML = `<div class="ide-head">⬚ ${tier}-그룹 (프리 프롬프트 그룹)</div>
        <div class="g-form">
          <div class="row">
            <button class="b" id="pg-g-run" style="flex:1">▶ 이 그룹 실행</button>
            <button class="g bad" id="pg-g-del">그룹 해제/삭제</button>
          </div>
          <div class="row">
            <button type="button" class="g" id="pg-g-wrap-parent" style="flex:1">＋ ⬚ ${tier + 1}-그룹 감싸기</button>
            <button type="button" class="g" id="pg-g-add-child" style="flex:1">＋ 하위 그룹</button>
          </div>
          <button type="button" class="g" id="pg-g-savetpl">📦 컴포넌트로 저장 (템플릿 라이브러리)</button>
          ${renderChangeCallout(chg.groups[g.id], "pg-g-clrrerun")}
          <label class="row" style="font-size:12.5px">
            <input type="checkbox" id="pg-g-completed" ${g.completed ? "checked" : ""}>
            <span>실행 완료 여부 (<code>completed</code>: <b>${g.completed ? "완료" : "미완료"}</b>)</span>
          </label>
          <div class="pg-hint">계층: <b>${ancestors.length ? ancestors.map(a => `${groupTier(a.id)}-그룹(${esc(a.label)})`).join(" › ") + " › " : ""}${tier}-그룹(${esc(g.label)})</b> · 하위 그룹 ${totalSubGroups}개 · 노드 ${totalSubNodes}개</div>
          <label class="fld"><span>그룹 ID</span><input id="pg-g-id" class="mono" value="${esc(g.id)}"></label>
          <label class="fld"><span>이름</span><input id="pg-g-label" value="${esc(g.label)}"></label>
          <label class="fld"><span>프리 프롬프트</span><textarea id="pg-g-pre" rows="5" style="min-height:95px" placeholder="공통 프리 프롬프트">${esc(g.pre_prompt || "")}</textarea></label>
          <div class="fld">
            <div class="row" style="justify-content:space-between">
              <span>⚡ 임시 프리 프롬프트 (1회성)</span>
              ${(g.temp_pre_prompt || "").trim() ? `<button type="button" class="icon" id="pg-g-clrtemppre">비우기</button>` : ""}
            </div>
            <textarea id="pg-g-temppre" rows="2" style="min-height:58px;border-color:${(g.temp_pre_prompt || "").trim() ? "#d97706" : "var(--line)"}" placeholder="1회성 임시 프리 프롬프트">${esc(g.temp_pre_prompt || "")}</textarea>
          </div>
          <label class="fld"><span>상위 그룹</span>
            <select id="pg-g-parent">
              <option value="">(없음 — 최상위 그룹)</option>
              ${parentOpts.map(p => `<option value="${esc(p.id)}" ${g.parent_group_id === p.id ? "selected" : ""}>${"  ".repeat(groupDepth(p.id))}⬚ ${groupTier(p.id)}-그룹 · ${esc(p.label)} (${esc(p.id)})</option>`).join("")}
            </select>
          </label>
          <div class="fld">
            <span>의존 대상 (dependencies)</span>
            <div class="pg-chips">${(g.dependencies || []).length
              ? g.dependencies.map(d => `<span class="pg-dep-pill"><span>→ ${esc(entityLabel(d))} <span class="muted">(${esc(d)})</span></span><button type="button" data-pg-grmdep="${esc(d)}" title="의존 제거">×</button></span>`).join("")
              : `<span class="muted" style="font-size:12px">의존 대상 없음</span>`}</div>
            ${depTargets.length ? `<select id="pg-g-adddep"><option value="">＋ 의존 대상 추가…</option>${depTargets.map(x => `<option value="${esc(x.id)}">${findGroup(x.id) ? "⬚ 그룹" : "◈ 노드"} · ${esc(x.label)} (${esc(x.id)})</option>`).join("")}</select>` : ""}
          </div>
          <div class="fld"><span>직접 포함된 프롬프트 노드 (${directNodes.length})</span>
            <div class="pg-chips">${directNodes.map(n => `<button type="button" class="g" style="padding:2px 8px;font-size:11.5px" data-pg-jump-node="${esc(n.id)}">◈ ${esc(n.label)}</button>`).join("") || `<span class="muted" style="font-size:12px">없음</span>`}</div>
          </div>
          <div class="fld"><span>하위 프리 프롬프트 그룹 (${childGroups.length})</span>
            <div class="pg-chips">${childGroups.map(c => `<button type="button" class="g" style="padding:2px 8px;font-size:11.5px" data-pg-jump-grp="${esc(c.id)}">⬚ ${esc(c.label)}</button>`).join("") || `<span class="muted" style="font-size:12px">없음</span>`}</div>
          </div>
        </div>`;

      $("#pg-g-run").onclick = run(() => startRun("group", g.id));
      $("#pg-g-del").onclick = removeSelected;
      $("#pg-g-wrap-parent").onclick = () => wrapEntityInParentGroup(g.id);
      $("#pg-g-add-child").onclick = () => addChildSubgroup(g.id);
      $("#pg-g-savetpl").onclick = run(async () => {
        syncGraphRelations();
        const tname = typeof prompt === "function" ? (prompt("저장할 컴포넌트 이름을 입력하세요:", g.label) || "").trim() : g.label;
        if (!tname) return;
        const row = await api("POST", `${WSP()}/prompt-graph-templates`, { name: tname, description: `${tier}-그룹 컴포넌트`, graph: PG.graph, group_id: g.id });
        PG.templates = [row, ...(PG.templates || [])];
        toast(`컴포넌트 '${tname}' 저장됨`);
      });
      if ($("#pg-g-clrrerun")) $("#pg-g-clrrerun").onclick = () => { snapshotUndo(); g.needs_rerun = false; g.rerun_reason = ""; touch(); draw(true); };
      $("#pg-g-completed").onchange = ev => {
        if (!manualSetCompleted(g.id, ev.target.checked)) { ev.target.checked = g.completed; return; }
        draw(true);
      };
      $("#pg-g-id").onchange = ev => { if (!renameEntity(g.id, ev.target.value.trim())) ev.target.value = g.id; else draw(true); };
      $("#pg-g-label").oninput = ev => { snapshotUndoOnce(`glbl:${g.id}`); g.label = ev.target.value; touch(); draw(false); };
      $("#pg-g-pre").oninput = ev => { snapshotUndoOnce(`gpre:${g.id}`); g.pre_prompt = ev.target.value; touch(); draw(false); };
      $("#pg-g-temppre").oninput = ev => { snapshotUndoOnce(`gtmp:${g.id}`); g.temp_pre_prompt = ev.target.value; touch(); draw(false); };
      if ($("#pg-g-clrtemppre")) $("#pg-g-clrtemppre").onclick = () => { snapshotUndo(); g.temp_pre_prompt = ""; touch(); draw(true); };
      $("#pg-g-parent").onchange = ev => { snapshotUndo(); g.parent_group_id = ev.target.value || null; syncGraphRelations(); fitAllAncestorBounds(g.id); touch(); draw(true); };
      document.querySelectorAll("[data-pg-grmdep]").forEach(b => b.onclick = () => removeEdgeByPair(g.id, b.dataset.pgGrmdep));
      if ($("#pg-g-adddep")) $("#pg-g-adddep").onchange = ev => { if (ev.target.value) addEdge(g.id, ev.target.value); };
      document.querySelectorAll("[data-pg-jump-node]").forEach(b => b.onclick = () => { PG.sel = { type: "node", id: b.dataset.pgJumpNode }; draw(true); });
      document.querySelectorAll("[data-pg-jump-grp]").forEach(b => b.onclick = () => { PG.sel = { type: "group", id: b.dataset.pgJumpGrp }; draw(true); });
      return;
    }

    const st = PG.graph.settings || (PG.graph.settings = { parallelism: 4, max_tool_steps: 8, default_model: "", edge_direction: "dependency", cross_graph_cache: false });
    const maxTier = PG.graph.groups.reduce((m, g) => Math.max(m, groupTier(g.id)), 0);
    bodyEl.innerHTML = `<div class="ide-head">그래프 설정</div>
      <div class="g-form">
        <div class="muted" style="font-size:12.5px">노드 ${PG.graph.nodes.length}개 · 그룹 ${PG.graph.groups.length}개 (${maxTier ? `최대 ${maxTier}-그룹` : "계층 없음"}) · 간선 ${PG.graph.edges.length}개</div>
        <div class="row">
          <label class="fld" style="flex:1"><span>최대 병렬 실행</span><input id="pg-s-par" type="number" min="1" max="8" value="${st.parallelism ?? 4}"></label>
          <label class="fld" style="flex:1"><span>MCP 조회 최대 단계</span><input id="pg-s-steps" type="number" min="1" max="16" value="${st.max_tool_steps ?? 8}"></label>
        </div>
        <label class="fld"><span>기본 실행 모델 (워크스페이스 기본값: ${esc(PG.defaultModel)})</span>
          <input id="pg-s-model" class="mono" value="${esc(st.default_model || "")}" placeholder="예: mid / cheap / advisor">
        </label>
        <label class="fld"><span>전체 화살표 방향</span>
          <select id="pg-s-edgedir">
            <option value="dependency" ${!isReversedDir() ? "selected" : ""}>의존 방향 (A → B: A가 B를 의존)</option>
            <option value="execution" ${isReversedDir() ? "selected" : ""}>실행 방향 (B → A: B 완료 후 A 실행)</option>
          </select>
        </label>
        <label class="row" style="font-size:12.5px">
          <input type="checkbox" id="pg-s-reuse" ${PG.reuseOutputs ? "checked" : ""}>
          <span>이미 출력이 있는 선행 의존 노드는 재실행 생략</span>
        </label>
        <label class="row" style="font-size:12.5px">
          <input type="checkbox" id="pg-s-autorun" ${st.auto_rerun_needs_rerun ? "checked" : ""}>
          <span>에이전트가 '재실행 필요'로 판단한 노드 자동 재실행</span>
        </label>
        <label class="row" style="font-size:12.5px">
          <input type="checkbox" id="pg-s-cache" ${st.cross_graph_cache ? "checked" : ""}>
          <span>크로스-그래프 입력 해시 캐시 활성화</span>
        </label>
        <div class="pg-hint">
          • <b>전체 화살표 방향 전환</b>: 모든 일반·그룹·조건·순환 화살표의 표시/연결 방향을 의존 방향과 실행 방향으로 일괄 전환합니다.<br>
          • <b>조건부 순환/재귀 & 출력 계약</b>: 간선 속성에서 조건부 순환(<code>is_loop</code>) 및 최대 반복 횟수를, 노드 속성에서 출력 계약(Schema/Assertion)과 산출물 연결을 설정할 수 있습니다.<br>
          • <b>Shift+드래그</b>: 점선 프리 프롬프트 그룹 생성 · <b>Alt+드래그</b>/<b>🔗 간선 연결</b>: 간선 연결
        </div>
      </div>`;
    $("#pg-s-par").onchange = e => { snapshotUndo(); st.parallelism = Math.max(1, Math.min(8, parseInt(e.target.value, 10) || 4)); touch(); };
    $("#pg-s-steps").onchange = e => { snapshotUndo(); st.max_tool_steps = Math.max(1, Math.min(16, parseInt(e.target.value, 10) || 8)); touch(); };
    $("#pg-s-model").onchange = e => { snapshotUndo(); st.default_model = e.target.value.trim(); touch(); draw(false); };
    $("#pg-s-edgedir").onchange = e => {
      snapshotUndo();
      setEdgeDirection(e.target.value);
      touch();
    };
    $("#pg-s-reuse").onchange = e => { PG.reuseOutputs = e.target.checked; };
    $("#pg-s-autorun").onchange = e => { snapshotUndo(); st.auto_rerun_needs_rerun = e.target.checked; touch(); };
    $("#pg-s-cache").onchange = e => { snapshotUndo(); st.cross_graph_cache = e.target.checked; touch(); };
  }

  // ------------------------------------------------------------ run timeline & polling
  const EV_META = {
    run_started: ["▶", "실행 시작"],
    group_started: ["⬚", "그룹 실행 시작"],
    group_finished: ["⬚", "그룹 실행 완료"],
    node_started: ["⏳", "노드 실행 시작"],
    node_cache_hit: ["⚡", "해시 캐시 적중"],
    mcp_tool_call: ["🔌", "MCP 조회"],
    contract_retry: ["📋", "계약 재시도"],
    contract_failed: ["⚠", "출력 계약 미충족"],
    loop_triggered: ["↺", "조건부 순환/재귀 실행"],
    loop_limit_reached: ["⏹", "순환 최대 반복 도달"],
    edge_temp_prompt_injected: ["⚡", "간선 임시 프롬프트 주입"],
    artifact_synced: ["🔗", "산출물 동기화"],
    impact_reviewed: ["⚑", "영향도 검토 (MCP)"],
    needs_rerun_marked: ["⚑", "재실행 필요 표시 (MCP)"],
    auto_rerun_queued: ["↻", "자동 재실행 추가"],
    node_finished: ["✅", "노드 완료"],
    node_failed: ["❌", "노드 실패"],
    node_skipped: ["⏭", "건너뜀"],
    run_finished: ["■", "실행 종료"],
  };

  function drawFeed() {
    const el = $("#pg-feed");
    if (!el) return;
    const r = PG.run;
    if (!r) {
      el.innerHTML = `<div class="muted" style="font-size:12.5px">아직 실행 기록이 없습니다. 전체·그룹·노드 실행 버튼을 눌러보세요.</div>`;
      return;
    }
    const evs = r.events || [];
    el.innerHTML = evs.slice().reverse().map(e => {
      const [ic, title] = EV_META[e.type] || ["•", e.type];
      const m = e.meta_json || {};
      const who = e.node ? `<b>${esc(entityLabel(e.node))}</b>` : "";
      const tgt = e.target ? ` → <b>${esc(entityLabel(e.target))}</b>` : "";
      const chips = [
        m.tool ? `<span class="chip mono">${esc(m.tool)}</span>` : "",
        m.model ? `<span class="chip">${esc(m.model)}</span>` : "",
        m.ms ? `<span class="muted">${(m.ms / 1000).toFixed(1)}s</span>` : "",
        m.mcp_calls ? `<span class="chip">MCP ${m.mcp_calls}회</span>` : "",
        m.cleared_temp_prompt ? `<span class="chip temp">⚡ 임시 프롬프트 소비됨</span>` : "",
        m.cleared_temp_pre_prompt ? `<span class="chip temp">⚡ 임시 프리 프롬프트 소비됨</span>` : "",
        m.status ? `<span class="chip ${G.statusCls(m.status)}">${esc(m.status)}</span>` : "",
      ].filter(Boolean).join(" ");
      const cls = e.type === "mcp_tool_call" ? "mcp" : ["node_failed", "node_skipped"].includes(e.type) ? "bad" : e.type === "run_finished" ? "fin" : "";
      const head = `${ic} ${title} ${who}${tgt} ${chips}`;
      const bodyText = e.type === "mcp_tool_call"
        ? `인자: ${JSON.stringify(m.arguments || {})}\n\n응답:\n${e.content || ""}`
        : e.content || "";
      return `<div class="pg-ev ${cls}">${bodyText ? `<details ${e.type === "node_finished" ? "" : ""}><summary>${head}</summary><pre>${esc(bodyText)}</pre></details>` : head}</div>`;
    }).join("") || `<div class="muted">실행 이벤트 대기 중…</div>`;
  }

  function drawRunHead() {
    const el = $("#pg-runhead");
    if (!el) return;
    const r = PG.run;
    if (!r) { el.innerHTML = ""; return; }
    el.innerHTML = `<div class="row">
      <span class="chip ${G.statusCls(r.status === "PARTIAL" ? "DEFER" : r.status)}">${esc(r.status)}</span>
      <span class="muted mono">${esc(String(r.run_id).slice(0, 8))}</span>
      <span class="muted" style="font-size:12px">대상: ${esc(r.target_type || "all")}${r.target_id ? ` (${esc(entityLabel(r.target_id))})` : ""}</span>
      ${r.active ? `<button class="g bad" id="pg-cancel" style="padding:2px 8px;font-size:12px">■ 중지</button>` : ""}
    </div>
    ${r.result_json?.error ? `<div class="bad" style="font-size:12.5px;margin-top:4px">${esc(r.result_json.error)}</div>` : ""}`;
    const c = $("#pg-cancel");
    if (c) c.onclick = run(async () => {
      await api("POST", `${WSP()}/prompt-graph-runs/${r.run_id}/cancel`, {}, { noInvalidate: true });
      toast("중지 요청됨 — 진행 중인 노드가 끝나면 멈춥니다.");
    });
  }

  function ingestRun(data, reset = false) {
    if (reset || !PG.run || PG.run.run_id !== data.run_id) PG.run = { ...data, events: [] };
    const known = new Set(PG.run.events.map(e => e.seq));
    const fresh = (data.events || []).filter(e => !known.has(e.seq));
    Object.assign(PG.run, { ...data, events: PG.run.events.concat(fresh) });

    if (data.graph_json?.nodes && PG.graph) {
      for (const rn of data.graph_json.nodes) {
        const ln = findNode(rn.id);
        if (!ln) continue;
        if (typeof rn.output === "string" && rn.output !== ln.output) ln.output = rn.output;
        if (typeof rn.temp_prompt === "string") ln.temp_prompt = rn.temp_prompt;
        if (typeof rn.completed === "boolean") ln.completed = rn.completed;
        if ("last_run_prompt" in rn) ln.last_run_prompt = rn.last_run_prompt;
        if ("last_run_temp_prompt" in rn) ln.last_run_temp_prompt = rn.last_run_temp_prompt;
        if ("last_run_pre_prompts" in rn) ln.last_run_pre_prompts = rn.last_run_pre_prompts;
        if (typeof rn.needs_rerun === "boolean") ln.needs_rerun = rn.needs_rerun;
        if (typeof rn.rerun_reason === "string") ln.rerun_reason = rn.rerun_reason;
        if (Array.isArray(rn.impact_history)) ln.impact_history = rn.impact_history;
        if (rn.artifact && typeof rn.artifact === "object") ln.artifact = rn.artifact;
      }
    }
    if (data.graph_json?.groups && PG.graph) {
      for (const rg of data.graph_json.groups) {
        const lg = findGroup(rg.id);
        if (!lg) continue;
        if (typeof rg.temp_pre_prompt === "string") lg.temp_pre_prompt = rg.temp_pre_prompt;
        if (typeof rg.completed === "boolean") lg.completed = rg.completed;
        if ("last_run_pre_prompt" in rg) lg.last_run_pre_prompt = rg.last_run_pre_prompt;
        if ("last_run_temp_pre_prompt" in rg) lg.last_run_temp_pre_prompt = rg.last_run_temp_pre_prompt;
        if (typeof rg.needs_rerun === "boolean") lg.needs_rerun = rg.needs_rerun;
        if (typeof rg.rerun_reason === "string") lg.rerun_reason = rg.rerun_reason;
      }
    }
    const now = Date.now();
    for (const e of fresh) {
      if ((e.type === "mcp_tool_call" || e.type === "needs_rerun_marked") && e.node && e.target) {
        PG.flashes.push({ from: e.node, to: e.target, t: now });
      }
    }
  }

  async function poll() {
    clearTimeout(PG.poll);
    if (!PG.run || G.getTAB() !== "pgraph") return;
    const after = PG.run.events.length ? Math.max(...PG.run.events.map(e => e.seq)) : 0;
    try {
      const data = await api("GET", `${WSP()}/prompt-graph-runs/${PG.run.run_id}?after=${after}`, undefined, { fresh: true });
      if (G.getTAB() !== "pgraph") return;
      ingestRun(data);
      draw(PG.sel?.type === "node" || PG.sel?.type === "group" || PG.inspTab === "changes" || data.status !== "RUNNING");
      drawFeed();
      drawRunHead();
      if (data.status !== "RUNNING") {
        refreshHistory();
        return;
      }
    } catch (e) {
      toast(e.message, true);
    }
    PG.poll = setTimeout(poll, 1200);
  }

  async function refreshHistory() {
    const sel = $("#pg-history");
    if (!sel) return;
    const res = await api("GET", `${WSP()}/prompt-graph-runs`, undefined, { fresh: true }).catch(() => ({ runs: [] }));
    sel.innerHTML = `<option value="">실행 기록…</option>` + (res.runs || []).map(r =>
      `<option value="${esc(r.run_id)}" ${PG.run?.run_id === r.run_id ? "selected" : ""}>${esc(r.status)} · ${esc(r.target_type)}${r.target_id ? `:${esc(r.target_id)}` : ""} · ${esc(new Date(r.created_at).toLocaleString())}</option>`
    ).join("");
  }

  async function startRun(targetType = "all", targetId = null, targetIds = null) {
    syncGraphRelations();
    const payload = {
      graph_id: PG.graphId || null,
      graph: PG.graph,
      target_type: targetType,
      target_id: targetId,
      reuse_existing_outputs: !!PG.reuseOutputs,
    };
    if (targetType === "subset" && Array.isArray(targetIds)) {
      payload.target_ids = targetIds;
    }
    const res = await api("POST", `${WSP()}/prompt-graph-runs`, payload, { noInvalidate: true });
    PG.run = {
      run_id: res.run_id,
      status: "RUNNING",
      target_type: targetType,
      target_id: res.target_id || targetId,
      events: [],
      node_state: {},
      result_json: {},
      active: true,
    };
    const runCnt = res.run_nodes?.length ?? 0;
    const skipCnt = res.skipped_clean_nodes?.length ?? 0;
    toast(runCnt === 0
      ? `변경되었거나 미실행된 노드가 없어 에이전트 호출을 생략했습니다.${skipCnt ? ` (완료 생략 ${skipCnt}개)` : ""}`
      : `프롬프트 그래프 실행 시작 (노드 ${runCnt}개${skipCnt ? ` · 완료 생략 ${skipCnt}개` : ""})`);
    draw(false);
    drawFeed();
    drawRunHead();
    refreshHistory();
    poll();
  }

  function setEdgeDirection(dir) {
    const next = dir === "execution" ? "execution" : "dependency";
    PG.edgeDir = next;
    PG.pendingLinkFrom = null;
    if (PG.graph?.settings) PG.graph.settings.edge_direction = next;
    G.store.set("agdd.pg.edgedir", next);
    const btn = $("#pg-edge-dir");
    if (btn) {
      btn.classList.toggle("on", next === "execution");
      btn.textContent = next === "execution" ? "⇄ 화살표: 실행 순서 (선행→후속)" : "⇄ 화살표: 의존 방향 (의존→선행)";
    }
    draw(true);
  }

  function loadGraph(g, graphId, name) {
    PG.graph = ensureArrays(JSON.parse(JSON.stringify(g || { nodes: [], groups: [], edges: [], settings: {} })));
    syncGraphRelations();
    if (g?.settings?.edge_direction === "execution" || g?.settings?.edge_direction === "dependency") {
      PG.edgeDir = g.settings.edge_direction;
      G.store.set("agdd.pg.edgedir", PG.edgeDir);
    } else if (PG.graph.settings) {
      PG.graph.settings.edge_direction = PG.edgeDir;
    }
    PG.graphId = graphId || "";
    PG.name = name || PG.name;
    PG.sel = null;
    PG.analytics = null;
    PG.changeSel.clear();
    PG.changeSeen.clear();
    PG.dirty = false;
    PG.fitPending = true;
  }

  async function save() {
    syncGraphRelations();
    const name = ($("#pg-name")?.value || "").trim() || "새 프롬프트 그래프";
    const row = await api("POST", `${WSP()}/prompt-graphs`, {
      graph_id: PG.graphId || null,
      name,
      graph: PG.graph,
    });
    PG.graphId = row.graph_id;
    PG.name = row.name;
    PG.dirty = false;
    loadGraph(row.graph_json, row.graph_id, row.name);
    toast("프롬프트 그래프 저장됨");
    G.render();
  }

  // ------------------------------------------------------------ view entry
  G.VIEWS.pgraph = async function promptGraphView() {
    const res = await api("GET", `${WSP()}/prompt-graphs`, undefined, { fresh: true });
    if (stale("pgraph")) return;
    PG.graphs = res.graphs || [];
    PG.presets = res.presets || {};
    PG.presetInfo = res.preset_info || {};
    PG.gateway = res.gateway || {};
    PG.defaultModel = res.default_model || "mid";

    if (PG.graph && PG.wsKey !== G.getWS()) {
      PG.graph = null; PG.run = null; PG.graphId = ""; PG.sel = null; PG.dirty = false;
      PG.undo = []; PG.redo = []; PG.flashes = [];
      clearTimeout(PG.poll);
    }
    PG.wsKey = G.getWS();
    if (!PG.graph) {
      const saved = PG.graphs.find(x => x.graph_id === PG.graphId) || PG.graphs[0];
      if (saved) loadGraph(saved.graph_json, saved.graph_id, saved.name);
      else loadGraph(PG.presets.nested_groups || PG.presets.basic_dependency, "", "다중 그룹 프롬프트 그래프");
    }

    const opt = (v, cur, lbl) => `<option value="${esc(v)}" ${String(v) === String(cur ?? "") ? "selected" : ""}>${esc(lbl)}</option>`;
    G.V(`<div class="row" style="justify-content:space-between;margin-bottom:10px;gap:10px;flex-wrap:wrap">
        <div>
          <h2>프롬프트 그래프 · 프리 프롬프트 그룹 & MCP 에이전트</h2>
          <p class="sub" style="margin:0">노드 간 의존 화살표(<b>A → B</b>: A는 B를 의존)와 중첩 가능한 점선 프리 프롬프트 사각형으로 에이전트를 병렬·순차 실행하고 MCP로 선행 결과를 조회합니다.</p>
        </div>
        <div class="row" style="flex-wrap:wrap">
          <select id="pg-select">${opt("", PG.graphId, "＋ 저장 안 된 그래프")}${PG.graphs.map(g => opt(g.graph_id, PG.graphId, g.name)).join("")}</select>
          <input id="pg-name" value="${esc(PG.name)}" style="width:180px" placeholder="그래프 이름">
          <button class="b" id="pg-save">${PG.dirty ? "저장 *" : "저장"}</button>
          ${PG.graphId ? `<button class="g bad" id="pg-del">삭제</button>` : ""}
        </div>
      </div>

      <div class="pg-toolbar">
        <button class="g" id="pg-add-node">＋ 프롬프트 노드</button>
        <button class="g" id="pg-add-group">＋ ⬚ 프리 프롬프트 그룹</button>
        <button class="g ${PG.wrapMode ? "on" : ""}" id="pg-wrap-btn" title="활성화 후 캔버스에서 드래그해 노드/그룹을 점선 사각형으로 감쌉니다">⬚ 드래그로 감싸기</button>
        <button class="g ${PG.linkMode ? "on" : ""}" id="pg-link-btn" title="활성화 후 노드/그룹을 드래그하거나 차례로 클릭해 간선을 연결합니다 (Alt+드래그도 가능)">🔗 간선 연결</button>
        <button class="g ${isReversedDir() ? "on" : ""}" id="pg-edge-dir" title="전체 간선 화살표 방향 전환 (의존 방향 vs 실행 순서)">${isReversedDir() ? "⇄ 화살표: 실행 순서 (선행→후속)" : "⇄ 화살표: 의존 방향 (의존→선행)"}</button>
        <button class="g ${PG.showProfileOverlay ? "on" : ""}" id="pg-prof-btn" title="캔버스에 임계 경로·성능 배지 오버레이 표시">📊 프로파일</button>
        <button class="g" id="pg-auto-layout" title="중첩 N-그룹·간선 방향 기준으로 겹침 없이 자동 정렬">⊞ 자동 정렬</button>
        <button class="g" id="pg-mcp-manage" title="MCP 서버를 통해 노드·N-그룹·루프를 자동 구성·리팩토링 (후반기·복잡한 그래프에 권장)">🤖 MCP 관리</button>
        <span class="g-sep"></span>
        <select id="pg-preset"><option value="">템플릿 불러오기…</option>${Object.entries(PG.presetInfo).map(([k, v]) => `<option value="${esc(k)}">${esc(v.title)}</option>`).join("")}</select>
        <span class="g-sep"></span>
        <button class="g" id="pg-undo" title="실행 취소 (Ctrl+Z)" ${PG.undo.length ? "" : "disabled"}>↶</button>
        <button class="g" id="pg-redo" title="다시 실행 (Ctrl+Shift+Z)" ${PG.redo.length ? "" : "disabled"}>↷</button>
        <button class="g" id="pg-zout">−</button><span class="mono muted" id="pg-zoomlbl" style="min-width:38px;text-align:center">100%</span><button class="g" id="pg-zin">＋</button>
        <button class="g" id="pg-fit">맞춤</button>
        <button class="g ${PG.full ? "on" : ""}" id="pg-full">⛶</button>
        <span class="g-sep"></span>
        <button class="g" id="pg-validate">DAG 검증</button>
        <button class="g ${PG.json ? "on" : ""}" id="pg-json">{ } JSON</button>
        <button class="b" id="pg-run-all" style="margin-left:auto">▶ 전체 그래프 실행</button>
      </div>

      <div class="pg-wrap ${PG.full ? "pg-full" : ""}" id="pg-wrap">
        ${PG.json ? `<div class="g-jsonbox"><textarea id="pg-raw" spellcheck="false">${esc(JSON.stringify(PG.graph, null, 2))}</textarea><div class="row" style="padding:8px"><button class="b" id="pg-apply-json">JSON 적용</button></div></div>`
          : `<div class="pg-stagebox"><div class="pg-fullbar"><button class="g" id="pg-full-x">⛶ 닫기</button><button class="b" id="pg-run-all2">▶ 전체 실행</button></div><div id="pg-stage" class="pg-stage"></div></div>`}
        <div id="pg-insp" class="pg-insp"></div>
      </div>

      <div class="pg-run">
        <div class="card" style="margin:0">
          <b>실행 제어 & 이력</b>
          <div class="pg-hint" style="margin:6px 0">전체 그래프, 특정 프리 프롬프트 그룹, 또는 개별 노드만 선택해 실행할 수 있습니다.</div>
          <div class="row" style="margin-top:8px">
            <button class="b" id="pg-run-all3">▶ 전체 실행</button>
            <select id="pg-history" style="flex:1;min-width:0"><option value="">실행 기록…</option></select>
          </div>
          <div id="pg-runhead" style="margin-top:8px"></div>
        </div>
        <div class="card" style="margin:0">
          <b>실행 & MCP 조회 타임라인</b> <span class="muted" style="font-size:12px">의존성 순서·병렬 실행·MCP 서버 조회 내역이 실시간으로 표시됩니다</span>
          <div id="pg-feed" class="pg-feed" style="margin-top:8px"></div>
        </div>
      </div>`);

    $("#pg-select").onchange = e => {
      if (PG.dirty && !confirm("저장하지 않은 변경 사항이 있습니다. 계속할까요?")) { e.target.value = PG.graphId; return; }
      const item = PG.graphs.find(x => x.graph_id === e.target.value);
      PG.undo = []; PG.redo = []; PG.run = null;
      if (item) loadGraph(item.graph_json, item.graph_id, item.name);
      else loadGraph(PG.presets.basic_dependency, "", "새 프롬프트 그래프");
      G.render();
    };
    $("#pg-name").oninput = () => { PG.name = $("#pg-name").value; touch(); };
    $("#pg-save").onclick = run(save);
    if ($("#pg-del")) $("#pg-del").onclick = run(async () => {
      if (!confirm(`프롬프트 그래프 '${PG.name}'을(를) 삭제할까요?`)) return;
      await api("DELETE", `${WSP()}/prompt-graphs/${PG.graphId}`);
      PG.graph = null; PG.graphId = ""; PG.run = null; PG.undo = []; PG.redo = [];
      G.render();
    });

    $("#pg-add-node").onclick = () => {
      const st = $("#pg-stage");
      addPromptNode(((st?.scrollLeft || 0) + 140) / (PG.zoom || 1), ((st?.scrollTop || 0) + 100) / (PG.zoom || 1));
    };
    $("#pg-add-group").onclick = () => {
      const st = $("#pg-stage");
      addPrePromptGroup(((st?.scrollLeft || 0) + 90) / (PG.zoom || 1), ((st?.scrollTop || 0) + 70) / (PG.zoom || 1));
    };
    $("#pg-wrap-btn").onclick = () => {
      PG.wrapMode = !PG.wrapMode;
      if (PG.wrapMode) {
        PG.linkMode = false;
        PG.pendingLinkFrom = null;
        $("#pg-link-btn")?.classList.remove("on");
        $("#pg-stage")?.classList.remove("link-mode");
      }
      $("#pg-wrap-btn").classList.toggle("on", PG.wrapMode);
      $("#pg-stage")?.classList.toggle("wrap-mode", PG.wrapMode);
      if (PG.wrapMode) toast("캔버스에서 드래그하여 노드나 그룹을 점선 프리 프롬프트 사각형으로 감싸세요.");
    };
    $("#pg-link-btn").onclick = () => {
      PG.linkMode = !PG.linkMode;
      PG.pendingLinkFrom = null;
      if (PG.linkMode) {
        PG.wrapMode = false;
        $("#pg-wrap-btn")?.classList.remove("on");
        $("#pg-stage")?.classList.remove("wrap-mode");
      }
      $("#pg-link-btn").classList.toggle("on", PG.linkMode);
      $("#pg-stage")?.classList.toggle("link-mode", PG.linkMode);
      draw(true);
      if (PG.linkMode) toast(isReversedDir()
        ? "선행 실행할 노드/그룹에서 후속 노드/그룹으로 드래그하거나 차례로 클릭하세요."
        : "의존할 노드/그룹에서 대상 노드/그룹으로 드래그하거나 차례로 클릭하세요.");
    };
    $("#pg-edge-dir").onclick = () => {
      snapshotUndo();
      setEdgeDirection(isReversedDir() ? "dependency" : "execution");
      touch();
      toast(isReversedDir()
        ? "전체 화살표 방향: 실행 순서 (선행 노드 → 후속 노드 · 순환 루프 포함)."
        : "전체 화살표 방향: 의존 방향 (의존 노드 → 선행 노드).");
    };
  function openMcpManageModal() {
    $("#pg-mcp-modal")?.remove();
    const nc = PG.graph?.nodes?.length || 0;
    const gc = PG.graph?.groups?.length || 0;
    const isSmall = nc < 5 && gc < 2;
    const backdrop = document.createElement("div");
    backdrop.id = "pg-mcp-modal";
    backdrop.className = "pg-modal-backdrop";
    backdrop.innerHTML = `<div class="pg-modal" role="dialog" aria-modal="true" aria-label="MCP 그래프 자동 관리">
      <div class="pg-modal-hd">
        <h3>🤖 MCP 서버 기반 그래프 자동 관리 (자동 겹침 방지)</h3>
        <button type="button" class="icon" id="pg-mm-x" title="닫기">✕</button>
      </div>
      <div style="padding:14px 16px;display:flex;flex-direction:column;gap:10px;overflow:auto">
        ${isSmall ? `<div class="pg-chg-callout st-modified">
          <b>💡 초반 소규모 그래프 안내 (현재 노드 ${nc}개 · 그룹 ${gc}개)</b>
          <span class="pg-hint">노드 수가 적은 초반 단계에서는 캔버스에서 직접 추가·연결하는 편이 빠릅니다. 노드·계층이 커진 후반기 리팩토링에 특히 효과적입니다 (원하시면 바로 실행 가능합니다).</span>
        </div>` : `<div class="pg-chg-callout st-clean">
          <b>✓ 후반기/다계층 그래프 관리 준비됨 (노드 ${nc}개 · 그룹 ${gc}개)</b>
          <span class="pg-hint">모든 MCP 변경 후 자동으로 계층형 레이아웃(<code>auto_layout_graph</code>)을 실행해 그룹·노드·제목이 겹치지 않게 정리합니다.</span>
        </div>`}
        <div class="row" style="gap:6px;flex-wrap:wrap">
          <button type="button" class="g" data-pg-mm-pre="현재 그래프의 연관 노드들을 논리적 1-그룹/2-그룹 계층으로 묶고 겹침 없이 정렬해줘.">⬚ 그룹 계층화</button>
          <button type="button" class="g" data-pg-mm-pre="출력 계약(contract)과 조건부 순환 루프(최대 3회)를 검증 단계에 추가하고 간선 임시 프롬프트도 채워줘.">↺ 검증·순환 루프 구성</button>
          <button type="button" class="g" data-pg-mm-pre="현재 그래프의 의존성과 그룹 배치를 점검하고 겹침 없이 깔끔히 재정렬해줘.">⊞ 구조 점검 &amp; 정렬</button>
        </div>
        <label class="fld">
          <span>MCP 관리 에이전트에 전달할 작업 지시</span>
          <textarea id="pg-mm-inst" rows="4" style="min-height:92px" placeholder="예: 리서치/설계/검증 단계를 중첩 N-그룹으로 정리하고, 검증 실패 시 초안 노드로 되돌아가는 조건부 순환 루프와 간선 임시 프롬프트를 추가해줘."></textarea>
        </label>
        <div id="pg-mm-log" class="pg-feed" style="max-height:190px"></div>
      </div>
      <div class="pg-modal-ft">
        <span class="muted" style="font-size:11.5px;margin-right:auto">CLI MCP: <code>python -m irade_agent.agdd.prompt_graph_mcp_cli</code></span>
        <button type="button" class="g" id="pg-mm-cancel">닫기</button>
        <button type="button" class="b" id="pg-mm-run">🤖 MCP 에이전트 실행</button>
      </div>
    </div>`;
    document.body.appendChild(backdrop);
    const close = () => backdrop.remove();
    $("#pg-mm-x").onclick = close;
    $("#pg-mm-cancel").onclick = close;
    backdrop.onclick = ev => { if (ev.target === backdrop) close(); };
    backdrop.querySelectorAll("[data-pg-mm-pre]").forEach(b => {
      b.onclick = () => { $("#pg-mm-inst").value = b.dataset.pgMmPre; };
    });
    $("#pg-mm-run").onclick = run(async () => {
      const inst = ($("#pg-mm-inst").value || "").trim();
      if (!inst) return toast("작업 지시를 입력하세요.", true);
      const btn = $("#pg-mm-run");
      btn.disabled = true;
      btn.textContent = "⏳ MCP 관리 실행 중…";
      try {
        syncGraphRelations();
        const res = await api("POST", `${WSP()}/prompt-graphs/mcp-manage`, {
          graph: PG.graph,
          graph_id: PG.graphId || null,
          instruction: inst,
          save: false,
        });
        if (res.changed && res.graph) {
          snapshotUndo();
          PG.graph = ensureArrays(res.graph);
          PG.fitPending = true;
          touch();
          draw(true);
        }
        const okLayout = res.layout_report?.ok !== false;
        const callsHtml = (res.mcp_calls || []).map(c => `<div class="pg-ev mcp"><b>🔌 ${esc(c.tool)}</b> <span class="muted">${esc(JSON.stringify(c.arguments || {}))}</span></div>`).join("");
        $("#pg-mm-log").innerHTML = `<div class="pg-ev ${okLayout ? "fin" : "bad"}">
          <b>${okLayout ? "✓ 겹침 없음 검증 통과" : "⚠ 레이아웃 점검 필요"}</b> (${(res.mcp_calls || []).length}회 MCP 호출)
          <pre>${esc(res.summary || "완료됨")}</pre>
        </div>${callsHtml}`;
        toast(`MCP 그래프 관리 완료 (${(res.mcp_calls || []).length}회 호출 · 겹침 0건)`);
      } finally {
        btn.disabled = false;
        btn.textContent = "🤖 MCP 에이전트 실행";
      }
    });
  }

    $("#pg-prof-btn").onclick = () => {
      PG.showProfileOverlay = !PG.showProfileOverlay;
      $("#pg-prof-btn").classList.toggle("on", PG.showProfileOverlay);
      draw(false);
    };
    $("#pg-auto-layout").onclick = run(async () => {
      syncGraphRelations();
      const res = await api("POST", `${WSP()}/prompt-graphs/auto-layout`, {
        graph: PG.graph,
        graph_id: PG.graphId || null,
        direction: "auto",
        save: false,
      });
      if (res.graph) {
        snapshotUndo();
        PG.graph = ensureArrays(res.graph);
        PG.fitPending = true;
        touch();
        draw(true);
        const ov = res.layout_report?.overlap_pairs?.length || 0;
        toast(`자동 정렬 완료 (겹침 ${ov}건)`);
      }
    });
    $("#pg-mcp-manage").onclick = () => openMcpManageModal();
    $("#pg-preset").onchange = e => {
      const k = e.target.value;
      if (!k) return;
      if ((PG.dirty || PG.graph.nodes.length > 0) && !confirm("현재 캔버스를 선택한 템플릿으로 교체할까요?")) { e.target.value = ""; return; }
      snapshotUndo();
      const u = PG.undo, r = PG.redo;
      loadGraph(PG.presets[k], PG.graphId, PG.name);
      PG.undo = u; PG.redo = r;
      touch();
      G.render();
    };

    const setFull = on => {
      PG.full = on;
      $("#pg-wrap")?.classList.toggle("pg-full", on);
      $("#pg-full")?.classList.toggle("on", on);
      PG.fitPending = true;
      setTimeout(() => draw(), 30);
    };
    PG._setFull = setFull;
    $("#pg-undo").onclick = performUndo;
    $("#pg-redo").onclick = performRedo;
    $("#pg-full").onclick = () => setFull(!PG.full);
    if ($("#pg-full-x")) $("#pg-full-x").onclick = () => setFull(false);
    const zoomTo = z => { PG.zoom = Math.max(0.3, Math.min(1.6, Math.round(z * 10) / 10)); draw(); };
    $("#pg-zin").onclick = () => zoomTo((PG.zoom || 1) + 0.1);
    $("#pg-zout").onclick = () => zoomTo((PG.zoom || 1) - 0.1);
    $("#pg-fit").onclick = () => { PG.fitPending = true; draw(); };

    $("#pg-validate").onclick = run(async () => {
      syncGraphRelations();
      const r = await api("POST", `${WSP()}/prompt-graphs/validate`, { graph: PG.graph }, { noInvalidate: true });
      if (r.ok) {
        PG.graph = r.graph;
        draw(true);
        toast("프롬프트 그래프 DAG 검증 통과 ✓");
      } else {
        toast(r.error, true);
      }
    });
    $("#pg-json").onclick = () => { syncGraphRelations(); PG.json = !PG.json; G.render(); };
    if (PG.json) $("#pg-apply-json").onclick = run(async () => {
      snapshotUndo();
      const u = PG.undo, r = PG.redo;
      const parsed = JSON.parse($("#pg-raw").value);
      const val = await api("POST", `${WSP()}/prompt-graphs/validate`, { graph: parsed }, { noInvalidate: true });
      if (!val.ok) throw new Error(val.error || "유효하지 않은 그래프 JSON입니다.");
      loadGraph(val.graph, PG.graphId, PG.name);
      PG.undo = u; PG.redo = r;
      touch();
      PG.json = false;
      G.render();
    });

    $("#pg-run-all").onclick = run(() => startRun("all", null));
    if ($("#pg-run-all2")) $("#pg-run-all2").onclick = run(() => { setFull(false); return startRun("all", null); });
    $("#pg-run-all3").onclick = run(() => startRun("all", null));
    $("#pg-history").onchange = run(async e => {
      if (!e.target.value) return;
      const data = await api("GET", `${WSP()}/prompt-graph-runs/${e.target.value}`, undefined, { fresh: true });
      ingestRun(data, true);
      draw(true); drawFeed(); drawRunHead();
      if (data.status === "RUNNING") poll();
    });

    if (!PG._keyBound) {
      PG._keyBound = true;
      document.addEventListener("keydown", e => {
        if (G.getTAB() !== "pgraph" || e.isComposing || !PG.graph) return;
        const mod = e.ctrlKey || e.metaKey;
        const code = e.code || "";
        if (e.key === "Escape" && $("#pg-destroy-modal")) { e.preventDefault(); closeDestroyModal(); return; }
        if (e.key === "Escape" && (PG.pendingLinkFrom || PG.linkMode || PG.wrapMode)) {
          e.preventDefault();
          PG.pendingLinkFrom = null;
          PG.linkMode = false;
          PG.wrapMode = false;
          $("#pg-link-btn")?.classList.remove("on");
          $("#pg-wrap-btn")?.classList.remove("on");
          draw(true);
          return;
        }
        if (e.key === "Escape" && PG.full) { e.preventDefault(); PG._setFull?.(false); return; }
        if (mod && code === "KeyS") { e.preventDefault(); run(save)(); return; }
        const ae = document.activeElement;
        const tag = (ae?.tagName || "").toUpperCase();
        const isText = ae?.isContentEditable || tag === "TEXTAREA" || tag === "SELECT" ||
          (tag === "INPUT" && !["checkbox", "radio", "range", "color", "button", "submit", "reset"].includes((ae.type || "text").toLowerCase()));
        if (PG.json || isText) return;
        if (mod && code === "KeyZ") { e.preventDefault(); if (e.shiftKey) performRedo(); else performUndo(); return; }
        if (mod && code === "KeyY") { e.preventDefault(); performRedo(); return; }
        if ((code === "Delete" || code === "Backspace") && PG.sel) { e.preventDefault(); removeSelected(); }
      });
    }

    if (!PG.json) draw(true); else drawInspector();
    drawFeed();
    drawRunHead();
    refreshHistory();
    if (PG.run?.status === "RUNNING") poll();
    clearInterval(PG.anim);
    PG.anim = setInterval(() => {
      if (G.getTAB() !== "pgraph") return clearInterval(PG.anim);
      if (PG.flashes.length) draw(false);
    }, 900);
  };
})();
