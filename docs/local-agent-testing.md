# Local Agent Testing

Use this setup to keep the web UI, chat client, browser relay, and controlled
Chrome on one development machine. No test traffic should reach production.

## Topology

```text
Browser UI :8080
    -> local web server -> local chat client -> Claude, Codex, or OpenCode CLI
                        -> local relay :8765 -> Chrome bridge -> dev Chrome :9223
```

For an Agent View test, `dev.sh agent-view` starts the entire local stack with
one generated development API key. The basic `dev.sh` command still starts only
the web server and relay for UI work.

For a Hosted Trial test, `dev.sh hosted-trial` starts the relay, private core,
web, Chrome bridge, hosted OpenRouter trial worker, and scheduler daemon —
using locally-generated service tokens and the real OpenRouter API.

## 1. Start the local servers

From the repository root:

```bash
./dev.sh agent-view
```

The defaults are:

- web UI: `http://localhost:8080`
- relay tunnel: `ws://127.0.0.1:8765/tunnel`
- private core: `http://127.0.0.1:8770`
- controlled Chrome CDP: `http://127.0.0.1:9223`
- logs and local credentials: `/tmp/unchained-dev/`

Use `RELAY_PORT` and `WEB_PORT` to choose different ports:

```bash
RELAY_PORT=9765 WEB_PORT=9080 PRIVATE_CORE_PORT=9770 BRIDGE_PORT=9224 \
  ./dev.sh agent-view
```

`agent-view` expects the private core checkout at
`../unchained-core-private/unchained`. Set `PRIVATE_CORE_DIR` when it lives
elsewhere. It runs the private core from that checkout without overlaying or
modifying public-repo stubs.

## 2. Hosted Trial (OpenRouter worker)

`hosted-trial` starts the same relay, private core, web, and Chrome bridge
as `agent-view`, but replaces the local CLI chat agent with a hosted
OpenRouter trial worker. This exercises the full credit accounting, turn
admission, hosted-model-allowlist paths, and scheduled task daemon.

**Prerequisites:**

- `OPENROUTER_API_KEY` must be exported in the environment before running.
- The private core checkout must be at `../unchained-core-private/unchained`
  (set `PRIVATE_CORE_DIR` if elsewhere).
- Hosted local mode uses the fixed development identity `dev@localhost` so the
  browser Dev Login and connector share one account.

```bash
export OPENROUTER_API_KEY=sk-or-...
./dev.sh hosted-trial
```

To verify the complete local control plane without loading a real provider key
or sending a model request, run:

```bash
./dev.sh smoke-hosted-trial
```

The smoke mode uses isolated alternate ports and temporary credentials, checks
all six service processes plus hosted-worker/browser readiness, then stops.

Defaults for hosted trial:

- web UI: `http://localhost:8080`
- trial chat: `http://localhost:8080/trial`
- UI model selector controls the submitted model
- `OPENROUTER_MODEL` env only controls the trial worker's fallback model
- trial agent ID: `trial-local` (override with `TRIAL_AGENT_ID`)
- logs, credentials, and all data dirs: `/tmp/unchained-dev/`

The script generates and persists these distinct local-only tokens under
`/tmp/unchained-dev/` with mode 600:

| File | Role |
|------|------|
| `trial-agent-key` | WebSocket auth key for the trial worker |
| `hosted-agent-service-token` | Bearer token for credit callback auth |
| `api-key` | Dev user API key (for bridge/relay) |

The script verifies all tokens are distinct and nonempty before starting.
Neither token is ever printed.

**Credit grant for the dev user:**

The trial worker requires a credit balance to make billing runs. Grant trial
credit to the `dev@localhost` user via the admin panel (recommended) or the
authenticated admin API. The admin grant endpoint requires an authenticated
admin session.

**Option A — Admin panel (browser, recommended):**

1. Open `http://localhost:8080/admin` in a browser.
2. Log in with Dev Login (`dev@localhost` is admin by default).
3. Use the "Credit" panel to grant credit to yourself.

**Option B — curl with cookie jar:**

```bash
# Login and save session cookie
curl -c /tmp/unchained-dev/cookies.txt -X POST http://localhost:8080/auth/dev \
  -H 'Content-Type: application/json' \
  -d '{"email":"dev@localhost"}'

# Get your user ID
USER_ID=$(curl -sb /tmp/unchained-dev/cookies.txt http://localhost:8080/auth/me \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["user_id"])')

# Grant credit (requires operation_id for idempotency)
curl -b /tmp/unchained-dev/cookies.txt \
  -X POST http://localhost:8080/admin/credit/grant \
  -H 'Content-Type: application/json' \
  -d "{\"user_id\":\"$USER_ID\",\"amount_usd\":\"1.00\",\"operation_id\":\"dev-grant-001\"}"
```

The `/admin/credit/grant` endpoint requires an authenticated admin cookie/session
and a unique `operation_id` for idempotent safe retry.

**Browser verification:**

1. Open `http://localhost:8080/trial` in a browser.
2. Select **Dev Login** and enter `dev@localhost`.
3. Type a simple prompt like "go to example.com and tell me the title".
4. Observe the agent using OpenRouter and the controlled `dev` Chrome profile.
5. Check trial worker logs: `tail -f /tmp/unchained-dev/trial-agent.log`

**Note:** Real OpenRouter calls may incur spend. To use a free model for an
interactive turn, select that free model in the trial UI. The
`OPENROUTER_MODEL` environment variable controls only the worker fallback and
scheduler default; it does not override the model submitted by the UI.

## 3. Create the browser session

Open the local UI and select **Dev Login**, or use:

```bash
curl -X POST http://localhost:8080/auth/dev \
  -H 'Content-Type: application/json' \
  -d '{"email":"dev@localhost"}'
```

The script creates `dev@localhost` before starting services and stores its API
key in `/tmp/unchained-dev/api-key` with mode 600. It passes that same key to
the relay-facing private core, web server, browser bridge, and chat client. Do
not commit or publish this value.

Read the key only when running an additional manual command:

```bash
export UNCHAINED_API_KEY="$(cat /tmp/unchained-dev/api-key)"
```

Do not commit, paste into documentation, or otherwise publish this value.

## 4. Use Agent View

Open the UI in your normal browser, not in the controlled `dev` Chrome window:

```text
http://localhost:8080/local?provider=opencode-cli
```

Select **Dev Login**, ask the agent to navigate, and open **Browser Preview**.
Keeping the controller UI outside the controlled Chrome prevents Agent View
from selecting and recursively mirroring its own UI tab. The adaptive Agent
Task Shell is the default; add `&shell=legacy` only for the old layout.

## 5. Verify the connection

```bash
curl http://127.0.0.1:8765/api/agents \
  -H "Authorization: Bearer $(cat /tmp/unchained-dev/api-key)"
```

The response should include the named `dev` browser profile. The local UI
should separately report that both the browser and chat client are ready.

For hosted trial, verify the trial worker is connected:

```bash
curl http://localhost:8080/web/chat/status?model=google/gemini-3.1-flash-lite \
  -H "Authorization: Bearer $(cat /tmp/unchained-dev/api-key)"
```

The response should show `"chat_connected": true` and `"chat_agent_id"` set to
the trial agent ID (`trial-local`).

If the UI remains offline:

1. Confirm both processes use the API key stored for `dev@localhost`.
2. Confirm every client URL points to `127.0.0.1`, not the production host.
3. Confirm the web and relay ports match any `WEB_PORT` or `RELAY_PORT`
   overrides used with `dev.sh`.
4. Check `/tmp/unchained-dev/web.log`, `/tmp/unchained-dev/relay.log`, and the
   client terminal output.

## Stop the local stack

```bash
./dev.sh stop
```

This stops the relay, web server, private core, browser bridge, chat agent,
trial worker, and scheduler daemon (whichever were started). The generated
JWT secret, development API key, trial agent key, and hosted service token
stay under `/tmp/unchained-dev` so restarts do not invalidate the browser
session or change the relay identity. Remove that directory to reset all
local credentials.
