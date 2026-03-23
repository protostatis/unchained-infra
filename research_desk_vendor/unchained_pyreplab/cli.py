from __future__ import annotations

import argparse
import base64
import csv
import getpass
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import webbrowser
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha1
from math import ceil
from multiprocessing import get_context
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, quote_plus, urlparse

from .analysis_runtime import build_district_metrics
from .lab_agent import LabAgentError, generate_code_turn, plan_mission, review_gather_qa, summarize_turn
from .lab_session import get_session
from .mcp_client import (
    DEFAULT_ENDPOINT,
    DEFAULT_AGENT_ENV_PATH,
    MCPClient,
    MCPError,
    extract_text,
    infer_agents_endpoint,
    parse_env_file,
    parse_json_if_possible,
    ResolvedCredentials,
    resolve_credentials,
)
from .planning import (
    RECIPE_GENERIC,
    RECIPE_HIGHSCHOOL,
    _domain_from_url,
    _looks_like_marketplace_listing_task,
    _is_search_engine_url,
    _normalize_domain,
    _normalize_space,
    build_analysis_plan,
    build_capsule_state,
    build_capture_brief,
    build_object_decision_review,
    build_gather_qa,
    build_gather_targets,
    build_object_manifest,
    build_primary_object_shape_artifact,
    build_readiness,
    build_row_schema,
    build_schema_refinement,
    build_scout_index,
    build_scout_summary,
    build_schema_summary,
    build_source_plan,
    build_source_index,
    build_task_spec,
    infer_recipe,
    mission_plan_is_low_information,
    normalize_mission_plan,
    render_analysis,
    summarize_gather_qa,
)
from .reload_control import is_reload_paused
from .scoring import score_highschool_districts

REPO_ROOT = Path(__file__).resolve().parent.parent
CAPSULES_ROOT = REPO_ROOT / "capsules"
DEFAULT_GATHER_PARALLEL_TABS = 10
MAX_GATHER_PARALLEL_TABS = 10
DEFAULT_GATHER_SCROLL_MOVES = 10
MAX_GATHER_SCROLL_MOVES = 10
DEFAULT_GATHER_AUTO_RECOVERY_WAVES = 3
RELOAD_WATCH_ROOTS = [REPO_ROOT / "unchained_pyreplab", REPO_ROOT / "tests"]
SETUP_CONFIG_VERSION = 1
SETUP_PROVIDER_CHOICES = ("trial", "claude", "codex", "openai", "builtin")
DEFAULT_ISOLATED_BRIDGE_PROFILE = "research_desk"
DEFAULT_ISOLATED_BRIDGE_PORT = 9333
DEFAULT_ISOLATED_BRIDGE_RELAY = "wss://api.unchainedsky.com/tunnel"
DEFAULT_ISOLATED_BRIDGE_DATA_DIR = str(Path.home() / ".unchained" / "research-desk-bridge")
SETUP_PROVIDER_CATALOG: dict[str, dict[str, str]] = {
    "trial": {
        "label": "Trial Agent",
        "tagline": "Included with your Unchained key",
        "description": "Uses a small Unchained-hosted trial agent backed by included trial credit. Best default for first-run because it avoids separate model setup.",
    },
    "claude": {
        "label": "Claude Code CLI",
        "tagline": "Browser-side client",
        "description": "Best if you already use Claude Code/Desktop and want Unchained MCP wired into that workflow. Research Desk keeps the built-in notebook agent unless a Claude command adapter is configured later.",
    },
    "codex": {
        "label": "Codex CLI",
        "tagline": "Best current full local-agent fit",
        "description": "Uses local Codex command mode for Mission, Lab Notes, Summary, and Gather QA. Best if you already use Codex on this machine.",
    },
    "openai": {
        "label": "OpenAI API key",
        "tagline": "Direct model control",
        "description": "Uses direct API calls for Mission, Lab Notes, Summary, and Gather QA. Best if you want explicit model control and have an API key ready.",
    },
    "builtin": {
        "label": "Built-in",
        "tagline": "Smoke-test mode",
        "description": "No external LLM setup. Good for local smoke testing, but weaker than the provider-backed modes.",
    },
}

PREFERRED_NAVIGATE_TOOLS = ("cdp_navigate", "navigate")
PREFERRED_JS_TOOLS = ("js_eval", "execute_js")
PREFERRED_DDM_TOOLS = ("ddm",)
PREFERRED_INTEL_PROBE_TOOLS = ("intel_probe",)
PREFERRED_INTEL_EXTRACT_TOOLS = ("intel_extract",)
PREFERRED_SCREENSHOT_TOOLS = ("cdp_screenshot", "screenshot")

NOVELTY_PROMPT_TEMPLATES: dict[str, list[dict[str, Any]]] = {
    "products": [
        {"label": "induction-cooktops", "prompt": "I want to compare 18 portable induction cooktops for a dorm room under 120 dollars with the best reviews", "expected_object": "products"},
        {"label": "carry-on-suitcases", "prompt": "I want to compare 24 carry-on suitcases for frequent business travel under 220 dollars with the best reviews", "expected_object": "products"},
        {"label": "noise-machines", "prompt": "I want to compare 20 noise machines for a nursery under 70 dollars with the best reviews", "expected_object": "products"},
        {"label": "dehumidifiers", "prompt": "I want to compare 18 compact dehumidifiers for a studio apartment under 180 dollars with the best reviews", "expected_object": "products"},
        {"label": "desk-fans", "prompt": "I want to compare 16 quiet desk fans for a small home office under 90 dollars with the best reviews", "expected_object": "products"},
        {"label": "mirrorless-cameras", "prompt": "Compare compact mirrorless cameras under 1200 dollars for travel photography and strong reviews.", "expected_object": "products"},
        {"label": "office-chairs", "prompt": "Compare ergonomic office chairs under 350 dollars for long workdays with the best reviews.", "expected_object": "products"},
    ],
    "vehicle_listings": [
        {"label": "used-hatchback-43215", "prompt": "I want to compare the best used hatchback near 43215", "expected_object": "vehicle_listings"},
        {"label": "used-hybrid-suv-98109", "prompt": "I want to compare the best used hybrid suv near 98109", "expected_object": "vehicle_listings"},
        {"label": "used-cargo-van-30318", "prompt": "I want to compare the best used cargo van near 30318", "expected_object": "vehicle_listings"},
        {"label": "used-compact-pickup-85016", "prompt": "I want to compare the best used compact pickup near 85016", "expected_object": "vehicle_listings"},
        {"label": "used-toyota-sienna-60657", "prompt": "Compare used Toyota Sienna listings near 60657 and surface the strongest value listings.", "expected_object": "vehicle_listings"},
    ],
    "mattress_listings": [
        {"label": "used-queen-mattress-60614", "prompt": "I want to compare the best used queen mattress near 60614", "expected_object": "mattress_listings"},
        {"label": "used-twin-mattress-75204", "prompt": "I want to compare the best used twin mattress near 75204", "expected_object": "mattress_listings"},
    ],
    "land_listings": [
        {"label": "boise-land-150k", "prompt": "Compare buildable land listings near Boise under 150k and find the best values.", "expected_object": "land_listings"},
        {"label": "mokena-land", "prompt": "Find land listings near Mokena IL and compare current asking prices.", "expected_object": "land_listings"},
    ],
    "rental_listings": [
        {"label": "lakeview-rentals", "prompt": "Explore apartments for rent in the Lakeview area and analyze the current price range in the area.", "expected_object": "rental_listings"},
        {"label": "phoenix-rentals", "prompt": "Compare rental listings in central Phoenix for two-bedroom apartments under 2200 dollars.", "expected_object": "rental_listings"},
    ],
    "home_sale_signals": [
        {"label": "phoenix-home-cooling", "prompt": "Find neighborhoods in Phoenix where home prices have cooled the most.", "expected_object": "home_sale_signals"},
        {"label": "atlanta-home-cooling", "prompt": "Compare which Atlanta neighborhoods have seen the sharpest home price drops recently.", "expected_object": "home_sale_signals"},
    ],
    "restaurants": [
        {"label": "ramen-restaurants-chicago", "prompt": "I want to compare ramen restaurants in Chicago by price tier versus the reviews they receive", "expected_object": "restaurants"},
        {"label": "taco-restaurants-austin", "prompt": "I want to compare taco restaurants in Austin by price tier versus the reviews they receive", "expected_object": "restaurants"},
        {"label": "burger-restaurants-phoenix", "prompt": "Compare burger restaurants in Phoenix by price tier versus review volume.", "expected_object": "restaurants"},
    ],
    "restaurant_chains": [
        {"label": "taco-franchise-us", "prompt": "I want to find out what is the best reviewed taco franchise around the country in US", "expected_object": "restaurant_chains"},
        {"label": "coffee-chain-us", "prompt": "I want to find out what is the best reviewed coffee chain around the country in US", "expected_object": "restaurant_chains"},
        {"label": "pizza-chain-us", "prompt": "I want to find out what is the best reviewed pizza chain around the country in US", "expected_object": "restaurant_chains"},
    ],
    "coworking_spaces": [
        {"label": "coworking-austin", "prompt": "Find the best coworking spaces in Austin by monthly desk price and review volume.", "expected_object": "coworking_spaces"},
        {"label": "coworking-denver", "prompt": "Compare coworking spaces in Denver by monthly desk price and review volume.", "expected_object": "coworking_spaces"},
    ],
    "stock_candidates": [
        {"label": "sp500-books", "prompt": "Evaluate S&P 500 top 50 stocks and analyze their books.", "expected_object": "stock_candidates"},
        {"label": "best-stock-2026", "prompt": "Find me the best stock to invest in 2026.", "expected_object": "stock_candidates"},
        {"label": "ai-infra-large-caps", "prompt": "Find the strongest large-cap stocks for 2026 using AI infrastructure demand, photonics exposure, and balance-sheet quality.", "expected_object": "stock_candidates"},
    ],
    "market_contracts": [
        {"label": "interest-rate-markets", "prompt": "Analyze active prediction market events about interest rates and compare the odds.", "expected_object": "market_contracts"},
        {"label": "latest-polymarket-bets", "prompt": "Can you analyze the latest Polymarket bets?", "expected_object": "market_contracts"},
        {"label": "crypto-market-odds", "prompt": "Analyze active prediction market events about Bitcoin, Ethereum, and stablecoin regulation and compare the odds.", "expected_object": "market_contracts"},
        {"label": "nfl-market-odds", "prompt": "Analyze active prediction market events about NFL free agency, the draft, and quarterback movement and compare the odds.", "expected_object": "market_contracts"},
    ],
}


class NoveltyStepTimeout(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _run_with_timeout(seconds: int, fn: Any, *args: Any, **kwargs: Any) -> Any:
    timeout_seconds = max(0, int(seconds or 0))
    if timeout_seconds <= 0:
        return fn(*args, **kwargs)
    ctx = get_context("spawn")
    queue: Any = ctx.Queue()
    process = ctx.Process(target=_run_with_timeout_child, args=(queue, fn, args, kwargs))
    process.start()
    process.join(float(timeout_seconds))
    if process.is_alive():
        process.terminate()
        process.join(timeout=3.0)
        raise NoveltyStepTimeout("timed out after {seconds}s".format(seconds=timeout_seconds))
    if queue.empty():
        if process.exitcode not in (0, None):
            raise RuntimeError("child process exited with code {code}".format(code=process.exitcode))
        return None
    status, payload = queue.get()
    if status == "ok":
        return payload
    if status == "system_exit":
        raise SystemExit(payload)
    if status == "exception":
        exc_name = str(payload.get("type", "RuntimeError"))
        message = str(payload.get("message", "")).strip()
        trace = str(payload.get("traceback", "")).strip()
        if trace:
            raise RuntimeError("{name}: {message}\n{trace}".format(name=exc_name, message=message, trace=trace))
        raise RuntimeError("{name}: {message}".format(name=exc_name, message=message))
    return None


def _run_with_timeout_child(queue: Any, fn: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
    try:
        result = fn(*args, **kwargs)
        queue.put(("ok", result))
    except SystemExit as exc:
        message = str(exc).strip()
        if not message and getattr(exc, "code", None) not in (None, 0):
            message = str(exc.code)
        queue.put(("system_exit", message or "SystemExit"))
    except BaseException as exc:  # noqa: BLE001
        queue.put(
            (
                "exception",
                {
                    "type": exc.__class__.__name__,
                    "message": str(exc),
                    "traceback": traceback.format_exc(),
                },
            )
        )


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or f"capsule-{int(time.time())}"


def _parse_ddm_page_metrics(ddm_text: str) -> tuple[int, int]:
    match = re.search(r"viewport:\s*(\d+)px\s*\|\s*page:\s*(\d+)px", ddm_text)
    if not match:
        return 0, 0
    return int(match.group(1)), int(match.group(2))


def _merge_page_text_segments(segments: list[str]) -> str:
    merged: list[str] = []
    seen: set[str] = set()
    for segment in segments:
        clean = str(segment or "").strip()
        if not clean:
            continue
        compact = " ".join(clean.split())
        if compact in seen:
            continue
        seen.add(compact)
        merged.append(clean)
    return "\n\n".join(merged)


def _scroll_fractions(move_count: int) -> list[float]:
    count = max(0, int(move_count or 0))
    if count <= 0:
        return []
    if count == 1:
        return [0.85]
    start = 0.12
    end = 0.96
    step = (end - start) / max(count - 1, 1)
    return [round(start + step * index, 4) for index in range(count)]


def _should_collect_scroll_slices(
    ddm_text: str,
    page_text: str,
    text_max: int,
    *,
    scroll_moves: int = 2,
) -> bool:
    viewport_height, page_height = _parse_ddm_page_metrics(ddm_text)
    if not viewport_height or not page_height:
        return False
    if scroll_moves <= 0:
        return False
    if page_height < max(int(viewport_height * 1.5), viewport_height + 320):
        return False
    if scroll_moves >= DEFAULT_GATHER_SCROLL_MOVES:
        return True
    return len(page_text.strip()) < max(2500, int(text_max * 0.85))


def _scroll_capture_supplemental_text(
    *,
    client: MCPClient,
    available_tools: list[str],
    agent_id: str,
    tab_id: str,
    text_max: int,
    scroll_moves: int,
) -> list[str]:
    supplemental: list[str] = []
    seen: set[str] = set()
    stagnant_moves = 0
    for fraction in _scroll_fractions(scroll_moves):
        scroll_expression = (
            "(() => {"
            " const root = document.scrollingElement || document.documentElement || document.body;"
            " const target = Math.floor(root.scrollHeight * FRACTION);"
            " window.scrollTo({ top: target, behavior: 'instant' });"
            " return JSON.stringify({ scrollY: window.scrollY, scrollHeight: root.scrollHeight });"
            "})()"
        ).replace("FRACTION", str(fraction))
        call_tool_variants(
            client,
            select_tool_candidates(PREFERRED_JS_TOOLS, available_tools),
            [
                {"agent_id": agent_id, "expression": scroll_expression, "tab_id": tab_id},
                {"agent_id": agent_id, "expression": scroll_expression},
            ],
            "scroll page",
        )
        time.sleep(0.65)
        _, page_text_result = call_tool_variants(
            client,
            select_tool_candidates(PREFERRED_DDM_TOOLS, available_tools),
            [
                {"agent_id": agent_id, "flags": f"--text --max {text_max}", "tab_id": tab_id},
                {"agent_id": agent_id, "flags": f"--text --max {text_max}"},
            ],
            "page text after scroll",
        )
        segment = extract_text(page_text_result)
        clean = " ".join(str(segment or "").split())
        if clean and clean not in seen:
            seen.add(clean)
            supplemental.append(segment)
            stagnant_moves = 0
        else:
            stagnant_moves += 1
            if stagnant_moves >= 2 and len(supplemental) >= min(4, max(1, scroll_moves // 2)):
                break
    return supplemental


def _gather_move_budget_profile(
    *,
    target_object: str,
    route_source_type: str,
    move_budget: int,
) -> dict[str, int]:
    budget = max(0, int(move_budget or 0))
    if budget <= 0:
        return {"scroll_moves": 0, "load_more_attempts": 0, "pagination_hops": 0}
    if route_source_type == "detail_followup":
        scroll_moves = min(4, budget)
        load_more_attempts = 1 if budget >= 6 and target_object in {"market_contracts", "products"} else 0
        pagination_hops = 0
    elif target_object in {
        "products",
        "rental_listings",
        "vehicle_listings",
        "land_listings",
        "coworking_spaces",
        "market_contracts",
    }:
        scroll_moves = min(6, budget)
        remaining = max(0, budget - scroll_moves)
        load_more_attempts = min(2, max(0, remaining // 2))
        remaining = max(0, remaining - load_more_attempts)
        pagination_hops = min(2, remaining)
    else:
        scroll_moves = min(5, budget)
        remaining = max(0, budget - scroll_moves)
        load_more_attempts = 1 if remaining >= 2 else 0
        remaining = max(0, remaining - load_more_attempts)
        pagination_hops = min(1, remaining)
    return {
        "scroll_moves": scroll_moves,
        "load_more_attempts": load_more_attempts,
        "pagination_hops": pagination_hops,
    }


def _gather_page_signal_score(
    *,
    target_object: str,
    route_source_type: str,
    title: str,
    url: str,
    page_text: str,
) -> int:
    text = _merge_page_text_segments([title, url, page_text]).lower()
    score = 0
    if any(token in text for token in ("404", "page not found", "access denied", "captcha", "sign in", "login")):
        score -= 80
    if target_object == "market_contracts":
        if any(token in text for token in ("yes", "no", "volume", "liquidity", "prediction market", "active markets")):
            score += 28
        if any(token in text for token in ("guide", "what is", "explained", "strategy", "blog", "article")):
            score -= 28
    elif target_object == "rental_listings":
        if any(token in text for token in ("bed", "bath", "$", "sq ft", "available", "apartments for rent")):
            score += 24
        if any(token in text for token in ("guide", "moving", "neighborhood guide", "market report", "news")):
            score -= 24
    elif target_object == "coworking_spaces":
        if any(token in text for token in ("dedicated desk", "hot desk", "private office", "month", "/month", "coworking")):
            score += 22
        if any(token in text for token in ("blog", "resources", "latest posts", "news")):
            score -= 24
    elif target_object == "home_sale_signals":
        if any(token in text for token in ("price cut", "home value", "median sale", "market temperature", "zestimate")):
            score += 24
        if any(token in text for token in ("how to buy", "guide", "listing detail", "tour")):
            score -= 24
    elif target_object == "stock_candidates":
        if any(token in text for token in ("ticker", "analyst", "top stocks", "stocks to buy", "buy-rated", "picks")):
            score += 22
        if any(token in text for token in ("etf", "index", "tracker", "market outlook", "screener")):
            score -= 28
    elif target_object == "products":
        if any(token in text for token in ("add to cart", "reviews", "stars", "$", "buy now", "rating")):
            score += 18
        if any(token in text for token in ("top 10", "best products", "roundup", "blog", "guide")):
            score -= 22
    if route_source_type == "detail_followup":
        score += 10
    return score


def _refine_gather_move_profile(
    *,
    base_profile: dict[str, int],
    target_object: str,
    route_source_type: str,
    title: str,
    url: str,
    page_text: str,
) -> tuple[dict[str, int], int]:
    score = _gather_page_signal_score(
        target_object=target_object,
        route_source_type=route_source_type,
        title=title,
        url=url,
        page_text=page_text,
    )
    profile = dict(base_profile)
    total_budget = max(0, sum(int(profile.get(key, 0) or 0) for key in ("scroll_moves", "load_more_attempts", "pagination_hops")))
    if score <= -20:
        profile["scroll_moves"] = min(profile.get("scroll_moves", 0), 2)
        profile["load_more_attempts"] = 0
        profile["pagination_hops"] = 0
    elif score <= 5:
        profile["scroll_moves"] = min(profile.get("scroll_moves", 0), 4)
        profile["load_more_attempts"] = min(profile.get("load_more_attempts", 0), 1)
        profile["pagination_hops"] = min(profile.get("pagination_hops", 0), 1)
    elif (
        score >= 24
        and route_source_type != "detail_followup"
        and target_object
        in {
            "products",
            "rental_listings",
            "vehicle_listings",
            "land_listings",
            "coworking_spaces",
            "market_contracts",
        }
    ):
        # High-yield grid/board pages should spend more of the fixed budget on reveals and next-page hops,
        # because that is where additional rows usually live.
        scroll_moves = max(3, min(profile.get("scroll_moves", 0), 4))
        desired_pagination = 4 if total_budget >= 8 else 2
        pagination_hops = min(4, max(profile.get("pagination_hops", 0), desired_pagination))
        load_more_attempts = min(2, max(profile.get("load_more_attempts", 0), 1))
        used_budget = scroll_moves + pagination_hops + load_more_attempts
        if total_budget and used_budget > total_budget:
            overflow = used_budget - total_budget
            reduce_scroll = min(overflow, max(0, scroll_moves - 3))
            scroll_moves -= reduce_scroll
            overflow -= reduce_scroll
            if overflow > 0:
                reduce_pagination = min(overflow, max(0, pagination_hops - 2))
                pagination_hops -= reduce_pagination
                overflow -= reduce_pagination
            if overflow > 0:
                load_more_attempts = max(0, load_more_attempts - overflow)
        profile["scroll_moves"] = scroll_moves
        profile["load_more_attempts"] = load_more_attempts
        profile["pagination_hops"] = pagination_hops
    return profile, score


def _progressive_reveal_expression() -> str:
    return (
        "(() => {"
        " const patterns = ['load more', 'show more', 'see more', 'more results', 'view more'];"
        " const nodes = Array.from(document.querySelectorAll('button, [role=\"button\"], a[href]'));"
        " for (const node of nodes) {"
        "   const text = (node.innerText || node.getAttribute('aria-label') || node.title || '').trim().toLowerCase();"
        "   if (!text) continue;"
        "   if (!patterns.some(pattern => text.includes(pattern))) continue;"
        "   const rect = node.getBoundingClientRect();"
        "   if (rect.width < 4 || rect.height < 4) continue;"
        "   node.click();"
        "   return JSON.stringify({clicked: true, label: text.slice(0, 120)});"
        " }"
        " return JSON.stringify({clicked: false});"
        "})()"
    )


def _structured_dom_lines_expression() -> str:
    return (
        "(() => {"
        " const selectors = ['a[href]', 'button', '[role=\"button\"]', 'article', 'li', 'tr', '[data-testid]'];"
        " const nodes = Array.from(document.querySelectorAll(selectors.join(',')));"
        " const rows = [];"
        " const seen = new Set();"
        " for (const node of nodes) {"
        "   const text = (node.innerText || '').replace(/\\s+/g, ' ').trim();"
        "   if (text.length < 12 || text.length > 260) continue;"
        "   if (seen.has(text)) continue;"
        "   seen.add(text);"
        "   rows.push(text);"
        "   if (rows.length >= 160) break;"
        " }"
        " return rows;"
        "})()"
    )


def _pagination_links_expression() -> str:
    return (
        "(() => {"
        " const anchors = Array.from(document.querySelectorAll('a[href]'));"
        " const rows = [];"
        " for (const anchor of anchors) {"
        "   const href = anchor.href || '';"
        "   if (!href.startsWith('http')) continue;"
        "   const text = (anchor.innerText || anchor.getAttribute('aria-label') || anchor.title || '').trim().split('\\n')[0].trim();"
        "   rows.push({href, title: text});"
        "   if (rows.length >= 120) break;"
        " }"
        " return rows;"
        "})()"
    )


def _score_pagination_candidate(
    *,
    href: str,
    title: str,
    current_url: str,
    site_hint: str,
) -> int:
    clean_href = str(href or "").strip()
    if not clean_href or clean_href == current_url:
        return -100
    if not clean_href.startswith(("http://", "https://")):
        return -100
    if _is_search_engine_url(clean_href):
        return -100
    if site_hint and not _domain_matches_site_hint(clean_href, site_hint):
        return -100
    lower_title = str(title or "").strip().lower()
    lower_href = clean_href.lower()
    score = 0
    if re.search(r"\b(next|more results|next page|page \d+)\b", lower_title):
        score += 30
    if any(token in lower_href for token in ("page=", "p=", "pg=", "offset=", "start=")):
        score += 18
    if any(token in lower_href for token in ("/page/", "/search", "/all", "/results")):
        score += 10
    if lower_href.rstrip("/") == current_url.rstrip("/"):
        score -= 30
    return score


def _find_pagination_url(
    *,
    client: MCPClient,
    available_tools: list[str],
    agent_id: str,
    tab_id: str,
    current_url: str,
    site_hint: str,
) -> str:
    _, js_result = call_tool_variants(
        client,
        select_tool_candidates(PREFERRED_JS_TOOLS, available_tools),
        [
            {"agent_id": agent_id, "expression": _pagination_links_expression(), "tab_id": tab_id},
            {"agent_id": agent_id, "expression": _pagination_links_expression()},
        ],
        "pagination links",
    )
    parsed = parse_json_if_possible(extract_text(js_result))
    if not isinstance(parsed, list):
        return ""
    best_match: tuple[int, str] | None = None
    for item in parsed:
        if not isinstance(item, dict):
            continue
        href = str(item.get("href", "")).strip()
        title = str(item.get("title", "")).strip()
        score = _score_pagination_candidate(
            href=href,
            title=title,
            current_url=current_url,
            site_hint=site_hint,
        )
        if best_match is None or score > best_match[0]:
            best_match = (score, href)
    if best_match and best_match[0] >= 22:
        return best_match[1]
    return ""


def _filter_structured_dom_lines(target_object: str, rows: list[str]) -> list[str]:
    filtered: list[str] = []
    seen: set[str] = set()
    for raw in rows:
        clean = _normalize_space(str(raw or ""))
        lower = clean.lower()
        if not clean or clean in seen:
            continue
        if target_object == "market_contracts":
            if not (
                re.search(r"\b(?:yes|no)\b", lower)
                or "%" in clean
                or "volume" in lower
                or "liquidity" in lower
                or clean.endswith("?")
            ):
                continue
        elif target_object in {"products", "rental_listings", "vehicle_listings", "land_listings", "coworking_spaces"}:
            if not any(token in lower for token in ("$", "/month", "reviews", "beds", "baths", "miles", "sq ft", "desk", "office", "price")):
                continue
        seen.add(clean)
        filtered.append(clean)
        if len(filtered) >= 80:
            break
    return filtered


def _read_current_page_text(
    *,
    client: MCPClient,
    available_tools: list[str],
    agent_id: str,
    tab_id: str,
    text_max: int,
    label: str,
) -> str:
    _, page_text_result = call_tool_variants(
        client,
        select_tool_candidates(PREFERRED_DDM_TOOLS, available_tools),
        [
            {"agent_id": agent_id, "flags": f"--text --max {text_max}", "tab_id": tab_id},
            {"agent_id": agent_id, "flags": f"--text --max {text_max}"},
        ],
        label,
    )
    return extract_text(page_text_result)


def _read_structured_dom_lines(
    *,
    client: MCPClient,
    available_tools: list[str],
    agent_id: str,
    tab_id: str,
    target_object: str,
) -> list[str]:
    if not target_object:
        return []
    _, js_result = call_tool_variants(
        client,
        select_tool_candidates(PREFERRED_JS_TOOLS, available_tools),
        [
            {"agent_id": agent_id, "expression": _structured_dom_lines_expression(), "tab_id": tab_id},
            {"agent_id": agent_id, "expression": _structured_dom_lines_expression()},
        ],
        "structured dom lines",
    )
    parsed = parse_json_if_possible(extract_text(js_result))
    if not isinstance(parsed, list):
        return []
    return _filter_structured_dom_lines(target_object, [str(item) for item in parsed if isinstance(item, str)])


def _infer_intel_extract_strategy(probe_text: str) -> str:
    lower = str(probe_text or "").lower()
    if not lower:
        return ""
    if "react_fiber" in lower or "react fiber" in lower or "react" in lower:
        return "react_fiber"
    if "data_testid" in lower or "testid" in lower or "data-testid" in lower:
        return "data_testid"
    if "host_attrs" in lower or "host attrs" in lower or "shadow" in lower:
        return "host_attrs"
    if "js_global" in lower or "data store" in lower or "__next_data__" in lower or "__nuxt__" in lower:
        return ""
    return ""


def _structured_value_to_lines(value: Any) -> list[str]:
    lines: list[str] = []
    if isinstance(value, dict):
        scalars: list[str] = []
        for key, nested in value.items():
            if isinstance(nested, (str, int, float, bool)) and str(nested).strip():
                scalars.append(f"{key}: {nested}")
            else:
                lines.extend(_structured_value_to_lines(nested))
        if scalars:
            lines.append(" | ".join(scalars))
        return lines
    if isinstance(value, list):
        for item in value:
            lines.extend(_structured_value_to_lines(item))
        return lines
    if isinstance(value, (str, int, float, bool)):
        text = _normalize_space(str(value))
        if text:
            lines.append(text)
    return lines


def _read_intel_enriched_lines(
    *,
    client: MCPClient,
    available_tools: list[str],
    agent_id: str,
    tab_id: str,
    target_object: str,
) -> tuple[str, str, list[str]]:
    if not target_object:
        return "", "", []
    probe_candidates = select_tool_candidates(PREFERRED_INTEL_PROBE_TOOLS, available_tools)
    extract_candidates = select_tool_candidates(PREFERRED_INTEL_EXTRACT_TOOLS, available_tools)
    if not probe_candidates or not extract_candidates:
        return "", "", []

    _, probe_result = call_tool_variants(
        client,
        probe_candidates,
        [{"agent_id": agent_id, "tab_id": tab_id}, {"agent_id": agent_id}],
        "intel probe",
    )
    probe_text = extract_text(probe_result)
    strategy = _infer_intel_extract_strategy(probe_text)
    call_args = [{"agent_id": agent_id, "tab_id": tab_id}, {"agent_id": agent_id}]
    if strategy:
        call_args = [
            {"agent_id": agent_id, "tab_id": tab_id, "strategy": strategy},
            {"agent_id": agent_id, "strategy": strategy},
        ]
    _, extract_result = call_tool_variants(
        client,
        extract_candidates,
        call_args,
        "intel extract",
    )
    extract_text_output = extract_text(extract_result)
    extract_parsed = parse_json_if_possible(extract_text_output)
    lines = _filter_structured_dom_lines(target_object, _structured_value_to_lines(extract_parsed))
    return probe_text, extract_text_output, lines


def _query_market_platform_domain_hints(query_text: str) -> list[str]:
    clean = str(query_text or "").lower()
    matches: list[tuple[int, str]] = []
    platform_map = {
        "polymarket": "polymarket.com",
        "kalshi": "kalshi.com",
        "predictit": "predictit.org",
        "manifold": "manifold.markets",
        "metaculus": "metaculus.com",
    }
    for token, domain in platform_map.items():
        position = clean.find(token)
        if position >= 0:
            matches.append((position, domain))
    matches.sort(key=lambda item: item[0])
    hints: list[str] = []
    for _, domain in matches:
        if domain not in hints:
            hints.append(domain)
    return hints


def _query_focus_tokens(query_text: str) -> list[str]:
    noisy = {
        "active",
        "prediction",
        "predictions",
        "market",
        "markets",
        "odds",
        "volume",
        "current",
        "latest",
        "compare",
        "analyze",
        "rates",
    }
    platform_tokens = {"polymarket", "kalshi", "predictit", "manifold", "metaculus"}
    return [token for token in _tokenize_query_text(query_text) if token not in noisy and token not in platform_tokens]


def _query_focus_mismatch_penalty(query_text: str, candidate_text: str) -> int:
    query = str(query_text or "").lower()
    candidate = str(candidate_text or "").lower()
    sports_tokens = ("nfl", "quarterback", "draft", "free agency", "super bowl")
    crypto_tokens = ("bitcoin", "ethereum", "stablecoin", "crypto", "solana", "xrp")
    macro_tokens = ("interest", "fed", "inflation", "rates", "rate cuts")
    if any(token in query for token in macro_tokens) and any(token in candidate for token in ("sports", "nfl", "nba", "mlb")):
        return 28
    if any(token in query for token in sports_tokens) and any(token in candidate for token in ("inflation", "fed", "interest", "rates", "macro")):
        return 28
    if any(token in query for token in crypto_tokens) and any(token in candidate for token in ("sports", "nfl", "nba", "election", "macro")):
        return 24
    return 0


def _resolve_gather_scroll_moves(scroll_moves: Optional[int] = None) -> int:
    candidate = scroll_moves
    if candidate is None:
        raw = os.environ.get("UNCHAINED_PYREPLAB_GATHER_SCROLL_MOVES", "").strip()
        if raw:
            try:
                candidate = int(raw)
            except ValueError:
                candidate = DEFAULT_GATHER_SCROLL_MOVES
    if candidate is None:
        candidate = DEFAULT_GATHER_SCROLL_MOVES
    candidate = max(0, int(candidate))
    return min(candidate, MAX_GATHER_SCROLL_MOVES)


def setup_config_path() -> Path:
    configured = os.environ.get("UNCHAINED_PYREPLAB_CONFIG_PATH", "").strip()
    if configured:
        return Path(configured).expanduser()
    xdg_root = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg_root:
        return Path(xdg_root).expanduser() / "unchained-pyreplab" / "config.json"
    return Path.home() / ".config" / "unchained-pyreplab" / "config.json"


def load_setup_config() -> dict[str, Any]:
    path = setup_config_path()
    payload = read_json(path, {})
    if not isinstance(payload, dict):
        return {}
    return payload


def write_setup_config(payload: dict[str, Any]) -> Path:
    path = setup_config_path()
    write_json(path, payload)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def apply_setup_environment(config: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    payload = config if isinstance(config, dict) else load_setup_config()
    if not isinstance(payload, dict):
        return {}
    endpoint = str(payload.get("mcp_endpoint", "")).strip()
    pyreplab_bin = str(payload.get("pyreplab_bin", "")).strip()
    if endpoint and not os.environ.get("UNCHAINED_MCP_ENDPOINT"):
        os.environ["UNCHAINED_MCP_ENDPOINT"] = endpoint
    if pyreplab_bin and not os.environ.get("PYREPLAB_BIN"):
        os.environ["PYREPLAB_BIN"] = pyreplab_bin
    env_defaults = payload.get("env_defaults", {})
    if isinstance(env_defaults, dict):
        for key, value in env_defaults.items():
            if not isinstance(key, str):
                continue
            text_value = str(value or "").strip()
            if text_value and not os.environ.get(key):
                os.environ[key] = text_value
    return payload


def _setup_default_provider() -> str:
    config = load_setup_config()
    provider = str(config.get("provider", "")).strip().lower()
    if provider in SETUP_PROVIDER_CHOICES:
        return provider
    if os.environ.get("UNCHAINED_PYREPLAB_AGENT_CMD"):
        return "codex"
    if os.environ.get("OPENAI_API_KEY") and os.environ.get("OPENAI_MODEL"):
        return "openai"
    return "trial"


def _prompt_choice(prompt: str, choices: tuple[str, ...], *, default: str) -> str:
    options = "/".join(choice if choice != default else f"{choice}*" for choice in choices)
    while True:
        raw = input(f"{prompt} [{options}]: ").strip().lower()
        if not raw:
            return default
        if raw in choices:
            return raw
        print("Please choose one of: {choices}".format(choices=", ".join(choices)))


def _print_provider_catalog(*, default: str) -> None:
    print("Research Desk needs an LLM provider for planning and notebook assistance.")
    print("Choose the provider you want to use on this machine:\n")
    for provider in ("trial", "claude", "codex", "openai", "builtin"):
        details = SETUP_PROVIDER_CATALOG[provider]
        marker = " (default)" if provider == default else ""
        print("- {label}{marker}".format(label=details["label"], marker=marker))
        print("  {tagline}".format(tagline=details["tagline"]))
        print("  {description}".format(description=details["description"]))
    print("")


def _confirm(prompt: str, *, default: bool = True) -> bool:
    suffix = "Y/n" if default else "y/N"
    raw = input(f"{prompt} [{suffix}]: ").strip().lower()
    if not raw:
        return default
    return raw in {"y", "yes"}


def _run_browser_agent_installer() -> None:
    subprocess.run(
        ["/bin/sh", "-lc", "curl -fsSL https://api.unchainedsky.com/install.sh | bash"],
        check=True,
    )


def _provider_pack(
    provider: str,
    *,
    openai_api_key: str = "",
    openai_model: str = "",
) -> dict[str, Any]:
    provider = provider.strip().lower()
    if provider not in SETUP_PROVIDER_CHOICES:
        raise SystemExit("Unsupported provider: {provider}".format(provider=provider))
    if provider == "codex":
        return {
            "provider": "codex",
            "lab_provider_mode": "command",
            "browser_client": "codex",
            "provider_note": "Research Desk will use Codex command mode for Mission, Lab Notes, Summary, and Gather QA.",
            "env_defaults": {
                "UNCHAINED_PYREPLAB_AGENT_CMD": "uv run unchained-pyreplab-codex-agent generation",
                "UNCHAINED_PYREPLAB_SUMMARY_CMD": "uv run unchained-pyreplab-codex-agent summary",
                "UNCHAINED_PYREPLAB_MISSION_CMD": "uv run unchained-pyreplab-codex-agent mission",
                "UNCHAINED_PYREPLAB_GATHER_QA_CMD": "uv run unchained-pyreplab-codex-agent gather-qa",
                "UNCHAINED_PYREPLAB_CODEX_MODEL": "gpt-5.3-codex",
                "UNCHAINED_PYREPLAB_CODEX_GENERATION_MODEL": "gpt-5.3-codex",
                "UNCHAINED_PYREPLAB_CODEX_SUMMARY_MODEL": "gpt-5.3-codex",
                "UNCHAINED_PYREPLAB_CODEX_MISSION_MODEL": "gpt-5.4",
                "UNCHAINED_PYREPLAB_CODEX_GATHER_QA_MODEL": "gpt-5.3-codex",
                "UNCHAINED_PYREPLAB_CODEX_SANDBOX": "read-only",
                "UNCHAINED_PYREPLAB_AGENT_TIMEOUT_SECONDS": "45",
                "UNCHAINED_PYREPLAB_SUMMARY_TIMEOUT_SECONDS": "12",
                "UNCHAINED_PYREPLAB_MISSION_TIMEOUT_SECONDS": "18",
                "UNCHAINED_PYREPLAB_GATHER_QA_TIMEOUT_SECONDS": "18",
            },
        }
    if provider == "trial":
        return {
            "provider": "trial",
            "lab_provider_mode": "trial",
            "browser_client": "included",
            "provider_note": "Research Desk will use the included Unchained-hosted trial agent and spend from the trial credit tied to your Unchained key.",
            "env_defaults": {
                "UNCHAINED_PYREPLAB_TRIAL_ENABLED": "1",
            },
        }
    if provider == "openai":
        api_key = openai_api_key.strip()
        model = openai_model.strip() or "gpt-5.4"
        if not api_key:
            raise SystemExit("OpenAI setup requires an API key. Pass --openai-api-key or run setup interactively.")
        return {
            "provider": "openai",
            "lab_provider_mode": "openai",
            "browser_client": "other",
            "provider_note": "Research Desk will call the OpenAI Responses API directly for Mission, Lab Notes, Summary, and Gather QA.",
            "env_defaults": {
                "OPENAI_API_KEY": api_key,
                "OPENAI_MODEL": model,
            },
        }
    if provider == "claude":
        return {
            "provider": "claude",
            "lab_provider_mode": "builtin",
            "browser_client": "claude-code",
            "provider_note": "Claude is saved as your preferred browser-side client, but Research Desk will use the built-in local notebook agent until a Claude command adapter is added.",
            "env_defaults": {},
        }
    return {
        "provider": "builtin",
        "lab_provider_mode": "builtin",
        "browser_client": "other",
        "provider_note": "Research Desk will use the built-in local notebook agent.",
        "env_defaults": {},
    }


def _provider_status(provider: str) -> dict[str, Any]:
    provider = provider.strip().lower()
    warnings: list[str] = []
    checks: list[str] = []
    if provider == "codex":
        if shutil.which("codex"):
            checks.append("codex_cli=found")
        else:
            warnings.append("codex CLI not found in PATH")
    elif provider == "trial":
        checks.append("trial_agent=unchained-hosted")
    elif provider == "claude":
        if shutil.which("claude"):
            checks.append("claude_cli=found")
        else:
            warnings.append("claude CLI not found in PATH")
    elif provider == "openai":
        checks.append("openai_api=configured")
    else:
        checks.append("builtin_agent=ready")
    return {"checks": checks, "warnings": warnings}


def _browser_setup_status(endpoint: str, timeout: int) -> dict[str, Any]:
    env_values = parse_env_file(DEFAULT_AGENT_ENV_PATH)
    installed = DEFAULT_AGENT_ENV_PATH.exists() and bool(env_values.get("UNCHAINED_API_KEY"))
    resolved = resolve_credentials(
        api_key=None,
        agent_id=None,
        endpoint=endpoint,
        timeout=timeout,
    )
    running = bool(resolved.api_key and resolved.agent_id)
    return {
        "installed": installed,
        "running": running,
        "api_key": resolved.api_key or "",
        "agent_id": resolved.agent_id or "",
        "agent_env_path": str(DEFAULT_AGENT_ENV_PATH),
        "agent_install_dir": str(DEFAULT_AGENT_ENV_PATH.parent),
        "endpoint": endpoint,
        "credential_source": resolved.source,
    }


def _isolated_bridge_defaults(config: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    payload = config if isinstance(config, dict) else load_setup_config()
    return {
        "mode": str(payload.get("browser_bridge_mode", "isolated")).strip() or "isolated",
        "profile": str(payload.get("browser_isolated_profile", DEFAULT_ISOLATED_BRIDGE_PROFILE)).strip()
        or DEFAULT_ISOLATED_BRIDGE_PROFILE,
        "port": int(payload.get("browser_isolated_port", DEFAULT_ISOLATED_BRIDGE_PORT) or DEFAULT_ISOLATED_BRIDGE_PORT),
        "data_dir": str(payload.get("browser_isolated_data_dir", DEFAULT_ISOLATED_BRIDGE_DATA_DIR)).strip()
        or DEFAULT_ISOLATED_BRIDGE_DATA_DIR,
        "relay_url": str(payload.get("browser_isolated_relay_url", DEFAULT_ISOLATED_BRIDGE_RELAY)).strip()
        or DEFAULT_ISOLATED_BRIDGE_RELAY,
        "headless": bool(payload.get("browser_isolated_headless", True)),
    }


def _discover_bridge_dir() -> str:
    candidates = [
        os.environ.get("UNCHAINED_BRIDGE_DIR", "").strip(),
        str(REPO_ROOT.parent / "unchainedsky_com" / "unchained-infra" / "unchained"),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if (path / "chrome_bridge.py").exists():
            return str(path)
    raise SystemExit(
        "Could not locate unchained-infra/unchained. Set UNCHAINED_BRIDGE_DIR or pass --bridge-dir."
    )


def _resolve_bridge_python(bridge_dir: Path) -> str:
    candidates = [
        bridge_dir / ".venv" / "bin" / "python",
        Path(sys.executable),
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    raise SystemExit("Could not locate a Python interpreter for chrome_bridge.py")


def _prompt_openai_api_key(default: str = "") -> str:
    if default:
        use_existing = _confirm("Use the existing OpenAI API key from your environment?", default=True)
        if use_existing:
            return default
    return getpass.getpass("OpenAI API key: ").strip()


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, indent=2, ensure_ascii=True) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=".tmp-",
        suffix=path.suffix,
        delete=False,
    ) as handle:
        handle.write(content)
        temp_path = Path(handle.name)
    temp_path.replace(path)


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return rows
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=True) + "\n")


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=True) + "\n")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames: list[str] = []
    for row in rows:
        for key in row.keys():
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def ensure_capsule(name: str, *, append: bool) -> tuple[Path, dict[str, Any]]:
    capsule_dir = CAPSULES_ROOT / slugify(name)
    manifest_path = capsule_dir / "manifest.json"
    if capsule_dir.exists() and not append and manifest_path.exists():
        raise SystemExit(f"Capsule already exists: {capsule_dir}")

    if manifest_path.exists():
        manifest = read_json(manifest_path, {})
    else:
        manifest = {
            "name": capsule_dir.name,
            "task": "",
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "pages": [],
            "endpoint": "",
            "agent_id": "",
            "recipe": "",
        }
    capsule_dir.mkdir(parents=True, exist_ok=True)
    return capsule_dir, manifest


def recent_agent_id_fallback(*, exclude_capsule: Optional[Path] = None) -> str:
    candidates: list[tuple[float, str]] = []
    if not CAPSULES_ROOT.exists():
        return ""
    for manifest_path in CAPSULES_ROOT.glob("*/manifest.json"):
        capsule_dir = manifest_path.parent
        if exclude_capsule is not None and capsule_dir == exclude_capsule:
            continue
        manifest = read_json(manifest_path, {})
        agent_id = str(manifest.get("agent_id", "")).strip()
        if not agent_id:
            continue
        try:
            mtime = manifest_path.stat().st_mtime
        except OSError:
            mtime = 0.0
        candidates.append((mtime, agent_id))
    if not candidates:
        return ""
    candidates.sort(reverse=True)
    return candidates[0][1]


def _preferred_agent_id(
    *,
    explicit: str = "",
    manifest_agent_id: str = "",
    exclude_capsule: Optional[Path] = None,
) -> str:
    return (
        str(explicit or "").strip()
        or str(manifest_agent_id or "").strip()
        or os.environ.get("UNCHAINED_AGENT_ID", "").strip()
        or recent_agent_id_fallback(exclude_capsule=exclude_capsule)
    )


def select_tool_candidates(preferred: tuple[str, ...], available: list[str]) -> list[str]:
    candidates = [tool for tool in preferred if tool in available]
    if candidates:
        return candidates
    return list(preferred)


def call_tool_variants(
    client: MCPClient,
    tool_candidates: list[str],
    argument_variants: list[dict[str, Any]],
    label: str,
) -> tuple[str, dict[str, Any]]:
    errors: list[str] = []
    for tool_name in tool_candidates:
        for arguments in argument_variants:
            try:
                return tool_name, client.call_tool(tool_name, arguments)
            except MCPError as exc:
                errors.append(f"{tool_name} {sorted(arguments.keys())}: {exc}")
    raise MCPError(f"Unable to call {label}. Recent errors: {'; '.join(errors[-6:])}")


def discover_pyreplab_bin() -> Optional[str]:
    candidates = [
        os.environ.get("PYREPLAB_BIN"),
        shutil.which("pyreplab"),
        str(REPO_ROOT.parent / "pyrepl" / "pyreplab"),
        "/Users/zhiminzou/Projects/pyrepl/pyreplab",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return None


def metadata_expression() -> str:
    return (
        "(() => ({"
        "title: document.title || '',"
        "url: location.href || '',"
        "text_length: (document.body && document.body.innerText ? document.body.innerText.length : 0)"
        "}))()"
    )


def decode_screenshot_text(raw: str) -> Optional[bytes]:
    candidate = "".join(raw.split())
    if not candidate:
        return None
    try:
        return base64.b64decode(candidate, validate=True)
    except Exception:
        pass

    parsed = parse_json_if_possible(candidate)
    if isinstance(parsed, dict):
        for key in ("data", "base64", "image", "png"):
            value = parsed.get(key)
            if isinstance(value, str):
                try:
                    return base64.b64decode(value, validate=True)
                except Exception:
                    continue
    return None


def update_manifest(capsule_dir: Path, manifest: dict[str, Any]) -> None:
    manifest["updated_at"] = now_iso()
    write_json(capsule_dir / "manifest.json", manifest)


def novelty_reports_root() -> Path:
    return CAPSULES_ROOT / "_novelty" / "reports"


def novelty_used_prompts_path() -> Path:
    return CAPSULES_ROOT / "_novelty" / "used_prompts.jsonl"


def novelty_lock_path() -> Path:
    return CAPSULES_ROOT / "_novelty" / "novelty-smoke.lock"


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@contextmanager
def novelty_single_flight_lock() -> Any:
    path = novelty_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": os.getpid(),
        "started_at": now_iso(),
    }
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            existing = read_json(path, {})
            existing_pid = int(existing.get("pid", 0) or 0) if isinstance(existing, dict) else 0
            if existing_pid and _pid_is_running(existing_pid):
                raise SystemExit(
                    "Another novelty smoke run is already in progress (pid {pid}). Wait for it to finish before starting a new one.".format(
                        pid=existing_pid
                    )
                )
            try:
                path.unlink()
            except OSError:
                raise SystemExit("Novelty smoke lock is busy and could not be cleared automatically.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=True))
        yield path
    finally:
        try:
            current = read_json(path, {})
            current_pid = int(current.get("pid", 0) or 0) if isinstance(current, dict) else 0
            if current_pid in (0, os.getpid()):
                path.unlink(missing_ok=True)
        except Exception:
            pass


def _novelty_catalog() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for family, templates in NOVELTY_PROMPT_TEMPLATES.items():
        for item in templates:
            prompt = str(item.get("prompt", "")).strip()
            if not prompt:
                continue
            rows.append(
                {
                    "family": family,
                    "label": str(item.get("label", "")).strip() or slugify(prompt)[:48],
                    "prompt": prompt,
                    "expected_object": str(item.get("expected_object", "")).strip(),
                }
            )
    return rows


def _used_prompt_digests() -> set[str]:
    return {
        str(row.get("prompt_digest", "")).strip()
        for row in read_jsonl(novelty_used_prompts_path())
        if str(row.get("prompt_digest", "")).strip()
    }


def _prompt_digest(prompt: str) -> str:
    return sha1(prompt.encode("utf-8")).hexdigest()[:24]


def _select_novelty_cases(
    *,
    count: int,
    families: Optional[list[str]] = None,
    seed: Optional[int] = None,
) -> list[dict[str, Any]]:
    requested_families = {item.strip() for item in (families or []) if item.strip()}
    catalog = [
        item
        for item in _novelty_catalog()
        if not requested_families or item["family"] in requested_families
    ]
    rng = random.Random(seed if seed is not None else int(time.time()))
    used = _used_prompt_digests()
    unused = [item for item in catalog if _prompt_digest(item["prompt"]) not in used]
    fallback = list(catalog)
    rng.shuffle(unused)
    rng.shuffle(fallback)

    selected: list[dict[str, Any]] = []
    covered_families: set[str] = set()
    for pool in (unused, fallback):
        for item in pool:
            if len(selected) >= count:
                break
            prompt = str(item.get("prompt", ""))
            if prompt in {row["prompt"] for row in selected}:
                continue
            family = str(item.get("family", "")).strip()
            if family and family not in covered_families:
                selected.append(item)
                covered_families.add(family)
        if len(selected) >= count:
            return selected[:count]
    for pool in (unused, fallback):
        for item in pool:
            if len(selected) >= count:
                break
            prompt = str(item.get("prompt", ""))
            if prompt in {row["prompt"] for row in selected}:
                continue
            selected.append(item)
    return selected[:count]


def _novelty_summary_issues(
    *,
    expected_object: str,
    actual_object: str,
    source_plan: dict[str, Any],
    readiness: dict[str, Any],
    primary_row_count: int = 0,
    has_primary_shape: bool = False,
    scout_error: str = "",
    gather_error: str = "",
    lab_error: str = "",
) -> list[str]:
    issues: list[str] = []
    if expected_object and actual_object and expected_object != actual_object:
        issues.append("target_object_mismatch")
    budget = dict(source_plan.get("source_budget") or {})
    if int(budget.get("planned_source_count", 0) or 0) == 0:
        issues.append("no_planned_sources")
    planning_status = str(budget.get("planning_status", "")).strip()
    scout_error_code = _novelty_runtime_error_code(scout_error)
    gather_error_code = _novelty_runtime_error_code(gather_error)
    if scout_error:
        issues.append(scout_error_code or "scout_error")
    if gather_error:
        issues.append(gather_error_code or "gather_error")
    if lab_error:
        issues.append("lab_error")
    overall_status = str(readiness.get("overall_status", "")).strip()
    if not has_primary_shape:
        issues.append("no_primary_shape")
    elif primary_row_count <= 0:
        issues.append("primary_shape_empty")
    if planning_status in {"underplanned", "tight"} and (
        not has_primary_shape or primary_row_count <= 0 or overall_status == "blocked"
    ):
        issues.append("planning_{status}".format(status=planning_status))
    if overall_status == "blocked" and not (scout_error or gather_error):
        issues.append("blocked")
    return issues


def _novelty_primary_shape_summary(object_manifest: dict[str, Any]) -> dict[str, Any]:
    primary_object = None
    for item in object_manifest.get("objects", []):
        if not isinstance(item, dict):
            continue
        if str(item.get("object_role", "")).strip() == "primary":
            primary_object = item
            break
    if not isinstance(primary_object, dict):
        return {
            "has_primary_shape": False,
            "primary_object_name": "",
            "primary_row_count": 0,
            "primary_columns": [],
        }
    return {
        "has_primary_shape": True,
        "primary_object_name": str(primary_object.get("name", "")).strip(),
        "primary_row_count": int(primary_object.get("row_count", 0) or 0),
        "primary_columns": [
            str(column.get("name", "")).strip()
            for column in primary_object.get("columns", [])
            if isinstance(column, dict) and str(column.get("name", "")).strip()
        ],
    }


def _novelty_target_satisfied(task_spec: dict[str, Any], primary_summary: dict[str, Any]) -> bool:
    if not bool(primary_summary.get("has_primary_shape")):
        return False
    row_count = int(primary_summary.get("primary_row_count", 0) or 0)
    target_min_rows = _task_target_min_rows(task_spec)
    if target_min_rows > 0:
        return row_count >= target_min_rows
    return row_count > 0


def _task_target_min_rows(task_spec: dict[str, Any]) -> int:
    target_objects = list(task_spec.get("target_objects") or [])
    for target in target_objects:
        if not isinstance(target, dict):
            continue
        sample_target = dict(target.get("sample_target") or {})
        try:
            min_rows = int(sample_target.get("min_rows", 0) or 0)
        except (TypeError, ValueError):
            min_rows = 0
        if min_rows > 0:
            return min_rows
    for item in task_spec.get("stop_conditions", []):
        if not isinstance(item, dict):
            continue
        if str(item.get("type", "")).strip() != "min_rows":
            continue
        try:
            return max(0, int(item.get("value", 0) or 0))
        except (TypeError, ValueError):
            continue
    return 0


def _next_recovery_target_ids(
    gather_targets: dict[str, Any],
    *,
    limit: int,
    exclude: set[str],
) -> list[str]:
    boosted: list[str] = []
    fallback: list[str] = []
    for target in gather_targets.get("targets", []):
        if not isinstance(target, dict):
            continue
        target_id = str(target.get("target_id", "")).strip()
        if not target_id or target_id in exclude:
            continue
        if str(target.get("gather_status", "")).strip() == "captured":
            continue
        recovery_boost = int((target.get("target_quality") or {}).get("recovery_boost", 0) or 0)
        if recovery_boost > 0:
            boosted.append(target_id)
        else:
            fallback.append(target_id)
    chosen = boosted + fallback
    return chosen[: max(1, limit)]


def _gather_qa_collapse_detected(gather_qa: dict[str, Any], primary_row_count: int) -> bool:
    if int(primary_row_count or 0) > 0:
        return False
    reviewed_pages = int(gather_qa.get("reviewed_page_count", 0) or 0)
    if reviewed_pages < 4:
        return False
    accepted_like_fraction = float(gather_qa.get("accepted_like_fraction", 0.0) or 0.0)
    if accepted_like_fraction > 0.25:
        return False
    status_counts = {
        str(key): int(value)
        for key, value in dict(gather_qa.get("status_counts") or {}).items()
        if str(key).strip()
    }
    rejected_like_count = sum(status_counts.get(key, 0) for key in ("blocked", "redirect", "retry"))
    if rejected_like_count < max(3, reviewed_pages - 1):
        return False
    top_reasons = {
        str(reason).strip()
        for reason in dict(gather_qa.get("top_reasons") or {}).keys()
        if str(reason).strip()
    }
    return bool(top_reasons.intersection({"blocked_page", "schema_page_mismatch", "domain_mismatch", "search_engine_page"}))


def _novelty_lab_query(primary_object_name: str) -> str:
    object_name = str(primary_object_name).strip()
    if object_name:
        return (
            "Describe the recommended dataframe for `{name}` and show the first 5 rows in dataframe form. "
            "Keep the answer dataframe-driven and brief."
        ).format(name=object_name)
    return "Describe the recommended dataframe and show the first 5 rows in dataframe form."


def _run_novelty_lab_turn(capsule_dir: Path, primary_object_name: str) -> dict[str, Any]:
    session = get_session(capsule_dir)
    query = _novelty_lab_query(primary_object_name)
    session.append_query(query, role="user")
    generated = generate_code_turn(session, query)
    code = str(generated.get("code", "")).rstrip()
    if not code:
        raise LabAgentError("agent returned no code for novelty lab turn")
    execution = session.execute_code(
        code,
        role="agent",
        metadata={
            "title": str(generated.get("title", "")),
            "intent": str(generated.get("intent", "")),
            "notes": str(generated.get("notes", "")),
        },
    )
    wait_loops = 0
    while execution.get("status") == "running" and wait_loops < 3:
        execution = session.wait_for_pending()
        wait_loops += 1
    summarized = summarize_turn(
        session,
        query=query,
        generated=generated,
        execution=execution,
    )
    markdown = str(summarized.get("markdown", "")).strip()
    if markdown:
        session.append_markdown(markdown, role="agent")
    output = str(execution.get("content", "")).strip()
    return {
        "query": query,
        "title": str(generated.get("title", "")).strip(),
        "intent": str(generated.get("intent", "")).strip(),
        "status": str(execution.get("status", "")).strip() or "unknown",
        "output_has_dataframe_preview": "rows=" in output and "|" in output,
        "markdown_present": bool(markdown),
        "error": str(execution.get("error", "")).strip(),
    }


def _novelty_runtime_error_code(message: str) -> str:
    text = str(message or "").strip().lower()
    if not text:
        return ""
    if "not connected" in text:
        return "browser_agent_disconnected"
    if "timed out after" in text:
        return "step_timeout"
    if "missing agent_id" in text:
        return "agent_unresolved"
    if "missing api key" in text:
        return "api_key_missing"
    if "network error" in text:
        return "network_unavailable"
    return ""


def _novelty_case_name(report_id: str, index: int, label: str) -> str:
    return "novelty-{report}-{index:02d}-{label}".format(
        report=report_id,
        index=index,
        label=slugify(label)[:36],
    )


def _novelty_report_payload(
    *,
    report_id: str,
    args: argparse.Namespace,
    report_rows: list[dict[str, Any]],
    family_counts: Counter[str],
    object_counts: Counter[str],
    issue_counts: Counter[str],
) -> dict[str, Any]:
    return {
        "generated_at": now_iso(),
        "report_id": report_id,
        "mode": {
            "run_scout": args.run_scout,
            "run_gather": args.run_gather,
            "gather_waves": args.gather_waves,
            "case_timeout": args.case_timeout,
        },
        "cases": report_rows,
        "aggregate": {
            "case_count": len(report_rows),
            "family_counts": dict(sorted(family_counts.items())),
            "object_counts": dict(sorted(object_counts.items())),
            "issue_counts": dict(sorted(issue_counts.items())),
        },
    }


def _novelty_case_timeout_seconds(args: argparse.Namespace) -> int:
    gather_waves = max(1, int(getattr(args, "gather_waves", 1) or 1))
    base_timeout = max(1, int(getattr(args, "case_timeout", 60) or 60))
    return max(base_timeout, base_timeout * (gather_waves + 1))


def _run_novelty_case(
    *,
    args: argparse.Namespace,
    report_id: str,
    index: int,
    case: dict[str, Any],
) -> dict[str, Any]:
    family = str(case.get("family", "")).strip()
    label = str(case.get("label", "")).strip() or family or "case"
    prompt = str(case.get("prompt", "")).strip()
    expected_object = str(case.get("expected_object", "")).strip()
    capsule_name = _novelty_case_name(report_id, index, label)
    capsule_dir, manifest = ensure_capsule(capsule_name, append=False)
    manifest["task"] = prompt
    manifest["endpoint"] = args.endpoint or os.environ.get("UNCHAINED_MCP_ENDPOINT", DEFAULT_ENDPOINT)
    manifest["recipe"] = manifest.get("recipe", "")
    update_manifest(capsule_dir, manifest)

    sync_task_files(
        capsule_dir,
        manifest,
        stage="planning",
        status="planned",
    )
    refresh_analysis(capsule_dir, manifest)

    task_spec = read_json(capsule_dir / "task_spec.json", {})
    source_plan = read_json(capsule_dir / "source_plan.json", {})
    readiness = read_json(capsule_dir / "readiness.json", {})
    object_manifest = read_json(capsule_dir / "object_manifest.json", {})
    actual_object = str(((task_spec.get("target_objects") or [{}])[0]).get("name", "")).strip()

    scout_result: dict[str, Any] = {}
    gather_result: dict[str, Any] = {}
    scout_error = ""
    gather_error = ""
    gather_waves_run = 0
    lab_result: dict[str, Any] = {}
    lab_error = ""
    scout_route_ids_all = [
        str(source.get("source_id", "")).strip()
        for source in source_plan.get("sources", [])
        if isinstance(source, dict)
        and str(source.get("route_role", "")).strip() == "scout"
    ]
    scout_ids: list[str] = []

    if args.run_scout > 0:
        scout_ids = scout_route_ids_all[: args.run_scout]
        if scout_ids:
            try:
                scout_result = _run_with_timeout(
                    args.case_timeout,
                    gather_selected_sources,
                    capsule_dir,
                    source_ids=scout_ids,
                    timeout=args.timeout,
                    text_max=args.text_max,
                    settle_seconds=args.settle_seconds,
                    screenshot=args.screenshot,
                    tab_id=args.tab_id,
                    debug=args.debug,
                )
            except (SystemExit, NoveltyStepTimeout) as exc:
                scout_error = str(exc).strip() or "scout_failed"
            source_plan = read_json(capsule_dir / "source_plan.json", {})
            readiness = read_json(capsule_dir / "readiness.json", {})
            object_manifest = read_json(capsule_dir / "object_manifest.json", {})

    if args.run_gather > 0:
        max_gather_waves = max(1, int(args.gather_waves or 1))
        cumulative_gather_result: dict[str, Any] = {
            "captured_count": 0,
            "captured_targets": [],
            "parallel_tabs_used": 0,
        }
        for wave in range(1, max_gather_waves + 1):
            primary_summary = _novelty_primary_shape_summary(object_manifest)
            if _novelty_target_satisfied(task_spec, primary_summary):
                break
            gather_targets = read_json(capsule_dir / "gather_targets.json", {})
            target_ids = [
                str(target.get("target_id", "")).strip()
                for target in gather_targets.get("targets", [])
                if isinstance(target, dict)
                and str(target.get("gather_status", "")).strip() != "captured"
            ][: args.run_gather]
            if not target_ids:
                break
            gather_waves_run = wave
            try:
                wave_result = _run_with_timeout(
                    args.case_timeout,
                    gather_selected_targets,
                    capsule_dir,
                    target_ids=target_ids,
                    timeout=args.timeout,
                    text_max=args.text_max,
                    settle_seconds=args.settle_seconds,
                    screenshot=args.screenshot,
                    tab_id=args.tab_id,
                    parallel_tabs=args.parallel_tabs,
                    debug=args.debug,
                )
                cumulative_gather_result["captured_count"] = int(cumulative_gather_result.get("captured_count", 0) or 0) + int(
                    wave_result.get("captured_count", 0) or 0
                )
                cumulative_gather_result["captured_targets"].extend(list(wave_result.get("captured_targets", []) or []))
                cumulative_gather_result["parallel_tabs_used"] = max(
                    int(cumulative_gather_result.get("parallel_tabs_used", 0) or 0),
                    int(wave_result.get("parallel_tabs_used", 0) or 0),
                )
            except (SystemExit, NoveltyStepTimeout) as exc:
                gather_error = str(exc).strip() or "gather_failed"
                break
            readiness = read_json(capsule_dir / "readiness.json", {})
            object_manifest = read_json(capsule_dir / "object_manifest.json", {})
        gather_result = cumulative_gather_result

    primary_summary = _novelty_primary_shape_summary(object_manifest)
    remaining_scout_ids = [source_id for source_id in scout_route_ids_all if source_id not in set(scout_ids)]
    if (
        args.run_scout > 0
        and args.run_gather > 0
        and not _novelty_target_satisfied(task_spec, primary_summary)
        and remaining_scout_ids
        and not scout_error
    ):
        recovery_scout_ids = remaining_scout_ids[: max(1, int(args.run_scout or 1))]
        try:
            recovery_scout_result = _run_with_timeout(
                args.case_timeout,
                gather_selected_sources,
                capsule_dir,
                source_ids=recovery_scout_ids,
                timeout=args.timeout,
                text_max=args.text_max,
                settle_seconds=args.settle_seconds,
                screenshot=args.screenshot,
                tab_id=args.tab_id,
                debug=args.debug,
            )
            scout_result["captured_count"] = int(scout_result.get("captured_count", 0) or 0) + int(
                recovery_scout_result.get("captured_count", 0) or 0
            )
            source_plan = read_json(capsule_dir / "source_plan.json", {})
            readiness = read_json(capsule_dir / "readiness.json", {})
            object_manifest = read_json(capsule_dir / "object_manifest.json", {})
            gather_targets = read_json(capsule_dir / "gather_targets.json", {})
            recovery_target_ids = [
                str(target.get("target_id", "")).strip()
                for target in gather_targets.get("targets", [])
                if isinstance(target, dict)
                and str(target.get("gather_status", "")).strip() != "captured"
            ][: args.run_gather]
            if recovery_target_ids:
                wave_result = _run_with_timeout(
                    args.case_timeout,
                    gather_selected_targets,
                    capsule_dir,
                    target_ids=recovery_target_ids,
                    timeout=args.timeout,
                    text_max=args.text_max,
                    settle_seconds=args.settle_seconds,
                    screenshot=args.screenshot,
                    tab_id=args.tab_id,
                    parallel_tabs=args.parallel_tabs,
                    debug=args.debug,
                )
                gather_result["captured_count"] = int(gather_result.get("captured_count", 0) or 0) + int(
                    wave_result.get("captured_count", 0) or 0
                )
                gather_result.setdefault("captured_targets", []).extend(list(wave_result.get("captured_targets", []) or []))
                gather_result["parallel_tabs_used"] = max(
                    int(gather_result.get("parallel_tabs_used", 0) or 0),
                    int(wave_result.get("parallel_tabs_used", 0) or 0),
                )
                gather_waves_run += 1
                readiness = read_json(capsule_dir / "readiness.json", {})
                object_manifest = read_json(capsule_dir / "object_manifest.json", {})
        except (SystemExit, NoveltyStepTimeout) as exc:
            if not scout_error:
                scout_error = str(exc).strip() or "scout_recovery_failed"

    budget = dict(source_plan.get("source_budget") or {})
    primary_summary = _novelty_primary_shape_summary(object_manifest)
    gather_qa = read_json(capsule_dir / "gather_qa.json", {})
    gather_qa_review = read_json(capsule_dir / "gather_qa_review.json", {})
    gather_qa_effective = summarize_gather_qa(gather_qa, gather_qa_review)
    if int(getattr(args, "run_lab", 0) or 0) > 0 and primary_summary["has_primary_shape"] and primary_summary["primary_row_count"] > 0:
        try:
            lab_result = _run_novelty_lab_turn(capsule_dir, primary_summary["primary_object_name"])
            if not lab_error and str(lab_result.get("status", "")).strip() == "error":
                lab_error = str(lab_result.get("error", "")).strip() or "lab_failed"
        except (LabAgentError, SystemExit, Exception) as exc:
            lab_error = str(exc).strip() or "lab_failed"
    row = {
        "family": family,
        "label": label,
        "prompt": prompt,
        "prompt_digest": _prompt_digest(prompt),
        "capsule_name": capsule_name,
        "capsule_path": str(capsule_dir),
        "expected_object": expected_object,
        "actual_object": actual_object,
        "planning_status": str(budget.get("planning_status", "")),
        "planned_source_count": int(budget.get("planned_source_count", 0) or 0),
        "scout_source_count": int(budget.get("scout_source_count", 0) or 0),
        "gather_source_count": int(budget.get("gather_source_count", 0) or 0),
        "recommended_source_count": int(budget.get("recommended_source_count", 0) or 0),
        "source_gap": int(budget.get("source_gap", 0) or 0),
        "readiness": str(readiness.get("overall_status", "")),
        "scout_captured_count": int(scout_result.get("captured_count", 0) or 0),
        "gather_captured_count": int(gather_result.get("captured_count", 0) or 0),
        "gather_waves_run": gather_waves_run,
        "primary_object_name": primary_summary["primary_object_name"],
        "primary_row_count": primary_summary["primary_row_count"],
        "has_primary_shape": bool(primary_summary["has_primary_shape"]),
        "accepted_like_fraction": float(gather_qa_effective.get("accepted_like_fraction", 0.0) or 0.0),
        "qa_reviewed_page_count": int(gather_qa_effective.get("reviewed_page_count", 0) or 0),
        "lab_ran": bool(lab_result),
        "lab_query": str(lab_result.get("query", "")),
        "lab_title": str(lab_result.get("title", "")),
        "lab_intent": str(lab_result.get("intent", "")),
        "lab_status": str(lab_result.get("status", "")),
        "lab_output_has_dataframe_preview": bool(lab_result.get("output_has_dataframe_preview", False)),
        "lab_markdown_present": bool(lab_result.get("markdown_present", False)),
        "lab_error": lab_error,
        "scout_error": scout_error,
        "scout_error_code": _novelty_runtime_error_code(scout_error),
        "gather_error": gather_error,
        "gather_error_code": _novelty_runtime_error_code(gather_error),
    }
    row["issues"] = _novelty_summary_issues(
        expected_object=expected_object,
        actual_object=actual_object,
        source_plan=source_plan,
        readiness=readiness,
        primary_row_count=int(primary_summary["primary_row_count"] or 0),
        has_primary_shape=bool(primary_summary["has_primary_shape"]),
        scout_error=scout_error,
        gather_error=gather_error,
        lab_error=lab_error,
    )
    append_jsonl(
        novelty_used_prompts_path(),
        {
            "recorded_at": now_iso(),
            "report_id": report_id,
            "prompt": prompt,
            "prompt_digest": row["prompt_digest"],
            "family": family,
            "capsule_name": capsule_name,
        },
    )
    return row


def source_entrypoint_to_url(source: dict[str, Any], *, page_number: int = 1) -> str:
    entrypoint = source.get("entrypoint") or {}
    mode = str(entrypoint.get("mode", "url")).strip() or "url"
    value = str(entrypoint.get("value", "")).strip()
    if mode == "url":
        return value
    if mode == "query":
        if not value:
            return ""
        site_hint = str(entrypoint.get("site_hint", "")).strip()
        query = value
        if site_hint and "site:{site}".format(site=site_hint.lower()) not in value.lower():
            query = "site:{site} {query}".format(site=site_hint, query=value)
        start = max(0, page_number - 1) * 10
        suffix = "" if start <= 0 else "&start={start}".format(start=start)
        return "https://www.google.com/search?q={query}{suffix}".format(query=quote_plus(query), suffix=suffix)
    if mode == "site_hint":
        if not value:
            return ""
        return "https://{host}".format(host=value.lstrip("/"))
    return value


def _result_links_expression() -> str:
    return (
        "(() => {"
        "const anchors = Array.from(document.querySelectorAll('a[href]'));"
        "const rows = [];"
        "for (const anchor of anchors) {"
        "  const href = anchor.href || '';"
        "  if (!href.startsWith('http')) continue;"
        "  let title = '';"
        "  const titleNode = anchor.querySelector('h3') || anchor.closest('a')?.querySelector('h3');"
        "  if (titleNode && titleNode.innerText) title = titleNode.innerText.trim();"
        "  if (!title && anchor.innerText) title = anchor.innerText.trim().split('\\n')[0].trim();"
        "  rows.push({href, title});"
        "  if (rows.length >= 40) break;"
        "}"
        "return rows;"
        "})()"
    )


def _site_anchor_links_expression() -> str:
    return (
        "(() => {"
        "const anchors = Array.from(document.querySelectorAll('a[href]'));"
        "const rows = [];"
        "for (const anchor of anchors) {"
        "  const href = anchor.href || '';"
        "  if (!href.startsWith('http')) continue;"
        "  const text = (anchor.innerText || anchor.getAttribute('aria-label') || anchor.title || '').trim().split('\\n')[0].trim();"
        "  rows.push({href, title: text});"
        "  if (rows.length >= 160) break;"
        "}"
        "return rows;"
        "})()"
    )


def _domain_from_any_url(url: str) -> str:
    match = re.search(r"^https?://([^/]+)", str(url).strip(), re.I)
    if not match:
        return ""
    return match.group(1).lower()


def _domain_matches_site_hint(url: str, site_hint: str) -> bool:
    clean_hint = str(site_hint).strip().lower()
    if not clean_hint:
        return True
    domain = _domain_from_any_url(url)
    if not domain:
        return False
    return domain == clean_hint or domain.endswith("." + clean_hint)


def _unwrap_search_result_url(url: str) -> str:
    clean = str(url or "").strip()
    if not clean:
        return ""
    parsed = urlparse(clean)
    host = (parsed.netloc or "").lower()
    if not host:
        return clean
    params = parse_qs(parsed.query)
    if "google." in host and parsed.path == "/url":
        for key in ("q", "url"):
            values = params.get(key) or []
            if values and str(values[0]).startswith(("https://", "http://")):
                return str(values[0]).strip()
    if any(token in host for token in ("bing.com", "duckduckgo.com", "search.yahoo.com")):
        for key in ("uddg", "u", "url", "q"):
            values = params.get(key) or []
            if values and str(values[0]).startswith(("https://", "http://")):
                return str(values[0]).strip()
    return clean


def _parse_first_result_urls_from_text(page_text: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for raw_line in page_text.splitlines():
        line = raw_line.strip()
        if not line.startswith(("https://", "http://")):
            continue
        if "google.com/" in line:
            continue
        candidate = line.split(" ", 1)[0].split("›", 1)[0].strip()
        if not candidate.startswith(("https://", "http://")):
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        urls.append(candidate)
    return urls


def _tokenize_query_text(text: str) -> list[str]:
    seen: set[str] = set()
    tokens: list[str] = []
    for token in re.findall(r"[a-z0-9]+", str(text).lower()):
        if len(token) <= 2:
            continue
        if token in {"and", "for", "the", "with", "true", "from", "this", "that", "your", "into", "home", "shop"}:
            continue
        if token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    return tokens


def _looks_like_search_engine_url(url: str) -> bool:
    domain = _domain_from_any_url(url).lower()
    if not domain:
        return False
    return any(
        domain == root or domain.endswith("." + root)
        for root in ("google.com", "bing.com", "search.yahoo.com", "duckduckgo.com")
    )


def _looks_like_unhelpful_candidate_url(url: str) -> bool:
    clean = str(url).strip().lower()
    if not clean:
        return True
    if any(marker in clean for marker in ("/cart", "/login", "/account", "/registry", "/wishlist", "/sorry/", "/blocked")):
        return True
    parsed = urlparse(clean)
    query_keys = set(parsed.query.split("&")) if parsed.query else set()
    if any(key.startswith(("q=", "s=", "search=", "k=")) for key in query_keys):
        return True
    segments = [segment for segment in parsed.path.split("/") if segment]
    if not segments:
        return True
    tail = segments[-1].lower()
    if tail in {"home", "shop", "stores", "store", "brands", "brand", "categories", "category"}:
        return True
    return False


def _looks_like_homepage_or_storefront_url(url: str) -> bool:
    clean = str(url).strip().lower()
    if not clean:
        return True
    parsed = urlparse(clean)
    segments = [segment for segment in parsed.path.split("/") if segment]
    if not segments:
        return True
    return segments[-1].lower() in {"home", "shop", "stores", "store", "brands", "brand", "categories", "category"}


def _score_site_candidate(
    *,
    href: str,
    title: str,
    query_tokens: list[str],
    site_hint: str,
    seed_url: str,
) -> int:
    if not href.startswith(("https://", "http://")):
        return -999
    if _looks_like_unhelpful_candidate_url(href):
        return -999
    if site_hint and not _domain_matches_site_hint(href, site_hint):
        return -999
    score = 20
    if site_hint:
        score += 10
    if seed_url and href.rstrip("/") != seed_url.rstrip("/"):
        score += 4
    title_tokens = set(_tokenize_query_text(title))
    href_tokens = set(_tokenize_query_text(urlparse(href).path.replace("-", " ").replace("_", " ")))
    overlap = len(set(query_tokens) & (title_tokens | href_tokens))
    score += overlap * 10
    clean_title = str(title).strip().lower()
    clean_query = " ".join(query_tokens)
    if clean_title and clean_query and clean_query in clean_title:
        score += 20
    if not clean_title:
        score -= 6
    return score


def _site_seed_pages(manifest: dict[str, Any], site_hint: str) -> list[dict[str, Any]]:
    candidates: list[tuple[int, dict[str, Any]]] = []
    for page in manifest.get("pages", []):
        if not isinstance(page, dict):
            continue
        final_url = str(page.get("final_url", "")).strip() or str(page.get("requested_url", "")).strip()
        if not final_url:
            continue
        if _looks_like_search_engine_url(final_url):
            continue
        if site_hint and not _domain_matches_site_hint(final_url, site_hint):
            continue
        score = 0
        if str(page.get("gather_target_id", "")).strip():
            score += 25
        if str(page.get("source_type", "")).strip() == "gather_target":
            score += 15
        score += min(int(page.get("text_chars", 0) or 0) // 200, 15)
        if _looks_like_unhelpful_candidate_url(final_url):
            score -= 20
        candidates.append((score, page))
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [page for _, page in candidates[:4]]


def _resolve_query_from_existing_site_pages(
    *,
    client: MCPClient,
    available_tools: list[str],
    agent_id: str,
    entrypoint: dict[str, Any],
    manifest: dict[str, Any],
    tab_id: str,
    settle_seconds: float,
) -> str:
    site_hint = str(entrypoint.get("site_hint", "")).strip()
    query_text = str(entrypoint.get("value", "")).strip()
    if not site_hint or not query_text:
        return ""
    seed_pages = _site_seed_pages(manifest, site_hint)
    if not seed_pages:
        return ""
    query_tokens = _tokenize_query_text(query_text)
    if not query_tokens:
        return ""
    best_match: tuple[int, str] | None = None
    for page in seed_pages:
        seed_url = str(page.get("final_url", "")).strip() or str(page.get("requested_url", "")).strip()
        if not seed_url:
            continue
        call_tool_variants(
            client,
            select_tool_candidates(PREFERRED_NAVIGATE_TOOLS, available_tools),
            [{"agent_id": agent_id, "url": seed_url, "tab_id": tab_id}, {"agent_id": agent_id, "url": seed_url}],
            "resolve site query navigate",
        )
        if settle_seconds > 0:
            time.sleep(settle_seconds)
        _, js_result = call_tool_variants(
            client,
            select_tool_candidates(PREFERRED_JS_TOOLS, available_tools),
            [
                {"agent_id": agent_id, "expression": _site_anchor_links_expression(), "tab_id": tab_id},
                {"agent_id": agent_id, "expression": _site_anchor_links_expression()},
            ],
            "resolve site query links",
        )
        parsed = parse_json_if_possible(extract_text(js_result))
        if not isinstance(parsed, list):
            continue
        for item in parsed:
            if not isinstance(item, dict):
                continue
            href = str(item.get("href", "")).strip()
            title = str(item.get("title", "")).strip()
            score = _score_site_candidate(
                href=href,
                title=title,
                query_tokens=query_tokens,
                site_hint=site_hint,
                seed_url=seed_url,
            )
            if best_match is None or score > best_match[0]:
                best_match = (score, href)
    if best_match and best_match[0] >= 34:
        return best_match[1]
    return ""


def resolve_query_entrypoint_url(
    *,
    client: MCPClient,
    available_tools: list[str],
    agent_id: str,
    entrypoint: dict[str, Any],
    manifest: Optional[dict[str, Any]] = None,
    tab_id: str,
    settle_seconds: float,
) -> str:
    if manifest:
        in_site_url = _resolve_query_from_existing_site_pages(
            client=client,
            available_tools=available_tools,
            agent_id=agent_id,
            entrypoint=entrypoint,
            manifest=manifest,
            tab_id=tab_id,
            settle_seconds=settle_seconds,
        )
        if in_site_url:
            return in_site_url

    search_url = source_entrypoint_to_url({"entrypoint": entrypoint})
    if not search_url:
        return ""

    call_tool_variants(
        client,
        select_tool_candidates(PREFERRED_NAVIGATE_TOOLS, available_tools),
        [{"agent_id": agent_id, "url": search_url, "tab_id": tab_id}, {"agent_id": agent_id, "url": search_url}],
        "resolve query navigate",
    )
    if settle_seconds > 0:
        time.sleep(settle_seconds)

    site_hint = str(entrypoint.get("site_hint", "")).strip()
    query_text = str(entrypoint.get("value", "")).strip().lower()
    preferred_domains = _query_market_platform_domain_hints(query_text)
    focus_tokens = _query_focus_tokens(query_text)
    _, js_result = call_tool_variants(
        client,
        select_tool_candidates(PREFERRED_JS_TOOLS, available_tools),
        [
            {"agent_id": agent_id, "expression": _result_links_expression(), "tab_id": tab_id},
            {"agent_id": agent_id, "expression": _result_links_expression()},
        ],
        "resolve query links",
    )
    parsed = parse_json_if_possible(extract_text(js_result))
    if isinstance(parsed, list):
        scored_links: list[tuple[int, str]] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            href = _unwrap_search_result_url(str(item.get("href", "")).strip())
            if not href.startswith(("https://", "http://")):
                continue
            if "google.com/" in href:
                continue
            if _looks_like_homepage_or_storefront_url(href):
                continue
            if site_hint and not _domain_matches_site_hint(href, site_hint):
                continue
            title = str(item.get("title", "")).strip().lower()
            combined_text = "{title} {href}".format(title=title, href=href.lower())
            score = 10
            if site_hint:
                score += 20
            href_domain = _domain_from_url(href).lower()
            if preferred_domains:
                if any(_normalize_domain(domain) == _normalize_domain(href_domain) for domain in preferred_domains):
                    score += 35
                else:
                    score -= 20
            focus_overlap = 0
            for token in focus_tokens:
                if token and token in combined_text:
                    focus_overlap += 1
            if focus_overlap >= 2:
                score += 22
            elif focus_overlap == 1:
                score += 10
            elif focus_tokens and preferred_domains:
                score -= 20
            score -= _query_focus_mismatch_penalty(query_text, combined_text)
            if any(term in title for term in ("guide", "explained", "what is", "article", "blog")):
                score -= 22
            scored_links.append((score, href))
        if scored_links:
            scored_links.sort(key=lambda item: item[0], reverse=True)
            return scored_links[0][1]

    _, text_result = call_tool_variants(
        client,
        select_tool_candidates(PREFERRED_DDM_TOOLS, available_tools),
        [
            {"agent_id": agent_id, "flags": "--text --max 3000", "tab_id": tab_id},
            {"agent_id": agent_id, "flags": "--text --max 3000"},
        ],
        "resolve query text",
    )
    for candidate in _parse_first_result_urls_from_text(extract_text(text_result)):
        if _domain_matches_site_hint(candidate, site_hint):
            return candidate
    return search_url


def _resolve_gather_parallel_tabs(
    *,
    target_count: int,
    tab_id: str,
    parallel_tabs: Optional[int],
) -> int:
    if target_count <= 1:
        return 1
    if tab_id != "auto":
        return 1
    candidate = parallel_tabs
    if candidate is None:
        raw = os.environ.get("UNCHAINED_PYREPLAB_GATHER_PARALLEL_TABS", "").strip()
        if raw:
            try:
                candidate = int(raw)
            except ValueError:
                candidate = DEFAULT_GATHER_PARALLEL_TABS
        else:
            candidate = DEFAULT_GATHER_PARALLEL_TABS
    candidate = max(1, int(candidate))
    return min(candidate, target_count, MAX_GATHER_PARALLEL_TABS)


def _friendly_capture_error(exc: Exception, *, agent_id: str = "") -> str:
    text = str(exc).strip()
    if "not connected" in text.lower():
        if agent_id:
            return "Browser agent {agent_id} is not connected. Reconnect the Unchained browser agent, then retry Scout or Gather.".format(
                agent_id=agent_id
            )
        return "Browser agent is not connected. Reconnect the Unchained browser agent, then retry Scout or Gather."
    return text or exc.__class__.__name__


def _scout_pages_per_route(*, source_count: int, scout_action_budget: int) -> int:
    if source_count <= 0:
        return 1
    if scout_action_budget <= 0:
        return 1
    # One discovery page is roughly one bounded browser batch: navigate + orient + extract.
    affordable_pages = max(1, scout_action_budget // 12)
    return max(1, min(6, ceil(affordable_pages / source_count)))


def _build_scout_assignments(
    *,
    sources: list[dict[str, Any]],
    source_budget: Optional[dict[str, Any]] = None,
) -> list[tuple[dict[str, Any], int]]:
    clean_sources = [source for source in sources if isinstance(source, dict)]
    if not clean_sources:
        return []
    budget = dict(source_budget or {})
    scout_sources = [
        source
        for source in clean_sources
        if str(source.get("route_role", "")).strip() == "scout"
        and str((source.get("entrypoint") or {}).get("mode", "")).strip().lower() == "query"
    ]
    pages_per_route = _scout_pages_per_route(
        source_count=len(scout_sources),
        scout_action_budget=int(budget.get("scout_action_budget", 0) or 0),
    )
    assignments: list[tuple[dict[str, Any], int]] = []
    for source in clean_sources:
        route_role = str(source.get("route_role", "")).strip()
        mode = str((source.get("entrypoint") or {}).get("mode", "")).strip().lower()
        page_count = pages_per_route if route_role == "scout" and mode == "query" else 1
        for page_number in range(1, page_count + 1):
            assignments.append((source, page_number))
    return assignments


def _capture_target_batch(
    *,
    endpoint: str,
    api_key: str,
    timeout: int,
    debug: bool,
    agent_id: str,
    capsule_dir: Path,
    assignments: list[tuple[int, dict[str, Any]]],
    text_max: int,
    settle_seconds: float,
    screenshot: bool,
    tab_id: str,
    scroll_moves: int,
    target_object: str = "",
    manifest: Optional[dict[str, Any]] = None,
) -> list[tuple[int, dict[str, Any]]]:
    if not assignments:
        return []
    client = MCPClient(endpoint=endpoint, api_key=api_key, timeout=timeout, debug=debug)
    client.initialize()
    available_tools = client.list_tools()
    captured_pages: list[tuple[int, dict[str, Any]]] = []
    for page_index, target in assignments:
        entrypoint = dict(target.get("entrypoint") or {})
        mode = str(entrypoint.get("mode", "")).strip()
        if mode == "query":
            requested_url = resolve_query_entrypoint_url(
                client=client,
                available_tools=available_tools,
                agent_id=agent_id,
                entrypoint=entrypoint,
                manifest=manifest,
                tab_id=tab_id,
                settle_seconds=settle_seconds,
            )
        else:
            requested_url = source_entrypoint_to_url(target)
        if not requested_url:
            continue
        page = capture_page(
            client=client,
            available_tools=available_tools,
            agent_id=agent_id,
            requested_url=requested_url,
            capsule_dir=capsule_dir,
            page_index=page_index,
            text_max=text_max,
            settle_seconds=settle_seconds,
            screenshot=screenshot,
            tab_id=tab_id,
            note="Gather target: {target_id}".format(target_id=str(target.get("target_id", ""))),
            scroll_moves=scroll_moves,
            target_object=target_object,
            route_source_type=str(target.get("route_source_type", "")).strip(),
            site_hint=str(entrypoint.get("site_hint", "")).strip(),
        )
        page["gather_target_id"] = str(target.get("target_id", ""))
        page["target_title"] = str(target.get("title", ""))
        page["source_type"] = "gather_target"
        page["source_entrypoint"] = entrypoint
        page["capture_tab_id"] = tab_id
        captured_pages.append((page_index, page))
    return captured_pages


def next_capture_batch_id(capsule_dir: Path) -> str:
    state = read_json(capsule_dir / "capsule_state.json", {})
    match = re.search(r"(\d+)$", str(state.get("latest_capture_batch_id", "")))
    index = int(match.group(1)) + 1 if match else 1
    return "capture-{index:03d}".format(index=index)


def gather_selected_sources(
    capsule_dir: Path,
    *,
    source_ids: list[str],
    timeout: int = 45,
    text_max: int = 5000,
    settle_seconds: float = 2.0,
    screenshot: bool = False,
    tab_id: str = "auto",
    debug: bool = False,
) -> dict[str, Any]:
    manifest = read_json(capsule_dir / "manifest.json", {})
    if not manifest:
        raise SystemExit(f"Missing manifest in {capsule_dir}")
    _assert_mission_action_allowed(capsule_dir, manifest)
    source_plan = read_json(capsule_dir / "source_plan.json", {})
    sources = [
        source
        for source in source_plan.get("sources", [])
        if isinstance(source, dict) and str(source.get("source_id", "")) in set(source_ids)
    ]
    if not sources:
        return {"captured_count": 0, "captured_sources": [], "analysis_path": None}
    source_budget = dict(source_plan.get("source_budget") or {})
    scout_assignments = _build_scout_assignments(sources=sources, source_budget=source_budget)
    if not scout_assignments:
        return {"captured_count": 0, "captured_sources": [], "analysis_path": None}

    resolved = resolve_credentials(
        api_key=None,
        agent_id=_preferred_agent_id(
            manifest_agent_id=str(manifest.get("agent_id", "")).strip(),
            exclude_capsule=capsule_dir,
        ),
        endpoint=str(manifest.get("endpoint") or DEFAULT_ENDPOINT),
        timeout=timeout,
    )
    if not resolved.api_key:
        raise SystemExit("Missing API key. Set UNCHAINED_API_KEY or add it to ~/unchained-agent/.env")
    if not resolved.agent_id:
        raise SystemExit("Missing agent_id. Set UNCHAINED_AGENT_ID or ensure /api/agents can discover one")

    manifest["endpoint"] = resolved.endpoint
    manifest["agent_id"] = resolved.agent_id
    update_manifest(capsule_dir, manifest)
    task_spec = read_json(capsule_dir / "task_spec.json", {})
    target_object = str(((task_spec.get("target_objects") or [{}])[0]).get("name", "")).strip()
    capture_batch_id = next_capture_batch_id(capsule_dir)
    sync_task_files(
        capsule_dir,
        manifest,
        stage="capturing",
        status="capture_in_progress",
        latest_capture_batch_id=capture_batch_id,
    )

    client = MCPClient(endpoint=resolved.endpoint, api_key=resolved.api_key, timeout=timeout, debug=debug)
    client.initialize()
    available_tools = client.list_tools()

    start_index = len(manifest.get("pages", [])) + 1
    captured_sources: list[dict[str, Any]] = []
    try:
        for offset, (source, page_number) in enumerate(scout_assignments):
            requested_url = source_entrypoint_to_url(source, page_number=page_number)
            entrypoint = dict(source.get("entrypoint") or {})
            if (
                str(source.get("source_type", "")).strip() == "official_source_search"
                and str(entrypoint.get("mode", "")).strip() == "query"
            ):
                resolved_url = resolve_query_entrypoint_url(
                    client=client,
                    available_tools=available_tools,
                    agent_id=resolved.agent_id,
                    entrypoint=entrypoint,
                    manifest=manifest,
                    tab_id=tab_id,
                    settle_seconds=settle_seconds,
                )
                if resolved_url:
                    requested_url = resolved_url
            if not requested_url:
                continue
            scout_note = "Scout: {source_id} page {page_number}".format(
                source_id=str(source.get("source_id", "")),
                page_number=page_number,
            )
            attempts = 0
            while True:
                try:
                    page = capture_page(
                        client=client,
                        available_tools=available_tools,
                        agent_id=resolved.agent_id,
                        requested_url=requested_url,
                        capsule_dir=capsule_dir,
                        page_index=start_index + offset,
                        text_max=text_max,
                        settle_seconds=settle_seconds,
                        screenshot=screenshot,
                        tab_id=tab_id,
                        note=scout_note,
                    )
                    break
                except MCPError:
                    attempts += 1
                    if attempts >= 2:
                        raise
                    time.sleep(1.0)
                    client = MCPClient(endpoint=resolved.endpoint, api_key=resolved.api_key, timeout=timeout, debug=debug)
                    client.initialize()
                    available_tools = client.list_tools()
            page["source_id"] = str(source.get("source_id", ""))
            page["source_type"] = str(source.get("source_type", ""))
            page["source_entrypoint"] = dict(source.get("entrypoint") or {})
            page["source_priority"] = source.get("priority", 0)
            page["scout_page_number"] = page_number
            manifest.setdefault("pages", []).append(page)
            update_manifest(capsule_dir, manifest)
            append_jsonl(
                capsule_dir / "capture_log.jsonl",
                {
                    "captured_at": page.get("captured_at", now_iso()),
                    "capture_batch_id": capture_batch_id,
                    "source_id": page["source_id"],
                    "source_type": page["source_type"],
                    "requested_url": page.get("requested_url", ""),
                    "final_url": page.get("final_url", ""),
                    "page_id": page.get("page_id", ""),
                    "entrypoint": page.get("source_entrypoint", {}),
                    "scout_page_number": page_number,
                },
            )
            captured_sources.append(
                {
                    "source_id": page["source_id"],
                    "page_id": page.get("page_id", ""),
                    "requested_url": page.get("requested_url", ""),
                    "final_url": page.get("final_url", ""),
                    "scout_page_number": page_number,
                }
            )
    except MCPError as exc:
        raise SystemExit(_friendly_capture_error(exc, agent_id=resolved.agent_id)) from exc

    update_page_tables(capsule_dir, manifest)
    analysis_path = refresh_analysis(capsule_dir, manifest)
    return {
        "captured_count": len(captured_sources),
        "captured_sources": captured_sources,
        "analysis_path": analysis_path,
        "expanded_route_count": len(scout_assignments),
    }


def gather_selected_targets(
    capsule_dir: Path,
    *,
    target_ids: list[str],
    timeout: int = 45,
    text_max: int = 5000,
    settle_seconds: float = 2.0,
    screenshot: bool = False,
    tab_id: str = "auto",
    parallel_tabs: Optional[int] = None,
    scroll_moves: Optional[int] = None,
    debug: bool = False,
    auto_recover_wave: bool = True,
) -> dict[str, Any]:
    manifest = read_json(capsule_dir / "manifest.json", {})
    if not manifest:
        raise SystemExit(f"Missing manifest in {capsule_dir}")
    _assert_mission_action_allowed(capsule_dir, manifest)
    gather_targets = read_json(capsule_dir / "gather_targets.json", {})
    requested_ids = {str(target_id).strip() for target_id in target_ids if str(target_id).strip()}
    targets = [
        target
        for target in gather_targets.get("targets", [])
        if isinstance(target, dict)
        and str(target.get("target_id", "")).strip() in requested_ids
        and str(target.get("gather_status", "")).strip() != "captured"
    ]
    if not targets:
        return {
            "captured_count": 0,
            "captured_targets": [],
            "analysis_path": None,
            "auto_recovery_wave_run": False,
            "skipped_already_captured": len(requested_ids),
        }

    resolved = resolve_credentials(
        api_key=None,
        agent_id=_preferred_agent_id(
            manifest_agent_id=str(manifest.get("agent_id", "")).strip(),
            exclude_capsule=capsule_dir,
        ),
        endpoint=str(manifest.get("endpoint") or DEFAULT_ENDPOINT),
        timeout=timeout,
    )
    if not resolved.api_key:
        raise SystemExit("Missing API key. Set UNCHAINED_API_KEY or add it to ~/unchained-agent/.env")
    if not resolved.agent_id:
        raise SystemExit("Missing agent_id. Set UNCHAINED_AGENT_ID or ensure /api/agents can discover one")

    manifest["endpoint"] = resolved.endpoint
    manifest["agent_id"] = resolved.agent_id
    update_manifest(capsule_dir, manifest)
    task_spec = read_json(capsule_dir / "task_spec.json", {})
    target_object = str(((task_spec.get("target_objects") or [{}])[0]).get("name", "")).strip()
    capture_batch_id = next_capture_batch_id(capsule_dir)
    sync_task_files(
        capsule_dir,
        manifest,
        stage="capturing",
        status="capture_in_progress",
        latest_capture_batch_id=capture_batch_id,
    )

    start_index = len(manifest.get("pages", [])) + 1
    desired_parallel_tabs = _resolve_gather_parallel_tabs(
        target_count=len(targets),
        tab_id=tab_id,
        parallel_tabs=parallel_tabs,
    )
    resolved_scroll_moves = _resolve_gather_scroll_moves(scroll_moves)
    managed_tab_ids: list[str] = []
    capture_tab_ids: list[str] = [tab_id]

    if desired_parallel_tabs > 1:
        control_client = MCPClient(endpoint=resolved.endpoint, api_key=resolved.api_key, timeout=timeout, debug=debug)
        for _ in range(desired_parallel_tabs):
            try:
                created = control_client.create_tab(resolved.agent_id, "about:blank")
            except Exception:
                break
            created_tab_id = str(created.get("id", "")).strip()
            if created_tab_id:
                managed_tab_ids.append(created_tab_id)
        if managed_tab_ids:
            capture_tab_ids = managed_tab_ids

    captured_pages: list[tuple[int, dict[str, Any]]] = []
    try:
        if len(capture_tab_ids) == 1:
            captured_pages = _capture_target_batch(
                endpoint=resolved.endpoint,
                api_key=resolved.api_key,
                timeout=timeout,
                debug=debug,
                agent_id=resolved.agent_id,
                capsule_dir=capsule_dir,
                assignments=[
                    (start_index + offset, target)
                    for offset, target in enumerate(targets)
                ],
                text_max=text_max,
                settle_seconds=settle_seconds,
                screenshot=screenshot,
                tab_id=capture_tab_ids[0],
                scroll_moves=resolved_scroll_moves,
                target_object=target_object,
                manifest=manifest,
            )
        else:
            assignments_by_tab: list[list[tuple[int, dict[str, Any]]]] = [[] for _ in capture_tab_ids]
            for offset, target in enumerate(targets):
                worker_index = offset % len(capture_tab_ids)
                assignments_by_tab[worker_index].append((start_index + offset, target))

            with ThreadPoolExecutor(max_workers=len(capture_tab_ids)) as executor:
                futures = [
                    executor.submit(
                        _capture_target_batch,
                        endpoint=resolved.endpoint,
                        api_key=resolved.api_key,
                        timeout=timeout,
                        debug=debug,
                        agent_id=resolved.agent_id,
                        capsule_dir=capsule_dir,
                        assignments=assignments,
                        text_max=text_max,
                        settle_seconds=settle_seconds,
                        screenshot=screenshot,
                        tab_id=worker_tab_id,
                        scroll_moves=resolved_scroll_moves,
                        target_object=target_object,
                        manifest=manifest,
                    )
                    for worker_tab_id, assignments in zip(capture_tab_ids, assignments_by_tab)
                    if assignments
                ]
                for future in as_completed(futures):
                    captured_pages.extend(future.result())
    except MCPError as exc:
        raise SystemExit(_friendly_capture_error(exc, agent_id=resolved.agent_id)) from exc
    finally:
        if managed_tab_ids:
            cleanup_client = MCPClient(endpoint=resolved.endpoint, api_key=resolved.api_key, timeout=timeout, debug=debug)
            for managed_tab_id in managed_tab_ids:
                try:
                    cleanup_client.close_tab(resolved.agent_id, managed_tab_id)
                except Exception:
                    continue

    captured_pages.sort(key=lambda item: item[0])
    captured_targets: list[dict[str, Any]] = []
    for _, page in captured_pages:
        manifest.setdefault("pages", []).append(page)
        update_manifest(capsule_dir, manifest)
        append_jsonl(
            capsule_dir / "capture_log.jsonl",
            {
                "captured_at": page.get("captured_at", now_iso()),
                "capture_batch_id": capture_batch_id,
                "gather_target_id": page["gather_target_id"],
                "requested_url": page.get("requested_url", ""),
                "final_url": page.get("final_url", ""),
                "page_id": page.get("page_id", ""),
                "entrypoint": page.get("source_entrypoint", {}),
                "capture_tab_id": page.get("capture_tab_id", ""),
            },
        )
        captured_targets.append(
            {
                "target_id": page["gather_target_id"],
                "page_id": page.get("page_id", ""),
                "requested_url": page.get("requested_url", ""),
                "final_url": page.get("final_url", ""),
                "tab_id": page.get("capture_tab_id", ""),
            }
        )

    update_page_tables(capsule_dir, manifest)
    analysis_path = refresh_analysis(capsule_dir, manifest)
    result = {
        "captured_count": len(captured_targets),
        "captured_targets": captured_targets,
        "analysis_path": analysis_path,
        "parallel_tabs_used": len(capture_tab_ids),
        "scroll_moves_used": resolved_scroll_moves,
        "auto_recovery_wave_run": False,
        "auto_recovery_wave_count": 0,
    }
    if not auto_recover_wave:
        return result

    task_spec = read_json(capsule_dir / "task_spec.json", {})
    object_manifest = read_json(capsule_dir / "object_manifest.json", {})
    readiness = read_json(capsule_dir / "readiness.json", {})
    gather_qa = read_json(capsule_dir / "gather_qa.json", {})
    gather_targets = read_json(capsule_dir / "gather_targets.json", {})
    primary_summary = _novelty_primary_shape_summary(object_manifest)
    target_min_rows = _task_target_min_rows(task_spec)
    row_progress = [int(primary_summary.get("primary_row_count", 0) or 0)]
    status_counts = {
        str(key): int(value)
        for key, value in dict(gather_qa.get("status_counts") or {}).items()
        if str(key).strip()
    }
    redirect_like_count = sum(status_counts.get(key, 0) for key in ("redirect", "retry", "blocked"))
    accepted_like_fraction = float(gather_qa.get("accepted_like_fraction", 0.0) or 0.0)
    should_auto_recover = False
    if target_min_rows > 0:
        should_auto_recover = int(primary_summary.get("primary_row_count", 0) or 0) < target_min_rows
    else:
        should_auto_recover = (
            primary_summary["primary_row_count"] <= 0
            and str(readiness.get("overall_status", "")).strip() == "blocked"
            and redirect_like_count > 0
            and accepted_like_fraction < 0.75
        )
    if should_auto_recover:
        seen_recovery_ids: set[str] = set(requested_ids)
        recovery_target_ids_all: list[str] = []
        previous_row_count = int(primary_summary.get("primary_row_count", 0) or 0)
        previous_accepted_like_fraction = accepted_like_fraction
        stop_reason = "target_met"
        for _wave in range(1, DEFAULT_GATHER_AUTO_RECOVERY_WAVES + 1):
            if target_min_rows > 0 and previous_row_count >= target_min_rows:
                stop_reason = "target_met"
                break
            recovery_target_ids = _next_recovery_target_ids(
                gather_targets,
                limit=max(1, min(4, len(target_ids))),
                exclude=seen_recovery_ids,
            )
            if not recovery_target_ids:
                stop_reason = "no_recovery_targets"
                break
            seen_recovery_ids.update(recovery_target_ids)
            recovery_target_ids_all.extend(recovery_target_ids)
            recovery_result = gather_selected_targets(
                capsule_dir,
                target_ids=recovery_target_ids,
                timeout=timeout,
                text_max=text_max,
                settle_seconds=settle_seconds,
                screenshot=screenshot,
                tab_id=tab_id,
                parallel_tabs=parallel_tabs,
                scroll_moves=scroll_moves,
                debug=debug,
                auto_recover_wave=False,
            )
            result["captured_count"] = int(result.get("captured_count", 0) or 0) + int(
                recovery_result.get("captured_count", 0) or 0
            )
            result["captured_targets"].extend(list(recovery_result.get("captured_targets", []) or []))
            result["parallel_tabs_used"] = max(
                int(result.get("parallel_tabs_used", 0) or 0),
                int(recovery_result.get("parallel_tabs_used", 0) or 0),
            )
            result["analysis_path"] = recovery_result.get("analysis_path") or result.get("analysis_path")
            result["auto_recovery_wave_run"] = True
            result["auto_recovery_wave_count"] = int(result.get("auto_recovery_wave_count", 0) or 0) + 1

            object_manifest = read_json(capsule_dir / "object_manifest.json", {})
            readiness = read_json(capsule_dir / "readiness.json", {})
            gather_qa = read_json(capsule_dir / "gather_qa.json", {})
            gather_targets = read_json(capsule_dir / "gather_targets.json", {})
            primary_summary = _novelty_primary_shape_summary(object_manifest)
            current_row_count = int(primary_summary.get("primary_row_count", 0) or 0)
            current_accepted_like_fraction = float(gather_qa.get("accepted_like_fraction", 0.0) or 0.0)
            row_progress.append(current_row_count)
            if target_min_rows > 0 and current_row_count >= target_min_rows:
                stop_reason = "target_met"
                break
            if _gather_qa_collapse_detected(gather_qa, current_row_count):
                stop_reason = "qa_collapse"
                break
            if (
                current_row_count <= previous_row_count
                and current_accepted_like_fraction <= previous_accepted_like_fraction
            ):
                stop_reason = "stalled"
                break
            previous_row_count = current_row_count
            previous_accepted_like_fraction = current_accepted_like_fraction
        else:
            stop_reason = "max_waves"

        if recovery_target_ids_all:
            result["auto_recovery_target_ids"] = recovery_target_ids_all
        result["auto_recovery_stopped_reason"] = stop_reason
    if row_progress:
        result["row_progress"] = row_progress
        result["row_progress_start"] = int(row_progress[0] or 0)
        result["row_progress_end"] = int(row_progress[-1] or 0)
    if target_min_rows > 0:
        result["row_target"] = target_min_rows
    return result

def sync_task_files(
    capsule_dir: Path,
    manifest: dict[str, Any],
    *,
    recipe_override: Optional[str] = None,
    source_urls: Optional[list[str]] = None,
    stage: Optional[str] = None,
    status: Optional[str] = None,
    latest_capture_batch_id: Optional[str] = None,
    latest_turn_id: Optional[str] = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    previous_task_spec = read_json(capsule_dir / "task_spec.json", {})
    previous_source_plan = read_json(capsule_dir / "source_plan.json", {})
    stored_mission_plan = dict(previous_task_spec.get("mission_plan") or {})
    mission_overrides = dict(previous_task_spec.get("mission_overrides") or {})
    source_urls_inferred_from_previous_plan = False
    if source_urls is None:
        source_urls_inferred_from_previous_plan = True
        source_urls = []
        for source in previous_source_plan.get("sources", []):
            if not isinstance(source, dict):
                continue
            if str(source.get("route_role", "")).strip() == "scout":
                continue
            entrypoint = source.get("entrypoint") or {}
            if entrypoint.get("mode") != "url":
                continue
            value = str(entrypoint.get("value", "")).strip()
            if value and value not in source_urls:
                source_urls.append(value)

    recipe = infer_recipe(
        str(manifest.get("task", "")),
        manifest,
        explicit=recipe_override or manifest.get("recipe") or None,
    )
    task_text = str(manifest.get("task", "")).strip()
    previous_prompt = str(previous_task_spec.get("user_prompt", "")).strip()

    canonical_object_names = {
        "coworking_spaces",
        "districts",
        "home_sale_signals",
        "land_listings",
        "listings",
        "market_contracts",
        "mattress_listings",
        "neighborhood_price_rankings",
        "products",
        "rental_listings",
        "restaurant_chains",
        "restaurants",
        "stock_candidates",
        "vehicle_listings",
    }

    def _looks_low_confidence_mission_override(payload: dict[str, Any]) -> bool:
        return mission_plan_is_low_information(payload)

    def _stale_custom_override_should_yield_to_planner(
        override_payload: dict[str, Any],
        planner_payload: dict[str, Any],
    ) -> bool:
        override_name = str(override_payload.get("name", "")).strip()
        planner_name = str(planner_payload.get("name", "")).strip()
        if not override_name or not planner_name or override_name == planner_name:
            return False
        if planner_name not in canonical_object_names:
            return False
        if override_name in canonical_object_names:
            return False
        override_required_columns = [
            str(item).strip()
            for item in list(override_payload.get("required_columns") or [])
            if str(item).strip()
        ]
        planner_required_columns = [
            str(item).strip()
            for item in list(planner_payload.get("required_columns") or [])
            if str(item).strip()
        ]
        return len(planner_required_columns) > len(override_required_columns)

    normalized_mission_guess = normalize_mission_plan(task_text, {})
    stale_custom_override_detected = _stale_custom_override_should_yield_to_planner(
        mission_overrides,
        normalized_mission_guess,
    ) or _stale_custom_override_should_yield_to_planner(
        stored_mission_plan,
        normalized_mission_guess,
    )

    should_replan_mission = (
        recipe != RECIPE_HIGHSCHOOL
        and bool(task_text)
        and (
            task_text != previous_prompt
            or not stored_mission_plan
            or _looks_low_confidence_mission_override(stored_mission_plan)
            or _looks_low_confidence_mission_override(mission_overrides)
            or stale_custom_override_detected
        )
    )
    mission_plan = stored_mission_plan
    if should_replan_mission:
        mission_plan = plan_mission(
            task_text,
            capsule_dir=capsule_dir,
            existing_task_spec=previous_task_spec,
        )
    applied_mission_overrides = dict(mission_overrides)
    mission_plan_target = str(mission_plan.get("name", "")).strip()
    existing_override_is_generic = _looks_low_confidence_mission_override(applied_mission_overrides)
    existing_override_is_stale_custom = _stale_custom_override_should_yield_to_planner(
        applied_mission_overrides,
        mission_plan,
    )
    previous_target_name = str((previous_task_spec.get("target_objects") or [{}])[0].get("name", "")).strip()
    mission_target_changed = bool(previous_target_name) and previous_target_name != mission_plan_target
    planner_override_allowed = not (
        _looks_like_marketplace_listing_task(task_text, seeded_urls=source_urls)
        and mission_plan_target == "products"
    )
    if (
        (
            not applied_mission_overrides
            or existing_override_is_generic
            or existing_override_is_stale_custom
        )
        and mission_plan_target not in {"", "records"}
        and planner_override_allowed
    ):
        applied_mission_overrides = dict(mission_plan)
    if source_urls_inferred_from_previous_plan and (
        existing_override_is_stale_custom or mission_target_changed
    ):
        source_urls = []
    if not source_urls and mission_plan:
        source_urls = [
            str(url).strip()
            for url in mission_plan.get("seed_urls", [])
            if str(url).strip()
        ]
    task_spec = build_task_spec(
        task_text,
        manifest,
        recipe,
        seeded_urls=source_urls,
        mission_overrides=applied_mission_overrides,
    )
    if mission_plan:
        task_spec["mission_plan"] = mission_plan
    write_json(capsule_dir / "task_spec.json", task_spec)
    row_schema = build_row_schema(task_spec)
    write_json(capsule_dir / "row_schema.json", row_schema)
    write_json(capsule_dir / "object_decision_review.json", build_object_decision_review(task_spec, row_schema))
    source_plan = build_source_plan(task_spec, manifest, recipe, seeded_urls=source_urls)
    write_json(capsule_dir / "source_plan.json", source_plan)

    pending_followups = [
        row
        for row in read_jsonl(capsule_dir / "followups" / "pending_followups.jsonl")
        if row.get("status", "pending") == "pending"
    ]
    previous_state = read_json(capsule_dir / "capsule_state.json", {})
    capsule_state = build_capsule_state(
        manifest,
        task_spec,
        previous=previous_state,
        stage=stage,
        status=status,
        latest_capture_batch_id=latest_capture_batch_id,
        latest_turn_id=latest_turn_id,
        pending_followup_count=len(pending_followups),
    )
    write_json(capsule_dir / "capsule_state.json", capsule_state)
    return task_spec, source_plan, capsule_state


def _mission_action_block_message(review: dict[str, Any]) -> str:
    title = str(review.get("title", "")).strip()
    summary = str(review.get("summary", "")).strip()
    question = str(review.get("question", "")).strip()
    return " ".join(part for part in [title, summary or question] if part).strip()


def refresh_object_decision_review(
    capsule_dir: Path,
    manifest: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    if manifest is None:
        manifest = read_json(capsule_dir / "manifest.json", {})
    existing_review = read_json(capsule_dir / "object_decision_review.json", {})
    task_spec = read_json(capsule_dir / "task_spec.json", {})
    source_plan = read_json(capsule_dir / "source_plan.json", {})
    row_schema = read_json(capsule_dir / "row_schema.json", {})
    if not task_spec or not source_plan:
        task_spec, source_plan, _capsule_state = sync_task_files(capsule_dir, manifest)
    if not isinstance(task_spec, dict) or not task_spec.get("target_objects"):
        return existing_review or {"status": "accepted"}
    if not isinstance(source_plan, dict) or not source_plan:
        return existing_review or {"status": "accepted"}
    if not row_schema:
        row_schema = build_row_schema(task_spec)
        write_json(capsule_dir / "row_schema.json", row_schema)
    scout_index = build_scout_index(capsule_dir, manifest, source_plan)
    scout_summary = build_scout_summary(scout_index)
    schema_refinement = build_schema_refinement(task_spec, scout_index, scout_summary)
    object_decision_review = build_object_decision_review(task_spec, row_schema, schema_refinement)
    write_json(capsule_dir / "scout_index.json", scout_index)
    write_json(capsule_dir / "scout_summary.json", scout_summary)
    write_json(capsule_dir / "schema_refinement.json", schema_refinement)
    write_json(capsule_dir / "object_decision_review.json", object_decision_review)
    return object_decision_review


def _assert_mission_action_allowed(
    capsule_dir: Path,
    manifest: dict[str, Any],
) -> None:
    review = refresh_object_decision_review(capsule_dir, manifest)
    if str(review.get("status", "")).strip() in {"needs_clarification", "replan_recommended"}:
        raise SystemExit(
            _mission_action_block_message(review)
            or "The Mission needs to be clarified before Scout or Gather continue."
        )


def manual_cells_path(capsule_dir: Path) -> Path:
    return capsule_dir / "manual_cells.py"


def ensure_manual_cells(capsule_dir: Path) -> Path:
    path = manual_cells_path(capsule_dir)
    if path.exists():
        return path
    template = """# %% Manual Context
from pathlib import Path

from unchained_pyreplab.analysis_runtime import build_district_metrics, load_analysis_context, to_dataframe

CAPSULE_DIR = Path(r"{capsule_dir}")
context = load_analysis_context(CAPSULE_DIR)
analysis_plan = context["analysis_plan"]
SOURCE_ROWS = context["source_rows"]
ENTITY_ROWS = context["entity_rows"]
SOURCE_DF = context["source_df"]
ENTITY_DF = context["entity_df"]
DISTRICT_METRICS = build_district_metrics(ENTITY_ROWS, SOURCE_ROWS)
DISTRICT_METRICS_DF = to_dataframe(DISTRICT_METRICS)

print({{"task_type": analysis_plan.get("task_type"), "source_count": len(SOURCE_DF), "entity_count": len(ENTITY_DF)}})

# %% Manual Cell Template
from pathlib import Path

from unchained_pyreplab.analysis_runtime import build_district_metrics, load_analysis_context, to_dataframe

CAPSULE_DIR = Path(r"{capsule_dir}")
context = load_analysis_context(CAPSULE_DIR)
analysis_plan = context["analysis_plan"]
SOURCE_ROWS = context["source_rows"]
ENTITY_ROWS = context["entity_rows"]
SOURCE_DF = context["source_df"]
ENTITY_DF = context["entity_df"]
DISTRICT_METRICS = build_district_metrics(ENTITY_ROWS, SOURCE_ROWS)
DISTRICT_METRICS_DF = to_dataframe(DISTRICT_METRICS)

# Replace this cell with new analysis. Keep each cell self-contained.
print(DISTRICT_METRICS_DF[["entity_name"]])
""".format(capsule_dir=capsule_dir.as_posix())
    path.write_text(template, encoding="utf-8")
    return path


def update_page_tables(capsule_dir: Path, manifest: dict[str, Any]) -> None:
    rows: list[dict[str, Any]] = []
    for page in manifest.get("pages", []):
        rows.append(
            {
                "page_id": page.get("page_id", ""),
                "requested_url": page.get("requested_url", ""),
                "final_url": page.get("final_url", ""),
                "title": page.get("title", ""),
                "captured_at": page.get("captured_at", ""),
                "text_chars": page.get("text_chars", 0),
                "snippet": page.get("snippet", ""),
                "source_id": page.get("source_id", ""),
                "source_type": page.get("source_type", ""),
                "scout_page_number": page.get("scout_page_number", ""),
                "gather_target_id": page.get("gather_target_id", ""),
                "capture_tab_id": page.get("capture_tab_id", ""),
            }
        )
    write_jsonl(capsule_dir / "tables" / "pages.jsonl", rows)
    write_csv(capsule_dir / "tables" / "pages.csv", rows)


def update_recipe_tables(
    capsule_dir: Path,
    source_index: dict[str, Any],
    schema_summary: dict[str, Any],
    analysis_plan: dict[str, Any],
    capture_brief: dict[str, Any],
    task_spec: Optional[dict[str, Any]] = None,
    manifest: Optional[dict[str, Any]] = None,
    scout_index: Optional[dict[str, Any]] = None,
) -> None:
    source_rows: list[dict[str, Any]] = []
    for source in source_index.get("sources", []):
        metrics = source.get("extracted_metrics", {})
        source_rows.append(
            {
                "page_id": source.get("page_id", ""),
                "entity_name": source.get("entity_name", ""),
                "source_type": source.get("source_type", ""),
                "source_role": source.get("source_role", ""),
                "source_quality_score": source.get("source_quality_score", 0),
                "comparability_flag": source.get("comparability_flag", ""),
                "grades_served": source.get("grades_served", ""),
                "student_teacher_ratio": metrics.get("student_teacher_ratio", ""),
                "math_proficiency_pct": metrics.get("math_proficiency_pct", ""),
                "reading_proficiency_pct": metrics.get("reading_proficiency_pct", ""),
                "graduation_rate_pct": metrics.get("graduation_rate_pct", ""),
                "average_sat": metrics.get("average_sat", ""),
                "average_act": metrics.get("average_act", ""),
                "overall_niche_grade": metrics.get("overall_niche_grade", ""),
                "rating_out_of_5": metrics.get("rating_out_of_5", ""),
                "review_count": metrics.get("review_count", ""),
                "present_metric_fields": "; ".join(source.get("present_metric_fields", [])),
                "title": source.get("title", ""),
                "final_url": source.get("final_url", ""),
                "excerpt": source.get("excerpt", ""),
            }
        )

    entity_rows: list[dict[str, Any]] = []
    for entity in schema_summary.get("entities", []):
        primary_source = entity.get("primary_source", {})
        entity_rows.append(
            {
                "entity_name": entity.get("entity_name", ""),
                "comparability_flag": entity.get("comparability_flag", ""),
                "source_count": entity.get("source_count", 0),
                "source_types": "; ".join(entity.get("source_types", [])),
                "fields_present": "; ".join(entity.get("fields_present", [])),
                "missing_fields": "; ".join(entity.get("missing_fields", [])),
                "primary_source_page_id": primary_source.get("page_id", ""),
                "primary_source_type": primary_source.get("source_type", ""),
                "primary_source_url": primary_source.get("final_url", ""),
            }
        )

    write_jsonl(capsule_dir / "tables" / "source_index.jsonl", source_rows)
    write_csv(capsule_dir / "tables" / "source_index.csv", source_rows)
    write_jsonl(capsule_dir / "tables" / "entities.jsonl", entity_rows)
    write_csv(capsule_dir / "tables" / "entities.csv", entity_rows)

    capture_rows: list[dict[str, Any]] = []
    for entity in capture_brief.get("entities", []):
        queries = [item.get("query", "") for item in entity.get("recommended_queries", []) if item.get("query")]
        capture_rows.append(
            {
                "entity_name": entity.get("entity_name", ""),
                "comparability_flag": entity.get("comparability_flag", ""),
                "current_source_types": "; ".join(entity.get("current_source_types", [])),
                "missing_required_source_types": "; ".join(entity.get("missing_required_source_types", [])),
                "source_mix_ready": entity.get("source_mix_ready", False),
                "missing_fields": "; ".join(entity.get("missing_fields", [])),
                "recommended_queries": " || ".join(queries[:4]),
            }
        )
    write_jsonl(capsule_dir / "tables" / "capture_targets.jsonl", capture_rows)
    write_csv(capsule_dir / "tables" / "capture_targets.csv", capture_rows)

    if analysis_plan.get("task_type") == RECIPE_HIGHSCHOOL:
        district_rows = build_district_metrics(
            list(schema_summary.get("entities", [])),
            list(source_index.get("sources", [])),
        )
        entity_lookup = {
            str(entity.get("entity_name", "")): entity
            for entity in schema_summary.get("entities", [])
            if isinstance(entity, dict)
        }
        enriched_district_rows: list[dict[str, Any]] = []
        for row in district_rows:
            entity = entity_lookup.get(str(row.get("entity_name", "")), {})
            enriched = dict(row)
            enriched["source_count"] = entity.get("source_count", 0)
            enriched["source_types"] = entity.get("source_types", [])
            enriched["source_page_ids"] = entity.get("source_page_ids", [])
            enriched["missing_source_types"] = entity.get("missing_source_types", [])
            enriched["source_mix_complete"] = entity.get("source_mix_complete", False)
            enriched["fields_present"] = entity.get("fields_present", [])
            enriched["missing_fields"] = entity.get("missing_fields", [])
            enriched_district_rows.append(enriched)

        ranked_district_rows = score_highschool_districts(enriched_district_rows, analysis_plan)
        write_jsonl(capsule_dir / "tables" / "districts.jsonl", enriched_district_rows)
        write_csv(capsule_dir / "tables" / "districts.csv", enriched_district_rows)
        write_jsonl(capsule_dir / "tables" / "ranked_districts.jsonl", ranked_district_rows)
        write_csv(capsule_dir / "tables" / "ranked_districts.csv", ranked_district_rows)
        return

    if task_spec and manifest and scout_index:
        shape_artifact = build_primary_object_shape_artifact(
            capsule_dir,
            manifest,
            task_spec,
            scout_index=scout_index,
        )
        if shape_artifact:
            object_name = str(shape_artifact.get("object_name", "")).strip()
            if object_name:
                rows = list(shape_artifact.get("rows", []))
                provenance_rows = list(shape_artifact.get("provenance_rows", []))
                write_jsonl(capsule_dir / "tables" / f"{object_name}.jsonl", rows)
                write_csv(capsule_dir / "tables" / f"{object_name}.csv", rows)
                write_jsonl(capsule_dir / "provenance" / f"{object_name}.jsonl", provenance_rows)
                write_json(
                    capsule_dir / "shape" / f"{object_name}.json",
                    {
                        "generated_at": now_iso(),
                        "object_name": object_name,
                        "extractor_name": str(shape_artifact.get("extractor_name", "")),
                        "extractor_version": int(shape_artifact.get("extractor_version", 1) or 1),
                        "row_count": int(shape_artifact.get("row_count", len(rows)) or 0),
                        "provenance_count": int(shape_artifact.get("provenance_count", len(provenance_rows)) or 0),
                    },
                )


def refresh_analysis(
    capsule_dir: Path,
    manifest: Optional[dict[str, Any]] = None,
    recipe_override: Optional[str] = None,
) -> Path:
    if manifest is None:
        manifest = read_json(capsule_dir / "manifest.json", {})
    if not manifest:
        raise SystemExit(f"Missing manifest in {capsule_dir}")

    recipe = infer_recipe(
        str(manifest.get("task", "")),
        manifest,
        explicit=recipe_override or manifest.get("recipe") or None,
    )
    manifest["recipe"] = recipe
    update_manifest(capsule_dir, manifest)

    source_index = build_source_index(capsule_dir, manifest, recipe)
    schema_summary = build_schema_summary(recipe, source_index)
    analysis_plan = build_analysis_plan(recipe, manifest, source_index, schema_summary)
    capture_brief = build_capture_brief(recipe, manifest, source_index, schema_summary)
    manifest["task_type"] = analysis_plan.get("task_type", recipe)
    update_manifest(capsule_dir, manifest)
    task_spec, source_plan, _capsule_state = sync_task_files(
        capsule_dir,
        manifest,
        recipe_override=recipe,
        stage="analysis",
        status="exploratory_ready",
    )

    write_json(capsule_dir / "source_index.json", source_index)
    write_json(capsule_dir / "schema_summary.json", schema_summary)
    write_json(capsule_dir / "analysis_plan.json", analysis_plan)
    write_json(capsule_dir / "capture_brief.json", capture_brief)
    scout_index = build_scout_index(capsule_dir, manifest, source_plan)
    scout_summary = build_scout_summary(scout_index)
    schema_refinement = build_schema_refinement(task_spec, scout_index, scout_summary)
    row_schema = build_row_schema(task_spec)
    object_decision_review = build_object_decision_review(task_spec, row_schema, schema_refinement)
    gather_targets = build_gather_targets(
        scout_index,
        manifest,
        task_spec,
        capsule_dir=capsule_dir,
        row_schema=row_schema,
        schema_refinement=schema_refinement,
    )
    gather_qa = build_gather_qa(capsule_dir, manifest, task_spec, gather_targets)
    gather_qa_review = review_gather_qa(
        capsule_dir=capsule_dir,
        task_spec=task_spec,
        gather_qa=gather_qa,
    )
    write_json(capsule_dir / "scout_index.json", scout_index)
    write_json(capsule_dir / "scout_summary.json", scout_summary)
    write_json(capsule_dir / "schema_refinement.json", schema_refinement)
    write_json(capsule_dir / "object_decision_review.json", object_decision_review)
    write_json(capsule_dir / "gather_targets.json", gather_targets)
    write_json(capsule_dir / "gather_qa.json", gather_qa)
    write_json(capsule_dir / "gather_qa_review.json", gather_qa_review)
    write_jsonl(capsule_dir / "tables" / "scout_index.jsonl", list(scout_index.get("rows", [])))
    write_jsonl(capsule_dir / "tables" / "gather_targets.jsonl", list(gather_targets.get("targets", [])))
    write_jsonl(capsule_dir / "tables" / "gather_qa.jsonl", list(gather_qa.get("rows", [])))
    write_jsonl(capsule_dir / "tables" / "gather_qa_review.jsonl", list(gather_qa_review.get("reviews", [])))
    update_recipe_tables(
        capsule_dir,
        source_index,
        schema_summary,
        analysis_plan,
        capture_brief,
        task_spec=task_spec,
        manifest=manifest,
        scout_index=scout_index,
    )
    gather_targets = build_gather_targets(
        scout_index,
        manifest,
        task_spec,
        capsule_dir=capsule_dir,
        row_schema=row_schema,
        schema_refinement=schema_refinement,
    )
    write_json(capsule_dir / "gather_targets.json", gather_targets)
    write_jsonl(capsule_dir / "tables" / "gather_targets.jsonl", list(gather_targets.get("targets", [])))
    object_manifest = build_object_manifest(capsule_dir, task_spec)
    readiness = build_readiness(task_spec, object_manifest, gather_qa, gather_qa_review)
    write_json(capsule_dir / "object_manifest.json", object_manifest)
    write_json(capsule_dir / "readiness.json", readiness)

    analysis_path = capsule_dir / "analysis.py"
    manual_path = ensure_manual_cells(capsule_dir)
    analysis_text = render_analysis(capsule_dir, analysis_plan)
    manual_text = manual_path.read_text(encoding="utf-8")
    if manual_text.strip():
        analysis_text = analysis_text.rstrip() + "\n\n" + manual_text.rstrip() + "\n"
    analysis_path.write_text(analysis_text, encoding="utf-8")
    sync_task_files(
        capsule_dir,
        manifest,
        recipe_override=recipe,
        stage="analysis",
        status=readiness.get("overall_status", "exploratory_ready"),
    )
    return analysis_path


def list_cells(analysis_path: Path) -> list[dict[str, Any]]:
    cells: list[dict[str, Any]] = []
    try:
        lines = analysis_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return cells
    for line in lines:
        if not line.startswith("# %%"):
            continue
        title = line[4:].strip()
        cells.append(
            {
                "index": len(cells),
                "title": title or "(untitled)",
                "kind": "manual" if title.lower().startswith("manual") else "generated",
            }
        )
    return cells


def collect_urls(args: argparse.Namespace) -> list[str]:
    urls: list[str] = list(args.url or [])
    if args.urls_file:
        try:
            for raw_line in Path(args.urls_file).read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if line and not line.startswith("#"):
                    urls.append(line)
        except OSError as exc:
            raise SystemExit(f"Unable to read urls file: {exc}") from exc
    clean = []
    seen: set[str] = set()
    for url in urls:
        value = url.strip()
        if value and value not in seen:
            seen.add(value)
            clean.append(value)
    return clean


def capture_page(
    *,
    client: MCPClient,
    available_tools: list[str],
    agent_id: str,
    requested_url: str,
    capsule_dir: Path,
    page_index: int,
    text_max: int,
    settle_seconds: float,
    screenshot: bool,
    tab_id: str,
    note: str = "",
    scroll_moves: int = 2,
    target_object: str = "",
    route_source_type: str = "",
    site_hint: str = "",
) -> dict[str, Any]:
    page_id = f"page-{page_index:03d}"
    page_dir = capsule_dir / "pages" / page_id
    page_dir.mkdir(parents=True, exist_ok=True)

    nav_tool, nav_result = call_tool_variants(
        client,
        select_tool_candidates(PREFERRED_NAVIGATE_TOOLS, available_tools),
        [{"agent_id": agent_id, "url": requested_url, "tab_id": tab_id}, {"agent_id": agent_id, "url": requested_url}],
        "navigate",
    )
    nav_text = extract_text(nav_result)
    (page_dir / "navigate.txt").write_text(nav_text, encoding="utf-8")

    if settle_seconds > 0:
        time.sleep(settle_seconds)

    js_tool, metadata_result = call_tool_variants(
        client,
        select_tool_candidates(PREFERRED_JS_TOOLS, available_tools),
        [
            {"agent_id": agent_id, "expression": metadata_expression(), "tab_id": tab_id},
            {"agent_id": agent_id, "expression": metadata_expression()},
        ],
        "metadata",
    )
    metadata_text = extract_text(metadata_result)
    metadata_parsed = parse_json_if_possible(metadata_text)
    if not isinstance(metadata_parsed, dict):
        metadata_parsed = {"raw": metadata_text}
    metadata_parsed["navigate_tool"] = nav_tool
    metadata_parsed["metadata_tool"] = js_tool

    _, ddm_result = call_tool_variants(
        client,
        select_tool_candidates(PREFERRED_DDM_TOOLS, available_tools),
        [
            {"agent_id": agent_id, "flags": f"--llm-2pass --cols 60", "tab_id": tab_id},
            {"agent_id": agent_id, "flags": f"--llm-2pass --cols 60"},
        ],
        "ddm orient",
    )
    ddm_text = extract_text(ddm_result)
    (page_dir / "ddm.txt").write_text(ddm_text, encoding="utf-8")

    page_text = _read_current_page_text(
        client=client,
        available_tools=available_tools,
        agent_id=agent_id,
        tab_id=tab_id,
        text_max=text_max,
        label="page text",
    )
    move_profile = _gather_move_budget_profile(
        target_object=target_object,
        route_source_type=route_source_type,
        move_budget=scroll_moves,
    )
    move_profile, page_signal_score = _refine_gather_move_profile(
        base_profile=move_profile,
        target_object=target_object,
        route_source_type=route_source_type,
        title=str(metadata_parsed.get("title", "")),
        url=str(metadata_parsed.get("url") or requested_url),
        page_text=page_text,
    )
    page_segments = [page_text]
    move_budget_used = 0
    explored_urls: list[str] = []
    current_url = str(metadata_parsed.get("url") or requested_url).strip() or requested_url
    if current_url:
        explored_urls.append(current_url)
    if _should_collect_scroll_slices(ddm_text, page_text, text_max, scroll_moves=move_profile["scroll_moves"]):
        supplemental_segments = _scroll_capture_supplemental_text(
            client=client,
            available_tools=available_tools,
            agent_id=agent_id,
            tab_id=tab_id,
            text_max=text_max,
            scroll_moves=move_profile["scroll_moves"],
        )
        if supplemental_segments:
            page_segments.extend(supplemental_segments)
            (page_dir / "scroll_slices.txt").write_text(
                _merge_page_text_segments(supplemental_segments),
                encoding="utf-8",
            )
            move_budget_used += move_profile["scroll_moves"]

    for _attempt in range(move_profile["load_more_attempts"]):
        _, action_result = call_tool_variants(
            client,
            select_tool_candidates(PREFERRED_JS_TOOLS, available_tools),
            [
                {"agent_id": agent_id, "expression": _progressive_reveal_expression(), "tab_id": tab_id},
                {"agent_id": agent_id, "expression": _progressive_reveal_expression()},
            ],
            "progressive reveal",
        )
        parsed_action = parse_json_if_possible(extract_text(action_result))
        if not isinstance(parsed_action, dict) or not parsed_action.get("clicked"):
            break
        move_budget_used += 1
        if settle_seconds > 0:
            time.sleep(max(0.4, settle_seconds / 2.0))
        revealed_text = _read_current_page_text(
            client=client,
            available_tools=available_tools,
            agent_id=agent_id,
            tab_id=tab_id,
            text_max=text_max,
            label="page text after reveal",
        )
        if revealed_text.strip():
            page_segments.append(revealed_text)

    for _hop in range(move_profile["pagination_hops"]):
        next_url = _find_pagination_url(
            client=client,
            available_tools=available_tools,
            agent_id=agent_id,
            tab_id=tab_id,
            current_url=current_url,
            site_hint=site_hint,
        )
        if not next_url or next_url in explored_urls:
            break
        call_tool_variants(
            client,
            select_tool_candidates(PREFERRED_NAVIGATE_TOOLS, available_tools),
            [{"agent_id": agent_id, "url": next_url, "tab_id": tab_id}, {"agent_id": agent_id, "url": next_url}],
            "gather pagination navigate",
        )
        move_budget_used += 1
        if settle_seconds > 0:
            time.sleep(settle_seconds)
        current_url = next_url
        explored_urls.append(next_url)
        paged_text = _read_current_page_text(
            client=client,
            available_tools=available_tools,
            agent_id=agent_id,
            tab_id=tab_id,
            text_max=text_max,
            label="page text after pagination",
        )
        if paged_text.strip():
            page_segments.append(paged_text)

    structured_dom_lines: list[str] = []
    intel_probe_text = ""
    intel_extract_text = ""
    if target_object and (page_signal_score >= 8 or route_source_type == "detail_followup"):
        intel_probe_text, intel_extract_text, structured_dom_lines = _read_intel_enriched_lines(
            client=client,
            available_tools=available_tools,
            agent_id=agent_id,
            tab_id=tab_id,
            target_object=target_object,
        )
        if not structured_dom_lines:
            structured_dom_lines = _read_structured_dom_lines(
                client=client,
                available_tools=available_tools,
                agent_id=agent_id,
                tab_id=tab_id,
                target_object=target_object,
            )
        if intel_probe_text.strip():
            (page_dir / "intel_probe.txt").write_text(intel_probe_text, encoding="utf-8")
        if intel_extract_text.strip():
            (page_dir / "intel_extract.txt").write_text(intel_extract_text, encoding="utf-8")
        if structured_dom_lines:
            (page_dir / "structured_dom.txt").write_text(
                _merge_page_text_segments(structured_dom_lines),
                encoding="utf-8",
            )
            page_segments.append(_merge_page_text_segments(structured_dom_lines))

    page_text = _merge_page_text_segments(page_segments)
    metadata_parsed["move_budget"] = scroll_moves
    metadata_parsed["move_budget_profile"] = move_profile
    metadata_parsed["move_budget_used"] = move_budget_used
    metadata_parsed["page_signal_score"] = page_signal_score
    metadata_parsed["route_source_type"] = route_source_type
    metadata_parsed["target_object"] = target_object
    metadata_parsed["explored_urls"] = explored_urls
    metadata_parsed["structured_dom_line_count"] = len(structured_dom_lines)
    metadata_parsed["intel_probe_used"] = bool(intel_probe_text.strip())
    metadata_parsed["intel_extract_used"] = bool(intel_extract_text.strip())
    write_json(page_dir / "metadata.json", metadata_parsed)
    if len(explored_urls) > 1:
        write_json(
            page_dir / "exploration.json",
            {
                "move_budget": scroll_moves,
                "move_budget_profile": move_profile,
                "move_budget_used": move_budget_used,
                "page_signal_score": page_signal_score,
                "route_source_type": route_source_type,
                "target_object": target_object,
                "explored_urls": explored_urls,
                "structured_dom_line_count": len(structured_dom_lines),
                "intel_probe_used": bool(intel_probe_text.strip()),
                "intel_extract_used": bool(intel_extract_text.strip()),
            },
        )
    (page_dir / "page_text.txt").write_text(page_text, encoding="utf-8")

    screenshot_rel = ""
    if screenshot:
        _, screenshot_result = call_tool_variants(
            client,
            select_tool_candidates(PREFERRED_SCREENSHOT_TOOLS, available_tools),
            [{"agent_id": agent_id, "tab_id": tab_id}, {"agent_id": agent_id}],
            "screenshot",
        )
        raw_screenshot = extract_text(screenshot_result)
        image_bytes = decode_screenshot_text(raw_screenshot)
        if image_bytes:
            (page_dir / "screenshot.png").write_bytes(image_bytes)
            screenshot_rel = f"pages/{page_id}/screenshot.png"
        else:
            (page_dir / "screenshot.txt").write_text(raw_screenshot, encoding="utf-8")
            screenshot_rel = f"pages/{page_id}/screenshot.txt"

    final_url = metadata_parsed.get("url")
    if not isinstance(final_url, str) or not final_url:
        final_url = requested_url
    title = metadata_parsed.get("title")
    if not isinstance(title, str):
        title = ""

    compact_text = " ".join(page_text.split())
    return {
        "page_id": page_id,
        "requested_url": requested_url,
        "final_url": final_url,
        "title": title,
        "captured_at": now_iso(),
        "text_chars": len(page_text),
        "snippet": compact_text[:280],
        "note": note,
        "move_budget_used": move_budget_used,
        "page_signal_score": page_signal_score,
        "explored_urls": explored_urls,
        "artifacts": {
            "metadata": f"pages/{page_id}/metadata.json",
            "navigate": f"pages/{page_id}/navigate.txt",
            "ddm": f"pages/{page_id}/ddm.txt",
            "page_text": f"pages/{page_id}/page_text.txt",
            "scroll_slices": f"pages/{page_id}/scroll_slices.txt" if (page_dir / "scroll_slices.txt").exists() else "",
            "structured_dom": f"pages/{page_id}/structured_dom.txt" if (page_dir / "structured_dom.txt").exists() else "",
            "intel_probe": f"pages/{page_id}/intel_probe.txt" if (page_dir / "intel_probe.txt").exists() else "",
            "intel_extract": f"pages/{page_id}/intel_extract.txt" if (page_dir / "intel_extract.txt").exists() else "",
            "exploration": f"pages/{page_id}/exploration.json" if (page_dir / "exploration.json").exists() else "",
            "screenshot": screenshot_rel,
        },
    }


def cmd_doctor(args: argparse.Namespace) -> int:
    resolved = resolve_credentials(
        api_key=args.api_key,
        agent_id=(args.agent_id or None),
        endpoint=args.endpoint,
        timeout=args.timeout,
    )
    pyreplab_bin = discover_pyreplab_bin()
    _print_mcp_status(resolved, include_repo_root=True)
    print(f"pyreplab_bin={pyreplab_bin or 'missing'}")

    if not args.ping:
        return 0

    if not resolved.api_key:
        raise SystemExit("Cannot ping MCP without an API key")
    if not resolved.agent_id:
        raise SystemExit("Cannot ping MCP without an agent_id")

    client = MCPClient(endpoint=resolved.endpoint, api_key=resolved.api_key, timeout=args.timeout, debug=args.debug)
    client.initialize()
    tools = client.list_tools()
    print(f"mcp_session_id={client.session_id}")
    print(f"tool_count={len(tools)}")
    print(f"tools={','.join(tools)}")
    return 0


def _print_mcp_status(resolved: ResolvedCredentials, *, include_repo_root: bool = False) -> None:
    if include_repo_root:
        print(f"repo_root={REPO_ROOT}")
    print(f"endpoint={resolved.endpoint}")
    print(f"agents_endpoint={resolved.agents_endpoint or infer_agents_endpoint(resolved.endpoint)}")
    print(f"api_key={'set' if resolved.api_key else 'missing'}")
    print(f"agent_id={resolved.agent_id or 'missing'}")
    print(f"credential_source={resolved.source}")
    print(f"agent_resolution={resolved.agent_resolution}")
    if resolved.agent_resolution_error:
        print(f"agent_resolution_error={resolved.agent_resolution_error}")


def cmd_mcp_status(args: argparse.Namespace) -> int:
    resolved = resolve_credentials(
        api_key=args.api_key,
        agent_id=(args.agent_id or None),
        endpoint=args.endpoint,
        timeout=args.timeout,
    )
    _print_mcp_status(resolved)
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    existing = apply_setup_environment()
    provider = (args.provider or "").strip().lower()
    if not provider:
        provider = str(existing.get("provider", "")).strip().lower()
    if provider not in SETUP_PROVIDER_CHOICES:
        provider = _setup_default_provider()
    if not args.provider and sys.stdin.isatty():
        _print_provider_catalog(default=provider)
        provider = _prompt_choice("Choose your Research Desk provider", SETUP_PROVIDER_CHOICES, default=provider)

    openai_api_key = args.openai_api_key or os.environ.get("OPENAI_API_KEY", "").strip()
    if provider == "openai" and not openai_api_key and sys.stdin.isatty():
        openai_api_key = _prompt_openai_api_key(openai_api_key)
    openai_model = args.openai_model or os.environ.get("OPENAI_MODEL", "").strip() or "gpt-5.4"

    endpoint = args.endpoint or os.environ.get("UNCHAINED_MCP_ENDPOINT", DEFAULT_ENDPOINT)
    browser = _browser_setup_status(endpoint=endpoint, timeout=args.timeout)
    should_install_browser_agent = args.install_browser_agent
    if not browser["installed"] and not should_install_browser_agent and sys.stdin.isatty() and not args.skip_browser_agent:
        should_install_browser_agent = _confirm(
            "Unchained browser bridge is not installed. Install it now using the official installer?",
            default=True,
        )
    if should_install_browser_agent and not browser["installed"]:
        print("Installing Unchained browser bridge...")
        _run_browser_agent_installer()
        browser = _browser_setup_status(endpoint=endpoint, timeout=args.timeout)

    pyreplab_bin = (args.pyreplab_bin or os.environ.get("PYREPLAB_BIN", "").strip() or discover_pyreplab_bin() or "")
    provider_pack = _provider_pack(
        provider,
        openai_api_key=openai_api_key,
        openai_model=openai_model,
    )
    provider_status = _provider_status(provider)
    isolated_bridge = _isolated_bridge_defaults(existing)
    config = {
        "version": SETUP_CONFIG_VERSION,
        "configured_at": now_iso(),
        "provider": provider_pack["provider"],
        "lab_provider_mode": provider_pack["lab_provider_mode"],
        "browser_client": provider_pack["browser_client"],
        "provider_note": provider_pack["provider_note"],
        "mcp_endpoint": endpoint,
        "pyreplab_bin": pyreplab_bin,
        "agent_env_path": browser["agent_env_path"],
        "agent_install_dir": browser["agent_install_dir"],
        "browser_agent_installed": browser["installed"],
        "browser_agent_running": browser["running"],
        "browser_bridge_mode": "isolated",
        "browser_isolated_profile": isolated_bridge["profile"],
        "browser_isolated_port": isolated_bridge["port"],
        "browser_isolated_data_dir": isolated_bridge["data_dir"],
        "browser_isolated_relay_url": isolated_bridge["relay_url"],
        "browser_isolated_headless": isolated_bridge["headless"],
        "provider_status": provider_status,
        "env_defaults": provider_pack["env_defaults"],
    }
    config_path = write_setup_config(config)
    apply_setup_environment(config)

    print("setup_config={path}".format(path=config_path))
    print("provider={provider}".format(provider=config["provider"]))
    print("lab_provider_mode={mode}".format(mode=config["lab_provider_mode"]))
    print("browser_agent_installed={value}".format(value="yes" if browser["installed"] else "no"))
    print("browser_agent_running={value}".format(value="yes" if browser["running"] else "no"))
    print(
        "browser_bridge_default={mode}:{profile}:port={port}:headless={headless}".format(
            mode=config["browser_bridge_mode"],
            profile=config["browser_isolated_profile"],
            port=config["browser_isolated_port"],
            headless="yes" if config["browser_isolated_headless"] else "no",
        )
    )
    print("agent_id={value}".format(value=browser["agent_id"] or "missing"))
    print("mcp_endpoint={value}".format(value=endpoint))
    print("pyreplab_bin={value}".format(value=pyreplab_bin or "missing"))
    print("provider_note={value}".format(value=config["provider_note"]))
    checks = provider_status.get("checks", [])
    if checks:
        print("provider_checks={value}".format(value=",".join(str(item) for item in checks)))
    warnings = list(provider_status.get("warnings", []))
    if not browser["installed"]:
        warnings.append("browser bridge not installed")
    elif not browser["running"]:
        warnings.append("browser bridge is installed but no running agent was discovered")
    if not pyreplab_bin:
        warnings.append("pyreplab not found")
    for warning in warnings:
        print("warning={value}".format(value=warning))
    print("next=uv run unchained-pyreplab serve --open --reload")

    if args.launch:
        serve_args = argparse.Namespace(host="127.0.0.1", port=8766, open=True, reload=True, reload_child=False)
        return cmd_serve(serve_args)
    return 0


def cmd_bridge_start(args: argparse.Namespace) -> int:
    config = apply_setup_environment()
    defaults = _isolated_bridge_defaults(config)
    bridge_dir = Path((args.bridge_dir or _discover_bridge_dir()).strip()).expanduser()
    if not (bridge_dir / "chrome_bridge.py").exists():
        raise SystemExit("chrome_bridge.py was not found in {path}".format(path=bridge_dir))

    agent_env_values = parse_env_file(DEFAULT_AGENT_ENV_PATH)
    api_key = (
        (args.api_key or "").strip()
        or os.environ.get("UNCHAINED_API_KEY", "").strip()
        or str(agent_env_values.get("UNCHAINED_API_KEY", "")).strip()
    )
    if not api_key:
        raise SystemExit("Missing API key. Set UNCHAINED_API_KEY or install the Unchained browser bridge first.")

    relay_url = (
        (args.relay or "").strip()
        or os.environ.get("UNCHAINED_RELAY_URL", "").strip()
        or str(agent_env_values.get("UNCHAINED_RELAY_URL", "")).strip()
        or defaults["relay_url"]
    )
    data_dir = Path((args.data_dir or defaults["data_dir"]).strip()).expanduser()
    profile = (args.profile or defaults["profile"]).strip() or DEFAULT_ISOLATED_BRIDGE_PROFILE
    port = int(args.port or defaults["port"])
    headless = not args.headed if args.headless is None else bool(args.headless)
    python_bin = _resolve_bridge_python(bridge_dir)

    env = os.environ.copy()
    env["UNCHAINED_API_KEY"] = api_key
    env["UNCHAINED_RELAY_URL"] = relay_url
    env["UNCHAINED_DATA_DIR"] = str(data_dir)
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")

    command = [python_bin, "chrome_bridge.py", "start", "--port", str(port), "--profile", profile]
    if headless:
        command.append("--headless")
    if args.daemon:
        command.append("--daemon")

    print("bridge_dir={path}".format(path=bridge_dir))
    print("bridge_profile={profile}".format(profile=profile))
    print("bridge_port={port}".format(port=port))
    print("bridge_data_dir={path}".format(path=data_dir))
    print("bridge_relay={value}".format(value=relay_url))
    print("bridge_headless={value}".format(value="yes" if headless else "no"))
    subprocess.run(command, cwd=str(bridge_dir), env=env, check=True)
    return 0


def cmd_capture(args: argparse.Namespace) -> int:
    urls = collect_urls(args)
    if not urls:
        raise SystemExit("Provide at least one --url or --urls-file")

    resolved = resolve_credentials(
        api_key=args.api_key,
        agent_id=_preferred_agent_id(explicit=args.agent_id or ""),
        endpoint=args.endpoint,
        timeout=args.timeout,
    )
    if not resolved.api_key:
        raise SystemExit("Missing API key. Set UNCHAINED_API_KEY or add it to ~/unchained-agent/.env")
    if not resolved.agent_id:
        raise SystemExit("Missing agent_id. Set UNCHAINED_AGENT_ID or ensure /api/agents can discover one")

    capsule_dir, manifest = ensure_capsule(args.name, append=args.append)
    manifest["task"] = args.task
    manifest["endpoint"] = resolved.endpoint
    manifest["agent_id"] = resolved.agent_id
    manifest["recipe"] = args.recipe or manifest.get("recipe", "")
    update_manifest(capsule_dir, manifest)
    capture_batch_id = next_capture_batch_id(capsule_dir)
    sync_task_files(
        capsule_dir,
        manifest,
        recipe_override=args.recipe,
        source_urls=urls,
        stage="capturing",
        status="capture_in_progress",
        latest_capture_batch_id=capture_batch_id,
    )

    client = MCPClient(endpoint=resolved.endpoint, api_key=resolved.api_key, timeout=args.timeout, debug=args.debug)
    client.initialize()
    available_tools = client.list_tools()

    start_index = len(manifest.get("pages", [])) + 1
    for offset, url in enumerate(urls):
        page = capture_page(
            client=client,
            available_tools=available_tools,
            agent_id=resolved.agent_id,
            requested_url=url,
            capsule_dir=capsule_dir,
            page_index=start_index + offset,
            text_max=args.text_max,
            settle_seconds=args.settle_seconds,
            screenshot=args.screenshot,
            tab_id=args.tab_id,
        )
        manifest.setdefault("pages", []).append(page)
        update_manifest(capsule_dir, manifest)
        print(f"captured {page['page_id']} {page['final_url']}")

    update_page_tables(capsule_dir, manifest)
    analysis_path = refresh_analysis(capsule_dir, manifest, recipe_override=args.recipe)
    print(f"capsule={capsule_dir}")
    print(f"recipe={manifest.get('recipe') or infer_recipe(args.task, manifest, explicit=args.recipe)}")
    print(f"task_spec={capsule_dir / 'task_spec.json'}")
    print(f"source_plan={capsule_dir / 'source_plan.json'}")
    print(f"capsule_state={capsule_dir / 'capsule_state.json'}")
    print(f"plan={capsule_dir / 'analysis_plan.json'}")
    print(f"brief={capsule_dir / 'capture_brief.json'}")
    print(f"analysis={analysis_path}")
    print(f"manual_cells={manual_cells_path(capsule_dir)}")
    print(f"next=python3 -m unchained_pyreplab lab {capsule_dir}")
    return 0


def run_pyreplab(analysis_path: Path, run_cell: int) -> int:
    pyreplab_bin = discover_pyreplab_bin()
    if not pyreplab_bin:
        raise SystemExit("Unable to find pyreplab. Set PYREPLAB_BIN or install it in PATH.")

    start_cmd = [pyreplab_bin, "start", "--workdir", str(REPO_ROOT)]

    start = subprocess.run(start_cmd, cwd=REPO_ROOT)
    if start.returncode != 0:
        return start.returncode

    for cell_index in range(run_cell + 1):
        run_cmd = [pyreplab_bin, "run", f"{analysis_path}:{cell_index}"]
        run = subprocess.run(run_cmd, cwd=REPO_ROOT)
        if run.returncode != 0:
            return run.returncode
    return 0


def cmd_lab(args: argparse.Namespace) -> int:
    capsule_dir = Path(args.capsule).resolve()
    if not capsule_dir.exists():
        raise SystemExit(f"Capsule does not exist: {capsule_dir}")
    manifest = read_json(capsule_dir / "manifest.json", {})
    analysis_path = refresh_analysis(capsule_dir, manifest)
    print(f"analysis={analysis_path}")
    if args.no_run:
        print(f"next=python3 -m unchained_pyreplab lab {capsule_dir}")
        return 0
    return run_pyreplab(analysis_path, args.run_cell)


def cmd_brief(args: argparse.Namespace) -> int:
    capsule_dir = Path(args.capsule).resolve()
    if not capsule_dir.exists():
        raise SystemExit(f"Capsule does not exist: {capsule_dir}")
    manifest = read_json(capsule_dir / "manifest.json", {})
    refresh_analysis(capsule_dir, manifest)
    capture_brief = read_json(capsule_dir / "capture_brief.json", {})
    capsule_state = read_json(capsule_dir / "capsule_state.json", {})
    source_plan = read_json(capsule_dir / "source_plan.json", {})
    object_manifest = read_json(capsule_dir / "object_manifest.json", {})
    readiness = read_json(capsule_dir / "readiness.json", {})
    summary = capture_brief.get("summary", {})

    print(f"capsule={capsule_dir}")
    print(f"recipe={capture_brief.get('recipe', '')}")
    print(f"workflow_stage={capsule_state.get('stage', 'unknown')}")
    print(f"workflow_status={capsule_state.get('status', 'unknown')}")
    print(f"readiness={readiness.get('overall_status', 'unknown')}")
    print(f"structured_objects={len(object_manifest.get('objects', []))}")
    print(f"planned_sources={len(source_plan.get('sources', []))}")
    print(f"region={capture_brief.get('region') or 'unknown'}")
    print(f"source_count={summary.get('source_count', 0)}")
    print(f"entity_count={summary.get('entity_count', 0)}")
    print(
        "comparable_entities={current}/{total}".format(
            current=summary.get("comparable_entity_count", 0),
            total=summary.get("entity_count", 0),
        )
    )
    print(
        "source_mix_ready={current}/{total}".format(
            current=summary.get("source_mix_ready_count", 0),
            total=summary.get("entity_count", 0),
        )
    )
    print(f"required_entity_source_types={','.join(capture_brief.get('required_entity_source_types', []))}")
    top_missing_source_types = capture_brief.get("summary", {}).get("top_missing_source_types", [])
    if top_missing_source_types:
        formatted = ",".join(
            "{name}:{count}".format(
                name=item.get("source_type", ""),
                count=item.get("count", 0),
            )
            for item in top_missing_source_types
        )
        print(f"top_missing_source_types={formatted}")
    top_missing_fields = capture_brief.get("summary", {}).get("top_missing_rank_critical_fields", [])
    if top_missing_fields:
        formatted = ",".join(
            "{name}:{count}".format(
                name=item.get("field", ""),
                count=item.get("count", 0),
            )
            for item in top_missing_fields
        )
        print(f"top_missing_rank_critical_fields={formatted}")

    for index, action in enumerate(capture_brief.get("priority_actions", []), start=1):
        print(f"priority_action[{index}]={action}")

    global_queries = capture_brief.get("global_queries", [])
    for item in global_queries[: args.query_limit]:
        print(f"global_query={item.get('query', '')}")

    if not args.verbose:
        return 0

    for entity in capture_brief.get("entities", []):
        print("")
        print(f"entity={entity.get('entity_name', '')}")
        print(f"summary={entity.get('summary', '')}")
        print(f"current_source_types={','.join(entity.get('current_source_types', [])) or 'none'}")
        print(f"missing_required_source_types={','.join(entity.get('missing_required_source_types', [])) or 'none'}")
        print(f"missing_rank_critical_fields={','.join(entity.get('missing_rank_critical_fields', [])) or 'none'}")
        queries = entity.get("recommended_queries", [])
        for item in queries[: args.query_limit]:
            print(f"query[{item.get('source_type', '')}]={item.get('query', '')}")
    return 0


def cmd_cells(args: argparse.Namespace) -> int:
    capsule_dir = Path(args.capsule).resolve()
    if not capsule_dir.exists():
        raise SystemExit(f"Capsule does not exist: {capsule_dir}")
    manifest = read_json(capsule_dir / "manifest.json", {})
    analysis_path = refresh_analysis(capsule_dir, manifest)
    manual_path = ensure_manual_cells(capsule_dir)
    print(f"capsule={capsule_dir}")
    print(f"analysis={analysis_path}")
    print(f"manual_cells={manual_path}")
    for cell in list_cells(analysis_path):
        print(
            "cell[{index}]={title} ({kind})".format(
                index=cell["index"],
                title=cell["title"],
                kind=cell["kind"],
            )
        )
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .webapp import run_server

    if args.reload and not getattr(args, "reload_child", False):
        return _cmd_serve_with_reload(args)
    if args.open and not getattr(args, "reload_child", False):
        webbrowser.open("http://{host}:{port}".format(host=args.host, port=args.port))
    run_server(host=args.host, port=args.port)
    return 0


def _reload_watch_token() -> str:
    latest_mtime_ns = 0
    file_count = 0
    for root in RELOAD_WATCH_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            try:
                stat = path.stat()
            except OSError:
                continue
            file_count += 1
            latest_mtime_ns = max(latest_mtime_ns, getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000)))
    return "{latest}:{count}".format(latest=latest_mtime_ns, count=file_count)


def _serve_child_argv(args: argparse.Namespace) -> list[str]:
    return [
        sys.executable,
        "-m",
        "unchained_pyreplab",
        "serve",
        "--host",
        args.host,
        "--port",
        str(args.port),
        "--reload-child",
    ]


def _stop_serve_child(child: subprocess.Popen[Any]) -> None:
    if child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=3)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=3)


def _cmd_serve_with_reload(args: argparse.Namespace) -> int:
    browser_opened = False
    child = subprocess.Popen(_serve_child_argv(args), cwd=str(REPO_ROOT))
    token = _reload_watch_token()
    pending_restart = False

    if args.open:
        webbrowser.open("http://{host}:{port}".format(host=args.host, port=args.port))
        browser_opened = True

    try:
        while True:
            if child.poll() is not None:
                return int(child.returncode or 0)
            time.sleep(0.8)
            next_token = _reload_watch_token()
            if next_token != token:
                token = next_token
                pending_restart = True
            if not pending_restart or is_reload_paused():
                continue
            _stop_serve_child(child)
            child = subprocess.Popen(_serve_child_argv(args), cwd=str(REPO_ROOT))
            pending_restart = False
            if args.open and not browser_opened:
                webbrowser.open("http://{host}:{port}".format(host=args.host, port=args.port))
                browser_opened = True
    except KeyboardInterrupt:
        _stop_serve_child(child)
        return 0


def cmd_followups(args: argparse.Namespace) -> int:
    capsule_dir = Path(args.capsule).resolve()
    manifest = read_json(capsule_dir / "manifest.json", {})
    if not manifest:
        raise SystemExit(f"Missing manifest in {capsule_dir}")

    pending_path = capsule_dir / "followups" / "pending_followups.jsonl"
    results_path = capsule_dir / "followups" / "results.jsonl"
    pending = read_jsonl(pending_path)
    todo = [row for row in pending if row.get("status", "pending") == "pending"]
    if args.limit:
        todo = todo[: args.limit]

    if not todo:
        print("no pending followups")
        return 0

    resolved = resolve_credentials(
        api_key=args.api_key,
        agent_id=_preferred_agent_id(
            explicit=args.agent_id or "",
            manifest_agent_id=str(manifest.get("agent_id", "")).strip(),
            exclude_capsule=capsule_dir,
        ),
        endpoint=args.endpoint or manifest.get("endpoint") or DEFAULT_ENDPOINT,
        timeout=args.timeout,
    )
    if not resolved.api_key or not resolved.agent_id:
        raise SystemExit("Followups require a resolved API key and agent_id")

    client = MCPClient(endpoint=resolved.endpoint, api_key=resolved.api_key, timeout=args.timeout, debug=args.debug)
    client.initialize()
    available_tools = client.list_tools()
    capture_batch_id = next_capture_batch_id(capsule_dir)
    sync_task_files(
        capsule_dir,
        manifest,
        source_urls=[str(task.get("url", "")) for task in todo if task.get("url")],
        stage="capturing",
        status="capture_in_progress",
        latest_capture_batch_id=capture_batch_id,
    )

    page_index = len(manifest.get("pages", [])) + 1
    completed_ids: set[str] = set()
    for offset, task in enumerate(todo):
        page = capture_page(
            client=client,
            available_tools=available_tools,
            agent_id=resolved.agent_id,
            requested_url=task["url"],
            capsule_dir=capsule_dir,
            page_index=page_index + offset,
            text_max=args.text_max,
            settle_seconds=args.settle_seconds,
            screenshot=args.screenshot,
            tab_id=args.tab_id,
            note=task.get("instruction", ""),
        )
        page["followup_id"] = task.get("followup_id", "")
        manifest.setdefault("pages", []).append(page)
        append_jsonl(
            results_path,
            {
                "followup_id": task.get("followup_id", ""),
                "completed_at": now_iso(),
                "instruction": task.get("instruction", ""),
                "url": task.get("url", ""),
                "page_id": page["page_id"],
                "final_url": page["final_url"],
                "title": page["title"],
            },
        )
        completed_ids.add(task.get("followup_id", ""))
        print(f"followup completed {task.get('followup_id', '')} -> {page['page_id']}")

    for row in pending:
        if row.get("followup_id", "") in completed_ids:
            row["status"] = "done"
            row["completed_at"] = now_iso()
    write_jsonl(pending_path, pending)
    update_page_tables(capsule_dir, manifest)
    refresh_analysis(capsule_dir, manifest)
    return 0


def cmd_novelty_smoke(args: argparse.Namespace) -> int:
    with novelty_single_flight_lock():
        selected = _select_novelty_cases(
            count=args.count,
            families=args.family,
            seed=args.seed,
        )
        if not selected:
            raise SystemExit("No novelty smoke cases available for the requested filters.")

        report_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        report_path = novelty_reports_root() / "novelty-smoke-{report_id}.json".format(report_id=report_id)
        report_rows: list[dict[str, Any]] = []
        issue_counts: Counter[str] = Counter()
        family_counts: Counter[str] = Counter()
        object_counts: Counter[str] = Counter()

        for index, case in enumerate(selected, start=1):
            family = str(case.get("family", "")).strip()
            try:
                row = _run_with_timeout(
                    _novelty_case_timeout_seconds(args),
                    _run_novelty_case,
                    args=args,
                    report_id=report_id,
                    index=index,
                    case=case,
                )
            except NoveltyStepTimeout as exc:
                label = str(case.get("label", "")).strip() or family or "case"
                prompt = str(case.get("prompt", "")).strip()
                expected_object = str(case.get("expected_object", "")).strip()
                capsule_name = _novelty_case_name(report_id, index, label)
                row = {
                    "family": family,
                    "label": label,
                    "prompt": prompt,
                    "prompt_digest": _prompt_digest(prompt),
                    "capsule_name": capsule_name,
                    "capsule_path": str(CAPSULES_ROOT / capsule_name),
                    "expected_object": expected_object,
                    "actual_object": "",
                    "planning_status": "",
                    "planned_source_count": 0,
                    "scout_source_count": 0,
                    "gather_source_count": 0,
                    "recommended_source_count": 0,
                    "source_gap": 0,
                    "readiness": "",
                    "scout_captured_count": 0,
                    "gather_captured_count": 0,
                    "gather_waves_run": 0,
                    "primary_object_name": "",
                    "primary_row_count": 0,
                    "has_primary_shape": False,
                    "accepted_like_fraction": 0.0,
                    "qa_reviewed_page_count": 0,
                    "scout_error": str(exc).strip(),
                    "scout_error_code": "case_timeout",
                    "gather_error": "",
                    "gather_error_code": "",
                    "issues": ["case_timeout", "no_primary_shape"],
                }
            report_rows.append(row)
            for issue in row["issues"]:
                issue_counts[issue] += 1
            if family:
                family_counts[family] += 1
            actual_object = str(row.get("actual_object", "")).strip()
            if actual_object:
                object_counts[actual_object] += 1
            write_json(
                report_path,
                _novelty_report_payload(
                    report_id=report_id,
                    args=args,
                    report_rows=report_rows,
                    family_counts=family_counts,
                    object_counts=object_counts,
                    issue_counts=issue_counts,
                ),
            )

        write_json(
            report_path,
            _novelty_report_payload(
                report_id=report_id,
                args=args,
                report_rows=report_rows,
                family_counts=family_counts,
                object_counts=object_counts,
                issue_counts=issue_counts,
            ),
        )

        print("report={path}".format(path=report_path))
        for row in report_rows:
            print(
                "{capsule} family={family} expected={expected} actual={actual} planning={planning} readiness={readiness} primary={primary}/{rows} qa={qa:.3f} issues={issues}".format(
                    capsule=row["capsule_name"],
                    family=row["family"] or "unknown",
                    expected=row["expected_object"] or "unknown",
                    actual=row["actual_object"] or "unknown",
                    planning=row["planning_status"] or "unknown",
                    readiness=row["readiness"] or "unknown",
                    primary=row["primary_object_name"] or "none",
                    rows=row["primary_row_count"],
                    qa=row["accepted_like_fraction"],
                    issues=",".join(row["issues"]) or "none",
                )
            )
        if issue_counts:
            formatted_issues = ",".join(
                "{name}:{count}".format(name=name, count=count)
                for name, count in sorted(issue_counts.items())
            )
            print("aggregate_issues={issues}".format(issues=formatted_issues))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local toy V1 for Unchained MCP + pyreplab")
    parser.set_defaults(func=None)

    subparsers = parser.add_subparsers(dest="command")

    setup = subparsers.add_parser("setup", help="Provider-first bootstrap for Research Desk")
    setup.add_argument("--provider", choices=SETUP_PROVIDER_CHOICES)
    setup.add_argument("--endpoint", default=os.environ.get("UNCHAINED_MCP_ENDPOINT", DEFAULT_ENDPOINT))
    setup.add_argument("--openai-api-key")
    setup.add_argument("--openai-model", default=os.environ.get("OPENAI_MODEL", "gpt-5.4"))
    setup.add_argument("--install-browser-agent", action="store_true")
    setup.add_argument("--skip-browser-agent", action="store_true")
    setup.add_argument("--pyreplab-bin")
    setup.add_argument("--timeout", type=int, default=45)
    setup.add_argument("--launch", action="store_true")
    setup.set_defaults(func=cmd_setup)

    doctor = subparsers.add_parser("doctor", help="Check local credentials and optionally ping MCP")
    doctor.add_argument("--endpoint", default=os.environ.get("UNCHAINED_MCP_ENDPOINT", DEFAULT_ENDPOINT))
    doctor.add_argument("--api-key")
    doctor.add_argument("--agent-id")
    doctor.add_argument("--timeout", type=int, default=45)
    doctor.add_argument("--ping", action="store_true")
    doctor.add_argument("--debug", action="store_true")
    doctor.set_defaults(func=cmd_doctor)

    mcp_status = subparsers.add_parser("mcp-status", help="Report MCP credential and agent discovery status")
    mcp_status.add_argument("--endpoint", default=os.environ.get("UNCHAINED_MCP_ENDPOINT", DEFAULT_ENDPOINT))
    mcp_status.add_argument("--api-key")
    mcp_status.add_argument("--agent-id")
    mcp_status.add_argument("--timeout", type=int, default=45)
    mcp_status.set_defaults(func=cmd_mcp_status)

    bridge_start = subparsers.add_parser("bridge-start", help="Start an isolated Unchained browser bridge")
    bridge_start.add_argument("--bridge-dir", help="Path to unchained-infra/unchained")
    bridge_start.add_argument("--api-key")
    bridge_start.add_argument("--relay", help="Relay tunnel URL")
    bridge_start.add_argument("--data-dir", help="Isolated bridge data directory")
    bridge_start.add_argument("--profile", help="Isolated bridge profile name")
    bridge_start.add_argument("--port", type=int, help="Isolated bridge CDP port")
    bridge_start.add_argument("--daemon", action="store_true", help="Run the bridge detached")
    bridge_start.add_argument("--headed", action="store_true", help="Run headed instead of the isolated headless default")
    bridge_start.add_argument("--headless", action="store_true", help=argparse.SUPPRESS)
    bridge_start.set_defaults(func=cmd_bridge_start, headless=None)

    capture = subparsers.add_parser("capture", help="Capture one or more URLs into a local capsule")
    capture.add_argument("--name", required=True, help="Capsule name")
    capture.add_argument("--task", required=True, help="Task description")
    capture.add_argument("--url", action="append", default=[], help="URL to capture; repeat for multiple")
    capture.add_argument("--urls-file", help="Text file with one URL per line")
    capture.add_argument("--append", action="store_true", help="Append to an existing capsule")
    capture.add_argument(
        "--recipe",
        choices=[RECIPE_GENERIC, RECIPE_HIGHSCHOOL],
        help="Optional deterministic recipe for planning and notebook generation",
    )
    capture.add_argument("--endpoint", default=os.environ.get("UNCHAINED_MCP_ENDPOINT", DEFAULT_ENDPOINT))
    capture.add_argument("--api-key")
    capture.add_argument("--agent-id")
    capture.add_argument("--timeout", type=int, default=45)
    capture.add_argument("--text-max", type=int, default=5000)
    capture.add_argument("--settle-seconds", type=float, default=2.0)
    capture.add_argument("--tab-id", default="auto")
    capture.add_argument("--screenshot", action="store_true")
    capture.add_argument("--debug", action="store_true")
    capture.set_defaults(func=cmd_capture)

    lab = subparsers.add_parser("lab", help="Refresh analysis.py and open it in pyreplab")
    lab.add_argument("capsule", help="Path to a capsule directory")
    lab.add_argument("--run-cell", type=int, default=0)
    lab.add_argument("--no-run", action="store_true")
    lab.set_defaults(func=cmd_lab)

    brief = subparsers.add_parser("brief", help="Print the recipe-aware capture brief for a capsule")
    brief.add_argument("capsule", help="Path to a capsule directory")
    brief.add_argument("--query-limit", type=int, default=3)
    brief.add_argument("--verbose", action="store_true")
    brief.set_defaults(func=cmd_brief)

    cells = subparsers.add_parser("cells", help="Show analysis cell indices and the manual cells file for a capsule")
    cells.add_argument("capsule", help="Path to a capsule directory")
    cells.set_defaults(func=cmd_cells)

    serve = subparsers.add_parser("serve", help="Run the local markdown-like lab web server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8766)
    serve.add_argument("--open", action="store_true")
    serve.add_argument("--reload", action="store_true", help="Restart the local server when Python files change")
    serve.add_argument("--reload-child", action="store_true", help=argparse.SUPPRESS)
    serve.set_defaults(func=cmd_serve)

    followups = subparsers.add_parser("followups", help="Run pending follow-ups for a capsule")
    followups.add_argument("capsule", help="Path to a capsule directory")
    followups.add_argument("--endpoint")
    followups.add_argument("--api-key")
    followups.add_argument("--agent-id")
    followups.add_argument("--timeout", type=int, default=45)
    followups.add_argument("--text-max", type=int, default=5000)
    followups.add_argument("--settle-seconds", type=float, default=2.0)
    followups.add_argument("--tab-id", default="auto")
    followups.add_argument("--screenshot", action="store_true")
    followups.add_argument("--limit", type=int)
    followups.add_argument("--debug", action="store_true")
    followups.set_defaults(func=cmd_followups)

    novelty = subparsers.add_parser("novelty-smoke", help="Create fresh novelty capsules and summarize planning/runtime variance")
    novelty.add_argument("--count", type=int, default=5)
    novelty.add_argument("--family", action="append", default=[], help="Limit to a specific family; repeatable")
    novelty.add_argument("--seed", type=int)
    novelty.add_argument("--endpoint", default=os.environ.get("UNCHAINED_MCP_ENDPOINT", DEFAULT_ENDPOINT))
    novelty.add_argument("--run-scout", type=int, default=0, help="Optional number of scout routes to capture per case")
    novelty.add_argument("--run-gather", type=int, default=0, help="Optional number of gather targets to capture per case")
    novelty.add_argument("--gather-waves", type=int, default=1, help="Optional number of gather waves to run per case")
    novelty.add_argument("--run-lab", type=int, default=1, help="Run one Lab Notes turn after Shape when a primary object exists")
    novelty.add_argument("--case-timeout", type=int, default=120, help="Timeout per live scout/gather step in seconds")
    novelty.add_argument("--timeout", type=int, default=45)
    novelty.add_argument("--text-max", type=int, default=5000)
    novelty.add_argument("--settle-seconds", type=float, default=2.0)
    novelty.add_argument("--tab-id", default="auto")
    novelty.add_argument("--parallel-tabs", type=int)
    novelty.add_argument("--screenshot", action="store_true")
    novelty.add_argument("--debug", action="store_true")
    novelty.set_defaults(func=cmd_novelty_smoke)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    apply_setup_environment()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.func is None:
        parser.print_help(sys.stderr)
        return 2
    try:
        return int(args.func(args) or 0)
    except MCPError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
