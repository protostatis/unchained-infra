## First-Look Preview Protocol

The first-look page has two independent streams:

- Chat SSE on `/web/chat` carries run lifecycle.
- Preview WS on `/web/first-look/preview/ws` carries screencast transport.

The live bug came from treating preview transport EOF as a semantic
run-complete signal. That was incorrect because the screencast stack below
`chat_flow` cannot distinguish "run finished" from "browser channel closed".

### Current Sequence

```text
Browser -> /web/chat            start run (SSE)
Browser -> /preview/ws          open preview websocket
Web -> Browser                  preview.attached
Web -> Browser                  preview.frame x N

private-core screencast iterator ends cleanly
Web -> Browser                  preview.ended(reason="done", retriable=false)
Browser                         stops preview retry logic

Meanwhile the chat SSE run can still continue for minutes.
```

### Proposed Sequence

```text
Browser -> /web/chat            start run (SSE)
Browser -> /preview/ws          open preview websocket
Web -> Browser                  preview.attached
Web -> Browser                  preview.frame x N

private-core screencast iterator ends cleanly
Web -> Browser                  preview.reconnecting(attempt=1)
Web                              rebuilds screencast transport
Web -> Browser                  preview.frame x M
```

Actual run completion comes only from the chat SSE `done` event:

```text
Chat SSE -> Browser             done
Browser                         marks run complete
Browser                         closes preview websocket after a short grace
```

### Public Preview Events

- `preview.attached`
- `preview.frame`
- `preview.reconnecting`
- `preview.ended`

`preview.ended` is transport-terminal only. Its `reason` is one of:

- `slow_client`
- `max_reconnects`
- `fatal`

`preview.ended` does not mean "run finished". That meaning belongs to the chat
SSE `done` event only.
