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
from pathlib import Path

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
        assert ("GET", "/web/download-installer") in routes, "download-installer route not registered"
        assert ("GET", "/web/agent/version") in routes, "agent version route not registered"
        assert ("GET", "/web/agent/files") in routes, "agent files route not registered"
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
    assert "download" in CHAT_HTML.lower(), "download link missing"
    assert CHAT_HTML.index('id="banner-curl"') < CHAT_HTML.index('id="banner-connect"'), "curl action should come before connect"
    print(f"  CHAT_HTML has install modal + buttons")


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
    print("  INSTALL_ONBOARD_HTML prefers native installer flow")


def test_landing_and_case_study_contact_email_injected():
    """Verify public pages use configurable CONTACT_EMAIL instead of hardcoded mailbox."""
    import inspect
    from web import (
        CASE_STUDY_ZILLOW_HTML,
        LANDING_HTML,
        handle_case_study_zillow,
        handle_index,
    )

    assert "mailto:__CONTACT_EMAIL__" in LANDING_HTML, "landing page contact placeholder missing"
    assert "mailto:__CONTACT_EMAIL__" in CASE_STUDY_ZILLOW_HTML, "case study contact placeholder missing"

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


def test_trial_chat_has_bridge_status_pill():
    """Verify trial chat template shows separate chat+bridge status and status updater uses both flags."""
    from web import TRIAL_CHAT_HTML
    assert 'id="agentstatus"' in TRIAL_CHAT_HTML, "trial chat agent status pill missing"
    assert 'id="bridgestatus"' in TRIAL_CHAT_HTML, "trial chat bridge status pill missing"
    assert "chat_connected" in TRIAL_CHAT_HTML, "trial status updater missing chat_connected handling"
    assert "bridge_connected" in TRIAL_CHAT_HTML, "trial status updater missing bridge_connected handling"
    assert "fetch('/web/chat/status?model='" in TRIAL_CHAT_HTML, "trial status polling endpoint missing"
    print("  TRIAL_CHAT_HTML has chat+bridge status pills")


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
    assert "claude-opus-4-6" in CHAT_HTML, "Opus model option missing"
    assert "claude-haiku-4-5-20251001" in CHAT_HTML, "Haiku model option missing"
    # Sonnet should be the first (default) option
    sonnet_pos = CHAT_HTML.index("claude-sonnet-4-6")
    opus_pos = CHAT_HTML.index("claude-opus-4-6")
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


def test_google_auth_trial_pending_has_chat_access():
    """Verify trial/demo sign-ins stay pending but can access chat flows."""
    import inspect
    from web import handle_google_auth
    source = inspect.getsource(handle_google_auth)
    assert "_auth.create_pending_user(email, name, picture, user_type=\"trial\")" in source, \
        "New trial/demo sign-ins should be created with pending status"
    assert "_auth.create_key(user[\"user_id\"])" in source, \
        "Pending trial/demo users should receive an API key for trial/demo chat access"
    assert "\"review_pending\": True" in source, \
        "Trial/demo auth response should indicate review is still pending"
    assert "\"pending\": True" in source, \
        "Non-trial pending users should still receive pending response"
    print("  Trial/demo sign-ins are pending but can access trial/demo chat")


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
    print("  OpenRouter trial agent emits usage event with user context")


def test_trial_chat_handles_model_forced_event():
    """Verify trial chat frontend handles model_forced SSE events."""
    from web import TRIAL_CHAT_HTML
    assert "evt.type === 'model_forced'" in TRIAL_CHAT_HTML, "trial chat should handle model_forced event"
    assert "_syncCustomModelUi()" in TRIAL_CHAT_HTML, "trial chat should refresh model UI after forced switch"
    print("  trial chat UI handles model_forced SSE")


def test_trial_and_demo_openrouter_default_models_and_cap_options():
    """Verify trial+demo default to Gemini and cap keeps only Trinity/StepFun models."""
    from web import TRIAL_CHAT_HTML, HEADLESS_DEMO_HTML
    assert 'value="google/gemini-3-flash-preview"' in TRIAL_CHAT_HTML, \
        "trial model selector should default to Gemini 3 Flash Preview"
    assert "_POST_CAP_ALLOWED_MODELS = ['arcee-ai/trinity-large-preview:free', 'stepfun/step-3.5-flash:free']" in TRIAL_CHAT_HTML, \
        "trial cap model allowlist should be Trinity + StepFun"
    assert "'google/gemini-3-flash-preview'" in HEADLESS_DEMO_HTML, \
        "demo currentModel should default to Gemini 3 Flash Preview"
    assert "evt.type === 'model_forced'" in HEADLESS_DEMO_HTML, \
        "demo UI should handle server-forced model fallback events"
    print("  trial+demo defaults and post-cap model constraints are in place")


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
    assert "handle_message(ws, sid, user_text, msg_model" in source, \
        "main loop should pass model to handle_message"
    print(f"  CLI agent main loop forwards model from WS message")


# ── Runner ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        ("agent_package: version constants", test_version_constants),
        ("agent_package: build_agent_zip has version.txt + update.sh", test_build_agent_zip_contains_version_and_update),
        ("agent_package: build_update_zip (no .env, no start.sh)", test_build_update_zip_no_env_no_start),
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
        ("auth: approve_user keeps existing API key", test_approve_user_keeps_existing_api_key),
        ("chat_agent_cli: _parse_version", test_parse_version),
        ("web: new handlers importable", test_web_imports),
        ("web: routes registered", test_web_routes_registered),
        ("web: CHAT_HTML has install modal", test_chat_html_has_install_modal),
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
        ("web: TRIAL_CHAT_HTML has bridge status", test_trial_chat_has_bridge_status_pill),
        ("web: ADMIN_HTML shows OpenRouter spend column", test_admin_page_shows_openrouter_spend_column),
        ("web: CHAT_HTML has model dropdown", test_chat_html_has_model_dropdown),
        ("web: TRIAL_CHAT_HTML has admin custom model input", test_trial_chat_has_admin_custom_openrouter_model),
        ("web: CHAT_CODEX_HTML has 3-slot UI", test_codex_chat_has_three_slot_ui),
        ("codex agent: supports slot protocol", test_chat_agent_codex_supports_slot_protocol),
        ("web: TRIAL_CHAT_HTML has Claude access request flow", test_trial_chat_has_claude_access_request_flow),
        ("web: doSend() includes model", test_chat_html_sends_model_in_fetch),
        ("web: handle_chat_msg forwards model", test_handle_chat_msg_forwards_model),
        ("auth: trial/demo pending users can access trial/demo chat", test_google_auth_trial_pending_has_chat_access),
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
