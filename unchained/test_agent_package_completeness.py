"""Completeness guard for the downloadable agent package.

Regression this covers: 0.3.130 shipped ``chat_agent_cli.py`` importing
``conversation_transcript`` (and, through it, ``tool_payloads``), but neither
module was listed in ``_PACKAGE_FILES``. The generated ZIP therefore could not
start at all: every client that updated crashed on import
(``ModuleNotFoundError: No module named 'conversation_transcript'``), the
supervisor's circuit breaker rolled the update back, and the device stayed
stuck on the previous client — which is the version that cannot acknowledge a
hosted archive, so hosted "New Chat" could never finalize on that machine.

The package must be startable, not merely free of private files.
"""

from __future__ import annotations

import ast
import io
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile

import agent_package

_SRC_DIR = os.path.dirname(os.path.abspath(agent_package.__file__))
_PACKAGE_PREFIX = "unchained-agent"


def _local_module_imports(path: str) -> set[str]:
    """Return imported sibling modules that ship from this source directory.

    Names with no matching ``<name>.py`` beside the sources are stdlib or
    third-party imports and are ignored.
    """
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)

    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return {
        name
        for name in names
        if os.path.exists(os.path.join(_SRC_DIR, f"{name}.py"))
    }


def _packaged_module_names() -> set[str]:
    return {
        os.path.splitext(os.path.basename(dest))[0]
        for dest in agent_package._PACKAGE_FILES
        if dest.endswith(".py")
    }


class AgentPackageCompletenessTest(unittest.TestCase):
    def test_every_declared_source_file_exists(self):
        """``_add_source_files`` silently skips missing sources; never rely on it."""
        missing = sorted(
            src
            for src in agent_package._PACKAGE_FILES.values()
            if not os.path.exists(os.path.join(_SRC_DIR, src))
        )
        self.assertEqual([], missing, "_PACKAGE_FILES points at missing sources")

    def test_packaged_files_only_import_packaged_local_modules(self):
        """Every local module a packaged file imports must also be packaged."""
        packaged = _packaged_module_names()
        offenders: dict[str, list[str]] = {}
        for dest, source in agent_package._PACKAGE_FILES.items():
            if not dest.endswith(".py"):
                continue
            imported = _local_module_imports(os.path.join(_SRC_DIR, source))
            for name in sorted(imported - packaged):
                offenders.setdefault(name, []).append(dest)

        self.assertEqual(
            {},
            offenders,
            "packaged files import local modules the ZIP does not ship",
        )

    def test_zip_ships_declared_files_and_current_version(self):
        """Both download paths — install and code-only update — share the list."""
        zips = {
            "install": agent_package.build_agent_zip(
                api_key="uc_live_test", relay_host="localhost"
            ),
            "update": agent_package.build_update_zip(),
        }
        for label, data in zips.items():
            with self.subTest(zip=label), zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = set(zf.namelist())
                version = zf.read(f"{_PACKAGE_PREFIX}/version.txt").decode("utf-8").strip()
                # Packaged sources are rewritten on the way in (env patching and
                # a future-import injection), so the copies must still parse.
                for name in sorted(n for n in names if n.endswith(".py")):
                    ast.parse(zf.read(name).decode("utf-8"), filename=name)

                missing = sorted(
                    dest
                    for dest in agent_package._PACKAGE_FILES
                    if f"{_PACKAGE_PREFIX}/{dest}" not in names
                )
                self.assertEqual(
                    [], missing, "declared package files are missing from the ZIP"
                )
                self.assertEqual(agent_package.VERSION, version)

    def test_extracted_package_imports_every_shipped_module(self):
        """Startability, not just contents: import what the download ships.

        ``start.sh`` runs ``python unchained/chat_agent_cli.py`` from the package
        root, so the module directory is on ``sys.path``. This imports each
        shipped module in a separate interpreter, inside a temp dir, so the
        repository cannot satisfy an import the ZIP forgot — which is exactly
        how 0.3.130 died on users' machines.
        """
        data = agent_package.build_agent_zip(
            api_key="uc_live_test", relay_host="localhost"
        )
        with tempfile.TemporaryDirectory() as tmp:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                zf.extractall(tmp)
                modules = sorted(
                    os.path.splitext(os.path.basename(name))[0]
                    for name in zf.namelist()
                    if name.startswith(f"{_PACKAGE_PREFIX}/unchained/")
                    and name.endswith(".py")
                )

            self.assertTrue(modules, "no packaged modules found in the ZIP")
            script = "import sys; sys.path.insert(0, 'unchained')\n"
            script += "".join(f"import {module}\n" for module in modules)
            proc = subprocess.run(
                # -I keeps the surrounding environment (PYTHONPATH, cwd) from
                # standing in for a file the ZIP should have carried.
                [sys.executable, "-I", "-c", script],
                cwd=os.path.join(tmp, _PACKAGE_PREFIX),
                capture_output=True,
                text=True,
                timeout=180,
            )

        self.assertEqual(
            0,
            proc.returncode,
            f"extracted package could not import its own modules:\n{proc.stderr}",
        )


if __name__ == "__main__":
    unittest.main()
