(function () {
  'use strict';

  /* =========================================================================
   * UTILITY HELPERS
   * ========================================================================= */
  function clamp(v, min, max) {
    return v < min ? min : (v > max ? max : v);
  }

  function lerp(a, b, t) {
    return a + (b - a) * t;
  }

  function smoothstep(edge0, edge1, x) {
    const t = clamp((x - edge0) / (edge1 - edge0), 0, 1);
    return t * t * (3 - 2 * t);
  }

  // Deterministic PRNG so target indices stay spatially consistent across resizes
  function mulberry32(seed) {
    let a = seed >>> 0;
    return function () {
      a |= 0;
      a = (a + 0x6D2B79F5) | 0;
      let t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  /* =========================================================================
   * 1. REVERSIBLE SCROLL-SCRUBBED SAND-WIND PARTICLE ENGINE
   * ========================================================================= */
  const canvas = document.getElementById('particle-canvas');
  const ctx = canvas.getContext('2d');
  const scrollTrackEl = document.getElementById('scroll-track');
  const vignetteEl = document.getElementById('stage-vignette');
  const chapterPanels = Array.from(document.querySelectorAll('.chapter-panel'));
  const hudChapterBtns = Array.from(document.querySelectorAll('.hud-ch-btn'));

  const MONO_TONES = [
    { r: 245, g: 245, b: 247 }, // Chalk White
    { r: 218, g: 218, b: 224 }, // Bright Silver
    { r: 161, g: 161, b: 170 }, // Silver
    { r: 99,  g: 99,  b: 107 }, // Ash Gray
  ];

  const CHAPTER_META = [
    { title: 'IRADE',        textSide: 'left',  cxRatio: 0.72 },
    { title: 'PROVERS',      textSide: 'right', cxRatio: 0.28 },
    { title: 'CELL GRAPH'  , textSide: 'left',  cxRatio: 0.72 },
    { title: 'STORE',        textSide: 'right', cxRatio: 0.28 },
    { title: 'SNAPSHOTS',    textSide: 'left',  cxRatio: 0.72 },
    { title: 'CRDT SYNC',    textSide: 'right', cxRatio: 0.28 },
  ];

  let vw = Math.max(300, window.innerWidth || 1024);
  let vh = Math.max(300, window.innerHeight || 768);
  let dpr = Math.min(window.devicePixelRatio || 1, 2);

  let sandParticles = [];
  let dustParticles = [];
  let shapes = [];
  let shapeAnchors = [];
  let labField = [];
  let customShape = null;

  // Subtle monochrome vector guides for Graph (Ch 03) and Radar (Ch 06)
  let geoGraphNodes = [];
  let geoRadar = null;

  // Reversible continuous scroll timeline state
  let rawP = 0;               // [0.0 .. 5.0]
  let smoothedP = 0;          // damped continuous [0.0 .. 5.0]
  let rawLabBlend = 0;        // [0.0 .. 1.0]
  let smoothedLabBlend = 0;
  let customBlendTarget = 0;  // 1 when user custom text is active
  let customBlend = 0;
  let customTextValue = null;

  let scrollSync = true;
  let focusView = false;

  let lastScrollY = window.scrollY || 0;
  let scrollVel = 0;
  let smoothDir = 1;
  let scatterGust = 0;
  let scatterDirX = 1;

  const pointer = { x: -9999, y: -9999, active: false, radius: 118 };

  function getParticleCount(width) {
    return width >= 900 ? 2000 : 950;
  }

  function pickTone(rng, brightBias) {
    const roll = rng();
    if (roll < brightBias) return MONO_TONES[0];
    if (roll < brightBias + 0.34) return MONO_TONES[1];
    if (roll < brightBias + 0.72) return MONO_TONES[2];
    return MONO_TONES[3];
  }

  function getAnchorForChapter(idx) {
    const cfg = CHAPTER_META[idx] || CHAPTER_META[0];
    if (vw >= 900) {
      return {
        cx: vw * cfg.cxRatio,
        cy: vh * 0.50,
        scale: Math.min(vw * 0.39, vh * 0.58, 430),
      };
    }
    // Mobile: particles centered in upper half while text docks in lower half
    return {
      cx: vw * 0.50,
      cy: vh * 0.28,
      scale: Math.min(vw * 0.78, vh * 0.32, 260),
    };
  }

  /**
   * Spatially sort sampled points along gentle diagonal strata so particle
   * index k maps coherently across all 6 shapes without chaotic crossings.
   */
  function sortSpatially(points, anchor, seed) {
    const rng = mulberry32(seed);
    const invS = 1 / Math.max(1, anchor.scale);
    for (let i = 0; i < points.length; i++) {
      const pt = points[i];
      const nx = (pt.x - anchor.cx) * invS;
      const ny = (pt.y - anchor.cy) * invS;
      pt._sk = ny * 2.1 + nx * 0.42 + (rng() - 0.5) * 0.14;
    }
    points.sort((a, b) => a._sk - b._sk);
    return points;
  }

  /**
   * Offscreen Canvas Typography Sampler (system sans-serif, works immediately with zero CDN fonts)
   */
  function sampleTextShape(text, count, anchor, ringMode, seed) {
    const rng = mulberry32(seed);
    const { cx, cy, scale } = anchor;
    const pts = [];

    try {
      const off = document.createElement('canvas');
      const octx = off.getContext('2d');
      const boxW = Math.max(240, Math.floor(scale * 1.44));
      const boxH = Math.max(120, Math.floor(scale * 0.64));
      off.width = boxW;
      off.height = boxH;

      const len = Math.max(1, text.length);
      let fontSize = Math.floor(boxW / (len * 0.62));
      fontSize = Math.min(fontSize, Math.floor(boxH * 0.74), 152);
      fontSize = Math.max(fontSize, 26);

      const fontStack = '-apple-system, BlinkMacSystemFont, "Apple SD Gothic Neo", "Malgun Gothic", "Segoe UI", Arial, sans-serif';
      octx.clearRect(0, 0, boxW, boxH);
      octx.fillStyle = '#FFFFFF';
      octx.textAlign = 'center';
      octx.textBaseline = 'middle';
      octx.font = `800 ${fontSize}px ${fontStack}`;

      while (octx.measureText(text).width > boxW * 0.86 && fontSize > 18) {
        fontSize -= 3;
        octx.font = `800 ${fontSize}px ${fontStack}`;
      }
      octx.fillText(text, boxW / 2, boxH / 2);

      const img = octx.getImageData(0, 0, boxW, boxH).data;
      const step = 3;
      for (let y = 0; y < boxH; y += step) {
        for (let x = 0; x < boxW; x += step) {
          if (img[(y * boxW + x) * 4 + 3] > 110) {
            pts.push({
              x: cx - boxW / 2 + x + (rng() - 0.5) * 1.2,
              y: cy - boxH / 2 + y + (rng() - 0.5) * 1.2,
            });
          }
        }
      }
    } catch (_err) {
      // Graceful geometric ring fallback if offscreen sampling is unavailable
      for (let i = 0; i < 360; i++) {
        const a = (i / 360) * Math.PI * 2;
        pts.push({ x: cx + Math.cos(a) * scale * 0.34, y: cy + Math.sin(a) * scale * 0.34 });
      }
    }

    // Deterministic shuffle so uniform subset covers every glyph stroke
    for (let i = pts.length - 1; i > 0; i--) {
      const j = Math.floor(rng() * (i + 1));
      const tmp = pts[i];
      pts[i] = pts[j];
      pts[j] = tmp;
    }

    const targets = [];
    const auxRatio = ringMode === 'vortex' ? 0.34 : (ringMode === 'orbit' ? 0.28 : 0.12);
    const auxCount = Math.floor(count * auxRatio);
    const mainCount = count - auxCount;

    for (let i = 0; i < mainCount; i++) {
      const p = pts.length > 0 ? pts[i % pts.length] : { x: cx, y: cy };
      const tone = pickTone(rng, 0.56);
      targets.push({
        x: p.x, y: p.y,
        size: 0.95 + rng() * 1.15,
        r: tone.r, g: tone.g, b: tone.b, alpha: 0.90,
        role: 'text',
      });
    }

    for (let i = 0; i < auxCount; i++) {
      const a = (i / auxCount) * Math.PI * 2 + rng() * 0.08;
      if (ringMode === 'orbit') {
        const tier = i % 2;
        const rx = scale * (0.56 + tier * 0.13);
        const ry = scale * (0.27 + tier * 0.08);
        const tilt = tier === 0 ? -0.23 : 0.23;
        const ox = Math.cos(a) * rx;
        const oy = Math.sin(a) * ry;
        const tone = pickTone(rng, tier === 0 ? 0.45 : 0.18);
        targets.push({
          x: cx + ox * Math.cos(tilt) - oy * Math.sin(tilt),
          y: cy + ox * Math.sin(tilt) + oy * Math.cos(tilt),
          size: 0.8 + rng() * 1.0,
          r: tone.r, g: tone.g, b: tone.b, alpha: tier === 0 ? 0.78 : 0.52,
          role: 'orbit',
          cx, cy, angle: a, rx, ry, tilt, tier,
        });
      } else if (ringMode === 'vortex') {
        const tier = i % 3;
        const rad = scale * (0.44 + tier * 0.115) + (rng() - 0.5) * 9;
        const tone = pickTone(rng, tier === 0 ? 0.50 : 0.20);
        targets.push({
          x: cx + Math.cos(a) * rad,
          y: cy + Math.sin(a) * rad * 0.86,
          size: 0.8 + rng() * 1.05,
          r: tone.r, g: tone.g, b: tone.b, alpha: 0.78 - tier * 0.12,
          role: 'vortex',
          cx, cy, angle: a, rx: rad, ry: rad * 0.86, tier,
        });
      } else {
        const rad = scale * (0.48 + rng() * 0.38);
        targets.push({
          x: cx + Math.cos(a) * rad,
          y: cy + Math.sin(a) * rad * 0.74,
          size: 0.75 + rng() * 0.75,
          r: 110, g: 110, b: 120, alpha: 0.28,
          role: 'ambient',
        });
      }
    }
    return sortSpatially(targets, anchor, seed + 17);
  }

  /**
   * Ch 03: 5-Node Autonomous Graph Pipeline (Upstream -> Spec -> Dev -> Verify -> Audit + feedback arcs)
   */
  function sampleGraphShape(count, anchor, seed) {
    const rng = mulberry32(seed);
    const { cx, cy, scale } = anchor;
    const effSpan = vw < 900 ? Math.min(scale * 1.15, vw * 0.84) : Math.min(scale * 1.22, vw * 0.43);
    const labels = ['UPSTREAM', 'SPEC', 'DEV', 'VERIFY', 'AUDIT'];
    const nodeR = Math.max(13, Math.min(24, effSpan * 0.062));

    const nodes = [];
    for (let i = 0; i < 5; i++) {
      const frac = i / 4;
      nodes.push({
        x: cx - effSpan / 2 + frac * effSpan,
        y: cy - 18 + Math.sin(frac * Math.PI) * -36,
        r: nodeR,
        label: labels[i],
      });
    }
    geoGraphNodes = nodes;

    const targets = [];
    const perNode = Math.floor((count * 0.50) / 5);
    const edgeCount = Math.floor(count * 0.24);
    const arcCount = Math.floor(count * 0.18);

    for (let n = 0; n < 5; n++) {
      const nd = nodes[n];
      for (let i = 0; i < perNode; i++) {
        const a = (i / perNode) * Math.PI * 2 + rng() * 0.12;
        const isCore = i % 5 === 0;
        const rr = isCore ? rng() * nd.r * 0.42 : nd.r * (0.88 + rng() * 0.20);
        const tone = pickTone(rng, isCore ? 0.86 : 0.46);
        targets.push({
          x: nd.x + Math.cos(a) * rr,
          y: nd.y + Math.sin(a) * rr,
          size: isCore ? 1.4 : 0.9 + rng() * 0.85,
          r: tone.r, g: tone.g, b: tone.b, alpha: 0.88,
          role: 'shape',
        });
      }
    }

    for (let i = 0; i < edgeCount; i++) {
      const seg = i % 4;
      const a = nodes[seg];
      const b = nodes[seg + 1];
      const fT = (Math.floor(i / 4) / Math.max(1, edgeCount / 4)) % 1;
      const tone = pickTone(rng, 0.66);
      targets.push({
        x: lerp(a.x, b.x, fT),
        y: lerp(a.y, b.y, fT),
        size: 1.0 + rng() * 0.7,
        r: tone.r, g: tone.g, b: tone.b, alpha: 0.82,
        role: 'edgeFlow',
        seg, flowT: fT,
      });
    }

    for (let i = 0; i < arcCount; i++) {
      const outer = i % 2 === 0;
      const sN = outer ? nodes[4] : nodes[3];
      const eN = outer ? nodes[1] : nodes[2];
      const p0 = { x: sN.x, y: sN.y + nodeR };
      const p1 = { x: (sN.x + eN.x) / 2, y: cy + (outer ? scale * 0.42 : scale * 0.24) };
      const p2 = { x: eN.x, y: eN.y + nodeR };
      const fT = i / arcCount;
      const inv = 1 - fT;
      const tone = outer ? MONO_TONES[1] : MONO_TONES[2];
      targets.push({
        x: inv * inv * p0.x + 2 * inv * fT * p1.x + fT * fT * p2.x,
        y: inv * inv * p0.y + 2 * inv * fT * p1.y + fT * fT * p2.y,
        size: 0.85 + rng() * 0.8,
        r: tone.r, g: tone.g, b: tone.b, alpha: outer ? 0.76 : 0.58,
        role: 'arcFlow',
        flowT: fT, p0, p1, p2,
      });
    }

    while (targets.length < count) {
      targets.push({
        x: cx + (rng() - 0.5) * effSpan * 1.25,
        y: cy + (rng() - 0.5) * scale * 0.85,
        size: 0.75 + rng() * 0.65,
        r: 99, g: 99, b: 107, alpha: 0.26,
        role: 'ambient',
      });
    }
    return sortSpatially(targets, anchor, seed + 17);
  }

  /**
   * Ch 04: 3D Isometric In-Memory RDB Stacked Cylinders (SRS/ARC/SCH) + Checkmark Shield Emblem
   */
  function sampleRdbShape(count, anchor, seed) {
    const rng = mulberry32(seed);
    const { cx, cy, scale } = anchor;
    const targets = [];

    const dbCx = cx - scale * 0.13;
    const dbCy = cy - scale * 0.02;
    const rx = scale * 0.29;
    const ry = rx * 0.34;
    const cylH = scale * 0.54;
    const topY = dbCy - cylH / 2;

    const platterCount = Math.floor(count * 0.48);
    const ribCount = Math.floor(count * 0.22);
    const shieldCount = Math.floor(count * 0.22);

    for (let i = 0; i < platterCount; i++) {
      const tier = i % 4;
      let x, y, tone;
      if (tier === 0) {
        const a = rng() * Math.PI * 2;
        const isRim = i % 3 !== 0;
        const rf = isRim ? (0.95 + rng() * 0.06) : Math.sqrt(rng()) * 0.86;
        x = dbCx + Math.cos(a) * rx * rf;
        y = topY + Math.sin(a) * ry * rf;
        tone = pickTone(rng, isRim ? 0.76 : 0.26);
      } else {
        const frac = tier / 3;
        const a = rng() * Math.PI;
        x = dbCx + Math.cos(a) * rx;
        y = topY + cylH * frac + Math.sin(a) * ry;
        tone = pickTone(rng, 0.56);
      }
      targets.push({ x, y, size: 0.9 + rng() * 1.0, r: tone.r, g: tone.g, b: tone.b, alpha: 0.86, role: 'shape' });
    }

    const ribPos = [-1.0, -0.5, 0.0, 0.5, 1.0];
    for (let i = 0; i < ribCount; i++) {
      const rp = ribPos[i % ribPos.length];
      const isOuter = Math.abs(rp) === 1.0;
      const x = dbCx + rp * rx + (rng() - 0.5) * 1.5;
      const yOff = Math.sqrt(Math.max(0, 1 - rp * rp)) * ry;
      const y = topY + yOff + rng() * cylH;
      const tone = isOuter ? MONO_TONES[0] : MONO_TONES[2];
      targets.push({ x, y, size: isOuter ? 1.15 : 0.85, r: tone.r, g: tone.g, b: tone.b, alpha: isOuter ? 0.86 : 0.52, role: 'shape' });
    }

    // Checkmark Shield Emblem
    const sx = cx + scale * 0.27;
    const sy = cy + scale * 0.16;
    const sr = scale * 0.18;
    const verts = [
      { x: sx, y: sy - sr },
      { x: sx + sr * 0.84, y: sy - sr * 0.54 },
      { x: sx + sr * 0.72, y: sy + sr * 0.34 },
      { x: sx, y: sy + sr * 1.00 },
      { x: sx - sr * 0.72, y: sy + sr * 0.34 },
      { x: sx - sr * 0.84, y: sy - sr * 0.54 },
    ];
    for (let i = 0; i < shieldCount; i++) {
      const m = i % 5;
      let x, y;
      if (m < 3) {
        const seg = i % verts.length;
        const a = verts[seg];
        const b = verts[(seg + 1) % verts.length];
        const f = rng();
        x = lerp(a.x, b.x, f);
        y = lerp(a.y, b.y, f);
      } else if (m === 3) {
        const f = rng();
        x = sx - sr * 0.36 + f * sr * 0.26;
        y = sy + sr * 0.02 + f * sr * 0.28;
      } else {
        const f = rng();
        x = sx - sr * 0.10 + f * sr * 0.50;
        y = sy + sr * 0.30 - f * sr * 0.56;
      }
      targets.push({ x, y, size: 1.05 + rng() * 0.8, r: 245, g: 245, b: 247, alpha: 0.92, role: 'shape' });
    }

    while (targets.length < count) {
      targets.push({
        x: cx + (rng() - 0.5) * scale * 1.2,
        y: cy + (rng() - 0.5) * scale * 0.85,
        size: 0.75 + rng() * 0.65,
        r: 99, g: 99, b: 107, alpha: 0.26,
        role: 'ambient',
      });
    }
    return sortSpatially(targets, anchor, seed + 17);
  }

  /**
   * Ch 05: AST Tree Nodes + Hexagonal Semantic Verification Shield + 'AST' Core Label
   */
  function sampleAstShape(count, anchor, seed) {
    const rng = mulberry32(seed);
    const { cx, cy, scale } = anchor;
    const targets = [];

    const rawText = sampleTextShape('AST', Math.floor(count * 0.34), {
      cx, cy: cy - scale * 0.14, scale: scale * 0.50,
    }, 'none', seed + 3);
    for (let i = 0; i < rawText.length; i++) {
      if (rawText[i].role === 'text') targets.push(rawText[i]);
    }

    const hexR = scale * 0.48;
    const verts = [];
    for (let k = 0; k < 6; k++) {
      const a = -Math.PI / 2 + (k * Math.PI) / 3;
      verts.push({ x: cx + Math.cos(a) * hexR, y: cy + Math.sin(a) * hexR });
    }

    const hexCount = Math.floor(count * 0.34);
    for (let i = 0; i < hexCount; i++) {
      const seg = i % 6;
      const a = verts[seg];
      const b = verts[(seg + 1) % 6];
      const f = rng();
      const inner = i % 4 === 0 ? 0.88 : 1.0;
      const px = cx + (lerp(a.x, b.x, f) - cx) * inner;
      const py = cy + (lerp(a.y, b.y, f) - cy) * inner;
      const tone = inner === 1.0 ? pickTone(rng, 0.70) : MONO_TONES[2];
      targets.push({ x: px, y: py, size: inner === 1.0 ? 1.15 : 0.8, r: tone.r, g: tone.g, b: tone.b, alpha: inner === 1.0 ? 0.86 : 0.46, role: 'shape' });
    }

    const root = { x: cx, y: cy + scale * 0.04 };
    const mids = [
      { x: cx - scale * 0.18, y: cy + scale * 0.18 },
      { x: cx + scale * 0.18, y: cy + scale * 0.18 },
    ];
    const leaves = [
      { x: cx - scale * 0.28, y: cy + scale * 0.32 },
      { x: cx - scale * 0.08, y: cy + scale * 0.32 },
      { x: cx + scale * 0.08, y: cy + scale * 0.32 },
      { x: cx + scale * 0.28, y: cy + scale * 0.32 },
    ];
    const edges = [
      [root, mids[0]], [root, mids[1]],
      [mids[0], leaves[0]], [mids[0], leaves[1]],
      [mids[1], leaves[2]], [mids[1], leaves[3]],
    ];
    const allNodes = [root, ...mids, ...leaves];

    const treeCount = Math.floor(count * 0.26);
    for (let i = 0; i < treeCount; i++) {
      if (i % 3 === 0) {
        const nd = allNodes[i % allNodes.length];
        const a = rng() * Math.PI * 2;
        const r = rng() * 8.5;
        targets.push({ x: nd.x + Math.cos(a) * r, y: nd.y + Math.sin(a) * r, size: 1.25, r: 245, g: 245, b: 247, alpha: 0.92, role: 'shape' });
      } else {
        const br = edges[i % edges.length];
        const f = rng();
        targets.push({
          x: lerp(br[0].x, br[1].x, f),
          y: lerp(br[0].y, br[1].y, f),
          size: 0.95, r: 192, g: 192, b: 200, alpha: 0.76, role: 'shape',
        });
      }
    }

    while (targets.length < count) {
      const v = verts[targets.length % 6];
      const a = rng() * Math.PI * 2;
      const r = 8 + rng() * 24;
      targets.push({ x: v.x + Math.cos(a) * r, y: v.y + Math.sin(a) * r, size: 0.75, r: 99, g: 99, b: 107, alpha: 0.28, role: 'ambient' });
    }
    return sortSpatially(targets, anchor, seed + 17);
  }

  /**
   * Ch 06: Concentric Research Radar Rings + 'DOCS' Core Typography + Sweep Pulse
   */
  function sampleRadarShape(count, anchor, seed) {
    const rng = mulberry32(seed);
    const { cx, cy, scale } = anchor;
    const targets = [];

    const rawText = sampleTextShape('DOCS', Math.floor(count * 0.36), {
      cx, cy, scale: scale * 0.54,
    }, 'none', seed + 5);
    for (let i = 0; i < rawText.length; i++) {
      if (rawText[i].role === 'text') targets.push(rawText[i]);
    }

    const outerR = scale * 0.50;
    geoRadar = { cx, cy, r: outerR };

    const ringCount = Math.floor(count * 0.44);
    for (let i = 0; i < ringCount; i++) {
      const tier = i % 3;
      const a = (i / ringCount) * Math.PI * 2 * 3;
      const rad = outerR * (0.60 + tier * 0.20);
      const tone = pickTone(rng, tier === 2 ? 0.60 : 0.24);
      targets.push({
        x: cx + Math.cos(a) * rad,
        y: cy + Math.sin(a) * rad,
        size: 0.85 + rng() * 0.85,
        r: tone.r, g: tone.g, b: tone.b, alpha: 0.78 - (2 - tier) * 0.14,
        role: 'radarRing',
        cx, cy, angle: a, rx: rad, ry: rad, tier,
      });
    }

    const tickCount = Math.floor(count * 0.14);
    for (let i = 0; i < tickCount; i++) {
      const dir = i % 4;
      const ang = (dir * Math.PI) / 2;
      const rad = outerR * (0.94 + rng() * 0.22);
      targets.push({
        x: cx + Math.cos(ang) * rad + (rng() - 0.5) * 2.2,
        y: cy + Math.sin(ang) * rad + (rng() - 0.5) * 2.2,
        size: 1.05, r: 245, g: 245, b: 247, alpha: 0.84, role: 'shape',
      });
    }

    while (targets.length < count) {
      const a = rng() * Math.PI * 2;
      const rad = outerR * (1.06 + rng() * 0.28);
      targets.push({ x: cx + Math.cos(a) * rad, y: cy + Math.sin(a) * rad, size: 0.75, r: 99, g: 99, b: 107, alpha: 0.26, role: 'ambient' });
    }
    return sortSpatially(targets, anchor, seed + 17);
  }

  /**
   * Ambient dispersed monochrome sand field behind #interactive-lab
   */
  function sampleLabField(count, seed) {
    const rng = mulberry32(seed);
    const pts = [];
    for (let i = 0; i < count; i++) {
      const tone = pickTone(rng, 0.22);
      pts.push({
        x: rng() * vw,
        y: rng() * vh,
        size: 0.75 + rng() * 1.1,
        r: tone.r, g: tone.g, b: tone.b, alpha: 0.44,
        role: 'ambient',
      });
    }
    return sortSpatially(pts, { cx: vw * 0.5, cy: vh * 0.5, scale: Math.max(vw, vh) }, seed + 19);
  }

  function precomputeShapes() {
    const count = sandParticles.length;
    shapeAnchors = CHAPTER_META.map((_, i) => getAnchorForChapter(i));
    shapes = [
      sampleTextShape('IRADE', count, shapeAnchors[0], 'orbit', 101),
      sampleTextShape('QED', count, shapeAnchors[1], 'vortex', 202),
      sampleGraphShape(count, shapeAnchors[2], 303),
      sampleRdbShape(count, shapeAnchors[3], 404),
      sampleAstShape(count, shapeAnchors[4], 505),
      sampleRadarShape(count, shapeAnchors[5], 606),
    ];
    labField = sampleLabField(count, 707);

    if (customTextValue) {
      const nearest = clamp(Math.round(smoothedP), 0, 5);
      const anchor = smoothedLabBlend > 0.35
        ? { cx: vw * 0.5, cy: vh * 0.45, scale: Math.min(vw * 0.48, vh * 0.50, 410) }
        : shapeAnchors[nearest];
      customShape = sampleTextShape(customTextValue, count, anchor, 'orbit', 909);
    }
  }

  function initParticles() {
    const count = getParticleCount(vw);
    const rng = mulberry32(4242);

    sandParticles = [];
    for (let i = 0; i < count; i++) {
      sandParticles.push({
        x: vw * 0.72,
        y: vh * 0.50,
        px: vw * 0.72,
        py: vh * 0.50,
        vx: 0,
        vy: 0,
        r: 235, g: 235, b: 240, alpha: 0.85,
        size: 1.0,
        phase: rng() * 0.38,                 // deterministic stagger [0..0.38]
        arcHeight: (rng() * 2 - 1) * 115,    // curved sand arc amplitude
        windAmp: 46 + rng() * 98,            // horizontal sand wind sweep
        noiseSeed: rng() * Math.PI * 20,
        shimmerPhase: rng() * Math.PI * 2,
      });
    }

    // 120 ambient drifting fine monochrome dust grains
    dustParticles = [];
    for (let i = 0; i < 120; i++) {
      dustParticles.push({
        x: rng() * vw,
        y: rng() * vh,
        vx: 0.14 + rng() * 0.32,
        vy: (rng() - 0.5) * 0.18,
        size: 0.55 + rng() * 0.95,
        alpha: 0.10 + rng() * 0.22,
        phase: rng() * Math.PI * 2,
      });
    }

    precomputeShapes();

    // Position immediately near Chapter 01 ('IRADE' + orbital ring) so y = 0 renders cleanly on frame 1
    const s0 = shapes[0];
    for (let i = 0; i < sandParticles.length; i++) {
      const p = sandParticles[i];
      const t = s0[i];
      p.x = t.x;
      p.y = t.y;
      p.px = t.x;
      p.py = t.y;
      p.r = t.r;
      p.g = t.g;
      p.b = t.b;
      p.size = t.size;
      p.alpha = t.alpha;
    }
  }

  function getMorphSpan() {
    const trackH = scrollTrackEl ? scrollTrackEl.offsetHeight : vh * 9;
    return Math.max(1, trackH - vh);
  }

  function readScrollTargets() {
    const y = window.scrollY || 0;
    const trackTop = scrollTrackEl ? scrollTrackEl.offsetTop : 0;
    const morphSpan = getMorphSpan();
    const relY = y - trackTop;

    const maxDoc = Math.max(1, document.documentElement.scrollHeight - vh);
    const docPct = clamp((y / maxDoc) * 100, 0, 100);
    const topFill = document.getElementById('top-progress-fill');
    if (topFill) topFill.style.width = `${docPct.toFixed(2)}%`;

    if (scrollSync) {
      rawP = clamp((relY / morphSpan) * 5, 0, 5);
      rawLabBlend = clamp((relY - morphSpan) / (vh * 0.72), 0, 1);
    }
  }

  function resizeCanvasSafely() {
    vw = Math.max(300, window.innerWidth || 1024);
    vh = Math.max(300, window.innerHeight || 768);
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.floor(vw * dpr);
    canvas.height = Math.floor(vh * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    if (sandParticles.length !== getParticleCount(vw)) {
      initParticles();
    } else {
      precomputeShapes();
    }
    readScrollTargets();
  }

  function evalTargetPoint(pt, tSec) {
    let x = pt.x;
    let y = pt.y;
    if (pt.role === 'orbit') {
      const dir = pt.tier === 0 ? 1 : -1;
      const a = pt.angle + tSec * 0.25 * dir;
      const ox = Math.cos(a) * pt.rx;
      const oy = Math.sin(a) * pt.ry;
      x = pt.cx + ox * Math.cos(pt.tilt) - oy * Math.sin(pt.tilt);
      y = pt.cy + ox * Math.sin(pt.tilt) + oy * Math.cos(pt.tilt);
    } else if (pt.role === 'vortex') {
      const pulse = 1 + Math.sin(tSec * 2.0 - pt.tier * 1.0) * 0.055;
      const a = pt.angle + tSec * (pt.tier % 2 === 0 ? 0.28 : -0.22);
      x = pt.cx + Math.cos(a) * pt.rx * pulse;
      y = pt.cy + Math.sin(a) * pt.ry * pulse;
    } else if (pt.role === 'edgeFlow' && geoGraphNodes.length === 5) {
      const fT = (pt.flowT + tSec * 0.22) % 1;
      const a = geoGraphNodes[pt.seg];
      const b = geoGraphNodes[pt.seg + 1];
      x = lerp(a.x, b.x, fT);
      y = lerp(a.y, b.y, fT);
    } else if (pt.role === 'arcFlow' && pt.p0) {
      const fT = (pt.flowT + tSec * 0.16) % 1;
      const inv = 1 - fT;
      x = inv * inv * pt.p0.x + 2 * inv * fT * pt.p1.x + fT * fT * pt.p2.x;
      y = inv * inv * pt.p0.y + 2 * inv * fT * pt.p1.y + fT * fT * pt.p2.y;
    } else if (pt.role === 'radarRing') {
      const a = pt.angle + tSec * (0.16 - pt.tier * 0.04);
      x = pt.cx + Math.cos(a) * pt.rx;
      y = pt.cy + Math.sin(a) * pt.ry;
    }
    return { x, y };
  }

  function updateReversibleChrome(idxA, idxB, morphT, labFade) {
    const isInLab = smoothedLabBlend > 0.35;
    document.body.classList.toggle('in-lab', isInLab);

    // Symmetric fixed editorial text panels driven by dist = Math.abs(smoothedP - i)
    for (let i = 0; i < chapterPanels.length; i++) {
      const panel = chapterPanels[i];
      const delta = smoothedP - i;
      const dist = Math.abs(delta);

      if (dist > 0.48 || labFade <= 0.01) {
        panel.style.opacity = '0';
        panel.style.visibility = 'hidden';
        panel.style.pointerEvents = 'none';
        const yOff = delta > 0 ? -24 : 24;
        panel.style.transform = vw >= 900
          ? `translate3d(0, calc(-50% + ${yOff}px), 0)`
          : `translate3d(0, ${yOff}px, 0)`;
      } else {
        const vis = (1 - smoothstep(0.10, 0.48, dist)) * labFade;
        const yOff = clamp(-delta * 54, -24, 24);
        panel.style.opacity = vis.toFixed(3);
        panel.style.visibility = 'visible';
        panel.style.pointerEvents = vis > 0.55 && !focusView ? 'auto' : 'none';
        panel.style.transform = vw >= 900
          ? `translate3d(0, calc(-50% + ${yOff.toFixed(1)}px), 0)`
          : `translate3d(0, ${(yOff * 0.5).toFixed(1)}px, 0)`;
      }
    }

    // Directional vignette behind text
    if (vw >= 900 && !focusView) {
      const cosV = Math.cos(smoothedP * Math.PI);
      const leftW = Math.max(0, cosV) * labFade;
      const rightW = Math.max(0, -cosV) * labFade;
      if (leftW > 0.02) {
        vignetteEl.style.background = `linear-gradient(90deg, rgba(5,5,6,${(0.88 * leftW).toFixed(2)}) 0%, rgba(5,5,6,${(0.48 * leftW).toFixed(2)}) 38%, rgba(5,5,6,0) 60%)`;
      } else if (rightW > 0.02) {
        vignetteEl.style.background = `linear-gradient(270deg, rgba(5,5,6,${(0.88 * rightW).toFixed(2)}) 0%, rgba(5,5,6,${(0.48 * rightW).toFixed(2)}) 38%, rgba(5,5,6,0) 60%)`;
      } else {
        vignetteEl.style.background = 'none';
      }
    } else {
      vignetteEl.style.background = 'none';
    }

    // Top Header Status Badge & Progress
    const pad2 = (n) => String(n).padStart(2, '0');
    const nearest = clamp(Math.round(smoothedP), 0, 5);
    const pctTrack = (smoothedP / 5) * 100;

    const chNumEl = document.getElementById('header-ch-num');
    const morphBadge = document.getElementById('header-morph-badge');
    const miniFill = document.getElementById('header-mini-fill');

    if (customBlend > 0.25 && customTextValue) {
      chNumEl.textContent = 'CUSTOM';
      morphBadge.textContent = `MORPH "${customTextValue}"`;
      miniFill.style.width = `${pctTrack.toFixed(1)}%`;
    } else if (isInLab) {
      chNumEl.textContent = 'LAB';
      morphBadge.textContent = 'LAB MODE';
      miniFill.style.width = '100%';
    } else {
      chNumEl.textContent = `${pad2(nearest + 1)} / 06`;
      miniFill.style.width = `${pctTrack.toFixed(1)}%`;

      if (morphT > 0.02 && morphT < 0.98) {
        const pct = Math.round(morphT * 100);
        if (smoothDir < -0.12) {
          morphBadge.textContent = `${pad2(idxB + 1)} → ${pad2(idxA + 1)} REVERT ${100 - pct}%`;
        } else {
          morphBadge.textContent = `${pad2(idxA + 1)} → ${pad2(idxB + 1)} MORPH ${pct}%`;
        }
      } else {
        const locked = morphT >= 0.98 ? idxB : idxA;
        morphBadge.textContent = `${pad2(locked + 1)} LOCKED`;
      }
    }

    // Bottom HUD scrubber & chapter highlight
    const scrubFill = document.getElementById('hud-scrub-fill');
    const scrubDot = document.getElementById('hud-scrub-dot');
    if (scrubFill && scrubDot) {
      scrubFill.style.width = `${pctTrack.toFixed(1)}%`;
      scrubDot.style.left = `${pctTrack.toFixed(1)}%`;
    }

    hudChapterBtns.forEach((btn) => {
      const idx = Number(btn.dataset.jump);
      btn.classList.toggle('active', !isInLab && idx === nearest);
    });
  }

  function animate(now) {
    requestAnimationFrame(animate);
    const tSec = now * 0.001;

    const currY = window.scrollY || 0;
    const deltaY = clamp(currY - lastScrollY, -75, 75);
    lastScrollY = currY;
    scrollVel = scrollVel * 0.80 + deltaY * 0.26;

    // Smooth continuous reversible scrubbing (factor 0.065)
    const pDiff = rawP - smoothedP;
    smoothedP += pDiff * 0.065;
    if (Math.abs(pDiff) > 0.0007) {
      smoothDir = lerp(smoothDir, clamp(pDiff * 24, -1, 1), 0.14);
    }

    smoothedLabBlend += (rawLabBlend - smoothedLabBlend) * 0.065;
    customBlend += (customBlendTarget - customBlend) * 0.065;

    scatterGust *= 0.93;
    if (scatterGust < 0.004) scatterGust = 0;

    const clampedP = clamp(smoothedP, 0, 4.9999);
    const idxA = Math.floor(clampedP);
    const idxB = Math.min(idxA + 1, 5);
    const rawT = clampedP - idxA;
    // Rest plateau at chapter peaks [0..0.14] and [0.86..1.0] for crisp reading
    const t = smoothstep(0.14, 0.86, rawT);

    const sideSign = idxA % 2 === 0 ? -1 : 1;
    const dirSign = sideSign * (smoothDir >= 0 ? 1 : -1);
    const labFade = 1 - smoothstep(0.02, 0.42, smoothedLabBlend);

    updateReversibleChrome(idxA, idxB, t, labFade);

    ctx.clearRect(0, 0, vw, vh);

    // 1. 120 Ambient Drifting Monochrome Dust Grains
    for (let i = 0; i < dustParticles.length; i++) {
      const d = dustParticles[i];
      const wx = d.vx + dirSign * Math.abs(pDiff) * 26 + scatterDirX * scatterGust * 3.4;
      const wy = d.vy - scrollVel * 0.14 + Math.sin(tSec * 1.1 + d.phase) * 0.2;
      d.x = (d.x + wx + vw) % vw;
      d.y = (d.y + wy + vh) % vh;
      ctx.fillStyle = `rgba(228, 228, 231, ${d.alpha})`;
      ctx.beginPath();
      ctx.arc(d.x, d.y, d.size, 0, Math.PI * 2);
      ctx.fill();
    }

    // 2. Subtle Vector Overlays for Ch 03 (Graph) & Ch 06 (Radar)
    if (customBlend < 0.25) {
      const cFade = 1 - customBlend * 4;
      const graphVis = clamp(1 - Math.abs(smoothedP - 2) * 2.4, 0, 1) * labFade * cFade;
      if (graphVis > 0.02 && geoGraphNodes.length === 5) {
        ctx.save();
        ctx.strokeStyle = `rgba(255, 255, 255, ${(0.15 * graphVis).toFixed(3)})`;
        ctx.lineWidth = 1;
        for (let i = 0; i < 4; i++) {
          ctx.beginPath();
          ctx.moveTo(geoGraphNodes[i].x, geoGraphNodes[i].y);
          ctx.lineTo(geoGraphNodes[i + 1].x, geoGraphNodes[i + 1].y);
          ctx.stroke();
        }
        ctx.font = '600 9.5px ui-monospace, SFMono-Regular, Menlo, monospace';
        ctx.textAlign = 'center';
        ctx.fillStyle = `rgba(245, 245, 247, ${(0.72 * graphVis).toFixed(3)})`;
        for (let i = 0; i < 5; i++) {
          const nd = geoGraphNodes[i];
          ctx.fillText(nd.label, nd.x, nd.y - nd.r - 10);
        }
        ctx.restore();
      }

      const radarVis = clamp(1 - Math.abs(smoothedP - 5) * 2.4, 0, 1) * labFade * cFade;
      if (radarVis > 0.02 && geoRadar) {
        ctx.save();
        const sweep = tSec * 1.15;
        const g = ctx.createRadialGradient(geoRadar.cx, geoRadar.cy, 6, geoRadar.cx, geoRadar.cy, geoRadar.r);
        g.addColorStop(0, 'rgba(255, 255, 255, 0)');
        g.addColorStop(1, `rgba(255, 255, 255, ${(0.16 * radarVis).toFixed(3)})`);
        ctx.fillStyle = g;
        ctx.beginPath();
        ctx.moveTo(geoRadar.cx, geoRadar.cy);
        ctx.arc(geoRadar.cx, geoRadar.cy, geoRadar.r, sweep, sweep + 0.56);
        ctx.closePath();
        ctx.fill();
        ctx.restore();
      }
    }

    // 3. Reversible Staggered Sand-Wind Physics & Streak Rendering
    const shapeA = shapes[idxA];
    const shapeB = shapes[idxB];
    if (!shapeA || !shapeB) return;

    const repR = pointer.radius;
    const repR2 = repR * repR;

    ctx.save();
    ctx.globalCompositeOperation = 'lighter';
    ctx.lineCap = 'round';

    for (let k = 0; k < sandParticles.length; k++) {
      const p = sandParticles[k];
      const ptA = shapeA[k];
      const ptB = shapeB[k];

      // Per-particle staggered local progress u -> smoothstep e
      const u = clamp((t - p.phase) / (1 - 0.38), 0, 1);
      const e = u * u * (3 - 2 * u);
      // Mid-flight arc peaks at e = 0.5, returns cleanly to 0 at e = 0 or e = 1
      const flight = Math.sin(e * Math.PI);

      const posA = evalTargetPoint(ptA, tSec);
      const posB = evalTargetPoint(ptB, tSec);

      let bx = lerp(posA.x, posB.x, e);
      let by = lerp(posA.y, posB.y, e);
      let tr = lerp(ptA.r, ptB.r, e);
      let tg = lerp(ptA.g, ptB.g, e);
      let tb = lerp(ptA.b, ptB.b, e);
      let tSize = lerp(ptA.size, ptB.size, e);
      let tAlpha = lerp(ptA.alpha, ptB.alpha, e);

      // Aeolian diagonal sand-wind arc + 2D trigonometric turbulence
      let swirlX = flight * (Math.cos(p.noiseSeed + tSec * 1.4) * p.windAmp * 0.55 + dirSign * p.windAmp * 0.45);
      let swirlY = flight * (p.arcHeight + Math.sin(p.noiseSeed * 1.6 + tSec * 1.8) * 46) - scrollVel * flight * 1.5;
      let totalFlight = flight;

      // Reversible dispersion into ambient sand field when entering #interactive-lab
      if (smoothedLabBlend > 0.002 && labField[k]) {
        const lu = clamp((smoothedLabBlend - p.phase * 0.65) / 0.75, 0, 1);
        const le = lu * lu * (3 - 2 * lu);
        const lf = Math.sin(le * Math.PI);
        const lp = labField[k];
        bx = lerp(bx, lp.x + Math.sin(tSec * 0.45 + p.noiseSeed) * 18, le);
        by = lerp(by, lp.y + Math.cos(tSec * 0.38 + p.shimmerPhase) * 14, le);
        swirlX += lf * Math.cos(p.noiseSeed + tSec * 1.3) * 65;
        swirlY += lf * p.arcHeight * 0.65;
        tr = lerp(tr, lp.r, le);
        tg = lerp(tg, lp.g, le);
        tb = lerp(tb, lp.b, le);
        tAlpha = lerp(tAlpha, lp.alpha, le);
        totalFlight = Math.max(totalFlight, lf);
      }

      // Reversible custom text morph override
      if (customBlend > 0.002 && customShape && customShape[k]) {
        const cu = clamp((customBlend - p.phase * 0.5) / 0.81, 0, 1);
        const ce = cu * cu * (3 - 2 * cu);
        const cf = Math.sin(ce * Math.PI);
        const cp = evalTargetPoint(customShape[k], tSec);
        bx = lerp(bx, cp.x, ce);
        by = lerp(by, cp.y, ce);
        swirlX += cf * Math.cos(p.noiseSeed + tSec * 1.5) * 68;
        swirlY += cf * p.arcHeight * 0.7;
        tr = lerp(tr, customShape[k].r, ce);
        tg = lerp(tg, customShape[k].g, ce);
        tb = lerp(tb, customShape[k].b, ce);
        tAlpha = lerp(tAlpha, customShape[k].alpha, ce);
        totalFlight = Math.max(totalFlight, cf);
      }

      const idleAmp = (1 - totalFlight) * 1.05;
      const tx = bx + swirlX + Math.sin(tSec * 1.3 + p.shimmerPhase) * idleAmp;
      const ty = by + swirlY + Math.cos(tSec * 1.1 + p.shimmerPhase) * idleAmp;

      // Record previous coordinate for crisp velocity wind streaks
      p.px = p.x;
      p.py = p.y;

      const stiffness = lerp(0.062, 0.036, totalFlight) * Math.max(0.28, 1 - scatterGust * 0.55);
      p.vx += (tx - p.x) * stiffness;
      p.vy += (ty - p.y) * stiffness;

      // Gentle mouse / touch repulsion
      if (pointer.active) {
        const dx = p.x - pointer.x;
        const dy = p.y - pointer.y;
        const d2 = dx * dx + dy * dy;
        if (d2 < repR2 && d2 > 0.01) {
          const dist = Math.sqrt(d2);
          const f = ((repR - dist) / repR) * 3.8;
          const nx = dx / dist;
          const ny = dy / dist;
          p.vx += (nx * 0.88 - ny * 0.28) * f;
          p.vy += (ny * 0.88 + nx * 0.28) * f;
        }
      }

      p.vx *= 0.835;
      p.vy *= 0.835;
      p.x += p.vx;
      p.y += p.vy;

      p.r += (tr - p.r) * 0.14;
      p.g += (tg - p.g) * 0.14;
      p.b += (tb - p.b) * 0.14;
      p.size += (tSize - p.size) * 0.14;
      p.alpha += (tAlpha - p.alpha) * 0.14;

      const speed = Math.hypot(p.x - p.px, p.y - p.py);
      if (speed > 1.9) {
        // Thin luminous monochrome wind streak from previous to current position
        const stretch = Math.min(3.4, 1.3 + speed * 0.12);
        ctx.strokeStyle = `rgba(${p.r | 0}, ${p.g | 0}, ${p.b | 0}, ${Math.min(0.95, p.alpha + 0.12)})`;
        ctx.lineWidth = Math.max(0.75, p.size * 0.86);
        ctx.beginPath();
        ctx.moveTo(p.x, p.y);
        ctx.lineTo(p.x - (p.x - p.px) * stretch, p.y - (p.y - p.py) * stretch);
        ctx.stroke();
      } else {
        ctx.fillStyle = `rgba(${p.r | 0}, ${p.g | 0}, ${p.b | 0}, ${p.alpha})`;
        ctx.beginPath();
        ctx.arc(p.x, p.y, p.size, 0, Math.PI * 2);
        ctx.fill();
      }
    }
    ctx.restore();
  }

  function triggerScatter() {
    scatterDirX = scatterDirX >= 0 ? -1 : 1;
    scatterGust = 1.5;
    for (let i = 0; i < sandParticles.length; i++) {
      const p = sandParticles[i];
      const f = 11 + (p.windAmp / 145) * 15;
      p.vx += scatterDirX * f;
      p.vy += Math.sin(p.y * 0.014 + p.noiseSeed) * 7.5 - 2.2;
    }
  }

  function jumpToChapter(chIdx) {
    const idx = clamp(chIdx, 0, 5);
    const destY = scrollTrackEl.offsetTop + (idx / 5) * getMorphSpan();
    scrollSync = true;
    customTextValue = null;
    customBlendTarget = 0;
    document.getElementById('hud-morph-input').value = '';
    updateSyncButton();
    window.scrollTo({ top: destY, behavior: 'smooth' });
  }

  // Window pointer & scroll listeners
  window.addEventListener('scroll', readScrollTargets, { passive: true });
  window.addEventListener('mousemove', (e) => {
    pointer.x = e.clientX;
    pointer.y = e.clientY;
    pointer.active = true;
  }, { passive: true });
  window.addEventListener('mouseout', (e) => {
    if (!e.relatedTarget) pointer.active = false;
  });
  window.addEventListener('touchstart', (e) => {
    if (e.touches.length) {
      pointer.x = e.touches[0].clientX;
      pointer.y = e.touches[0].clientY;
      pointer.active = true;
    }
  }, { passive: true });
  window.addEventListener('touchmove', (e) => {
    if (e.touches.length) {
      pointer.x = e.touches[0].clientX;
      pointer.y = e.touches[0].clientY;
      pointer.active = true;
    }
  }, { passive: true });
  window.addEventListener('touchend', () => { pointer.active = false; }, { passive: true });

  /* =========================================================================
   * 2. HEADER & BOTTOM HUD INTERACTIONS
   * ========================================================================= */
  document.querySelectorAll('[data-jump]').forEach((btn) => {
    btn.addEventListener('click', () => jumpToChapter(Number(btn.dataset.jump)));
  });
  document.getElementById('btn-top-story').addEventListener('click', () => jumpToChapter(0));
  document.getElementById('brand-home').addEventListener('click', (e) => {
    e.preventDefault();
    jumpToChapter(0);
  });

  const syncBtn = document.getElementById('hud-sync-btn');
  function updateSyncButton() {
    syncBtn.textContent = scrollSync ? '● 스크롤 연동 ON' : '○ 스크롤 연동 OFF';
    syncBtn.classList.toggle('on', scrollSync);
  }

  syncBtn.addEventListener('click', () => {
    scrollSync = !scrollSync;
    updateSyncButton();
    if (scrollSync) {
      customTextValue = null;
      customBlendTarget = 0;
      document.getElementById('hud-morph-input').value = '';
      readScrollTargets();
    }
  });

  document.getElementById('hud-morph-form').addEventListener('submit', (e) => {
    e.preventDefault();
    const val = document.getElementById('hud-morph-input').value.trim().slice(0, 8);
    if (!val) return;
    customTextValue = val;
    scrollSync = false;
    updateSyncButton();

    const nearest = clamp(Math.round(smoothedP), 0, 5);
    const anchor = smoothedLabBlend > 0.35
      ? { cx: vw * 0.5, cy: vh * 0.45, scale: Math.min(vw * 0.48, vh * 0.50, 410) }
      : shapeAnchors[nearest];
    customShape = sampleTextShape(val, sandParticles.length, anchor, 'orbit', 909 + val.length * 17);
    customBlendTarget = 1;
  });

  document.getElementById('hud-scatter-btn').addEventListener('click', triggerScatter);

  const focusBtn = document.getElementById('hud-focus-btn');
  const focusExitPill = document.getElementById('focus-exit-pill');
  function setFocusView(enabled) {
    focusView = enabled;
    document.body.classList.toggle('focus-view', focusView);
    focusBtn.textContent = focusView ? '● 집중 ON' : '입자 집중';
    focusBtn.classList.toggle('solid-on', focusView);
  }
  focusBtn.addEventListener('click', () => setFocusView(!focusView));
  focusExitPill.addEventListener('click', () => setFocusView(false));
  window.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && focusView) setFocusView(false);
  });

  /* =========================================================================
   * 8. SAFE BOOTSTRAP & RESIZE OBSERVER
   * ========================================================================= */
  let booted = false;
  function boot() {
    if (booted) return;
    booted = true;
    resizeCanvasSafely();
    readScrollTargets();
    smoothedP = rawP;
    smoothedLabBlend = rawLabBlend;
    requestAnimationFrame(animate);
  }

  let resizeTimer = null;
  function scheduleResize() {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(resizeCanvasSafely, 120);
  }

  window.addEventListener('resize', scheduleResize);
  if (typeof ResizeObserver !== 'undefined') {
    try {
      const ro = new ResizeObserver(scheduleResize);
      ro.observe(document.documentElement);
    } catch (_e) {
      // Fallback to window resize listener already attached
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
  
