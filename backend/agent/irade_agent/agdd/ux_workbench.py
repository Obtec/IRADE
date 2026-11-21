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
    def list(self, workspace_id: str, view_key: str | None = None) -> list[dict]: ...


class MemoryOverrideStore:
    def __init__(self):
        self.rows: dict[tuple, dict] = {}

    def get(self, workspace_id, view_key, selector):
        r = self.rows.get((workspace_id, view_key, selector))
        return dict(r) if r else None

    def put(self, row):
        self.rows[(row["workspace_id"], row["view_key"], row["component_selector"])] = dict(row)

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
