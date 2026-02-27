"""Boundary tests for open-core split."""

from __future__ import annotations

import ast
import io
import os
import zipfile


def _module_imports(path: str) -> set[str]:
    with open(path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)

    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_public_boundary_modules_do_not_import_private_impls():
    root = os.path.dirname(os.path.abspath(__file__))
    public_boundary_files = [
        "cloud_tools.py",
        "api.py",
        "mcp_server.py",
        "private_core_client.py",
    ]
    forbidden = {"cdp", "ddm", "intel"}

    violations: list[str] = []
    for rel in public_boundary_files:
        path = os.path.join(root, rel)
        imports = _module_imports(path)
        hit = sorted(imports & forbidden)
        if hit:
            violations.append(f"{rel}: {', '.join(hit)}")

    assert not violations, "Direct private imports found:\n" + "\n".join(violations)


def test_agent_package_excludes_private_core_files():
    from agent_package import build_agent_zip

    zip_bytes = build_agent_zip(api_key="uc_live_test", relay_host="localhost")
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = set(zf.namelist())

    forbidden_suffixes = {
        "unchained/cdp.py",
        "unchained/ddm.py",
        "unchained/intel.py",
        "unchained/private_core_engine.py",
    }
    leaked = []
    for name in names:
        for suffix in forbidden_suffixes:
            if name.endswith(suffix):
                leaked.append(name)

    assert not leaked, f"Agent package leaked private core files: {leaked}"
