# Benchmark

Standard procedure for browser benchmark runs:

1. Overlay private core into `unchained-infra/unchained` so `private_core_client`
   uses the real inprocess implementations.
2. Start a local relay on `127.0.0.1:8765` with a shared `PRIVATE_CORE_TOKEN`.
3. Create a local bench API key in the local `auth.db`.
4. Start `chrome_bridge.py` against the local relay with that key.
5. Run `benchmark.runner` with the local `CDP_AGENT_ID`, `CDP_RELAY_HOST=127.0.0.1`,
   and `CDP_RELAY_PORT=8765`.
6. Leave `PRIVATE_CORE_URL` unset so benchmark tool calls stay inprocess instead of
   going to a remote private-core HTTP service.

This keeps benchmark traffic off production. `benchmark.runner` blocks remote relay
and private-core targets by default. Use `--allow-remote` only for deliberate
exceptions.

Example local setup:

```bash
cd /Users/zhiminzou/Projects/unchainedsky_com
./unchained-infra/tools/install_private_core.sh \
  unchained-core-private/unchained \
  unchained-infra/unchained

cd unchained-infra/unchained
PRIVATE_CORE_TOKEN=benchtoken123 uv run python relay.py --port 8765
uv run python auth.py create bench-user
uv run python chrome_bridge.py start \
  --relay ws://127.0.0.1:8765/tunnel \
  --key <local_api_key>
```

Example smoke test:

```bash
cd /Users/zhiminzou/Projects/unchainedsky_com/unchained-infra/unchained
export PRIVATE_CORE_TOKEN=benchtoken123
export CDP_AGENT_ID=<local_agent_id>
export CDP_RELAY_HOST=127.0.0.1
export CDP_RELAY_PORT=8765
uv run python -m benchmark.runner --agents autorouter --subset wiki_gdp_click
```

Example hard-task run in parallel tabs:

```bash
cd /Users/zhiminzou/Projects/unchainedsky_com/unchained-infra/unchained
export PRIVATE_CORE_TOKEN=benchtoken123
export CDP_AGENT_ID=<local_agent_id>
export CDP_RELAY_HOST=127.0.0.1
export CDP_RELAY_PORT=8765
uv run python -m benchmark.runner --agents autorouter --difficulty hard --parallel-tasks 13
```
