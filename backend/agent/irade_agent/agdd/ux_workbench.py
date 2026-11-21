"""Interactive UX Workbench backend (DOC.md §5.5, REQ-004 / ARC-004 / SCH-004).

User edits from the live preview are stored declaratively in ``ux_overrides``.
``locked_by_user`` entries can never be overwritten by an agent; ``audit_patch``
is the Audit-node check that rejects agent code/config touching locked selectors.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Protocol


class OverrideStore(Protocol):
    def get(self, workspace_id: str, view_key: str, selector: str) -> dict | None: ...
    def put(self, row: dict) -> None: ...
    def delete(self, workspace_id: str, view_key: str, selector: str) -> bool: ...
    def list(self, workspace_id: str, view_key: str | None = None) -> list[dict]: ...


class MemoryOverrideStore:
    def __init__(self):
        self.rows: dict[tuple, dict] = {}

    def get(self, workspace_id, view_key, selector):
        r = self.rows.get((workspace_id, view_key, selector))
        return dict(r) if r else None

    def put(self, row):
        self.rows[(row["workspace_id"], row["view_key"], row["component_selector"])] = dict(row)

    def delete(self, workspace_id, view_key, selector) -> bool:
        return self.rows.pop((workspace_id, view_key, selector), None) is not None

    def list(self, workspace_id, view_key=None):
        return [dict(r) for (w, v, _), r in sorted(self.rows.items()) if w == workspace_id and (view_key in (None, v))]


class LockedOverrideError(PermissionError):
    pass


ALLOWED_KEYS = {"layout", "tokens", "visibility", "order", "size", "spacing", "variant", "props"}


def _validate_config(config: dict) -> None:
    if not isinstance(config, dict):
        raise ValueError("config_json must be an object")
    unknown = set(config) - ALLOWED_KEYS
    if unknown:
        raise ValueError(f"unknown config keys: {sorted(unknown)}")


@dataclass
class Workbench:
    store: OverrideStore

    def user_set(self, workspace_id, view_key, selector, config: dict, user: str, lock: bool = True) -> dict:
        _validate_config(config)
        row = {"workspace_id": workspace_id, "view_key": view_key, "component_selector": selector,
               "config_json": config, "locked_by_user": lock, "updated_by": user}
        self.store.put(row)
        return row

    def unlock(self, workspace_id, view_key, selector, user: str) -> None:
        row = self.store.get(workspace_id, view_key, selector)
        if row:
            row.update(locked_by_user=False, updated_by=user)
            self.store.put(row)

    def user_delete(self, workspace_id, view_key, selector) -> bool:
        return self.store.delete(workspace_id, view_key, selector)

    def agent_delete(self, workspace_id, view_key, selector) -> bool:
        cur = self.store.get(workspace_id, view_key, selector)
        if cur and cur.get("locked_by_user"):
            raise LockedOverrideError(f"{view_key}:{selector} is locked by user")
        return self.store.delete(workspace_id, view_key, selector)

    def agent_set(self, workspace_id, view_key, selector, config: dict, agent: str) -> dict:
        _validate_config(config)
        cur = self.store.get(workspace_id, view_key, selector)
        if cur and cur.get("locked_by_user"):
            raise LockedOverrideError(f"{view_key}:{selector} is locked by user")
        row = {"workspace_id": workspace_id, "view_key": view_key, "component_selector": selector,
               "config_json": config, "locked_by_user": False, "updated_by": agent}
        self.store.put(row)
        return row

    def export_bindings(self, workspace_id, view_key) -> dict:
        """Declarative binding file consumed by frontend code (ux-overrides.json)."""
        return {r["component_selector"]: r["config_json"] for r in self.store.list(workspace_id, view_key)}

    def import_bindings(self, workspace_id, view_key, bindings: dict, user: str) -> None:
        """Reverse sync: bindings edited in code flow back unless locked rows would change."""
        for sel, cfg in bindings.items():
            cur = self.store.get(workspace_id, view_key, sel)
            if cur and cur.get("locked_by_user") and cur["config_json"] != cfg:
                raise LockedOverrideError(f"{sel} is locked; code change rejected")
            self.user_set(workspace_id, view_key, sel, cfg, user, lock=bool(cur and cur.get("locked_by_user")))


@dataclass
class AuditResult:
    ok: bool
    blocked: list[str] = field(default_factory=list)


_BINDING_FILE = re.compile(r"ux-overrides\.json$")


def audit_patch(workbench: Workbench, workspace_id: str, view_key: str, unified_diff: str) -> AuditResult:
    """Block agent diffs that touch locked selectors (in source or the binding file)."""
    locked = [r["component_selector"] for r in workbench.store.list(workspace_id, view_key) if r.get("locked_by_user")]
    changed = [l[1:] for l in unified_diff.splitlines()
               if l[:1] in "+-" and not l.startswith(("+++", "---"))]
    blocked = sorted({sel for sel in locked for l in changed if sel in l})
    return AuditResult(not blocked, blocked)


def dumps(bindings: dict) -> str:
    return json.dumps(bindings, ensure_ascii=False, indent=2, sort_keys=True)


UX_REQ_RX = re.compile(r"<<AGDD_UX_REQUEST:(\{.*?\})>>", re.S)


def _clamp_int(val, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(round(float(val)))))
    except (TypeError, ValueError):
        return default


def normalize_ux_request(obj: dict | None) -> dict | None:
    """Validate and sanitize an agent-emitted 2D UX visual request."""
    if not isinstance(obj, dict):
        return None
    raw_view = str(obj.get("view_key") or "home").strip()
    view_key = raw_view if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", raw_view) else "home"
    prompt = str(obj.get("prompt") or "").strip()[:2000]
    vp = obj.get("viewport") if isinstance(obj.get("viewport"), dict) else {}
    width = _clamp_int(vp.get("width", 1200), 320, 3840, 1200)
    height = _clamp_int(vp.get("height", 720), 320, 2160, 720)
    elems = []
    raw_elems = obj.get("elements") if isinstance(obj.get("elements"), list) else []
    for i, el in enumerate(raw_elems[:40]):
        if not isinstance(el, dict):
            continue
        sel = str(el.get("selector") or f"#el-{i + 1}").strip()[:64] or f"#el-{i + 1}"
        elems.append({
            "selector": sel,
            "kind": str(el.get("kind") or "section").strip()[:32] or "section",
            "label": str(el.get("label") or sel).strip()[:120],
            "x": _clamp_int(el.get("x", 40 + (i % 3) * 260), 0, width, 40),
            "y": _clamp_int(el.get("y", 40 + (i // 3) * 140), 0, height, 40),
            "w": _clamp_int(el.get("w", 240), 24, width, 240),
            "h": _clamp_int(el.get("h", 120), 24, height, 120),
            "note": str(el.get("note") or "").strip()[:500],
        })
    return {
        "view_key": view_key,
        "prompt": prompt,
        "viewport": {"width": width, "height": height},
        "elements": elems,
    }


def parse_ux_request(text: str) -> dict | None:
    """Extract the latest ``<<AGDD_UX_REQUEST:{...}>>`` marker from CLI/model output."""
    matches = UX_REQ_RX.findall(text or "")
    for raw in reversed(matches):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        norm = normalize_ux_request(obj)
        if norm is not None:
            return norm
    return None


def summarize_2d_layout(
    bindings: dict,
    locked_selectors: list[str] | set[str] = (),
    viewport: dict | None = None,
) -> dict:
    """Convert raw ``ux_overrides`` bindings into a structured 2D spatial layout + relations for agents."""
    vp_w = _clamp_int((viewport or {}).get("width", 1200), 320, 3840, 1200)
    vp_h = _clamp_int((viewport or {}).get("height", 720), 320, 2160, 720)
    locked_set = set(locked_selectors or ())
    elements = []
    connections = []
    for idx, (sel, cfg) in enumerate(sorted((bindings or {}).items())):
        if not isinstance(cfg, dict):
            continue
        layout = cfg.get("layout") if isinstance(cfg.get("layout"), dict) else {}
        size = cfg.get("size") if isinstance(cfg.get("size"), dict) else {}
        props = cfg.get("props") if isinstance(cfg.get("props"), dict) else {}
        spacing = cfg.get("spacing") if isinstance(cfg.get("spacing"), dict) else {}
        tokens = cfg.get("tokens") if isinstance(cfg.get("tokens"), dict) else {}
        x = _clamp_int(layout.get("x", 0), 0, vp_w, 0)
        y = _clamp_int(layout.get("y", 0), 0, vp_h, 0)
        w = _clamp_int(size.get("w", layout.get("w", 240)), 24, vp_w, 240)
        h = _clamp_int(size.get("h", layout.get("h", 120)), 24, vp_h, 120)
        order = cfg.get("order") if isinstance(cfg.get("order"), int) else layout.get("z", idx)
        conns = [str(t) for t in (props.get("connects_to") or []) if isinstance(t, str) and t.strip()]
        for tgt in conns:
            connections.append({"from": sel, "to": tgt})
        elements.append({
            "selector": sel,
            "kind": str(props.get("kind") or "section"),
            "label": str(props.get("label") or sel),
            "note": str(props.get("note") or ""),
            "x": x,
            "y": y,
            "w": w,
            "h": h,
            "xPct": round(100.0 * x / vp_w, 1),
            "yPct": round(100.0 * y / vp_h, 1),
            "wPct": round(100.0 * w / vp_w, 1),
            "hPct": round(100.0 * h / vp_h, 1),
            "z": order,
            "layout": layout,
            "spacing": spacing,
            "tokens": tokens,
            "visibility": cfg.get("visibility", "visible"),
            "variant": cfg.get("variant"),
            "connects_to": conns,
            "locked": sel in locked_set,
        })

    parent_of: dict[str, str] = {}
    relations: list[dict] = []
    for a in elements:
        containers = [
            b for b in elements
            if b["selector"] != a["selector"]
            and b["x"] <= a["x"] and b["y"] <= a["y"]
            and b["x"] + b["w"] >= a["x"] + a["w"]
            and b["y"] + b["h"] >= a["y"] + a["h"]
        ]
        if containers:
            smallest = min(containers, key=lambda b: b["w"] * b["h"])
            parent_of[a["selector"]] = smallest["selector"]
            relations.append({"type": "contained_in", "source": a["selector"], "target": smallest["selector"]})

    for i, a in enumerate(elements):
        for b in elements[i + 1:]:
            if parent_of.get(a["selector"]) != parent_of.get(b["selector"]):
                continue
            if a["x"] + a["w"] <= b["x"] + 8:
                relations.append({"type": "left_of", "source": a["selector"], "target": b["selector"]})
            elif b["x"] + b["w"] <= a["x"] + 8:
                relations.append({"type": "left_of", "source": b["selector"], "target": a["selector"]})
            if a["y"] + a["h"] <= b["y"] + 8:
                relations.append({"type": "above", "source": a["selector"], "target": b["selector"]})
            elif b["y"] + b["h"] <= a["y"] + 8:
                relations.append({"type": "above", "source": b["selector"], "target": a["selector"]})

    return {
        "viewport": {"width": vp_w, "height": vp_h},
        "elements": elements,
        "connections": connections,
        "spatial_relations": relations,
        "locked_selectors": sorted(locked_set),
    }
