# Financial Workspace Control Plane

Feature-flagged (default **off**) account-scoped workspace runtime for the
public terminal: S2S checkpoint ingestion, claim-aware OAuth across Google /
Facebook / GitHub, exactly-once workspace/import, new-account-only credit, and
account-scoped wake/sleep canary scaffolding.

All behavior is inert while `FIN_TERMINAL_WORKSPACE_ENABLED` is `false`. The
existing six-seat pilot, the signed-in singleton terminal, and the normal
auth/credit flows are untouched.

## Architecture

```
[fin-terminal app (S2S)] ── POST /internal/financial-workspace/checkpoints
        │                    (Bearer FIN_WORKSPACE_CONTROL_TOKEN, Docker-internal)
        v
[fin-terminal-workspace-control:8790]   (same unchained web app, own port)
        │  S3 + AWS KMS (envelope-encrypted checkpoints)
        │  SQLite (relay_data:/data/auth.db) — claims/workspaces/outbox/credit
        │  browser flow (dedicated /workspace/* namespace):
        v
[unbrowser.unchainedsky.com/fin-terminal-workspace/*] via Caddy
        │   /workspace/auth/claim       → claim initiation (secret in HttpOnly cookie)
        │   /workspace/oauth/{provider}/start  → OAuth state bound to the claim
        │   /workspace/oauth/{provider}/callback → state binding verified, claim accepted
        │   /workspace/workspace, /workspace/snapshots, /workspace/runtime/status
        │   /workspace/done             → claim completion (CTA gated on provider)
        │   /workspace-terminal         → the /fin-terminal/ leg
        │   /attach/{slug}/*            → account runtime proxy (HTTP + WS)
        │  runtime provider (host-side, no Docker socket in containers):
        v
[fin-workspace-runtime-provider] (host systemd, Docker authority)
        │  per-account container fin-workspace-<slug> on private fin_ws_<slug>
        │  network + fin_ws_<slug>_data volume (checkpoint file provisioned)
        └─ /v1/health | /v1/accounts/{slug}/wake|sleep|flush|status
```

Caddy only ever proxies the exact `/fin-terminal-workspace/*` browser surface
(single matcher, prefix stripped) and denies `/internal/*` on the public edge.
Internal endpoints are reachable only from the Docker-internal network.

## Private workspace leg — authenticated `/fin-terminal/`

When the feature is enabled, Caddy maps `/fin-terminal/` to the leg
(`/workspace-terminal` after stripping the prefix) instead of the marketing
index or the public singleton. The leg fails closed at every step:

1. Session-authenticated account required (401, no CTA).
2. Imported workspace required (404, no CTA).
3. Validated host-side runtime provider required — the provider must answer
   `/v1/health` with `accountRuntime` + `checkpointFile` capabilities.
   Otherwise the leg returns 503 with an explicit reason and no CTA, and
   **activation itself is gated**: the control plane refuses to boot when the
   feature is enabled without a validated provider.
4. Success → wake the account runtime (provisioning the imported checkpoint
   to the per-account checkpoint file) → serve the attach page; the browser
   then reaches the account's isolated runtime via
   `/fin-terminal/attach/{slug}/` (proxied HTTP + WebSocket by the control
   plane over the private per-account network).

The done page's "Open workspace" CTA renders only while the provider is
validated; otherwise the page carries no CTA (fail closed, no false route).
The host-side provider is documented in
[`docs/terminal-runtime-reconciler.md`](terminal-runtime-reconciler.md) and
implemented by `deploy/workspace_runtime_provider.py` +
`deploy/fin-workspace-runtime-provider.service`.

## Fail-closed configuration

Enabling the feature without the explicit control token, S3 bucket, region,
KMS key, and cookie domain is a startup error (`create_app()` raises; the
container refuses to boot). `JWT_SECRET` is **never** a fallback for the
control token and `LocalCheckpointStore` is **never** used in production —
storage is `S3CheckpointStore` or nothing.

Required env (see `.env.workspace.example`):

| Variable | Purpose |
|---|---|
| `FIN_TERMINAL_WORKSPACE_ENABLED` | master flag (Caddy + control plane) — canonical `true`/`false` |
| `FIN_WORKSPACE_ENABLED` | app-level flag (mirrors the master flag; normalizes `1|true|yes|on`) |
| `FIN_WORKSPACE_CONTROL_TOKEN` | S2S bearer for `/internal/*` (32+ chars) |
| `FIN_WORKSPACE_COOKIE_DOMAIN` | parent domain for the HttpOnly claim cookie |
| `FIN_WORKSPACE_S3_BUCKET` | checkpoint object storage |
| `FIN_WORKSPACE_S3_REGION` | explicit region (no `AWS_REGION` fallback) |
| `FIN_WORKSPACE_KMS_KEY_ID` | KMS key for envelope DEK wrapping |
| `FIN_WORKSPACE_RUNTIME_PROVIDER_URL` | host-side runtime provider base (hard enablement gate) |
| `FIN_WORKSPACE_RUNTIME_PROVIDER_TOKEN` | shared secret with the runtime provider (32+ chars) |
| `FIN_TERMINAL_BASE_URL` | public base (defaults to `https://unbrowser.unchainedsky.com/fin-terminal-workspace`) |

**Hard enablement gate:** enabling the feature without a validated host-side
runtime provider is a startup error. `validate_fin_workspace_config()`
requires `FIN_WORKSPACE_RUNTIME_PROVIDER_URL` +
`FIN_WORKSPACE_RUNTIME_PROVIDER_TOKEN`, and `_init_fin_workspace_control_plane`
requires the provider to answer `/v1/health` with `accountRuntime` +
`checkpointFile` capabilities before the control plane boots. Until the
pinned app image's account-runtime support is verified, the operator keeps
`FIN_WORKSPACE_RUNTIME_APP_CAPABLE=false` on the provider and activation
stays failed closed — `/fin-terminal/` is never falsely routed.

## S2S checkpoint contract

`POST /internal/financial-workspace/checkpoints`

- Header: `Authorization: Bearer $FIN_WORKSPACE_CONTROL_TOKEN`
- Body (bounded, server-generated checkpoint):
  ```json
  {
    "requestId": "req-001",
    "source": {"sessionId": "sess-1", "workerId": "seat-01",
               "generation": "g-1", "sourceRevision": "rev-1"},
    "checkpoint": {"holdings": [], "balance": 0}
  }
  ```
- Response `201` (canonical snake_case; `expires_at` is Unix epoch **seconds**):
  ```json
  {"checkpoint_id": "fcp-...", "expires_at": 1750000000.0, "handoff_id": "fh-...",
   "handoff_secret": "<S2S only>", "auth_url": "https://.../fin-terminal-workspace/workspace/auth/claim?handoff_id=...", "status": "ready"}
  ```
- The `handoff_secret` is returned **only** in this S2S response. It never
  appears in the `auth_url`, a log line, or a browser URL.
- A `requestId` already in `ready` state returns `200` with the same secret
  (idempotent).
- The app-side gateway normalizes this response: `expires_at` (seconds) →
  `expiresAt` (epoch ms, `* 1000`), `checkpoint_id` → `checkpointId`, etc. The
  camelCase spelling is tolerated for rollout; snake_case is canonical.

Other internal endpoints (`GET /checkpoints/{id}`, `POST /claim`,
`POST /claim/accept`, `GET /claims/{id}`, `GET /workspace`,
`GET /snapshots`, `POST /effects/process`, `POST /sweep`, runtime
wake/sleep/status) are documented in `unchained/web_app/handlers/fin_workspace.py`.

The exact cross-repo wire contract (units, headers, cookie names, env vars,
paths) is the canonical document in
[`docs/financial-terminal-cross-repo-contract.md`](financial-terminal-cross-repo-contract.md).

## Claim flow (browser)

1. The app's gateway exports the assigned worker's authoritative checkpoint,
   forwards it to this control plane, and sets the **handoff secret** in a
   host-only `HttpOnly; Secure; SameSite=Lax; Path=/` cookie named
   `fin-terminal-handoff-secret`. The browser opens `auth_url`, which contains
   only the opaque `handoff_id` and lives at
   `/fin-terminal-workspace/workspace/auth/claim` (dedicated `/workspace/*`
   namespace — never `/auth/...` or `/api/...`, so the claim OAuth routes can
   not shadow the site's login routes).
2. The claim page holds **no secret**: selecting a provider POSTs
   `{handoff_id, browser_nonce, audience}` to `/workspace/claim`. The control
   plane reads the handoff secret server-side from the
   `fin-terminal-handoff-secret` cookie, verifies it, creates the claim,
   **rotates the handoff cookie away**, sets the `fw_claim_secret` (HttpOnly,
   Secure, SameSite=Lax, parent-domain) and `fw_claim_nonce` cookies, and
   returns `claim_id` + the OAuth start URL.
3. The browser follows `/workspace/oauth/{provider}/start?claim_id=...` — the
   provider OAuth state is bound to the claim.
4. `/workspace/oauth/{provider}/callback` verifies the claim cookie and the
   exact state binding, get-or-creates the user, records the provider origin,
   and accepts the claim **exactly once** (workspace + import + snapshot +
   outbox effects in one transaction).
5. New accounts receive an idempotent USD 1.00 grant; accounts that already
   had a credit account receive nothing.

Security invariants (tested):

- The handoff secret is never readable by browser JS: it exists only in the
  HttpOnly cookie and in the S2S create-checkpoint response. There is no
  `postMessage` secret path and no `handoff_secret` field accepted in the
  claim body (sending one returns 400).
- The handoff secret never appears in `auth_url`, the claim page HTML, Caddy
  logs (`log_skip`), referrers (`no-referrer`), or analytics.
- The claim page and Google GSI page carry `Content-Security-Policy` meta tags.

Exact callback allowlist: only `google`, `facebook`, `github` are accepted at
both the Caddy layer and the handler; any other provider returns 404.

## Outbox processing and sweeping

When the feature is enabled, the control plane starts two background loops:

- `FIN_WORKSPACE_EFFECT_INTERVAL_SECONDS` (default 15) — processes pending
  `account_grant` effects through the credit ledger with idempotency keys.
- `FIN_WORKSPACE_SWEEP_INTERVAL_SECONDS` (default 300) — expires
  ready/claiming checkpoints past their 1-hour TTL and deletes their encrypted
  storage.

The same work is available on demand via `POST /internal/.../effects/process`
and `POST /internal/.../sweep`.

## Account-scoped runtime control (canary)

`POST /internal/financial-workspace/runtime/wake|sleep` and
`GET /internal/financial-workspace/runtime/status` (control-token protected)
toggle `awake|asleep|draining` per account in
`financial_workspace_runtimes`. The browser surface exposes
`GET /workspace/runtime/status`. The `/fin-terminal/` leg (see above)
orchestrates the host-side provider: wake provisions the account's isolated
container + checkpoint file, and `POST /internal/financial-workspace/runtime/flush`
persists a checkpoint flushed back from the runtime as a new snapshot. This is
inert when the feature is off.

## Deployment

- `fin-terminal-workspace-control` builds from this repo (same image as
  `web`) and runs `python -m web --port 8790`. It is only started with the
  `fin-terminal-workspace` compose profile.
- Networks: `fin_terminal_public` (Caddy), `fin_terminal_public_state`
  (Redis), and `fin_terminal_workspace_egress` (S3/KMS egress only — the only
  non-internal network the control plane joins). No Docker socket; the
  host-side `fin-workspace-runtime-provider` service owns Docker.
- `/fin-terminal/` routes to the private-workspace leg **only when**
  `FIN_TERMINAL_WORKSPACE_ENABLED=true` **and** the runtime provider is
  validated; otherwise it fails closed (no CTA) and the signed-in singleton
  (`fin-terminal:8787`) serves when the flag is off (rollback by turning the
  flag off). It never renders the marketing index.
