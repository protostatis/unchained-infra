# MCP Local Browser Guide (Production)

Last validated: March 11, 2026.

This guide shows how to drive your local Chrome through the production MCP
endpoint (`https://api.unchainedsky.com/mcp`).

## What This Enables

- Keep your real browser session (cookies, extensions, 2FA state, IP)
- Use MCP tools (`cdp_navigate`, `ddm`, `cdp_click`, `js_eval`, `cdp_set_file`) from any
  MCP-compatible client
- Run multiple Chrome profiles simultaneously (e.g. personal + work + social)
- Discover tabs opened by provisioned Chrome flows, including OAuth popups
- Zero-config path if you already installed the agent via `curl | bash`
- Avoid setting up and maintaining a custom Playwright skill for each tool host

## Prerequisites

- A valid Unchained API key (`uc_live_...`) and local Chrome
- Either:
  - installed agent already running (`~/unchained-agent`, recommended)
  - or local repo checkout + `uv` for manual bridge start

If you already run the packaged agent, reuse its existing API key from
`~/unchained-agent/.env`.

For local self-hosted dev, you can mint a key:

```bash
cd unchained-infra/unchained
uv run python -c "from auth import Auth; print(Auth().create_key('u-dev'))"
```

## Fast Path (Zero Config): Existing Installed Agent

If the agent is already installed, you do not need to run `uv run ... chrome_bridge.py`
from source.

1. Ensure your installed agent is running:

```bash
cd ~/unchained-agent
./start.sh --daemon
```

2. Point your MCP client to:

```text
https://api.unchainedsky.com/mcp
```

3. That's it — `agent_id` is auto-detected from your API key. You can
   verify connectivity with:

```bash
API_KEY="$(grep '^UNCHAINED_API_KEY=' ~/unchained-agent/.env | cut -d= -f2-)"
curl -sS https://api.unchainedsky.com/api/agents \
  -H "Authorization: Bearer $API_KEY"
```

## Manual Path: Start Bridge from Repo

```bash
cd unchained-infra/unchained
UNCHAINED_RELAY_URL=wss://api.unchainedsky.com/tunnel \
UNCHAINED_API_KEY=<your_uc_live_key> \
uv run python chrome_bridge.py start --no-headless
```

Expected output includes:

```text
[agent] authenticated as claude-xxxxxxxx
```

The `agent_id` is auto-detected from your API key — you do not need to save or
pass it to MCP tools.

## (Optional) Verify Agent Connectivity

```bash
curl -sS https://api.unchainedsky.com/api/agents \
  -H "Authorization: Bearer <your_uc_live_key>"
```

You should see your `agent_id` in the response list.

## Connect MCP Client

Example (Claude Code):

```bash
claude --mcp-server https://api.unchainedsky.com/mcp
```

Then call tools directly — `agent_id` is auto-detected from your API key:

- `cdp_navigate` with `url=https://slickdeals.net`
- `js_eval` with `expression=document.title`
- `ddm` with `flags=--text --find Slickdeals`
- `list_provisioned_tabs` after a provisioned login flow opens a popup tab

## Provisioned Chrome Tabs And OAuth Popups

When a provisioned Chrome flow opens a second tab or popup window, the default
profile-level `agent_id` is not enough to target it. Use the provisioned-tab
discovery tool to get a tab-scoped ID first.

Typical flow:

1. Start the provisioned flow in the app or with your existing MCP/browser tools.
2. Call `list_provisioned_tabs` to discover tabs for the provisioned Chrome slot.
3. Copy the returned `prov-<slot>-<tab_id>` value.
4. Pass that value as `tab_id` to `ddm`, `cdp_click`, `cdp_type`, `js_eval`, or other tab-aware tools.

Example:

```text
list_provisioned_tabs

Slot ab12 (profile: Profile 5, 2 tabs):
  prov-ab12-AAA111...  X / Login  https://x.com/i/flow/login
  prov-ab12-BBB222...  Sign in - Google  [popup]  https://accounts.google.com/signin

ddm tab_id=prov-ab12-BBB222...
cdp_click x=742 y=508 tab_id=prov-ab12-BBB222...
```

Notes:

- `list_connected_agents` finds bridge/profile agents. `list_provisioned_tabs` finds tabs inside a provisioned Chrome instance.
- `ddm --tabs` now includes popup windows and marks them with `[popup]`.
- Auto tab selection still prefers normal page tabs. Use an explicit `prov-...` `tab_id` when you need the popup itself.

## Multi-Profile Support

You can connect multiple Chrome profiles simultaneously under the same API key.
Each profile gets its own agent on the relay, and you target it by passing the
profile name in the `agent_id` parameter of any MCP tool.

### Start a second bridge with a named profile

```bash
# Terminal 1 — default profile (already running)
cd ~/unchained-agent && ./start.sh --daemon

# Terminal 2 — "facebook" profile on a separate CDP port
UNCHAINED_RELAY_URL=wss://api.unchainedsky.com/tunnel \
UNCHAINED_API_KEY=<your_uc_live_key> \
uv run python chrome_bridge.py start --no-headless \
  --profile facebook --port 9223
```

Each profile gets its own Chrome data directory (`~/.unchained/chrome_<name>/`)
with separate cookies, sessions, and extensions.

### Discover connected profiles

Use the `list_connected_agents` MCP tool to see all connected agents:

```text
Connected agents:
  claude-abc12345 (profile: default)
  claude-abc12345-facebook (profile: facebook)
```

### Target a specific profile

Pass the profile name in the `agent_id` parameter of any tool:

```text
cdp_navigate url=https://facebook.com agent_id=facebook
ddm agent_id=facebook
```

When `agent_id` is empty (the default), tools target the default profile.

## Agent Prompt Snippet (AGENTS.md / CLAUDE.md)

Use this subsection in your agent instruction file so tool behavior stays
consistent:

```md
### Unchained MCP Tool Use

- MCP endpoint: `https://api.unchainedsky.com/mcp`
- `agent_id` is auto-detected from your API key — you do not need to pass it.
- To target a specific Chrome profile, pass the profile name in `agent_id` (e.g. `agent_id=facebook`).
- Use `list_connected_agents` to discover all connected profiles.
- Use `list_provisioned_tabs` after profile provisioning or OAuth flows to discover `prov-<slot>-<tab_id>` values for new tabs and popups.

#### DDM-First Methodology

Every browsing task follows this pipeline:

1. Step 1: ORIENT — `navigate` and `click` already return DDM page layout in their output. Read that. Do not call `ddm` separately after them. Only call `ddm` separately after `type`, or for `--text`, `--at x,y`, `--find`, `--js`.
2. Step 2: IDENTIFY — `ddm --at x,y` on targets from Step 1. It returns href, class, text, and aria attributes for elements you want to interact with.
3. Step 3: CLASSIFY — `intel --probe` on unknown SPAs. This fingerprints the page and ranks extraction strategies. It reveals framework, data stores, and shadow DOM.
4. Step 4: ACT — Use coordinates from DDM to click, or navigate to URLs from `--at`. For SPA widgets, use `js` with `.click()` on the element.
5. Step 5: VERIFY — After `navigate` or `click`, check the `"=== Page Layout ==="` section in their output. After `type` or other actions, run `ddm` to verify.
6. Step 6: EXTRACT — Choose method based on page type:
   - Simple text: `ddm --text --max 5000`
   - Shadow DOM: `intel --extract` with `host_attrs`
   - SPA with data store: `intel --stores` -> `intel --find-paths` -> `js`
   - Structured data: `js` with `querySelectorAll`
   - data-testid rich pages: `intel --extract` with `data_testid`

#### Guardrails

- Use `cdp_navigate`, `cdp_click`, `cdp_type` for actions.
- Use `js_eval` for deterministic reads (title, URLs, structured DOM data).
- Use `cdp_set_file` to upload files to `<input type="file">` elements without the OS picker.
- Use `cdp_screenshot` only for visual-only states (CAPTCHA, image verification).
- If you get `Agent ... not connected`, stop and ask user to start/restart the local bridge.
```

This keeps instruction quality high without duplicating private-core internals.

## Raw MCP Smoke Test (No SDK)

Use this when debugging handshake/tool issues:

```bash
python - <<'PY'
import json, urllib.request

URL = "https://api.unchainedsky.com/mcp"
API_KEY = "uc_live_..."   # your API key

def post(payload, sid=None):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {API_KEY}",
    }
    if sid:
        headers["mcp-session-id"] = sid
    req = urllib.request.Request(
        URL, data=json.dumps(payload).encode(), method="POST", headers=headers
    )
    with urllib.request.urlopen(req, timeout=45) as r:
        return r.status, r.headers, r.read().decode("utf-8", "replace")

init = {
    "jsonrpc": "2.0",
    "id": "1",
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "manual-smoke", "version": "0.1"},
    },
}
status, headers, _ = post(init)
sid = headers.get("Mcp-Session-Id") or headers.get("mcp-session-id")
print("initialize:", status, "session:", sid)

post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}, sid)

nav = {
    "jsonrpc": "2.0",
    "id": "2",
    "method": "tools/call",
    "params": {
        "name": "cdp_navigate",
        "arguments": {"url": "https://example.com"},
    },
}
status, _, body = post(nav, sid)
print("navigate:", status)
print(body[:500])
PY
```

## Why This Is Better (For Product Usage) Than a Playwright Skill

For Unchained's main user flow, MCP + local bridge is the better default:

- Auth reliability: uses the user's already-authenticated browser session
- Less setup drift: one MCP server endpoint, no per-skill wrapper maintenance
- Cross-client reuse: same MCP tools work with multiple MCP hosts/agents
- Lower operational overhead: no separate browser sandbox lifecycle to manage
- Better alignment with Unchained moat: "your browser, your identity, your IP"

Playwright skill still has valid use cases:

- deterministic CI test automation in isolated environments
- DOM/visual assertions against a disposable browser context
- scripted QA flows where local user identity is not required

## Troubleshooting

- `404` on `/mcp`: confirm deploy includes Caddy route for exact `/mcp`
- `4004 Agent ... not connected`: bridge is not running or wrong `agent_id`
- `401 Missing Authorization header` on `/api/agents`: add Bearer API key
- Bridge reconnect loop: verify relay URL and API key are valid
