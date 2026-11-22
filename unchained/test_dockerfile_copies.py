"""Guard: every local module the runtime imports must be copied into the image.

The image copies files individually rather than the whole directory, so a new
top-level module is invisible to Docker until someone adds a COPY line. That
shipped a crash loop: `web` imported `mcp_identity` and the container could not
start. This test walks imports from the entrypoints and fails if a reachable
local module has no COPY (or a copied package directory).
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DOCKERFILE = ROOT.parent / "Dockerfile"

# Entrypoints in the image, plus the packages copied wholesale.
ROOT_MODULES = ("web.py", "mcp_server.py", "relay.py", "private_core_server.py", "api.py")
PACKAGE_DIRS = ("web_app", "benchmark")


def _copied_sources(dockerfile: Path) -> tuple[set[str], set[str]]:
    modules: set[str] = set()
    dirs: set[str] = set()
    pattern = re.compile(r"^COPY\s+unchained/(\S+)\s+", re.MULTILINE)
    for source in pattern.findall(dockerfile.read_text()):
        if source.endswith("/"):
            dirs.add(source.rstrip("/"))
        elif source.endswith(".py") and "/" not in source:
            modules.add(Path(source).stem)
    return modules, dirs


def _import_roots(path: Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text())
    except SyntaxError:
        return set()
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            if node.module:
                roots.add(node.module.split(".")[0])
    return roots


def _local_modules() -> dict[str, Path]:
    return {p.stem: p for p in ROOT.glob("*.py")}


def _entrypoints() -> list[Path]:
    entries = [ROOT / name for name in ROOT_MODULES if (ROOT / name).is_file()]
    for package in PACKAGE_DIRS:
        entries.extend(sorted((ROOT / package).rglob("*.py")))
    return entries


class DockerfileCoverageTests(unittest.TestCase):
    def test_reachable_local_modules_are_copied(self):
        copied_modules, copied_dirs = _copied_sources(DOCKERFILE)
        self.assertTrue(copied_modules, "no COPY lines parsed from Dockerfile")

        local = _local_modules()
        seen: set[str] = set()
        queue = list(_entrypoints())
        while queue:
            path = queue.pop()
            for root in _import_roots(path):
                if root in seen or root not in local:
                    continue
                seen.add(root)
                queue.append(local[root])

        missing = sorted(name for name in seen if name not in copied_modules)
        self.assertEqual(
            missing,
            [],
            "Dockerfile does not copy these imported modules: " + ", ".join(missing),
        )

    def test_oauth_ops_scripts_are_in_the_image(self):
        text = DOCKERFILE.read_text()
        for name in (
            "register_mcp_oauth_client.py",
            "manage_mcp_oauth_policy.py",
        ):
            self.assertIn(f"COPY unchained/scripts/{name} scripts/{name}", text)

    def test_new_oauth_modules_are_listed(self):
        copied_modules, _ = _copied_sources(DOCKERFILE)
        for name in (
            "mcp_identity",
            "mcp_oauth_policy",
            "mcp_oauth_store",
            "mcp_oauth_provider",
        ):
            self.assertIn(name, copied_modules)


if __name__ == "__main__":
    unittest.main(verbosity=2)
