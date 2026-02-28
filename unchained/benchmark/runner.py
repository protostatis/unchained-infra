"""Benchmark entry point with per-task tab isolation and optional parallelism.

Standard benchmark procedure:
1. Overlay private core into ``unchained-infra/unchained`` so ``private_core_client``
   uses the real inprocess implementations.
2. Start a local relay on ``127.0.0.1:8765`` with a shared ``PRIVATE_CORE_TOKEN``.
3. Create a local bench API key in ``auth.db``.
4. Start ``chrome_bridge.py`` against the local relay with that key.
5. Run ``benchmark.runner`` with ``CDP_AGENT_ID``, ``CDP_RELAY_HOST=127.0.0.1``,
   and ``CDP_RELAY_PORT=8765``. Leave ``PRIVATE_CORE_URL`` unset so benchmark tool
   calls stay inprocess instead of going to a remote private-core HTTP service.

This keeps benchmark traffic off production. Remote relay/private-core targets are
blocked by default; use ``--allow-remote`` only for deliberate exceptions.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import urllib.parse
from datetime import datetime

from benchmark.common import BenchmarkBrowser, BenchmarkConfig
from benchmark.evaluator import evaluate

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
    """Load tasks from tasks.jsonl with optional filters."""
    tasks: list[dict] = []
    with open(TASKS_FILE) as handle:
        for line in handle:
            line = line.strip()
            if line:
                tasks.append(json.loads(line))

    if subset:
        tasks = [task for task in tasks if task["id"] in subset]
    if site:
        tasks = [task for task in tasks if task["site"] == site]
    if difficulty:
        tasks = [task for task in tasks if task.get("difficulty") == difficulty]
    return tasks


def save_result(result_entry: dict, run_id: str):
    """Append one JSONL result entry."""
    os.makedirs(RESULTS_DIR, exist_ok=True)
    path = os.path.join(RESULTS_DIR, f"{run_id}.jsonl")
    with open(path, "a") as handle:
        handle.write(json.dumps(result_entry, ensure_ascii=False) + "\n")


def _normalize_agent_name(agent_name: str) -> str:
    normalized = (agent_name or "").strip().lower()
    if normalized == "autorouter":
        return "openrouter"
    return normalized


def _agent_label(agent_name: str) -> str:
    normalized = _normalize_agent_name(agent_name)
    if agent_name.strip().lower() == "autorouter":
        return "autorouter"
    return normalized


def _resolve_model_name(
    agent_name: str,
    *,
    cli_model: str | None,
    orch_model: str | None,
    or_model: str | None,
) -> str:
    normalized = _normalize_agent_name(agent_name)
    if normalized == "orchestrator":
        return orch_model or "claude-sonnet-4-6"
    if normalized == "openrouter":
        if or_model:
            return or_model
        env_model = os.environ.get("OPENROUTER_MODEL", "").strip()
        if env_model:
            return env_model
        from chat_agent_openrouter import DEFAULT_MODEL

        return DEFAULT_MODEL
    return cli_model or "sonnet"


async def _run_backend(
    agent_name: str,
    task: dict,
    config: BenchmarkConfig,
    *,
    cli_model: str | None,
    orch_model: str | None,
    or_model: str | None,
    max_turns: int,
    intermediate_goal_mode: str,
):
    normalized = _normalize_agent_name(agent_name)
    if normalized == "cli":
        from benchmark.agent_cli import run_task

        return await run_task(
            task,
            config=config,
            model=cli_model,
            max_turns=max_turns,
            intermediate_goal_mode=intermediate_goal_mode,
        )
    if normalized == "orchestrator":
        from benchmark.agent_orchestrator import run_task

        return await run_task(
            task,
            config=config,
            model=orch_model,
            max_turns=max_turns,
            intermediate_goal_mode=intermediate_goal_mode,
        )
    if normalized == "openrouter":
        from benchmark.agent_openrouter import run_task

        return await run_task(
            task,
            config=config,
            model=or_model,
            max_turns=max_turns,
            intermediate_goal_mode=intermediate_goal_mode,
        )
    raise ValueError(f"Unknown agent: {agent_name}")


def _build_result_entry(
    task: dict,
    task_result,
    *,
    task_id: str,
    agent_name: str,
    model_name: str,
    tab_id: str,
) -> dict:
    passed, reason = evaluate(task, task_result.answer)

    progress_dict = {}
    progress_bucket = "unknown"
    intervention_dict = {}
    should_intervene = False
    progress = None
    intervention = None

    if _HAS_PROGRESS_CRITIC:
        progress = score_tool_log(
            task_result.tool_log,
            final_answer=task_result.answer,
            error_text=task_result.error,
            loops=task_result.loops,
        )
        intervention = build_intervention_feedback(progress=progress, loops=task_result.loops)
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
    progress_suffix = ""
    if _HAS_PROGRESS_CRITIC and progress and progress.available:
        progress_suffix = f" progress={progress_bucket} score={progress.final_score:.1f}"
    print(
        f"  {status} ({wall}) tools={task_result.tool_calls} loops={task_result.loops}"
        f"{tok_str}{progress_suffix}"
    )
    if should_intervene and intervention:
        top_reasons = ",".join(intervention.reason_codes[:3])
        print(f"  Intervention: {intervention.severity} ({top_reasons})")
    if task_result.error:
        print(f"  Error: {task_result.error[:160]}")
    if not passed:
        print(f"  Reason: {reason}")

    return {
        "task_id": task_id,
        "agent": agent_name,
        "model": model_name,
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
        "intermediate_goal_mode": getattr(task_result, "intermediate_goal_mode", "off"),
        "intermediate_goal_enabled": getattr(task_result, "intermediate_goal_enabled", False),
        "hardness": getattr(task_result, "hardness", {}),
        "progress": progress_dict,
        "progress_bucket": progress_bucket,
        "intervention": intervention_dict,
        "tab_id": tab_id,
        "timestamp": datetime.now().isoformat(),
    }


async def run_agent_on_tasks(
    agent_name: str,
    tasks: list[dict],
    *,
    run_id: str,
    base_config: BenchmarkConfig,
    cli_model: str | None = None,
    orch_model: str | None = None,
    or_model: str | None = None,
    max_turns: int = 25,
    intermediate_goal_mode: str = "off",
    parallel_tasks: int = 1,
    keep_tabs: bool = False,
) -> list[dict]:
    """Run all tasks for one agent with optional task-level parallelism."""
    parallel_tasks = max(1, min(parallel_tasks, len(tasks)))
    controller = BenchmarkBrowser(base_config)
    results_by_index: list[dict | None] = [None] * len(tasks)
    save_lock = asyncio.Lock()
    sem = asyncio.Semaphore(parallel_tasks)

    async def _run_one(index: int, task: dict):
        task_id = task["id"]
        print(f"\n  [{index + 1}/{len(tasks)}] {task_id}: {task['task'][:60]}...")

        tab_id = "auto"
        created_tab = False
        model_name = _resolve_model_name(
            agent_name,
            cli_model=cli_model,
            orch_model=orch_model,
            or_model=or_model,
        )

        try:
            if base_config.agent_id:
                tab_id = await controller.create_tab("about:blank")
                created_tab = True
            elif parallel_tasks > 1:
                raise RuntimeError("parallel task execution requires --agent-id or CDP_AGENT_ID")

            task_result = await _run_backend(
                agent_name,
                task,
                base_config.with_tab(tab_id),
                cli_model=cli_model,
                orch_model=orch_model,
                or_model=or_model,
                max_turns=max_turns,
                intermediate_goal_mode=intermediate_goal_mode,
            )
        except Exception as exc:
            from benchmark.common import TaskResult

            task_result = TaskResult(error=f"Runner error: {exc}")
        finally:
            if created_tab and not keep_tabs:
                try:
                    await controller.close_tab(tab_id)
                except Exception as exc:
                    print(f"  Warning: failed to close tab {tab_id[:12]}: {exc}")

        entry = _build_result_entry(
            task,
            task_result,
            task_id=task_id,
            agent_name=_agent_label(agent_name),
            model_name=model_name or "unknown",
            tab_id=tab_id,
        )
        async with save_lock:
            save_result(entry, run_id)
        return index, entry

    try:
        if parallel_tasks == 1:
            for index, task in enumerate(tasks):
                _, entry = await _run_one(index, task)
                results_by_index[index] = entry
        else:
            async def _bounded(index: int, task: dict):
                async with sem:
                    return await _run_one(index, task)

            pending = [
                asyncio.create_task(_bounded(index, task))
                for index, task in enumerate(tasks)
            ]
            for finished in asyncio.as_completed(pending):
                index, entry = await finished
                results_by_index[index] = entry
    finally:
        await controller.close()

    return [entry for entry in results_by_index if entry is not None]


def print_summary(all_results: dict[str, list[dict]], tasks: list[dict]):
    """Print a summary table."""
    today = datetime.now().strftime("%Y-%m-%d")
    n_tasks = len(tasks)
    sites = sorted(set(task["site"] for task in tasks))

    print(f"\n{'=' * 60}")
    print(f"Unchained Agent Benchmark — {today}")
    print(f"{'=' * 60}")
    print(f"Tasks: {n_tasks} | Sites: {len(sites)} ({', '.join(sites)})")

    for agent_name, results in all_results.items():
        n_pass = sum(1 for result in results if result["passed"])
        avg_tools = sum(result["tool_calls"] for result in results) / max(len(results), 1)
        total_loops = sum(result["loops"] for result in results)
        n_degraded = sum(1 for result in results if result.get("progress_bucket") == "degraded_complete")
        model = results[0]["model"] if results else "unknown"
        mode = results[0].get("intermediate_goal_mode", "off") if results else "off"
        total_tokens = sum(result.get("total_tokens", 0) for result in results)
        avg_tokens = total_tokens / max(len(results), 1)
        tokens_per_pass = total_tokens / max(n_pass, 1) if total_tokens else 0

        print(f"\n{agent_name} ({model}) [intermediate_goals={mode}]")
        print(
            f"  Pass: {n_pass}/{n_tasks} ({(n_pass / max(n_tasks, 1)) * 100:.0f}%)  "
            f"Avg tools: {avg_tools:.1f}  Loops: {total_loops}  Degraded: {n_degraded}"
        )
        if total_tokens:
            print(
                f"  Avg tokens: {avg_tokens:.0f}  Total tokens: {total_tokens}  "
                f"Tokens/pass: {tokens_per_pass:.0f}"
            )

    agent_names = list(all_results.keys())
    if len(agent_names) >= 2:
        print(f"\n{'Task':<20}", end="")
        for name in agent_names:
            print(f"| {name:<20}", end="")
        print()
        print("-" * (20 + 22 * len(agent_names)))
        lookup: dict[str, dict[str, dict]] = {}
        for agent_name, results in all_results.items():
            for result in results:
                lookup.setdefault(result["task_id"], {})[agent_name] = result
        for task in tasks:
            row = f"{task['id']:<20}"
            for name in agent_names:
                result = lookup.get(task["id"], {}).get(name)
                if result:
                    status = "PASS" if result["passed"] else "FAIL"
                    wall = f"{result['wall_seconds']:.0f}s"
                    tok = result.get("total_tokens", 0)
                    tok_str = f" tok={tok}" if tok else ""
                    row += f"| {status}  {wall}{tok_str:<14}"
                else:
                    row += f"| {'—':<19}"
            print(row)
    elif len(agent_names) == 1:
        agent_name = agent_names[0]
        print(f"\n{'Task':<20} | Result")
        print("-" * 60)
        for result in all_results[agent_name]:
            status = "PASS" if result["passed"] else "FAIL"
            wall = f"{result['wall_seconds']:.0f}s"
            tok = result.get("total_tokens", 0)
            tok_str = f"  tok={tok}" if tok else ""
            print(f"{result['task_id']:<20} | {status}  {wall}  tools={result['tool_calls']}{tok_str}")

    print()


def _validate_agent_prereqs(agent_name: str, base_config: BenchmarkConfig) -> bool:
    normalized = _normalize_agent_name(agent_name)
    if normalized == "cli":
        if not shutil.which("claude"):
            print("ERROR: claude CLI not found. Skipping.", file=sys.stderr)
            return False
        return True
    if normalized == "orchestrator":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print("ERROR: ANTHROPIC_API_KEY not set. Skipping.", file=sys.stderr)
            return False
        if not base_config.agent_id:
            print("ERROR: orchestrator benchmark requires --agent-id or CDP_AGENT_ID. Skipping.", file=sys.stderr)
            return False
        return True
    if normalized == "openrouter":
        if not os.environ.get("OPENROUTER_API_KEY"):
            print("ERROR: OPENROUTER_API_KEY not set. Skipping.", file=sys.stderr)
            return False
        if not base_config.agent_id:
            print("ERROR: openrouter benchmark requires --agent-id or CDP_AGENT_ID. Skipping.", file=sys.stderr)
            return False
        return True
    print(f"ERROR: unknown agent {agent_name}. Skipping.", file=sys.stderr)
    return False


def _is_local_host(host: str | None) -> bool:
    normalized = (host or "").strip().lower()
    if not normalized:
        return True
    if normalized in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}:
        return True
    if normalized.endswith(".localhost"):
        return True
    if normalized in {"relay", "private-core", "host.docker.internal"}:
        return True
    return False


def _is_local_url(url: str | None) -> bool:
    if not url:
        return True
    parsed = urllib.parse.urlparse(url)
    return _is_local_host(parsed.hostname)


def _enforce_safe_targets(base_config: BenchmarkConfig, *, allow_remote: bool):
    if allow_remote:
        return

    if not _is_local_host(base_config.relay_host):
        print(
            "ERROR: benchmark runner refuses remote relay targets by default. "
            f"relay_host={base_config.relay_host!r}. "
            "Start the local stack or pass --allow-remote to override.",
            file=sys.stderr,
        )
        sys.exit(1)

    if not _is_local_url(base_config.private_core_url):
        print(
            "ERROR: benchmark runner refuses remote private-core targets by default. "
            f"PRIVATE_CORE_URL={base_config.private_core_url!r}. "
            "Use a local private-core URL or pass --allow-remote to override.",
            file=sys.stderr,
        )
        sys.exit(1)


async def _preflight_agent_check(config: BenchmarkConfig) -> str | None:
    """Quick reachability probe for the configured browser agent."""
    if not config.agent_id:
        return None

    browser = BenchmarkBrowser(config)
    try:
        result = await asyncio.wait_for(
            browser.execute_tool("ddm", {"flags": "--tabs"}, default_tab_id="auto"),
            timeout=10,
        )
    except asyncio.TimeoutError:
        return (
            f"Agent {config.agent_id} did not respond to preflight within 10s. "
            "Is chrome_bridge.py connected to the local relay?"
        )
    except Exception as exc:
        return f"Agent {config.agent_id} preflight failed: {exc}"
    finally:
        await browser.close()

    output = (result or "").strip()
    if not output:
        return f"Agent {config.agent_id} preflight returned no output."
    if output.lower().startswith("tool error"):
        return f"Agent {config.agent_id} unreachable: {output[:200]}"
    return None


async def main():
    parser = argparse.ArgumentParser(
        description="Unchained browser agent benchmark",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Standard local benchmark flow:
  #   PRIVATE_CORE_TOKEN=benchtoken123 uv run python relay.py --port 8765
  #   uv run python auth.py create bench-user
  #   uv run python chrome_bridge.py start --relay ws://127.0.0.1:8765/tunnel --key <local_api_key>
  #   export PRIVATE_CORE_TOKEN=benchtoken123 CDP_AGENT_ID=<local_agent_id>
  #   export CDP_RELAY_HOST=127.0.0.1 CDP_RELAY_PORT=8765
  #   uv run python -m benchmark.runner --agents autorouter --difficulty hard
  uv run python -m benchmark.runner --agents cli --subset hn_top5
  uv run python -m benchmark.runner --agents autorouter --difficulty hard --parallel-tasks 13
  uv run python -m benchmark.runner --agents cli,orchestrator --site hackernews
  uv run python -m benchmark.runner --agents openrouter --or-model meta-llama/llama-3.3-70b-instruct:free
  uv run python -m benchmark.runner --agents autorouter --difficulty hard --parallel-tasks 13 --allow-remote
""",
    )
    parser.add_argument(
        "--agents",
        default="cli",
        help="Comma-separated agents: cli, orchestrator, openrouter, autorouter",
    )
    parser.add_argument("--subset", default=None, help="Comma-separated task ids")
    parser.add_argument("--site", default=None, help="Filter by site")
    parser.add_argument(
        "--difficulty",
        choices=["easy", "medium", "hard"],
        default=None,
        help="Filter by difficulty",
    )
    parser.add_argument("--cli-model", default=None, help="Claude CLI model (default: sonnet)")
    parser.add_argument("--orch-model", default=None, help="Anthropic API model id")
    parser.add_argument("--or-model", default=None, help="OpenRouter model id")
    parser.add_argument("--max-turns", type=int, default=25, help="Maximum turns per task")
    parser.add_argument(
        "--intermediate-goals",
        choices=["off", "on", "auto"],
        default="off",
        help="Intermediate-goal prompt mode",
    )
    parser.add_argument("--agent-id", default=None, help="CDP agent id")
    parser.add_argument("--relay-host", default=None, help="Relay host")
    parser.add_argument("--relay-port", type=int, default=None, help="Relay port")
    parser.add_argument(
        "--parallel-tasks",
        type=int,
        default=1,
        help="How many tasks to run concurrently in separate tabs",
    )
    parser.add_argument(
        "--keep-tabs",
        action="store_true",
        help="Keep per-task tabs open after the run for debugging",
    )
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="Allow benchmark traffic to target non-local relay/private-core endpoints",
    )
    args = parser.parse_args()

    agent_names = [name.strip() for name in args.agents.split(",") if name.strip()]
    subset = [value.strip() for value in args.subset.split(",")] if args.subset else None
    tasks = load_tasks(subset=subset, site=args.site, difficulty=args.difficulty)
    if not tasks:
        print("No tasks matched filters.", file=sys.stderr)
        sys.exit(1)

    agent_id = args.agent_id or os.environ.get("CDP_AGENT_ID")
    relay_host = args.relay_host or os.environ.get("CDP_RELAY_HOST", "127.0.0.1")
    relay_port = args.relay_port
    if relay_port is None:
        relay_port = int(os.environ.get("CDP_RELAY_PORT", "8765"))

    base_config = BenchmarkConfig(
        agent_id=agent_id,
        relay_host=relay_host,
        relay_port=relay_port,
        private_core_url=os.environ.get("PRIVATE_CORE_URL") or None,
        private_core_token=os.environ.get("PRIVATE_CORE_TOKEN") or None,
    )
    allow_remote = args.allow_remote or os.environ.get("BENCH_ALLOW_REMOTE") == "1"
    _enforce_safe_targets(base_config, allow_remote=allow_remote)

    if args.parallel_tasks > 1 and not base_config.agent_id:
        print("ERROR: --parallel-tasks > 1 requires --agent-id or CDP_AGENT_ID.", file=sys.stderr)
        sys.exit(1)

    print(
        f"Benchmark: {len(tasks)} tasks, agents: {', '.join(agent_names)}, "
        f"parallel_tasks={max(1, args.parallel_tasks)}, "
        f"intermediate_goals={args.intermediate_goals}"
    )
    print(
        f"  target relay={base_config.relay_host}:{base_config.relay_port} "
        f"private_core={base_config.private_core_url or 'inprocess'}"
    )
    if not _HAS_PROGRESS_CRITIC:
        print("  (progress_critic unavailable; running without progress scoring)")
    if not base_config.agent_id:
        print("  (no agent id supplied; tasks will reuse the default tab)")

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    all_results: dict[str, list[dict]] = {}

    for agent_name in agent_names:
        print(f"\n{'=' * 60}")
        print(f"Agent: {_agent_label(agent_name)}")
        print(f"{'=' * 60}")

        if not _validate_agent_prereqs(agent_name, base_config):
            continue

        preflight_err = await _preflight_agent_check(base_config)
        if preflight_err:
            print(f"  PREFLIGHT FAIL: {preflight_err}", file=sys.stderr)
            print(
                "  Hint: follow benchmark/README.md and start the local relay plus local chrome_bridge.py first.",
                file=sys.stderr,
            )
            continue
        if base_config.agent_id:
            print(f"  Preflight OK: agent {base_config.agent_id} connected")

        results = await run_agent_on_tasks(
            agent_name,
            tasks,
            run_id=run_id,
            base_config=base_config,
            cli_model=args.cli_model,
            orch_model=args.orch_model,
            or_model=args.or_model,
            max_turns=args.max_turns,
            intermediate_goal_mode=args.intermediate_goals,
            parallel_tasks=args.parallel_tasks,
            keep_tabs=args.keep_tabs,
        )
        all_results[_agent_label(agent_name)] = results

    if not all_results:
        print("No agents ran successfully.", file=sys.stderr)
        sys.exit(1)

    print_summary(all_results, tasks)
    print(f"Results saved to: benchmark/results/{run_id}.jsonl")


if __name__ == "__main__":
    asyncio.run(main())
