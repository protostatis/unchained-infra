# Known Issues Backlog

Standing operational issues that aren't blocking but are worth tracking so
they don't mask future failures. Items here are triaged — not urgent.

## `list_provisioned_tabs` and `cdp_provision_cleanup` have divergent views of slot state

**Status:** open, self-healing, related to `PROVISION_ORPHAN_BUG.md` in the
`marketing_unchainedsky` repo.

**Symptom:** When a provisioned Chrome loses its CDP connection, the MCP
server's internal tracking can end up in a state where:

- `list_provisioned_tabs` still reports the slot with all its tabs
  (e.g. `Slot 715f (profile: Profile 5, 3 tabs): ...`)
- `cdp_provision_cleanup` on the same session returns
  `"No provisioned Chrome instances to clean up."` — as if nothing exists
- A subsequent `list_provisioned_tabs` call shows no slots at all

Seen live on 2026-04-11 while diagnosing a separate qwen-proxy incident: two
orphan slots (`715f`, `381a`) on Profile 5 were listed by `list_provisioned_tabs`
but invisible to `cdp_provision_cleanup`. They disappeared on the next list
call without intervention.

**Why it matters:** The same class of desync described in
`PROVISION_ORPHAN_BUG.md` (Chrome OS process survives after CDP drop). In
today's incident it self-healed, but on other days it has left an orphan
Chrome window on the operator's machine. More importantly, it can mask real
provisioning failures — a caller that trusts `list_provisioned_tabs` sees
"slot exists" while `cdp_*` calls against that slot fail because the server
has already discarded its handle.

**Options considered (see `PROVISION_ORPHAN_BUG.md`):**

1. Track the OS PID at launch and kill by PID during cleanup.
2. Tag the user-data-dir with a unique marker so cleanup can `pkill` orphans
   by pattern.
3. Add a CDP keepalive and proactively kill Chrome when it drops.
4. Apply a provision TTL — auto-kill if no CDP command within N seconds of
   launch.

**Not urgent** because the current self-heal behavior keeps the pool from
growing unbounded. Re-prioritize if an orphan ever survives across runs.

## `LLM_IDLE_TIMEOUT=1800s` may be redundant for the qwen-proxy path

**Status:** open, cosmetic / config-simplification.

**Context:** Commit
[`6aa053f`](https://github.com/protostatis/marketing_unchainedsky/commit/6aa053f)
in `marketing_unchainedsky` raised the LLM idle timeout to 1800s specifically
to cover long qwen-proxy response times. At the time, qwen-proxy was a
one-shot JSON responder that wrote nothing to the socket until the full
subprocess finished (often 60–120s+), so any idle timeout on the gateway side
would trip before qwen-code could reply.

Commit
[`7525090`](https://github.com/protostatis/marketing_unchainedsky/commit/7525090)
fixed this at the root by adding SSE streaming with `: ping` keepalive
comments every 10s to `openclaw/qwen-proxy.mjs`. The socket is never idle
long enough for any reasonable idle timeout to fire, regardless of the
underlying subprocess runtime.

**Question:** Is the 1800s idle timeout still earning its keep, or can it
come back down to a sane default (e.g. the old 180s–300s)? A shorter timeout
would surface genuine LLM hangs faster, at the cost of removing the safety
net for any *other* provider that might also be non-streaming.

**Options:**

- **Leave it alone** — belt-and-braces, costs nothing day-to-day.
- **Back it out** — reduces config drift; easier to reason about what's
  actually protecting each code path. Acceptable if qwen-proxy is the only
  known non-streaming provider.

**Suggested action:** revisit on the next config-cleanup pass. Not worth a
dedicated change.
