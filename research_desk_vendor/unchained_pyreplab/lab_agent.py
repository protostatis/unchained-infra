from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .mcp_client import DEFAULT_AGENT_ENV_PATH, DEFAULT_ENDPOINT, infer_api_base, parse_env_file
from .planning import normalize_mission_plan

from .analysis_runtime import table_columns, table_row_count
from .lab_session import LabSession

OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
UNCHAINED_TRIAL_PATHS = {
    "mission": "/research-desk/agent/mission",
    "generation": "/research-desk/agent/generate",
    "summary": "/research-desk/agent/summary",
    "gather_qa": "/research-desk/agent/gather-qa",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class LabAgentError(RuntimeError):
    """Raised when the local lab agent cannot produce a response."""


def _compact_agent_error_message(message: str) -> str:
    text = str(message or "").strip()
    if not text:
        return "unknown error"
    if "\n--------" in text:
        text = text.split("\n--------", 1)[0].strip()
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > 220:
        text = text[:217].rstrip() + "..."
    return text


def _recent_turns(session: LabSession, limit: int = 8) -> list[dict[str, Any]]:
    turns = session.read_turns()
    return turns[-limit:]


def _namespace_summary(session: LabSession) -> list[dict[str, Any]]:
    if hasattr(session, "namespace_rows"):
        rows = session.namespace_rows()
        if rows:
            return rows
    rows: list[dict[str, Any]] = []
    for key, value in sorted(session.namespace.items()):
        if key.startswith("__"):
            continue
        row: dict[str, Any] = {"name": key, "type": type(value).__name__}
        if hasattr(value, "shape") and hasattr(value, "columns"):
            row["size"] = table_row_count(value)
            row["columns"] = table_columns(value)
        elif isinstance(value, list):
            row["size"] = len(value)
            if value and isinstance(value[0], dict):
                row["columns"] = list(value[0].keys())[:12]
        elif isinstance(value, dict):
            row["size"] = len(value)
            row["keys"] = list(value.keys())[:12]
        rows.append(row)
    return rows


def build_turn_context(session: LabSession, query: str) -> dict[str, Any]:
    session.ensure_initialized()
    context = session.namespace.get("context", {})
    capsule = session.namespace.get("capsule")
    task_spec = session.namespace.get("task_spec") or {}
    source_plan = session.namespace.get("source_plan") or {}
    object_manifest = session.namespace.get("object_manifest") or {}
    readiness = session.namespace.get("readiness") or {}
    gather_qa = session.namespace.get("gather_qa") or {}
    gather_qa_review = session.namespace.get("gather_qa_review") or {}
    capsule_state = session.namespace.get("capsule_state") or {}
    capture_brief = session.namespace.get("capture_brief") or {}
    analysis_plan = session.namespace.get("analysis_plan") or {}
    agent_mode = detect_agent_mode()
    configured_model = ""
    configured_provider = ""
    recommended_dataframe = str(session.namespace.get("recommended_dataframe", "")).strip()
    if agent_mode == "command":
        configured_model = os.environ.get("UNCHAINED_PYREPLAB_CODEX_MODEL", "").strip()
        configured_provider = "codex"
    elif agent_mode == "trial":
        configured_model = "openrouter-trial"
        configured_provider = "unchained-trial"
    elif agent_mode == "openai":
        configured_model = os.environ.get("OPENAI_MODEL", "").strip()
        configured_provider = "openai"
    else:
        configured_model = "builtin"
        configured_provider = "local"
    return {
        "capsule_path": str(session.capsule_dir),
        "capsule_summary": capsule.summary() if capsule else {},
        "task_spec": task_spec,
        "source_plan": source_plan,
        "object_manifest": object_manifest,
        "readiness": readiness,
        "gather_qa": gather_qa,
        "gather_qa_review": gather_qa_review,
        "capsule_state": capsule_state,
        "task": analysis_plan.get("task", ""),
        "task_type": analysis_plan.get("task_type", ""),
        "agent_mode": agent_mode,
        "configured_model": configured_model,
        "configured_provider": configured_provider,
        "recommended_dataframe": recommended_dataframe,
        "ranking_policy": analysis_plan.get("ranking_policy", {}),
        "bootstrap_summary": capture_brief.get("summary", {}),
        "priority_actions": capture_brief.get("priority_actions", []),
        "available_objects": _namespace_summary(session),
        "recent_turns": _recent_turns(session),
        "user_query": query,
        "execution_rules": {
            "print_required": True,
            "prefer_existing_objects": True,
            "avoid_network": True,
        },
        "source_count": len(context.get("source_rows", [])) if isinstance(context, dict) else 0,
        "entity_count": len(context.get("entity_rows", [])) if isinstance(context, dict) else 0,
    }


def detect_agent_mode() -> str:
    if os.environ.get("UNCHAINED_PYREPLAB_AGENT_CMD"):
        return "command"
    if os.environ.get("UNCHAINED_PYREPLAB_TRIAL_ENABLED"):
        return "trial"
    if os.environ.get("OPENAI_API_KEY") and os.environ.get("OPENAI_MODEL"):
        return "openai"
    return "builtin"


def _command_output_to_json(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if not text:
        raise LabAgentError("agent command returned empty output")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LabAgentError("agent command did not return valid JSON") from exc
    if not isinstance(payload, dict):
        raise LabAgentError("agent command must return a JSON object")
    return payload


def _run_command_agent(prompt: dict[str, Any], *, env_name: str) -> dict[str, Any]:
    command = os.environ.get(env_name, "").strip()
    if not command:
        raise LabAgentError(
            "No lab agent configured. Set {name} to a command that reads JSON on stdin and returns JSON.".format(
                name=env_name
            )
        )
    timeout_env = env_name.replace("_CMD", "_TIMEOUT_SECONDS")
    default_timeout = "15" if env_name == "UNCHAINED_PYREPLAB_SUMMARY_CMD" else "45"
    timeout_seconds = float(os.environ.get(timeout_env, default_timeout))
    try:
        proc = subprocess.run(
            command,
            input=json.dumps(prompt, ensure_ascii=True),
            text=True,
            capture_output=True,
            shell=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise LabAgentError(
            "agent command timed out after {seconds:.0f}s".format(seconds=timeout_seconds)
        ) from exc
    if proc.returncode != 0:
        raise LabAgentError(
            "agent command failed: {stderr}".format(stderr=(proc.stderr.strip() or "unknown error"))
        )
    return _command_output_to_json(proc.stdout)


def _openai_request(payload: dict[str, Any]) -> dict[str, Any]:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    model = os.environ.get("OPENAI_MODEL", "").strip()
    if not api_key or not model:
        raise LabAgentError("Set OPENAI_API_KEY and OPENAI_MODEL to use the OpenAI lab agent mode.")

    request = urllib.request.Request(
        OPENAI_RESPONSES_URL,
        method="POST",
        headers={
            "Authorization": "Bearer {key}".format(key=api_key),
            "Content-Type": "application/json",
        },
        data=json.dumps(payload, ensure_ascii=True).encode("utf-8"),
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            body = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise LabAgentError("OpenAI request failed: {body}".format(body=body)) from exc
    except Exception as exc:
        raise LabAgentError("OpenAI request failed: {error}".format(error=exc)) from exc

    try:
        result = json.loads(body)
    except json.JSONDecodeError as exc:
        raise LabAgentError("OpenAI response was not valid JSON") from exc
    if not isinstance(result, dict):
        raise LabAgentError("OpenAI response was not a JSON object")
    return result


def _extract_openai_text(response: dict[str, Any]) -> str:
    output = response.get("output")
    if isinstance(output, list):
        texts: list[str] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    texts.append(part["text"])
        if texts:
            return "\n".join(texts)
    if isinstance(response.get("output_text"), str):
        return response["output_text"]
    raise LabAgentError("OpenAI response did not contain text output")


def _openai_json_agent(system_prompt: str, prompt: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "model": os.environ.get("OPENAI_MODEL", "").strip(),
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
            {"role": "user", "content": [{"type": "input_text", "text": json.dumps(prompt, ensure_ascii=True)}]},
        ],
        "text": {"format": {"type": "json_object"}},
    }
    response = _openai_request(payload)
    return _command_output_to_json(_extract_openai_text(response))


def _unchained_trial_api_key() -> str:
    api_key = os.environ.get("UNCHAINED_API_KEY", "").strip()
    if api_key:
        return api_key
    env_values = parse_env_file(DEFAULT_AGENT_ENV_PATH)
    api_key = str(env_values.get("UNCHAINED_API_KEY", "")).strip()
    if api_key:
        return api_key
    raise LabAgentError("Set UNCHAINED_API_KEY or install the Unchained browser bridge to use the trial agent.")


def _unchained_trial_url(kind: str) -> str:
    api_base = os.environ.get("UNCHAINED_PYREPLAB_TRIAL_API_BASE", "").strip()
    if not api_base:
        api_base = infer_api_base(os.environ.get("UNCHAINED_MCP_ENDPOINT", DEFAULT_ENDPOINT))
    path = UNCHAINED_TRIAL_PATHS.get(kind, "")
    if not path:
        raise LabAgentError("Unknown trial agent route: {kind}".format(kind=kind))
    return api_base.rstrip("/") + path


def _unchained_trial_json_agent(kind: str, prompt: dict[str, Any]) -> dict[str, Any]:
    api_key = _unchained_trial_api_key()
    request = urllib.request.Request(
        _unchained_trial_url(kind),
        method="POST",
        headers={
            "Authorization": "Bearer {key}".format(key=api_key),
            "Content-Type": "application/json",
        },
        data=json.dumps(prompt, ensure_ascii=True).encode("utf-8"),
    )
    timeout_seconds = float(os.environ.get("UNCHAINED_PYREPLAB_TRIAL_TIMEOUT_SECONDS", "20"))
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise LabAgentError("Unchained trial agent failed: {body}".format(body=body)) from exc
    except Exception as exc:
        raise LabAgentError("Unchained trial agent failed: {error}".format(error=exc)) from exc
    return _command_output_to_json(body)


def _generation_system_prompt() -> str:
    return (
        "You are a local notebook agent. "
        "Given a JSON context and a user query, return a JSON object with keys "
        "`title`, `code`, `intent`, and optional `notes`. "
        "Write a small self-contained Python cell that uses the existing objects in the namespace. "
        "Prefer dataframe variables like source_df, entity_df, district_metrics_df, ranked_districts_df, plus capture_brief and analysis_plan. "
        "Use readiness and object_manifest when deciding whether the data is sufficient for a stronger claim. "
        "Keep tabular work in DataFrames instead of converting to dict/list when possible. "
        "Use `show_df(frame, ...)` for tabular outputs so the notebook stays dataframe-driven. "
        "If the user asks to sort, list, compare, rank, break down, inspect rows, or inspect schema, prefer `show_df(...)` over printing dicts, lists, or markdown tables. "
        "Only emit dict/list output when the user explicitly asks for JSON-like data or when returning a tiny scalar summary. "
        "Raw record lists are available as source_records, entity_records, district_metric_records, and ranked_district_records when needed. "
        "Do not browse the network. "
        "Always print the result you want the user to see. "
        "If the query cannot be answered with the current data, write code that prints a concise missing-data summary instead."
    )


def _summary_system_prompt() -> str:
    return (
        "You are a local notebook agent. "
        "Given a JSON object with the user query, generated code, execution output, and capsule context, "
        "return a JSON object with keys `markdown`, optional `status`, optional `followups`, and optional `caveats`. "
        "The markdown should be concise and readable to a data scientist using a notebook workflow. "
        "If the execution output already contains a dataframe preview or markdown table from `show_df`, do not rewrite the table as prose or another markdown table; summarize what it shows briefly and let the dataframe stand."
    )


def _mission_system_prompt() -> str:
    return (
        "You are a mission planning agent for a local research notebook product. "
        "Given a user mission prompt, infer a compact structured plan for the first analysis object. "
        "Return a JSON object with keys `objective`, `questions`, `name`, `description`, `grain`, "
        "`primary_key`, `measures`, `dimensions`, `required_columns`, `min_rows`, `seed_urls`, `source_preferences`, and `notes`. "
        "Infer the row object from the prompt itself. Do not assume every commerce task is about price. "
        "Prefer a minimal observable schema first: only include fields that are likely to be extractable directly "
        "from the first pass of source pages. "
        "Do not invent derived metrics like value scores, market deltas, weighted scores, or normalized rankings "
        "unless the prompt explicitly asks for them and the raw fields to compute them are obvious. "
        "If the prompt is about restaurants, infer a restaurant-style row object. "
        "If the prompt is about policies, infer policy entities. "
        "Only include seed URLs when they are obvious and useful from the prompt; otherwise return an empty array. "
        "Use `source_preferences` to describe discovery strategy without overcommitting to specific websites. "
        "Each source preference should include `source_type`, optional `query_hint`, optional `site_hint`, optional `route_role`, and optional `rationale`. "
        "Prefer generic source families like retailer search, review directory search, map directory search, official source search, or marketplace search. "
        "Only name a specific site in `site_hint` when the user explicitly mentioned it or it is clearly implied by an existing seed URL. "
        "Keep field names concise and python-friendly."
    )


def _gather_qa_system_prompt() -> str:
    return (
        "You are a bounded gather QA reviewer for a local browser-research pipeline. "
        "You will only review ambiguous captured pages after deterministic QA has already run. "
        "Return a JSON object with keys `reviews` and `notes`. "
        "Each review must contain `page_id`, `review_status`, `confidence`, `rationale`, and `suggested_next_action`. "
        "Allowed `review_status` values are `accepted`, `retry`, `redirect`, `duplicate`, and `blocked`. "
        "Be conservative. Only override the deterministic status when the provided metadata and page excerpt strongly support it. "
        "Do not invent missing facts, do not browse, and do not shape rows."
    )


def _builtin_plan_mission(prompt: dict[str, Any]) -> dict[str, Any]:
    user_prompt = str(prompt.get("user_prompt", "")).strip()
    plan = normalize_mission_plan(user_prompt, {})
    notes = list(plan.get("notes", []))
    notes.append("builtin-mission-planner")
    plan["notes"] = notes
    return plan


def plan_mission(
    user_prompt: str,
    *,
    capsule_dir: Path | None = None,
    existing_task_spec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "capsule_path": str(capsule_dir) if capsule_dir is not None else "",
        "user_prompt": str(user_prompt).strip(),
        "existing_task_spec": existing_task_spec or {},
    }
    try:
        if os.environ.get("UNCHAINED_PYREPLAB_MISSION_CMD"):
            mission_plan = _run_command_agent(payload, env_name="UNCHAINED_PYREPLAB_MISSION_CMD")
            return normalize_mission_plan(payload["user_prompt"], mission_plan)
        if os.environ.get("UNCHAINED_PYREPLAB_TRIAL_ENABLED"):
            mission_plan = _unchained_trial_json_agent("mission", payload)
            return normalize_mission_plan(payload["user_prompt"], mission_plan)
        if os.environ.get("OPENAI_API_KEY") and os.environ.get("OPENAI_MODEL"):
            mission_plan = _openai_json_agent(_mission_system_prompt(), payload)
            return normalize_mission_plan(payload["user_prompt"], mission_plan)
    except LabAgentError as exc:
        fallback = normalize_mission_plan(payload["user_prompt"], _builtin_plan_mission(payload))
        notes = list(fallback.get("notes", []))
        notes.append("mission-agent-fallback:{message}".format(message=str(exc)))
        fallback["notes"] = notes
        return fallback
    return normalize_mission_plan(payload["user_prompt"], _builtin_plan_mission(payload))


def _builtin_review_gather_qa(prompt: dict[str, Any]) -> dict[str, Any]:
    reviews: list[dict[str, Any]] = []
    for row in prompt.get("ambiguous_rows", []):
        if not isinstance(row, dict):
            continue
        deterministic_status = str(row.get("deterministic_status", "retry")).strip() or "retry"
        text_length = int(row.get("text_length", 0) or 0)
        target_match_score = float(row.get("target_match_score", 0.0) or 0.0)
        reasons = [str(item) for item in row.get("deterministic_reasons", []) if str(item).strip()]
        review_status = deterministic_status
        suggested_next_action = str(row.get("suggested_next_action", "")).strip() or "retry_capture"
        rationale = "Builtin Gather QA preserved the deterministic decision."
        confidence = 0.65

        if deterministic_status == "retry" and text_length >= 220 and target_match_score >= 0.2:
            review_status = "accepted"
            suggested_next_action = "shape_rows"
            rationale = "The page excerpt is long enough and overlaps the target strongly enough to accept."
            confidence = 0.72
        elif deterministic_status == "redirect" and "search_engine_page" in reasons:
            rationale = "The capture still landed on a search engine page, so it should redirect to a direct target."
            confidence = 0.9
        elif deterministic_status == "blocked":
            rationale = "The excerpt looks blocked by a captcha, login wall, or interstitial."
            confidence = 0.92
        elif deterministic_status == "duplicate":
            rationale = "The page looks like a duplicate capture of an already reviewed target."
            confidence = 0.9

        reviews.append(
            {
                "page_id": str(row.get("page_id", "")),
                "review_status": review_status,
                "confidence": confidence,
                "rationale": rationale,
                "suggested_next_action": suggested_next_action,
            }
        )
    return {
        "reviews": reviews,
        "notes": ["builtin-gather-qa-review"],
    }


def review_gather_qa(
    *,
    capsule_dir: Path,
    task_spec: dict[str, Any],
    gather_qa: dict[str, Any],
) -> dict[str, Any]:
    rows = [
        row
        for row in gather_qa.get("rows", [])
        if isinstance(row, dict)
    ]
    ambiguous_rows: list[dict[str, Any]] = []
    for row in rows:
        status = str(row.get("qa_status", "")).strip()
        reasons = [str(item) for item in row.get("reasons", []) if str(item).strip()]
        should_review = status in {"retry", "redirect"} or (
            status == "accepted" and "weak_target_match" in reasons and float(row.get("qa_score", 0) or 0) < 85
        )
        if not should_review:
            continue
        ambiguous_rows.append(
            {
                "page_id": str(row.get("page_id", "")),
                "target_title": str(row.get("target_title", "")),
                "target_object_names": list(row.get("target_object_names", [])),
                "deterministic_status": status,
                "deterministic_reasons": reasons,
                "expected_domain": str(row.get("expected_domain", "")),
                "actual_domain": str(row.get("actual_domain", "")),
                "requested_url": str(row.get("requested_url", "")),
                "final_url": str(row.get("final_url", "")),
                "text_length": int(row.get("text_length", 0) or 0),
                "target_match_score": float(row.get("target_match_score", 0.0) or 0.0),
                "page_excerpt": str(row.get("page_excerpt", "")),
                "suggested_next_action": str(row.get("suggested_next_action", "")),
            }
        )
    payload = {
        "capsule_path": str(capsule_dir),
        "task_spec": task_spec,
        "ambiguous_rows": ambiguous_rows[:8],
    }
    if not ambiguous_rows:
        return {
            "generated_at": "",
            "review_mode": "none",
            "reviewed_page_count": 0,
            "reviews": [],
            "notes": ["no-ambiguous-pages"],
        }
    try:
        if os.environ.get("UNCHAINED_PYREPLAB_GATHER_QA_CMD"):
            review = _run_command_agent(payload, env_name="UNCHAINED_PYREPLAB_GATHER_QA_CMD")
            review_mode = "command"
        elif os.environ.get("UNCHAINED_PYREPLAB_TRIAL_ENABLED"):
            review = _unchained_trial_json_agent("gather_qa", payload)
            review_mode = "trial"
        elif os.environ.get("OPENAI_API_KEY") and os.environ.get("OPENAI_MODEL"):
            review = _openai_json_agent(_gather_qa_system_prompt(), payload)
            review_mode = "openai"
        else:
            review = _builtin_review_gather_qa(payload)
            review_mode = "builtin"
    except LabAgentError as exc:
        review = _builtin_review_gather_qa(payload)
        review_mode = "builtin-fallback"
        notes = list(review.get("notes", []))
        notes.append("gather-qa-agent-fallback:{message}".format(message=str(exc)))
        review["notes"] = notes

    reviews: list[dict[str, Any]] = []
    for item in review.get("reviews", []):
        if not isinstance(item, dict):
            continue
        page_id = str(item.get("page_id", "")).strip()
        if not page_id:
            continue
        reviews.append(
            {
                "page_id": page_id,
                "review_status": str(item.get("review_status", "retry")).strip() or "retry",
                "confidence": max(0.0, min(float(item.get("confidence", 0.0) or 0.0), 1.0)),
                "rationale": str(item.get("rationale", "")).strip(),
                "suggested_next_action": str(item.get("suggested_next_action", "")).strip(),
            }
        )
    return {
        "generated_at": _now_iso(),
        "review_mode": review_mode,
        "reviewed_page_count": len(reviews),
        "reviews": reviews,
        "notes": [str(item) for item in review.get("notes", []) if str(item).strip()],
    }


def _builtin_generate(prompt: dict[str, Any]) -> dict[str, Any]:
    query = str(prompt.get("user_query", "")).strip()
    query_lower = query.lower()
    available_objects = prompt.get("available_objects", [])
    recommended_dataframe = str(prompt.get("recommended_dataframe", "")).strip()
    listings_frame_setup = (
        "if 'listings_top100_df' in globals():\n"
        "    frame = listings_top100_df.copy()\n"
        "elif 'listings_df' in globals():\n"
        "    frame = listings_df.copy()\n"
        "else:\n"
        "    print({'error': 'listings_df is not available yet', 'available_tables': list(table_frames.keys()) if 'table_frames' in globals() else []})\n"
        "    frame = None\n"
        "frame_ready = frame is not None\n"
        "if frame_ready and 'is_candidate' in frame.columns:\n"
        "    frame = frame.loc[frame['is_candidate'].fillna(False)]\n"
        "if frame_ready and ('price_value' not in frame.columns or frame.empty):\n"
        "    print({'error': 'listings dataframe has no candidate price rows', 'columns': list(frame.columns)})\n"
        "    frame_ready = False\n"
    )

    dataframe_reference = _resolve_dataframe_reference(
        query,
        available_objects=available_objects,
        recommended_dataframe=recommended_dataframe,
    )

    if dataframe_reference and any(
        term in query_lower for term in ("describe", "info", "schema", "columns", "column", "dtypes", "dtype")
    ):
        return {
            "title": "DataFrame Overview",
            "intent": "Describe the requested dataframe and preview a few rows",
            "code": (
                "frame_name = {frame_name!r}\n"
                "frame = globals().get(frame_name)\n"
                "if frame is None:\n"
                "    print({{'error': 'dataframe not available', 'requested_frame': frame_name}})\n"
                "else:\n"
                "    rows = [{{'dataframe': frame_name, 'row_count': int(frame.shape[0]), 'column_count': int(frame.shape[1])}}]\n"
                "    show_df(rows, limit=5)\n"
                "    schema_rows = [{{'column': str(column), 'dtype': str(dtype)}} for column, dtype in frame.dtypes.items()]\n"
                "    show_df(schema_rows, limit=20)\n"
                "    show_df(frame, limit=8)"
            ).format(frame_name=dataframe_reference),
            "notes": ["builtin-agent", "resolved-dataframe:{name}".format(name=dataframe_reference)],
        }

    if dataframe_reference and any(
        term in query_lower
        for term in ("strongest evidence", "why it wins", "why it leads", "top evidence", "strongest rows")
    ):
        return {
            "title": "Strongest Evidence",
            "intent": "Show the strongest current rows from the requested dataframe",
            "code": _generic_dataframe_signal_code(dataframe_reference),
            "notes": ["builtin-agent", "resolved-dataframe:{name}".format(name=dataframe_reference)],
        }

    if dataframe_reference and any(
        term in query_lower for term in ("thin evidence", "duplicates", "follow-up", "followup", "weak spots")
    ):
        return {
            "title": "Evidence Gaps",
            "intent": "Surface rows that need follow-up or QA attention",
            "code": _generic_dataframe_qa_code(dataframe_reference),
            "notes": ["builtin-agent", "resolved-dataframe:{name}".format(name=dataframe_reference)],
        }

    if "rankable" in query_lower:
        return {
            "title": "Rankable Entities",
            "intent": "List the currently rankable entities",
            "code": (
                "rows = ranked_districts_df.loc[ranked_districts_df['is_rankable'].fillna(False), ['entity_name', 'final_rankable_score', 'exploratory_score', 'ranking_status']].copy()\n"
                "show_df(rows, limit=20)"
            ),
            "notes": ["builtin-agent"],
        }

    if "highest scoring" in query_lower or "top scoring" in query_lower or "best scoring" in query_lower:
        return {
            "title": "Highest Scoring Entities",
            "intent": "Show exploratory and final ranking signals",
            "code": (
                "columns = ['final_rank', 'entity_name', 'final_rankable_score', 'exploratory_rank', "
                "'exploratory_score', 'ranking_status']\n"
                "show_df(ranked_districts_df.loc[:, columns], limit=20)"
            ),
            "notes": ["builtin-agent"],
        }

    if (
        ("price" in query_lower or "expensive" in query_lower)
        and any(term in query_lower for term in ("highest", "highest price", "most expensive", "sort", "descending"))
        and "lowest" not in query_lower
    ):
        return {
            "title": "Highest Priced Listings",
            "intent": "Sort the iron-set listings from highest to lowest price",
            "code": (
                listings_frame_setup
                + "if frame_ready:\n"
                "    sample_columns = [column for column in ['title_clean', 'price_value', 'shipping_text', 'condition', 'listing_url'] if column in frame.columns]\n"
                "    result_df = frame.nlargest(10, 'price_value').loc[:, sample_columns]\n"
                "    show_df(result_df, limit=10)"
            ),
            "notes": ["builtin-agent"],
        }

    if "condition" in query_lower and ("price" in query_lower or "listing" in query_lower):
        return {
            "title": "Condition Breakdown",
            "intent": "Group current candidate listings by condition and summarize their prices",
            "code": (
                listings_frame_setup
                + "if frame_ready:\n"
                "    grouped = frame.groupby('condition', dropna=False)['price_value'].agg(['count', 'median', 'mean']).reset_index()\n"
                "    grouped = grouped.sort_values(['count', 'median'], ascending=[False, False])\n"
                "    grouped = grouped.rename(columns={'count': 'listing_count', 'median': 'median_price', 'mean': 'mean_price'})\n"
                "    show_df(grouped, limit=20)"
            ),
            "notes": ["builtin-agent"],
        }

    if any(term in query_lower for term in ("lowest price", "cheapest", "least expensive", "lowest priced")):
        return {
            "title": "Lowest Priced Listings",
            "intent": "Show the cheapest current candidate iron-set listings",
            "code": (
                listings_frame_setup
                + "if frame_ready:\n"
                "    sample_columns = [column for column in ['title_clean', 'price_value', 'shipping_text', 'condition', 'listing_url'] if column in frame.columns]\n"
                "    result_df = frame.nsmallest(10, 'price_value').loc[:, sample_columns]\n"
                "    show_df(result_df, limit=10)"
            ),
            "notes": ["builtin-agent"],
        }

    if "price" in query_lower or "median" in query_lower or "average" in query_lower:
        return {
            "title": "Price Comparison",
            "intent": "Summarize the extracted listings price distribution",
            "code": (
                listings_frame_setup
                + "if frame_ready:\n"
                "    summary_rows = [\n"
                "        {'metric': 'listing_count', 'value': int(len(frame))},\n"
                "        {'metric': 'median_price', 'value': round(float(frame['price_value'].median()), 2)},\n"
                "        {'metric': 'mean_price', 'value': round(float(frame['price_value'].mean()), 2)},\n"
                "        {'metric': 'min_price', 'value': round(float(frame['price_value'].min()), 2)},\n"
                "        {'metric': 'max_price', 'value': round(float(frame['price_value'].max()), 2)},\n"
                "    ]\n"
                "    show_df(summary_rows, limit=10)\n"
                "    sample_columns = [column for column in ['title_clean', 'price_value', 'shipping_text', 'condition', 'listing_url'] if column in frame.columns]\n"
                "    show_df(frame.nsmallest(10, 'price_value').loc[:, sample_columns], limit=10)"
            ),
            "notes": ["builtin-agent"],
        }

    if "report card" in query_lower or "state source" in query_lower:
        return {
            "title": "State Report Card Gaps",
            "intent": "Show entities still missing state report-card sources",
            "code": (
                "report_card_gaps = []\n"
                "for entity in capture_brief.get('entities', []):\n"
                "    report_card_gaps.append({\n"
                "        'entity_name': entity.get('entity_name'),\n"
                "        'missing_state_report_card': 'state_report_card' in entity.get('missing_required_source_types', []),\n"
                "        'state_queries': [item.get('query') for item in entity.get('recommended_queries', []) if item.get('source_type') == 'state_report_card'],\n"
                "    })\n"
                "show_df(report_card_gaps, limit=20)"
            ),
            "notes": ["builtin-agent"],
        }

    if "missing" in query_lower or "what is missing" in query_lower or "what's missing" in query_lower:
        return {
            "title": "Missing Data Summary",
            "intent": "Summarize source and metric gaps",
            "code": (
                "summary_rows = [{'metric': key, 'value': value} for key, value in (capture_brief.get('summary', {}) or {}).items()]\n"
                "show_df(summary_rows, limit=20)\n"
                "priority_rows = [{'priority_action': action} for action in capture_brief.get('priority_actions', [])]\n"
                "if priority_rows:\n"
                "    show_df(priority_rows, limit=20)"
            ),
            "notes": ["builtin-agent"],
        }

    if "column" in query_lower or "schema" in query_lower:
        return {
            "title": "Object Columns",
            "intent": "Describe available object columns",
            "code": (
                "rows = []\n"
                "for object_name in ['source_df', 'entity_df', 'district_metrics_df', 'ranked_districts_df']:\n"
                "    if object_name not in globals():\n"
                "        continue\n"
                "    frame = globals()[object_name]\n"
                "    columns = list(frame.columns) if hasattr(frame, 'columns') else []\n"
                "    rows.append({'object_name': object_name, 'column_count': len(columns), 'columns': ', '.join(str(column) for column in columns)})\n"
                "show_df(rows, limit=20)"
            ),
            "notes": ["builtin-agent"],
        }

    if "how many rows" in query_lower or "row count" in query_lower or "how many" in query_lower:
        return {
            "title": "Row Counts",
            "intent": "Count current notebook objects",
            "code": (
                "rows = [\n"
                "    {'object_name': 'source_df', 'row_count': len(source_df)},\n"
                "    {'object_name': 'entity_df', 'row_count': len(entity_df)},\n"
                "    {'object_name': 'district_metrics_df', 'row_count': len(district_metrics_df)},\n"
                "    {'object_name': 'ranked_districts_df', 'row_count': len(ranked_districts_df) if 'ranked_districts_df' in globals() else 0},\n"
                "]\n"
                "show_df(rows, limit=20)"
            ),
            "notes": ["builtin-agent"],
        }

    if "academic" in query_lower:
        return {
            "title": "Academic Signal",
            "intent": "Sort entities by academic performance score",
            "code": (
                "rows = ranked_districts_df.sort_values('academic_performance_score', ascending=False, na_position='last')\n"
                "show_df(rows.loc[:, ['entity_name', 'academic_performance_score', 'ranking_status']], limit=20)"
            ),
            "notes": ["builtin-agent"],
        }

    if dataframe_reference and _query_prefers_dataframe(query):
        return {
            "title": "DataFrame Preview",
            "intent": "Show the requested dataframe and a compact summary",
            "code": (
                "frame_name = {frame_name!r}\n"
                "frame = globals().get(frame_name)\n"
                "if frame is None:\n"
                "    print({{'error': 'dataframe not available', 'requested_frame': frame_name}})\n"
                "else:\n"
                "    summary_rows = [\n"
                "        {{'metric': 'dataframe', 'value': frame_name}},\n"
                "        {{'metric': 'row_count', 'value': int(frame.shape[0])}},\n"
                "        {{'metric': 'column_count', 'value': int(frame.shape[1])}},\n"
                "    ]\n"
                "    show_df(summary_rows, limit=10)\n"
                "    show_df(frame, limit=12)"
            ).format(frame_name=dataframe_reference),
            "notes": ["builtin-agent", "resolved-dataframe:{name}".format(name=dataframe_reference)],
        }

    return {
        "title": "Context Summary",
        "intent": "Show current objects and suggest narrower next questions",
        "code": (
            "rows = []\n"
            "for name in sorted([key for key in globals() if key.endswith('_df')]):\n"
            "    frame = globals().get(name)\n"
            "    if hasattr(frame, 'shape'):\n"
            "        rows.append({{'object_name': name, 'row_count': int(frame.shape[0]), 'column_count': int(frame.shape[1])}})\n"
            "show_df(rows, limit=20)\n"
            "priority_rows = [{{'priority_action': action}} for action in capture_brief.get('priority_actions', [])]\n"
            "if priority_rows:\n"
            "    show_df(priority_rows, limit=20)\n"
            "print('Ask about a dataframe by name, for example describe {frame}.')".format(
                frame=(recommended_dataframe or "the primary dataframe")
            )
        ),
        "notes": ["builtin-agent"],
    }


def _resolve_dataframe_reference(
    query: str,
    *,
    available_objects: Any,
    recommended_dataframe: str = "",
) -> str:
    names: list[str] = []
    if isinstance(available_objects, list):
        for item in available_objects:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            if name.endswith("_df"):
                names.append(name)
    if recommended_dataframe and recommended_dataframe.endswith("_df"):
        names.insert(0, recommended_dataframe)
    ordered_names: list[str] = []
    seen: set[str] = set()
    for name in names:
        if name and name not in seen:
            ordered_names.append(name)
            seen.add(name)

    query_lower = str(query).lower()
    explicit = re.findall(r"\b[a-zA-Z_][a-zA-Z0-9_]*_df\b", query_lower)
    if explicit:
        requested = explicit[0]
        if requested in seen:
            return requested
        for name in ordered_names:
            singular = name
            if singular.endswith("ies_df") and len(singular) > len("ies_df"):
                singular = singular[:-6] + "y_df"
            elif singular.endswith("s_df") and not singular.endswith("ss_df"):
                singular = singular[:-4] + "_df"
            elif singular.endswith("_rows_df"):
                singular = singular[:-8] + "_df"
            if requested == singular:
                return name

    describe_like = any(term in query_lower for term in ("describe", "info", "schema", "columns", "column", "dtype", "dtypes"))
    if describe_like and recommended_dataframe:
        return recommended_dataframe

    if _query_prefers_dataframe(query):
        alias_map: dict[str, str] = {}
        for name in ordered_names:
            stem = name[:-3] if name.endswith("_df") else name
            alias_candidates = {
                stem,
                stem.replace("_", " "),
            }
            if stem.endswith("ies"):
                singular = stem[:-3] + "y"
                alias_candidates.add(singular)
                alias_candidates.add(singular.replace("_", " "))
            elif stem.endswith("s") and not stem.endswith("ss"):
                singular = stem[:-1]
                alias_candidates.add(singular)
                alias_candidates.add(singular.replace("_", " "))
            for alias in alias_candidates:
                cleaned = alias.strip().lower()
                if cleaned:
                    alias_map[cleaned] = name
        for alias, frame_name in sorted(alias_map.items(), key=lambda item: len(item[0]), reverse=True):
            if alias in query_lower:
                return frame_name
    return ""


def _query_prefers_dataframe(query: str) -> bool:
    query_lower = str(query).lower()
    if any(token in query_lower for token in ("json", "dictionary", "dict", "raw object", "raw json")):
        return False
    dataframe_terms = (
        "show",
        "list",
        "sort",
        "rank",
        "ranking",
        "top",
        "lowest",
        "highest",
        "breakdown",
        "compare",
        "rows",
        "columns",
        "schema",
        "table",
        "dataframe",
    )
    return any(term in query_lower for term in dataframe_terms)


def _generic_dataframe_signal_code(frame_name: str) -> str:
    template = """
frame_name = __FRAME_NAME__
frame = globals().get(frame_name)
if frame is None:
    print({'error': 'dataframe not available', 'requested_frame': frame_name})
else:
    working = frame.copy()
    label_columns = [column for column in [
        'space_name', 'market_title', 'security_name', 'ticker', 'listing_title', 'title_clean',
        'entity_name', 'property_name', 'neighborhood', 'city', 'state'
    ] if column in working.columns]
    priority_metrics = [
        'row_confidence_score', 'review_count', 'rating_value', 'volume_usd', 'liquidity_usd',
        'monthly_price_usd', 'price_value', 'price_usd', 'yes_price', 'yoy_change_pct',
        'mom_change_pct', 'odometer_miles'
    ]
    metric_columns = [column for column in priority_metrics if column in working.columns]
    if not metric_columns:
        numeric_columns = [str(column) for column in working.select_dtypes(include='number').columns]
        metric_columns = numeric_columns[:3]
    sort_metric = metric_columns[0] if metric_columns else ''
    ascending = sort_metric in {'monthly_price_usd', 'price_value', 'price_usd', 'odometer_miles'}
    if sort_metric:
        working = working.sort_values(sort_metric, ascending=ascending, na_position='last')
    summary_rows = [
        {'metric': 'dataframe', 'value': frame_name},
        {'metric': 'row_count', 'value': int(working.shape[0])},
        {'metric': 'column_count', 'value': int(working.shape[1])},
        {'metric': 'ranking_metric', 'value': sort_metric or 'none'},
    ]
    show_df(summary_rows, limit=10)
    selected = []
    for column in label_columns + metric_columns + ['row_confidence', 'source_domain', 'source_url']:
        if column in working.columns and column not in selected:
            selected.append(column)
    if not selected:
        selected = list(working.columns[:8])
    show_df(working.loc[:, selected], limit=8)
""".strip()
    return template.replace("__FRAME_NAME__", repr(frame_name))


def _generic_dataframe_qa_code(frame_name: str) -> str:
    template = """
frame_name = __FRAME_NAME__
frame = globals().get(frame_name)
if frame is None:
    print({'error': 'dataframe not available', 'requested_frame': frame_name})
else:
    working = frame.copy()
    if 'qa_status' in working.columns:
        issue_rows = working.loc[working['qa_status'].fillna('') != 'accepted'].copy()
        if issue_rows.empty:
            issue_rows = working.copy()
        selected = [column for column in [
            'page_id', 'qa_status', 'qa_score', 'suggested_next_action', 'expected_domain',
            'actual_domain', 'target_title', 'final_url'
        ] if column in issue_rows.columns]
        show_df(issue_rows.loc[:, selected], limit=12)
    elif 'review_status' in working.columns:
        selected = [column for column in [
            'page_id', 'review_status', 'confidence', 'suggested_next_action', 'rationale'
        ] if column in working.columns]
        show_df(working.loc[:, selected], limit=12)
    else:
        show_df(working, limit=12)
""".strip()
    return template.replace("__FRAME_NAME__", repr(frame_name))


def _enforce_dataframe_first(generated: dict[str, Any], query: str) -> dict[str, Any]:
    code = str(generated.get("code", "")).strip()
    if not code or not _query_prefers_dataframe(query) or "show_df(" in code:
        return generated
    updated_code = re.sub(
        r"print\((?P<expr>[^\n]+?)\.to_dict\(orient=['\"]records['\"]\)\)",
        r"show_df(\g<expr>)",
        code,
    )
    if updated_code != code:
        payload = dict(generated)
        notes = [str(item) for item in payload.get("notes", []) if str(item).strip()]
        notes.append("dataframe-output-enforced")
        payload["notes"] = notes
        payload["code"] = updated_code
        return payload
    return generated


def _sanitize_dataframe_truthiness(generated: dict[str, Any]) -> dict[str, Any]:
    code = str(generated.get("code", "")).strip()
    if not code:
        return generated
    pattern = re.compile(
        r"^(?P<indent>\s*)(?P<var>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<chain>globals\(\)\.get\([^)]+\)(?:\s+or\s+globals\(\)\.get\([^)]+\))+)\s*$",
        re.MULTILINE,
    )

    def replacer(match: re.Match[str]) -> str:
        indent = match.group("indent")
        var = match.group("var")
        chain = str(match.group("chain")).strip()
        pieces = [piece.strip() for piece in re.split(r"\s+or\s+", chain) if piece.strip()]
        if len(pieces) < 2:
            return match.group(0)
        lines = [f"{indent}{var} = {pieces[0]}"]
        for piece in pieces[1:]:
            lines.append(f"{indent}if {var} is None:")
            lines.append(f"{indent}    {var} = {piece}")
        return "\n".join(lines)

    updated_code = pattern.sub(replacer, code)
    if updated_code == code:
        return generated
    payload = dict(generated)
    notes = [str(item) for item in payload.get("notes", []) if str(item).strip()]
    notes.append("dataframe-truthiness-sanitized")
    payload["notes"] = notes
    payload["code"] = updated_code
    return payload


def _normalize_generated_turn(generated: dict[str, Any], query: str) -> dict[str, Any]:
    payload = _enforce_dataframe_first(generated, query)
    payload = _sanitize_dataframe_truthiness(payload)
    return payload


def _builtin_summarize(
    query: str,
    *,
    generated: dict[str, Any],
    execution: dict[str, Any],
) -> dict[str, Any]:
    title = str(generated.get("title", "Agent Reply")).strip() or "Agent Reply"
    if execution.get("status") == "running":
        body = str(execution.get("content", "")).strip() or "pyreplab is still executing."
        markdown = "## {title}\n\nExecution is still running in pyreplab.\n\n```text\n{body}\n```".format(
            title=title,
            body=body,
        )
        return {"markdown": markdown, "status": "running"}
    if execution.get("status") == "error":
        error = str(execution.get("error", "")).strip() or str(execution.get("content", "")).strip()
        markdown = "## {title}\n\nExecution failed.\n\n```text\n{error}\n```".format(
            title=title,
            error=error,
        )
        return {"markdown": markdown, "status": "error"}

    output = str(execution.get("content", "")).strip() or "(no stdout)"
    parsed_output = _parse_structured_output(output)
    markdown = _render_builtin_markdown(
        title,
        query=query,
        output=output,
        parsed_output=parsed_output,
    )
    return {"markdown": markdown, "status": "ok"}


def _parse_structured_output(text: str) -> Any:
    clean = text.strip()
    if not clean or clean == "(no stdout)":
        return None
    for parser in (json.loads, ast.literal_eval):
        try:
            return parser(clean)
        except Exception:
            continue
    return None


def _pretty_key(key: str) -> str:
    return key.replace("_", " ").strip()


def _format_scalar(key: str, value: Any) -> str:
    if isinstance(value, float):
        if "price" in key or "cost" in key:
            return "${value:,.2f}".format(value=value)
        return "{value:.2f}".format(value=value)
    return str(value)


def _truncate(text: str, limit: int = 96) -> str:
    clean = " ".join(str(text).split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3].rstrip() + "..."


def _render_price_markdown(title: str, query: str, payload: dict[str, Any]) -> str:
    summary = payload.get("summary", {})
    lowest_priced = payload.get("lowest_priced", [])
    lines = [
        "## {title}".format(title=title),
        "",
        "Question: {query}".format(query=query),
        "",
        "- Sample: {count} candidate listings".format(count=summary.get("listing_count", 0)),
        "- Median price: {value}".format(value=_format_scalar("median_price", summary.get("median_price", ""))),
        "- Mean price: {value}".format(value=_format_scalar("mean_price", summary.get("mean_price", ""))),
        "- Price range: {low} to {high}".format(
            low=_format_scalar("min_price", summary.get("min_price", "")),
            high=_format_scalar("max_price", summary.get("max_price", "")),
        ),
    ]
    if lowest_priced:
        lines.extend(["", "### Lowest Priced Examples"])
        for item in lowest_priced[:5]:
            if not isinstance(item, dict):
                continue
            lines.append(
                "- {title} | {price} | {condition} | {shipping}".format(
                    title=_truncate(item.get("title_clean", "")),
                    price=_format_scalar("price_value", item.get("price_value", "")),
                    condition=item.get("condition", "unknown"),
                    shipping=item.get("shipping_text", "shipping n/a") or "shipping n/a",
                )
            )
    return "\n".join(lines)


def _looks_like_listing_rows(payload: list[Any]) -> bool:
    if not payload:
        return False
    first = payload[0]
    if not isinstance(first, dict):
        return False
    return "price_value" in first and ("title_clean" in first or "listing_url" in first)


def _render_listing_rows(title: str, query: str, payload: list[dict[str, Any]]) -> str:
    lines = [
        "## {title}".format(title=title),
        "",
        "Question: {query}".format(query=query),
        "",
        "- Returned rows: {count}".format(count=len(payload)),
        "",
    ]
    for index, item in enumerate(payload[:10], start=1):
        lines.append(
            "{index}. {title} | {price} | {condition} | {shipping}".format(
                index=index,
                title=_truncate(item.get("title_clean", "") or item.get("listing_url", ""), limit=120),
                price=_format_scalar("price_value", item.get("price_value", "")),
                condition=item.get("condition", "unknown"),
                shipping=item.get("shipping_text", "shipping n/a") or "shipping n/a",
            )
        )
    if len(payload) > 10:
        lines.extend(["", "- ... {count} more rows".format(count=len(payload) - 10)])
    return "\n".join(lines)


def _render_simple_list(title: str, query: str, payload: list[Any]) -> str:
    lines = [
        "## {title}".format(title=title),
        "",
        "Question: {query}".format(query=query),
        "",
    ]
    if not payload:
        lines.append("- No rows returned.")
        return "\n".join(lines)
    for item in payload[:8]:
        if isinstance(item, dict):
            parts = []
            for key in ("entity_name", "title_clean", "price_value", "condition", "shipping_text", "ranking_status"):
                if key in item and item.get(key) not in (None, "", []):
                    parts.append("{label}: {value}".format(label=_pretty_key(key), value=_format_scalar(key, item.get(key))))
            if not parts:
                for key, value in list(item.items())[:4]:
                    if value not in (None, "", []):
                        parts.append("{label}: {value}".format(label=_pretty_key(str(key)), value=_format_scalar(str(key), value)))
            lines.append("- " + " | ".join(parts))
        else:
            lines.append("- {value}".format(value=item))
    if len(payload) > 8:
        lines.append("- ... {count} more rows".format(count=len(payload) - 8))
    return "\n".join(lines)


def _render_simple_dict(title: str, query: str, payload: dict[str, Any]) -> str:
    lines = [
        "## {title}".format(title=title),
        "",
        "Question: {query}".format(query=query),
        "",
    ]
    scalar_rows = [(key, value) for key, value in payload.items() if not isinstance(value, (dict, list))]
    nested_rows = [(key, value) for key, value in payload.items() if isinstance(value, (dict, list))]
    for key, value in scalar_rows:
        lines.append("- {key}: {value}".format(key=_pretty_key(str(key)), value=_format_scalar(str(key), value)))
    for key, value in nested_rows:
        lines.extend(["", "### {title}".format(title=_pretty_key(str(key)).title())])
        if isinstance(value, dict):
            for child_key, child_value in value.items():
                if isinstance(child_value, (dict, list)):
                    continue
                lines.append(
                    "- {key}: {value}".format(
                        key=_pretty_key(str(child_key)),
                        value=_format_scalar(str(child_key), child_value),
                    )
                )
        elif isinstance(value, list):
            if not value:
                lines.append("- none")
            else:
                for item in value[:6]:
                    if isinstance(item, dict):
                        parts = []
                        for child_key, child_value in list(item.items())[:4]:
                            if child_value in (None, "", []):
                                continue
                            parts.append(
                                "{key}: {value}".format(
                                    key=_pretty_key(str(child_key)),
                                    value=_format_scalar(str(child_key), child_value),
                                )
                            )
                        lines.append("- " + " | ".join(parts))
                    else:
                        lines.append("- {value}".format(value=item))
                if len(value) > 6:
                    lines.append("- ... {count} more rows".format(count=len(value) - 6))
    return "\n".join(lines)


def _render_builtin_markdown(
    title: str,
    *,
    query: str,
    output: str,
    parsed_output: Any,
) -> str:
    if isinstance(parsed_output, dict) and "summary" in parsed_output and "lowest_priced" in parsed_output:
        return _render_price_markdown(title, query, parsed_output)
    if isinstance(parsed_output, list) and _looks_like_listing_rows(parsed_output):
        return _render_listing_rows(title, query, parsed_output)
    if isinstance(parsed_output, list):
        return _render_simple_list(title, query, parsed_output)
    if isinstance(parsed_output, dict):
        return _render_simple_dict(title, query, parsed_output)
    if "\n|" in output and "\n|---" in output:
        return "## {title}\n\nQuestion: {query}\n\n{output}".format(
            title=title,
            query=query,
            output=output,
        )
    return "## {title}\n\nQuestion: {query}\n\n```text\n{output}\n```".format(
        title=title,
        query=query,
        output=output,
    )


def generate_code_turn(session: LabSession, query: str) -> dict[str, Any]:
    prompt = build_turn_context(session, query)
    try:
        if os.environ.get("UNCHAINED_PYREPLAB_AGENT_CMD"):
            return _normalize_generated_turn(_run_command_agent(prompt, env_name="UNCHAINED_PYREPLAB_AGENT_CMD"), query)
        if os.environ.get("UNCHAINED_PYREPLAB_TRIAL_ENABLED"):
            return _normalize_generated_turn(_unchained_trial_json_agent("generation", prompt), query)
        if os.environ.get("OPENAI_API_KEY") and os.environ.get("OPENAI_MODEL"):
            return _normalize_generated_turn(_openai_json_agent(_generation_system_prompt(), prompt), query)
    except LabAgentError as exc:
        fallback = _normalize_generated_turn(_builtin_generate(prompt), query)
        notes = [str(item) for item in fallback.get("notes", []) if str(item).strip()]
        notes.append(
            "generation-agent-fallback:{message}".format(
                message=_compact_agent_error_message(str(exc))
            )
        )
        fallback["notes"] = notes
        return fallback
    return _normalize_generated_turn(_builtin_generate(prompt), query)


def summarize_turn(
    session: LabSession,
    *,
    query: str,
    generated: dict[str, Any],
    execution: dict[str, Any],
) -> dict[str, Any]:
    if execution.get("status") == "running":
        return _builtin_summarize(
            query,
            generated=generated,
            execution=execution,
        )
    prompt = {
        "context": build_turn_context(session, query),
        "generated": generated,
        "execution": execution,
    }
    try:
        if os.environ.get("UNCHAINED_PYREPLAB_SUMMARY_CMD"):
            return _run_command_agent(prompt, env_name="UNCHAINED_PYREPLAB_SUMMARY_CMD")
        if os.environ.get("UNCHAINED_PYREPLAB_AGENT_CMD"):
            return _run_command_agent(prompt, env_name="UNCHAINED_PYREPLAB_AGENT_CMD")
        if os.environ.get("UNCHAINED_PYREPLAB_TRIAL_ENABLED"):
            return _unchained_trial_json_agent("summary", prompt)
        if os.environ.get("OPENAI_API_KEY") and os.environ.get("OPENAI_MODEL"):
            return _openai_json_agent(_summary_system_prompt(), prompt)
    except LabAgentError as exc:
        fallback = _builtin_summarize(
            query,
            generated=generated,
            execution=execution,
        )
        message = _compact_agent_error_message(str(exc))
        markdown = str(fallback.get("markdown", "")).rstrip()
        fallback["markdown"] = (
            markdown
            + "\n\n_Caveat: external summary agent failed, so the reply used the local formatter instead ({message})._".format(
                message=message
            )
        )
        fallback["caveats"] = [message]
        return fallback
    return _builtin_summarize(
        query,
        generated=generated,
        execution=execution,
    )
