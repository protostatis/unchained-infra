"""Public cloud tool wrappers.

This module is the stable boundary used by API/Web/agents.
Implementation lives behind `private_core_client`.
"""

from __future__ import annotations

from private_core_client import get_private_core_client


def _client():
    return get_private_core_client()


async def run_ddm(agent_id: str, tab_id: str, flags: list[str], relay_host: str = "127.0.0.1", relay_port: int = 8765) -> str:
    return await _client().run_ddm(agent_id, tab_id, flags, relay_host, relay_port)


async def run_intel(agent_id: str, tab_id: str, flags: list[str], relay_host: str = "127.0.0.1", relay_port: int = 8765) -> str:
    return await _client().run_intel(agent_id, tab_id, flags, relay_host, relay_port)


async def run_cdp_command(
    agent_id: str,
    tab_id: str,
    method: str,
    params: dict | None = None,
    relay_host: str = "127.0.0.1",
    relay_port: int = 8765,
) -> dict:
    return await _client().run_cdp_command(agent_id, tab_id, method, params, relay_host, relay_port)


async def run_js(agent_id: str, tab_id: str, expression: str, relay_host: str = "127.0.0.1", relay_port: int = 8765) -> str:
    return await _client().run_js(agent_id, tab_id, expression, relay_host, relay_port)


async def navigate(agent_id: str, tab_id: str, url: str, relay_host: str = "127.0.0.1", relay_port: int = 8765) -> str:
    return await _client().navigate(agent_id, tab_id, url, relay_host, relay_port)


async def click(agent_id: str, tab_id: str, x: int = 0, y: int = 0,
                relay_host: str = "127.0.0.1", relay_port: int = 8765,
                element_id: str = "", label: str = "") -> str:
    return await _client().click(agent_id, tab_id, x, y, relay_host, relay_port,
                                 element_id=element_id, label=label)


async def scroll(agent_id: str, tab_id: str, direction: str = "down", amount: int = 500, relay_host: str = "127.0.0.1", relay_port: int = 8765) -> str:
    return await _client().scroll(agent_id, tab_id, direction, amount, relay_host, relay_port)


async def type_text(agent_id: str, tab_id: str, text: str, relay_host: str = "127.0.0.1", relay_port: int = 8765) -> str:
    return await _client().type_text(agent_id, tab_id, text, relay_host, relay_port)


async def press_enter(agent_id: str, tab_id: str, relay_host: str = "127.0.0.1", relay_port: int = 8765) -> str:
    return await _client().press_enter(agent_id, tab_id, relay_host, relay_port)


async def key_press(agent_id: str, tab_id: str, key: str, modifiers: int = 0, relay_host: str = "127.0.0.1", relay_port: int = 8765) -> str:
    return await _client().key_press(agent_id, tab_id, key, modifiers, relay_host, relay_port)


async def submit_form(agent_id: str, tab_id: str, relay_host: str = "127.0.0.1", relay_port: int = 8765) -> str:
    return await _client().submit_form(agent_id, tab_id, relay_host, relay_port)


async def screenshot(agent_id: str, tab_id: str, relay_host: str = "127.0.0.1", relay_port: int = 8765) -> str:
    return await _client().screenshot(agent_id, tab_id, relay_host, relay_port)


async def create_tab(agent_id: str, url: str = "about:blank", relay_host: str = "127.0.0.1", relay_port: int = 8765) -> str | None:
    return await _client().create_tab(agent_id, url, relay_host, relay_port)


async def provision_launch(agent_id: str, profile_path: str, relay_host: str = "127.0.0.1", relay_port: int = 8765) -> dict:
    return await _client().provision_launch(agent_id, profile_path, relay_host, relay_port)


async def provision_cleanup(agent_id: str, relay_host: str = "127.0.0.1", relay_port: int = 8765, slot: str = "") -> dict:
    return await _client().provision_cleanup(agent_id, relay_host, relay_port, slot=slot)


async def provision_status(agent_id: str, relay_host: str = "127.0.0.1", relay_port: int = 8765) -> dict:
    return await _client().provision_status(agent_id, relay_host, relay_port)


async def set_file(agent_id: str, tab_id: str, selector: str, file_path: str, relay_host: str = "127.0.0.1", relay_port: int = 8765) -> str:
    return await _client().set_file(agent_id, tab_id, selector, file_path, relay_host, relay_port)


async def wait_ready(agent_id: str, tab_id: str, strategy: str = "both", relay_host: str = "127.0.0.1", relay_port: int = 8765) -> str:
    return await _client().wait_ready(agent_id, tab_id, strategy, relay_host, relay_port)


async def close_tab(agent_id: str, tab_id: str, relay_host: str = "127.0.0.1", relay_port: int = 8765) -> bool:
    return await _client().close_tab(agent_id, tab_id, relay_host, relay_port)
