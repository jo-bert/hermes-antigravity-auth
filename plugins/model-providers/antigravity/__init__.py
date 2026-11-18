import json
import os
import urllib.request

import providers
from providers.base import ProviderProfile

DEFAULT_BASE_URL = "http://127.0.0.1:8999/v1"

# Bootstrap list only: the proxy serves the live Antigravity catalogue, which is
# authoritative. Google renames ids often enough that a pinned table goes stale.
FALLBACK_MODELS = (
    "gemini-4-hydrogen-sonar",
    "gemini-3.8-flash",
    "gemini-3.8-flash-tiered",
    "gemini-3-flash",
    "gemini-3.5-flash-lite",
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
        """Mirror the models the local proxy reports as currently served."""
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