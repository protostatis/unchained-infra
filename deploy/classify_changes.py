#!/usr/bin/env python3
"""Classify changed files into affected docker-compose services.

Reads a list of file paths from stdin (one per line, relative to repo root)
and prints affected service names, one per line, sorted.

Special outputs:
  ALL      — full rebuild required (Dockerfile, deps, or unknown file)
  COMPOSE  — compare resolved old/new Compose service configs on the deploy host
  caddy    — only caddy config changed (graceful reload)
  (empty stdout) — no service rebuild required (docs-only changes)

Used by deploy.sh to skip rebuilding/restarting services that aren't affected
by a given deploy.

The mapping is conservative: when in doubt, return ALL. The risk of an
unnecessary full rebuild is much lower than the risk of skipping a needed one.
"""
from __future__ import annotations

import sys
from typing import Iterable

SERVICES = {
    "caddy",
    "relay",
    "private-core",
    "mcp",
    "unbrowser-egress",
    "unbrowser-mcp",
    "web",
    "scheduler",
    "trial-agent",
}

# Files baked into ALL service images (require full rebuild). These are the
# Docker build context files that affect every service's image hash.
FULL_REBUILD_FILES = {
    "Dockerfile",
    "unchained/pyproject.toml",
    "unchained/uv.lock",
}

# Compose changes are resolved on the deploy host against the pre-upload
# snapshot. A Compose edit does not inherently require every container to be
# recreated; deploy.sh compares the effective per-service config and falls
# back to ALL if that comparison cannot be completed safely.
COMPOSE_FILES = {
    "docker-compose.yml",
}

# Caddy-only changes — graceful reload, no other service touched.
CADDY_FILES = {
    "Caddyfile",
}

TOP_LEVEL_OWNERSHIP: dict[str, set[str]] = {
    "Dockerfile.unbrowser-mcp": {"unbrowser-egress", "unbrowser-mcp"},
}

# Per-file ownership inside unchained/.
# Path is relative to repo root (so prefixed with "unchained/").
# Value is the set of services whose Python entry point transitively imports
# the file or depends on its presence at runtime.
UNCHAINED_OWNERSHIP: dict[str, set[str]] = {
    # web entry point and its private modules
    "unchained/web.py": {"web"},
    "unchained/web_state.py": {"web"},
    "unchained/web_routes.py": {"web"},
    "unchained/webmcp.py": {"web"},
    "unchained/api.py": {"web"},
    "unchained/agent_stream.py": {"web"},
    "unchained/agent_package.py": {"web"},
    "unchained/chat_event_transport.py": {"web", "trial-agent"},
    "unchained/template_utils.py": {"web"},
    "unchained/published_results.py": {"web"},
    "unchained/orchestrator.py": {"web"},
    "unchained/nudge.py": {"web"},
    "unchained/reflex.py": {"web"},
    "unchained/context_compact.py": {"web"},
    "unchained/signup_agent.py": {"web"},
    "unchained/favicon.svg": {"web"},
    "unchained/icon.svg": {"web"},
    "unchained/og-image.png": {"web"},
    "unchained/cdp_tool.py": {"web"},  # packaged into agent ZIP, served by web
    "unchained/cdp_tool_packaged.py": {"web"},
    "unchained/chat_agent_cli.py": {"web"},  # packaged into agent ZIP
    "unchained/chat_agent_codex.py": {"web"},
    "unchained/chat_agent_gemini.py": {"web"},
    "unchained/chat_agent_sdk.py": {"web"},

    # relay entry point
    "unchained/relay.py": {"relay"},
    "unchained/chrome_bridge.py": {"relay"},

    # mcp entry point
    "unchained/mcp_server.py": {"mcp"},

    # hosted unbrowser MCP egress proxy image
    "unchained/unbrowser_ssrf_proxy.py": {"unbrowser-egress", "unbrowser-mcp"},

    # private-core engine + CDP/DDM/intel and their helpers
    "unchained/private_core_server.py": {"private-core"},
    "unchained/private_core_engine.py": {"private-core"},
    "unchained/cdp.py": {"private-core"},
    "unchained/ddm.py": {"private-core"},
    "unchained/intel.py": {"private-core"},
    "unchained/editable_helpers.js": {"private-core"},
    "unchained/challenge_detection.py": {"private-core"},
    "unchained/domain_policy.py": {"private-core"},
    "unchained/overlay_js.py": {"private-core"},
    "unchained/provision_helpers.py": {"private-core"},
    "unchained/engage_cdp.py": {"private-core"},
    "unchained/dom_stream.py": {"private-core"},
    "unchained/maps_stream.py": {"private-core"},
    "unchained/canvas_density.py": {"private-core"},
    "unchained/canvas_intercept.py": {"private-core"},

    # scheduler entry point
    "unchained/scheduled_tasks.py": {"scheduler"},
    "unchained/scheduled_jobs.example.json": {"scheduler"},

    # trial-agent entry point
    "unchained/chat_agent_openrouter.py": {"trial-agent"},

    # Cross-cutting modules — touched by multiple services
    "unchained/auth.py": {"web", "relay", "mcp"},
    "unchained/analytics.py": {"web", "mcp"},
    "unchained/cloud_tools.py": {"web", "mcp"},
    "unchained/private_core_client.py": {"web", "mcp"},
    "unchained/private_core_contracts.py": {"web", "mcp", "private-core"},
    "unchained/rate_limit.py": {"web", "mcp"},
    "unchained/scheduler_agent.py": {"web", "scheduler"},
    "unchained/scheduler_tool.py": {"web", "scheduler"},

    # Docs / non-runtime — no rebuild
    "unchained/CLAUDE.md": set(),
    "unchained/LABEL_RESOLUTION.md": set(),
    "unchained/README.md": set(),
    "unchained/SHOW_ENHANCEMENT_PLAN.md": set(),
}

# Subtree rules — applied if no exact match in UNCHAINED_OWNERSHIP.
# Path prefix → service set. Order matters (more specific first).
SUBTREE_RULES: list[tuple[str, set[str]]] = [
    ("unchained/web_app/", {"web"}),
    ("unchained/installers/", {"web"}),
    ("unchained/benchmark/", {"private-core"}),
    ("unchained/scripts/", set()),  # dev/maintenance scripts, not runtime
    ("unchained/docs/", set()),
    ("unchained/testdata/", set()),
    ("rhythm/", {"web", "mcp", "private-core"}),  # rhythm tools used broadly
    ("research_desk_vendor/", {"web"}),
]


def classify_path(path: str) -> set[str]:
    """Return set of services affected by a change to `path`.

    Returns {"ALL"} when the change requires rebuilding every service
    (Dockerfile/deps changed, or the path is unknown).
    Returns an empty set when the change doesn't require any rebuild
    (docs, testdata, dev scripts).
    """
    if path in FULL_REBUILD_FILES:
        return {"ALL"}
    if path in COMPOSE_FILES:
        return {"COMPOSE"}
    if path in CADDY_FILES:
        return {"caddy"}
    if path in TOP_LEVEL_OWNERSHIP:
        return TOP_LEVEL_OWNERSHIP[path]
    if path in UNCHAINED_OWNERSHIP:
        return UNCHAINED_OWNERSHIP[path]
    for prefix, services in SUBTREE_RULES:
        if path.startswith(prefix):
            return services
    # Skip noise that always shows up but never affects runtime
    if path.startswith("unchained/__pycache__/") or path.endswith(".pyc"):
        return set()
    if path.startswith("unchained/") and path.endswith("_test.py"):
        return set()
    if path.startswith("unchained/test_"):
        return set()
    if path.endswith(".md") or path.endswith(".txt"):
        return set()
    # Unknown — be safe, rebuild everything
    return {"ALL"}


def main(paths: Iterable[str]) -> int:
    affected: set[str] = set()
    for raw in paths:
        path = raw.strip()
        if not path:
            continue
        result = classify_path(path)
        if "ALL" in result:
            print("ALL")
            return 0
        affected |= result
    if not affected:
        return 0
    # Don't include "ALL" sentinel here since we returned early above
    for svc in sorted(affected):
        print(svc)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.stdin))
