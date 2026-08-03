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
        │  browser flow:
        v
[unbrowser.unchainedsky.com/fin-terminal-workspace/*] via Caddy
        │   /auth/claim  → claim initiation (secret in POST body only)
        │   /auth/{provider}/start → OAuth state bound to the claim
        │   /callback/{provider}   → state binding verified, claim accepted
        │   /api/workspace, /api/snapshots, /api/runtime/status
```

Caddy only ever proxies the exact `/fin-terminal-workspace/*` browser surface
and denies `/internal/*` on the public edge. Internal endpoints are reachable
only from the Docker-internal network.

## Fail-closed configuration

Enabling the feature without the explicit control token, S3 bucket, region,
KMS key, and cookie domain is a startup error (`create_app()` raises; the
container refuses to boot). `JWT_SECRET` is **never** a fallback for the
control token and `LocalCheckpointStore` is **never** used in production —
storage is `S3CheckpointStore` or nothing.

Required env (see `.env.workspace.example`):

| Variable | Purpose |
|---|---|
| `FIN_TERMINAL_WORKSPACE_ENABLED` | master flag (Caddy + control plane) |
| `FIN_WORKSPACE_ENABLED` | app-level flag (mirrors the master flag) |
| `FIN_WORKSPACE_CONTROL_TOKEN` | S2S bearer for `/internal/*` (32+ chars) |
| `FIN_WORKSPACE_COOKIE_DOMAIN` | parent domain for the HttpOnly claim cookie |
| `FIN_WORKSPACE_S3_BUCKET` | checkpoint object storage |
| `FIN_WORKSPACE_S3_REGION` | explicit region (no `AWS_REGION` fallback) |
| `FIN_WORKSPACE_KMS_KEY_ID` | KMS key for envelope DEK wrapping |
| `FIN_TERMINAL_BASE_URL` | public base (defaults to `https://unbrowser.unchainedsky.com/fin-terminal-workspace`) |

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
- Response `201`:
  ```json
  {"checkpoint_id": "fcp-...", "expires_at": 0, "handoff_id": "fh-...",
   "handoff_secret": "<S2S only>", "auth_url": "https://.../fin-terminal-workspace/auth/claim?handoff_id=...", "status": "ready"}
  ```
- The `handoff_secret` is returned **only** in this S2S response. It never
  appears in the `auth_url`, a log line, or a browser URL.
- A `requestId` already in `ready` state returns `200` with the same secret
  (idempotent).

Other internal endpoints (`GET /checkpoints/{id}`, `POST /claim`,
`POST /claim/accept`, `GET /claims/{id}`, `GET /workspace`,
`GET /snapshots`, `POST /effects/process`, `POST /sweep`, runtime
wake/sleep/status) are documented in `unchained/web_app/handlers/fin_workspace.py`.

## Claim flow (browser)

1. The app opens `auth_url` (only the opaque `handoff_id` is in the URL).
2. The claim page holds the secret in memory (postMessage from the app
   frontend) and `POST /api/claim` with `{handoff_id, handoff_secret,
   browser_nonce, audience}`.
3. The control plane sets an HttpOnly Secure SameSite=Lax **parent-domain**
   cookie (`fw_claim_secret`, `Path=/`) and redirects to
   `/auth/{provider}/start?claim_id=...` — the provider OAuth state is bound
   to the claim.
4. `/callback/{provider}` verifies the claim cookie and the exact state
   binding, get-or-creates the user, records the provider origin, and accepts
   the claim **exactly once** (workspace + import + snapshot + outbox effects
   in one transaction).
5. New accounts receive an idempotent USD 1.00 grant; accounts that already
   had a credit account receive nothing.

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
`financial_workspace_runtimes`. The browser surface exposes `GET /api/runtime/status`.
This is canary scaffolding for warm/cool account workspaces; it is inert when
the feature is off.

## Deployment

- `fin-terminal-workspace-control` builds from this repo (same image as
  `web`) and runs `python -m web --port 8790`. It is only started with the
  `fin-terminal-workspace` compose profile.
- Networks: `fin_terminal_public` (Caddy), `fin_terminal_public_state`
  (Redis), and `fin_terminal_workspace_egress` (S3/KMS egress only — the only
  non-internal network the control plane joins). No Docker socket.
- `/fin-terminal/` routes to the workspace control plane **only when**
  `FIN_TERMINAL_WORKSPACE_ENABLED=true`; otherwise the signed-in singleton
  (`fin-terminal:8787`) is served unchanged (rollback by turning the flag off).
