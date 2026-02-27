# Debugging Map

This map is for triaging agent/web issues quickly using structured `[trace]` logs.

## Key Trace Events

- `chat.msg.in`: Incoming `/web/chat` request accepted.
- `chat.msg.forwarded`: Message successfully forwarded to chat agent WebSocket.
- `chat.msg.forward_error`: WebSocket forward failed before SSE stream.
- `chat.msg.stream_end`: SSE stream ended (normal completion or disconnect).
- `cmd.in`: Incoming `/web/cmd` action request.
- `cmd.ok`: `/web/cmd` action executed successfully.
- `cmd.unknown`: Unsupported `/web/cmd` action.
- `cmd.chrome_unavailable`: Relay/bridge/Chrome unavailable (returns 502 guidance).
- `cmd.error`: Non-connectivity command failure (returns 500).

## Core Correlation Fields

- `req_id`: Request correlation ID (from `X-Request-ID` or generated server-side).
- `user_id`: Authenticated user identity.
- `agent_id`: Authenticated agent namespace.
- `session_id`: Chat session correlation key.
- `action`: `/web/cmd` action name.
- `tab_id`: Target tab identifier (`auto` or explicit ID).

## Triage Workflow

1. Find `chat.msg.in` for the user/session.
2. Verify `chat.msg.forwarded` appears for the same `req_id`.
3. Confirm `chat.msg.stream_end` and inspect `stream_completed`.
4. For tool failures, pivot to `cmd.in` and check for `cmd.ok` vs `cmd.chrome_unavailable` / `cmd.error`.
