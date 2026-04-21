# Spec: External Auth Redirect for searchagentsky.com

## Context

searchagentsky.com (sky-search) now has shareable result pages — after an agent run, users can click "Share" to publish a permanent link at `/r/:id`. Publishing requires authentication, but sky-search has **zero auth infrastructure** — no Google OAuth, no user DB, no session management.

**unchainedsky.com is the auth provider.** When a sky-search user wants to share, they're redirected here to log in, then sent back with a signed JWT.

This is already deployed on sky-search (protostatis/sky-search#12). The Share button redirects to:

```
https://unchainedsky.com/auth/login?redirect_uri=https://searchagentsky.com/auth/callback&scope=share
```

That endpoint currently 404s. This spec describes what to build.

## What to build

### New endpoint: `GET /auth/login`

Query params:
- `redirect_uri` (required) — where to redirect after login. Must match an allowlist.
- `scope` (optional) — currently just `share`, reserved for future use.

**Behavior:**

1. **Validate `redirect_uri`** against an allowlist:
   - `https://searchagentsky.com/auth/callback`
   - `https://search.unchainedsky.com/auth/callback`
   - In dev: `http://localhost:3000/auth/callback`
   - Reject all others with 400.

2. **If the user is already logged in** (has a valid session):
   - Mint a JWT containing `{ sub, name, email, picture }` from the user record
   - Sign it with `JWT_SECRET` (shared with sky-search)
   - Redirect immediately to `{redirect_uri}?token={jwt}`

3. **If the user is NOT logged in**:
   - Store `redirect_uri` and `scope` in the session (or a short-lived cookie)
   - Proceed with the normal Google Sign-In flow
   - After successful login, mint the JWT and redirect to `{redirect_uri}?token={jwt}`

### JWT format

```json
{
  "sub": "google-user-id-12345",
  "name": "John Doe",
  "email": "john@example.com",
  "picture": "https://lh3.googleusercontent.com/...",
  "iat": 1776802484,
  "exp": 1776888884
}
```

- Signed with `JWT_SECRET` (HS256)
- TTL: 24 hours
- `sub` = the Google `sub` claim (stable user identifier)
- Sky-search verifies with the same `JWT_SECRET` — no network call back to unchainedsky.com

### Env var

`JWT_SECRET` must be set on the unchainedsky.com server. The same value is already set on sky-search (35.153.83.133).

Current production value on sky-search — retrieve it with:
```
ssh -i ~/.ssh/unchained-key.pem ec2-user@35.153.83.133 "grep JWT_SECRET /opt/sky-search/server/.env"
```

Set the same value on the unchainedsky.com server.

## What NOT to build

- No changes to the existing login UI — reuse the current Google OAuth flow
- No new user table or schema — use existing user records
- No API endpoints beyond the redirect — sky-search doesn't call back after getting the JWT
- No changes to sky-search — it's already deployed and waiting for this endpoint

## Security considerations

- **Allowlisted redirect URIs only** — prevents open redirect attacks
- **Short JWT TTL (24h)** — limits exposure if a token leaks
- **HTTPS only** in production redirect URIs
- **`scope=share`** is informational for now but could gate JWT claims in the future

## Test plan

1. Visit `https://unchainedsky.com/auth/login?redirect_uri=https://searchagentsky.com/auth/callback&scope=share`
2. If not logged in → Google Sign-In → redirect to `searchagentsky.com/auth/callback?token=...`
3. If already logged in → immediate redirect with token
4. Token should be verifiable: `jwt.verify(token, JWT_SECRET)` returns `{ sub, name, email, picture }`
5. Bad `redirect_uri` → 400 error
6. Full flow: searchagentsky.com → run query → click Share → redirected to unchainedsky.com → login → redirected back → result published → link copied
