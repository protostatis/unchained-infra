# unchained-pyreplab

Local toy V1 for the "browser to lab" loop:

1. Capture page evidence through Unchained MCP.
2. Materialize a local research capsule.
3. Open generated notebook-style cells in `pyreplab`.
4. Queue follow-up browser tasks from analysis.

This prototype does not host compute or data. It uses:

- your local/browser-side Unchained agent
- your Unchained MCP endpoint
- local files under `capsules/`
- local `pyreplab`

## What it can do

- initialize against `https://api.unchainedsky.com/mcp`
- auto-discover your connected `agent_id` from the API when possible
- capture one or more URLs into a local capsule with:
  - page text
  - DDM orientation output
  - page metadata
  - optional screenshots
- generate `source_index.json`, `schema_summary.json`, and `analysis_plan.json`
- generate `task_spec.json` and `capsule_state.json` so each capsule has an explicit task contract and workflow state
- generate `source_plan.json` so capture has an explicit seeded source plan before analysis starts
- generate `object_manifest.json` and `readiness.json` so the lab opens on explicit structured objects with a clear analysis gate
- generate `capture_brief.json` with recipe-aware source gaps and suggested search queries
- render `analysis.py` from a deterministic recipe plan
- run a local terminal-style lab web server backed by `pyreplab`
- queue and run follow-up revisits against the same capsule

## Prerequisites

- a running Unchained local agent
- a valid API key in `~/unchained-agent/.env` or `UNCHAINED_API_KEY`
- `uv`
- `pyreplab` available either:
  - in `PATH`
  - via `PYREPLAB_BIN`
  - or in the sibling repo `../pyrepl/pyreplab`

## Quick start

Install the project and create the local `.venv`:

```bash
uv sync
```

Bootstrap Research Desk with one provider-first command:

```bash
uv run unchained-pyreplab setup
```

That command now:

- starts with provider choice
- defaults to `Trial Agent`, which uses your Unchained key and included trial credit
- saves your chosen provider pack locally
- auto-detects the Unchained browser bridge from `~/unchained-agent/.env`
- auto-discovers `agent_id` when possible
- resolves `pyreplab`
- writes local defaults so later `doctor`, `serve`, and `capture` runs do not require manual provider env exports

It writes local config to:

```text
~/.config/unchained-pyreplab/config.json
```

Start an isolated browser bridge by default:

```bash
uv run unchained-pyreplab bridge-start
```

That path defaults to a separate Research Desk bridge:

- isolated profile: `research_desk`
- isolated CDP port: `9333`
- isolated data dir: `~/.unchained/research-desk-bridge`
- headless by default so it does not interfere with a shared headed bridge

If you want to watch the browser for a live smoke:

```bash
uv run unchained-pyreplab bridge-start --headed
```

Check local setup:

```bash
uv run unchained-pyreplab doctor
```

Remote handshake check:

```bash
uv run unchained-pyreplab doctor --ping
```

Create a capsule from a few URLs:

```bash
uv run unchained-pyreplab capture \
  --recipe highschool_district_compare \
  --task "Compare Chicago-area high-school districts" \
  --name chicago-highschools \
  --url "https://www.niche.com/k12/d/glenbrook-high-school-district-225-il/" \
  --url "https://www.niche.com/k12/d/new-trier-township-high-school-district-no-203-il/" \
  --url "https://www.niche.com/k12/d/hinsdale-township-high-school-district-no-86-il/"
```

Open the generated analysis in `pyreplab` and run cell 0:

```bash
uv run unchained-pyreplab lab capsules/chicago-highschools
```

Start the local interactive lab server:

```bash
uv run unchained-pyreplab serve --open --reload
```

Out of the box, the local server falls back to a small built-in lab agent so you can smoke-test the loop before wiring any external model. The capsule view is append-only and async, so asking a question no longer jumps the page back to the top. The web UI now uses `pyreplab` as the execution backend, shows kernel state in the sidebar, and exposes a `Wait` action for long-running cells. Good first prompts:

```text
Which districts are rankable today?
What is missing before a final ranking?
Can you show the report card gaps?
```

If you want the agent to synthesize richer code and prose, configure a user-owned lab agent.

Command mode:

```bash
export UNCHAINED_PYREPLAB_AGENT_CMD='your-command-that-reads-json-and-returns-json'
```

Optional separate summarizer:

```bash
export UNCHAINED_PYREPLAB_SUMMARY_CMD='your-command-that-reads-json-and-returns-json'
```

Codex CLI mode:

```bash
codex login
export UNCHAINED_PYREPLAB_AGENT_CMD='uv run unchained-pyreplab-codex-agent generation'
export UNCHAINED_PYREPLAB_SUMMARY_CMD='uv run unchained-pyreplab-codex-agent summary'
export UNCHAINED_PYREPLAB_MISSION_CMD='uv run unchained-pyreplab-codex-agent mission'
```

Optional Codex settings:

```bash
export UNCHAINED_PYREPLAB_CODEX_MODEL='gpt-5.3-codex'
export UNCHAINED_PYREPLAB_CODEX_GENERATION_MODEL='gpt-5.3-codex'
export UNCHAINED_PYREPLAB_CODEX_SUMMARY_MODEL='gpt-5.3-codex'
export UNCHAINED_PYREPLAB_CODEX_MISSION_MODEL='gpt-5.4'
export UNCHAINED_PYREPLAB_CODEX_PROFILE='default'
export UNCHAINED_PYREPLAB_CODEX_SANDBOX='read-only'
export UNCHAINED_PYREPLAB_AGENT_TIMEOUT_SECONDS='45'
export UNCHAINED_PYREPLAB_SUMMARY_TIMEOUT_SECONDS='12'
export UNCHAINED_PYREPLAB_MISSION_TIMEOUT_SECONDS='18'
```

Then restart the local lab server:

```bash
uv run unchained-pyreplab serve --open --reload
```

Repo-local launcher:

```bash
./scripts/serve_with_codex.sh --check
./scripts/serve_with_codex.sh
```

The adapter reads the same JSON prompt object that the built-in command mode uses, calls `codex exec` with a JSON schema, and returns a JSON object back to the lab. Generation returns `title` / `intent` / `code`, summary returns `markdown`, and mission planning returns the inferred object model for the Mission page.

The default launcher uses `gpt-5.4` for Mission planning, and `gpt-5.3-codex` for generation, summary, and Gather QA. If the external summary agent times out or fails, the lab falls back to the local formatter instead of leaving the request hanging.

The generation command should return JSON like:

```json
{
  "title": "Academic Strength Check",
  "intent": "Compare current academic indicators",
  "code": "print(ranked_districts_df.sort_values(\"academic_performance_score\", ascending=False)[[\"entity_name\", \"academic_performance_score\", \"ranking_status\"]].to_dict(orient=\"records\"))"
}
```

The summary command should return JSON like:

```json
{
  "markdown": "## Result\n\nNew Trier currently has the strongest exploratory academic signal, but it is not final-rankable yet."
}
```

Inspect the source-selection brief for the capsule:

```bash
uv run unchained-pyreplab brief capsules/chicago-highschools
```

Show the per-entity query detail only when you want it:

```bash
uv run unchained-pyreplab brief capsules/chicago-highschools --verbose
```

Process any follow-ups queued from analysis:

```bash
uv run unchained-pyreplab followups capsules/chicago-highschools
```

## Generated capsule layout

```text
capsules/chicago-hotels/
├── capture_brief.json
├── capsule_state.json
├── manual_cells.py
├── analysis_plan.json
├── analysis.py
├── manifest.json
├── object_manifest.json
├── readiness.json
├── task_spec.json
├── schema_summary.json
├── source_plan.json
├── source_index.json
├── pages/
│   ├── page-001/
│   │   ├── ddm.txt
│   │   ├── metadata.json
│   │   ├── navigate.txt
│   │   ├── page_text.txt
│   │   └── screenshot.png
├── followups/
│   ├── pending_followups.jsonl
│   └── results.jsonl
├── lab/
│   └── turns.jsonl
└── tables/
    ├── capture_targets.csv
    ├── entities.csv
    ├── pages.csv
    ├── pages.jsonl
    ├── source_index.csv
    └── source_index.jsonl
```

## Analysis loop

Two local interaction modes now exist:

1. `pyreplab` mode
2. local lab web mode

The web mode is terminal-like:

- `ask` turns where the agent writes code, `pyreplab` runs it, and the agent formats the reply
- markdown turns for user/operator notes
- code turns that run through a persistent `pyreplab` session
- output turns persisted under `lab/turns.jsonl`
- a `Wait` action for commands still running in `pyreplab`
- `builtin` agent mode for smoke testing without external model credentials
- async submit plus auto-scroll, so the active composer stays at the bottom like a CLI transcript
- dataframe-first notebook objects: `source_df`, `entity_df`, `district_metrics_df`, `ranked_districts_df`
- raw record lists remain available as `source_records`, `entity_records`, `district_metric_records`, `ranked_district_records`

Useful commands:

```bash
uv run unchained-pyreplab serve --open --reload
uv run unchained-pyreplab cells capsules/chicago-highschools
uv run unchained-pyreplab brief capsules/chicago-highschools
```

Inside generated `analysis.py`, use:

```python
from unchained_pyreplab.capsule_runtime import load_capsule

capsule = load_capsule("capsules/chicago-hotels")
pages = capsule.table("pages")
capsule.request_followup(
    url=pages.iloc[0]["final_url"],
    instruction="Revisit and capture the exact cancellation policy text.",
)
```

Then run:

```bash
uv run unchained-pyreplab followups capsules/chicago-hotels
```

## Notes

- The default MCP endpoint is `https://api.unchainedsky.com/mcp`.
- If you specifically want `https://unchainedsky.com/mcp`, set `UNCHAINED_MCP_ENDPOINT`.
- The capture workflow is intentionally simple: navigate, extract page text, capture DDM, optionally capture screenshot.
- Use `--recipe highschool_district_compare` for the school-district demo.
- `capture_brief.json` is the recipe-aware source-selection layer for the browser side. It tells you which source types are still missing and gives suggested search queries.
- The local web server does not host compute or data. It holds a local terminal-style notebook UI over the capsule plus a user-owned agent hook.
- If no agent env vars are set, the web lab uses a built-in fallback so the `ask` loop can be smoke-tested locally.
- `lab --run-cell N` executes cells `0..N` so later cells have the setup they need.
- The generated cells reload capsule context on each run so the local `pyreplab` flow works from a cold start.
- This is a toy V1 for evaluating the loop, not a general-purpose browser automation layer.
- `uv sync` creates `.venv` in this repo, which `pyreplab` can auto-detect from the project root.
