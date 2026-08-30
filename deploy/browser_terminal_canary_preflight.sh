#!/usr/bin/env bash
# Validate the browser-terminal canary overlay before its first activation.
# This intentionally performs no Compose mutation and requires the edge flag to
# remain disabled while the image and network contract are being checked.

set -euo pipefail
umask 077

if [[ "$#" -ne 0 ]]; then
    echo "usage: $0" >&2
    exit 2
fi

script_dir="$(cd "$(dirname "$0")/.." && pwd)"
cd "$script_dir"

config_file="$(mktemp)"
cleanup() {
    rm -f "$config_file"
}
trap cleanup EXIT

docker compose \
    --profile fin-terminal-browser-canary \
    -f docker-compose.yml \
    -f docker-compose.browser-terminal.yml \
    config --format json > "$config_file"

image="$(python3 - "$config_file" <<'PY'
import json
import re
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    config = json.load(handle)
services = config.get("services", {})
browser = services.get("fin-terminal-browser")
caddy = services.get("caddy")
browser_mcp = services.get("fin-terminal-browser-mcp")
singleton = services.get("fin-terminal")
if not all(isinstance(service, dict) for service in (browser, browser_mcp, caddy, singleton)):
    raise SystemExit("browser canary services are missing from the rendered Compose config")

image = browser.get("image")
if not isinstance(image, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:+-]*@sha256:[0-9a-f]{64}", image):
    raise SystemExit("FIN_TERMINAL_BROWSER_IMAGE must be an immutable digest-pinned image")

environment = browser.get("environment", {})
if not isinstance(environment, dict):
    raise SystemExit("browser canary environment must render as a mapping")
required = {
    "NODE_ENV": "production",
    "TERMINAL_RUNTIME_MODE": "browser",
    "PUBLIC_BASE_PATH": "/fin-terminal-browser/",
    "MARKET_ROOT": "/app",
    "MARKET_DATA_DIR": "/data",
}
for name, expected in required.items():
    if environment.get(name) != expected:
        raise SystemExit(f"browser canary environment {name} is not {expected!r}")

if browser.get("ports"):
    raise SystemExit("browser canary must not publish a host port")
if browser_mcp.get("ports"):
    raise SystemExit("browser canary MCP must not publish a host port")
if "fin_terminal_browser" not in browser.get("networks", {}):
    raise SystemExit("browser canary is missing the Caddy network")
if "fin_terminal_browser_mcp" not in browser.get("networks", {}):
    raise SystemExit("browser canary is missing the private MCP network")
if "fin_terminal_browser_mcp" not in browser_mcp.get("networks", {}):
    raise SystemExit("browser canary MCP is missing its private network")
if "unbrowser_egress_proxy" not in browser_mcp.get("networks", {}):
    raise SystemExit("browser canary MCP is missing the egress proxy network")

caddy_environment = caddy.get("environment", {})
if not isinstance(caddy_environment, dict) or str(caddy_environment.get("FIN_TERMINAL_BROWSER_ENABLED", "")).lower() != "false":
    raise SystemExit("FIN_TERMINAL_BROWSER_ENABLED must remain false during canary preflight")
if not isinstance(singleton.get("environment", {}), dict):
    raise SystemExit("singleton environment must render as a mapping")
if caddy_environment.get("FIN_TERMINAL_BROWSER_PROXY_TOKEN") != environment.get("MARKET_PROXY_TOKEN"):
    raise SystemExit("Caddy and browser canary proxy tokens do not match")
if not environment.get("MARKET_PROXY_TOKEN") or environment.get("MARKET_PROXY_TOKEN") == singleton["environment"].get("MARKET_PROXY_TOKEN"):
    raise SystemExit("browser canary proxy token must be present and distinct from the Pi token")

print(image)
PY
)"

if ! docker image inspect "$image" >/dev/null 2>&1; then
    docker image pull "$image" >/dev/null
fi
docker image inspect "$image" >/dev/null
docker run --rm --network none --read-only \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=16m \
    --env NODE_ENV=production \
    --env HOST=127.0.0.1 \
    --env PORT=8787 \
    --env PUBLIC_BASE_PATH=/fin-terminal-browser/ \
    --env TERMINAL_RUNTIME_MODE=browser \
    --env MARKET_PROXY_TOKEN=canary-preflight-proxy-token \
    --env OPENROUTER_API_KEY=canary-preflight-openrouter-key \
    --env UNBROWSER_MCP_URL=http://127.0.0.1:8767/mcp \
    --entrypoint sh "$image" -c '
        set -eu
        node dist-server/server/browser-terminal-main.js >/tmp/browser-terminal.log 2>&1 &
        pid=$!
        trap "kill $pid 2>/dev/null || true; wait $pid 2>/dev/null || true" EXIT
        for attempt in $(seq 1 20); do
            if ! kill -0 "$pid" 2>/dev/null; then
                cat /tmp/browser-terminal.log
                exit 1
            fi
            if node -e "fetch(\"http://127.0.0.1:8787/api/ready\").then(r=>{if(!r.ok)process.exit(1)}).catch(()=>process.exit(1))"; then
                exit 0
            fi
            sleep 0.25
        done
        cat /tmp/browser-terminal.log
        exit 1
    '
echo "browser-terminal canary image and disabled overlay contract are valid"
