"""Functional healthcheck for the hosted Unbrowser MCP router."""

from __future__ import annotations

import http.client
import json


def main() -> None:
    initialize = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": "healthcheck",
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "unbrowser-mcp-healthcheck", "version": "1"},
            },
        }
    )
    connection = http.client.HTTPConnection("localhost", 8767, timeout=10)
    try:
        connection.request(
            "POST",
            "/mcp",
            body=initialize,
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        response.read()
        session_id = response.getheader("Mcp-Session-Id")
        if response.status != 200 or not session_id:
            raise RuntimeError(f"MCP initialize failed with HTTP {response.status}")
        connection.request("DELETE", "/mcp", headers={"Mcp-Session-Id": session_id})
        close_response = connection.getresponse()
        close_response.read()
        if close_response.status not in (200, 202, 204):
            raise RuntimeError(f"MCP cleanup failed with HTTP {close_response.status}")
    finally:
        connection.close()


if __name__ == "__main__":
    main()
