# Unchained — Browser Automation Core

> *Chains fall from my wrists*
> *Wind rushes where walls once stood*
> *I am sky, unchained*

CDP transport, DOM Density Map, and Bayesian page intelligence tools.

## Tools

| File | Purpose |
|------|---------|
| `cdp.py` | Chrome DevTools Protocol class, Chrome lifecycle, tab management |
| `ddm.py` | DOM Density Map — page layout + element navigation (~500 tokens) |
| `intel.py` | Bayesian extraction strategy selector (8 strategies) |
| `webmcp.py` | WebMCP tool discovery + invocation |
| `canvas_density.py` | Canvas/chart data reading |
| `dom_stream.py` | Real-time DOM mutation tracking |

## Quick Start

```bash
cd unchained/
uv run ddm.py --llm-2pass              # Map current page
uv run ddm.py --llm-2pass <url>        # Navigate + map
uv run ddm.py --at 694,584             # Reverse lookup at coords
uv run ddm.py --text                   # Extract page text
uv run ddm.py --api                    # Find app APIs
uv run intel.py --probe                # Classify page
uv run intel.py --extract              # Auto-extract data
```

## Scheduled Prompts (Local)

Define recurring prompts in a local jobs file and trigger them via your own
agent connection:

```bash
cd unchained/
cp scheduled_jobs.example.json scheduled_jobs.json

# See computed next run times (UTC)
python3 scheduled_tasks.py --jobs scheduled_jobs.json --state scheduled_jobs.state.json list

# Execute any jobs currently due (uses UNCHAINED_API_KEY and UNCHAINED_API_URL)
python3 scheduled_tasks.py --jobs scheduled_jobs.json --state scheduled_jobs.state.json run-due

# Run as a local polling daemon
python3 scheduled_tasks.py --jobs scheduled_jobs.json --state scheduled_jobs.state.json daemon --poll-seconds 30
```

You can also use the web scheduler planner at `/scheduler` to edit/save jobs,
preview next run times, and download `scheduled_jobs.json`.

Schedule types supported:
- `{"every_seconds": 3600}`
- `{"every_minutes": 30}`
- `{"daily_at": "09:15"}` (UTC)
- `{"at": "2026-03-01T18:00:00Z"}` (one-time)

## User Onboarding

New users get a personal browser agent in 3 steps:

### 1. Sign in

Go to `https://api.unchainedsky.com/chat` and sign in with Google.

### 2. Download and run the agent

The chat UI shows an "agent offline" banner with a **Download Agent** link. Click it to get `unchained-agent.zip` — pre-configured with your API key.

```bash
unzip unchained-agent.zip
cd unchained-agent
./start.sh
```

`start.sh` will:
- Create a Python virtual environment and install dependencies
- Start `chrome_bridge.py` (tunnels your local Chrome to the relay)
- Start `chat_agent_cli.py` (connects to the server, waits for messages)

### What's in the download

```
unchained-agent/
├── .env                          # Your API key + server URLs (pre-filled)
├── start.sh                      # Run this — sets up everything and starts the agent
├── requirements.txt              # Python dependencies
└── unchained/
    ├── chat_agent_cli.py         # Chat agent — uses Claude CLI, no API key needed
    ├── chrome_bridge.py          # Tunnels your local Chrome to the relay server
    ├── scheduled_tasks.py        # Local scheduler for pre-scheduled prompts
    ├── cdp_tool.py               # Thin HTTP client — calls server API for browser tools
    └── auth.py                   # API key validation (used by chrome_bridge)
```

**You only need to run `./start.sh`** — it starts both `chrome_bridge.py` and `chat_agent_cli.py` automatically.

The browser intelligence tools (DOM Density Map, page intelligence, CDP engine) run on the server. When Claude calls `cdp_tool.py ddm --llm-2pass`, it makes an HTTP request to the server which runs the tools through the relay tunnel to your Chrome. This means tools get updated server-side without needing a new download.

### 3. Chat

The UI status turns green ("agent online") within 10 seconds. Type a message — it routes to your agent, which drives your local Chrome and streams results back.

### Prerequisites

- **Python 3.13+** on the machine running the agent
- **Chrome** running with remote debugging:
  ```bash
  # macOS
  /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222
  ```
- **Claude CLI** installed (`claude` command available in PATH)

### Architecture

```
Phone (chat UI) → EC2 server (SSE bridge)
                     ↕ WebSocket per agent
              Your Mac: chat_agent_cli.py → claude -p → Bash → cdp_tool.py
                                                                    ↓ HTTP
                  EC2 server: /web/cmd → DDM / intel / CDP engine (proprietary)
                     ↕ WebSocket tunnel
              Your Mac: chrome_bridge.py → local Chrome CDP
```

Each user gets isolated routing — messages go only to their agent, identified by a hash of their API key.

## Dependencies

- Python >= 3.13
- httpx >= 0.28.1
- websockets >= 16.0
