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

## 2. Create the browser session

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

## 3. Use Agent View

Open the UI in your normal browser, not in the controlled `dev` Chrome window:

```text
http://localhost:8080/local?provider=opencode-cli
```

Select **Dev Login**, ask the agent to navigate, and open **Browser Preview**.
Keeping the controller UI outside the controlled Chrome prevents Agent View
from selecting and recursively mirroring its own UI tab. The adaptive Agent
Task Shell is the default; add `&shell=legacy` only for the old layout.

## 4. Verify the connection

```bash
curl http://127.0.0.1:8765/api/agents \
  -H "Authorization: Bearer $(cat /tmp/unchained-dev/api-key)"
```

The response should include the named `dev` browser profile. The local UI
should separately report that both the browser and chat client are ready.

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

This stops the relay, web server, private core, browser bridge, and chat agent
started by `agent-view`. The generated JWT secret and development API key stay
under `/tmp/unchained-dev` so restarts do not invalidate the browser session or
change the relay identity. Remove that directory to reset all local credentials.
