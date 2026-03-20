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

import base64
import hashlib
import os
import re
import sys

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_request
from fastmcp.utilities.types import Image

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
        "return page layout inline — no separate DDM call needed after them.\n\n"

        "## Extraction flow (use on every new page)\n"
        "1. ddm (default flags) — orient yourself, find layout + interactive elements\n"
        "2. If you need page TEXT and the page is static/article-like → ddm --text\n"
        "3. If ddm --text returns weak/empty text (JS-heavy SPA, card grid, lazy-loaded "
        "board) → run intel_probe to identify the best extraction strategy\n"
        "4. Follow the probe's top-ranked strategy:\n"
        "   - js_global ranked high → intel_stores → intel_shape/intel_find_paths → js_eval\n"
        "   - host_attrs / react_fiber / data_testid ranked high → intel_extract (with that strategy)\n"
        "   - innerText ranked high → ddm --text is sufficient\n\n"

        "## Quick reference by site type\n"
        "- Static pages (Wikipedia, HN, docs): ddm → ddm --text\n"
        "- React/Next SPAs (npm, GitHub): intel_probe → intel_extract react_fiber or data_testid\n"
        "- Web-component sites (Reddit): intel_probe → intel_extract host_attrs\n"
        "- Data-store sites (YouTube, Nuxt): intel_probe → intel_stores → js_eval\n"
        "- Card grids / boards (Kalshi, Polymarket): intel_probe → follow top strategy"
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


def _user_owns_agent(user_id: str, agent_id: str) -> bool:
    """Check if a full agent_id belongs to any active key owned by this user."""
    for key in _auth.get_keys_for_user(user_id):
        key_hash = hashlib.sha256(key.encode()).hexdigest()[:8]
        if agent_id.startswith(f"claude-{key_hash}") or agent_id.startswith(f"headless-{key_hash}"):
            return True
    return False


def _agent_id_from_key(api_key: str, profile: str = "") -> str:
    """Derive the agent_id from an API key and optional profile name."""
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()[:8]
    if profile and profile != "default":
        if not re.match(r'^[a-zA-Z0-9_-]{1,32}$', profile):
            raise ValueError(f"Invalid profile name: {profile!r}")
        return f"claude-{key_hash}-{profile}"
    return f"claude-{key_hash}"


def _resolve_agent(profile: str = "") -> str:
    """Authenticate the caller and resolve the target agent_id.

    Accepts either:
    - A profile name (e.g. "facebook") → derives claude-<hash>-facebook
    - A full agent ID from list_connected_agents (e.g. "claude-abc12345-facebook")
      → validates ownership and uses it directly
    - Empty string → default agent (claude-<hash>)
    """
    api_key = _extract_api_key()
    if not api_key:
        raise ValueError(
            "Authorization: Bearer <api_key> header is required."
        )
    info = _auth.validate_key(api_key)
    if info is None:
        raise ValueError("Invalid API key.")

    if not profile:
        return _agent_id_from_key(api_key)

    # If caller passed a full agent ID (from list_connected_agents), validate
    # ownership by checking it matches a key hash belonging to this user.
    # This is necessary because private-core uses an internal token that
    # bypasses relay per-user ownership checks.
    if re.match(r'^(?:claude|headless)-[0-9a-f]{8}', profile):
        if _user_owns_agent(info["user_id"], profile):
            return profile
        raise ValueError(f"Agent {profile} does not belong to you.")

    # Otherwise treat as a profile name suffix
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
      --text --max 8000       (raise text cap for long pages, default ~4000 chars)
      --at 694,584            (reverse lookup at pixel coordinates)
      --forms                 (detect forms)
      --js "expression"       (execute JavaScript)

    When --text returns weak/empty results on JS-heavy pages (SPAs, card grids,
    lazy-loaded boards), switch to the intel pipeline: intel_probe first, then
    follow the top-ranked strategy.
    """
    aid = _resolve_agent(profile=agent_id)
    return await cloud_tools.run_ddm(aid, tab_id, flags.split())


@mcp.tool()
async def intel_probe(tab_id: str = "auto", agent_id: str = "") -> str:
    """Page intelligence probe — DOM fingerprint + Bayesian strategy ranking.

    Returns ~100 tokens. Identifies the page framework (Nuxt/Next/React),
    data stores, shadow DOM structure, and ranks 8 extraction strategies.
    Run this on first visit to any unknown SPA.

    After probe, follow the top-ranked strategy:
      js_global high   → intel_stores → intel_shape/intel_find_paths → js_eval
      host_attrs high  → intel_extract with strategy="host_attrs"
      react_fiber high → intel_extract with strategy="react_fiber"
      data_testid high → intel_extract with strategy="data_testid"
      innerText high   → ddm --text is sufficient (no intel_extract needed)
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

    Site examples:
      Reddit       → strategy="host_attrs" (web components with rich attributes)
      GitHub       → strategy="data_testid" (data-testid annotated elements)
      npm/Next.js  → strategy="react_fiber" (React fiber tree traversal)
      YouTube      → use intel_stores + js_eval instead (ytInitialData global)
      Nuxt sites   → use intel_stores + js_eval instead (__NUXT__ global)

    If unsure which strategy, omit strategy param to auto-select based on probe.
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
async def cdp_scroll(direction: str = "down", amount: int = 500,
                     tab_id: str = "auto", agent_id: str = "") -> str:
    """Scroll the page. Returns updated page layout.

    Args:
        direction: "up", "down", "left", or "right"
        amount: pixels to scroll (default 500, roughly one viewport height, max 50000)
    """
    if direction not in ("up", "down", "left", "right"):
        return f"Invalid direction: {direction!r}. Use up, down, left, or right."
    amount = max(1, min(amount, 50000))
    aid = _resolve_agent(profile=agent_id)
    return await cloud_tools.scroll(aid, tab_id, direction, amount)


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
async def cdp_press_enter(tab_id: str = "auto", agent_id: str = "") -> str:
    """Press Enter on the currently focused element.

    Use after typing into a search box or form field to submit.
    Click the target input first (cdp_click) to ensure it has focus.
    """
    aid = _resolve_agent(profile=agent_id)
    return await cloud_tools.press_enter(aid, tab_id)


@mcp.tool()
async def cdp_key_press(key: str, modifiers: int = 0,
                        tab_id: str = "auto", agent_id: str = "") -> str:
    """Press a keyboard key with optional modifier keys.

    Args:
        key: Key name. Special keys: Enter, Tab, Escape, Backspace, Delete,
             ArrowUp, ArrowDown, ArrowLeft, ArrowRight, Space, Home, End,
             PageUp, PageDown. Single characters (a-z, 0-9) also accepted.
        modifiers: Bitmask for modifier keys (sum values to combine):
             0=none, 1=Alt, 2=Ctrl, 4=Meta/Cmd, 8=Shift.
             Example: Ctrl+Shift = 2+8 = 10.

    Use for keyboard shortcuts (Ctrl+A, Escape to close), arrow-key navigation
    in dropdowns, Tab to move between form fields, etc.
    """
    if not (0 <= modifiers <= 15):
        return "Invalid modifiers: must be 0-15 (1=Alt, 2=Ctrl, 4=Meta, 8=Shift)."
    aid = _resolve_agent(profile=agent_id)
    return await cloud_tools.key_press(aid, tab_id, key, modifiers)


@mcp.tool()
async def js_eval(expression: str,
                  tab_id: str = "auto", agent_id: str = "") -> str:
    """Execute JavaScript on the page and return the result.

    Returns: JSON for objects/arrays, raw string for primitives.
    Use for: reading page data, interacting with SPA widgets,
    extracting structured data with querySelectorAll.

    Example — extract all card titles from a grid page:
      [...document.querySelectorAll('.card-title')].map(e => e.textContent.trim())

    For JS data stores discovered via intel_stores, access them directly:
      JSON.stringify(window.__NUXT__.data.deals.slice(0,5))
      JSON.stringify(ytInitialData.contents.twoColumnBrowseResultsRenderer)
    """
    aid = _resolve_agent(profile=agent_id)
    return await cloud_tools.run_js(aid, tab_id, expression)


@mcp.tool()
async def cdp_screenshot(tab_id: str = "auto", agent_id: str = "") -> Image:
    """Take a screenshot of the current page.

    Returns PNG image content. Use sparingly (~2100 tokens) — prefer
    DDM for page understanding (~500 tokens).
    Only use for: CAPTCHAs, visual state, image verification.
    """
    aid = _resolve_agent(profile=agent_id)
    try:
        png_b64 = await cloud_tools.screenshot(aid, tab_id)
        return Image(data=base64.b64decode(png_b64, validate=True), format="png")
    except Exception:
        try:
            ddm_text = await cloud_tools.run_ddm(aid, tab_id, ["--text"])
            raise RuntimeError(
                f"Screenshot unavailable. Page text via DDM:\n\n{ddm_text}"
            )
        except RuntimeError:
            raise
        except Exception:
            raise RuntimeError("Screenshot failed and DDM fallback also failed.")


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

    Use this to discover available agents when you have multiple
    Chrome profiles connected. To target a specific profile in other
    tools, pass either the full agent_id (e.g. claude-abc12345-facebook)
    or just the profile name (e.g. facebook) in the agent_id parameter.
    """
    api_key = _extract_api_key()
    if not api_key:
        raise ValueError("Authorization: Bearer <api_key> header is required.")
    info = _auth.validate_key(api_key)
    if info is None:
        raise ValueError("Invalid API key.")

    from urllib.parse import urlparse
    import httpx

    relay_url = os.environ.get("RELAY_INTERNAL_URL", "ws://relay:8765")
    parsed = urlparse(relay_url)
    host = parsed.hostname or "relay"
    port = parsed.port or 8765
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


@mcp.tool()
async def cdp_provision_launch(profile_path: str, agent_id: str = "") -> str:
    """Launch a temporary Chrome with a user profile for OAuth or authenticated browsing.

    Args:
        profile_path: Absolute path to the Chrome profile directory
            (e.g. "/Users/you/Library/Application Support/Google/Chrome/Profile 5").
        agent_id: Agent to provision on (default: auto-detected).

    Returns the provisioned slot ID and initial tab ID. Use the returned
    prov-prefixed tab_id with ddm, cdp_click, cdp_type, etc.
    """
    aid = _resolve_agent(profile=agent_id)
    result = await cloud_tools.provision_launch(aid, profile_path)
    if not result or "error" in result:
        err = result.get("error", "Unknown error") if result else "No response"
        return f"Error: {err}"
    tab_id = result.get("tab_id", "")
    slot = result.get("slot", "")
    port = result.get("port", "")
    lines = [f"Provisioned Chrome launched."]
    if slot:
        lines.append(f"  Slot: {slot}")
    if tab_id:
        lines.append(f"  Tab ID: {tab_id}")
    if port:
        lines.append(f"  Debug port: {port}")
    return "\n".join(lines)


@mcp.tool()
async def cdp_provision_cleanup(slot: str = "", agent_id: str = "") -> str:
    """Clean up provisioned Chrome instances.

    Args:
        slot: Specific slot to clean up (e.g. "dc31"). If empty, cleans up all.
        agent_id: Agent to clean up on (default: auto-detected).
    """
    aid = _resolve_agent(profile=agent_id)
    result = await cloud_tools.provision_cleanup(aid, slot=slot)
    status = result.get("status", "") if isinstance(result, dict) else ""
    cleaned = result.get("cleaned") if isinstance(result, dict) else None
    if status == "cleaned_up":
        if cleaned and not slot:
            return f"Cleaned up {cleaned} provisioned Chrome instance{'s' if cleaned != 1 else ''}."
        if slot:
            return f"Cleaned up provisioned Chrome slot {slot}."
        return "Cleaned up provisioned Chrome."
    if status == "no_provision_chrome":
        return "No provisioned Chrome instances to clean up."
    if status == "nothing_to_clean":
        return f"Slot not found — nothing cleaned. Use list_provisioned_tabs to see active slots."
    if status == "error":
        return f"Cleanup error: {result.get('error', 'unknown')}"
    return f"Unexpected cleanup result: {result}"


@mcp.tool()
async def list_provisioned_tabs(agent_id: str = "") -> str:
    """List all tabs in provisioned Chrome instances.

    Use this after provisioning Chrome with a profile to discover
    new tabs (e.g., OAuth popups). Returns prov-prefixed tab IDs
    that can be passed to ddm, cdp_click, cdp_type, etc.
    """
    aid = _resolve_agent(profile=agent_id)
    result = await cloud_tools.provision_status(aid)
    if "error" in result:
        return f"Error: {result['error']}"
    slots = result.get("slots", {})
    if not slots:
        return "No provisioned Chrome instances."
    lines = []
    for slot, info in slots.items():
        profile = info.get("profile", "")
        tabs = info.get("tabs", [])
        lines.append(f"Slot {slot} (profile: {profile}, {len(tabs)} tab{'s' if len(tabs) != 1 else ''}):")
        for t in tabs:
            if "error" in t:
                lines.append(f"  [error] {t['error']}")
                continue
            tab_id = t.get("tab_id", "")
            title = t.get("title", "(empty)")[:50]
            url = t.get("url", "")[:80]
            popup = "  [popup]" if t.get("type") == "popup" else ""
            lines.append(f"  {tab_id}  {title}{popup}  {url}")
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
