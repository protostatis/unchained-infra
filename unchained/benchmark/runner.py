"""runner.py — Benchmark entry point.

Loads tasks from tasks.jsonl, runs agents sequentially, evaluates results,
writes per-task JSONL to results/, and prints a comparison summary table.

Tests the deployed HTTP split path:
    cdp_tool.py → cloud_tools → private_core_client → HTTP → private_core_engine
    → CDP via relay → Chrome

Usage:
    cd unchained-infra/unchained
    uv run python -m benchmark.runner --agents cli --subset hn_top5
    uv run python -m benchmark.runner --agents cli --difficulty hard
    uv run python -m benchmark.runner --agents orchestrator --subset hn_top5
    uv run python -m benchmark.runner --agents cli,orchestrator --site hackernews
    uv run python -m benchmark.runner --agents cli \
        --agent-id a-ff47f780 --relay-host api.unchainedsky.com --relay-port 443
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime

# Graceful import — benchmark works without private core overlay
try:
    from benchmark.progress_critic import (
        build_intervention_feedback,
        classify_progress_bucket,
        score_tool_log,
    )
    _HAS_PROGRESS_CRITIC = True
except RuntimeError:
    _HAS_PROGRESS_CRITIC = False

BENCHMARK_DIR = os.path.dirname(os.path.abspath(__file__))
TASKS_FILE = os.path.join(BENCHMARK_DIR, "tasks.jsonl")
RESULTS_DIR = os.path.join(BENCHMARK_DIR, "results")


def load_tasks(
    subset: list[str] | None = None,
    site: str | None = None,
    difficulty: str | None = None,
) -> list[dict]:
    """Load tasks from tasks.jsonl, optionally filtering."""
    tasks = []
    with open(TASKS_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            tasks.append(json.loads(line))

    if subset:
        tasks = [t for t in tasks if t["id"] in subset]
    if site:
        tasks = [t for t in tasks if t["site"] == site]
    if difficulty:
        tasks = [t for t in tasks if t.get("difficulty") == difficulty]

    return tasks


def save_result(result_entry: dict, run_id: str):
    """Append a result entry to the run's JSONL file."""
    os.makedirs(RESULTS_DIR, exist_ok=True)
    path = os.path.join(RESULTS_DIR, f"{run_id}.jsonl")
    with open(path, "a") as f:
        f.write(json.dumps(result_entry) + "\n")


async def run_agent_on_tasks(
    agent_name: str,
    tasks: list[dict],
    run_id: str,
    cli_model: str | None = None,
    orch_model: str | None = None,
    max_turns: int = 25,
    intermediate_goal_mode: str = "off",
    agent_id: str | None = None,
    relay_host: str | None = None,
    relay_port: int | None = None,
    private_core_url: str | None = None,
    private_core_token: str | None = None,
) -> list[dict]:
    """Run all tasks through one agent, returning result entries."""
    results = []

    for i, task in enumerate(tasks, 1):
        task_id = task["id"]
        print(f"\n  [{i}/{len(tasks)}] {task_id}: {task['task'][:60]}...")

        if agent_name == "cli":
            from benchmark.agent_cli import run_task
            task_result = await run_task(
                task,
                model=cli_model,
                max_turns=max_turns,
                intermediate_goal_mode=intermediate_goal_mode,
                agent_id=agent_id,
                relay_host=relay_host,
                relay_port=relay_port,
                private_core_url=private_core_url,
                private_core_token=private_core_token,
            )
        elif agent_name == "orchestrator":
            from benchmark.agent_orchestrator import run_task
            task_result = await run_task(
                task,
                model=orch_model,
                max_turns=max_turns,
                intermediate_goal_mode=intermediate_goal_mode,
                agent_id=agent_id,
            )
        else:
            print(f"  Unknown agent: {agent_name}")
            continue

        # Evaluate
        from benchmark.evaluator import evaluate
        passed, reason = evaluate(task, task_result.answer)

        # Progress scoring (graceful degradation)
        progress_dict = {}
        progress_bucket = "unknown"
        intervention_dict = {}
        should_intervene = False
        if _HAS_PROGRESS_CRITIC:
            progress = score_tool_log(
                task_result.tool_log,
                final_answer=task_result.answer,
                error_text=task_result.error,
                loops=task_result.loops,
            )
            intervention = build_intervention_feedback(
                progress=progress,
                loops=task_result.loops,
            )
            progress_bucket = classify_progress_bucket(
                passed=passed,
                loops=task_result.loops,
                progress=progress,
            )
            progress_dict = progress.to_dict()
            intervention_dict = intervention.to_dict()
            should_intervene = intervention.should_intervene

        status = "PASS" if passed else "FAIL"
        wall = f"{task_result.wall_seconds:.0f}s"
        tok_str = f" tok={task_result.total_tokens}" if task_result.total_tokens else ""

        if _HAS_PROGRESS_CRITIC and progress.available:
            print(
                f"  {status} ({wall}) tools={task_result.tool_calls} loops={task_result.loops}"
                f"{tok_str} progress={progress_bucket} score={progress.final_score:.1f}"
            )
        else:
            print(
                f"  {status} ({wall}) tools={task_result.tool_calls} loops={task_result.loops}"
                f"{tok_str}"
            )
        if should_intervene and _HAS_PROGRESS_CRITIC:
            top_reasons = ",".join(intervention.reason_codes[:3])
            print(f"  Intervention: {intervention.severity} ({top_reasons})")
        if task_result.error:
            print(f"  Error: {task_result.error[:100]}")
        if not passed:
            print(f"  Reason: {reason}")

        entry = {
            "task_id": task_id,
            "agent": agent_name,
            "model": orch_model if agent_name == "orchestrator" else (cli_model or "sonnet"),
            "passed": passed,
            "reason": reason,
            "wall_seconds": round(task_result.wall_seconds, 1),
            "tool_calls": task_result.tool_calls,
            "turns": task_result.turns,
            "loops": task_result.loops,
            "error": task_result.error,
            "prompt_tokens": task_result.prompt_tokens,
            "completion_tokens": task_result.completion_tokens,
            "total_tokens": task_result.total_tokens,
            "answer_preview": task_result.answer[:300] if task_result.answer else "",
            "tool_log": task_result.tool_log,
            "intermediate_goal_mode": getattr(task_result, "intermediate_goal_mode", intermediate_goal_mode),
            "intermediate_goal_enabled": getattr(task_result, "intermediate_goal_enabled", False),
            "hardness": getattr(task_result, "hardness", {}),
            "progress": progress_dict,
            "progress_bucket": progress_bucket,
            "intervention": intervention_dict,
            "timestamp": datetime.now().isoformat(),
        }
        results.append(entry)
        save_result(entry, run_id)

    return results


def print_summary(
    all_results: dict[str, list[dict]],
    tasks: list[dict],
):
    """Print a comparison summary table."""
    today = datetime.now().strftime("%Y-%m-%d")
    n_tasks = len(tasks)
    sites = sorted(set(t["site"] for t in tasks))

    print(f"\n{'=' * 60}")
    print(f"Unchained Agent Benchmark — {today}")
    print(f"{'=' * 60}")
    print(f"Tasks: {n_tasks} | Sites: {len(sites)} ({', '.join(sites)})")

    # Per-agent summary
    for agent_name, results in all_results.items():
        n_pass = sum(1 for r in results if r["passed"])
        avg_tools = sum(r["tool_calls"] for r in results) / max(len(results), 1)
        total_loops = sum(r["loops"] for r in results)
        n_degraded = sum(1 for r in results if r.get("progress_bucket") == "degraded_complete")
        model = results[0]["model"] if results else "unknown"
        mode = results[0].get("intermediate_goal_mode", "off") if results else "off"

        total_tokens = sum(r.get("total_tokens", 0) for r in results)
        avg_tokens = total_tokens / max(len(results), 1)
        tokens_per_pass = total_tokens / max(n_pass, 1) if total_tokens else 0

        print(f"\n{agent_name} ({model}) [intermediate_goals={mode}]")
        pct = n_pass / max(n_tasks, 1) * 100
        print(
            f"  Pass: {n_pass}/{n_tasks} ({pct:.0f}%)  Avg tools: {avg_tools:.1f}  "
            f"Loops: {total_loops}  Degraded: {n_degraded}"
        )
        if total_tokens:
            print(
                f"  Avg tokens: {avg_tokens:.0f}  Total tokens: {total_tokens}  "
                f"Tokens/pass: {tokens_per_pass:.0f}"
            )

    # Per-task comparison table
    agent_names = list(all_results.keys())
    if len(agent_names) >= 2:
        print(f"\n{'Task':<20}", end="")
        for name in agent_names:
            print(f"| {name:<20}", end="")
        print()
        print("-" * (20 + 22 * len(agent_names)))

        # Build lookup: task_id -> agent -> result
        lookup: dict[str, dict[str, dict]] = {}
        for agent_name, results in all_results.items():
            for r in results:
                lookup.setdefault(r["task_id"], {})[agent_name] = r

        for task in tasks:
            tid = task["id"]
            row = f"{tid:<20}"
            for name in agent_names:
                r = lookup.get(tid, {}).get(name)
                if r:
                    status = "PASS" if r["passed"] else "FAIL"
                    wall = f"{r['wall_seconds']:.0f}s"
                    tok = r.get("total_tokens", 0)
                    tok_s = f" tok={tok}" if tok else ""
                    row += f"| {status}  {wall}{tok_s:<14}"
                else:
                    row += f"| {'—':<19}"
            print(row)

    elif len(agent_names) == 1:
        # Single agent — simple list
        name = agent_names[0]
        print(f"\n{'Task':<20} | Result")
        print("-" * 60)
        for r in all_results[name]:
            status = "PASS" if r["passed"] else "FAIL"
            wall = f"{r['wall_seconds']:.0f}s"
            tools = r["tool_calls"]
            tok = r.get("total_tokens", 0)
            tok_s = f"  tok={tok}" if tok else ""
            print(f"{r['task_id']:<20} | {status}  {wall}  tools={tools}{tok_s}")

    print()


async def main():
    parser = argparse.ArgumentParser(
        description="Unchained Browser Agent Benchmark",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  uv run python -m benchmark.runner --agents cli --subset hn_top5
  uv run python -m benchmark.runner --agents cli --difficulty hard
  uv run python -m benchmark.runner --agents orchestrator --subset hn_top5
  uv run python -m benchmark.runner --agents cli,orchestrator --site hackernews
  uv run python -m benchmark.runner --agents cli \\
      --agent-id a-ff47f780 --relay-host api.unchainedsky.com --relay-port 443
""",
    )
    parser.add_argument(
        "--agents",
        default="cli",
        help="Comma-separated agent names: cli, orchestrator (default: cli)",
    )
    parser.add_argument(
        "--subset",
        default=None,
        help="Comma-separated task IDs to run (default: all)",
    )
    parser.add_argument(
        "--site",
        default=None,
        help="Run only tasks for this site (e.g. hackernews, wikipedia)",
    )
    parser.add_argument(
        "--difficulty",
        choices=["easy", "medium", "hard"],
        default=None,
        help="Run only tasks of this difficulty level",
    )
    parser.add_argument(
        "--cli-model",
        default=None,
        help="Claude CLI model name: opus, sonnet, haiku (default: sonnet)",
    )
    parser.add_argument(
        "--orch-model",
        default=None,
        help="Claude API model ID (default: claude-sonnet-4-6)",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=25,
        help="Maximum tool-use turns per task (default: 25)",
    )
    parser.add_argument(
        "--intermediate-goals",
        choices=["off", "on", "auto"],
        default="off",
        help="Intermediate-goal prompt mode: off (baseline), on (always), auto (hard tasks only).",
    )
    parser.add_argument(
        "--agent-id",
        default=None,
        help="CDP agent ID (overrides CDP_AGENT_ID env var)",
    )
    parser.add_argument(
        "--relay-host",
        default=None,
        help="Relay host (overrides CDP_RELAY_HOST env var)",
    )
    parser.add_argument(
        "--relay-port",
        type=int,
        default=None,
        help="Relay port (overrides CDP_RELAY_PORT env var)",
    )
    args = parser.parse_args()

    agent_names = [a.strip() for a in args.agents.split(",")]
    subset = [s.strip() for s in args.subset.split(",")] if args.subset else None

    tasks = load_tasks(subset=subset, site=args.site, difficulty=args.difficulty)
    if not tasks:
        print("No tasks matched filters.", file=sys.stderr)
        sys.exit(1)

    # Resolve agent_id from args or env
    agent_id = args.agent_id or os.environ.get("CDP_AGENT_ID")
    relay_host = args.relay_host or os.environ.get("CDP_RELAY_HOST")
    relay_port = args.relay_port
    if relay_port is None:
        rp = os.environ.get("CDP_RELAY_PORT")
        relay_port = int(rp) if rp else None
    private_core_url = os.environ.get("PRIVATE_CORE_URL")
    private_core_token = os.environ.get("PRIVATE_CORE_TOKEN")

    print(
        f"Benchmark: {len(tasks)} tasks, agents: {', '.join(agent_names)}, "
        f"intermediate_goals={args.intermediate_goals}"
    )
    if not _HAS_PROGRESS_CRITIC:
        print("  (progress_critic unavailable — running without progress scoring)")

    # Generate run ID
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    all_results: dict[str, list[dict]] = {}

    for agent_name in agent_names:
        print(f"\n{'=' * 60}")
        print(f"Agent: {agent_name}")
        print(f"{'=' * 60}")

        # Validate prerequisites
        if agent_name == "cli":
            import shutil
            if not shutil.which("claude"):
                print("ERROR: claude CLI not found. Install Claude Code first. Skipping.", file=sys.stderr)
                continue
        elif agent_name == "orchestrator":
            if not os.environ.get("ANTHROPIC_API_KEY"):
                print("ERROR: ANTHROPIC_API_KEY not set. Skipping.", file=sys.stderr)
                continue
            if not agent_id:
                print("ERROR: --agent-id or CDP_AGENT_ID required for orchestrator. Skipping.", file=sys.stderr)
                continue

        results = await run_agent_on_tasks(
            agent_name=agent_name,
            tasks=tasks,
            run_id=run_id,
            cli_model=args.cli_model,
            orch_model=args.orch_model,
            max_turns=args.max_turns,
            intermediate_goal_mode=args.intermediate_goals,
            agent_id=agent_id,
            relay_host=relay_host,
            relay_port=relay_port,
            private_core_url=private_core_url,
            private_core_token=private_core_token,
        )
        all_results[agent_name] = results

    if all_results:
        print_summary(all_results, tasks)
        print(f"Results saved to: benchmark/results/{run_id}.jsonl")
    else:
        print("No agents ran successfully.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
