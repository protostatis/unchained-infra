#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
IMAGE_TAG="${IMAGE_TAG:-unchained-research-desk-package-smoke}"

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required for deploy/research_desk_package_image_smoke.sh" >&2
  exit 1
fi

cd "${REPO_ROOT}"

docker build -t "${IMAGE_TAG}" .
docker run --rm "${IMAGE_TAG}" python - <<'PY'
from agent_package import build_research_desk_zip

zip_bytes = build_research_desk_zip()
if not zip_bytes:
    raise SystemExit("build_research_desk_zip returned no bytes")
print(f"zip_ok {len(zip_bytes)}")
PY
