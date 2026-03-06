"""Command dispatch helpers for /web/cmd."""

from __future__ import annotations


class CmdInputError(ValueError):
    """Raised when a /web/cmd request is missing required input."""


class UnknownCmdActionError(ValueError):
    """Raised when /web/cmd action is unknown."""


def is_chrome_unavailable_error(exc: Exception) -> bool:
    """Return True when the error indicates Chrome/CDP is unavailable."""
    err = str(exc).lower()
    return any(
        key in err
        for key in (
            "connect",
            "refused",
            "timed out",
            "not connected",
            "no close frame",
            "ws_error",
            "1006",
        )
    )


async def _cmd_ddm(body, agent_id, tab_id, relay_host, relay_port, cloud_tools):
    flags = body.get("flags", ["--llm-2pass", "--cols", "60"])
    result = await cloud_tools.run_ddm(agent_id, tab_id, flags, relay_host, relay_port)
    return {"type": "text", "data": result}


async def _cmd_text(body, agent_id, tab_id, relay_host, relay_port, cloud_tools):
    flags = ["--text"]
    find = body.get("find")
    if find:
        flags.extend(["--find", find])
    result = await cloud_tools.run_ddm(agent_id, tab_id, flags, relay_host, relay_port)
    return {"type": "text", "data": result}


async def _cmd_navigate(body, agent_id, tab_id, relay_host, relay_port, cloud_tools):
    url = body.get("url")
    if not url:
        raise CmdInputError("url required")
    result = await cloud_tools.navigate(agent_id, tab_id, url, relay_host, relay_port)
    return {"type": "text", "data": result}


async def _cmd_screenshot(body, agent_id, tab_id, relay_host, relay_port, cloud_tools):
    _ = body
    result = await cloud_tools.screenshot(agent_id, tab_id, relay_host, relay_port)
    return {"type": "image", "data": result}


async def _cmd_js(body, agent_id, tab_id, relay_host, relay_port, cloud_tools):
    expression = body.get("expression")
    if not expression:
        raise CmdInputError("expression required")
    result = await cloud_tools.run_js(agent_id, tab_id, expression, relay_host, relay_port)
    return {"type": "text", "data": result}


async def _cmd_click(body, agent_id, tab_id, relay_host, relay_port, cloud_tools):
    x = body.get("x")
    y = body.get("y")
    if x is None or y is None:
        raise CmdInputError("x and y required")
    result = await cloud_tools.click(agent_id, tab_id, int(x), int(y), relay_host, relay_port)
    return {"type": "text", "data": result}


async def _cmd_type(body, agent_id, tab_id, relay_host, relay_port, cloud_tools):
    text = body.get("text")
    if not text:
        raise CmdInputError("text required")
    result = await cloud_tools.type_text(agent_id, tab_id, text, relay_host, relay_port)
    return {"type": "text", "data": result}


async def _cmd_intel(body, agent_id, tab_id, relay_host, relay_port, cloud_tools):
    flags = body.get("flags", ["--probe"])
    result = await cloud_tools.run_intel(agent_id, tab_id, flags, relay_host, relay_port)
    return {"type": "text", "data": result}


_CMD_ACTIONS = {
    "ddm": _cmd_ddm,
    "text": _cmd_text,
    "navigate": _cmd_navigate,
    "screenshot": _cmd_screenshot,
    "js": _cmd_js,
    "click": _cmd_click,
    "type": _cmd_type,
    "intel": _cmd_intel,
}


async def run_cmd_action(*, action, body, agent_id, tab_id, relay_host, relay_port, cloud_tools):
    runner = _CMD_ACTIONS.get(action)
    if runner is None:
        raise UnknownCmdActionError(action)
    return await runner(body, agent_id, tab_id, relay_host, relay_port, cloud_tools)
