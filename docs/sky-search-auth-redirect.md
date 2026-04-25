# External Auth Redirect — Unchained Provider Contract

## Context

sky-search (searchagentsky.com / search.unchainedsky.com) has shareable result
pages. Publishing requires authentication, but sky-search has no auth
infrastructure. unchainedsky.com is the auth provider.

This document describes the provider-side contract. The matching client-side
spec lives in the sky-search repo under `docs/AUTH_FLOW.md`.

## Endpoints

### `GET /auth/login`

Query params:
- `redirect_uri` (required) — where to send the user after login. Must match the allowlist.
- `scope` (optional) — `share` for the publish flow; reserved for future use.
- `state` (optional) — opaque value from the client; echoed back unchanged.

**Behavior:**

1. Validate `redirect_uri` against the allowlist (see below). Return 400 if invalid.
2. If the user is already logged in:
   - Generate a random 64-hex-character one-time code (`secrets.token_hex(32)`).
   - Persist it in `auth_codes` table with a 120-second TTL, bound to `redirect_uri`.
   - Redirect to `{redirect_uri}?code={code}[&state={state}]`.
3. If the user is not logged in:
   - Serve a minimal Google Sign-In page.
   - After sign-in, the page reloads (preserving all query params).
   - The handler runs again with a session cookie and issues the code.

### `POST /auth/token`

Exchanges a one-time code for an identity JWT. Called server-side from the
client's `/auth/callback` handler.

Request body (JSON):
```json
{
  "grant_type": "authorization_code",
  "code": "<one_time_code>",
  "redirect_uri": "<exact_redirect_uri_used_at_login>"
}
```

Success response:
```json
{
  "access_token": "<jwt>",
  "token_type": "Bearer",
  "expires_in": 86400,
  "scope": "share"
}
```

Error responses follow OAuth 2.0 error format:
```json
{ "error": "invalid_grant", "error_description": "Code expired." }
```

Possible `error` values: `invalid_request`, `unsupported_grant_type`,
`invalid_grant`.

## JWT format

```json
{
  "sub": "<user_id>",
  "name": "Jane Doe",
  "email": "jane@example.com",
  "picture": "https://lh3.googleusercontent.com/...",
  "aud": "sky-search",
  "iat": 1776802484,
  "exp": 1776888884
}
```

- Signed with `JWT_SECRET` (HS256), shared with sky-search.
- TTL: 24 hours.
- `aud` claim: `"sky-search"` — clients must verify this.

## Redirect URI allowlist

Production:
- `https://searchagentsky.com/auth/callback`
- `https://search.unchainedsky.com/auth/callback`

Development (only when the provider runs without `GOOGLE_CLIENT_ID`):
- `http://localhost:3000/auth/callback`
- `http://127.0.0.1:3000/auth/callback`

A production-hosted provider always rejects localhost redirect URIs.

## Security invariants

- One-time codes expire after 120 seconds.
- Each code is single-use; replay returns `invalid_grant`.
- Codes are bound to the exact `redirect_uri` used at login time.
- Bearer tokens never appear in redirect URLs.
- Localhost redirect URIs are rejected in production.

## Test plan

1. Visit `/auth/login?redirect_uri=https://searchagentsky.com/auth/callback&scope=share&state=abc`
2. Not logged in → Google Sign-In → redirect to `/auth/callback?code=...&state=abc`
3. Already logged in → immediate redirect with code
4. `POST /auth/token` with the code → returns `access_token`
5. Bad `redirect_uri` → 400
6. Replay the same code → `invalid_grant`
7. Wait >120 s then exchange → `Code expired.`
