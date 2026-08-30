#!/usr/bin/env bash

TOP_LEVEL_CONTEXT_FILES=(
    "Dockerfile"
    "Dockerfile.unbrowser-mcp"
    "docker-compose.yml"
    "docker-compose.public-terminal.yml"
    "docker-compose.browser-terminal.yml"
    "Caddyfile"
)

# Host-side public-terminal runtime controller files. These are not part of a
# container build context, but the protected activation workflow installs the
# exact deployed copies into systemd and verifies their hashes against main.
HOST_RUNTIME_FILES=(
    "terminal_runtime_reconciler.py"
    "terminal-runtime-reconciler.service"
)

UNCHAINED_RUNTIME_FILES=(
    "relay.py"
    "rate_limit.py"
    "auth.py"
    "checkpoint_store.py"
    "financial_workspace.py"
    "analytics.py"
    "credit.py"
    "hosted_conversations.py"
    "conversation_transcript.py"
    "cloud_tools.py"
    "private_core_client.py"
    "private_core_contracts.py"
    "private_core_engine.py"
    "private_core_server.py"
    "editable_helpers.js"
    "challenge_detection.py"
    "domain_policy.py"
    "api.py"
    "mcp_server.py"
    "unbrowser_mcp_router.py"
    "unbrowser_ssrf_proxy.py"
    "orchestrator.py"
    "cdp.py"
    "ddm.py"
    "intel.py"
    "web.py"
    "web_state.py"
    "overlay_js.py"
    "cdp_tool_packaged.py"
    "published_results.py"
    "provision_helpers.py"
    "template_utils.py"
    "agent_package.py"
    "chat_event_transport.py"
    "chrome_bridge.py"
    "chat_agent_cli.py"
    "chat_agent_openrouter.py"
    "chat_agent_gemini.py"
    "chat_agent_codex.py"
    "chat_agent_sdk.py"
    "context_compact.py"
    "tool_payloads.py"
    "scheduler_agent.py"
    "scheduler_tool.py"
    "signup_agent.py"
    "nudge.py"
    "reflex.py"
    "pyproject.toml"
    "CLAUDE.md"
    "scheduled_tasks.py"
    "scheduled_jobs.example.json"
    "favicon.svg"
    "og-image.png"
)

BENCHMARK_CONTEXT_FILES=(
    "__init__.py"
    "progress_critic.py"
    "intermediate_goal.py"
)

RESEARCH_DESK_VENDOR_ROOT_FILES=(
    "manifest.json"
    "README.md"
    "pyproject.toml"
    "setup.py"
)
