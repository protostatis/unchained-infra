# Frontend Route Plan: MCP Onboarding Page

This plan defines a new public frontend route that explains how to use the
production MCP server and why this is the preferred path over setting up a
Playwright skill for core user workflows.

## Proposed Route

- Path: `/mcp`
- Method: `GET`
- Handler: `web_app.handlers.pages:handle_mcp_page`
- Audience:
  - users who already have a local browser and want agent control quickly
  - users comparing integration options (MCP vs custom skill wrappers)

## Page Goals

- Lead with the "zero config if agent already installed" path
- Explain the architecture in plain language: local Chrome + bridge + MCP server
- Give copy-paste setup steps that work end-to-end
- Frame the decision clearly: when MCP is better, when Playwright still fits
- Reduce support load from repeated setup questions

## Content Outline

1. Hero + value proposition
- "Use your own browser through MCP in minutes"
- CTA buttons:
  - `Open Chat` (`/local`) for login/API key retrieval
  - `Docs` (deep link to `docs/mcp-local-browser-guide.md` or public mirror)

2. Two-path quickstart
- Path A (recommended): "Already installed agent" -> point MCP client at
  `https://api.unchainedsky.com/mcp` and use current connected `agent_id`
- Path B (manual): start `chrome_bridge.py` to
  `wss://api.unchainedsky.com/tunnel`

3. Copy blocks
- bridge launch command (`--no-headless`)
- MCP server URL
- example tool calls (`cdp_navigate`, `ddm`, `js_eval`)

4. MCP vs Playwright skill comparison (single table)
- rows:
  - auth/session fidelity
  - setup and maintenance burden
  - cross-client portability
  - CI determinism
  - best-fit use cases

5. Troubleshooting
- `/mcp` routing check
- `Agent not connected`
- missing/invalid API key

## Implementation Plan

1. Route wiring
- Add `("GET", "/mcp", "web_app.handlers.pages:handle_mcp_page")` in
  `unchained/web_app/routes.py`.

2. Handler
- Add `handle_mcp_page()` in `unchained/web_app/handlers/pages.py`.
- Keep it public (`no auth`) but include clear note that tool execution requires
  an active bridge + API key.

3. Template
- Add `MCP_PAGE_HTML` in `unchained/web_app/templates.py` (preferred), or inline
  in handler if we want to ship faster first.
- Keep page mobile-friendly (same constraints as `/local` and `/demo`).

4. Analytics events
- Track page view (`mcp_page_view`)
- Track command copy clicks (`mcp_copy_bridge_cmd`, `mcp_copy_mcp_url`)
- Track outbound CTA clicks

5. Tests
- Add route existence contract in `test_web_contracts.py`
- Add handler smoke test (200 + expected key text markers)

## Acceptance Criteria

- `GET /mcp` returns 200 in local dev and production
- existing installed-agent users can complete setup without source checkout
- user can copy commands directly from page without external docs
- comparison section gives explicit "MCP default / Playwright exception" guidance
- route does not require login to read, but clearly states execution prerequisites

## Non-Goals (This PR)

- No change to MCP auth enforcement behavior
- No new backend APIs
- No Playwright integration changes

## Follow-Up (Recommended)

- Harden MCP auth enforcement in `mcp_server.py` so tool calls require validated
  API key ownership checks before launch traffic increases from this route.
