#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
HOSTED_BASE="${HOSTED_BASE:-https://unchainedsky.com}"
LOCAL_BASE="${LOCAL_BASE:-http://127.0.0.1:8766}"
RESULTS_DIR="${RESULTS_DIR:-${REPO_ROOT}/benchmark/results}"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"
CURL_FLAGS=(--fail --silent --show-error --location --connect-timeout 5 --max-time 20)

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
ARTIFACT_PATH="${RESULTS_DIR%/}/research-desk-release-smoke-${STAMP}.json"
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/research-desk-release-smoke.XXXXXX")"
HOSTED_HTML_PATH="${TMP_DIR}/hosted.html"
FIRST_LOOK_HTML_PATH="${TMP_DIR}/first-look.html"
LOCAL_STATUS_PATH="${TMP_DIR}/local-status.json"

cleanup() {
  rm -rf "${TMP_DIR}"
}
trap cleanup EXIT

mkdir -p "$RESULTS_DIR"

curl "${CURL_FLAGS[@]}" "${HOSTED_BASE%/}/labs/research-desk" -o "${HOSTED_HTML_PATH}"
curl "${CURL_FLAGS[@]}" "${HOSTED_BASE%/}/first-look" -o "${FIRST_LOOK_HTML_PATH}"
curl "${CURL_FLAGS[@]}" "${LOCAL_BASE%/}/web/research-desk/status" -o "${LOCAL_STATUS_PATH}"

"${PYTHON_BIN}" - "$ARTIFACT_PATH" "$HOSTED_BASE" "$LOCAL_BASE" "$HOSTED_HTML_PATH" "$FIRST_LOOK_HTML_PATH" "$LOCAL_STATUS_PATH" <<'PY'
import json
import sys
from datetime import datetime, timezone

artifact_path, hosted_base, local_base, hosted_html_path, first_look_html_path, local_status_path = sys.argv[1:]

with open(hosted_html_path, "r", encoding="utf-8") as handle:
    hosted_html = handle.read()
with open(first_look_html_path, "r", encoding="utf-8") as handle:
    first_look_html = handle.read()
with open(local_status_path, "r", encoding="utf-8") as handle:
    status = json.load(handle)

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
if not payload["ok"]:
    sys.exit(1)
PY

echo "Wrote ${ARTIFACT_PATH}"
