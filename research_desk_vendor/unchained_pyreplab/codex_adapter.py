from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .lab_agent import (
    _gather_qa_system_prompt,
    _generation_system_prompt,
    _mission_system_prompt,
    _summary_system_prompt,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

GENERATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "title": {"type": "string"},
        "intent": {"type": "string"},
        "code": {"type": "string"},
        "notes": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": ["title", "intent", "code", "notes"],
}

SUMMARY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "markdown": {"type": "string"},
        "status": {"type": "string"},
        "followups": {
            "type": "array",
            "items": {"type": "string"},
        },
        "caveats": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": ["markdown", "status", "followups", "caveats"],
}

MISSION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "objective": {"type": "string"},
        "questions": {"type": "array", "items": {"type": "string"}},
        "name": {"type": "string"},
        "description": {"type": "string"},
        "grain": {"type": "string"},
        "primary_key": {"type": "array", "items": {"type": "string"}},
        "measures": {"type": "array", "items": {"type": "string"}},
        "dimensions": {"type": "array", "items": {"type": "string"}},
        "required_columns": {"type": "array", "items": {"type": "string"}},
        "min_rows": {"type": "string"},
        "seed_urls": {"type": "array", "items": {"type": "string"}},
        "source_preferences": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "source_type": {"type": "string"},
                    "query_hint": {"type": "string"},
                    "site_hint": {"type": "string"},
                    "route_role": {"type": "string"},
                    "rationale": {"type": "string"},
                },
                "required": ["source_type", "query_hint", "site_hint", "route_role", "rationale"],
            },
        },
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "objective",
        "questions",
        "name",
        "description",
        "grain",
        "primary_key",
        "measures",
        "dimensions",
        "required_columns",
        "min_rows",
        "seed_urls",
        "source_preferences",
        "notes",
    ],
}

GATHER_QA_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "reviews": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "page_id": {"type": "string"},
                    "review_status": {"type": "string"},
                    "confidence": {"type": "number"},
                    "rationale": {"type": "string"},
                    "suggested_next_action": {"type": "string"},
                },
                "required": [
                    "page_id",
                    "review_status",
                    "confidence",
                    "rationale",
                    "suggested_next_action",
                ],
            },
        },
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["reviews", "notes"],
}


def _load_stdin_json() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        raise SystemExit("codex adapter expected JSON on stdin")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit("codex adapter received invalid JSON on stdin") from exc
    if not isinstance(payload, dict):
        raise SystemExit("codex adapter expected a JSON object on stdin")
    return payload


def _schema_for_mode(mode: str) -> dict[str, Any]:
    if mode == "generation":
        return GENERATION_SCHEMA
    if mode == "summary":
        return SUMMARY_SCHEMA
    if mode == "mission":
        return MISSION_SCHEMA
    if mode == "gather-qa":
        return GATHER_QA_SCHEMA
    raise ValueError("unknown mode: {mode}".format(mode=mode))


def _instruction_for_mode(mode: str) -> str:
    if mode == "generation":
        return _generation_system_prompt()
    if mode == "summary":
        return _summary_system_prompt()
    if mode == "mission":
        return _mission_system_prompt()
    if mode == "gather-qa":
        return _gather_qa_system_prompt()
    raise ValueError("unknown mode: {mode}".format(mode=mode))


def _prompt_for_mode(mode: str, payload: dict[str, Any]) -> str:
    return (
        "{instruction}\n\n"
        "Return only a JSON object that matches the provided output schema. "
        "Do not wrap the response in markdown fences. "
        "Do not include any explanatory prose outside the JSON object.\n\n"
        "{schema_hint}\n\n"
        "Context JSON:\n"
        "{payload}\n"
    ).format(
        instruction=_instruction_for_mode(mode),
        schema_hint=(
            "Always include `notes` as an array, even if it is empty."
            if mode == "generation"
            else (
                "Always include `status` as a string and `followups` / `caveats` as arrays, even if they are empty."
                if mode == "summary"
                else (
                    "Always include `questions`, `primary_key`, `measures`, `dimensions`, `required_columns`, `seed_urls`, `source_preferences`, and `notes` as arrays, even if they are empty. Always include `min_rows` as a string."
                    if mode == "mission"
                    else "Always include `reviews` and `notes` as arrays, even if they are empty."
                )
            )
        ),
        payload=json.dumps(payload, indent=2, ensure_ascii=True),
    )


def _build_codex_command(
    *,
    mode: str,
    schema_path: Path,
    output_path: Path,
    sandbox: str,
) -> list[str]:
    command = [
        "codex",
        "exec",
        "-",
        "--cd",
        str(REPO_ROOT),
        "--sandbox",
        sandbox,
        "--output-schema",
        str(schema_path),
        "--output-last-message",
        str(output_path),
        "--color",
        "never",
        "--ephemeral",
    ]
    profile = os.environ.get("UNCHAINED_PYREPLAB_CODEX_PROFILE", "").strip()
    if profile:
        command.extend(["--profile", profile])
    if mode == "summary":
        model = os.environ.get("UNCHAINED_PYREPLAB_CODEX_SUMMARY_MODEL", "").strip()
    elif mode == "mission":
        model = os.environ.get("UNCHAINED_PYREPLAB_CODEX_MISSION_MODEL", "").strip()
    elif mode == "gather-qa":
        model = os.environ.get("UNCHAINED_PYREPLAB_CODEX_GATHER_QA_MODEL", "").strip()
    else:
        model = os.environ.get("UNCHAINED_PYREPLAB_CODEX_GENERATION_MODEL", "").strip()
    if not model:
        model = os.environ.get("UNCHAINED_PYREPLAB_CODEX_MODEL", "").strip()
    if model:
        command.extend(["--model", model])
    extra_cd = os.environ.get("UNCHAINED_PYREPLAB_CODEX_ADD_DIR", "").strip()
    if extra_cd:
        command.extend(["--add-dir", extra_cd])
    if mode not in {"generation", "summary", "mission", "gather-qa"}:
        raise ValueError("unknown mode: {mode}".format(mode=mode))
    return command


def run_codex_adapter(mode: str, *, sandbox: str = "read-only") -> dict[str, Any]:
    payload = _load_stdin_json()
    prompt = _prompt_for_mode(mode, payload)
    schema = _schema_for_mode(mode)
    with tempfile.TemporaryDirectory(prefix="codex-lab-") as tmpdir:
        tmp_path = Path(tmpdir)
        schema_path = tmp_path / "{mode}.schema.json".format(mode=mode)
        output_path = tmp_path / "{mode}.output.json".format(mode=mode)
        schema_path.write_text(json.dumps(schema, indent=2, ensure_ascii=True), encoding="utf-8")
        command = _build_codex_command(
            mode=mode,
            schema_path=schema_path,
            output_path=output_path,
            sandbox=sandbox,
        )
        proc = subprocess.run(
            command,
            input=prompt,
            text=True,
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            stdout = (proc.stdout or "").strip()
            detail = stderr or stdout or "unknown error"
            raise SystemExit("codex exec failed: {detail}".format(detail=detail))
        try:
            result = json.loads(output_path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise SystemExit("codex exec did not write an output file") from exc
        except json.JSONDecodeError as exc:
            raise SystemExit("codex exec returned non-JSON output") from exc
    if not isinstance(result, dict):
        raise SystemExit("codex exec returned a non-object payload")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bridge unchained pyreplab agent calls to codex exec.")
    parser.add_argument("mode", choices=["generation", "summary", "mission", "gather-qa"])
    parser.add_argument(
        "--sandbox",
        default=os.environ.get("UNCHAINED_PYREPLAB_CODEX_SANDBOX", "read-only"),
        choices=["read-only", "workspace-write", "danger-full-access"],
        help="Sandbox passed to codex exec.",
    )
    args = parser.parse_args(argv)
    result = run_codex_adapter(args.mode, sandbox=args.sandbox)
    json.dump(result, sys.stdout, ensure_ascii=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
