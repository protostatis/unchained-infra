#!/usr/bin/env python3
"""Merge legacy hosted-conversation JSON files into persistent storage.

The web container historically wrote this repository below ``$HOME``. Before
recreating that container, deployment stops it, snapshots the legacy directory,
and runs this tool in a short-lived helper container sharing the persistent
``/data`` volume. Existing destination files are never overwritten unless
their bytes match exactly.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import sys
import uuid


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def preserve_json_files(source: Path, destination: Path) -> tuple[int, int]:
    """Copy source JSON files without overwriting conflicting destination data."""
    if source.resolve() == destination.resolve():
        return 0, 0
    if source.is_symlink() or destination.is_symlink():
        raise RuntimeError("hosted conversation store roots must not be symlinks")
    if not source.is_dir():
        return 0, 0

    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    copied = unchanged = 0
    for root, dirnames, filenames in os.walk(source, followlinks=False):
        root_path = Path(root)
        if any((root_path / name).is_symlink() for name in dirnames):
            raise RuntimeError("hosted conversation store contains a symlinked directory")
        relative_root = root_path.relative_to(source)
        target_root = destination / relative_root
        if target_root.is_symlink():
            raise RuntimeError("persistent hosted conversation path contains a symlink")
        target_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        for name in filenames:
            if not name.endswith(".json"):
                continue
            src = root_path / name
            if src.is_symlink():
                raise RuntimeError("hosted conversation store contains a symlinked file")
            if not src.is_file():
                continue
            dest = target_root / name
            if dest.is_symlink():
                raise RuntimeError("persistent hosted conversation path contains a symlink")
            if dest.exists():
                if dest.is_symlink() or not dest.is_file() or _digest(src) != _digest(dest):
                    raise RuntimeError(
                        "conflicting hosted conversation metadata exists in persistent storage"
                    )
                unchanged += 1
                continue

            temp = dest.with_name(f".{dest.name}.{uuid.uuid4().hex}.tmp")
            try:
                with src.open("rb") as source_file, temp.open("xb") as target_file:
                    os.chmod(temp, 0o600)
                    shutil.copyfileobj(source_file, target_file, length=1024 * 1024)
                    target_file.flush()
                    os.fsync(target_file.fileno())
                os.utime(temp, ns=(src.stat().st_atime_ns, src.stat().st_mtime_ns))
                os.replace(temp, dest)
                copied += 1
            finally:
                try:
                    temp.unlink()
                except FileNotFoundError:
                    pass
    return copied, unchanged


def _default_source() -> Path:
    home = Path.home()
    data_dir = os.environ.get("UNCHAINED_DATA_DIR")
    return Path(
        os.environ.get("UNCHAINED_HOSTED_DATA_DIR")
        or (Path(data_dir) if data_dir else home / ".unchained") / "hosted-conversations"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=_default_source())
    parser.add_argument(
        "--destination-dir", type=Path, default=Path("/data/hosted-conversations")
    )
    args = parser.parse_args(argv)
    source = args.source_dir
    destination = args.destination_dir
    copied, unchanged = preserve_json_files(source, destination)
    print(
        "hosted conversation metadata preserved "
        f"(copied={copied}, already_present={unchanged})"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: could not preserve hosted conversation metadata ({type(exc).__name__})", file=sys.stderr)
        raise SystemExit(1)
