"""Shared benchmark utilities."""

from __future__ import annotations

import json
import os
import shlex
from dataclasses import dataclass, field
from typing import Any

from private_core_client import PrivateCoreClient


@dataclass(slots=True)
class BenchmarkConfig:
    """Resolved browser/runtime configuration for one benchmark task."""

    agent_id: str | None = None
    relay_host: str = "127.0.0.1"
    relay_port: int = 8765
    private_core_url: str | None = None
    private_core_token: str | None = None
    tab_id: str = "auto"

    def with_tab(self, tab_id: str) -> "BenchmarkConfig":
        return BenchmarkConfig(
            agent_id=self.agent_id,
            relay_host=self.relay_host,
            relay_port=self.relay_port,
            private_core_url=self.private_core_url,
            private_core_token=self.private_core_token,
            tab_id=tab_id or "auto",
        )

    def subprocess_env(self) -> dict[str, str]:
        """Environment for agent subprocesses targeting this task's tab."""
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        if self.agent_id:
            env["CDP_AGENT_ID"] = self.agent_id
        env["CDP_RELAY_HOST"] = self.relay_host
        env["CDP_RELAY_PORT"] = str(self.relay_port)
        env["CDP_TAB_ID"] = self.tab_id or "auto"
        if self.private_core_url:
            env["PRIVATE_CORE_URL"] = self.private_core_url
        if self.private_core_token:
            env["PRIVATE_CORE_TOKEN"] = self.private_core_token
        return env


@dataclass(slots=True)
class TaskResult:
    """Normalized benchmark result for one agent-task execution."""

    answer: str = ""
    tool_calls: int = 0
    turns: int = 0
    loops: int = 0
    wall_seconds: float = 0.0
    error: str = ""
    tool_log: list[dict[str, Any]] = field(default_factory=list)
    intermediate_goal_mode: str = "off"
    intermediate_goal_enabled: bool = False
    hardness: dict[str, Any] = field(default_factory=dict)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    _recent_signatures: list[str] = field(default_factory=list, repr=False)

    def record_tool_call(
        self,
        tool_name: str,
        tool_input: dict[str, Any] | None,
        *,
        call_signature: str | None = None,
    ):
        self.tool_calls += 1
        self.tool_log.append(
            {
                "turn": self.tool_calls,
                "tool": tool_name,
                "input": json.dumps(tool_input or {}, ensure_ascii=False)[:400],
            }
        )
        sig = call_signature or f"{tool_name}:{json.dumps(tool_input or {}, sort_keys=True)}"
        self._recent_signatures.append(sig)
        if len(self._recent_signatures) > 6:
            self._recent_signatures.pop(0)
        if self._recent_signatures.count(sig) >= 3:
            self.loops += 1


def apply_default_tab_id(input_data: dict[str, Any], default_tab_id: str) -> dict[str, Any]:
    """Populate tab_id when the model omits it."""
    data = dict(input_data or {})
    current = str(data.get("tab_id", "") or "").strip()
    if not current or current == "auto":
        data["tab_id"] = default_tab_id or "auto"
    return data


def parse_flag_string(raw_flags: str | list[str] | tuple[str, ...], default: list[str]) -> list[str]:
    """Parse tool flag strings the same way a shell would."""
    if isinstance(raw_flags, (list, tuple)):
        return [str(flag) for flag in raw_flags]
    raw = (raw_flags or "").strip()
    if not raw:
        return list(default)
    return shlex.split(raw)


def extract_cdp_tool_name(command: str) -> str:
    """Extract logical tool name from a cdp_tool.py shell command."""
    if "cdp_tool.py" not in command:
        return "bash"

    parts = command.split()
    try:
        idx = next(i for i, part in enumerate(parts) if "cdp_tool.py" in part)
    except StopIteration:
        return "cdp_tool"

    if idx + 1 >= len(parts):
        return "cdp_tool"

    subcmd = parts[idx + 1]
    if subcmd in (
        "ddm",
        "intel",
        "navigate",
        "click",
        "type",
        "js",
        "screenshot",
        "tabs",
        "new-tab",
        "close-tab",
    ):
        return subcmd
    return "cdp_tool"


class BenchmarkBrowser:
    """Explicit browser controller for benchmark tab lifecycle and tools."""

    def __init__(self, config: BenchmarkConfig):
        self.config = config
        self.client = PrivateCoreClient(
            base_url=config.private_core_url or None,
            token=config.private_core_token or None,
        )

    async def close(self):
        await self.client.close()

    async def create_tab(self, url: str = "about:blank") -> str:
        if not self.config.agent_id:
            raise RuntimeError("agent_id is required to allocate benchmark tabs")
        tab_id = ""
        if self.config.private_core_url:
            cdp_result = await self.client.run_cdp_command(
                self.config.agent_id,
                "auto",
                "Target.createTarget",
                {"url": url},
                self.config.relay_host,
                self.config.relay_port,
            )
            if isinstance(cdp_result, dict):
                tab_id = str(cdp_result.get("targetId", "") or "")
        if not tab_id:
            tab_id = await self.client.create_tab(
                self.config.agent_id,
                url,
                self.config.relay_host,
                self.config.relay_port,
            ) or ""
        if not tab_id:
            raise RuntimeError("create_tab returned no tab id")
        return tab_id

    async def close_tab(self, tab_id: str):
        if not self.config.agent_id or not tab_id:
            return
        if self.config.private_core_url:
            await self.client.close_tab(
                self.config.agent_id,
                tab_id,
                self.config.relay_host,
                self.config.relay_port,
            )
            return
        await self.client.close_tab(
            self.config.agent_id,
            tab_id,
            self.config.relay_host,
            self.config.relay_port,
        )

    async def execute_tool(self, name: str, input_data: dict[str, Any], *, default_tab_id: str) -> str:
        """Execute one browser tool and return its text result."""
        if not self.config.agent_id:
            return f"Tool error ({name}): agent_id is required"

        args = apply_default_tab_id(input_data, default_tab_id)
        tab_id = str(args.get("tab_id", default_tab_id) or default_tab_id or "auto")
        relay_host = self.config.relay_host
        relay_port = self.config.relay_port
        agent_id = self.config.agent_id

        try:
            if name == "ddm":
                flags = parse_flag_string(args.get("flags", ""), ["--llm-2pass", "--cols", "60"])
                return await self.client.run_ddm(agent_id, tab_id, flags, relay_host, relay_port)
            if name == "intel_probe":
                return await self.client.run_intel(agent_id, tab_id, ["--probe"], relay_host, relay_port)
            if name == "intel_extract":
                flags = ["--extract"]
                strategy = str(args.get("strategy", "") or "").strip()
                if strategy:
                    flags += ["--strategy", strategy]
                return await self.client.run_intel(agent_id, tab_id, flags, relay_host, relay_port)
            if name == "intel_stores":
                return await self.client.run_intel(agent_id, tab_id, ["--stores"], relay_host, relay_port)
            if name == "intel_find_paths":
                return await self.client.run_intel(
                    agent_id,
                    tab_id,
                    ["--find-paths", str(args["global_name"]), str(args["key"])],
                    relay_host,
                    relay_port,
                )
            if name == "navigate":
                return await self.client.navigate(
                    agent_id,
                    tab_id,
                    str(args["url"]),
                    relay_host,
                    relay_port,
                )
            if name == "click":
                return await self.client.click(
                    agent_id,
                    tab_id,
                    int(args["x"]),
                    int(args["y"]),
                    relay_host,
                    relay_port,
                )
            if name == "type_text":
                return await self.client.type_text(
                    agent_id,
                    tab_id,
                    str(args["text"]),
                    relay_host,
                    relay_port,
                )
            if name == "press_enter":
                return await self.client.press_enter(agent_id, tab_id, relay_host, relay_port)
            if name == "submit_form":
                return await self.client.submit_form(agent_id, tab_id, relay_host, relay_port)
            if name == "js_eval":
                return await self.client.run_js(
                    agent_id,
                    tab_id,
                    str(args["expression"]),
                    relay_host,
                    relay_port,
                )
            if name == "screenshot":
                data = await self.client.screenshot(agent_id, tab_id, relay_host, relay_port)
                return f"[screenshot captured, {len(data)} bytes base64]"
            return f"Unknown tool: {name}"
        except Exception as exc:
            return f"Tool error ({name}): {exc}"
