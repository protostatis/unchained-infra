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
import json
import os
import re
import sys

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_request
from fastmcp.utilities.types import Image

import cloud_tools
from auth import Auth
from private_core_client import PrivateCoreError

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
        "- Card grids / boards (Kalshi, Polymarket): intel_probe → follow top strategy\n\n"

        "## Iframes\n"
        "DDM shows iframes as 'X' blocks in the grid and lists them in hints "
        "(e.g. `Iframe: stripe.com (400×200)`). The X block is opaque — DDM "
        "cannot see inside from the main page context.\n"
        "- If your task needs content or interaction inside an iframe:\n"
        "  1. cdp_list_frames — identify frames by index and URL\n"
        "  2. ddm_frame(frame_id) — run DDM inside the iframe to see its layout "
        "and interactive elements, same output format as ddm\n"
        "  3. js_eval_frame(frame_id, expr) — query or manipulate elements inside\n"
        "- Ignore iframes that are ads or decorative embeds (doubleclick, ads, trackers)\n"
        "- Functional iframes to act on: payment forms (stripe.com, paypal.com), "
        "auth widgets (accounts.google.com, apple.com), CAPTCHAs, embedded signup forms\n\n"

        "## Rhythm (event-driven SPA automation)\n"
        "For SPAs where DDM/intel gives weak results and you need to interact:\n"
        "1. rhythm_train (once per site) — learn the page's element timing\n"
        "2. rhythm_catch — scan page DOM for specific data terms\n"
        "3. rhythm_execute — run multi-step plans at event speed (no LLM between steps)\n"
        "4. rhythm_query — check what's already learned before training"
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

def _append_iframe_tip(ddm_output: str) -> str:
    """Append a tool-discovery hint when DDM output contains iframe hints.

    DDM renders cross-origin iframes as opaque 'X' blocks and surfaces them
    in the hints section as 'Iframe: domain (WxH)'.  When the agent sees that
    line it may not know what to do next.  This nudge closes that gap without
    touching the private-core renderer.
    """
    if "Iframe:" not in ddm_output:
        return ddm_output
    tip = (
        "\n[Iframes detected] use cdp_list_frames to identify frames by index/URL, "
        "then ddm_frame(frame_id) to see inside or js_eval_frame(frame_id, expr) to interact."
    )
    return ddm_output + tip


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
    result = await cloud_tools.run_ddm(aid, tab_id, flags.split())
    return _append_iframe_tip(result)


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
async def cdp_wait_ready(strategy: str = "both",
                         tab_id: str = "auto", agent_id: str = "") -> str:
    """Wait for page to finish loading.

    Args:
        strategy: "dom" (DOM stability), "network" (network idle), "both" (default)
    """
    if strategy not in ("dom", "network", "both"):
        return f"Invalid strategy: {strategy!r}. Use dom, network, or both."
    aid = _resolve_agent(profile=agent_id)
    return await cloud_tools.wait_ready(aid, tab_id, strategy)


@mcp.tool()
async def cdp_click(x: int | None = None, y: int | None = None,
                    element_id: str = "", label: str = "",
                    tab_id: str = "auto", agent_id: str = "") -> str:
    """Click an element by coordinates, DDM element ID, or label.

    Exactly one click mode must be used:
    - Coordinates: cdp_click(x=500, y=300)
    - Element ID from DDM: cdp_click(element_id="B3")
    - Label text: cdp_click(label="Notifications")

    Element IDs and labels come from DDM output (e.g. B1:"Submit" at grid(14,8) px(400,300)).
    """
    has_coords = x is not None or y is not None
    has_element = bool(element_id)
    has_label = bool(label)
    modes = sum([has_coords, has_element, has_label])
    if modes == 0:
        return "Error: provide x/y coordinates, element_id, or label."
    if modes > 1:
        return "Error: use only one click mode — coordinates, element_id, or label."
    if has_coords and (x is None or y is None):
        return "Error: both x and y are required for coordinate clicks."
    aid = _resolve_agent(profile=agent_id)
    return await cloud_tools.click(aid, tab_id, 0 if x is None else x, 0 if y is None else y,
                                   element_id=element_id, label=label)


@mcp.tool()
async def cdp_scroll(direction: str = "down", amount: int = 500,
                     tab_id: str = "auto", agent_id: str = "") -> str:
    """Scroll the page. Returns updated page layout.

    Args:
        direction: "up", "down", "left", or "right"
        amount: pixels to scroll (default 500, roughly one viewport height, max 5000)
    """
    if direction not in ("up", "down", "left", "right"):
        return f"Invalid direction: {direction!r}. Use up, down, left, or right."
    amount = max(1, min(amount, 5000))
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
    _SPECIAL_KEYS = frozenset({
        "Enter", "Tab", "Escape", "Backspace", "Delete",
        "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight",
        "Space", "Home", "End", "PageUp", "PageDown",
    })
    if not (0 <= modifiers <= 15):
        return "Invalid modifiers: must be 0-15 (1=Alt, 2=Ctrl, 4=Meta, 8=Shift)."
    if key not in _SPECIAL_KEYS and len(key) != 1:
        return f"Invalid key: {key!r}. Use a special key name or single character."
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
async def js_eval_frame(frame_id: str, expression: str,
                        tab_id: str = "auto", agent_id: str = "") -> str:
    """Execute JavaScript inside an iframe's context.

    Use list_frames first to find frame IDs. frame_id can be:
    - An index like "0", "1" (from list_frames output)
    - A raw CDP frameId string

    This enables interaction with cross-origin iframes that can't be
    accessed from the main page's JavaScript context (e.g. ProtonMail
    signup forms, embedded payment widgets, challenge iframes).

    Example — read input value from first iframe:
      js_eval_frame(frame_id="0", expression="document.querySelector('input')?.value")
    """
    aid = _resolve_agent(profile=agent_id)
    return await cloud_tools.run_js_in_frame(aid, tab_id, frame_id, expression)


@mcp.tool()
async def cdp_list_frames(tab_id: str = "auto", agent_id: str = "") -> str:
    """List all iframes on the page with their frame IDs and URLs.

    Use this when you see iframes in DDM output and need to interact
    with content inside them. Returns frame indices that can be passed
    to ddm_frame or js_eval_frame.
    """
    aid = _resolve_agent(profile=agent_id)
    return await cloud_tools.list_frames(aid, tab_id)


@mcp.tool()
async def ddm_frame(frame_id: str, tab_id: str = "auto", agent_id: str = "") -> str:
    """Run DDM inside an iframe — returns the same layout map as ddm but for the frame's document.

    Use after cdp_list_frames to get frame_id. frame_id can be:
    - An index like "0", "1" (from cdp_list_frames output)
    - A raw CDP frameId string

    DDM on the main page shows iframes as opaque 'X' blocks — ddm_frame lets
    you see inside: the iframe's own grid, interactive elements, and hints.

    Typical flow when a task requires interacting with an embedded form or widget:
      1. ddm                  → see Iframe: stripe.com hint + X block in grid
      2. cdp_list_frames      → get frame index for stripe.com
      3. ddm_frame("0")       → see the payment form layout inside the iframe
      4. js_eval_frame("0", "document.querySelector('#cardNumber').value")
    """
    aid = _resolve_agent(profile=agent_id)
    try:
        return await cloud_tools.run_ddm_in_frame(aid, tab_id, frame_id, ["--llm-2pass", "--cols", "60"])
    except (NotImplementedError, PrivateCoreError):
        return (
            "ddm_frame is not yet available on this server. "
            "Fallback — inspect the iframe with js_eval_frame instead:\n"
            "  Interactive elements: js_eval_frame(frame_id, "
            "\"[...document.querySelectorAll('input,button,select,textarea,a'))"
            ".map(e => `${e.tagName} id=${e.id} name=${e.name} placeholder=${e.placeholder}`.trim())"
            ".join('\\n')\")\n"
            "  Page text: js_eval_frame(frame_id, \"document.body.innerText.slice(0,2000)\")"
        )


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
    except Exception as screenshot_exc:
        # Screenshot failed — try page text as fallback context.
        try:
            page_text = await cloud_tools.run_ddm(aid, tab_id, ["--text"])
        except Exception:
            raise RuntimeError("Screenshot failed. Could not retrieve page text either.") from screenshot_exc
        # Truncate at last newline boundary to avoid cutting mid-line
        if len(page_text) > 4000:
            cut = page_text[:4000].rfind("\n")
            truncated = page_text[:cut if cut > 0 else 4000] + "\n[truncated]"
        else:
            truncated = page_text
        raise RuntimeError(
            f"Screenshot failed. Page text fallback:\n\n{truncated}"
        ) from screenshot_exc


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
async def cdp_set_tab_alias(alias: str, tab_id: str,
                            agent_id: str = "") -> str:
    """Name a tab for easy reference. Use the alias anywhere tab_id is accepted.

    Example: cdp_set_tab_alias(alias="reddit", tab_id="3C96B...") then
             cdp_click(x=100, y=200, tab_id="reddit")
    """
    alias = alias.strip()
    if not alias:
        return "Alias cannot be empty."
    if len(alias) > 64:
        return "Alias too long (max 64 characters)."
    if alias.lower() == "auto":
        return "Cannot use 'auto' as an alias — it is reserved."
    aid = _resolve_agent(profile=agent_id)
    return await cloud_tools.set_tab_alias(aid, alias, tab_id)


@mcp.tool()
async def cdp_list_tab_aliases(agent_id: str = "") -> str:
    """List all named tab aliases."""
    aid = _resolve_agent(profile=agent_id)
    return await cloud_tools.list_tab_aliases(aid)


@mcp.tool()
async def cdp_set_cookies(cookies: str, tab_id: str = "auto", agent_id: str = "") -> str:
    """Inject cookies for authentication.

    Args:
        cookies: JSON array string. Each cookie needs name, value, domain.
                 Optional: path, secure, httpOnly, sameSite, expires.
    Example: '[{"name":"session","value":"abc123","domain":".example.com"}]'
    """
    aid = _resolve_agent(profile=agent_id)
    try:
        cookie_list = json.loads(cookies)
    except json.JSONDecodeError:
        return "Invalid JSON. Expected an array of cookie objects."
    if not isinstance(cookie_list, list):
        return "cookies must be a JSON array of cookie objects."
    for i, c in enumerate(cookie_list):
        if not isinstance(c, dict):
            return f"Cookie at index {i} is not an object."
        if not all(k in c for k in ("name", "value", "domain")):
            return f"Cookie at index {i} missing required field(s). Need: name, value, domain."
        domain = c["domain"]
        if not isinstance(domain, str) or not domain.strip():
            return f"Cookie at index {i} has empty or invalid domain."
        # Block overly broad domains that could affect all sites
        if domain in (".", ".com", ".org", ".net", ".io", ".co"):
            return f"Cookie at index {i} has overly broad domain '{domain}'."
    return await cloud_tools.set_cookies(aid, tab_id, cookie_list)


@mcp.tool()
async def cdp_get_cookies(urls: str = "", tab_id: str = "auto", agent_id: str = "") -> str:
    """Get cookies from the browser for session saving.

    Args:
        urls: Optional comma-separated URLs to filter by domain. Empty = current page.
    """
    aid = _resolve_agent(profile=agent_id)
    url_list = [u.strip() for u in urls.split(",") if u.strip()] if urls else None
    return await cloud_tools.get_cookies(aid, tab_id, url_list)


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
async def cdp_provision_launch(profile_path: str, agent_id: str = "", stealth: bool = False) -> str:
    """Launch a temporary Chrome with a user profile for OAuth or authenticated browsing.

    Args:
        profile_path: Absolute path to the Chrome profile directory
            (e.g. "/Users/you/Library/Application Support/Google/Chrome/Profile 5").
        agent_id: Agent to provision on (default: auto-detected).
        stealth: Inject fingerprint overrides to evade bot detection.
            Patches navigator.webdriver, outerWidth/outerHeight, WebGL,
            chrome.runtime, and disables automation-controlled blink features.

    Returns the provisioned slot ID and initial tab ID. Use the returned
    prov-prefixed tab_id with ddm, cdp_click, cdp_type, etc.
    """
    aid = _resolve_agent(profile=agent_id)
    # Generate a caller_tag from the API key so cleanup-all only affects
    # this caller's provisioned Chromes, not other MCP clients'.
    api_key = _extract_api_key()
    caller_tag = hashlib.sha256(api_key.encode()).hexdigest()[:12] if api_key else ""
    result = await cloud_tools.provision_launch(aid, profile_path, stealth=stealth, caller_tag=caller_tag)
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
    aid = _resolve_agent(profile=agent_id)  # raises if no API key
    # Pass caller_tag so cleanup-all only kills this caller's provisions.
    # _resolve_agent already validated the key, so _extract_api_key is safe.
    api_key = _extract_api_key()
    if not api_key:
        raise ValueError("Authorization: Bearer <api_key> header is required.")
    caller_tag = hashlib.sha256(api_key.encode()).hexdigest()[:12]
    result = await cloud_tools.provision_cleanup(aid, slot=slot, caller_tag=caller_tag)
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
# Rhythm tools — event-driven SPA automation
# ---------------------------------------------------------------------------

@mcp.tool()
async def rhythm_catch(
    url: str, task: str, catch_terms: str,
    click_text: str = "", tab_id: str = "auto", agent_id: str = "",
) -> str:
    """Scan an SPA page for specific data using event-driven interception.

    Navigates to the URL, injects a DOM scanner, and returns text matching
    your catch terms. Optionally click an element first to trigger SPA
    navigation before scanning. 20-100x faster than screenshot-based extraction.

    Args:
        url: Page URL to navigate to.
        task: What you're looking for (e.g. "find home prices in SF").
        catch_terms: Comma-separated terms to scan for (e.g. "price,bed,sqft").
        click_text: Optional — text of element to click before scanning.
    """
    aid = _resolve_agent(profile=agent_id)
    terms = [t.strip() for t in catch_terms.split(",") if t.strip()]
    return await cloud_tools.run_rhythm_catch(
        aid, tab_id, url, task, terms, click_text=click_text,
    )


@mcp.tool()
async def rhythm_execute(
    url: str, targets: str,
    tab_id: str = "auto", agent_id: str = "",
) -> str:
    """Execute a multi-step action plan on a page at event speed.

    Runs click/type/scroll steps using DOM MutationObservers — no LLM calls
    between steps. 5-20x faster than screenshot-based agents.

    Args:
        url: Page URL to navigate to.
        targets: JSON array of steps. Each step:
            {"action": "click"|"type"|"scroll", "text": "...", "value": "..."}
    """
    aid = _resolve_agent(profile=agent_id)
    try:
        target_list = json.loads(targets)
    except json.JSONDecodeError:
        return "Invalid JSON in targets parameter."
    if not isinstance(target_list, list):
        return "targets must be a JSON array of step objects."
    return await cloud_tools.run_rhythm_execute(aid, tab_id, url, target_list)


@mcp.tool()
async def rhythm_train(
    url: str, click_link_text: str = "",
    tab_id: str = "auto", agent_id: str = "",
) -> str:
    """Train Rhythm on a new SPA page by recording its interactive elements.

    Call once per site pattern — the schema covers all pages matching
    the same route (e.g. train on /product/123, works on /product/456).

    Args:
        url: Page URL to train on.
        click_link_text: Optional — specific link to click for SPA nav observation.
    """
    aid = _resolve_agent(profile=agent_id)
    return await cloud_tools.run_rhythm_train(
        aid, tab_id, url, click_link_text=click_link_text,
    )


@mcp.tool()
async def rhythm_query(
    action: str, url: str = "", domain: str = "", agent_id: str = "",
) -> str:
    """Query what Rhythm already knows about sites.

    Args:
        action: "lookup_url" | "list_all" | "get_graph" | "list_sites"
        url: Required for lookup_url.
        domain: Required for get_graph, optional filter for list_all.
    """
    _resolve_agent(profile=agent_id)
    return await cloud_tools.run_rhythm_query(action, url=url, domain=domain)


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
