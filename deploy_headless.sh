#!/bin/bash
# Deploy dedicated headless trial-agent stack to a separate EC2 instance.
#
# Usage:
#   EC2_HOST=1.2.3.4 ./deploy_headless.sh
#   EC2_HOST=1.2.3.4 ENV_FILE=.env.headless ./deploy_headless.sh --build

set -euo pipefail

EC2_HOST="${EC2_HOST:?EC2_HOST env var is required (e.g. EC2_HOST=1.2.3.4 ./deploy_headless.sh)}"
EC2_USER="${EC2_USER:-ec2-user}"
REMOTE_DIR="${REMOTE_DIR:-/home/$EC2_USER/unchained-headless}"
ENV_FILE="${ENV_FILE:-.env.headless}"

if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: env file not found: $ENV_FILE"
    echo "Copy .env.headless.example to .env.headless and set real values first."
    exit 1
fi

FORCE_BUILD=false
if [[ "${1:-}" == "--build" ]]; then
    FORCE_BUILD=true
fi

# Note: accept the host key manually first time with:
# ssh "${SSH_OPTS[@]}" "$EC2_USER@$EC2_HOST"
SSH_OPTS=()
if [[ -n "${KEY_PATH:-}" ]]; then
    SSH_OPTS+=(-i "$KEY_PATH")
fi
SSH_CMD=(ssh "${SSH_OPTS[@]}" "$EC2_USER@$EC2_HOST")
SCP_CMD=(scp "${SSH_OPTS[@]}")

echo "==> Deploying headless worker to $EC2_HOST"
"${SSH_CMD[@]}" "mkdir -p $REMOTE_DIR/unchained"

echo "==> Uploading stack files..."
"${SCP_CMD[@]}" \
    Dockerfile.headless \
    docker-compose.headless.yml \
    "$ENV_FILE" \
    "$EC2_USER@$EC2_HOST:$REMOTE_DIR/"

echo "==> Uploading Python modules..."
"${SCP_CMD[@]}" \
    unchained/auth.py \
    unchained/chat_agent_openrouter.py \
    unchained/chrome_bridge.py \
    unchained/cloud_tools.py \
    unchained/context_compact.py \
    unchained/cdp.py \
    unchained/ddm.py \
    unchained/intel.py \
    unchained/nudge.py \
    unchained/reflex.py \
    unchained/CLAUDE.md \
    "$EC2_USER@$EC2_HOST:$REMOTE_DIR/unchained/"

echo "==> Starting containers..."
if $FORCE_BUILD; then
    # --build mode: force a full no-cache image rebuild, then start containers.
    "${SSH_CMD[@]}" "cd \"$REMOTE_DIR\" && cp \"$(basename "$ENV_FILE")\" .env && chmod 600 .env && docker compose -f docker-compose.headless.yml --env-file .env build --no-cache && docker compose -f docker-compose.headless.yml --env-file .env up -d"
else
    # default mode: rebuild as needed using cache during `up --build`.
    "${SSH_CMD[@]}" "cd \"$REMOTE_DIR\" && cp \"$(basename "$ENV_FILE")\" .env && chmod 600 .env && docker compose -f docker-compose.headless.yml --env-file .env up -d --build"
fi

echo ""
echo "==> Headless stack status:"
"${SSH_CMD[@]}" "docker compose -f \"$REMOTE_DIR/docker-compose.headless.yml\" --env-file \"$REMOTE_DIR/.env\" ps"

echo ""
echo "==> Recent logs (headless-bridge):"
"${SSH_CMD[@]}" "docker compose -f \"$REMOTE_DIR/docker-compose.headless.yml\" --env-file \"$REMOTE_DIR/.env\" logs headless-bridge --tail 20"

echo ""
echo "==> Recent logs (headless-agent):"
"${SSH_CMD[@]}" "docker compose -f \"$REMOTE_DIR/docker-compose.headless.yml\" --env-file \"$REMOTE_DIR/.env\" logs headless-agent --tail 20"

echo ""
echo "==> Headless deploy complete."
echo "    Remote directory: $REMOTE_DIR"
