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

## Supported surfaces

| Surface | Position |
|---|---|
| **Claude** custom connector | Supported OAuth path; exact callback allowlisted |
| **Gemini CLI** | Supported bearer header (`gemini mcp add … --header`); no OAuth |
| **Gemini web** (`gemini.google.com`) | Assisted preview; add via Settings → Connected apps in a normal browser |
| **Gemini mobile** | No setup promise; use a connection created on the web if Google offers it |
| **Gemini desktop** | **Embedded setup is not supported.** Its webview has no WebAuthn, so passkey accounts fail at Google's own sign-in before `/authorize` is ever reached |
| **ChatGPT** | Not enabled; its callback is not in the policy table until verified by a real connection |
| **Gemini Enterprise** | Assisted manual client (`scripts/register_mcp_oauth_client.py`) |

Google's own help page documents custom apps as added in the web app and
available in web and mobile. Do not promise desktop inheritance; the desktop
app may present the same settings surface, but that flow is not documented and
is where the embedded-webview failure occurs.

Consumer custom apps are also restricted by Google to personal Google accounts,
US, 18+, English, with History enabled. Link those restrictions rather than
promising general availability.

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
- **Refresh rotation with replay detection.** Rotation consumes the old token
  and issues its successor inside one transaction. A rotated token presented
  again by its own client revokes the whole family, including live access
  tokens. There is deliberately **no grace window**: tolerating a re-submitted
  token would fork the family and let a thief keep rotating independently.
- **Bounded connection lifetime.** A family expires 90 days after it was
  created, regardless of rotation, while each refresh token still has a sliding
  30-day lifetime. Rotation cannot renew a connection forever.
- **One grant, one browser.** The consent screen pins the exact `agent_id`.
  Every tool call re-checks the pin; another profile of the same key is a
  different browser and is refused.
- **Provisioned Chromes are out of scope.** A connector cannot list, launch or
  target `prov-` tabs; a middleware applies that to every tool call, so a new
  tool is covered without another call-site check.
- **Key revocation is immediate.** Transport-level token verification and each
  tool call both confirm the grant's backing API key is still active, so
  revoking a key cuts connector access at once rather than waiting out the
  15-minute access token.
- **Account rejection is immediate and total.** Rejecting a user deactivates
  every key and revokes every connector grant in one transaction; the admin
  action also detaches grants explicitly. Re-approval issues a *fresh* key and
  never resurrects the old one or its grants.
- **OAuth requires an approved account.** API-key callers are unchanged, so
  pending trial access keeps working exactly as before.
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

Removing a policy takes effect **immediately for admission**: an automatic
client whose stored callbacks no longer match is refused at `get_client`, so
authorization, token and consent operations stop working for it. `revalidate`
reports those clients, and `revalidate --apply` additionally revokes their
grants and tokens, discards pending requests and deletes the client rows in one
transaction. Running it is cleanup; not running it leaves unusable clients
behind.

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

## Account lifecycle

| Event | Effect |
|---|---|
| Admin rejects a user | Status set, **all keys deactivated**, grants revoked — atomically |
| User re-approved | Status set, a **new** key issued if the stored one is inactive |
| Key revoked | Its grants are revoked on the next OAuth call (transport + tool layer) |
| Grant disconnected at `/oauth/connections` | Grants, tokens and token families revoked immediately |
| Rejected/pending account holds a token | OAuth refused; API-key behaviour unchanged |

Key creation is refused for a rejected account, inside the same write
transaction as the insert, so a rejection cannot race a fresh key into
existence.

## Maintenance

The MCP server runs a prune task inside FastMCP's lifespan (every 10 minutes,
off the event loop). It removes expired authorization requests, approvals and
codes, expired access tokens, and token families that have been expired or
revoked for more than 7 days. Consumed refresh ancestors are kept while their
family is alive — that is what makes replay detectable.

## Deliberately not built

- JWTs, JWKS or distributed signing.
- Client ID Metadata Documents, OIDC, password or client-credentials grants.
- Confidential clients and client secrets.
- Fine-grained scopes (one honest scope, `mcp:browser`, backed by the full tool
  set).
- Multi-agent grants or automatic failover to another connected browser.

## Known interop details

- Source-IP rate limiting on `/authorize` is not implemented yet; it needs
  trusted-proxy handling in the MCP service, which currently has none. The
  per-client and global outstanding-request ceilings are the current bound.
- The MCP SDK's `RevocationRequest` requires a `client_secret` field in the form
  even for public clients; clients that omit it get `400 invalid_request` from
  `/revoke`. Revoking from `/oauth/connections` is unaffected.
- The 401 challenge includes `error="invalid_token"` alongside
  `resource_metadata`. Discovery works, but if a connector is strict about a
  bare challenge this is the first thing to adjust.
- **Google Persistent Linking may require a stable refresh token.** Google's
  Connected Apps guide describes returning the same refresh token rather than
  rotating it. That conflicts with rotation here and is untested against a real
  Gemini connection. Do not weaken rotation globally to satisfy an assumption;
  measure it during the assisted Gemini-web test.
- **A placeholder secret is not confidential-client authentication.** Manual and
  Enterprise clients are registered as public (`token_endpoint_auth_method=none`),
  and the secret field is ignored. Verify the console actually works that way
  before describing it as supported.
- **Embedded webviews cannot do WebAuthn.** Any client that signs the user in
  through an in-app browser will fail for passkey-only Google accounts before
  reaching this server.
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
python test_auth_lifecycle.py      # rejection, re-approval, key creation
python test_mcp_oauth_gate.py      # the master switch gates credentials
python test_mcp_server.py          # existing tool behaviour unchanged
```
