#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
HOSTED_BASE="${HOSTED_BASE:-https://unchainedsky.com}"
LOCAL_BASE="${LOCAL_BASE:-http://127.0.0.1:8766}"
RESULTS_DIR_INPUT="${RESULTS_DIR:-${REPO_ROOT}/benchmark/results}"
PYTHON_BIN="${PYTHON_BIN:-}"
CURL_FLAGS=(--fail --silent --show-error --location --connect-timeout 5 --max-time 20)

if [[ "${RESULTS_DIR_INPUT}" == "/" ]]; then
  RESULTS_DIR="/"
else
  RESULTS_DIR="${RESULTS_DIR_INPUT%/}"
fi

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
ARTIFACT_PATH="${RESULTS_DIR}/research-desk-release-smoke-${STAMP}.json"
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/research-desk-release-smoke.XXXXXX")"
HOSTED_HTML_PATH="${TMP_DIR}/hosted.html"
FIRST_LOOK_HTML_PATH="${TMP_DIR}/first-look.html"
LOCAL_STATUS_PATH="${TMP_DIR}/local-status.json"
ARTIFACT_WRITTEN=0

json_escape() {
  local value="${1//\\/\\\\}"
  value="${value//\"/\\\"}"
  value="${value//$'\n'/ }"
  value="${value//$'\r'/ }"
  value="${value//$'\t'/ }"
  printf '%s' "${value}"
}

write_failure_artifact() {
  local exit_code="$1"
  local message="$2"
  mkdir -p "$RESULTS_DIR"
  cat >"${ARTIFACT_PATH}" <<EOF
{
  "ok": false,
  "generated_at_utc": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "hosted_base": "${HOSTED_BASE}",
  "local_base": "${LOCAL_BASE}",
  "failure": {
    "exit_code": ${exit_code},
    "message": "$(json_escape "$message")"
  }
}
EOF
  ARTIFACT_WRITTEN=1
}

cleanup() {
  local exit_code="$1"
  local line_no="$2"
  local failed_command="$3"
  if [[ -f "${ARTIFACT_PATH}" ]]; then
    ARTIFACT_WRITTEN=1
  fi
  if [[ "${ARTIFACT_WRITTEN}" != "1" ]]; then
    write_failure_artifact "${exit_code}" "research_desk_release_smoke.sh failed at line ${line_no}: ${failed_command}"
  fi
  rm -rf "${TMP_DIR}"
  trap - EXIT
  exit "${exit_code}"
}
trap 'cleanup $? ${LINENO} "${BASH_COMMAND:-unknown}"' EXIT

mkdir -p "$RESULTS_DIR"

if [[ -z "${PYTHON_BIN}" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
  else
    echo "python3 interpreter not found" >&2
    exit 1
  fi
fi

curl "${CURL_FLAGS[@]}" "${HOSTED_BASE%/}/labs/research-desk" -o "${HOSTED_HTML_PATH}"
curl "${CURL_FLAGS[@]}" "${HOSTED_BASE%/}/first-look" -o "${FIRST_LOOK_HTML_PATH}"
curl "${CURL_FLAGS[@]}" "${LOCAL_BASE%/}/web/research-desk/status" -o "${LOCAL_STATUS_PATH}"

# Keep these checks in sync with the manual browser pass in docs/research_desk_release_smoke.md.
if ! "${PYTHON_BIN}" "${SCRIPT_DIR}/research_desk_release_smoke.py" \
  "$ARTIFACT_PATH" \
  "$HOSTED_BASE" \
  "$LOCAL_BASE" \
  "$HOSTED_HTML_PATH" \
  "$FIRST_LOOK_HTML_PATH" \
  "$LOCAL_STATUS_PATH"; then
  ARTIFACT_WRITTEN=1
  exit 1
fi
ARTIFACT_WRITTEN=1

echo "Wrote ${ARTIFACT_PATH}"
