# OAuth for remote MCP connectors

## Why this exists

`POST https://unchainedsky.com/mcp` has always authenticated with a static
header: `Authorization: Bearer <api_key>`. Local clients (Claude Code, Codex,
Cursor, OpenCode) can send that header. **Remote MCP connectors cannot.**

| Surface | Credential the UI accepts | Header field? |
|---|---|---|
| Claude (web/desktop), custom connector | OAuth 2.1, discovery from a 401 | No |
| ChatGPT, custom MCP server | OAuth | No |
| Gemini Apps, "Add a custom app" | OAuth 2.1 (DCR, or client id/secret under *Advanced features*) | No |
| Gemini Enterprise, custom MCP data store | "OAuth 2.0" or "No authentication" | No |
| Gemini CLI | `--header "Authorization: Bearer ..."` | Yes |

Google's [Connected Apps OAuth 2.1 guide](https://developers.google.com/connected-apps/mcp/oauth)
requires PKCE, Authorization Server Metadata and Dynamic Client Registration
(or a pre-registered client id/secret). There is no option anywhere in those
consoles for an arbitrary header or a static bearer token, so the endpoint has
to become an authorization server.

## Shape of the solution

The MCP server owns the protocol; the website owns the login. Both already
mount the same SQLite file (`relay_data:/data/auth.db`), so they share state
without a second auth mechanism.

```
connector                     unchainedsky.com
    |                               |
    |-- POST /mcp (no token) -----> | 401 + WWW-Authenticate: resource_metadata=...
    |-- GET /.well-known/oauth-protected-resource/mcp
    |-- GET /.well-known/oauth-authorization-server
    |-- POST /register  (optional, DCR)
    |-- GET /authorize?code_challenge=...&resource=https://unchainedsky.com/mcp
    |                               | 302 -> /oauth/consent?request=<handle>
    |                               |    (web: session login, agent picker)
    |                               | 302 -> /oauth/complete?ticket=<ticket>   (MCP)
    |<-- 302 redirect_uri?code=...  |
    |-- POST /token (code + verifier) -> access + refresh
    |-- POST /mcp  Authorization: Bearer uco_at_...  ✔
```

Key properties:

- **Public clients only.** `token_endpoint_auth_method=none`, PKCE S256
  mandatory. There is no client secret to leak or hash-compare, and no
  confidential-client support to get wrong.
- **Opaque tokens, hashed at rest.** Access (`uco_at_`), refresh (`uco_rt_`) and
  codes (`uco_ac_`) are random 256-bit values; only SHA-256 hashes are stored.
  Revocation is a row update, not a JWT blacklist.
- **Refresh rotation with replay detection.** A rotated refresh token presented
  again revokes the whole token family, including live access tokens.
- **One grant, one browser.** The consent screen pins the exact `agent_id`.
  Every tool call re-checks the pin; another profile of the same key is a
  different browser and is refused.
- **Provisioned Chromes are out of scope.** A connector cannot list, launch or
  target `prov-` tabs; a middleware applies that to every tool call, so a new
  tool is covered without another call-site check.
- **Key revocation is immediate.** Each OAuth call confirms the grant's backing
  API key is still active, so revoking a key cuts connector access at once
  rather than waiting out the 15-minute access token.
- **No bearer upgrade.** Only a real browser session can approve; an API key
  sent to `/oauth/consent` cannot mint a grant.
- **The master switch gates credentials, not just routes.** With
  `MCP_OAUTH_ENABLED=0`, previously issued `uco_at_` tokens are refused by the
  tool layer as well.
- **Legacy keys unchanged.** `load_access_token` accepts both `uco_at_` tokens
  and existing `uc_...` API keys, so nothing changes for current MCP users.

### Why the agent pin matters

`agent_id` is not free-form: it is `claude-<sha256(api_key)[:8]>` (plus an
optional profile suffix), derived identically by the relay, the local agent and
the MCP server. An OAuth token carries a *user*, not a key. Resolving it to
"any agent this user has" would let a connector silently drive a browser the
user never approved, so consent binds the token to one exact key fingerprint and
agent id, and a revoked key invalidates the grant.

## Configuration

| Variable | Service | Default | Meaning |
|---|---|---|---|
| `MCP_OAUTH_ENABLED` | mcp, web | `0` | Master switch. Must match in both or consent 404s. |
| `MCP_OAUTH_ISSUER` | mcp, web | `https://unchainedsky.com` | Canonical issuer; never derived from request headers. |
| `MCP_OAUTH_RESOURCE` | mcp | `<issuer>/mcp` | RFC 8707 audience. `/authorize` rejects any other resource. |
| `MCP_OAUTH_DCR_ENABLED` | mcp | `0` | Open registration. Leave off until manual clients are verified. |
| `MCP_OAUTH_DCR_MAX_PER_HOUR` | mcp | `60` | Registration budget per rolling hour. |
| `MCP_OAUTH_DCR_MAX_TOTAL` | mcp | `5000` | Absolute client cap. |
| `MCP_OAUTH_MAX_PENDING_PER_CLIENT` | mcp | `20` | Outstanding `/authorize` requests per client. |
| `MCP_OAUTH_MAX_PENDING_GLOBAL` | mcp | `5000` | Outstanding `/authorize` requests overall. |

Caddy routes `/authorize`, `/token`, `/register`, `/revoke`, `/oauth/complete`
and the `/.well-known/oauth-*` documents to `mcp:8766`. `/oauth/consent` and
`/oauth/connections` stay on `web:8080` through the catch-all.

`GET /mcp` keeps serving the install page to browser navigations (matcher
requires `Accept: text/html`), while a connector's unauthenticated
`GET /mcp` with `Accept: application/json` now reaches the transport and gets
the 401 challenge it needs. `/mcp/setup` is a stable alias for the install page.

## Callback admission

A callback URL is where the authorization code lands, so automatic
registration only accepts destinations an operator has approved.  Policies live
in `mcp_oauth_callback_policies` and name an exact scheme, host, port and path
(optionally a path prefix) plus whether a query string is allowed.  Two are
seeded — Claude's and Gemini Enterprise's — and the seed is versioned, so a
policy an operator removes stays removed.

```bash
uv run scripts/manage_mcp_oauth_policy.py list
uv run scripts/manage_mcp_oauth_policy.py add \
    --host chatgpt.com --path /connector_platform_oauth_redirect \
    --note "ChatGPT custom MCP server"
```

Removing a policy does not silently take effect: automatic clients whose stored
callbacks no longer match are reported by `revalidate`, and only withdrawn with
`--apply`, which revokes their grants and discards their pending requests in one
transaction.

Manual clients (registered by an operator) skip admission but never syntax
validation: https only, no userinfo, no fragments, no issuer-host callbacks, and
loopback allowed only for native clients an operator registered.

## Operating it

1. Deploy with `MCP_OAUTH_ENABLED=1` and `MCP_OAUTH_DCR_ENABLED=0`.
2. Register a client for each connector that needs one:

   ```bash
   uv run scripts/register_mcp_oauth_client.py \
       --client-id gemini-enterprise \
       --name "Gemini Enterprise" \
       --redirect-uri https://vertexaisearch.cloud.google.com/oauth-redirect
   ```

   The client is public, so the console's "client secret" field accepts any
   placeholder.
3. Paste `https://unchainedsky.com/mcp` into the connector, sign in when the
   consent page appears, and pick the browser it may drive.
4. Only after the manual path is verified **and** the connector's callback is in
   the policy table, turn on `MCP_OAUTH_DCR_ENABLED=1` so Claude/ChatGPT/Gemini
   Apps can register themselves. A registration from an unlisted callback is
   refused with an actionable error rather than parked for approval: DCR has no
   "wait for an administrator" step, and the authorization request expires long
   before a human would review it.
5. Users can revoke from `/oauth/connections`, and a key revocation in the
   admin UI invalidates every connector bound to it.

## Security notes

- Redirect URIs are matched exactly against what was registered; error
  redirects use the same validated URI. `https` only, or `http` on loopback.
- The authorization request is an immutable row; approval is a single
  conditional `UPDATE`, so two tabs cannot both succeed.
- CSRF on the consent POST is a signed-in session plus a double-submit cookie
  scoped to `/oauth`, SameSite=Lax.
- The consent page shows the full callback URI and its host, labels the client
  as operator-registered or policy-matched, and refuses to render an Allow form
  when the callback is no longer admitted.
- Completion and token issuance re-check admission, because a policy can be
  withdrawn between approval and redirect; the failure target is our own error
  page, never the retired callback.
- Consent, completion and connection pages send `Cache-Control: no-store` and
  `Referrer-Policy: no-referrer`.
- Client-supplied names are HTML-escaped and marked unverified; nothing fetched
  from a client is rendered.
- Browser provisioning (`cdp_provision_launch`) is denied for OAuth callers: a
  new Chrome from another profile would widen what consent promised.

## Deliberately not built

- JWTs, JWKS or distributed signing.
- Client ID Metadata Documents, OIDC, password or client-credentials grants.
- Confidential clients and client secrets.
- Fine-grained scopes (one honest scope, `mcp:browser`, backed by the full tool
  set).
- Multi-agent grants or automatic failover to another connected browser.

## Known interop details

- The MCP SDK's `RevocationRequest` requires a `client_secret` field in the form
  even for public clients; clients that omit it get `400 invalid_request` from
  `/revoke`. Revoking from `/oauth/connections` is unaffected.
- The 401 challenge includes `error="invalid_token"` alongside
  `resource_metadata`. Discovery works, but if a connector is strict about a
  bare challenge this is the first thing to adjust.
- The SDK requires `client_id` in the token request body. That matches RFC 6749
  §3.2.1 for public clients, so Basic-only client authentication is out of
  scope by design.

## Tests

```bash
cd unchained
python test_mcp_oauth_store.py     # storage, atomicity, rotation, replay
python test_mcp_oauth_flow.py      # discovery -> consent -> code -> token -> /mcp
python test_mcp_oauth_policy.py    # URI validation, policy matching, labels
python test_mcp_oauth_consent.py   # the handler a browser actually hits
python test_mcp_identity.py        # credential -> principal -> pinned agent
python test_mcp_oauth_gate.py      # the master switch gates credentials
python test_mcp_server.py          # existing tool behaviour unchanged
```
