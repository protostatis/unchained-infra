# Show Enhancement Plan — Process Visibility + Live Research

**Goal:** Transform the stream from "watch a bot click around maps" into a live AI travel documentary — where viewers can see the agent think, learn about each location in real time, and understand why it chose the next destination.

---

## Three Layers

### Layer 1 — Process Visibility
Show the agent's internal state on screen at all times. Viewers should never wonder "what is it doing?"

### Layer 2 — Live Wikipedia Research
After arriving at each location, fetch a short summary from Wikipedia and narrate it via TTS + display in overlay. Makes each stop educational rather than just visual.

### Layer 3 — Research-Driven Navigation
Feed the Wikipedia summary into the LLM context so it can pick the *next* destination based on thematic connections discovered from research. Creates a narrative arc across cities.

---

## Why This Works for Streaming

| Problem Today | Fix |
|---|---|
| Viewers don't know what the agent is doing | Layer 1: live status row on overlay |
| Stops feel random, no story | Layer 2: narrated Wikipedia facts at each location |
| City sequence has no coherent theme | Layer 3: LLM chains destinations by discovered theme |
| Show feels scripted / predictable | Research-driven decisions are genuinely emergent |
| No hook to keep viewers watching | "Where will the AI go next, and why?" is the hook |

---

## Overlay Redesign

Current overlay: 2 rows (location + narration), ~100px tall.

Proposed overlay: 3 rows, ~145px tall:

```
┌────────────────────────────────────────────────────────────┐
│ [LIVE]  📍 Senso-ji Temple, Tokyo        12 visited  🤖   │  ← Row 1: unchanged
│ [AI▸]  "Tokyo's oldest temple, built in 628 AD — one of   │  ← Row 2: Wikipedia fact (replaces static narration)
│          the most visited sites in Asia"                   │
│ [⚙]   Searching top sights in Seoul…                      │  ← Row 3: NEW — thinking status
└────────────────────────────────────────────────────────────┘
```

**Row 3 behavior:**
- Visible only during transitions (searching, navigating, looking up Wikipedia)
- Fades out after 8 seconds of inactivity
- Font: monospace, slightly smaller, dimmer color (e.g. `#aaa`)

---

## New State Fields

Add to `state` dict in `agent_stream.py`:

```python
state["thinking"]  = ""   # Short status string shown in overlay row 3
state["research"]  = ""   # Wikipedia extract for current location (1-2 sentences)
```

---

## `thinking` Update Points

Set `state["thinking"]` at every key transition moment:

| Moment | Value |
|---|---|
| Before `get_city_attractions(city)` | `"Searching top sights in {city}…"` |
| Before `go_to_city(city)` | `"Navigating to {attraction_name}…"` |
| Before `go_to_next_spot()` | `"Moving to next spot in {city}…"` |
| After arrival, before Wikipedia fetch | `"Looking up {location_name} on Wikipedia…"` |
| Before `call_agent()` | `"AI choosing next destination…"` |
| After agent returns decision | `""` (clear — action complete) |

Implement as a helper to keep call sites clean:

```python
async def set_thinking(msg: str):
    state["thinking"] = msg
    # Optionally log to console too
    print(f"[think] {msg}")
```

---

## Wikipedia Research — Implementation

### API

No browser needed. Wikipedia's REST API returns JSON:

```
GET https://en.wikipedia.org/api/rest_v1/page/summary/{title}
```

Response shape:
```json
{
  "title": "Senso-ji",
  "extract": "Senso-ji is an ancient Buddhist temple located in Asakusa, Tokyo. It is Tokyo's oldest temple..."
}
```

Use only the `extract` field. Truncate to first 1-2 sentences (split on `. `, take first 2).

### Search title

Use the location name from `state["location"]`. Strip city suffix if present (e.g. `"Times Square, New York"` → `"Times Square"`). URL-encode spaces as `_`.

### Non-blocking fetch

Spawn as a background task immediately after arrival so it runs while the agent is still settling in Street View:

```python
asyncio.create_task(research_location(state["location"]))
```

### Implementation sketch

```python
import aiohttp

async def fetch_wikipedia_summary(name: str) -> str:
    title = name.split(",")[0].strip().replace(" ", "_")
    url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as r:
                if r.status == 200:
                    data = await r.json()
                    extract = data.get("extract", "")
                    # First 2 sentences only
                    sentences = extract.split(". ")
                    return ". ".join(sentences[:2]) + ("." if len(sentences) > 1 else "")
    except Exception:
        pass
    return ""

async def research_location(name: str):
    await set_thinking(f"Looking up {name} on Wikipedia…")
    summary = await fetch_wikipedia_summary(name)
    if summary:
        state["research"] = summary
        state["narration"] = summary          # show in overlay row 2
        asyncio.create_task(speak(summary))   # TTS narration
    state["thinking"] = ""                   # clear thinking status
```

### Fallback

If Wikipedia returns no result or times out, keep `state["research"]` from the previous location. Never block navigation on research.

---

## Research-Driven Navigation — LLM Context

### Pass research into agent context

In `call_agent()`, add `state["research"]` to the context block sent to the LLM:

```python
context = f"""
Current location: {state['location']}
Coordinates: {state['lat']}, {state['lng']}
Research: {state['research']}
Cities visited: {', '.join(state['visited'][-5:])}
"""
```

### Updated system prompt (addition)

Append to `AGENT_SYSTEM`:

```
When you have research about the current location, use it to find a thematic connection
to the next destination. Explain the connection in your narration. Examples:
- Buddhist temple in Tokyo → suggest Wat Phra Kaew in Bangkok (shared Buddhist heritage)
- Victorian architecture in Melbourne → suggest Cape Town or Edinburgh (shared colonial era)
- Street food market in Marrakech → suggest a night market in Taipei or Bangkok

Always state the connection explicitly so viewers understand why you chose the next location.
Format: "From [current] we travel to [next] — both [shared theme]."
```

---

## Implementation Steps

| # | Task | File | Notes |
|---|---|---|---|
| 1 | Add `thinking` and `research` to `state` dict | `agent_stream.py` | Initialize to `""` |
| 2 | Add `set_thinking(msg)` helper | `agent_stream.py` | Sets state, prints to console |
| 3 | Update `OVERLAY_HTML` — add row 3 | `agent_stream.py` | Show `thinking`, fade after 8s |
| 4 | Increase overlay height 100→145px | `agent_stream.py` | Update CSS + OBS browser source height |
| 5 | Add `/state` response for `thinking` field | `agent_stream.py` | Overlay JS polls this |
| 6 | Add `fetch_wikipedia_summary(name)` | `agent_stream.py` | aiohttp GET, 5s timeout |
| 7 | Add `research_location(name)` | `agent_stream.py` | Calls fetch, updates state, speaks |
| 8 | Call `set_thinking()` at each transition | `agent_stream.py` | See transition table above |
| 9 | Spawn `research_location` after arrival | `agent_stream.py` | `asyncio.create_task(...)` |
| 10 | Pass `state["research"]` into `call_agent` context | `agent_stream.py` | Add to context block |
| 11 | Update `AGENT_SYSTEM` with thematic prompt | `agent_stream.py` | See prompt addition above |
| 12 | Add `aiohttp` to dependencies | `pyproject.toml` | If not already present |

---

## What Viewers See (Example Flow)

1. **Row 3:** `"Searching top sights in Tokyo…"` — agent is picking attractions
2. **Row 3:** `"Navigating to Senso-ji Temple…"` — first attraction loading
3. **Row 1:** `"📍 Senso-ji Temple, Tokyo"` — Street View loads
4. **Row 3:** `"Looking up Senso-ji Temple on Wikipedia…"` — research starting
5. **Row 2:** `"Senso-ji is Tokyo's oldest temple, founded in 628 AD. It receives over 30 million visitors annually."` — Wikipedia fact narrated via TTS
6. **Row 3:** `"Moving to next spot in Tokyo…"` — agent moves to next attraction
7. *(repeat for 3-4 more spots in Tokyo)*
8. **Row 3:** `"AI choosing next destination…"` — LLM reasoning
9. **Row 2:** `"From Senso-ji we travel to Wat Phra Kaew in Bangkok — both are ancient Buddhist temple complexes that define their city's spiritual identity."` — thematic connection narrated

---

## Token Cost Estimate

| Component | Cost | Frequency |
|---|---|---|
| Wikipedia fetch | ~0 tokens (HTTP, no LLM) | Once per location |
| Extra context in `call_agent` | ~50 tokens | Once per city |
| Thinking status updates | 0 tokens | Pure state mutation |
| Overlay row 3 | 0 tokens | Client-side JS |

**Net LLM cost increase: ~50 tokens per city change.** Negligible.

---

## Dependencies

- `aiohttp` — for Wikipedia HTTP fetch (non-blocking). Add to `pyproject.toml` if not present.
- No new browser actions needed — Wikipedia REST API requires no CDP.
- No new OBS scenes — just resize the browser source height.

---

## Out of Scope (Future Ideas)

- Google search for locations Wikipedia misses
- Multiple language Wikipedia sources for non-English cities
- Saving research to a local log file for post-stream content
- Chat integration: viewers suggest themes, agent responds thematically
