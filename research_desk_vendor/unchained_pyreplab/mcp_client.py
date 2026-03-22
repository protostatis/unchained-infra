from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

DEFAULT_ENDPOINT = "https://api.unchainedsky.com/mcp"
DEFAULT_AGENT_ENV_PATH = Path.home() / "unchained-agent" / ".env"


class MCPError(RuntimeError):
    """Raised when an MCP request fails."""


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def infer_agents_endpoint(endpoint: str) -> str:
    clean = endpoint.rstrip("/")
    if clean.endswith("/mcp"):
        return clean[: -len("/mcp")] + "/api/agents"
    return "https://api.unchainedsky.com/api/agents"


def infer_api_base(endpoint: str) -> str:
    clean = endpoint.rstrip("/")
    if clean.endswith("/mcp"):
        return clean[: -len("/mcp")]
    parsed = urllib.parse.urlsplit(clean)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return clean


def extract_first_agent_id(payload: Any) -> Optional[str]:
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                agent_id = item.get("agent_id") or item.get("id")
                if isinstance(agent_id, str) and agent_id:
                    return agent_id
        return None

    if isinstance(payload, dict):
        agent_id = payload.get("agent_id") or payload.get("id")
        if isinstance(agent_id, str) and agent_id:
            return agent_id
        agents = payload.get("agents")
        if isinstance(agents, list):
            return extract_first_agent_id(agents)
    return None


def fetch_agent_id(api_key: str, endpoint: str, timeout: int) -> Optional[str]:
    request = urllib.request.Request(
        infer_agents_endpoint(endpoint),
        method="GET",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
    except Exception:
        return None

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None
    return extract_first_agent_id(payload)


@dataclass
class ResolvedCredentials:
    api_key: Optional[str]
    agent_id: Optional[str]
    endpoint: str
    source: str


def resolve_credentials(
    api_key: Optional[str],
    agent_id: Optional[str],
    endpoint: str,
    timeout: int,
) -> ResolvedCredentials:
    env_file_values = parse_env_file(DEFAULT_AGENT_ENV_PATH)
    resolved_api_key = api_key or os.environ.get("UNCHAINED_API_KEY") or env_file_values.get("UNCHAINED_API_KEY")
    resolved_agent_id = agent_id or os.environ.get("UNCHAINED_AGENT_ID") or env_file_values.get("UNCHAINED_AGENT_ID")
    source = "flags"

    if not api_key and os.environ.get("UNCHAINED_API_KEY"):
        source = "env"
    elif not api_key and env_file_values.get("UNCHAINED_API_KEY"):
        source = "agent-env-file"

    if resolved_api_key and not resolved_agent_id:
        discovered = fetch_agent_id(resolved_api_key, endpoint=endpoint, timeout=timeout)
        if discovered:
            resolved_agent_id = discovered
            source = "auto-discovered-agent"

    return ResolvedCredentials(
        api_key=resolved_api_key,
        agent_id=resolved_agent_id,
        endpoint=endpoint,
        source=source,
    )


def parse_sse_json_events(raw_body: str) -> list[Any]:
    events: list[Any] = []
    buffer: list[str] = []
    for line in raw_body.splitlines():
        if line.startswith("data:"):
            chunk = line[5:].lstrip()
            if chunk == "[DONE]":
                continue
            buffer.append(chunk)
            continue
        if line.strip():
            continue
        if buffer:
            joined = "\n".join(buffer)
            buffer.clear()
            try:
                events.append(json.loads(joined))
            except json.JSONDecodeError:
                events.append(joined)

    if buffer:
        joined = "\n".join(buffer)
        try:
            events.append(json.loads(joined))
        except json.JSONDecodeError:
            events.append(joined)
    return events


def parse_rpc_response(raw_body: str) -> Optional[dict[str, Any]]:
    text = raw_body.strip()
    if not text:
        return None

    try:
        payload = json.loads(text)
        if isinstance(payload, dict):
            return payload
    except json.JSONDecodeError:
        pass

    for event in reversed(parse_sse_json_events(raw_body)):
        if isinstance(event, dict):
            return event
    return None


def extract_text(result: dict[str, Any]) -> str:
    content = result.get("content")
    if isinstance(content, list):
        texts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    texts.append(text)
        if texts:
            return "\n".join(texts)

    if "structuredContent" in result:
        return json.dumps(result["structuredContent"], indent=2)

    return json.dumps(result, indent=2)


def parse_json_if_possible(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


class MCPClient:
    def __init__(self, endpoint: str, api_key: str, timeout: int = 45, debug: bool = False):
        self.endpoint = endpoint
        self.api_key = api_key
        self.timeout = timeout
        self.debug = debug
        self.session_id: Optional[str] = None

    def initialize(self) -> None:
        response = self._rpc_request(
            {
                "jsonrpc": "2.0",
                "id": "init-1",
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "unchained-pyreplab", "version": "0.1.0"},
                },
            },
            include_session=False,
            allow_empty=False,
        )
        if not self.session_id and isinstance(response.get("result"), dict):
            maybe_sid = response["result"].get("sessionId")
            if isinstance(maybe_sid, str) and maybe_sid:
                self.session_id = maybe_sid
        if not self.session_id:
            raise MCPError("initialize succeeded but no MCP session id was returned")

        self._rpc_request(
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            },
            include_session=True,
            allow_empty=True,
        )

    def list_tools(self) -> list[str]:
        response = self._rpc_request(
            {
                "jsonrpc": "2.0",
                "id": "tools-list-1",
                "method": "tools/list",
                "params": {},
            },
            include_session=True,
            allow_empty=False,
        )
        result = response.get("result")
        if not isinstance(result, dict):
            return []
        tools = result.get("tools")
        if not isinstance(tools, list):
            return []
        names: list[str] = []
        for tool in tools:
            if isinstance(tool, dict) and isinstance(tool.get("name"), str):
                names.append(tool["name"])
        return names

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        response = self._rpc_request(
            {
                "jsonrpc": "2.0",
                "id": f"tool-call-{name}",
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
            include_session=True,
            allow_empty=False,
        )
        result = response.get("result")
        if isinstance(result, dict) and result.get("isError"):
            raise MCPError(extract_text(result))
        if isinstance(result, dict):
            return result
        return {"raw_result": result}

    def list_tabs(self, agent_id: str) -> list[dict[str, Any]]:
        payload = self._http_json(
            "GET",
            "{base}/api/agents/{agent_id}/http/GET/json".format(
                base=infer_api_base(self.endpoint),
                agent_id=urllib.parse.quote(agent_id, safe=""),
            ),
        )
        if isinstance(payload, dict):
            payload = payload.get("body", payload)
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict) and item.get("type") == "page"]
        return []

    def create_tab(self, agent_id: str, url: str = "about:blank") -> dict[str, Any]:
        encoded_url = urllib.parse.quote(url, safe=':/?#[]@!$&\'()*+,;=-._~')
        payload = self._http_json(
            "GET",
            "{base}/api/agents/{agent_id}/http/PUT/json/new?{url}".format(
                base=infer_api_base(self.endpoint),
                agent_id=urllib.parse.quote(agent_id, safe=""),
                url=encoded_url,
            ),
        )
        if isinstance(payload, dict):
            payload = payload.get("body", payload)
        if isinstance(payload, dict):
            return payload
        raise MCPError(f"Unexpected create_tab response: {payload!r}")

    def close_tab(self, agent_id: str, tab_id: str) -> bool:
        payload = self._http_json(
            "GET",
            "{base}/api/agents/{agent_id}/http/PUT/json/close/{tab_id}".format(
                base=infer_api_base(self.endpoint),
                agent_id=urllib.parse.quote(agent_id, safe=""),
                tab_id=urllib.parse.quote(tab_id, safe=""),
            ),
        )
        if isinstance(payload, dict):
            payload = payload.get("body", payload)
        if isinstance(payload, dict) and "success" in payload:
            return bool(payload.get("success"))
        return True

    def _rpc_request(
        self,
        payload: dict[str, Any],
        *,
        include_session: bool,
        allow_empty: bool,
    ) -> dict[str, Any]:
        request = urllib.request.Request(
            self.endpoint,
            method="POST",
            data=json.dumps(payload).encode("utf-8"),
            headers=self._headers(include_session),
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw_body = response.read().decode("utf-8", "replace")
                sid = response.headers.get("Mcp-Session-Id") or response.headers.get("mcp-session-id")
                if sid:
                    self.session_id = sid
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", "replace")
            raise MCPError(f"HTTP {exc.code}: {error_body}") from exc
        except urllib.error.URLError as exc:
            raise MCPError(f"Network error: {exc.reason}") from exc

        parsed = parse_rpc_response(raw_body)
        if parsed is None:
            if allow_empty:
                return {}
            raise MCPError(f"Unexpected non-JSON response body: {raw_body[:300]}")
        if "error" in parsed:
            raise MCPError(str(parsed["error"]))
        return parsed

    def _http_json(
        self,
        method: str,
        url: str,
        *,
        body: Optional[dict[str, Any]] = None,
    ) -> Any:
        data = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, method=method, data=data, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw_body = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", "replace")
            raise MCPError(f"HTTP {exc.code}: {error_body}") from exc
        except urllib.error.URLError as exc:
            raise MCPError(f"Network error: {exc.reason}") from exc
        if not raw_body.strip():
            return {}
        try:
            return json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise MCPError(f"Unexpected non-JSON HTTP response body: {raw_body[:300]}") from exc

    def _headers(self, include_session: bool) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self.api_key}",
        }
        if include_session and self.session_id:
            headers["mcp-session-id"] = self.session_id
        return headers
