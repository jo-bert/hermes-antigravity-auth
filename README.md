# hermes-antigravity-auth

Google Antigravity IDE OAuth auth plugin and proxy provider for **Hermes Agent** (developed by Nous Research). This is the **official Hermes version** of the [@mrhisyammm/opencode-antigravity-auth](https://github.com/mrhisyammm/opencode-antigravity-auth) plugin!

Allows you to use Gemini 3.8 Flash, Gemini 3.6 Flash, Gemini Pro, and Claude 4.6 (Opus/Sonnet) for **FREE** inside Hermes Agent using your Google OAuth credentials pool with **isolated cooldowns** and **exponential backoff auto-rotation**!

---

## ⚡ Quick One-Line Installation

Open your terminal and run the command corresponding to your operating system to automatically download, install, and configure the plugin:

- **Windows (PowerShell - run as Administrator):**

  ```powershell
  Set-ExecutionPolicy Bypass -Scope Process -Force; irm https://raw.githubusercontent.com/mrhisyammm/hermes-antigravity-auth/main/install.ps1 | iex
  ```

- **macOS / Linux:**

  ```bash
  curl -fsSL https://raw.githubusercontent.com/mrhisyammm/hermes-antigravity-auth/main/install.sh | bash
  ```

---

## What You Get (Features)

- **Claude Opus 4.6, Sonnet 4.6**, **Gemini 3.8 / 3.6 / 3.5 Flash**, **Gemini Pro** and **GPT-OSS 120B** via Google OAuth
- **Full Tool Calling**: Function calls, tool results, parallel calls and structured outputs (`response_format`) are translated in both directions, so Hermes' agent loop works on every model.
- **Self-Contained Proxy**: Runs its own lightweight proxy server locally in the background (`port 8999`), making it 100% plug-and-play without any external software dependencies.
- **Multi-Account Support**: Add multiple Google accounts; requests auto-rotate dynamically.
- **Isolated Cooldowns**: Cooldowns are tracked separately per account and model family (`claude` vs `gemini`). If Claude gets rate-limited, you can still use Gemini models on the same account!
- **Progressive Cooldowns (Exponential Backoff)**: Cooldown duration escalates based on consecutive failures (1 min → 5 mins → 30 mins → 2 hours) to avoid Google account blocks/verification checks and ensure long-term account safety.
- **Interactive TUI Manager**: Run account operations via `/antigravity-mrhisyammm` which spawns a separate terminal window to handle your logins, active account selection, and quota checks seamlessly without input hijacking.
- **Live Model Discovery**: The model list is read from Antigravity at runtime (`fetchAvailableModels`) instead of a hardcoded table, so new models appear automatically and Google's renames stop breaking the plugin. Friendly names are mapped onto whatever id Antigravity currently serves (e.g. `gemini-3.8-flash` → `gemini-3.8-flash-tiered`), with fuzzy matching as a backstop.
- **Concurrent Requests**: The proxy is multi-threaded, so a long generation no longer blocks every other request (including Hermes' own session-title call).

---

<details open>
<summary><b>⚠️ Terms of Service Warning — Read Before Installing</b></summary>

> [!CAUTION]
> Using this plugin (and any proxy for Antigravity) violates Google's Terms of Service. A number of users have reported their Google accounts being **banned** or **shadow-banned** (restricted access without explicit notification).
>
> **By using this plugin, you acknowledge:**
>
> - This is an unofficial tool not endorsed by Google
> - Your account may be suspended or permanently banned
> - You assume all risks associated with using this plugin
>

</details>

---

## How to Use

### 1. Chat Commands (CLI & Desktop GUI)

The following commands work directly inside your Hermes session (both in the terminal CLI chat box and the Desktop GUI app chat window):

- **`/antigravity-mrhisyammm`**: Launches the **Google Antigravity Accounts Manager** interactive menu in a new PowerShell window. Select option `2` to log in, `1` to choose your active account, `3` to check quotas, or `4` to remove accounts.
- **`/antigravity-mrhisyammm-login`**: Directly triggers the browser login flow and sets the account active, printing the result in your chat.
- **`/antigravity-mrhisyammm-quota`**: Directly fetches and prints your live Weekly and 5-Hour limits right in your chat window.

### 2. Select Model

Run the model configuration wizard:

```bash
hermes model --refresh
```

Select **`Google Antigravity`** as your provider, and then choose any of the models (e.g. `gemini-3.8-flash`, `gemini-3.6-flash-high`, `claude-opus-4-6-thinking`, or `claude-sonnet-4-6`).

The list is fetched live from Antigravity. To see exactly what your account can reach right now:

```bash
curl -s http://127.0.0.1:8999/v1/models | python3 -m json.tool
```

---

## Technical Details

### Configuration Path

The plugin shares the exact same accounts file as the OpenCode version, meaning if you already logged in on OpenCode, your accounts will automatically load in Hermes:

- **Accounts Database**: `~/.config/opencode/antigravity-accounts.json` (on Windows, `~` is your home directory)
- **Fallback Directory**: `~/AppData/Local/hermes/antigravity-accounts.json` (if OpenCode is not installed on the machine, the plugin runs completely self-contained here).

### Model Names

Antigravity's own ids are not the marketing names, and they change. The proxy resolves a requested name in this order: exact id in the live catalogue → alias table → Google's deprecation redirect → fuzzy prefix match. If nothing matches you get a `404` listing every model your account can actually use, rather than a silent substitution.

| You ask for | Antigravity serves |
| --- | --- |
| `gemini-3.8-flash` | `gemini-3.8-flash-tiered` |
| `gemini-3.6-flash` | `gemini-3.6-flash-high` |
| `gemini-3.5-flash` | `gemini-3.5-flash-low` |
| `gemini-3.1-pro-high` | `gemini-pro-agent` (Google-declared successor) |
| `claude-sonnet-4-6-thinking` | `claude-sonnet-4-6` |

### Troubleshooting

Run the proxy with verbose logging to see model resolution, account rotation and upstream errors:

```bash
ANTIGRAVITY_DEBUG=1 hermes gateway restart
```

Health check: `curl http://127.0.0.1:8999/health`
