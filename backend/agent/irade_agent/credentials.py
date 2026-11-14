"""Credential normalization without persisting or leaking API keys."""
import os


def personal_key(request):
    value = request.get("api_key") if isinstance(request, dict) else ""
    if value is None or value == "":
        return ""
    if (
        not isinstance(value, str)
        or len(value) > 4096
        or any(ord(char) < 33 or ord(char) > 126 for char in value)
    ):
        raise ValueError("AGENT_API_KEY_INVALID")
    return value


def effective_key(request, profile=None):
    key = personal_key(request)
    if key:
        return key
    if profile and profile.get("api_key"):
        return profile["api_key"]
    return os.getenv("AGENT_API_KEY", "").strip()
