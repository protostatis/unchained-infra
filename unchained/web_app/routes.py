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
    ("GET", "/web/static/signed-chat-reconnect.js", "handle_signed_chat_reconnect_asset"),
    ("GET", "/web/wasmbrowser/{filename}", "handle_wasmbrowser_asset"),
    ("GET", "/og-image.png", "handle_og_image"),
    ("GET", "/robots.txt", "handle_robots_txt"),
    ("GET", "/sitemap.xml", "handle_sitemap_xml"),
    ("GET", "/google83c650022d8db556.html", "handle_google_verification"),
    (
        "GET",
        "/google333e7a6c98af8946.html",
        "handle_google_verification_current",
    ),
    # Docker-internal readiness endpoint. Caddy denies /internal/* publicly.
    ("GET", "/internal/health", "handle_internal_health"),
    (
        "GET",
        "/internal/fin-terminal/auth",
        "web_app.handlers.fin_terminal:handle_fin_terminal_auth",
    ),
    (
        "GET",
        "/internal/fin-terminal/browser-auth",
        "web_app.handlers.fin_terminal:handle_fin_terminal_browser_auth",
    ),
    ("GET", "/", "handle_index"),
    ("GET", "/unbrowser", "web_app.handlers.pages:handle_unbrowser_page"),
    ("GET", "/go/unbrowser-connect", "web_app.handlers.pages:handle_unbrowser_outbound"),
    ("GET", "/go/unbrowser-github", "web_app.handlers.pages:handle_unbrowser_outbound"),
    ("GET", "/go/unbrowser-smithery", "web_app.handlers.pages:handle_unbrowser_outbound"),
    ("GET", "/chrome-tax", "web_app.handlers.pages:handle_chrome_tax_page"),
    (
        "GET",
        "/browserbase-alternative",
        "web_app.handlers.pages:handle_browserbase_alternative_page",
    ),
    (
        "GET",
        "/browser-mcp-alternative",
        "web_app.handlers.pages:handle_browser_mcp_alternative_page",
    ),
    ("GET", "/web/unbrowser/sources", "web_app.handlers.unbrowser_demo:handle_unbrowser_sources"),
    ("GET", "/web/unbrowser/runtime", "web_app.handlers.unbrowser_demo:handle_unbrowser_runtime"),
    ("GET", "/web/unbrowser/stream", "web_app.handlers.unbrowser_demo:handle_unbrowser_stream"),
    ("GET", "/tab", "web_app.handlers.pages:handle_tab_page"),
    ("GET", "/mcp-guide", "web_app.handlers.pages:handle_mcp_guide_page"),
    ("GET", "/test", "handle_test"),
    ("GET", "/privacy", "web_app.handlers.pages:handle_privacy_page"),
    ("GET", "/privacy-policy", "web_app.handlers.pages:handle_privacy_page"),
    ("GET", "/data-deletion", "web_app.handlers.pages:handle_data_deletion_page"),
    ("GET", "/auth/login", "web_app.handlers.auth_admin:handle_auth_login"),
    ("POST", "/auth/token", "web_app.handlers.auth_admin:handle_auth_token"),
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
    ("GET", "/admin/settings/hosted-models", "web_app.handlers.auth_admin:handle_admin_hosted_models"),
    ("POST", "/admin/settings/hosted-models", "web_app.handlers.auth_admin:handle_admin_hosted_models_update"),
    ("GET", "/chat", "web_app.handlers.pages:handle_chat_redirect"),
    ("GET", "/trial", "web_app.handlers.pages:handle_trial_page"),
    ("GET", "/workspace", "web_app.handlers.pages:handle_workspace_page"),
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
    ("GET", "/cli", "web_app.handlers.pages:handle_cli_page"),
    ("GET", "/app", "web_app.handlers.pages:handle_claude_page"),
    ("POST", "/web/labs/you-navigate/run", "web_app.handlers.x_manager_demo:handle_x_manager_demo_run"),
    ("POST", "/web/labs/x-manager/run", "web_app.handlers.x_manager_demo:handle_x_manager_demo_run"),
    ("GET", "/chat/ws", "web_app.handlers.chat_stream:handle_chat_ws"),
    ("POST", "/web/overlay-followup", "web_app.handlers.overlay_ws:handle_overlay_followup"),
    ("POST", "/web/chat", "web_app.handlers.chat_stream:handle_chat_msg"),
    ("GET", "/web/chat/active", "web_app.handlers.chat_stream:handle_chat_active"),
    ("GET", "/web/chat/events", "web_app.handlers.chat_stream:handle_chat_events"),
    ("POST", "/web/chat/cancel", "web_app.handlers.chat_stream:handle_chat_cancel"),
    ("GET", "/web/chat/status", "web_app.handlers.chat_flow:handle_chat_status"),
    ("GET", "/web/first-look/preflight", "web_app.handlers.chat_flow:handle_first_look_preflight"),
    ("GET", "/web/first-look/preview/ws", "web_app.handlers.chat_flow:handle_first_look_preview_ws"),
    ("GET", "/web/chat/preview/ws", "web_app.handlers.chat_flow:handle_chat_preview_ws"),
    ("POST", "/web/first-look/signal", "web_app.handlers.chat_flow:handle_first_look_signal"),
    ("POST", "/web/chat/update-client", "web_app.handlers.chat_flow:handle_chat_update_client"),
    ("POST", "/web/chat/install-research-desk", "web_app.handlers.chat_flow:handle_chat_install_research_desk"),
    ("GET", "/web/chat/history", "web_app.handlers.chat_flow:handle_chat_history"),
    ("POST", "/web/chat/new", "web_app.handlers.chat_flow:handle_chat_new"),
    ("POST", "/web/chat/new/ack", "web_app.handlers.chat_flow:handle_chat_new_ack"),
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
    # Hosted-worker accounting callbacks. Caddy denies /internal/* publicly;
    # the trial-agent reaches these over the Docker-internal app network.
    ("POST", "/internal/credit/reserve", "web_app.handlers.credit_internal:handle_credit_reserve"),
    ("POST", "/internal/credit/submitted", "web_app.handlers.credit_internal:handle_credit_mark_submitted"),
    ("POST", "/internal/credit/settle", "web_app.handlers.credit_internal:handle_credit_settle"),
    ("POST", "/internal/credit/release", "web_app.handlers.credit_internal:handle_credit_release"),
    ("POST", "/internal/credit/provider-balance", "web_app.handlers.credit_internal:handle_credit_provider_balance"),
    # User-facing credit status
    ("GET", "/web/credit/status", "web_app.handlers.auth_admin:handle_credit_status"),
    ("GET", "/web/credit/status/history", "web_app.handlers.auth_admin:handle_credit_history"),
    # Admin grant
    ("POST", "/admin/credit/grant", "web_app.handlers.auth_admin:handle_admin_credit_grant"),

    # -- Financial workspace control plane (feature-flagged) --
    ("POST", "/internal/financial-workspace/checkpoints",
     "web_app.handlers.fin_workspace:handle_fin_workspace_create_checkpoint"),
    ("GET", "/internal/financial-workspace/checkpoints/{checkpoint_id}",
     "web_app.handlers.fin_workspace:handle_fin_workspace_get_checkpoint"),
    ("POST", "/internal/financial-workspace/claim",
     "web_app.handlers.fin_workspace:handle_fin_workspace_claim"),
    ("POST", "/internal/financial-workspace/claim/accept",
     "web_app.handlers.fin_workspace:handle_fin_workspace_claim_accept"),
    ("GET", "/internal/financial-workspace/claims/{claim_id}",
     "web_app.handlers.fin_workspace:handle_fin_workspace_get_claim"),
    ("GET", "/internal/financial-workspace/workspace",
     "web_app.handlers.fin_workspace:handle_fin_workspace_get_workspace"),
    ("GET", "/internal/financial-workspace/snapshots",
     "web_app.handlers.fin_workspace:handle_fin_workspace_get_snapshots"),
    ("POST", "/internal/financial-workspace/effects/process",
     "web_app.handlers.fin_workspace:handle_fin_workspace_process_effects"),
    ("POST", "/internal/financial-workspace/sweep",
     "web_app.handlers.fin_workspace:handle_fin_workspace_sweep"),
    # Account-scoped runtime control (wake/sleep/status) — canary S2S API.
    ("POST", "/internal/financial-workspace/runtime/wake",
     "web_app.handlers.fin_workspace:handle_fin_workspace_runtime_wake"),
    ("POST", "/internal/financial-workspace/runtime/sleep",
     "web_app.handlers.fin_workspace:handle_fin_workspace_runtime_sleep"),
    ("GET", "/internal/financial-workspace/runtime/status",
     "web_app.handlers.fin_workspace:handle_fin_workspace_runtime_status"),
    ("POST", "/internal/financial-workspace/runtime/flush",
     "web_app.handlers.fin_workspace:handle_fin_workspace_runtime_flush"),

    # Browser handoff/auth/callback routes — proxied by Caddy under
    # /fin-terminal-workspace (prefix stripped). Dedicated /workspace/*
    # namespace so the claim OAuth routes can never shadow (or be shadowed
    # by) the site's own login routes (/auth/facebook/..., /auth/github/...).
    # Exact provider allowlist.
    ("GET", "/workspace/auth/claim",
     "web_app.handlers.fin_workspace:handle_fin_workspace_auth_claim_page"),
    ("POST", "/workspace/claim",
     "web_app.handlers.fin_workspace:handle_fin_workspace_browser_claim"),
    ("GET", "/workspace/claims/{claim_id}",
     "web_app.handlers.fin_workspace:handle_fin_workspace_browser_get_claim"),
    ("GET", "/workspace/workspace",
     "web_app.handlers.fin_workspace:handle_fin_workspace_browser_get_workspace"),
    ("GET", "/workspace/snapshots",
     "web_app.handlers.fin_workspace:handle_fin_workspace_browser_get_snapshots"),
    ("GET", "/workspace/runtime/status",
     "web_app.handlers.fin_workspace:handle_fin_workspace_browser_runtime_status"),
    ("POST", "/workspace/oauth/google",
     "web_app.handlers.fin_workspace_auth:handle_claim_google_token"),
    ("GET", "/workspace/done",
     "web_app.handlers.fin_workspace_auth:handle_claim_done"),
    ("GET", "/workspace/oauth/{provider}/start",
     "web_app.handlers.fin_workspace_auth:handle_claim_oauth_start"),
    ("GET", "/workspace/oauth/{provider}/callback",
     "web_app.handlers.fin_workspace_auth:handle_claim_oauth_callback"),
    # Private workspace leg: authenticated /fin-terminal/. Caddy strips
    # /fin-terminal and rewrites to /terminal/<rest> so the account runtime's
    # root-relative surface (/, /assets/*, /ws) is proxied unchanged with the
    # server-derived principal injected. Fails closed when no validated
    # runtime provider exists — never the marketing index.
    ("GET", "/terminal",
     "web_app.handlers.fin_workspace:handle_fin_workspace_terminal_proxy"),
    ("GET", "/terminal/{tail:.*}",
     "web_app.handlers.fin_workspace:handle_fin_workspace_terminal_proxy"),
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
