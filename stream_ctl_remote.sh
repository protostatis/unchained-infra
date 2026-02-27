#!/bin/bash
# stream_ctl_remote.sh — Remote control terminal for unchainedskytv_prod
#
# Usage:
#   ./stream_ctl_remote.sh              # interactive AI-assisted mode
#   ./stream_ctl_remote.sh --raw        # raw command mode
#   ./stream_ctl_remote.sh status       # one-off status check
#   ./stream_ctl_remote.sh go "Machu Picchu, Peru"
#   ./stream_ctl_remote.sh say "Walking through ancient ruins"
#   ./stream_ctl_remote.sh skip

EC2_HOST="${EC2_HOST:-}"
SSH_KEY="${EC2_SSH_KEY_PATH:-}"
CONTAINER="unchainedskytv-stream-1"

if [ -z "$EC2_HOST" ]; then
    echo "ERROR: EC2_HOST env var is required."
    exit 1
fi

SSH_OPTS=()
if [ -n "$SSH_KEY" ]; then
    SSH_OPTS+=(-i "$SSH_KEY")
fi
SSH_DEST="ubuntu@$EC2_HOST"

# One-off command mode
if [ $# -ge 1 ] && [ "$1" != "--raw" ]; then
    CMD="$1"
    shift
    ARG="$*"

    # Base64-encode the argument to survive all shell layers
    ARG_B64=$(printf '%s' "$ARG" | base64)

    # Build a self-contained Python script
    PYSCRIPT=$(cat <<'PYEOF'
import socket, json, os, sys, base64
cmd = sys.argv[1]
arg = base64.b64decode(sys.argv[2]).decode() if len(sys.argv) > 2 and sys.argv[2] else ""
payload = {"cmd": cmd}
if cmd == "go":
    payload["dest"] = arg
elif cmd == "say":
    payload["text"] = arg
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(5)
s.connect(os.path.expanduser("~/.unchained/stream.sock"))
s.sendall(json.dumps(payload).encode() + b"\n")
data = json.loads(s.recv(8192).decode())
s.close()
for k, v in sorted(data.items()):
    if isinstance(v, list):
        v = ", ".join(str(x) for x in v) or "(empty)"
    print(f"  {k}: {v}")
PYEOF
)

    # Base64-encode the script too
    SCRIPT_B64=$(printf '%s' "$PYSCRIPT" | base64)
    ssh "${SSH_OPTS[@]}" "$SSH_DEST" \
        "docker exec $CONTAINER bash -c 'echo $SCRIPT_B64 | base64 -d | python3 - $CMD $ARG_B64'"
    exit 0
fi

# Interactive mode
MODE=""
if [ "$1" = "--raw" ]; then
    MODE="--raw"
fi

ssh -t "${SSH_OPTS[@]}" "$SSH_DEST" \
    "docker exec -it $CONTAINER python3 stream_ctl.py $MODE"
