(() => {
  const G = window.AGDD;
  const { $, esc, api, peek, CACHE, store, toast, run, stale, openTerminal, loadMonaco, langOf, darkMode } = G;

  const CELL_ICON = { python: ["λ", "#38bdf8"], lean: ["∀", "#a78bfa"], rocq: ["⊢", "#c084fc"], html: ["⟨⟩", "#f59e0b"], markdown: ["≡", "#94a3b8"] };
  const EXT_MAP = { py: "python", lean: "lean", v: "rocq", html: "html", md: "markdown" };
  const RUNNABLE = new Set(["py", "sh", "js", "mjs", "lean", "v"]);

  const IDE = G.IDE;
  IDE.dirty = IDE.dirty || new Map();
  IDE.dirs = IDE.dirs || [];
  IDE.runOut = IDE.runOut || null;
  IDE.termOpen = !!IDE.termOpen;
  IDE.term = IDE.term || null;

  const ideApi = (p, fresh) => api("GET", `/workspaces/${G.getWS()}/ide${p}`, undefined, { fresh });
  const ideMut = (method, p, body) => api(method, `/workspaces/${G.getWS()}/ide${p}`, body, { noInvalidate: true });
  const encPath = p => p.split("/").map(encodeURIComponent).join("/");
  const extOf = p => (p.split(".").pop() || "").toLowerCase();

  function renderSimpleMarkdown(md) {
    const blocks = [];
    let text = String(md || "").replace(/\r\n?/g, "\n");
    text = text.replace(/```([A-Za-z0-9_+-]*)\n([\s\S]*?)```/g, (_, lang, code) => {
      const idx = blocks.length;
      blocks.push(`<pre><code class="lang-${esc(lang)}">${esc(code.replace(/\n$/, ""))}</code></pre>`);
      return `\n@@BLOCK_${idx}@@\n`;
    });
    const inline = s => {
      let out = esc(s);
      out = out.replace(/`([^`]+)`/g, (_, c) => `<code>${c}</code>`);
      out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
      out = out.replace(/\*([^*]+)\*/g, "<em>$1</em>");
      out = out.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_, lbl, rawHref) => {
        const href = rawHref.replace(/&amp;/g, "&");
        if (/^(https?:\/\/|\/|#)/i.test(href)) return `<a href="${esc(href)}" target="_blank" rel="noopener noreferrer">${lbl}</a>`;
        return `${lbl} (${esc(rawHref)})`;
      });
      return out;
    };
    const lines = text.split("\n"), out = [];
    let i = 0;
    while (i < lines.length) {
      const ln = lines[i];
      const bm = /^@@BLOCK_(\d+)@@$/.exec(ln.trim());
      if (bm) { out.push(blocks[+bm[1]]); i++; continue; }
      if (!ln.trim()) { i++; continue; }
      const hm = /^(#{1,4})\s+(.+)$/.exec(ln);
      if (hm) { out.push(`<h${hm[1].length}>${inline(hm[2])}</h${hm[1].length}>`); i++; continue; }
      if (/^([-*_]){3,}$/.test(ln.trim())) { out.push("<hr>"); i++; continue; }
      if (/^>\s?/.test(ln)) {
        const bq = [];
        while (i < lines.length && /^>\s?/.test(lines[i])) { bq.push(inline(lines[i].replace(/^>\s?/, ""))); i++; }
        out.push(`<blockquote>${bq.join("<br>")}</blockquote>`);
        continue;
      }
      if (/^\|.+\|$/.test(ln.trim()) && i + 1 < lines.length && /^\|[\s:|-]+\|$/.test(lines[i + 1].trim())) {
        const parseRow = r => r.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map(c => c.trim());
        const ths = parseRow(ln).map(c => `<th>${inline(c)}</th>`).join("");
        i += 2;
        const trs = [];
        while (i < lines.length && /^\|.+\|$/.test(lines[i].trim())) {
          trs.push(`<tr>${parseRow(lines[i]).map(c => `<td>${inline(c)}</td>`).join("")}</tr>`);
          i++;
        }
        out.push(`<table><thead><tr>${ths}</tr></thead><tbody>${trs.join("")}</tbody></table>`);
        continue;
      }
      if (/^[-*+]\s+/.test(ln)) {
        const items = [];
        while (i < lines.length && /^[-*+]\s+/.test(lines[i])) { items.push(`<li>${inline(lines[i].replace(/^[-*+]\s+/, ""))}</li>`); i++; }
        out.push(`<ul>${items.join("")}</ul>`);
        continue;
      }
      if (/^\d+\.\s+/.test(ln)) {
        const items = [];
        while (i < lines.length && /^\d+\.\s+/.test(lines[i])) { items.push(`<li>${inline(lines[i].replace(/^\d+\.\s+/, ""))}</li>`); i++; }
        out.push(`<ol>${items.join("")}</ol>`);
        continue;
      }
      const para = [];
      while (i < lines.length && lines[i].trim() && !/^(#{1,4}\s+|>\s?|[-*+]\s+|\d+\.\s+|([-*_]){3,}$|@@BLOCK_\d+@@$)/.test(lines[i])) {
        para.push(inline(lines[i]));
        i++;
      }
      out.push(`<p>${para.join("<br>")}</p>`);
    }
    return out.join("\n");
  }

  function idePrompt(title, defaultVal = "", placeholder = "") {
    return new Promise(resolve => {
      document.getElementById("ide-modal-backdrop")?.remove();
      const wrap = document.createElement("div");
      wrap.id = "ide-modal-backdrop";
      wrap.style.cssText = "position:fixed;inset:0;z-index:120;background:rgba(0,0,0,.45);display:grid;place-items:center;padding:16px";
      wrap.innerHTML = `<div class="card" style="width:min(440px,100%);margin:0;box-shadow:0 12px 36px rgba(0,0,0,.3)">
        <h3 style="margin:0 0 10px">${esc(title)}</h3>
        <input id="ide-prompt-in" value="${esc(defaultVal)}" placeholder="${esc(placeholder)}" style="width:100%;margin-bottom:12px">
        <div class="row" style="justify-content:flex-end"><button type="button" class="g" id="ide-prompt-cancel">취소</button><button type="button" class="b" id="ide-prompt-ok">확인</button></div>
      </div>`;
      document.body.appendChild(wrap);
      const inp = wrap.querySelector("#ide-prompt-in");
      const finish = v => { wrap.remove(); resolve(v); };
      wrap.querySelector("#ide-prompt-cancel").onclick = () => finish(null);
      wrap.querySelector("#ide-prompt-ok").onclick = () => finish(inp.value);
      wrap.onclick = e => { if (e.target === wrap) finish(null); };
      inp.onkeydown = e => {
        if (e.key === "Escape") { e.preventDefault(); finish(null); }
        else if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); finish(inp.value); }
      };
      requestAnimationFrame(() => { inp.focus(); inp.select(); });
    });
  }

  function stopIdeTerm() {
    if (IDE.term) {
      try { IDE.term.stop(); } catch {}
      IDE.term = null;
    }
  }
  IDE.stopTerm = stopIdeTerm;

  function notifyStudioCellChange() {
    const frame = document.querySelector("#studio-host iframe, .ide-split iframe");
    if (frame && frame.contentWindow) {
      try { frame.contentWindow.postMessage({ type: "irade:vm-cells-updated" }, location.origin); } catch {}
    }
  }

  function ideTreeHtml() {
    if (!IDE.tree) return `<div class="muted" style="padding:12px">저장소를 불러오는 중…</div>`;
    const st = IDE.tree.status || {};
    const q = (IDE.filter || "").toLowerCase();
    const rawFiles = IDE.tree.files || [];
    const allFiles = rawFiles.filter(f => !q || f.toLowerCase().includes(q));
    const cellFiles = allFiles.filter(f => f.startsWith("cells/") && !f.endsWith("/.irade-manifest.json"));
    const otherFiles = allFiles.filter(f => !f.startsWith("cells/"));
    const changed = allFiles.filter(f => st[f]);
    const del = Object.keys(st).filter(f => st[f] === "D");
    const emptyDirs = (IDE.tree.dirs || []).filter(d => (!q || d.toLowerCase().includes(q)) && !rawFiles.some(f => f === d || f.startsWith(d + "/")));

    const item = f => {
      const ext = extOf(f);
      const isCell = f.startsWith("cells/");
      const badge = isCell && EXT_MAP[ext] ? CELL_ICON[EXT_MAP[ext]] : null;
      const dirtyMark = IDE.dirty.has(f) ? `<span class="st st-M">●</span>` : "";
      const gitMark = st[f] ? `<span class="st st-${esc(st[f][0] === "?" ? "U" : st[f][0])}">${esc(st[f] === "??" ? "U" : st[f])}</span>` : "";
      return `<div class="tree-row ${f === IDE.active && !IDE.commit ? "on" : ""}">
        <button class="tree-item" data-f="${esc(f)}" title="${esc(f)}">
          ${badge ? `<span style="color:${badge[1]};font-weight:700">${badge[0]}</span>` : ""}
          <span style="overflow:hidden;text-overflow:ellipsis">${esc(f)}</span>
          ${dirtyMark}${gitMark}
        </button>
        <span class="tree-acts">
          <button class="icon" data-ren="${esc(f)}" title="이름 바꾸기">✎</button>
          <button class="icon bad" data-del-f="${esc(f)}" title="삭제">✕</button>
        </span>
      </div>`;
    };
    const dirRow = d => `<div class="tree-row">
      <button class="tree-item" data-new-in-dir="${esc(d)}" title="폴더 '${esc(d)}'에 새 파일 만들기">
        <span>📁</span><span style="overflow:hidden;text-overflow:ellipsis">${esc(d)}</span><span class="st muted" style="font-weight:400">(빈 폴더)</span>
      </button>
      <span class="tree-acts">
        <button class="icon" data-new-in-dir="${esc(d)}" title="폴더에 파일 추가">+📄</button>
        <button class="icon bad" data-del-f="${esc(d)}" title="폴더 삭제">✕</button>
      </span>
    </div>`;

    const parts = [];
    if (cellFiles.length) {
      parts.push(`<div class="ide-head" style="border-top:0">노트북 셀 파일 (${cellFiles.length})</div>` + cellFiles.map(item).join(""));
    }
    if (changed.length || del.length) {
      parts.push(`<div class="ide-head">Git 변경 (${changed.length + del.length})</div>` + changed.map(item).join("") +
        del.map(f => `<div class="tree-row"><div class="tree-item"><span class="st st-D" style="margin:0">D</span><s>${esc(f)}</s></div></div>`).join(""));
    }
    const repoListing = emptyDirs.map(dirRow).join("") + otherFiles.slice(0, 2000).map(item).join("");
    parts.push(`<div class="ide-head">저장소 파일 (${otherFiles.length}${emptyDirs.length ? ` · 폴더 ${emptyDirs.length}` : ""})</div>` +
      (repoListing || `<div class="muted" style="padding:8px 12px;font-size:12px">파일이 없습니다. 상단 <b>+📄</b>로 만드세요.</div>`));
    return parts.join("");
  }

  function ideLogHtml() {
    if (!IDE.tree) return "";
    return IDE.tree.log?.length ? IDE.tree.log.map(c => `<button class="commit ${c.sha === IDE.commit ? "on" : ""}" data-c="${esc(c.sha)}"><b>${esc(c.subject || "(no message)")}</b>
      <span class="mono">${esc(c.sha.slice(0, 7))}</span> <span>· ${esc(c.author)} · ${esc((c.date || "").replace("T", " ").slice(0, 16))}</span></button>`).join("")
      : `<div class="muted" style="padding:12px">커밋이 없습니다.</div>`;
  }

  function ideTabsHtml() {
    const tabs = IDE.open.map(f => {
      const dirtyDot = IDE.dirty.has(f) ? `<span class="st st-M">●</span>` : "";
      return `<button class="ide-tab ${f === IDE.active && !IDE.commit ? "on" : ""}" data-tab="${esc(f)}">${esc(f.split("/").pop())}${dirtyDot}<span class="x" data-close="${esc(f)}">✕</span></button>`;
    });
    if (IDE.commit) tabs.push(`<button class="ide-tab on">커밋 ${esc(IDE.commit.slice(0, 7))}</button>`);
    if (IDE.active === "__diff__" && !IDE.commit) tabs.push(`<button class="ide-tab on">작업 트리 diff</button>`);
    return tabs.join("");
  }

  async function saveActiveFile() {
    const path = IDE.active;
    if (!path || path === "__diff__" || IDE.commit || !IDE.repo) return;
    if (IDE.files[path]?.truncated) return toast("256 KiB 초과 파일은 잘림 방지를 위해 읽기 전용으로 열렸습니다", true);
    const text = IDE.dirty.has(path)
      ? IDE.dirty.get(path)
      : (IDE.editor && typeof IDE.editor.getValue === "function" ? IDE.editor.getValue() : IDE.files[path]?.content);
    if (typeof text !== "string") return;
    const res = await ideMut("PUT", `/repos/${encodeURIComponent(IDE.repo)}/files/${encPath(path)}`, { content: text });
    IDE.files[path] = { ...(IDE.files[path] || {}), path, content: text, head: res.head ?? IDE.files[path]?.head, exists: true };
    IDE.dirty.delete(path);
    for (const k of [...CACHE.keys()]) if (k.includes("/ide/")) CACHE.delete(k);
    if (path.startsWith("cells/")) notifyStudioCellChange();
    toast(`저장됨: ${path}`);
    if ($("#ide-tabs")) $("#ide-tabs").innerHTML = ideTabsHtml();
    if ($("#ide-save")) $("#ide-save").disabled = true;
    await refreshTreeOnly();
  }

  async function refreshTreeOnly() {
    if (!IDE.repo) return;
    IDE.tree = await ideApi(`/repos/${encodeURIComponent(IDE.repo)}/tree`, true);
    if ($("#ide-tree")) $("#ide-tree").innerHTML = ideTreeHtml();
    if ($("#ide-log")) $("#ide-log").innerHTML = ideLogHtml();
  }

  async function ideShow() {
    if (!$("#ide-tabs")) return;
    $("#ide-tabs").innerHTML = ideTabsHtml();
    $("#ide-tree").innerHTML = ideTreeHtml();
    if ($("#ide-log")) $("#ide-log").innerHTML = ideLogHtml();
    const host = $("#ide-editor"), preview = $("#ide-preview"), empty = $("#ide-empty");
    const hasDoc = Boolean(IDE.commit || IDE.active);
    empty.style.display = hasDoc ? "none" : "grid";
    preview.hidden = true;
    host.style.display = "block";
    if (IDE.editor) { IDE.editor.dispose(); IDE.editor = null; }

    const isRealFile = Boolean(IDE.active && IDE.active !== "__diff__" && !IDE.commit);
    const ext = isRealFile ? extOf(IDE.active) : "";
    const canPrev = isRealFile && (ext === "html" || ext === "md");
    const canRun = isRealFile && RUNNABLE.has(ext);
    const activeFile = isRealFile ? IDE.files[IDE.active] : null;
    const isTrunc = Boolean(activeFile?.truncated);
    const canRev = Boolean(isRealFile && activeFile?.head != null && (IDE.tree?.status?.[IDE.active] || IDE.dirty.has(IDE.active)));

    $("#ide-path").textContent = IDE.commit
      ? `commit ${IDE.commit.slice(0, 7)}`
      : IDE.active === "__diff__"
        ? `${IDE.repo} · working diff`
        : IDE.active
          ? `/workspace/repos/${IDE.repo}/${IDE.active}`
          : (IDE.repo ? `/workspace/repos/${IDE.repo}` : "");
    if ($("#ide-trunc-warn")) $("#ide-trunc-warn").hidden = !isTrunc;

    $("#ide-mode").style.visibility = isRealFile ? "visible" : "hidden";
    const prevBtn = $("#ide-mode-preview");
    if (prevBtn) prevBtn.hidden = !canPrev;
    $("#ide-mode").querySelectorAll("button").forEach(b => b.classList.toggle("on", b.dataset.m === IDE.mode));
    if ($("#ide-save")) $("#ide-save").disabled = !(isRealFile && !isTrunc && IDE.dirty.has(IDE.active));
    if ($("#ide-revert")) $("#ide-revert").hidden = !canRev;
    if ($("#ide-run")) $("#ide-run").hidden = !canRun;
    if ($("#ide-ask-agent")) $("#ide-ask-agent").hidden = !isRealFile;

    if (!hasDoc) return;
    if (IDE.mode === "preview" && canPrev) {
      host.style.display = "none";
      preview.hidden = false;
      const content = IDE.dirty.has(IDE.active) ? IDE.dirty.get(IDE.active) : (IDE.files[IDE.active]?.content || "");
      if (ext === "html") {
        preview.setAttribute("sandbox", "allow-scripts");
        preview.srcdoc = content;
      } else {
        preview.setAttribute("sandbox", "");
        const dark = darkMode();
        const mdCss = dark
          ? "body{font:14px/1.75 system-ui,sans-serif;padding:20px 24px;margin:0;background:#18181b;color:#e4e4e7;overflow-wrap:anywhere}h1,h2,h3,h4{color:#fafafa;margin:18px 0 8px}h2{border-bottom:1px solid #27272a;padding-bottom:6px}code{background:#27272a;padding:2px 6px;border-radius:4px;font-family:ui-monospace,Menlo,monospace}pre{padding:12px;background:#111114;border:1px solid #27272a;border-radius:8px;overflow:auto}pre code{background:none;padding:0}blockquote{border-left:3px solid #7fc0a9;padding-left:12px;margin:10px 0;color:#a1a1aa}table{border-collapse:collapse;width:100%;margin:10px 0}th,td{border:1px solid #3f3f46;padding:6px 10px}a{color:#7fc0a9}hr{border:0;border-top:1px solid #27272a;margin:16px 0}"
          : "body{font:14px/1.75 system-ui,sans-serif;padding:20px 24px;margin:0;background:#ffffff;color:#1d1d1b;overflow-wrap:anywhere}h1,h2,h3,h4{color:#111;margin:18px 0 8px}h2{border-bottom:1px solid #e3e2dc;padding-bottom:6px}code{background:#efeee8;padding:2px 6px;border-radius:4px;font-family:ui-monospace,Menlo,monospace}pre{padding:12px;background:#f7f7f5;border:1px solid #e3e2dc;border-radius:8px;overflow:auto}pre code{background:none;padding:0}blockquote{border-left:3px solid #2f5d50;padding-left:12px;margin:10px 0;color:#6b6b66}table{border-collapse:collapse;width:100%;margin:10px 0}th,td{border:1px solid #e3e2dc;padding:6px 10px}a{color:#2f5d50}hr{border:0;border-top:1px solid #e3e2dc;margin:16px 0}";
        preview.srcdoc = `<!doctype html><html><head><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"><style>${mdCss}</style></head><body>${renderSimpleMarkdown(content)}</body></html>`;
      }
      return;
    }

    const monaco = await loadMonaco();
    const opts = {
      automaticLayout: true, minimap: { enabled: false }, fontSize: 13, scrollBeyondLastLine: false,
      theme: darkMode() ? "vs-dark" : "vs", renderWhitespace: "selection", tabSize: 2,
    };
    if (IDE.commit || IDE.active === "__diff__") {
      const text = IDE.commit ? IDE.files["commit:" + IDE.commit] : IDE.files.__diff__;
      IDE.editor = monaco.editor.create(host, { ...opts, readOnly: true, value: text || "(변경 사항 없음)", language: "diff" });
      return;
    }
    const f = IDE.files[IDE.active];
    if (!f) return;
    if (f.binary) {
      IDE.editor = monaco.editor.create(host, { ...opts, readOnly: true, value: `바이너리 파일 (${f.size} bytes)` });
      return;
    }
    if (IDE.mode === "diff") {
      const ed = monaco.editor.createDiffEditor(host, { ...opts, readOnly: true, renderSideBySide: host.clientWidth > 700 });
      ed.setModel({
        original: monaco.editor.createModel(f.head ?? "", langOf(f.path)),
        modified: monaco.editor.createModel(IDE.dirty.has(f.path) ? IDE.dirty.get(f.path) : (f.content ?? ""), langOf(f.path)),
      });
      IDE.editor = ed;
      return;
    }
    const val = IDE.dirty.has(f.path) ? IDE.dirty.get(f.path) : (f.content ?? "");
    const ed = monaco.editor.create(host, { ...opts, readOnly: isTrunc, value: val, language: langOf(f.path) });
    if (!isTrunc) {
      ed.onDidChangeModelContent(() => {
        const next = ed.getValue();
        if (next !== (IDE.files[f.path]?.content ?? "")) IDE.dirty.set(f.path, next);
        else IDE.dirty.delete(f.path);
        if ($("#ide-save")) $("#ide-save").disabled = !IDE.dirty.has(f.path);
        $("#ide-tabs").innerHTML = ideTabsHtml();
      });
      ed.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => run(saveActiveFile)());
    }
    IDE.editor = ed;
  }

  async function ideOpen(path) {
    IDE.commit = "";
    if (!IDE.files[path]) {
      $("#ide-empty").style.display = "grid";
      $("#ide-empty").innerHTML = `<div class="muted">${esc(path)} 여는 중…</div>`;
      IDE.files[path] = await ideApi(`/repos/${encodeURIComponent(IDE.repo)}/files/${encPath(path)}`, true);
    }
    if (!IDE.open.includes(path)) IDE.open.push(path);
    IDE.active = path;
    IDE.mode = "file";
    await ideShow();
  }

  async function ideLoadRepo(fresh) {
    IDE.tree = null; IDE.open = []; IDE.active = ""; IDE.files = {}; IDE.dirty.clear(); IDE.commit = "";
    IDE.ws = G.getWS(); IDE.treeRepo = IDE.repo;
    if (IDE.repo) IDE.tree = await ideApi(`/repos/${encodeURIComponent(IDE.repo)}/tree`, fresh);
    await ideShow();
  }

  function renderRunOut() {
    const outEl = $("#ide-run-out");
    if (!outEl) return;
    if (!IDE.runOut) {
      outEl.innerHTML = `<div class="muted" style="padding:8px">실행 가능한 파일(.py, .sh, .js, .lean, .v)을 열고 <b>▶ 실행</b>을 누르면 결과가 여기에 표시됩니다.</div>`;
      return;
    }
    const r = IDE.runOut;
    outEl.innerHTML = `<div class="row" style="margin-bottom:6px"><b class="${r.exit_code === 0 ? "ok" : "bad"}">${r.exit_code === 0 ? "✓ 성공" : "✕ 오류 (exit " + r.exit_code + ")"}</b> <span class="mono muted">${esc(r.path)}</span></div>
      <pre style="max-height:140px">${esc((r.stdout || "") + (r.stderr ? "\n" + r.stderr : "") || "(출력 없음)")}</pre>`;
  }

  function toggleBottom(open, tab) {
    IDE.termOpen = open;
    IDE.bottomTab = tab || IDE.bottomTab || "out";
    const box = $("#ide-bottom");
    if (!box) return;
    box.hidden = !open;
    $("#ide-bot-out").hidden = IDE.bottomTab !== "out";
    $("#ide-bot-term").hidden = IDE.bottomTab !== "term";
    document.querySelectorAll("[data-ibot]").forEach(b => b.classList.toggle("on", b.dataset.ibot === IDE.bottomTab));
    if (open && IDE.bottomTab === "term" && !IDE.term) {
      $("#ide-term-host").innerHTML = "";
      IDE.term = openTerminal($("#ide-term-host"), "ide", false);
    }
    if (open && IDE.bottomTab === "out") renderRunOut();
  }

  function ideBind() {
    $("#ide-tree").onclick = run(async e => {
      const del = e.target.closest("[data-del-f]");
      if (del) {
        e.stopPropagation();
        const p = del.dataset.delF;
        if (!confirm(`${p} 파일을 삭제할까요?`)) return;
        await ideMut("DELETE", `/repos/${encodeURIComponent(IDE.repo)}/files/${encPath(p)}`);
        IDE.open = IDE.open.filter(x => x !== p);
        delete IDE.files[p]; IDE.dirty.delete(p);
        if (IDE.active === p) IDE.active = IDE.open[IDE.open.length - 1] || "";
        if (p.startsWith("cells/")) notifyStudioCellChange();
        await refreshTreeOnly(); await ideShow(); toast("삭제됨");
        return;
      }
      const newDirBtn = e.target.closest("[data-new-in-dir]");
      if (newDirBtn) {
        e.stopPropagation();
        const dir = newDirBtn.dataset.newInDir;
        const p = await idePrompt(`폴더 '${dir}'에 새 파일 만들기`, `${dir}/main.py`, `${dir}/example.py`);
        if (!p || !p.trim()) return;
        const clean = p.trim();
        await ideMut("PUT", `/repos/${encodeURIComponent(IDE.repo)}/files/${encPath(clean)}`, { content: "" });
        if (clean.startsWith("cells/")) notifyStudioCellChange();
        await refreshTreeOnly();
        await ideOpen(clean);
        return;
      }
      const ren = e.target.closest("[data-ren]");
      if (ren) {
        e.stopPropagation();
        const src = ren.dataset.ren;
        const dst = await idePrompt("새 경로/파일명", src);
        if (!dst || dst.trim() === src) return;
        const cleanDst = dst.trim();
        await ideMut("POST", `/repos/${encodeURIComponent(IDE.repo)}/rename`, { from: src, to: cleanDst });
        const idx = IDE.open.indexOf(src);
        if (idx >= 0) IDE.open[idx] = cleanDst;
        if (IDE.dirty.has(src)) {
          const draft = IDE.dirty.get(src);
          IDE.dirty.delete(src);
          IDE.dirty.set(cleanDst, draft);
        }
        delete IDE.files[src];
        if (src.startsWith("cells/") || cleanDst.startsWith("cells/")) notifyStudioCellChange();
        await refreshTreeOnly();
        if (IDE.active === src) {
          delete IDE.files[cleanDst];
          await ideOpen(cleanDst);
        } else {
          await ideShow();
        }
        toast("이름 변경됨");
        return;
      }
      const b = e.target.closest("[data-f]");
      if (b) await ideOpen(b.dataset.f);
    });
    if ($("#ide-log")) {
      $("#ide-log").onclick = run(async e => {
        const b = e.target.closest("[data-c]"); if (!b) return;
        const sha = b.dataset.c;
        if (!IDE.files["commit:" + sha]) IDE.files["commit:" + sha] = (await ideApi(`/repos/${encodeURIComponent(IDE.repo)}/commits/${sha}`)).patch;
        IDE.commit = sha; await ideShow();
      });
    }
    $("#ide-tabs").onclick = run(async e => {
      const x = e.target.closest("[data-close]");
      if (x) {
        e.stopPropagation();
        const f = x.dataset.close;
        if (IDE.dirty.has(f) && !confirm(`${f}의 저장되지 않은 변경을 버릴까요?`)) return;
        IDE.dirty.delete(f);
        IDE.open = IDE.open.filter(p => p !== f);
        if (IDE.active === f) IDE.active = IDE.open[IDE.open.length - 1] || "";
        return ideShow();
      }
      const t = e.target.closest("[data-tab]");
      if (t) { IDE.commit = ""; IDE.active = t.dataset.tab; await ideShow(); }
    });
    $("#ide-mode").onclick = run(async e => {
      const b = e.target.closest("[data-m]");
      if (b) { IDE.mode = b.dataset.m; await ideShow(); }
    });
  }

  document.addEventListener("keydown", e => {
    if (G.getTAB() === "ide" && !e.isComposing && (e.ctrlKey || e.metaKey) && e.code === "KeyS" && IDE.active && !IDE.commit && IDE.active !== "__diff__") {
      e.preventDefault();
      run(saveActiveFile)();
    }
  });

  G.VIEWS.ide = async function ideView() {
    const WS = G.getWS();
    const sid = G.iradeId();
    const paneBtns = `<span class="seg" id="ide-pane">
      <button data-p="files" class="${IDE.pane === "files" ? "on" : ""}">파일 IDE</button>
      <button data-p="studio" class="${IDE.pane === "studio" ? "on" : ""}">셀 노트북 (Studio)</button>
      <button data-p="split" class="${IDE.pane === "split" ? "on" : ""}">분할 (파일 + 셀)</button>
    </span>`;
    const bindPane = () => {
      $("#ide-pane").onclick = e => {
        const b = e.target.closest("[data-p]"); if (!b) return;
        IDE.pane = b.dataset.p; store.set("agdd.idepane", IDE.pane); G.render();
      };
    };

    if (IDE.pane === "studio") {
      stopIdeTerm();
      G.V(`<div class="row" style="justify-content:space-between; margin-bottom:12px">
          <div><h2>IDE · 노트북 Studio</h2><p class="sub" style="margin:0">대화 · Lean 4/Rocq 증명 · Python · 문서 셀을 편집하며, 저장 시 VM의 <span class="mono">/workspace/cells</span>와 자동 동기화됩니다.</p></div>
          <div class="row">${paneBtns}${sid ? `<a class="g linkbtn" href="/agdd/notebook?workspace=${sid}" target="_blank" rel="noopener" style="padding:6px 12px;border:1px solid var(--line);border-radius:6px;color:var(--ink)">전체 화면 ↗</a>` : ""}</div></div>
        ${sid ? "" : `<div class="card">노트북 Studio는 IRADE 로그인 상태에서 workspace를 선택하면 사용할 수 있습니다.</div>`}`);
      if (sid) {
        const host = $("#studio-host"), cur = host.querySelector("iframe");
        if (!cur || cur.dataset.ws !== sid) host.innerHTML = `<iframe class="studio-frame" data-ws="${esc(sid)}" src="/agdd/notebook?workspace=${esc(sid)}&embed=1" title="IRADE 노트북 Studio"></iframe>`;
        host.hidden = false; G.fitStudio();
      }
      return bindPane();
    }

    let repos = [], err = "";
    const cachedRepos = peek(`/workspaces/${WS}/ide/repos`);
    if (cachedRepos === undefined) G.V(`<h2>IDE · 코드 & 셀</h2><p class="sub">VM 저장소 목록을 불러오는 중…</p><div class="skeleton"></div>`);
    try {
      repos = cachedRepos ?? await ideApi("/repos");
      if (!repos.length) {
        await ideMut("POST", "/repos", { repo: "workspace" });
        repos = await ideApi("/repos", true);
      }
    } catch (e) { err = e.message; }
    if (stale("ide")) return;
    if (!repos.find(r => r.repo === IDE.repo)) IDE.repo = repos.find(r => r.repo === "workspace")?.repo || repos[0]?.repo || "";

    const splitOn = IDE.pane === "split";
    const ideShell = `<div class="ide-shell">
      <div class="ide">
        <div class="ide-col">
          <div class="ide-head">탐색기
            <span class="row" style="gap:2px">
              <button class="icon" id="ide-new-file" title="새 파일">+📄</button>
              <button class="icon" id="ide-new-dir" title="새 폴더">+📁</button>
            </span>
          </div>
          <div style="padding:6px 8px;border-bottom:1px solid var(--line)"><input id="ide-filter" placeholder="파일 찾기 (Ctrl+P)" style="width:100%;padding:4px 8px;font-size:12px"></div>
          <div class="ide-scroll" id="ide-tree"></div>
        </div>
        <div class="ide-col">
          <div class="ide-tabs" id="ide-tabs"></div>
          <div class="ide-head" style="text-transform:none; letter-spacing:0; font-size:12px">
            <span class="row" style="gap:6px;min-width:0"><span class="mono" id="ide-path">${esc(IDE.repo ? "/workspace/repos/" + IDE.repo : "")}</span><span class="chip warn" id="ide-trunc-warn" hidden>256 KiB 초과 · 읽기 전용</span></span>
            <span class="row" style="gap:6px">
              <span class="seg" id="ide-mode"><button data-m="file">편집</button><button data-m="diff">HEAD Diff</button><button data-m="preview" id="ide-mode-preview" hidden>미리보기</button></span>
              <button class="g" id="ide-run" style="padding:3px 9px;font-size:12px" hidden>▶ 실행</button>
              <button class="g" id="ide-ask-agent" style="padding:3px 9px;font-size:12px" hidden>✦ 에이전트 전달</button>
              <button class="g bad" id="ide-revert" style="padding:3px 9px;font-size:12px" hidden title="HEAD 버전으로 되돌리기">↶ 되돌리기</button>
              <button class="b" id="ide-save" style="padding:3px 9px;font-size:12px" disabled>저장</button>
            </span>
          </div>
          <div class="ide-editor">
            <div id="ide-editor" style="position:absolute; inset:0"></div>
            <iframe id="ide-preview" style="position:absolute; inset:0; width:100%; height:100%; border:0; background:#fff" hidden></iframe>
            <div class="ide-empty" id="ide-empty"><div><div style="font-size:28px; margin-bottom:6px">⌘</div>왼쪽 탐색기에서 파일이나 <span class="mono">cells/</span> 셀 파일을 여세요.<br><span class="muted" style="font-size:12px">Ctrl+S 저장 · Ctrl+P 찾기 · 여기서 편집한 <span class="mono">cells/</span> 파일은 Studio와 동기화됩니다.</span></div></div>
          </div>
          <div class="ide-bottom" id="ide-bottom" hidden>
            <div class="ide-head" style="padding:4px 10px">
              <span class="seg"><button data-ibot="out" class="on">실행 출력</button><button data-ibot="term">통합 터미널</button></span>
              <button class="icon" id="ide-bot-close">✕</button>
            </div>
            <div id="ide-bot-out" style="padding:8px 10px;overflow:auto;max-height:200px"><div id="ide-run-out"></div></div>
            <div id="ide-bot-term" style="padding:6px" hidden><div id="ide-term-host" class="term" style="height:210px"></div></div>
          </div>
        </div>
        ${splitOn ? "" : `<div class="ide-col log"><div class="ide-head">커밋 타임라인</div><div class="ide-scroll" id="ide-log" style="padding:0"></div></div>`}
      </div>
    </div>`;

    G.V(`<div class="row" style="justify-content:space-between; margin-bottom:10px">
        <div><h2>IDE · 코드 & 셀</h2><p class="sub" style="margin:0">VM의 <span class="mono">/workspace/repos/${esc(IDE.repo || "workspace")}</span> 파일을 직접 생성·편집·실행하고, 노트북 셀(<span class="mono">cells/</span>)과 양방향 동기화합니다.</p></div>
        <div class="row">
          ${paneBtns}
          <select id="ide-repo">${repos.length ? repos.map(r => `<option value="${esc(r.repo)}" ${r.repo === IDE.repo ? "selected" : ""}>${esc(r.repo)} · ${esc(r.branch)}${r.dirty ? " ●" : ""}</option>`).join("") : `<option value="">저장소 없음</option>`}</select>
          <button class="g" id="ide-new-repo">+ 저장소</button>
          <button class="g" id="ide-wdiff">작업 트리 diff</button>
          <button class="g" id="ide-commit">✓ 커밋</button>
          <button class="g" id="ide-toggle-term">터미널</button>
          <button class="g" id="ide-refresh">새로고침</button>
        </div></div>
      ${err ? `<div class="card bad">${esc(err)}</div>` : ""}
      ${splitOn
        ? `<div class="ide-split">${ideShell}<div class="ide-split-studio">${sid ? `<iframe class="studio-frame" style="height:calc(100vh - 180px);min-height:480px" src="/agdd/notebook?workspace=${esc(sid)}&embed=1" title="IRADE 노트북 Studio"></iframe>` : `<div class="card">IRADE 로그인 후 Studio를 사용할 수 있습니다.</div>`}</div></div>`
        : ideShell}`);

    bindPane(); ideBind();
    $("#ide-filter").oninput = () => { IDE.filter = $("#ide-filter").value.toLowerCase(); $("#ide-tree").innerHTML = ideTreeHtml(); };
    $("#ide-repo").onchange = run(async () => { IDE.repo = $("#ide-repo").value; store.set("agdd.repo", IDE.repo); await ideLoadRepo(); });
    $("#ide-new-repo").onclick = run(async () => {
      const name = await idePrompt("새 저장소 이름 (영문/숫자/._-)", "app");
      if (!name || !name.trim()) return;
      await ideMut("POST", "/repos", { repo: name.trim() });
      IDE.repo = name.trim(); store.set("agdd.repo", IDE.repo);
      for (const k of [...CACHE.keys()]) if (k.includes("/ide/")) CACHE.delete(k);
      G.render();
    });
    $("#ide-new-file").onclick = run(async () => {
      if (!IDE.repo) return;
      const p = await idePrompt("새 파일 경로 (예: src/app.py 또는 cells/note.md)", "main.py");
      if (!p || !p.trim()) return;
      const clean = p.trim();
      await ideMut("PUT", `/repos/${encodeURIComponent(IDE.repo)}/files/${encPath(clean)}`, { content: "" });
      if (clean.startsWith("cells/")) notifyStudioCellChange();
      await refreshTreeOnly();
      await ideOpen(clean);
    });
    $("#ide-new-dir").onclick = run(async () => {
      if (!IDE.repo) return;
      const p = await idePrompt("새 폴더 경로 (예: src/components)", "src");
      if (!p || !p.trim()) return;
      await ideMut("POST", `/repos/${encodeURIComponent(IDE.repo)}/mkdir`, { path: p.trim() });
      await refreshTreeOnly();
      toast(`폴더 생성됨: ${p.trim()}`);
    });
    $("#ide-save").onclick = run(saveActiveFile);
    $("#ide-revert").onclick = run(async () => {
      const path = IDE.active;
      if (!path || !IDE.repo) return;
      if (!confirm(`${path}의 변경 사항을 HEAD 커밋 상태로 되돌릴까요?`)) return;
      if (IDE.files[path]?.head != null && IDE.tree?.status?.[path]) {
        await ideMut("POST", `/repos/${encodeURIComponent(IDE.repo)}/revert`, { path });
      }
      IDE.dirty.delete(path);
      delete IDE.files[path];
      for (const k of [...CACHE.keys()]) if (k.includes("/ide/")) CACHE.delete(k);
      if (path.startsWith("cells/")) notifyStudioCellChange();
      await refreshTreeOnly();
      await ideOpen(path);
      toast(`되돌림: ${path}`);
    });
    $("#ide-commit").onclick = run(async () => {
      if (!IDE.repo) return;
      if (IDE.dirty.size && confirm(`저장되지 않은 변경 ${IDE.dirty.size}건을 먼저 저장하고 커밋할까요?`)) {
        for (const p of [...IDE.dirty.keys()]) {
          if (IDE.files[p]?.truncated) continue;
          const content = IDE.dirty.get(p);
          await ideMut("PUT", `/repos/${encodeURIComponent(IDE.repo)}/files/${encPath(p)}`, { content });
          IDE.dirty.delete(p);
        }
      }
      const msg = await idePrompt("Git 커밋 메시지", "update workspace files");
      if (!msg || !msg.trim()) return;
      const r = await ideMut("POST", `/repos/${encodeURIComponent(IDE.repo)}/commit`, { message: msg.trim() });
      if (!r.ok) return toast(r.error || "커밋할 변경 사항이 없습니다", true);
      for (const k of [...CACHE.keys()]) if (k.includes("/ide/")) CACHE.delete(k);
      await refreshTreeOnly();
      await ideShow();
      toast(`커밋됨: ${String(r.sha || "").slice(0, 7)}`);
    });
    $("#ide-run").onclick = run(async () => {
      if (!IDE.active || IDE.commit || IDE.active === "__diff__") return;
      if (IDE.dirty.has(IDE.active)) await saveActiveFile();
      toggleBottom(true, "out");
      $("#ide-run-out").innerHTML = `<div class="muted">실행 중… (${esc(IDE.active)})</div>`;
      IDE.runOut = await ideMut("POST", `/repos/${encodeURIComponent(IDE.repo)}/run`, { path: IDE.active });
      renderRunOut();
    });
    $("#ide-ask-agent").onclick = () => {
      if (!IDE.active) return;
      G.COLLAB.repo = IDE.repo;
      G.COLLAB.attachments = [{ path: IDE.active }];
      G.openTab("collab", { collabMode: "chat" });
    };
    $("#ide-toggle-term").onclick = () => toggleBottom(!IDE.termOpen || IDE.bottomTab !== "term", "term");
    $("#ide-bot-close").onclick = () => toggleBottom(false);
    document.querySelectorAll("[data-ibot]").forEach(b => b.onclick = () => toggleBottom(true, b.dataset.ibot));
    $("#ide-refresh").onclick = run(async () => {
      for (const k of [...CACHE.keys()]) if (k.includes("/ide/")) CACHE.delete(k);
      const keep = IDE.active;
      await ideLoadRepo(true);
      if (keep && keep !== "__diff__" && IDE.tree?.files.includes(keep)) await ideOpen(keep);
      toast("새로고침됨");
    });
    $("#ide-wdiff").onclick = run(async () => {
      if (!IDE.repo) return;
      IDE.files.__diff__ = (await ideApi(`/repos/${encodeURIComponent(IDE.repo)}/diff`, true)).patch;
      IDE.commit = ""; IDE.active = "__diff__"; await ideShow();
    });

    if (IDE.termOpen) toggleBottom(true, IDE.bottomTab || "out");
    if (IDE.repo && IDE.tree && IDE.ws === WS && IDE.treeRepo === IDE.repo) await ideShow();
    else if (IDE.repo) await ideLoadRepo();
    else await ideShow();
    if (IDE.pendingOpen) {
      const target = IDE.pendingOpen;
      IDE.pendingOpen = "";
      // Files just written by an agent may be missing from the cached tree; refresh once before giving up.
      if (IDE.repo && !IDE.tree?.files?.includes(target)) await refreshTreeOnly();
      if (IDE.tree?.files?.includes(target)) await ideOpen(target);
      else toast(`파일을 찾을 수 없습니다: ${target}`, true);
    }
  };
})();
