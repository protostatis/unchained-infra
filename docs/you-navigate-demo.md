# "Unchained drives. You navigate." Demo

This document covers the local demo route for the slogan-driven X.com growth
controller prototype.

Primary route:

- `GET /labs/you-navigate`
- `POST /web/labs/you-navigate/run`

Compatibility aliases currently exist at:

- `GET /labs/x-manager`
- `POST /web/labs/x-manager/run`

The demo is read-only. It never posts, likes, follows, or sends anything on X.

## What It Demonstrates

- a tiny controller loop over the existing Unchained browser stack
- a read-only X.com reconnaissance workflow
- a toy reward critic over profile, mentions, topic, competitor, and finish modes
- high-level browser control through a named local Chrome profile

## Controller And Reward-Critic Split

The demo uses the simplest architecture that can run before any custom RL
training:

- `Base agent`: does the browser work
- `Controller`: picks the next research mode
- `Ephemeral critic`: scores how useful that move was

The reward critic is intentionally ephemeral. It is created for one evaluation,
sees the current state, action, and result, assigns a structured score, and
disappears. This is the practical "LLM as reward model" version of the idea,
without needing a trained reward model first.

For the X.com growth use case, the critic can score:

- `intent_alignment`: did this move get closer to what the user actually wants
- `information_gain`: did it reduce ambiguity usefully
- `growth_value`: is this likely to improve qualified engagement or account growth
- `brand_fit`: does it match the account voice and positioning
- `efficiency`: was this a good use of a turn
- `risk`: was it spammy, off-brand, or low-signal

Example critic output:

```json
{
  "intent_alignment": 0.82,
  "information_gain": 0.65,
  "growth_value": 0.71,
  "brand_fit": 0.90,
  "efficiency": 0.55,
  "risk": 0.08,
  "total_reward": 0.74,
  "reason": "Scanning active niche conversations was better than drafting a new post because the user appears to want relevant followers, not broad impressions."
}
```

Simple reward formula:

```text
reward =
0.30 * intent_alignment +
0.20 * information_gain +
0.25 * growth_value +
0.15 * brand_fit +
0.10 * efficiency -
0.25 * risk
```

Why this matters:

- no custom RL training is required for v0
- the current Unchained browser stack can run it now
- the scores create labels for later offline learning
- explicit user feedback and delayed outcomes can be layered on later

The intended logging shape is:

```text
state, action, result, critic_scores, user_response
```

That gives the team judged trajectories before training a bandit, reranker, or
fuller policy.

## Local Smoke Test

Start from this worktree:

```bash
cd /Users/zhiminzou/Projects/unchainedsky_com/unchained-infra/.claude/worktrees/x-manager-guest-profile
```

### 1. Start the local stack

If nothing else is using the default ports:

```bash
./dev.sh
```

If another checkout is already using `8080` or `8765`, use alternate ports:

```bash
RELAY_PORT=9765 WEB_PORT=9080 ./dev.sh
```

### 2. Log in with dev auth

If Google OAuth is not configured, local dev auth is available:

```bash
curl -sS -c /tmp/unchained-cookies.txt -b /tmp/unchained-cookies.txt \
  -X POST http://localhost:9080/auth/dev \
  -H 'Content-Type: application/json' \
  -d '{"email":"dev@localhost"}'
```

Adjust the port if you used `8080`.

### 3. Read the exact API key tied to the web session

The web app computes the expected `agent_id` from the logged-in user's stored API
key. The browser bridge must use that same key.

```bash
cd /Users/zhiminzou/Projects/unchainedsky_com/unchained-infra/.claude/worktrees/x-manager-guest-profile/unchained
uv run python - <<'PY'
from auth import Auth
user = Auth().find_user_by_email("dev@localhost")
print(user["api_key"])
PY
```

### 4. Start a named `guest` browser profile bridge

For the alternate-port example:

```bash
UNCHAINED_RELAY_URL=ws://127.0.0.1:9765/tunnel \
UNCHAINED_API_KEY=<paste_the_key_from_step_3> \
uv run python chrome_bridge.py start --no-headless \
  --profile guest --port 9223
```

For the default-port example, use `ws://127.0.0.1:8765/tunnel`.

### 5. Sign into X in that Chrome window

The demo uses the named profile directly, so the `guest` Chrome window should be
logged into the X account you want to inspect.

### 6. Verify the relay sees the profile agent

Alternate-port example:

```bash
curl -sS http://127.0.0.1:9765/api/agents \
  -H "Authorization: Bearer <paste_the_key_from_step_3>"
```

You should see a connected agent like:

```text
claude-<hash>-guest
```

### 7. Run the demo

Open:

```text
http://localhost:9080/labs/you-navigate
```

Then fill:

- `X handle`: the target account
- `Browser profile`: `guest`
- `Growth brief`: what you want the toy controller to infer
- `Peer or competitor handles`: optional
- `Max controller steps`: `3`

Click `Run Toy Loop`.

## Troubleshooting

### "No agent found" or "agent not connected"

This almost always means the web app and the browser bridge are using different
API keys.

The local web session derives `agent_id` from the approved user's stored key.
The relay derives the connected browser `agent_id` from `UNCHAINED_API_KEY`.
If those keys differ, the page will look for the wrong agent, even if Chrome is
open.

Fix:

1. Log in via `/auth/dev`.
2. Read the user's stored key from `Auth().find_user_by_email(...)`.
3. Restart the bridge with that exact key.
4. Confirm `/api/agents` shows `claude-<hash>-guest`.

### X shows a login gate

The `guest` Chrome profile is not signed into X, or you launched the wrong
profile name.

### The page loads but nothing happens

Check the correct relay port in `UNCHAINED_RELAY_URL`. If the web app was started
with `RELAY_PORT=9765`, the bridge must also point to `9765`.
