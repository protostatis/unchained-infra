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
    overlay: bool = False,
) -> dict:
    return await _client().run_cdp_command(agent_id, tab_id, method, params, relay_host, relay_port, overlay=overlay)


async def run_js(agent_id: str, tab_id: str, expression: str, relay_host: str = "127.0.0.1", relay_port: int = 8765, overlay: bool = False) -> str:
    return await _client().run_js(agent_id, tab_id, expression, relay_host, relay_port, overlay=overlay)


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


async def stream_screencast(
    agent_id: str,
    tab_id: str,
    *,
    relay_host: str = "127.0.0.1",
    relay_port: int = 8765,
    width: int = 1280,
    height: int = 720,
    quality: int = 70,
    image_format: str = "jpeg",
    every_nth_frame: int = 1,
    max_frames: int = 600,
    stream_timeout: float = 120.0,
):
    async for event in _client().stream_screencast(
        agent_id,
        tab_id,
        relay_host=relay_host,
        relay_port=relay_port,
        width=width,
        height=height,
        quality=quality,
        image_format=image_format,
        every_nth_frame=every_nth_frame,
        max_frames=max_frames,
        stream_timeout=stream_timeout,
    ):
        yield event


async def create_tab(agent_id: str, url: str = "about:blank", relay_host: str = "127.0.0.1", relay_port: int = 8765) -> str | None:
    return await _client().create_tab(agent_id, url, relay_host, relay_port)


async def provision_launch(agent_id: str, profile_path: str, relay_host: str = "127.0.0.1", relay_port: int = 8765, stealth: bool = False, caller_tag: str = "") -> dict:
    return await _client().provision_launch(agent_id, profile_path, relay_host, relay_port, stealth=stealth, caller_tag=caller_tag)


async def provision_cleanup(agent_id: str, relay_host: str = "127.0.0.1", relay_port: int = 8765, slot: str = "", caller_tag: str = "") -> dict:
    return await _client().provision_cleanup(agent_id, relay_host, relay_port, slot=slot, caller_tag=caller_tag)


async def provision_status(agent_id: str, relay_host: str = "127.0.0.1", relay_port: int = 8765) -> dict:
    return await _client().provision_status(agent_id, relay_host, relay_port)


async def set_file(agent_id: str, tab_id: str, selector: str, file_path: str, relay_host: str = "127.0.0.1", relay_port: int = 8765) -> str:
    return await _client().set_file(agent_id, tab_id, selector, file_path, relay_host, relay_port)


async def wait_ready(agent_id: str, tab_id: str, strategy: str = "both", relay_host: str = "127.0.0.1", relay_port: int = 8765) -> str:
    return await _client().wait_ready(agent_id, tab_id, strategy, relay_host, relay_port)


async def set_tab_alias(agent_id: str, alias: str, tab_id: str) -> str:
    return await _client().set_tab_alias(agent_id, alias, tab_id)


async def list_tab_aliases(agent_id: str) -> str:
    return await _client().list_tab_aliases(agent_id)


async def set_cookies(agent_id: str, tab_id: str, cookies: list,
                      relay_host: str = "127.0.0.1", relay_port: int = 8765) -> str:
    return await _client().set_cookies(agent_id, tab_id, cookies, relay_host, relay_port)


async def get_cookies(agent_id: str, tab_id: str, urls: list | None = None,
                      relay_host: str = "127.0.0.1", relay_port: int = 8765) -> str:
    return await _client().get_cookies(agent_id, tab_id, urls, relay_host, relay_port)


async def close_tab(agent_id: str, tab_id: str, relay_host: str = "127.0.0.1", relay_port: int = 8765) -> bool:
    return await _client().close_tab(agent_id, tab_id, relay_host, relay_port)


async def run_js_in_frame(agent_id: str, tab_id: str, frame_id: str,
                          expression: str, relay_host: str = "127.0.0.1",
                          relay_port: int = 8765) -> str:
    return await _client().run_js_in_frame(agent_id, tab_id, frame_id,
                                           expression, relay_host, relay_port)


async def list_frames(agent_id: str, tab_id: str,
                      relay_host: str = "127.0.0.1",
                      relay_port: int = 8765) -> str:
    return await _client().list_frames(agent_id, tab_id, relay_host, relay_port)


async def run_ddm_in_frame(agent_id: str, tab_id: str, frame_id: str,
                           flags: list[str], relay_host: str = "127.0.0.1",
                           relay_port: int = 8765) -> str:
    return await _client().run_ddm_in_frame(agent_id, tab_id, frame_id,
                                            flags, relay_host, relay_port)


# --- Rhythm tools ---

async def run_rhythm_catch(agent_id: str, tab_id: str, url: str, task: str, catch_terms: list,
                           click_text: str = "",
                           relay_host: str = "127.0.0.1", relay_port: int = 8765) -> str:
    return await _client().run_rhythm_catch(
        agent_id, tab_id, url, task, catch_terms,
        click_text, relay_host, relay_port,
    )


async def run_rhythm_execute(agent_id: str, tab_id: str, url: str, targets: list,
                             relay_host: str = "127.0.0.1", relay_port: int = 8765) -> str:
    return await _client().run_rhythm_execute(
        agent_id, tab_id, url, targets, relay_host, relay_port,
    )


async def run_rhythm_train(agent_id: str, tab_id: str, url: str, click_link_text: str = "",
                           relay_host: str = "127.0.0.1", relay_port: int = 8765) -> str:
    return await _client().run_rhythm_train(
        agent_id, tab_id, url, click_link_text, relay_host, relay_port,
    )


async def run_rhythm_query(action: str, url: str = "", domain: str = "") -> str:
    return await _client().run_rhythm_query(action, url, domain)
