"""Client for calling the private core service.

Modes:
- http: call PRIVATE_CORE_URL + /core/execute
- inprocess: call private_core_engine directly (compat mode)
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from private_core_contracts import (
    CORE_EXECUTE_PATH,
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
    OP_SUBMIT_FORM,
    OP_TYPE_TEXT,
    PRIVATE_CORE_OPS,
)


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

    async def execute(self, op: str, **kwargs: Any) -> Any:
        """Execute one private-core operation and return its result payload."""
        if op not in PRIVATE_CORE_OPS:
            raise PrivateCoreError(f"Unsupported private-core op: {op}")

        if self.mode == "http":
            if not self.base_url:
                raise PrivateCoreError("PRIVATE_CORE_URL is required for http mode")
            return await self._execute_http(op, **kwargs)
        return await self._execute_inprocess(op, **kwargs)

    async def _execute_http(self, op: str, **kwargs: Any) -> Any:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        payload = {"op": op, **kwargs}

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                resp = await client.post(f"{self.base_url}{CORE_EXECUTE_PATH}", json=payload, headers=headers)
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
        fn = dispatch[op]
        return await fn(**kwargs)

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
    ) -> dict:
        return await self.execute(
            OP_RUN_CDP_COMMAND,
            agent_id=agent_id,
            tab_id=tab_id,
            method=method,
            params=params,
            relay_host=relay_host,
            relay_port=relay_port,
        )

    async def run_js(self, agent_id: str, tab_id: str, expression: str, relay_host: str, relay_port: int) -> str:
        return await self.execute(
            OP_RUN_JS,
            agent_id=agent_id,
            tab_id=tab_id,
            expression=expression,
            relay_host=relay_host,
            relay_port=relay_port,
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

    async def click(self, agent_id: str, tab_id: str, x: int, y: int, relay_host: str, relay_port: int) -> str:
        return await self.execute(
            OP_CLICK,
            agent_id=agent_id,
            tab_id=tab_id,
            x=x,
            y=y,
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

    async def submit_form(self, agent_id: str, tab_id: str, relay_host: str, relay_port: int) -> str:
        return await self.execute(
            OP_SUBMIT_FORM,
            agent_id=agent_id,
            tab_id=tab_id,
            relay_host=relay_host,
            relay_port=relay_port,
        )

    async def screenshot(self, agent_id: str, tab_id: str, relay_host: str, relay_port: int) -> str:
        return await self.execute(
            OP_SCREENSHOT,
            agent_id=agent_id,
            tab_id=tab_id,
            relay_host=relay_host,
            relay_port=relay_port,
        )

    async def create_tab(self, agent_id: str, url: str, relay_host: str, relay_port: int) -> str | None:
        return await self.execute(
            OP_CREATE_TAB,
            agent_id=agent_id,
            url=url,
            relay_host=relay_host,
            relay_port=relay_port,
        )

    async def provision_launch(self, agent_id: str, profile_path: str, relay_host: str, relay_port: int) -> dict:
        return await self.execute(
            OP_PROVISION_LAUNCH,
            agent_id=agent_id,
            profile_path=profile_path,
            relay_host=relay_host,
            relay_port=relay_port,
        )

    async def provision_cleanup(self, agent_id: str, relay_host: str, relay_port: int) -> bool:
        return await self.execute(
            OP_PROVISION_CLEANUP,
            agent_id=agent_id,
            relay_host=relay_host,
            relay_port=relay_port,
        )

    async def close_tab(self, agent_id: str, tab_id: str, relay_host: str, relay_port: int) -> bool:
        return await self.execute(
            OP_CLOSE_TAB,
            agent_id=agent_id,
            tab_id=tab_id,
            relay_host=relay_host,
            relay_port=relay_port,
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
