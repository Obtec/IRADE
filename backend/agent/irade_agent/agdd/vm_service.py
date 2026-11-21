"""Workspace ↔ VM binding, agent settings and CLI usage bookkeeping (shared by agdd-api and workers)."""
from __future__ import annotations

import os
import threading

from psycopg.types.json import Jsonb

from . import infra
from .cli_agents import AgentSettings, VmAgent
from .vm import CANVAS_BASE, RuntimeClient, VmShell

REPOS_DIR = "/workspace/repos"
_shells: dict[tuple, VmShell] = {}
_lock = threading.Lock()


def enabled() -> bool:
    return os.environ.get("AGDD_EXECUTION", "vm") == "vm" and RuntimeClient().available()


def canvas_for(workspace_id) -> int:
    with infra.pg() as c:
        r = c.execute("SELECT canvas_id FROM workspace_vms WHERE workspace_id=%s", (workspace_id,)).fetchone()
        if r:
            return r["canvas_id"]
        r = c.execute("INSERT INTO workspace_vms (workspace_id, canvas_id) VALUES (%s, %s + nextval('agdd_vm_seq')) "
                      "ON CONFLICT (workspace_id) DO UPDATE SET workspace_id = EXCLUDED.workspace_id "
                      "RETURNING canvas_id", (workspace_id, CANVAS_BASE)).fetchone()
        c.commit()
        return r["canvas_id"]


def shell(workspace_id, name: str = "agdd") -> VmShell:
    """One automation PTY per (workspace, purpose); reopened transparently if the guest reaped it."""
    key = (str(workspace_id), name)
    with _lock:
        sh = _shells.get(key)
        if sh is None:
            sh = _shells[key] = VmShell(RuntimeClient(), canvas_for(workspace_id), name)
        return sh


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


def agent(workspace_id, override: dict | None = None, name: str = "agdd") -> VmAgent:
    return VmAgent(shell(workspace_id, name), load_settings(workspace_id, override))


def record_usage(workspace_id, run_id, node, settings: AgentSettings, res, ms: int, cheap: bool = False):
    """CLI calls are billed by the provider plan, not the gateway: keep tokens/cost + rate-limit snapshots."""
    from . import pgstore
    model = f"{settings.cli}:{(settings.cheap_model if cheap else settings.model) or 'default'}"
    with infra.pg() as c:
        if run_id:
            pgstore.record_execution(c, run_id, node or "-", model, res.usage or {}, res.cost_usd or 0, ms)
        if res.rate_limits:
            c.execute("INSERT INTO cli_usage_snapshots (workspace_id, cli, rate_limits) VALUES (%s,%s,%s) "
                      "ON CONFLICT (workspace_id, cli) DO UPDATE SET rate_limits=EXCLUDED.rate_limits, "
                      "captured_at=now()", (workspace_id, settings.cli, Jsonb(res.rate_limits)))
        c.commit()


def repo_path(repo_key: str) -> str:
    import re
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", repo_key or "") or repo_key.startswith("."):
        raise ValueError("invalid repo_key")
    return f"{REPOS_DIR}/{repo_key}"
