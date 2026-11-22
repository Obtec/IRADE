(() => {
  const G = window.AGDD;
  const { $, esc, api, store, toast, run, stale, openTerminal, statusCls } = G;

  const COLLAB = G.COLLAB;
  COLLAB.mode = COLLAB.mode || store.get("agdd.collabmode") || "chat"; // chat | cli
  COLLAB.sendMode = COLLAB.sendMode || "agdd"; // agdd | cli_chat
  COLLAB.repo = COLLAB.repo || store.get("agdd.repo") || "";
  COLLAB.attachments = COLLAB.attachments || [];
  COLLAB.pollTimer = COLLAB.pollTimer || null;
  COLLAB.es = COLLAB.es || null;
  COLLAB.watchTerm = COLLAB.watchTerm || null;
  COLLAB.cliTerm = COLLAB.cliTerm || null;
  COLLAB.cliName = COLLAB.cliName || "main";
  COLLAB.sqlOut = COLLAB.sqlOut || "";
  COLLAB.renderedMode = COLLAB.renderedMode || "";
  COLLAB.renderedWs = COLLAB.renderedWs || "";
  COLLAB.feedSig = COLLAB.feedSig || "";
  COLLAB.filesSig = COLLAB.filesSig || "";
  COLLAB.watchRunId = COLLAB.watchRunId || "";
  COLLAB.pendingUx = COLLAB.pendingUx || [];
  COLLAB.msgUxMap = COLLAB.msgUxMap instanceof Map ? COLLAB.msgUxMap : new Map();

  function stopCollab() {
    clearTimeout(COLLAB.pollTimer); COLLAB.pollTimer = null;
    if (COLLAB.es) { try { COLLAB.es.close(); } catch {} COLLAB.es = null; }
    if (COLLAB.watchTerm) { try { COLLAB.watchTerm.stop(); } catch {} COLLAB.watchTerm = null; }
    if (COLLAB.cliTerm) { try { COLLAB.cliTerm.stop(); } catch {} COLLAB.cliTerm = null; }
    COLLAB.renderedMode = "";
    COLLAB.watchRunId = "";
    COLLAB.feedSig = "";
    COLLAB.filesSig = "";
  }
  COLLAB.stop = stopCollab;

  function renderRunStepper(runObj) {
    if (!runObj) return "";
    const steps = ["ambiguity", "dev", "semantic", "verify", "audit"];
    const cur = String(runObj.current_node || "").toLowerCase();
    const st = runObj.status;
    const curIdx = steps.indexOf(cur);
    return `<div class="collab-stepper">${steps.map((s, idx) => {
      const cls = st === "SUCCEEDED" || (curIdx > idx && st !== "FAILED") ? "done"
        : cur === s ? (st === "WAITING_HITL" ? "wait" : (st === "FAILED" || st === "ESCALATED" ? "bad" : "on")) : "";
      return `<span class="step-pill ${cls}">${esc(s)}</span>`;
    }).join("")}</div>`;
  }

  function renderMessage(m) {
    const meta = m.meta_json || {};
    const files = (meta.changed_files || []).map(f => `<button class="chip link-chip" data-open-ide="${esc(f)}">📄 ${esc(f)}</button>`).join(" ");
    const att = (meta.attachments || []).map(a => `<span class="chip">📎 ${esc(a.path)}</span>`).join(" ");
    let extra = "";
    if (meta.event === "UX_VISUAL_REQUEST" && meta.ux_request) {
      const ur = meta.ux_request;
      const msgKey = String(m.msg_id ?? `${m.created_at || ""}:${ur.view_key || "home"}`);
      COLLAB.msgUxMap.set(msgKey, { req: ur, runId: m.run_id || null });
      extra += `<div class="collab-card-ux">
        <div><b>🎨 2D UX 구성 요청</b> · 뷰 <span class="mono">${esc(ur.view_key || "home")}</span></div>
        <p style="margin:4px 0 8px">${esc(ur.prompt || "캔버스에서 UI 요소 위치·크기·관계·잠금을 지정해 주세요.")}</p>
        <button class="b" data-open-ux-msg="${esc(msgKey)}" data-open-ux="${esc(ur.view_key || "home")}" data-ux-prompt="${esc(ur.prompt || "")}">2D UX 캔버스에서 구성하기 →</button>
      </div>`;
    }
    if (meta.event === "PREFLIGHT_BLOCKED" && Array.isArray(meta.conflicts)) {
      extra += meta.conflicts.slice(0, 4).map(c =>
        `<div class="collab-conflict"><span class="chip ${c.severity === "CRITICAL" ? "bad" : "warn"}">${esc(c.severity)}</span> <b>${esc(c.conflict_type)}</b> ${esc((c.involved_ids || []).join(", "))}<div class="muted">${esc(c.explanation)}</div></div>`
      ).join("");
    }
    if (meta.last_patch) {
      extra += `<details style="margin-top:6px"><summary>변경 diff 보기</summary><pre style="max-height:180px;margin-top:6px">${esc(meta.last_patch)}</pre></details>`;
    }
    const roleLabel = m.role === "user" ? "나" : m.role === "event" ? "AGDD 그래프" : (m.mode === "cli_chat" ? "CLI 에이전트" : "AGDD 에이전트");
    const modeTag = m.mode === "ux_canvas" ? "2D UX" : m.mode === "cli_chat" ? "CLI" : "AGDD";
    return `<div class="cmsg cmsg-${esc(m.role)}">
      <div class="cmsg-head"><b>${esc(roleLabel)}</b> <span class="chip">${esc(modeTag)}</span> ${m.run_id ? `<span class="mono muted">${esc(String(m.run_id).slice(0, 8))}</span>` : ""} <span class="muted" style="margin-left:auto;font-size:11px">${esc(String(m.created_at || "").replace("T", " ").slice(11, 19))}</span></div>
      <div class="cmsg-body">${esc(m.content)}</div>
      ${att || files ? `<div class="row" style="margin-top:6px">${att}${files}</div>` : ""}
      ${extra}
    </div>`;
  }

  function attachLogStream(runId) {
    if (COLLAB.es) { try { COLLAB.es.close(); } catch {} COLLAB.es = null; }
    if (!runId || !$("#collab-logs")) return;
    const ctl = new AbortController();
    COLLAB.es = { close: () => ctl.abort() };
    fetch(`${G.getBASE()}/runs/${runId}/logs`, { headers: G.authHeaders(), signal: ctl.signal }).then(async res => {
      const rd = res.body.getReader(), dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await rd.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf("\n\n")) >= 0) {
          const ev = buf.slice(0, i); buf = buf.slice(i + 2);
          const line = ev.split("\n").find(l => l.startsWith("data: "));
          if (line && $("#collab-logs")) {
            $("#collab-logs").textContent += JSON.parse(line.slice(6)).line + "\n";
            $("#collab-logs").scrollTop = 1e9;
          }
        }
      }
    }).catch(() => {});
  }

  function renderAttChips() {
    const listEl = $("#c-att-list");
    if (!listEl) return;
    listEl.innerHTML = COLLAB.attachments.map((a, i) => `<span class="chip">${esc(a.path)} <button class="icon" data-rm-att="${i}">✕</button></span>`).join("");
    listEl.querySelectorAll("[data-rm-att]").forEach(b => b.onclick = () => {
      COLLAB.attachments.splice(+b.dataset.rmAtt, 1);
      renderAttChips();
    });
  }

  function statusChipsHtml(spec, s) {
    return `<span class="chip ${spec.conflict_passed ? "ok" : "warn"}">In-Memory DB v${esc(spec.version ?? 0)} · ACCEPT SRS ${spec.accept?.srs ?? 0} / ARC ${spec.accept?.arc ?? 0} / SCH ${spec.accept?.sch ?? 0}</span>
      <span class="chip">CLI: <b>${esc(s.cli || "claude")}</b>${s.model ? " · " + esc(s.model) : ""} · ${esc(s.effort || "medium")}</span>
      ${s.instructions ? `<span class="chip ok">튜닝 지침 적용됨</span>` : `<span class="chip">튜닝 지침 없음</span>`}`;
  }

  function uxAlertHtml(pendingUx) {
    if (!pendingUx.length) return "";
    return `<div class="card" style="border-color:var(--warn);background:color-mix(in srgb,var(--warn) 8%,var(--panel))">
      <div class="row" style="justify-content:space-between">
        <div><b>🎨 에이전트 2D UX 구성 요청 대기 중</b> · 뷰 <span class="mono">${esc(pendingUx[0].ux_request?.view_key || "home")}</span> — <span class="muted">${esc(pendingUx[0].ux_request?.prompt || "2D 캔버스에서 레이아웃을 지정하세요")}</span></div>
        <button class="b" id="c-open-pending-ux">2D UX 캔버스 열기 →</button>
      </div></div>`;
  }

  function bindUxAlert() {
    if ($("#c-open-pending-ux")) {
      $("#c-open-pending-ux").onclick = () => {
        const p = COLLAB.pendingUx[0];
        if (!p) return;
        G.openTab("ux", { uxView: p.ux_request?.view_key || "home", uxDecisionId: p.decision_id, uxPending: p.ux_request });
      };
    }
  }

  function bindFeedLinks(feedEl) {
    feedEl.querySelectorAll("[data-open-ide]").forEach(b => b.onclick = () => {
      G.IDE.pane = "files";
      G.IDE.repo = COLLAB.repo;
      G.IDE.pendingOpen = b.dataset.openIde;
      G.openTab("ide");
    });
    feedEl.querySelectorAll("[data-open-ux-msg]").forEach(b => b.onclick = () => {
      const entry = COLLAB.msgUxMap.get(b.dataset.openUxMsg) || {};
      const ur = entry.req || { view_key: b.dataset.openUx || "home", prompt: b.dataset.uxPrompt || "" };
      const match = COLLAB.pendingUx.find(p => (entry.runId && p.run_id === entry.runId) || p.ux_request?.view_key === (ur.view_key || "home"));
      G.openTab("ux", {
        uxView: ur.view_key || "home",
        uxDecisionId: match?.decision_id ?? null,
        uxPending: ur,
      });
    });
  }

  function patchCollabChatState(state, files) {
    const s = state.settings || {};
    const spec = state.spec_summary || {};
    COLLAB.pendingUx = state.pending_ux || [];

    if ($("#c-status-chips")) $("#c-status-chips").innerHTML = statusChipsHtml(spec, s);
    if ($("#c-tune")) $("#c-tune").textContent = `⚙ 에이전트 튜닝 (${s.cli || "claude"})`;
    if ($("#c-ux-alert")) {
      $("#c-ux-alert").innerHTML = uxAlertHtml(COLLAB.pendingUx);
      bindUxAlert();
    }

    const filesSig = files.slice(0, 300).join("\n");
    if ($("#c-att-pick") && filesSig !== COLLAB.filesSig) {
      COLLAB.filesSig = filesSig;
      const curPick = $("#c-att-pick").value;
      $("#c-att-pick").innerHTML = `<option value="">+ 파일/셀 첨부…</option>` + files.slice(0, 300).map(f => `<option value="${esc(f)}">${esc(f)}</option>`).join("");
      $("#c-att-pick").value = files.includes(curPick) ? curPick : "";
    }
    const cliOpt = $("#c-send-mode")?.querySelector('option[value="cli_chat"]');
    if (cliOpt) cliOpt.textContent = `직접 CLI 대화 (${s.cli || "claude"})`;

    const msgs = state.messages || [];
    const tail = msgs[msgs.length - 1];
    const nextFeedSig = `${msgs.length}:${tail?.msg_id || 0}:${String(tail?.content || "").length}`;
    const feed = $("#c-feed");
    if (feed && nextFeedSig !== COLLAB.feedSig) {
      const wasFirst = !COLLAB.feedSig;
      const nearBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 40;
      COLLAB.feedSig = nextFeedSig;
      COLLAB.msgUxMap.clear();
      feed.innerHTML = msgs.length
        ? msgs.map(renderMessage).join("")
        : `<div class="muted" style="padding:24px;text-align:center">대화를 시작해 보세요.<br><b>자율 그래프 개발(AGDD)</b>은 명세 사전검사 → Dev CLI → 의미론적 검증 → 테스트 → Audit을 자동 수행하며,<br>시각적 배치가 필요할 때 <b>2D UX 캔버스</b>로 연결됩니다.</div>`;
      bindFeedLinks(feed);
      if (wasFirst || nearBottom) feed.scrollTop = feed.scrollHeight;
    }

    const activeRun = (state.runs || [])[0] || null;
    if ($("#c-run-status")) {
      $("#c-run-status").innerHTML = activeRun
        ? `<span class="chip ${statusCls(activeRun.status)}">${esc(activeRun.status)}</span>`
        : `<span class="muted">없음</span>`;
    }
    if (activeRun) {
      if ($("#c-run-active")) $("#c-run-active").hidden = false;
      if ($("#c-run-empty")) $("#c-run-empty").hidden = true;
      if ($("#c-run-meta")) {
        $("#c-run-meta").innerHTML = `<div class="mono muted" style="font-size:11.5px;margin-bottom:6px">${esc(String(activeRun.run_id).slice(0, 8))} · ${esc(activeRun.route_mode)} · @ ${esc(activeRun.current_node)}</div>${renderRunStepper(activeRun)}`;
      }
      if (COLLAB.watchRunId !== activeRun.run_id) {
        if (COLLAB.watchTerm) { try { COLLAB.watchTerm.stop(); } catch {} COLLAB.watchTerm = null; }
        if ($("#c-watch-box")) $("#c-watch-box").hidden = true;
        if ($("#c-watch-term")) $("#c-watch-term").innerHTML = "";
        if ($("#collab-logs")) $("#collab-logs").textContent = "";
        COLLAB.watchRunId = activeRun.run_id;
        attachLogStream(activeRun.run_id);
      }
      clearTimeout(COLLAB.pollTimer);
      if (["PENDING", "RUNNING"].includes(activeRun.status)) {
        COLLAB.pollTimer = setTimeout(() => { if (G.getTAB() === "collab" && COLLAB.mode === "chat") renderCollab(); }, 3000);
      }
    } else {
      COLLAB.watchRunId = "";
      if (COLLAB.watchTerm) { try { COLLAB.watchTerm.stop(); } catch {} COLLAB.watchTerm = null; }
      if ($("#c-run-active")) $("#c-run-active").hidden = true;
      if ($("#c-run-empty")) $("#c-run-empty").hidden = false;
    }
  }

  async function renderCollab() {
    const WS = G.getWS();
    const state = await api("GET", `/workspaces/${WS}/collab/state`, undefined, { fresh: true });
    if (stale("collab")) return;
    const s = state.settings || {};
    const spec = state.spec_summary || {};
    if (!COLLAB.repo) COLLAB.repo = s.default_repo || "workspace";

    let files = [];
    if (state.vm_enabled) {
      try { files = (await api("GET", `/workspaces/${WS}/ide/repos/${encodeURIComponent(COLLAB.repo)}/tree`)).files || []; } catch {}
      if (stale("collab")) return;
    }

    COLLAB.pendingUx = state.pending_ux || [];
    const modeSeg = `<span class="seg" id="c-mode-seg">
      <button data-cm="chat" class="${COLLAB.mode === "chat" ? "on" : ""}">💬 대화 기반 (AGDD)</button>
      <button data-cm="cli" class="${COLLAB.mode === "cli" ? "on" : ""}">⌨ 순수 CLI</button>
    </span>`;

    const headerBar = `<div class="row" style="justify-content:space-between;margin-bottom:10px">
      <div><h2>협업 · CLI / 대화</h2><p class="sub" style="margin:0">명세 인메모리 DB · VM 파일/셀 · 2D UX 캔버스와 연결된 자율 그래프 에이전트 및 순수 CLI 협업 공간입니다.</p></div>
      <div class="row">
        ${modeSeg}
        <button class="g" id="c-sync-db">인메모리 DB 동기화</button>
        <button class="g" id="c-tune">⚙ 에이전트 튜닝 (${esc(s.cli || "claude")})</button>
      </div>
    </div>
    <div class="card row" style="padding:8px 12px;margin-bottom:10px;justify-content:space-between">
      <div class="row" id="c-status-chips">${statusChipsHtml(spec, s)}</div>
      <div class="row">
        <label class="muted" style="font-size:12px">저장소 <input id="c-repo" value="${esc(COLLAB.repo)}" size="12" style="padding:3px 6px;font-size:12px"></label>
        <button class="g" id="c-to-ux" style="padding:4px 10px;font-size:12px">🎨 2D UX 캔버스</button>
        <button class="g" id="c-to-ide" style="padding:4px 10px;font-size:12px">⌘ 파일 IDE</button>
      </div>
    </div>
    <div id="c-ux-alert">${uxAlertHtml(COLLAB.pendingUx)}</div>`;

    if (COLLAB.mode === "cli") {
      stopCollab();
      COLLAB.renderedMode = "cli";
      COLLAB.renderedWs = WS;
      G.V(`${headerBar}
        <div class="card" style="padding:10px 12px">
          <div class="row" style="margin-bottom:8px;justify-content:space-between">
            <div class="row">
              <b>순수 CLI 협업 터미널</b>
              <select id="c-tname"><option value="main" ${COLLAB.cliName === "main" ? "selected" : ""}>main</option><option value="t2" ${COLLAB.cliName === "t2" ? "selected" : ""}>t2</option><option value="t3" ${COLLAB.cliName === "t3" ? "selected" : ""}>t3</option></select>
              <button class="b" id="c-launch-cli">⚡ 튜닝된 ${esc(s.cli || "claude")} 실행</button>
              <button class="g" data-cli-cmd="agdd-spec status">agdd-spec status</button>
              <button class="g" data-cli-cmd="agdd-spec scope">agdd-spec scope</button>
              <button class="g" data-cli-cmd="agdd-spec files">셀 파일 목록</button>
              <button class="g" id="c-check-ux">2D UX 요청 확인</button>
            </div>
            <span class="muted" style="font-size:12px">DB: <span class="mono">/dev/shm/agdd-spec.db</span> · 셀: <span class="mono">/workspace/cells</span></span>
          </div>
          <div id="c-term" class="term" style="height:calc(100vh - 320px);min-height:460px"></div>
        </div>`);

      const openCli = n => {
        if (COLLAB.cliTerm) COLLAB.cliTerm.stop();
        $("#c-term").innerHTML = "";
        COLLAB.cliName = n;
        COLLAB.cliTerm = openTerminal($("#c-term"), n, false);
      };
      openCli(COLLAB.cliName);
      $("#c-tname").onchange = () => openCli($("#c-tname").value);
      $("#c-launch-cli").onclick = run(async () => {
        const r = await api("POST", `/workspaces/${WS}/collab/cli-launch`, { repo_key: COLLAB.repo }, { noInvalidate: true });
        COLLAB.cliTerm?.type(`${r.command}\r`);
        toast(`${r.cli} 실행 명령을 터미널에 전송했습니다`);
      });
      document.querySelectorAll("[data-cli-cmd]").forEach(b => b.onclick = () => COLLAB.cliTerm?.type(b.dataset.cliCmd + "\r"));
      $("#c-check-ux").onclick = run(async () => {
        const res = await api("GET", `/workspaces/${WS}/ux/pending-request`, undefined, { fresh: true });
        if (!res.pending?.length) return toast("대기 중인 2D UX 요청이 없습니다 (agdd-spec request-ux로 생성 가능)");
        const p = res.pending[0];
        G.openTab("ux", { uxView: p.ux_request?.view_key || "home", uxDecisionId: p.decision_id, uxPending: p.ux_request });
      });
      bindCommonControls();
      return;
    }

    // Conversational AGDD Mode: preserve composer input and terminal watch box across poll updates
    const shellMounted = COLLAB.renderedMode === "chat" && COLLAB.renderedWs === WS && !!$("#c-input") && !!$("#c-feed");
    if (shellMounted) {
      patchCollabChatState(state, files);
      return;
    }

    COLLAB.renderedMode = "chat";
    COLLAB.renderedWs = WS;
    COLLAB.feedSig = "";
    COLLAB.filesSig = "";
    COLLAB.watchRunId = "";

    G.V(`${headerBar}
      <div class="collab-grid">
        <div class="collab-main card" style="padding:0;display:flex;flex-direction:column;height:calc(100vh - 280px);min-height:500px">
          <div id="c-feed" class="collab-feed"></div>
          <div class="collab-composer">
            <div class="row" style="margin-bottom:6px;justify-content:space-between">
              <div class="row">
                <select id="c-send-mode">
                  <option value="agdd" ${COLLAB.sendMode === "agdd" ? "selected" : ""}>자율 그래프 개발 (AGDD 파이프라인)</option>
                  <option value="cli_chat" ${COLLAB.sendMode === "cli_chat" ? "selected" : ""}>직접 CLI 대화 (${esc(s.cli || "claude")})</option>
                </select>
                <select id="c-att-pick" style="max-width:210px"><option value="">+ 파일/셀 첨부…</option></select>
                <span id="c-att-list" class="row"></span>
              </div>
              <button class="g" id="c-clear" style="padding:3px 8px;font-size:12px">기록 지우기</button>
            </div>
            <div class="row" style="align-items:flex-end">
              <textarea id="c-input" style="flex:1;min-height:66px;max-height:160px;resize:vertical" placeholder="구현할 기능, 수정 요청, 또는 명세 기반 개발 목표를 입력하세요… (Ctrl+Enter 전송)"></textarea>
              <button class="b" id="c-send" style="height:40px;padding:0 18px">전송 ↵</button>
            </div>
          </div>
        </div>
        <div class="collab-rail">
          <div class="card" id="c-run-card">
            <div class="row" style="justify-content:space-between;margin-bottom:6px">
              <b>최근 AGDD 실행</b>
              <span id="c-run-status"></span>
            </div>
            <div id="c-run-active" hidden>
              <div id="c-run-meta"></div>
              <div class="row" style="margin-top:8px">
                <button class="g" id="c-watch-run" style="padding:4px 8px;font-size:12px">자동화 터미널 관전</button>
                <button class="g" id="c-refresh-state" style="padding:4px 8px;font-size:12px">새로고침</button>
              </div>
              <pre id="collab-logs" style="margin-top:8px;max-height:170px;font-size:11.5px"></pre>
              <div id="c-watch-box" style="margin-top:8px" hidden><div id="c-watch-term" class="term" style="height:200px"></div></div>
            </div>
            <p id="c-run-empty" class="muted" style="font-size:12px;margin:0">메시지를 보내면 자율 그래프 실행이 시작됩니다.</p>
          </div>
          <div class="card">
            <div class="row" style="justify-content:space-between;margin-bottom:6px"><b>In-Memory Spec DB 조회</b><span class="muted" style="font-size:11px">SQLite :memory:</span></div>
            <div class="row" style="margin-bottom:6px">
              <button class="g" data-qsql="SELECT req_id, domain, title, adoption_status FROM srs_requirements ORDER BY req_id" style="padding:3px 7px;font-size:11px">SRS</button>
              <button class="g" data-qsql="SELECT arch_id, layer, component_name, adoption_status FROM arch_definitions ORDER BY arch_id" style="padding:3px 7px;font-size:11px">ARC</button>
              <button class="g" data-qsql="SELECT schema_id, storage_engine, table_or_key_name FROM schema_definitions ORDER BY schema_id" style="padding:3px 7px;font-size:11px">SCH</button>
              <button class="g" data-qsql="SELECT source_type, source_id, relation_type, target_type, target_id FROM spec_traceability" style="padding:3px 7px;font-size:11px">추적</button>
            </div>
            <div class="row" style="margin-bottom:6px">
              <input id="c-sql" placeholder="SELECT ... 또는 FTS 검색어" style="flex:1;font-size:12px;padding:4px 6px" class="mono">
              <button class="g" id="c-sql-run" style="padding:4px 8px;font-size:12px">조회</button>
            </div>
            <pre id="c-sql-out" style="max-height:190px;font-size:11px">${esc(COLLAB.sqlOut || "버튼을 눌러 인메모리 DB를 즉시 조회하세요.")}</pre>
          </div>
        </div>
      </div>`);

    bindCommonControls();
    renderAttChips();

    $("#c-send-mode").onchange = () => { COLLAB.sendMode = $("#c-send-mode").value; };
    $("#c-att-pick").onchange = () => {
      const p = $("#c-att-pick").value; if (!p) return;
      if (!COLLAB.attachments.some(a => a.path === p)) COLLAB.attachments.push({ path: p });
      $("#c-att-pick").value = "";
      renderAttChips();
    };

    const doSend = run(async () => {
      const curWs = G.getWS();
      const text = $("#c-input").value.trim();
      if (!text && !COLLAB.attachments.length) return;
      $("#c-send").disabled = true;
      try {
        const res = await api("POST", `/workspaces/${curWs}/collab/send`, {
          mode: $("#c-send-mode").value,
          message: text,
          repo_key: COLLAB.repo,
          attachments: COLLAB.attachments,
        }, { noInvalidate: true });
        COLLAB.attachments = [];
        $("#c-input").value = "";
        renderAttChips();
        if (res.blocked) toast("사전 명세 충돌이 있어 중단되었습니다", true);
        else if (res.mode === "agdd") toast(`AGDD 실행 시작 (${res.route_mode})`);
        await renderCollab();
      } finally {
        if ($("#c-send")) $("#c-send").disabled = false;
      }
    });
    $("#c-send").onclick = doSend;
    $("#c-input").onkeydown = e => { if (e.key === "Enter" && !e.isComposing && (e.ctrlKey || e.metaKey)) { e.preventDefault(); doSend(); } };
    $("#c-clear").onclick = run(async () => {
      if (!confirm("협업 대화 기록을 모두 삭제할까요?")) return;
      await api("DELETE", `/workspaces/${G.getWS()}/collab/messages`);
      COLLAB.feedSig = "";
      await renderCollab();
    });

    const runQuery = run(async sqlOrFts => {
      const q = String(sqlOrFts || $("#c-sql").value).trim();
      if (!q) return;
      const isSql = /^(select|with)\b/i.test(q);
      const r = await api("POST", `/workspaces/${G.getWS()}/specs/query`, isSql ? { sql: q } : { fts: q }, { noInvalidate: true });
      COLLAB.sqlOut = JSON.stringify(isSql ? r.rows : r.matches, null, 2);
      if ($("#c-sql-out")) $("#c-sql-out").textContent = COLLAB.sqlOut;
    });
    $("#c-sql-run").onclick = () => runQuery();
    document.querySelectorAll("[data-qsql]").forEach(b => b.onclick = () => { $("#c-sql").value = b.dataset.qsql; runQuery(b.dataset.qsql); });

    $("#c-watch-run").onclick = () => {
      const box = $("#c-watch-box");
      if (!box || !COLLAB.watchRunId) return;
      box.hidden = !box.hidden;
      if (!box.hidden && !COLLAB.watchTerm) {
        $("#c-watch-term").innerHTML = "";
        COLLAB.watchTerm = openTerminal($("#c-watch-term"), `agdd-${String(COLLAB.watchRunId).slice(0, 8)}`, true);
      }
    };
    $("#c-refresh-state").onclick = () => renderCollab();

    patchCollabChatState(state, files);
  }

  function bindCommonControls() {
    $("#c-mode-seg").onclick = e => {
      const b = e.target.closest("[data-cm]"); if (!b) return;
      COLLAB.mode = b.dataset.cm; store.set("agdd.collabmode", COLLAB.mode);
      stopCollab(); renderCollab();
    };
    $("#c-repo").onchange = () => {
      COLLAB.repo = $("#c-repo").value.trim() || "workspace";
      store.set("agdd.repo", COLLAB.repo);
      if (COLLAB.mode === "chat") renderCollab();
    };
    $("#c-sync-db").onclick = run(async () => {
      const r = await api("POST", `/workspaces/${G.getWS()}/vm/sync-spec-db`, { repo_key: COLLAB.repo }, { noInvalidate: true });
      toast(`동기화됨: ${r.db_path || "ok"}`);
    });
    $("#c-tune").onclick = () => G.openTab("agents");
    $("#c-to-ux").onclick = () => G.openTab("ux");
    $("#c-to-ide").onclick = () => { G.IDE.pane = "files"; G.openTab("ide"); };
    bindUxAlert();
  }

  G.VIEWS.collab = renderCollab;
})();
