"""Route specs and route registration helpers for web app.

Keeping this table outside ``web.py`` lowers context size when adding or
reviewing endpoints.
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping, Sequence

from aiohttp import web

RouteSpec = tuple[str, str, str]


ROUTE_SPECS: tuple[RouteSpec, ...] = (
    ("GET", "/favicon.svg", "handle_favicon"),
    ("GET", "/", "handle_index"),
    ("GET", "/test", "handle_test"),
    ("POST", "/auth/google", "handle_google_auth"),
    ("POST", "/auth/request-claude-access", "handle_request_claude_access"),
    ("POST", "/auth/logout", "handle_logout"),
    ("GET", "/auth/me", "handle_auth_me"),
    ("POST", "/web/cmd", "handle_cmd"),
    ("GET", "/setup", "handle_setup_page"),
    ("GET", "/scheduler", "handle_scheduler_page"),
    ("GET", "/web/scheduler/jobs", "handle_scheduler_jobs"),
    ("POST", "/web/scheduler/jobs", "handle_scheduler_jobs"),
    ("GET", "/web/scheduler/history", "handle_scheduler_history"),
    ("POST", "/web/scheduler/preview", "handle_scheduler_preview"),
    ("GET", "/admin", "handle_admin_page"),
    ("GET", "/admin/users", "handle_admin_users"),
    ("GET", "/admin/pending", "handle_admin_pending"),
    ("POST", "/admin/approve", "handle_admin_approve"),
    ("POST", "/admin/reject", "handle_admin_reject"),
    ("GET", "/chat", "handle_chat_redirect"),
    ("GET", "/trial", "handle_trial_page"),
    ("GET", "/chat-gemini", "handle_chat_gemini_page"),
    ("GET", "/chat-codex", "handle_chat_codex_page"),
    ("GET", "/chat-claude", "handle_chat_claude_page"),
    ("GET", "/demo", "handle_demo_page"),
    ("GET", "/case-study/zillow-rental", "handle_case_study_zillow"),
    ("GET", "/local", "handle_local_page"),
    ("GET", "/install", "handle_install_page"),
    ("GET", "/app", "handle_claude_page"),
    ("GET", "/chat/ws", "handle_chat_ws"),
    ("POST", "/web/chat", "handle_chat_msg"),
    ("POST", "/web/chat/cancel", "handle_chat_cancel"),
    ("GET", "/web/chat/status", "handle_chat_status"),
    ("GET", "/web/chat/history", "handle_chat_history"),
    ("POST", "/web/chat/new", "handle_chat_new"),
    ("GET", "/web/chat/slots", "handle_chat_slots"),
    ("POST", "/web/chat/switch", "handle_chat_switch"),
    ("GET", "/web/download-agent", "handle_download_agent"),
    ("GET", "/web/download-installer", "handle_download_installer"),
    ("POST", "/web/install-token", "handle_install_token"),
    ("POST", "/web/install/claim/start", "handle_install_claim_start"),
    ("POST", "/web/install/claim/poll", "handle_install_claim_poll"),
    ("POST", "/web/install/claim/approve", "handle_install_claim_approve"),
    ("POST", "/web/install/bootstrap", "handle_install_bootstrap"),
    ("GET", "/install/script", "handle_install_script"),
    ("GET", "/install/windows/script", "handle_install_script_windows"),
    ("GET", "/install/{token}", "handle_install_script"),
    ("GET", "/install/windows/{token}", "handle_install_script_windows"),
    ("GET", "/install/claim/{claim_id}", "handle_install_claim_page"),
    ("GET", "/trial/connector", "handle_trial_connector"),
    ("POST", "/trial/token", "handle_trial_token"),
    ("GET", "/trial/script", "handle_trial_script"),
    ("GET", "/trial/{token}", "handle_trial_script"),
    ("GET", "/web/agent/version", "handle_agent_version"),
    ("GET", "/web/agent/files", "handle_agent_files"),
    ("GET", "/web/provision/profiles", "handle_provision_profiles"),
    ("POST", "/web/provision/start", "handle_provision_start"),
    ("GET", "/web/provision/status", "handle_provision_status"),
    ("POST", "/web/provision/confirm", "handle_provision_confirm"),
    ("POST", "/web/provision/save-manual", "handle_provision_save_manual"),
    ("POST", "/web/provision/revoke", "handle_provision_revoke"),
)


def register_route_specs(
    app: web.Application,
    route_specs: Sequence[RouteSpec],
    handler_lookup: Mapping[str, object],
    *,
    include_dev_auth: bool,
    dev_auth_handler,
) -> None:
    """Register routes by handler name.

    ``handler_lookup`` is typically ``globals()`` from the caller module.
    For lower-context extension work, handler names may also use
    ``module_path:function_name`` and will be imported lazily.
    """

    for method, path, handler_name in route_specs:
        handler = handler_lookup.get(handler_name)
        if handler is None and ":" in handler_name:
            module_path, func_name = handler_name.split(":", 1)
            module = importlib.import_module(module_path)
            handler = getattr(module, func_name, None)
        if handler is None:
            raise KeyError(f"Unknown handler: {handler_name}")
        if method == "GET":
            app.router.add_get(path, handler)
        elif method == "POST":
            app.router.add_post(path, handler)
        else:
            raise ValueError(f"Unsupported route method: {method}")

    if include_dev_auth:
        app.router.add_post("/auth/dev", dev_auth_handler)
