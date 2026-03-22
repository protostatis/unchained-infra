#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
source "${SCRIPT_DIR}/runtime_context_files.sh"

IMAGE_TAG="${IMAGE_TAG:-unchained-research-desk-package-smoke}"
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/research-desk-package-image-smoke.XXXXXX")"

cleanup() {
  rm -rf "${TMP_DIR}"
}
trap cleanup EXIT

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required for deploy/research_desk_package_image_smoke.sh" >&2
  exit 1
fi

cd "${REPO_ROOT}"

mkdir -p "${TMP_DIR}/unchained/benchmark" "${TMP_DIR}/research_desk_vendor/unchained_pyreplab"

for rel in "${TOP_LEVEL_CONTEXT_FILES[@]}"; do
  cp "${rel}" "${TMP_DIR}/"
done

for rel in "${UNCHAINED_RUNTIME_FILES[@]}"; do
  cp "unchained/${rel}" "${TMP_DIR}/unchained/"
done

for rel in "${BENCHMARK_CONTEXT_FILES[@]}"; do
  cp "unchained/benchmark/${rel}" "${TMP_DIR}/unchained/benchmark/"
done

cp -R unchained/web_app "${TMP_DIR}/unchained/"

if [[ -d "unchained/installers" ]]; then
  cp -R unchained/installers "${TMP_DIR}/unchained/"
fi

for rel in "${RESEARCH_DESK_VENDOR_ROOT_FILES[@]}"; do
  cp "research_desk_vendor/${rel}" "${TMP_DIR}/research_desk_vendor/"
done

shopt -s nullglob
RESEARCH_DESK_VENDOR_FILES=(research_desk_vendor/unchained_pyreplab/*.py)
shopt -u nullglob
if [[ "${#RESEARCH_DESK_VENDOR_FILES[@]}" -eq 0 ]]; then
  echo "Research Desk vendor package has no Python files" >&2
  exit 1
fi
cp "${RESEARCH_DESK_VENDOR_FILES[@]}" "${TMP_DIR}/research_desk_vendor/unchained_pyreplab/"

docker build -t "${IMAGE_TAG}" "${TMP_DIR}"
docker run --rm "${IMAGE_TAG}" python - <<'PY'
from agent_package import build_research_desk_zip

zip_bytes = build_research_desk_zip()
if not zip_bytes:
    raise SystemExit("build_research_desk_zip returned no bytes")
print(f"zip_ok {len(zip_bytes)}")
PY
