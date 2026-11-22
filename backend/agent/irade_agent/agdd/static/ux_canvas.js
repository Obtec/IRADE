(() => {
  const G = window.AGDD;
  const { $, esc, api, toast, run, stale } = G;

  const CANVAS_META_SELECTOR = ":canvas";
  const DEFAULT_CANVAS = () => ({
    bg: "",
    theme: "auto",
    gridSize: 8,
    showGrid: true,
    snapGrid: true,
    snapGuides: true,
    showColumns: false,
    columns: 12,
    colMargin: 24,
    colGutter: 16,
    density: "normal",
    defaultAccent: "#2f5d50",
    defaultRadius: 10,
    showRuler: false,
  });

  const UXC = G.UXC;
  UXC.view = UXC.view || "home";
  UXC.vp = UXC.vp || { width: 1200, height: 720 };
  UXC.canvas = { ...DEFAULT_CANVAS(), ...(UXC.canvas || {}) };
  UXC.items = UXC.items || {};
  UXC.removed = UXC.removed instanceof Set ? UXC.removed : new Set(UXC.removed || []);
  UXC.sel = UXC.sel || "";
  UXC.sels = UXC.sels instanceof Set ? UXC.sels : new Set(UXC.sel ? [UXC.sel] : []);
  UXC.clipboard = UXC.clipboard || null;
  UXC.connectMode = !!UXC.connectMode;
  UXC.json = !!UXC.json;
  UXC.undo = UXC.undo || [];
  UXC.redo = UXC.redo || [];
  UXC.dirty = !!UXC.dirty;
  UXC.pendingReq = UXC.pendingReq || null;
  UXC.decisionId = UXC.decisionId || null;
  UXC.zoom = UXC.zoom || "fit";
  UXC.showMeta = !!UXC.showMeta;
  UXC.layerFilter = UXC.layerFilter || "";
  UXC.openSections = UXC.openSections || { content: true, layout: true, style: true, inner: false, agent: true };
  UXC._undoKey = null;
  UXC._spaceDown = false;
  UXC._modalOpen = false;
  UXC._inlineEdit = false;

  const ACCENT_SWATCHES = ["#2f5d50", "#2563eb", "#4f46e5", "#d97706", "#e11d48", "#475569"];
  const VP_PRESETS = [
    ["1440x900", "Desktop 1440×900"],
    ["1200x720", "Desktop 1200×720"],
    ["1920x1080", "Full HD 1920×1080"],
    ["768x1024", "Tablet 768×1024"],
    ["390x844", "Mobile 390×844"],
  ];

  const ICONS = {
    header: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="1.5" y="2.5" width="13" height="11" rx="2"/><line x1="1.5" y1="6" x2="14.5" y2="6"/></svg>',
    sidebar: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="1.5" y="2.5" width="13" height="11" rx="2"/><line x1="6" y1="2.5" x2="6" y2="13.5"/></svg>',
    hero: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="1.5" y="2.5" width="13" height="11" rx="2"/><line x1="3.5" y1="6.5" x2="9.5" y2="6.5"/><line x1="3.5" y1="9" x2="7.5" y2="9"/></svg>',
    section: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="2" y="2.5" width="12" height="11" rx="2" stroke-dasharray="2.5 2"/></svg>',
    card: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="2" y="3" width="12" height="10" rx="2"/><line x1="4.5" y1="6.5" x2="10" y2="6.5"/><line x1="4.5" y1="9.5" x2="11.5" y2="9.5"/></svg>',
    chart: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2.5 13V9.5M6.2 13V6M9.8 13V7.5M13.5 13V3.5" stroke-linecap="round"/></svg>',
    table: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"><rect x="1.5" y="2.5" width="13" height="11" rx="1.5"/><line x1="1.5" y1="6" x2="14.5" y2="6"/><line x1="1.5" y1="9.5" x2="14.5" y2="9.5"/><line x1="6.5" y1="6" x2="6.5" y2="13.5"/></svg>',
    button: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="1.5" y="4.5" width="13" height="7" rx="3.5"/><line x1="5" y1="8" x2="11" y2="8" stroke-linecap="round"/></svg>',
    input: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="1.5" y="4" width="13" height="8" rx="2"/><line x1="4.5" y1="6.5" x2="4.5" y2="9.5" stroke-linecap="round"/></svg>',
    text: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M3 4h10M8 4v9M5.5 13h5" stroke-linecap="round"/></svg>',
    image: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"><rect x="1.5" y="2.5" width="13" height="11" rx="2"/><circle cx="5.5" cy="6" r="1.2"/><path d="M2.5 12l3.5-3.5 2.5 2.5 2-2 3 3"/></svg>',
    modal: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="2.5" y="2" width="11" height="12" rx="2"/><line x1="2.5" y1="5.5" x2="13.5" y2="5.5"/><line x1="7.5" y1="11" x2="11" y2="11"/></svg>',
    tabs: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2 11h12M3 8V5.5A1.5 1.5 0 0 1 4.5 4h2A1.5 1.5 0 0 1 8 5.5V8m1 0h4"/></svg>',
    footer: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="1.5" y="2.5" width="13" height="11" rx="2"/><line x1="1.5" y1="10" x2="14.5" y2="10"/></svg>',
  };

  const KIND_SPECS = {
    header:  { label: "Header",  ko: "헤더",     w: 1120, h: 64,  minW: 240, minH: 44, radius: 10, accent: "#2f5d50", variants: [["appbar", "앱바 (기본)"], ["search", "검색형"], ["minimal", "미니멀"]] },
    sidebar: { label: "Sidebar", ko: "사이드바", w: 224,  h: 560, minW: 64,  minH: 140, radius: 12, accent: "#2f5d50", variants: [["nav", "내비게이션"], ["compact", "컴팩트"], ["rail", "아이콘 레일"]] },
    hero:    { label: "Hero",    ko: "히어로",   w: 880,  h: 240, minW: 260, minH: 120, radius: 14, accent: "#2f5d50", variants: [["split", "스플릿 (시각 카드)"], ["centered", "중앙 정렬"], ["gradient", "강조 그라데이션"]] },
    section: { label: "Section", ko: "섹션",     w: 640,  h: 320, minW: 160, minH: 96, radius: 12, accent: "#475569", variants: [["surface", "서피스"], ["dashed", "영역 점선"], ["subtle", "투명 가이드"]] },
    card:    { label: "Card",    ko: "카드",     w: 296,  h: 176, minW: 120, minH: 76, radius: 12, accent: "#2f5d50", variants: [["elevated", "기본 카드"], ["metric", "KPI 지표"], ["media", "미디어 상단"], ["outlined", "아웃라인"]] },
    chart:   { label: "Chart",   ko: "차트",     w: 440,  h: 240, minW: 160, minH: 110, radius: 12, accent: "#2563eb", variants: [["bar", "막대 차트"], ["area", "영역 차트"], ["line", "선형 차트"], ["donut", "도넛 차트"]] },
    table:   { label: "Table",   ko: "테이블",   w: 560,  h: 248, minW: 220, minH: 120, radius: 10, accent: "#2f5d50", variants: [["striped", "줄무늬"], ["clean", "깔끔한 행"], ["compact", "조밀한 행"]] },
    button:  { label: "Button",  ko: "버튼",     w: 144,  h: 44,  minW: 56,  minH: 28, radius: 8,  accent: "#2f5d50", variants: [["primary", "Primary"], ["secondary", "Secondary"], ["outline", "Outline"], ["ghost", "Ghost"], ["danger", "Danger"]] },
    input:   { label: "Input",   ko: "입력창",   w: 280,  h: 64,  minW: 110, minH: 32, radius: 8,  accent: "#2563eb", variants: [["text", "텍스트 입력"], ["search", "검색창"], ["select", "셀렉트"], ["textarea", "멀티라인"]] },
    text:    { label: "Text",    ko: "텍스트",   w: 320,  h: 72,  minW: 72,  minH: 28, radius: 6,  accent: "#475569", variants: [["heading", "소제목"], ["title", "큰 제목"], ["body", "본문"], ["caption", "캡션"]] },
    image:   { label: "Image",   ko: "미디어",   w: 320,  h: 192, minW: 64,  minH: 64, radius: 12, accent: "#4f46e5", variants: [["cover", "이미지"], ["video", "비디오 플레이어"], ["avatar", "아바타"]] },
    modal:   { label: "Modal",   ko: "모달",     w: 400,  h: 256, minW: 220, minH: 150, radius: 14, accent: "#2f5d50", variants: [["dialog", "다이얼로그"], ["alert", "경고 확인"], ["sheet", "시트"]] },
    tabs:    { label: "Tabs",    ko: "탭바",     w: 440,  h: 48,  minW: 160, minH: 34, radius: 9,  accent: "#2f5d50", variants: [["underline", "밑줄 탭"], ["pill", "필 탭"]] },
    footer:  { label: "Footer",  ko: "푸터",     w: 1120, h: 56,  minW: 240, minH: 38, radius: 10, accent: "#475569", variants: [["standard", "표준 푸터"], ["minimal", "미니멀"]] },
  };

  const PALETTE = ["header", "sidebar", "hero", "section", "card", "chart", "table", "button", "input", "text", "image", "modal", "tabs", "footer"];

  const KIND_ALIASES = {
    nav: "header", navbar: "header", topbar: "header", appbar: "header",
    aside: "sidebar", menu: "sidebar", drawer: "sidebar",
    banner: "hero", jumbotron: "hero",
    container: "section", panel: "section", grid: "section", list: "section", box: "section",
    kpi: "card", stat: "card", tile: "card",
    graph: "chart", plot: "chart", analytics: "chart",
    datagrid: "table",
    btn: "button", cta: "button",
    field: "input", search: "input", select: "input", textarea: "input", form: "input",
    heading: "text", title: "text", paragraph: "text", label: "text",
    media: "image", photo: "image", video: "image", avatar: "image",
    dialog: "modal", popup: "modal",
    tab: "tabs",
  };

  function resolveVisualKind(raw) {
    const k = String(raw || "section").trim().toLowerCase();
    if (KIND_SPECS[k]) return k;
    return KIND_ALIASES[k] || "generic";
  }

  function safeColor(val, fallback) {
    const s = String(val || "").trim();
    return /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$/.test(s) ? s : fallback;
  }

  function isDarkHex(hex) {
    const h = safeColor(hex, "");
    if (!h) return false;
    let c = h.slice(1);
    if (c.length === 3) c = c.split("").map(ch => ch + ch).join("");
    if (c.length === 8) c = c.slice(0, 6);
    const r = parseInt(c.slice(0, 2), 16), g = parseInt(c.slice(2, 4), 16), b = parseInt(c.slice(4, 6), 16);
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) < 142;
  }

  const deepClone = o => JSON.parse(JSON.stringify(o));
  const clampInt = (v, lo, hi, fb) => {
    const n = parseInt(v, 10);
    return Number.isFinite(n) ? Math.max(lo, Math.min(hi, n)) : fb;
  };

  /* ---------------- Selection Helpers ---------------- */
  function setSelection(list, primary = "") {
    const valid = [...new Set((list || []).filter(s => UXC.items[s]))];
    UXC.sels = new Set(valid);
    if (primary && UXC.sels.has(primary)) UXC.sel = primary;
    else UXC.sel = valid[valid.length - 1] || "";
    UXC._undoKey = null;
  }

  function getSelected() {
    const valid = [...UXC.sels].filter(s => UXC.items[s]);
    if (UXC.sel && UXC.items[UXC.sel] && !valid.includes(UXC.sel)) valid.push(UXC.sel);
    return valid;
  }

  function isSelected(sel) {
    return sel === UXC.sel || UXC.sels.has(sel);
  }

  /* ---------------- Undo / Redo / Dirty ---------------- */
  function captureState() {
    return JSON.stringify({
      items: UXC.items,
      removed: [...UXC.removed],
      vp: UXC.vp,
      canvas: UXC.canvas,
      sel: UXC.sel,
      sels: [...UXC.sels],
    });
  }

  function syncToolbarState() {
    const u = $("#uxc-undo"), r = $("#uxc-redo");
    if (u) u.disabled = !UXC.undo.length;
    if (r) r.disabled = !UXC.redo.length;
    $("#uxc-grid")?.classList.toggle("on", !!UXC.canvas.showGrid);
    $("#uxc-cols-btn")?.classList.toggle("on", !!UXC.canvas.showColumns);
    $("#uxc-meta")?.classList.toggle("on", !!UXC.showMeta);
    const sv = $("#uxc-save");
    if (sv) sv.innerHTML = UXC.dirty ? `<span class="uxc-badge-dot"></span>저장` : "저장";
    const vpSel = $("#uxc-vp");
    if (vpSel) {
      const k = `${UXC.vp.width}x${UXC.vp.height}`;
      vpSel.value = VP_PRESETS.some(([pk]) => pk === k) ? k : "custom";
    }
  }

  function snapshotUndo() {
    UXC.undo.push(captureState());
    if (UXC.undo.length > 60) UXC.undo.shift();
    UXC.redo = [];
    UXC.dirty = true;
    UXC._undoKey = null;
    syncToolbarState();
  }

  function snapshotUndoOnce(key) {
    if (UXC._undoKey === key) return;
    snapshotUndo();
    UXC._undoKey = key;
  }

  function restoreSnapshot(raw) {
    const p = JSON.parse(raw);
    UXC.items = p.items || {};
    UXC.removed = new Set(p.removed || []);
    if (p.vp) UXC.vp = { width: p.vp.width || 1200, height: p.vp.height || 720 };
    if (p.canvas) UXC.canvas = { ...DEFAULT_CANVAS(), ...p.canvas };
    setSelection(p.sels || (p.sel ? [p.sel] : []), p.sel || "");
    UXC.connectMode = false;
    UXC.dirty = true;
    drawStage();
    drawInspector();
    syncToolbarState();
  }

  function performUndo() {
    if (!UXC.undo.length) return;
    UXC.redo.push(captureState());
    restoreSnapshot(UXC.undo.pop());
  }

  function performRedo() {
    if (!UXC.redo.length) return;
    UXC.undo.push(captureState());
    restoreSnapshot(UXC.redo.pop());
  }

  /* ---------------- Data Models (Table, Chart, Canvas) ---------------- */
  function defaultTableData() {
    return {
      columns: ["ID", "항목", "상태", "업데이트"],
      rows: [
        ["#104", "인증 게이트웨이", "완료", "오늘"],
        ["#105", "명세 동기화 API", "진행중", "2분 전"],
        ["#106", "2D 캔버스 렌더러", "검토", "방금"],
        ["#107", "접근성 감사", "대기", "어제"],
      ],
      badgeCol: 2,
      showToolbar: true,
      showHeader: true,
    };
  }

  function normalizeTableData(raw) {
    const base = defaultTableData();
    const src = raw && typeof raw === "object" ? raw : {};
    let cols = Array.isArray(src.columns) ? src.columns.map((c, i) => String(c ?? `열 ${i + 1}`)) : [...base.columns];
    if (!cols.length) cols = ["열 1"];
    const rowsIn = Array.isArray(src.rows) ? src.rows : base.rows;
    const rows = rowsIn.map(r => {
      const arr = Array.isArray(r) ? r : cols.map(c => (r && typeof r === "object" ? r[c] : ""));
      return cols.map((_, ci) => String(arr[ci] ?? ""));
    });
    const badgeRaw = src.badgeCol !== undefined ? parseInt(src.badgeCol, 10) : base.badgeCol;
    const badgeCol = Number.isInteger(badgeRaw) && badgeRaw >= 0 && badgeRaw < cols.length ? badgeRaw : -1;
    return {
      columns: cols,
      rows,
      badgeCol,
      showToolbar: src.showToolbar !== false,
      showHeader: src.showHeader !== false,
    };
  }

  function defaultChartData() {
    return {
      labels: ["월", "화", "수", "목", "금", "토", "일"],
      values: [44, 68, 52, 86, 62, 95, 76],
      unit: "%",
    };
  }

  function normalizeChartData(raw) {
    const base = defaultChartData();
    const src = raw && typeof raw === "object" ? raw : {};
    const vals = Array.isArray(src.values) && src.values.length
      ? src.values.map(v => Math.max(0, Number(v) || 0))
      : [...base.values];
    const labels = Array.isArray(src.labels) && src.labels.length
      ? vals.map((_, i) => String(src.labels[i] ?? `#${i + 1}`))
      : vals.map((_, i) => base.labels[i] || `P${i + 1}`);
    return { labels, values: vals, unit: String(src.unit ?? "%") };
  }

  function loadCanvasMeta(cfg) {
    const def = DEFAULT_CANVAS();
    const c = cfg && typeof cfg === "object" ? cfg : {};
    const sz = c.size || {};
    const ly = c.layout || {};
    const sp = c.spacing || {};
    const tk = c.tokens || {};
    const pr = c.props || {};
    UXC.vp = {
      width: clampInt(sz.w, 320, 3840, 1200),
      height: clampInt(sz.h, 320, 2160, 720),
    };
    UXC.canvas = {
      bg: safeColor(tk.bg, ""),
      theme: ["auto", "light", "dark"].includes(pr.theme) ? pr.theme : def.theme,
      gridSize: [4, 8, 12, 16, 24].includes(+pr.gridSize) ? +pr.gridSize : def.gridSize,
      showGrid: pr.showGrid !== false,
      snapGrid: pr.snapGrid !== false,
      snapGuides: pr.snapGuides !== false,
      showColumns: !!pr.showColumns,
      columns: clampInt(ly.columns, 1, 24, def.columns),
      colMargin: clampInt(sp.padding, 0, 160, def.colMargin),
      colGutter: clampInt(sp.gap, 0, 80, def.colGutter),
      density: ["compact", "normal", "comfortable"].includes(pr.density) ? pr.density : def.density,
      defaultAccent: safeColor(tk.accent, def.defaultAccent),
      defaultRadius: clampInt(tk.radius, 0, 32, def.defaultRadius),
      showRuler: !!pr.showRuler,
    };
  }

  function exportCanvasMetaItem() {
    return {
      locked: false,
      config: {
        layout: { columns: UXC.canvas.columns },
        size: { w: UXC.vp.width, h: UXC.vp.height },
        spacing: { padding: UXC.canvas.colMargin, gap: UXC.canvas.colGutter },
        tokens: {
          bg: UXC.canvas.bg,
          accent: UXC.canvas.defaultAccent,
          radius: UXC.canvas.defaultRadius,
        },
        props: {
          kind: "canvas",
          gridSize: UXC.canvas.gridSize,
          showGrid: UXC.canvas.showGrid,
          snapGrid: UXC.canvas.snapGrid,
          snapGuides: UXC.canvas.snapGuides,
          showColumns: UXC.canvas.showColumns,
          density: UXC.canvas.density,
          theme: UXC.canvas.theme,
          showRuler: UXC.canvas.showRuler,
        },
      },
    };
  }

  function makeBox(selector, kind, label, x, y, w, h, note = "", extra = {}) {
    const vk = resolveVisualKind(kind);
    const spec = KIND_SPECS[vk] || KIND_SPECS.card;
    const z = extra.z ?? (Object.keys(UXC.items).length + 1);
    const bg = extra.bg ? safeColor(extra.bg, "") : "";
    const defAccent = UXC.canvas?.defaultAccent || spec.accent || "#2f5d50";
    const accent = safeColor(extra.accent, spec.accent === "#2f5d50" ? defAccent : (spec.accent || defAccent));
    const radius = extra.radius ?? (vk === "card" || vk === "section" || vk === "table" ? UXC.canvas?.defaultRadius : spec.radius) ?? spec.radius ?? 10;
    const props = {
      kind: kind || "section",
      label: label || selector,
      note,
      connects_to: Array.isArray(extra.connects_to) ? [...extra.connects_to] : [],
    };
    if (extra.props && typeof extra.props === "object") Object.assign(props, deepClone(extra.props));
    if (vk === "table") props.table = normalizeTableData(extra.table || props.table);
    if (vk === "chart" && (extra.chart || props.chart)) props.chart = normalizeChartData(extra.chart || props.chart);

    return {
      locked: extra.locked !== undefined ? !!extra.locked : true,
      config: {
        layout: {
          x, y, z,
          direction: extra.direction || "column",
          columns: extra.columns || 1,
          ...(extra.align ? { align: extra.align } : {}),
          ...(extra.justify ? { justify: extra.justify } : {}),
        },
        size: { w, h },
        spacing: { padding: extra.padding ?? 14, gap: extra.gap ?? 10 },
        tokens: {
          bg,
          ...(bg ? { bgCustom: true } : {}),
          accent,
          radius,
          ...(extra.tokens ? deepClone(extra.tokens) : {}),
        },
        visibility: extra.visibility || "visible",
        order: z,
        variant: extra.variant || (spec.variants?.[0]?.[0] || "default"),
        props,
      },
    };
  }

  function ensureBoxFields(sel, item, idx) {
    const c = item?.config || {};
    const rawProps = { kind: "section", label: sel, note: "", connects_to: [], ...(c.props || {}) };
    const vk = resolveVisualKind(rawProps.kind);
    const spec = KIND_SPECS[vk] || KIND_SPECS.card;
    if (vk === "table") rawProps.table = normalizeTableData(rawProps.table);
    if (vk === "chart" && rawProps.chart) rawProps.chart = normalizeChartData(rawProps.chart);

    const layout = {
      x: 40 + (idx % 3) * 260,
      y: 40 + Math.floor(idx / 3) * 160,
      z: idx + 1,
      direction: "column",
      columns: 1,
      ...(c.layout || {}),
    };
    const size = { w: spec.w || 240, h: spec.h || 120, ...(c.size || {}) };
    const spacing = { padding: 14, gap: 10, ...(c.spacing || {}) };
    const rawTokens = c.tokens || {};
    const isLegacyBg = rawTokens.bg === "#1f2937" && !rawTokens.bgCustom;
    const bg = isLegacyBg ? "" : safeColor(rawTokens.bg, "");
    const tokens = {
      ...rawTokens,
      bg,
      ...(bg && rawTokens.bgCustom ? { bgCustom: true } : {}),
      accent: safeColor(rawTokens.accent, spec.accent || "#2f5d50"),
      radius: Number.isFinite(+rawTokens.radius) ? Math.max(0, Math.min(32, +rawTokens.radius)) : (spec.radius ?? 10),
    };
    if (!tokens.bgCustom) delete tokens.bgCustom;
    const variant = c.variant || (spec.variants?.[0]?.[0] || "default");
    return {
      locked: item?.locked !== false,
      config: {
        layout,
        size,
        spacing,
        tokens,
        visibility: c.visibility || "visible",
        order: c.order ?? layout.z,
        variant,
        props: rawProps,
      },
    };
  }

  function loadPendingIntoCanvas(req) {
    if (!req) return;
    if (req.viewport) UXC.vp = { width: req.viewport.width || 1200, height: req.viewport.height || 720 };
    for (const el of req.elements || []) {
      if (!UXC.items[el.selector]) {
        const vk = resolveVisualKind(el.kind || "section");
        const spec = KIND_SPECS[vk] || KIND_SPECS.card;
        UXC.items[el.selector] = makeBox(
          el.selector,
          el.kind || "section",
          el.label || el.selector,
          el.x ?? 40,
          el.y ?? 40,
          el.w ?? spec.w ?? 240,
          el.h ?? spec.h ?? 120,
          el.note || "",
          { variant: el.variant, props: el.props },
        );
      }
    }
  }

  /* ---------------- Box CSS Variables Helper ---------------- */
  const SHADOW_MAP = {
    none: "none",
    sm: "0 1px 3px rgba(18,18,16,0.08)",
    md: "0 8px 20px rgba(18,18,16,0.13)",
    lg: "0 18px 36px rgba(18,18,16,0.20)",
  };
  const ALIGN_MAP = { start: "flex-start", center: "center", end: "flex-end", stretch: "stretch" };
  const JUSTIFY_MAP = { start: "flex-start", center: "center", end: "flex-end", between: "space-between" };

  function buildBoxStyleAttr(c, accent, rad) {
    const tk = c.tokens || {};
    const sp = c.spacing || {};
    const ly = c.layout || {};
    const parts = [
      `left:${ly.x}px`,
      `top:${ly.y}px`,
      `width:${c.size.w}px`,
      `height:${c.size.h}px`,
      `z-index:${ly.z || 1}`,
      `--obj-accent:${accent}`,
      `--obj-radius:${rad}px`,
    ];
    if (tk.textColor && safeColor(tk.textColor, "")) {
      parts.push(`--obj-fg:${safeColor(tk.textColor, "")}`);
    }
    if (Number.isFinite(+tk.opacity) && +tk.opacity < 100) {
      parts.push(`--obj-opacity:${Math.max(0.1, Math.min(1, +tk.opacity / 100))}`);
    }
    if (Number.isFinite(+tk.fontSize) && +tk.fontSize > 0) parts.push(`--obj-fs:${+tk.fontSize}px`);
    if (tk.fontWeight) parts.push(`--obj-fw:${esc(tk.fontWeight)}`);
    if (["left", "center", "right"].includes(tk.textAlign)) parts.push(`--obj-ta:${tk.textAlign}`);
    if (tk.borderWidth !== undefined && tk.borderWidth !== "") parts.push(`--obj-bw:${Math.max(0, Math.min(8, +tk.borderWidth || 0))}px`);
    if (tk.borderStyle) parts.push(`--obj-bs:${esc(tk.borderStyle)}`);
    if (tk.borderColor && safeColor(tk.borderColor, "")) parts.push(`--obj-bc:${safeColor(tk.borderColor, "")}`);
    if (tk.shadow && SHADOW_MAP[tk.shadow]) parts.push(`--obj-shadow:${SHADOW_MAP[tk.shadow]}`);
    if (sp.paddingCustom && Number.isFinite(+sp.padding)) parts.push(`--obj-pad:${Math.max(0, +sp.padding)}px`);
    if (sp.gapCustom && Number.isFinite(+sp.gap)) parts.push(`--obj-gap:${Math.max(0, +sp.gap)}px`);
    if (ly.align && ALIGN_MAP[ly.align]) parts.push(`--obj-align:${ALIGN_MAP[ly.align]}`);
    if (ly.justify && JUSTIFY_MAP[ly.justify]) parts.push(`--obj-justify:${JUSTIFY_MAP[ly.justify]}`);
    return parts.join(";");
  }

  /* ---------------- Realistic Per-Kind Renderers ---------------- */
  function renderInterior(sel, item) {
    const c = item.config;
    const w = c.size.w, h = c.size.h;
    const p = c.props || {};
    const rawKind = p.kind || "section";
    const vk = resolveVisualKind(rawKind);
    const variant = c.variant || (KIND_SPECS[vk]?.variants?.[0]?.[0] || "default");
    const label = p.label || sel;
    const note = (p.note || "").trim();
    const bg = safeColor(c.tokens?.bg, "");
    const bgStyle = bg ? `--obj-bg:${bg};` : "";
    const accent = safeColor(c.tokens?.accent, KIND_SPECS[vk]?.accent || "#2f5d50");

    switch (vk) {
      case "header": {
        const navItems = Array.isArray(p.navItems) && p.navItems.length ? p.navItems : ["개요", "분석", "문서", "설정"];
        const activeIdx = clampInt(p.activeIndex, 0, Math.max(0, navItems.length - 1), 0);
        const showNav = variant !== "minimal" && w >= 460;
        const shownNav = w >= 700 ? navItems : navItems.slice(0, 3);
        const navHtml = variant === "search"
          ? `<div class="uxc-hdr-search"><svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="7" cy="7" r="4.8"/><line x1="10.8" y1="10.8" x2="14" y2="14"/></svg><span>${esc(p.searchPlaceholder || note || "검색…")}</span></div>`
          : showNav
            ? `<div class="uxc-hdr-nav">${shownNav.map((t, i) => `<span class="${i === activeIdx ? "active" : ""}">${esc(t)}</span>`).join("")}</div>`
            : "";
        const ctaLabel = p.ctaLabel !== undefined ? p.ctaLabel : "시작하기";
        return `<div class="uxc-k-header v-${esc(variant)}" style="${bgStyle}">
          <div class="uxc-hdr-brand">
            <span class="uxc-hdr-mark"><svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 8.5L6.5 12 13 4.5" stroke-linecap="round" stroke-linejoin="round"/></svg></span>
            <span class="uxc-hdr-title">${esc(label)}</span>
          </div>
          ${navHtml}
          <div class="uxc-hdr-actions">
            ${w >= 340 && ctaLabel ? `<span class="uxc-mini-btn solid">${esc(ctaLabel)}</span>` : ""}
            <span class="uxc-avatar-dot">U</span>
          </div>
        </div>`;
      }

      case "sidebar": {
        const rail = variant === "rail" || w < 116;
        const iconPaths = [
          '<path d="M2.5 2.5h4.5v4.5H2.5zM9 2.5h4.5v4.5H9zM2.5 9h4.5v4.5H2.5zM9 9h4.5v4.5H9z"/>',
          '<path d="M2 4.5h4l1.5 1.8H14v7.2H2z"/>',
          '<path d="M3 13V8m5 5V4m5 9V6.5"/>',
          '<circle cx="6.5" cy="5.5" r="2.2"/><path d="M2.5 13c.6-2.2 2.2-3.2 4-3.2s3.4 1 4 3.2"/>',
          '<circle cx="8" cy="8" r="2.2"/><path d="M8 2.5v1.5m0 8v1.5M2.5 8H4m8 0h1.5"/>',
        ];
        const navItems = Array.isArray(p.navItems) && p.navItems.length ? p.navItems : ["대시보드", "프로젝트", "분석", "팀원", "설정"];
        const activeIdx = clampInt(p.activeIndex, 0, Math.max(0, navItems.length - 1), 0);
        const maxItems = Math.max(2, Math.min(navItems.length, Math.floor((h - 90) / 34)));
        const footerTxt = p.footerLabel !== undefined ? p.footerLabel : "Workspace";
        return `<div class="uxc-k-sidebar ${rail ? "uxc-sb-rail" : ""}" style="${bgStyle}">
          <div class="uxc-sb-head">
            <span class="uxc-hdr-mark" style="width:22px;height:22px;border-radius:6px"><svg width="12" height="12" viewBox="0 0 16 16" fill="currentColor"><rect x="3" y="3" width="4" height="4" rx="1"/><rect x="9" y="3" width="4" height="4" rx="1"/><rect x="3" y="9" width="4" height="4" rx="1"/></svg></span>
            ${rail ? "" : `<div style="min-width:0"><div class="uxc-sb-title">${esc(label)}</div>${note ? `<div class="uxc-sb-sub">${esc(note)}</div>` : ""}</div>`}
          </div>
          <div class="uxc-sb-list">
            ${navItems.slice(0, maxItems).map((t, i) => `<div class="uxc-sb-item ${i === activeIdx ? "active" : ""}">
              <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">${iconPaths[i % iconPaths.length]}</svg>
              ${rail ? "" : `<span>${esc(t)}</span>`}
            </div>`).join("")}
          </div>
          ${h >= 220 && footerTxt ? `<div class="uxc-sb-foot" style="${rail ? "justify-content:center" : ""}"><span class="uxc-avatar-dot" style="width:20px;height:20px;font-size:9px">A</span>${rail ? "" : `<span>${esc(footerTxt)}</span>`}</div>` : ""}
        </div>`;
      }

      case "hero": {
        const showVisual = variant === "split" && w >= 480 && h >= 150;
        const badge = p.badge !== undefined ? p.badge : "✦ Featured";
        const cta1 = p.primaryCta !== undefined ? p.primaryCta : "지금 시작하기 →";
        const cta2 = p.secondaryCta !== undefined ? p.secondaryCta : "자세히 보기";
        return `<div class="uxc-k-hero v-${esc(variant)}" style="${bgStyle}">
          <div class="uxc-hero-copy">
            ${badge ? `<div><span class="uxc-pill accent">${esc(badge)}</span></div>` : ""}
            <div class="uxc-hero-title">${esc(label)}</div>
            <div class="uxc-hero-desc">${esc(note || "핵심 가치와 주요 액션을 명확하게 전달하는 히어로 배너 영역입니다.")}</div>
            <div class="uxc-hero-ctas">
              ${cta1 ? `<span class="uxc-mini-btn solid">${esc(cta1)}</span>` : ""}
              ${w >= 320 && cta2 ? `<span class="uxc-mini-btn ghost">${esc(cta2)}</span>` : ""}
            </div>
          </div>
          ${showVisual ? `<div class="uxc-hero-visual">
            <div style="display:flex;justify-content:space-between;align-items:center">
              <span class="uxc-skel" style="width:48%;height:9px"></span>
              <span class="uxc-pill accent" style="font-size:9px;padding:1px 6px">Live</span>
            </div>
            <div class="uxc-skel soft" style="width:76%;height:7px"></div>
            <div style="flex:1;border-radius:7px;background:color-mix(in srgb,var(--obj-accent) 10%, var(--uxc-surface-alt));border:1px solid var(--uxc-hairline);display:flex;align-items:flex-end;gap:6px;padding:8px">
              <i style="flex:1;height:42%;border-radius:3px;background:color-mix(in srgb,var(--obj-accent) 45%, transparent)"></i>
              <i style="flex:1;height:75%;border-radius:3px;background:color-mix(in srgb,var(--obj-accent) 70%, transparent)"></i>
              <i style="flex:1;height:56%;border-radius:3px;background:color-mix(in srgb,var(--obj-accent) 55%, transparent)"></i>
              <i style="flex:1;height:90%;border-radius:3px;background:var(--obj-accent)"></i>
            </div>
          </div>` : ""}
        </div>`;
      }

      case "section": {
        const cols = Math.max(1, Math.min(6, Number(c.layout?.columns) || 1));
        const dir = c.layout?.direction || "column";
        const trackCount = dir === "row" ? Math.max(2, cols) : cols;
        const badge = dir === "grid" || cols > 1 ? `${dir} · ${trackCount}열` : dir;
        return `<div class="uxc-k-section v-${esc(variant)}" style="${bgStyle}">
          <div class="uxc-sec-head">
            <div class="uxc-sec-title">
              <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="2" y="2.5" width="12" height="11" rx="2"/></svg>
              <span>${esc(label)}</span>
              ${note ? `<span class="uxc-sec-note">· ${esc(note)}</span>` : ""}
            </div>
            <span class="uxc-pill">${esc(badge)}</span>
          </div>
          ${h >= 120 && trackCount > 1 ? `<div class="uxc-sec-tracks" style="grid-template-columns:repeat(${trackCount},1fr)">${Array.from({ length: trackCount }, () => '<div class="uxc-sec-track"></div>').join("")}</div>` : ""}
        </div>`;
      }

      case "card": {
        if (variant === "metric") {
          const numMatch = note.match(/[$₩€£]?\d[\d,.]*[%KMB+]*/);
          const kpi = p.metricValue || (numMatch ? numMatch[0] : "12,840");
          const trend = p.metricTrend !== undefined ? p.metricTrend : "↗ +14.2%";
          const restNote = note && !p.metricValue && numMatch ? note.replace(numMatch[0], "").trim() : note;
          const showSpark = p.showSpark !== false;
          return `<div class="uxc-k-card v-metric" style="${bgStyle}">
            <div class="uxc-card-head">
              <span class="uxc-card-title" style="color:var(--obj-muted);font-size:12px;font-weight:600">${esc(label)}</span>
              ${trend ? `<span class="uxc-kpi-trend">${esc(trend)}</span>` : ""}
            </div>
            <div class="uxc-kpi-val">${esc(kpi)}</div>
            ${restNote ? `<div class="uxc-card-note" style="-webkit-line-clamp:1">${esc(restNote)}</div>` : ""}
            ${h >= 115 && showSpark ? `<svg class="uxc-spark" viewBox="0 0 200 44" preserveAspectRatio="none">
              <path d="M0 36 C 28 32, 42 18, 66 22 C 92 26, 114 10, 140 14 C 164 18, 180 6, 200 4 L 200 44 L 0 44 Z" fill="${accent}" fill-opacity="0.14"/>
              <path d="M0 36 C 28 32, 42 18, 66 22 C 92 26, 114 10, 140 14 C 164 18, 180 6, 200 4" fill="none" stroke="${accent}" stroke-width="2.2" stroke-linecap="round"/>
            </svg>` : ""}
          </div>`;
        }
        const badge = p.badge !== undefined ? p.badge : "활성";
        const cta = p.ctaLabel !== undefined ? p.ctaLabel : "자세히 →";
        if (variant === "media") {
          return `<div class="uxc-k-card v-media" style="${bgStyle}">
            <div class="uxc-card-media-top"><svg width="24" height="24" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3"><rect x="1.5" y="2.5" width="13" height="11" rx="2"/><circle cx="5.5" cy="6" r="1.2"/><path d="M2.5 12l3.5-3.5 2.5 2.5 2-2 3 3"/></svg></div>
            <div class="uxc-card-media-body">
              <div class="uxc-card-title">${esc(label)}</div>
              ${note ? `<div class="uxc-card-note">${esc(note)}</div>` : `<div class="uxc-skel" style="width:82%"></div><div class="uxc-skel soft" style="width:58%"></div>`}
              ${h >= 165 ? `<div class="uxc-card-foot"><span>${esc(badge || "업데이트됨")}</span><span style="color:var(--obj-accent-fg);font-weight:600">${esc(cta || "열기 →")}</span></div>` : ""}
            </div>
          </div>`;
        }
        return `<div class="uxc-k-card v-${esc(variant)}" style="${bgStyle}">
          <div class="uxc-card-head">
            <div class="uxc-card-head-l">
              <span class="uxc-card-icon"><svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="2.5" y="3" width="11" height="10" rx="2"/><path d="M5.5 7h5M5.5 10h3.5" stroke-linecap="round"/></svg></span>
              <span class="uxc-card-title">${esc(label)}</span>
            </div>
            ${badge ? `<span class="uxc-pill accent" style="font-size:9.5px;padding:1px 6px">${esc(badge)}</span>` : ""}
          </div>
          ${note
            ? `<div class="uxc-card-note">${esc(note)}</div>`
            : `<div style="display:flex;flex-direction:column;gap:6px;margin-top:2px">
                <div class="uxc-skel" style="width:88%"></div>
                <div class="uxc-skel soft" style="width:66%"></div>
              </div>`}
          ${h >= 126 && cta ? `<div class="uxc-card-foot"><span>${esc(c.layout?.direction || "column")}</span><span style="color:var(--obj-accent-fg);font-weight:600">${esc(cta)}</span></div>` : ""}
        </div>`;
      }

      case "chart": {
        const ch = normalizeChartData(p.chart);
        const vals = ch.values;
        const maxV = Math.max(1, ...vals);
        let plotSvg = "";
        let legendHtml = "";
        if (variant === "donut") {
          const total = Math.max(1, vals.reduce((a, b) => a + b, 0));
          const p0 = Math.round((vals[0] / total) * 100);
          const circ = 2 * Math.PI * 42;
          let off = 0;
          const arcs = vals.slice(0, 4).map((v, idx) => {
            const len = (v / total) * circ;
            const op = idx === 0 ? 1 : Math.max(0.18, 0.72 - idx * 0.18);
            const seg = `<circle cx="60" cy="60" r="42" fill="none" stroke="${accent}" stroke-opacity="${op.toFixed(2)}" stroke-width="14" stroke-dasharray="${len.toFixed(1)} ${circ.toFixed(1)}" stroke-dashoffset="${(-off).toFixed(1)}" transform="rotate(-90 60 60)"/>`;
            off += len;
            return seg;
          }).join("");
          plotSvg = `<svg class="uxc-chart-svg" viewBox="0 0 120 120" preserveAspectRatio="xMidYMid meet">
            <circle cx="60" cy="60" r="42" fill="none" stroke="var(--uxc-skeleton)" stroke-width="14"/>
            ${arcs}
            <text x="60" y="58" text-anchor="middle" fill="currentColor" font-size="16" font-weight="800">${p0}${esc(ch.unit)}</text>
            <text x="60" y="73" text-anchor="middle" fill="var(--obj-muted)" font-size="8.5">${esc(ch.labels[0] || "주요")}</text>
          </svg>`;
          if (w >= 260) {
            legendHtml = `<div class="uxc-chart-legend">${vals.slice(0, 4).map((v, idx) => {
              const op = idx === 0 ? 1 : Math.max(0.22, 0.72 - idx * 0.18);
              return `<div class="uxc-chart-leg-row"><i style="background:${accent};opacity:${op.toFixed(2)}"></i><span>${esc(ch.labels[idx])} (${v}${esc(ch.unit)})</span></div>`;
            }).join("")}</div>`;
          }
        } else if (variant === "line" || variant === "area") {
          const pts = vals.map((v, i) => {
            const x = vals.length === 1 ? 140 : 8 + (i / (vals.length - 1)) * 264;
            const y = 104 - (v / maxV) * 88;
            return [Math.round(x * 10) / 10, Math.round(y * 10) / 10];
          });
          const poly = pts.map(([x, y]) => `${x},${y}`).join(" ");
          const areaPath = pts.length ? `M ${pts[0][0]} 112 ` + pts.map(([x, y]) => `L ${x} ${y}`).join(" ") + ` L ${pts[pts.length - 1][0]} 112 Z` : "";
          const linePath = pts.length ? `M ${pts[0][0]} ${pts[0][1]} ` + pts.slice(1).map(([x, y]) => `L ${x} ${y}`).join(" ") : "";
          plotSvg = `<svg class="uxc-chart-svg" viewBox="0 0 280 120" preserveAspectRatio="none">
            <line x1="0" y1="25" x2="280" y2="25" stroke="var(--uxc-hairline)" stroke-dasharray="3 3"/>
            <line x1="0" y1="60" x2="280" y2="60" stroke="var(--uxc-hairline)" stroke-dasharray="3 3"/>
            <line x1="0" y1="95" x2="280" y2="95" stroke="var(--uxc-hairline)" stroke-dasharray="3 3"/>
            ${variant === "area" && areaPath ? `<path d="${areaPath}" fill="${accent}" fill-opacity="0.16"/>` : ""}
            ${linePath ? `<path d="${linePath}" fill="none" stroke="${accent}" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>` : ""}
            ${pts.map(([x, y], i) => `<circle cx="${x}" cy="${y}" r="${i === pts.length - 1 ? 4 : 3}" fill="${i === pts.length - 1 ? accent : "var(--uxc-surface)"}" stroke="${accent}" stroke-width="2"/>`).join("")}
            <polyline points="${poly}" fill="none" stroke="none"/>
          </svg>`;
        } else {
          const n = Math.max(1, vals.length);
          const step = 264 / n;
          const bw = Math.max(6, Math.min(32, step * 0.62));
          const maxIdx = vals.indexOf(maxV);
          plotSvg = `<svg class="uxc-chart-svg" viewBox="0 0 280 120" preserveAspectRatio="none">
            <line x1="0" y1="25" x2="280" y2="25" stroke="var(--uxc-hairline)" stroke-dasharray="3 3"/>
            <line x1="0" y1="62" x2="280" y2="62" stroke="var(--uxc-hairline)" stroke-dasharray="3 3"/>
            <line x1="0" y1="100" x2="280" y2="100" stroke="var(--uxc-hairline)"/>
            ${vals.map((v, idx) => {
              const x = 8 + idx * step + (step - bw) / 2;
              const bh = Math.max(4, Math.round((v / maxV) * 86));
              const y = 100 - bh;
              const op = idx === maxIdx ? 1 : (0.38 + (v / maxV) * 0.5);
              return `<rect x="${x.toFixed(1)}" y="${y}" width="${bw.toFixed(1)}" height="${bh}" rx="4" fill="${accent}" fill-opacity="${op.toFixed(2)}"/>`;
            }).join("")}
          </svg>`;
        }
        return `<div class="uxc-k-chart" style="${bgStyle}">
          <div class="uxc-chart-head">
            <span class="uxc-card-title">${esc(label)}</span>
            <span class="uxc-pill">${esc(note || `${vals.length}개 항목`)}</span>
          </div>
          <div class="uxc-chart-svg-wrap">${plotSvg}${legendHtml}</div>
        </div>`;
      }

      case "table": {
        const tbl = normalizeTableData(p.table);
        const colCount = tbl.columns.length;
        const colTmpl = tbl.columns.map((_, ci) => (ci === 0 && colCount >= 3 ? "0.75fr" : "minmax(0, 1fr)")).join(" ");
        const barH = tbl.showToolbar ? 39 : 0;
        const hdrH = tbl.showHeader ? 28 : 0;
        const rowH = variant === "compact" ? 25 : 29;
        const rawFit = Math.max(1, Math.floor((h - barH - hdrH - 4) / rowH));
        const maxRows = tbl.rows.length > rawFit
          ? Math.max(1, Math.floor((h - barH - hdrH - 26) / rowH))
          : rawFit;
        const shownRows = tbl.rows.slice(0, maxRows);
        const hiddenCount = Math.max(0, tbl.rows.length - shownRows.length);
        const POSITIVE_RE = /^(완료|활성|성공|통과|승인|정상|done|ok|active|passed|ready)$/i;

        return `<div class="uxc-k-table v-${esc(variant)}" style="${bgStyle}--tbl-cols:${colTmpl};">
          ${tbl.showToolbar ? `<div class="uxc-tbl-bar">
            <span class="uxc-card-title">${esc(label)}</span>
            <span class="uxc-pill">${esc(note || `${tbl.rows.length}행 · ${colCount}열`)}</span>
          </div>` : ""}
          <div class="uxc-tbl-grid">
            ${tbl.showHeader ? `<div class="uxc-tbl-row head">${tbl.columns.map((col, ci) =>
              `<span class="uxc-tbl-cell" data-utbl-hdr="${ci}" title="더블클릭으로 열 이름 수정">${esc(col)}</span>`
            ).join("")}</div>` : ""}
            ${shownRows.length
              ? shownRows.map((row, ri) => `<div class="uxc-tbl-row ${ri % 2 === 1 ? "alt" : ""}">
                  ${tbl.columns.map((_, ci) => {
                    const val = row[ci] ?? "";
                    if (ci === tbl.badgeCol && val.trim()) {
                      const pos = POSITIVE_RE.test(val.trim());
                      return `<span class="uxc-tbl-cell" data-utbl-cell="${ri},${ci}" title="더블클릭으로 셀 수정"><span class="uxc-pill ${pos ? "accent" : ""}" style="font-size:10px;padding:1px 6px">${esc(val)}</span></span>`;
                    }
                    const cls = ci === 0 ? "mono" : "";
                    const sty = ci === 0 ? "color:var(--obj-muted)" : (ci === 1 ? "font-weight:500" : "");
                    return `<span class="uxc-tbl-cell ${cls}" style="${sty}" data-utbl-cell="${ri},${ci}" title="더블클릭으로 셀 수정">${esc(val)}</span>`;
                  }).join("")}
                </div>`).join("")
              : `<div class="uxc-tbl-empty">행이 없습니다. 오른쪽 속성 또는 <b>▦ 데이터</b> 버튼으로 행을 추가하세요.</div>`}
            ${hiddenCount > 0 ? `<div class="uxc-tbl-more"><span>+${hiddenCount}행 더 있음 (높이를 키우거나 데이터 편집)</span><span class="mono">${tbl.rows.length} rows</span></div>` : ""}
          </div>
        </div>`;
      }

      case "button": {
        const darkFg = variant === "primary" && !isDarkHex(bg || accent);
        const icon = p.icon ? `<span>${esc(p.icon)}</span>` : "";
        return `<div class="uxc-k-button v-${esc(variant)} ${darkFg ? "btn-dark-fg" : ""}" style="${bgStyle}${p.disabled ? "opacity:0.55;" : ""}">
          <span class="uxc-btn-main">${icon}${esc(label)}</span>
          ${note && h >= 54 ? `<span class="uxc-btn-sub">${esc(note)}</span>` : ""}
        </div>`;
      }

      case "input": {
        const compact = h < 54;
        const valOrPh = p.defaultValue || p.placeholder || note || (variant === "search" ? "검색어를 입력하세요…" : variant === "select" ? "옵션 선택…" : "입력하세요…");
        const isVal = Boolean(p.defaultValue);
        const leadIcon = variant === "search"
          ? `<svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="7" cy="7" r="4.8"/><line x1="10.8" y1="10.8" x2="14" y2="14"/></svg>`
          : "";
        const trail = variant === "select"
          ? `<svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 6l4 4 4-4" stroke-linecap="round" stroke-linejoin="round"/></svg>`
          : variant === "search" ? `<span class="uxc-inp-kbd">⌘K</span>` : "";
        return `<div class="uxc-k-input v-${esc(variant)}" style="${bgStyle}">
          ${compact ? "" : `<div class="uxc-inp-label">${esc(label)}${p.required ? ' <span style="color:var(--bad)">*</span>' : ""}</div>`}
          <div class="uxc-inp-box">
            <span style="display:flex;align-items:center;gap:6px;min-width:0;overflow:hidden">${leadIcon}<span class="uxc-inp-ph" style="${isVal ? "color:var(--obj-fg)" : ""}">${esc(compact ? (p.defaultValue || p.placeholder || note || label) : valOrPh)}</span></span>
            ${trail}
          </div>
          ${!compact && p.helperText && h >= 78 ? `<div style="font-size:10.5px;color:var(--obj-muted);padding-left:2px">${esc(p.helperText)}</div>` : ""}
        </div>`;
      }

      case "text":
        return `<div class="uxc-k-text v-${esc(variant)}" style="${bgStyle}">
          <div class="uxc-txt-main">${esc(label)}</div>
          ${note ? `<div class="uxc-txt-sub">${esc(note)}</div>` : ""}
        </div>`;

      case "image": {
        if (variant === "avatar") {
          const avSize = Math.max(32, Math.min(w - 12, h - (note ? 28 : 12), 92));
          return `<div class="uxc-k-image v-avatar" style="${bgStyle}">
            <div class="uxc-avatar-ring" style="width:${avSize}px;height:${avSize}px">
              <svg width="${Math.round(avSize * 0.48)}" height="${Math.round(avSize * 0.48)}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="8" cy="5.5" r="2.7"/><path d="M3 13.5c.8-2.5 2.7-3.8 5-3.8s4.2 1.3 5 3.8" stroke-linecap="round"/></svg>
            </div>
            ${h >= 96 ? `<div style="margin-top:6px;font-size:11.5px;font-weight:600">${esc(label)}</div>` : ""}
          </div>`;
        }
        if (variant === "video") {
          return `<div class="uxc-k-image v-video" style="${bgStyle}">
            <div class="uxc-video-play"><svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor"><path d="M5.2 3.5l7.8 4.5-7.8 4.5z"/></svg></div>
            <div class="uxc-video-bar"><span>${esc(p.duration || "01:24")}</span><div class="uxc-video-track"><i></i></div><span>${esc(label)}</span></div>
          </div>`;
        }
        return `<div class="uxc-k-image v-cover" style="${bgStyle}">
          <svg width="38" height="38" viewBox="0 0 24 24" fill="none" stroke="${accent}" stroke-width="1.4" opacity="0.72">
            <rect x="3" y="4" width="18" height="16" rx="3"/><circle cx="8.5" cy="9.5" r="1.8"/><path d="M4.5 17.5l4.8-4.8 3.5 3.4 2.7-2.6 4 4" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
          ${h >= 88 ? `<div class="uxc-img-cap"><span>${esc(p.caption || label)}</span><span class="mono" style="color:var(--obj-muted);font-weight:400">${esc(p.aspect || `${w}×${h}`)}</span></div>` : ""}
        </div>`;
      }

      case "modal": {
        const cancelTxt = p.cancelLabel !== undefined ? p.cancelLabel : "취소";
        const okTxt = p.confirmLabel !== undefined ? p.confirmLabel : "확인";
        const showInp = p.showInput !== false;
        return `<div class="uxc-k-modal v-${esc(variant)}" style="${bgStyle}">
          <div class="uxc-modal-head">
            <span>${esc(label)}</span>
            <span class="uxc-modal-close">✕</span>
          </div>
          <div class="uxc-modal-body">
            ${note
              ? `<div class="uxc-card-note">${esc(note)}</div>`
              : `<div class="uxc-skel" style="width:90%"></div><div class="uxc-skel soft" style="width:72%"></div>`}
            ${h >= 200 && showInp ? `<div class="uxc-inp-box" style="flex:0 0 32px;margin-top:4px"><span class="uxc-inp-ph">${esc(p.inputPlaceholder || "입력 필드…")}</span></div>` : ""}
          </div>
          <div class="uxc-modal-foot">
            ${cancelTxt ? `<span class="uxc-mini-btn ghost">${esc(cancelTxt)}</span>` : ""}
            ${okTxt ? `<span class="uxc-mini-btn solid" style="${variant === "alert" ? "background:#dc2626" : ""}">${esc(okTxt)}</span>` : ""}
          </div>
        </div>`;
      }

      case "tabs": {
        const items = Array.isArray(p.items) && p.items.length
          ? p.items
          : (note ? note.split(/[,|]/).map(s => s.trim()).filter(Boolean) : [label || "개요", "상세", "설정", "기록"]).slice(0, 6);
        const activeIdx = clampInt(p.activeIndex, 0, Math.max(0, items.length - 1), 0);
        return `<div class="uxc-k-tabs v-${esc(variant)}" style="${bgStyle}">
          ${items.map((t, i) => `<span class="uxc-tab-item ${i === activeIdx ? "active" : ""}">${esc(t)}</span>`).join("")}
        </div>`;
      }

      case "footer": {
        const links = Array.isArray(p.links) && p.links.length ? p.links : ["문서", "개인정보", "이용약관", "문의"];
        const copy = p.copyright !== undefined ? p.copyright : "© 2026";
        return `<div class="uxc-k-footer v-${esc(variant)}" style="${bgStyle}">
          <span style="font-weight:600;color:var(--obj-fg)">${esc(label)}</span>
          ${variant !== "minimal" && w >= 420 ? `<div class="uxc-ftr-links">${links.map(l => `<span>${esc(l)}</span>`).join("")}</div>` : ""}
          <span>${esc(copy)}</span>
        </div>`;
      }

      default:
        return `<div class="uxc-k-generic" style="${bgStyle}">
          <div class="uxc-card-head">
            <span class="uxc-card-title">${esc(label)}</span>
            <span class="uxc-pill accent">${esc(rawKind)}</span>
          </div>
          ${note ? `<div class="uxc-card-note">${esc(note)}</div>` : `<div class="uxc-skel soft" style="width:70%"></div>`}
        </div>`;
    }
  }

  /* ---------------- Edge-Anchored Curved Connectors ---------------- */
  function clampVal(v, lo, hi) {
    return hi < lo ? (lo + hi) / 2 : Math.max(lo, Math.min(hi, v));
  }

  function edgeAnchors(a, b) {
    const ax1 = a.layout.x, ay1 = a.layout.y, aw = a.size.w, ah = a.size.h;
    const bx1 = b.layout.x, by1 = b.layout.y, bw = b.size.w, bh = b.size.h;
    const acx = ax1 + aw / 2, acy = ay1 + ah / 2;
    const bcx = bx1 + bw / 2, bcy = by1 + bh / 2;
    const dx = bcx - acx, dy = bcy - acy;

    let x1, y1, x2, y2, c1x, c1y, c2x, c2y;
    const horizSeparated = ax1 + aw <= bx1 + 2 || bx1 + bw <= ax1 + 2;
    const vertSeparated = ay1 + ah <= by1 + 2 || by1 + bh <= ay1 + 2;
    const useHoriz = horizSeparated && (!vertSeparated || Math.abs(dx) >= Math.abs(dy));

    if (useHoriz) {
      if (dx >= 0) { x1 = ax1 + aw; x2 = bx1; } else { x1 = ax1; x2 = bx1 + bw; }
      y1 = ah > bh * 1.6 ? clampVal(bcy, ay1 + 20, ay1 + ah - 20) : acy;
      y2 = bh > ah * 1.6 ? clampVal(y1, by1 + 20, by1 + bh - 20) : bcy;
      const off = Math.max(10, Math.min(84, Math.abs(x2 - x1) * 0.48));
      const s = dx >= 0 ? 1 : -1;
      c1x = x1 + s * off; c1y = y1; c2x = x2 - s * off; c2y = y2;
    } else {
      if (dy >= 0) { y1 = ay1 + ah; y2 = by1; } else { y1 = ay1; y2 = by1 + bh; }
      x1 = aw > bw * 1.6 ? clampVal(bcx, ax1 + 24, ax1 + aw - 24) : acx;
      x2 = bw > aw * 1.6 ? clampVal(x1, bx1 + 24, bx1 + bw - 24) : bcx;
      const off = Math.max(10, Math.min(84, Math.abs(y2 - y1) * 0.48));
      const s = dy >= 0 ? 1 : -1;
      c1x = x1; c1y = y1 + s * off; c2x = x2; c2y = y2 - s * off;
    }
    return { x1, y1, x2, y2, d: `M ${x1} ${y1} C ${c1x} ${c1y}, ${c2x} ${c2y}, ${x2} ${y2}` };
  }

  function buildArrowsMarkup() {
    const out = [];
    for (const [sel, item] of Object.entries(UXC.items)) {
      const c = item.config;
      for (const tgt of c.props?.connects_to || []) {
        const b = UXC.items[tgt];
        if (!b) continue;
        const active = isSelected(sel) || isSelected(tgt);
        const { x1, y1, d } = edgeAnchors(c, b.config);
        out.push(`<path d="${d}" class="uxc-conn-halo"/>`
          + `<path d="${d}" class="uxc-conn-line ${active ? "active" : ""}" marker-end="url(#${active ? "ux-arr-on" : "ux-arr"})"/>`
          + `<circle cx="${x1}" cy="${y1}" r="3.2" class="uxc-conn-dot ${active ? "active" : ""}"/>`);
      }
    }
    return out.join("");
  }

  function updateArrowsOnly(board) {
    const g = board?.querySelector("#uxc-arrows-g");
    if (g) g.innerHTML = buildArrowsMarkup();
  }

  /* ---------------- In-place Box DOM Sync ---------------- */
  function syncBoxDom(sel) {
    const board = $("#uxc-board");
    const it = UXC.items[sel];
    if (!board || !it) return;
    const box = board.querySelector(`[data-usel="${CSS.escape(sel)}"]`);
    if (!box) return;
    const c = it.config;
    const vk = resolveVisualKind(c.props?.kind);
    const spec = KIND_SPECS[vk];
    const accent = safeColor(c.tokens?.accent, spec?.accent || "#2f5d50");
    const bg = safeColor(c.tokens?.bg, "");
    const rad = c.tokens?.radius ?? spec?.radius ?? 10;

    box.setAttribute("style", buildBoxStyleAttr(c, accent, rad));
    box.classList.toggle("locked", !!it.locked);
    box.classList.toggle("is-hidden", c.visibility === "hidden");
    box.classList.toggle("uxc-compact-handles", c.size.w < 96 || c.size.h < 52);
    box.classList.toggle("is-custom-dark", !!bg && isDarkHex(bg));
    box.classList.toggle("is-custom-light", !!bg && !isDarkHex(bg));

    const surf = box.querySelector(".uxc-surface");
    if (surf) surf.innerHTML = renderInterior(sel, it);
    const dim = box.querySelector(".uxc-dim-pill");
    if (dim) dim.textContent = `${c.layout.x},${c.layout.y} · ${c.size.w}×${c.size.h}`;
    const kindTag = box.querySelector(".uxc-tag-kind");
    if (kindTag) kindTag.textContent = c.props?.kind || "section";
    updateArrowsOnly(board);
  }

  /* ---------------- Canvas Stage Rendering ---------------- */
  function getScale(stage) {
    const availW = Math.max(320, (stage?.clientWidth || 900) - 72);
    if (UXC.zoom === "fit") return Math.min(1, Math.max(0.35, availW / UXC.vp.width));
    const n = Number(UXC.zoom);
    return Number.isFinite(n) ? Math.min(2, Math.max(0.25, n)) : 1;
  }

  function deviceClass() {
    if (UXC.vp.width <= 460) return "uxc-frame-mobile";
    if (UXC.vp.width <= 840) return "uxc-frame-tablet";
    return "uxc-frame-desktop";
  }

  function deviceChromeTop() {
    const cls = deviceClass();
    if (cls === "uxc-frame-mobile") {
      return `<div class="uxc-device-top"><span>9:41</span><div class="uxc-notch"></div><span class="mono" style="font-size:10px">${UXC.vp.width}×${UXC.vp.height}</span></div>`;
    }
    return `<div class="uxc-device-top">
      <div class="uxc-dots"><i></i><i></i><i></i></div>
      <div class="uxc-urlbar mono">preview://${esc(UXC.view)}</div>
      <span class="mono" style="font-size:10px">${UXC.vp.width}×${UXC.vp.height}</span>
    </div>`;
  }

  function buildColumnOverlayMarkup() {
    if (!UXC.canvas.showColumns) return "";
    const cols = clampInt(UXC.canvas.columns, 1, 24, 12);
    const margin = clampInt(UXC.canvas.colMargin, 0, 160, 24);
    const gutter = clampInt(UXC.canvas.colGutter, 0, 80, 16);
    const tracks = Array.from({ length: cols }, () => '<div class="uxc-col-track"></div>').join("");
    return `<div class="uxc-col-overlay" style="padding:0 ${margin}px;gap:${gutter}px;grid-template-columns:repeat(${cols},minmax(0,1fr))">${tracks}</div>`;
  }

  function buildRulersMarkup() {
    if (!UXC.canvas.showRuler) return "";
    const step = UXC.vp.width >= 1000 ? 200 : 100;
    let rx = "", ry = "";
    for (let x = 0; x <= UXC.vp.width; x += step) {
      rx += `<span class="uxc-ruler-tick-x" style="left:${x}px">${x}</span>`;
    }
    for (let y = 0; y <= UXC.vp.height; y += step) {
      ry += `<span class="uxc-ruler-tick-y" style="top:${y}px">${y}</span>`;
    }
    return `<div class="uxc-ruler-x" style="width:${UXC.vp.width}px">${rx}</div><div class="uxc-ruler-y" style="height:${UXC.vp.height}px">${ry}</div>`;
  }

  function drawStage() {
    const stage = $("#uxc-stage");
    if (!stage) return;
    const prevScroll = { left: stage.scrollLeft, top: stage.scrollTop };
    const vp = UXC.vp;
    const scale = getScale(stage);
    const slotW = Math.round(vp.width * scale);
    const slotH = Math.round(vp.height * scale);

    const zr = $("#uxc-zoom-val");
    if (zr) zr.textContent = UXC.zoom === "fit" ? `맞춤 (${Math.round(scale * 100)}%)` : `${Math.round(scale * 100)}%`;

    const entries = Object.entries(UXC.items);
    const boxes = entries.map(([sel, item]) => {
      const c = item.config;
      const primary = UXC.sel === sel;
      const selected = isSelected(sel);
      const onCls = selected ? (primary ? "on" : "on multi-on") : "";
      const lk = item.locked ? "locked" : "";
      const hid = c.visibility === "hidden" ? "is-hidden" : "";
      const compact = (c.size.w < 96 || c.size.h < 52) ? "uxc-compact-handles" : "";
      const vk = resolveVisualKind(c.props?.kind);
      const spec = KIND_SPECS[vk];
      const accent = safeColor(c.tokens?.accent, spec?.accent || "#2f5d50");
      const bg = safeColor(c.tokens?.bg, "");
      const contrastCls = bg ? (isDarkHex(bg) ? "is-custom-dark" : "is-custom-light") : "";
      const rad = c.tokens?.radius ?? spec?.radius ?? 10;

      return `<div class="uxc-box ${onCls} ${lk} ${hid} ${compact} ${contrastCls}" data-usel="${esc(sel)}" style="${buildBoxStyleAttr(c, accent, rad)}">
        <div class="uxc-surface">${renderInterior(sel, item)}</div>
        <div class="uxc-tag">
          <span class="uxc-tag-sel mono">${esc(sel)}</span>
          <span class="uxc-tag-kind">${esc(c.props?.kind || "section")}</span>
          ${item.locked ? `<span class="uxc-tag-lock" title="사용자 잠금됨 — 에이전트 수정 차단">🔒</span>` : ""}
          <span class="uxc-tag-acts">
            ${vk === "table" ? `<button type="button" data-uact="table-edit" title="테이블 데이터 편집 (Enter)">▦ 데이터</button>` : ""}
            <button type="button" data-uact="lock" title="잠금 토글 (L)">${item.locked ? "🔒" : "🔓"}</button>
            <button type="button" data-uact="connect" title="다른 요소에 연결 (C)">↗</button>
            <button type="button" data-uact="dup" title="복제 (Ctrl+D)">⧉</button>
            <button type="button" class="del" data-uact="del" title="삭제 (Del)">✕</button>
          </span>
        </div>
        <div class="uxc-dim-pill mono">${c.layout.x},${c.layout.y} · ${c.size.w}×${c.size.h}</div>
        <div class="uxc-h uxc-h-nw" data-uresize="${esc(sel)}" data-udir="nw"></div>
        <div class="uxc-h uxc-h-n"  data-uresize="${esc(sel)}" data-udir="n"></div>
        <div class="uxc-h uxc-h-ne" data-uresize="${esc(sel)}" data-udir="ne"></div>
        <div class="uxc-h uxc-h-e"  data-uresize="${esc(sel)}" data-udir="e"></div>
        <div class="uxc-h uxc-h-se" data-uresize="${esc(sel)}" data-udir="se"></div>
        <div class="uxc-h uxc-h-s"  data-uresize="${esc(sel)}" data-udir="s"></div>
        <div class="uxc-h uxc-h-sw" data-uresize="${esc(sel)}" data-udir="sw"></div>
        <div class="uxc-h uxc-h-w"  data-uresize="${esc(sel)}" data-udir="w"></div>
      </div>`;
    }).join("");

    const emptyHint = entries.length === 0
      ? `<div class="uxc-board-empty"><b>2D UX 캔버스가 비어 있습니다</b><span>상단 팔레트에서 컴포넌트를 추가하거나 빈 곳을 더블클릭하세요. 오른쪽 속성 패널에서 대시보드·랜딩 템플릿도 바로 불러올 수 있습니다.</span></div>`
      : "";

    const boardCls = [
      "uxc-board",
      UXC.canvas.showGrid ? "" : "no-grid",
      UXC.showMeta ? "show-meta" : "",
      UXC.connectMode ? "connecting" : "",
      `density-${UXC.canvas.density || "normal"}`,
    ].filter(Boolean).join(" ");

    const themeCls = UXC.canvas.theme === "light" ? "theme-light" : (UXC.canvas.theme === "dark" ? "theme-dark" : "");
    const boardBgStyle = UXC.canvas.bg ? `--uxc-board-custom-bg:${UXC.canvas.bg};` : "";
    const gridStyle = `--uxc-grid-step:${UXC.canvas.gridSize || 8}px;`;
    const dCls = deviceClass();

    stage.innerHTML = `<div class="uxc-stage-inner">
      <div class="uxc-frame ${dCls} ${themeCls}">
        ${deviceChromeTop()}
        <div class="uxc-frame-body">
          <div class="uxc-canvas-slot" style="width:${slotW}px;height:${slotH}px">
            <div class="${boardCls}" id="uxc-board" tabindex="-1" style="width:${vp.width}px;height:${vp.height}px;transform:scale(${scale});transform-origin:0 0;${gridStyle}${boardBgStyle}">
              ${buildRulersMarkup()}
              ${buildColumnOverlayMarkup()}
              ${emptyHint}
              ${boxes}
              <svg class="uxc-arrows" width="${vp.width}" height="${vp.height}">
                <defs>
                  <marker id="ux-arr" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="6.5" markerHeight="6.5" orient="auto"><path d="M0,1 L9,5 L0,9 z" fill="var(--accent)"/></marker>
                  <marker id="ux-arr-on" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,1 L9,5 L0,9 z" fill="var(--uxc-sel)"/></marker>
                </defs>
                <g id="uxc-arrows-g">${buildArrowsMarkup()}</g>
              </svg>
              <div class="uxc-guides" id="uxc-guides"></div>
              <div class="uxc-marquee" id="uxc-marquee" hidden></div>
            </div>
          </div>
        </div>
        ${dCls === "uxc-frame-mobile" ? `<div class="uxc-device-bottom"><div class="uxc-home-pill"></div></div>` : ""}
      </div>
    </div>`;

    stage.scrollLeft = prevScroll.left;
    stage.scrollTop = prevScroll.top;
    bindStage(scale);
    syncToolbarState();
  }

  /* ---------------- On-Canvas Table Cell Double-Click Inline Editor ---------------- */
  function startInlineTableCellEdit(sel, targetEl, board) {
    const it = UXC.items[sel];
    if (!it) return;
    const isHdr = targetEl.hasAttribute("data-utbl-hdr");
    const hdrIdx = isHdr ? parseInt(targetEl.getAttribute("data-utbl-hdr"), 10) : -1;
    const cellCoord = !isHdr ? String(targetEl.getAttribute("data-utbl-cell") || "").split(",") : [];
    const rIdx = parseInt(cellCoord[0], 10);
    const cIdx = parseInt(cellCoord[1], 10);
    const tbl = normalizeTableData(it.config.props.table);

    if (isHdr && (hdrIdx < 0 || hdrIdx >= tbl.columns.length)) return;
    if (!isHdr && (rIdx < 0 || rIdx >= tbl.rows.length || cIdx < 0 || cIdx >= tbl.columns.length)) return;

    const br = board.getBoundingClientRect();
    const tr = targetEl.getBoundingClientRect();
    const scale = br.width / UXC.vp.width;

    const inp = document.createElement("input");
    inp.type = "text";
    inp.className = "uxc-inline-cell-edit";
    inp.value = isHdr ? tbl.columns[hdrIdx] : tbl.rows[rIdx][cIdx];
    inp.style.left = Math.round((tr.left - br.left) / scale - 4) + "px";
    inp.style.top = Math.round((tr.top - br.top) / scale - 3) + "px";
    inp.style.width = Math.max(88, Math.round(tr.width / scale + 8)) + "px";
    inp.style.height = Math.max(24, Math.round(tr.height / scale + 6)) + "px";

    UXC._inlineEdit = true;
    board.appendChild(inp);
    inp.focus();
    inp.select();

    let finished = false;
    const cleanup = commit => {
      if (finished) return;
      finished = true;
      UXC._inlineEdit = false;
      const val = inp.value;
      inp.remove();
      if (commit) {
        snapshotUndo();
        const curTbl = normalizeTableData(it.config.props.table);
        if (isHdr) curTbl.columns[hdrIdx] = val || `열 ${hdrIdx + 1}`;
        else curTbl.rows[rIdx][cIdx] = val;
        it.config.props.table = curTbl;
        drawStage();
        drawInspector();
      }
      $("#uxc-board")?.focus({ preventScroll: true });
    };

    inp.onkeydown = e => {
      e.stopPropagation();
      if (e.key === "Enter") { e.preventDefault(); cleanup(true); }
      else if (e.key === "Escape") { e.preventDefault(); cleanup(false); }
    };
    inp.onblur = () => cleanup(true);
    inp.onpointerdown = e => e.stopPropagation();
    inp.ondblclick = e => e.stopPropagation();
  }

  /* ---------------- Context Menu ---------------- */
  function closeContextMenu() {
    document.querySelector(".uxc-ctx-menu")?.remove();
  }

  function showContextMenu(clientX, clientY, boxSel, boardPt) {
    closeContextMenu();
    if (boxSel && !isSelected(boxSel)) {
      setSelection([boxSel], boxSel);
      drawStage();
      drawInspector();
    }
    const selList = getSelected();
    const hasSel = selList.length > 0;
    const singleSel = selList.length === 1 ? selList[0] : "";
    const singleItem = singleSel ? UXC.items[singleSel] : null;
    const isTable = singleItem && resolveVisualKind(singleItem.config.props?.kind) === "table";

    const menu = document.createElement("div");
    menu.className = "uxc-ctx-menu";
    const items = [];
    if (isTable) {
      items.push({ id: "tbl", label: "▦ 테이블 데이터 편집…", kbd: "Enter" });
      items.push({ sep: true });
    }
    if (hasSel) {
      items.push({ id: "copy", label: "복사", kbd: "Ctrl+C" });
      items.push({ id: "cut", label: "잘라내기", kbd: "Ctrl+X" });
      items.push({ id: "dup", label: "복제", kbd: "Ctrl+D" });
      if (singleSel) items.push({ id: "conn", label: "다른 요소에 연결", kbd: "C" });
      items.push({ sep: true });
      items.push({ id: "front", label: "맨 앞으로", kbd: "Ctrl+]" });
      items.push({ id: "back", label: "맨 뒤로", kbd: "Ctrl+[" });
      items.push({ id: "lock", label: singleItem ? (singleItem.locked ? "🔓 잠금 해제" : "🔒 잠금") : "🔒 잠금 토글", kbd: "L" });
      items.push({ id: "hide", label: singleItem?.config.visibility === "hidden" ? "👁 표시" : "🙈 숨기기", kbd: "H" });
      items.push({ sep: true });
      items.push({ id: "del", label: selList.length > 1 ? `선택 삭제 (${selList.length}개)` : "삭제", kbd: "Del", bad: true });
    } else {
      items.push({ id: "paste", label: "여기에 붙여넣기", kbd: "Ctrl+V", disabled: !UXC.clipboard?.length });
      items.push({ id: "add-card", label: "+ 카드 추가", kbd: "더블클릭" });
      items.push({ id: "add-table", label: "+ 테이블 추가", kbd: "" });
      items.push({ id: "sel-all", label: "전체 선택", kbd: "Ctrl+A", disabled: !Object.keys(UXC.items).length });
      items.push({ sep: true });
      items.push({ id: "shortcuts", label: "단축키 도움말…", kbd: "?" });
    }

    menu.innerHTML = items.map(it => it.sep
      ? `<div class="uxc-ctx-sep"></div>`
      : `<button type="button" class="uxc-ctx-item ${it.bad ? "bad" : ""}" data-ctx="${it.id}" ${it.disabled ? "disabled" : ""}>
          <span>${esc(it.label)}</span>${it.kbd ? `<span class="uxc-ctx-kbd">${esc(it.kbd)}</span>` : ""}
        </button>`
    ).join("");

    document.body.appendChild(menu);
    const mw = menu.offsetWidth || 220, mh = menu.offsetHeight || 260;
    menu.style.left = Math.min(window.innerWidth - mw - 8, Math.max(8, clientX)) + "px";
    menu.style.top = Math.min(window.innerHeight - mh - 8, Math.max(8, clientY)) + "px";

    menu.onclick = e => {
      const btn = e.target.closest("[data-ctx]");
      if (!btn || btn.disabled) return;
      const act = btn.dataset.ctx;
      closeContextMenu();
      if (act === "tbl" && singleSel) openTableEditorModal(singleSel);
      else if (act === "copy") copySelected();
      else if (act === "cut") { copySelected(); deleteSelected(); }
      else if (act === "dup") duplicateSelected();
      else if (act === "conn" && singleSel) { UXC.sel = singleSel; UXC.connectMode = !UXC.connectMode; drawStage(); drawInspector(); }
      else if (act === "front" && singleSel) reorderLayer(singleSel, "front");
      else if (act === "back" && singleSel) reorderLayer(singleSel, "back");
      else if (act === "lock") toggleLockSelected();
      else if (act === "hide") toggleHideSelected();
      else if (act === "del") deleteSelected();
      else if (act === "paste") pasteClipboard(boardPt);
      else if (act === "add-card") addElement("card", "Card", 296, 176, boardPt.x, boardPt.y);
      else if (act === "add-table") addElement("table", "Table", 560, 248, boardPt.x, boardPt.y);
      else if (act === "sel-all") selectAllBoxes();
      else if (act === "shortcuts") openShortcutsModal();
    };
  }

  /* ---------------- Direct Manipulation, Multi-Drag, Marquee & Magnetic Snapping ---------------- */
  function bindStage(scale) {
    const stage = $("#uxc-stage");
    const board = $("#uxc-board");
    if (!board || !stage) return;
    let drag = null;
    const gStep = Math.max(1, UXC.canvas.gridSize || 8);
    const snapGrid = (n, free) => (free || !UXC.canvas.snapGrid) ? Math.round(n) : Math.round(n / gStep) * gStep;

    function computeSnap(ignoreSet, x, y, w, h, free) {
      if (free || !UXC.canvas.snapGuides) return { x, y, gx: null, gy: null };
      const thresh = 6;
      const xLines = [0, UXC.vp.width / 2, UXC.vp.width];
      const yLines = [0, UXC.vp.height / 2, UXC.vp.height];
      if (UXC.canvas.showColumns) {
        const cols = clampInt(UXC.canvas.columns, 1, 24, 12);
        const margin = clampInt(UXC.canvas.colMargin, 0, 160, 24);
        const gutter = clampInt(UXC.canvas.colGutter, 0, 80, 16);
        const cw = (UXC.vp.width - margin * 2 - gutter * (cols - 1)) / cols;
        if (cw > 8) {
          for (let i = 0; i < cols; i++) {
            const cx1 = margin + i * (cw + gutter);
            xLines.push(Math.round(cx1), Math.round(cx1 + cw));
          }
        }
      }
      for (const [k, it] of Object.entries(UXC.items)) {
        if (ignoreSet.has(k)) continue;
        const l = it.config.layout, s = it.config.size;
        xLines.push(l.x, l.x + s.w / 2, l.x + s.w);
        yLines.push(l.y, l.y + s.h / 2, l.y + s.h);
      }

      let bestDx = thresh + 1, bestGx = null, outX = x;
      for (const line of xLines) {
        for (const anchor of [0, w / 2, w]) {
          const d = line - (x + anchor);
          if (Math.abs(d) < Math.abs(bestDx)) { bestDx = d; bestGx = line; outX = x + d; }
        }
      }
      if (Math.abs(bestDx) > thresh) { bestGx = null; outX = x; }

      let bestDy = thresh + 1, bestGy = null, outY = y;
      for (const line of yLines) {
        for (const anchor of [0, h / 2, h]) {
          const d = line - (y + anchor);
          if (Math.abs(d) < Math.abs(bestDy)) { bestDy = d; bestGy = line; outY = y + d; }
        }
      }
      if (Math.abs(bestDy) > thresh) { bestGy = null; outY = y; }

      outX = Math.max(0, Math.min(UXC.vp.width - w, Math.round(outX)));
      outY = Math.max(0, Math.min(UXC.vp.height - h, Math.round(outY)));
      return { x: outX, y: outY, gx: bestGx, gy: bestGy };
    }

    function renderGuides(gx, gy) {
      const g = $("#uxc-guides");
      if (!g) return;
      g.innerHTML = (gx !== null ? `<div class="uxc-guide-x" style="left:${gx}px"></div>` : "")
        + (gy !== null ? `<div class="uxc-guide-y" style="top:${gy}px"></div>` : "");
    }

    stage.onwheel = e => {
      if (!(e.ctrlKey || e.metaKey)) return;
      e.preventDefault();
      const cur = UXC.zoom === "fit" ? getScale(stage) : Number(UXC.zoom);
      const dir = e.deltaY < 0 ? 1 : -1;
      UXC.zoom = Math.min(2, Math.max(0.25, Math.round((cur + dir * 0.1) * 100) / 100));
      $("#uxc-zfit")?.classList.remove("on");
      drawStage();
    };

    board.oncontextmenu = e => {
      e.preventDefault();
      const bx = e.target.closest("[data-usel]");
      const r = board.getBoundingClientRect();
      const bxPt = {
        x: Math.max(0, Math.min(UXC.vp.width - 120, snapGrid((e.clientX - r.left) / scale, false))),
        y: Math.max(0, Math.min(UXC.vp.height - 80, snapGrid((e.clientY - r.top) / scale, false))),
      };
      showContextMenu(e.clientX, e.clientY, bx?.dataset.usel || "", bxPt);
    };

    board.onpointerdown = e => {
      closeContextMenu();
      if (e.target.closest(".uxc-inline-cell-edit")) return;
      if (UXC._inlineEdit) return;

      if (UXC._spaceDown || e.button === 1) {
        e.preventDefault();
        stage.classList.add("pan-active");
        board.setPointerCapture(e.pointerId);
        drag = { mode: "pan", sx: e.clientX, sy: e.clientY, sl: stage.scrollLeft, st: stage.scrollTop };
        return;
      }

      if (e.button !== 0) return;

      const actBtn = e.target.closest("[data-uact]");
      if (actBtn) {
        e.stopPropagation();
        const box = actBtn.closest("[data-usel]");
        const sel = box?.dataset.usel;
        if (!sel || !UXC.items[sel]) return;
        const act = actBtn.dataset.uact;
        if (act === "table-edit") {
          openTableEditorModal(sel);
        } else if (act === "lock") {
          snapshotUndo();
          UXC.items[sel].locked = !UXC.items[sel].locked;
          drawStage(); drawInspector();
        } else if (act === "connect") {
          setSelection([sel], sel);
          UXC.connectMode = !UXC.connectMode;
          drawStage(); drawInspector();
        } else if (act === "dup") {
          duplicateBox(sel, UXC.items[sel].config);
        } else if (act === "del") {
          deleteBox(sel);
        }
        board.focus({ preventScroll: true });
        return;
      }

      const activeEl = document.activeElement;
      if (activeEl && activeEl !== board && typeof activeEl.blur === "function") activeEl.blur();
      board.focus({ preventScroll: true });

      const rh = e.target.closest("[data-uresize]");
      if (rh) {
        e.stopPropagation();
        const sel = rh.dataset.uresize, dir = rh.dataset.udir || "se", it = UXC.items[sel];
        if (!it) return;
        const vk = resolveVisualKind(it.config.props?.kind);
        const spec = KIND_SPECS[vk] || {};
        const el = board.querySelector(`[data-usel="${CSS.escape(sel)}"]`);
        el?.classList.add("dragging");
        drag = {
          mode: "resize",
          pointerId: e.pointerId,
          captured: false,
          dir,
          sel,
          sx: e.clientX,
          sy: e.clientY,
          x: it.config.layout.x,
          y: it.config.layout.y,
          w: it.config.size.w,
          h: it.config.size.h,
          minW: spec.minW || 44,
          minH: spec.minH || 28,
          changed: false,
          before: captureState(),
          el,
        };
        return;
      }

      const bx = e.target.closest("[data-usel]");
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
        if (e.shiftKey) {
          const cur = new Set(getSelected());
          if (cur.has(sel)) cur.delete(sel); else cur.add(sel);
          setSelection([...cur], cur.has(sel) ? sel : "");
          drawStage();
          drawInspector();
          return;
        }
        if (!isSelected(sel)) {
          setSelection([sel], sel);
        } else {
          UXC.sel = sel;
        }
        const selectedList = getSelected();
        board.querySelectorAll(".uxc-box").forEach(n => {
          const ns = n.dataset.usel;
          const isOn = selectedList.includes(ns);
          n.classList.toggle("on", isOn);
          n.classList.toggle("multi-on", isOn && selectedList.length > 1 && ns !== UXC.sel);
        });
        updateArrowsOnly(board);
        drawInspector();

        const origins = {};
        for (const s of selectedList) {
          const item = UXC.items[s];
          if (!item) continue;
          origins[s] = {
            x: item.config.layout.x,
            y: item.config.layout.y,
            w: item.config.size.w,
            h: item.config.size.h,
            el: board.querySelector(`[data-usel="${CSS.escape(s)}"]`),
          };
        }
        drag = {
          mode: "move",
          pointerId: e.pointerId,
          captured: false,
          sel,
          selectedList,
          origins,
          sx: e.clientX,
          sy: e.clientY,
          changed: false,
          before: captureState(),
        };
        return;
      }

      // Empty canvas click / marquee drag
      if (UXC.connectMode) UXC.connectMode = false;
      const r = board.getBoundingClientRect();
      const bx0 = (e.clientX - r.left) / scale;
      const by0 = (e.clientY - r.top) / scale;
      drag = {
        mode: "marquee",
        pointerId: e.pointerId,
        captured: false,
        bx0,
        by0,
        sx: e.clientX,
        sy: e.clientY,
        shift: e.shiftKey,
        active: false,
        rect: { x: bx0, y: by0, w: 0, h: 0 },
      };
    };

    board.onpointermove = e => {
      if (!drag) return;
      if (drag.mode === "pan") {
        stage.scrollLeft = drag.sl - (e.clientX - drag.sx);
        stage.scrollTop = drag.st - (e.clientY - drag.sy);
        return;
      }

      if (drag.mode === "marquee") {
        if (!drag.active && Math.hypot(e.clientX - drag.sx, e.clientY - drag.sy) < 5) return;
        if (!drag.captured && drag.pointerId !== undefined) {
          drag.captured = true;
          try { board.setPointerCapture(drag.pointerId); } catch {}
        }
        drag.active = true;
        const r = board.getBoundingClientRect();
        const bx1 = (e.clientX - r.left) / scale;
        const by1 = (e.clientY - r.top) / scale;
        const rx = Math.max(0, Math.min(drag.bx0, bx1));
        const ry = Math.max(0, Math.min(drag.by0, by1));
        const rw = Math.max(0, Math.min(UXC.vp.width, Math.max(drag.bx0, bx1)) - rx);
        const rh = Math.max(0, Math.min(UXC.vp.height, Math.max(drag.by0, by1)) - ry);
        drag.rect = { x: rx, y: ry, w: rw, h: rh };
        const mq = $("#uxc-marquee");
        if (mq) {
          mq.hidden = false;
          mq.style.left = rx + "px";
          mq.style.top = ry + "px";
          mq.style.width = rw + "px";
          mq.style.height = rh + "px";
        }
        return;
      }

      const dx = (e.clientX - drag.sx) / scale;
      const dy = (e.clientY - drag.sy) / scale;
      const free = e.altKey;

      if (drag.mode === "move") {
        if (!drag.changed && Math.hypot(e.clientX - drag.sx, e.clientY - drag.sy) < 3) return;
        if (!drag.captured && drag.pointerId !== undefined) {
          drag.captured = true;
          try { board.setPointerCapture(drag.pointerId); } catch {}
        }
        const primary = drag.origins[drag.sel];
        if (!primary) return;

        let minDx = -Infinity, maxDx = Infinity, minDy = -Infinity, maxDy = Infinity;
        for (const org of Object.values(drag.origins)) {
          minDx = Math.max(minDx, -org.x);
          maxDx = Math.min(maxDx, UXC.vp.width - org.w - org.x);
          minDy = Math.max(minDy, -org.y);
          maxDy = Math.min(maxDy, UXC.vp.height - org.h - org.y);
        }

        let moveDx = snapGrid(primary.x + dx, free) - primary.x;
        let moveDy = snapGrid(primary.y + dy, free) - primary.y;
        moveDx = Math.max(minDx, Math.min(maxDx, moveDx));
        moveDy = Math.max(minDy, Math.min(maxDy, moveDy));

        if (drag.selectedList.length === 1) {
          const snapped = computeSnap(new Set([drag.sel]), primary.x + moveDx, primary.y + moveDy, primary.w, primary.h, free);
          moveDx = snapped.x - primary.x;
          moveDy = snapped.y - primary.y;
          renderGuides(snapped.gx, snapped.gy);
        } else {
          renderGuides(null, null);
        }

        for (const [s, org] of Object.entries(drag.origins)) {
          const c = UXC.items[s]?.config;
          if (!c) continue;
          const nx = Math.round(org.x + moveDx);
          const ny = Math.round(org.y + moveDy);
          if (nx !== c.layout.x || ny !== c.layout.y) drag.changed = true;
          c.layout.x = nx;
          c.layout.y = ny;
          if (org.el) {
            org.el.classList.add("dragging");
            org.el.style.left = nx + "px";
            org.el.style.top = ny + "px";
            const dim = org.el.querySelector(".uxc-dim-pill");
            if (dim) dim.textContent = `${nx},${ny} · ${c.size.w}×${c.size.h}`;
          }
        }
        updateArrowsOnly(board);
        return;
      }

      if (drag.mode === "resize") {
        const c = UXC.items[drag.sel]?.config;
        if (!c) return;
        if (!drag.changed && Math.hypot(e.clientX - drag.sx, e.clientY - drag.sy) < 2) return;
        if (!drag.captured && drag.pointerId !== undefined) {
          drag.captured = true;
          try { board.setPointerCapture(drag.pointerId); } catch {}
        }
        const dir = drag.dir;
        let nx = drag.x, ny = drag.y, nw = drag.w, nh = drag.h;
        if (dir.includes("e")) nw = Math.max(drag.minW, Math.min(UXC.vp.width - drag.x, snapGrid(drag.w + dx, free)));
        if (dir.includes("s")) nh = Math.max(drag.minH, Math.min(UXC.vp.height - drag.y, snapGrid(drag.h + dy, free)));
        if (dir.includes("w")) {
          const right = drag.x + drag.w;
          nx = Math.max(0, Math.min(right - drag.minW, snapGrid(drag.x + dx, free)));
          nw = right - nx;
        }
        if (dir.includes("n")) {
          const bottom = drag.y + drag.h;
          ny = Math.max(0, Math.min(bottom - drag.minH, snapGrid(drag.y + dy, free)));
          nh = bottom - ny;
        }
        if (nx !== c.layout.x || ny !== c.layout.y || nw !== c.size.w || nh !== c.size.h) drag.changed = true;
        c.layout.x = nx; c.layout.y = ny; c.size.w = nw; c.size.h = nh;
        drag.el.style.left = nx + "px";
        drag.el.style.top = ny + "px";
        drag.el.style.width = nw + "px";
        drag.el.style.height = nh + "px";
        drag.el.classList.toggle("uxc-compact-handles", nw < 96 || nh < 52);
        const dim = drag.el?.querySelector(".uxc-dim-pill");
        if (dim) dim.textContent = `${c.layout.x},${c.layout.y} · ${c.size.w}×${c.size.h}`;
        updateArrowsOnly(board);
      }
    };

    const finishDrag = () => {
      if (!drag) return;
      const d = drag;
      drag = null;
      if (d.captured && d.pointerId !== undefined && board.hasPointerCapture?.(d.pointerId)) {
        try { board.releasePointerCapture(d.pointerId); } catch {}
      }
      if (d.mode === "pan") {
        stage.classList.remove("pan-active");
        return;
      }
      if (d.mode === "marquee") {
        const mq = $("#uxc-marquee");
        if (mq) mq.hidden = true;
        if (d.active) {
          const { x, y, w, h } = d.rect;
          const hit = Object.entries(UXC.items)
            .filter(([, it]) => {
              const l = it.config.layout, s = it.config.size;
              return l.x < x + w && l.x + s.w > x && l.y < y + h && l.y + s.h > y;
            })
            .map(([sel]) => sel);
          if (d.shift) {
            const merged = new Set([...getSelected(), ...hit]);
            setSelection([...merged], hit[0] || UXC.sel);
          } else {
            setSelection(hit, hit[0] || "");
          }
        } else if (!d.shift) {
          setSelection([], "");
        }
        drawStage();
        drawInspector();
        return;
      }

      if (d.origins) {
        for (const org of Object.values(d.origins)) org.el?.classList.remove("dragging");
      }
      d.el?.classList.remove("dragging");
      renderGuides(null, null);
      if (d.changed) {
        UXC.undo.push(d.before);
        if (UXC.undo.length > 60) UXC.undo.shift();
        UXC.redo = [];
        UXC.dirty = true;
        syncToolbarState();
        drawStage();
        drawInspector();
      }
    };

    board.onpointerup = finishDrag;
    board.onpointercancel = finishDrag;

    board.ondblclick = e => {
      const hitEl = e.target !== board ? e.target : (document.elementFromPoint(e.clientX, e.clientY) || e.target);
      if (hitEl.closest(".uxc-inline-cell-edit")) return;
      const cellEl = hitEl.closest("[data-utbl-cell], [data-utbl-hdr]");
      const bx = hitEl.closest("[data-usel]");
      if (cellEl && bx) {
        const sel = bx.dataset.usel;
        const it = UXC.items[sel];
        if (it && resolveVisualKind(it.config.props?.kind) === "table") {
          setSelection([sel], sel);
          startInlineTableCellEdit(sel, cellEl, board);
          return;
        }
      }
      if (bx) {
        setSelection([bx.dataset.usel], bx.dataset.usel);
        drawStage();
        drawInspector();
        const inp = $("#ux-label");
        if (inp) { inp.focus(); inp.select(); }
        return;
      }
      const r = board.getBoundingClientRect();
      const x = Math.max(0, Math.min(UXC.vp.width - 296, snapGrid((e.clientX - r.left) / scale, false)));
      const y = Math.max(0, Math.min(UXC.vp.height - 176, snapGrid((e.clientY - r.top) / scale, false)));
      addElement("card", "Card", 296, 176, x, y);
    };
  }

  /* ---------------- Box & Multi-Box Manipulation Helpers ---------------- */
  function addElement(kind, label, w, h, x = 40, y = 40, extra = {}) {
    snapshotUndo();
    let i = 1;
    while (UXC.items[`#${kind}-${i}`]) i++;
    const sel = `#${kind}-${i}`;
    UXC.removed.delete(sel);
    const maxZ = Object.values(UXC.items).reduce((m, it) => Math.max(m, it.config.layout.z || 1), 0);
    const cw = Math.min(w, UXC.vp.width);
    const ch = Math.min(h, UXC.vp.height);
    const cx = Math.max(0, Math.min(UXC.vp.width - cw, x));
    const cy = Math.max(0, Math.min(UXC.vp.height - ch, y));
    UXC.items[sel] = makeBox(sel, kind, `${label} ${i}`, cx, cy, cw, ch, extra.note || "", { ...extra, z: maxZ + 1 });
    setSelection([sel], sel);
    UXC.connectMode = false;
    drawStage();
    drawInspector();
  }

  function duplicateBox(sel, c) {
    const nx = Math.min(UXC.vp.width - c.size.w, c.layout.x + 24);
    const ny = Math.min(UXC.vp.height - c.size.h, c.layout.y + 24);
    const item = UXC.items[sel];
    addElement(c.props.kind || "card", c.props.label || "Copy", c.size.w, c.size.h, nx, ny, {
      locked: item?.locked,
      note: c.props.note || "",
      variant: c.variant,
      accent: c.tokens?.accent,
      bg: c.tokens?.bg,
      radius: c.tokens?.radius,
      tokens: c.tokens,
      direction: c.layout?.direction,
      columns: c.layout?.columns,
      align: c.layout?.align,
      justify: c.layout?.justify,
      padding: c.spacing?.padding,
      gap: c.spacing?.gap,
      visibility: c.visibility,
      props: { ...deepClone(c.props), label: `${c.props.label || sel} (사본)`, connects_to: [] },
    });
  }

  function duplicateSelected() {
    const sels = getSelected();
    if (!sels.length) return;
    if (sels.length === 1) return duplicateBox(sels[0], UXC.items[sels[0]].config);
    snapshotUndo();
    const created = [];
    for (const sel of sels) {
      const it = UXC.items[sel];
      if (!it) continue;
      const c = it.config;
      const kind = c.props?.kind || "card";
      let i = 1;
      while (UXC.items[`#${kind}-${i}`]) i++;
      const nsel = `#${kind}-${i}`;
      UXC.removed.delete(nsel);
      const maxZ = Object.values(UXC.items).reduce((m, x) => Math.max(m, x.config.layout.z || 1), 0);
      const cloneCfg = deepClone(c);
      cloneCfg.layout.x = Math.min(UXC.vp.width - cloneCfg.size.w, cloneCfg.layout.x + 24);
      cloneCfg.layout.y = Math.min(UXC.vp.height - cloneCfg.size.h, cloneCfg.layout.y + 24);
      cloneCfg.layout.z = maxZ + 1;
      cloneCfg.order = maxZ + 1;
      cloneCfg.props.connects_to = [];
      UXC.items[nsel] = { locked: it.locked, config: cloneCfg };
      created.push(nsel);
    }
    setSelection(created, created[created.length - 1] || "");
    drawStage();
    drawInspector();
  }

  function deleteBox(sel) {
    if (!UXC.items[sel]) return;
    snapshotUndo();
    delete UXC.items[sel];
    UXC.removed.add(sel);
    for (const it of Object.values(UXC.items)) {
      const conns = it.config.props?.connects_to;
      if (Array.isArray(conns)) it.config.props.connects_to = conns.filter(t => t !== sel);
    }
    UXC.sels.delete(sel);
    if (UXC.sel === sel) setSelection([...UXC.sels], "");
    UXC.connectMode = false;
    drawStage();
    drawInspector();
  }

  function deleteSelected() {
    const sels = getSelected();
    if (!sels.length) return;
    snapshotUndo();
    const rmSet = new Set(sels);
    for (const sel of sels) {
      delete UXC.items[sel];
      UXC.removed.add(sel);
    }
    for (const it of Object.values(UXC.items)) {
      const conns = it.config.props?.connects_to;
      if (Array.isArray(conns)) it.config.props.connects_to = conns.filter(t => !rmSet.has(t));
    }
    setSelection([], "");
    UXC.connectMode = false;
    drawStage();
    drawInspector();
  }

  function copySelected() {
    const sels = getSelected();
    if (!sels.length) return;
    UXC.clipboard = sels.map(sel => ({
      sel,
      locked: UXC.items[sel].locked,
      config: deepClone(UXC.items[sel].config),
    }));
    toast(`${sels.length}개 요소 복사됨`);
  }

  function pasteClipboard(targetPt = null) {
    if (!UXC.clipboard?.length) return;
    snapshotUndo();
    const minX = Math.min(...UXC.clipboard.map(x => x.config.layout.x));
    const minY = Math.min(...UXC.clipboard.map(x => x.config.layout.y));
    const created = [];
    for (const entry of UXC.clipboard) {
      const c = deepClone(entry.config);
      const kind = c.props?.kind || "card";
      let i = 1;
      while (UXC.items[`#${kind}-${i}`]) i++;
      const nsel = `#${kind}-${i}`;
      UXC.removed.delete(nsel);
      const maxZ = Object.values(UXC.items).reduce((m, x) => Math.max(m, x.config.layout.z || 1), 0);
      const nx = targetPt ? (targetPt.x + (c.layout.x - minX)) : (c.layout.x + 24);
      const ny = targetPt ? (targetPt.y + (c.layout.y - minY)) : (c.layout.y + 24);
      c.layout.x = Math.max(0, Math.min(UXC.vp.width - c.size.w, Math.round(nx)));
      c.layout.y = Math.max(0, Math.min(UXC.vp.height - c.size.h, Math.round(ny)));
      c.layout.z = maxZ + 1;
      c.order = maxZ + 1;
      c.props.connects_to = [];
      UXC.items[nsel] = { locked: entry.locked, config: c };
      created.push(nsel);
    }
    setSelection(created, created[created.length - 1] || "");
    drawStage();
    drawInspector();
  }

  function selectAllBoxes() {
    const all = Object.keys(UXC.items);
    if (!all.length) return;
    setSelection(all, all[all.length - 1]);
    drawStage();
    drawInspector();
  }

  function toggleLockSelected() {
    const sels = getSelected();
    if (!sels.length) return;
    snapshotUndo();
    const next = !sels.every(s => UXC.items[s].locked);
    sels.forEach(s => { UXC.items[s].locked = next; });
    drawStage();
    drawInspector();
  }

  function toggleHideSelected() {
    const sels = getSelected();
    if (!sels.length) return;
    snapshotUndo();
    const next = sels.every(s => UXC.items[s].config.visibility === "hidden") ? "visible" : "hidden";
    sels.forEach(s => { UXC.items[s].config.visibility = next; });
    drawStage();
    drawInspector();
  }

  function reorderLayer(sel, op) {
    const list = Object.entries(UXC.items).sort((a, b) => (a[1].config.layout.z || 0) - (b[1].config.layout.z || 0));
    const idx = list.findIndex(([k]) => k === sel);
    if (idx < 0) return;
    snapshotUndo();
    const [entry] = list.splice(idx, 1);
    if (op === "back") list.unshift(entry);
    else if (op === "down") list.splice(Math.max(0, idx - 1), 0, entry);
    else if (op === "up") list.splice(Math.min(list.length, idx + 1), 0, entry);
    else if (op === "front") list.push(entry);
    list.forEach(([, it], i) => { it.config.layout.z = i + 1; it.config.order = i + 1; });
    drawStage();
    drawInspector();
  }

  function alignSelected(mode) {
    const sels = getSelected();
    if (!sels.length) return;
    snapshotUndo();
    const configs = sels.map(s => UXC.items[s].config);
    if (sels.length === 1) {
      const c = configs[0];
      if (mode === "left") c.layout.x = 0;
      else if (mode === "center-h") c.layout.x = Math.round((UXC.vp.width - c.size.w) / 2);
      else if (mode === "right") c.layout.x = Math.max(0, UXC.vp.width - c.size.w);
      else if (mode === "top") c.layout.y = 0;
      else if (mode === "center-v") c.layout.y = Math.round((UXC.vp.height - c.size.h) / 2);
      else if (mode === "bottom") c.layout.y = Math.max(0, UXC.vp.height - c.size.h);
      else if (mode === "fit-w") {
        const m = UXC.canvas.showColumns ? UXC.canvas.colMargin : 0;
        c.layout.x = m;
        c.size.w = Math.max(44, UXC.vp.width - m * 2);
      } else if (mode === "fit-h") {
        c.layout.y = 0;
        c.size.h = Math.max(28, UXC.vp.height);
      }
    } else {
      const minX = Math.min(...configs.map(c => c.layout.x));
      const maxX = Math.max(...configs.map(c => c.layout.x + c.size.w));
      const minY = Math.min(...configs.map(c => c.layout.y));
      const maxY = Math.max(...configs.map(c => c.layout.y + c.size.h));
      const midX = (minX + maxX) / 2, midY = (minY + maxY) / 2;
      const primary = UXC.items[UXC.sel]?.config || configs[0];

      if (mode === "left") configs.forEach(c => { c.layout.x = minX; });
      else if (mode === "center-h") configs.forEach(c => { c.layout.x = Math.max(0, Math.round(midX - c.size.w / 2)); });
      else if (mode === "right") configs.forEach(c => { c.layout.x = Math.max(0, maxX - c.size.w); });
      else if (mode === "top") configs.forEach(c => { c.layout.y = minY; });
      else if (mode === "center-v") configs.forEach(c => { c.layout.y = Math.max(0, Math.round(midY - c.size.h / 2)); });
      else if (mode === "bottom") configs.forEach(c => { c.layout.y = Math.max(0, maxY - c.size.h); });
      else if (mode === "match-w") configs.forEach(c => { c.size.w = Math.min(UXC.vp.width - c.layout.x, primary.size.w); });
      else if (mode === "match-h") configs.forEach(c => { c.size.h = Math.min(UXC.vp.height - c.layout.y, primary.size.h); });
      else if (mode === "dist-h" && configs.length >= 3) {
        const sorted = [...configs].sort((a, b) => a.layout.x - b.layout.x);
        const totalW = sorted.reduce((acc, c) => acc + c.size.w, 0);
        const span = sorted[sorted.length - 1].layout.x + sorted[sorted.length - 1].size.w - sorted[0].layout.x;
        const gap = Math.max(0, (span - totalW) / (sorted.length - 1));
        let cur = sorted[0].layout.x;
        sorted.forEach(c => { c.layout.x = Math.round(cur); cur += c.size.w + gap; });
      } else if (mode === "dist-v" && configs.length >= 3) {
        const sorted = [...configs].sort((a, b) => a.layout.y - b.layout.y);
        const totalH = sorted.reduce((acc, c) => acc + c.size.h, 0);
        const span = sorted[sorted.length - 1].layout.y + sorted[sorted.length - 1].size.h - sorted[0].layout.y;
        const gap = Math.max(0, (span - totalH) / (sorted.length - 1));
        let cur = sorted[0].layout.y;
        sorted.forEach(c => { c.layout.y = Math.round(cur); cur += c.size.h + gap; });
      }
    }
    drawStage();
    drawInspector();
  }

  /* ---------------- Starter Templates ---------------- */
  function applyTemplate(tplKey) {
    if (Object.keys(UXC.items).length && !confirm("현재 캔버스 요소를 템플릿으로 교체할까요? (Undo로 되돌릴 수 있습니다)")) return;
    snapshotUndo();
    for (const k of Object.keys(UXC.items)) UXC.removed.add(k);
    UXC.items = {};
    const W = UXC.vp.width, H = UXC.vp.height;
    const defs = [];
    if (tplKey === "dashboard") {
      if (W >= 700) {
        defs.push(["#header", "header", "IRADE Dashboard", 24, 20, W - 48, 60, "검색, 필터, 워크스페이스 전환", { variant: "search", z: 4 }]);
        defs.push(["#sidebar", "sidebar", "메뉴", 24, 96, 208, H - 120, "메인 내비게이션", { variant: "nav", z: 3 }]);
        const mx = 248, mw = W - mx - 24, cw = Math.floor((mw - 32) / 3);
        defs.push(["#kpi-1", "card", "활성 워크플로", mx, 96, cw, 144, "24 · +18% 이번 주", { variant: "metric", z: 5 }]);
        defs.push(["#kpi-2", "card", "통과율", mx + cw + 16, 96, cw, 144, "98.4% · 안정", { variant: "metric", accent: "#2563eb", z: 6 }]);
        defs.push(["#kpi-3", "card", "월간 비용", mx + (cw + 16) * 2, 96, cw, 144, "$14.20 · 예산 내", { variant: "metric", accent: "#4f46e5", z: 7 }]);
        defs.push(["#main-chart", "chart", "실행 추이", mx, 256, Math.floor(mw * 0.58), H - 280, "최근 7일", { variant: "area", accent: "#2563eb", z: 8 }]);
        defs.push(["#recent-tbl", "table", "최근 실행 로그", mx + Math.floor(mw * 0.58) + 16, 256, mw - Math.floor(mw * 0.58) - 16, H - 280, "실시간 동기화", { variant: "striped", z: 9 }]);
      } else {
        defs.push(["#header", "header", "Dashboard", 16, 16, W - 32, 56, "", { variant: "minimal", z: 1 }]);
        defs.push(["#kpi-1", "card", "활성 워크플로", 16, 88, W - 32, 144, "24 · +18%", { variant: "metric", z: 2 }]);
        defs.push(["#main-chart", "chart", "실행 추이", 16, 248, W - 32, 224, "최근 7일", { variant: "bar", z: 3 }]);
        defs.push(["#recent-tbl", "table", "최근 실행", 16, 488, W - 32, 220, "", { variant: "compact", z: 4 }]);
      }
    } else if (tplKey === "landing") {
      defs.push(["#header", "header", "IRADE Studio", 24, 20, W - 48, 60, "", { variant: "appbar", z: 1 }]);
      defs.push(["#hero", "hero", "명세에서 코드까지, 하나의 캔버스", 24, 96, W - 48, 232, "시각적 제약과 잠금으로 에이전트가 안전하게 UI를 구현합니다.", { variant: W >= 600 ? "split" : "centered", connects_to: ["#cta-btn"], z: 2 }]);
      if (W >= 700) {
        const cw = Math.floor((W - 80) / 3);
        defs.push(["#feat-1", "card", "명세 동기화", 24, 344, cw, 168, "SRS/ARC/SCH 스냅샷을 실시간 반영", { variant: "elevated", z: 3 }]);
        defs.push(["#feat-2", "card", "잠금 보호", 24 + cw + 16, 344, cw, 168, "사용자 잠금 요소는 Audit이 변경 차단", { variant: "elevated", accent: "#2563eb", z: 4 }]);
        defs.push(["#feat-3", "card", "실시간 검증", 24 + (cw + 16) * 2, 344, cw, 168, "테스트와 의미론적 검증 자동 실행", { variant: "elevated", accent: "#4f46e5", z: 5 }]);
      } else {
        defs.push(["#feat-1", "card", "핵심 기능", 24, 344, W - 48, 160, "SRS/ARC/SCH 기반 자동 개발", { variant: "elevated", z: 3 }]);
      }
      defs.push(["#cta-btn", "button", "무료로 시작하기", 24, H - 144, 176, 44, "", { variant: "primary", z: 6 }]);
      defs.push(["#footer", "footer", "IRADE Platform", 24, H - 76, W - 48, 52, "", { variant: "standard", z: 7 }]);
    } else if (tplKey === "form") {
      const fw = Math.min(480, W - 48), fx = Math.round((W - fw) / 2);
      defs.push(["#form-card", "section", "계정 및 워크스페이스 설정", fx, 48, fw, Math.min(540, H - 96), "입력 후 저장 시 즉시 반영", { variant: "surface", z: 1 }]);
      defs.push(["#title-txt", "text", "프로젝트 생성", fx + 24, 92, fw - 48, 52, "새 AGDD 워크스페이스 정보를 입력하세요.", { variant: "heading", z: 2 }]);
      defs.push(["#name-inp", "input", "프로젝트 이름", fx + 24, 164, fw - 48, 64, "예: irade-web-portal", { variant: "text", z: 3 }]);
      defs.push(["#role-sel", "input", "기본 템플릿", fx + 24, 244, fw - 48, 64, "대시보드 / 랜딩 선택", { variant: "select", z: 4 }]);
      defs.push(["#desc-inp", "input", "설명 메모", fx + 24, 324, fw - 48, 96, "에이전트 참고 지침을 입력하세요…", { variant: "textarea", z: 5 }]);
      defs.push(["#cancel-btn", "button", "취소", fx + 24, 444, 112, 42, "", { variant: "outline", z: 6 }]);
      defs.push(["#submit-btn", "button", "생성하기", fx + fw - 160, 444, 136, 42, "", { variant: "primary", z: 7 }]);
    }
    for (const [sel, kind, label, x, y, w, h, note, extra] of defs) {
      UXC.removed.delete(sel);
      UXC.items[sel] = makeBox(sel, kind, label, x, y, w, h, note, extra);
    }
    setSelection([], "");
    drawStage();
    drawInspector();
  }

  /* ---------------- CSV / TSV Parsing & Formatting ---------------- */
  function parseDelimitedText(text) {
    const lines = String(text || "").replace(/\r\n?/g, "\n").split("\n").filter(l => l.trim().length > 0);
    if (!lines.length) return [];
    const first = lines[0];
    const delim = (first.split("\t").length > first.split(",").length) ? "\t" : ",";
    const parseLine = line => {
      if (delim === "\t") return line.split("\t").map(s => s.trim());
      const out = [];
      let cur = "", inQ = false;
      for (let i = 0; i < line.length; i++) {
        const ch = line[i];
        if (inQ) {
          if (ch === '"' && line[i + 1] === '"') { cur += '"'; i++; }
          else if (ch === '"') inQ = false;
          else cur += ch;
        } else if (ch === '"') {
          inQ = true;
        } else if (ch === ",") {
          out.push(cur.trim()); cur = "";
        } else {
          cur += ch;
        }
      }
      out.push(cur.trim());
      return out;
    };
    return lines.map(parseLine);
  }

  function toCsvText(tbl) {
    const q = v => {
      const s = String(v ?? "");
      return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
    };
    return [tbl.columns.map(q).join(","), ...tbl.rows.map(r => r.map(q).join(","))].join("\n");
  }

  /* ---------------- Table Data Editor Modal ---------------- */
  function openTableEditorModal(sel) {
    const item = UXC.items[sel];
    if (!item) return;
    document.querySelector(".uxc-modal-backdrop")?.remove();
    UXC._modalOpen = true;

    const draft = normalizeTableData(deepClone(item.config.props.table));
    let showImport = false;

    const backdrop = document.createElement("div");
    backdrop.className = "uxc-modal-backdrop";

    const renderModal = () => {
      backdrop.innerHTML = `<div class="uxc-modal-dialog" role="dialog" aria-modal="true">
        <div class="uxc-modal-dlg-head">
          <span>▦ 테이블 데이터 편집 · <span class="mono">${esc(sel)}</span> <span class="muted" style="font-weight:400;font-size:12px">(${draft.columns.length}열 × ${draft.rows.length}행)</span></span>
          <button type="button" class="icon" id="uxm-close" title="닫기 (Esc)">✕</button>
        </div>
        <div class="uxc-modal-dlg-body">
          <div class="row" style="justify-content:space-between">
            <div class="row">
              <button type="button" class="g" id="uxm-add-col">+ 열 추가</button>
              <button type="button" class="g" id="uxm-add-row">+ 행 추가</button>
              <button type="button" class="g ${showImport ? "on" : ""}" id="uxm-tog-imp">CSV/TSV 가져오기</button>
              <button type="button" class="g" id="uxm-copy-csv">CSV 복사</button>
              <button type="button" class="g bad" id="uxm-clear-rows">모든 행 비우기</button>
            </div>
            <div class="row" style="font-size:12px">
              <label>배지 열 <select id="uxm-badge"><option value="-1">없음</option>${draft.columns.map((c, i) => `<option value="${i}" ${i === draft.badgeCol ? "selected" : ""}>${esc(c)}</option>`).join("")}</select></label>
              <label class="row" style="gap:4px"><input type="checkbox" id="uxm-tbar" ${draft.showToolbar ? "checked" : ""}><span>제목 바</span></label>
              <label class="row" style="gap:4px"><input type="checkbox" id="uxm-hdr" ${draft.showHeader ? "checked" : ""}><span>헤더 행</span></label>
            </div>
          </div>

          ${showImport ? `<div class="card" style="margin:0;padding:10px;background:var(--bg)">
            <div class="muted" style="font-size:12px;margin-bottom:6px">CSV 또는 스프레드시트에서 복사한 탭(TSV) 데이터를 붙여넣으세요.</div>
            <textarea id="uxm-csv-in" rows="4" style="min-height:86px" placeholder="ID,이름,상태,담당자&#10;#201,로그인 API,완료,Alice&#10;#202,결제 연동,진행중,Bob"></textarea>
            <div class="row" style="justify-content:space-between;margin-top:6px">
              <label class="row" style="font-size:12px;gap:5px"><input type="checkbox" id="uxm-csv-hdr" checked><span>첫 행을 열 헤더로 사용</span></label>
              <button type="button" class="b" id="uxm-csv-apply">가져오기 적용</button>
            </div>
          </div>` : ""}

          <div class="uxc-sheet-wrap">
            <table class="uxc-sheet-table">
              <thead>
                <tr>
                  <th style="width:36px;text-align:center">#</th>
                  ${draft.columns.map((col, ci) => `<th>
                    <div class="uxc-sheet-col-head">
                      <input class="uxm-col-inp" data-ci="${ci}" value="${esc(col)}" placeholder="열 ${ci + 1}">
                      <button type="button" class="icon" data-cmv="${ci},-1" ${ci === 0 ? "disabled" : ""} title="왼쪽으로">←</button>
                      <button type="button" class="icon" data-cmv="${ci},1" ${ci === draft.columns.length - 1 ? "disabled" : ""} title="오른쪽으로">→</button>
                      <button type="button" class="icon bad" data-cdel="${ci}" ${draft.columns.length <= 1 ? "disabled" : ""} title="열 삭제">✕</button>
                    </div>
                  </th>`).join("")}
                  <th style="width:94px;text-align:center">행 작업</th>
                </tr>
              </thead>
              <tbody>
                ${draft.rows.length ? draft.rows.map((row, ri) => `<tr>
                  <td class="mono muted" style="text-align:center">${ri + 1}</td>
                  ${draft.columns.map((_, ci) => `<td><input class="uxm-cell-inp" data-ri="${ri}" data-ci="${ci}" value="${esc(row[ci] ?? "")}"></td>`).join("")}
                  <td><div class="uxc-mini-row-acts" style="justify-content:center">
                    <button type="button" data-rmv="${ri},-1" ${ri === 0 ? "disabled" : ""} title="위로">↑</button>
                    <button type="button" data-rmv="${ri},1" ${ri === draft.rows.length - 1 ? "disabled" : ""} title="아래로">↓</button>
                    <button type="button" data-rdup="${ri}" title="행 복제">⧉</button>
                    <button type="button" class="del" data-rdel="${ri}" title="행 삭제">✕</button>
                  </div></td>
                </tr>`).join("") : `<tr><td colspan="${draft.columns.length + 2}" class="muted" style="text-align:center;padding:18px">행이 없습니다. <b>+ 행 추가</b>를 눌러 데이터를 입력하세요.</td></tr>`}
              </tbody>
            </table>
          </div>
        </div>
        <div class="uxc-modal-dlg-foot">
          <span class="muted" style="font-size:12px">셀에서 <b>Enter</b>: 아래 행으로 이동 (마지막 행이면 새 행 추가) · <b>Ctrl+Enter</b>: 적용</span>
          <div class="row">
            <button type="button" class="g" id="uxm-cancel">취소 (Esc)</button>
            <button type="button" class="b" id="uxm-apply">적용</button>
          </div>
        </div>
      </div>`;

      const close = commit => {
        UXC._modalOpen = false;
        window.removeEventListener("keydown", onModalKey, true);
        backdrop.remove();
        if (commit) {
          snapshotUndo();
          item.config.props.table = normalizeTableData(draft);
          drawStage();
          drawInspector();
        }
        $("#uxc-board")?.focus({ preventScroll: true });
      };

      const onModalKey = e => {
        if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(false); }
        else if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); e.stopPropagation(); close(true); }
      };
      window.removeEventListener("keydown", backdrop._onKey || (() => {}), true);
      backdrop._onKey = onModalKey;
      window.addEventListener("keydown", onModalKey, true);

      backdrop.querySelector("#uxm-close").onclick = () => close(false);
      backdrop.querySelector("#uxm-cancel").onclick = () => close(false);
      backdrop.querySelector("#uxm-apply").onclick = () => close(true);

      backdrop.querySelector("#uxm-add-col").onclick = () => {
        draft.columns.push(`열 ${draft.columns.length + 1}`);
        draft.rows.forEach(r => r.push(""));
        renderModal();
      };
      backdrop.querySelector("#uxm-add-row").onclick = () => {
        draft.rows.push(draft.columns.map(() => ""));
        renderModal();
        const last = backdrop.querySelector(`.uxm-cell-inp[data-ri="${draft.rows.length - 1}"][data-ci="0"]`);
        last?.focus();
      };
      backdrop.querySelector("#uxm-tog-imp").onclick = () => { showImport = !showImport; renderModal(); };
      backdrop.querySelector("#uxm-copy-csv").onclick = () => {
        const csv = toCsvText(draft);
        navigator.clipboard?.writeText(csv).then(() => toast("CSV가 클립보드에 복사되었습니다")).catch(() => toast("클립보드 복사 실패", true));
      };
      backdrop.querySelector("#uxm-clear-rows").onclick = () => {
        draft.rows = [];
        renderModal();
      };
      backdrop.querySelector("#uxm-badge").onchange = e => { draft.badgeCol = parseInt(e.target.value, 10); };
      backdrop.querySelector("#uxm-tbar").onchange = e => { draft.showToolbar = e.target.checked; };
      backdrop.querySelector("#uxm-hdr").onchange = e => { draft.showHeader = e.target.checked; };

      if (showImport) {
        backdrop.querySelector("#uxm-csv-apply").onclick = () => {
          const parsed = parseDelimitedText(backdrop.querySelector("#uxm-csv-in").value);
          if (!parsed.length) return toast("가져올 데이터가 없습니다", true);
          const firstHdr = backdrop.querySelector("#uxm-csv-hdr").checked;
          const maxCols = Math.max(...parsed.map(r => r.length));
          if (firstHdr) {
            draft.columns = Array.from({ length: maxCols }, (_, i) => parsed[0][i] || `열 ${i + 1}`);
            draft.rows = parsed.slice(1).map(r => Array.from({ length: maxCols }, (_, i) => r[i] ?? ""));
          } else {
            if (maxCols > draft.columns.length) {
              while (draft.columns.length < maxCols) draft.columns.push(`열 ${draft.columns.length + 1}`);
            }
            draft.rows = parsed.map(r => draft.columns.map((_, i) => r[i] ?? ""));
          }
          if (draft.badgeCol >= draft.columns.length) draft.badgeCol = -1;
          showImport = false;
          renderModal();
          toast(`${draft.rows.length}행 가져옴 (적용을 누르면 반영됩니다)`);
        };
      }

      backdrop.querySelectorAll(".uxm-col-inp").forEach(inp => {
        const ci = +inp.dataset.ci;
        inp.oninput = () => {
          draft.columns[ci] = inp.value;
          const opt = backdrop.querySelector(`#uxm-badge option[value="${ci}"]`);
          if (opt) opt.textContent = inp.value || `열 ${ci + 1}`;
        };
      });
      backdrop.querySelectorAll("[data-cmv]").forEach(b => b.onclick = () => {
        const [ci, dir] = b.dataset.cmv.split(",").map(Number);
        const ni = ci + dir;
        if (ni < 0 || ni >= draft.columns.length) return;
        [draft.columns[ci], draft.columns[ni]] = [draft.columns[ni], draft.columns[ci]];
        draft.rows.forEach(r => { [r[ci], r[ni]] = [r[ni], r[ci]]; });
        if (draft.badgeCol === ci) draft.badgeCol = ni;
        else if (draft.badgeCol === ni) draft.badgeCol = ci;
        renderModal();
      });
      backdrop.querySelectorAll("[data-cdel]").forEach(b => b.onclick = () => {
        const ci = +b.dataset.cdel;
        if (draft.columns.length <= 1) return;
        draft.columns.splice(ci, 1);
        draft.rows.forEach(r => r.splice(ci, 1));
        if (draft.badgeCol === ci) draft.badgeCol = -1;
        else if (draft.badgeCol > ci) draft.badgeCol--;
        renderModal();
      });

      backdrop.querySelectorAll(".uxm-cell-inp").forEach(inp => {
        const ri = +inp.dataset.ri, ci = +inp.dataset.ci;
        inp.oninput = () => { draft.rows[ri][ci] = inp.value; };
        inp.onkeydown = e => {
          if (e.key === "Enter" && !e.ctrlKey && !e.metaKey) {
            e.preventDefault();
            if (ri === draft.rows.length - 1) draft.rows.push(draft.columns.map(() => ""));
            renderModal();
            backdrop.querySelector(`.uxm-cell-inp[data-ri="${ri + 1}"][data-ci="${ci}"]`)?.focus();
          }
        };
      });
      backdrop.querySelectorAll("[data-rmv]").forEach(b => b.onclick = () => {
        const [ri, dir] = b.dataset.rmv.split(",").map(Number);
        const ni = ri + dir;
        if (ni < 0 || ni >= draft.rows.length) return;
        [draft.rows[ri], draft.rows[ni]] = [draft.rows[ni], draft.rows[ri]];
        renderModal();
      });
      backdrop.querySelectorAll("[data-rdup]").forEach(b => b.onclick = () => {
        const ri = +b.dataset.rdup;
        draft.rows.splice(ri + 1, 0, [...draft.rows[ri]]);
        renderModal();
      });
      backdrop.querySelectorAll("[data-rdel]").forEach(b => b.onclick = () => {
        draft.rows.splice(+b.dataset.rdel, 1);
        renderModal();
      });
    };

    backdrop.onclick = e => { if (e.target === backdrop) backdrop.querySelector("#uxm-cancel")?.click(); };
    document.body.appendChild(backdrop);
    renderModal();
  }

  /* ---------------- Shortcuts Help Modal ---------------- */
  function openShortcutsModal() {
    const existing = document.querySelector(".uxc-modal-backdrop");
    if (existing) { UXC._modalOpen = false; existing.remove(); return; }
    UXC._modalOpen = true;
    const backdrop = document.createElement("div");
    backdrop.className = "uxc-modal-backdrop";
    const row = (label, kbd) => `<div class="uxc-kbd-row"><span>${esc(label)}</span><kbd class="uxc-kbd">${esc(kbd)}</kbd></div>`;
    backdrop.innerHTML = `<div class="uxc-modal-dialog sm" role="dialog" aria-modal="true">
      <div class="uxc-modal-dlg-head">
        <span>⌨ 2D UX 캔버스 단축키</span>
        <button type="button" class="icon" id="uxs-close">✕</button>
      </div>
      <div class="uxc-modal-dlg-body">
        <div class="uxc-kbd-grid">
          <div class="uxc-kbd-group">
            <h4>편집 · 저장</h4>
            ${row("저장", "Ctrl / ⌘ + S")}
            ${row("실행 취소", "Ctrl / ⌘ + Z")}
            ${row("다시 실행", "Ctrl+Shift+Z / Ctrl+Y")}
            ${row("복사 / 잘라내기 / 붙여넣기", "Ctrl + C / X / V")}
            ${row("복제", "Ctrl / ⌘ + D")}
            ${row("전체 선택", "Ctrl / ⌘ + A")}
            ${row("선택 삭제", "Delete / Backspace")}
          </div>
          <div class="uxc-kbd-group">
            <h4>이동 · 크기 · 선택</h4>
            ${row("그리드 단위 이동", "← ↑ → ↓")}
            ${row("큰 간격 이동 (3×)", "Shift + 화살표")}
            ${row("1px 미세 이동", "Alt + 화살표")}
            ${row("크기 조절", "Ctrl + 화살표")}
            ${row("다중 선택 추가/토글", "Shift + 클릭 / 드래그")}
            ${row("요소 순서 순환", "Tab / Shift + Tab")}
            ${row("선택 해제 / 모드 종료", "Escape")}
          </div>
          <div class="uxc-kbd-group">
            <h4>레이어 · 보기 · 줌</h4>
            ${row("앞 / 뒤 레이어 이동", "] / [")}
            ${row("맨 앞 / 맨 뒤로", "Ctrl + ] / [")}
            ${row("잠금 토글 / 숨김 토글", "L / H")}
            ${row("연결 모드 토글", "C")}
            ${row("그리드 / 컬럼 가이드 토글", "G / Shift + G")}
            ${row("블루프린트 라벨 토글", "B")}
            ${row("확대 / 축소 / 맞춤 / 100%", "+ / - / 0 / 1")}
            ${row("화면 패닝 / 휠 줌", "Space+드래그 / Ctrl+휠")}
          </div>
          <div class="uxc-kbd-group">
            <h4>테이블 · 인라인 편집</h4>
            ${row("테이블 데이터 모달 열기", "테이블 선택 + Enter")}
            ${row("셀 / 열 이름 바로 수정", "캔버스 셀 더블클릭")}
            ${row("요소 레이블 수정", "F2 (또는 더블클릭)")}
            ${row("우클릭 메뉴", "오른쪽 클릭")}
            ${row("단축키 도움말 열기/닫기", "?")}
          </div>
        </div>
      </div>
      <div class="uxc-modal-dlg-foot">
        <span class="muted" style="font-size:12px">한글 입력기(IME) 상태에서도 모든 영문 단축키가 동작합니다.</span>
        <button type="button" class="b" id="uxs-ok">확인 (Esc)</button>
      </div>
    </div>`;

    const close = () => {
      UXC._modalOpen = false;
      window.removeEventListener("keydown", onKey, true);
      backdrop.remove();
      $("#uxc-board")?.focus({ preventScroll: true });
    };
    const onKey = e => {
      if (e.key === "Escape" || e.key === "?" || (e.shiftKey && e.code === "Slash")) {
        e.preventDefault(); e.stopPropagation(); close();
      }
    };
    window.addEventListener("keydown", onKey, true);
    backdrop.onclick = e => { if (e.target === backdrop) close(); };
    backdrop.querySelector("#uxs-close").onclick = close;
    backdrop.querySelector("#uxs-ok").onclick = close;
    document.body.appendChild(backdrop);
  }

  /* ---------------- Reusable Inspector List & Table Mini-Sheet Builders ---------------- */
  function buildStringListHtml(idPrefix, items, addLabel) {
    return `<div class="uxc-list-editor" data-list-id="${idPrefix}">
      ${items.map((val, idx) => `<div class="uxc-list-row">
        <input data-litem="${idPrefix}" data-li="${idx}" value="${esc(val)}">
        <button type="button" class="g" data-ldel="${idPrefix}" data-li="${idx}" ${items.length <= 1 ? "disabled" : ""} title="삭제">✕</button>
      </div>`).join("")}
      <button type="button" class="g" data-ladd="${idPrefix}" style="font-size:11.5px;padding:3px 8px">${esc(addLabel)}</button>
    </div>`;
  }

  function buildTableMiniInspectorHtml(sel, c) {
    const tbl = normalizeTableData(c.props.table);
    return `<div class="uxc-details-body" style="padding-top:2px">
      <div class="row" style="gap:4px">
        <button type="button" class="g" id="ux-tbl-add-col" style="flex:1;padding:4px 6px;font-size:11.5px">+ 열 추가</button>
        <button type="button" class="g" id="ux-tbl-add-row" style="flex:1;padding:4px 6px;font-size:11.5px">+ 행 추가</button>
        <button type="button" class="b" id="ux-tbl-modal" style="padding:4px 8px;font-size:11.5px">⤢ 크게 편집</button>
      </div>
      <div class="uxc-grid2">
        <label class="fld"><span>배지 강조 열</span><select id="ux-tbl-badge"><option value="-1">없음</option>${tbl.columns.map((col, i) => `<option value="${i}" ${i === tbl.badgeCol ? "selected" : ""}>${esc(col)}</option>`).join("")}</select></label>
        <div class="fld"><span>표시 옵션</span><div class="row" style="gap:8px;padding-top:4px;font-size:11.5px;color:var(--ink)">
          <label class="row" style="gap:3px"><input type="checkbox" id="ux-tbl-tbar" ${tbl.showToolbar ? "checked" : ""}><span>제목</span></label>
          <label class="row" style="gap:3px"><input type="checkbox" id="ux-tbl-hdr" ${tbl.showHeader ? "checked" : ""}><span>헤더</span></label>
        </div></div>
      </div>
      <div class="fld">
        <span>열 정의 (${tbl.columns.length}개)</span>
        <div class="uxc-list-editor">
          ${tbl.columns.map((col, ci) => `<div class="uxc-list-row">
            <input class="ux-tbl-col-in" data-ci="${ci}" value="${esc(col)}" placeholder="열 ${ci + 1}">
            <button type="button" class="g" data-tcmv="${ci},-1" ${ci === 0 ? "disabled" : ""} title="왼쪽">←</button>
            <button type="button" class="g" data-tcmv="${ci},1" ${ci === tbl.columns.length - 1 ? "disabled" : ""} title="오른쪽">→</button>
            <button type="button" class="g bad" data-tcdel="${ci}" ${tbl.columns.length <= 1 ? "disabled" : ""} title="열 삭제">✕</button>
          </div>`).join("")}
        </div>
      </div>
      <div class="fld">
        <span>행 데이터 (${tbl.rows.length}행 — 캔버스 셀 더블클릭도 가능)</span>
        <div class="uxc-mini-sheet-wrap">
          <table class="uxc-mini-sheet">
            <thead><tr>
              <th style="width:22px">#</th>
              ${tbl.columns.map(col => `<th>${esc(col)}</th>`).join("")}
              <th style="width:62px">작업</th>
            </tr></thead>
            <tbody>
              ${tbl.rows.length ? tbl.rows.map((row, ri) => `<tr>
                <td class="mono muted" style="text-align:center">${ri + 1}</td>
                ${tbl.columns.map((_, ci) => `<td><input class="ux-tbl-cell-in" data-ri="${ri}" data-ci="${ci}" value="${esc(row[ci] ?? "")}"></td>`).join("")}
                <td><div class="uxc-mini-row-acts">
                  <button type="button" data-trmv="${ri},-1" ${ri === 0 ? "disabled" : ""} title="위로">↑</button>
                  <button type="button" data-trmv="${ri},1" ${ri === tbl.rows.length - 1 ? "disabled" : ""} title="아래로">↓</button>
                  <button type="button" class="del" data-trdel="${ri}" title="행 삭제">✕</button>
                </div></td>
              </tr>`).join("") : `<tr><td colspan="${tbl.columns.length + 2}" class="muted" style="text-align:center;padding:10px">행이 없습니다.</td></tr>`}
            </tbody>
          </table>
        </div>
      </div>
    </div>`;
  }

  function buildComponentContentSection(sel, vk, c) {
    const p = c.props || {};
    if (vk === "table") {
      const tbl = normalizeTableData(p.table);
      return `<details class="uxc-details" data-udsec="content" open>
        <summary>테이블 데이터 (${tbl.columns.length}열 × ${tbl.rows.length}행)</summary>
        ${buildTableMiniInspectorHtml(sel, c)}
      </details>`;
    }

    let inner = "";
    if (vk === "chart") {
      const ch = normalizeChartData(p.chart);
      inner = `<label class="fld"><span>단위 (Unit)</span><input id="ux-ch-unit" value="${esc(ch.unit)}"></label>
        <div class="fld"><span>데이터 포인트 (${ch.values.length}개)</span>
          <div class="uxc-list-editor">
            ${ch.values.map((v, i) => `<div class="uxc-list-row">
              <input class="ux-ch-lbl" data-chi="${i}" value="${esc(ch.labels[i] || "")}" placeholder="라벨">
              <input class="ux-ch-val" data-chi="${i}" type="number" value="${v}" style="max-width:74px">
              <button type="button" class="g bad" data-chdel="${i}" ${ch.values.length <= 1 ? "disabled" : ""}>✕</button>
            </div>`).join("")}
            <button type="button" class="g" id="ux-ch-add" style="font-size:11.5px;padding:3px 8px">+ 포인트 추가</button>
          </div>
        </div>`;
    } else if (vk === "header") {
      const nav = Array.isArray(p.navItems) && p.navItems.length ? p.navItems : ["개요", "분석", "문서", "설정"];
      inner = `<div class="uxc-grid2">
          <label class="fld"><span>CTA 버튼</span><input id="ux-p-cta" value="${esc(p.ctaLabel ?? "시작하기")}"></label>
          <label class="fld"><span>활성 메뉴(0~)</span><input id="ux-p-act" type="number" min="0" value="${clampInt(p.activeIndex, 0, nav.length - 1, 0)}"></label>
        </div>
        <label class="fld"><span>검색 플레이스홀더</span><input id="ux-p-sph" value="${esc(p.searchPlaceholder ?? "")}" placeholder="검색…"></label>
        <div class="fld"><span>메뉴 항목</span>${buildStringListHtml("hnav", nav, "+ 메뉴 추가")}</div>`;
    } else if (vk === "sidebar") {
      const nav = Array.isArray(p.navItems) && p.navItems.length ? p.navItems : ["대시보드", "프로젝트", "분석", "팀원", "설정"];
      inner = `<div class="uxc-grid2">
          <label class="fld"><span>하단 라벨</span><input id="ux-p-foot" value="${esc(p.footerLabel ?? "Workspace")}"></label>
          <label class="fld"><span>활성 메뉴(0~)</span><input id="ux-p-act" type="number" min="0" value="${clampInt(p.activeIndex, 0, nav.length - 1, 0)}"></label>
        </div>
        <div class="fld"><span>내비게이션 항목</span>${buildStringListHtml("snav", nav, "+ 메뉴 추가")}</div>`;
    } else if (vk === "tabs") {
      const tabs = Array.isArray(p.items) && p.items.length ? p.items : (p.note ? p.note.split(/[,|]/).map(s => s.trim()).filter(Boolean) : ["개요", "상세", "설정", "기록"]);
      inner = `<label class="fld"><span>활성 탭 인덱스 (0부터)</span><input id="ux-p-act" type="number" min="0" value="${clampInt(p.activeIndex, 0, Math.max(0, tabs.length - 1), 0)}"></label>
        <div class="fld"><span>탭 목록</span>${buildStringListHtml("tabs", tabs, "+ 탭 추가")}</div>`;
    } else if (vk === "card") {
      inner = `<div class="uxc-grid2">
          <label class="fld"><span>배지</span><input id="ux-p-badge" value="${esc(p.badge ?? "활성")}"></label>
          <label class="fld"><span>하단 CTA</span><input id="ux-p-cta" value="${esc(p.ctaLabel ?? "자세히 →")}"></label>
        </div>
        ${c.variant === "metric" ? `<div class="uxc-grid2">
          <label class="fld"><span>KPI 값</span><input id="ux-p-mval" value="${esc(p.metricValue ?? "")}" placeholder="12,840"></label>
          <label class="fld"><span>추이 배지</span><input id="ux-p-mtrend" value="${esc(p.metricTrend ?? "↗ +14.2%")}"></label>
        </div>
        <label class="row" style="font-size:12px"><input type="checkbox" id="ux-p-spark" ${p.showSpark !== false ? "checked" : ""}><span>스파크라인 차트 표시</span></label>` : ""}`;
    } else if (vk === "hero") {
      inner = `<label class="fld"><span>상단 배지</span><input id="ux-p-badge" value="${esc(p.badge ?? "✦ Featured")}"></label>
        <div class="uxc-grid2">
          <label class="fld"><span>주 CTA 버튼</span><input id="ux-p-cta1" value="${esc(p.primaryCta ?? "지금 시작하기 →")}"></label>
          <label class="fld"><span>보조 버튼</span><input id="ux-p-cta2" value="${esc(p.secondaryCta ?? "자세히 보기")}"></label>
        </div>`;
    } else if (vk === "input") {
      inner = `<div class="uxc-grid2">
          <label class="fld"><span>플레이스홀더</span><input id="ux-p-ph" value="${esc(p.placeholder ?? "")}" placeholder="입력하세요…"></label>
          <label class="fld"><span>기본값</span><input id="ux-p-def" value="${esc(p.defaultValue ?? "")}"></label>
        </div>
        <label class="fld"><span>도움말 텍스트</span><input id="ux-p-help" value="${esc(p.helperText ?? "")}"></label>
        <label class="row" style="font-size:12px"><input type="checkbox" id="ux-p-req" ${p.required ? "checked" : ""}><span>필수 입력 (*)</span></label>`;
    } else if (vk === "button") {
      inner = `<div class="uxc-grid2">
          <label class="fld"><span>아이콘/접두사</span><input id="ux-p-icon" value="${esc(p.icon ?? "")}" placeholder="예: + , ↗"></label>
          <label class="row" style="font-size:12px;padding-top:16px"><input type="checkbox" id="ux-p-dis" ${p.disabled ? "checked" : ""}><span>비활성 상태</span></label>
        </div>`;
    } else if (vk === "modal") {
      inner = `<div class="uxc-grid2">
          <label class="fld"><span>취소 버튼</span><input id="ux-p-mcan" value="${esc(p.cancelLabel ?? "취소")}"></label>
          <label class="fld"><span>확인 버튼</span><input id="ux-p-mok" value="${esc(p.confirmLabel ?? "확인")}"></label>
        </div>
        <label class="row" style="font-size:12px"><input type="checkbox" id="ux-p-minp" ${p.showInput !== false ? "checked" : ""}><span>본문 입력창 표시</span></label>`;
    } else if (vk === "footer") {
      const links = Array.isArray(p.links) && p.links.length ? p.links : ["문서", "개인정보", "이용약관", "문의"];
      inner = `<label class="fld"><span>저작권 문구</span><input id="ux-p-copy" value="${esc(p.copyright ?? "© 2026")}"></label>
        <div class="fld"><span>푸터 링크</span>${buildStringListHtml("flinks", links, "+ 링크 추가")}</div>`;
    } else if (vk === "image") {
      inner = `<div class="uxc-grid2">
          <label class="fld"><span>캡션</span><input id="ux-p-cap" value="${esc(p.caption ?? "")}"></label>
          <label class="fld"><span>비율/길이</span><input id="ux-p-asp" value="${esc(c.variant === "video" ? (p.duration ?? "01:24") : (p.aspect ?? ""))}"></label>
        </div>`;
    }
    if (!inner) return "";
    return `<details class="uxc-details" data-udsec="content" ${UXC.openSections.content ? "open" : ""}>
      <summary>컴포넌트 콘텐츠</summary>
      <div class="uxc-details-body">${inner}</div>
    </details>`;
  }

  /* ---------------- Inspector ---------------- */
  function drawInspector() {
    const el = $("#uxc-insp");
    if (!el) return;
    const sels = getSelected();

    /* -------- 1. Canvas Global Settings Inspector -------- */
    if (!sels.length) {
      const entries = Object.entries(UXC.items).sort((a, b) => (b[1].config.layout.z || 0) - (a[1].config.layout.z || 0));
      const q = (UXC.layerFilter || "").trim().toLowerCase();
      const shownEntries = q
        ? entries.filter(([sel, it]) => sel.toLowerCase().includes(q) || (it.config.props?.label || "").toLowerCase().includes(q))
        : entries;

      const layersHtml = entries.length
        ? `<div class="uxc-layer-list">${shownEntries.map(([sel, it]) => {
            const c = it.config, vk = resolveVisualKind(c.props?.kind);
            return `<div class="uxc-layer-row" data-ulayer="${esc(sel)}">
              <span style="display:inline-flex;width:14px;height:14px;color:${safeColor(c.tokens?.accent, "#2f5d50")}">${ICONS[vk] || ICONS.card}</span>
              <span class="uxc-layer-name"><b>${esc(c.props?.label || sel)}</b> <span class="uxc-layer-sub mono">${esc(sel)}</span></span>
              <button type="button" class="icon" data-ulayer-vis="${esc(sel)}" title="표시/숨김">${c.visibility === "hidden" ? "🙈" : "👁"}</button>
              <button type="button" class="icon" data-ulayer-lk="${esc(sel)}" title="잠금 토글">${it.locked ? "🔒" : "🔓"}</button>
            </div>`;
          }).join("")}</div>`
        : `<div class="muted" style="font-size:12px">배치된 요소가 없습니다.</div>`;

      const vpKey = `${UXC.vp.width}x${UXC.vp.height}`;
      const cv = UXC.canvas;
      const defAcc = safeColor(cv.defaultAccent, "#2f5d50");

      el.innerHTML = `<div class="ide-head"><span>캔버스 설정</span><button type="button" class="g" id="uxc-kbd-open" style="padding:2px 7px;font-size:11px">? 단축키</button></div>
      <div class="g-form">
        <div class="uxc-grid2">
          <label class="fld"><span>해상도 프리셋</span><select id="uxc-c-vpp">${VP_PRESETS.map(([k, t]) => `<option value="${k}" ${k === vpKey ? "selected" : ""}>${esc(t)}</option>`).join("")}<option value="custom" ${VP_PRESETS.some(([k]) => k === vpKey) ? "" : "selected"}>사용자 지정</option></select></label>
          <div class="fld"><span>방향</span><button type="button" class="g" id="uxc-c-flip" style="padding:5px 6px;font-size:12px">↔ 가로/세로 전환</button></div>
        </div>
        <div class="uxc-grid2">
          <label class="fld"><span>너비 W (px)</span><input id="uxc-c-vpw" type="number" min="320" max="3840" value="${UXC.vp.width}"></label>
          <label class="fld"><span>높이 H (px)</span><input id="uxc-c-vph" type="number" min="320" max="2160" value="${UXC.vp.height}"></label>
        </div>

        <details class="uxc-details" open>
          <summary>그리드 · 스냅 · 컬럼 가이드</summary>
          <div class="uxc-details-body">
            <div class="uxc-grid2">
              <label class="fld"><span>그리드 간격</span><select id="uxc-c-gsize">${[4, 8, 12, 16, 24].map(n => `<option value="${n}" ${cv.gridSize === n ? "selected" : ""}>${n} px</option>`).join("")}</select></label>
              <label class="fld"><span>밀도</span><select id="uxc-c-dens"><option value="compact" ${cv.density === "compact" ? "selected" : ""}>조밀</option><option value="normal" ${cv.density === "normal" ? "selected" : ""}>보통</option><option value="comfortable" ${cv.density === "comfortable" ? "selected" : ""}>여유</option></select></label>
            </div>
            <div class="uxc-grid2" style="font-size:12px;color:var(--ink)">
              <label class="row" style="gap:5px"><input type="checkbox" id="uxc-c-sgrid" ${cv.showGrid ? "checked" : ""}><span>그리드 표시</span></label>
              <label class="row" style="gap:5px"><input type="checkbox" id="uxc-c-snapg" ${cv.snapGrid ? "checked" : ""}><span>그리드 스냅</span></label>
              <label class="row" style="gap:5px"><input type="checkbox" id="uxc-c-snapl" ${cv.snapGuides ? "checked" : ""}><span>정렬 가이드</span></label>
              <label class="row" style="gap:5px"><input type="checkbox" id="uxc-c-ruler" ${cv.showRuler ? "checked" : ""}><span>좌표 눈금자</span></label>
            </div>
            <label class="row" style="font-size:12px;gap:5px"><input type="checkbox" id="uxc-c-scol" ${cv.showColumns ? "checked" : ""}><span>레이아웃 컬럼 가이드 표시</span></label>
            <div class="uxc-grid3">
              <label class="fld"><span>컬럼 수</span><select id="uxc-c-cols">${[4, 6, 8, 12, 16].map(n => `<option value="${n}" ${cv.columns === n ? "selected" : ""}>${n}열</option>`).join("")}</select></label>
              <label class="fld"><span>여백</span><input id="uxc-c-cmar" type="number" min="0" max="160" value="${cv.colMargin}"></label>
              <label class="fld"><span>간격</span><input id="uxc-c-cgut" type="number" min="0" max="80" value="${cv.colGutter}"></label>
            </div>
          </div>
        </details>

        <details class="uxc-details" open>
          <summary>캔버스 테마 · 기본 토큰</summary>
          <div class="uxc-details-body">
            <div class="uxc-grid2">
              <div class="fld">
                <span>프리뷰 테마</span>
                <div class="uxc-seg-sm" id="uxc-c-theme">
                  <button type="button" data-cth="auto" class="${cv.theme === "auto" ? "on" : ""}">자동</button>
                  <button type="button" data-cth="light" class="${cv.theme === "light" ? "on" : ""}">라이트</button>
                  <button type="button" data-cth="dark" class="${cv.theme === "dark" ? "on" : ""}">다크</button>
                </div>
              </div>
              <div class="fld">
                <span>보드 배경</span>
                <div class="row" style="gap:4px">
                  <input id="uxc-c-bg" type="color" class="uxc-color-pick" value="${esc((cv.bg || "#ffffff").slice(0, 7))}">
                  <button type="button" class="g" id="uxc-c-bg-auto" style="padding:2px 6px;font-size:11px" ${cv.bg ? "" : "disabled"}>자동</button>
                </div>
              </div>
            </div>
            <div class="fld">
              <span>기본 강조색</span>
              <div class="uxc-swatches">
                ${ACCENT_SWATCHES.map(hex => `<button type="button" class="uxc-swatch ${defAcc.toLowerCase() === hex ? "on" : ""}" data-cswatch="${hex}" style="background:${hex}" title="${hex}"></button>`).join("")}
                <input id="uxc-c-acc" type="color" class="uxc-color-pick" value="${esc(defAcc.slice(0, 7))}">
              </div>
            </div>
            <label class="fld">
              <span>기본 모서리 둥글기 (${cv.defaultRadius}px)</span>
              <input id="uxc-c-rad" type="range" min="0" max="28" value="${cv.defaultRadius}">
            </label>
            <button type="button" class="g" id="uxc-c-apply-all" style="font-size:11.5px;padding:4px 8px" ${entries.length ? "" : "disabled"}>모든 요소에 기본 강조색·둥글기 적용</button>
          </div>
        </details>

        <div class="uxc-sect-title">스타터 템플릿</div>
        <div class="uxc-tpl-grid">
          <button type="button" class="uxc-tpl-btn" data-utpl="dashboard">${ICONS.chart}<span>대시보드</span></button>
          <button type="button" class="uxc-tpl-btn" data-utpl="landing">${ICONS.hero}<span>랜딩</span></button>
          <button type="button" class="uxc-tpl-btn" data-utpl="form">${ICONS.modal}<span>폼 모달</span></button>
        </div>
        <div class="row" style="justify-content:space-between;margin-top:4px">
          <span class="uxc-sect-title" style="border:0;margin:0;padding:0">레이어 (${entries.length})</span>
          ${entries.length ? `<button type="button" class="icon bad" id="uxc-c-clear" title="모든 요소 삭제" style="font-size:11px">모두 비우기</button>` : ""}
        </div>
        ${entries.length > 3 ? `<input id="uxc-layer-q" placeholder="레이어 검색…" value="${esc(UXC.layerFilter)}" style="font-size:12px;padding:4px 7px">` : ""}
        ${layersHtml}
        <div class="uxc-sect-title">에이전트 전달</div>
        <label class="fld"><span>에이전트에 전달할 메모</span><textarea id="uxc-note" rows="3" style="min-height:60px" placeholder="예: 상단 헤더 아래 좌측 사이드바와 2열 대시보드 카드로 구성하세요.">${esc(UXC.agentNote || "")}</textarea></label>
        <button class="b" id="uxc-send-agent">${UXC.decisionId ? "2D 구성을 에이전트에 전달 & 재개" : "이 2D 구성으로 에이전트 실행 →"}</button>
      </div>`;

      $("#uxc-kbd-open").onclick = () => openShortcutsModal();
      $("#uxc-c-vpp").onchange = e => {
        if (e.target.value === "custom") return;
        snapshotUndo();
        const [w, h] = e.target.value.split("x").map(Number);
        UXC.vp = { width: w, height: h };
        drawStage(); drawInspector();
      };
      $("#uxc-c-flip").onclick = () => {
        snapshotUndo();
        UXC.vp = { width: Math.max(320, UXC.vp.height), height: Math.max(320, UXC.vp.width) };
        drawStage(); drawInspector();
      };
      $("#uxc-c-vpw").onchange = e => { snapshotUndo(); UXC.vp.width = clampInt(e.target.value, 320, 3840, UXC.vp.width); drawStage(); drawInspector(); };
      $("#uxc-c-vph").onchange = e => { snapshotUndo(); UXC.vp.height = clampInt(e.target.value, 320, 2160, UXC.vp.height); drawStage(); drawInspector(); };
      $("#uxc-c-gsize").onchange = e => { snapshotUndo(); cv.gridSize = +e.target.value; drawStage(); };
      $("#uxc-c-dens").onchange = e => { snapshotUndo(); cv.density = e.target.value; drawStage(); };
      $("#uxc-c-sgrid").onchange = e => { snapshotUndo(); cv.showGrid = e.target.checked; drawStage(); };
      $("#uxc-c-snapg").onchange = e => { snapshotUndo(); cv.snapGrid = e.target.checked; };
      $("#uxc-c-snapl").onchange = e => { snapshotUndo(); cv.snapGuides = e.target.checked; };
      $("#uxc-c-ruler").onchange = e => { snapshotUndo(); cv.showRuler = e.target.checked; drawStage(); };
      $("#uxc-c-scol").onchange = e => { snapshotUndo(); cv.showColumns = e.target.checked; drawStage(); };
      $("#uxc-c-cols").onchange = e => { snapshotUndo(); cv.columns = +e.target.value; drawStage(); };
      $("#uxc-c-cmar").onchange = e => { snapshotUndo(); cv.colMargin = clampInt(e.target.value, 0, 160, 24); drawStage(); };
      $("#uxc-c-cgut").onchange = e => { snapshotUndo(); cv.colGutter = clampInt(e.target.value, 0, 80, 16); drawStage(); };
      el.querySelectorAll("[data-cth]").forEach(b => b.onclick = () => { snapshotUndo(); cv.theme = b.dataset.cth; drawStage(); drawInspector(); });
      $("#uxc-c-bg").oninput = e => { snapshotUndoOnce("cbg"); cv.bg = e.target.value; drawStage(); };
      $("#uxc-c-bg").onchange = () => { UXC._undoKey = null; drawInspector(); };
      $("#uxc-c-bg-auto").onclick = () => { snapshotUndo(); cv.bg = ""; drawStage(); drawInspector(); };
      el.querySelectorAll("[data-cswatch]").forEach(b => b.onclick = () => { snapshotUndo(); cv.defaultAccent = b.dataset.cswatch; drawInspector(); });
      $("#uxc-c-acc").oninput = e => { snapshotUndoOnce("cacc"); cv.defaultAccent = e.target.value; };
      $("#uxc-c-acc").onchange = () => { UXC._undoKey = null; drawInspector(); };
      $("#uxc-c-rad").oninput = e => { snapshotUndoOnce("crad"); cv.defaultRadius = parseInt(e.target.value, 10) || 0; };
      $("#uxc-c-rad").onchange = () => { UXC._undoKey = null; drawInspector(); };
      $("#uxc-c-apply-all").onclick = () => {
        snapshotUndo();
        for (const it of Object.values(UXC.items)) {
          it.config.tokens.accent = cv.defaultAccent;
          it.config.tokens.radius = cv.defaultRadius;
        }
        drawStage();
        toast("모든 요소에 기본 토큰을 적용했습니다");
      };
      if ($("#uxc-c-clear")) {
        $("#uxc-c-clear").onclick = () => {
          if (!confirm("모든 캔버스 요소를 삭제할까요?")) return;
          selectAllBoxes();
          deleteSelected();
        };
      }
      if ($("#uxc-layer-q")) {
        $("#uxc-layer-q").oninput = e => { UXC.layerFilter = e.target.value; drawInspector(); $("#uxc-layer-q")?.focus(); };
      }
      el.querySelectorAll("[data-utpl]").forEach(b => b.onclick = () => applyTemplate(b.dataset.utpl));
      el.querySelectorAll("[data-ulayer]").forEach(row => row.onclick = e => {
        if (e.target.closest("[data-ulayer-vis]") || e.target.closest("[data-ulayer-lk]")) return;
        const s = row.dataset.ulayer;
        if (e.shiftKey) {
          const cur = new Set(getSelected());
          cur.add(s);
          setSelection([...cur], s);
        } else {
          setSelection([s], s);
        }
        drawStage(); drawInspector();
      });
      el.querySelectorAll("[data-ulayer-vis]").forEach(b => b.onclick = e => {
        e.stopPropagation();
        snapshotUndo();
        const c = UXC.items[b.dataset.ulayer].config;
        c.visibility = c.visibility === "hidden" ? "visible" : "hidden";
        drawStage(); drawInspector();
      });
      el.querySelectorAll("[data-ulayer-lk]").forEach(b => b.onclick = e => {
        e.stopPropagation();
        snapshotUndo();
        const it = UXC.items[b.dataset.ulayer];
        it.locked = !it.locked;
        drawStage(); drawInspector();
      });
      $("#uxc-note").oninput = e => { UXC.agentNote = e.target.value; };
      $("#uxc-send-agent").onclick = run(() => submitToAgent(!UXC.decisionId));
      return;
    }

    /* -------- 2. Multi-Selection Inspector -------- */
    if (sels.length > 1) {
      el.innerHTML = `<div class="ide-head"><span>${sels.length}개 요소 선택됨</span><button type="button" class="g" id="uxc-m-clear" style="padding:2px 8px;font-size:11px">선택 해제</button></div>
      <div class="g-form">
        <div class="uxc-multi-chips">${sels.map(s => `<span class="uxc-multi-chip mono">${esc(s)}<button type="button" data-umrm="${esc(s)}" title="선택에서 제외">✕</button></span>`).join("")}</div>
        <div class="uxc-sect-title">정렬 · 배분</div>
        <div class="uxc-align-bar">
          <button type="button" data-ualign="left" title="왼쪽 정렬">◧ 좌</button>
          <button type="button" data-ualign="center-h" title="가로 중앙 정렬">◫ 중</button>
          <button type="button" data-ualign="right" title="오른쪽 정렬">◨ 우</button>
          <button type="button" data-ualign="top" title="위쪽 정렬">⬒ 상</button>
          <button type="button" data-ualign="center-v" title="세로 중앙 정렬">⊟ 중</button>
          <button type="button" data-ualign="bottom" title="아래쪽 정렬">⬓ 하</button>
          <button type="button" data-ualign="match-w" title="기준 요소 너비 맞추기">↔W</button>
        </div>
        <div class="row">
          <button type="button" class="g" data-ualign="dist-h" style="flex:1;font-size:11.5px;padding:4px 6px" ${sels.length < 3 ? "disabled" : ""}>가로 균등 배분</button>
          <button type="button" class="g" data-ualign="dist-v" style="flex:1;font-size:11.5px;padding:4px 6px" ${sels.length < 3 ? "disabled" : ""}>세로 균등 배분</button>
          <button type="button" class="g" data-ualign="match-h" style="font-size:11.5px;padding:4px 8px">↕H 맞춤</button>
        </div>
        <div class="uxc-sect-title">일괄 스타일 · 상태</div>
        <div class="uxc-swatches">
          ${ACCENT_SWATCHES.map(hex => `<button type="button" class="uxc-swatch" data-mswatch="${hex}" style="background:${hex}" title="일괄 강조색 ${hex}"></button>`).join("")}
        </div>
        <label class="fld"><span>일괄 모서리 둥글기</span><input id="ux-m-rad" type="range" min="0" max="28" value="10"></label>
        <div class="row">
          <button type="button" class="g" id="ux-m-lock" style="flex:1">🔒 잠금 토글 (L)</button>
          <button type="button" class="g" id="ux-m-hide" style="flex:1">👁 숨김 토글 (H)</button>
        </div>
        <div class="row">
          <button type="button" class="g" id="ux-m-dup" style="flex:1">일괄 복제 (Ctrl+D)</button>
          <button type="button" class="g bad" id="ux-m-del" style="flex:1">일괄 삭제 (Del)</button>
        </div>
      </div>`;

      $("#uxc-m-clear").onclick = () => { setSelection([], ""); drawStage(); drawInspector(); };
      el.querySelectorAll("[data-umrm]").forEach(b => b.onclick = () => {
        const next = sels.filter(x => x !== b.dataset.umrm);
        setSelection(next, next[0] || "");
        drawStage(); drawInspector();
      });
      el.querySelectorAll("[data-ualign]").forEach(b => b.onclick = () => alignSelected(b.dataset.ualign));
      el.querySelectorAll("[data-mswatch]").forEach(b => b.onclick = () => {
        snapshotUndo();
        sels.forEach(s => { UXC.items[s].config.tokens.accent = b.dataset.mswatch; });
        drawStage();
      });
      $("#ux-m-rad").oninput = e => {
        snapshotUndoOnce("mrad");
        const v = parseInt(e.target.value, 10) || 0;
        sels.forEach(s => { UXC.items[s].config.tokens.radius = v; syncBoxDom(s); });
      };
      $("#ux-m-rad").onchange = () => { UXC._undoKey = null; };
      $("#ux-m-lock").onclick = toggleLockSelected;
      $("#ux-m-hide").onclick = toggleHideSelected;
      $("#ux-m-dup").onclick = duplicateSelected;
      $("#ux-m-del").onclick = deleteSelected;
      return;
    }

    /* -------- 3. Single Element Detailed Inspector -------- */
    const sel = sels[0], it = UXC.items[sel], c = it.config;
    const rawKind = c.props.kind || "section";
    const vk = resolveVisualKind(rawKind);
    const kindChoices = Object.keys(KIND_SPECS);
    if (!kindChoices.includes(rawKind)) kindChoices.unshift(rawKind);
    const variants = KIND_SPECS[vk]?.variants || [["default", "기본"]];
    const accent = safeColor(c.tokens?.accent, KIND_SPECS[vk]?.accent || "#2f5d50");
    const customBg = safeColor(c.tokens?.bg, "");
    const customFg = safeColor(c.tokens?.textColor, "");
    const customBc = safeColor(c.tokens?.borderColor, "");
    const conns = c.props.connects_to || [];
    const tk = c.tokens || {};
    const opPct = Number.isFinite(+tk.opacity) ? +tk.opacity : 100;

    el.innerHTML = `<div class="ide-head">
        <span class="uxc-insp-title">${ICONS[vk] || ""} 요소 속성</span>
        <div class="row" style="gap:4px">
          <button type="button" class="g ${UXC.connectMode ? "on" : ""}" id="uxc-conn" style="padding:2px 8px;font-size:11px;white-space:nowrap">${UXC.connectMode ? "대상 클릭…" : "↗ 연결"}</button>
        </div>
      </div>
      <div class="g-form">
        <label class="fld"><span>셀렉터 (ID)</span><input id="ux-sel" class="mono" value="${esc(sel)}"></label>
        <div class="uxc-grid2">
          <label class="fld"><span>종류</span><select id="ux-kind">${kindChoices.map(k => `<option value="${esc(k)}" ${k === rawKind ? "selected" : ""}>${esc(KIND_SPECS[k]?.label || k)}</option>`).join("")}</select></label>
          <label class="fld"><span>스타일 변형</span><select id="ux-variant">${variants.map(([v, t]) => `<option value="${esc(v)}" ${v === c.variant ? "selected" : ""}>${esc(t)}</option>`).join("")}</select></label>
        </div>
        <label class="fld"><span>레이블 (제목)</span><input id="ux-label" value="${esc(c.props.label || "")}"></label>

        ${buildComponentContentSection(sel, vk, c)}

        <details class="uxc-details" data-udsec="layout" ${UXC.openSections.layout ? "open" : ""}>
          <summary>위치 · 크기 · 정렬 · 레이어</summary>
          <div class="uxc-details-body">
            <div class="uxc-grid4">
              <label class="fld"><span>X</span><input id="ux-x" type="number" value="${c.layout.x}"></label>
              <label class="fld"><span>Y</span><input id="ux-y" type="number" value="${c.layout.y}"></label>
              <label class="fld"><span>W</span><input id="ux-w" type="number" value="${c.size.w}"></label>
              <label class="fld"><span>H</span><input id="ux-h" type="number" value="${c.size.h}"></label>
            </div>
            <div class="uxc-align-bar">
              <button type="button" data-ualign="left" title="캔버스 왼쪽">◧</button>
              <button type="button" data-ualign="center-h" title="가로 중앙">◫</button>
              <button type="button" data-ualign="right" title="캔버스 오른쪽">◨</button>
              <button type="button" data-ualign="top" title="캔버스 상단">⬒</button>
              <button type="button" data-ualign="center-v" title="세로 중앙">⊟</button>
              <button type="button" data-ualign="bottom" title="캔버스 하단">⬓</button>
              <button type="button" data-ualign="fit-w" title="가로 꽉 채우기">↔</button>
            </div>
            <div class="row" style="justify-content:space-between">
              <div class="uxc-z-bar" style="flex:1">
                <button type="button" class="g" data-uz="back" title="맨 뒤로 (Ctrl+[)">맨뒤</button>
                <button type="button" class="g" data-uz="down" title="한 단계 뒤로 ([)">뒤</button>
                <button type="button" class="g" data-uz="up" title="한 단계 앞으로 (])">앞</button>
                <button type="button" class="g" data-uz="front" title="맨 앞으로 (Ctrl+])">맨앞</button>
              </div>
              <label class="row" style="font-size:12px;gap:4px"><input type="checkbox" id="ux-vis" ${c.visibility === "hidden" ? "checked" : ""}><span>숨김</span></label>
            </div>
          </div>
        </details>

        <details class="uxc-details" data-udsec="style" ${UXC.openSections.style ? "open" : ""}>
          <summary>스타일 · 디자인 토큰</summary>
          <div class="uxc-details-body">
            <div class="fld">
              <span>강조색 (Accent)</span>
              <div class="uxc-swatches">
                ${ACCENT_SWATCHES.map(hex => `<button type="button" class="uxc-swatch ${accent.toLowerCase() === hex ? "on" : ""}" data-uswatch="${hex}" style="background:${hex}" title="${hex}"></button>`).join("")}
                <input id="ux-accent" type="color" class="uxc-color-pick" value="${esc(accent.slice(0, 7))}">
              </div>
            </div>
            <div class="uxc-grid2">
              <div class="fld">
                <span>배경색</span>
                <div class="row" style="gap:4px">
                  <input id="ux-bg" type="color" class="uxc-color-pick" value="${esc((customBg || "#ffffff").slice(0, 7))}">
                  <button type="button" class="g" id="ux-bg-auto" style="padding:2px 6px;font-size:11px" ${customBg ? "" : "disabled"}>자동</button>
                </div>
              </div>
              <div class="fld">
                <span>글자색</span>
                <div class="row" style="gap:4px">
                  <input id="ux-fg" type="color" class="uxc-color-pick" value="${esc((customFg || "#1d1d1b").slice(0, 7))}">
                  <button type="button" class="g" id="ux-fg-auto" style="padding:2px 6px;font-size:11px" ${customFg ? "" : "disabled"}>자동</button>
                </div>
              </div>
            </div>
            <div class="uxc-grid2">
              <label class="fld">
                <span>둥글기 (${tk.radius ?? 10}px)</span>
                <input id="ux-radius" type="range" min="0" max="32" value="${tk.radius ?? 10}">
              </label>
              <label class="fld">
                <span>투명도 (${opPct}%)</span>
                <input id="ux-opacity" type="range" min="10" max="100" step="5" value="${opPct}">
              </label>
            </div>
            <div class="uxc-grid3">
              <label class="fld"><span>테두리 px</span><input id="ux-bw" type="number" min="0" max="8" placeholder="자동" value="${tk.borderWidth ?? ""}"></label>
              <label class="fld"><span>선 스타일</span><select id="ux-bs"><option value="">자동</option>${["solid", "dashed", "dotted", "none"].map(s => `<option value="${s}" ${tk.borderStyle === s ? "selected" : ""}>${s}</option>`).join("")}</select></label>
              <div class="fld"><span>선 색상</span><div class="row" style="gap:3px"><input id="ux-bc" type="color" class="uxc-color-pick" value="${esc((customBc || "#d5d3ca").slice(0, 7))}"><button type="button" class="g" id="ux-bc-auto" style="padding:2px 5px;font-size:10.5px" ${customBc ? "" : "disabled"}>×</button></div></div>
            </div>
            <div class="uxc-grid3">
              <label class="fld"><span>그림자</span><select id="ux-shd"><option value="">자동</option>${[["none", "없음"], ["sm", "약함"], ["md", "보통"], ["lg", "강함"]].map(([k, t]) => `<option value="${k}" ${tk.shadow === k ? "selected" : ""}>${t}</option>`).join("")}</select></label>
              <label class="fld"><span>글꼴 px</span><input id="ux-fs" type="number" min="10" max="36" placeholder="자동" value="${tk.fontSize ?? ""}"></label>
              <label class="fld"><span>굵기</span><select id="ux-fw"><option value="">자동</option>${["400", "500", "600", "700", "800"].map(w => `<option value="${w}" ${String(tk.fontWeight || "") === w ? "selected" : ""}>${w}</option>`).join("")}</select></label>
            </div>
            <div class="fld">
              <span>텍스트 정렬</span>
              <div class="uxc-seg-sm" id="ux-ta">
                <button type="button" data-uta="" class="${!tk.textAlign ? "on" : ""}">자동</button>
                <button type="button" data-uta="left" class="${tk.textAlign === "left" ? "on" : ""}">좌</button>
                <button type="button" data-uta="center" class="${tk.textAlign === "center" ? "on" : ""}">중</button>
                <button type="button" data-uta="right" class="${tk.textAlign === "right" ? "on" : ""}">우</button>
              </div>
            </div>
          </div>
        </details>

        <details class="uxc-details" data-udsec="inner" ${UXC.openSections.inner ? "open" : ""}>
          <summary>내부 배치 · 여백 (Flex / Grid)</summary>
          <div class="uxc-details-body">
            <div class="uxc-grid3">
              <label class="fld"><span>방향</span><select id="ux-dir"><option value="column" ${c.layout.direction === "column" ? "selected" : ""}>column</option><option value="row" ${c.layout.direction === "row" ? "selected" : ""}>row</option><option value="grid" ${c.layout.direction === "grid" ? "selected" : ""}>grid</option></select></label>
              <label class="fld"><span>열 수</span><input id="ux-cols" type="number" min="1" max="12" value="${c.layout.columns ?? 1}"></label>
              <label class="fld"><span>Padding</span><input id="ux-pad" type="number" min="0" max="64" value="${c.spacing.padding ?? 14}"></label>
            </div>
            <div class="uxc-grid3">
              <label class="fld"><span>Gap</span><input id="ux-gap" type="number" min="0" max="64" value="${c.spacing.gap ?? 10}"></label>
              <label class="fld"><span>교차축(Align)</span><select id="ux-aln"><option value="">자동</option>${["stretch", "start", "center", "end"].map(a => `<option value="${a}" ${c.layout.align === a ? "selected" : ""}>${a}</option>`).join("")}</select></label>
              <label class="fld"><span>주축(Justify)</span><select id="ux-jst"><option value="">자동</option>${["start", "center", "between", "end"].map(j => `<option value="${j}" ${c.layout.justify === j ? "selected" : ""}>${j}</option>`).join("")}</select></label>
            </div>
          </div>
        </details>

        <details class="uxc-details" data-udsec="agent" ${UXC.openSections.agent ? "open" : ""}>
          <summary>에이전트 의도 · 보호</summary>
          <div class="uxc-details-body">
            <label class="fld"><span>2D 의도/동작 설명</span><textarea id="ux-enote" rows="2" style="min-height:52px">${esc(c.props.note || "")}</textarea></label>
            <label class="row" style="font-size:12.5px"><input type="checkbox" id="ux-lock" ${it.locked ? "checked" : ""}> <span>🔒 사용자 잠금 (에이전트 수정 차단)</span></label>
            ${conns.length ? `<div class="row" style="gap:4px">${conns.map(t => `<span class="uxc-conn-chip">${esc(sel)} → ${esc(t)}<button type="button" data-uunconn="${esc(t)}" title="연결 해제">✕</button></span>`).join("")}</div>` : ""}
          </div>
        </details>

        <div class="row" style="margin-top:4px">
          <button class="g" id="ux-dup" style="flex:1">복제 (Ctrl+D)</button>
          <button class="g bad" id="ux-del" style="flex:1">삭제 (Del)</button>
        </div>
      </div>`;

    el.querySelectorAll("[data-udsec]").forEach(det => {
      det.ontoggle = () => { UXC.openSections[det.dataset.udsec] = det.open; };
    });

    const bindNum = (id, fn) => {
      const node = $(id);
      if (node) node.onchange = e => { snapshotUndo(); fn(parseInt(e.target.value, 10) || 0); drawStage(); };
    };

    $("#ux-sel").onchange = e => {
      const next = e.target.value.trim();
      if (!next || next === sel || UXC.items[next] || next === CANVAS_META_SELECTOR) return;
      snapshotUndo();
      UXC.items[next] = UXC.items[sel];
      delete UXC.items[sel];
      UXC.removed.add(sel);
      UXC.removed.delete(next);
      for (const other of Object.values(UXC.items)) {
        const arr = other.config.props?.connects_to;
        if (Array.isArray(arr)) other.config.props.connects_to = arr.map(t => t === sel ? next : t);
      }
      setSelection([next], next);
      drawStage(); drawInspector();
    };
    $("#ux-kind").onchange = e => {
      snapshotUndo();
      const nk = e.target.value.trim() || "section";
      c.props.kind = nk;
      const nvk = resolveVisualKind(nk);
      const spec = KIND_SPECS[nvk];
      if (spec) {
        c.variant = spec.variants?.[0]?.[0] || "default";
        if (!c.tokens?.bgCustom) c.tokens.accent = spec.accent || c.tokens.accent;
      }
      if (nvk === "table") c.props.table = normalizeTableData(c.props.table);
      drawStage(); drawInspector();
    };
    $("#ux-variant").onchange = e => { snapshotUndo(); c.variant = e.target.value; drawStage(); drawInspector(); };
    $("#ux-label").oninput = e => { snapshotUndoOnce(`lbl:${sel}`); c.props.label = e.target.value; syncBoxDom(sel); };
    $("#ux-label").onchange = () => { UXC._undoKey = null; };

    bindNum("#ux-x", v => { c.layout.x = Math.max(0, Math.min(UXC.vp.width - c.size.w, v)); });
    bindNum("#ux-y", v => { c.layout.y = Math.max(0, Math.min(UXC.vp.height - c.size.h, v)); });
    bindNum("#ux-w", v => { c.size.w = Math.max(44, Math.min(UXC.vp.width - c.layout.x, v)); });
    bindNum("#ux-h", v => { c.size.h = Math.max(28, Math.min(UXC.vp.height - c.layout.y, v)); });
    el.querySelectorAll("[data-ualign]").forEach(b => b.onclick = () => alignSelected(b.dataset.ualign));
    el.querySelectorAll("[data-uz]").forEach(b => b.onclick = () => reorderLayer(sel, b.dataset.uz));
    $("#ux-vis").onchange = e => { snapshotUndo(); c.visibility = e.target.checked ? "hidden" : "visible"; drawStage(); };

    el.querySelectorAll("[data-uswatch]").forEach(b => b.onclick = () => {
      snapshotUndo(); c.tokens.accent = b.dataset.uswatch; drawStage(); drawInspector();
    });
    $("#ux-accent").oninput = e => { snapshotUndoOnce(`acc:${sel}`); c.tokens.accent = e.target.value; syncBoxDom(sel); };
    $("#ux-accent").onchange = () => { UXC._undoKey = null; drawInspector(); };
    $("#ux-bg").oninput = e => { snapshotUndoOnce(`bg:${sel}`); c.tokens.bg = e.target.value; c.tokens.bgCustom = true; syncBoxDom(sel); };
    $("#ux-bg").onchange = () => { UXC._undoKey = null; drawInspector(); };
    $("#ux-bg-auto").onclick = () => { snapshotUndo(); c.tokens.bg = ""; delete c.tokens.bgCustom; drawStage(); drawInspector(); };
    $("#ux-fg").oninput = e => { snapshotUndoOnce(`fg:${sel}`); c.tokens.textColor = e.target.value; syncBoxDom(sel); };
    $("#ux-fg").onchange = () => { UXC._undoKey = null; drawInspector(); };
    $("#ux-fg-auto").onclick = () => { snapshotUndo(); delete c.tokens.textColor; drawStage(); drawInspector(); };
    $("#ux-radius").oninput = e => { snapshotUndoOnce(`rad:${sel}`); c.tokens.radius = parseInt(e.target.value, 10) || 0; syncBoxDom(sel); };
    $("#ux-radius").onchange = () => { UXC._undoKey = null; drawInspector(); };
    $("#ux-opacity").oninput = e => { snapshotUndoOnce(`opa:${sel}`); c.tokens.opacity = parseInt(e.target.value, 10) || 100; syncBoxDom(sel); };
    $("#ux-opacity").onchange = () => { UXC._undoKey = null; drawInspector(); };
    $("#ux-bw").onchange = e => {
      snapshotUndo();
      if (e.target.value === "") delete c.tokens.borderWidth; else c.tokens.borderWidth = Math.max(0, Math.min(8, parseInt(e.target.value, 10) || 0));
      syncBoxDom(sel);
    };
    $("#ux-bs").onchange = e => { snapshotUndo(); if (!e.target.value) delete c.tokens.borderStyle; else c.tokens.borderStyle = e.target.value; syncBoxDom(sel); };
    $("#ux-bc").oninput = e => { snapshotUndoOnce(`bc:${sel}`); c.tokens.borderColor = e.target.value; syncBoxDom(sel); };
    $("#ux-bc").onchange = () => { UXC._undoKey = null; drawInspector(); };
    $("#ux-bc-auto").onclick = () => { snapshotUndo(); delete c.tokens.borderColor; drawStage(); drawInspector(); };
    $("#ux-shd").onchange = e => { snapshotUndo(); if (!e.target.value) delete c.tokens.shadow; else c.tokens.shadow = e.target.value; syncBoxDom(sel); };
    $("#ux-fs").onchange = e => {
      snapshotUndo();
      if (e.target.value === "") delete c.tokens.fontSize; else c.tokens.fontSize = Math.max(10, Math.min(36, parseInt(e.target.value, 10) || 14));
      syncBoxDom(sel);
    };
    $("#ux-fw").onchange = e => { snapshotUndo(); if (!e.target.value) delete c.tokens.fontWeight; else c.tokens.fontWeight = e.target.value; syncBoxDom(sel); };
    el.querySelectorAll("[data-uta]").forEach(b => b.onclick = () => {
      snapshotUndo();
      if (!b.dataset.uta) delete c.tokens.textAlign; else c.tokens.textAlign = b.dataset.uta;
      drawStage(); drawInspector();
    });

    $("#ux-dir").onchange = e => { snapshotUndo(); c.layout.direction = e.target.value; syncBoxDom(sel); };
    bindNum("#ux-cols", v => { c.layout.columns = Math.max(1, Math.min(12, v)); });
    bindNum("#ux-pad", v => { c.spacing.padding = Math.max(0, Math.min(64, v)); c.spacing.paddingCustom = true; });
    bindNum("#ux-gap", v => { c.spacing.gap = Math.max(0, Math.min(64, v)); c.spacing.gapCustom = true; });
    $("#ux-aln").onchange = e => { snapshotUndo(); if (!e.target.value) delete c.layout.align; else c.layout.align = e.target.value; syncBoxDom(sel); };
    $("#ux-jst").onchange = e => { snapshotUndo(); if (!e.target.value) delete c.layout.justify; else c.layout.justify = e.target.value; syncBoxDom(sel); };

    $("#ux-enote").oninput = e => { snapshotUndoOnce(`note:${sel}`); c.props.note = e.target.value; syncBoxDom(sel); };
    $("#ux-enote").onchange = () => { UXC._undoKey = null; };
    $("#ux-lock").onchange = e => { snapshotUndo(); it.locked = e.target.checked; drawStage(); };
    $("#uxc-conn").onclick = () => { UXC.connectMode = !UXC.connectMode; drawStage(); drawInspector(); };
    el.querySelectorAll("[data-uunconn]").forEach(b => b.onclick = () => {
      snapshotUndo();
      c.props.connects_to = (c.props.connects_to || []).filter(t => t !== b.dataset.uunconn);
      drawStage(); drawInspector();
    });
    $("#ux-dup").onclick = () => duplicateBox(sel, c);
    $("#ux-del").onclick = () => deleteBox(sel);

    /* -------- Bind Component-Specific Content Editors -------- */
    if (vk === "table") {
      const tbl = normalizeTableData(c.props.table);
      const commitTbl = (redrawInsp = true) => {
        c.props.table = tbl;
        syncBoxDom(sel);
        if (redrawInsp) drawInspector();
      };
      $("#ux-tbl-add-col").onclick = () => {
        snapshotUndo();
        tbl.columns.push(`열 ${tbl.columns.length + 1}`);
        tbl.rows.forEach(r => r.push(""));
        commitTbl(true);
      };
      $("#ux-tbl-add-row").onclick = () => {
        snapshotUndo();
        tbl.rows.push(tbl.columns.map(() => ""));
        commitTbl(true);
      };
      $("#ux-tbl-modal").onclick = () => openTableEditorModal(sel);
      $("#ux-tbl-badge").onchange = e => { snapshotUndo(); tbl.badgeCol = parseInt(e.target.value, 10); commitTbl(false); };
      $("#ux-tbl-tbar").onchange = e => { snapshotUndo(); tbl.showToolbar = e.target.checked; commitTbl(false); };
      $("#ux-tbl-hdr").onchange = e => { snapshotUndo(); tbl.showHeader = e.target.checked; commitTbl(false); };
      el.querySelectorAll(".ux-tbl-col-in").forEach(inp => {
        const ci = +inp.dataset.ci;
        inp.oninput = () => {
          snapshotUndoOnce(`tcol:${sel}:${ci}`);
          tbl.columns[ci] = inp.value;
          const th = el.querySelectorAll(".uxc-mini-sheet thead th")[ci + 1];
          if (th) th.textContent = inp.value;
          const opt = $(`#ux-tbl-badge option[value="${ci}"]`);
          if (opt) opt.textContent = inp.value || `열 ${ci + 1}`;
          commitTbl(false);
        };
        inp.onchange = () => { UXC._undoKey = null; };
      });
      el.querySelectorAll("[data-tcmv]").forEach(b => b.onclick = () => {
        const [ci, dir] = b.dataset.tcmv.split(",").map(Number);
        const ni = ci + dir;
        if (ni < 0 || ni >= tbl.columns.length) return;
        snapshotUndo();
        [tbl.columns[ci], tbl.columns[ni]] = [tbl.columns[ni], tbl.columns[ci]];
        tbl.rows.forEach(r => { [r[ci], r[ni]] = [r[ni], r[ci]]; });
        if (tbl.badgeCol === ci) tbl.badgeCol = ni;
        else if (tbl.badgeCol === ni) tbl.badgeCol = ci;
        commitTbl(true);
      });
      el.querySelectorAll("[data-tcdel]").forEach(b => b.onclick = () => {
        const ci = +b.dataset.tcdel;
        if (tbl.columns.length <= 1) return;
        snapshotUndo();
        tbl.columns.splice(ci, 1);
        tbl.rows.forEach(r => r.splice(ci, 1));
        if (tbl.badgeCol === ci) tbl.badgeCol = -1;
        else if (tbl.badgeCol > ci) tbl.badgeCol--;
        commitTbl(true);
      });
      el.querySelectorAll(".ux-tbl-cell-in").forEach(inp => {
        const ri = +inp.dataset.ri, ci = +inp.dataset.ci;
        inp.oninput = () => {
          snapshotUndoOnce(`tcell:${sel}:${ri}:${ci}`);
          tbl.rows[ri][ci] = inp.value;
          commitTbl(false);
        };
        inp.onchange = () => { UXC._undoKey = null; };
      });
      el.querySelectorAll("[data-trmv]").forEach(b => b.onclick = () => {
        const [ri, dir] = b.dataset.trmv.split(",").map(Number);
        const ni = ri + dir;
        if (ni < 0 || ni >= tbl.rows.length) return;
        snapshotUndo();
        [tbl.rows[ri], tbl.rows[ni]] = [tbl.rows[ni], tbl.rows[ri]];
        commitTbl(true);
      });
      el.querySelectorAll("[data-trdel]").forEach(b => b.onclick = () => {
        snapshotUndo();
        tbl.rows.splice(+b.dataset.trdel, 1);
        commitTbl(true);
      });
    } else if (vk === "chart") {
      const ch = normalizeChartData(c.props.chart);
      $("#ux-ch-unit").oninput = e => { snapshotUndoOnce(`chu:${sel}`); ch.unit = e.target.value; c.props.chart = ch; syncBoxDom(sel); };
      $("#ux-ch-unit").onchange = () => { UXC._undoKey = null; };
      el.querySelectorAll(".ux-ch-lbl").forEach(inp => {
        const i = +inp.dataset.chi;
        inp.oninput = () => { snapshotUndoOnce(`chl:${sel}:${i}`); ch.labels[i] = inp.value; c.props.chart = ch; syncBoxDom(sel); };
        inp.onchange = () => { UXC._undoKey = null; };
      });
      el.querySelectorAll(".ux-ch-val").forEach(inp => {
        const i = +inp.dataset.chi;
        inp.oninput = () => { snapshotUndoOnce(`chv:${sel}:${i}`); ch.values[i] = Math.max(0, Number(inp.value) || 0); c.props.chart = ch; syncBoxDom(sel); };
        inp.onchange = () => { UXC._undoKey = null; };
      });
      el.querySelectorAll("[data-chdel]").forEach(b => b.onclick = () => {
        if (ch.values.length <= 1) return;
        snapshotUndo();
        const i = +b.dataset.chdel;
        ch.labels.splice(i, 1); ch.values.splice(i, 1);
        c.props.chart = ch; drawStage(); drawInspector();
      });
      $("#ux-ch-add").onclick = () => {
        snapshotUndo();
        ch.labels.push(`항목 ${ch.labels.length + 1}`);
        ch.values.push(50);
        c.props.chart = ch; drawStage(); drawInspector();
      };
    } else {
      const bindPropInput = (id, key, isCheck = false) => {
        const node = $(id);
        if (!node) return;
        if (isCheck) {
          node.onchange = e => { snapshotUndo(); c.props[key] = e.target.checked; syncBoxDom(sel); };
        } else {
          node.oninput = e => { snapshotUndoOnce(`p:${sel}:${key}`); c.props[key] = e.target.value; syncBoxDom(sel); };
          node.onchange = () => { UXC._undoKey = null; };
        }
      };
      bindPropInput("#ux-p-cta", "ctaLabel");
      bindPropInput("#ux-p-sph", "searchPlaceholder");
      bindPropInput("#ux-p-foot", "footerLabel");
      bindPropInput("#ux-p-badge", "badge");
      bindPropInput("#ux-p-mval", "metricValue");
      bindPropInput("#ux-p-mtrend", "metricTrend");
      bindPropInput("#ux-p-spark", "showSpark", true);
      bindPropInput("#ux-p-cta1", "primaryCta");
      bindPropInput("#ux-p-cta2", "secondaryCta");
      bindPropInput("#ux-p-ph", "placeholder");
      bindPropInput("#ux-p-def", "defaultValue");
      bindPropInput("#ux-p-help", "helperText");
      bindPropInput("#ux-p-req", "required", true);
      bindPropInput("#ux-p-icon", "icon");
      bindPropInput("#ux-p-dis", "disabled", true);
      bindPropInput("#ux-p-mcan", "cancelLabel");
      bindPropInput("#ux-p-mok", "confirmLabel");
      bindPropInput("#ux-p-minp", "showInput", true);
      bindPropInput("#ux-p-copy", "copyright");
      bindPropInput("#ux-p-cap", "caption");
      if ($("#ux-p-asp")) {
        const key = c.variant === "video" ? "duration" : "aspect";
        bindPropInput("#ux-p-asp", key);
      }
      if ($("#ux-p-act")) {
        $("#ux-p-act").onchange = e => { snapshotUndo(); c.props.activeIndex = Math.max(0, parseInt(e.target.value, 10) || 0); syncBoxDom(sel); };
      }
      const listPropKey = vk === "header" ? "navItems" : vk === "sidebar" ? "navItems" : vk === "tabs" ? "items" : vk === "footer" ? "links" : "";
      if (listPropKey) {
        const defaults = vk === "header" ? ["개요", "분석", "문서", "설정"]
          : vk === "sidebar" ? ["대시보드", "프로젝트", "분석", "팀원", "설정"]
          : vk === "tabs" ? (c.props.note ? c.props.note.split(/[,|]/).map(s => s.trim()).filter(Boolean) : ["개요", "상세", "설정", "기록"])
          : ["문서", "개인정보", "이용약관", "문의"];
        const arr = Array.isArray(c.props[listPropKey]) && c.props[listPropKey].length ? c.props[listPropKey] : [...defaults];
        el.querySelectorAll("[data-litem]").forEach(inp => {
          const i = +inp.dataset.li;
          inp.oninput = () => { snapshotUndoOnce(`li:${sel}:${i}`); arr[i] = inp.value; c.props[listPropKey] = arr; syncBoxDom(sel); };
          inp.onchange = () => { UXC._undoKey = null; };
        });
        el.querySelectorAll("[data-ldel]").forEach(b => b.onclick = () => {
          if (arr.length <= 1) return;
          snapshotUndo(); arr.splice(+b.dataset.li, 1); c.props[listPropKey] = arr; drawStage(); drawInspector();
        });
        el.querySelectorAll("[data-ladd]").forEach(b => b.onclick = () => {
          snapshotUndo(); arr.push(`항목 ${arr.length + 1}`); c.props[listPropKey] = arr; drawStage(); drawInspector();
        });
      }
    }
  }

  /* ---------------- Global Keyboard & Resize ---------------- */
  function isTextEditingTarget(el) {
    if (!el) return false;
    if (el.isContentEditable) return true;
    const tag = (el.tagName || "").toUpperCase();
    if (tag === "TEXTAREA" || tag === "SELECT") return true;
    if (tag === "INPUT") {
      const t = (el.type || "text").toLowerCase();
      return !["checkbox", "radio", "range", "color", "button", "submit", "reset", "file"].includes(t);
    }
    return false;
  }

  function bindGlobalListeners() {
    if (UXC._keyBound) return;
    UXC._keyBound = true;

    window.addEventListener("pointerdown", e => {
      if (!e.target.closest(".uxc-ctx-menu")) closeContextMenu();
    });

    window.addEventListener("keydown", e => {
      if (G.getTAB() !== "ux") return;
      const mod = e.ctrlKey || e.metaKey;
      const code = e.code || "";

      // Save works everywhere (even while typing in inspector inputs)
      if (mod && code === "KeyS") {
        e.preventDefault();
        run(() => submitToAgent(false))();
        return;
      }

      if (UXC._modalOpen || UXC._inlineEdit) return;

      if (e.key === "Escape") {
        closeContextMenu();
        if (UXC.connectMode) { UXC.connectMode = false; drawStage(); drawInspector(); }
        else if (getSelected().length) { setSelection([], ""); drawStage(); drawInspector(); }
        return;
      }

      if (UXC.json || isTextEditingTarget(document.activeElement)) return;

      if (code === "Space" && !mod) {
        if (!UXC._spaceDown) {
          UXC._spaceDown = true;
          $("#uxc-stage")?.classList.add("is-panning");
        }
        e.preventDefault();
        return;
      }

      if (e.key === "?" || (e.shiftKey && code === "Slash")) {
        e.preventDefault();
        openShortcutsModal();
        return;
      }

      if (mod && code === "KeyZ") {
        e.preventDefault();
        if (e.shiftKey) performRedo(); else performUndo();
        return;
      }
      if (mod && code === "KeyY") {
        e.preventDefault();
        performRedo();
        return;
      }
      if (mod && code === "KeyA") {
        e.preventDefault();
        selectAllBoxes();
        return;
      }
      if (mod && code === "KeyC") {
        e.preventDefault();
        copySelected();
        return;
      }
      if (mod && code === "KeyX") {
        e.preventDefault();
        copySelected();
        deleteSelected();
        return;
      }
      if (mod && code === "KeyV") {
        e.preventDefault();
        pasteClipboard();
        return;
      }
      if (mod && code === "KeyD") {
        e.preventDefault();
        duplicateSelected();
        return;
      }

      // View & Canvas toggles
      if (!mod && code === "KeyG") {
        e.preventDefault();
        snapshotUndo();
        if (e.shiftKey) UXC.canvas.showColumns = !UXC.canvas.showColumns;
        else UXC.canvas.showGrid = !UXC.canvas.showGrid;
        drawStage();
        if (!getSelected().length) drawInspector();
        return;
      }
      if (!mod && code === "KeyB") {
        e.preventDefault();
        UXC.showMeta = !UXC.showMeta;
        drawStage();
        return;
      }
      if (code === "Equal" || code === "NumpadAdd") {
        e.preventDefault();
        const cur = UXC.zoom === "fit" ? getScale($("#uxc-stage")) : Number(UXC.zoom);
        UXC.zoom = Math.min(2, Math.round((cur + 0.15) * 100) / 100);
        $("#uxc-zfit")?.classList.remove("on");
        drawStage();
        return;
      }
      if (code === "Minus" || code === "NumpadSubtract") {
        e.preventDefault();
        const cur = UXC.zoom === "fit" ? getScale($("#uxc-stage")) : Number(UXC.zoom);
        UXC.zoom = Math.max(0.25, Math.round((cur - 0.15) * 100) / 100);
        $("#uxc-zfit")?.classList.remove("on");
        drawStage();
        return;
      }
      if (code === "Digit0" || code === "Numpad0") {
        e.preventDefault();
        UXC.zoom = "fit";
        $("#uxc-zfit")?.classList.add("on");
        drawStage();
        return;
      }
      if (code === "Digit1" || code === "Numpad1") {
        e.preventDefault();
        UXC.zoom = 1;
        $("#uxc-zfit")?.classList.remove("on");
        drawStage();
        return;
      }

      if (code === "Tab" && !mod) {
        const list = Object.entries(UXC.items).sort((a, b) => (a[1].config.layout.z || 0) - (b[1].config.layout.z || 0)).map(([k]) => k);
        if (!list.length) return;
        e.preventDefault();
        const idx = list.indexOf(UXC.sel);
        const nextIdx = e.shiftKey
          ? (idx <= 0 ? list.length - 1 : idx - 1)
          : (idx < 0 || idx >= list.length - 1 ? 0 : idx + 1);
        setSelection([list[nextIdx]], list[nextIdx]);
        drawStage();
        drawInspector();
        return;
      }

      const sels = getSelected();
      if (!sels.length) return;

      if (code === "Delete" || code === "Backspace") {
        e.preventDefault();
        deleteSelected();
        return;
      }
      if (!mod && code === "KeyL") {
        e.preventDefault();
        toggleLockSelected();
        return;
      }
      if (!mod && code === "KeyH") {
        e.preventDefault();
        toggleHideSelected();
        return;
      }
      if (!mod && code === "KeyC" && sels.length === 1) {
        e.preventDefault();
        UXC.connectMode = !UXC.connectMode;
        drawStage();
        drawInspector();
        return;
      }
      if (!mod && (code === "Enter" || code === "F2") && sels.length === 1) {
        e.preventDefault();
        const s = sels[0];
        const vk = resolveVisualKind(UXC.items[s]?.config.props?.kind);
        if (code === "Enter" && vk === "table") {
          openTableEditorModal(s);
        } else {
          const inp = $("#ux-label");
          if (inp) { inp.focus(); inp.select(); }
        }
        return;
      }
      if ((code === "BracketRight" || code === "BracketLeft") && sels.length === 1) {
        e.preventDefault();
        const op = code === "BracketRight" ? ((mod || e.shiftKey) ? "front" : "up") : ((mod || e.shiftKey) ? "back" : "down");
        reorderLayer(sels[0], op);
        return;
      }

      if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(code)) {
        e.preventDefault();
        const gStep = Math.max(1, UXC.canvas.gridSize || 8);
        const step = e.altKey ? 1 : (e.shiftKey ? gStep * 3 : gStep);
        snapshotUndo();
        for (const s of sels) {
          const c = UXC.items[s]?.config;
          if (!c) continue;
          if (mod) {
            const vk = resolveVisualKind(c.props?.kind);
            const sp = KIND_SPECS[vk] || {};
            if (code === "ArrowLeft")  c.size.w = Math.max(sp.minW || 44, c.size.w - step);
            if (code === "ArrowRight") c.size.w = Math.min(UXC.vp.width - c.layout.x, c.size.w + step);
            if (code === "ArrowUp")    c.size.h = Math.max(sp.minH || 28, c.size.h - step);
            if (code === "ArrowDown")  c.size.h = Math.min(UXC.vp.height - c.layout.y, c.size.h + step);
          } else {
            if (code === "ArrowLeft")  c.layout.x = Math.max(0, c.layout.x - step);
            if (code === "ArrowRight") c.layout.x = Math.min(UXC.vp.width - c.size.w, c.layout.x + step);
            if (code === "ArrowUp")    c.layout.y = Math.max(0, c.layout.y - step);
            if (code === "ArrowDown")  c.layout.y = Math.min(UXC.vp.height - c.size.h, c.layout.y + step);
          }
        }
        drawStage();
        drawInspector();
      }
    });

    window.addEventListener("keyup", e => {
      if (e.code === "Space" && UXC._spaceDown) {
        UXC._spaceDown = false;
        $("#uxc-stage")?.classList.remove("is-panning", "pan-active");
      }
    });

    window.addEventListener("blur", () => {
      UXC._spaceDown = false;
      $("#uxc-stage")?.classList.remove("is-panning", "pan-active");
    });

    let rTimer = null;
    window.addEventListener("resize", () => {
      if (G.getTAB() !== "ux" || UXC.json || UXC.zoom !== "fit") return;
      clearTimeout(rTimer);
      rTimer = setTimeout(() => { if (G.getTAB() === "ux" && !UXC.json && $("#uxc-stage")) drawStage(); }, 80);
    });
  }

  /* ---------------- Agent Submission & View Entry ---------------- */
  async function submitToAgent(launchRun) {
    const WS = G.getWS();
    const note = ($("#uxc-note")?.value ?? UXC.agentNote ?? "").trim();
    const elementsPayload = {
      ...UXC.items,
      [CANVAS_META_SELECTOR]: exportCanvasMetaItem(),
    };
    const res = await api("POST", `/workspaces/${WS}/ux/${encodeURIComponent(UXC.view)}/submit-to-agent`, {
      elements: elementsPayload,
      removed: [...UXC.removed],
      viewport: UXC.vp,
      decision_id: UXC.decisionId,
      note,
      launch_run: !!launchRun,
    }, { noInvalidate: true });
    UXC.removed.clear();
    UXC.pendingReq = null;
    UXC.decisionId = null;
    UXC.dirty = false;
    syncToolbarState();
    toast(res.resumed_run_id ? "2D UX 구성을 에이전트에 전달하고 실행을 시작했습니다" : "2D UX 구성 저장됨");
    if (res.resumed_run_id) G.openTab("collab", { collabMode: "chat" });
    else if (launchRun) G.render();
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
    UXC.canvas = DEFAULT_CANVAS();
    UXC.vp = { width: 1200, height: 720 };
    let idx = 0;
    for (const row of (fullRes.items || [])) {
      if (row.component_selector === CANVAS_META_SELECTOR) {
        loadCanvasMeta(row.config_json);
        continue;
      }
      UXC.items[row.component_selector] = ensureBoxFields(
        row.component_selector,
        { locked: row.locked_by_user, config: row.config_json },
        idx++,
      );
    }
    UXC.removed = new Set();
    UXC.dirty = false;
    setSelection(getSelected(), UXC.sel);

    const pending = (pendRes.pending || [])[0];
    if (pending && (!UXC.pendingReq || pending.ux_request?.view_key === UXC.view)) {
      UXC.pendingReq = pending.ux_request;
      UXC.decisionId = pending.decision_id;
    }
    if (UXC.pendingReq && UXC.pendingReq.view_key === UXC.view) loadPendingIntoCanvas(UXC.pendingReq);

    const paletteButtons = PALETTE.map(k => {
      const sp = KIND_SPECS[k];
      return `<button type="button" class="uxc-pbtn" data-uadd="${k}" data-ul="${sp.label}" data-uw="${sp.w}" data-uh="${sp.h}" title="${sp.ko} 추가">${ICONS[k] || ""}<span>${sp.label}</span></button>`;
    }).join("");

    const vpKey = `${UXC.vp.width}x${UXC.vp.height}`;

    G.V(`<div class="row" style="justify-content:space-between;margin-bottom:10px">
        <div><h2>UX 2D 캔버스 · 오버라이드</h2><p class="sub" style="margin:0">컴포넌트 배치·표 데이터·디자인 토큰·단축키를 정밀하게 구성하면 선언형 JSON과 공간 관계로 에이전트에 전달됩니다.</p></div>
        <div class="row">
          <select id="uxc-view">${viewNames.map(v => `<option value="${esc(v)}" ${v === UXC.view ? "selected" : ""}>${esc(v)}</option>`).join("")}</select>
          <button class="g" id="uxc-new-view">+ 새 뷰</button>
          <select id="uxc-vp">${VP_PRESETS.map(([k, t]) => `<option value="${k}" ${k === vpKey ? "selected" : ""}>${esc(t)}</option>`).join("")}<option value="custom" ${VP_PRESETS.some(([k]) => k === vpKey) ? "" : "selected"}>${UXC.vp.width}×${UXC.vp.height} (사용자)</option></select>
          <button class="g" id="uxc-save" title="저장 (Ctrl+S)">저장</button>
          <button class="b" id="uxc-deliver">${UXC.decisionId ? "2D 전달 & 재개 →" : "에이전트에 2D 전달 →"}</button>
        </div>
      </div>
      ${UXC.pendingReq ? `<div class="card" style="border-color:var(--warn);background:color-mix(in srgb,var(--warn) 8%,var(--panel))">
        <b>🎨 에이전트 시각 배치 요청 (${esc(UXC.pendingReq.view_key)})</b>: ${esc(UXC.pendingReq.prompt || "요소를 배치하고 전달하세요.")}
      </div>` : ""}
      <div class="uxc-toolbar">
        <div class="uxc-palette-group">${paletteButtons}</div>
        <div class="uxc-ctrl-group">
          <button type="button" class="uxc-tbtn" id="uxc-undo" title="실행 취소 (Ctrl+Z)">↶</button>
          <button type="button" class="uxc-tbtn" id="uxc-redo" title="다시 실행 (Ctrl+Shift+Z)">↷</button>
          <span class="g-sep"></span>
          <button type="button" class="uxc-tbtn ${UXC.canvas.showGrid ? "on" : ""}" id="uxc-grid" title="그리드 표시 (G)">그리드</button>
          <button type="button" class="uxc-tbtn ${UXC.canvas.showColumns ? "on" : ""}" id="uxc-cols-btn" title="컬럼 가이드 표시 (Shift+G)">컬럼</button>
          <button type="button" class="uxc-tbtn ${UXC.showMeta ? "on" : ""}" id="uxc-meta" title="모든 셀렉터/잠금 라벨 항상 표시 (B)">블루프린트</button>
          <span class="g-sep"></span>
          <button type="button" class="uxc-tbtn" id="uxc-zout" title="축소 (-)">−</button>
          <button type="button" class="uxc-tbtn ${UXC.zoom === "fit" ? "on" : ""}" id="uxc-zfit" title="화면에 맞추기 (0)"><span id="uxc-zoom-val" class="mono">맞춤</span></button>
          <button type="button" class="uxc-tbtn" id="uxc-zin" title="확대 (+)">+</button>
          <span class="g-sep"></span>
          <button type="button" class="uxc-tbtn ${UXC.json ? "on" : ""}" id="uxc-json">{ } JSON</button>
          <button type="button" class="uxc-tbtn" id="uxc-kbd" title="단축키 도움말 (?)">?</button>
        </div>
      </div>
      <div class="g-wrap uxc-wrap">
        ${UXC.json
          ? `<div class="g-jsonbox"><textarea id="uxc-raw" spellcheck="false">${esc(JSON.stringify({ [CANVAS_META_SELECTOR]: exportCanvasMetaItem(), ...UXC.items }, null, 2))}</textarea><div class="row" style="padding:8px"><button class="b" id="uxc-apply-json">JSON 적용</button></div></div>`
          : `<div id="uxc-stage" class="uxc-stage" tabindex="-1"></div>`}
        <div id="uxc-insp" class="g-insp"></div>
      </div>`);

    bindGlobalListeners();

    $("#uxc-view").onchange = () => { UXC.view = $("#uxc-view").value; setSelection([], ""); G.render(); };
    $("#uxc-new-view").onclick = () => {
      const v = prompt("새 뷰 이름 (예: dashboard):", "dashboard");
      if (v && /^[A-Za-z0-9_.-]{1,64}$/.test(v.trim())) { UXC.view = v.trim(); setSelection([], ""); G.render(); }
    };
    $("#uxc-vp").onchange = () => {
      const val = $("#uxc-vp").value;
      if (val === "custom") return;
      snapshotUndo();
      const [w, h] = val.split("x").map(Number);
      UXC.vp = { width: w, height: h };
      drawStage();
      if (!getSelected().length) drawInspector();
    };
    document.querySelectorAll("[data-uadd]").forEach(b => b.onclick = () => {
      if (UXC.json) { UXC.json = false; G.render().then(() => addElement(b.dataset.uadd, b.dataset.ul, +b.dataset.uw, +b.dataset.uh)); return; }
      const n = Object.keys(UXC.items).length;
      const ox = 32 + (n % 4) * 24, oy = 32 + (n % 5) * 24;
      addElement(b.dataset.uadd, b.dataset.ul, +b.dataset.uw, +b.dataset.uh, ox, oy);
    });
    $("#uxc-undo").onclick = performUndo;
    $("#uxc-redo").onclick = performRedo;
    $("#uxc-grid").onclick = () => {
      snapshotUndo();
      UXC.canvas.showGrid = !UXC.canvas.showGrid;
      drawStage();
      if (!getSelected().length) drawInspector();
    };
    $("#uxc-cols-btn").onclick = () => {
      snapshotUndo();
      UXC.canvas.showColumns = !UXC.canvas.showColumns;
      drawStage();
      if (!getSelected().length) drawInspector();
    };
    $("#uxc-meta").onclick = () => { UXC.showMeta = !UXC.showMeta; drawStage(); };
    const stepZoom = dir => {
      const cur = UXC.zoom === "fit" ? getScale($("#uxc-stage")) : Number(UXC.zoom);
      UXC.zoom = Math.min(2, Math.max(0.25, Math.round((cur + dir * 0.15) * 100) / 100));
      $("#uxc-zfit")?.classList.remove("on");
      drawStage();
    };
    $("#uxc-zout").onclick = () => stepZoom(-1);
    $("#uxc-zin").onclick = () => stepZoom(1);
    $("#uxc-zfit").onclick = () => { UXC.zoom = "fit"; $("#uxc-zfit")?.classList.add("on"); drawStage(); };
    $("#uxc-json").onclick = () => { UXC.json = !UXC.json; G.render(); };
    $("#uxc-kbd").onclick = () => openShortcutsModal();

    if (UXC.json) {
      $("#uxc-apply-json").onclick = run(async () => {
        const parsed = JSON.parse($("#uxc-raw").value);
        snapshotUndo();
        UXC.items = {};
        let idx = 0;
        for (const [sel, v] of Object.entries(parsed)) {
          if (sel === CANVAS_META_SELECTOR) loadCanvasMeta(v?.config || v);
          else UXC.items[sel] = ensureBoxFields(sel, v, idx++);
        }
        UXC.json = false;
        G.render();
      });
    } else {
      drawStage();
    }
    drawInspector();
    syncToolbarState();
    $("#uxc-save").onclick = run(() => submitToAgent(false));
    $("#uxc-deliver").onclick = run(() => submitToAgent(!UXC.decisionId));
  };
})();
