# Unchained Infra

Open infrastructure and control plane for Unchained, a browser automation system
built on raw Chrome DevTools Protocol.

Most browser agents break at authentication. Unchained avoids that by driving the
user's own Chrome session, with their existing cookies, extensions, IP, and 2FA
state, instead of replaying brittle logins in a sandbox.

This repository contains the public-facing pieces of that system: relay, web UI,
agent packaging, browser bridge, deployment assets, and server orchestration.
The proprietary extraction engine lives behind a documented runtime boundary in a
separate private repository.

## What is in this repo

- `unchained/web.py`: chat UI, auth flows, scheduler UI, SSE chat transport
- `unchained/relay.py`: WebSocket relay for browser-agent tunnels and CDP clients
- `unchained/chrome_bridge.py`: local or headless bridge from Chrome CDP to relay
- `unchained/chat_agent_cli.py`: local agent lanes for Claude CLI, Codex CLI,
  OpenCode CLI, and related model backends
- `unchained/agent_package.py`: downloadable agent bundle generator
- `docker-compose.yml`: production deployment topology
- `deploy.sh` and `deploy_headless.sh`: EC2 deployment entrypoints

## What stays private

The DDM, page-intelligence, and core CDP execution logic are not stored in this
repository. Public code talks to that layer through `private_core_client.py`.

That split is intentional:

- the relay, UI, auth, packaging, and deployment code are open for inspection
- the high-leverage browser extraction heuristics stay in the private core repo
- CI enforces the boundary with import guards and artifact checks

See [docs/open-core-split-plan.md](./docs/open-core-split-plan.md) for the
current open-core model.

## Why developers can evaluate this repo

- The browser tunnel is here. You can inspect how agent auth, relay routing, and
  CDP proxying actually work.
- The deployment path is here. Docker Compose, Caddy routing, EC2 deploy scripts,
  and headless worker definitions are part of the public repo.
- The trust boundary is explicit. Public services do not directly import private
  browser-intelligence modules.
- The local dev path is real. You can run the relay and web app locally with
  `./dev.sh`.

## System shape

```text
Phone / browser
    |
    | HTTPS + SSE
    v
Caddy -> web -> private_core_client -> private core service
   |       |
   |       +-> chat agent websocket
   |
   +-> relay -> chrome_bridge -> user's Chrome DevTools endpoint
```

More detail: [docs/architecture.md](./docs/architecture.md)

## Quick start

### Local development

```bash
cd unchained-infra/unchained
uv sync
cd ..
./dev.sh
```

Then open `http://localhost:8080`.

If Google OAuth is not configured, the app falls back to dev auth:

```bash
curl -X POST http://localhost:8080/auth/dev \
  -H 'Content-Type: application/json' \
  -d '{"email":"dev@localhost"}'
```

### Production deploy

```bash
cd ~/Projects/unchainedsky_com
./unchained-infra/tools/install_private_core.sh \
  unchained-core-private/unchained \
  unchained-infra/unchained

cd unchained-infra
KEY_PATH=~/.ssh/unchained-key.pem \
EC2_HOST=<host> \
EC2_USER=ubuntu \
./deploy.sh
```

## Verification

These are the quickest checks for the public repo boundary and local stack:

```bash
cd unchained-infra/unchained
uv run python test_open_core_boundary.py

cd ..
python3 tools/oss_guard/check_private_imports.py
python3 tools/oss_guard/check_agent_artifact_leaks.py
```

## Repository layout

```text
unchained-infra/
├── docs/                    # Architecture, setup, roadmap, and design notes
├── deploy/                  # Deployment helpers
├── tools/                   # Private-core overlay + OSS boundary guards
├── unchained/               # Python application code
├── docker-compose.yml       # Production stack
├── docker-compose.headless.yml
└── deploy.sh
```

## Documentation

- [docs/README.md](./docs/README.md): docs index
- [docs/architecture.md](./docs/architecture.md): service layout and request flow
- [docs/cloud-tools-execution-map.md](./docs/cloud-tools-execution-map.md): where
  browser actions execute across the public/private boundary
- [docs/debugging-map.md](./docs/debugging-map.md): trace events and incident triage
- [docs/mcp-local-browser-guide.md](./docs/mcp-local-browser-guide.md): run
  production MCP against your local Chrome bridge
- [docs/mcp-frontend-route-plan.md](./docs/mcp-frontend-route-plan.md): plan for
  a public `/mcp` onboarding route and positioning copy
- [docs/unbrowser-mcp-route.md](./docs/unbrowser-mcp-route.md): hosted
  unbrowser MCP route and deployment notes
- [docs/you-navigate-demo.md](./docs/you-navigate-demo.md): local setup and
  reward-critic framing for the "Unchained drives. You navigate." demo
- [docs/split-repo-setup.md](./docs/split-repo-setup.md): CI and private-core overlay
- [unchained/benchmark/README.md](./unchained/benchmark/README.md): local benchmark flow
- [unchained/README.md](./unchained/README.md): package-level developer notes

## License

[MIT](./LICENSE)
