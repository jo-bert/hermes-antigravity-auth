import json
import urllib.request
import urllib.parse
import urllib.error
import os
import re
import threading
import socket
import http.server
import socketserver
import webbrowser
import time
import uuid
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ── Proxy Configuration ──────────────────────────────────────
PROXY_PORT = 8999
CLIENT_ID = "1071006060591-tmhssin2h21lcre235vtolojh4g403ep" + ".apps.googleusercontent.com"
CLIENT_SECRET = "GOCSPX-" + "K58FWR486LdLJ1mLB8sXC4z6qDAf"
ENDPOINT = "https://daily-cloudcode-pa.sandbox.googleapis.com"
REDIRECT_URI = "http://localhost:51121/oauth-callback"
SCOPES = "https://www.googleapis.com/auth/cloud-platform https://www.googleapis.com/auth/userinfo.email https://www.googleapis.com/auth/userinfo.profile"

# Upstream access tokens are short lived; refresh a little before they expire.
TOKEN_TTL_MARGIN = 120
# How long the upstream model catalogue stays cached before being refetched.
CATALOG_TTL = 900
UPSTREAM_TIMEOUT = 600

DEBUG = os.environ.get("ANTIGRAVITY_DEBUG", "").lower() in ("1", "true", "yes")

# Token & Project Cache
_token_cache = {}  # email -> (access_token, expires_at)
_project_cache = {}  # email -> project_id
_cooldown_cache = {}  # "email:family" -> timestamp until cooldown expires
_consecutive_failures = {}  # "email:family" -> int count
_catalog_cache = {"fetched_at": 0.0, "models": {}}
# Gemini 3.x refuses a conversation that replays a functionCall without the
# opaque thoughtSignature it originally emitted. OpenAI's wire format has
# nowhere to carry that, so keep it here keyed by tool-call id and re-attach it
# when the caller sends the history back.
#
# This is persisted to disk: without it every gateway restart, and every
# `hermes --continue` of an older session, would replay tool history the model
# no longer accepts and silently fall back to the flattened text form.
_thought_signatures = {}
_thought_sig_order = []
_THOUGHT_SIG_MAX = 2048
_thought_sig_dirty = False
_cache_lock = threading.Lock()


def get_thought_sig_path():
    return os.path.join(os.path.dirname(get_accounts_file_path()),
                        "antigravity-thought-signatures.json")


def load_thought_signatures():
    path = get_thought_sig_path()
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            stored = json.load(f)
        order = stored.get("order") or []
        sigs = stored.get("signatures") or {}
    except Exception as e:
        _log(f"could not read thought signature store: {e}")
        return
    with _cache_lock:
        _thought_signatures.clear()
        _thought_sig_order.clear()
        for call_id in order:
            if call_id in sigs:
                _thought_sig_order.append(call_id)
                _thought_signatures[call_id] = sigs[call_id]
    _log(f"loaded {len(_thought_sig_order)} thought signatures from disk")


def flush_thought_signatures():
    global _thought_sig_dirty
    with _cache_lock:
        if not _thought_sig_dirty:
            return
        payload = {"order": list(_thought_sig_order),
                   "signatures": dict(_thought_signatures)}
        _thought_sig_dirty = False

    path = get_thought_sig_path()
    tmp = f"{path}.tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
    except Exception as e:
        _log(f"could not persist thought signatures: {e}")


def _thought_sig_flusher():
    while True:
        time.sleep(3)
        try:
            flush_thought_signatures()
        except Exception:
            pass


def remember_thought_signature(call_id, signature):
    global _thought_sig_dirty
    if not call_id or not signature:
        return
    with _cache_lock:
        if _thought_signatures.get(call_id) == signature:
            return
        if call_id not in _thought_signatures:
            _thought_sig_order.append(call_id)
            while len(_thought_sig_order) > _THOUGHT_SIG_MAX:
                _thought_signatures.pop(_thought_sig_order.pop(0), None)
        _thought_signatures[call_id] = signature
        _thought_sig_dirty = True


def get_thought_signature(call_id):
    with _cache_lock:
        return _thought_signatures.get(call_id)


def _log(msg):
    if DEBUG:
        print(f"[antigravity] {msg}", flush=True)


# ── Auth & Account Helpers ────────────────────────────────────
def get_accounts_file_path():
    path = os.path.expanduser('~/AppData/Local/hermes/antigravity-accounts.json')
    if not os.path.exists(path):
        # Fallback to OpenCode path
        opencode_path = os.path.expanduser('~/.config/opencode/antigravity-accounts.json')
        if not os.path.exists(opencode_path):
            appdata = os.environ.get('APPDATA')
            if appdata:
                opencode_path = os.path.join(appdata, 'opencode', 'antigravity-accounts.json')
        if os.path.exists(opencode_path):
            return opencode_path
    return path

def load_accounts_data():
    path = get_accounts_file_path()
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {"version": 4, "accounts": [], "activeIndex": 0, "activeIndexByFamily": {"claude": 0, "gemini": 0}}

def save_accounts_data(data):
    path = get_accounts_file_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)

def get_active_account():
    data = load_accounts_data()
    accounts = data.get("accounts", [])
    if not accounts:
        raise ValueError("No accounts configured. Type /antigravity-mrhisyammm-login in chat to log in.")

    active_idx = data.get("activeIndex", 0)
    if active_idx < 0 or active_idx >= len(accounts):
        active_idx = 0
    return accounts[active_idx]

def refresh_token(ref_token, with_expiry=False):
    url = "https://oauth2.googleapis.com/token"
    data = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": ref_token,
        "grant_type": "refresh_token"
    }).encode("utf-8")

    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        res_data = json.loads(resp.read().decode("utf-8"))
        if with_expiry:
            return res_data["access_token"], int(res_data.get("expires_in", 3600))
        return res_data["access_token"]

def antigravity_headers(access_token):
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "User-Agent": "antigravity/windows/amd64",
        "X-Goog-Api-Client": "google-cloud-sdk vscode_cloudshelleditor/0.1",
        "Client-Metadata": '{"ideType":"ANTIGRAVITY","platform":"PLATFORM_UNSPECIFIED","pluginType":"GEMINI"}'
    }

def load_project_id(access_token):
    url = f"{ENDPOINT}/v1internal:loadCodeAssist"
    body = json.dumps({
        "metadata": {
            "ideType": "ANTIGRAVITY",
            "platform": "PLATFORM_UNSPECIFIED",
            "pluginType": "GEMINI"
        }
    }).encode("utf-8")

    headers = antigravity_headers(access_token)
    headers["User-Agent"] = "google-api-nodejs-client/9.15.1"

    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        res_data = json.loads(resp.read().decode("utf-8"))
        project_field = res_data.get("cloudaicompanionProject")
        if isinstance(project_field, dict):
            return project_field.get("id")
        return project_field

def get_auth_credentials(account):
    """Return a (still valid) access token and project id for an account."""
    email = account.get("email", "")
    now = time.time()
    with _cache_lock:
        cached = _token_cache.get(email)
        project_id = _project_cache.get(email)

    if cached and cached[1] > now and project_id:
        return cached[0], project_id

    token, expires_in = refresh_token(account["refreshToken"], with_expiry=True)
    project_id = load_project_id(token)
    with _cache_lock:
        _token_cache[email] = (token, now + max(60, expires_in - TOKEN_TTL_MARGIN))
        _project_cache[email] = project_id

    return token, project_id

def invalidate_credentials(email):
    with _cache_lock:
        _token_cache.pop(email, None)
        _project_cache.pop(email, None)


# ── Model Catalogue ───────────────────────────────────────────
# Friendly / legacy names that Hermes (or a user) may ask for, mapped onto the
# id Antigravity actually serves. Antigravity renames its ids often, so this is
# only a hint: resolve_model() always prefers the live catalogue and falls back
# to fuzzy matching, which keeps things working when Google renames a model.
MODEL_ALIASES = {
    # Anthropic
    "claude-3-5-sonnet-latest": "claude-sonnet-4-6",
    "claude-3-5-sonnet-20241022": "claude-sonnet-4-6",
    "claude-3-5-sonnet-20240620": "claude-sonnet-4-6",
    "claude-3-opus-20240229": "claude-opus-4-6-thinking",
    "claude-3-5-haiku-latest": "claude-sonnet-4-6",
    "claude-opus-4-6": "claude-opus-4-6-thinking",
    "claude-sonnet-4-6-thinking": "claude-sonnet-4-6",
    # Gemini — Antigravity serves the 3.8 flash generation under a "-tiered" id.
    "gemini-3.8-flash": "gemini-3.8-flash-tiered",
    "gemini-3.8-flash-low": "gemini-3.8-flash-tiered",
    "gemini-3.8-flash-high": "gemini-3.8-flash-tiered",
    "gemini-3.7-flash": "gemini-3.7-flash-tiered",
    "gemini-3.6-flash": "gemini-3.6-flash-high",
    "gemini-3.5-flash": "gemini-3.5-flash-low",
    "gemini-3.1-pro": "gemini-3.1-pro-low",
    "gemini-3.1-pro-high": "gemini-pro-agent",
    "gemini-3-pro": "gemini-pro-agent",
    "gemini-flash": "gemini-3.8-flash-tiered",
    "gemini-pro": "gemini-pro-agent",
}

# Used for /v1/models and the provider profile when the live catalogue cannot
# be reached (offline, no account logged in yet, ...).
FALLBACK_MODELS = [
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
]

# Ids that are not chat models (autocomplete / internal experiment slots).
_NON_CHAT_PREFIXES = ("tab_", "chat_")

def fetch_upstream_models(force=False):
    """Fetch the live Antigravity model catalogue, cached for CATALOG_TTL."""
    now = time.time()
    with _cache_lock:
        if not force and _catalog_cache["models"] and now - _catalog_cache["fetched_at"] < CATALOG_TTL:
            return _catalog_cache["models"]

    try:
        account = get_active_account()
        token, _project_id = get_auth_credentials(account)
        req = urllib.request.Request(
            f"{ENDPOINT}/v1internal:fetchAvailableModels",
            data=b"{}",
            headers=antigravity_headers(token),
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        _log(f"model catalogue fetch failed: {e}")
        with _cache_lock:
            return dict(_catalog_cache["models"])

    models = {}
    deprecated = payload.get("deprecatedModelIds", {}) or {}
    for model_id, info in (payload.get("models", {}) or {}).items():
        if model_id.startswith(_NON_CHAT_PREFIXES):
            continue
        # Google marks superseded ids explicitly; follow the redirect.
        target = deprecated.get(model_id, {}).get("newModelId")
        models[model_id] = {
            "display_name": info.get("displayName") or model_id,
            "provider": info.get("modelProvider", ""),
            "max_output_tokens": info.get("maxOutputTokens"),
            "supports_thinking": bool(info.get("supportsThinking")),
            "recommended": bool(info.get("recommended")),
            "superseded_by": target,
        }

    with _cache_lock:
        _catalog_cache["models"] = models
        _catalog_cache["fetched_at"] = now
    _log(f"model catalogue refreshed: {len(models)} models")
    return models

def _normalize(name):
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())

def model_family(model_id):
    lowered = (model_id or "").lower()
    if "claude" in lowered:
        return "claude"
    if "gpt" in lowered:
        return "claude"  # shares the "Claude and GPT models" quota bucket
    return "gemini"

def resolve_model(requested):
    """Map an OpenAI-style model name onto a live Antigravity model id.

    Returns (upstream_id, info_dict). Raises ValueError when nothing matches so
    the caller can answer with a real error instead of silently substituting a
    different model.
    """
    requested = (requested or "").strip()
    if not requested:
        requested = "gemini-3.8-flash"

    catalog = fetch_upstream_models()

    def _resolve_in(name, seen=()):
        info = catalog.get(name)
        if info is None:
            return None
        # Follow Google's own deprecation redirects.
        target = info.get("superseded_by")
        if target and target in catalog and target not in seen:
            return _resolve_in(target, seen + (name,))
        return name

    # 1. The catalogue knows this id verbatim.
    hit = _resolve_in(requested)
    if hit:
        return hit, catalog[hit]

    # 2. A known alias points at something the catalogue serves.
    alias = MODEL_ALIASES.get(requested) or MODEL_ALIASES.get(requested.lower())
    if alias:
        hit = _resolve_in(alias)
        if hit:
            return hit, catalog[hit]

    # 3. No catalogue (offline / not logged in): trust the alias table blindly
    #    so the request still has a chance of succeeding.
    if not catalog:
        return alias or requested, {}

    # 4. Fuzzy match, so a rename upstream ("gemini-3.8-flash" ->
    #    "gemini-3.8-flash-tiered") does not break the mapping again.
    target = _normalize(alias or requested)
    candidates = [m for m in catalog if _normalize(m).startswith(target)]
    if not candidates:
        candidates = [m for m in catalog if target and target in _normalize(m)]
    if candidates:
        # Prefer a recommended id, then the shortest (least suffixed) one.
        candidates.sort(key=lambda m: (not catalog[m].get("recommended"), len(m)))
        return candidates[0], catalog[candidates[0]]

    raise ValueError(
        f"Unknown model '{requested}'. Available Antigravity models: "
        + ", ".join(sorted(catalog))
    )

def list_public_models():
    """Model ids exposed over /v1/models: live catalogue plus friendly aliases."""
    catalog = fetch_upstream_models()
    ids = list(catalog) if catalog else list(FALLBACK_MODELS)
    for alias, target in MODEL_ALIASES.items():
        if alias not in ids and (not catalog or target in catalog):
            ids.append(alias)
    return ids

# Kept for backwards compatibility with anything importing this name.
MODEL_MAPPING = MODEL_ALIASES


# ── OpenAI -> Gemini request translation ──────────────────────
# Gemini's function-declaration schema only understands a subset of JSON Schema;
# unknown keys make the request fail outright.
_SCHEMA_KEEP = {
    "type", "format", "description", "nullable", "enum", "items", "properties",
    "required", "minimum", "maximum", "minItems", "maxItems", "anyOf",
}

def sanitize_schema(schema):
    if not isinstance(schema, dict):
        return {"type": "string"}

    out = {}
    for key, value in schema.items():
        if key in ("oneOf", "allOf"):
            out["anyOf"] = [sanitize_schema(s) for s in value if isinstance(s, dict)]
        elif key == "const":
            out["enum"] = [value]
            out.setdefault("type", "string")
        elif key not in _SCHEMA_KEEP:
            continue
        elif key == "properties" and isinstance(value, dict):
            out["properties"] = {k: sanitize_schema(v) for k, v in value.items()}
        elif key == "items":
            out["items"] = sanitize_schema(value)
        elif key == "anyOf" and isinstance(value, list):
            out["anyOf"] = [sanitize_schema(s) for s in value if isinstance(s, dict)]
        elif key == "type" and isinstance(value, list):
            # ["string", "null"] -> string + nullable
            non_null = [t for t in value if t != "null"]
            out["type"] = non_null[0] if non_null else "string"
            if len(non_null) != len(value):
                out["nullable"] = True
        else:
            out[key] = value

    if "properties" in out and "type" not in out:
        out["type"] = "object"
    if "items" in out and "type" not in out:
        out["type"] = "array"
    # An object with no properties is rejected upstream; drop the empty shell.
    if out.get("type") == "object" and not out.get("properties"):
        out.pop("required", None)
    return out

def _content_to_parts(content):
    """Turn an OpenAI message content (string or content-part list) into parts."""
    if content is None:
        return []
    if isinstance(content, str):
        return [{"text": content}] if content else []

    parts = []
    for part in content:
        if not isinstance(part, dict):
            parts.append({"text": str(part)})
            continue
        ptype = part.get("type")
        if ptype == "text":
            text = part.get("text", "")
            if text:
                parts.append({"text": text})
        elif ptype == "image_url":
            url = (part.get("image_url") or {}).get("url", "")
            if url.startswith("data:"):
                header, _, b64 = url.partition(",")
                mime = header[5:].split(";")[0] or "image/png"
                parts.append({"inlineData": {"mimeType": mime, "data": b64}})
            elif url:
                parts.append({"fileData": {"fileUri": url}})
    return parts

def translate_openai_to_gemini(messages, degrade_tool_calls=False):
    """Translate OpenAI chat messages into Gemini `contents` + systemInstruction.

    When `degrade_tool_calls` is set, past tool calls and their results are
    replayed as plain text instead of functionCall/functionResponse parts. That
    is the escape hatch for a Gemini thinking model whose thoughtSignature we no
    longer hold (proxy restarted, history replayed from disk, ...).

    Returns (contents, system_parts).
    """
    contents = []
    system_parts = []
    # tool_call_id -> function name, so tool results can be named correctly.
    call_names = {}

    for msg in messages or []:
        role = msg.get("role", "user")
        content = msg.get("content")

        if role in ("system", "developer"):
            system_parts.extend(_content_to_parts(content))
            continue

        if role == "tool":
            call_id = msg.get("tool_call_id", "")
            name = call_names.get(call_id) or msg.get("name") or "tool"
            text = content if isinstance(content, str) else json.dumps(content)
            try:
                payload = json.loads(text) if text else {}
                if not isinstance(payload, dict):
                    payload = {"result": payload}
            except Exception:
                payload = {"result": text}
            if degrade_tool_calls:
                contents.append({"role": "user", "parts": [
                    {"text": f"[result of {name}]\n{text}"}]})
                continue
            fr = {"name": name, "response": payload}
            if call_id:
                fr["id"] = call_id
            contents.append({"role": "user", "parts": [{"functionResponse": fr}]})
            continue

        if role == "assistant":
            parts = _content_to_parts(content)
            for call in msg.get("tool_calls") or []:
                fn = call.get("function") or {}
                name = fn.get("name", "")
                call_id = call.get("id", "")
                if call_id:
                    call_names[call_id] = name
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except Exception:
                    args = {}
                if degrade_tool_calls:
                    parts.append({"text": f"[called {name} with {json.dumps(args)}]"})
                    continue
                fc = {"name": name, "args": args}
                if call_id:
                    fc["id"] = call_id
                call_part = {"functionCall": fc}
                signature = get_thought_signature(call_id)
                if signature:
                    call_part["thoughtSignature"] = signature
                parts.append(call_part)
            if not parts:
                continue
            contents.append({"role": "model", "parts": parts})
            continue

        parts = _content_to_parts(content)
        if parts:
            contents.append({"role": "user", "parts": parts})

    # Gemini rejects two consecutive turns with the same role.
    merged = []
    for item in contents:
        if merged and merged[-1]["role"] == item["role"]:
            merged[-1]["parts"].extend(item["parts"])
        else:
            merged.append(item)

    if not merged:
        merged = [{"role": "user", "parts": [{"text": ""}]}]

    return merged, system_parts

def translate_tools(req_json):
    """Translate OpenAI `tools` / `tool_choice` into Gemini equivalents."""
    declarations = []
    for tool in req_json.get("tools") or []:
        if tool.get("type") not in (None, "function"):
            continue
        fn = tool.get("function") or {}
        name = fn.get("name")
        if not name:
            continue
        decl = {"name": name, "description": fn.get("description", "")}
        params = fn.get("parameters")
        if isinstance(params, dict) and params.get("properties"):
            decl["parameters"] = sanitize_schema(params)
        declarations.append(decl)

    if not declarations:
        return None, None

    tools = [{"functionDeclarations": declarations}]

    choice = req_json.get("tool_choice")
    tool_config = None
    if choice == "none":
        tool_config = {"functionCallingConfig": {"mode": "NONE"}}
    elif choice == "required":
        tool_config = {"functionCallingConfig": {"mode": "ANY"}}
    elif isinstance(choice, dict):
        name = (choice.get("function") or {}).get("name")
        if name:
            tool_config = {"functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": [name]}}
    elif choice == "auto":
        tool_config = {"functionCallingConfig": {"mode": "AUTO"}}

    return tools, tool_config

def build_generation_config(req_json, model_info):
    cfg = {}
    if req_json.get("temperature") is not None:
        cfg["temperature"] = req_json["temperature"]
    if req_json.get("top_p") is not None:
        cfg["topP"] = req_json["top_p"]

    max_tokens = req_json.get("max_completion_tokens") or req_json.get("max_tokens")
    if max_tokens:
        cap = model_info.get("max_output_tokens")
        # Hermes asks for 64k by default; clamp so smaller models don't 400.
        cfg["maxOutputTokens"] = min(int(max_tokens), int(cap)) if cap else int(max_tokens)

    stop = req_json.get("stop")
    if isinstance(stop, str):
        cfg["stopSequences"] = [stop]
    elif isinstance(stop, list) and stop:
        cfg["stopSequences"] = [s for s in stop if isinstance(s, str)][:5]

    fmt = req_json.get("response_format") or {}
    ftype = fmt.get("type")
    if ftype == "json_object":
        cfg["responseMimeType"] = "application/json"
    elif ftype == "json_schema":
        cfg["responseMimeType"] = "application/json"
        schema = (fmt.get("json_schema") or {}).get("schema")
        if isinstance(schema, dict):
            cfg["responseJsonSchema"] = schema

    return cfg

def build_gemini_request(req_json, model_info, degrade_tool_calls=False):
    contents, system_parts = translate_openai_to_gemini(
        req_json.get("messages", []), degrade_tool_calls=degrade_tool_calls)
    inner = {"contents": contents}
    if system_parts:
        inner["systemInstruction"] = {"parts": system_parts}

    tools, tool_config = translate_tools(req_json)
    if tools:
        inner["tools"] = tools
        if tool_config:
            inner["toolConfig"] = tool_config

    gen_cfg = build_generation_config(req_json, model_info)
    if gen_cfg:
        inner["generationConfig"] = gen_cfg
    return inner


# ── Gemini -> OpenAI response translation ─────────────────────
_FINISH_REASONS = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
    "PROHIBITED_CONTENT": "content_filter",
    "BLOCKLIST": "content_filter",
}

def extract_parts(candidate):
    """Pull visible text and function calls out of a candidate.

    Thinking models return `thought` parts and bare `thoughtSignature` parts
    interleaved with the answer, and the answer itself can be split across
    several parts — the old code only ever read parts[0], which dropped text.
    """
    text_chunks = []
    calls = []
    for part in (candidate.get("content") or {}).get("parts") or []:
        if part.get("thought"):
            continue
        fc = part.get("functionCall")
        if fc:
            remember_thought_signature(fc.get("id"), part.get("thoughtSignature"))
            calls.append(fc)
            continue
        text = part.get("text")
        if text:
            text_chunks.append(text)
    return "".join(text_chunks), calls

def convert_usage(usage_meta):
    if not usage_meta:
        return None
    prompt = usage_meta.get("promptTokenCount", 0) or 0
    completion = usage_meta.get("candidatesTokenCount", 0) or 0
    thoughts = usage_meta.get("thoughtsTokenCount", 0) or 0
    total = usage_meta.get("totalTokenCount", prompt + completion + thoughts) or 0
    out = {
        "prompt_tokens": prompt,
        "completion_tokens": completion + thoughts,
        "total_tokens": total,
    }
    if thoughts:
        out["completion_tokens_details"] = {"reasoning_tokens": thoughts}
    return out

def make_tool_call(fc, index):
    return {
        "index": index,
        "id": fc.get("id") or f"call_{uuid.uuid4().hex[:24]}",
        "type": "function",
        "function": {
            "name": fc.get("name", ""),
            "arguments": json.dumps(fc.get("args") or {}),
        },
    }


# ── Upstream call with account rotation ───────────────────────
class UpstreamError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status

def apply_cooldown(email, family, status_code):
    cooldown_key = f"{email}:{family}"
    with _cache_lock:
        failures = _consecutive_failures.get(cooldown_key, 0) + 1
        _consecutive_failures[cooldown_key] = failures
        if failures == 1:
            duration = 60
        elif failures == 2:
            duration = 300
        elif failures == 3:
            duration = 1800
        else:
            duration = 7200
        _cooldown_cache[cooldown_key] = time.time() + duration
    invalidate_credentials(email)
    print(f"[Proxy] Account {email} got HTTP {status_code} on {family} "
          f"(consecutive failures: {failures}), {duration}s cooldown applied.", flush=True)

def call_upstream(req_json, mapped_model, model_info, family, stream):
    """Try each enabled account in turn; return the open upstream response."""
    data = load_accounts_data()
    accounts = data.get("accounts", [])
    if not accounts:
        raise UpstreamError(
            "No Google Antigravity accounts configured. "
            "Run /antigravity-mrhisyammm-login in Hermes to log in.", 401)

    active_idx = data.get("activeIndex", 0)
    if active_idx < 0 or active_idx >= len(accounts):
        active_idx = 0

    action = "streamGenerateContent" if stream else "generateContent"
    url = f"{ENDPOINT}/v1internal:{action}" + ("?alt=sse" if stream else "")

    last_err = None
    last_status = 502
    skipped_for_cooldown = False

    for offset in range(len(accounts)):
        idx = (active_idx + offset) % len(accounts)
        account = accounts[idx]
        email = account.get("email", "")

        if not account.get("enabled", True):
            continue

        cooldown_key = f"{email}:{family}"
        with _cache_lock:
            cooldown_until = _cooldown_cache.get(cooldown_key, 0)
        if time.time() < cooldown_until:
            skipped_for_cooldown = True
            remaining = int(cooldown_until - time.time())
            last_err = f"{email} is cooling down for {remaining}s on {family} models"
            last_status = 429
            continue

        try:
            token, project_id = get_auth_credentials(account)
        except Exception as e:
            last_err = f"Auth error on {email}: {e}"
            invalidate_credentials(email)
            continue

        def _post(degrade):
            body = json.dumps({
                "project": project_id,
                "model": mapped_model,
                "request": build_gemini_request(req_json, model_info, degrade_tool_calls=degrade),
            }).encode("utf-8")
            req = urllib.request.Request(url, data=body, headers=antigravity_headers(token))
            return urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT)

        try:
            try:
                resp = _post(False)
            except urllib.error.HTTPError as he:
                err_text = he.read().decode("utf-8", errors="ignore")
                # A replayed conversation whose thoughtSignature we lost: retry
                # once with the tool history flattened to plain text.
                if he.code == 400 and "thought_signature" in err_text:
                    _log("missing thought signature, retrying with flattened tool history")
                    resp = _post(True)
                else:
                    raise UpstreamError(
                        f"Upstream HTTP {he.code} on {email}: {err_text}", he.code) from None
        except UpstreamError as ue:
            last_status = ue.status
            last_err = str(ue)
            _log(last_err)
            if ue.status in (429, 403):
                apply_cooldown(email, family, ue.status)
            elif ue.status == 401:
                invalidate_credentials(email)
            else:
                # 400/404 are request-level problems: rotating accounts cannot
                # help, so surface them straight away.
                raise
            continue
        except urllib.error.HTTPError as he:
            err_text = he.read().decode("utf-8", errors="ignore")
            last_status = he.code
            last_err = f"Upstream HTTP {he.code} on {email}: {err_text}"
            _log(last_err)
            raise UpstreamError(last_err, he.code)
        except Exception as e:
            last_err = f"Request error on {email}: {e}"
            last_status = 502
            _log(last_err)
            continue

        with _cache_lock:
            _consecutive_failures[cooldown_key] = 0

        if idx != active_idx:
            data["activeIndex"] = idx
            data["activeIndexByFamily"] = {"claude": idx, "gemini": idx}
            try:
                save_accounts_data(data)
            except Exception:
                pass

        return resp

    if skipped_for_cooldown and last_status == 429:
        raise UpstreamError(f"All accounts are rate limited. {last_err}", 429)
    raise UpstreamError(f"All accounts failed. Last error: {last_err}", last_status)


# ── Proxy HTTP Handler ─────────────────────────────────────────
class AntigravityProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AntigravityProxy/2.0"

    def log_message(self, format, *args):
        _log("%s - %s" % (self.address_string(), format % args))

    # ---- low level helpers -----------------------------------
    def _send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status, message, err_type="upstream_error"):
        self._send_json(status, {"error": {"message": message, "type": err_type, "code": status}})

    def _begin_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        # Close-delimited: without this the handler holds the socket open and
        # (on a threaded server) leaks a thread per request.
        self.send_header("Connection", "close")
        self.end_headers()

    def _write_sse(self, obj):
        self.wfile.write(f"data: {json.dumps(obj)}\n\n".encode("utf-8"))
        self.wfile.flush()

    # ---- routes ----------------------------------------------
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/v1/models", "/models"):
            models = [{
                "id": mid,
                "object": "model",
                "created": 1782210769,
                "owned_by": "antigravity",
            } for mid in list_public_models()]
            self._send_json(200, {"object": "list", "data": models})
            return

        if path in ("/health", "/v1/health"):
            self._send_json(200, {"status": "ok", "port": PROXY_PORT})
            return

        self._send_error_json(404, f"Unknown path {path}", "invalid_request_error")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path not in ("/v1/chat/completions", "/chat/completions"):
            self._send_error_json(404, f"Unknown path {path}", "invalid_request_error")
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
            req_json = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except Exception as e:
            self._send_error_json(400, f"Invalid JSON body: {e}", "invalid_request_error")
            return

        requested_model = req_json.get("model") or "gemini-3.8-flash"
        stream = bool(req_json.get("stream", False))

        try:
            mapped_model, model_info = resolve_model(requested_model)
        except ValueError as e:
            self._send_error_json(404, str(e), "invalid_request_error")
            return

        family = model_family(mapped_model)
        _log(f"{requested_model} -> {mapped_model} (family={family}, stream={stream})")

        try:
            resp = call_upstream(req_json, mapped_model, model_info, family, stream)
        except UpstreamError as e:
            self._send_error_json(e.status, str(e))
            return
        except Exception as e:
            self._send_error_json(500, f"Proxy error: {e}")
            return

        try:
            if stream:
                self._relay_stream(resp, req_json, requested_model)
            else:
                self._relay_once(resp, requested_model)
        finally:
            try:
                resp.close()
            except Exception:
                pass

    # ---- response relays -------------------------------------
    def _relay_once(self, resp, requested_model):
        payload = json.loads(resp.read().decode("utf-8"))
        response_obj = payload.get("response", {}) or {}
        candidates = response_obj.get("candidates") or []

        text, calls = ("", [])
        finish_raw = None
        if candidates:
            text, calls = extract_parts(candidates[0])
            finish_raw = candidates[0].get("finishReason")

        message = {"role": "assistant", "content": text or None}
        if calls:
            message["tool_calls"] = [make_tool_call(c, i) for i, c in enumerate(calls)]
            finish_reason = "tool_calls"
        else:
            message["content"] = text
            finish_reason = _FINISH_REASONS.get(finish_raw, "stop")

        out = {
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": requested_model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        }
        usage = convert_usage(response_obj.get("usageMetadata"))
        if usage:
            out["usage"] = usage
        self._send_json(200, out)

    def _relay_stream(self, resp, req_json, requested_model):
        self._begin_sse()

        chunk_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())
        include_usage = bool((req_json.get("stream_options") or {}).get("include_usage"))

        def envelope(delta, finish_reason=None):
            return {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": requested_model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
            }

        usage = None
        finish_reason = "stop"
        tool_index = 0
        saw_tool_call = False
        opened = False

        try:
            for raw in resp:
                line = raw.decode("utf-8", errors="ignore").strip()
                if not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if not data_str or data_str == "[DONE]":
                    continue
                try:
                    payload = json.loads(data_str)
                except Exception:
                    continue

                response_obj = payload.get("response", {}) or {}
                usage = convert_usage(response_obj.get("usageMetadata")) or usage

                for candidate in response_obj.get("candidates") or []:
                    text, calls = extract_parts(candidate)

                    if not opened and (text or calls):
                        self._write_sse(envelope({"role": "assistant", "content": ""}))
                        opened = True

                    if text:
                        self._write_sse(envelope({"content": text}))

                    for call in calls:
                        saw_tool_call = True
                        self._write_sse(envelope({"tool_calls": [make_tool_call(call, tool_index)]}))
                        tool_index += 1

                    raw_finish = candidate.get("finishReason")
                    if raw_finish:
                        finish_reason = _FINISH_REASONS.get(raw_finish, "stop")

            if saw_tool_call:
                finish_reason = "tool_calls"
            if not opened:
                self._write_sse(envelope({"role": "assistant", "content": ""}))

            self._write_sse(envelope({}, finish_reason))

            if include_usage:
                self._write_sse({
                    "id": chunk_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": requested_model,
                    "choices": [],
                    "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                })

            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            _log("client disconnected mid-stream")
        except Exception as e:
            _log(f"stream relay error: {e}")
            try:
                self._write_sse(envelope({"content": f"\n[proxy stream error: {e}]"}, "stop"))
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except Exception:
                pass
        finally:
            self.close_connection = True


# ── Background Server Thread ──────────────────────────────────
def is_port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('127.0.0.1', port)) == 0

def start_background_proxy(port=PROXY_PORT):
    if is_port_in_use(port):
        return None

    server = None
    try:
        # Threaded: a single slow generation must not block every other request.
        ThreadingHTTPServer.allow_reuse_address = True
        server = ThreadingHTTPServer(('127.0.0.1', port), AntigravityProxyHandler)
        server.daemon_threads = True
    except Exception as e:
        _log(f"could not bind proxy port {port}: {e}")
        return None

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    _log(f"proxy listening on 127.0.0.1:{port}")
    return server

# Launch proxy immediately
load_thought_signatures()
threading.Thread(target=_thought_sig_flusher, daemon=True).start()
start_background_proxy()

# ── Interactive CLI & OAuth Manager ─────────────────────────
auth_code = None
server_instance = None

class OAuthCallbackHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        global auth_code
        parsed_url = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed_url.query)
        
        if "code" in query:
            auth_code = query["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"""
            <html>
                <body style="font-family: sans-serif; text-align: center; padding-top: 50px;">
                    <h2 style="color: #2e7d32;">Authentication Successful!</h2>
                    <p>You can close this tab and return to the terminal.</p>
                </body>
            </html>
            """)
            threading.Thread(target=lambda: server_instance.shutdown()).start()
        else:
            self.send_response(400)
            self.end_headers()

def start_local_server():
    global server_instance
    handler = OAuthCallbackHandler
    socketserver.TCPServer.allow_reuse_address = True
    server_instance = socketserver.TCPServer(("127.0.0.1", 51121), handler)
    server_instance.serve_forever()

def perform_oauth_flow():
    global auth_code
    auth_code = None
    
    t = threading.Thread(target=start_local_server)
    t.start()
    
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent"
    }
    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)
    
    print("\nOpening browser for Google login...")
    webbrowser.open(auth_url)
    
    print("Waiting for callback on port 51121 (timeout 60s)...")
    t.join(timeout=60)
    
    return auth_code

def exchange_code_for_tokens(code):
    url = "https://oauth2.googleapis.com/token"
    data = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code"
    }).encode("utf-8")
    
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))

def fetch_user_email(access_token):
    url = "https://www.googleapis.com/oauth2/v2/userinfo"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        return data.get("email")

def run_login_silent():
    code = perform_oauth_flow()
    if not code:
        return "Login failed: OAuth timeout or cancelled."
        
    try:
        tokens = exchange_code_for_tokens(code)
        access_token = tokens["access_token"]
        refresh_token = tokens["refresh_token"]
        email = fetch_user_email(access_token)
        
        if not email:
            return "Login failed: could not fetch user email."
            
        data = load_accounts_data()
        accounts = data.get("accounts", [])
        
        existing_idx = next((i for i, a in enumerate(accounts) if a.get("email") == email), None)
        import time
        now_ms = int(time.time() * 1000)
        
        account_entry = {
            "email": email,
            "refreshToken": refresh_token,
            "addedAt": now_ms,
            "lastUsed": now_ms,
            "enabled": True
        }
        
        if existing_idx is not None:
            accounts[existing_idx] = account_entry
            msg = f"Account {email} updated successfully!"
        else:
            accounts.append(account_entry)
            msg = f"Account {email} added successfully!"
            
        new_idx = existing_idx if existing_idx is not None else len(accounts) - 1
        data["activeIndex"] = new_idx
        data["activeIndexByFamily"] = {"claude": new_idx, "gemini": new_idx}
        data["accounts"] = accounts
        save_accounts_data(data)
        
        with _cache_lock:
            _token_cache[email] = (access_token, time.time() + 3300)
            _project_cache[email] = load_project_id(access_token)
            
        return f"✓ {msg} It is now set as the active account for Hermes Agent."
    except Exception as e:
        return f"✗ Login error: {str(e)}"

def run_login():
    code = perform_oauth_flow()
    if not code:
        print("Login failed.")
        return
        
    try:
        tokens = exchange_code_for_tokens(code)
        access_token = tokens["access_token"]
        refresh_token = tokens["refresh_token"]
        email = fetch_user_email(access_token)
        
        if not email:
            print("Failed to fetch user email.")
            return
            
        data = load_accounts_data()
        accounts = data.get("accounts", [])
        
        existing_idx = next((i for i, a in enumerate(accounts) if a.get("email") == email), None)
        import time
        now_ms = int(time.time() * 1000)
        
        account_entry = {
            "email": email,
            "refreshToken": refresh_token,
            "addedAt": now_ms,
            "lastUsed": now_ms,
            "enabled": True
        }
        
        if existing_idx is not None:
            accounts[existing_idx] = account_entry
            print(f"\nAccount {email} updated successfully!")
        else:
            accounts.append(account_entry)
            print(f"\nAccount {email} added successfully!")
            
        data["accounts"] = accounts
        save_accounts_data(data)
    except Exception as e:
        print(f"\nError during login: {str(e)}")

def run_list_and_select():
    data = load_accounts_data()
    accounts = data.get("accounts", [])
    if not accounts:
        print("\nNo accounts configured.")
        return
        
    active_idx = data.get("activeIndex", 0)
    
    print("\nConfigured Accounts:")
    for idx, acc in enumerate(accounts):
        marker = "-> " if idx == active_idx else "   "
        status = "enabled" if acc.get("enabled", True) else "disabled"
        print(f"{marker}{idx + 1}. {acc.get('email')} ({status})")
        
    choice = input("\nEnter account index to select active, or press Enter to cancel: ").strip()
    if not choice:
        return
        
    try:
        idx = int(choice) - 1
        if idx >= 0 and idx < len(accounts):
            data["activeIndex"] = idx
            data["activeIndexByFamily"] = {"claude": idx, "gemini": idx}
            save_accounts_data(data)
            print(f"Selected active account: {accounts[idx].get('email')}")
        else:
            print("Invalid index.")
    except ValueError:
        print("Invalid input.")

def run_remove():
    data = load_accounts_data()
    accounts = data.get("accounts", [])
    if not accounts:
        print("\nNo accounts configured.")
        return
        
    print("\nAccounts:")
    for idx, acc in enumerate(accounts):
        print(f"  {idx + 1}. {acc.get('email')}")
        
    choice = input("\nEnter account index to remove, or press Enter to cancel: ").strip()
    if not choice:
        return
        
    try:
        idx = int(choice) - 1
        if idx >= 0 and idx < len(accounts):
            removed = accounts.pop(idx)
            active_idx = data.get("activeIndex", 0)
            if active_idx >= len(accounts):
                active_idx = max(0, len(accounts) - 1)
            data["activeIndex"] = active_idx
            data["activeIndexByFamily"] = {"claude": active_idx, "gemini": active_idx}
            data["accounts"] = accounts
            save_accounts_data(data)
            print(f"Removed account: {removed.get('email')}")
        else:
            print("Invalid index.")
    except ValueError:
        print("Invalid input.")

def get_quota_summary_string(email, access_token, project_id):
    url = f"{ENDPOINT}/v1internal:retrieveUserQuotaSummary"
    body = json.dumps({"project": project_id}).encode("utf-8")
    
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "User-Agent": "antigravity/windows/amd64",
        "X-Goog-Api-Client": "google-cloud-sdk vscode_cloudshelleditor/0.1",
        "Client-Metadata": '{"ideType":"ANTIGRAVITY","platform":"PLATFORM_UNSPECIFIED","pluginType":"GEMINI"}'
    }
    
    lines = []
    try:
        req = urllib.request.Request(url, data=body, headers=headers)
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            
            def format_duration(target_str):
                if not target_str: return "N/A"
                try:
                    from datetime import datetime, timezone
                    target = datetime.fromisoformat(target_str.replace("Z", "+00:00"))
                    now = datetime.now(timezone.utc)
                    delta = target - now
                    if delta.total_seconds() <= 0: return "now"
                    hours = int(delta.total_seconds() // 3600)
                    minutes = int((delta.total_seconds() % 3600) // 60)
                    if hours >= 24:
                        return f"{hours // 24}d {hours % 24}h"
                    if hours > 0:
                        return f"{hours}h {minutes}m"
                    return f"{minutes}m"
                except Exception:
                    return target_str
            
            for group in data.get("groups", []):
                lines.append(f"\n  [{group.get('displayName')}]")
                buckets = group.get("buckets", [])
                for bucket in buckets:
                    if bucket.get("disabled"):
                        lines.append(f"  - {bucket.get('displayName')}: N/A (does not apply)")
                    else:
                        pct = int(bucket.get("remainingFraction", 0) * 100)
                        reset_in = format_duration(bucket.get("resetTime"))
                        lines.append(f"  - {bucket.get('displayName')}: {pct}% (resets: {reset_in})")
    except Exception as e:
        lines.append(f"  Failed to fetch quota: {str(e)}")
    return "\n".join(lines)

def run_quota_silent():
    data = load_accounts_data()
    accounts = data.get("accounts", [])
    if not accounts:
        return "No accounts configured."
        
    outputs = []
    for acc in accounts:
        email = acc.get("email")
        disabled_str = " (disabled)" if not acc.get("enabled", True) else ""
        outputs.append(f"Account: {email}{disabled_str}")
        try:
            token = refresh_token(acc["refreshToken"])
            project_id = load_project_id(token)
            summary = get_quota_summary_string(email, token, project_id)
            outputs.append(summary)
        except Exception as e:
            outputs.append(f"  Error: {str(e)}")
        outputs.append("-" * 35)
    return "\n".join(outputs)

def fetch_and_print_quota_summary(email, access_token, project_id):
    url = f"{ENDPOINT}/v1internal:retrieveUserQuotaSummary"
    body = json.dumps({"project": project_id}).encode("utf-8")
    
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "User-Agent": "antigravity/windows/amd64",
        "X-Goog-Api-Client": "google-cloud-sdk vscode_cloudshelleditor/0.1",
        "Client-Metadata": '{"ideType":"ANTIGRAVITY","platform":"PLATFORM_UNSPECIFIED","pluginType":"GEMINI"}'
    }
    
    try:
        req = urllib.request.Request(url, data=body, headers=headers)
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            
            def format_duration(target_str):
                if not target_str: return "N/A"
                try:
                    from datetime import datetime, timezone
                    target = datetime.fromisoformat(target_str.replace("Z", "+00:00"))
                    now = datetime.now(timezone.utc)
                    delta = target - now
                    if delta.total_seconds() <= 0: return "now"
                    hours = int(delta.total_seconds() // 3600)
                    minutes = int((delta.total_seconds() % 3600) // 60)
                    if hours >= 24:
                        return f"{hours // 24}d {hours % 24}h"
                    if hours > 0:
                        return f"{hours}h {minutes}m"
                    return f"{minutes}m"
                except Exception:
                    return target_str
            
            for group in data.get("groups", []):
                print(f"\n  ┌─ {group.get('displayName')}")
                buckets = group.get("buckets", [])
                for b_idx, bucket in enumerate(buckets):
                    connector = "└─" if b_idx == len(buckets) - 1 else "├─"
                    if bucket.get("disabled"):
                        print(f"  │  {connector} {bucket.get('displayName').ljust(20)} N/A (does not apply)")
                    else:
                        pct = int(bucket.get("remainingFraction", 0) * 100)
                        reset_in = format_duration(bucket.get("resetTime"))
                        print(f"  │  {connector} {bucket.get('displayName').ljust(20)} {pct}% (resets: {reset_in})")
    except Exception as e:
        print(f"  ❌ Failed to fetch quota: {str(e)}")

def run_quota():
    data = load_accounts_data()
    accounts = data.get("accounts", [])
    if not accounts:
        print("\nNo accounts configured.")
        return
        
    print("\n📊 Checking quotas for all accounts...")
    for idx, acc in enumerate(accounts):
        email = acc.get("email")
        disabled_str = " (disabled)" if not acc.get("enabled", True) else ""
        print(f"\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        print(f"  {email}{disabled_str}")
        print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        
        try:
            token = refresh_token(acc["refreshToken"])
            project_id = load_project_id(token)
            fetch_and_print_quota_summary(email, token, project_id)
        except Exception as e:
            print(f"  ❌ Error: {str(e)}")
    print("")

def run_interactive_menu():
    while True:
        print("\n" + "="*45)
        print("     Google Antigravity Accounts Manager")
        print("="*45)
        print("  1. List accounts / Select active account")
        print("  2. Log in a new account (OAuth)")
        print("  3. Check quotas for all accounts")
        print("  4. Remove an account")
        print("  5. Exit")
        print("="*45)
        
        choice = input("\nChoose option: ").strip()
        if choice == "1":
            run_list_and_select()
        elif choice == "2":
            run_login()
        elif choice == "3":
            run_quota()
        elif choice == "4":
            run_remove()
        elif choice == "5":
            break
        else:
            print("Invalid option.")

def handle_cli(args):
    cmd = getattr(args, "antigravity_command", None)
    if cmd == "login":
        run_login()
    elif cmd == "list":
        run_list_and_select()
    elif cmd == "remove":
        run_remove()
    elif cmd == "quota":
        run_quota()
    else:
        run_interactive_menu()

def setup_argparse(subparser):
    subs = subparser.add_subparsers(dest="antigravity_command")
    subs.add_parser("login", help="Log in a new Google Antigravity account")
    subs.add_parser("list", help="List and select active account")
    subs.add_parser("remove", help="Remove a configured account")
    subs.add_parser("quota", help="Check live quotas for all accounts")

def handle_slash_command(raw_args: str) -> str:
    import subprocess
    import shutil
    import sys
    print("\nLaunching Google Antigravity Accounts Manager in a new terminal window...")
    cmd = "hermes antigravity-mrhisyammm"
    try:
        if sys.platform == "win32":
            subprocess.Popen(f'start powershell -Command "{cmd}"', shell=True)
        elif sys.platform == "darwin":
            script = f'tell application "Terminal" to do script "{cmd}"'
            subprocess.Popen(["osascript", "-e", script, "-e",
                              'tell application "Terminal" to activate'])
        else:
            for term in ("x-terminal-emulator", "gnome-terminal", "konsole", "xterm"):
                if shutil.which(term):
                    subprocess.Popen([term, "-e", "bash", "-lc", cmd])
                    break
            else:
                return ("✗ No terminal emulator found. Please run "
                        f"'{cmd}' manually in a new terminal.")
        return "✓ Google Antigravity Accounts Manager opened in a new terminal window. Manage your accounts there, then return here."
    except Exception as e:
        return f"✗ Failed to open terminal window: {str(e)}\nPlease run '{cmd}' manually in a new terminal."

def _mock_pre_llm_call(*args, **kwargs):
    return None

def handle_login_slash(raw_args: str) -> str:
    return run_login_silent()

def handle_quota_slash(raw_args: str) -> str:
    return run_quota_silent()

def register(ctx):
    # Register the CLI subcommand tree
    ctx.register_cli_command(
        name="antigravity-mrhisyammm",
        help="Manage Google Antigravity accounts and quotas",
        setup_fn=setup_argparse,
        handler_fn=handle_cli
    )
    # Register the in-session slash command /antigravity (spawns external terminal)
    ctx.register_command(
        "antigravity-mrhisyammm",
        handler=handle_slash_command,
        description="Open the Google Antigravity accounts manager in a new external terminal window"
    )
    # Register the in-session slash command /antigravity-login (direct login)
    ctx.register_command(
        "antigravity-mrhisyammm-login",
        handler=handle_login_slash,
        description="Directly log in a new Google Antigravity account (Works in Desktop GUI & CLI)"
    )
    # Register the in-session slash command /antigravity-quota (direct print quota)
    ctx.register_command(
        "antigravity-mrhisyammm-quota",
        handler=handle_quota_slash,
        description="Directly display quota summary (Works in Desktop GUI & CLI)"
    )
    # Register the hook declared in plugin.yaml to pass validation
    ctx.register_hook("pre_llm_call", _mock_pre_llm_call)
