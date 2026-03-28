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
    ("GET", "/og-image.png", "handle_og_image"),
    ("GET", "/robots.txt", "handle_robots_txt"),
    ("GET", "/sitemap.xml", "handle_sitemap_xml"),
    ("GET", "/google83c650022d8db556.html", "handle_google_verification"),
    ("GET", "/", "handle_index"),
    ("GET", "/tab", "web_app.handlers.pages:handle_tab_page"),
    ("GET", "/mcp-guide", "web_app.handlers.pages:handle_mcp_guide_page"),
    ("GET", "/test", "handle_test"),
    ("GET", "/privacy", "web_app.handlers.pages:handle_privacy_page"),
    ("GET", "/privacy-policy", "web_app.handlers.pages:handle_privacy_page"),
    ("GET", "/data-deletion", "web_app.handlers.pages:handle_data_deletion_page"),
    ("POST", "/auth/google", "web_app.handlers.auth_admin:handle_google_auth"),
    ("GET", "/auth/facebook/start", "web_app.handlers.auth_admin:handle_facebook_start"),
    ("GET", "/auth/facebook/callback", "web_app.handlers.auth_admin:handle_facebook_callback"),
    ("GET", "/auth/github/start", "web_app.handlers.auth_admin:handle_github_start"),
    ("GET", "/auth/github/callback", "web_app.handlers.auth_admin:handle_github_callback"),
    ("POST", "/auth/request-claude-access", "web_app.handlers.auth_admin:handle_request_claude_access"),
    ("POST", "/auth/logout", "web_app.handlers.auth_admin:handle_logout"),
    ("GET", "/auth/me", "web_app.handlers.auth_admin:handle_auth_me"),
    ("POST", "/web/analytics/event", "web_app.handlers.analytics:handle_analytics_event"),
    ("POST", "/web/analytics/events", "web_app.handlers.analytics:handle_analytics_events"),
    ("POST", "/web/cmd", "handle_cmd"),
    ("GET", "/setup", "web_app.handlers.auth_admin:handle_setup_page"),
    ("GET", "/scheduler", "web_app.handlers.auth_admin:handle_scheduler_page"),
    ("GET", "/web/scheduler/jobs", "web_app.handlers.auth_admin:handle_scheduler_jobs"),
    ("POST", "/web/scheduler/jobs", "web_app.handlers.auth_admin:handle_scheduler_jobs"),
    ("GET", "/web/scheduler/history", "web_app.handlers.auth_admin:handle_scheduler_history"),
    ("POST", "/web/scheduler/preview", "web_app.handlers.auth_admin:handle_scheduler_preview"),
    ("POST", "/web/scheduler/agent/list", "web_app.handlers.auth_admin:handle_scheduler_agent_list"),
    ("POST", "/web/scheduler/agent/preview", "web_app.handlers.auth_admin:handle_scheduler_agent_preview"),
    ("POST", "/web/scheduler/agent/upsert", "web_app.handlers.auth_admin:handle_scheduler_agent_upsert"),
    ("POST", "/web/scheduler/agent/delete", "web_app.handlers.auth_admin:handle_scheduler_agent_delete"),
    ("GET", "/admin", "web_app.handlers.auth_admin:handle_admin_page"),
    ("GET", "/landing-v2", "web_app.handlers.auth_admin:handle_landing_v2"),
    ("GET", "/admin/users", "web_app.handlers.auth_admin:handle_admin_users"),
    ("GET", "/admin/analytics/funnel", "web_app.handlers.analytics:handle_admin_analytics_funnel"),
    ("GET", "/admin/pending", "web_app.handlers.auth_admin:handle_admin_pending"),
    ("POST", "/admin/approve", "web_app.handlers.auth_admin:handle_admin_approve"),
    ("POST", "/admin/reject", "web_app.handlers.auth_admin:handle_admin_reject"),
    ("GET", "/chat", "web_app.handlers.pages:handle_chat_redirect"),
    ("GET", "/trial", "web_app.handlers.pages:handle_trial_page"),
    ("GET", "/chat-gemini", "web_app.handlers.pages:handle_chat_gemini_page"),
    ("GET", "/chat-codex", "web_app.handlers.pages:handle_chat_codex_page"),
    ("GET", "/chat-claude", "web_app.handlers.pages:handle_chat_claude_page"),
    ("GET", "/first-look", "web_app.handlers.pages:handle_first_look_page"),
    ("GET", "/first-look-preview", "web_app.handlers.pages:handle_first_look_preview_page"),
    ("GET", "/demo", "web_app.handlers.pages:handle_demo_page"),
    ("GET", "/mcp", "web_app.handlers.pages:handle_mcp_page"),
    ("GET", "/r/{slug}", "web_app.handlers.pages:handle_published_result"),
    ("POST", "/web/publish-result", "web_app.handlers.pages:handle_publish_result"),
    ("GET", "/web/publish/pending", "web_app.handlers.pages:handle_pending_results"),
    ("POST", "/web/publish/approve", "web_app.handlers.pages:handle_approve_result"),
    ("POST", "/web/publish/reject", "web_app.handlers.pages:handle_reject_result"),
    ("GET", "/case-study/zillow-rental", "web_app.handlers.pages:handle_case_study_zillow"),
    ("GET", "/use/apartment-hunting", "web_app.handlers.pages:handle_use_case_apartment"),
    ("GET", "/use/flight-comparison", "web_app.handlers.pages:handle_use_case_flights"),
    ("GET", "/use/competitor-monitoring", "web_app.handlers.pages:handle_use_case_competitor"),
    ("GET", "/use/price-tracking", "web_app.handlers.pages:handle_use_case_price_tracking"),
    ("GET", "/labs/research-desk", "web_app.handlers.pages:handle_research_desk_page"),
    ("GET", "/labs/you-navigate", "web_app.handlers.x_manager_demo:handle_x_manager_demo_page"),
    ("GET", "/labs/x-manager", "web_app.handlers.x_manager_demo:handle_x_manager_demo_page"),
    ("GET", "/local", "web_app.handlers.pages:handle_local_page"),
    ("GET", "/install", "web_app.handlers.pages:handle_install_page"),
    ("GET", "/app", "web_app.handlers.pages:handle_claude_page"),
    ("POST", "/web/labs/you-navigate/run", "web_app.handlers.x_manager_demo:handle_x_manager_demo_run"),
    ("POST", "/web/labs/x-manager/run", "web_app.handlers.x_manager_demo:handle_x_manager_demo_run"),
    ("GET", "/chat/ws", "web_app.handlers.chat_stream:handle_chat_ws"),
    ("POST", "/web/overlay-followup", "web_app.handlers.overlay_ws:handle_overlay_followup"),
    ("POST", "/web/chat", "web_app.handlers.chat_stream:handle_chat_msg"),
    ("POST", "/web/chat/cancel", "web_app.handlers.chat_stream:handle_chat_cancel"),
    ("GET", "/web/chat/status", "web_app.handlers.chat_flow:handle_chat_status"),
    ("GET", "/web/first-look/preflight", "web_app.handlers.chat_flow:handle_first_look_preflight"),
    ("GET", "/web/first-look/preview/ws", "web_app.handlers.chat_flow:handle_first_look_preview_ws"),
    ("POST", "/web/first-look/signal", "web_app.handlers.chat_flow:handle_first_look_signal"),
    ("POST", "/web/chat/update-client", "web_app.handlers.chat_flow:handle_chat_update_client"),
    ("POST", "/web/chat/install-research-desk", "web_app.handlers.chat_flow:handle_chat_install_research_desk"),
    ("GET", "/web/chat/history", "web_app.handlers.chat_flow:handle_chat_history"),
    ("POST", "/web/chat/new", "web_app.handlers.chat_flow:handle_chat_new"),
    ("GET", "/web/chat/slots", "web_app.handlers.chat_flow:handle_chat_slots"),
    ("POST", "/web/chat/switch", "web_app.handlers.chat_flow:handle_chat_switch"),
    ("GET", "/web/chat/archives", "web_app.handlers.chat_flow:handle_chat_archives"),
    ("POST", "/web/chat/restore-archive", "web_app.handlers.chat_flow:handle_chat_restore_archive"),
    ("POST", "/web/chat/delete-archive", "web_app.handlers.chat_flow:handle_chat_delete_archive"),
    ("GET", "/web/download-agent", "web_app.handlers.install_flow:handle_download_agent"),
    ("GET", "/web/download-installer", "web_app.handlers.install_flow:handle_download_installer"),
    ("POST", "/web/install-token", "web_app.handlers.install_flow:handle_install_token"),
    ("POST", "/web/install/claim/start", "web_app.handlers.install_flow:handle_install_claim_start"),
    ("POST", "/web/install/claim/poll", "web_app.handlers.install_flow:handle_install_claim_poll"),
    ("POST", "/web/install/claim/approve", "web_app.handlers.install_flow:handle_install_claim_approve"),
    ("POST", "/web/install/bootstrap", "web_app.handlers.install_flow:handle_install_bootstrap"),
    ("GET", "/install.sh", "web_app.handlers.install_flow:handle_public_install_script"),
    ("GET", "/install/script", "web_app.handlers.install_flow:handle_install_script"),
    ("GET", "/install/windows/script", "web_app.handlers.install_flow:handle_install_script_windows"),
    ("GET", "/install/{token}", "web_app.handlers.install_flow:handle_install_script"),
    ("GET", "/install/windows/{token}", "web_app.handlers.install_flow:handle_install_script_windows"),
    ("GET", "/install/claim/{claim_id}", "web_app.handlers.install_flow:handle_install_claim_page"),
    ("GET", "/trial/connector", "web_app.handlers.install_flow:handle_trial_connector"),
    ("POST", "/trial/token", "web_app.handlers.install_flow:handle_trial_token"),
    ("GET", "/trial/script", "web_app.handlers.install_flow:handle_trial_script"),
    ("GET", "/trial/windows/script", "web_app.handlers.install_flow:handle_trial_script_windows"),
    ("GET", "/trial/{token}", "web_app.handlers.install_flow:handle_trial_script"),
    ("GET", "/web/agent/version", "handle_agent_version"),
    ("GET", "/web/agent/files", "handle_agent_files"),
    ("GET", "/web/research-desk/files", "handle_research_desk_files"),
    ("GET", "/web/provision/profiles", "web_app.handlers.provision:handle_provision_profiles"),
    ("POST", "/web/provision/start", "web_app.handlers.provision:handle_provision_start"),
    ("GET", "/web/provision/status", "web_app.handlers.provision:handle_provision_status"),
    ("POST", "/web/provision/confirm", "web_app.handlers.provision:handle_provision_confirm"),
    ("POST", "/web/provision/save-manual", "web_app.handlers.provision:handle_provision_save_manual"),
    ("POST", "/web/provision/revoke", "web_app.handlers.provision:handle_provision_revoke"),
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
