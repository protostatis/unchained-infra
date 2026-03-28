#!/usr/bin/env bash
set -euo pipefail

SRC_DIR="${1:-private-core/unchained}"
DST_DIR="${2:-unchained}"

if [[ ! -d "$SRC_DIR" ]]; then
  echo "private core source directory not found: $SRC_DIR" >&2
  exit 1
fi

for f in \
  cdp.py \
  ddm.py \
  intel.py \
  editable_helpers.js \
  private_core_engine.py \
  private_core_server.py \
  private_core_contracts.py \
  challenge_detection.py \
  domain_policy.py \
  CLAUDE.md \
  LABEL_RESOLUTION.md
do
  if [[ ! -f "$SRC_DIR/$f" ]]; then
    echo "missing private core file: $SRC_DIR/$f" >&2
    exit 1
  fi
  cp "$SRC_DIR/$f" "$DST_DIR/$f"
  echo "installed $f"
done

# Overlay benchmark subpackage files
mkdir -p "$DST_DIR/benchmark"
for f in benchmark/progress_critic.py benchmark/intermediate_goal.py; do
  if [[ ! -f "$SRC_DIR/$f" ]]; then
    echo "missing private core file: $SRC_DIR/$f" >&2
    exit 1
  fi
  cp "$SRC_DIR/$f" "$DST_DIR/$f"
  echo "installed $f"
done
