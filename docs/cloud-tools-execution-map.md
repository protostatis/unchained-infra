# Cloud Tools Execution Map

This document explains exactly where each `cloud_tools` call runs after the open-core split.

## Quick Model

1. **Public wrapper layer**: `unchained/cloud_tools.py`
- Runs in whichever public service imports it (`web`, `api`, `mcp_server`, agents/orchestrators).
- Contains no proprietary implementation logic.
- Forwards calls to `private_core_client`.

2. **Boundary client layer**: `unchained/private_core_client.py`
- Also runs in the caller process.
- Two modes:
  - `http` mode (when `PRIVATE_CORE_URL` is set): sends RPC-style POST to private core service.
  - `inprocess` mode (fallback/dev): imports `private_core_engine` in-process.

3. **Private execution layer**: `unchained/private_core_server.py` + `unchained/private_core_engine.py`
- In production compose, this runs in the `private-core` container/service on port `8770`.
- This is where proprietary `cdp`/`ddm`/`intel` behavior executes.

4. **Browser endpoint layer**: relay + bridge + local Chrome
- `private_core_engine` connects via relay `/cdp/<agent>/<tab>`.
- Relay forwards to `chrome_bridge.py` on user machine.
- Bridge talks to user’s local Chrome DevTools endpoint.

## Function-by-Function Map

All functions below are exported by `unchained/cloud_tools.py`.

| Function | Public wrapper runs in | Private execution runs in | Final browser interaction |
|---|---|---|---|
| `run_ddm` | caller service process | `private_core_engine.run_ddm` | CDP via relay -> bridge -> local Chrome |
| `run_intel` | caller service process | `private_core_engine.run_intel` | CDP via relay -> bridge -> local Chrome |
| `run_cdp_command` | caller service process | `private_core_engine.run_cdp_command` | CDP via relay -> bridge -> local Chrome |
| `run_js` | caller service process | `private_core_engine.run_js` | CDP via relay -> bridge -> local Chrome |
| `navigate` | caller service process | `private_core_engine.navigate` | CDP via relay -> bridge -> local Chrome |
| `click` | caller service process | `private_core_engine.click` | CDP via relay -> bridge -> local Chrome |
| `type_text` | caller service process | `private_core_engine.type_text` | CDP via relay -> bridge -> local Chrome |
| `press_enter` | caller service process | `private_core_engine.press_enter` | CDP via relay -> bridge -> local Chrome |
| `submit_form` | caller service process | `private_core_engine.submit_form` | CDP via relay -> bridge -> local Chrome |
| `screenshot` | caller service process | `private_core_engine.screenshot` | CDP via relay -> bridge -> local Chrome |
| `create_tab` | caller service process | `private_core_engine.create_tab` | relay HTTP proxy -> bridge -> local Chrome |
| `provision_launch` | caller service process | `private_core_engine.provision_launch` | relay HTTP proxy -> bridge |
| `provision_cleanup` | caller service process | `private_core_engine.provision_cleanup` | relay HTTP proxy -> bridge |
| `provision_status` | caller service process | `private_core_engine.provision_status` | relay HTTP proxy -> bridge |
| `set_file` | caller service process | `private_core_engine.set_file` | CDP via relay -> bridge -> local Chrome |
| `close_tab` | caller service process | `private_core_engine.close_tab` | CDP via relay -> bridge -> local Chrome |

## Production Defaults (Current Compose)

In `docker-compose.yml`:
- Public services (`relay`, `mcp`, `web`, `trial-agent`) set:
  - `PRIVATE_CORE_URL=http://private-core:8770`
  - `PRIVATE_CORE_TOKEN=${PRIVATE_CORE_TOKEN:-}`
- Private service:
  - `private-core` runs `python private_core_server.py --host 0.0.0.0 --port 8770`

So in production, cloud tool calls are network boundary calls into private-core.

## Local/Dev Fallback Behavior

If `PRIVATE_CORE_URL` is unset:
- `private_core_client` defaults to `inprocess` mode.
- Calls execute by importing `private_core_engine` inside the same process.

This fallback is for compatibility/dev only; the intended hardened deployment is `http` mode with a separate `private-core` service.
