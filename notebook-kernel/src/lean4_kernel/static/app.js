/* Lean 4 + Python Proof Workspace × IRADE Workspace R&D Studio (VS Code Multi-Workspace Workbench UI) */
(function () {
  "use strict";

  const STORAGE_TOKEN_KEY = "agora_proof_jwt_token";
  const STORAGE_LOCAL_USER_KEY = "agora_proof_local_user";
  const STORAGE_OPEN_TABS_KEY = "agora_proof_open_workspaces";
  const STORAGE_ACTIVE_TAB_KEY = "agora_proof_active_workspace";

  const FEATURED_SYMBOLS = [
    ["∀", "forall"], ["∃", "exists"], ["→", "to"], ["↔", "iff"], ["←", "gets"],
    ["↦", "mapsto"], ["∧", "and"], ["∨", "or"], ["¬", "not"], ["⊢", "vdash"],
    ["⟨", "<"], ["⟩", ">"], ["≤", "le"], ["≥", "ge"], ["≠", "ne"],
    ["≡", "equiv"], ["≈", "approx"], ["·", "."], ["ℕ", "nat"], ["ℤ", "int"],
    ["ℚ", "rat"], ["ℝ", "real"], ["ℂ", "complex"], ["∅", "empty"], ["α", "alpha"],
    ["β", "beta"], ["γ", "gamma"], ["λ", "fun"], ["∈", "in"], ["⊆", "sub"],
  ];

  const state = {
    clientId: "client-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 7),
    token: localStorage.getItem(STORAGE_TOKEN_KEY) || "",
    localUser: localStorage.getItem(STORAGE_LOCAL_USER_KEY) || "local-researcher",
    userProfile: null,
    agoraStatus: null,
    redisInfo: null,
    unicodeMap: {},
    magicsMap: {},
    notebooks: [], // All workspaces from /api/workspaces
    openWorkspaceIds: [], // Ordered list of open workspace tab IDs
    openWorkspaces: {}, // Map of workspaceId -> { workspace, lifecycle, kernel, collaborators }
    eventSources: {}, // Map of workspaceId -> EventSource
    currentNotebook: null, // Active workspace object
    currentLifecycle: null,
    kernelStatus: null,
    collaborators: [],
    focusedCellId: null,
    focusedTextarea: null,
    markdownEditing: new Set(),
    pendingMfaToken: null,
    saveTimer: null,
    activeActivityPanel: "explorer",
    sidebarCollapsed: false,
  };

  // Check URL query params for seamless handoff from IRADE Workspace FE
  const urlParams = new URLSearchParams(window.location.search);
  if (urlParams.get("token")) {
    state.token = urlParams.get("token").trim();
    localStorage.setItem(STORAGE_TOKEN_KEY, state.token);
  }
  const initialCanvasId = urlParams.get("canvasId") || "";
  const initialNotebookId = urlParams.get("workspaceId") || urlParams.get("notebookId") || "";

  // DOM Elements
  const elWorkbench = document.getElementById("vscode-workbench");
  const elOpenWorkspacesList = document.getElementById("open-workspaces-list");
  const elOpenWorkspacesCount = document.getElementById("open-workspaces-count");
  const elNotebookList = document.getElementById("notebook-list");
  const elEditorTabsList = document.getElementById("editor-tabs-list");
  const elCellsContainer = document.getElementById("cells-container");
  const elTitleInput = document.getElementById("notebook-title-input");
  const elSaveIndicator = document.getElementById("save-status-indicator");
  const elRedisPill = document.getElementById("redis-lifecycle-pill");
  const elRealtimePill = document.getElementById("realtime-sync-pill");
  const elLinkedCanvasPill = document.getElementById("linked-canvas-pill");
  const elKernelDot = document.getElementById("kernel-status-dot");
  const elKernelText = document.getElementById("kernel-status-text");
  const elAgoraHealthPill = document.getElementById("agora-health-pill");
  const elUserAvatar = document.getElementById("user-avatar");
  const elUserName = document.getElementById("user-display-name");
  const elUserSub = document.getElementById("user-display-sub");
  const elLinkOpenAgora = document.getElementById("link-open-agora");
  const elSelectCanvas = document.getElementById("select-agora-canvas");
  const elInputCanvasId = document.getElementById("input-canvas-id");
  const elBridgeStatus = document.getElementById("canvas-bridge-status");
  const elUnicodePalette = document.getElementById("unicode-palette");
  const elMagicPalette = document.getElementById("magic-palette");
  const elPopover = document.getElementById("editor-popover");
  const elStatusRemote = document.getElementById("status-remote-label");
  const elStatusRedis = document.getElementById("status-redis-lifecycle");
  const elStatusSummary = document.getElementById("status-notebook-summary");
  const elStatusCursor = document.getElementById("status-cursor-pos");

  function showToast(message) {
    const container = document.getElementById("toast-container");
    if (!container) return;
    const toast = document.createElement("div");
    toast.className = "pn-toast";
    toast.textContent = message;
    container.appendChild(toast);
    setTimeout(() => {
      toast.remove();
    }, 3400);
  }

  function persistOpenTabsState() {
    try {
      localStorage.setItem(STORAGE_OPEN_TABS_KEY, JSON.stringify(state.openWorkspaceIds));
      if (state.currentNotebook && state.currentNotebook.id) {
        localStorage.setItem(STORAGE_ACTIVE_TAB_KEY, state.currentNotebook.id);
      } else {
        localStorage.removeItem(STORAGE_ACTIVE_TAB_KEY);
      }
    } catch (_) {}
  }

  function apiHeaders(extra = {}) {
    const headers = {
      "Content-Type": "application/json",
      "X-Proof-Client": state.clientId,
      ...extra,
    };
    if (state.token) {
      headers["Authorization"] = "Bearer " + state.token;
    }
    if (state.localUser) {
      headers["X-Proof-User"] = state.localUser;
    }
    return headers;
  }

  async function apiFetch(path, options = {}) {
    const resp = await fetch(path, {
      ...options,
      headers: apiHeaders(options.headers || {}),
    });
    const contentType = resp.headers.get("content-type") || "";
    const data = contentType.includes("application/json") ? await resp.json() : await resp.text();
    if (!resp.ok) {
      const msg = (data && data.error) ? data.error : `HTTP ${resp.status}`;
      throw new Error(msg);
    }
    return data;
  }

  function escapeHtml(str) {
    return String(str || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function cellLanguage(cell) {
    if (!cell || cell.cellType === "markdown") return "markdown";
    const lang = String(cell.language || "").toLowerCase();
    if (lang === "python" || lang === "python3" || lang === "py") return "python";
    if (lang === "rocq" || lang === "coq") return "rocq";
    return "lean4";
  }

  // Markdown + LaTeX math renderer for text cells
  function renderMarkdownHtml(source) {
    const raw = String(source || "").trim();
    if (!raw) {
      return "<span style='color:#858585;font-style:italic;'>더블클릭하여 마크다운/수식 연구 노트를 작성하세요...</span>";
    }

    const codeBlocks = [];
    let text = raw.replace(/```([a-zA-Z0-9_-]*)\n([\s\S]*?)```/g, function (_, lang, code) {
      const idx = codeBlocks.length;
      codeBlocks.push(`<pre><code class="lang-${escapeHtml(lang)}">${escapeHtml(code)}</code></pre>`);
      return `@@CODEBLOCK_${idx}@@`;
    });

    const mathBlocks = [];
    text = text.replace(/\$\$([\s\S]+?)\$\$/g, function (_, expr) {
      const idx = mathBlocks.length;
      mathBlocks.push(`<div class="pn-math-block">${escapeHtml(expr.trim())}</div>`);
      return `@@MATHBLOCK_${idx}@@`;
    });

    const lines = text.split("\n");
    const htmlLines = [];
    let inUl = false;
    let inOl = false;

    function closeLists() {
      if (inUl) {
        htmlLines.push("</ul>");
        inUl = false;
      }
      if (inOl) {
        htmlLines.push("</ol>");
        inOl = false;
      }
    }

    function formatInline(lineStr) {
      let s = escapeHtml(lineStr);
      s = s.replace(/\$([^$\n]+)\$/g, '<span class="pn-math-inline">$1</span>');
      s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
      s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
      s = s.replace(/\*([^*]+)\*/g, "<em>$1</em>");
      s = s.replace(
        /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
        '<a href="$2" target="_blank" rel="noopener" style="color:#3794ff;">$1</a>'
      );
      return s;
    }

    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      const trimmed = line.trim();
      if (!trimmed) {
        closeLists();
        continue;
      }
      if (/^@@(CODEBLOCK|MATHBLOCK)_\d+@@$/.test(trimmed)) {
        closeLists();
        htmlLines.push(trimmed);
        continue;
      }
      if (trimmed.startsWith("#### ")) {
        closeLists();
        htmlLines.push(`<h4>${formatInline(trimmed.slice(5))}</h4>`);
      } else if (trimmed.startsWith("### ")) {
        closeLists();
        htmlLines.push(`<h3>${formatInline(trimmed.slice(4))}</h3>`);
      } else if (trimmed.startsWith("## ")) {
        closeLists();
        htmlLines.push(`<h2>${formatInline(trimmed.slice(3))}</h2>`);
      } else if (trimmed.startsWith("# ")) {
        closeLists();
        htmlLines.push(`<h1>${formatInline(trimmed.slice(2))}</h1>`);
      } else if (trimmed.startsWith("- ") || trimmed.startsWith("* ")) {
        if (inOl) {
          htmlLines.push("</ol>");
          inOl = false;
        }
        if (!inUl) {
          htmlLines.push("<ul>");
          inUl = true;
        }
        htmlLines.push(`<li>${formatInline(trimmed.slice(2))}</li>`);
      } else if (/^\d+\.\s+/.test(trimmed)) {
        if (inUl) {
          htmlLines.push("</ul>");
          inUl = false;
        }
        if (!inOl) {
          htmlLines.push("<ol>");
          inOl = true;
        }
        htmlLines.push(`<li>${formatInline(trimmed.replace(/^\d+\.\s+/, ""))}</li>`);
      } else if (trimmed.startsWith("> ")) {
        closeLists();
        htmlLines.push(`<blockquote>${formatInline(trimmed.slice(2))}</blockquote>`);
      } else {
        closeLists();
        htmlLines.push(`<p>${formatInline(line)}</p>`);
      }
    }
    closeLists();

    let finalHtml = htmlLines.join("\n");
    finalHtml = finalHtml.replace(/@@CODEBLOCK_(\d+)@@/g, (_, idx) => codeBlocks[Number(idx)] || "");
    finalHtml = finalHtml.replace(/@@MATHBLOCK_(\d+)@@/g, (_, idx) => mathBlocks[Number(idx)] || "");
    return finalHtml;
  }

  // Mount HTML containing <script> tags (used by Lean 4 InfoView widget) so scripts execute
  function mountInteractiveHtml(container, rawHtml) {
    const wrapper = document.createElement("div");
    wrapper.style.height = "auto";
    wrapper.style.overflow = "visible";
    wrapper.innerHTML = rawHtml;
    const scripts = Array.from(wrapper.querySelectorAll("script"));
    scripts.forEach((oldScript) => {
      const newScript = document.createElement("script");
      Array.from(oldScript.attributes).forEach((attr) => {
        newScript.setAttribute(attr.name, attr.value);
      });
      newScript.textContent = oldScript.textContent;
      oldScript.parentNode.replaceChild(newScript, oldScript);
    });
    container.appendChild(wrapper);
  }

  function stripAnsi(str) {
    return String(str || "").replace(/\x1b\[[0-9;]*m/g, "");
  }

  function renderOutputsInto(container, outputs) {
    container.innerHTML = "";
    if (!Array.isArray(outputs) || outputs.length === 0) {
      return;
    }

    const hasHtmlErrorCard = outputs.some((o) => {
      const h = (o && o.data && o.data["text/html"]) || "";
      return h.includes("l4iv-sec-MsgErr") || h.includes("lean4-card-err");
    });

    outputs.forEach((out) => {
      if (!out || typeof out !== "object") return;
      const otype = out.output_type;

      if (otype === "stream") {
        const pre = document.createElement("pre");
        pre.className = "pn-output-stream " + (out.name === "stderr" ? "stderr" : "stdout");
        pre.textContent = stripAnsi(out.text || "");
        container.appendChild(pre);
      } else if (otype === "display_data" || otype === "execute_result") {
        const data = out.data || {};
        if (data["text/html"]) {
          mountInteractiveHtml(container, data["text/html"]);
        } else if (data["image/svg+xml"]) {
          const div = document.createElement("div");
          div.innerHTML = data["image/svg+xml"];
          container.appendChild(div);
        } else if (data["image/png"]) {
          const img = document.createElement("img");
          img.src = "data:image/png;base64," + data["image/png"];
          img.style.maxWidth = "100%";
          container.appendChild(img);
        } else if (data["text/markdown"]) {
          const div = document.createElement("div");
          div.className = "pn-md-preview";
          div.innerHTML = renderMarkdownHtml(data["text/markdown"]);
          container.appendChild(div);
        } else if (data["text/plain"]) {
          const pre = document.createElement("pre");
          pre.className = "pn-output-stream";
          pre.textContent = stripAnsi(data["text/plain"]);
          container.appendChild(pre);
        }
      } else if (otype === "error") {
        if (hasHtmlErrorCard) {
          return;
        }
        const pre = document.createElement("div");
        pre.className = "pn-output-error";
        const hdr = document.createElement("div");
        hdr.className = "pn-error-header";
        hdr.textContent = `✗ ${out.ename || "ExecutionError"}`;
        const body = document.createElement("div");
        const rawTb = Array.isArray(out.traceback) && out.traceback.length > 0
          ? out.traceback.join("\n")
          : `${out.evalue || ""}`;
        body.textContent = stripAnsi(rawTb);
        pre.appendChild(hdr);
        pre.appendChild(body);
        container.appendChild(pre);
      }
    });
  }

  function updateLifecycleBadge(lifecycle) {
    state.currentLifecycle = lifecycle || state.currentLifecycle;
    const lc = state.currentLifecycle;
    if (!elRedisPill || !elStatusRedis) return;

    if (!state.currentNotebook || !lc) {
      elRedisPill.className = "vscode-pill vscode-pill-redis cold";
      elRedisPill.textContent = "💾 Redis Standby";
      elStatusRedis.textContent = "Redis: Standby (COLD)";
      return;
    }

    const isHot = lc.state === "HOT" || lc.inRedis;
    const gen = lc.cacheGeneration ? ` · gen #${lc.cacheGeneration}` : "";
    if (isHot) {
      elRedisPill.className = "vscode-pill vscode-pill-redis hot";
      elRedisPill.textContent = `⚡ Redis HOT${gen}`;
      elRedisPill.title = `Workspace '${state.currentNotebook.title}' is mounted in Redis (${lc.redisBackend || "redis"})`;
      elStatusRedis.textContent = `⚡ Redis: HOT${gen}`;
    } else {
      elRedisPill.className = "vscode-pill vscode-pill-redis cold";
      elRedisPill.textContent = "💾 Disk COLD";
      elStatusRedis.textContent = "💾 Redis: COLD (Flushed to Disk)";
    }
  }

  function updateKernelBadge(kernel) {
    state.kernelStatus = kernel || state.kernelStatus;
    const k = state.kernelStatus || { state: "idle", alive: false, executionCount: 0, kernels: {} };
    elKernelDot.className = "vscode-kernel-dot " + (
      k.state === "busy" ? "busy" : (k.alive ? "" : "stopped")
    );
    const leanK = (k.kernels && k.kernels.lean4) || {};
    const pyK = (k.kernels && k.kernels.python) || {};
    const leanLabel = `∀ Lean 4 (${leanK.state || (k.alive ? "idle" : "ready")})`;
    const pyLabel = `🐍 Python 3 (${pyK.state || "ready"})`;
    const countLabel = typeof k.executionCount === "number" && k.executionCount > 0
      ? ` · #${k.executionCount}`
      : "";
    elKernelText.textContent = `${leanLabel} · ${pyLabel}${countLabel}`;
  }

  function updateSessionUI() {
    const user = state.userProfile || {};
    const agora = state.agoraStatus || {};
    if (agora.reachable) {
      elAgoraHealthPill.textContent = "Workspace Connected";
      elAgoraHealthPill.className = "vscode-pill vscode-pill-ok";
    } else {
      elAgoraHealthPill.textContent = "Workspace Offline";
      elAgoraHealthPill.className = "vscode-pill vscode-pill-warn";
    }
    if (agora.publicUrl) {
      elLinkOpenAgora.href = agora.publicUrl;
    }

    const displayName = user.nickname || user.userId || "Researcher";
    elUserAvatar.textContent = displayName.slice(0, 1).toUpperCase();
    elUserName.textContent = user.tagNumber && user.mode === "agora"
      ? `${displayName} #${user.tagNumber}`
      : displayName;
    elUserSub.textContent = user.authenticated
      ? `IRADE Workspace (${user.email || user.userId})`
      : `Local Workspace (${user.userId || "local"})`;

    if (elStatusRemote) {
      elStatusRemote.textContent = user.authenticated
        ? `⇄ Workspace: ${displayName}`
        : `⇄ Local R&D (${user.userId || "local"})`;
    }
  }

  // Render VS Code top multi-workspace tabs bar + Open Workspaces section in Sidebar
  function renderEditorTabs() {
    if (!elEditorTabsList) return;
    elEditorTabsList.innerHTML = "";

    state.openWorkspaceIds.forEach((wsId) => {
      const entry = state.openWorkspaces[wsId];
      const summary = state.notebooks.find((n) => n.id === wsId);
      const ws = (entry && entry.workspace) || summary;
      if (!ws) return;

      const isActive = Boolean(state.currentNotebook && state.currentNotebook.id === wsId);
      const lc = (entry && entry.lifecycle) || (summary && summary.lifecycle) || { state: "HOT" };
      const isHot = lc.state === "HOT" || lc.inRedis;

      const tab = document.createElement("div");
      tab.className = "vscode-tab" + (isActive ? " active" : "");
      tab.setAttribute("role", "tab");
      tab.setAttribute("aria-selected", isActive ? "true" : "false");
      tab.title = `${ws.title || wsId} — Redis ${isHot ? "HOT (Mounted)" : "COLD (Disk)"}`;

      const icon = document.createElement("span");
      icon.className = "vscode-tab-icon";
      icon.textContent = "∀";

      const name = document.createElement("span");
      const rawTitle = (isActive && state.currentNotebook.title) ? state.currentNotebook.title : (ws.title || wsId);
      name.textContent = rawTitle;

      const redisDot = document.createElement("span");
      redisDot.className = "vscode-tab-redis-dot" + (isHot ? "" : " cold");
      redisDot.title = isHot ? "Mounted in Redis (HOT)" : "Flushed to Disk (COLD)";

      const closeBtn = document.createElement("button");
      closeBtn.type = "button";
      closeBtn.className = "vscode-tab-close";
      closeBtn.title = "탭 닫기 및 Redis에서 디스크(COLD)로 언마운트";
      closeBtn.textContent = "×";
      closeBtn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        closeWorkspaceTab(wsId);
      });

      tab.appendChild(icon);
      tab.appendChild(name);
      tab.appendChild(redisDot);
      tab.appendChild(closeBtn);

      tab.addEventListener("click", () => {
        if (!isActive) {
          openOrSwitchWorkspace(wsId);
        }
      });

      elEditorTabsList.appendChild(tab);
    });

    // Also render OPEN WORKSPACES (REDIS HOT) list in the sidebar
    if (elOpenWorkspacesCount) {
      elOpenWorkspacesCount.textContent = String(state.openWorkspaceIds.length);
    }
    if (elOpenWorkspacesList) {
      elOpenWorkspacesList.innerHTML = "";
      if (state.openWorkspaceIds.length === 0) {
        const emptyHint = document.createElement("div");
        emptyHint.style.fontSize = "11px";
        emptyHint.style.color = "#858585";
        emptyHint.style.padding = "2px 4px";
        emptyHint.textContent = "열린 Workspace 탭이 없습니다.";
        elOpenWorkspacesList.appendChild(emptyHint);
      } else {
        state.openWorkspaceIds.forEach((wsId) => {
          const entry = state.openWorkspaces[wsId];
          const summary = state.notebooks.find((n) => n.id === wsId);
          const ws = (entry && entry.workspace) || summary;
          if (!ws) return;
          const isActive = Boolean(state.currentNotebook && state.currentNotebook.id === wsId);

          const row = document.createElement("div");
          row.className = "pn-nb-item" + (isActive ? " active" : "");

          const left = document.createElement("div");
          left.style.display = "flex";
          left.style.alignItems = "center";
          left.style.gap = "6px";
          left.style.overflow = "hidden";

          const hotBadge = document.createElement("span");
          hotBadge.className = "pn-redis-badge hot";
          hotBadge.textContent = "HOT";

          const titleSpan = document.createElement("span");
          titleSpan.className = "pn-nb-item-title";
          titleSpan.textContent = ws.title || wsId;

          left.appendChild(hotBadge);
          left.appendChild(titleSpan);

          const unmBtn = document.createElement("button");
          unmBtn.type = "button";
          unmBtn.className = "pn-nb-delete-btn";
          unmBtn.title = "탭 닫기 (Redis Unmount → COLD)";
          unmBtn.textContent = "×";
          unmBtn.addEventListener("click", (e) => {
            e.stopPropagation();
            closeWorkspaceTab(wsId);
          });

          row.appendChild(left);
          row.appendChild(unmBtn);
          row.addEventListener("click", () => openOrSwitchWorkspace(wsId));
          elOpenWorkspacesList.appendChild(row);
        });
      }
    }
  }

  function renderNotebookList() {
    elNotebookList.innerHTML = "";
    if (state.notebooks.length === 0) {
      const empty = document.createElement("div");
      empty.style.fontSize = "11.5px";
      empty.style.color = "#858585";
      empty.style.padding = "4px";
      empty.textContent = "생성된 Workspace가 없습니다. 상단 ＋ 버튼으로 새 Workspace를 만드세요.";
      elNotebookList.appendChild(empty);
      renderEditorTabs();
      return;
    }

    state.notebooks.forEach((nb) => {
      const item = document.createElement("div");
      const isActive = Boolean(state.currentNotebook && state.currentNotebook.id === nb.id);
      const isOpenInTab = state.openWorkspaceIds.includes(nb.id);
      const lc = nb.lifecycle || (state.openWorkspaces[nb.id] && state.openWorkspaces[nb.id].lifecycle) || {};
      const isHot = Boolean(lc.state === "HOT" || lc.inRedis || isOpenInTab);

      item.className = "pn-nb-item" + (isActive ? " active" : "");

      const left = document.createElement("div");
      const titleRow = document.createElement("div");
      titleRow.style.display = "flex";
      titleRow.style.alignItems = "center";
      titleRow.style.gap = "5px";

      const badge = document.createElement("span");
      badge.className = "pn-redis-badge " + (isHot ? "hot" : "cold");
      badge.textContent = isHot ? "HOT" : "COLD";

      const title = document.createElement("div");
      title.className = "pn-nb-item-title";
      title.textContent = nb.title || nb.id;

      titleRow.appendChild(badge);
      titleRow.appendChild(title);

      const meta = document.createElement("div");
      meta.className = "pn-nb-item-meta";
      const leanCnt = nb.leanCellCount !== undefined ? nb.leanCellCount : (nb.codeCellCount || 0);
      const pyCnt = nb.pythonCellCount || 0;
      const mdCnt = nb.markdownCellCount || 0;
      const canvasTag = nb.linkedCanvasId ? " · 🔗 Canvas" : "";
      meta.textContent = `${leanCnt} lean · ${pyCnt} py · ${mdCnt} md${canvasTag}`;

      left.appendChild(titleRow);
      left.appendChild(meta);

      const delBtn = document.createElement("button");
      delBtn.type = "button";
      delBtn.className = "pn-nb-delete-btn";
      delBtn.title = "Workspace 영구 삭제";
      delBtn.textContent = "🗑";
      delBtn.addEventListener("click", async (e) => {
        e.stopPropagation();
        try {
          if (state.openWorkspaceIds.includes(nb.id)) {
            await closeWorkspaceTab(nb.id, true);
          }
          await apiFetch(`/api/workspaces/${encodeURIComponent(nb.id)}`, { method: "DELETE" });
          await refreshNotebookListOnly();
          if (!state.currentNotebook) {
            renderCurrentNotebook();
          }
          showToast(`Workspace '${nb.title || nb.id}'가 삭제되었습니다.`);
        } catch (err) {
          showToast("삭제 실패: " + err.message);
        }
      });

      item.appendChild(left);
      item.appendChild(delBtn);
      item.addEventListener("click", () => openOrSwitchWorkspace(nb.id));
      elNotebookList.appendChild(item);
    });
    renderEditorTabs();
  }

  // Dynamically size code editor and line gutter so no lines are ever clipped
  function updateCodeEditorSizeAndGutter(textarea, gutterEl) {
    const linesCount = Math.max(1, (textarea.value || "").split("\n").length);
    const nums = [];
    for (let i = 1; i <= linesCount; i++) {
      nums.push(i);
    }
    gutterEl.textContent = nums.join("\n");

    textarea.style.height = "auto";
    const lineBasedHeight = linesCount * 21 + 22;
    const scrollBasedHeight = textarea.scrollHeight ? textarea.scrollHeight + 4 : 0;
    const targetH = Math.max(64, lineBasedHeight, scrollBasedHeight);
    textarea.style.height = targetH + "px";
  }

  // Dynamically size markdown editor textarea to its exact content height
  function updateMarkdownEditorSize(textarea) {
    const linesCount = Math.max(1, (textarea.value || "").split("\n").length);
    textarea.style.height = "auto";
    const lineBasedHeight = linesCount * 21 + 24;
    const scrollBasedHeight = textarea.scrollHeight ? textarea.scrollHeight + 6 : 0;
    const targetH = Math.max(88, lineBasedHeight, scrollBasedHeight);
    textarea.style.height = targetH + "px";
  }

  function updateCursorStatus(textarea) {
    if (!elStatusCursor || !textarea) return;
    const pos = typeof textarea.selectionStart === "number" ? textarea.selectionStart : 0;
    const upto = (textarea.value || "").slice(0, pos);
    const lines = upto.split("\n");
    const ln = lines.length;
    const col = lines[lines.length - 1].length;
    elStatusCursor.textContent = `Ln ${ln}, Col ${col}`;
  }

  function updateStatusSummary() {
    if (!elStatusSummary) return;
    if (!state.currentNotebook) {
      elStatusSummary.textContent = `${state.openWorkspaceIds.length} open workspaces`;
      return;
    }
    const cells = state.currentNotebook.cells || [];
    const leanCnt = cells.filter((c) => cellLanguage(c) === "lean4").length;
    const pyCnt = cells.filter((c) => cellLanguage(c) === "python").length;
    const mdCnt = cells.filter((c) => c.cellType === "markdown").length;
    elStatusSummary.textContent = `${cells.length} cells (${leanCnt} Lean 4 · ${pyCnt} Python · ${mdCnt} Markdown)`;
  }

  function scheduleAutoSave() {
    if (!state.currentNotebook) return;
    elSaveIndicator.textContent = "● Redis Syncing...";
    if (state.saveTimer) {
      clearTimeout(state.saveTimer);
    }
    state.saveTimer = setTimeout(() => {
      saveCurrentNotebook(true);
    }, 400);
  }

  function updateCollaboratorsUI(collaborators) {
    if (Array.isArray(collaborators)) {
      state.collaborators = collaborators;
    }
    const list = state.collaborators || [];
    const count = Math.max(1, list.length);
    if (elRealtimePill) {
      const names = list.map((c) => c.nickname || c.userId || "Researcher").join(", ");
      elRealtimePill.textContent = `⚡ Live Sync · 👥 ${count} Online`;
      elRealtimePill.title = names ? `접속 중인 연구자: ${names}` : "실시간 Redis 동기화 및 다중 사용자 협업 활성화됨";
    }
    document.querySelectorAll(".pn-collab-badge").forEach((el) => el.remove());
    list.forEach((c) => {
      if (!c || c.clientId === state.clientId || !c.activeCellId) return;
      const cellHeaderLeft = elCellsContainer.querySelector(
        `.pn-cell[data-cell-id="${c.activeCellId}"] .pn-cell-header-left`
      );
      if (cellHeaderLeft) {
        const badge = document.createElement("span");
        badge.className = "pn-collab-badge";
        badge.textContent = `👤 ${c.nickname || c.userId || "User"}`;
        cellHeaderLeft.appendChild(badge);
      }
    });
  }

  function sendPresenceUpdate(activeCellId) {
    if (!state.currentNotebook) return;
    apiFetch(`/api/workspaces/${encodeURIComponent(state.currentNotebook.id)}/presence`, {
      method: "POST",
      body: JSON.stringify({
        clientId: state.clientId,
        activeCellId: activeCellId || state.focusedCellId || null,
      }),
    })
      .then((res) => {
        if (res && res.collaborators) {
          updateCollaboratorsUI(res.collaborators);
        }
      })
      .catch(() => {});
  }

  function ensureWorkspaceEventStream(wsId) {
    if (typeof window.EventSource === "undefined") return;
    if (state.eventSources[wsId]) return;

    const params = new URLSearchParams();
    params.set("clientId", state.clientId);
    if (state.token) params.set("token", state.token);
    if (state.localUser) params.set("user", state.localUser);

    const url = `/api/workspaces/${encodeURIComponent(wsId)}/events?${params.toString()}`;
    const es = new EventSource(url);
    state.eventSources[wsId] = es;

    es.onmessage = (msgEvt) => {
      try {
        const evt = JSON.parse(msgEvt.data);
        if (!evt || typeof evt !== "object") return;
        const targetWsId = evt.workspaceId || evt.notebookId || wsId;
        const isCurrent = Boolean(state.currentNotebook && state.currentNotebook.id === targetWsId);

        if (evt.type === "connected") {
          if (evt.lifecycle && state.openWorkspaces[targetWsId]) {
            state.openWorkspaces[targetWsId].lifecycle = evt.lifecycle;
            if (isCurrent) updateLifecycleBadge(evt.lifecycle);
          }
          if (isCurrent && Array.isArray(evt.collaborators)) {
            updateCollaboratorsUI(evt.collaborators);
          }
          return;
        }

        if (evt.type === "workspace.lifecycle" && evt.data && evt.data.lifecycle) {
          if (state.openWorkspaces[targetWsId]) {
            state.openWorkspaces[targetWsId].lifecycle = evt.data.lifecycle;
          }
          if (isCurrent) {
            updateLifecycleBadge(evt.data.lifecycle);
          }
          renderNotebookList();
          return;
        }

        if (evt.type === "presence.updated" && evt.data && Array.isArray(evt.data.collaborators)) {
          if (state.openWorkspaces[targetWsId]) {
            state.openWorkspaces[targetWsId].collaborators = evt.data.collaborators;
          }
          if (isCurrent) {
            updateCollaboratorsUI(evt.data.collaborators);
          }
          return;
        }

        if (evt.type === "canvas.synced" && evt.data && isCurrent) {
          if (elLinkedCanvasPill && evt.data.canvasId) {
            elLinkedCanvasPill.style.display = "inline-flex";
            elLinkedCanvasPill.textContent = `🔗 Canvas Synced ✓ (${String(evt.data.canvasId).slice(0, 8)})`;
          }
          if (elBridgeStatus && evt.data.canvasUrl) {
            elBridgeStatus.innerHTML = `⚡ 실시간 캔버스 동기화됨 · <a href="${escapeHtml(evt.data.canvasUrl)}" target="_blank" rel="noopener">캔버스 열기 ↗</a>`;
          }
          return;
        }

        if (evt.senderClientId && evt.senderClientId === state.clientId) {
          return;
        }

        if (evt.type === "notebook.saved" && evt.data && (evt.data.workspace || evt.data.notebook)) {
          const incoming = evt.data.workspace || evt.data.notebook;
          if (state.openWorkspaces[targetWsId]) {
            state.openWorkspaces[targetWsId].workspace = incoming;
            if (evt.data.lifecycle) {
              state.openWorkspaces[targetWsId].lifecycle = evt.data.lifecycle;
            }
          }
          if (!isCurrent) {
            renderEditorTabs();
            return;
          }

          const activeEl = document.activeElement;
          const isTypingInCell = activeEl && (activeEl.classList.contains("pn-code-editor") || activeEl.classList.contains("pn-md-editor"));
          const activeCellSection = isTypingInCell ? activeEl.closest(".pn-cell") : null;
          const activeCellId = activeCellSection ? activeCellSection.dataset.cellId : null;
          const activeValue = isTypingInCell ? activeEl.value : null;
          const activeSelStart = isTypingInCell ? activeEl.selectionStart : null;
          const activeSelEnd = isTypingInCell ? activeEl.selectionEnd : null;

          state.currentNotebook = incoming;
          if (activeCellId && activeValue !== null) {
            const targetCell = (state.currentNotebook.cells || []).find((c) => c.id === activeCellId);
            if (targetCell) {
              targetCell.source = activeValue;
            }
          }
          renderCurrentNotebook();
          updateCollaboratorsUI(state.collaborators);
          elSaveIndicator.textContent = evt.sender && evt.sender.nickname
            ? `Redis Synced (${evt.sender.nickname})`
            : "Redis Synced";

          if (activeCellId) {
            const restoredTa = elCellsContainer.querySelector(`.pn-cell[data-cell-id="${activeCellId}"] textarea`);
            if (restoredTa) {
              restoredTa.focus();
              if (typeof activeSelStart === "number" && typeof activeSelEnd === "number") {
                restoredTa.selectionStart = activeSelStart;
                restoredTa.selectionEnd = activeSelEnd;
              }
            }
          }
          return;
        }

        if (evt.type === "cell.executing" && evt.data && evt.data.cellId && isCurrent) {
          const cid = evt.data.cellId;
          const cellEl = elCellsContainer.querySelector(`.pn-cell[data-cell-id="${cid}"]`);
          if (cellEl) {
            cellEl.className = "pn-cell status-running";
            const pill = cellEl.querySelector(".pn-cell-status-pill");
            if (pill) {
              const who = (evt.sender && evt.sender.nickname) ? ` (${evt.sender.nickname})` : "";
              pill.className = "pn-cell-status-pill running";
              pill.textContent = `⟳ Running${who}...`;
            }
          }
          return;
        }

        if (evt.type === "cell.executed" && evt.data && evt.data.cellId) {
          const d = evt.data;
          const targetEntry = state.openWorkspaces[targetWsId];
          if (targetEntry && targetEntry.workspace) {
            const cell = (targetEntry.workspace.cells || []).find((c) => c.id === d.cellId);
            if (cell) {
              if (typeof d.code === "string") cell.source = d.code;
              if (d.language) cell.language = d.language;
              cell.outputs = d.outputs || [];
              cell.executionCount = d.executionCount;
              cell.lastRunStatus = d.lastRunStatus || "ok";
            }
          }
          if (!isCurrent) return;

          if (d.kernel) updateKernelBadge(d.kernel);
          if (d.lifecycle) updateLifecycleBadge(d.lifecycle);
          const cellEl = elCellsContainer.querySelector(`.pn-cell[data-cell-id="${d.cellId}"]`);
          if (cellEl) {
            cellEl.className = `pn-cell status-${d.lastRunStatus || "ok"}`;
            const promptSpan = cellEl.querySelector(".pn-exec-Prompt");
            if (promptSpan) {
              promptSpan.textContent = `[${typeof d.executionCount === "number" ? d.executionCount : " "}]`;
            }
            const pill = cellEl.querySelector(".pn-cell-status-pill");
            if (pill) {
              pill.className = `pn-cell-status-pill ${d.lastRunStatus || "ok"}`;
              pill.textContent = d.lastRunStatus === "ok"
                ? (d.language === "python" ? "✓ Executed" : "✓ Verified")
                : "✗ Error";
            }
            const outputsWrap = cellEl.querySelector(".pn-cell-outputs");
            if (outputsWrap) {
              renderOutputsInto(outputsWrap, d.outputs || []);
            }
          }
        }
      } catch (_) {}
    };
  }

  function closeWorkspaceEventStream(wsId) {
    const es = state.eventSources[wsId];
    if (es) {
      try {
        es.close();
      } catch (_) {}
      delete state.eventSources[wsId];
    }
  }

  async function saveCurrentNotebook(silent = false) {
    if (!state.currentNotebook) return;
    if (state.saveTimer) {
      clearTimeout(state.saveTimer);
      state.saveTimer = null;
    }
    state.currentNotebook.title = elTitleInput.value.trim() || "Untitled Workspace";
    try {
      const res = await apiFetch(
        `/api/workspaces/${encodeURIComponent(state.currentNotebook.id)}`,
        {
          method: "PUT",
          body: JSON.stringify({ workspace: state.currentNotebook }),
        }
      );
      state.currentNotebook = res.workspace || res.notebook;
      state.openWorkspaces[state.currentNotebook.id] = {
        workspace: state.currentNotebook,
        lifecycle: res.lifecycle,
        kernel: res.kernel,
        collaborators: res.collaborators || state.collaborators,
      };
      updateKernelBadge(res.kernel);
      updateLifecycleBadge(res.lifecycle);
      elSaveIndicator.textContent = "Redis HOT · Saved";
      await refreshNotebookListOnly();
      updateStatusSummary();
      if (!silent) {
        showToast("Workspace가 Redis 및 디스크에 저장되었습니다.");
      }
    } catch (err) {
      elSaveIndicator.textContent = "Save Error";
      if (!silent) {
        showToast("저장 실패: " + err.message);
      }
    }
  }

  // Render VS Code Welcome / Workspace Picker when no workspace tab is currently open
  function renderWorkspaceWelcomePicker() {
    elTitleInput.value = "";
    elTitleInput.placeholder = "Select or Create a Workspace...";
    elSaveIndicator.textContent = "No Workspace";
    elLinkedCanvasPill.style.display = "none";
    updateLifecycleBadge(null);
    updateStatusSummary();
    renderEditorTabs();

    elCellsContainer.innerHTML = "";
    const screen = document.createElement("div");
    screen.className = "vscode-welcome-screen";

    const header = document.createElement("div");
    header.className = "vscode-welcome-header";
    header.innerHTML = `
      <h1 class="vscode-welcome-title">
        <span class="vscode-app-icon" style="width:28px;height:28px;font-size:16px;">∀</span>
        <span>Lean 4 &amp; Python R&amp;D Proof Studio</span>
      </h1>
      <p class="vscode-welcome-subtitle">
        VS Code에서 파일을 열듯 접속할 <strong>Workspace</strong>를 선택하거나 새로 생성하세요.
        열린 Workspace는 <code>IRADE Workspace</code>의 캔버스처럼 즉시 <strong>Redis (HOT)</strong>에 마운트되어 실시간 동기화되며,
        상단 탭을 여러 개 띄워 자유롭게 오가거나 탭을 닫아 디스크(<strong>COLD</strong>)로 내릴 수 있습니다.
      </p>
    `;
    screen.appendChild(header);

    const grid = document.createElement("div");
    grid.className = "vscode-welcome-grid";

    // Left column: Start / Create Workspace
    const leftCol = document.createElement("div");
    leftCol.innerHTML = `<h2 class="vscode-welcome-section-title">Start Workspace</h2>`;
    const actions = document.createElement("div");
    actions.className = "vscode-welcome-actions";

    const btnNewLean = document.createElement("button");
    btnNewLean.type = "button";
    btnNewLean.className = "vscode-welcome-action-btn";
    btnNewLean.innerHTML = `<span>∀</span><span><strong>새 Lean 4 증명 Workspace 생성</strong><br/><small style="color:#9d9d9d">형식 검증(Theorem Proving) + InfoView</small></span>`;
    btnNewLean.addEventListener("click", () => promptNewNotebook("lean4"));

    const btnNewPy = document.createElement("button");
    btnNewPy.type = "button";
    btnNewPy.className = "vscode-welcome-action-btn";
    btnNewPy.innerHTML = `<span>🐍</span><span><strong>새 Python 3 코드 Workspace 생성</strong><br/><small style="color:#9d9d9d">Python 수치/데이터 실험 + Lean 4 혼합</small></span>`;
    btnNewPy.addEventListener("click", () => promptNewNotebook("python"));

    const btnOpenIpynb = document.createElement("button");
    btnOpenIpynb.type = "button";
    btnOpenIpynb.className = "vscode-welcome-action-btn";
    btnOpenIpynb.innerHTML = `<span>📂</span><span><strong>로컬 .ipynb 파일 열기</strong><br/><small style="color:#9d9d9d">Jupyter 노트북을 Workspace로 마운트</small></span>`;
    btnOpenIpynb.addEventListener("click", () => document.getElementById("input-import-ipynb").click());

    actions.appendChild(btnNewLean);
    actions.appendChild(btnNewPy);
    actions.appendChild(btnOpenIpynb);
    leftCol.appendChild(actions);

    // Right column: Existing Workspaces list
    const rightCol = document.createElement("div");
    rightCol.innerHTML = `<h2 class="vscode-welcome-section-title">Select Workspace to Mount in Redis (${state.notebooks.length})</h2>`;

    if (state.notebooks.length === 0) {
      const emptyBox = document.createElement("div");
      emptyBox.className = "vscode-welcome-empty";
      emptyBox.innerHTML = `
        아직 저장된 Workspace가 없습니다.<br/>
        좌측의 <strong>새 Lean 4 증명 Workspace 생성</strong> 또는 <strong>새 Python 3 코드 Workspace 생성</strong>을 클릭하여
        빈 Workspace를 시작하세요.
      `;
      rightCol.appendChild(emptyBox);
    } else {
      const listWrap = document.createElement("div");
      listWrap.className = "vscode-welcome-ws-list";
      state.notebooks.forEach((ws) => {
        const lc = ws.lifecycle || {};
        const isHot = Boolean(lc.state === "HOT" || lc.inRedis);
        const row = document.createElement("div");
        row.className = "vscode-welcome-ws-row";

        const info = document.createElement("div");
        const name = document.createElement("div");
        name.className = "vscode-welcome-ws-name";
        name.innerHTML = `
          <span>∀ ${escapeHtml(ws.title || ws.id)}</span>
          <span class="pn-redis-badge ${isHot ? "hot" : "cold"}">${isHot ? "⚡ Redis HOT" : "💾 Disk COLD"}</span>
        `;
        const meta = document.createElement("div");
        meta.className = "vscode-welcome-ws-meta";
        const leanCnt = ws.leanCellCount !== undefined ? ws.leanCellCount : (ws.codeCellCount || 0);
        const pyCnt = ws.pythonCellCount || 0;
        const mdCnt = ws.markdownCellCount || 0;
        meta.textContent = `${leanCnt} Lean 4 · ${pyCnt} Python · ${mdCnt} Markdown · ID: ${ws.id}`;
        info.appendChild(name);
        info.appendChild(meta);

        const openBtn = document.createElement("button");
        openBtn.type = "button";
        openBtn.className = "vscode-btn vscode-btn-primary";
        openBtn.textContent = isHot ? "Open Tab (HOT)" : "⚡ Mount & Open";
        openBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          openOrSwitchWorkspace(ws.id);
        });

        row.appendChild(info);
        row.appendChild(openBtn);
        row.addEventListener("click", () => openOrSwitchWorkspace(ws.id));
        listWrap.appendChild(row);
      });
      rightCol.appendChild(listWrap);
    }

    grid.appendChild(leftCol);
    grid.appendChild(rightCol);
    screen.appendChild(grid);
    elCellsContainer.appendChild(screen);
  }

  function renderCurrentNotebook() {
    const nb = state.currentNotebook;
    if (!nb) {
      renderWorkspaceWelcomePicker();
      return;
    }

    elTitleInput.value = nb.title || "";
    elSaveIndicator.textContent = "Redis HOT · Saved";

    if (nb.linkedCanvasId) {
      elLinkedCanvasPill.style.display = "inline-flex";
      elLinkedCanvasPill.textContent = `🔗 Canvas: ${nb.linkedCanvasId.slice(0, 8)}...`;
      if (!elInputCanvasId.value) {
        elInputCanvasId.value = nb.linkedCanvasId;
      }
    } else {
      elLinkedCanvasPill.style.display = "none";
    }

    elCellsContainer.innerHTML = "";
    (nb.cells || []).forEach((cell, index) => {
      const cellEl = buildCellElement(cell, index);
      elCellsContainer.appendChild(cellEl);
    });

    requestAnimationFrame(() => {
      elCellsContainer.querySelectorAll("textarea.pn-code-editor").forEach((ta) => {
        const gutter = ta.parentElement && ta.parentElement.querySelector(".pn-line-gutter");
        if (gutter) updateCodeEditorSizeAndGutter(ta, gutter);
      });
      elCellsContainer.querySelectorAll("textarea.pn-md-editor").forEach((ta) => {
        updateMarkdownEditorSize(ta);
      });
    });

    renderEditorTabs();
    updateStatusSummary();
  }

  function buildCellElement(cell, index) {
    const lang = cellLanguage(cell);
    const isCode = lang !== "markdown";
    const isPython = lang === "python";
    const isRocq = lang === "rocq";

    const cellEl = document.createElement("section");
    cellEl.className = [
      "pn-cell",
      isCode ? `status-${cell.lastRunStatus || "idle"}` : "pn-cell-markdown-type",
      state.focusedCellId === cell.id ? "focused" : "",
    ].filter(Boolean).join(" ");
    cellEl.dataset.cellId = cell.id;

    cellEl.addEventListener("click", () => {
      const changed = state.focusedCellId !== cell.id;
      state.focusedCellId = cell.id;
      document.querySelectorAll(".pn-cell.focused").forEach((el) => el.classList.remove("focused"));
      cellEl.classList.add("focused");
      if (changed) {
        sendPresenceUpdate(cell.id);
      }
    });

    // Header (VS Code Notebook Cell Action Bar)
    const header = document.createElement("div");
    header.className = "pn-cell-header";

    const headerLeft = document.createElement("div");
    headerLeft.className = "pn-cell-header-left";

    if (isCode) {
      const gutterRunBtn = document.createElement("button");
      gutterRunBtn.type = "button";
      gutterRunBtn.className = "pn-run-gutter-btn";
      gutterRunBtn.textContent = "▶";
      gutterRunBtn.title = isPython
        ? "Python 코드 셀 실행 (Shift+Enter)"
        : (isRocq ? "Rocq (Coq) 증명 셀 실행 (Shift+Enter)" : "Lean 4 증명 셀 실행 (Shift+Enter)");
      gutterRunBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        executeCellById(cell.id, false);
      });
      headerLeft.appendChild(gutterRunBtn);
    }

    const promptSpan = document.createElement("span");
    promptSpan.className = "pn-exec-Prompt";
    promptSpan.textContent = isCode
      ? `[${typeof cell.executionCount === "number" ? cell.executionCount : " "}]`
      : `Markdown #${index + 1}`;

    const typeSelect = document.createElement("select");
    typeSelect.className = "pn-cell-type-select";
    typeSelect.innerHTML = `
      <option value="lean4" ${lang === "lean4" ? "selected" : ""}>∀ Lean 4 (Proof)</option>
      <option value="rocq" ${lang === "rocq" ? "selected" : ""}>🐓 Rocq (Coq)</option>
      <option value="python" ${lang === "python" ? "selected" : ""}>🐍 Python 3 (Code)</option>
      <option value="markdown" ${lang === "markdown" ? "selected" : ""}>¶ Markdown (Text)</option>
    `;
    typeSelect.addEventListener("change", () => {
      const chosen = typeSelect.value;
      if (chosen === "markdown") {
        cell.cellType = "markdown";
        cell.language = "markdown";
        cell.outputs = [];
        cell.executionCount = null;
        state.markdownEditing.add(cell.id);
      } else {
        cell.cellType = "code";
        cell.language = chosen;
      }
      renderCurrentNotebook();
      scheduleAutoSave();
    });

    const statusPill = document.createElement("span");
    const st = isCode ? (cell.lastRunStatus || "idle") : "idle";
    statusPill.className = `pn-cell-status-pill ${st}`;
    statusPill.textContent = isCode
      ? (st === "ok"
          ? (isPython ? "✓ Executed (Python)" : (isRocq ? "✓ Verified (Rocq)" : "✓ Verified (Lean 4)"))
          : st === "error"
            ? "✗ Error"
            : st === "running"
              ? (isPython ? "⟳ Running Python..." : "⟳ Proving...")
              : (isPython ? "Python Ready" : (isRocq ? "Rocq Ready" : "Lean 4 Ready")))
      : (state.markdownEditing.has(cell.id) ? "Editing Markdown" : "Rendered Markdown (더블클릭하여 편집)");

    headerLeft.appendChild(promptSpan);
    headerLeft.appendChild(typeSelect);
    headerLeft.appendChild(statusPill);

    const headerRight = document.createElement("div");
    headerRight.className = "pn-cell-header-right";

    if (isCode) {
      const runBtn = document.createElement("button");
      runBtn.type = "button";
      runBtn.className = "pn-cell-tool-btn primary";
      runBtn.textContent = isPython ? "▶ Run Python" : "▶ Run Proof";
      runBtn.title = "셀 실행 (Shift+Enter)";
      runBtn.addEventListener("click", () => executeCellById(cell.id, false));
      headerRight.appendChild(runBtn);

      const inspectBtn = document.createElement("button");
      inspectBtn.type = "button";
      inspectBtn.className = "pn-cell-tool-btn";
      inspectBtn.textContent = isPython ? "🔍 Inspect" : (isRocq ? "🔍 Check" : "🔍 #check");
      inspectBtn.title = "커서 위치의 심볼/타입 확인 (Shift+Tab)";
      inspectBtn.addEventListener("click", () => {
        const ta = cellEl.querySelector("textarea.pn-code-editor");
        if (ta) triggerInspect(cell, ta);
      });
      headerRight.appendChild(inspectBtn);
    } else {
      const isEditing = state.markdownEditing.has(cell.id);
      const toggleMdBtn = document.createElement("button");
      toggleMdBtn.type = "button";
      toggleMdBtn.className = "pn-cell-tool-btn primary";
      toggleMdBtn.textContent = isEditing ? "✓ Done (Preview)" : "✎ Edit Markdown";
      toggleMdBtn.addEventListener("click", () => {
        if (state.markdownEditing.has(cell.id)) {
          state.markdownEditing.delete(cell.id);
        } else {
          state.markdownEditing.add(cell.id);
        }
        renderCurrentNotebook();
        scheduleAutoSave();
      });
      headerRight.appendChild(toggleMdBtn);
    }

    const addLeanBelowBtn = document.createElement("button");
    addLeanBelowBtn.type = "button";
    addLeanBelowBtn.className = "pn-cell-tool-btn";
    addLeanBelowBtn.textContent = "+ Lean 4";
    addLeanBelowBtn.title = "아래에 Lean 4 증명 셀 추가";
    addLeanBelowBtn.addEventListener("click", () => insertCellAt(index + 1, "code", "lean4"));

    const addRocqBelowBtn = document.createElement("button");
    addRocqBelowBtn.type = "button";
    addRocqBelowBtn.className = "pn-cell-tool-btn";
    addRocqBelowBtn.textContent = "+ Rocq";
    addRocqBelowBtn.title = "아래에 Rocq (Coq) 증명 셀 추가";
    addRocqBelowBtn.addEventListener("click", () => insertCellAt(index + 1, "code", "rocq"));

    const addPyBelowBtn = document.createElement("button");
    addPyBelowBtn.type = "button";
    addPyBelowBtn.className = "pn-cell-tool-btn";
    addPyBelowBtn.textContent = "+ Python";
    addPyBelowBtn.title = "아래에 Python 3 코드 셀 추가";
    addPyBelowBtn.addEventListener("click", () => insertCellAt(index + 1, "code", "python"));

    const addMdBelowBtn = document.createElement("button");
    addMdBelowBtn.type = "button";
    addMdBelowBtn.className = "pn-cell-tool-btn";
    addMdBelowBtn.textContent = "+ Markdown";
    addMdBelowBtn.title = "아래에 텍스트 셀 추가";
    addMdBelowBtn.addEventListener("click", () => insertCellAt(index + 1, "markdown", "markdown"));

    const upBtn = document.createElement("button");
    upBtn.type = "button";
    upBtn.className = "pn-cell-tool-btn";
    upBtn.textContent = "↑";
    upBtn.disabled = index === 0;
    upBtn.addEventListener("click", () => moveCell(index, -1));

    const downBtn = document.createElement("button");
    downBtn.type = "button";
    downBtn.className = "pn-cell-tool-btn";
    downBtn.textContent = "↓";
    downBtn.disabled = index === (state.currentNotebook.cells.length - 1);
    downBtn.addEventListener("click", () => moveCell(index, 1));

    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.className = "pn-cell-tool-btn";
    delBtn.textContent = "🗑";
    delBtn.title = "셀 삭제";
    delBtn.addEventListener("click", () => deleteCellAt(index));

    headerRight.appendChild(addLeanBelowBtn);
    headerRight.appendChild(addRocqBelowBtn);
    headerRight.appendChild(addPyBelowBtn);
    headerRight.appendChild(addMdBelowBtn);
    headerRight.appendChild(upBtn);
    headerRight.appendChild(downBtn);
    headerRight.appendChild(delBtn);

    header.appendChild(headerLeft);
    header.appendChild(headerRight);
    cellEl.appendChild(header);

    // Body
    if (isCode) {
      const editorWrap = document.createElement("div");
      editorWrap.className = "pn-editor-wrap";

      const gutter = document.createElement("div");
      gutter.className = "pn-line-gutter";

      const textarea = document.createElement("textarea");
      textarea.className = "pn-code-editor";
      textarea.spellcheck = false;
      textarea.value = cell.source || "";
      textarea.placeholder = isPython
        ? "# Python 3 코드를 입력하세요 (예: import math, print(...), 리스트/행렬 계산 등)"
        : (isRocq
            ? "(* Rocq (Coq) 증명 또는 정의를 입력하세요 (예: Lemma, Definition, Proof. ... Qed.) *)"
            : "-- Lean 4 증명 또는 명령을 입력하세요 (예: theorem, def, #eval, #check, %env)");
      updateCodeEditorSizeAndGutter(textarea, gutter);

      textarea.addEventListener("focus", () => {
        state.focusedCellId = cell.id;
        state.focusedTextarea = textarea;
        document.querySelectorAll(".pn-cell.focused").forEach((el) => el.classList.remove("focused"));
        cellEl.classList.add("focused");
        updateCursorStatus(textarea);
      });

      textarea.addEventListener("click", () => updateCursorStatus(textarea));
      textarea.addEventListener("keyup", () => updateCursorStatus(textarea));

      textarea.addEventListener("input", () => {
        cell.source = textarea.value;
        updateCodeEditorSizeAndGutter(textarea, gutter);
        updateCursorStatus(textarea);
        scheduleAutoSave();
      });

      textarea.addEventListener("keydown", (ev) => handleCodeEditorKeydown(ev, cell, textarea));

      editorWrap.appendChild(gutter);
      editorWrap.appendChild(textarea);
      cellEl.appendChild(editorWrap);

      const outputsWrap = document.createElement("div");
      outputsWrap.className = "pn-cell-outputs";
      cellEl.appendChild(outputsWrap);
      renderOutputsInto(outputsWrap, cell.outputs || []);
    } else {
      const mdContainer = document.createElement("div");
      mdContainer.className = "pn-md-cell-container";

      const isEditing = state.markdownEditing.has(cell.id);
      if (isEditing) {
        const mdWrap = document.createElement("div");
        mdWrap.className = "pn-md-editor-wrap";
        const ta = document.createElement("textarea");
        ta.className = "pn-md-editor";
        ta.value = cell.source || "";
        ta.placeholder = "# 마크다운 제목, 설명, 수식 ($x^2 + y^2 = z^2$) 작성... (Shift+Enter로 렌더링 완료)";
        updateMarkdownEditorSize(ta);

        ta.addEventListener("focus", () => {
          state.focusedCellId = cell.id;
          state.focusedTextarea = ta;
          updateCursorStatus(ta);
        });
        ta.addEventListener("input", () => {
          cell.source = ta.value;
          updateMarkdownEditorSize(ta);
          updateCursorStatus(ta);
          scheduleAutoSave();
        });
        ta.addEventListener("keydown", (ev) => {
          if (ev.key === "Enter" && (ev.shiftKey || ev.ctrlKey || ev.metaKey)) {
            ev.preventDefault();
            state.markdownEditing.delete(cell.id);
            renderCurrentNotebook();
            scheduleAutoSave();
          } else if (ev.key === "Tab" && !ev.shiftKey) {
            if (tryExpandBackslashUnicode(ta)) {
              ev.preventDefault();
              cell.source = ta.value;
              updateMarkdownEditorSize(ta);
              scheduleAutoSave();
            }
          }
        });
        mdWrap.appendChild(ta);
        mdContainer.appendChild(mdWrap);
      } else {
        const preview = document.createElement("div");
        preview.className = "pn-md-preview";
        preview.title = "더블클릭하여 텍스트 셀 편집";
        preview.innerHTML = renderMarkdownHtml(cell.source || "");
        preview.addEventListener("dblclick", () => {
          state.markdownEditing.add(cell.id);
          renderCurrentNotebook();
          requestAnimationFrame(() => {
            const ta = elCellsContainer.querySelector(`.pn-cell[data-cell-id="${cell.id}"] textarea.pn-md-editor`);
            if (ta) ta.focus();
          });
        });
        mdContainer.appendChild(preview);
      }

      cellEl.appendChild(mdContainer);
    }

    return cellEl;
  }

  function tryExpandBackslashUnicode(textarea) {
    const pos = textarea.selectionStart;
    const before = textarea.value.slice(0, pos);
    const bsIdx = before.lastIndexOf("\\");
    if (bsIdx === -1) return false;
    const fragment = before.slice(bsIdx + 1);
    if (!fragment || /\s/.test(fragment)) return false;

    const exact = state.unicodeMap["\\" + fragment] || state.unicodeMap[fragment];
    if (exact) {
      textarea.value = textarea.value.slice(0, bsIdx) + exact + textarea.value.slice(pos);
      const newPos = bsIdx + exact.length;
      textarea.selectionStart = newPos;
      textarea.selectionEnd = newPos;
      return true;
    }
    return false;
  }

  async function handleCodeEditorKeydown(ev, cell, textarea) {
    if (ev.key === "Escape") {
      hidePopover();
      return;
    }
    if (ev.key === "Enter" && ev.shiftKey) {
      ev.preventDefault();
      hidePopover();
      await executeCellById(cell.id, true);
      return;
    }
    if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) {
      ev.preventDefault();
      hidePopover();
      await executeCellById(cell.id, false);
      return;
    }
    if (ev.key === "Tab" && ev.shiftKey) {
      ev.preventDefault();
      await triggerInspect(cell, textarea);
      return;
    }
    if (ev.key === "Tab" && !ev.shiftKey) {
      ev.preventDefault();
      if (tryExpandBackslashUnicode(textarea)) {
        cell.source = textarea.value;
        const gutter = textarea.parentElement.querySelector(".pn-line-gutter");
        if (gutter) updateCodeEditorSizeAndGutter(textarea, gutter);
        scheduleAutoSave();
        return;
      }
      await triggerCompletion(cell, textarea);
    }
  }

  function showPopoverNearTextarea(textarea, htmlBuilder) {
    const rect = textarea.getBoundingClientRect();
    elPopover.innerHTML = "";
    htmlBuilder(elPopover);
    elPopover.style.display = "block";
    elPopover.style.left = Math.min(window.innerWidth - 460, Math.max(16, rect.left + 48)) + "px";
    elPopover.style.top = Math.min(window.innerHeight - 280, rect.bottom - 10) + "px";
  }

  function hidePopover() {
    elPopover.style.display = "none";
    elPopover.innerHTML = "";
  }

  async function triggerCompletion(cell, textarea) {
    if (!state.currentNotebook) return;
    const pos = textarea.selectionStart;
    const lang = cellLanguage(cell);
    try {
      const res = await apiFetch(
        `/api/workspaces/${encodeURIComponent(state.currentNotebook.id)}/complete`,
        {
          method: "POST",
          body: JSON.stringify({ code: textarea.value, cursorPos: pos, language: lang }),
        }
      );
      const matches = res.matches || [];
      if (matches.length === 0) {
        const start = textarea.selectionStart;
        const end = textarea.selectionEnd;
        textarea.value = textarea.value.slice(0, start) + "  " + textarea.value.slice(end);
        textarea.selectionStart = textarea.selectionEnd = start + 2;
        cell.source = textarea.value;
        return;
      }
      if (matches.length === 1) {
        applyCompletionMatch(cell, textarea, res.cursorStart, res.cursorEnd, matches[0].insert);
        return;
      }
      showPopoverNearTextarea(textarea, (box) => {
        matches.forEach((m) => {
          const row = document.createElement("div");
          row.className = "pn-comp-item";
          const labelSpan = document.createElement("span");
          labelSpan.textContent = m.label || m.insert;
          const kindSpan = document.createElement("span");
          kindSpan.className = "pn-comp-kind";
          kindSpan.textContent = m.kind || "";
          row.appendChild(labelSpan);
          row.appendChild(kindSpan);
          row.addEventListener("click", () => {
            applyCompletionMatch(cell, textarea, res.cursorStart, res.cursorEnd, m.insert);
            hidePopover();
          });
          box.appendChild(row);
        });
      });
    } catch (_) {}
  }

  function applyCompletionMatch(cell, textarea, start, end, insertText) {
    textarea.value = textarea.value.slice(0, start) + insertText + textarea.value.slice(end);
    const nextPos = start + insertText.length;
    textarea.focus();
    textarea.selectionStart = textarea.selectionEnd = nextPos;
    cell.source = textarea.value;
    const gutter = textarea.parentElement.querySelector(".pn-line-gutter");
    if (gutter) updateCodeEditorSizeAndGutter(textarea, gutter);
    scheduleAutoSave();
  }

  async function triggerInspect(cell, textarea) {
    if (!state.currentNotebook) return;
    const pos = textarea.selectionStart;
    const lang = cellLanguage(cell);
    try {
      const res = await apiFetch(
        `/api/workspaces/${encodeURIComponent(state.currentNotebook.id)}/inspect`,
        {
          method: "POST",
          body: JSON.stringify({ code: textarea.value, cursorPos: pos, language: lang }),
        }
      );
      showPopoverNearTextarea(textarea, (box) => {
        if (res.found && (res.html || res.text)) {
          if (res.html) {
            const div = document.createElement("div");
            div.innerHTML = res.html;
            box.appendChild(div);
          } else {
            const pre = document.createElement("pre");
            pre.style.margin = "4px";
            pre.style.fontFamily = "var(--vsc-font-mono)";
            pre.textContent = stripAnsi(res.text);
            box.appendChild(pre);
          }
        } else {
          const msg = document.createElement("div");
          msg.style.padding = "6px 8px";
          msg.style.color = "#9d9d9d";
          msg.textContent = "커서 위치에서 심볼/타입 정보를 찾을 수 없습니다.";
          box.appendChild(msg);
        }
      });
    } catch (err) {
      showToast("심볼 검사 실패: " + err.message);
    }
  }

  async function executeCellById(cellId, advanceFocus = false) {
    if (!state.currentNotebook) return;
    const cells = state.currentNotebook.cells || [];
    const idx = cells.findIndex((c) => c.id === cellId);
    if (idx === -1) return;
    const cell = cells[idx];
    const lang = cellLanguage(cell);
    if (lang === "markdown") {
      state.markdownEditing.delete(cell.id);
      renderCurrentNotebook();
      return;
    }

    cell.lastRunStatus = "running";
    updateKernelBadge({ ...(state.kernelStatus || {}), state: "busy", alive: true });

    const cellEl = elCellsContainer.querySelector(`.pn-cell[data-cell-id="${cell.id}"]`);
    if (cellEl) {
      cellEl.className = "pn-cell status-running focused";
      const pill = cellEl.querySelector(".pn-cell-status-pill");
      if (pill) {
        pill.className = "pn-cell-status-pill running";
        pill.textContent = lang === "python" ? "⟳ Running Python..." : "⟳ Proving...";
      }
    }

    try {
      const res = await apiFetch(
        `/api/workspaces/${encodeURIComponent(state.currentNotebook.id)}/execute`,
        {
          method: "POST",
          body: JSON.stringify({
            cellId: cell.id,
            language: lang,
            code: cell.source || "",
            persist: true,
          }),
        }
      );

      cell.outputs = res.outputs || [];
      cell.executionCount = res.executionCount;
      cell.lastRunStatus = res.exitCode === 0 ? "ok" : "error";
      updateKernelBadge(res.kernel);
      if (res.lifecycle) updateLifecycleBadge(res.lifecycle);
      elSaveIndicator.textContent = "Redis HOT · Saved";

      if (cellEl) {
        cellEl.className = `pn-cell status-${cell.lastRunStatus} focused`;
        const promptSpan = cellEl.querySelector(".pn-exec-Prompt");
        if (promptSpan) {
          promptSpan.textContent = `[${typeof cell.executionCount === "number" ? cell.executionCount : " "}]`;
        }
        const pill = cellEl.querySelector(".pn-cell-status-pill");
        if (pill) {
          pill.className = `pn-cell-status-pill ${cell.lastRunStatus}`;
          pill.textContent = cell.lastRunStatus === "ok"
            ? (lang === "python" ? "✓ Executed (Python)" : "✓ Verified (Lean 4)")
            : "✗ Error";
        }
        const outputsWrap = cellEl.querySelector(".pn-cell-outputs");
        if (outputsWrap) {
          renderOutputsInto(outputsWrap, cell.outputs);
        }
      }

      if (advanceFocus) {
        if (idx + 1 < cells.length) {
          const nextId = cells[idx + 1].id;
          state.focusedCellId = nextId;
          const nextTa = elCellsContainer.querySelector(`.pn-cell[data-cell-id="${nextId}"] textarea`);
          if (nextTa) nextTa.focus();
        } else {
          insertCellAt(cells.length, "code", lang);
        }
      }
    } catch (err) {
      cell.lastRunStatus = "error";
      showToast("셀 실행 오류: " + err.message);
      renderCurrentNotebook();
    }
  }

  async function runAllCells() {
    if (!state.currentNotebook) return;
    const cells = state.currentNotebook.cells || [];
    for (let i = 0; i < cells.length; i++) {
      if (cells[i].cellType !== "markdown") {
        await executeCellById(cells[i].id, false);
      }
    }
    showToast("현재 Workspace의 모든 셀 실행이 완료되었습니다.");
  }

  function clearAllOutputs() {
    if (!state.currentNotebook) return;
    (state.currentNotebook.cells || []).forEach((cell) => {
      if (cell.cellType !== "markdown") {
        cell.outputs = [];
        cell.executionCount = null;
        cell.lastRunStatus = "idle";
      }
    });
    renderCurrentNotebook();
    scheduleAutoSave();
    showToast("모든 셀 출력이 초기화되었습니다.");
  }

  function insertCellAt(index, cellType, language = "lean4") {
    if (!state.currentNotebook) {
      showToast("먼저 Workspace를 선택하거나 새로 생성하세요.");
      return;
    }
    const isMd = cellType === "markdown" || language === "markdown";
    const resolvedLang = isMd ? "markdown" : (language === "python" ? "python" : "lean4");
    const newId = `cell-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`;
    const newCell = {
      id: newId,
      cellType: isMd ? "markdown" : "code",
      language: resolvedLang,
      source: "",
      executionCount: null,
      outputs: [],
      lastRunStatus: "idle",
    };
    if (isMd) {
      state.markdownEditing.add(newId);
    }
    state.currentNotebook.cells.splice(index, 0, newCell);
    state.focusedCellId = newId;
    renderCurrentNotebook();
    scheduleAutoSave();

    const ta = elCellsContainer.querySelector(`.pn-cell[data-cell-id="${newId}"] textarea`);
    if (ta) ta.focus();
  }

  function moveCell(index, delta) {
    if (!state.currentNotebook) return;
    const cells = state.currentNotebook.cells;
    const target = index + delta;
    if (target < 0 || target >= cells.length) return;
    const [removed] = cells.splice(index, 1);
    cells.splice(target, 0, removed);
    renderCurrentNotebook();
    scheduleAutoSave();
  }

  function deleteCellAt(index) {
    if (!state.currentNotebook) return;
    const cells = state.currentNotebook.cells;
    if (cells.length <= 1) {
      showToast("Workspace에는 최소 1개의 셀이 필요합니다.");
      return;
    }
    cells.splice(index, 1);
    renderCurrentNotebook();
    scheduleAutoSave();
  }

  function insertTextAtActiveEditor(textToInsert) {
    let ta = state.focusedTextarea;
    if (!ta || !document.body.contains(ta)) {
      ta = elCellsContainer.querySelector("textarea.pn-code-editor, textarea.pn-md-editor");
    }
    if (!ta) {
      showToast("먼저 증명/코드 셀이나 편집 중인 텍스트 셀을 클릭하세요.");
      return;
    }
    const start = ta.selectionStart || 0;
    const end = ta.selectionEnd || 0;
    ta.value = ta.value.slice(0, start) + textToInsert + ta.value.slice(end);
    const nextPos = start + textToInsert.length;
    ta.focus();
    ta.selectionStart = ta.selectionEnd = nextPos;
    ta.dispatchEvent(new Event("input"));
  }

  function buildUnicodeAndMagicPalette() {
    elUnicodePalette.innerHTML = "";
    FEATURED_SYMBOLS.forEach(([sym, abbr]) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "pn-sym-btn";
      btn.title = `\\${abbr} → ${sym}`;
      btn.innerHTML = `<span>${escapeHtml(sym)}</span><span class="pn-sym-abbr">\\${escapeHtml(abbr)}</span>`;
      btn.addEventListener("click", () => insertTextAtActiveEditor(sym));
      elUnicodePalette.appendChild(btn);
    });

    elMagicPalette.innerHTML = "";
    Object.entries(state.magicsMap || {}).forEach(([magic, desc]) => {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "pn-magic-chip";
      chip.title = desc;
      chip.textContent = magic;
      chip.addEventListener("click", () => {
        insertTextAtActiveEditor(magic.startsWith("%%") ? `${magic}\n` : magic);
      });
      elMagicPalette.appendChild(chip);
    });
  }

  async function refreshNotebookListOnly() {
    const data = await apiFetch("/api/workspaces");
    state.notebooks = data.workspaces || data.notebooks || [];
    state.redisInfo = data.redis || state.redisInfo;
    renderNotebookList();
  }

  // Mount a workspace into Redis (HOT) and open/switch to its tab in the VS Code editor
  async function openOrSwitchWorkspace(wsId) {
    if (!wsId) return;
    try {
      const res = await apiFetch(`/api/workspaces/${encodeURIComponent(wsId)}/mount`, {
        method: "POST",
        body: JSON.stringify({ clientId: state.clientId }),
      });
      const ws = res.workspace || res.notebook;
      if (!ws) return;

      if (!state.openWorkspaceIds.includes(ws.id)) {
        state.openWorkspaceIds.push(ws.id);
      }
      state.openWorkspaces[ws.id] = {
        workspace: ws,
        lifecycle: res.lifecycle,
        kernel: res.kernel,
        collaborators: res.collaborators || [],
      };
      state.currentNotebook = ws;
      state.currentLifecycle = res.lifecycle;
      persistOpenTabsState();

      updateKernelBadge(res.kernel);
      updateLifecycleBadge(res.lifecycle);
      ensureWorkspaceEventStream(ws.id);
      await refreshNotebookListOnly();
      renderCurrentNotebook();
    } catch (err) {
      showToast("Workspace 마운트 실패: " + err.message);
    }
  }

  // Close a workspace tab and unmount it from Redis (HOT -> COLD disk flush)
  async function closeWorkspaceTab(wsId, silent = false) {
    if (!wsId) return;
    if (state.currentNotebook && state.currentNotebook.id === wsId) {
      await saveCurrentNotebook(true);
    }
    closeWorkspaceEventStream(wsId);

    try {
      await apiFetch(`/api/workspaces/${encodeURIComponent(wsId)}/unmount`, {
        method: "POST",
        body: JSON.stringify({ clientId: state.clientId, force: true }),
      });
    } catch (_) {}

    const idx = state.openWorkspaceIds.indexOf(wsId);
    if (idx !== -1) {
      state.openWorkspaceIds.splice(idx, 1);
    }
    const closedTitle = (state.openWorkspaces[wsId] && state.openWorkspaces[wsId].workspace && state.openWorkspaces[wsId].workspace.title) || wsId;
    delete state.openWorkspaces[wsId];

    if (state.currentNotebook && state.currentNotebook.id === wsId) {
      const nextId = state.openWorkspaceIds[Math.max(0, idx - 1)] || state.openWorkspaceIds[0] || null;
      if (nextId) {
        await openOrSwitchWorkspace(nextId);
      } else {
        state.currentNotebook = null;
        state.currentLifecycle = null;
        persistOpenTabsState();
        await refreshNotebookListOnly();
        renderCurrentNotebook();
      }
    } else {
      persistOpenTabsState();
      await refreshNotebookListOnly();
    }

    if (!silent) {
      showToast(`'${closedTitle}' 탭이 닫히고 Redis에서 디스크(COLD)로 언마운트되었습니다.`);
    }
  }

  async function refreshNotebooks(preferredId = "") {
    await refreshNotebookListOnly();
    const targetId = preferredId || initialNotebookId;
    if (targetId && state.notebooks.some((n) => n.id === targetId)) {
      await openOrSwitchWorkspace(targetId);
      return;
    }

    // Restore previously open tabs if they still exist on disk
    let savedTabs = [];
    try {
      savedTabs = JSON.parse(localStorage.getItem(STORAGE_OPEN_TABS_KEY) || "[]");
    } catch (_) {}
    const validSavedTabs = Array.isArray(savedTabs)
      ? savedTabs.filter((id) => state.notebooks.some((n) => n.id === id))
      : [];

    if (validSavedTabs.length > 0) {
      for (const tabId of validSavedTabs) {
        await openOrSwitchWorkspace(tabId);
      }
      const activeSaved = localStorage.getItem(STORAGE_ACTIVE_TAB_KEY);
      if (activeSaved && validSavedTabs.includes(activeSaved)) {
        await openOrSwitchWorkspace(activeSaved);
      }
    } else {
      // Show VS Code Workspace Picker screen (no pre-populated example!)
      state.currentNotebook = null;
      renderCurrentNotebook();
    }
  }

  async function refreshCanvases() {
    if (!state.token) {
      elSelectCanvas.innerHTML = `<option value="">— IRADE Workspace 로그인 후 캔버스 목록 조회 가능 —</option>`;
      return;
    }
    try {
      const res = await apiFetch("/api/agora/canvases");
      const list = res.canvases || [];
      elSelectCanvas.innerHTML = `<option value="">— IRADE Workspace 캔버스 선택 (${list.length}개) —</option>`;
      list.forEach((c) => {
        const cid = c.id || c.canvasId || "";
        const opt = document.createElement("option");
        opt.value = cid;
        opt.textContent = `${c.title || "Untitled Canvas"} (${cid.slice(0, 8)})`;
        elSelectCanvas.appendChild(opt);
      });
      if (initialCanvasId) {
        elSelectCanvas.value = initialCanvasId;
        elInputCanvasId.value = initialCanvasId;
      }
    } catch (err) {
      elSelectCanvas.innerHTML = `<option value="">— 캔버스 조회 실패 (${escapeHtml(err.message)}) —</option>`;
    }
  }

  async function refreshSession() {
    try {
      const data = await apiFetch("/api/auth/session");
      state.userProfile = data.user;
      state.agoraStatus = data.agora;
      state.redisInfo = data.redis;
      updateSessionUI();
      await refreshCanvases();
    } catch (err) {
      showToast("세션 확인 오류: " + err.message);
    }
  }

  // VS Code Activity Bar & Sidebar Toggle
  function toggleSidebar(forceOpen = null) {
    if (typeof forceOpen === "boolean") {
      state.sidebarCollapsed = !forceOpen;
    } else {
      state.sidebarCollapsed = !state.sidebarCollapsed;
    }
    elWorkbench.classList.toggle("sidebar-collapsed", state.sidebarCollapsed);
  }

  document.querySelectorAll(".vscode-activity-btn[data-panel]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const panel = btn.dataset.panel;
      if (state.activeActivityPanel === panel && !state.sidebarCollapsed) {
        toggleSidebar(false);
        return;
      }
      state.activeActivityPanel = panel;
      toggleSidebar(true);
      document.querySelectorAll(".vscode-activity-btn[data-panel]").forEach((b) => {
        b.classList.toggle("active", b === btn);
      });
      const sectionEl = document.getElementById("sec-" + panel);
      if (sectionEl) {
        sectionEl.open = true;
        sectionEl.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    });
  });

  document.addEventListener("click", (ev) => {
    if (!elPopover.contains(ev.target)) {
      hidePopover();
    }
  });

  elTitleInput.addEventListener("input", () => {
    if (!state.currentNotebook) return;
    state.currentNotebook.title = elTitleInput.value;
    renderEditorTabs();
    scheduleAutoSave();
  });

  document.getElementById("btn-run-all").addEventListener("click", runAllCells);
  document.getElementById("btn-clear-outputs").addEventListener("click", clearAllOutputs);

  document.getElementById("btn-interrupt-kernel").addEventListener("click", async () => {
    if (!state.currentNotebook) return;
    try {
      const res = await apiFetch(
        `/api/workspaces/${encodeURIComponent(state.currentNotebook.id)}/interrupt`,
        { method: "POST", body: "{}" }
      );
      updateKernelBadge(res.status);
      showToast("커널 인터럽트 요청을 전송했습니다.");
    } catch (err) {
      showToast("인터럽트 실패: " + err.message);
    }
  });

  async function resetCurrentKernel() {
    if (!state.currentNotebook) return;
    try {
      const res = await apiFetch(
        `/api/workspaces/${encodeURIComponent(state.currentNotebook.id)}/reset`,
        { method: "POST", body: "{}" }
      );
      updateKernelBadge(res.status);
      showToast("Lean 4 및 Python 커널 환경이 초기 상태로 재시작되었습니다.");
    } catch (err) {
      showToast("커널 리셋 실패: " + err.message);
    }
  }

  document.getElementById("btn-reset-kernel").addEventListener("click", resetCurrentKernel);

  document.getElementById("btn-add-code-cell").addEventListener("click", () => {
    const len = state.currentNotebook ? state.currentNotebook.cells.length : 0;
    insertCellAt(len, "code", "lean4");
  });

  document.getElementById("btn-add-python-cell").addEventListener("click", () => {
    const len = state.currentNotebook ? state.currentNotebook.cells.length : 0;
    insertCellAt(len, "code", "python");
  });

  document.getElementById("btn-add-md-cell").addEventListener("click", () => {
    const len = state.currentNotebook ? state.currentNotebook.cells.length : 0;
    insertCellAt(len, "markdown", "markdown");
  });

  document.getElementById("btn-save-notebook").addEventListener("click", () => saveCurrentNotebook(false));

  document.getElementById("btn-unmount-workspace").addEventListener("click", () => {
    if (!state.currentNotebook) {
      showToast("현재 열려 있는 Workspace가 없습니다.");
      return;
    }
    closeWorkspaceTab(state.currentNotebook.id, false);
  });

  document.getElementById("menu-close-workspace").addEventListener("click", () => {
    if (state.currentNotebook) {
      closeWorkspaceTab(state.currentNotebook.id, false);
    } else {
      renderWorkspaceWelcomePicker();
    }
  });

  document.getElementById("btn-export-ipynb").addEventListener("click", async () => {
    if (!state.currentNotebook) return;
    await saveCurrentNotebook(true);
    window.open(`/api/workspaces/${encodeURIComponent(state.currentNotebook.id)}/export.ipynb`, "_blank");
  });

  document.getElementById("btn-export-lean").addEventListener("click", async () => {
    if (!state.currentNotebook) return;
    await saveCurrentNotebook(true);
    window.open(`/api/workspaces/${encodeURIComponent(state.currentNotebook.id)}/export.lean`, "_blank");
  });

  async function promptNewNotebook(defaultLang = "lean4") {
    const defaultName = defaultLang === "python" ? "Python & Proof Workspace" : "Lean 4 Proof Workspace";
    const title = window.prompt("새 Workspace 이름을 입력하세요:", defaultName);
    if (!title) return;
    try {
      const res = await apiFetch("/api/workspaces", {
        method: "POST",
        body: JSON.stringify({ title, language: defaultLang }),
      });
      const ws = res.workspace || res.notebook;
      await openOrSwitchWorkspace(ws.id);
      showToast(`새 Workspace '${ws.title}'가 생성되어 Redis(HOT)에 마운트되었습니다.`);
    } catch (err) {
      showToast("생성 실패: " + err.message);
    }
  }

  document.getElementById("btn-new-notebook").addEventListener("click", () => promptNewNotebook("lean4"));
  document.getElementById("menu-new-nb").addEventListener("click", () => promptNewNotebook("lean4"));
  document.getElementById("menu-toggle-sidebar").addEventListener("click", () => toggleSidebar());
  document.getElementById("menu-run-all").addEventListener("click", runAllCells);
  document.getElementById("menu-reset-kernel").addEventListener("click", resetCurrentKernel);

  const fileInputIpynb = document.getElementById("input-import-ipynb");
  document.getElementById("btn-import-ipynb").addEventListener("click", () => fileInputIpynb.click());
  fileInputIpynb.addEventListener("change", async () => {
    const file = fileInputIpynb.files && fileInputIpynb.files[0];
    if (!file) return;
    try {
      const text = await file.text();
      const ipynb = JSON.parse(text);
      const title = file.name.replace(/\.ipynb$/i, "");
      const res = await apiFetch("/api/workspaces/import-ipynb", {
        method: "POST",
        body: JSON.stringify({ title, ipynb }),
      });
      const ws = res.workspace || res.notebook;
      await openOrSwitchWorkspace(ws.id);
      showToast(`'${ws.title}' Workspace를 열어 Redis(HOT)에 마운트했습니다.`);
    } catch (err) {
      showToast(".ipynb 가져오기 실패: " + err.message);
    } finally {
      fileInputIpynb.value = "";
    }
  });

  // IRADE Workspace Canvas Bridge events
  elSelectCanvas.addEventListener("change", () => {
    if (elSelectCanvas.value) {
      elInputCanvasId.value = elSelectCanvas.value;
    }
  });

  document.getElementById("btn-refresh-canvases").addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    refreshCanvases();
  });

  document.getElementById("btn-create-canvas").addEventListener("click", async () => {
    if (!state.token) {
      showToast("먼저 IRADE Workspace 로그인을 수행하거나 JWT 토큰을 설정해주세요.");
      openAuthModal();
      return;
    }
    const titleInput = elInputCanvasId.value.trim() || (state.currentNotebook && state.currentNotebook.title) || "Lean 4 R&D Canvas";
    try {
      const res = await apiFetch("/api/agora/canvases", {
        method: "POST",
        body: JSON.stringify({
          title: titleInput,
          description: "Created from Lean 4 Proof Workspace Studio",
        }),
      });
      const cid = res.canvas && (res.canvas.id || res.canvas.canvasId);
      await refreshCanvases();
      if (cid) {
        elSelectCanvas.value = cid;
        elInputCanvasId.value = cid;
      }
      showToast("IRADE Workspace에 새 협업 캔버스를 생성했습니다.");
    } catch (err) {
      showToast("캔버스 생성 실패: " + err.message);
    }
  });

  document.getElementById("btn-export-canvas").addEventListener("click", async () => {
    if (!state.currentNotebook) {
      showToast("먼저 내보낼 Workspace를 여세요.");
      return;
    }
    if (!state.token) {
      showToast("캔버스로 내보내려면 IRADE Workspace 로그인이 필요합니다.");
      openAuthModal();
      return;
    }
    const canvasId = (elSelectCanvas.value || elInputCanvasId.value || state.currentNotebook.linkedCanvasId || "").trim();
    if (!canvasId) {
      showToast("내보낼 대상 캔버스를 선택하거나 Canvas ID를 입력하세요.");
      return;
    }
    await saveCurrentNotebook(true);
    elBridgeStatus.textContent = "캔버스로 Workspace를 내보내는 중...";
    try {
      const res = await apiFetch(`/api/agora/canvases/${encodeURIComponent(canvasId)}/export`, {
        method: "POST",
        body: JSON.stringify({
          notebookId: state.currentNotebook.id,
          includeSummaryCard: true,
        }),
      });
      state.currentNotebook = res.workspace || res.notebook;
      renderCurrentNotebook();
      await refreshNotebookListOnly();
      const canvasUrl = res.export && res.export.canvasUrl;
      elBridgeStatus.innerHTML = canvasUrl
        ? `✅ 내보내기 완료! <a href="${escapeHtml(canvasUrl)}" target="_blank" rel="noopener">IRADE Workspace 캔버스에서 열기 ↗</a>`
        : "✅ 캔버스 내보내기 완료!";
      showToast("IRADE Workspace 캔버스로 Workspace를 내보냈습니다.");
    } catch (err) {
      elBridgeStatus.textContent = "❌ 내보내기 실패: " + err.message;
      showToast("캔버스 내보내기 오류: " + err.message);
    }
  });

  document.getElementById("btn-import-canvas").addEventListener("click", async () => {
    if (!state.token) {
      showToast("캔버스에서 가져오려면 IRADE Workspace 로그인이 필요합니다.");
      openAuthModal();
      return;
    }
    const canvasId = (elSelectCanvas.value || elInputCanvasId.value || "").trim();
    if (!canvasId) {
      showToast("가져올 IRADE Workspace 캔버스를 선택하거나 Canvas ID를 입력하세요.");
      return;
    }
    elBridgeStatus.textContent = "캔버스 객체를 새 Workspace로 가져오는 중...";
    try {
      const res = await apiFetch(`/api/agora/canvases/${encodeURIComponent(canvasId)}/import`, {
        method: "POST",
        body: "{}",
      });
      const ws = res.workspace || res.notebook;
      await openOrSwitchWorkspace(ws.id);
      elBridgeStatus.textContent = `✅ '${ws.title}' Workspace로 가져왔습니다.`;
      showToast("IRADE Workspace 캔버스에서 Workspace를 가져왔습니다.");
    } catch (err) {
      elBridgeStatus.textContent = "❌ 가져오기 실패: " + err.message;
      showToast("캔버스 가져오기 오류: " + err.message);
    }
  });

  // Auth Modal controls
  const elModalBackdrop = document.getElementById("auth-modal-backdrop");
  const elModalFeedback = document.getElementById("auth-modal-feedback");

  function openAuthModal() {
    document.getElementById("agora-jwt-input").value = state.token;
    document.getElementById("local-researcher-input").value = state.localUser;
    elModalFeedback.textContent = "";
    elModalBackdrop.style.display = "flex";
  }

  document.getElementById("btn-open-auth-modal").addEventListener("click", openAuthModal);
  document.getElementById("btn-sidebar-auth").addEventListener("click", openAuthModal);
  document.getElementById("menu-open-agora-modal").addEventListener("click", openAuthModal);

  document.getElementById("btn-close-auth-modal").addEventListener("click", () => {
    elModalBackdrop.style.display = "none";
  });

  document.querySelectorAll(".vscode-modal-tab").forEach((tabBtn) => {
    tabBtn.addEventListener("click", () => {
      document.querySelectorAll(".vscode-modal-tab").forEach((b) => b.classList.remove("active"));
      document.querySelectorAll(".vscode-tab-panel").forEach((p) => (p.style.display = "none"));
      tabBtn.classList.add("active");
      const target = document.getElementById("tab-panel-" + tabBtn.dataset.tab);
      if (target) target.style.display = "block";
    });
  });

  document.getElementById("btn-submit-agora-login").addEventListener("click", async () => {
    const email = document.getElementById("agora-login-email").value.trim();
    const password = document.getElementById("agora-login-password").value;
    const mfaCode = document.getElementById("agora-login-mfa").value.trim();

    try {
      elModalFeedback.style.color = "#3794ff";
      elModalFeedback.textContent = "IRADE Workspace 인증 중...";
      let data;
      if (state.pendingMfaToken && mfaCode) {
        data = await apiFetch("/api/auth/mfa", {
          method: "POST",
          body: JSON.stringify({ mfa_token: state.pendingMfaToken, code: mfaCode }),
        });
      } else {
        data = await apiFetch("/api/auth/login", {
          method: "POST",
          body: JSON.stringify({ email, password }),
        });
      }

      if (data.mfa_required || data.mfaRequired) {
        state.pendingMfaToken = data.mfa_token || data.mfaToken;
        document.getElementById("agora-mfa-box").style.display = "block";
        elModalFeedback.textContent = "MFA 6자리 인증 코드를 입력한 뒤 다시 로그인 버튼을 눌러주세요.";
        return;
      }

      const token = data.access_token || data.accessToken || data.token;
      if (!token) {
        throw new Error("응답에 access_token이 포함되어 있지 않습니다.");
      }
      state.pendingMfaToken = null;
      state.token = token;
      localStorage.setItem(STORAGE_TOKEN_KEY, token);
      await refreshSession();
      await refreshNotebooks();
      elModalBackdrop.style.display = "none";
      showToast("IRADE Workspace 계정으로 로그인되었습니다.");
    } catch (err) {
      elModalFeedback.style.color = "#f14c4c";
      elModalFeedback.textContent = "로그인 실패: " + err.message;
    }
  });

  document.getElementById("btn-save-jwt-token").addEventListener("click", async () => {
    state.token = document.getElementById("agora-jwt-input").value.trim();
    if (state.token) {
      localStorage.setItem(STORAGE_TOKEN_KEY, state.token);
    } else {
      localStorage.removeItem(STORAGE_TOKEN_KEY);
    }
    await refreshSession();
    await refreshNotebooks();
    elModalBackdrop.style.display = "none";
    showToast("IRADE Workspace 토큰 설정이 업데이트되었습니다.");
  });

  document.getElementById("btn-clear-jwt-token").addEventListener("click", async () => {
    state.token = "";
    localStorage.removeItem(STORAGE_TOKEN_KEY);
    document.getElementById("agora-jwt-input").value = "";
    await refreshSession();
    await refreshNotebooks();
    elModalBackdrop.style.display = "none";
    showToast("IRADE Workspace 연동이 해제되고 로컬 연구자 모드로 전환되었습니다.");
  });

  document.getElementById("btn-save-local-user").addEventListener("click", async () => {
    const val = document.getElementById("local-researcher-input").value.trim() || "local-researcher";
    state.localUser = val;
    state.token = "";
    localStorage.removeItem(STORAGE_TOKEN_KEY);
    localStorage.setItem(STORAGE_LOCAL_USER_KEY, val);
    await refreshSession();
    await refreshNotebooks();
    elModalBackdrop.style.display = "none";
    showToast(`로컬 연구자 세션 '${val}'(으)로 전환되었습니다.`);
  });

  // Global Ctrl+S save & Ctrl+B sidebar toggle shortcuts
  window.addEventListener("keydown", (ev) => {
    if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "s") {
      ev.preventDefault();
      saveCurrentNotebook(false);
    } else if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "b") {
      ev.preventDefault();
      toggleSidebar();
    }
  });

  // Initialize application
  async function init() {
    try {
      const uni = await apiFetch("/api/unicode");
      state.unicodeMap = uni.abbreviations || {};
      state.magicsMap = uni.magics || {};
      buildUnicodeAndMagicPalette();
    } catch (_) {}

    await refreshSession();
    await refreshNotebooks();

    if (initialCanvasId) {
      elInputCanvasId.value = initialCanvasId;
    }
  }

  init();
})();
