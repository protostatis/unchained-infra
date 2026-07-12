# Local Agent Testing

Use this setup to keep the web UI, chat client, browser relay, and controlled
Chrome on one development machine. No test traffic should reach production.

## Topology

```text
Browser UI :8080
    -> local web server -> local chat client -> Claude, Codex, or OpenCode CLI
                       -> local relay :8765 -> Chrome bridge -> guest Chrome :9223
```

`dev.sh` starts only the web server and relay. Start the Chrome bridge and chat
client separately.

## 1. Start the local servers

From the repository root:

```bash
./dev.sh
```

The defaults are:

- web UI: `http://localhost:8080`
- relay tunnel: `ws://127.0.0.1:8765/tunnel`
- logs: `/tmp/unchained-dev/web.log` and `/tmp/unchained-dev/relay.log`

Use `RELAY_PORT` and `WEB_PORT` to choose different ports:

```bash
RELAY_PORT=9765 WEB_PORT=9080 ./dev.sh
```

## 2. Create the dev-auth session

Open the local UI and select **Dev Login**, or use:

```bash
curl -X POST http://localhost:8080/auth/dev \
  -H 'Content-Type: application/json' \
  -d '{"email":"dev@localhost"}'
```

The browser session, chat client, and Chrome bridge must resolve to the same
stored API key. A mismatched key produces a different `agent_id`, so the UI
will report that the local agent is offline.

Read the key after creating the dev-auth session:

```bash
cd unchained
uv run python - <<'PY'
from auth import Auth

user = Auth().find_user_by_email("dev@localhost")
if not user:
    raise SystemExit("Log in through /auth/dev first")
print(user["api_key"])
PY
```

Do not commit, paste into documentation, or otherwise publish this value.

## 3. Start the local Chrome bridge

In a separate terminal, from `unchained-infra/unchained`:

```bash
uv run python chrome_bridge.py start \
  --relay ws://127.0.0.1:8765/tunnel \
  --key <local-api-key> \
  --no-headless \
  --profile guest \
  --port 9223
```

The named `guest` profile keeps the controlled target distinct from any Chrome
window used to observe the test UI.

## 4. Start the local chat client

In another terminal, from `unchained-infra/unchained`:

```bash
UNCHAINED_API_KEY=<local-api-key> \
UNCHAINED_SERVER=ws://127.0.0.1:8080/chat/ws \
UNCHAINED_RELAY_HOST=127.0.0.1 \
UNCHAINED_RELAY_PORT=8765 \
PYTHONUNBUFFERED=1 \
uv run python chat_agent_cli.py
```

The model selected in the local UI chooses the corresponding installed CLI.
For example, open:

```text
http://localhost:8080/local?provider=opencode-cli
```

The adaptive Agent Task Shell is the default. Add `&shell=legacy` when a test
specifically needs the previous chat-first layout.

`chat_agent_cli.py` defaults to the production server when
`UNCHAINED_SERVER` is omitted. Always set all three localhost routing
variables above for an isolated local test. The script also does not implement
a conventional `--help` path; invoking it with `--help` starts the client.

## 5. Verify the connection

```bash
curl http://127.0.0.1:8765/api/agents \
  -H 'Authorization: Bearer <local-api-key>'
```

The response should include the named `guest` browser profile. The local UI
should separately report that both the browser and chat client are ready.

If the UI remains offline:

1. Confirm both processes use the API key stored for `dev@localhost`.
2. Confirm every client URL points to `127.0.0.1`, not the production host.
3. Confirm the web and relay ports match any `WEB_PORT` or `RELAY_PORT`
   overrides used with `dev.sh`.
4. Check `/tmp/unchained-dev/web.log`, `/tmp/unchained-dev/relay.log`, and the
   client terminal output.

## Stop the local stack

Stop the chat client and Chrome bridge with `Ctrl-C`, then stop the servers:

```bash
./dev.sh stop
```

`dev.sh` does not start a private-core service. UI-only work and public-safe
tests do not require one. End-to-end private-core development requires the
separate private workspace setup.
