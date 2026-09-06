import json
import os
import urllib.request

import providers
from providers.base import ProviderProfile

DEFAULT_BASE_URL = "http://127.0.0.1:8999/v1"

# Shown when the local proxy cannot be reached (Hermes not started yet, no
# account logged in, ...). The live list comes from the proxy, which in turn
# asks Antigravity, so this only has to be good enough to bootstrap.
FALLBACK_MODELS = (
    "gemini-3.8-flash",
    "gemini-3.8-flash-tiered",
    "gemini-3.6-flash-high",
    "gemini-3.6-flash-medium",
    "gemini-3.6-flash-low",
    "gemini-3.5-flash-low",
    "gemini-3-flash",
    "gemini-pro-agent",
    "gemini-3.1-pro-low",
    "claude-opus-4-6-thinking",
    "claude-sonnet-4-6",
    "gpt-oss-120b-medium",
)


class AntigravityProfile(ProviderProfile):
    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 8.0,
        **kwargs
    ) -> list[str] | None:
        """Ask the local proxy for the models Antigravity currently serves.

        Google renames these ids regularly, so a hardcoded table goes stale;
        the proxy reads the live catalogue and we just mirror it here.
        """
        url = (base_url or os.environ.get("ANTIGRAVITY_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        try:
            req = urllib.request.Request(f"{url}/models", headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            models = [m["id"] for m in payload.get("data", []) if m.get("id")]
            if models:
                return sorted(models)
        except Exception:
            pass
        return list(FALLBACK_MODELS)


antigravity = AntigravityProfile(
    name="antigravity",
    aliases=("agy",),
    display_name="Google Antigravity",
    description="Query Gemini and Claude models directly using your Google OAuth accounts pool",
    signup_url="https://github.com/mrhisyammm/opencode-antigravity-auth",
    env_vars=("ANTIGRAVITY_API_KEY", "ANTIGRAVITY_BASE_URL"),
    base_url=DEFAULT_BASE_URL,
    auth_type="api_key",
    default_aux_model="gemini-3.8-flash",
    fallback_models=FALLBACK_MODELS,
)

providers.register_provider(antigravity)
