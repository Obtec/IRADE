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
  UXC.zoom = UXC.zoom || "fit";
  UXC.showGrid = UXC.showGrid !== false;
  UXC.showMeta = !!UXC.showMeta;

  const ACCENT_SWATCHES = ["#2f5d50", "#2563eb", "#4f46e5", "#d97706", "#e11d48", "#475569"];

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

  function snapshotUndo() {
    UXC.undo.push(JSON.stringify({ items: UXC.items, removed: [...UXC.removed] }));
    if (UXC.undo.length > 60) UXC.undo.shift();
    UXC.redo = [];
  }

  function makeBox(selector, kind, label, x, y, w, h, note = "", extra = {}) {
    const vk = resolveVisualKind(kind);
    const spec = KIND_SPECS[vk] || KIND_SPECS.card;
    const z = extra.z ?? (Object.keys(UXC.items).length + 1);
    const bg = extra.bg ? safeColor(extra.bg, "") : "";
    return {
      locked: extra.locked !== undefined ? !!extra.locked : true,
      config: {
        layout: {
          x, y, z,
          direction: extra.direction || "column",
          columns: extra.columns || 1,
        },
        size: { w, h },
        spacing: { padding: extra.padding ?? 14, gap: extra.gap ?? 10 },
        tokens: {
          bg,
          ...(bg ? { bgCustom: true } : {}),
          accent: safeColor(extra.accent, spec.accent || "#2f5d50"),
          radius: extra.radius ?? spec.radius ?? 10,
        },
        visibility: extra.visibility || "visible",
        order: z,
        variant: extra.variant || (spec.variants?.[0]?.[0] || "default"),
        props: {
          kind: kind || "section",
          label: label || selector,
          note,
          connects_to: Array.isArray(extra.connects_to) ? [...extra.connects_to] : [],
        },
      },
    };
  }

  function ensureBoxFields(sel, item, idx) {
    const c = item?.config || {};
    const props = { kind: "section", label: sel, note: "", connects_to: [], ...(c.props || {}) };
    const vk = resolveVisualKind(props.kind);
    const spec = KIND_SPECS[vk] || KIND_SPECS.card;
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
    // Migrate legacy untouched default #1f2937 to theme-native empty bg so light mode renders cleanly.
    const isLegacyBg = rawTokens.bg === "#1f2937" && !rawTokens.bgCustom;
    const bg = isLegacyBg ? "" : safeColor(rawTokens.bg, "");
    const tokens = {
      bg,
      ...(bg && rawTokens.bgCustom ? { bgCustom: true } : {}),
      accent: safeColor(rawTokens.accent, spec.accent || "#2f5d50"),
      radius: Number.isFinite(+rawTokens.radius) ? Math.max(0, Math.min(32, +rawTokens.radius)) : (spec.radius ?? 10),
    };
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
        props,
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
        );
      }
    }
  }

  /* ---------------- Realistic Per-Kind Renderers ---------------- */
  function renderInterior(sel, item) {
    const c = item.config;
    const w = c.size.w, h = c.size.h;
    const rawKind = c.props?.kind || "section";
    const vk = resolveVisualKind(rawKind);
    const variant = c.variant || (KIND_SPECS[vk]?.variants?.[0]?.[0] || "default");
    const label = c.props?.label || sel;
    const note = (c.props?.note || "").trim();
    const bg = safeColor(c.tokens?.bg, "");
    const bgStyle = bg ? `--obj-bg:${bg};` : "";
    const accent = safeColor(c.tokens?.accent, KIND_SPECS[vk]?.accent || "#2f5d50");

    switch (vk) {
      case "header": {
        const showNav = variant !== "minimal" && w >= 480;
        const navHtml = variant === "search"
          ? `<div class="uxc-hdr-search"><svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="7" cy="7" r="4.8"/><line x1="10.8" y1="10.8" x2="14" y2="14"/></svg><span>${esc(note || "검색…")}</span></div>`
          : showNav
            ? `<div class="uxc-hdr-nav"><span class="active">개요</span><span>분석</span><span>문서</span>${w >= 720 ? "<span>설정</span>" : ""}</div>`
            : "";
        return `<div class="uxc-k-header v-${esc(variant)}" style="${bgStyle}">
          <div class="uxc-hdr-brand">
            <span class="uxc-hdr-mark"><svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 8.5L6.5 12 13 4.5" stroke-linecap="round" stroke-linejoin="round"/></svg></span>
            <span class="uxc-hdr-title">${esc(label)}</span>
          </div>
          ${navHtml}
          <div class="uxc-hdr-actions">
            ${w >= 340 ? `<span class="uxc-mini-btn solid">시작하기</span>` : ""}
            <span class="uxc-avatar-dot">U</span>
          </div>
        </div>`;
      }

      case "sidebar": {
        const rail = variant === "rail" || w < 116;
        const navItems = [
          ["대시보드", '<path d="M2.5 2.5h4.5v4.5H2.5zM9 2.5h4.5v4.5H9zM2.5 9h4.5v4.5H2.5zM9 9h4.5v4.5H9z"/>'],
          ["프로젝트", '<path d="M2 4.5h4l1.5 1.8H14v7.2H2z"/>'],
          ["분석", '<path d="M3 13V8m5 5V4m5 9V6.5"/>'],
          ["팀원", '<circle cx="6.5" cy="5.5" r="2.2"/><path d="M2.5 13c.6-2.2 2.2-3.2 4-3.2s3.4 1 4 3.2"/>'],
          ["설정", '<circle cx="8" cy="8" r="2.2"/><path d="M8 2.5v1.5m0 8v1.5M2.5 8H4m8 0h1.5"/>'],
        ];
        const maxItems = Math.max(2, Math.min(navItems.length, Math.floor((h - 90) / 34)));
        return `<div class="uxc-k-sidebar ${rail ? "uxc-sb-rail" : ""}" style="${bgStyle}">
          <div class="uxc-sb-head">
            <span class="uxc-hdr-mark" style="width:22px;height:22px;border-radius:6px"><svg width="12" height="12" viewBox="0 0 16 16" fill="currentColor"><rect x="3" y="3" width="4" height="4" rx="1"/><rect x="9" y="3" width="4" height="4" rx="1"/><rect x="3" y="9" width="4" height="4" rx="1"/></svg></span>
            ${rail ? "" : `<div style="min-width:0"><div class="uxc-sb-title">${esc(label)}</div>${note ? `<div class="uxc-sb-sub">${esc(note)}</div>` : ""}</div>`}
          </div>
          <div class="uxc-sb-list">
            ${navItems.slice(0, maxItems).map(([t, p], i) => `<div class="uxc-sb-item ${i === 0 ? "active" : ""}">
              <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">${p}</svg>
              ${rail ? "" : `<span>${esc(t)}</span>`}
            </div>`).join("")}
          </div>
          ${h >= 220 ? `<div class="uxc-sb-foot" style="${rail ? "justify-content:center" : ""}"><span class="uxc-avatar-dot" style="width:20px;height:20px;font-size:9px">A</span>${rail ? "" : `<span>Workspace</span>`}</div>` : ""}
        </div>`;
      }

      case "hero": {
        const showVisual = variant === "split" && w >= 480 && h >= 150;
        return `<div class="uxc-k-hero v-${esc(variant)}" style="${bgStyle}">
          <div class="uxc-hero-copy">
            <div><span class="uxc-pill accent">✦ Featured</span></div>
            <div class="uxc-hero-title">${esc(label)}</div>
            <div class="uxc-hero-desc">${esc(note || "핵심 가치와 주요 액션을 명확하게 전달하는 히어로 배너 영역입니다.")}</div>
            <div class="uxc-hero-ctas">
              <span class="uxc-mini-btn solid">지금 시작하기 →</span>
              ${w >= 320 ? `<span class="uxc-mini-btn ghost">자세히 보기</span>` : ""}
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
          const kpi = numMatch ? numMatch[0] : "12,840";
          const restNote = note && numMatch ? note.replace(numMatch[0], "").trim() : note;
          return `<div class="uxc-k-card v-metric" style="${bgStyle}">
            <div class="uxc-card-head">
              <span class="uxc-card-title" style="color:var(--obj-muted);font-size:12px;font-weight:600">${esc(label)}</span>
              <span class="uxc-kpi-trend">↗ +14.2%</span>
            </div>
            <div class="uxc-kpi-val">${esc(kpi)}</div>
            ${restNote ? `<div class="uxc-card-note" style="-webkit-line-clamp:1">${esc(restNote)}</div>` : ""}
            ${h >= 115 ? `<svg class="uxc-spark" viewBox="0 0 200 44" preserveAspectRatio="none">
              <path d="M0 36 C 28 32, 42 18, 66 22 C 92 26, 114 10, 140 14 C 164 18, 180 6, 200 4 L 200 44 L 0 44 Z" fill="${accent}" fill-opacity="0.14"/>
              <path d="M0 36 C 28 32, 42 18, 66 22 C 92 26, 114 10, 140 14 C 164 18, 180 6, 200 4" fill="none" stroke="${accent}" stroke-width="2.2" stroke-linecap="round"/>
            </svg>` : ""}
          </div>`;
        }
        if (variant === "media") {
          return `<div class="uxc-k-card v-media" style="${bgStyle}">
            <div class="uxc-card-media-top"><svg width="24" height="24" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3"><rect x="1.5" y="2.5" width="13" height="11" rx="2"/><circle cx="5.5" cy="6" r="1.2"/><path d="M2.5 12l3.5-3.5 2.5 2.5 2-2 3 3"/></svg></div>
            <div class="uxc-card-media-body">
              <div class="uxc-card-title">${esc(label)}</div>
              ${note ? `<div class="uxc-card-note">${esc(note)}</div>` : `<div class="uxc-skel" style="width:82%"></div><div class="uxc-skel soft" style="width:58%"></div>`}
              ${h >= 165 ? `<div class="uxc-card-foot"><span>업데이트됨</span><span style="color:var(--obj-accent-fg);font-weight:600">열기 →</span></div>` : ""}
            </div>
          </div>`;
        }
        return `<div class="uxc-k-card v-${esc(variant)}" style="${bgStyle}">
          <div class="uxc-card-head">
            <div class="uxc-card-head-l">
              <span class="uxc-card-icon"><svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="2.5" y="3" width="11" height="10" rx="2"/><path d="M5.5 7h5M5.5 10h3.5" stroke-linecap="round"/></svg></span>
              <span class="uxc-card-title">${esc(label)}</span>
            </div>
            <span class="uxc-pill accent" style="font-size:9.5px;padding:1px 6px">활성</span>
          </div>
          ${note
            ? `<div class="uxc-card-note">${esc(note)}</div>`
            : `<div style="display:flex;flex-direction:column;gap:6px;margin-top:2px">
                <div class="uxc-skel" style="width:88%"></div>
                <div class="uxc-skel soft" style="width:66%"></div>
              </div>`}
          ${h >= 126 ? `<div class="uxc-card-foot"><span>${esc(c.layout?.direction || "column")}</span><span style="color:var(--obj-accent-fg);font-weight:600">자세히 →</span></div>` : ""}
        </div>`;
      }

      case "chart": {
        let plotSvg = "";
        let legendHtml = "";
        if (variant === "donut") {
          plotSvg = `<svg class="uxc-chart-svg" viewBox="0 0 120 120" preserveAspectRatio="xMidYMid meet">
            <circle cx="60" cy="60" r="42" fill="none" stroke="var(--uxc-skeleton)" stroke-width="14"/>
            <circle cx="60" cy="60" r="42" fill="none" stroke="${accent}" stroke-width="14" stroke-dasharray="168 264" stroke-dashoffset="0" transform="rotate(-90 60 60)" stroke-linecap="round"/>
            <circle cx="60" cy="60" r="42" fill="none" stroke="${accent}" stroke-opacity="0.4" stroke-width="14" stroke-dasharray="56 264" stroke-dashoffset="-174" transform="rotate(-90 60 60)"/>
            <text x="60" y="58" text-anchor="middle" fill="currentColor" font-size="16" font-weight="800">68%</text>
            <text x="60" y="73" text-anchor="middle" fill="var(--obj-muted)" font-size="8.5">완료율</text>
          </svg>`;
          if (w >= 260) {
            legendHtml = `<div class="uxc-chart-legend">
              <div class="uxc-chart-leg-row"><i style="background:${accent}"></i><span>핵심 지표 (68%)</span></div>
              <div class="uxc-chart-leg-row"><i style="background:${accent};opacity:.4"></i><span>진행 중 (21%)</span></div>
              <div class="uxc-chart-leg-row"><i style="background:var(--uxc-skeleton)"></i><span>대기 (11%)</span></div>
            </div>`;
          }
        } else if (variant === "line" || variant === "area") {
          plotSvg = `<svg class="uxc-chart-svg" viewBox="0 0 280 120" preserveAspectRatio="none">
            <line x1="0" y1="25" x2="280" y2="25" stroke="var(--uxc-hairline)" stroke-dasharray="3 3"/>
            <line x1="0" y1="60" x2="280" y2="60" stroke="var(--uxc-hairline)" stroke-dasharray="3 3"/>
            <line x1="0" y1="95" x2="280" y2="95" stroke="var(--uxc-hairline)" stroke-dasharray="3 3"/>
            ${variant === "area" ? `<path d="M6 96 C 46 84, 62 46, 98 54 C 134 62, 152 24, 188 30 C 224 36, 248 16, 274 12 L 274 114 L 6 114 Z" fill="${accent}" fill-opacity="0.16"/>` : ""}
            <path d="M6 96 C 46 84, 62 46, 98 54 C 134 62, 152 24, 188 30 C 224 36, 248 16, 274 12" fill="none" stroke="${accent}" stroke-width="2.6" stroke-linecap="round"/>
            <circle cx="98" cy="54" r="3.5" fill="var(--uxc-surface)" stroke="${accent}" stroke-width="2"/>
            <circle cx="188" cy="30" r="3.5" fill="var(--uxc-surface)" stroke="${accent}" stroke-width="2"/>
            <circle cx="274" cy="12" r="4" fill="${accent}"/>
          </svg>`;
        } else {
          const bars = [44, 68, 52, 86, 62, 95, 76];
          plotSvg = `<svg class="uxc-chart-svg" viewBox="0 0 280 120" preserveAspectRatio="none">
            <line x1="0" y1="25" x2="280" y2="25" stroke="var(--uxc-hairline)" stroke-dasharray="3 3"/>
            <line x1="0" y1="62" x2="280" y2="62" stroke="var(--uxc-hairline)" stroke-dasharray="3 3"/>
            <line x1="0" y1="100" x2="280" y2="100" stroke="var(--uxc-hairline)"/>
            ${bars.map((v, idx) => {
              const bw = 24, gap = 14, x = 12 + idx * (bw + gap), bh = Math.round(v * 0.9), y = 100 - bh;
              const op = idx === 5 ? 1 : (0.38 + (v / 180));
              return `<rect x="${x}" y="${y}" width="${bw}" height="${bh}" rx="4" fill="${accent}" fill-opacity="${op.toFixed(2)}"/>`;
            }).join("")}
          </svg>`;
        }
        return `<div class="uxc-k-chart" style="${bgStyle}">
          <div class="uxc-chart-head">
            <span class="uxc-card-title">${esc(label)}</span>
            <span class="uxc-pill">${esc(note || "최근 7일")}</span>
          </div>
          <div class="uxc-chart-svg-wrap">${plotSvg}${legendHtml}</div>
        </div>`;
      }

      case "table": {
        const rows = [
          ["#104", "인증 게이트웨이", "완료", "오늘"],
          ["#105", "명세 동기화 API", "진행중", "2분 전"],
          ["#106", "2D 캔버스 렌더러", "검토", "방금"],
          ["#107", "접근성 감사", "대기", "어제"],
        ];
        const maxRows = Math.max(2, Math.min(rows.length, Math.floor((h - 76) / 32)));
        return `<div class="uxc-k-table v-${esc(variant)}" style="${bgStyle}">
          <div class="uxc-tbl-bar">
            <span class="uxc-card-title">${esc(label)}</span>
            <span class="uxc-pill">${esc(note || `${rows.length}건`)}</span>
          </div>
          <div class="uxc-tbl-grid">
            <div class="uxc-tbl-row head"><span>ID</span><span>항목</span><span>상태</span><span>업데이트</span></div>
            ${rows.slice(0, maxRows).map(([id, name, st, tm], idx) => `<div class="uxc-tbl-row ${idx % 2 === 1 ? "alt" : ""}">
              <span class="mono" style="color:var(--obj-muted)">${id}</span>
              <span style="font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${name}</span>
              <span><span class="uxc-pill ${idx === 0 ? "accent" : ""}" style="font-size:10px;padding:1px 6px">${st}</span></span>
              <span style="color:var(--obj-muted);font-size:11px">${tm}</span>
            </div>`).join("")}
          </div>
        </div>`;
      }

      case "button": {
        const darkFg = variant === "primary" && !isDarkHex(bg || accent);
        return `<div class="uxc-k-button v-${esc(variant)} ${darkFg ? "btn-dark-fg" : ""}" style="${bgStyle}">
          <span class="uxc-btn-main">${esc(label)}</span>
          ${note && h >= 54 ? `<span class="uxc-btn-sub">${esc(note)}</span>` : ""}
        </div>`;
      }

      case "input": {
        const compact = h < 54;
        const placeholder = note || (variant === "search" ? "검색어를 입력하세요…" : variant === "select" ? "옵션 선택…" : "입력하세요…");
        const leadIcon = variant === "search"
          ? `<svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="7" cy="7" r="4.8"/><line x1="10.8" y1="10.8" x2="14" y2="14"/></svg>`
          : "";
        const trail = variant === "select"
          ? `<svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 6l4 4 4-4" stroke-linecap="round" stroke-linejoin="round"/></svg>`
          : variant === "search" ? `<span class="uxc-inp-kbd">⌘K</span>` : "";
        return `<div class="uxc-k-input v-${esc(variant)}" style="${bgStyle}">
          ${compact ? "" : `<div class="uxc-inp-label">${esc(label)}</div>`}
          <div class="uxc-inp-box">
            <span style="display:flex;align-items:center;gap:6px;min-width:0;overflow:hidden">${leadIcon}<span class="uxc-inp-ph">${esc(compact ? (note || label) : placeholder)}</span></span>
            ${trail}
          </div>
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
            <div class="uxc-video-bar"><span>01:24</span><div class="uxc-video-track"><i></i></div><span>${esc(label)}</span></div>
          </div>`;
        }
        return `<div class="uxc-k-image v-cover" style="${bgStyle}">
          <svg width="38" height="38" viewBox="0 0 24 24" fill="none" stroke="${accent}" stroke-width="1.4" opacity="0.72">
            <rect x="3" y="4" width="18" height="16" rx="3"/><circle cx="8.5" cy="9.5" r="1.8"/><path d="M4.5 17.5l4.8-4.8 3.5 3.4 2.7-2.6 4 4" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
          ${h >= 88 ? `<div class="uxc-img-cap"><span>${esc(label)}</span><span class="mono" style="color:var(--obj-muted);font-weight:400">${w}×${h}</span></div>` : ""}
        </div>`;
      }

      case "modal":
        return `<div class="uxc-k-modal v-${esc(variant)}" style="${bgStyle}">
          <div class="uxc-modal-head">
            <span>${esc(label)}</span>
            <span class="uxc-modal-close">✕</span>
          </div>
          <div class="uxc-modal-body">
            ${note
              ? `<div class="uxc-card-note">${esc(note)}</div>`
              : `<div class="uxc-skel" style="width:90%"></div><div class="uxc-skel soft" style="width:72%"></div>`}
            ${h >= 200 ? `<div class="uxc-inp-box" style="flex:0 0 32px;margin-top:4px"><span class="uxc-inp-ph">입력 필드…</span></div>` : ""}
          </div>
          <div class="uxc-modal-foot">
            <span class="uxc-mini-btn ghost">취소</span>
            <span class="uxc-mini-btn solid" style="${variant === "alert" ? "background:#dc2626" : ""}">확인</span>
          </div>
        </div>`;

      case "tabs": {
        const items = (note ? note.split(/[,|]/).map(s => s.trim()).filter(Boolean) : [label || "개요", "상세", "설정", "기록"]).slice(0, 5);
        return `<div class="uxc-k-tabs v-${esc(variant)}" style="${bgStyle}">
          ${items.map((t, i) => `<span class="uxc-tab-item ${i === 0 ? "active" : ""}">${esc(t)}</span>`).join("")}
        </div>`;
      }

      case "footer":
        return `<div class="uxc-k-footer v-${esc(variant)}" style="${bgStyle}">
          <span style="font-weight:600;color:var(--obj-fg)">${esc(label)}</span>
          ${variant !== "minimal" && w >= 420 ? `<div class="uxc-ftr-links"><span>문서</span><span>개인정보</span><span>이용약관</span><span>문의</span></div>` : ""}
          <span>© 2026</span>
        </div>`;

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
        const active = sel === UXC.sel || tgt === UXC.sel;
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

  /* ---------------- Canvas Stage Rendering ---------------- */
  function getScale(stage) {
    const availW = Math.max(320, (stage?.clientWidth || 900) - 68);
    if (UXC.zoom === "fit") return Math.min(1, Math.max(0.35, availW / UXC.vp.width));
    const n = Number(UXC.zoom);
    return Number.isFinite(n) ? Math.min(1.5, Math.max(0.35, n)) : 1;
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

  function drawStage() {
    const stage = $("#uxc-stage");
    if (!stage) return;
    const vp = UXC.vp;
    const scale = getScale(stage);
    const slotW = Math.round(vp.width * scale);
    const slotH = Math.round(vp.height * scale);

    const zr = $("#uxc-zoom-val");
    if (zr) zr.textContent = Math.round(scale * 100) + "%";

    const entries = Object.entries(UXC.items);
    const boxes = entries.map(([sel, item]) => {
      const c = item.config;
      const on = UXC.sel === sel ? "on" : "";
      const lk = item.locked ? "locked" : "";
      const hid = c.visibility === "hidden" ? "is-hidden" : "";
      const compact = (c.size.w < 96 || c.size.h < 52) ? "uxc-compact-handles" : "";
      const vk = resolveVisualKind(c.props?.kind);
      const spec = KIND_SPECS[vk];
      const accent = safeColor(c.tokens?.accent, spec?.accent || "#2f5d50");
      const bg = safeColor(c.tokens?.bg, "");
      const contrastCls = bg ? (isDarkHex(bg) ? "is-custom-dark" : "is-custom-light") : "";
      const rad = c.tokens?.radius ?? spec?.radius ?? 10;

      return `<div class="uxc-box ${on} ${lk} ${hid} ${compact} ${contrastCls}" data-usel="${esc(sel)}"
          style="left:${c.layout.x}px;top:${c.layout.y}px;width:${c.size.w}px;height:${c.size.h}px;z-index:${c.layout.z || 1};--obj-accent:${accent};--obj-radius:${rad}px">
        <div class="uxc-surface">${renderInterior(sel, item)}</div>
        <div class="uxc-tag">
          <span class="uxc-tag-sel mono">${esc(sel)}</span>
          <span class="uxc-tag-kind">${esc(c.props?.kind || "section")}</span>
          ${item.locked ? `<span class="uxc-tag-lock" title="사용자 잠금됨 — 에이전트 수정 차단">🔒</span>` : ""}
          <span class="uxc-tag-acts">
            <button type="button" data-uact="lock" title="잠금 토글">${item.locked ? "🔒" : "🔓"}</button>
            <button type="button" data-uact="connect" title="다른 요소에 연결">↗</button>
            <button type="button" data-uact="dup" title="복제">⧉</button>
            <button type="button" class="del" data-uact="del" title="삭제">✕</button>
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
      UXC.showGrid ? "" : "no-grid",
      UXC.showMeta ? "show-meta" : "",
      UXC.connectMode ? "connecting" : "",
    ].filter(Boolean).join(" ");

    const dCls = deviceClass();
    stage.innerHTML = `<div class="uxc-stage-inner">
      <div class="uxc-frame ${dCls}">
        ${deviceChromeTop()}
        <div class="uxc-frame-body">
          <div class="uxc-canvas-slot" style="width:${slotW}px;height:${slotH}px">
            <div class="${boardCls}" id="uxc-board" style="width:${vp.width}px;height:${vp.height}px;transform:scale(${scale});transform-origin:0 0">
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
            </div>
          </div>
        </div>
        ${dCls === "uxc-frame-mobile" ? `<div class="uxc-device-bottom"><div class="uxc-home-pill"></div></div>` : ""}
      </div>
    </div>`;

    bindStage(scale);
  }

  /* ---------------- Direct Manipulation & Magnetic Snapping ---------------- */
  function bindStage(scale) {
    const board = $("#uxc-board");
    if (!board) return;
    let drag = null;
    const snapGrid = (n, free) => free ? Math.round(n) : Math.round(n / 8) * 8;

    function computeSnap(sel, x, y, w, h, free) {
      if (free) return { x, y, gx: null, gy: null };
      const thresh = 6;
      const xLines = [0, UXC.vp.width / 2, UXC.vp.width];
      const yLines = [0, UXC.vp.height / 2, UXC.vp.height];
      for (const [k, it] of Object.entries(UXC.items)) {
        if (k === sel) continue;
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

    board.onpointerdown = e => {
      const actBtn = e.target.closest("[data-uact]");
      if (actBtn) {
        e.stopPropagation();
        const box = actBtn.closest("[data-usel]");
        const sel = box?.dataset.usel;
        if (!sel || !UXC.items[sel]) return;
        const act = actBtn.dataset.uact;
        if (act === "lock") {
          snapshotUndo();
          UXC.items[sel].locked = !UXC.items[sel].locked;
          drawStage(); drawInspector();
        } else if (act === "connect") {
          UXC.sel = sel;
          UXC.connectMode = !UXC.connectMode;
          drawStage(); drawInspector();
        } else if (act === "dup") {
          const c = UXC.items[sel].config;
          duplicateBox(sel, c);
        } else if (act === "del") {
          deleteBox(sel);
        }
        return;
      }

      const rh = e.target.closest("[data-uresize]");
      if (rh) {
        e.stopPropagation();
        const sel = rh.dataset.uresize, dir = rh.dataset.udir || "se", it = UXC.items[sel];
        if (!it) return;
        const vk = resolveVisualKind(it.config.props?.kind);
        const spec = KIND_SPECS[vk] || {};
        const el = board.querySelector(`[data-usel="${CSS.escape(sel)}"]`);
        el?.classList.add("dragging");
        board.setPointerCapture(e.pointerId);
        drag = {
          mode: "resize",
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
          before: JSON.stringify({ items: UXC.items, removed: [...UXC.removed] }),
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
        UXC.sel = sel;
        board.querySelectorAll(".uxc-box").forEach(n => n.classList.toggle("on", n === bx));
        updateArrowsOnly(board);
        drawInspector();
        const it = UXC.items[sel];
        board.setPointerCapture(e.pointerId);
        drag = {
          mode: "move",
          sel,
          sx: e.clientX,
          sy: e.clientY,
          x: it.config.layout.x,
          y: it.config.layout.y,
          changed: false,
          before: JSON.stringify({ items: UXC.items, removed: [...UXC.removed] }),
          el: bx,
        };
        return;
      }

      if (UXC.connectMode) UXC.connectMode = false;
      UXC.sel = "";
      board.classList.remove("connecting");
      board.querySelectorAll(".uxc-box").forEach(n => n.classList.remove("on"));
      updateArrowsOnly(board);
      drawInspector();
    };

    board.onpointermove = e => {
      if (!drag) return;
      const dx = (e.clientX - drag.sx) / scale;
      const dy = (e.clientY - drag.sy) / scale;
      const free = e.altKey;
      const c = UXC.items[drag.sel]?.config;
      if (!c) return;

      if (drag.mode === "move") {
        if (!drag.changed && Math.hypot(e.clientX - drag.sx, e.clientY - drag.sy) < 3) return;
        drag.el?.classList.add("dragging");
        const rawX = Math.max(0, Math.min(UXC.vp.width - c.size.w, snapGrid(drag.x + dx, free)));
        const rawY = Math.max(0, Math.min(UXC.vp.height - c.size.h, snapGrid(drag.y + dy, free)));
        const snapped = computeSnap(drag.sel, rawX, rawY, c.size.w, c.size.h, free);
        if (snapped.x !== c.layout.x || snapped.y !== c.layout.y) drag.changed = true;
        c.layout.x = snapped.x;
        c.layout.y = snapped.y;
        drag.el.style.left = c.layout.x + "px";
        drag.el.style.top = c.layout.y + "px";
        renderGuides(snapped.gx, snapped.gy);
      } else {
        if (!drag.changed && Math.hypot(e.clientX - drag.sx, e.clientY - drag.sy) < 2) return;
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
      }

      const dim = drag.el?.querySelector(".uxc-dim-pill");
      if (dim) dim.textContent = `${c.layout.x},${c.layout.y} · ${c.size.w}×${c.size.h}`;
      updateArrowsOnly(board);
    };

    const finishDrag = () => {
      if (!drag) return;
      drag.el?.classList.remove("dragging");
      renderGuides(null, null);
      const changed = drag.changed;
      const before = drag.before;
      drag = null;
      if (changed) {
        UXC.undo.push(before);
        if (UXC.undo.length > 60) UXC.undo.shift();
        UXC.redo = [];
        drawStage();
        drawInspector();
      }
    };

    board.onpointerup = finishDrag;
    board.onpointercancel = finishDrag;

    board.ondblclick = e => {
      const bx = e.target.closest("[data-usel]");
      if (bx) {
        UXC.sel = bx.dataset.usel;
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

  /* ---------------- Box Manipulation Helpers ---------------- */
  function addElement(kind, label, w, h, x = 40, y = 40, extra = {}) {
    snapshotUndo();
    let i = 1;
    while (UXC.items[`#${kind}-${i}`]) i++;
    const sel = `#${kind}-${i}`;
    UXC.removed.delete(sel);
    const maxZ = Object.values(UXC.items).reduce((m, it) => Math.max(m, it.config.layout.z || 1), 0);
    const cx = Math.max(0, Math.min(UXC.vp.width - w, x));
    const cy = Math.max(0, Math.min(UXC.vp.height - h, y));
    UXC.items[sel] = makeBox(sel, kind, `${label} ${i}`, cx, cy, w, h, extra.note || "", { ...extra, z: maxZ + 1 });
    UXC.sel = sel;
    UXC.connectMode = false;
    drawStage();
    drawInspector();
  }

  function duplicateBox(sel, c) {
    const nx = Math.min(UXC.vp.width - c.size.w, c.layout.x + 24);
    const ny = Math.min(UXC.vp.height - c.size.h, c.layout.y + 24);
    addElement(c.props.kind || "card", c.props.label || "Copy", c.size.w, c.size.h, nx, ny, {
      note: c.props.note || "",
      variant: c.variant,
      accent: c.tokens?.accent,
      bg: c.tokens?.bg,
      radius: c.tokens?.radius,
      direction: c.layout?.direction,
      columns: c.layout?.columns,
      padding: c.spacing?.padding,
      gap: c.spacing?.gap,
      visibility: c.visibility,
    });
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
    if (UXC.sel === sel) UXC.sel = "";
    UXC.connectMode = false;
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
    UXC.sel = "";
    drawStage();
    drawInspector();
  }

  /* ---------------- Inspector ---------------- */
  function drawInspector() {
    const el = $("#uxc-insp");
    if (!el) return;
    if (!UXC.sel || !UXC.items[UXC.sel]) {
      const entries = Object.entries(UXC.items).sort((a, b) => (b[1].config.layout.z || 0) - (a[1].config.layout.z || 0));
      const layersHtml = entries.length
        ? `<div class="uxc-layer-list">${entries.map(([sel, it]) => {
            const c = it.config, vk = resolveVisualKind(c.props?.kind);
            return `<div class="uxc-layer-row" data-ulayer="${esc(sel)}">
              <span style="display:inline-flex;width:14px;height:14px;color:${safeColor(c.tokens?.accent, "#2f5d50")}">${ICONS[vk] || ICONS.card}</span>
              <span class="uxc-layer-name"><b>${esc(c.props?.label || sel)}</b> <span class="uxc-layer-sub mono">${esc(sel)}</span></span>
              <button type="button" class="icon" data-ulayer-vis="${esc(sel)}" title="표시/숨김">${c.visibility === "hidden" ? "🙈" : "👁"}</button>
              <button type="button" class="icon" data-ulayer-lk="${esc(sel)}" title="잠금 토글">${it.locked ? "🔒" : "🔓"}</button>
            </div>`;
          }).join("")}</div>`
        : `<div class="muted" style="font-size:12px">배치된 요소가 없습니다.</div>`;

      el.innerHTML = `<div class="ide-head">캔버스 속성</div><div class="g-form">
        <div class="muted" style="font-size:12px">뷰 <b class="mono">${esc(UXC.view)}</b> · ${entries.length}개 요소 · ${UXC.vp.width}×${UXC.vp.height}</div>
        <div class="uxc-sect-title">스타터 템플릿</div>
        <div class="uxc-tpl-grid">
          <button type="button" class="uxc-tpl-btn" data-utpl="dashboard">${ICONS.chart}<span>대시보드</span></button>
          <button type="button" class="uxc-tpl-btn" data-utpl="landing">${ICONS.hero}<span>랜딩</span></button>
          <button type="button" class="uxc-tpl-btn" data-utpl="form">${ICONS.modal}<span>폼 모달</span></button>
        </div>
        <div class="uxc-sect-title">레이어 (${entries.length})</div>
        ${layersHtml}
        <div class="uxc-sect-title">에이전트 전달</div>
        <label class="fld"><span>에이전트에 전달할 메모</span><textarea id="uxc-note" rows="3" style="min-height:66px" placeholder="예: 상단 헤더 아래 좌측 사이드바와 2열 대시보드 카드로 구성하세요.">${esc(UXC.agentNote || "")}</textarea></label>
        <button class="b" id="uxc-send-agent">${UXC.decisionId ? "2D 구성을 에이전트에 전달 & 재개" : "이 2D 구성으로 에이전트 실행 →"}</button>
        <div class="muted" style="font-size:11px;line-height:1.5">• 더블클릭: 카드 추가 / <b>Alt+드래그</b>: 1px 미세 배치<br>• 화살표: 8px 이동 (Shift 24px) · Delete: 삭제</div>
      </div>`;

      el.querySelectorAll("[data-utpl]").forEach(b => b.onclick = () => applyTemplate(b.dataset.utpl));
      el.querySelectorAll("[data-ulayer]").forEach(row => row.onclick = e => {
        if (e.target.closest("[data-ulayer-vis]") || e.target.closest("[data-ulayer-lk]")) return;
        UXC.sel = row.dataset.ulayer;
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

    const sel = UXC.sel, it = UXC.items[sel], c = it.config;
    const rawKind = c.props.kind || "section";
    const vk = resolveVisualKind(rawKind);
    const kindChoices = Object.keys(KIND_SPECS);
    if (!kindChoices.includes(rawKind)) kindChoices.unshift(rawKind);
    const variants = KIND_SPECS[vk]?.variants || [["default", "기본"]];
    const accent = safeColor(c.tokens?.accent, KIND_SPECS[vk]?.accent || "#2f5d50");
    const customBg = safeColor(c.tokens?.bg, "");
    const conns = c.props.connects_to || [];

    el.innerHTML = `<div class="ide-head">
        <span class="uxc-insp-title">${ICONS[vk] || ""} 요소 속성</span>
        <button class="g ${UXC.connectMode ? "on" : ""}" id="uxc-conn" style="padding:2px 8px;font-size:11px;white-space:nowrap">${UXC.connectMode ? "대상 클릭…" : "↗ 연결"}</button>
      </div>
      <div class="g-form">
        <label class="fld"><span>셀렉터 (ID)</span><input id="ux-sel" class="mono" value="${esc(sel)}"></label>
        <div class="uxc-grid2">
          <label class="fld"><span>종류</span><select id="ux-kind">${kindChoices.map(k => `<option value="${esc(k)}" ${k === rawKind ? "selected" : ""}>${esc(KIND_SPECS[k]?.label || k)}</option>`).join("")}</select></label>
          <label class="fld"><span>스타일 변형</span><select id="ux-variant">${variants.map(([v, t]) => `<option value="${esc(v)}" ${v === c.variant ? "selected" : ""}>${esc(t)}</option>`).join("")}</select></label>
        </div>
        <label class="fld"><span>레이블 (제목)</span><input id="ux-label" value="${esc(c.props.label || "")}"></label>

        <div class="uxc-sect-title">위치 · 크기 · 레이어</div>
        <div class="uxc-grid4">
          <label class="fld"><span>X</span><input id="ux-x" type="number" value="${c.layout.x}"></label>
          <label class="fld"><span>Y</span><input id="ux-y" type="number" value="${c.layout.y}"></label>
          <label class="fld"><span>W</span><input id="ux-w" type="number" value="${c.size.w}"></label>
          <label class="fld"><span>H</span><input id="ux-h" type="number" value="${c.size.h}"></label>
        </div>
        <div class="row" style="justify-content:space-between">
          <div class="uxc-z-bar" style="flex:1">
            <button type="button" class="g" data-uz="back" title="맨 뒤로">맨뒤</button>
            <button type="button" class="g" data-uz="down" title="한 단계 뒤로">뒤</button>
            <button type="button" class="g" data-uz="up" title="한 단계 앞으로">앞</button>
            <button type="button" class="g" data-uz="front" title="맨 앞으로">맨앞</button>
          </div>
          <label class="row" style="font-size:12px;gap:4px"><input type="checkbox" id="ux-vis" ${c.visibility === "hidden" ? "checked" : ""}><span>숨김</span></label>
        </div>

        <div class="uxc-sect-title">디자인 토큰</div>
        <div class="fld">
          <span>강조색 (Accent)</span>
          <div class="uxc-swatches">
            ${ACCENT_SWATCHES.map(hex => `<button type="button" class="uxc-swatch ${accent.toLowerCase() === hex ? "on" : ""}" data-uswatch="${hex}" style="background:${hex}" title="${hex}"></button>`).join("")}
            <input id="ux-accent" type="color" class="uxc-color-pick" value="${esc(accent.slice(0, 7))}">
          </div>
        </div>
        <div class="row">
          <label class="fld" style="flex:1">
            <span>모서리 둥글기 (${c.tokens?.radius ?? 10}px)</span>
            <input id="ux-radius" type="range" min="0" max="28" value="${c.tokens?.radius ?? 10}">
          </label>
          <div class="fld">
            <span>배경색</span>
            <div class="row" style="gap:4px">
              <input id="ux-bg" type="color" class="uxc-color-pick" value="${esc((customBg || "#ffffff").slice(0, 7))}">
              <button type="button" class="g" id="ux-bg-auto" style="padding:2px 6px;font-size:11px" ${customBg ? "" : "disabled"}>자동</button>
            </div>
          </div>
        </div>

        <div class="uxc-sect-title">내부 배치 · 여백</div>
        <div class="uxc-grid3">
          <label class="fld"><span>방향</span><select id="ux-dir"><option value="column" ${c.layout.direction === "column" ? "selected" : ""}>column</option><option value="row" ${c.layout.direction === "row" ? "selected" : ""}>row</option><option value="grid" ${c.layout.direction === "grid" ? "selected" : ""}>grid</option></select></label>
          <label class="fld"><span>열 수</span><input id="ux-cols" type="number" min="1" max="12" value="${c.layout.columns ?? 1}"></label>
          <label class="fld"><span>Gap</span><input id="ux-gap" type="number" min="0" value="${c.spacing.gap ?? 10}"></label>
        </div>

        <div class="uxc-sect-title">에이전트 의도 · 보호</div>
        <label class="fld"><span>2D 의도/동작 설명</span><textarea id="ux-enote" rows="2" style="min-height:56px">${esc(c.props.note || "")}</textarea></label>
        <label class="row" style="font-size:12.5px"><input type="checkbox" id="ux-lock" ${it.locked ? "checked" : ""}> <span>🔒 사용자 잠금 (에이전트 수정 차단)</span></label>
        ${conns.length ? `<div class="row" style="gap:4px">${conns.map(t => `<span class="uxc-conn-chip">${esc(sel)} → ${esc(t)}<button type="button" data-uunconn="${esc(t)}" title="연결 해제">✕</button></span>`).join("")}</div>` : ""}
        <div class="row" style="margin-top:4px">
          <button class="g" id="ux-dup" style="flex:1">복제</button>
          <button class="g bad" id="ux-del" style="flex:1">삭제</button>
        </div>
      </div>`;

    const bindNum = (id, fn) => { $(id).onchange = e => { snapshotUndo(); fn(parseInt(e.target.value, 10) || 0); drawStage(); }; };
    $("#ux-sel").onchange = e => {
      const next = e.target.value.trim();
      if (!next || next === sel || UXC.items[next]) return;
      snapshotUndo();
      UXC.items[next] = UXC.items[sel];
      delete UXC.items[sel];
      UXC.removed.add(sel);
      UXC.removed.delete(next);
      for (const other of Object.values(UXC.items)) {
        const arr = other.config.props?.connects_to;
        if (Array.isArray(arr)) other.config.props.connects_to = arr.map(t => t === sel ? next : t);
      }
      UXC.sel = next;
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
      drawStage(); drawInspector();
    };
    $("#ux-variant").onchange = e => { snapshotUndo(); c.variant = e.target.value; drawStage(); };
    $("#ux-label").oninput = e => { c.props.label = e.target.value; drawStage(); };
    bindNum("#ux-x", v => { c.layout.x = Math.max(0, Math.min(UXC.vp.width - c.size.w, v)); });
    bindNum("#ux-y", v => { c.layout.y = Math.max(0, Math.min(UXC.vp.height - c.size.h, v)); });
    bindNum("#ux-w", v => { c.size.w = Math.max(44, Math.min(UXC.vp.width - c.layout.x, v)); });
    bindNum("#ux-h", v => { c.size.h = Math.max(28, Math.min(UXC.vp.height - c.layout.y, v)); });
    el.querySelectorAll("[data-uz]").forEach(b => b.onclick = () => reorderLayer(sel, b.dataset.uz));
    $("#ux-vis").onchange = e => { snapshotUndo(); c.visibility = e.target.checked ? "hidden" : "visible"; drawStage(); };
    el.querySelectorAll("[data-uswatch]").forEach(b => b.onclick = () => {
      snapshotUndo(); c.tokens.accent = b.dataset.uswatch; drawStage(); drawInspector();
    });
    $("#ux-accent").oninput = e => { c.tokens.accent = e.target.value; drawStage(); };
    $("#ux-accent").onchange = () => drawInspector();
    $("#ux-radius").oninput = e => { c.tokens.radius = parseInt(e.target.value, 10) || 0; drawStage(); };
    $("#ux-radius").onchange = () => drawInspector();
    $("#ux-bg").oninput = e => { c.tokens.bg = e.target.value; c.tokens.bgCustom = true; drawStage(); };
    $("#ux-bg").onchange = () => drawInspector();
    $("#ux-bg-auto").onclick = () => { snapshotUndo(); c.tokens.bg = ""; delete c.tokens.bgCustom; drawStage(); drawInspector(); };
    $("#ux-dir").onchange = e => { snapshotUndo(); c.layout.direction = e.target.value; drawStage(); };
    bindNum("#ux-cols", v => { c.layout.columns = Math.max(1, Math.min(12, v)); });
    bindNum("#ux-gap", v => { c.spacing.gap = Math.max(0, v); });
    $("#ux-enote").oninput = e => { c.props.note = e.target.value; drawStage(); };
    $("#ux-lock").onchange = e => { snapshotUndo(); it.locked = e.target.checked; drawStage(); };
    $("#uxc-conn").onclick = () => { UXC.connectMode = !UXC.connectMode; drawStage(); drawInspector(); };
    el.querySelectorAll("[data-uunconn]").forEach(b => b.onclick = () => {
      snapshotUndo();
      c.props.connects_to = (c.props.connects_to || []).filter(t => t !== b.dataset.uunconn);
      drawStage(); drawInspector();
    });
    $("#ux-dup").onclick = () => duplicateBox(sel, c);
    $("#ux-del").onclick = () => deleteBox(sel);
  }

  /* ---------------- Global Keyboard & Resize ---------------- */
  function bindGlobalListeners() {
    if (!UXC._keyBound) {
      UXC._keyBound = true;
      window.addEventListener("keydown", e => {
        if (G.getTAB() !== "ux" || UXC.json) return;
        const tag = (document.activeElement?.tagName || "").toUpperCase();
        if (["INPUT", "TEXTAREA", "SELECT"].includes(tag) || document.activeElement?.isContentEditable) return;

        if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z") {
          e.preventDefault();
          if (e.shiftKey) $("#uxc-redo")?.click(); else $("#uxc-undo")?.click();
          return;
        }
        if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "d" && UXC.sel && UXC.items[UXC.sel]) {
          e.preventDefault();
          duplicateBox(UXC.sel, UXC.items[UXC.sel].config);
          return;
        }
        if (e.key === "Escape") {
          if (UXC.connectMode) { UXC.connectMode = false; drawStage(); drawInspector(); }
          else if (UXC.sel) { UXC.sel = ""; drawStage(); drawInspector(); }
          return;
        }
        if (!UXC.sel || !UXC.items[UXC.sel]) return;
        if (e.key === "Delete" || e.key === "Backspace") {
          e.preventDefault();
          deleteBox(UXC.sel);
          return;
        }
        const step = e.altKey ? 1 : (e.shiftKey ? 24 : 8);
        const c = UXC.items[UXC.sel].config;
        let moved = false;
        if (e.key === "ArrowLeft")  { snapshotUndo(); c.layout.x = Math.max(0, c.layout.x - step); moved = true; }
        if (e.key === "ArrowRight") { snapshotUndo(); c.layout.x = Math.min(UXC.vp.width - c.size.w, c.layout.x + step); moved = true; }
        if (e.key === "ArrowUp")    { snapshotUndo(); c.layout.y = Math.max(0, c.layout.y - step); moved = true; }
        if (e.key === "ArrowDown")  { snapshotUndo(); c.layout.y = Math.min(UXC.vp.height - c.size.h, c.layout.y + step); moved = true; }
        if (moved) {
          e.preventDefault();
          drawStage();
          drawInspector();
        }
      });

      let rTimer = null;
      window.addEventListener("resize", () => {
        if (G.getTAB() !== "ux" || UXC.json || UXC.zoom !== "fit") return;
        clearTimeout(rTimer);
        rTimer = setTimeout(() => { if (G.getTAB() === "ux" && !UXC.json && $("#uxc-stage")) drawStage(); }, 80);
      });
    }
  }

  /* ---------------- Agent Submission & View Entry ---------------- */
  async function submitToAgent(launchRun) {
    const WS = G.getWS();
    const note = ($("#uxc-note")?.value ?? UXC.agentNote ?? "").trim();
    const res = await api("POST", `/workspaces/${WS}/ux/${encodeURIComponent(UXC.view)}/submit-to-agent`, {
      elements: UXC.items,
      removed: [...UXC.removed],
      viewport: UXC.vp,
      decision_id: UXC.decisionId,
      note,
      launch_run: !!launchRun,
    }, { noInvalidate: true });
    UXC.removed.clear();
    UXC.pendingReq = null;
    UXC.decisionId = null;
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

    const paletteButtons = PALETTE.map(k => {
      const sp = KIND_SPECS[k];
      return `<button type="button" class="uxc-pbtn" data-uadd="${k}" data-ul="${sp.label}" data-uw="${sp.w}" data-uh="${sp.h}" title="${sp.ko} 추가">${ICONS[k] || ""}<span>${sp.label}</span></button>`;
    }).join("");

    G.V(`<div class="row" style="justify-content:space-between;margin-bottom:10px">
        <div><h2>UX 2D 캔버스 · 오버라이드</h2><p class="sub" style="margin:0">실제 UI 컴포넌트를 2차원 공간에 배치·연결·잠금하면 선언형 JSON과 공간 관계로 변환되어 에이전트에 전달됩니다.</p></div>
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
      <div class="uxc-toolbar">
        <div class="uxc-palette-group">${paletteButtons}</div>
        <div class="uxc-ctrl-group">
          <button type="button" class="uxc-tbtn" id="uxc-undo" title="실행 취소 (Ctrl+Z)">↶</button>
          <button type="button" class="uxc-tbtn" id="uxc-redo" title="다시 실행 (Ctrl+Shift+Z)">↷</button>
          <span class="g-sep"></span>
          <button type="button" class="uxc-tbtn ${UXC.showGrid ? "on" : ""}" id="uxc-grid" title="그리드 점 표시">그리드</button>
          <button type="button" class="uxc-tbtn ${UXC.showMeta ? "on" : ""}" id="uxc-meta" title="모든 셀렉터/잠금 라벨 항상 표시">블루프린트</button>
          <span class="g-sep"></span>
          <button type="button" class="uxc-tbtn" id="uxc-zout" title="축소">−</button>
          <button type="button" class="uxc-tbtn ${UXC.zoom === "fit" ? "on" : ""}" id="uxc-zfit" title="화면에 맞추기"><span id="uxc-zoom-val" class="mono">맞춤</span></button>
          <button type="button" class="uxc-tbtn" id="uxc-zin" title="확대">+</button>
          <span class="g-sep"></span>
          <button type="button" class="uxc-tbtn ${UXC.json ? "on" : ""}" id="uxc-json">{ } JSON</button>
        </div>
      </div>
      <div class="g-wrap">
        ${UXC.json
          ? `<div class="g-jsonbox"><textarea id="uxc-raw" spellcheck="false">${esc(JSON.stringify(UXC.items, null, 2))}</textarea><div class="row" style="padding:8px"><button class="b" id="uxc-apply-json">JSON 적용</button></div></div>`
          : `<div id="uxc-stage" class="uxc-stage"></div>`}
        <div id="uxc-insp" class="g-insp"></div>
      </div>`);

    bindGlobalListeners();

    $("#uxc-view").onchange = () => { UXC.view = $("#uxc-view").value; UXC.sel = ""; G.render(); };
    $("#uxc-new-view").onclick = () => {
      const v = prompt("새 뷰 이름 (예: dashboard):", "dashboard");
      if (v && /^[A-Za-z0-9_.-]{1,64}$/.test(v.trim())) { UXC.view = v.trim(); UXC.sel = ""; G.render(); }
    };
    $("#uxc-vp").onchange = () => {
      const [w, h] = $("#uxc-vp").value.split("x").map(Number);
      UXC.vp = { width: w, height: h };
      drawStage();
      if (!UXC.sel) drawInspector();
    };
    document.querySelectorAll("[data-uadd]").forEach(b => b.onclick = () => {
      if (UXC.json) { UXC.json = false; G.render().then(() => addElement(b.dataset.uadd, b.dataset.ul, +b.dataset.uw, +b.dataset.uh)); return; }
      const n = Object.keys(UXC.items).length;
      const ox = 32 + (n % 4) * 24, oy = 32 + (n % 5) * 24;
      addElement(b.dataset.uadd, b.dataset.ul, +b.dataset.uw, +b.dataset.uh, ox, oy);
    });
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
    $("#uxc-grid").onclick = () => { UXC.showGrid = !UXC.showGrid; $("#uxc-grid").classList.toggle("on", UXC.showGrid); drawStage(); };
    $("#uxc-meta").onclick = () => { UXC.showMeta = !UXC.showMeta; $("#uxc-meta").classList.toggle("on", UXC.showMeta); drawStage(); };
    const stepZoom = dir => {
      const cur = UXC.zoom === "fit" ? getScale($("#uxc-stage")) : Number(UXC.zoom);
      UXC.zoom = Math.min(1.5, Math.max(0.35, Math.round((cur + dir * 0.15) * 100) / 100));
      $("#uxc-zfit")?.classList.remove("on");
      drawStage();
    };
    $("#uxc-zout").onclick = () => stepZoom(-1);
    $("#uxc-zin").onclick = () => stepZoom(1);
    $("#uxc-zfit").onclick = () => { UXC.zoom = "fit"; $("#uxc-zfit")?.classList.add("on"); drawStage(); };
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
