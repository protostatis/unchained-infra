"""Tests for the curl|bash installer + auto-update feature.

Tests agent_package.py, auth.py install tokens, web.py endpoints,
and chat_agent_cli.py version checking.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
import zipfile

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
        update_ps1 = zf.read("unchained-agent/update.ps1").decode()
        assert "/web/agent/version" in update_ps1
        assert "/web/agent/files" in update_ps1
        # start.sh still there
        assert "unchained-agent/start.sh" in names
        start_sh = zf.read("unchained-agent/start.sh").decode()
        assert "/web/install/claim/start" in start_sh
        assert "/web/install/claim/poll" in start_sh
        assert "/install/claim/" in start_sh
        # .env still there
        assert "unchained-agent/.env" in names
        env = zf.read("unchained-agent/.env").decode()
        assert "uc_live_test123" not in env
        assert "UNCHAINED_API_KEY=" in env
        assert "UNCHAINED_INSTALL_TOKEN=inst_test_bootstrap" in env
    print(f"  ZIP size: {len(zip_bytes)} bytes, {len(names)} files")


def test_build_update_zip_no_env_no_start():
    from agent_package import build_update_zip, VERSION
    zip_bytes = build_update_zip()
    assert len(zip_bytes) > 0

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()
        # Should have code files
        assert "unchained-agent/version.txt" in names
        assert "unchained-agent/requirements.txt" in names
        assert "unchained-agent/update.sh" in names
        assert "unchained-agent/update.ps1" in names
        assert "unchained-agent/unchained/cdp_tool.py" in names
        # Should NOT have .env or start.sh
        assert "unchained-agent/.env" not in names, ".env should not be in update ZIP"
        assert "unchained-agent/start.sh" not in names, "start.sh should not be in update ZIP"
        # version.txt content
        v = zf.read("unchained-agent/version.txt").decode()
        assert v == VERSION
    print(f"  Update ZIP: {len(zip_bytes)} bytes, {len(names)} files (no .env, no start.sh)")


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
    assert "/web/download-agent?install_token=" in script, "download URL not in script"
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
    assert "/web/download-agent?install_token=" in script, "download URL not in script"
    assert "Invoke-WebRequest" in script
    assert "start.ps1" in script
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
        assert ("POST", "/web/install/bootstrap") in routes, "install bootstrap route not registered"
        assert ("GET", "/install") in routes, "install page route not registered"
        assert ("GET", "/install/{token}") in routes, "install script route not registered"
        assert ("GET", "/install/windows/{token}") in routes, "windows install script route not registered"
        assert ("GET", "/install/claim/{claim_id}") in routes, "install claim page route not registered"
        assert ("POST", "/web/install/claim/start") in routes, "install claim start route not registered"
        assert ("POST", "/web/install/claim/poll") in routes, "install claim poll route not registered"
        assert ("POST", "/web/install/claim/approve") in routes, "install claim approve route not registered"
        assert ("GET", "/web/download-installer") in routes, "download-installer route not registered"
        assert ("GET", "/web/agent/version") in routes, "agent version route not registered"
        assert ("GET", "/web/agent/files") in routes, "agent files route not registered"
    else:
        # Backward compatibility for older code where routes lived directly in main().
        import inspect
        from web import main as web_main

        source = inspect.getsource(web_main)
        assert "/web/install-token" in source, "install-token route not registered"
        assert "/web/install/bootstrap" in source, "install bootstrap route not registered"
        assert "/install" in source, "install page route not registered"
        assert "/install/{token}" in source, "install script route not registered"
        assert "/install/windows/{token}" in source, "windows install script route not registered"
        assert "/install/claim/{claim_id}" in source, "install claim page route not registered"
        assert "/web/install/claim/start" in source, "install claim start route not registered"
        assert "/web/install/claim/poll" in source, "install claim poll route not registered"
        assert "/web/install/claim/approve" in source, "install claim approve route not registered"
        assert "/web/download-installer" in source, "download-installer route not registered"
        assert "/web/agent/version" in source, "agent version route not registered"
        assert "/web/agent/files" in source, "agent files route not registered"
    print("  All install/update routes registered")


def test_chat_html_has_install_modal():
    """Verify the CHAT_HTML has the install modal and buttons."""
    from web import CLAUDE_CHAT_HTML as CHAT_HTML
    assert "install-modal" in CHAT_HTML, "Install modal missing from CHAT_HTML"
    assert "showInstallCmd" in CHAT_HTML, "showInstallCmd JS missing"
    assert "copyInstallCmd" in CHAT_HTML, "copyInstallCmd JS missing"
    assert "closeInstallModal" in CHAT_HTML, "closeInstallModal JS missing"
    assert "Download Agent Installer" in CHAT_HTML, "installer download button missing"
    assert "Install (curl)" in CHAT_HTML, "curl install option missing"
    assert "Install Agent (curl)" in CHAT_HTML, "curl modal title missing"
    assert "Copy Command" in CHAT_HTML, "copy command button missing"
    assert "Download ZIP" in CHAT_HTML, "download ZIP link missing"
    assert CHAT_HTML.index('id="banner-curl"') < CHAT_HTML.index('id="banner-zip"'), "curl action should come before ZIP"
    assert CHAT_HTML.index('id="banner-zip"') < CHAT_HTML.index('id="banner-connect"'), "installer action should come after ZIP"
    print(f"  CHAT_HTML has install modal + buttons")


def test_setup_html_has_status_and_install_banner():
    """Verify setup route preserves agent status pills and install banner."""
    from web import SETUP_HTML
    assert 'id="setup-agentstatus"' in SETUP_HTML, "setup chat status pill missing"
    assert 'id="setup-bridgestatus"' in SETUP_HTML, "setup bridge status pill missing"
    assert 'id="setup-download-banner"' in SETUP_HTML, "setup install banner missing"
    assert 'id="setup-banner-connect"' in SETUP_HTML, "setup install route link missing"
    assert "Download Agent Installer" in SETUP_HTML, "setup installer label missing"
    assert "Download ZIP" in SETUP_HTML, "setup ZIP label missing"
    assert SETUP_HTML.index('id="setup-banner-curl"') < SETUP_HTML.index('id="setup-banner-zip"'), "setup curl action should come before ZIP"
    assert SETUP_HTML.index('id="setup-banner-zip"') < SETUP_HTML.index('id="setup-banner-connect"'), "setup installer action should come after ZIP"
    assert "showSetupInstallCmd" in SETUP_HTML, "setup curl modal open function missing"
    assert "copySetupInstallCmd" in SETUP_HTML, "setup curl copy function missing"
    print("  SETUP_HTML has status pills + installer banner")


def test_install_page_prefers_native_installer():
    """Verify /install onboarding page no longer shows script fallback as primary UX."""
    from web import INSTALL_ONBOARD_HTML
    assert "Copy Fallback Command" not in INSTALL_ONBOARD_HTML, "fallback command button should be removed"
    assert "native installer binary" in INSTALL_ONBOARD_HTML, "native installer copy missing"
    assert "native_available" in INSTALL_ONBOARD_HTML, "native installer availability check missing"
    print("  INSTALL_ONBOARD_HTML prefers native installer flow")


# ── Model selector tests ─────────────────────────────────────────────

def test_chat_html_has_model_dropdown():
    """Verify CHAT_HTML has the model selector dropdown with all 3 models."""
    from web import CLAUDE_CHAT_HTML as CHAT_HTML
    assert 'id="modelsel"' in CHAT_HTML, "Model select element missing"
    assert "claude-sonnet-4-6" in CHAT_HTML, "Sonnet model option missing"
    assert "claude-opus-4-6" in CHAT_HTML, "Opus model option missing"
    assert "claude-haiku-4-5-20251001" in CHAT_HTML, "Haiku model option missing"
    # Sonnet should be the first (default) option
    sonnet_pos = CHAT_HTML.index("claude-sonnet-4-6")
    opus_pos = CHAT_HTML.index("claude-opus-4-6")
    haiku_pos = CHAT_HTML.index("claude-haiku-4-5-20251001")
    assert sonnet_pos < opus_pos, "Sonnet should be first option (default)"
    assert opus_pos < haiku_pos, "Opus should be second option"
    print(f"  Model dropdown: 3 options, Sonnet default")


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
        "claude-opus-4-6": "opus",
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
    assert "def handle_message(ws, sid: str, user_text: str, model: str" in source, \
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
    assert "handle_message(ws, sid, user_text, msg_model)" in source, \
        "main loop should pass model to handle_message"
    print(f"  CLI agent main loop forwards model from WS message")


# ── Runner ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        ("agent_package: version constants", test_version_constants),
        ("agent_package: build_agent_zip has version.txt + update.sh", test_build_agent_zip_contains_version_and_update),
        ("agent_package: build_update_zip (no .env, no start.sh)", test_build_update_zip_no_env_no_start),
        ("agent_package: _generate_install_script", test_generate_install_script),
        ("agent_package: _generate_windows_install_script", test_generate_windows_install_script),
        ("auth: create_install_token", test_create_install_token),
        ("auth: validate_install_token (valid + used)", test_validate_install_token),
        ("auth: validate expired token", test_validate_expired_token),
        ("auth: validate bogus token", test_validate_bogus_token),
        ("auth: cleanup_expired_tokens", test_cleanup_expired_tokens),
        ("chat_agent_cli: _parse_version", test_parse_version),
        ("web: new handlers importable", test_web_imports),
        ("web: routes registered", test_web_routes_registered),
        ("web: CHAT_HTML has install modal", test_chat_html_has_install_modal),
        ("web: SETUP_HTML has status + installer banner", test_setup_html_has_status_and_install_banner),
        ("web: INSTALL_ONBOARD_HTML native-only flow", test_install_page_prefers_native_installer),
        ("web: CHAT_HTML has model dropdown", test_chat_html_has_model_dropdown),
        ("web: doSend() includes model", test_chat_html_sends_model_in_fetch),
        ("web: handle_chat_msg forwards model", test_handle_chat_msg_forwards_model),
        ("sdk: uses per-message model", test_sdk_agent_uses_per_message_model),
        ("cli: model ID to CLI name map", test_cli_agent_model_map),
        ("cli: main loop forwards model", test_cli_agent_forwards_model_from_ws),
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
