# Documentation

This directory holds the repository-level documentation that does not need to
live at the repo root.

## Core docs

- [architecture.md](./architecture.md): system topology, service responsibilities,
  and end-to-end request flow
- [cloud-tools-execution-map.md](./cloud-tools-execution-map.md): exact execution
  path for browser actions across `web`, `relay`, `private_core_client`, and the
  private core
- [debugging-map.md](./debugging-map.md): trace events and fast triage checklist
- [local-agent-testing.md](./local-agent-testing.md): isolated local web, chat
  client, relay, and controlled Chrome setup
- [open-core-split-plan.md](./open-core-split-plan.md): public/private boundary and
  repository split rationale
- [split-repo-setup.md](./split-repo-setup.md): CI, secrets, and private-core overlay

## Product and planning

- [roadmap.md](./roadmap.md): product and platform roadmap
- [template-extraction-notes.md](./template-extraction-notes.md): current notes on
  reducing inline template duplication in `web.py`
- [you-navigate-demo.md](./you-navigate-demo.md): local smoke test and guest-profile
  setup for the "Unchained drives. You navigate." demo

## Specialized docs

- [streaming.md](./streaming.md): live-stream subsystem
- [mcp-local-browser-guide.md](./mcp-local-browser-guide.md): production
  quickstart for controlling local Chrome through MCP
- [mcp-frontend-route-plan.md](./mcp-frontend-route-plan.md): implementation
  plan for a public `/mcp` onboarding route
- [unbrowser-mcp-route.md](./unbrowser-mcp-route.md): hosted unbrowser MCP
  route and deployment notes
- [fin-terminal-route.md](./fin-terminal-route.md): authenticated singleton
  financial terminal deployment and security boundaries
- [../unchained/benchmark/README.md](../unchained/benchmark/README.md): local
  benchmark runner and safety procedure
- [../unchained/README.md](../unchained/README.md): package-level tool and agent notes
