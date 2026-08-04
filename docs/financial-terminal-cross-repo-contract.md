# Financial Terminal ↔ Workspace Control Plane — Canonical Cross-Repo Contract

**This is the single canonical contract.** Both repositories implement exactly
this wire schema, units, headers, cookie names/scopes, env vars, and paths:

- App: [`protostatis/unbrowser-fin-terminal`](https://github.com/protostatis/unbrowser-fin-terminal)
- Infra: [`protostatis/unchained-infra`](https://github.com/protostatis/unchained-infra)
  (this repository)

Any change to a value below must update this document and both implementations
in the same change. Version bumps (`v1`, wire `expires_at` seconds) are
intentional and additive-only until a new major version is cut.

## 0. Feature-boolean contract

Every feature-flag env value accepts **`1|true|yes|on`** (trimmed,
case-insensitive) as truthy and normalizes it before use; everything else is
off. Caddy expression matchers and the Compose render use the canonical
**`true`/`false`** value (Caddy's CEL matchers require the literal boolean),
so the master flag on the edge is always `true`/`false` while the control
plane, gateway, and reconciler normalize all four spellings:

- `FIN_TERMINAL_WORKSPACE_ENABLED` (Caddy + Compose + control plane + gateway)
- `FIN_WORKSPACE_ENABLED` (app-level mirror, derived from the master flag)
- `FINANCIAL_WORKSPACE_CHECKPOINTS` (gateway, derived from the master flag)
- `TERMINAL_RUNTIME_FEATURE_ENABLED` (reconciler + gateway + workers)

Infra tests render the compose with the master flag set and assert the same
value reaches every consumer (see `test_terminal_runtime_reconciler.py` →
`ComposeRenderTests`).

---

## 1. Workspace checkpoint create (S2S, app gateway → infra control plane)

`POST /internal/financial-workspace/checkpoints`

| Item | Value |
|---|---|
| Transport | Docker-internal HTTP only. Caddy denies `/internal/*` on the public edge. |
| Auth header | `Authorization: Bearer $FIN_WORKSPACE_CONTROL_TOKEN` |
| Request body | `{requestId, source: {sessionId, workerId, generation, sourceRevision?}, checkpoint}` |

Response `201` (canonical **snake_case**):

```json
{
  "checkpoint_id": "fcp-...",
  "expires_at": 1750000000.0,
  "handoff_id": "fh-...",
  "handoff_secret": "<S2S only>",
  "auth_url": "https://unbrowser.unchainedsky.com/fin-terminal-workspace/workspace/auth/claim?handoff_id=fh-...",
  "already_exists": false,
  "status": "ready"
}
```

- **`expires_at` is Unix epoch SECONDS (float).** The app normalizes it to
  epoch **ms** (`* 1000`) before computing the handoff cookie's Express
  `maxAge` (which is **milliseconds**) and before returning `expiresAt` to its
  own browser (the checkpoint codec uses ms everywhere). A value above
  `9_000_000_000` (i.e. already in ms) is rejected, never silently converted.
- The camelCase spelling (`checkpointId`, `expiresAt`, `handoffId`,
  `handoffSecret`, `authUrl`) is tolerated by the app **during rollout only**
  and normalized to the same internal type. `expiresAt` in the tolerated
  spelling is still epoch **seconds**. New deployments must emit snake_case.
- `handoff_secret` is returned **only** in this S2S response. It never appears
  in `auth_url`, logs, referrers, or browser JS.
- A repeated `requestId` already in `ready` state returns `200` with the same
  secret (idempotent).

## 2. Handoff secret / claim cookie flow

| Cookie | Set by | Read by | Attributes |
|---|---|---|---|
| `fin-terminal-handoff-secret` | app gateway (`workspace-checkpoint-control.ts`) | infra control plane, server-side at `POST /workspace/claim` | `HttpOnly; Secure; SameSite=Lax; Path=/`; **host-only** (no `Domain`). Optional `Domain` only via `FINANCIAL_WORKSPACE_HANDOFF_COOKIE_DOMAIN` if the surfaces are ever split across subdomains. |
| `fw_claim_secret` | infra control plane at claim initiation | infra control plane at OAuth start/callback | `HttpOnly; Secure; SameSite=Lax; Path=/; Domain=$FIN_WORKSPACE_COOKIE_DOMAIN` |
| `fw_claim_nonce` | infra control plane at claim initiation | infra control plane at claim accept | `HttpOnly; Secure; SameSite=Lax; Path=/; Domain=$FIN_WORKSPACE_COOKIE_DOMAIN` |

The handoff secret and claim secret are host-only/parent-domain **HttpOnly**
cookies. They are never accepted from JS, the POST body, a URL, or a `Referer`:

- The claim body carries only `{handoff_id, browser_nonce, audience}`. Sending
  `handoff_secret` in the body is rejected with 400.
- There is no `postMessage` secret path in either page; the claim page and the
  Google GSI page carry `Content-Security-Policy` meta tags and
  `referrer=no-referrer`.
- At claim initiation the control plane reads the handoff secret from
  `fin-terminal-handoff-secret`, verifies it against the stored hash, creates
  the claim, **clears the handoff cookie**, sets `fw_claim_secret` +
  `fw_claim_nonce`, and returns `{claim_id, oauth_start_url}`.
- At claim accept the control plane clears `fw_claim_secret`/`fw_claim_nonce`.

## 3. Private management API (app gateway ← infra reconciler + app workers)

Listener: port **8789** (`TERMINAL_RUNTIME_MANAGEMENT_PORT`), never published
to the host, never proxied by Caddy. The host reconciler reaches it via
`docker exec -i <gateway> node -e <script>` on loopback; worker permit clients
reach it over private Compose networks at `http://fin-terminal-public-gateway:8789`.

Every request requires header `X-Management-Token: $TERMINAL_RUNTIME_MANAGEMENT_TOKEN`.

### `POST /api/management/reconcile-snapshot` — body `{}`

```json
{
  "version": 1,
  "seats": {
    "seat-01": {
      "workerId": "seat-01",
      "status": "healthy",
      "phase": "active",
      "generation": "gen-...",
      "assigned": true,
      "idleSeconds": 0,
      "drainRequested": false,
      "drainId": null,
      "containerId": ""
    }
  },
  "totalAssigned": 1,
  "totalQueued": 0,
  "plan": {
    "desiredRunning": 2,
    "scaleDownCandidates": [],
    "activateCandidates": []
  }
}
```

- `seats` always contains exactly six named records keyed by the gateway's
  **worker id** (`seat-01`..`seat-06`).
- `status` is one of `absent | starting | healthy | draining | stopped`
  (mapped from the app's phase model; `recycling` → `stopped`).
- `assigned` is true for `assigned|admitted|active|disconnected` phases.
- `idleSeconds` is the whole number of elapsed idle seconds (floored). It is
  populated for `ready-idle` seats from the moment the slot became healthy and
  unassigned (persisted across gateway restarts) and for `active` seats from
  the session's last-activity time; it is monotonic and never negative.
  Draining seats report `0`. The reconciler uses it to order scale-down
  candidates (longest idle first).
- Five-minute eligibility is **exact**: the gateway only lists a ready-idle
  seat as a scale-down candidate after `TERMINAL_RUNTIME_IDLE_SCALE_DOWN`
  seconds (default 300) of continuous idle, and the `/drain` endpoint rejects a
  request before that threshold with `409 { accepted: false }`. Drain-then-stop
  and the generation CAS still protect every seat.
- `containerId` is always `""` — Docker authority is host-side.
- The reconciler maps worker ids to its allowlisted Compose service names
  (`fin-terminal-public-seat-01`..`-06`) via an exact tested bijection.
- `plan.desiredRunning` is **authoritative** for the reconcile decision;
  `totalAssigned`/`totalQueued` are informational.

### `POST /api/management/reconcile-plan` — body `{}`

```json
{ "version": 1, "reconciled": true, "plan": { "desiredRunning": 2, "scaleDownCandidates": [], "activateCandidates": [] } }
```

### `POST /api/management/drain`

Request: `{"workerId": "seat-01", "drainId": "dr-...", "expectedGeneration": "gen-..."}`

- Success `200`: `{"accepted": true, "drainId": "dr-..."}`
- Conflict `409`: `{"accepted": false, "reason": "unknown seat" | "generation mismatch" | "seat ... is protected (...)" | "cannot drain seat in ... phase" | "seat is already draining with a different drain-id"}`
- `expectedGeneration` is a **CAS**: a value that does not match the seat's
  current generation is rejected so a replaced worker is never drained.
- The reconciler generates `drainId` (`dr-<hex>`); the gateway treats
  `(drainId, generation)` idempotently.

### `POST /api/management/activate`

Request: `{"workerId": "seat-01"}`

- Success `200`: `{"accepted": true}`
- Conflict `409`: `{"accepted": false, "reason": "unknown seat" | "drain sticky; generation unchanged"}`
- A non-draining seat is an accepted no-op. A draining seat is released only
  when its generation changed since the drain (the reconciler restarted the
  container); a same-generation activate is rejected (sticky drain).

### Seat status mapping (app phase → wire status)

| app phase | wire `status` | `assigned` |
|---|---|---|
| `absent` | `absent` | false |
| `starting` | `starting` | false |
| `ready-idle` | `healthy` | false |
| `assigned` | `healthy` | true |
| `admitted` | `healthy` | true |
| `active` | `healthy` | true |
| `disconnected` | `healthy` | true |
| `draining` | `draining` | false |
| `recycling` | `stopped` | false |

### Worker research-permit surface (same listener, same token)

`POST /api/management/research-permits/acquire|status|heartbeat|release`
with `{sessionId, workerGeneration}` / `{requestId}` / `{requestId, sessionId?}`
/ `{requestId}`. Workers reach it over the private seat networks.

## 4. Public / internal paths (Caddy prefix stripping must match handlers)

Public base: `https://unbrowser.unchainedsky.com/fin-terminal-workspace`
(`FIN_TERMINAL_BASE_URL`). Caddy strips `/fin-terminal-workspace` and proxies
to `fin-terminal-workspace-control:8790`.

| Public URL (Caddy) | Stripped → handler route | Handler |
|---|---|---|
| `POST /fin-terminal-workspace/workspace/auth/claim?handoff_id=...` | `GET /workspace/auth/claim` | `handle_fin_workspace_auth_claim_page` |
| `POST /fin-terminal-workspace/workspace/claim` | `POST /workspace/claim` | `handle_fin_workspace_browser_claim` |
| `GET /fin-terminal-workspace/workspace/claims/{claim_id}` | `GET /workspace/claims/{claim_id}` | `handle_fin_workspace_browser_get_claim` |
| `GET /fin-terminal-workspace/workspace/workspace` | `GET /workspace/workspace` | `handle_fin_workspace_browser_get_workspace` |
| `GET /fin-terminal-workspace/workspace/snapshots` | `GET /workspace/snapshots` | `handle_fin_workspace_browser_get_snapshots` |
| `GET /fin-terminal-workspace/workspace/runtime/status` | `GET /workspace/runtime/status` | `handle_fin_workspace_browser_runtime_status` |
| `POST /fin-terminal-workspace/workspace/oauth/google` | `POST /workspace/oauth/google` | `handle_claim_google_token` |
| `GET /fin-terminal-workspace/workspace/oauth/{provider}/start?claim_id=...` | `GET /workspace/oauth/{provider}/start` | `handle_claim_oauth_start` |
| `GET /fin-terminal-workspace/workspace/oauth/{provider}/callback` | `GET /workspace/oauth/{provider}/callback` | `handle_claim_oauth_callback` |
| `GET /fin-terminal-workspace/workspace/done?claim_id=...&status=...` | `GET /workspace/done` | `handle_claim_done` |

The claim surface is the **dedicated `/workspace/*` namespace** — it can never
shadow (or be shadowed by) the site's own login OAuth routes
(`/auth/facebook/...`, `/auth/github/...`). Router-resolution tests in
`test_web_routes.py` pin both sides: the login routes resolve to the login
handlers and the claim routes resolve to the claim handlers.

Provider allowlist is exact: `google`, `facebook`, `github`; anything else
returns 404. Caddy strips `Authorization`, `Proxy-Authorization`,
`X-Management-Token`, `X-Fin-Terminal-User`, `X-Fin-Terminal-Proxy-Token` on
the workspace surface, preserves browser cookies, sets `log_skip`, and denies
`/internal/*` on the public edge.

Internal (control-token protected, Docker-internal only):

| Route | Purpose |
|---|---|
| `POST /internal/financial-workspace/checkpoints` | create checkpoint (S2S) |
| `GET /internal/financial-workspace/checkpoints/{checkpoint_id}` | checkpoint status (never returns the handoff secret) |
| `POST /internal/financial-workspace/claim` | claim initiation (cookie secret) |
| `POST /internal/financial-workspace/claim/accept` | claim acceptance |
| `GET /internal/financial-workspace/claims/{claim_id}` | claim status |
| `GET /internal/financial-workspace/workspace` / `snapshots` | user data reads |
| `POST /internal/financial-workspace/effects/process` / `sweep` | outbox / expiry |
| `POST /internal/financial-workspace/runtime/wake` / `sleep`; `GET .../status` | account runtime |
| `POST /internal/financial-workspace/runtime/flush` | persist a checkpoint flushed back from the account runtime (`{slug, checkpoint}`) |

Worker-side private route (app, never public):

| Route | Purpose |
|---|---|
| `POST /internal/financial-workspace/checkpoint-export` | worker exports authoritative checkpoint for the exact `{sessionId, generation}`; headers `X-Fin-Terminal-Control-Token` + worker proxy token |

## 4b. Private workspace leg — authenticated `/fin-terminal/`

When `FIN_TERMINAL_WORKSPACE_ENABLED=true`, Caddy maps `/fin-terminal/` to the
control plane's private-workspace leg (`/workspace-terminal` after the prefix
is stripped; subpaths such as `/fin-terminal/attach/<slug>/` proxy unchanged
and are routed to the account's isolated runtime). The leg NEVER renders the
marketing index or the public singleton:

1. Session-authenticated account required (401 fail-closed page otherwise).
2. Imported workspace required (404 fail-closed page otherwise).
3. A **validated** host-side runtime provider required — the provider must
   answer `/v1/health` with `accountRuntime` + `checkpointFile` capabilities.
   Without it the leg returns 503 with an explicit reason and **no CTA**
   (activation itself is gated at control-plane boot).
4. On success the leg wakes the account runtime (provisioning the imported
   checkpoint to the per-account checkpoint file) and serves the attach page;
   `/fin-terminal/attach/{slug}/*` is proxied (HTTP + WebSocket) to
   `fin-workspace-<slug>:8787` over the private per-account network. The slug
   is derived server-side from the session — never trusted from the URL.

The account runtime contract (app core, validated via the provider's
`FIN_WORKSPACE_RUNTIME_APP_CAPABLE` flag):

- Per-account container `fin-workspace-<slug>` on private network
  `fin_ws_<slug>` (internal) + volume `fin_ws_<slug>_data`; no published host
  ports; `cap_drop ALL`; no-new-privileges; read-only rootfs; never a Docker
  socket inside a container (Docker authority is host-side only).
- Checkpoint-file provisioning: the imported snapshot is written to
  `FIN_WORKSPACE_CHECKPOINT_FILE` (default `/data/checkpoint.json`) on the
  per-account volume; the app runtime reads it on boot.
- The control-plane container is attached to the per-account network at wake
  and detached at sleep so it is the only bridge to the account runtime.
- Wake/attach/flush/sleep lifecycle is owned by the host-side
  `fin-workspace-runtime-provider` systemd service.

When the workspace flag is OFF the signed-in singleton
(`fin-terminal:8787` + `forward_auth`) serves `/fin-terminal/*` unchanged —
Caddy's singleton matchers are the exact negation of the workspace matcher,
so the path can never fall through to the landing page.

## 5. Environment variables

### Control plane (infra `fin-terminal-workspace-control`)

| Variable | Purpose | Default |
|---|---|---|
| `FIN_TERMINAL_WORKSPACE_ENABLED` | master flag (Caddy + control plane + gateway); canonical `true`/`false` | `false` |
| `FIN_WORKSPACE_ENABLED` | app-level mirror of the master flag (normalizes `1|true|yes|on`) | derived |
| `FIN_WORKSPACE_CONTROL_TOKEN` | S2S bearer for `/internal/*` (32+ chars) | required when enabled |
| `FIN_WORKSPACE_COOKIE_DOMAIN` | parent domain for `fw_claim_secret`/`fw_claim_nonce` | required when enabled |
| `FIN_WORKSPACE_S3_BUCKET` / `_REGION` / `KMS_KEY_ID` | envelope-encrypted checkpoint storage | required when enabled |
| `FIN_WORKSPACE_RUNTIME_PROVIDER_URL` | host-side runtime provider base (hard enablement gate) | `http://host.docker.internal:8793` |
| `FIN_WORKSPACE_RUNTIME_PROVIDER_TOKEN` | shared secret with the provider (32+ chars) | required when enabled |
| `FIN_TERMINAL_BASE_URL` | public base (`/fin-terminal-workspace`) | `https://unbrowser.unchainedsky.com/fin-terminal-workspace` |

### Gateway (app `fin-terminal-public-gateway`)

| Variable | Purpose |
|---|---|
| `TERMINAL_RUNTIME_MODE=public-gateway` | runtime mode |
| `TERMINAL_RUNTIME_MANAGEMENT_TOKEN` | must equal the reconciler's token |
| `TERMINAL_RUNTIME_FEATURE_ENABLED` | must equal the reconciler's flag |
| `TERMINAL_RUNTIME_MANAGEMENT_PORT` | private listener port (default `8789`) |
| `FINANCIAL_WORKSPACE_CHECKPOINTS` | derived from `FIN_TERMINAL_WORKSPACE_ENABLED` |
| `FINANCIAL_WORKSPACE_CONTROL_TOKEN` | must equal `FIN_WORKSPACE_CONTROL_TOKEN` |
| `FINANCIAL_WORKSPACE_SERVICE_URL` | `http://fin-terminal-workspace-control:8790` |
| `FINANCIAL_WORKSPACE_HANDOFF_COOKIE_DOMAIN` | optional `Domain` for the handoff cookie (host-only when unset) |

### Worker seats (app `fin-terminal-public-seat-01..06`)

| Variable | Purpose |
|---|---|
| `TERMINAL_RUNTIME_FEATURE_ENABLED` / `TERMINAL_RUNTIME_MANAGEMENT_TOKEN` | match gateway/reconciler |
| `TERMINAL_RUNTIME_MANAGEMENT_URL` | `http://fin-terminal-public-gateway:8789` (private seat network) |

### Reconciler (infra host service)

`TERMINAL_RUNTIME_FEATURE_ENABLED`, `TERMINAL_RUNTIME_MANAGEMENT_TOKEN`,
`TERMINAL_RUNTIME_MANAGEMENT_PORT` (default `8789`),
`TERMINAL_RUNTIME_COMPOSE_PROJECT`, `TERMINAL_RUNTIME_COMPOSE_DIR`, interval and
resource-guard thresholds. See `.env.reconciler.example`.

## 6. Wire-unit cheat sheet

| Value | Unit |
|---|---|
| checkpoint `expires_at` (S2S wire) | Unix epoch **seconds** |
| gateway→browser `expiresAt`, checkpoint `createdAt`/`expiresAt`, `CheckpointEvent.at` | Unix epoch **ms** |
| Express cookie `maxAge` | **ms** |
| `idleSeconds` (management v1) | seconds (integer) |
| `TERMINAL_RUNTIME_IDLE_SCALE_DOWN` | seconds |
| permit/claim TTLs | ms (app), seconds (control plane) |
