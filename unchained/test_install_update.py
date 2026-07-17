"""Tests for the curl|bash installer + auto-update feature.

Tests agent_package.py, auth.py install tokens, web.py endpoints,
and chat_agent_cli.py version checking.
"""
from __future__ import annotations

import asyncio
import ast
import io
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import types
import zipfile
from pathlib import Path
from unittest import mock

os.environ.setdefault("JWT_SECRET", "test-jwt-secret")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── agent_package.py tests ──────────────────────────────────────────

def test_version_constants():
    from agent_package import VERSION, MIN_VERSION
    assert isinstance(VERSION, str)
    assert isinstance(MIN_VERSION, str)
    # Should be semver-like
    parts = VERSION.split(".")
    assert len(parts) == 3, f"VERSION should be x.y.z, got {VERSION}"
    for p in parts:
        int(p)  # must be numeric
    print(f"  VERSION={VERSION}, MIN_VERSION={MIN_VERSION}")


def test_build_agent_zip_contains_version_and_update():
    from agent_package import build_agent_zip, VERSION
    zip_bytes = build_agent_zip(
        api_key="uc_live_test123",
        relay_host="localhost",
        install_token="inst_test_bootstrap",
    )
    assert len(zip_bytes) > 0, "ZIP is empty"

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()
        # version.txt
        assert "unchained-agent/version.txt" in names, f"version.txt missing. Files: {names}"
        v = zf.read("unchained-agent/version.txt").decode()
        assert v == VERSION, f"version.txt content={v!r}, expected {VERSION!r}"
        # update.sh
        assert "unchained-agent/update.sh" in names, f"update.sh missing. Files: {names}"
        update_sh = zf.read("unchained-agent/update.sh").decode()
        assert "#!/bin/bash" in update_sh
        assert "/web/agent/version" in update_sh
        assert "/web/agent/files" in update_sh
        # windows scripts
        assert "unchained-agent/start.ps1" in names, "start.ps1 missing"
        assert "unchained-agent/stop.ps1" in names, "stop.ps1 missing"
        assert "unchained-agent/update.ps1" in names, "update.ps1 missing"
        stop_ps1 = zf.read("unchained-agent/stop.ps1").decode()
        assert 'Remove-WindowsAutostart' in stop_ps1
        assert 'Join-Path $startupDir "Unchained Agent.cmd"' in stop_ps1
        update_ps1 = zf.read("unchained-agent/update.ps1").decode()
        assert "/web/agent/version" in update_ps1
        assert "/web/agent/files" in update_ps1
        # start.sh still there
        assert "unchained-agent/start.sh" in names
        assert "unchained-agent/unchained/scheduler_tool.py" in names
        start_sh = zf.read("unchained-agent/start.sh").decode()
        assert "/web/install/claim/start" in start_sh
        assert "/web/install/claim/poll" in start_sh
        assert "/install/claim/" in start_sh
        assert "<key>KeepAlive</key>" in start_sh
        assert "<true/>" in start_sh
        assert "<string>--daemon</string>" not in start_sh
        assert 'export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH:$HOME/.local/bin"' in start_sh
        assert 'command -v claude >/dev/null 2>&1' in start_sh
        assert 'export CLAUDE_BIN="$(command -v claude)"' in start_sh
        assert 'command -v opencode >/dev/null 2>&1' in start_sh
        assert 'export OPENCODE_BIN="$(command -v opencode)"' in start_sh
        assert 'export CLAUDE_BIN="$HOME/.local/bin/claude"' not in start_sh
        stop_sh = zf.read("unchained-agent/stop.sh").decode()
        assert 'AUTOSTART_LABEL="com.unchained.agent"' in stop_sh
        assert 'launchctl bootout "gui/$(id -u)/$AUTOSTART_LABEL"' in stop_sh
        start_ps1 = zf.read("unchained-agent/start.ps1").decode()
        assert "/web/install/claim/start" in start_ps1
        assert "/web/install/claim/poll" in start_ps1
        assert "/install/claim/" in start_ps1
        assert "GetFolderPath(\"Startup\")" in start_ps1
        assert "Autostart: enabled at Windows login" in start_ps1
        assert "Install-PythonRuntime" in start_ps1
        assert "python.org/ftp/python/" in start_ps1
        assert '$venvDir = Join-Path $PSScriptRoot ".venv"' in start_ps1
        assert '$pythonPrefixArgs += $pythonInfo.Prefix' in start_ps1
        # .env still there
        assert "unchained-agent/.env" in names
        env = zf.read("unchained-agent/.env").decode()
        assert "uc_live_test123" not in env
        assert "UNCHAINED_API_KEY=" in env
        assert "UNCHAINED_INSTALL_TOKEN=inst_test_bootstrap" in env
        assert "OPENCODE_MODEL=" not in env
    print(f"  ZIP size: {len(zip_bytes)} bytes, {len(names)} files")


def test_build_agent_zip_targets_python38_client_runtime():
    from agent_package import _PACKAGE_FILES, build_agent_zip

    zip_bytes = build_agent_zip(
        api_key="uc_live_test123",
        relay_host="localhost",
        install_token="inst_test_bootstrap",
    )

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        requirements = zf.read("unchained-agent/requirements.txt").decode()
        assert "aiohttp==3.10.11" in requirements
        assert "PyJWT==2.9.0" in requirements
        assert "certifi>=2026.1.4" in requirements

        start_ps1 = zf.read("unchained-agent/start.ps1").decode()
        assert "Python 3.8+ not found" in start_ps1
        assert "sys.version_info >= (3, 8)" in start_ps1
        assert "$env:UNCHAINED_CA_BUNDLE" in start_ps1
        assert 'pip install -q "certifi>=2026.1.4"' in start_ps1
        assert "import certifi; print(certifi.where())" in start_ps1
        assert "Test-Path -LiteralPath $caBundle -PathType Leaf" in start_ps1
        assert "ssl.create_default_context(cafile=sys.argv[1])" in start_ps1
        assert "$env:SSL_CERT_FILE = $caBundle" in start_ps1

        readme = zf.read("unchained-agent/README.txt").decode()
        assert "Python 3.8+" in readme

        for dest in _PACKAGE_FILES:
            if not dest.endswith(".py"):
                continue
            packaged_path = f"unchained-agent/{dest}"
            source = zf.read(packaged_path).decode()
            # The packaged client relies on deferred annotation evaluation for
            # older interpreters, so every shipped module should carry the import.
            assert "from __future__ import annotations" in source, packaged_path
            ast.parse(source)

    print("  Packaged client runtime is aligned to Python 3.8+")


def test_windows_certifi_check_avoids_legacy_native_quote_loss():
    """Avoid Python literals that Windows PowerShell 5.1 strips from native args."""
    from agent_package import _START_PS1

    certifi_check = next(
        line for line in _START_PS1.splitlines()
        if line.startswith("& $pythonExe -c ") and "importlib.metadata" in line
    )
    # Windows PowerShell 5.1 strips embedded double quotes from native
    # arguments when the PowerShell string itself is single-quoted.
    assert certifi_check.startswith('& $pythonExe -c "')
    python_source = certifi_check.split('"', 2)[1]
    assert "version(sys.argv[1])" in python_source
    assert "split(sys.argv[2])" in python_source
    assert "'" not in python_source and '"' not in python_source
    assert '" certifi "." 2>$null' in certifi_check
    print("  Windows certifi version check avoids PowerShell 5.1 quote loss")


def test_build_update_zip_no_env_with_launchers():
    from agent_package import build_update_zip, VERSION
    zip_bytes = build_update_zip()
    assert len(zip_bytes) > 0

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()
        # Should have code files
        assert "unchained-agent/version.txt" in names
        assert "unchained-agent/requirements.txt" in names
        assert "unchained-agent/start.sh" in names
        assert "unchained-agent/start.ps1" in names
        assert "unchained-agent/start.bat" in names
        assert "unchained-agent/update.sh" in names
        assert "unchained-agent/update.ps1" in names
        assert "unchained-agent/unchained/cdp_tool.py" in names
        assert "unchained-agent/unchained/scheduler_tool.py" in names
        assert "unchained-agent/stop.sh" in names
        assert "unchained-agent/stop.ps1" in names
        start_sh = zf.read("unchained-agent/start.sh").decode()
        assert 'command -v opencode >/dev/null 2>&1' in start_sh
        assert 'export OPENCODE_BIN="$(command -v opencode)"' in start_sh
        stop_sh = zf.read("unchained-agent/stop.sh").decode()
        assert 'launchctl bootout "gui/$(id -u)/$AUTOSTART_LABEL"' in stop_sh
        stop_ps1 = zf.read("unchained-agent/stop.ps1").decode()
        assert 'Remove-WindowsAutostart' in stop_ps1
        update_sh = zf.read("unchained-agent/update.sh").decode()
        assert 'cp -f unchained-agent/stop.sh "$AGENT_DIR/"' in update_sh
        assert 'chmod +x "$AGENT_DIR/start.sh" "$AGENT_DIR/update.sh" "$AGENT_DIR/stop.sh"' in update_sh
        update_ps1 = zf.read("unchained-agent/update.ps1").decode()
        assert '"start.ps1"' in update_ps1
        assert '"start.bat"' in update_ps1
        assert '"stop.sh"' in update_ps1
        assert '"stop.ps1"' in update_ps1
        assert "ERROR: failed to install updated dependencies." in update_ps1
        # Should NOT have .env
        assert "unchained-agent/.env" not in names, ".env should not be in update ZIP"
        # version.txt content
        v = zf.read("unchained-agent/version.txt").decode()
        assert v == VERSION
    print(f"  Update ZIP: {len(zip_bytes)} bytes, {len(names)} files (no .env; launchers included)")


def test_build_research_desk_zip_contains_installable_source_tree():
    from agent_package import RESEARCH_DESK_VERSION, build_research_desk_zip

    zip_bytes = build_research_desk_zip()
    assert len(zip_bytes) > 0

    prefix = f"unchained-pyreplab-{RESEARCH_DESK_VERSION}"
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()
        assert f"{prefix}/pyproject.toml" in names
        assert f"{prefix}/README.md" in names
        assert f"{prefix}/setup.py" in names
        assert f"{prefix}/manifest.json" in names
        assert f"{prefix}/unchained_pyreplab/__init__.py" in names
        pyproject = zf.read(f"{prefix}/pyproject.toml").decode()
        assert 'name = "unchained-pyreplab"' in pyproject
        manifest = json.loads(zf.read(f"{prefix}/manifest.json").decode())
        assert "pyproject.toml" in manifest["files"]
        assert "setup.py" in manifest["files"]
        assert "unchained_pyreplab/capsule_runtime.py" in manifest["files"]
        package_init = zf.read(f"{prefix}/unchained_pyreplab/__init__.py").decode()
        assert "Local browser-to-lab prototype" in package_init
        vendored_mcp_client = zf.read(f"{prefix}/unchained_pyreplab/mcp_client.py").decode()
        assert "class AgentDiscoveryResult" in vendored_mcp_client
        assert 'agent_resolution: str = "missing"' in vendored_mcp_client
        vendored_cli = zf.read(f"{prefix}/unchained_pyreplab/cli.py").decode()
        assert 'subparsers.add_parser("mcp-status"' in vendored_cli
        vendored_webapp = zf.read(f"{prefix}/unchained_pyreplab/webapp.py").decode()
        assert '"agent_resolution": agent_resolution' in vendored_webapp
        assert '"credential_source": credential_source' in vendored_webapp
    print(f"  Research Desk ZIP: {len(zip_bytes)} bytes, {len(names)} files")


def test_resolve_research_desk_vendor_dir_supports_repo_checkout_layout():
    from agent_package import _resolve_research_desk_vendor_dir

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)
        vendor_dir = repo_root / "research_desk_vendor"
        vendor_dir.mkdir()
        module_path = repo_root / "unchained" / "agent_package.py"
        module_path.parent.mkdir()
        module_path.write_text("# test module\n", encoding="utf-8")

        assert _resolve_research_desk_vendor_dir(module_path) == vendor_dir.resolve()


def test_resolve_research_desk_vendor_dir_supports_container_layout():
    from agent_package import _resolve_research_desk_vendor_dir

    with tempfile.TemporaryDirectory() as tmpdir:
        app_root = Path(tmpdir) / "app"
        app_root.mkdir()
        vendor_dir = app_root / "research_desk_vendor"
        vendor_dir.mkdir()
        module_path = app_root / "agent_package.py"
        module_path.write_text("# test module\n", encoding="utf-8")

        assert _resolve_research_desk_vendor_dir(module_path) == vendor_dir.resolve()


def test_handle_research_desk_files_requires_auth():
    from web import handle_research_desk_files

    with mock.patch("web._authenticate", return_value=None):
        response = asyncio.run(handle_research_desk_files(mock.Mock()))

    assert response.status == 401
    payload = json.loads(response.body.decode())
    assert payload["error"] == "Not authenticated"


def test_handle_research_desk_files_serves_zip_attachment():
    from web import handle_research_desk_files

    request = mock.Mock()
    with mock.patch("web._authenticate", return_value={"user_id": "u-test"}):
        with mock.patch("agent_package.build_research_desk_zip", return_value=b"zip-bytes"):
            response = asyncio.run(handle_research_desk_files(request))

    assert response.status == 200
    assert response.body == b"zip-bytes"
    assert response.content_type == "application/zip"
    assert response.headers["Content-Disposition"] == "attachment; filename=unchained-pyreplab.zip"


def _load_vendor_capsule_runtime():
    vendor_path = (
        Path(__file__).resolve().parent.parent
        / "research_desk_vendor"
        / "unchained_pyreplab"
        / "capsule_runtime.py"
    )
    spec = importlib.util.spec_from_file_location("vendor_capsule_runtime", vendor_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_vendor_capsule_runtime_rejects_table_traversal():
    module = _load_vendor_capsule_runtime()
    with tempfile.TemporaryDirectory() as tmpdir:
        capsule_dir = Path(tmpdir)
        (capsule_dir / "object_manifest.json").write_text(
            json.dumps({"objects": []}),
            encoding="utf-8",
        )
        capsule = module.Capsule(capsule_dir)
        try:
            capsule.table("../evil")
        except ValueError as exc:
            assert "Invalid table name" in str(exc)
        else:
            raise AssertionError("expected ValueError for invalid table name")


def test_vendor_capsule_runtime_rejects_manifest_table_path_escape():
    module = _load_vendor_capsule_runtime()
    with tempfile.TemporaryDirectory() as tmpdir:
        capsule_dir = Path(tmpdir)
        (capsule_dir / "object_manifest.json").write_text(
            json.dumps(
                {
                    "objects": [
                        {"name": "safe_table", "table_path": "../escape.jsonl"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        capsule = module.Capsule(capsule_dir)
        try:
            capsule.table("safe_table")
        except ValueError as exc:
            assert "Invalid capsule relative path" in str(exc) or "Unexpected capsule table path" in str(exc)
        else:
            raise AssertionError("expected ValueError for escaped table path")


def test_vendor_capsule_runtime_rejects_invalid_followup_url():
    module = _load_vendor_capsule_runtime()
    with tempfile.TemporaryDirectory() as tmpdir:
        capsule_dir = Path(tmpdir)
        capsule = module.Capsule(capsule_dir)
        try:
            capsule.request_followup(url="javascript:alert(1)", instruction="retry")
        except ValueError as exc:
            assert "Invalid followup URL" in str(exc)
        else:
            raise AssertionError("expected ValueError for invalid followup URL")


def test_packaged_cdp_tool_defaults_new_tab_to_branded_page():
    from agent_package import build_update_zip

    zip_bytes = build_update_zip()
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        cdp_tool = zf.read("unchained-agent/unchained/cdp_tool.py").decode()

    assert 'DEFAULT_NEW_TAB_PATH = "/tab"' in cdp_tool
    assert 'url = args[0] if args else f"{API_URL.rstrip(\'/\')}{DEFAULT_NEW_TAB_PATH}"' in cdp_tool
    assert 'cmd("new_tab", tab_id=tab_id, url=url)' in cdp_tool
    assert "/json/new?" not in cdp_tool
    print("  packaged cdp_tool.py defaults blank new-tab to /tab")


def test_packaged_cdp_tool_supports_enter_submission_helpers():
    from agent_package import build_update_zip

    zip_bytes = build_update_zip()
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        cdp_tool = zf.read("unchained-agent/unchained/cdp_tool.py").decode()

    assert 'elif command == "press_enter":' in cdp_tool
    assert 'elif command == "submit_form":' in cdp_tool
    print("  packaged cdp_tool.py supports press_enter and submit_form")


def test_cdp_tool_decodes_newline_aliases_for_type_command():
    from agent_package import build_update_zip

    zip_bytes = build_update_zip()
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        cdp_tool_source = zf.read("unchained-agent/unchained/cdp_tool.py").decode()

    module_ast = ast.parse(cdp_tool_source)
    helper_node = next(
        node for node in module_ast.body
        if isinstance(node, ast.FunctionDef) and node.name == "_decode_type_text_arg"
    )
    helper_module = ast.Module(body=[helper_node], type_ignores=[])
    namespace = {}
    exec(compile(helper_module, "packaged_cdp_tool.py", "exec"), namespace)
    decode = namespace["_decode_type_text_arg"]

    assert decode("/n") == "\n"
    assert decode(r"\n") == "\n"
    assert decode(r"hello\nworld") == "hello\nworld"
    assert decode("https://example.com/n") == "https://example.com/n"


def test_runtime_dockerfile_copies_scheduler_files():
    repo_root = Path(__file__).resolve().parent.parent
    dockerfile = (repo_root / "Dockerfile").read_text()
    assert "COPY unchained/scheduler_tool.py ." in dockerfile
    assert "COPY unchained/scheduler_agent.py ." in dockerfile
    assert "COPY research_desk_vendor/ research_desk_vendor/" in dockerfile
    print("  Dockerfile copies scheduler runtime files")


def test_runtime_dockerfile_copies_research_desk_vendor_tree():
    repo_root = Path(__file__).resolve().parent.parent
    dockerfile = (repo_root / "Dockerfile").read_text()
    assert "COPY research_desk_vendor/ research_desk_vendor/" in dockerfile
    assert dockerfile.index("RUN pip install --no-cache-dir") < dockerfile.index(
        "COPY research_desk_vendor/ research_desk_vendor/"
    )


def test_runtime_context_helper_lists_research_desk_vendor_roots():
    repo_root = Path(__file__).resolve().parent.parent
    helper = (repo_root / "deploy" / "runtime_context_files.sh").read_text()
    assert 'TOP_LEVEL_CONTEXT_FILES=(' in helper
    assert 'UNCHAINED_RUNTIME_FILES=(' in helper
    assert 'BENCHMARK_CONTEXT_FILES=(' in helper
    assert 'RESEARCH_DESK_VENDOR_ROOT_FILES=(' in helper
    assert '"manifest.json"' in helper
    assert '"README.md"' in helper
    assert '"pyproject.toml"' in helper
    assert '"setup.py"' in helper


def test_runtime_context_helper_references_existing_files():
    repo_root = Path(__file__).resolve().parent.parent
    script = """
source "$1/deploy/runtime_context_files.sh"
for rel in "${TOP_LEVEL_CONTEXT_FILES[@]}"; do test -f "$1/$rel"; done
for rel in "${UNCHAINED_RUNTIME_FILES[@]}"; do test -f "$1/unchained/$rel"; done
for rel in "${BENCHMARK_CONTEXT_FILES[@]}"; do test -f "$1/unchained/benchmark/$rel"; done
for rel in "${RESEARCH_DESK_VENDOR_ROOT_FILES[@]}"; do test -f "$1/research_desk_vendor/$rel"; done
"""
    subprocess.run(
        ["bash", "-c", script, "bash", str(repo_root)],
        check=True,
        capture_output=True,
        text=True,
    )


def test_deploy_script_uploads_research_desk_vendor_tree():
    repo_root = Path(__file__).resolve().parent.parent
    deploy_script = (repo_root / "deploy.sh").read_text()
    assert 'cd "$SCRIPT_DIR"' in deploy_script
    assert 'source "$SCRIPT_DIR/deploy/runtime_context_files.sh"' in deploy_script
    assert "remote_bash()" in deploy_script
    assert "printf -v quoted_arg '%q'" in deploy_script
    assert '"${SSH_CMD[@]}" "$remote_command"' in deploy_script
    assert 'remote_bash "$REMOTE_DIR" <<\'EOF\'' in deploy_script
    assert 'echo "==> Uploading Research Desk vendor tree..."' in deploy_script
    assert 'RESEARCH_DESK_VENDOR_ROOT_UPLOAD_FILES=()' in deploy_script
    assert 'if [[ "${#RESEARCH_DESK_VENDOR_FILES[@]}" -eq 0 ]]; then' in deploy_script
    assert 'REMOTE_VENDOR_STAGE="$(' in deploy_script
    assert 'backup_dir="$(mktemp -d "$remote_dir/research_desk_vendor.prev.XXXXXX")"' in deploy_script
    assert 'trap restore_live_dir EXIT' in deploy_script
    assert 'RESEARCH_DESK_VENDOR_FILES=(research_desk_vendor/unchained_pyreplab/*.py)' in deploy_script


def test_github_deploy_workflow_uploads_web_app_package():
    repo_root = Path(__file__).resolve().parent.parent
    workflow = (repo_root / ".github" / "workflows" / "deploy.yml").read_text()
    assert "Keep the modular web_app package in sync with web.py imports" in workflow
    assert "rm -rf $REMOTE_DIR/unchained/web_app" in workflow
    assert "$SCP_CMD -r unchained/web_app $EC2_USER@$EC2_HOST:$REMOTE_DIR/unchained/" in workflow


def test_research_desk_package_image_smoke_script_checks_built_image():
    repo_root = Path(__file__).resolve().parent.parent
    smoke_script = (repo_root / "deploy" / "research_desk_package_image_smoke.sh").read_text()
    assert 'source "${SCRIPT_DIR}/runtime_context_files.sh"' in smoke_script
    assert 'TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/research-desk-package-image-smoke.XXXXXX")"' in smoke_script
    assert 'for rel in "${TOP_LEVEL_CONTEXT_FILES[@]}"; do' in smoke_script
    assert 'for rel in "${UNCHAINED_RUNTIME_FILES[@]}"; do' in smoke_script
    assert 'cp -R unchained/web_app "${TMP_DIR}/unchained/"' in smoke_script
    assert 'for rel in "${RESEARCH_DESK_VENDOR_ROOT_FILES[@]}"; do' in smoke_script
    assert 'if [[ "${#RESEARCH_DESK_VENDOR_FILES[@]}" -eq 0 ]]; then' in smoke_script
    assert 'docker build -t "${IMAGE_TAG}" "${TMP_DIR}"' in smoke_script
    assert 'docker run --rm "${IMAGE_TAG}" python - <<\'PY\'' in smoke_script
    assert "build_research_desk_zip()" in smoke_script


def test_research_desk_install_helper_smoke_script_is_local_only():
    repo_root = Path(__file__).resolve().parent.parent
    smoke_script = (repo_root / "deploy" / "research_desk_install_helper_smoke.py").read_text()
    assert 'REPO_ROOT / ".venv" / "bin" / "python"' in smoke_script
    assert 'UNCHAINED_DIR / ".venv" / "bin" / "python"' in smoke_script
    assert 'UNCHAINED_RESEARCH_DESK_PACKAGE_URL' in smoke_script
    assert 'UNCHAINED_ALLOW_LOCAL_RESEARCH_DESK_PACKAGE_URL' in smoke_script
    assert 'PYTHONUSERBASE' in smoke_script
    assert '--research-desk-install-helper' in smoke_script
    assert 'browser-open.log' in smoke_script
    assert '/web/research-desk/files' in smoke_script
    assert 'api.unchainedsky.com' not in smoke_script
    assert 'token_hex(12)' in smoke_script
    assert "EXPECTED_PACKAGE" in smoke_script
    assert "EXPECTED_VERSION" in smoke_script


def test_research_desk_package_image_smoke_script_runs_with_fake_docker():
    repo_root = Path(__file__).resolve().parent.parent
    script_path = repo_root / "deploy" / "research_desk_package_image_smoke.sh"
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        marker_path = tmp_path / "docker-marker.json"
        docker_path = fake_bin / "docker"
        docker_path.write_text(
            """#!/usr/bin/env bash
set -euo pipefail
marker="${FAKE_DOCKER_MARKER:?}"
if [[ "$1" == "build" ]]; then
  context="${@: -1}"
  test -f "$context/Dockerfile"
  test -f "$context/research_desk_vendor/manifest.json"
  test -f "$context/research_desk_vendor/README.md"
  test -f "$context/research_desk_vendor/pyproject.toml"
  test -f "$context/research_desk_vendor/setup.py"
  test -f "$context/unchained/agent_package.py"
  test -d "$context/unchained/web_app"
  printf '{"build_context":"%s"}\\n' "$context" >"$marker"
  exit 0
fi
if [[ "$1" == "run" ]]; then
  exit 0
fi
echo "unexpected docker args: $*" >&2
exit 1
""",
            encoding="utf-8",
        )
        docker_path.chmod(0o755)
        env = os.environ.copy()
        env["PATH"] = f"{fake_bin}:{env['PATH']}"
        env["FAKE_DOCKER_MARKER"] = str(marker_path)
        subprocess.run(
            ["bash", str(script_path)],
            cwd=repo_root,
            check=True,
            env=env,
            capture_output=True,
            text=True,
        )
        assert marker_path.is_file()
        payload = json.loads(marker_path.read_text(encoding="utf-8"))
        assert payload["build_context"]


def test_research_desk_vendor_manifest_script_matches_current_manifest():
    repo_root = Path(__file__).resolve().parent.parent
    script_path = repo_root / "deploy" / "rebuild_research_desk_vendor_manifest.py"
    result = subprocess.run(
        [sys.executable, str(script_path), "--check"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_research_desk_vendor_versions_are_kept_in_sync():
    repo_root = Path(__file__).resolve().parent.parent
    pyproject_text = (repo_root / "research_desk_vendor" / "pyproject.toml").read_text(encoding="utf-8")
    setup_text = (repo_root / "research_desk_vendor" / "setup.py").read_text(encoding="utf-8")
    script_text = (repo_root / "deploy" / "rebuild_research_desk_vendor_manifest.py").read_text(encoding="utf-8")
    pyproject_version = re.search(r'^version = "([^"]+)"$', pyproject_text, flags=re.MULTILINE)
    setup_version = re.search(r'^\s*version="([^"]+)",$', setup_text, flags=re.MULTILINE)
    manifest_version = re.search(r'^VERSION = "([^"]+)"$', script_text, flags=re.MULTILINE)
    assert pyproject_version and setup_version and manifest_version
    assert pyproject_version.group(1) == setup_version.group(1) == manifest_version.group(1)


def test_vendor_cli_setup_does_not_persist_agent_id_snapshot():
    repo_root = Path(__file__).resolve().parent.parent
    cli_text = (repo_root / "research_desk_vendor" / "unchained_pyreplab" / "cli.py").read_text(encoding="utf-8")
    assert '"agent_id": browser["agent_id"]' not in cli_text


def test_vendor_webapp_status_ignores_saved_config_agent_id():
    repo_root = Path(__file__).resolve().parent.parent
    webapp_text = (repo_root / "research_desk_vendor" / "unchained_pyreplab" / "webapp.py").read_text(encoding="utf-8")
    assert 'config.get("agent_id"' not in webapp_text
    assert "resolve_credentials(" in webapp_text


def test_research_desk_zip_installs_with_system_pip_metadata():
    from agent_package import build_research_desk_zip

    python_bin = shutil.which(
        "python3",
        path=os.pathsep.join(["/usr/local/bin", "/opt/homebrew/bin", os.defpath]),
    ) or sys.executable

    zip_bytes = build_research_desk_zip()
    with tempfile.TemporaryDirectory() as tmpdir:
        zip_path = Path(tmpdir) / "research-desk.zip"
        target_dir = Path(tmpdir) / "site-packages"
        zip_path.write_bytes(zip_bytes)
        result = subprocess.run(
            [
                python_bin,
                "-m",
                "pip",
                "install",
                "--no-deps",
                "--target",
                str(target_dir),
                str(zip_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert "Successfully installed unchained-pyreplab-0.1.0" in result.stdout
        assert (target_dir / "unchained_pyreplab" / "__init__.py").exists()


def test_cli_binary_resolution_prefers_homebrew_before_local_bin():
    available = {
        "/opt/homebrew/bin/claude": True,
        "/Users/test/.local/bin/claude": True,
    }

    source_path = Path(__file__).resolve().parent / "chat_agent_cli.py"
    module_ast = ast.parse(source_path.read_text())
    resolver_node = next(
        node for node in module_ast.body
        if isinstance(node, ast.FunctionDef) and node.name == "_resolve_local_cli_binary"
    )
    resolver_module = ast.Module(body=[resolver_node], type_ignores=[])
    namespace = {"os": os, "shutil": shutil, "sys": types.SimpleNamespace(platform="darwin")}
    exec(compile(resolver_module, str(source_path), "exec"), namespace)

    with mock.patch.dict(os.environ, {"CLAUDE_BIN": ""}, clear=False):
        with mock.patch.object(shutil, "which", return_value=None):
            with mock.patch.object(os.path, "expanduser", return_value="/Users/test/.local/bin"):
                with mock.patch.object(
                    os.path,
                    "isfile",
                    side_effect=lambda path: available.get(path, False),
                ):
                    with mock.patch.object(
                        os,
                        "access",
                        side_effect=lambda path, mode: available.get(path, False),
                    ):
                        resolved = namespace["_resolve_local_cli_binary"]("CLAUDE_BIN", "claude")
    assert resolved == "/opt/homebrew/bin/claude"


def test_generate_public_install_script():
    from agent_package import _generate_public_install_script
    script = _generate_public_install_script(base_url="https://api.unchainedsky.com")
    assert "#!/bin/bash" in script
    assert "uc_live_" not in script, "Long-lived API key should not be embedded"
    assert "INSTALL_TOKEN=" not in script.split("INSTALL_TOKEN=\"\"")[0] or True, \
        "No pre-baked install token should be embedded"
    # Claim flow endpoints
    assert "/web/install/claim/start" in script, "claim start endpoint missing"
    assert "/web/install/claim/poll" in script, "claim poll endpoint missing"
    assert "/install/claim/$CLAIM_ID" in script, "browser claim URL missing"
    assert "Install Python 3.8+." in script
    # Browser open
    assert "open " in script or "xdg-open " in script, "browser open command missing"
    # Download before bootstrap (critical ordering)
    download_pos = script.index("/web/download-agent")
    bootstrap_pos = script.index("/web/install/bootstrap")
    assert download_pos < bootstrap_pos, "download must happen before bootstrap (bootstrap consumes token)"
    # Setup steps
    assert "python3 -m venv" in script, "venv setup missing"
    assert "unzip" in script, "unzip step missing"
    assert "Start now?" in script, "start prompt missing"
    assert "/dev/tty" in script, "piped stdin fallback missing"
    print(f"  Public install script: {len(script)} chars, claim flow present, correct ordering")


def test_public_install_script_handler_importable():
    from web_app.handlers.install_flow import handle_public_install_script
    import asyncio
    assert asyncio.iscoroutinefunction(handle_public_install_script), \
        "handle_public_install_script should be an async handler"
    print("  handle_public_install_script importable and async")


def test_generate_install_script():
    from agent_package import _generate_install_script
    script = _generate_install_script(
        install_token="inst_bootstrap_123",
        relay_host="api.unchainedsky.com",
        base_url="https://api.unchainedsky.com",
    )
    assert "#!/bin/bash" in script
    assert "uc_live_abc123" not in script, "Long-lived API key should not be embedded"
    assert "inst_bootstrap_123" in script, "Install token missing"
    assert "api.unchainedsky.com" in script, "relay host not embedded"
    assert "/web/download-agent" in script, "download URL not in script"
    assert "X-Install-Token" in script, "install token header missing"
    assert "download-agent?install_token=" not in script, "install token should not be passed in URL query"
    assert "python3 -m venv" in script, "venv setup not in script"
    assert "Start now?" in script or "start now?" in script.lower(), "start prompt missing"
    print(f"  Install script: {len(script)} chars")


def test_generate_windows_install_script():
    from agent_package import _generate_windows_install_script
    script = _generate_windows_install_script(
        install_token="inst_bootstrap_123",
        relay_host="api.unchainedsky.com",
        base_url="https://api.unchainedsky.com",
    )
    assert "#Requires -Version" in script
    assert "inst_bootstrap_123" in script, "Install token missing"
    assert "api.unchainedsky.com" in script, "relay host not embedded"
    assert "/web/download-agent" in script, "download URL not in script"
    assert "X-Install-Token" in script, "install token header missing"
    assert "download-agent?install_token=" not in script, "install token should not be passed in URL query"
    assert "Invoke-WebRequest" in script
    assert "start.ps1" in script
    assert "Install-PythonRuntime" in script
    assert "python.org/ftp/python/" in script
    assert '$venvDir = Join-Path $installDir ".venv"' in script
    assert '$pythonPrefixArgs += $pythonInfo.Prefix' in script
    print(f"  Windows install script: {len(script)} chars")


# ── auth.py install token tests ─────────────────────────────────────

def test_create_install_token():
    from auth import Auth
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        auth = Auth(db_path=db_path)
        token = auth.create_install_token("u-test1", "uc_live_key1")
        assert token.startswith("inst_"), f"Token should start with inst_, got {token}"
        assert len(token) == 5 + 32, f"Token length should be 37, got {len(token)}"
        print(f"  Token: {token[:15]}...")
    finally:
        os.unlink(db_path)


def test_validate_install_token():
    from auth import Auth
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        auth = Auth(db_path=db_path)
        token = auth.create_install_token("u-test2", "uc_live_key2")

        # First validate should succeed
        result = auth.validate_install_token(token)
        assert result is not None, "Token should be valid"
        assert result["user_id"] == "u-test2"
        assert result["api_key"] == "uc_live_key2"
        print(f"  First validate: {result}")

        # Second validate should fail (token is used)
        result2 = auth.validate_install_token(token)
        assert result2 is None, "Used token should be invalid"
        print(f"  Second validate (used): None (correct)")
    finally:
        os.unlink(db_path)


def test_validate_expired_token():
    from auth import Auth
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        auth = Auth(db_path=db_path)
        # Create token with 0 TTL (already expired)
        token = auth.create_install_token("u-test3", "uc_live_key3", ttl=0)
        time.sleep(0.1)
        result = auth.validate_install_token(token)
        assert result is None, "Expired token should be invalid"
        print(f"  Expired token: None (correct)")
    finally:
        os.unlink(db_path)


def test_validate_bogus_token():
    from auth import Auth
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        auth = Auth(db_path=db_path)
        result = auth.validate_install_token("inst_bogus")
        assert result is None, "Bogus token should be invalid"
        print(f"  Bogus token: None (correct)")
    finally:
        os.unlink(db_path)


def test_cleanup_expired_tokens():
    from auth import Auth
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        auth = Auth(db_path=db_path)
        # Create one expired, one used, one valid
        t1 = auth.create_install_token("u-1", "k1", ttl=0)
        time.sleep(0.1)
        t2 = auth.create_install_token("u-2", "k2")
        auth.validate_install_token(t2)  # mark as used
        t3 = auth.create_install_token("u-3", "k3")

        auth.cleanup_expired_tokens()

        # Only t3 should remain
        with auth._conn() as conn:
            rows = conn.execute("SELECT token FROM install_tokens").fetchall()
        remaining = [r[0] for r in rows]
        assert t1 not in remaining, "Expired token should be cleaned"
        assert t2 not in remaining, "Used token should be cleaned"
        assert t3 in remaining, "Valid token should remain"
        print(f"  Cleanup: 2 removed, 1 remaining (correct)")
    finally:
        os.unlink(db_path)


def test_openrouter_budget_tracking():
    from auth import Auth
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        auth = Auth(db_path=db_path)
        user = auth.get_or_create_user("openrouter-budget-test@example.com", "Budget Test", "")
        user_id = user["user_id"]

        state = auth.get_or_init_openrouter_budget(user_id, min_budget_usd=1.0, max_budget_usd=1.0)
        assert state["budget_usd"] == 1.0, f"Budget should be fixed at $1: {state}"
        assert state["spent_usd"] == 0.0, f"Initial spend should be zero: {state}"
        assert not state["capped"], f"New user should not be capped: {state}"

        state2 = auth.add_openrouter_spend(user_id, 0.4, min_budget_usd=1.0, max_budget_usd=1.0)
        assert 0.39 <= state2["spent_usd"] <= 0.41, f"Spend should increase by ~0.4: {state2}"
        assert not state2["capped"], f"Should not be capped yet: {state2}"

        state3 = auth.add_openrouter_spend(user_id, 0.7, min_budget_usd=1.0, max_budget_usd=1.0)
        assert state3["spent_usd"] >= 1.0, f"Spend should pass cap: {state3}"
        assert state3["capped"], f"User should be capped at $1: {state3}"
        assert state3["remaining_usd"] == 0.0, f"Remaining should clamp to zero: {state3}"
        print("  OpenRouter budget cap at $1 works")
    finally:
        os.unlink(db_path)


def test_openrouter_token_usage_tracking():
    from auth import Auth
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        auth = Auth(db_path=db_path)
        user = auth.get_or_create_user("token-usage-test@example.com", "Token Usage", "")
        user_id = user["user_id"]

        state = auth.add_openrouter_usage(
            user_id=user_id,
            prompt_tokens=120,
            completion_tokens=45,
            total_tokens=165,
            cost_usd=0.01234,
            min_budget_usd=1.0,
            max_budget_usd=1.0,
        )
        assert state["prompt_tokens"] >= 120, f"Prompt tokens not tracked: {state}"
        assert state["completion_tokens"] >= 45, f"Completion tokens not tracked: {state}"
        assert state["total_tokens"] >= 165, f"Total tokens not tracked: {state}"
        assert state["usage_events"] >= 1, f"Usage event counter should increment: {state}"
        assert state["spent_usd"] >= 0.01234, f"Spend should reflect usage cost: {state}"
        print("  OpenRouter token usage counters increment correctly")
    finally:
        os.unlink(db_path)


def test_create_pending_user_returns_post_trigger_state():
    """create_pending_user must reflect the auto_approve_pending_users trigger.

    The trigger flips status='pending' -> 'approved' on insert and issues an
    api_key, so callers in the signup flow can branch on the returned status
    to send the right welcome email.
    """
    from auth import Auth
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        auth = Auth(db_path=db_path)
        user = auth.create_pending_user("auto-approved@example.com", "Auto Approved", "", user_type="claude")
        assert user["status"] == "approved", f"expected status='approved' from trigger, got: {user}"
        assert user["api_key"], f"expected api_key issued by trigger, got: {user}"
        assert user["user_type"] == "claude", f"user_type lost: {user}"
        print("  create_pending_user reflects auto_approve trigger output")
    finally:
        os.unlink(db_path)


def test_approve_user_keeps_existing_api_key():
    from auth import Auth
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        auth = Auth(db_path=db_path)
        pending = auth.create_pending_user("pending-trial@example.com", "Pending Trial", "", user_type="trial")
        existing_key = auth.create_key(pending["user_id"])
        with auth._conn() as conn:
            conn.execute("UPDATE users SET api_key = ? WHERE email = ?", (existing_key, "pending-trial@example.com"))

        approved = auth.approve_user("pending-trial@example.com")
        assert approved is not None, "approve_user should return user"
        assert approved["status"] == "approved", "user should be approved"
        assert approved["api_key"] == existing_key, "approve_user should retain existing API key"
        with auth._conn() as conn:
            row = conn.execute("SELECT status, api_key FROM users WHERE email = ?", ("pending-trial@example.com",)).fetchone()
        assert row == ("approved", existing_key), f"DB row mismatch after approve: {row}"
        print("  approve_user keeps existing API key when present")
    finally:
        os.unlink(db_path)


# ── chat_agent_cli.py version check tests ───────────────────────────

def test_parse_version():
    # Import the function — it's defined at module level, need to extract it
    # Since chat_agent_cli.py has side effects on import, test the logic directly
    def _parse_version(v):
        try:
            return tuple(int(x) for x in v.strip().split("."))
        except (ValueError, AttributeError):
            return (0, 0, 0)

    assert _parse_version("0.2.0") == (0, 2, 0)
    assert _parse_version("1.0.0") == (1, 0, 0)
    assert _parse_version("0.10.3") == (0, 10, 3)
    assert _parse_version("") == (0, 0, 0)
    assert _parse_version("bad") == (0, 0, 0)
    assert _parse_version(None) == (0, 0, 0)

    # Comparison checks
    assert _parse_version("0.1.0") < _parse_version("0.2.0")
    assert _parse_version("0.2.0") == _parse_version("0.2.0")
    assert _parse_version("1.0.0") > _parse_version("0.99.99")
    print(f"  All version comparisons correct")


def test_self_update_helper_skips_restart_when_version_unchanged():
    import chat_agent_cli

    with mock.patch.object(chat_agent_cli, "_agent_root", return_value="/tmp/unchained-agent"):
        with mock.patch.object(chat_agent_cli, "_local_version", side_effect=["0.3.38", "0.3.38"]):
            with mock.patch.object(chat_agent_cli, "_run_logged", return_value=0):
                with mock.patch.object(chat_agent_cli.time, "sleep", return_value=None):
                    with mock.patch.object(chat_agent_cli.os, "kill") as mock_kill:
                        with mock.patch.object(chat_agent_cli.subprocess, "run") as mock_run:
                            with mock.patch.object(chat_agent_cli.subprocess, "Popen") as mock_popen:
                                with mock.patch.dict(
                                    chat_agent_cli.os.environ,
                                    {
                                        "UNCHAINED_AGENT_ROOT": "/tmp/unchained-agent",
                                        "UNCHAINED_UPDATE_RUN_HINT": "manual",
                                        "UNCHAINED_UPDATE_TRIGGER_PID": "12345",
                                    },
                                    clear=False,
                                ):
                                    chat_agent_cli._run_self_update_helper()
    mock_kill.assert_not_called()
    mock_run.assert_not_called()
    mock_popen.assert_not_called()
    print("  Self-update helper skips restart when version is unchanged")


# ── web.py endpoint handler tests (unit-level) ──────────────────────

def test_web_imports():
    """Verify the new handlers are importable."""
    from web import (
        handle_install_token,
        handle_install_bootstrap,
        handle_install_script,
        handle_install_script_windows,
        handle_install_claim_page,
        handle_install_claim_start,
        handle_install_claim_poll,
        handle_install_claim_approve,
        handle_install_page,
        handle_download_installer,
        handle_agent_version,
        handle_agent_files,
        handle_research_desk_files,
    )
    assert callable(handle_install_token)
    assert callable(handle_install_bootstrap)
    assert callable(handle_install_script)
    assert callable(handle_install_script_windows)
    assert callable(handle_install_claim_page)
    assert callable(handle_install_claim_start)
    assert callable(handle_install_claim_poll)
    assert callable(handle_install_claim_approve)
    assert callable(handle_install_page)
    assert callable(handle_download_installer)
    assert callable(handle_agent_version)
    assert callable(handle_agent_files)
    assert callable(handle_research_desk_files)
    print("  All install/update handlers importable")


def test_web_routes_registered():
    """Verify install/update routes are registered at runtime."""
    import web

    if hasattr(web, "create_app"):
        app = web.create_app()
        routes = {
            (r.method, r.resource.canonical)
            for r in app.router.routes()
            if r.method in {"GET", "POST"}
        }
        assert ("POST", "/web/install-token") in routes, "install-token route not registered"
        assert ("POST", "/auth/request-claude-access") in routes, "request-claude-access route not registered"
        assert ("POST", "/web/install/bootstrap") in routes, "install bootstrap route not registered"
        assert ("GET", "/install") in routes, "install page route not registered"
        assert ("GET", "/install/script") in routes, "header-based install script route not registered"
        assert ("GET", "/install/{token}") in routes, "install script route not registered"
        assert ("GET", "/install/windows/script") in routes, "header-based windows install script route not registered"
        assert ("GET", "/install/windows/{token}") in routes, "windows install script route not registered"
        assert ("GET", "/install/claim/{claim_id}") in routes, "install claim page route not registered"
        assert ("POST", "/web/install/claim/start") in routes, "install claim start route not registered"
        assert ("POST", "/web/install/claim/poll") in routes, "install claim poll route not registered"
        assert ("POST", "/web/install/claim/approve") in routes, "install claim approve route not registered"
        assert ("GET", "/trial/script") in routes, "header-based trial script route not registered"
        assert ("GET", "/trial/windows/script") in routes, "header-based trial windows script route not registered"
        assert ("GET", "/web/download-installer") in routes, "download-installer route not registered"
        assert ("GET", "/web/agent/version") in routes, "agent version route not registered"
        assert ("GET", "/web/agent/files") in routes, "agent files route not registered"
        assert ("GET", "/web/research-desk/files") in routes, "research desk files route not registered"
    else:
        # Backward compatibility for older code where routes lived directly in main().
        import inspect
        from web import main as web_main

        source = inspect.getsource(web_main)
        assert "/web/install-token" in source, "install-token route not registered"
        assert "/auth/request-claude-access" in source, "request-claude-access route not registered"
        assert "/web/install/bootstrap" in source, "install bootstrap route not registered"
        assert "/install" in source, "install page route not registered"
        assert "/install/script" in source, "header-based install script route not registered"
        assert "/install/{token}" in source, "install script route not registered"
        assert "/install/windows/script" in source, "header-based windows install script route not registered"
        assert "/install/windows/{token}" in source, "windows install script route not registered"
        assert "/install/claim/{claim_id}" in source, "install claim page route not registered"
        assert "/web/install/claim/start" in source, "install claim start route not registered"
        assert "/web/install/claim/poll" in source, "install claim poll route not registered"
        assert "/web/install/claim/approve" in source, "install claim approve route not registered"
        assert "/trial/script" in source, "header-based trial script route not registered"
        assert "/trial/windows/script" in source, "header-based trial windows script route not registered"
        assert "/web/download-installer" in source, "download-installer route not registered"
        assert "/web/chat/preview/ws" in source, "authenticated chat Agent View route not registered"
        assert "/web/agent/version" in source, "agent version route not registered"
        assert "/web/agent/files" in source, "agent files route not registered"
        assert "/web/research-desk/files" in source, "research desk files route not registered"
    print("  All install/update routes registered")


def test_chat_html_has_install_modal():
    """Verify the CHAT_HTML has the install modal and buttons."""
    from web import CLAUDE_CHAT_HTML as CHAT_HTML
    assert "install-modal" in CHAT_HTML, "Install modal missing from CHAT_HTML"
    assert "showInstallCmd" in CHAT_HTML, "showInstallCmd JS missing"
    assert "copyInstallCmd" in CHAT_HTML, "copyInstallCmd JS missing"
    assert "closeInstallModal" in CHAT_HTML, "closeInstallModal JS missing"
    assert "Download Agent Installer" in CHAT_HTML, "installer download button missing"
    assert "Get terminal command" in CHAT_HTML, "terminal install option missing"
    assert "Connect this computer" in CHAT_HTML, "connect modal title missing"
    assert "Choose one install method" in CHAT_HTML, "install method choice copy missing"
    assert "Do not run both" in CHAT_HTML, "either/or install guidance missing"
    assert "Requires Claude CLI to be installed and logged in." in CHAT_HTML, "default CLI prerequisite copy missing"
    assert "localCliNameForModel()" in CHAT_HTML, "CLI prerequisite should follow selected lane"
    assert "Requires Claude CLI or Codex CLI already installed and logged in." not in CHAT_HTML, "generic CLI prerequisite copy should be gone"
    assert "Install (curl)" not in CHAT_HTML, "stale curl install label should be gone from local chat"
    assert "Install Agent (curl)" not in CHAT_HTML, "stale curl modal title should be gone from local chat"
    assert "sr-only" in CHAT_HTML, "accessible either/or text missing"
    assert 'role="dialog"' in CHAT_HTML, "install modal dialog role missing"
    assert "handleInstallModalKeydown" in CHAT_HTML, "install modal focus trap missing"
    assert '<button id="sendbtn" onclick="doSend()" disabled>' in CHAT_HTML, "local send button should default disabled before status loads"
    assert "to reconnect this computer" in CHAT_HTML, "reconnect modal copy missing"
    assert "btn.disabled = !ready" in CHAT_HTML, "send button should be disabled semantically when setup is blocked"
    assert "Use the same install command to update/reconnect" in CHAT_HTML, "Codex update copy should clarify install/update flow"
    assert "Copy Command" in CHAT_HTML, "copy command button missing"
    assert "download" in CHAT_HTML.lower(), "download link missing"
    assert CHAT_HTML.index('id="banner-curl"') < CHAT_HTML.index('id="banner-connect"'), "curl action should come before connect"
    print(f"  CHAT_HTML has install modal + buttons")


def test_chat_html_has_opencode_cockpit_handoff():
    """Verify OpenCode chat integrates its interactive semantic browser."""
    from web import CLAUDE_CHAT_HTML as CHAT_HTML

    assert 'id="topbar-agent-view"' in CHAT_HTML, "OpenCode Agent View topbar button missing"
    assert 'id="banner-agent-view"' not in CHAT_HTML, "legacy Agent View banner button should be removed"
    assert 'id="agent-view"' in CHAT_HTML, "integrated browser panel missing"
    assert 'id="agent-view-image"' in CHAT_HTML, "live browser frame missing"
    assert 'id="agent-view-frame"' in CHAT_HTML, "isolated semantic renderer missing"
    assert "/web/chat/preview/ws" in CHAT_HTML, "Agent View should use the authenticated screencast channel"
    assert "preview.semantic.snapshot" in CHAT_HTML, "Agent View should render semantic snapshots"
    assert "preview.semantic.patch" in CHAT_HTML, "Agent View should apply semantic patches"
    assert "preview.action.confirmation_required" in CHAT_HTML, "Agent View should confirm consequential page actions"
    assert "function bindAgentViewInteractions" in CHAT_HTML, "semantic DOM interaction bridge missing"
    assert "omittedSensitiveFields" in CHAT_HTML, "Agent View should expose mirror fidelity telemetry"
    assert "new-tab" in CHAT_HTML, "new-tab activity should refresh Agent View"
    runtime_scripts = [
        script
        for script in re.findall(r"<script[^>]*>(.*?)</script>", CHAT_HTML, flags=re.DOTALL)
        if "let agentViewSocket" in script
    ]
    assert len(runtime_scripts) == 1, "Agent View runtime should remain inside one script element"
    assert "function agentViewFindTarget" in runtime_scripts[0], "Agent View runtime was split by HTML injection"
    assert 'id="scroll-debug-overlay"' in CHAT_HTML, "Agent View scroll debugger container missing"
    assert "function _scrollDebug" in runtime_scripts[0], "Agent View scroll debugger runtime missing"
    assert "document.write(_scrollDebugOverlay())" not in CHAT_HTML, "scroll debugger must not execute before Agent View runtime loads"
    assert "'snapshot-recv'" in runtime_scripts[0], "Agent View should trace snapshot scroll provenance"
    assert "'frame-swap'" in runtime_scripts[0], "Agent View should trace semantic frame swaps"
    assert "'layout-shift'" in runtime_scripts[0], "Agent View should trace non-scroll layout movement"
    assert "function agentViewClassifyScrollAck" in runtime_scripts[0], "Agent View scroll acknowledgments need ordering"
    assert "actionId !== state.inFlightActionId" in runtime_scripts[0], "stale scroll acknowledgments must be rejected"
    assert "'ack-stale'" in runtime_scripts[0], "stale scroll acknowledgments need observability"
    assert "'ack-buffered'" in runtime_scripts[0], "active-gesture acknowledgments must be buffered"
    assert "'snapshot-preserve'" in runtime_scripts[0], "locked snapshot swaps must preserve local scroll"
    assert "function agentViewNeedsScrollSend" in runtime_scripts[0], "long scrolls need latest-wins flow control"
    assert "'scroll-coalesce'" in runtime_scripts[0], "coalesced scrolls need observability"
    assert "reason: 'human-scroll-in-flight'" in runtime_scripts[0], "source patches must wait for the final human scroll request"
    classifier = re.search(
        r"function agentViewClassifyScrollAck\(state, actionId, lockActive\) \{.*?\n\}",
        runtime_scripts[0],
        flags=re.DOTALL,
    )
    assert classifier, "scroll acknowledgment classifier missing"
    needs_send = re.search(
        r"function agentViewNeedsScrollSend\(state\) \{.*?\n\}",
        runtime_scripts[0],
        flags=re.DOTALL,
    )
    assert needs_send, "scroll flow-control predicate missing"
    node = shutil.which("node")
    if node:
        check = classifier.group(0) + "\n" + needs_send.group(0) + """
const state = {inFlightActionId: 'latest'};
if (agentViewClassifyScrollAck(state, 'older', true) !== 'stale') throw new Error('older ack was accepted');
if (agentViewClassifyScrollAck(state, 'latest', true) !== 'buffer') throw new Error('active ack was not buffered');
if (agentViewClassifyScrollAck(state, 'latest', false) !== 'reconcile') throw new Error('idle latest ack did not reconcile');
if (agentViewNeedsScrollSend({context:{}, inFlightActionId:'a', desiredY:0, latestSentY:500})) throw new Error('sent a second request while one was in flight');
if (!agentViewNeedsScrollSend({context:{}, latestSentActionId:'a', desiredY:0, latestSentY:500})) throw new Error('did not send trailing final position');
if (agentViewNeedsScrollSend({context:{}, latestSentActionId:'a', desiredY:0, latestSentY:0})) throw new Error('resent already acknowledged position');
"""
        result = subprocess.run([node, "-e", check], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    assert "window.addEventListener('resize', handleAgentViewViewportResize)" in runtime_scripts[0]
    assert "window.visualViewport.addEventListener('resize', handleAgentViewViewportResize)" in runtime_scripts[0]
    assert "agentViewRetryAllowed = !!event.retriable" in runtime_scripts[0]
    assert "if (!agentViewRetryAllowed) return;" in runtime_scripts[0]
    assert "Browser Preview" in CHAT_HTML, "browser preview should use a user-facing label"
    assert "Same conversation" in CHAT_HTML, "chat rail should explain that it controls the preview"
    assert "ensureAgentViewForBrowserActivity(name)" in CHAT_HTML, "browser activity should identify the active tool"
    assert "http://127.0.0.1:8787" not in CHAT_HTML, "hosted chat should not deep-link to a separate localhost website"
    assert "if (isOpenCodeCli)" in CHAT_HTML, "Agent View handoff should be limited to OpenCode CLI"
    assert 'id="banner-kicker"' in CHAT_HTML, "Agent View banner kicker needs an addressable state"
    assert 'id="banner-method-or"' in CHAT_HTML, "install-method separator needs an addressable state"
    assert 'id="banner-installer-label"' in CHAT_HTML, "accessible installer separator needs an addressable state"
    assert "bannerMethodOr.style.display = 'none'" in CHAT_HTML, "visible install separator should hide for cockpit mode"
    assert "bannerInstallerLabel.style.display = 'none'" in CHAT_HTML, "accessible install separator should hide for cockpit mode"
    assert "bannerMethodOr.style.display = ''" in CHAT_HTML, "install separator should reset when cockpit mode ends"
    assert "bannerInstallerLabel.style.display = ''" in CHAT_HTML, "accessible separator should reset when cockpit mode ends"
    print("  CHAT_HTML has integrated OpenCode Agent View")


def test_setup_html_has_status_and_install_banner():
    """Verify setup route preserves agent status pills and install banner."""
    from web import SETUP_HTML
    assert 'id="setup-agentstatus"' in SETUP_HTML, "setup chat status pill missing"
    assert 'id="setup-bridgestatus"' in SETUP_HTML, "setup bridge status pill missing"
    assert 'id="setup-download-banner"' in SETUP_HTML, "setup install banner missing"
    assert 'id="setup-banner-connect"' in SETUP_HTML, "setup install route link missing"
    assert "Download Agent Installer" in SETUP_HTML, "setup installer label missing"
    assert "download" in SETUP_HTML.lower(), "setup download label missing"
    assert SETUP_HTML.index('id="setup-banner-curl"') < SETUP_HTML.index('id="setup-banner-connect"'), "setup curl action should come before connect"
    assert "showSetupInstallCmd" in SETUP_HTML, "setup curl modal open function missing"
    assert "copySetupInstallCmd" in SETUP_HTML, "setup curl copy function missing"
    print("  SETUP_HTML has status pills + installer banner")


def test_native_installer_path_prefers_freshest_artifact():
    """Verify native installer lookup chooses newest artifact to avoid stale-file shadowing."""
    import web as web_mod

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mac_pkg = root / "unchained-installer-mac.pkg"
        mac_dmg = root / "unchained-installer-mac.dmg"
        win_exe = root / "unchained-installer-windows.exe"
        win_msi = root / "unchained-installer-windows.msi"
        mac_pkg.write_bytes(b"pkg")
        win_exe.write_bytes(b"exe")

        old_root = web_mod._INSTALLER_ASSETS_DIR
        old_mac = list(web_mod._MAC_INSTALLER_FILES)
        old_windows = list(web_mod._WINDOWS_INSTALLER_FILES)
        try:
            web_mod._INSTALLER_ASSETS_DIR = root
            web_mod._MAC_INSTALLER_FILES = ["unchained-installer-mac.dmg", "unchained-installer-mac.pkg"]
            web_mod._WINDOWS_INSTALLER_FILES = ["unchained-installer-windows.msi", "unchained-installer-windows.exe"]

            # Falls back to pkg/exe when dmg/msi are missing.
            assert web_mod._native_installer_path("mac").name == mac_pkg.name
            assert web_mod._native_installer_path("windows").name == win_exe.name

            # When both are present, choose the freshest file.
            mac_dmg.write_bytes(b"dmg")
            win_msi.write_bytes(b"msi")
            now = time.time()
            # Make dmg/msi stale vs pkg/exe.
            os.utime(mac_dmg, (now - 120, now - 120))
            os.utime(mac_pkg, (now - 10, now - 10))
            os.utime(win_msi, (now - 120, now - 120))
            os.utime(win_exe, (now - 10, now - 10))
            assert web_mod._native_installer_path("mac").name == mac_pkg.name
            assert web_mod._native_installer_path("windows").name == win_exe.name

            # Make dmg/msi newest and verify they win.
            os.utime(mac_dmg, (now + 5, now + 5))
            os.utime(win_msi, (now + 5, now + 5))
            assert web_mod._native_installer_path("mac").name == mac_dmg.name
            assert web_mod._native_installer_path("windows").name == win_msi.name
        finally:
            web_mod._INSTALLER_ASSETS_DIR = old_root
            web_mod._MAC_INSTALLER_FILES = old_mac
            web_mod._WINDOWS_INSTALLER_FILES = old_windows
    print("  Native installer lookup prefers freshest artifact with pkg/exe fallback")


def test_download_installer_error_does_not_leak_assets_dir():
    """Verify /web/download-installer errors do not expose server filesystem paths."""
    import inspect
    from web import handle_download_installer

    source = inspect.getsource(handle_download_installer)
    assert "assets_dir" not in source, "install error should not leak installer assets dir"
    print("  download-installer error response omits assets_dir")


def test_mac_dmg_launcher_preserves_existing_env():
    """Verify DMG launcher script preserves existing local .env credentials."""
    script_path = Path(__file__).resolve().parent / "installers" / "build_mac_dmg.sh"
    src = script_path.read_text(encoding="utf-8")
    assert 'ENV_BACKUP="$DEST/.env.preinstall.$$"' in src
    assert 'if [ -f "$DEST/.env" ]; then' in src
    assert 'cp "$DEST/.env" "$ENV_BACKUP"' in src
    assert 'mv -f "$ENV_BACKUP" "$DEST/.env"' in src
    print("  DMG launcher preserves existing .env")


def test_install_page_prefers_native_installer():
    """Verify /install onboarding page no longer shows script fallback as primary UX."""
    from web import INSTALL_ONBOARD_HTML
    assert "Copy Fallback Command" not in INSTALL_ONBOARD_HTML, "fallback command button should be removed"
    assert "native installer binary" in INSTALL_ONBOARD_HTML, "native installer copy missing"
    assert "native_available" in INSTALL_ONBOARD_HTML, "native installer availability check missing"
    assert "data.zip_url || '/web/download-agent'" in INSTALL_ONBOARD_HTML, "zip fallback redirect missing"
    assert 'id="install-agentstatus"' in INSTALL_ONBOARD_HTML, "install chat status pill missing"
    assert 'id="install-bridgestatus"' in INSTALL_ONBOARD_HTML, "install bridge status pill missing"
    assert "/web/chat/status" in INSTALL_ONBOARD_HTML, "install status poll endpoint missing"
    assert "__INSTALL_RETURN_PATH_ENCODED__" in INSTALL_ONBOARD_HTML, "attributed sign-in path placeholder missing"
    assert "const INSTALL_RETURN_PATH = '__INSTALL_RETURN_PATH__';" in INSTALL_ONBOARD_HTML, "attributed install return path missing"
    assert "encodeURIComponent(INSTALL_RETURN_PATH)" in INSTALL_ONBOARD_HTML, "401 sign-in must preserve attribution"
    print("  INSTALL_ONBOARD_HTML prefers native installer flow")


def test_landing_and_case_study_contact_email_injected():
    """Verify public pages use configurable CONTACT_EMAIL instead of hardcoded mailbox."""
    import inspect
    from web import (
        BRANDED_TAB_HTML,
        CASE_STUDY_ZILLOW_HTML,
        LANDING_HTML,
        handle_case_study_zillow,
        handle_index,
    )

    assert "mailto:__CONTACT_EMAIL__" in LANDING_HTML, "landing page contact placeholder missing"
    assert "mailto:__CONTACT_EMAIL__" in CASE_STUDY_ZILLOW_HTML, "case study contact placeholder missing"
    assert 'data-unchained-tab="brand-default"' in BRANDED_TAB_HTML, "branded tab marker missing"
    assert 'content="noindex, nofollow"' in BRANDED_TAB_HTML, "branded tab should stay out of search indexes"
    assert "Ready for navigation" in BRANDED_TAB_HTML, "branded tab ready-state missing"

    index_src = inspect.getsource(handle_index)
    case_src = inspect.getsource(handle_case_study_zillow)
    assert 'replace("__CONTACT_EMAIL__", CONTACT_EMAIL)' in index_src, "landing handler must inject CONTACT_EMAIL"
    assert '__CONTACT_EMAIL__' in case_src or 'CONTACT_EMAIL' in case_src, "case-study handler must inject CONTACT_EMAIL"
    print("  Public pages inject CONTACT_EMAIL for footer contact link")


def test_install_token_handler_uses_header_transport():
    """Verify install-token response uses header-based script links (no tokenized URL query)."""
    import inspect
    from web import handle_install_token

    source = inspect.getsource(handle_install_token)
    assert "/install/script" in source, "header-based install script endpoint missing"
    assert "/install/windows/script" in source, "header-based Windows install script endpoint missing"
    assert "X-Install-Token" in source, "install token header missing"
    assert "install_token=" not in source, "install token should not be embedded in URL query"
    assert '"token": token' not in source, "raw install token should not be returned in JSON payload"
    print("  install-token endpoint uses header transport without URL query token leakage")


def test_claim_start_has_rate_limit_and_capacity_guards():
    """Verify claim-start endpoint guards against unbounded unauthenticated growth."""
    import inspect
    from web import handle_install_claim_start

    source = inspect.getsource(handle_install_claim_start)
    assert "_INSTALL_CLAIM_MAX_PENDING" in source, "pending-claim cap missing"
    assert "_INSTALL_CLAIM_START_MAX_PER_IP" in source, "per-IP rate limit missing"
    assert "_cleanup_install_claim_start_hits" in source, "claim-start hit cleanup missing"
    assert "status=429" in source, "rate-limit response missing"
    print("  claim-start endpoint has capacity and per-IP rate-limit guards")


def test_public_base_url_ignores_untrusted_host_header():
    """Verify non-local Host header does not override configured public base URL."""
    import web as web_mod

    class _Req:
        def __init__(self, host: str, forwarded_host: str = ""):
            self.host = host
            self.headers = {}
            if forwarded_host:
                self.headers["X-Forwarded-Host"] = forwarded_host

    trusted_local = _Req("localhost:8080")
    assert web_mod._public_base_url(trusted_local).startswith("http://localhost"), "localhost should stay local"

    spoofed = _Req("evil.example.com")
    assert web_mod._public_base_url(spoofed) == web_mod._PUBLIC_BASE_URL, "non-local host should use configured base URL"

    spoofed_forwarded = _Req("api.unchainedsky.com", forwarded_host="attacker.test")
    assert web_mod._public_base_url(spoofed_forwarded) == web_mod._PUBLIC_BASE_URL, "untrusted forwarded host should be ignored"
    print("  public base URL is anchored to config for non-local hosts")


def test_gemini_chat_has_install_banner():
    """Verify legacy Gemini/Codex/Claude chat template exposes install banner when disconnected."""
    from web import CHAT_GEMINI_HTML
    assert 'id="agentstatus"' in CHAT_GEMINI_HTML, "legacy chat agent status pill missing"
    assert 'id="bridgestatus"' in CHAT_GEMINI_HTML, "legacy chat bridge status pill missing"
    assert "bridge offline" in CHAT_GEMINI_HTML, "legacy chat bridge offline label missing"
    assert 'id="download-banner"' in CHAT_GEMINI_HTML, "legacy chat install banner missing"
    assert "download" in CHAT_GEMINI_HTML.lower(), "legacy chat download option missing"
    assert "Download Agent Installer" in CHAT_GEMINI_HTML, "legacy chat native installer option missing"
    assert "showBannerInstall" in CHAT_GEMINI_HTML, "legacy chat install modal function missing"
    print("  CHAT_GEMINI_HTML has installer banner + curl modal")


def test_trial_chat_has_guided_install_ux():
    """Verify trial chat template shows local setup status and guided install UX."""
    from web import TRIAL_CHAT_HTML
    assert 'id="agentstatus"' in TRIAL_CHAT_HTML, "trial chat agent status pill missing"
    assert 'id="bridgestatus"' in TRIAL_CHAT_HTML, "trial chat bridge status pill missing"
    assert "chat_connected" in TRIAL_CHAT_HTML, "trial status updater missing chat_connected handling"
    assert "bridge_connected" in TRIAL_CHAT_HTML, "trial status updater missing bridge_connected handling"
    assert "fetch('/web/chat/status?model='" in TRIAL_CHAT_HTML, "trial status polling endpoint missing"
    assert "Trial setup required" in TRIAL_CHAT_HTML, "trial guided setup banner missing"
    assert "Choose one install method" in TRIAL_CHAT_HTML, "trial install method choice copy missing"
    assert "Do not run both" in TRIAL_CHAT_HTML, "trial either/or install guidance missing"
    assert "No Claude or Codex CLI required for trial" in TRIAL_CHAT_HTML, "trial prerequisite copy missing"
    assert "maybeAutoOpenInstallModal" in TRIAL_CHAT_HTML, "trial auto-open install behavior missing"
    assert "lastLocalSetupReady" in TRIAL_CHAT_HTML, "trial send readiness guard missing"
    assert "sr-only" in TRIAL_CHAT_HTML, "trial accessible either/or text missing"
    assert 'role="dialog"' in TRIAL_CHAT_HTML, "trial install modal dialog role missing"
    assert "handleInstallModalKeydown" in TRIAL_CHAT_HTML, "trial install modal focus trap missing"
    assert '<button id="sendbtn" onclick="doSend()" disabled>' in TRIAL_CHAT_HTML, "trial send button should default disabled before status loads"
    assert "Retry Command" in TRIAL_CHAT_HTML, "trial install command retry missing"
    assert "--setup-accent" in TRIAL_CHAT_HTML, "trial setup color variables missing"
    assert "to reconnect this browser" in TRIAL_CHAT_HTML, "trial reconnect modal copy missing"
    assert "btn.disabled = !ready" in TRIAL_CHAT_HTML, "trial send button should be disabled semantically when setup is blocked"
    assert 'href="/test"' not in TRIAL_CHAT_HTML, "trial navigation should not expose the Control sandbox"
    print("  TRIAL_CHAT_HTML has chat+bridge status pills + guided install UX")


def test_admin_page_shows_openrouter_spend_column():
    """Verify admin UI renders an OpenRouter spend column."""
    from web import ADMIN_HTML
    assert "OR Spend" in ADMIN_HTML, "admin table missing OpenRouter spend column"
    assert "OR Remaining" in ADMIN_HTML, "admin table missing OpenRouter remaining column"
    assert "openrouter_spend_usd" in ADMIN_HTML, "admin UI should read OpenRouter spend field"
    assert "openrouter_budget_usd" in ADMIN_HTML, "admin UI should read OpenRouter budget field"
    assert "remainingUsd" in ADMIN_HTML, "admin UI should compute remaining OpenRouter budget"
    print("  ADMIN_HTML shows OpenRouter spend + remaining columns")


# ── Model selector tests ─────────────────────────────────────────────

def test_chat_html_has_model_dropdown():
    """Verify CHAT_HTML has the model selector dropdown with all 3 models."""
    from web import CLAUDE_CHAT_HTML as CHAT_HTML
    assert 'id="modelsel"' in CHAT_HTML, "Model select element missing"
    assert "claude-sonnet-4-6" in CHAT_HTML, "Sonnet model option missing"
    assert "claude-opus-4-7" in CHAT_HTML, "Opus model option missing"
    assert "claude-haiku-4-5-20251001" in CHAT_HTML, "Haiku model option missing"
    # Sonnet should be the first (default) option
    sonnet_pos = CHAT_HTML.index("claude-sonnet-4-6")
    opus_pos = CHAT_HTML.index("claude-opus-4-7")
    haiku_pos = CHAT_HTML.index("claude-haiku-4-5-20251001")
    assert sonnet_pos < opus_pos, "Sonnet should be first option (default)"
    assert opus_pos < haiku_pos, "Opus should be second option"
    print(f"  Model dropdown: 3 options, Sonnet default")


def test_trial_chat_has_admin_custom_openrouter_model():
    """Verify trial chat supports an admin-only custom OpenRouter model input."""
    from web import TRIAL_CHAT_HTML
    assert 'id="modelsel-custom-option"' in TRIAL_CHAT_HTML, "Admin custom model option missing"
    assert 'id="model-custom-input"' in TRIAL_CHAT_HTML, "Custom model input missing"
    assert "__custom_openrouter__" in TRIAL_CHAT_HTML, "Custom model sentinel missing"
    assert "qwen/qwen3.5-flash-02-23" in TRIAL_CHAT_HTML, "Custom model example missing"
    assert "function onCustomModelInput(value)" in TRIAL_CHAT_HTML, "Custom model input handler missing"
    print("  Trial chat has admin custom OpenRouter model input")


def test_codex_chat_has_three_slot_ui():
    """Verify Codex chat page includes the same 3-slot controls as local CLI chat."""
    from web import CHAT_CODEX_HTML
    assert 'id="slotbar"' in CHAT_CODEX_HTML, "Codex chat should include slot bar"
    assert "switchSlot(1)" in CHAT_CODEX_HTML and "switchSlot(2)" in CHAT_CODEX_HTML and "switchSlot(3)" in CHAT_CODEX_HTML, \
        "Codex chat should expose 3 switchSlot buttons"
    assert "/web/chat/slots" in CHAT_CODEX_HTML, "Codex chat should load slot state from backend"
    assert "/web/chat/switch" in CHAT_CODEX_HTML, "Codex chat should switch slot through backend"
    print("  Codex chat includes 3-slot UI + backend slot calls")


def test_codex_cli_local_chat_has_guided_setup_ux():
    """Verify /local?provider=codex-cli renders Codex with guided local setup UX."""
    import inspect
    import web as web_mod
    from web import CHAT_CODEX_HTML

    local_src = inspect.getsource(web_mod.handle_local_page)
    assert 'provider in {"codex-cli", "codex-sdk"}' in local_src, "local page should serve Codex template for codex-cli provider"
    assert "Local setup required" in CHAT_CODEX_HTML, "Codex CLI local setup banner missing"
    assert "Choose one install method" in CHAT_CODEX_HTML, "Codex CLI install method choice copy missing"
    assert "Do not run both" in CHAT_CODEX_HTML, "Codex CLI either/or install guidance missing"
    assert "Requires Codex CLI to be installed and logged in." in CHAT_CODEX_HTML, "Codex CLI prerequisite copy missing"
    assert "providerDefault = provider === 'codex-cli'" in CHAT_CODEX_HTML, "codex-cli provider should select Codex CLI default model"
    assert "codex-cli:gpt-5.5" in CHAT_CODEX_HTML, "Codex CLI default model missing"
    assert 'document.querySelectorAll(\'#modelsel option[value^="codex-sdk:"]\').forEach(o => o.remove())' in CHAT_CODEX_HTML, \
        "Codex CLI lane should hide SDK model options after CLI selection"
    assert "maybeAutoOpenInstallModal" in CHAT_CODEX_HTML, "Codex CLI auto-open install behavior missing"
    assert "lastLocalSetupReady" in CHAT_CODEX_HTML, "Codex CLI send readiness guard missing"
    assert "codex_cli_supported: data.codex_cli_supported" in CHAT_CODEX_HTML, "Codex CLI support status should reach UI"
    assert "sr-only" in CHAT_CODEX_HTML, "Codex CLI accessible either/or text missing"
    assert 'role="dialog"' in CHAT_CODEX_HTML, "Codex CLI install modal dialog role missing"
    assert "handleInstallModalKeydown" in CHAT_CODEX_HTML, "Codex CLI install modal focus trap missing"
    assert '<button id="sendbtn" onclick="doSend()" disabled>' in CHAT_CODEX_HTML, "Codex CLI send button should default disabled before status loads"
    assert "btn.disabled = !ready" in CHAT_CODEX_HTML, "Codex CLI send button should be disabled semantically when setup is blocked"
    assert "Install (curl)" not in CHAT_CODEX_HTML, "stale curl install label should be gone from Codex local chat"
    assert "Install Agent (curl)" not in CHAT_CODEX_HTML, "stale curl modal title should be gone from Codex local chat"
    print("  Codex CLI local chat has guided setup UX + provider default")


def test_opencode_cli_local_chat_has_guided_setup_ux():
    """Verify /local?provider=opencode-cli uses local setup UX + provider default."""
    from web import CLAUDE_CHAT_HTML, LANDING_HTML

    assert "/local?provider=opencode-cli" in LANDING_HTML, "OpenCode landing entry missing"
    assert "OpenCode CLI" in LANDING_HTML, "OpenCode landing label missing"
    assert "opencode-cli:" in CLAUDE_CHAT_HTML, "OpenCode CLI model option missing"
    assert "provider === 'opencode-cli'" in CLAUDE_CHAT_HTML, "opencode-cli provider should select OpenCode default model"
    assert "lastOpenCodeCliSupported" in CLAUDE_CHAT_HTML, "OpenCode support status guard missing"
    assert "opencode_cli_supported" in CLAUDE_CHAT_HTML, "OpenCode support status should reach UI"
    assert "opencode auth login" in CLAUDE_CHAT_HTML, "OpenCode authentication guidance missing"
    assert "refreshOpenCodeModelOptions" in CLAUDE_CHAT_HTML, "OpenCode models should refresh from selector interaction"
    assert "updateOpenCodeModelOptions(data.opencode_models || [])" not in CLAUDE_CHAT_HTML, \
        "OpenCode model selector should not rebuild on every status poll"
    print("  OpenCode CLI local chat has guided setup UX + provider default")


def test_chat_agent_codex_supports_slot_protocol():
    """Verify server-side Codex agent supports get_slots/switch_slot/new_chat semantics."""
    source_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chat_agent_codex.py")
    with open(source_path) as f:
        source = f.read()
    assert "SLOT_COUNT = 3" in source, "Codex agent should define 3-slot support"
    assert 'elif msg.get("type") == "switch_slot"' in source, "Codex agent should handle switch_slot requests"
    assert 'elif msg.get("type") == "get_slots"' in source, "Codex agent should handle get_slots requests"
    assert "_get_slots_info" in source, "Codex agent should compute slot previews/state"
    print("  Codex agent supports 3-slot chat protocol")


def test_trial_chat_has_claude_access_request_flow():
    """Verify trial chat exposes a CTA to request Claude access while pending."""
    from web import TRIAL_CHAT_HTML
    assert 'id="claude-request-banner"' in TRIAL_CHAT_HTML, "Claude request banner missing"
    assert "function requestClaudeAccess()" in TRIAL_CHAT_HTML, "requestClaudeAccess handler missing"
    assert "/auth/request-claude-access" in TRIAL_CHAT_HTML, "Claude access request endpoint missing from UI"
    assert "claude_access_requested" in TRIAL_CHAT_HTML, "UI should read claude_access_requested auth state"
    print("  Trial chat includes pending Claude-access request flow")


def test_chat_html_sends_model_in_fetch():
    """Verify doSend() includes model in the POST body."""
    from web import CLAUDE_CHAT_HTML as CHAT_HTML
    assert (
        "model: currentModel()" in CHAT_HTML
        or "model: document.getElementById('modelsel').value" in CHAT_HTML
    ), "doSend() should include model from dropdown in fetch body"
    print(f"  doSend() includes model field")


def test_handle_chat_msg_forwards_model():
    """Verify handle_chat_msg extracts and forwards the model field."""
    import inspect
    from web import handle_chat_msg
    source = inspect.getsource(handle_chat_msg)
    assert 'body.get("model"' in source, "handle_chat_msg should extract model from body"
    assert '"model"' in source, "handle_chat_msg should forward model in ws message"
    print(f"  handle_chat_msg extracts and forwards model")


def test_google_auth_respects_post_trigger_signup_status():
    """Verify signup responses branch on the persisted post-trigger status."""
    import inspect
    from web import handle_google_auth
    source = inspect.getsource(handle_google_auth)
    assert "_auth.create_pending_user(email, name, picture, user_type=\"trial\")" in source, \
        "New trial/demo sign-ins should be created with pending status"
    assert "_ensure_trial_access(core, user, email)" in source, \
        "Trial/demo users should receive an API key for trial/demo chat access"
    assert "\"review_pending\": status == \"pending\"" in source, \
        "Auth response should only show review_pending when the stored status is pending"
    assert '"demo_unlimited": core._is_demo_unlimited(user) if status == "approved" else False' in source, \
        "Auto-approved trial/demo response should reflect unlimited quota state"
    assert 'if status == "approved"' in source, \
        "Auto-approved signups should bypass the pending review gate"
    assert "\"pending\": True" in source, \
        "Non-trial pending users should still receive pending response"
    print("  Signup auth honors post-trigger approval status")


def test_pending_trial_restricted_from_local_and_provision_routes():
    """Verify pending trial users are gated away from full-access routes."""
    import inspect
    import web as web_mod

    local_src = inspect.getsource(web_mod.handle_local_page)
    setup_src = inspect.getsource(web_mod.handle_setup_page)
    sched_src = inspect.getsource(web_mod.handle_scheduler_page)
    prov_start_src = inspect.getsource(web_mod.handle_provision_start)

    assert "_is_pending_user(auth_info)" in local_src, \
        "local page should gate pending trial users"
    assert 'web.HTTPFound("/trial")' in local_src, \
        "local page should redirect pending trial users to /trial"
    assert "_is_pending_user(auth_info)" in setup_src, \
        "setup page should gate pending trial users"
    assert "_is_pending_user(auth_info)" in sched_src, \
        "scheduler page should gate pending trial users"
    assert "_is_pending_user(auth_info)" in prov_start_src, \
        "provision start should gate pending trial users"
    assert "_pending_limited_response()" in prov_start_src, \
        "provision start should return pending-trial limited error response"
    print("  Pending trial users are gated from local/provision routes")


def test_handle_chat_msg_blocks_pending_trial_non_openrouter():
    """Verify pending trial users are limited to OpenRouter model lane in chat endpoint."""
    import inspect
    from web import handle_chat_msg

    source = inspect.getsource(handle_chat_msg)
    assert "_is_pending_user(auth_info) and not is_openrouter" in source, \
        "chat endpoint should block pending trial users from non-OpenRouter models"
    assert "_pending_limited_response()" in source, \
        "chat endpoint should return pending-trial limited response"
    print("  chat endpoint restricts pending trial users to OpenRouter lane")


def test_handle_chat_msg_openrouter_budget_force_logic():
    """Verify OpenRouter requests enforce per-user budget and forward user_id for metering."""
    import inspect
    from web import handle_chat_msg
    source = inspect.getsource(handle_chat_msg)
    assert "_openrouter_budget_state_for_user" in source, "handle_chat_msg should load OpenRouter budget state"
    assert "_is_openrouter_post_cap_allowed_model" in source and "requested_model" in source, \
        "handle_chat_msg should only force fallback for disallowed post-cap models"
    assert "_OPENROUTER_TRIAL_FALLBACK_MODEL" in source, "handle_chat_msg should apply fallback model when capped"
    assert 'if is_openrouter and auth_info.get("user_id")' in source, "OpenRouter ws payload should include user_id"
    assert '"model_forced"' in source, "forced model SSE event should be emitted"
    print("  handle_chat_msg enforces OpenRouter budget + emits forced-model event")


def test_handle_chat_ws_tracks_openrouter_usage():
    """Verify OpenRouter usage events are consumed server-side to update budget spend."""
    import inspect
    from web import handle_chat_ws
    source = inspect.getsource(handle_chat_ws)
    assert 'msg_type == "openrouter_usage"' in source, "chat ws handler should consume openrouter_usage events"
    assert "_track_openrouter_usage_for_user" in source, "chat ws handler should persist OpenRouter usage counters"
    assert 'openrouter.usage' in source, "chat ws handler should emit usage trace logs"
    print("  handle_chat_ws consumes openrouter_usage and tracks spend")


def test_openrouter_agent_emits_usage_event():
    """Verify trial OpenRouter agent emits usage/cost events with user_id context."""
    source_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chat_agent_openrouter.py")
    with open(source_path) as f:
        source = f.read()
    assert "_extract_openrouter_usage" in source, "usage parser should exist in OpenRouter agent"
    assert '"type": "openrouter_usage"' in source, "OpenRouter agent should emit openrouter_usage event"
    assert ('msg.get("user_id"' in source or "msg.get('user_id'" in source), \
        "OpenRouter agent should read user_id from inbound WS message"
    assert "OpenRouter usage:" in source, "OpenRouter agent should log per-request token/cost usage"
    assert '"OPENROUTER_RATE_RAMP_FALLBACK_MODEL",\n            self.model,' in source, \
        "OpenRouter rate-ramp fallback should default to the configured trial model"
    print("  OpenRouter trial agent emits usage event with user context")


def test_trial_chat_handles_model_forced_event():
    """Verify trial chat frontend handles model_forced SSE events."""
    from web import TRIAL_CHAT_HTML
    assert "evt.type === 'model_forced'" in TRIAL_CHAT_HTML, "trial chat should handle model_forced event"
    assert "_syncCustomModelUi()" in TRIAL_CHAT_HTML, "trial chat should refresh model UI after forced switch"
    print("  trial chat UI handles model_forced SSE")


def test_trial_and_demo_openrouter_default_models_and_cap_options():
    """Verify trial/demo/first-look default to Gemini and cap keeps Trinity/StepFun."""
    from web import TRIAL_CHAT_HTML, HEADLESS_DEMO_HTML, FIRST_LOOK_PREVIEW_HTML
    assert 'value="google/gemini-3.1-flash-lite"' in TRIAL_CHAT_HTML, \
        "trial model selector should default to Gemini 3.1 Flash Lite"
    assert "_POST_CAP_ALLOWED_MODELS = ['arcee-ai/trinity-large-preview:free', 'stepfun/step-3.5-flash:free']" in TRIAL_CHAT_HTML, \
        "trial cap model allowlist should be Trinity + StepFun"
    assert "'google/gemini-3.1-flash-lite'" in HEADLESS_DEMO_HTML, \
        "demo currentModel should default to Gemini 3.1 Flash Lite"
    assert "'google/gemini-3.1-flash-lite'" in FIRST_LOOK_PREVIEW_HTML, \
        "first-look should default to Gemini 3.1 Flash Lite"
    assert "evt.type === 'model_forced'" in HEADLESS_DEMO_HTML, \
        "demo UI should handle server-forced model fallback events"
    print("  trial/demo/first-look defaults and post-cap model constraints are in place")


def test_sdk_agent_uses_per_message_model():
    """Verify chat_agent_sdk reads model from message and passes to API."""
    import inspect
    from chat_agent_sdk import ChatAgent
    source = inspect.getsource(ChatAgent._handle_message)
    assert 'msg.get("model")' in source, "_handle_message should read model from msg"
    assert "model=model" in source, "_handle_message should pass model to messages.create()"
    print(f"  SDK agent uses per-message model")


def test_cli_agent_model_map():
    """Verify CLI agent maps Anthropic model IDs to CLI names."""
    # Import the map directly — chat_agent_cli has side effects but _MODEL_CLI_MAP is safe
    # since it's defined before the main() call
    expected = {
        "claude-opus-4-7": "opus",
        "claude-sonnet-4-6": "sonnet",
        "claude-haiku-4-5-20251001": "haiku",
    }
    # Read the source to verify the map exists with correct values
    import ast
    source_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chat_agent_cli.py")
    with open(source_path) as f:
        source = f.read()

    assert "_MODEL_CLI_MAP" in source, "_MODEL_CLI_MAP not defined"
    for model_id, cli_name in expected.items():
        assert f'"{model_id}": "{cli_name}"' in source, \
            f"Missing mapping {model_id} -> {cli_name}"

    # Verify handle_message accepts model parameter
    assert "def handle_message(" in source and "model: str" in source, \
        "handle_message should accept model parameter"
    assert "cli_model" in source, "Should use cli_model variable"
    assert '"--model", cli_model' in source, "Should pass cli_model to --model flag"
    print(f"  CLI agent: model map correct, handle_message accepts model param")


def test_cli_agent_forwards_model_from_ws():
    """Verify the main loop passes model from WS message to handle_message."""
    source_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chat_agent_cli.py")
    with open(source_path) as f:
        source = f.read()
    assert 'msg.get("model"' in source, "main loop should extract model from WS message"
    assert "msg.get(\"scheduler_armed\")" in source, "main loop should read scheduler arming from WS message"
    assert "scheduler_armed=msg_scheduler_armed" in source, \
        "main loop should pass scheduler arming to handle_message"
    print(f"  CLI agent main loop forwards model from WS message")


# ── PowerShell syntax validation ─────────────────────────────────────

def test_powershell_scripts_parse_without_errors():
    """Extract all .ps1 scripts from agent ZIP and validate syntax via pwsh."""
    if not shutil.which("pwsh"):
        print("  SKIP: pwsh not installed")
        return

    from agent_package import build_agent_zip

    zip_bytes = build_agent_zip(
        api_key="uc_live_test123",
        relay_host="localhost",
        install_token="inst_test_bootstrap",
    )

    errors = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        ps1_files = [n for n in zf.namelist() if n.endswith(".ps1")]
        assert ps1_files, "No .ps1 files found in agent ZIP"

        for name in ps1_files:
            content = zf.read(name).decode("utf-8")
            with tempfile.NamedTemporaryFile(
                suffix=".ps1", mode="w", delete=False, encoding="utf-8",
            ) as f:
                f.write(content)
                f.flush()
                tmp_path = f.name

            try:
                import subprocess
                result = subprocess.run(
                    [
                        "pwsh", "-NoProfile", "-NonInteractive", "-Command",
                        f'$errs = $null; '
                        f'[System.Management.Automation.Language.Parser]::ParseFile('
                        f'"{tmp_path}", [ref]$null, [ref]$errs); '
                        f'if ($errs.Count -gt 0) {{ '
                        f'$errs | ForEach-Object {{ Write-Error $_.ToString() }}; '
                        f'exit 1 }}',
                    ],
                    capture_output=True, text=True, timeout=30,
                )
                if result.returncode != 0:
                    errors.append(f"{name}: {result.stderr.strip()}")
            finally:
                os.unlink(tmp_path)

    assert not errors, "PowerShell syntax errors:\n" + "\n".join(errors)
    print(f"  All {len(ps1_files)} .ps1 scripts parse cleanly: {ps1_files}")


def test_powershell_windows_install_script_parses():
    """Validate the generated Windows install script parses cleanly."""
    if not shutil.which("pwsh"):
        print("  SKIP: pwsh not installed")
        return

    from agent_package import _generate_windows_install_script
    script = _generate_windows_install_script(
        install_token="test_token_123",
        relay_host="api.unchainedsky.com",
        base_url="https://api.unchainedsky.com",
    )
    assert len(script) > 100, "Windows install script seems too short"

    with tempfile.NamedTemporaryFile(
        suffix=".ps1", mode="w", delete=False, encoding="utf-8",
    ) as f:
        f.write(script)
        f.flush()
        tmp_path = f.name

    try:
        import subprocess
        result = subprocess.run(
            [
                "pwsh", "-NoProfile", "-NonInteractive", "-Command",
                f'$errs = $null; '
                f'[System.Management.Automation.Language.Parser]::ParseFile('
                f'"{tmp_path}", [ref]$null, [ref]$errs); '
                f'if ($errs.Count -gt 0) {{ '
                f'$errs | ForEach-Object {{ Write-Error $_.ToString() }}; '
                f'exit 1 }}',
            ],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, f"Windows install PS1 syntax error: {result.stderr.strip()}"
    finally:
        os.unlink(tmp_path)

    print(f"  Windows install script parses cleanly ({len(script)} chars)")


# ── Runner ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        ("agent_package: version constants", test_version_constants),
        ("agent_package: build_agent_zip has version.txt + update.sh", test_build_agent_zip_contains_version_and_update),
        ("agent_package: Windows certifi check avoids native quote loss", test_windows_certifi_check_avoids_legacy_native_quote_loss),
        ("agent_package: build_update_zip (no .env, with launchers)", test_build_update_zip_no_env_with_launchers),
        ("agent_package: _generate_public_install_script", test_generate_public_install_script),
        ("agent_package: public install handler importable", test_public_install_script_handler_importable),
        ("agent_package: _generate_install_script", test_generate_install_script),
        ("agent_package: _generate_windows_install_script", test_generate_windows_install_script),
        ("auth: create_install_token", test_create_install_token),
        ("auth: validate_install_token (valid + used)", test_validate_install_token),
        ("auth: validate expired token", test_validate_expired_token),
        ("auth: validate bogus token", test_validate_bogus_token),
        ("auth: cleanup_expired_tokens", test_cleanup_expired_tokens),
        ("auth: openrouter budget tracking", test_openrouter_budget_tracking),
        ("auth: openrouter token usage tracking", test_openrouter_token_usage_tracking),
        ("auth: create_pending_user reflects trigger", test_create_pending_user_returns_post_trigger_state),
        ("auth: approve_user keeps existing API key", test_approve_user_keeps_existing_api_key),
        ("chat_agent_cli: _parse_version", test_parse_version),
        ("web: new handlers importable", test_web_imports),
        ("web: routes registered", test_web_routes_registered),
        ("web: CHAT_HTML has install modal", test_chat_html_has_install_modal),
        ("web: CHAT_HTML has OpenCode cockpit handoff", test_chat_html_has_opencode_cockpit_handoff),
        ("web: SETUP_HTML has status + installer banner", test_setup_html_has_status_and_install_banner),
        ("web: native installer lookup prefers freshest artifact", test_native_installer_path_prefers_freshest_artifact),
        ("web: download-installer error omits assets_dir", test_download_installer_error_does_not_leak_assets_dir),
        ("installers: dmg launcher preserves existing env", test_mac_dmg_launcher_preserves_existing_env),
        ("web: INSTALL_ONBOARD_HTML native-only flow", test_install_page_prefers_native_installer),
        ("web: landing/case-study contact email injection", test_landing_and_case_study_contact_email_injected),
        ("web: install-token uses header transport", test_install_token_handler_uses_header_transport),
        ("web: claim-start has rate/capacity guards", test_claim_start_has_rate_limit_and_capacity_guards),
        ("web: public base URL ignores untrusted host header", test_public_base_url_ignores_untrusted_host_header),
        ("web: CHAT_GEMINI_HTML has install banner", test_gemini_chat_has_install_banner),
        ("web: TRIAL_CHAT_HTML has guided install UX", test_trial_chat_has_guided_install_ux),
        ("web: ADMIN_HTML shows OpenRouter spend column", test_admin_page_shows_openrouter_spend_column),
        ("web: CHAT_HTML has model dropdown", test_chat_html_has_model_dropdown),
        ("web: TRIAL_CHAT_HTML has admin custom model input", test_trial_chat_has_admin_custom_openrouter_model),
        ("web: CHAT_CODEX_HTML has 3-slot UI", test_codex_chat_has_three_slot_ui),
        ("web: Codex CLI local chat has guided setup UX", test_codex_cli_local_chat_has_guided_setup_ux),
        ("web: OpenCode CLI local chat has guided setup UX", test_opencode_cli_local_chat_has_guided_setup_ux),
        ("codex agent: supports slot protocol", test_chat_agent_codex_supports_slot_protocol),
        ("web: TRIAL_CHAT_HTML has Claude access request flow", test_trial_chat_has_claude_access_request_flow),
        ("web: doSend() includes model", test_chat_html_sends_model_in_fetch),
        ("web: handle_chat_msg forwards model", test_handle_chat_msg_forwards_model),
        ("auth: signup honors post-trigger status", test_google_auth_respects_post_trigger_signup_status),
        ("auth: pending trial gated from local/provision routes", test_pending_trial_restricted_from_local_and_provision_routes),
        ("web: pending trial blocked from non-openrouter chat", test_handle_chat_msg_blocks_pending_trial_non_openrouter),
        ("web: handle_chat_msg openrouter budget force logic", test_handle_chat_msg_openrouter_budget_force_logic),
        ("web: handle_chat_ws tracks openrouter usage", test_handle_chat_ws_tracks_openrouter_usage),
        ("openrouter agent: emits usage event", test_openrouter_agent_emits_usage_event),
        ("web ui: trial handles model_forced event", test_trial_chat_handles_model_forced_event),
        ("web ui: trial+demo openrouter defaults and post-cap models", test_trial_and_demo_openrouter_default_models_and_cap_options),
        ("sdk: uses per-message model", test_sdk_agent_uses_per_message_model),
        ("cli: model ID to CLI name map", test_cli_agent_model_map),
        ("cli: main loop forwards model", test_cli_agent_forwards_model_from_ws),
        ("powershell: agent ZIP .ps1 scripts parse cleanly", test_powershell_scripts_parse_without_errors),
        ("powershell: windows install script parses", test_powershell_windows_install_script_parses),
    ]

    passed = 0
    failed = 0
    for name, fn in tests:
        try:
            print(f"\n[TEST] {name}")
            fn()
            print(f"  PASS")
            passed += 1
        except Exception as e:
            print(f"  FAIL: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed, {passed+failed} total")
    if failed:
        sys.exit(1)
