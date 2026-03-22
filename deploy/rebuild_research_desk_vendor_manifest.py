#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
VENDOR_ROOT = REPO_ROOT / "research_desk_vendor"
VENDOR_PACKAGE_DIR = VENDOR_ROOT / "unchained_pyreplab"
ROOT_FILES = ("README.md", "pyproject.toml", "setup.py")
MANIFEST_PATH = VENDOR_ROOT / "manifest.json"
VERSION = "0.1.0"


def build_manifest() -> dict[str, object]:
    files: dict[str, str] = {}
    for rel in ROOT_FILES:
        path = VENDOR_ROOT / rel
        files[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    for path in sorted(VENDOR_PACKAGE_DIR.glob("*.py")):
        rel_path = path.relative_to(VENDOR_ROOT).as_posix()
        files[rel_path] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"files": files, "version": VERSION}


def main() -> int:
    parser = argparse.ArgumentParser(description="Regenerate or verify research_desk_vendor/manifest.json.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if the current manifest does not match the vendored files.",
    )
    args = parser.parse_args()

    manifest = build_manifest()
    rendered = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    if args.check:
        current = MANIFEST_PATH.read_text(encoding="utf-8")
        return 0 if current == rendered else 1
    MANIFEST_PATH.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
