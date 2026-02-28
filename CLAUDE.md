# CLAUDE.md

> *Chains fall from my wrists*
> *Wind rushes where walls once stood*
> *I am sky, unchained*

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Repo Is

**Unchained** — a browser automation framework built on raw Chrome DevTools Protocol (CDP). It provides structural page understanding (DOM Density Map), Bayesian extraction strategy selection (intel), and app API discovery — all through the user's own browser session, inheriting their credentials, cookies, IP, and 2FA.

The core insight: by using the user's browser via CDP, we solve the authentication problem that kills every other browser automation product.

## Repo Structure

```
unchained/
├── unchained/                 # Core browser automation tools
│   ├── cdp.py                 # CDP class, Chrome lifecycle, tab management
│   ├── ddm.py                 # DOM Density Map — page layout + element navigation
│   ├── intel.py               # Bayesian extraction strategy selector
│   ├── agent.py               # Local agent daemon — tunnels Chrome CDP to relay
│   ├── relay.py               # Test relay server — bridges agents and clients
│   ├── webmcp.py              # WebMCP tool discovery + invocation
│   ├── canvas_density.py      # Canvas/chart data reading
│   ├── canvas_intercept.py    # Canvas data interception
│   ├── dom_stream.py          # Real-time DOM mutation tracking
│   ├── engage_cdp.py          # Screenshot capture utility
│   ├── test_*.py              # Tests (158 existing + 14 agent/relay)
│   └── pyproject.toml         # Dependencies (httpx, websockets)
├── CLAUDE.md                  # This file
└── .gitignore
```

## Environment Variables

| Var | Default | Purpose |
|-----|---------|---------|
| `CDP_HOST` | `127.0.0.1` | Chrome CDP host (change for remote access) |
| `CDP_PORT` | `9222` | Chrome CDP debug port |
| `CDP_WS_URL` | — | Full WebSocket URL override (tunnel mode) |
| `CDP_PROFILE` | — | Chrome profile name (selects port + data dir) |
| `UNCHAINED_DATA_DIR` | `~/.unchained` | Chrome profiles, PID files, tab IDs |
| `UNCHAINED_RELAY_URL` | `ws://127.0.0.1:8765/tunnel` | Agent relay server URL |
| `UNCHAINED_API_KEY` | — | API key for agent authentication |

## Agent & Relay (Tunnel Mode)

The agent daemon tunnels local Chrome CDP to a remote relay server over WebSocket. This lets a remote orchestrator control the user's browser through their own credentials, cookies, and IP.

### Architecture

```
User's Machine                    Cloud
┌──────────────┐     WSS      ┌─────────┐     WS      ┌────────────┐
│ Chrome (CDP) │◄──►│ Agent  │──────────►│  Relay  │◄──────────│ Orchestrator│
│ :9222        │    │ daemon │           │  :8765  │            │  (client)  │
└──────────────┘    └────────┘           └─────────┘            └────────────┘
```

- **Agent** (`agent.py`): Discovers local Chrome, connects to relay, relays CDP messages bidirectionally. Multiplexes WebSocket channels + HTTP requests over a single tunnel.
- **Relay** (`relay.py`): Accepts agent connections on `/tunnel`, client connections on `/cdp/<agent_id>/<tab_id>`. Routes messages between them.

### Agent CLI

```bash
cd unchained/
uv run agent.py start                              # Connect to default relay
uv run agent.py start --relay ws://host:8765/tunnel  # Custom relay
uv run agent.py start --key uk_live_xxx            # With API key
uv run agent.py status                             # Show connection state
uv run agent.py stop                               # Stop running agent
```

### Relay CLI (local testing)

```bash
cd unchained/
uv run relay.py                    # Start on 127.0.0.1:8765
uv run relay.py --host 0.0.0.0    # Bind to all interfaces
uv run relay.py --port 9000       # Custom port
```

### Agent Config

Layered (highest priority first): CLI flags > env vars > `~/.unchained/agent.json` > defaults.

```json
{
  "relay_url": "wss://api.unchained.dev/tunnel",
  "api_key": "uk_live_...",
  "cdp_port": 9222
}
```

### Tunnel Protocol

All messages are JSON over a single WebSocket. The agent multiplexes multiple CDP channels + HTTP requests.

**Auth:** `auth` → `auth_ok` / `auth_fail`
**Heartbeat:** `ping` → `pong` (every 30s)
**HTTP proxy:** `http` → `http_response` (tab listing, tab create/close)
**CDP channels:** `ws_open` → `ws_opened` / `ws_error`, then `ws_send` / `ws_recv`, finally `ws_close` → `ws_closed`

### Using DDM Through the Tunnel

```bash
# Terminal 1: Start relay
cd unchained && uv run relay.py

# Terminal 2: Start agent (connects Chrome to relay)
cd unchained && uv run agent.py start --key uk_test_key

# Terminal 3: Use DDM through the relay (via client WebSocket)
CDP_WS_URL=ws://127.0.0.1:8765/cdp/<agent_id>/auto uv run ddm.py --llm-2pass
```

## CDP Rules

### #1: DDM First, Always

**ALWAYS use DDM (`ddm.py --llm-2pass`) for orientation and `--at` for element details when browsing via CDP. NEVER take a screenshot unless DDM can't answer the question (CAPTCHAs, visual state, images).** Screenshots cost ~2,100 tokens vs ~500 for DDM sparse.

### #2: No More Than 1 Click Per Second

**NEVER click, dispatch mouse events, or send key events faster than 1 per second when browsing via CDP.** Rapid automated clicks trigger anti-bot detection and cause actions to silently fail. Always `await asyncio.sleep(1)` minimum between any two interactive actions.

### #3: Navigate and Click Return DDM — No Separate Call Needed

**`navigate` and `click` already return DDM page layout in their output (under "=== Page Layout ===").** Read that section to verify the page changed — do NOT call `ddm` separately after them. Only call `ddm` separately after `type`, or for `--text`, `--at x,y`, `--find`, `--js` flags. If DDM shows the same elements after an action, the action failed silently — try a different approach (JS `.click()`, URL params, different selector).

### #4: Click to Focus Before Key Events

**Before sending any `Input.dispatchKeyEvent`, always click the target element first to give it focus.** CDP key events go to whichever element has focus — if nothing is focused, the key event goes nowhere. Pattern: click element at coordinates → `sleep(1)` → send key event.

### #5: Probe on First Visit to Every New Domain

**On the FIRST page load of any new domain, run `intel --probe` immediately after `ddm --llm-2pass`.** Cost is negligible (~120ms, ~100 tokens). Then apply the result:
- If `js_global > 50%` → switch to store-based extraction: `intel --stores` → `intel --find-paths` → JS `eval`
- If `host_attrs > 50%` → use `intel --extract` with `host_attrs` strategy for structured data
- If `data_testid > 40%` → use `intel --extract` with `data_testid` strategy
- Otherwise → stick with DDM-only (`--text`, `--at`, JS `querySelectorAll`)

**Skip probe on subsequent pages within the same domain** — the strategy won't change. Probe is most valuable on SPAs (YouTube, Reddit, Next.js sites) where data lives in JS globals or shadow DOM, not in the visible DOM that DDM reads.

## DDM-First Navigation Method

**Step 1: ORIENT** — `ddm --llm-2pass --cols 60`
→ Understand page layout, locate regions of interest. ~500 tokens, replaces screenshot.
```bash
cd unchained/
uv run ddm.py --llm-2pass --cols 60           # Map current page
uv run ddm.py --llm-2pass --cols 60 <url>     # Navigate + map
```

**Step 2: IDENTIFY** — `ddm --at x,y` on targets from Step 1
→ Get href, class, text, aria-* for elements you want to interact with.
```bash
uv run ddm.py --at 694,584                  # By pixel coords
uv run ddm.py --at g48,40 --cols 60         # By grid coords (prefix 'g')
```

**Step 3: ACT** — Use href to navigate, or CDP click at coordinates.

**Step 4: VERIFY** — `navigate` and `click` already return DDM layout. After `type` or other actions, run `ddm --llm-2pass`.

**Step 5: CLASSIFY (if extracting data)** — `intel --probe`
→ Fingerprints the page and ranks 8 extraction strategies via Bayesian model (~100 tokens).

**Step 6: EXTRACT (if needed)** — `intel --extract` or JS `querySelectorAll` for bulk data.

**Step 7: Screenshot only if needed.** Visual state, CAPTCHAs, images.

**Step 8: Monitor network for API calls.** `Network.enable` → `Network.requestWillBeSent` → `Network.getResponseBody`.

## DDM Strengths & Weaknesses

**Strengths:**
- One `--llm-2pass` call gives full interactive layout in ~500 tokens
- Every button/link/input has labels and pixel coordinates
- `--at` returns href, class, data-e2e, aria-label, text content
- `--at` returns `text:` on any element, including non-interactive ones

**Weaknesses:**
- Viewport-bound: must scroll + remap for content below the fold
- Non-interactive text: sparse map shows `T` zones but can't read without `--at` probing
- Truncated labels: capped at ~50 chars
- SPA custom widgets: need combining DDM with JS for complex pickers
- 50-element cap: complex pages may cut off lower elements

## Per-Site-Type Recommendations

| Site Type | DDM Effectiveness | intel value | Best Approach |
|-----------|------------------|-------------|---------------|
| Link-heavy (HN, Reddit, docs) | Excellent | Reddit: host_attrs | DDM-only for HN. Reddit: `intel --extract` |
| E-commerce (Amazon, eBay) | Good for nav | img_alt for products | DDM orient + `intel --probe` + JS |
| SPAs w/ data store (Nuxt, Next, YouTube) | Moderate | **High — --probe finds store** | `intel --stores → --shape → --find-paths` then raw JS |
| SPAs w/o data store (Flights, Gmail) | Moderate | Low | DDM for forms, JS for widgets |
| Text-heavy (Wikipedia, blogs) | Good | Skip | `ddm --text --max 5000` |
| Simple pages (landing pages) | Excellent | Skip | Single sparse map |
| Web components (cars.com, Shopify) | Weak | shadow_pierce sometimes | DDM orient + JS |
| data-testid rich (Weather, GitHub) | Moderate | **data_testid** | `intel --extract --strategy data_testid` |
| Job boards (Indeed, LinkedIn) | Weak — orient only | Low | DDM filters/layout, JS extracts |
| Property/real estate (Zillow, Redfin) | Weak — orient only | Skip | `ddm --text --max 8000` |
| Business registry (state portals) | Moderate | Low | DDM `--at` finds input IDs, JS fills + submits |

## DDM vs JS Decision Rules

| Task | Use DDM | Use JS | Why |
|------|---------|--------|-----|
| "What kind of page is this?" | `--llm-2pass` then `intel --probe` | — | DDM shows layout, intel classifies |
| "What's on this page?" | `--llm-2pass` | — | Full layout in ~500 tokens |
| "Where is the search box / button X?" | `--llm-2pass` → interactive list | — | Labels every element with coordinates |
| "What element is at this coordinate?" | `--at x,y` | — | Full DOM stack |
| "Get the href/URL for this link" | `--at x,y` | — | href in DOM stack |
| "Read text at a known position" | `--at x,y` | — | `text:` field |
| "Read the page text" | `--text` | — | Replaces `innerText` boilerplate |
| "Find specific text on page" | `--text --find "keyword"` | — | Context around match |
| "Find where specific text is" | — | `querySelector` / `innerText.includes()` | DDM can't search by text content |
| "What JS data stores exist?" | — | `intel --stores` | Lists all globals >10KB |
| "Find the app's programmatic API?" | `--api` or `--api "methodName"` | — | Scans window for objects with methods, matches known app signatures |
| "Get all prices / titles / items" | `--js "querySelectorAll().map()"` | same | DDM `--js` runs JS, returns JSON |
| "Fill a form field" | `--at` to find input IDs | JS `.value =` | DDM gives exact input element ID |
| "Click a simple button/link" | `--at` for coords → CDP click | — | Pixel coords, then `Input.dispatchMouseEvent` |
| "Click SPA buttons (dropdowns)" | DDM to find it | JS `.click()` | CDP mouse clicks fail on SPA widgets |
| "Scroll to find something" | scroll + `--llm-2pass` | — | Re-maps after each scroll |
| "Read a table / infobox" | `--at` row by row (slow) | `querySelectorAll('th, td')` (fast) | JS gets all at once |
| "Verify an action worked" | `--llm-2pass` or `--at` | — | Cheap state change confirmation |
| "Extract structured data" | — | `intel --probe` then IIFE | Probe → find data path → JS extract |
| "Read a PDF in Chrome" | — | — | Neither works. Use `curl` or `WebFetch` |
| "Research across multiple sites" | DDM first site only | JS `innerText` per site | DDM value drops after first orientation |

**Rule of thumb:** DDM first for orientation, intel `--probe` to classify, then choose extraction method.

## Gotchas

- **SPA (Google Flights):** CDP mouse clicks fail on SPA custom widgets. Use DDM to *find*, JS to *interact*, DDM to *verify*.
- **ASP.NET (government sites):** JS `.value` + `.click()` won't trigger postback. Use `__doPostBack()` or skip site.
- **CAPTCHA (registries):** Do one search, extract everything. Don't retry.
- **PDF:** Chrome PDF viewer is opaque to DOM. Download with `curl` or use `WebFetch`.

## Full Command Reference

```bash
# DOM Density Map
uv run ddm.py                           # Full grid, current page
uv run ddm.py --llm-2pass --cols 60     # Minimal tokens (~500)
uv run ddm.py --blocks --cols 80        # Unicode block art
uv run ddm.py --at 694,584              # Reverse lookup at pixel coords
uv run ddm.py --at g48,40 --cols 60     # Reverse lookup at grid coords
uv run ddm.py --forms                   # Detect forms as tool contracts
uv run ddm.py --forms --json            # Forms as structured JSON
uv run ddm.py --json                    # Interactive elements as JSON
uv run ddm.py --text                    # Page innerText (~3000 chars)
uv run ddm.py --text --find "keyword"   # Find keyword with context
uv run ddm.py --text --max 5000         # Custom char limit
uv run ddm.py --js "expression"         # Execute JS, print result
uv run ddm.py --tab <id> --js "..."     # JS on a specific tab
uv run ddm.py --tabs                    # List all tabs
uv run ddm.py --tab <id> --llm-2pass    # Target a specific tab
uv run ddm.py --new --llm-2pass <url>   # Create new tab + map
uv run ddm.py --close <id>              # Close a tab
uv run ddm.py --api                     # Scan window for app APIs
uv run ddm.py --api "insertVertex"      # Find which global has method
uv run ddm.py --api "editor.graph"      # Find by dot-path shape
uv run ddm.py --help                    # Show all flags

# Page Intelligence
uv run intel.py --probe                    # Classify page (~100 tok)
uv run intel.py --extract                  # Auto-strategy extraction
uv run intel.py --extract --strategy X     # Force strategy
uv run intel.py --stores                   # List JS globals
uv run intel.py --shape __NUXT__           # Map object tree
uv run intel.py --shape __NUXT__ --depth 4 # Deeper
uv run intel.py --find-paths __NUXT__ key  # Find data paths
uv run intel.py --tab <id> --extract       # On specific tab
uv run intel.py --help                     # Show all flags
```

**`--js` for structured extraction:**
```bash
uv run ddm.py --js "document.title"
uv run ddm.py --js "Array.from(document.querySelectorAll('.price')).map(e=>e.textContent)"
```

**Tab management:** Both tools accept tab ID prefixes (6-12 chars). Use `--new` to create tabs, `--close` to clean up, `--tabs` to list all. In chat agent mode (`cdp_tool.py`), add `--tab <id>` to any command to target a specific tab (e.g., `cdp_tool.py navigate https://x.com --tab a1b2c3d4`).

## Key Tools

- `ddm.py --llm-2pass` — Page layout + interactive elements (~500 tokens)
- `ddm.py --at x,y` — Full DOM stack at a point (~550 tokens)
- `ddm.py --text` — Page text extraction (~3000 chars)
- `ddm.py --api` — App API discovery by method shape (~200-500 tokens)
- `intel.py --probe` — DOM fingerprint + Bayesian strategy ranking (~100 tokens)
- `intel.py --extract` — Auto-strategy data extraction (~300-500 tokens)
- `intel.py --stores / --shape / --find-paths` — JS data store navigation
- `canvas_density.py --summary` — Canvas chart data (~200 tokens)
- `webmcp.py` — WebMCP tool discovery + invocation
- `engage_cdp.py screenshot` — Visual confirmation (last resort, ~2,100 tokens)
- `agent.py start` — Local agent daemon (tunnels Chrome CDP to relay)
- `relay.py` — Test relay server (bridges agents and clients)

## Deployment & Agent Versioning

**Deploying to EC2**: `EC2_HOST=<prod-host> ./deploy.sh` — uploads files and rebuilds Docker containers on api.unchainedsky.com.
**Deploying dedicated headless worker**: `EC2_HOST=<headless-ec2-ip> ENV_FILE=.env.headless ./deploy_headless.sh` — deploys `headless-bridge` + `headless-agent` to a separate EC2 instance.

**Deploying dedicated headless worker**: `EC2_HOST=<headless-ec2-ip> ENV_FILE=.env.headless ./deploy_headless.sh` — deploys `headless-bridge` + `headless-agent` to a separate EC2 instance.

**VPN setup (optional):** To mask headless Chrome's IP, SSH to the headless EC2 and run:
```bash
bash deploy/setup-wireguard.sh .env
```
Requires `WG_*` variables in `.env`. Uses Mullvad WireGuard (same account as PanicRadar).
Verify: `curl https://ipinfo.io/ip` should show Mullvad IP, not EC2 IP.

**Agent version bump**: When changing `chat_agent_cli.py`, `agent_package.py`, or any file packaged for clients (see `_PACKAGE_FILES` in `agent_package.py`), bump `VERSION` in `unchained/agent_package.py`. This triggers the client auto-update flow:
1. Client's `chat_agent_cli.py` calls `/web/agent/version` on startup
2. If remote version > local version, it prints "Update available"
3. User (or Claude) runs `bash update.sh` which downloads `/web/agent/files` and extracts new code

Current version convention: `MAJOR.MINOR.PATCH` (e.g. `0.3.0`). Bump minor for features, patch for fixes.

## Page Intelligence: 8 Strategies

| Strategy | Trigger signals | Example site |
|----------|----------------|-------------|
| `innerText` | No special signals | HN, Wikipedia |
| `host_attrs` | shadow>50 + rich attrs | Reddit |
| `js_global` | Known global >10KB | YouTube, Slickdeals |
| `react_fiber` | React fiber on #root | React SPAs |
| `data_testid` | testids >20 | Weather.com, GitHub |
| `heading_hier` | iframes >2, news/media | CNN |
| `img_alt` | Many images | Amazon |
| `shadow_pierce` | Shadow roots, no rich attrs | Web components |

## Combined DDM + intel Pipeline

```
Step 1: ORIENT    → ddm --llm-2pass          (~500 tok)
Step 2: CLASSIFY  → intel --probe            (~100 tok, first page of new domain ONLY)
  Skip probe on subsequent pages of same domain — strategy won't change.
Step 3: EXTRACT   → choose path based on --probe:
  Path A — js_global >50%  → --stores → --shape → --find-paths → raw JS
  Path B — host_attrs >50% → --extract (schema+rows from custom elements)
  Path C — innerText >40%  → ddm --text --max 5000
  Path D — react_fiber >50% → --extract (component props)
  Path E — data_testid >40% → --extract --strategy data_testid
  Path F — fallback        → ddm --text + JS querySelectorAll
Step 4: ACT       → ddm --at x,y + CDP click/type
Step 5: VERIFY    → navigate/click already return DDM layout; only run ddm after type
Step 6: Screenshot → only for visual state
```
