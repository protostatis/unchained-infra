"""Structural tests for centralized route specs."""

from __future__ import annotations

import asyncio
import importlib
import os
import unittest
from types import SimpleNamespace

os.environ.setdefault("JWT_SECRET", "test-jwt-secret")

from yarl import URL

import web
import web_routes
from web_routes import ROUTE_SPECS


class TestWebRouteSpecs(unittest.TestCase):
    def test_route_specs_have_no_duplicate_method_path_pairs(self):
        seen: set[tuple[str, str]] = set()
        for method, path, _handler in ROUTE_SPECS:
            key = (method, path)
            self.assertNotIn(key, seen, f"duplicate route spec: {key}")
            seen.add(key)

    def test_route_spec_handlers_resolve_to_callables(self):
        missing: list[str] = []
        not_callable: list[str] = []

        for _method, _path, handler_name in ROUTE_SPECS:
            handler = None
            if ":" in handler_name:
                module_name, func_name = handler_name.split(":", 1)
                module = importlib.import_module(module_name)
                handler = getattr(module, func_name, None)
            else:
                handler = getattr(web, handler_name, None)
            if handler is None:
                missing.append(handler_name)
            elif not callable(handler):
                not_callable.append(handler_name)

        self.assertEqual(missing, [], f"missing handlers in route specs: {sorted(set(missing))}")
        self.assertEqual(not_callable, [], f"non-callable handlers: {sorted(set(not_callable))}")

    def test_register_route_specs_supports_module_qualified_handlers(self):
        app = web.web.Application()
        web_routes.register_route_specs(
            app,
            [("GET", "/__route-spec-test", "web:handle_index")],
            {},
            include_dev_auth=False,
            dev_auth_handler=web.handle_dev_auth,
        )

        routes = {
            (route.method, route.resource.canonical)
            for route in app.router.routes()
            if route.method in {"GET", "POST"}
        }
        self.assertIn(("GET", "/__route-spec-test"), routes)


class TestClaimOAuthRouteResolution(unittest.TestCase):
    """Router-resolution tests for the claim OAuth surface.

    The claim flow lives under the dedicated /workspace/* namespace. The
    site's own login OAuth routes (/auth/facebook/..., /auth/github/...)
    resolve to the login handlers and the claim routes resolve to the claim
    handlers — neither can shadow the other.
    """

    @classmethod
    def setUpClass(cls):
        cls.app = web.web.Application()
        web._register_routes(cls.app)

    def _resolve(self, method: str, path: str):
        req = SimpleNamespace(
            method=method,
            rel_url=URL(path),
            headers={},
            match_info={},
        )
        return asyncio.run(self.app.router.resolve(req))

    def test_login_oauth_routes_unshadowed(self):
        """The claim flow must not shadow the site's login OAuth routes."""
        for path, expected in (
            ("/auth/facebook/start", "handle_facebook_start"),
            ("/auth/facebook/callback", "handle_facebook_callback"),
            ("/auth/github/start", "handle_github_start"),
            ("/auth/github/callback", "handle_github_callback"),
        ):
            match = self._resolve("GET", path)
            self.assertEqual(match.handler.__name__, expected, f"route {path}")

    def test_claim_oauth_routes_resolve_to_claim_handlers(self):
        """The claim OAuth start/callback resolve to the claim handlers only
        under the dedicated workspace namespace."""
        cases = (
            ("GET", "/workspace/oauth/github/start", "handle_claim_oauth_start"),
            ("GET", "/workspace/oauth/github/callback", "handle_claim_oauth_callback"),
            ("GET", "/workspace/oauth/facebook/start", "handle_claim_oauth_start"),
            ("GET", "/workspace/oauth/facebook/callback", "handle_claim_oauth_callback"),
            ("GET", "/workspace/oauth/google/start", "handle_claim_oauth_start"),
            ("GET", "/workspace/oauth/google/callback", "handle_claim_oauth_callback"),
            ("POST", "/workspace/oauth/google", "handle_claim_google_token"),
        )
        for method, path, expected in cases:
            match = self._resolve(method, path)
            self.assertEqual(match.handler.__name__, expected, f"route {method} {path}")

    def test_claim_surface_routes(self):
        for method, path, expected in (
            ("GET", "/workspace/auth/claim", "handle_fin_workspace_auth_claim_page"),
            ("POST", "/workspace/claim", "handle_fin_workspace_browser_claim"),
            ("GET", "/workspace/claims/fcl-1", "handle_fin_workspace_browser_get_claim"),
            ("GET", "/workspace/workspace", "handle_fin_workspace_browser_get_workspace"),
            ("GET", "/workspace/snapshots", "handle_fin_workspace_browser_get_snapshots"),
            ("GET", "/workspace/runtime/status", "handle_fin_workspace_browser_runtime_status"),
            ("GET", "/workspace/done", "handle_claim_done"),
            ("GET", "/terminal", "handle_fin_workspace_terminal_proxy"),
            ("GET", "/terminal/ws", "handle_fin_workspace_terminal_proxy"),
        ):
            match = self._resolve(method, path)
            self.assertEqual(match.handler.__name__, expected, f"route {method} {path}")

    def test_no_claim_route_under_legacy_auth_or_api_paths(self):
        """No claim handler may be reachable at the legacy /auth/{provider}/*
        or /api/* paths that would collide with the site's login routes."""
        from web_app.handlers import fin_workspace_auth
        from web_app.handlers import fin_workspace

        claim_handlers = {
            fin_workspace_auth.handle_claim_oauth_start,
            fin_workspace_auth.handle_claim_oauth_callback,
            fin_workspace_auth.handle_claim_google_token,
            fin_workspace.handle_fin_workspace_browser_claim,
        }
        for route in self.app.router.routes():
            if route.method not in {"GET", "POST"}:
                continue
            canonical = route.resource.canonical
            if canonical.startswith("/auth/") or canonical.startswith("/api/"):
                self.assertNotIn(route.handler, claim_handlers,
                                 f"claim handler registered at {canonical}")


if __name__ == "__main__":
    unittest.main()
