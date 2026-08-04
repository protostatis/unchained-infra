# Financial Terminal ↔ Workspace Control Plane — Canonical Cross-Repo Contract

**This is the single canonical contract.** Both repositories implement exactly
this wire schema, units, headers, cookie names/scopes, env vars, and paths:

- App: [`protostatis/unbrowser-fin-terminal`](https://github.com/protostatis/unbrowser-fin-terminal)
- Infra: [`protostatis/unchained-infra`](https://github.com/protostatis/unchained-infra)
  (this repository)

Any change to a value below must update this document and both implementations
in the same change. Version bumps (`v1`, wire `expires_at` seconds) are
intentional and additive-only until a new major version is cut.

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
  "auth_url": "https://unbrowser.unchainedsky.com/fin-terminal-workspace/auth/claim?handoff_id=fh-...",
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
| `fin-terminal-handoff-secret` | app gateway (`workspace-checkpoint-control.ts`) | infra control plane, server-side at `POST /api/claim` | `HttpOnly; Secure; SameSite=Lax; Path=/`; **host-only** (no `Domain`). Optional `Domain` only via `FINANCIAL_WORKSPACE_HANDOFF_COOKIE_DOMAIN` if the surfaces are ever split across subdomains. |
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
- `idleSeconds` is `idleSinceMs / 1000` (integer seconds). It is populated for
  `active` seats from the session's last-activity time; seats without a
  tracked idle time (e.g. `ready-idle`) report `0`. The reconciler uses it only
  to order scale-down candidates, so a zero value is safe (drain-then-stop and
  generation CAS still protect every seat).
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
| `GET /fin-terminal-workspace/auth/claim?handoff_id=...` | `GET /auth/claim` | `handle_fin_workspace_auth_claim_page` |
| `POST /fin-terminal-workspace/api/claim` | `POST /api/claim` | `handle_fin_workspace_browser_claim` |
| `GET /fin-terminal-workspace/api/claims/{claim_id}` | `GET /api/claims/{claim_id}` | `handle_fin_workspace_browser_get_claim` |
| `GET /fin-terminal-workspace/api/workspace` | `GET /api/workspace` | `handle_fin_workspace_browser_get_workspace` |
| `GET /fin-terminal-workspace/api/snapshots` | `GET /api/snapshots` | `handle_fin_workspace_browser_get_snapshots` |
| `GET /fin-terminal-workspace/api/runtime/status` | `GET /api/runtime/status` | `handle_fin_workspace_browser_runtime_status` |
| `POST /fin-terminal-workspace/api/google` | `POST /api/google` | `handle_claim_google_token` |
| `GET /fin-terminal-workspace/auth/{provider}/start?claim_id=...` | `GET /auth/{provider}/start` | `handle_claim_oauth_start` |
| `GET /fin-terminal-workspace/auth/{provider}/callback` | `GET /auth/{provider}/callback` | `handle_claim_oauth_callback` |
| `GET /fin-terminal-workspace/done?claim_id=...&status=...` | `GET /done` | `handle_claim_done` |

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

Worker-side private route (app, never public):

| Route | Purpose |
|---|---|
| `POST /internal/financial-workspace/checkpoint-export` | worker exports authoritative checkpoint for the exact `{sessionId, generation}`; headers `X-Fin-Terminal-Control-Token` + worker proxy token |

## 5. Environment variables

### Control plane (infra `fin-terminal-workspace-control`)

| Variable | Purpose | Default |
|---|---|---|
| `FIN_TERMINAL_WORKSPACE_ENABLED` | master flag (Caddy + control plane + gateway) | `false` |
| `FIN_WORKSPACE_ENABLED` | app-level mirror of the master flag | derived |
| `FIN_WORKSPACE_CONTROL_TOKEN` | S2S bearer for `/internal/*` (32+ chars) | required when enabled |
| `FIN_WORKSPACE_COOKIE_DOMAIN` | parent domain for `fw_claim_secret`/`fw_claim_nonce` | required when enabled |
| `FIN_WORKSPACE_S3_BUCKET` / `_REGION` / `KMS_KEY_ID` | envelope-encrypted checkpoint storage | required when enabled |
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
