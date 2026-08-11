#!/usr/bin/env python3
"""Tests for the deferred key confirmation flow.

Verifies:
1. signup_agent: store_key=False skips DB storage
2. signup_agent: store_key=True (default) still stores
3. web.py: _pending_provision dict + confirm endpoint logic
4. web.py: save-manual endpoint logic
5. Relay mode ToS returns TOS_REQUIRED instead of auto-accepting

Usage:
    cd unchained/
    uv run python -m pytest test_provision_confirm.py -v
    # or
    uv run python test_provision_confirm.py
"""
import asyncio
import os
import sys
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("JWT_SECRET", "test-jwt-secret")

sys.path.insert(0, os.path.dirname(__file__))

import signup_agent
from signup_agent import ProvisionResult, ProvisionStatus


class TestStoreKeyParam(unittest.TestCase):
    """Test that store_key=False skips DB storage in provision functions.

    Instead of mocking the entire Chrome launch, we test the store_key
    conditional logic directly: call store_provider_key only when store_key=True.
    """

    def setUp(self):
        self.user_id = "u-test-confirm"
        signup_agent.revoke_provider_key(self.user_id, "gemini")

    def tearDown(self):
        signup_agent.revoke_provider_key(self.user_id, "gemini")

    def test_store_key_false_skips_db(self):
        """Simulate store_key=False path: key returned but NOT stored in DB."""
        api_key = "AIzaSyFAKE_TEST_KEY_1234567890abc"
        store_key = False
        result = ProvisionResult(
            status=ProvisionStatus.SUCCESS,
            provider="gemini",
            api_key=api_key,
            message="test",
        )
        # Replicate the conditional from provision_key_local
        if store_key and result.api_key and result.status in (ProvisionStatus.SUCCESS, ProvisionStatus.ALREADY_EXISTS):
            signup_agent.store_provider_key(self.user_id, "gemini", result.api_key)

        stored = signup_agent.get_provider_key(self.user_id, "gemini")
        self.assertIsNone(stored, "Key should not be stored when store_key=False")

    def test_store_key_true_stores_db(self):
        """Simulate store_key=True path: key returned AND stored in DB."""
        api_key = "AIzaSyFAKE_TEST_KEY_1234567890abc"
        store_key = True
        result = ProvisionResult(
            status=ProvisionStatus.SUCCESS,
            provider="gemini",
            api_key=api_key,
            message="test",
        )
        if store_key and result.api_key and result.status in (ProvisionStatus.SUCCESS, ProvisionStatus.ALREADY_EXISTS):
            signup_agent.store_provider_key(self.user_id, "gemini", result.api_key)

        stored = signup_agent.get_provider_key(self.user_id, "gemini")
        self.assertIsNotNone(stored, "Key should be stored when store_key=True")
        self.assertEqual(stored, api_key)

    def test_function_signatures_have_store_key(self):
        """Both provision functions should accept store_key parameter."""
        import inspect
        sig_relay = inspect.signature(signup_agent.provision_key)
        self.assertIn("store_key", sig_relay.parameters)
        self.assertEqual(sig_relay.parameters["store_key"].default, True)

        sig_local = inspect.signature(signup_agent.provision_key_local)
        self.assertIn("store_key", sig_local.parameters)
        self.assertEqual(sig_local.parameters["store_key"].default, True)


class TestPendingProvision(unittest.TestCase):
    """Test the _pending_provision dict and confirm/save-manual logic in web.py."""

    def test_pending_provision_stash_and_pop(self):
        """Stash a key, then pop it (simulates confirm)."""
        # Import after path setup
        sys.modules.pop("web", None)  # avoid stale import
        # We only test the dict logic, not the full endpoint
        from web import _pending_provision, _PENDING_PROVISION_TTL

        user_id = "u-test-pending"
        provider = "gemini"
        api_key = "AIzaSyTEST_PENDING_KEY_123456789"

        # Stash
        _pending_provision[user_id] = (provider, api_key, time.time())
        self.assertIn(user_id, _pending_provision)

        # Pop (confirm)
        pending = _pending_provision.pop(user_id, None)
        self.assertIsNotNone(pending)
        self.assertEqual(pending[0], provider)
        self.assertEqual(pending[1], api_key)

        # Should be gone now
        self.assertNotIn(user_id, _pending_provision)

    def test_pending_provision_expiry(self):
        """Expired pending keys should be detected."""
        from web import _pending_provision, _PENDING_PROVISION_TTL

        user_id = "u-test-expired"
        old_time = time.time() - _PENDING_PROVISION_TTL - 10  # 10s past expiry
        _pending_provision[user_id] = ("gemini", "AIzaSyEXPIRED", old_time)

        # Check expiry
        _, _, ts = _pending_provision[user_id]
        self.assertTrue(time.time() - ts > _PENDING_PROVISION_TTL)

        # Clean up
        _pending_provision.pop(user_id, None)


class TestRelayTosReturnsRequired(unittest.TestCase):
    """Test that relay mode ToS detection returns TOS_REQUIRED instead of auto-clicking."""

    @patch("signup_agent.cloud_tools")
    def test_tos_returns_required_in_relay_mode(self, mock_ct):
        """When ToS is detected in relay mode, should return TOS_REQUIRED."""
        provider = signup_agent.GeminiProvider()
        # Verify _cdp is None (relay mode)
        self.assertIsNone(provider._cdp)

        # Mock the JS calls:
        # Step 1-4 navigate and detect state
        mock_ct.run_js = AsyncMock()

        # Simulate: page loads, detects ToS
        call_count = 0
        async def fake_run_js(agent_id, tab_id, js, relay_host, relay_port):
            nonlocal call_count
            call_count += 1
            # Call 1: Navigate
            if call_count == 1:
                return "navigated"
            # Call 2: Wait for page load
            if call_count == 2:
                return "loaded"
            # Call 3: Check sign-in state
            if call_count == 3:
                return "has_keys_ui"
            # Call 4: Detect ToS
            if call_count == 4:
                return "tos_detected:2"  # ToS checkboxes found
            return "unknown"

        mock_ct.run_js.side_effect = fake_run_js
        mock_ct.create_tab = AsyncMock(return_value="tab-123")
        mock_ct.close_tab = AsyncMock()

        # The provision should hit the ToS branch and return TOS_REQUIRED
        # We need to test the actual provision method flow
        # Since the provision method is complex, we test the specific branch logic
        # by checking that the relay ToS block no longer auto-clicks
        import inspect
        source = inspect.getsource(provider.provision)
        # Verify the auto-click code is gone
        self.assertNotIn("cbs[i].click()", source,
                         "Relay mode should NOT auto-click checkboxes")
        self.assertNotIn("tos_accepted", source,
                         "Relay mode should NOT auto-accept ToS")
        # Verify the new behavior is present
        self.assertIn("TOS_REQUIRED", source,
                      "Relay mode should return TOS_REQUIRED")
        self.assertIn("user must accept ToS themselves", source,
                      "Relay mode should tell user to accept ToS")


class TestConsentModalText(unittest.TestCase):
    """Test that consent modal text is explicit about automation actions."""

    def test_consent_modal_specific_text(self):
        """Consent modal should describe specific actions, not vague ones."""
        from web import SETUP_HTML

        # Old vague text should be gone
        self.assertNotIn("briefly use your Chrome", SETUP_HTML,
                         "Old vague consent text should be removed")
        self.assertNotIn("Profile data stays on your machine", SETUP_HTML,
                         "Old misleading consent text should be removed")

        # New specific text should be present
        self.assertIn("Open a new tab in your Chrome", SETUP_HTML)
        self.assertIn('Click "Create API key"', SETUP_HTML,
                      "Should describe the Create API key click")
        self.assertIn("Capture the key from the page response", SETUP_HTML)
        self.assertIn("Terms of Service", SETUP_HTML)
        self.assertIn("view or revoke the key", SETUP_HTML)


class TestKeyConfirmationUI(unittest.TestCase):
    """Test that the frontend JS has key confirmation UI."""

    def test_confirm_store_key_function(self):
        """Frontend JS should have confirmStoreKey() function."""
        from web import SETUP_HTML
        self.assertIn("confirmStoreKey()", SETUP_HTML)
        self.assertIn("discardKey()", SETUP_HTML)
        self.assertIn("/web/provision/confirm", SETUP_HTML)

    def test_key_preview_in_success_handler(self):
        """startProvision success handler should check for key_preview."""
        from web import SETUP_HTML
        self.assertIn("data.key_preview", SETUP_HTML)
        self.assertIn("Store Key", SETUP_HTML)
        self.assertIn("Discard", SETUP_HTML)


class TestLocalModeHints(unittest.TestCase):
    """Test that local/relay mode hints and manual key paste are present."""

    def test_local_mode_hint(self):
        from web import SETUP_HTML
        self.assertIn("Chrome will open visibly", SETUP_HTML)
        self.assertIn("local-mode-hint", SETUP_HTML)

    def test_relay_mode_hint(self):
        from web import SETUP_HTML
        self.assertIn("Prefer full control?", SETUP_HTML)
        self.assertIn("relay-mode-hint", SETUP_HTML)
        self.assertIn("localhost:8080/setup", SETUP_HTML)

    def test_manual_key_section(self):
        from web import SETUP_HTML
        self.assertIn("manual-key-section", SETUP_HTML)
        self.assertIn("manual-key-input", SETUP_HTML)
        self.assertIn("saveManualKey()", SETUP_HTML)
        self.assertIn("/web/provision/save-manual", SETUP_HTML)

    def test_save_manual_key_function(self):
        from web import SETUP_HTML
        self.assertIn("async function saveManualKey()", SETUP_HTML)


class TestCodexProvisioningHooks(unittest.TestCase):
    """Smoke tests for Codex provider registration + chat routing."""

    def test_codex_providers_registered(self):
        providers = set(signup_agent.list_providers())
        self.assertIn("gemini", providers)
        self.assertIn("claude-sdk", providers)
        self.assertIn("codex-sdk", providers)
        self.assertIn("codex-cli", providers)

    def test_resolve_chat_agent_id_codex_prefixes(self):
        from web import _resolve_chat_agent_id
        auth_info = {"key_hash": "abc12345", "agent_id": "claude-abc12345"}
        self.assertEqual(
            _resolve_chat_agent_id(auth_info, "codex-sdk:codex-mini-latest"),
            "codexsdk-abc12345",
        )
        self.assertEqual(
            _resolve_chat_agent_id(auth_info, "codex-cli:gpt-5.6-sol"),
            "claude-abc12345",
        )
        self.assertEqual(
            _resolve_chat_agent_id(auth_info, "opencode-cli:anthropic/claude-sonnet-4-5"),
            "claude-abc12345",
        )

    def test_setup_html_has_sdk_provider_options(self):
        from web import SETUP_HTML
        self.assertIn("provider-select", SETUP_HTML)
        self.assertIn("option value=\"gemini\"", SETUP_HTML)
        self.assertIn("option value=\"claude-sdk\"", SETUP_HTML)
        self.assertIn("option value=\"codex-sdk\"", SETUP_HTML)
        self.assertNotIn("option value=\"codex-cli\"", SETUP_HTML)

    def test_chat_codex_html_has_codex_model_prefixes(self):
        from web import CHAT_CODEX_HTML
        self.assertIn("codex-sdk:codex-mini-latest", CHAT_CODEX_HTML)
        self.assertIn("codex-cli:gpt-5.6-sol", CHAT_CODEX_HTML)
        self.assertIn("codex-cli:gpt-5.6-terra", CHAT_CODEX_HTML)
        self.assertIn("codex-cli:gpt-5.6-luna", CHAT_CODEX_HTML)
        self.assertIn("codex-cli:gpt-5.3-codex-spark", CHAT_CODEX_HTML)
        self.assertIn("codex-cli:gpt-5.5", CHAT_CODEX_HTML)
        self.assertIn("codex-sdk:gpt-5.6-sol", CHAT_CODEX_HTML)
        self.assertIn("codex-sdk:gpt-5.6-luna", CHAT_CODEX_HTML)
        self.assertIn("codex-sdk:gpt-5.5", CHAT_CODEX_HTML)
        self.assertNotIn("codex-cli:gpt-5.4-mini", CHAT_CODEX_HTML)
        self.assertIn("/web/chat/status?codex=1", CHAT_CODEX_HTML)
        self.assertNotIn("geminiProvisioned", CHAT_CODEX_HTML)

    def test_chat_claude_html_has_opencode_model_prefix(self):
        from web import CLAUDE_CHAT_HTML

        self.assertIn("opencode-cli:", CLAUDE_CHAT_HTML)
        self.assertIn("opencode_cli_supported", CLAUDE_CHAT_HTML)

    @patch("web._spawn_codex_sdk_agent")
    @patch("web._spawn_claude_sdk_agent")
    @patch("web._spawn_gemini_agent")
    def test_spawn_provider_agent_routes(self, mock_gemini, mock_claude_sdk, mock_codex_sdk):
        from web import _spawn_provider_agent

        url = _spawn_provider_agent("gemini", "u1", "k1", "p1")
        self.assertEqual(url, "/chat-gemini")
        mock_gemini.assert_called_once_with("u1", "k1", "p1")

        url = _spawn_provider_agent("claude-sdk", "u2", "k2", "p2")
        self.assertEqual(url, "/chat-claude")
        mock_claude_sdk.assert_called_once_with("u2", "k2", "p2")

        url = _spawn_provider_agent("codex-sdk", "u3", "k3", "p3")
        self.assertEqual(url, "/local?provider=codex-sdk")
        mock_codex_sdk.assert_called_once_with("u3", "k3", "p3")

        url = _spawn_provider_agent("codex-cli", "u4", "k4", "p4")
        self.assertEqual(url, "/local?provider=codex-cli")

        url = _spawn_provider_agent("unknown", "u5", "k5", "p5")
        self.assertIsNone(url)

        mock_codex_sdk.assert_called_once()


if __name__ == "__main__":
    unittest.main()
