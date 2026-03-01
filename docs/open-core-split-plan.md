# Open-Core Split Plan (Infra Public, Core Private)

## Goal

Open-source the infrastructure/control plane to maximize adoption, while keeping the `cdp`/`ddm`/`probe_intel` stack private under a commercial license.

For this repo, `probe_intel` maps to `intel.py` and its strategy logic.

## Core Principle

Split by **value capture**, not convenience:

- Public code should make integration, deployment, and community contribution easy.
- Private code should contain everything that materially determines extraction quality, robustness, and iteration speed.

If a competitor can copy a file and get meaningfully closer to your results, that file stays private.

## Non-Negotiable Rules

1. **Open interface, closed implementation**
- Public code can call a stable private API.
- Public code cannot import private implementation modules.

2. **Hard runtime boundary**
- Private core runs as a separate service/image/package.
- Public services call private core through authenticated RPC/HTTP only.

3. **One-way dependency**
- Public repo defines API contracts and clients.
- Private repo implements contracts.
- Private must not require public internals that force code leakage.

4. **Least disclosure**
- Private responses return results, not strategy internals.
- Logs/errors must not expose weights, heuristics, ranking rationale, or hidden selectors.

5. **Private-by-default governance**
- New “intelligence” modules are private by default.
- Opening a module requires explicit IP review.

## Current Repo Mapping

### Keep Private (commercial)

- `unchained/cdp.py`
- `unchained/ddm.py`
- `unchained/intel.py`
- `unchained/orchestrator.py` (contains system methodology and tool strategy)
- Any benchmark/eval artifacts that encode private extraction strategy:
  - `unchained/benchmark/*`

### Candidate Public (infra/control plane)

- `unchained/relay.py`
- `unchained/chrome_bridge.py`
- `unchained/auth.py` (or a simplified OSS auth adapter)
- `unchained/api.py` (without private implementation imports)
- `unchained/web.py` (if product UI is intended OSS)
- `unchained/mcp_server.py` (tool wrappers only)
- `unchained/agent_package.py`
- `unchained/cdp_tool.py` (thin client)
- Deployment and compose assets:
  - `docker-compose.yml`
  - `Dockerfile`
  - `.github/workflows/ci.yml`

### Boundary File To Introduce

- `private_core_client.py` (public): typed client for private service endpoints.
- `private_core_contracts.py` (public): request/response schemas and errors.

Public modules must call `private_core_client` instead of importing `cdp/ddm/intel`.

## Target Architecture

1. Public services:
- Relay
- API/Web
- MCP wrappers
- Agent tooling

2. Private service:
- `core-engine` with internal implementation of `cdp/ddm/intel`

3. Public-to-private contract:
- `POST /core/ddm`
- `POST /core/intel/probe`
- `POST /core/intel/extract`
- `POST /core/cdp/{action}`

4. Auth model:
- Service-to-service auth (signed token or mTLS) for public->private calls.
- End-user API keys stay in public control plane.

## Packaging and Licensing Policy

1. Public repo license:
- Apache-2.0 (recommended) or MIT.

2. Private repo license:
- Proprietary commercial license (EULA/terms), no redistribution, no reverse engineering, no benchmark publication without permission.

3. Shipping policy:
- Public release artifacts must not include private source, prompts, benchmark corpora, or strategy constants.

## Rollout Phases

1. **Phase 1: Contract extraction**
- Define contracts and move existing callers to `private_core_client`.
- No behavior change.

2. **Phase 2: Private service isolation**
- Move `cdp/ddm/intel/orchestrator intelligence` into private repo/service.
- Keep public wrappers stable.

3. **Phase 3: OSS hardening**
- Add contribution docs, extension points, and mock core backend.
- Publish public repo.

4. **Phase 4: Commercial enforcement**
- Private image distribution + customer license workflow.

## Test Plan

The plan validates both functional behavior and IP boundary enforcement.

### A. Contract Tests (Public)

Purpose: ensure public services only rely on stable private contracts.

- Test all `private_core_client` calls with mocked private responses.
- Validate schema compatibility and error mapping.
- Freeze contract versions (`v1`) and add backward-compat tests.

Exit criteria:
- Public tests pass without private source present.

### B. Boundary Import Tests (Public)

Purpose: block accidental imports of private modules.

- Static scan for forbidden imports:
  - `import cdp`, `import ddm`, `import intel`
  - `from cdp import`, `from ddm import`, `from intel import`
- Fail CI if any are found outside approved private-only paths.

Exit criteria:
- Zero forbidden imports in OSS tree.

### C. Artifact Leakage Tests (Public CI)

Purpose: prevent private IP in releases.

- Scan release tarball/container for forbidden file names and signatures:
  - `ddm.py`, `intel.py`, `benchmark/*`, strategy keyword lists.
- Scan docs for private prompts and internal heuristics.

Exit criteria:
- Release artifacts contain no forbidden files/strings.

### D. Runtime Separation Tests (Integration)

Purpose: verify hard boundary at runtime.

- Start public stack + mock private service.
- Confirm all tool operations work through network calls only.
- Kill private service and verify public stack fails with expected contract errors.

Exit criteria:
- Public stack does not function when private endpoint is absent.
- No local fallback imports to private modules.

### E. Security Tests (Public + Private)

Purpose: ensure boundary cannot be bypassed.

- Unauthorized calls to private endpoints return 401/403.
- Replay/expired service token tests.
- Ensure sensitive internals are redacted from logs and error payloads.

Exit criteria:
- Auth enforced on all private endpoints.
- No strategy internals in logs/errors.

### F. Regression Tests (Private)

Purpose: preserve quality moat without exposing internals.

- Keep private eval suite and benchmark tasks in private CI.
- Track extraction success, latency, and stability across versions.
- Gate private releases on regression thresholds.

Exit criteria:
- Private KPIs meet release bars.

## CI Gate Checklist (Must Pass Before OSS Release)

1. Boundary import tests pass.
2. Artifact leakage scan passes.
3. Contract tests pass against mock private backend.
4. Runtime separation tests confirm no direct private imports.
5. Security tests pass for private endpoint auth and redaction.

## Immediate Next Step in This Repo

Implement `private_core_client.py` and refactor:

- `unchained/cloud_tools.py`
- `unchained/mcp_server.py`
- `unchained/api.py`

so no public-facing path imports private implementations directly.
