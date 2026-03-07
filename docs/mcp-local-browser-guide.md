# MCP Local Browser Guide (Production)

Last validated: March 7, 2026.

This guide shows how to drive your local Chrome through the production MCP
endpoint (`https://api.unchainedsky.com/mcp`).

## What This Enables

- Keep your real browser session (cookies, extensions, 2FA state, IP)
- Use MCP tools (`cdp_navigate`, `ddm`, `cdp_click`, `js_eval`) from any
  MCP-compatible client
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

3. Get your connected `agent_id` (from same API key in `~/unchained-agent/.env`):

```bash
API_KEY="$(grep '^UNCHAINED_API_KEY=' ~/unchained-agent/.env | cut -d= -f2-)"
curl -sS https://api.unchainedsky.com/api/agents \
  -H "Authorization: Bearer $API_KEY"
```

Use the returned `agent_id` in MCP tool calls.

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

Save that `agent_id`.

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

Then call tools with your `agent_id`, for example:

- `cdp_navigate` with `url=https://slickdeals.net`
- `js_eval` with `expression=document.title`
- `ddm` with `flags=--text --find Slickdeals`

## DDM-First Methodology

Every browsing task follows this pipeline:

1. Step 1: ORIENT — `navigate` and `click` already return DDM page layout in
   their output. Read that. Do not call `ddm` separately after them. Only call
   `ddm` separately after `type`, or for `--text`, `--at x,y`, `--find`, `--js`.
2. Step 2: IDENTIFY — `ddm --at x,y` on targets from Step 1. It returns href,
   class, text, and aria attributes for elements you want to interact with.
3. Step 3: CLASSIFY — `intel --probe` on unknown SPAs. This fingerprints the
   page and ranks extraction strategies. It reveals framework, data stores, and
   shadow DOM.
4. Step 4: ACT — Use coordinates from DDM to click, or navigate to URLs from
   `--at`. For SPA widgets, use `js` with `.click()` on the element.
5. Step 5: VERIFY — After `navigate` or `click`, check the `"=== Page Layout ==="`
   section in their output. After `type` or other actions, run `ddm` to verify.
6. Step 6: EXTRACT — Choose method based on page type:
   - Simple text: `ddm --text --max 5000`
   - Shadow DOM: `intel --extract` with `host_attrs`
   - SPA with data store: `intel --stores` -> `intel --find-paths` -> `js`
   - Structured data: `js` with `querySelectorAll`
   - data-testid rich pages: `intel --extract` with `data_testid`

## Agent Prompt Snippet (AGENTS.md / CLAUDE.md)

Use this subsection in your agent instruction file so tool behavior stays
consistent:

```md
### Unchained MCP Tool Use

- MCP endpoint: `https://api.unchainedsky.com/mcp`
- Always include `agent_id` in tool calls.
- On every new page, run `ddm` first for orientation.
- On unknown SPAs, run `intel_probe` before extraction.
- Use `cdp_navigate`, `cdp_click`, `cdp_type` for actions.
- After `cdp_navigate`/`cdp_click`, use returned layout first; call `ddm` again only if needed.
- Use `js_eval` for deterministic reads (title, URLs, structured DOM data).
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
AGENT = "claude-xxxxxxxx"

def post(payload, sid=None):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
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
        "arguments": {"agent_id": AGENT, "url": "https://example.com"},
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
