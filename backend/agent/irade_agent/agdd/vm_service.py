"""Workspace ↔ VM binding and agent settings (shared by agdd-api and workers)."""
from __future__ import annotations

import os
import threading

from psycopg.types.json import Jsonb

from . import infra
from .api_agents import AgentSettings, ApiAgent
from .vm import CANVAS_BASE, RuntimeClient, VmShell

REPOS_DIR = "/workspace/repos"
_shells: dict[tuple, VmShell] = {}
_canvas_cache: dict[str, int] = {}
_expanded_cids: set[int] = set()
_lock = threading.RLock()


def enabled() -> bool:
    return os.environ.get("AGDD_EXECUTION", "vm") == "vm" and RuntimeClient().available()


def canvas_for(workspace_id) -> int:
    key = str(workspace_id)
    with _lock:
        hit = _canvas_cache.get(key)
        if hit is not None:
            return hit
    with infra.pg() as c:
        r = c.execute("SELECT canvas_id FROM workspace_vms WHERE workspace_id=%s", (workspace_id,)).fetchone()
        if r:
            cid = r["canvas_id"]
        else:
            r = c.execute("INSERT INTO workspace_vms (workspace_id, canvas_id) VALUES (%s, %s + nextval('agdd_vm_seq')) "
                          "ON CONFLICT (workspace_id) DO UPDATE SET workspace_id = EXCLUDED.workspace_id "
                          "RETURNING canvas_id", (workspace_id, CANVAS_BASE)).fetchone()
            c.commit()
            cid = r["canvas_id"]
    with _lock:
        _canvas_cache[key] = int(cid)
    return int(cid)


def shell(workspace_id, name: str = "agdd") -> VmShell:
    """One automation PTY per (workspace, purpose); reopened transparently if the guest reaped it."""
    key = (str(workspace_id), name)
    with _lock:
        sh = _shells.get(key)
        if sh is not None:
            return sh
    cid = canvas_for(workspace_id)
    with _lock:
        sh = _shells.get(key)
        if sh is None:
            sh = _shells[key] = VmShell(RuntimeClient(), cid, name)
        return sh


def maybe_expand_disk(workspace_id, min_gib: int = 12) -> bool:
    """If the VM disk is below ``min_gib`` and a larger tier exists, expand it once and reset cached PTYs."""
    cid = canvas_for(workspace_id)
    with _lock:
        if cid in _expanded_cids:
            return False
    rc = RuntimeClient()
    st = rc.settings(cid)
    cur = int(st.get("diskGiB") or 0)
    if cur >= min_gib:
        with _lock:
            _expanded_cids.add(cid)
        return False
    sizes = sorted(int(x) for x in (st.get("diskSizesGiB") or []) if isinstance(x, (int, float)) and int(x) >= min_gib)
    if not sizes:
        return False
    with _lock:
        if cid in _expanded_cids:
            return False
        _expanded_cids.add(cid)
    try:
        res = rc.set_disk(cid, sizes[0], int(st.get("revision") or 0))
    except Exception:
        with _lock:
            _expanded_cids.discard(cid)
        raise
    if res.get("error"):
        with _lock:
            _expanded_cids.discard(cid)
        return False
    ws_prefix = str(workspace_id)
    evicted = []
    with _lock:
        for k in [k for k in _shells if k[0] == ws_prefix]:
            evicted.append(_shells.pop(k))
    for sh in evicted:
        sh.opened = False
    return True


def load_settings(workspace_id, override: dict | None = None) -> AgentSettings:
    with infra.pg(readonly=True) as c:
        r = c.execute("SELECT settings_json FROM workspace_agent_settings WHERE workspace_id=%s",
                      (workspace_id,)).fetchone()
    return AgentSettings.from_row({**(r["settings_json"] if r else {}), **(override or {})})


def save_settings(workspace_id, data: dict, user) -> AgentSettings:
    s = AgentSettings.from_row(data)
    with infra.pg() as c:
        c.execute("INSERT INTO workspace_agent_settings (workspace_id, settings_json, updated_by) VALUES (%s,%s,%s) "
                  "ON CONFLICT (workspace_id) DO UPDATE SET settings_json=EXCLUDED.settings_json, "
                  "updated_by=EXCLUDED.updated_by, updated_at=now()", (workspace_id, Jsonb(s.__dict__), user))
        c.commit()
    return s


def agent(workspace_id, override: dict | None = None, name: str = "agdd", run_id=None, node: str = "") -> ApiAgent:
    """API-key agent (LLM Gateway) whose repository tools run in this workspace's VM. Budget gating and cost
    accounting happen in the gateway (x-agdd-workspace / x-agdd-run headers)."""
    return ApiAgent(shell(workspace_id, name), load_settings(workspace_id, override), workspace_id, run_id, node)


def repo_path(repo_key: str) -> str:
    import re
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", repo_key or "") or repo_key.startswith("."):
        raise ValueError("invalid repo_key")
    return f"{REPOS_DIR}/{repo_key}"
