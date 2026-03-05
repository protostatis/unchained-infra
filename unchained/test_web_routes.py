"""Structural tests for centralized route specs."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("JWT_SECRET", "test-jwt-secret")

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
            handler = getattr(web, handler_name, None)
            if handler is None:
                missing.append(handler_name)
            elif not callable(handler):
                not_callable.append(handler_name)

        self.assertEqual(missing, [], f"missing handlers in web.py: {sorted(set(missing))}")
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


if __name__ == "__main__":
    unittest.main()
