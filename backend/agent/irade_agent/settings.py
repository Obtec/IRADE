"""Option validation and allowlisted API profile resolution for IRADE agent."""
import json
import math
import os
import re
from urllib.parse import urlparse

API_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def defaults():
    effort = os.getenv("AGENT_REASONING_EFFORT", "low").strip().lower()
    if effort not in ("low", "high", "max"):
        effort = "low"
    try:
        tokens = int(os.getenv("AGENT_MAX_TOKENS", "8192"))
    except ValueError:
        tokens = 8192
    tokens = max(512, min(32768, tokens))
    try:
        steps = int(os.getenv("AGENT_MAX_STEPS", "8"))
    except ValueError:
        steps = 8
    steps = max(1, min(12, steps))
    return {
        "reasoning_effort": effort,
        "max_tokens": tokens,
        "max_steps": steps,
        "temperature": 1,
        "top_p": 0.95,
    }


def _bounded(name, value, low, high, integer=False):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < low
        or value > high
        or (integer and not isinstance(value, int))
    ):
        raise ValueError("AGENT_OPTIONS_INVALID")
    return value


def options(raw):
    if raw is None:
        raw = {}
    if not isinstance(raw, dict) or set(raw) - set(defaults()):
        raise ValueError("AGENT_OPTIONS_INVALID")
    config = {**defaults(), **raw}
    if config["reasoning_effort"] not in ("low", "high", "max"):
        raise ValueError("AGENT_OPTIONS_INVALID")
    config["max_tokens"] = _bounded("max_tokens", config["max_tokens"], 512, 32768, True)
    config["max_steps"] = _bounded("max_steps", config["max_steps"], 1, 12, True)
    config["temperature"] = _bounded("temperature", config["temperature"], 0, 2)
    config["top_p"] = _bounded("top_p", config["top_p"], 0.05, 1)
    return config


def _valid_api_url(url):
    if not isinstance(url, str) or not url.strip():
        return False
    parsed = urlparse(url.strip())
    return parsed.scheme == "https" and bool(parsed.netloc)


def api_profiles():
    """Return server-allowlisted API profiles keyed by API ID."""
    primary_url = os.getenv("AGENT_BASE_URL", "https://api.exaone.ai/v1").strip()
    primary_model = os.getenv("AGENT_MODEL", "k-exaone_v2.5").strip() or "k-exaone_v2.5"
    primary_key = os.getenv("AGENT_API_KEY", "").strip()
    primary_id = os.getenv("AGENT_API_ID", primary_model).strip()
    if not API_ID_RE.fullmatch(primary_id):
        primary_id = "k-exaone_v2.5"
    profiles = {}
    if _valid_api_url(primary_url):
        profiles[primary_id] = {
            "id": primary_id,
            "label": os.getenv("AGENT_API_LABEL", f"EXAONE 2.5 ({primary_model})"),
            "base_url": primary_url,
            "model": primary_model,
            "api_key": primary_key,
            "reasoning_mode": "exaone",
        }

    extra_raw = os.getenv("AGENT_APIS_JSON", "").strip()
    if extra_raw:
        try:
            extra_items = json.loads(extra_raw)
        except ValueError:
            extra_items = []
        if isinstance(extra_items, list):
            for entry in extra_items:
                if not isinstance(entry, dict):
                    continue
                pid = str(entry.get("id") or "").strip()
                base_url = str(entry.get("base_url") or "").strip()
                model = str(entry.get("model") or "").strip()
                if not API_ID_RE.fullmatch(pid) or not _valid_api_url(base_url) or not model:
                    continue
                api_key = entry.get("api_key")
                if api_key is None and entry.get("api_key_env"):
                    api_key = os.getenv(str(entry["api_key_env"]), "")
                profiles[pid] = {
                    "id": pid,
                    "label": str(entry.get("label") or f"{pid} ({model})"),
                    "base_url": base_url,
                    "model": model,
                    "api_key": str(api_key or "").strip(),
                    "reasoning_mode": str(entry.get("reasoning_mode") or "exaone"),
                }
    return profiles


def resolve_api(requested_id=None):
    profiles = api_profiles()
    if not profiles:
        raise ValueError("AGENT_NOT_CONFIGURED")
    if not requested_id:
        return next(iter(profiles.values()))
    if not isinstance(requested_id, str) or requested_id not in profiles:
        raise ValueError("AGENT_API_UNKNOWN")
    return profiles[requested_id]


def public_apis():
    return [
        {
            "id": profile["id"],
            "label": profile["label"],
            "model": profile["model"],
            "default_configured": bool(profile.get("api_key")),
        }
        for profile in api_profiles().values()
    ]
