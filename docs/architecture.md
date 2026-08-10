# Unchained Architecture

> Browser automation framework built on raw Chrome DevTools Protocol (CDP).
> Core insight: by using the user's own browser via CDP, we solve the authentication
> problem that kills every other browser automation product.

## System Overview

```
                          ┌─────────────────────────────────────────────────────────────┐
                          │                   EC2 (Docker Compose)                      │
                          │                                                             │
 User's Phone             │   ┌───────────┐                                             │
 ┌────────────┐  HTTPS    │   │   Caddy   │  TLS termination, path-based routing        │
 │  Chat UI   │──────────►│   │  :80/:443 │                                             │
 │ (browser)  │◄──────────│   └─────┬─────┘                                             │
 └────────────┘  SSE      │         │                                                   │
                          │   ┌─────┴──────────────────────────────────────┐             │
                          │   │              Route table                   │             │
                          │   │  /tunnel, /cdp/*, /api/* → relay:8765     │             │
                          │   │  /mcp, /mcp/*            → mcp:8766      │             │
                          │   │  /unbrowser-mcp/*        → unbrowser-mcp │             │
                          │   │  /web/*, /chat, /*       → web:8080      │             │
                          │   │  /internal/*             → denied        │             │
                          │   └──┬──────────┬─────────────┬───────────────┘             │
                          │      │          │             │                              │
                          │   ┌──▼──┐   ┌───▼──┐    ┌────▼───┐                          │
                          │   │Relay│   │ MCP  │    │  Web   │                          │
                          │   │:8765│   │:8766 │    │ :8080  │                          │
                          │   └──┬──┘   └──────┘    └────┬───┘                          │
                          │      │                       │                              │
                          │      │  shared volume: relay_data                            │
                          │      │    (/data/auth.db, /data/analytics.db)              │
                          └──────┼───────────────────────┼──────────────────────────────┘
                                 │ WSS                   │ SSE/HTTP
                                 │                       │
 User's Mac                      │                       │
 ┌───────────────────────────────┼───────────────────────┼──────────────────┐
 │                               │                       │                  │
 │   ┌───────────────────┐   ┌──▼──────────────┐   ┌────▼──────────────┐   │
 │   │   Chrome (:9222)  │◄─►│ chrome_bridge.py│   │ chat_agent_cli.py │   │
 │   │  User's session,  │   │ Tunnels CDP to  │   │ Receives messages │   │
 │   │  cookies, 2FA     │   │ relay over WSS  │   │ from chat, runs   │   │
 │   └───────────────────┘   └─────────────────┘   │ Claude CLI        │   │
 │                                                  └───────┬───────────┘   │
 │                                                          │               │
 │                                                  ┌───────▼───────────┐   │
 │                                                  │    cdp_tool.py    │   │
 │                                                  │ HTTP calls to     │   │
 │                                                  │ server /web/cmd   │   │
 │                                                  └───────────────────┘   │
 └──────────────────────────────────────────────────────────────────────────┘
```

## Services (Docker Compose)

| Service | Port | Role |
|---------|------|------|
| **Caddy** | 80, 443 | TLS termination (Let's Encrypt auto-cert), reverse proxy, path routing |
| **Relay** | 8765 | WebSocket relay: agent tunnel (`/tunnel`), CDP proxy (`/cdp/*`), REST API (`/api/*`), health check |
| **MCP** | 8766 | FastMCP server exposing DDM/intel tools for MCP-compatible clients |
| **unbrowser MCP** | 8767 | Session-isolating HTTP broker: one loopback `mcp-proxy` → `unbrowser --mcp` worker per MCP session, routed under `/unbrowser-mcp` on isolated networks |
| **Financial terminal** | 8787 | Authenticated singleton market-research UI; uses OpenRouter and the internal unbrowser MCP service |
| **Web** | 8080 | Chat UI, OAuth, SSE bridge, hosted-credit authority, and agent-package downloads |
| **Trial agent** | internal | Hosted OpenRouter tool-use worker; inference is server-side while browser actions route through the selected bridge |
| **Private Core** | 8770 | Proprietary execution service for CDP/DDM/intel operations (`/core/execute`) |

Stateful services share a `relay_data` volume containing `auth.db` for auth,
API-key, hosted-credit, and scheduler state, plus `analytics.db` for web
analytics and funnel data. Treat any
`analytics_*` tables still present in `auth.db` as deprecated legacy state.

## Data Flow: Chat Message Lifecycle

```
1. User types message in chat UI (phone browser)
2. POST /web/cmd → web.py → finds agent WebSocket in _chat_agents dict
3. web.py sends {"type":"message","text":"..."} over WS to chat_agent_cli.py
4. chat_agent_cli.py pipes message to the selected local CLI (Claude, Codex, or OpenCode)
5. The local CLI calls cdp_tool.py (e.g., ddm --llm-2pass)
6. cdp_tool.py → HTTP POST /web/cmd on server → cloud_tools.py
7. cloud_tools.py calls private_core_client → private_core_server:/core/execute
8. private_core_engine builds CDP_WS_URL → ws://relay:8765/cdp/<agent_id>/auto
9. DDM/intel tools connect to relay, which tunnels CDP to chrome_bridge.py
10. Results flow back: Chrome → bridge → relay → private_core_engine → cloud_tools → cdp_tool → local CLI → chat_agent → web.py → SSE → phone
```

### Hybrid hosted trial lane

The `/trial` lane moves model inference to the server without moving the user's
browser session:

```text
Browser UI -> web:8080 -> trial-agent -> OpenRouter
                    |          |
                    |          +-> cloud_tools -> relay -> user's chrome_bridge
                    +-> auth.db credit ledger
```

For each authenticated chat turn, `web` creates a server-owned inference run.
Before every OpenRouter attempt, the worker obtains an atomic credit hold, then
persists a `submitted` boundary before network I/O. Successful attempts settle
provider-reported usage. A pre-submit failure releases its hold; a submitted
request whose callback is lost, cancelled, timed out, or swept as stale is
captured conservatively. Provider retries and model changes require a new hold.
Amounts are stored as integer micro-USD.

Credit is grant-based in this beta: there is no payment processor or checkout
flow. Admins allocate credit from the admin UI. Existing trial budget state is
migrated once into an opening grant. Anonymous First Look requests are forced to
the configured free-model lane and use a shared system accounting principal.

Hosted turns also pass race-safe global and per-account admission limits and an
absolute deadline. The hosted `/schedule` tools use a short-lived grant bound to
the authenticated user and chat session; the worker never receives the user's
API key.

The worker's reserve/submitted/settle/release callbacks live under
`/internal/credit/*`. They require `HOSTED_AGENT_SERVICE_TOKEN`, are called over
the Docker `app` network, and are explicitly rejected by Caddy at the public
edge. Control-plane run creation, run completion, user balances, and admin
grants are not exposed through the worker callback interface.

## Agent Authentication

- Users sign in with Google OAuth on the chat page
- Server issues an HTTP-only session JWT (HS256) with a 30-day lifetime that
  successful external SSO can refresh, capped at 90 days from the original login
- Server creates API key (`uc_live_` + 24 hex chars) stored in SQLite
- Agent ID derived from API key hash: `a-{sha256(key)[:8]}`
- Agent package (ZIP) ships with pre-filled `.env` containing the API key
- Chrome bridge and chat agent authenticate to relay using this key

## Key Modules

### Core Tools (run server-side through relay tunnel)
- **cdp.py** (1341 LOC) — CDP WebSocket client, Chrome lifecycle, tab management, anti-bot mouse/keyboard simulation
- **ddm.py** (1794 LOC) — DOM Density Map: structural page understanding in ~500 tokens. JS DOM walker, grid renderer, interactive element extraction
- **intel.py** (1264 LOC) — Bayesian extraction strategy selector: 8 strategies, DOM fingerprinting, JS data store discovery

### Tunnel System
- **chrome_bridge.py** (609 LOC) — Runs on user's machine. Connects to relay, multiplexes CDP WebSocket channels + HTTP requests over single tunnel. Handles provisioned Chrome lifecycle (launch with user profile, tab discovery, cleanup)
- **relay.py** (339 LOC) — Runs on server. Routes messages between agents (on `/tunnel`) and clients (on `/cdp/<agent_id>/<tab_id>`). Proxies provision HTTP requests to bridge

### Web & API
- **web.py** (1466 LOC) — Chat UI (HTML/JS), Google OAuth, SSE bridge, `/web/cmd` API dispatching to cloud_tools
- **api.py** (351 LOC) — REST API for programmatic access (agent/tab listing, CDP commands, orchestrator)
- **auth.py** (222 LOC) — SQLite-backed API key CRUD with `uc_live_` prefix format

### Agent (runs on user's machine)
- **chat_agent_cli.py** (332 LOC) — Connects to server via WebSocket, receives chat messages, pipes to Claude CLI, streams responses back
- **chat_agent_sdk.py** (310 LOC) — Alternative agent using Anthropic SDK directly (no Claude CLI dependency)
- **agent_package.py** (352 LOC) — Builds downloadable ZIP with start.sh, pre-configured .env, patched agent modules

### Orchestration
- **orchestrator.py** (424 LOC) — Claude API tool-use loop with DDM/intel/CDP tools. Used by API endpoint for programmatic automation
- **cloud_tools.py** — Public boundary wrapper used by API/Web/agents
- **private_core_client.py** — Contract client for the private core service
- **private_core_server.py** — Private service endpoint (`/core/execute`)
- **private_core_engine.py** — Proprietary cdp/ddm/intel implementation
- **mcp_server.py** — FastMCP server exposing DDM/intel/CDP/provisioning tools for MCP-compatible AI clients
- **Dockerfile.unbrowser-mcp** — Hosted unbrowser MCP bridge using `pyunbrowser` and `mcp-proxy`
- **unbrowser_ssrf_proxy.py** — Egress guard for the public unbrowser MCP route

## Deployment

### Production (GitHub Actions)
- Pull requests run public-safe CI without private-core credentials.
- A protected `main` push runs public and private-integrated checks. A passing
  revision then waits for the GitHub `production` Environment approval before
  deployment.
- The CI workflow invokes `./deploy.sh` as the sole deployment implementation.
  It serializes remote deploys, snapshots the prior source release, validates
  service and public HTTP health, and restores the snapshot if a deployment
  fails before its health gate.
- Deployment SSH uses a preconfigured known-host entry; CI does not trust a
  host key discovered at deploy time.
- Caddy auto-provisions TLS certs via Let's Encrypt
- Domain: `api.unchainedsky.com`

### Manual Deploy
```bash
git switch main
git pull --ff-only origin main
DEPLOY_REVISION="$(git rev-parse HEAD)" EC2_HOST=<prod-host> ./deploy.sh
DEPLOY_REVISION="$(git rev-parse HEAD)" EC2_HOST=<prod-host> ./deploy.sh --build
```

Manual deploys use the same remote lock, health gate, and rollback behavior as
CI deploys. They require a clean worktree and an explicit revision matching the
current `origin/main`; the private-core overlay is applied only after that guard
succeeds. The source check requires network access to `origin` and fails closed
when current `main` cannot be queried. Provide
`DEPLOY_SSH_KNOWN_HOSTS_FILE` to require a pinned SSH host key for a manual
deployment.

### Dedicated Headless Trial Worker (separate EC2)

Use this when you want OpenRouter trial traffic to run on an isolated EC2 host with
headless Chromium (instead of colocating `trial-agent` with the main API stack).

```bash
cp .env.headless.example .env.headless
# edit .env.headless with real keys/hosts
EC2_HOST=<headless-ec2-ip> ./deploy_headless.sh
```

This deploys `docker-compose.headless.yml` (`headless-bridge` + `headless-agent`)
to a separate remote directory (`/home/ec2-user/unchained-headless` by default).

## Environment Variables

### Server-side (docker-compose.yml)
| Variable | Service | Purpose |
|----------|---------|---------|
| `GOOGLE_CLIENT_ID` | web | Google OAuth client ID |
| `JWT_SECRET` | web | HMAC key for JWT signing |
| `ALLOWED_EMAILS` | web | Comma-separated whitelist |
| `UNCHAINED_DB_PATH` | all | Path to auth.db (default: `/data/auth.db`) |
| `UNCHAINED_ANALYTICS_DB_PATH` | web | Path to dedicated analytics.db (default: sibling of `UNCHAINED_DB_PATH`, usually `/data/analytics.db`) |
| `RELAY_INTERNAL_URL` | mcp, web | Internal WebSocket URL to relay |
| `PRIVATE_CORE_URL` | relay, mcp, web, trial-agent | URL for private-core service |
| `PRIVATE_CORE_TOKEN` | relay, mcp, web, trial-agent, private-core | Bearer token for public->private service auth |
| `TRIAL_AGENT_KEY` | web, trial-agent | WebSocket identity for the hosted worker |
| `HOSTED_AGENT_SERVICE_TOKEN` | web, trial-agent | Required dedicated bearer token for internal credit callbacks and scoped scheduler calls; generate independently from every other key |
| `OPENROUTER_API_KEY` | trial-agent, fin-terminal | Shared provider credential for hosted inference and private terminal research |
| `FIN_TERMINAL_PROXY_TOKEN` | caddy, fin-terminal | Required independent token authenticating edge-to-persistent-terminal requests |
| `FIN_TERMINAL_ALLOWED_EMAILS` | web | Optional approved operator emails added to the admin allowlist |
| `HOSTED_MAX_ACTIVE_TURNS` | web | Optional global hosted-turn limit (default: `16`) |
| `HOSTED_MAX_ACTIVE_TURNS_PER_USER` | web | Optional per-account hosted-turn limit (default: `3`) |
| `HOSTED_TURN_DEADLINE_SECONDS` | web | Optional absolute hosted-turn deadline (default: `600`); `/schedule` grants remain valid for the deadline plus a one-minute setup margin |
| `HOSTED_MAX_USER_PROMPT_CHARS` | web | Optional inbound hosted-user prompt cap (default: `20000`) |
| `HOSTED_MAX_INTERNAL_CONTEXT_CHARS` | trial-agent | Optional per-attempt serialized internal agent-context budget (default: `400000`); startup fails closed above the reviewed catalog-credit boundary |
| `HOSTED_MAX_INPUT_CHARS` | trial-agent | Deprecated fallback for `HOSTED_MAX_INTERNAL_CONTEXT_CHARS`; used only when the new setting is unset |
| `CREDIT_STALE_RUN_TTL_SECONDS` | web | Optional crash-recovery sweep age (default: `7200`) |
| `CREDIT_ADMIN_ALLOWLIST` | web | Optional comma-separated additional hosted model IDs; unknown models use the conservative default hold |
| `CREDIT_DEFAULT_RESERVATION_MICRO_USD` | web | Optional per-attempt hold for explicitly allowlisted models not in the built-in catalog (default: `1000000`, or $1) |
| `UNCHAINED_SESSIONS_DIR` | web, trial-agent | Shared active hosted-conversation directory (default: `/data/sessions`) |

> **Migration:** A trial-agent deployment with neither context variable set now
> uses the `400000` internal-context default. Set
> `HOSTED_MAX_INTERNAL_CONTEXT_CHARS` explicitly before rollout; the legacy
> `HOSTED_MAX_INPUT_CHARS` remains a fallback only. Production deployment
> rejects a missing, duplicate, or out-of-range canonical value.

### Client-side (agent .env)
| Variable | Purpose |
|----------|---------|
| `UNCHAINED_API_KEY` | Agent authentication key |
| `UNCHAINED_SERVER` | Server URL (default: `wss://api.unchainedsky.com`) |
| `CDP_PORT` | Chrome debug port (default: 9222) |

## Security Model

- Each user gets isolated routing — messages go only to their agent
- Agent ID is deterministic from API key (hash-based), so one key = one agent
- Chrome bridge inherits user's browser session (cookies, IP, 2FA) — no credentials stored server-side
- All external traffic encrypted via Caddy TLS
- Hosted provider spend fails closed: paid attempts require an active grant,
  an atomic hold, and a persisted submission transition
- Hosted-worker callbacks are internal-network-only and use a dedicated token;
  `TRIAL_AGENT_KEY` cannot authorize credit or scheduler callbacks
- The financial terminal requires both web-session authorization at Caddy and a
  deployment-only proxy token; client-provided identity headers are discarded
- Proprietary tools (DDM, intel, CDP engine) run server-side only; agent package gets thin HTTP client (`cdp_tool.py`)
