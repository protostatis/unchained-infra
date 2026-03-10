"""Unchained MCP Server — expose DDM/intel as MCP tools for Mode B (self-drive).

Users with their own Claude Code or Claude API can connect to this server
and use DDM/intel as MCP tools. The tools run on our cloud, connecting to
the user's Chrome through the tunnel relay.

Transport: Streamable HTTP on port 8766.
Auth: API key in request headers.

Usage:
    uv run mcp_server.py                         # Start on 0.0.0.0:8766
    uv run mcp_server.py --port 9000             # Custom port

Claude Code connects:
    claude --mcp-server https://api.unchained.dev/mcp
"""

import hashlib
import json
import os
import sys

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_request

import cloud_tools
from auth import Auth

# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "unchained",
    instructions=(
        "Unchained browser automation tools. Use DDM (dom density map) for "
        "page orientation (~500 tokens), intel for extraction strategy "
        "classification, and CDP tools for interaction. Navigate and click "
        "return page layout inline — no separate DDM call needed after them."
    ),
)

# Auth instance — shared across all tool calls
_auth = Auth()


def _extract_api_key() -> str:
    """Extract the Bearer API key from the current HTTP request headers."""
    try:
        request = get_http_request()
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            return auth_header[7:].strip()
    except RuntimeError:
        pass
    return ""


def _agent_id_from_key(api_key: str, profile: str = "") -> str:
    """Derive the agent_id from an API key and optional profile name."""
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()[:8]
    if profile and profile != "default":
        return f"claude-{key_hash}-{profile}"
    return f"claude-{key_hash}"


def _resolve_agent(profile: str = "") -> str:
    """Authenticate the caller and derive agent_id from their API key.

    If profile is provided and not "default", the agent_id includes
    the profile suffix (e.g. claude-abc12345-facebook).
    """
    api_key = _extract_api_key()
    if not api_key:
        raise ValueError(
            "Authorization: Bearer <api_key> header is required."
        )
    info = _auth.validate_key(api_key)
    if info is None:
        raise ValueError("Invalid API key.")
    return _agent_id_from_key(api_key, profile)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def ddm(flags: str = "--llm-2pass --cols 60",
              tab_id: str = "auto", agent_id: str = "") -> str:
    """DOM Density Map — structural page layout + interactive elements.

    Returns ~500 tokens of page understanding. Use this FIRST on every page
    to orient yourself before taking any action.

    Common flags:
      --llm-2pass --cols 60   (default, best for orientation)
      --text                  (extract page text)
      --text --find "keyword" (find text on page)
      --at 694,584            (reverse lookup at pixel coordinates)
      --forms                 (detect forms)
      --js "expression"       (execute JavaScript)
    """
    aid = _resolve_agent(profile=agent_id)
    return await cloud_tools.run_ddm(aid, tab_id, flags.split())


@mcp.tool()
async def intel_probe(tab_id: str = "auto", agent_id: str = "") -> str:
    """Page intelligence probe — DOM fingerprint + Bayesian strategy ranking.

    Returns ~100 tokens. Identifies the page framework (Nuxt/Next/React),
    data stores, shadow DOM structure, and ranks 8 extraction strategies.
    Run this on first visit to any unknown SPA.
    """
    aid = _resolve_agent(profile=agent_id)
    return await cloud_tools.run_intel(aid, tab_id, ["--probe"])


@mcp.tool()
async def intel_extract(tab_id: str = "auto",
                        strategy: str = "", agent_id: str = "") -> str:
    """Extract structured data using auto-selected or forced strategy.

    Strategies: innerText, host_attrs, js_global, react_fiber,
    data_testid, heading_hier, img_alt, shadow_pierce.

    Best for: Reddit (host_attrs), GitHub (data_testid), React SPAs (react_fiber).
    """
    aid = _resolve_agent(profile=agent_id)
    flags = ["--extract"]
    if strategy:
        flags += ["--strategy", strategy]
    return await cloud_tools.run_intel(aid, tab_id, flags)


@mcp.tool()
async def intel_stores(tab_id: str = "auto", agent_id: str = "") -> str:
    """List all JavaScript data stores on the page (globals >10KB).

    Use on Nuxt/Next/YouTube sites to discover data before extraction.
    Follow up with intel_shape and intel_find_paths.
    """
    aid = _resolve_agent(profile=agent_id)
    return await cloud_tools.run_intel(aid, tab_id, ["--stores"])


@mcp.tool()
async def intel_shape(global_name: str,
                      depth: int = 3, tab_id: str = "auto",
                      agent_id: str = "") -> str:
    """Map the shape of a JavaScript global object.

    Args:
        global_name: Name of the JS global (e.g. "__NUXT__", "ytInitialData")
        depth: How deep to traverse (default 3)
    """
    aid = _resolve_agent(profile=agent_id)
    return await cloud_tools.run_intel(
        aid, tab_id,
        ["--shape", global_name, "--depth", str(depth)],
    )


@mcp.tool()
async def intel_find_paths(global_name: str,
                           pattern: str, tab_id: str = "auto",
                           agent_id: str = "") -> str:
    """Find paths to a key pattern inside a JavaScript global.

    Args:
        global_name: Name of the JS global (e.g. "__NUXT__")
        pattern: Key name to search for (e.g. "deals", "title", "price")
    """
    aid = _resolve_agent(profile=agent_id)
    return await cloud_tools.run_intel(
        aid, tab_id,
        ["--find-paths", global_name, pattern],
    )


@mcp.tool()
async def cdp_navigate(url: str,
                       tab_id: str = "auto", agent_id: str = "") -> str:
    """Navigate the browser to a URL. Returns page title and final URL."""
    aid = _resolve_agent(profile=agent_id)
    return await cloud_tools.navigate(aid, tab_id, url)


@mcp.tool()
async def cdp_click(x: int, y: int,
                    tab_id: str = "auto", agent_id: str = "") -> str:
    """Click at pixel coordinates. Get coordinates from DDM output."""
    aid = _resolve_agent(profile=agent_id)
    return await cloud_tools.click(aid, tab_id, x, y)


@mcp.tool()
async def cdp_type(text: str,
                   tab_id: str = "auto", agent_id: str = "") -> str:
    """Type text into the currently focused element.

    Click on an input field first (using cdp_click) to give it focus,
    then use this to type text.
    """
    aid = _resolve_agent(profile=agent_id)
    return await cloud_tools.type_text(aid, tab_id, text)


@mcp.tool()
async def js_eval(expression: str,
                  tab_id: str = "auto", agent_id: str = "") -> str:
    """Execute JavaScript on the page and return the result.

    Returns: JSON for objects/arrays, raw string for primitives.
    Use for: reading page data, interacting with SPA widgets,
    extracting structured data with querySelectorAll.
    """
    aid = _resolve_agent(profile=agent_id)
    return await cloud_tools.run_js(aid, tab_id, expression)


@mcp.tool()
async def cdp_screenshot(tab_id: str = "auto", agent_id: str = "") -> str:
    """Take a screenshot of the current page.

    Returns base64-encoded PNG. Use sparingly (~2100 tokens) — prefer
    DDM for page understanding (~500 tokens).
    Only use for: CAPTCHAs, visual state, image verification.
    """
    aid = _resolve_agent(profile=agent_id)
    return await cloud_tools.screenshot(aid, tab_id)


@mcp.tool()
async def cdp_set_file(selector: str, file_path: str,
                       tab_id: str = "auto", agent_id: str = "") -> str:
    """Set a file on an <input type="file"> element without the OS file picker.

    Args:
        selector: CSS selector for the file input (e.g. 'input[type="file"]')
        file_path: Absolute path to the file on the agent's machine
    """
    aid = _resolve_agent(profile=agent_id)
    return await cloud_tools.set_file(aid, tab_id, selector, file_path)


@mcp.tool()
async def list_connected_agents(agent_id: str = "") -> str:
    """List all connected browser agents with their IDs and profiles.

    Use this to discover available agent_ids when you have multiple
    Chrome profiles connected (e.g. claude-abc12345-facebook).
    Pass the profile name in other tools' agent_id param to target
    a specific profile.
    """
    api_key = _extract_api_key()
    if not api_key:
        raise ValueError("Authorization: Bearer <api_key> header is required.")
    info = _auth.validate_key(api_key)
    if info is None:
        raise ValueError("Invalid API key.")

    relay_host = os.environ.get("RELAY_INTERNAL_URL", "ws://relay:8765")
    # Extract host from ws://host:port
    host = relay_host.replace("ws://", "").replace("wss://", "").split(":")[0]
    port_str = relay_host.split(":")[-1] if ":" in relay_host.split("//")[-1] else "8765"
    try:
        port = int(port_str)
    except ValueError:
        port = 8765

    import httpx
    scheme = "https" if port == 443 else "http"
    port_part = "" if port in (443, 80) else f":{port}"
    api_url = f"{scheme}://{host}{port_part}/api/agents"
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(api_url, headers=headers)
            if not resp.is_success:
                return "Failed to query relay for agents."
            agents = resp.json()
    except Exception as e:
        return f"Error querying agents: {e}"

    if not agents:
        return "No agents connected."
    lines = ["Connected agents:"]
    for a in agents:
        profile = a.get("profile", "default")
        lines.append(f"  {a['agent_id']} (profile: {profile})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    host = "0.0.0.0"
    port = 8766

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--host" and i + 1 < len(args):
            host = args[i + 1]
            i += 2
        elif args[i] == "--port" and i + 1 < len(args):
            port = int(args[i + 1])
            i += 2
        elif args[i] in ("--help", "-h"):
            print("""Usage: uv run mcp_server.py [options]

Options:
    --host <host>    Bind address (default: 0.0.0.0)
    --port <port>    Bind port (default: 8766)
""")
            return
        else:
            i += 1

    print(f"[mcp] Starting MCP server on {host}:{port}")
    mcp.run(transport="streamable-http", host=host, port=port)


if __name__ == "__main__":
    main()
