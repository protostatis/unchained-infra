"""WebMCP Tool — Discover and call WebMCP tools on any page.

Shims navigator.modelContext before page load to intercept tool
registrations from WebMCP-enabled sites. Works without Chrome's
early preview API — we polyfill it via CDP.

Usage:
    cd unchained/
    uv run webmcp.py                          # List tools on current page
    uv run webmcp.py <url>                    # Navigate + list tools
    uv run webmcp.py call <name> <json>       # Call a tool with JSON params
    uv run webmcp.py call <name> k=v k2=v2    # Call with key=value params
    uv run webmcp.py schema <name>            # Show full schema for a tool
"""

import asyncio
import json
import sys

from cdp import CDP, _get_ws_url, _ensure_chrome


# Injected BEFORE page JS runs via Page.addScriptToEvaluateOnNewDocument.
# Creates a shim for navigator.modelContext that captures all registerTool
# calls, storing the full tool objects for later querying.
SHIM_JS = r"""
(function() {
    // Don't clobber if native API exists
    if (navigator.modelContext) {
        // Native API — wrap registerTool to also capture tools
        var _native = navigator.modelContext;
        var _tools = {};
        var _origRegister = _native.registerTool.bind(_native);
        var _origUnregister = _native.unregisterTool.bind(_native);

        _native.registerTool = function(tool) {
            _tools[tool.name] = tool;
            return _origRegister(tool);
        };
        _native.unregisterTool = function(name) {
            delete _tools[name];
            return _origUnregister(name);
        };
        window.__wmcp = {tools: _tools, native: true};
        return;
    }

    // No native API — create full shim
    var tools = {};
    navigator.modelContext = {
        registerTool: function(tool) {
            tools[tool.name] = tool;
        },
        unregisterTool: function(name) {
            delete tools[name];
        }
    };
    window.__wmcp = {tools: tools, native: false};
})()
"""

# Extract captured tool metadata (no execute functions — those aren't serializable)
LIST_JS = r"""
(function() {
    var w = window.__wmcp;
    if (!w) return JSON.stringify({error: "shim not installed"});
    var tools = w.tools;
    var result = [];
    for (var name in tools) {
        var t = tools[name];
        result.push({
            name: t.name,
            description: t.description || "",
            inputSchema: t.inputSchema || {},
            outputSchema: t.outputSchema || {},
            annotations: t.annotations || {}
        });
    }
    return JSON.stringify({native: w.native, count: result.length, tools: result});
})()
"""

# Get full schema for one tool
SCHEMA_JS = r"""
(function(toolName) {
    var w = window.__wmcp;
    if (!w) return JSON.stringify({error: "shim not installed"});
    var t = w.tools[toolName];
    if (!t) return JSON.stringify({error: "tool not found: " + toolName});
    return JSON.stringify({
        name: t.name,
        description: t.description || "",
        inputSchema: t.inputSchema || {},
        outputSchema: t.outputSchema || {},
        annotations: t.annotations || {}
    });
})(%TOOL_NAME%)
"""

# Call a tool's execute function — always returns a Promise for awaitPromise
CALL_JS = r"""
(async function(toolName, params) {
    var w = window.__wmcp;
    if (!w) return JSON.stringify({error: "shim not installed"});
    var t = w.tools[toolName];
    if (!t) return JSON.stringify({error: "tool not found: " + toolName});
    if (!t.execute) return JSON.stringify({error: "tool has no execute function"});
    try {
        var result = await t.execute(params);
        return JSON.stringify({ok: true, result: result});
    } catch(e) {
        return JSON.stringify({ok: false, error: String(e)});
    }
})(%TOOL_NAME%, %PARAMS%)
"""


def render_tools(data):
    """Render tool list for terminal output."""
    lines = []
    native = data.get('native', False)
    count = data.get('count', 0)
    lines.append(f"=== WebMCP Tools ({count}) ===")
    lines.append(f"API: {'native navigator.modelContext' if native else 'shim (polyfill)'}")
    lines.append("")

    if count == 0:
        lines.append("No WebMCP tools registered on this page.")
        return '\n'.join(lines)

    for t in data['tools']:
        name = t['name']
        desc = t.get('description', '')
        anno = t.get('annotations', {})
        readonly = anno.get('readOnlyHint', 'unknown')

        lines.append(f"  {name}")
        if desc:
            lines.append(f"    {desc}")
        lines.append(f"    readonly={readonly}")

        # Show input params
        schema = t.get('inputSchema', {})
        props = schema.get('properties', {})
        required = set(schema.get('required', []))
        if props:
            lines.append("    params:")
            for pname, pschema in props.items():
                ptype = pschema.get('type', '?')
                pdesc = pschema.get('description', '')
                req = '*' if pname in required else ' '
                # Truncate description
                if len(pdesc) > 60:
                    pdesc = pdesc[:57] + '...'
                lines.append(f"      {req} {pname}: {ptype} — {pdesc}")
        lines.append("")

    return '\n'.join(lines)


def render_schema(data):
    """Render full schema for one tool."""
    lines = []
    lines.append(f"=== {data['name']} ===")
    lines.append(f"Description: {data.get('description', '')}")
    lines.append(f"Annotations: {json.dumps(data.get('annotations', {}))}")
    lines.append("")
    lines.append("Input Schema:")
    lines.append(json.dumps(data.get('inputSchema', {}), indent=2))
    lines.append("")
    lines.append("Output Schema:")
    lines.append(json.dumps(data.get('outputSchema', {}), indent=2))
    return '\n'.join(lines)


async def run(args):
    url = None
    command = "list"  # default
    tool_name = None
    tool_params = {}

    i = 0
    while i < len(args):
        if args[i] == 'call' and i + 1 < len(args):
            command = 'call'
            tool_name = args[i + 1]
            i += 2
            # Remaining args are params
            remaining = args[i:]
            if remaining and remaining[0].startswith('{'):
                # JSON params
                tool_params = json.loads(' '.join(remaining))
            else:
                # key=value params
                for kv in remaining:
                    if '=' in kv:
                        k, v = kv.split('=', 1)
                        # Try to parse as number/bool
                        try:
                            v = json.loads(v)
                        except (json.JSONDecodeError, ValueError):
                            pass
                        tool_params[k] = v
            break
        elif args[i] == 'schema' and i + 1 < len(args):
            command = 'schema'
            tool_name = args[i + 1]
            i += 2
        elif not args[i].startswith('-'):
            url = args[i]
            i += 1
        else:
            i += 1

    _ensure_chrome()
    ws_url = _get_ws_url()
    cdp = CDP(ws_url)
    await cdp.connect()

    try:
        script_id = ''

        if command == 'call' and not url:
            # For call without URL: inject shim directly on current page
            # (no reload — preserves current page state)
            check = await cdp.execute_js('!!window.__wmcp')
            has_shim = check.get('result', {}).get('value', False)
            if not has_shim:
                await cdp.execute_js(SHIM_JS)
        else:
            # For list/schema/call-with-url: install shim before page load
            result = await cdp.send('Page.addScriptToEvaluateOnNewDocument',
                                    {'source': SHIM_JS})
            script_id = result.get('identifier', '')

            if url:
                await cdp.navigate(url, wait=3)
            else:
                await cdp.send('Page.reload', {})
                await asyncio.sleep(3)

        if command == 'list':
            if script_id:
                await cdp.send('Page.removeScriptToEvaluateOnNewDocument',
                               {'identifier': script_id})
            r = await cdp.execute_js(LIST_JS)
            raw = r.get('result', {}).get('value', '{}')
            data = json.loads(raw)
            if 'error' in data:
                print(f"Error: {data['error']}")
            else:
                print(render_tools(data))

        elif command == 'schema':
            if script_id:
                await cdp.send('Page.removeScriptToEvaluateOnNewDocument',
                               {'identifier': script_id})
            js = SCHEMA_JS.replace('%TOOL_NAME%', json.dumps(tool_name))
            r = await cdp.execute_js(js)
            raw = r.get('result', {}).get('value', '{}')
            data = json.loads(raw)
            if 'error' in data:
                print(f"Error: {data['error']}")
            else:
                print(render_schema(data))

        elif command == 'call':
            # Keep shim installed — tool may navigate (triggers new page load)
            js = CALL_JS.replace('%TOOL_NAME%', json.dumps(tool_name))
            js = js.replace('%PARAMS%', json.dumps(tool_params))
            # Use awaitPromise since execute functions are async
            r = await cdp.send('Runtime.evaluate', {
                'expression': js,
                'returnByValue': True,
                'awaitPromise': True,
            })
            # Clean up shim after call completes
            if script_id:
                await cdp.send('Page.removeScriptToEvaluateOnNewDocument',
                               {'identifier': script_id})
            raw = r.get('result', {}).get('value', '{}')
            if isinstance(raw, dict):
                data = raw
            else:
                data = json.loads(raw)
            if 'error' in data:
                print(f"Error: {data['error']}")
            elif data.get('ok'):
                result = data.get('result')
                if isinstance(result, str):
                    print(result)
                else:
                    print(json.dumps(result, indent=2))
            else:
                print(f"Call failed: {data.get('error', 'unknown')}")

            # Wait for any navigation triggered by the call
            await asyncio.sleep(2)

            # Show post-call page info and any new tools
            url_r = await cdp.execute_js('window.location.href')
            print(f"\nPage: {url_r.get('result', {}).get('value', '')}")

            tools_r = await cdp.execute_js(LIST_JS)
            tools_raw = tools_r.get('result', {}).get('value', '{}')
            tools_data = json.loads(tools_raw)
            if tools_data.get('count', 0) > 0:
                print(f"Tools now available: {', '.join(t['name'] for t in tools_data['tools'])}")

    finally:
        if cdp.ws:
            await cdp.ws.close()


def main():
    asyncio.run(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
