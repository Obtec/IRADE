"""Only numeric, allowlisted provider quota data separated per API and API key."""
from collections import OrderedDict
from contextvars import ContextVar
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import time

_DEFAULT_SCOPE = "__default__"
_server_states = {}
_personal = OrderedDict()
_context = ContextVar("irade_agent_quota_context", default=None)


def _scope_hash(api_id, base_url, model, api_key):
    raw = "\0".join([api_id or "", base_url or "", model or "", api_key or ""])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _path():
    return Path(os.getenv("AGENT_SOCKET", "/run/irade-agent/agent.sock")).parent / "quota.json"


def bind(api_key, profile=None):
    profile = profile or {}
    api_id = profile.get("id") or os.getenv("AGENT_MODEL", "k-exaone_v2.5")
    base_url = profile.get("base_url") or os.getenv("AGENT_BASE_URL", "https://api.exaone.ai/v1")
    model = profile.get("model") or os.getenv("AGENT_MODEL", "k-exaone_v2.5")
    server_key = profile.get("api_key") or os.getenv("AGENT_API_KEY", "")
    is_personal = bool(api_key and api_key != server_key)
    scope = _scope_hash(api_id, base_url, model, api_key if is_personal else server_key)
    return _context.set(
        {
            "scope": scope,
            "api_id": api_id,
            "personal": is_personal,
        }
    )


def unbind(token):
    _context.reset(token)


def _current_descriptor():
    ctx = _context.get()
    if ctx is not None:
        return ctx
    api_id = os.getenv("AGENT_MODEL", "k-exaone_v2.5")
    base_url = os.getenv("AGENT_BASE_URL", "https://api.exaone.ai/v1")
    server_key = os.getenv("AGENT_API_KEY", "")
    return {
        "scope": _scope_hash(api_id, base_url, api_id, server_key),
        "api_id": api_id,
        "personal": False,
    }


def _load_server_disk():
    try:
        saved = json.loads(_path().read_text())
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    if isinstance(saved, dict) and isinstance(saved.get("entries"), dict):
        return saved["entries"]
    return {}


def snapshot():
    global _server_states
    desc = _current_descriptor()
    scope = desc["scope"]
    if desc["personal"]:
        if scope in _personal:
            _personal.move_to_end(scope)
        return copy.deepcopy(_personal.get(scope, {}))
    cache_key = (str(_path()), scope)
    if cache_key not in _server_states:
        disk_entries = _load_server_disk()
        entry = disk_entries.get(scope)
        _server_states[cache_key] = entry if isinstance(entry, dict) else {}
    return copy.deepcopy(_server_states[cache_key])


def _number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) and 0 <= number <= 1e12 else None
    except (TypeError, ValueError):
        return None


def record(headers, status):
    global _server_states
    desc = _current_descriptor()
    scope = desc["scope"]
    previous = snapshot()
    limits = {}
    for window in ("second", "minute", "day"):
        limit = _number(headers.get(f"x-{window}-rate-limit"))
        remaining = _number(headers.get(f"x-{window}-rate-limit-remaining"))
        if limit is not None or remaining is not None:
            limits[window] = {"limit": limit, "remaining": remaining}
    observed = time.time()
    retry_ms = _number(headers.get("retry-after-ms"))
    retry_seconds = _number(headers.get("retry-after"))
    retry = retry_ms / 1000 if retry_ms is not None else retry_seconds
    current = {
        "api": desc["api_id"],
        "limits": limits or previous.get("limits", {}),
        "observed_at": observed,
        "limits_observed_at": observed if limits else previous.get("limits_observed_at"),
        "status": status,
        "retry_at": observed + retry if retry is not None else None,
    }
    if desc["personal"]:
        _personal[scope] = current
        _personal.move_to_end(scope)
        while len(_personal) > 64:
            _personal.popitem(last=False)
        return snapshot()
    cache_key = (str(_path()), scope)
    _server_states[cache_key] = current
    try:
        disk_entries = _load_server_disk()
        disk_entries[scope] = current
        temporary = _path().with_suffix(".tmp")
        temporary.write_text(json.dumps({"version": 2, "entries": disk_entries}))
        temporary.chmod(0o600)
        temporary.replace(_path())
    except OSError:
        pass
    return snapshot()
