"""Shared /web/cmd command dispatch helpers."""

from __future__ import annotations


class CmdInputError(ValueError):
    """Invalid user input for a command action."""


class UnknownCmdActionError(ValueError):
    """Unsupported /web/cmd action."""


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


async def _cmd_rhythm_train(body, agent_id, tab_id, relay_host, relay_port, cloud_tools):
    url = body.get("url")
    if not url:
        raise CmdInputError("url required")
    result = await cloud_tools.run_rhythm_train(
        agent_id, tab_id, url,
        click_link_text=body.get("click_link_text", ""),
        relay_host=relay_host, relay_port=relay_port)
    return {"type": "text", "data": result}


async def _cmd_rhythm_catch(body, agent_id, tab_id, relay_host, relay_port, cloud_tools):
    url = body.get("url")
    task = body.get("task")
    catch_terms_raw = body.get("catch_terms")
    if not url or not task or not catch_terms_raw:
        raise CmdInputError("url, task, and catch_terms required")
    if isinstance(catch_terms_raw, list):
        terms = [t.strip() for t in catch_terms_raw if t.strip()]
    else:
        terms = [t.strip() for t in catch_terms_raw.split(",") if t.strip()]
    result = await cloud_tools.run_rhythm_catch(
        agent_id, tab_id, url, task, terms,
        click_text=body.get("click_text", ""),
        relay_host=relay_host, relay_port=relay_port)
    return {"type": "text", "data": result}


async def _cmd_rhythm_execute(body, agent_id, tab_id, relay_host, relay_port, cloud_tools):
    import json
    url = body.get("url")
    targets_raw = body.get("targets")
    if not url or not targets_raw:
        raise CmdInputError("url and targets required")
    if isinstance(targets_raw, list):
        target_list = targets_raw
    else:
        try:
            target_list = json.loads(targets_raw)
        except (json.JSONDecodeError, TypeError):
            raise CmdInputError("Invalid JSON in targets parameter")
        if not isinstance(target_list, list):
            raise CmdInputError("targets must be a JSON array of step objects")
    result = await cloud_tools.run_rhythm_execute(
        agent_id, tab_id, url, target_list,
        relay_host=relay_host, relay_port=relay_port)
    return {"type": "text", "data": result}


async def _cmd_rhythm_query(body, agent_id, tab_id, relay_host, relay_port, cloud_tools):
    query_action = body.get("query_action")
    if not query_action:
        raise CmdInputError("action required")
    result = await cloud_tools.run_rhythm_query(
        query_action, url=body.get("url", ""), domain=body.get("domain", ""))
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
    "rhythm_train": _cmd_rhythm_train,
    "rhythm_catch": _cmd_rhythm_catch,
    "rhythm_execute": _cmd_rhythm_execute,
    "rhythm_query": _cmd_rhythm_query,
}


def is_chrome_unavailable_error(exc: Exception) -> bool:
    err = str(exc).lower()
    return any(
        k in err
        for k in (
            "connect",
            "refused",
            "timed out",
            "not connected",
            "no close frame",
            "ws_error",
            "1006",
        )
    )


async def run_cmd_action(
    *,
    action: str,
    body: dict,
    agent_id: str,
    tab_id: str,
    relay_host: str,
    relay_port: int,
    cloud_tools,
) -> dict:
    """Run one /web/cmd action and return normalized response payload."""
    runner = _CMD_ACTIONS.get(action)
    if runner is None:
        raise UnknownCmdActionError(action)
    return await runner(body, agent_id, tab_id, relay_host, relay_port, cloud_tools)
