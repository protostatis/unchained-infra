#!/usr/bin/env bash
set -euo pipefail

HOSTED_BASE="${HOSTED_BASE:-https://unchainedsky.com}"
LOCAL_BASE="${LOCAL_BASE:-http://127.0.0.1:8766}"
RESULTS_DIR="${RESULTS_DIR:-benchmark/results}"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
ARTIFACT_PATH="${RESULTS_DIR%/}/research-desk-release-smoke-${STAMP}.json"

mkdir -p "$RESULTS_DIR"

HOSTED_HTML="$(curl -fsSL "${HOSTED_BASE%/}/labs/research-desk")"
FIRST_LOOK_HTML="$(curl -fsSL "${HOSTED_BASE%/}/first-look")"
LOCAL_STATUS_JSON="$(curl -fsSL "${LOCAL_BASE%/}/web/research-desk/status")"

/usr/bin/python3 - "$ARTIFACT_PATH" "$HOSTED_BASE" "$LOCAL_BASE" "$HOSTED_HTML" "$FIRST_LOOK_HTML" "$LOCAL_STATUS_JSON" <<'PY'
import json
import sys
from datetime import datetime, timezone

artifact_path, hosted_base, local_base, hosted_html, first_look_html, local_status_json = sys.argv[1:]
status = json.loads(local_status_json)

checks = {
    "hosted_has_connect_button": 'id="connect-local-desk"' in hosted_html,
    "hosted_has_create_button": 'id="create-local-mission"' in hosted_html,
    "hosted_has_run_next_button": 'id="run-local-next-step"' in hosted_html,
    "hosted_has_lab_link": 'id="mission-watch-lab-link"' in hosted_html,
    "first_look_has_research_desk_handoff": "/labs/research-desk" in first_look_html,
    "local_status_ok": bool(status.get("ok")),
    "local_has_launch_url": bool(((status.get("local_urls") or {}).get("home") or "").strip()),
    "local_has_handshake_start": bool(((status.get("handshake") or {}).get("start_url") or "").strip()),
    "local_has_mission_create": bool((((status.get("handshake") or {}).get("actions") or {}).get("mission_create_url") or "").strip()),
    "local_has_mission_advance": bool((((status.get("handshake") or {}).get("actions") or {}).get("mission_advance_url") or "").strip()),
}

payload = {
    "ok": all(checks.values()),
    "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    "hosted_base": hosted_base,
    "local_base": local_base,
    "checks": checks,
    "local_summary": {
        "launch_ready": bool(status.get("launch_ready")),
        "configured_provider": str(((status.get("provider") or {}).get("configured_provider") or "")).strip(),
        "browser_client": str(((status.get("provider") or {}).get("browser_client") or "")).strip(),
        "agent_id": str(((status.get("bridge") or {}).get("agent_id") or "")).strip(),
        "capsule_count": int(((status.get("capsules") or {}).get("count") or 0)),
    },
}

with open(artifact_path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2, sort_keys=True)
    handle.write("\n")

print(artifact_path)
PY

echo "Wrote ${ARTIFACT_PATH}"
