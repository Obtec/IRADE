"""VLM objective-defect screening (DOC.md §5.5).

Playwright captures desktop + mobile screenshots and the accessibility tree; a
cheap vision model checks only 4 objective defects. Deterministic DOM checks
(broken images, contrast, overflow) run in-page first and are merged.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Callable

DEFECTS = ("TEXT_CLIPPED", "OVERLAP", "BROKEN_IMAGE", "LOW_CONTRAST")
VIEWPORTS = {"desktop": (1440, 900), "mobile": (390, 844)}
# vlm(prompt, images_b64: list[str], schema) -> dict
VisionModel = Callable[[str, list[str], dict], dict]

SCHEMA = {"type": "object", "properties": {"defects": {"type": "array", "items": {"type": "object", "properties": {
    "type": {"enum": list(DEFECTS)}, "description": {"type": "string"}, "selector": {"type": "string"}}}}},
    "required": ["defects"]}
PROMPT = ("스크린샷에서 다음 객관적 결함만 보고하세요: TEXT_CLIPPED(텍스트 잘림), OVERLAP(요소 겹침), "
          "BROKEN_IMAGE(깨진 이미지), LOW_CONTRAST(명도 대비 미달). 디자인 취향·주관적 평가는 금지.")

DOM_CHECK_JS = r"""
() => {
  const out = [];
  const lum = c => { const m = c.match(/\d+(\.\d+)?/g); if (!m) return 1;
    const [r,g,b] = m.slice(0,3).map(v => { v = v/255; return v <= 0.03928 ? v/12.92 : ((v+0.055)/1.055)**2.4; });
    return 0.2126*r + 0.7152*g + 0.0722*b; };
  const bg = el => { while (el) { const c = getComputedStyle(el).backgroundColor;
    if (c && !c.endsWith(', 0)') && c !== 'transparent') return c; el = el.parentElement; } return 'rgb(255,255,255)'; };
  const sel = el => el.id ? '#' + el.id : el.tagName.toLowerCase() + (el.className && typeof el.className === 'string' ? '.' + el.className.trim().split(/\s+/).join('.') : '');
  document.querySelectorAll('img').forEach(i => { if (i.complete && i.naturalWidth === 0) out.push({type:'BROKEN_IMAGE', selector: sel(i), description: i.src}); });
  document.querySelectorAll('body *').forEach(el => {
    const s = getComputedStyle(el);
    if (!el.childNodes.length || ![...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim())) return;
    if ((s.overflow === 'hidden' || s.textOverflow === 'ellipsis') && el.scrollWidth > el.clientWidth + 1)
      out.push({type:'TEXT_CLIPPED', selector: sel(el), description: el.textContent.trim().slice(0,60)});
    const a = lum(s.color), b = lum(bg(el)); const ratio = (Math.max(a,b)+0.05)/(Math.min(a,b)+0.05);
    const large = parseFloat(s.fontSize) >= 24 || (parseFloat(s.fontSize) >= 18.66 && +s.fontWeight >= 700);
    if (ratio < (large ? 3 : 4.5)) out.push({type:'LOW_CONTRAST', selector: sel(el), description: ratio.toFixed(2)});
  });
  return out.slice(0, 50);
}
"""


@dataclass
class Capture:
    viewport: str
    png: bytes
    a11y: dict | None
    dom_defects: list[dict]


def capture(url: str) -> list[Capture]:
    from playwright.sync_api import sync_playwright  # installed in the worker image

    caps = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            for name, (w, h) in VIEWPORTS.items():
                page = browser.new_page(viewport={"width": w, "height": h})
                page.goto(url, wait_until="networkidle")
                try:
                    a11y = page.accessibility.snapshot()
                except Exception:
                    a11y = None
                caps.append(Capture(name, page.screenshot(full_page=True), a11y, page.evaluate(DOM_CHECK_JS)))
                page.close()
        finally:
            browser.close()
    return caps


def screen(captures: list[Capture], vlm: VisionModel | None) -> list[dict]:
    defects = []
    for c in captures:
        defects += [dict(d, viewport=c.viewport, source="dom") for d in c.dom_defects if d.get("type") in DEFECTS]
        if vlm:
            r = vlm(PROMPT, [base64.b64encode(c.png).decode()], SCHEMA)
            defects += [dict(d, viewport=c.viewport, source="vlm") for d in r.get("defects", [])
                        if d.get("type") in DEFECTS]
    seen, out = set(), []
    for d in defects:
        k = (d["viewport"], d["type"], d.get("selector", ""))
        if k not in seen:
            seen.add(k)
            out.append(d)
    return out
