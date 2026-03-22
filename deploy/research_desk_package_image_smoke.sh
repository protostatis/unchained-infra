#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
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

mkdir -p "${TMP_DIR}/research_desk_vendor/unchained_pyreplab"
cp Dockerfile "${TMP_DIR}/"
cp -R unchained "${TMP_DIR}/"
cp research_desk_vendor/manifest.json "${TMP_DIR}/research_desk_vendor/"
cp research_desk_vendor/README.md "${TMP_DIR}/research_desk_vendor/"
cp research_desk_vendor/pyproject.toml "${TMP_DIR}/research_desk_vendor/"
cp research_desk_vendor/unchained_pyreplab/*.py "${TMP_DIR}/research_desk_vendor/unchained_pyreplab/"

docker build -t "${IMAGE_TAG}" "${TMP_DIR}"
docker run --rm "${IMAGE_TAG}" python - <<'PY'
from agent_package import build_research_desk_zip

zip_bytes = build_research_desk_zip()
if not zip_bytes:
    raise SystemExit("build_research_desk_zip returned no bytes")
print(f"zip_ok {len(zip_bytes)}")
PY
