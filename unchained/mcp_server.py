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


def _agent_id_from_key(api_key: str) -> str:
    """Derive the agent_id from an API key (same logic as chat_agent_cli.py)."""
    return f"claude-{hashlib.sha256(api_key.encode()).hexdigest()[:8]}"


def _resolve_agent() -> str:
    """Authenticate the caller and derive agent_id from their API key."""
    api_key = _extract_api_key()
    if not api_key:
        raise ValueError(
            "Authorization: Bearer <api_key> header is required."
        )
    info = _auth.validate_key(api_key)
    if info is None:
        raise ValueError("Invalid API key.")
    return _agent_id_from_key(api_key)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def ddm(flags: str = "--llm-2pass --cols 60",
              tab_id: str = "auto") -> str:
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
    aid = _resolve_agent()
    return await cloud_tools.run_ddm(aid, tab_id, flags.split())


@mcp.tool()
async def intel_probe(tab_id: str = "auto") -> str:
    """Page intelligence probe — DOM fingerprint + Bayesian strategy ranking.

    Returns ~100 tokens. Identifies the page framework (Nuxt/Next/React),
    data stores, shadow DOM structure, and ranks 8 extraction strategies.
    Run this on first visit to any unknown SPA.
    """
    aid = _resolve_agent()
    return await cloud_tools.run_intel(aid, tab_id, ["--probe"])


@mcp.tool()
async def intel_extract(tab_id: str = "auto",
                        strategy: str = "") -> str:
    """Extract structured data using auto-selected or forced strategy.

    Strategies: innerText, host_attrs, js_global, react_fiber,
    data_testid, heading_hier, img_alt, shadow_pierce.

    Best for: Reddit (host_attrs), GitHub (data_testid), React SPAs (react_fiber).
    """
    aid = _resolve_agent()
    flags = ["--extract"]
    if strategy:
        flags += ["--strategy", strategy]
    return await cloud_tools.run_intel(aid, tab_id, flags)


@mcp.tool()
async def intel_stores(tab_id: str = "auto") -> str:
    """List all JavaScript data stores on the page (globals >10KB).

    Use on Nuxt/Next/YouTube sites to discover data before extraction.
    Follow up with intel_shape and intel_find_paths.
    """
    aid = _resolve_agent()
    return await cloud_tools.run_intel(aid, tab_id, ["--stores"])


@mcp.tool()
async def intel_shape(global_name: str,
                      depth: int = 3, tab_id: str = "auto") -> str:
    """Map the shape of a JavaScript global object.

    Args:
        global_name: Name of the JS global (e.g. "__NUXT__", "ytInitialData")
        depth: How deep to traverse (default 3)
    """
    aid = _resolve_agent()
    return await cloud_tools.run_intel(
        aid, tab_id,
        ["--shape", global_name, "--depth", str(depth)],
    )


@mcp.tool()
async def intel_find_paths(global_name: str,
                           pattern: str, tab_id: str = "auto") -> str:
    """Find paths to a key pattern inside a JavaScript global.

    Args:
        global_name: Name of the JS global (e.g. "__NUXT__")
        pattern: Key name to search for (e.g. "deals", "title", "price")
    """
    aid = _resolve_agent()
    return await cloud_tools.run_intel(
        aid, tab_id,
        ["--find-paths", global_name, pattern],
    )


@mcp.tool()
async def cdp_navigate(url: str,
                       tab_id: str = "auto") -> str:
    """Navigate the browser to a URL. Returns page title and final URL."""
    aid = _resolve_agent()
    return await cloud_tools.navigate(aid, tab_id, url)


@mcp.tool()
async def cdp_click(x: int, y: int,
                    tab_id: str = "auto") -> str:
    """Click at pixel coordinates. Get coordinates from DDM output."""
    aid = _resolve_agent()
    return await cloud_tools.click(aid, tab_id, x, y)


@mcp.tool()
async def cdp_type(text: str,
                   tab_id: str = "auto") -> str:
    """Type text into the currently focused element.

    Click on an input field first (using cdp_click) to give it focus,
    then use this to type text.
    """
    aid = _resolve_agent()
    return await cloud_tools.type_text(aid, tab_id, text)


@mcp.tool()
async def js_eval(expression: str,
                  tab_id: str = "auto") -> str:
    """Execute JavaScript on the page and return the result.

    Returns: JSON for objects/arrays, raw string for primitives.
    Use for: reading page data, interacting with SPA widgets,
    extracting structured data with querySelectorAll.
    """
    aid = _resolve_agent()
    return await cloud_tools.run_js(aid, tab_id, expression)


@mcp.tool()
async def cdp_screenshot(tab_id: str = "auto") -> str:
    """Take a screenshot of the current page.

    Returns base64-encoded PNG. Use sparingly (~2100 tokens) — prefer
    DDM for page understanding (~500 tokens).
    Only use for: CAPTCHAs, visual state, image verification.
    """
    aid = _resolve_agent()
    return await cloud_tools.screenshot(aid, tab_id)


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
