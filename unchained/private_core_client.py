"""Client for calling the private core service.

Modes:
- http: call PRIVATE_CORE_URL + /core/execute
- inprocess: call private_core_engine directly (compat mode)
"""

from __future__ import annotations

import json
import os
from typing import Any

import aiohttp
import httpx

from private_core_contracts import (
    CORE_EXECUTE_PATH,
    CORE_SCREENCAST_PATH,
    OP_CLICK,
    OP_CLOSE_TAB,
    OP_CREATE_TAB,
    OP_NAVIGATE,
    OP_PRESS_ENTER,
    OP_PROVISION_CLEANUP,
    OP_PROVISION_LAUNCH,
    OP_RUN_CDP_COMMAND,
    OP_RUN_DDM,
    OP_RUN_INTEL,
    OP_RUN_JS,
    OP_SCREENSHOT,
    OP_SET_FILE,
    OP_SUBMIT_FORM,
    OP_TYPE_TEXT,
    PRIVATE_CORE_OPS,
)

# New ops — import with fallback for staggered deploys where the private-core
# overlay may not yet include these constants.
def _safe_import_op(name: str, default: str) -> str:
    """Import an OP constant from contracts, falling back to string literal."""
    import private_core_contracts as _c
    return getattr(_c, name, default)

OP_KEY_PRESS = _safe_import_op("OP_KEY_PRESS", "key_press")
OP_SCROLL = _safe_import_op("OP_SCROLL", "scroll")
OP_WAIT_READY = _safe_import_op("OP_WAIT_READY", "wait_ready")
OP_SET_TAB_ALIAS = _safe_import_op("OP_SET_TAB_ALIAS", "set_tab_alias")
OP_LIST_TAB_ALIASES = _safe_import_op("OP_LIST_TAB_ALIASES", "list_tab_aliases")
OP_SET_COOKIES = _safe_import_op("OP_SET_COOKIES", "set_cookies")
OP_GET_COOKIES = _safe_import_op("OP_GET_COOKIES", "get_cookies")
OP_PROVISION_STATUS = _safe_import_op("OP_PROVISION_STATUS", "provision_status")
OP_RUN_JS_IN_FRAME = _safe_import_op("OP_RUN_JS_IN_FRAME", "run_js_in_frame")
OP_LIST_FRAMES = _safe_import_op("OP_LIST_FRAMES", "list_frames")
OP_RUN_DDM_IN_FRAME = _safe_import_op("OP_RUN_DDM_IN_FRAME", "run_ddm_in_frame")
OP_RHYTHM_CATCH = _safe_import_op("OP_RHYTHM_CATCH", "rhythm_catch")
OP_RHYTHM_EXECUTE = _safe_import_op("OP_RHYTHM_EXECUTE", "rhythm_execute")
OP_RHYTHM_TRAIN = _safe_import_op("OP_RHYTHM_TRAIN", "rhythm_train")
OP_RHYTHM_QUERY = _safe_import_op("OP_RHYTHM_QUERY", "rhythm_query")


class PrivateCoreError(RuntimeError):
    """Error returned while calling private core."""


class PrivateCoreClient:
    """Boundary client used by public modules to execute private-core operations."""

    def __init__(
        self,
        base_url: str | None = None,
        mode: str | None = None,
        token: str | None = None,
        timeout_seconds: float = 120.0,
    ):
        env_url = os.environ.get("PRIVATE_CORE_URL", "")
        self.base_url = (base_url if base_url is not None else env_url).rstrip("/")

        env_mode = os.environ.get("PRIVATE_CORE_MODE", "").strip().lower()
        resolved_mode = (mode or env_mode or ("http" if self.base_url else "inprocess")).lower()
        if resolved_mode not in {"http", "inprocess"}:
            raise ValueError(f"Unknown PRIVATE_CORE mode: {resolved_mode}")
        self.mode = resolved_mode

        self.token = token if token is not None else os.environ.get("PRIVATE_CORE_TOKEN", "")
        self.timeout_seconds = timeout_seconds
        self._http_client: httpx.AsyncClient | None = None

    async def execute(self, op: str, **kwargs: Any) -> Any:
        """Execute one private-core operation and return its result payload."""
        if op not in PRIVATE_CORE_OPS:
            raise PrivateCoreError(f"Unsupported private-core op: {op}")

        if self.mode == "http":
            if not self.base_url:
                raise PrivateCoreError("PRIVATE_CORE_URL is required for http mode")
            return await self._execute_http(op, **kwargs)
        return await self._execute_inprocess(op, **kwargs)

    def _get_http_client(self) -> httpx.AsyncClient:
        """Return a persistent HTTP client, creating it on first use."""
        if self._http_client is None or self._http_client.is_closed:
            headers = {"Content-Type": "application/json"}
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            self._http_client = httpx.AsyncClient(
                timeout=self.timeout_seconds,
                headers=headers,
            )
        return self._http_client

    async def close(self):
        """Close the persistent HTTP client."""
        if self._http_client is not None and not self._http_client.is_closed:
            await self._http_client.aclose()
            self._http_client = None

    async def _execute_http(self, op: str, **kwargs: Any) -> Any:
        payload = {"op": op, **kwargs}

        try:
            client = self._get_http_client()
            resp = await client.post(f"{self.base_url}{CORE_EXECUTE_PATH}", json=payload)
        except Exception as exc:  # pragma: no cover - network path
            raise PrivateCoreError(f"Failed to reach private core: {exc}") from exc

        try:
            data = resp.json()
        except Exception as exc:
            raise PrivateCoreError(f"Invalid response from private core: HTTP {resp.status_code}") from exc

        if not resp.is_success:
            msg = data.get("error", f"HTTP {resp.status_code}")
            raise PrivateCoreError(str(msg))

        if not data.get("ok", False):
            raise PrivateCoreError(str(data.get("error", "Unknown private-core failure")))
        return data.get("result")

    async def _execute_inprocess(self, op: str, **kwargs: Any) -> Any:
        # Lazy import keeps public module import-time free of private implementation.
        import private_core_engine as engine

        dispatch = {
            OP_RUN_DDM: engine.run_ddm,
            OP_RUN_INTEL: engine.run_intel,
            OP_RUN_CDP_COMMAND: engine.run_cdp_command,
            OP_RUN_JS: engine.run_js,
            OP_NAVIGATE: engine.navigate,
            OP_CLICK: engine.click,
            OP_TYPE_TEXT: engine.type_text,
            OP_PRESS_ENTER: engine.press_enter,
            OP_SUBMIT_FORM: engine.submit_form,
            OP_SCREENSHOT: engine.screenshot,
            OP_CREATE_TAB: engine.create_tab,
            OP_PROVISION_LAUNCH: engine.provision_launch,
            OP_PROVISION_CLEANUP: engine.provision_cleanup,
            OP_CLOSE_TAB: engine.close_tab,
        }
        # Ops that may not yet exist in the engine (safe for staggered deploys)
        _optional_ops = [
            (OP_SET_FILE, "set_file"),
            (OP_PROVISION_STATUS, "provision_status"),
            (OP_SCROLL, "scroll"),
            (OP_KEY_PRESS, "key_press"),
            (OP_WAIT_READY, "wait_ready"),
            (OP_SET_TAB_ALIAS, "set_tab_alias"),
            (OP_LIST_TAB_ALIASES, "list_tab_aliases"),
            (OP_SET_COOKIES, "set_cookies"),
            (OP_GET_COOKIES, "get_cookies"),
            (OP_RUN_JS_IN_FRAME, "run_js_in_frame"),
            (OP_LIST_FRAMES, "list_frames"),
            (OP_RUN_DDM_IN_FRAME, "run_ddm_in_frame"),
        ]
        for op_name, fn_name in _optional_ops:
            fn = getattr(engine, fn_name, None)
            if fn is not None:
                dispatch[op_name] = fn
        fn = dispatch.get(op)
        if fn is None:
            raise NotImplementedError(f"Op {op!r} not available in engine")
        return await fn(**kwargs)

    async def stream_screencast(
        self,
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
        """Yield screencast websocket events from private-core."""
        normalized_image_format = str(image_format or "jpeg").strip().lower() or "jpeg"
        if normalized_image_format not in {"jpeg", "png"}:
            raise PrivateCoreError("Unsupported screencast image format")

        if self.mode == "http":
            if not self.base_url:
                raise PrivateCoreError("PRIVATE_CORE_URL is required for http mode")
            headers: dict[str, str] = {}
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            read_timeout = max(float(stream_timeout) + 15.0, float(self.timeout_seconds))
            params = {
                "agent_id": agent_id,
                "tab_id": tab_id,
                "width": str(width),
                "height": str(height),
                "quality": str(quality),
                "format": normalized_image_format,
                "every_nth_frame": str(every_nth_frame),
                "max_frames": str(max_frames),
                "stream_timeout": str(stream_timeout),
            }
            timeout = aiohttp.ClientTimeout(
                total=read_timeout + 10.0,
                sock_connect=self.timeout_seconds,
                sock_read=read_timeout,
            )
            try:
                async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                    async with session.ws_connect(
                        f"{self.base_url}{CORE_SCREENCAST_PATH}",
                        params=params,
                        heartbeat=30,
                        autoping=True,
                    ) as ws:
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                try:
                                    payload = json.loads(msg.data)
                                except Exception as exc:
                                    raise PrivateCoreError("Invalid screencast message from private core") from exc
                                if isinstance(payload, dict):
                                    yield payload
                                    if payload.get("type") == "status":
                                        return
                            elif msg.type in {
                                aiohttp.WSMsgType.CLOSE,
                                aiohttp.WSMsgType.CLOSED,
                                aiohttp.WSMsgType.CLOSING,
                            }:
                                return
                            elif msg.type == aiohttp.WSMsgType.ERROR:
                                raise PrivateCoreError("Private core screencast websocket error")
            except Exception as exc:
                if isinstance(exc, PrivateCoreError):
                    raise
                raise PrivateCoreError(f"Failed to reach private core screencast: {exc}") from exc
            return

        import private_core_engine as engine

        async for frame in engine.stream_screencast(
            agent_id,
            tab_id,
            relay_host=relay_host,
            relay_port=relay_port,
            width=width,
            height=height,
            quality=quality,
            image_format=normalized_image_format,
            every_nth_frame=every_nth_frame,
            max_frames=max_frames,
            stream_timeout=stream_timeout,
        ):
            yield {
                "type": "frame",
                "mime": frame.get("mime", "image/jpeg"),
                "data": frame.get("data", ""),
                "metadata": frame.get("metadata", {}),
            }

    async def run_ddm(self, agent_id: str, tab_id: str, flags: list[str], relay_host: str, relay_port: int) -> str:
        return await self.execute(
            OP_RUN_DDM,
            agent_id=agent_id,
            tab_id=tab_id,
            flags=flags,
            relay_host=relay_host,
            relay_port=relay_port,
        )

    async def run_intel(self, agent_id: str, tab_id: str, flags: list[str], relay_host: str, relay_port: int) -> str:
        return await self.execute(
            OP_RUN_INTEL,
            agent_id=agent_id,
            tab_id=tab_id,
            flags=flags,
            relay_host=relay_host,
            relay_port=relay_port,
        )

    async def run_cdp_command(
        self,
        agent_id: str,
        tab_id: str,
        method: str,
        params: dict | None,
        relay_host: str,
        relay_port: int,
        overlay: bool = False,
    ) -> dict:
        return await self.execute(
            OP_RUN_CDP_COMMAND,
            agent_id=agent_id,
            tab_id=tab_id,
            method=method,
            params=params,
            relay_host=relay_host,
            relay_port=relay_port,
            overlay=overlay,
        )

    async def run_js(self, agent_id: str, tab_id: str, expression: str, relay_host: str, relay_port: int, overlay: bool = False) -> str:
        return await self.execute(
            OP_RUN_JS,
            agent_id=agent_id,
            tab_id=tab_id,
            expression=expression,
            relay_host=relay_host,
            relay_port=relay_port,
            overlay=overlay,
        )

    async def navigate(self, agent_id: str, tab_id: str, url: str, relay_host: str, relay_port: int) -> str:
        return await self.execute(
            OP_NAVIGATE,
            agent_id=agent_id,
            tab_id=tab_id,
            url=url,
            relay_host=relay_host,
            relay_port=relay_port,
        )

    async def click(self, agent_id: str, tab_id: str, x: int = 0, y: int = 0,
                    relay_host: str = "127.0.0.1", relay_port: int = 8765,
                    element_id: str = "", label: str = "") -> str:
        kwargs = dict(
            agent_id=agent_id,
            tab_id=tab_id,
            x=x,
            y=y,
            relay_host=relay_host,
            relay_port=relay_port,
        )
        if element_id:
            kwargs["element_id"] = element_id
        if label:
            kwargs["label"] = label
        return await self.execute(OP_CLICK, **kwargs)

    async def scroll(self, agent_id: str, tab_id: str, direction: str, amount: int, relay_host: str, relay_port: int) -> str:
        return await self.execute(
            OP_SCROLL,
            agent_id=agent_id,
            tab_id=tab_id,
            direction=direction,
            amount=amount,
            relay_host=relay_host,
            relay_port=relay_port,
        )

    async def type_text(self, agent_id: str, tab_id: str, text: str, relay_host: str, relay_port: int) -> str:
        return await self.execute(
            OP_TYPE_TEXT,
            agent_id=agent_id,
            tab_id=tab_id,
            text=text,
            relay_host=relay_host,
            relay_port=relay_port,
        )

    async def press_enter(self, agent_id: str, tab_id: str, relay_host: str, relay_port: int) -> str:
        return await self.execute(
            OP_PRESS_ENTER,
            agent_id=agent_id,
            tab_id=tab_id,
            relay_host=relay_host,
            relay_port=relay_port,
        )

    async def key_press(self, agent_id: str, tab_id: str, key: str, modifiers: int, relay_host: str, relay_port: int) -> str:
        return await self.execute(
            OP_KEY_PRESS,
            agent_id=agent_id,
            tab_id=tab_id,
            key=key,
            modifiers=modifiers,
            relay_host=relay_host,
            relay_port=relay_port,
        )

    async def submit_form(self, agent_id: str, tab_id: str, relay_host: str, relay_port: int) -> str:
        return await self.execute(
            OP_SUBMIT_FORM,
            agent_id=agent_id,
            tab_id=tab_id,
            relay_host=relay_host,
            relay_port=relay_port,
        )

    async def screenshot(self, agent_id: str, tab_id: str, relay_host: str, relay_port: int,
                         *, format: str = "png", quality: int | None = None,
                         max_width: int | None = None, max_height: int | None = None) -> str:
        kwargs: dict = dict(
            agent_id=agent_id, tab_id=tab_id,
            relay_host=relay_host, relay_port=relay_port,
            format=format,
        )
        if quality is not None:
            kwargs["quality"] = quality
        if max_width is not None:
            kwargs["max_width"] = max_width
        if max_height is not None:
            kwargs["max_height"] = max_height
        return await self.execute(OP_SCREENSHOT, **kwargs)

    async def create_tab(self, agent_id: str, url: str, relay_host: str, relay_port: int) -> str | None:
        return await self.execute(
            OP_CREATE_TAB,
            agent_id=agent_id,
            url=url,
            relay_host=relay_host,
            relay_port=relay_port,
        )

    async def provision_launch(self, agent_id: str, profile_path: str, relay_host: str, relay_port: int, stealth: bool = False, caller_tag: str = "") -> dict:
        return await self.execute(
            OP_PROVISION_LAUNCH,
            agent_id=agent_id,
            profile_path=profile_path,
            relay_host=relay_host,
            relay_port=relay_port,
            stealth=stealth,
            caller_tag=caller_tag,
        )

    async def provision_cleanup(self, agent_id: str, relay_host: str, relay_port: int, slot: str = "", caller_tag: str = "") -> dict:
        return await self.execute(
            OP_PROVISION_CLEANUP,
            agent_id=agent_id,
            relay_host=relay_host,
            relay_port=relay_port,
            slot=slot,
            caller_tag=caller_tag,
        )

    async def provision_status(self, agent_id: str, relay_host: str, relay_port: int) -> dict:
        return await self.execute(
            OP_PROVISION_STATUS,
            agent_id=agent_id,
            relay_host=relay_host,
            relay_port=relay_port,
        )

    async def set_file(self, agent_id: str, tab_id: str, selector: str, file_path: str, relay_host: str, relay_port: int) -> str:
        return await self.execute(
            OP_SET_FILE,
            agent_id=agent_id,
            tab_id=tab_id,
            selector=selector,
            file_path=file_path,
            relay_host=relay_host,
            relay_port=relay_port,
        )

    async def wait_ready(self, agent_id: str, tab_id: str, strategy: str = "both",
                         relay_host: str = "127.0.0.1", relay_port: int = 8765) -> str:
        return await self.execute(
            OP_WAIT_READY,
            agent_id=agent_id,
            tab_id=tab_id,
            strategy=strategy,
            relay_host=relay_host,
            relay_port=relay_port,
        )

    async def set_tab_alias(self, agent_id: str, alias: str, tab_id: str) -> str:
        return await self.execute(
            OP_SET_TAB_ALIAS,
            agent_id=agent_id,
            alias=alias,
            tab_id=tab_id,
        )

    async def list_tab_aliases(self, agent_id: str) -> str:
        return await self.execute(
            OP_LIST_TAB_ALIASES,
            agent_id=agent_id,
        )

    async def set_cookies(self, agent_id: str, tab_id: str, cookies: list,
                          relay_host: str = "127.0.0.1", relay_port: int = 8765) -> str:
        return await self.execute(
            OP_SET_COOKIES,
            agent_id=agent_id,
            tab_id=tab_id,
            cookies=cookies,
            relay_host=relay_host,
            relay_port=relay_port,
        )

    async def get_cookies(self, agent_id: str, tab_id: str, urls: list | None = None,
                          relay_host: str = "127.0.0.1", relay_port: int = 8765) -> str:
        kwargs = dict(
            agent_id=agent_id,
            tab_id=tab_id,
            relay_host=relay_host,
            relay_port=relay_port,
        )
        if urls is not None:
            kwargs["urls"] = urls
        return await self.execute(OP_GET_COOKIES, **kwargs)

    async def close_tab(self, agent_id: str, tab_id: str, relay_host: str, relay_port: int) -> bool:
        return await self.execute(
            OP_CLOSE_TAB,
            agent_id=agent_id,
            tab_id=tab_id,
            relay_host=relay_host,
            relay_port=relay_port,
        )

    async def run_js_in_frame(self, agent_id: str, tab_id: str, frame_id: str,
                              expression: str, relay_host: str, relay_port: int) -> str:
        return await self.execute(
            OP_RUN_JS_IN_FRAME,
            agent_id=agent_id,
            tab_id=tab_id,
            frame_id=frame_id,
            expression=expression,
            relay_host=relay_host,
            relay_port=relay_port,
        )

    async def list_frames(self, agent_id: str, tab_id: str, relay_host: str, relay_port: int) -> str:
        return await self.execute(
            OP_LIST_FRAMES,
            agent_id=agent_id,
            tab_id=tab_id,
            relay_host=relay_host,
            relay_port=relay_port,
        )

    async def run_ddm_in_frame(self, agent_id: str, tab_id: str, frame_id: str,
                               flags: list[str], relay_host: str, relay_port: int) -> str:
        return await self.execute(
            OP_RUN_DDM_IN_FRAME,
            agent_id=agent_id,
            tab_id=tab_id,
            frame_id=frame_id,
            flags=flags,
            relay_host=relay_host,
            relay_port=relay_port,
        )

    # --- Rhythm tools ---

    async def run_rhythm_catch(self, agent_id: str, tab_id: str,
                               url: str, task: str, catch_terms: list,
                               click_text: str = "",
                               relay_host: str = "127.0.0.1", relay_port: int = 8765) -> str:
        return await self.execute(
            OP_RHYTHM_CATCH,
            agent_id=agent_id, tab_id=tab_id,
            url=url, task=task, catch_terms=catch_terms,
            click_text=click_text,
            relay_host=relay_host, relay_port=relay_port,
        )

    async def run_rhythm_execute(self, agent_id: str, tab_id: str,
                                 url: str, targets: list,
                                 relay_host: str = "127.0.0.1", relay_port: int = 8765) -> str:
        return await self.execute(
            OP_RHYTHM_EXECUTE,
            agent_id=agent_id, tab_id=tab_id,
            url=url, targets=targets,
            relay_host=relay_host, relay_port=relay_port,
        )

    async def run_rhythm_train(self, agent_id: str, tab_id: str,
                               url: str, click_link_text: str = "",
                               relay_host: str = "127.0.0.1", relay_port: int = 8765) -> str:
        return await self.execute(
            OP_RHYTHM_TRAIN,
            agent_id=agent_id, tab_id=tab_id,
            url=url, click_link_text=click_link_text,
            relay_host=relay_host, relay_port=relay_port,
        )

    async def run_rhythm_query(self, action: str, url: str = "", domain: str = "") -> str:
        return await self.execute(
            OP_RHYTHM_QUERY,
            action=action, url=url, domain=domain,
        )


_default_client: PrivateCoreClient | None = None


def get_private_core_client() -> PrivateCoreClient:
    """Return the process-wide private-core client."""
    global _default_client
    if _default_client is None:
        _default_client = PrivateCoreClient()
    return _default_client


def _set_private_core_client_for_tests(client: PrivateCoreClient | None):
    """Test helper: replace/reset the process-wide client."""
    global _default_client
    _default_client = client
