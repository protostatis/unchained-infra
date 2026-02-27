# Unchained Live Stream System

AI-orchestrated live travel show that controls a real Chrome browser via CDP,
narrated and directed by Claude, streamable to YouTube/Twitch via OBS.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  agent_stream.py  (main process)                                │
│                                                                 │
│  ┌─────────────┐   ┌──────────────┐   ┌──────────────────────┐ │
│  │ Claude agent│   │  Maps / CDP  │   │  OBS WebSocket v5    │ │
│  │ every 2 min │──▶│  Street View │   │  auto start/stop     │ │
│  └─────────────┘   └──────────────┘   └──────────────────────┘ │
│         │                                                       │
│  ┌──────▼──────────────────────────────────────────────────┐   │
│  │  _emit() → _event_log + _tail_queues                    │   │
│  └──────────────────────────────────────────────────────┬──┘   │
│                                                         │       │
│  ┌──────────────────┐   ┌──────────────────────────────▼──┐   │
│  │  HTTP overlay    │   │  Unix socket server              │   │
│  │  :8878/overlay   │   │  ~/.unchained/stream.sock        │   │
│  │  (OBS browser    │   │  cmds: status/go/say/website/    │   │
│  │   source)        │   │  skip/tail                       │   │
│  └──────────────────┘   └─────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
                                    │
                    ┌───────────────▼────────────────┐
                    │  stream_ctl.py  (control REPL) │
                    │                                │
                    │  • Claude-powered NL interface │
                    │  • background tail thread      │
                    │  • /log  /events  raw cmds     │
                    └────────────────────────────────┘
```

**Data flows:**
- `agent_stream.py` owns the browser, all state, and the event bus
- `stream_ctl.py` connects to the socket for commands and event tailing
- The OBS overlay polls `/state` every 2s over HTTP
- The agent calls `claude --print` as a subprocess (no separate API key)

---

## Quick Start

```bash
# 1. Start the stream (opens director terminal automatically)
cd unchained/
uv run agent_stream.py --start "Shibuya Crossing, Tokyo"

# 2. With OBS auto-start
uv run agent_stream.py --obs --start "Shibuya Crossing, Tokyo"

# 3. Faster agent calls for testing
uv run agent_stream.py --interval 30 --no-chat

# 4. Reconnect the director terminal manually if closed
uv run stream_ctl.py
```

---

## Files

| File | Purpose |
|------|---------|
| `unchained/agent_stream.py` | Main orchestration process |
| `unchained/stream_ctl.py` | Director control REPL |
| `unchained/maps_stream.py` | Older dumb iteration script (no agent) |
| `.env.obs` | OBS credentials (gitignored) |
| `~/.unchained/stream.sock` | Unix socket (created at runtime) |
| `~/.unchained/stream.log` | Raw stdout/stderr log (truncated each run) |
| `~/.unchained/ctl_history` | readline history for stream_ctl |

---

## agent_stream.py

### CLI flags

| Flag | Default | Description |
|------|---------|-------------|
| `--start "Place"` | Times Square, New York | Starting location |
| `--interval N` | 120 | Seconds between Claude decisions |
| `--maps-tab <id>` | auto-detect | Chrome tab ID for Google Maps |
| `--chat-tab <id>` | auto-detect | Chrome tab ID for YouTube chat |
| `--no-chat` | off | Skip YouTube chat polling |
| `--no-ctl` | off | Don't auto-launch director terminal |
| `--obs` | off | Enable OBS WebSocket control |
| `--obs-host <ip>` | from .env.obs | OBS host |
| `--obs-port <n>` | from .env.obs | OBS WebSocket port (default 4455) |
| `--obs-password <pw>` | from .env.obs | OBS WebSocket password |

### Config constants

```python
OVERLAY_PORT   = 8878     # HTTP overlay server port
AGENT_INTERVAL = 120      # seconds between Claude decisions
STEP_DELAY     = 3.5      # seconds between Street View arrow-key steps
CHAT_POLL_SECS = 6        # seconds between YouTube chat reads
MAX_STEPS      = 20       # step cap before forcing an agent decision
```

### State object

Live state is a plain dict, served at `/state` and via the socket `status` command:

```json
{
  "current":   "Shibuya Crossing, Tokyo, Japan",
  "narration": "Busiest pedestrian crossing on Earth",
  "queue":     ["Chernobyl", "Machu Picchu"],
  "visited":   ["Times Square, New York", "Shibuya Crossing, Tokyo, Japan"],
  "total":     2,
  "status":    "exploring",
  "step":      7,
  "mode":      "streetview",
  "socket":    "/Users/you/.unchained/stream.sock",
  "log":       "/Users/you/.unchained/stream.log"
}
```

`mode` is `"streetview"` while navigating Google Maps, `"website"` during web visits.

### Agent loop logic

Every tick (0.5s), the main loop checks in order:

1. **Poll YouTube chat** — scan for `!go <place>` commands, append to `state["queue"]`
2. **Drain `_pending_actions`** — execute immediate commands from `stream_ctl` (`skip`, `website`)
3. **Agent decision** — triggered when any of:
   - `AGENT_INTERVAL` seconds have elapsed
   - `state["step"] >= MAX_STEPS` (walked too far without a decision)
   - `state["queue"]` is non-empty (honor a `!go` or `go` command)
4. **Street View step** — send `ArrowUp` key every `STEP_DELAY` seconds; pan left/right every 7 steps

### Agent prompt & response

The agent (Claude) receives current state + last 6 visited places + recent chat, and responds with JSON:

```json
{
  "action":      "streetview" | "website" | "explore",
  "destination": "Place Name, Country",
  "url":         "https://...",
  "narration":   "Overlay text ≤90 chars, present tense",
  "reason":      "Internal reasoning (not shown to viewers)"
}
```

- `streetview` — search Google Maps, extract lat/lng, navigate to Street View URL
- `website` — navigate the Maps tab to a URL for 15s, then return to previous location
- `explore` — stay, keep stepping forward

The Claude CLI is called via subprocess with `CLAUDECODE` unset (to allow nested invocation from within a Claude Code session):

```python
env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
proc = await asyncio.create_subprocess_exec(
    "claude", "--print", "--output-format", "text", full_prompt, env=env
)
```

### OBS WebSocket (v5 protocol)

Credentials loaded from `.env.obs`:

```
OBS_HOST=192.168.1.121
OBS_PORT=4455
OBS_PASSWORD=yourpassword
```

Handshake: Hello (op 0) → auth SHA-256 challenge → Identify (op 1) → Identified (op 2).
Requests use op 6, responses op 7, matched by `requestId`.

On startup: `StartStream`. On Ctrl+C: `StopStream` then close.

### OBS overlay

Browser source at `http://127.0.0.1:8878/overlay` — add in OBS:
- Width: 860, Height: 110
- Transparent background
- Position at bottom of scene

The overlay polls `/state` every 2s and displays:
- **Top row**: `LIVE` badge · current location · visited count · next queue · brand
- **Bottom row**: `AI▸` + Claude's narration text

### Event bus

`_emit(type_, **kwargs)` appends to `_event_log` (capped at 100) and puts to all `_tail_queues`.

| Event type | Fields | Trigger |
|-----------|--------|---------|
| `thinking` | — | Agent about to call Claude |
| `agent_plan` | `action`, `dest`, `narration`, `reason` | Claude responded with a plan |
| `nav` | `place` | `go_to()` called |
| `arrived` | `place`, `total` | Street View loaded successfully |
| `website` | `url` | `visit_website()` called |
| `ctl` | `cmd`, `dest`/`text`/`url` | Command received from stream_ctl |
| `chat` | `dest` | `!go` detected in YouTube chat |
| `agent_paused` | — | `agent pause` command received |
| `agent_resumed` | — | `agent resume` command received |

### Log file

`~/.unchained/stream.log` — all stdout and stderr tee'd via `_Tee`, truncated fresh each run. View live:

```bash
tail -f ~/.unchained/stream.log
```

---

## stream_ctl.py

Auto-launched by `agent_stream.py` in a new Terminal window via `osascript`. Can also be run manually to reconnect.

### CLI flags

| Flag | Description |
|------|-------------|
| `--raw` | Skip Claude, accept raw commands only |

### Natural language mode (default)

Type anything. Claude reads the live stream state and responds conversationally. When it wants to take an action it embeds an `<action>` tag which `stream_ctl` parses and sends to the socket:

```
you > let's go somewhere cold and remote

claude > Great idea — heading to Svalbard, the northernmost permanent settlement
         on Earth. Ice, polar bears, and total darkness in winter.
  → {'cmd': 'go', 'dest': 'Longyearbyen, Svalbard, Norway'} — {'ok': True, 'queued': 'Longyearbyen, Svalbard, Norway'}
```

### Raw commands

These work in both modes and are parsed before Claude is called:

**Navigation**

| Command | Effect |
|---------|--------|
| `go <place>` | Queue destination, force immediate agent call |
| `say <text>` | Override overlay narration instantly (≤90 chars) |
| `website <url>` | Trigger a web visit (~15s then back to Maps) |
| `skip` | Force agent to pick next destination now |
| `status` | Print full stream state incl. agent + OBS status |
| `quit` / `q` | Exit ctl (stream keeps running) |

**Agent orchestrator**

| Command | Effect |
|---------|--------|
| `agent pause` | Freeze AI decisions — keeps Street View stepping but stops automatic destination changes |
| `agent resume` | Resume AI decisions |
| `agent status` | Return `{"paused": true/false}` |

Pausing is useful when something visually interesting is on screen and you don't want the agent to jump away on its next scheduled decision.

**OBS**

| Command | Effect |
|---------|--------|
| `obs start` | Call OBS `StartStream` |
| `obs stop` | Call OBS `StopStream` |
| `obs status` | Return `{"active": bool, "timecode": "HH:MM:SS.mmm"}` |

OBS commands only work when `--obs` was passed to `agent_stream.py` and the WebSocket connection succeeded. `status` shows `obs: connected` or `obs: not connected`.

### Slash commands

| Command | Effect |
|---------|--------|
| `/help` | Print all commands with examples |
| `/log` | `tail -f ~/.unchained/stream.log` — Ctrl+C to return |
| `/events` | Raw JSON event stream from socket tail — Ctrl+C to return |

### Live event feed

A background daemon thread connects to the socket `tail` endpoint and prints events as they arrive, interleaved with your prompt:

```
🤔 agent thinking…

🤖 agent decided: STREETVIEW → Longyearbyen, Svalbard, Norway
   narration : "88° North — the world's northernmost town"
   reason    : Viewer asked for cold and remote; Svalbard is extreme and visually striking

🗺  navigating → Longyearbyen, Svalbard, Norway
📍 arrived: Longyearbyen, Svalbard, Norway  (4 visited)

you >
```

If the connection drops (agent restarted), the thread reconnects automatically after 3s.

### Socket protocol

All messages are newline-delimited JSON over a Unix domain socket at `~/.unchained/stream.sock`.

**Request → Response (one-shot):**

```jsonc
// status
{"cmd": "status"} → { ...state, "socket": "...", "log": "..." }

// go
{"cmd": "go", "dest": "Tokyo Tower"} → {"ok": true, "queued": "Tokyo Tower"}

// say
{"cmd": "say", "text": "Walking through Akihabara"} → {"ok": true}

// website
{"cmd": "website", "url": "https://en.wikipedia.org/wiki/Tokyo"} → {"ok": true}

// skip
{"cmd": "skip"} → {"ok": true}
```

**Streaming (tail mode):**

```jsonc
// send once — server switches to push mode, streams events indefinitely
{"cmd": "tail"}
// → last 15 events replayed immediately, then new events as they happen
// → connection stays open until client disconnects
```

---

## Troubleshooting

### Agent not making decisions
- Check `~/.unchained/stream.log` or `/log` in stream_ctl
- `claude --version` — ensure Claude Code CLI is installed
- Check `AGENT_INTERVAL` — default is 120s; use `--interval 30` for testing

### Street View not loading
- Some places have no Street View coverage — the agent will fall back to `explore`
- Check `[maps] no coords` or `[maps] no street view` in the log

### OBS not connecting
- OBS → Tools → WebSocket Server Settings → confirm enabled
- Check `.env.obs` has correct `OBS_HOST`, `OBS_PORT`, `OBS_PASSWORD`
- OBS must be running on the host machine before `agent_stream.py` starts

### stream_ctl can't connect
- Socket path: `~/.unchained/stream.sock` — only exists while `agent_stream.py` is running
- Run `uv run stream_ctl.py` to reconnect after a restart
- `/log` will show recent agent output even if ctl was disconnected

### `claude --print` nested session error
- Handled automatically: `CLAUDECODE` is stripped from the subprocess env
- If you see this error in the log, check your shell environment for unusual `CLAUDECODE` values

### YouTube chat not detected
- Ensure a YouTube Studio or YouTube Live tab is open in Chrome
- Chat polling reads `!go <place>` commands from the page text
- Use `--chat-tab <id>` to specify the exact tab if auto-detect fails
