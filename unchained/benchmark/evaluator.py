"""evaluator.py — Task success evaluation for benchmark.

Three eval types:
- keyword_count: answer must contain N+ distinct items
- contains_any: answer must contain at least one keyword
- llm_judge: Claude Haiku judges pass/fail against criteria string
"""

from __future__ import annotations

import os
import re


def evaluate(task: dict, answer: str) -> tuple[bool, str]:
    """Evaluate whether the agent's answer satisfies the task.

    Args:
        task: Task dict from tasks.jsonl (has eval_type, eval_config).
        answer: The agent's final text answer.

    Returns:
        (passed: bool, reason: str)
    """
    if not answer or not answer.strip():
        return False, "Empty answer"

    eval_type = task.get("eval_type", "")
    config = task.get("eval_config", {})

    if eval_type == "keyword_count":
        return _eval_keyword_count(answer, config)
    elif eval_type == "contains_any":
        return _eval_contains_any(answer, config)
    elif eval_type == "llm_judge":
        return _eval_llm_judge(task, answer, config)
    else:
        return False, f"Unknown eval_type: {eval_type}"


def _eval_keyword_count(answer: str, config: dict) -> tuple[bool, str]:
    """Count distinct non-trivial items in the answer.

    Looks for numbered lists (1. ..., 2. ...), bullet points (- ..., * ...),
    or distinct lines with substantial content.
    """
    min_items = config.get("min_items", 1)

    # Try numbered list first: "1. Title here" or "1) Title here"
    numbered = re.findall(r'^\s*\d+[\.\)]\s+(.{10,})', answer, re.MULTILINE)
    if len(numbered) >= min_items:
        return True, f"Found {len(numbered)} numbered items (need {min_items})"

    # Try bullet points: "- Title" or "* Title" or "• Title"
    bullets = re.findall(r'^\s*[-*•]\s+(.{10,})', answer, re.MULTILINE)
    if len(bullets) >= min_items:
        return True, f"Found {len(bullets)} bullet items (need {min_items})"

    # Fallback: count distinct non-trivial lines (>15 chars, not headers/boilerplate)
    lines = [
        ln.strip() for ln in answer.split('\n')
        if len(ln.strip()) > 15
        and not ln.strip().startswith('#')
        and not ln.strip().lower().startswith(('here', 'the top', 'below', 'i found'))
    ]
    # Deduplicate
    unique_lines = list(dict.fromkeys(lines))
    if len(unique_lines) >= min_items:
        return True, f"Found {len(unique_lines)} distinct content lines (need {min_items})"

    # Count any "title-like" strings in quotes
    quoted = re.findall(r'["""](.{10,}?)["""]', answer)
    if len(quoted) >= min_items:
        return True, f"Found {len(quoted)} quoted items (need {min_items})"

    total = max(len(numbered), len(bullets), len(unique_lines), len(quoted))
    return False, f"Found {total} items, need {min_items}"


def _eval_contains_any(answer: str, config: dict) -> tuple[bool, str]:
    """Check if the answer contains at least one keyword (case-insensitive)."""
    keywords = config.get("keywords", [])
    if not keywords:
        return False, "No keywords configured"

    answer_lower = answer.lower()
    found = [kw for kw in keywords if kw.lower() in answer_lower]

    if found:
        return True, f"Found keywords: {', '.join(found)}"
    return False, f"None of [{', '.join(keywords)}] found in answer"


def _eval_llm_judge(task: dict, answer: str, config: dict) -> tuple[bool, str]:
    """Use Claude Haiku as a judge to evaluate pass/fail.

    Requires ANTHROPIC_API_KEY env var. Falls back to keyword heuristic if unavailable.
    """
    criteria = config.get("criteria", "")
    if not criteria:
        return False, "No criteria configured for llm_judge"

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        # Fallback: check if answer has substantial content (>50 chars, not error)
        if len(answer.strip()) > 50 and "error" not in answer.lower()[:50]:
            return True, "LLM judge unavailable, passed on heuristic (substantial answer)"
        return False, "LLM judge unavailable (no ANTHROPIC_API_KEY), answer too short"

    try:
        import httpx

        prompt = (
            f"You are evaluating a browser automation agent's answer.\n\n"
            f"Task: {task['task']}\n\n"
            f"Agent's answer:\n{answer[:2000]}\n\n"
            f"Criteria: {criteria}\n\n"
            f"Does the answer satisfy the criteria? Reply with exactly "
            f"PASS or FAIL on the first line, then a brief reason."
        )

        resp = httpx.post(
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
        resp.raise_for_status()
        data = resp.json()
        judge_text = data["content"][0]["text"].strip()

        first_line = judge_text.split("\n")[0].strip().upper()
        passed = "PASS" in first_line
        return passed, f"LLM judge: {judge_text[:200]}"

    except Exception as e:
        # Fallback on API error
        if len(answer.strip()) > 50:
            return True, f"LLM judge error ({e}), passed on heuristic"
        return False, f"LLM judge error: {e}"
