#!/usr/bin/env python3
"""Ensure downloadable agent artifacts do not contain private core files."""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "unchained"))

from agent_package import build_agent_zip  # noqa: E402

FORBIDDEN_SUFFIXES = {
    "unchained/cdp.py",
    "unchained/ddm.py",
    "unchained/intel.py",
    "unchained/private_core_engine.py",
}


def main() -> int:
    zip_bytes = build_agent_zip(api_key="uc_live_guard", relay_host="localhost")
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()

    leaked = []
    for name in names:
        if any(name.endswith(suffix) for suffix in FORBIDDEN_SUFFIXES):
            leaked.append(name)

    if leaked:
        print("Private files leaked into agent artifact:")
        for item in leaked:
            print(f"- {item}")
        return 1

    print("Agent artifact leak check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
