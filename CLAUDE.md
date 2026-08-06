# CLAUDE.md — Public Repo Guide

This repo contains the public infrastructure and packaging layer for
`unchainedsky.com`. It intentionally omits private-core implementation details,
internal operator playbooks, and live production access notes.

## What Belongs Here

- public web UI, relay, agent packaging, deploy scripts, and OSS-safe tests
- open-core stubs under `unchained/` that are overlaid from the private repo at
  deploy time
- public architecture and contributor documentation

## What Must Stay Out

- live production IPs, SSH commands tied to a real host, or deploy-user details
- VPN vendor/account notes, internal account names, or private provider setup
- detailed private-core algorithms, extraction heuristics, or operator playbooks

Put internal-only material in the private workspace or `unchained-core-private`.

## Build And Test

Run commands from the repo root unless noted.

```bash
uv sync --package unchained

cd unchained
uv run python test_open_core_boundary.py
uv run python test_cloud_tools_click.py

cd ..
uv run python tools/oss_guard/check_private_imports.py
uv run python tools/oss_guard/check_agent_artifact_leaks.py
uv run python tools/oss_guard/check_public_doc_leaks.py
```

## Open-Core Boundary

- `cloud_tools.py` is the public facade for private-core functionality
- public modules must not import `cdp`, `ddm`, or `intel` directly
- deploys overlay the real implementations from `unchained-core-private`
- docs in this repo should describe the public contract, not private heuristics

## Public Doc Rules

- use placeholders like `<prod-host>` and `<deploy-user>` in examples
- describe deploy steps generically, for example:

```bash
DEPLOY_REVISION="$(git rev-parse HEAD)" \
  KEY_PATH=~/.ssh/<deploy-key>.pem EC2_HOST=<prod-host> ./deploy.sh
ssh -i ~/.ssh/<deploy-key>.pem <deploy-user>@<prod-host> \
  "docker compose -f /home/<deploy-user>/unchained/docker-compose.yml ps"
```

- keep internal runbooks in the private workspace docs instead of this repo
- treat `unchained/CLAUDE.md` as a public stub only

## Agent Versioning

When changing `chat_agent_cli.py`, `agent_package.py`, or any file in
`_PACKAGE_FILES`, bump `VERSION` in `unchained/agent_package.py` so clients can
auto-update via `/web/agent/version`.
