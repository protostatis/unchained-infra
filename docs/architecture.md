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
| **unbrowser MCP** | 8767 | Hosted `unbrowser --mcp` bridged to HTTP with `mcp-proxy`; routed under `/unbrowser-mcp` on isolated networks |
| **Web** | 8080 | Chat UI, Google OAuth login, SSE bridge to agents, download endpoint for agent package |
| **Private Core** | 8770 | Proprietary execution service for CDP/DDM/intel operations (`/core/execute`) |

All services share a `relay_data` volume containing `auth.db` for auth/API-key
state and `analytics.db` for web analytics and funnel data. Treat any
`analytics_*` tables still present in `auth.db` as deprecated legacy state.

## Data Flow: Chat Message Lifecycle

```
1. User types message in chat UI (phone browser)
2. POST /web/cmd → web.py → finds agent WebSocket in _chat_agents dict
3. web.py sends {"type":"message","text":"..."} over WS to chat_agent_cli.py
4. chat_agent_cli.py pipes message to Claude CLI (claude -p --allowedTools)
5. Claude calls cdp_tool.py (e.g., ddm --llm-2pass)
6. cdp_tool.py → HTTP POST /web/cmd on server → cloud_tools.py
7. cloud_tools.py calls private_core_client → private_core_server:/core/execute
8. private_core_engine builds CDP_WS_URL → ws://relay:8765/cdp/<agent_id>/auto
9. DDM/intel tools connect to relay, which tunnels CDP to chrome_bridge.py
10. Results flow back: Chrome → bridge → relay → private_core_engine → cloud_tools → cdp_tool → Claude → chat_agent → web.py → SSE → phone
```

## Agent Authentication

- Users sign in with Google OAuth on the chat page
- Server issues JWT (HS256, 7-day expiry) stored as HTTP cookie
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
- Push to `main` triggers `.github/workflows/deploy.yml`
- SCP uploads source files to EC2
- SSH runs `docker compose up -d --build` on the instance
- Caddy auto-provisions TLS certs via Let's Encrypt
- Domain: `api.unchainedsky.com`

### Manual Deploy
```bash
./deploy.sh              # Deploy with defaults
./deploy.sh --build      # Force rebuild (no cache)
```

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
- Proprietary tools (DDM, intel, CDP engine) run server-side only; agent package gets thin HTTP client (`cdp_tool.py`)
