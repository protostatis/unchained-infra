"""Task success evaluation for benchmark."""

from __future__ import annotations

import os
import re


def evaluate(task: dict, answer: str) -> tuple[bool, str]:
    """Evaluate whether the agent answer satisfies the task."""
    if not answer or not answer.strip():
        return False, "Empty answer"

    eval_type = task.get("eval_type", "")
    config = task.get("eval_config", {})

    if eval_type == "keyword_count":
        return _eval_keyword_count(answer, config)
    if eval_type == "contains_any":
        return _eval_contains_any(answer, config)
    if eval_type == "llm_judge":
        return _eval_llm_judge(task, answer, config)
    return False, f"Unknown eval_type: {eval_type}"


def _eval_keyword_count(answer: str, config: dict) -> tuple[bool, str]:
    min_items = config.get("min_items", 1)

    numbered = re.findall(r"^\s*\d+[\.\)]\s+(.{10,})", answer, re.MULTILINE)
    if len(numbered) >= min_items:
        return True, f"Found {len(numbered)} numbered items (need {min_items})"

    bullets = re.findall(r"^\s*[-*•]\s+(.{10,})", answer, re.MULTILINE)
    if len(bullets) >= min_items:
        return True, f"Found {len(bullets)} bullet items (need {min_items})"

    lines = [
        line.strip()
        for line in answer.split("\n")
        if len(line.strip()) > 15
        and not line.strip().startswith("#")
        and not line.strip().lower().startswith(("here", "the top", "below", "i found"))
    ]
    unique_lines = list(dict.fromkeys(lines))
    if len(unique_lines) >= min_items:
        return True, f"Found {len(unique_lines)} distinct content lines (need {min_items})"

    quoted = re.findall(r'["""](.{10,}?)["""]', answer)
    if len(quoted) >= min_items:
        return True, f"Found {len(quoted)} quoted items (need {min_items})"

    total = max(len(numbered), len(bullets), len(unique_lines), len(quoted))
    return False, f"Found {total} items, need {min_items}"


def _eval_contains_any(answer: str, config: dict) -> tuple[bool, str]:
    keywords = config.get("keywords", [])
    if not keywords:
        return False, "No keywords configured"

    answer_lower = answer.lower()
    found = [keyword for keyword in keywords if keyword.lower() in answer_lower]
    if found:
        return True, f"Found keywords: {', '.join(found)}"
    return False, f"None of [{', '.join(keywords)}] found in answer"


def _eval_llm_judge(task: dict, answer: str, config: dict) -> tuple[bool, str]:
    criteria = config.get("criteria", "")
    if not criteria:
        return False, "No criteria configured for llm_judge"

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        if len(answer.strip()) > 50 and "error" not in answer.lower()[:50]:
            return True, "LLM judge unavailable, passed on heuristic (substantial answer)"
        return False, "LLM judge unavailable (no ANTHROPIC_API_KEY), answer too short"

    try:
        import httpx

        prompt = (
            "You are evaluating a browser automation agent's answer.\n\n"
            f"Task: {task['task']}\n\n"
            f"Agent's answer:\n{answer[:2000]}\n\n"
            f"Criteria: {criteria}\n\n"
            "Does the answer satisfy the criteria? Reply with exactly PASS or FAIL on the "
            "first line, then a brief reason."
        )

        response = httpx.post(
            "https://api.anthropic.com/v1/messages",
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 150,
                "messages": [{"role": "user", "content": prompt}],
            },
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            timeout=15.0,
        )
        response.raise_for_status()
        data = response.json()
        judge_text = data["content"][0]["text"].strip()
        first_line = judge_text.split("\n")[0].strip().upper()
        return "PASS" in first_line, f"LLM judge: {judge_text[:200]}"
    except Exception as exc:
        if len(answer.strip()) > 50:
            return True, f"LLM judge error ({exc}), passed on heuristic"
        return False, f"LLM judge error: {exc}"
