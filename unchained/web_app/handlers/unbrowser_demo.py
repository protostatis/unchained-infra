"""Public `/unbrowser` demo endpoints.

The live scan intentionally accepts only fixed scenario ids. It never accepts
arbitrary URLs from the browser, keeping the public demo bounded and OSS-safe.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from typing import Any

from aiohttp import web


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    category: str
    url: str
    expected: str = "ok"


@dataclass(frozen=True)
class Scenario:
    id: str
    label: str
    description: str
    sources: tuple[Source, ...]
    bullets: tuple[str, ...]
    prompt: str = ""
    meta: str = ""


SCENARIOS: dict[str, Scenario] = {
    "ai-agents": Scenario(
        id="ai-agents",
        label="AI agent market",
        description="Scan sites about browser automation and AI web tools.",
        sources=(
            Source("browserbase", "Browserbase", "Browser-agent infra", "https://www.browserbase.com"),
            Source("hyperbrowser", "Hyperbrowser", "Browser-agent infra", "https://www.hyperbrowser.ai"),
            Source("browserless", "Browserless", "Browser automation", "https://www.browserless.io"),
            Source("steel", "Steel", "Headless browser API", "https://www.steel.dev"),
            Source("browseruse", "Browser Use", "Web agents", "https://browser-use.com"),
            Source("firecrawl", "Firecrawl", "AI web extraction", "https://www.firecrawl.dev"),
            Source("agentql", "AgentQL", "AI web query layer", "https://www.agentql.com"),
            Source("tavily", "Tavily", "Agent search API", "https://www.tavily.com"),
            Source("exa", "Exa", "AI search", "https://exa.ai"),
            Source("jina", "Jina Reader", "URL to markdown", "https://jina.ai/reader"),
            Source("browseai", "Browse AI", "No-code web monitor", "https://www.browse.ai"),
            Source("visualping", "Visualping", "Change detection", "https://visualping.io"),
            Source("distill", "Distill", "Website monitoring", "https://distill.io"),
            Source("klue", "Klue", "Competitive intelligence", "https://www.klue.com"),
            Source("crayon", "Crayon", "Competitive intelligence", "https://www.crayon.co"),
            Source("apify", "Apify", "Scraping platform", "https://www.apify.com"),
            Source("diffbot", "Diffbot", "Knowledge graph extraction", "https://www.diffbot.com"),
            Source("scrapingbee", "ScrapingBee", "Scraping API", "https://www.scrapingbee.com"),
        ),
        bullets=(
            "The strongest market split is infra vs application: browser primitives, extraction APIs, and business monitoring workflows.",
            "unbrowser is the margin engine: cheap broad coverage and explicit challenge detection before escalating expensive browser work.",
        ),
        prompt="What changed this week in AI browser-agent infrastructure?",
        meta="Tech news / developer forums / product pages",
    ),
    "news": Scenario(
        id="news",
        label="News coverage",
        description="Fetch major public news pages and show blocker cases.",
        sources=(
            Source("hackernews", "Hacker News", "Tech news", "https://news.ycombinator.com/best"),
            Source("bbcnews", "BBC News", "Major news", "https://www.bbc.com/news"),
            Source("guardian", "The Guardian", "Major news", "https://www.theguardian.com/international"),
            Source("npr", "NPR", "Public radio news", "https://www.npr.org/sections/news/"),
            Source("nytimes", "NYTimes", "Major news", "https://www.nytimes.com"),
            Source("aljazeera", "Al Jazeera", "Global news", "https://www.aljazeera.com"),
            Source("reuters", "Reuters", "Major news", "https://www.reuters.com", "blocked"),
            Source("cnn", "CNN", "Major news", "https://edition.cnn.com", "partial"),
            Source("lobsters", "Lobste.rs", "Developer news", "https://lobste.rs"),
        ),
        bullets=(
            "News pages make freshness, citations, and source coverage easy to understand.",
            "Blocker detection matters because news sites vary widely in bot-wall and JS behavior.",
        ),
        prompt="latest news headlines across major sources",
        meta="Reuters / BBC / NPR / AP",
    ),
    "betting": Scenario(
        id="betting",
        label="Betting odds pages",
        description="Try odds pages where JavaScript-heavy tables often need fallback.",
        sources=(
            Source("oddsshark-nba", "OddsShark NBA", "Sports odds", "https://www.oddsshark.com/nba/odds", "fallback"),
            Source("oddsshark-mlb", "OddsShark MLB", "Sports odds", "https://www.oddsshark.com/mlb/odds", "fallback"),
            Source("covers-nba", "Covers NBA", "Sports odds", "https://www.covers.com/sport/basketball/nba/odds", "partial"),
            Source("vegasinsider", "VegasInsider", "Sports odds", "https://www.vegasinsider.com/nba/odds/las-vegas/"),
            Source("actionnetwork", "Action Network", "Betting analysis", "https://www.actionnetwork.com/nba/odds", "partial"),
            Source("cbssports-betting", "CBS Sports Betting", "Betting content", "https://www.cbssports.com/betting"),
            Source("espn-betting", "ESPN Betting", "Betting content", "https://www.espn.com/betting", "blocked"),
            Source("fanduel", "FanDuel", "Sportsbook", "https://www.fanduel.com", "partial"),
        ),
        bullets=(
            "Odds pages create a visceral demo because users can see fast-changing tables and hard-page routing.",
            "A production odds agent needs jurisdiction-aware compliance and responsible-gambling guardrails.",
        ),
        prompt="latest betting odds and NBA moneylines",
        meta="Sportsbooks / odds boards / blockers",
    ),
    "public-data": Scenario(
        id="public-data",
        label="Public data sources",
        description="Scan government, policy, and public data pages.",
        sources=(
            Source("census", "US Census Stories", "Government data", "https://www.census.gov/library/stories/2026.html"),
            Source("eff", "EFF Deeplinks", "Digital rights", "https://www.eff.org/deeplinks"),
            Source("statista", "Statista Charts", "Market data", "https://www.statista.com/chart/"),
            Source("wikipedia-events", "Wikipedia Current Events", "Current events", "https://en.wikipedia.org/wiki/Portal:Current_events"),
            Source("bbcfuture", "BBC Future", "Science and environment", "https://www.bbc.com/future"),
            Source("ssa", "Social Security COLA", "Government data", "https://www.ssa.gov/oact/cola/central.html", "partial"),
            Source("nature", "Nature Articles", "Research", "https://www.nature.com/nature/articles", "partial"),
        ),
        bullets=(
            "Public-data monitoring is enterprise-friendly because sources are stable and citation-grade.",
            "The moat is normalization: dates, agencies, affected industries, and source provenance.",
        ),
        prompt="census policy data about AI adoption",
        meta="Census / NIST / OECD / public docs",
    ),
    "developer-docs": Scenario(
        id="developer-docs",
        label="Developer docs",
        description="Fetch docs pages and inspect headings, links, and versioned references.",
        sources=(
            Source("mdn", "MDN Web Docs", "Web platform docs", "https://developer.mozilla.org/en-US/"),
            Source("node-docs", "Node.js Docs", "Runtime docs", "https://nodejs.org/en/docs"),
            Source("python-docs", "Python Docs", "Language docs", "https://docs.python.org/3/"),
            Source("react-docs", "React Docs", "Frontend docs", "https://react.dev"),
            Source("next-docs", "Next.js Docs", "Framework docs", "https://nextjs.org/docs"),
            Source("vite-docs", "Vite Docs", "Build tool docs", "https://vite.dev/guide/"),
            Source("kubernetes-docs", "Kubernetes Docs", "Infrastructure docs", "https://kubernetes.io/docs/home/"),
            Source("docker-docs", "Docker Docs", "Container docs", "https://docs.docker.com/"),
        ),
        bullets=(
            "Docs pages are public, structured, and rich with headings and internal links.",
            "A production docs agent would track changed sections, deprecations, and versioned URLs.",
        ),
        prompt="developer docs updates for React Node Python and MDN",
        meta="MDN / React / Node / Python",
    ),
    "security": Scenario(
        id="security",
        label="Security advisories",
        description="Scan public CVE, advisory, and security research pages.",
        sources=(
            Source("cisa-kev", "CISA KEV", "Known exploited vulns", "https://www.cisa.gov/known-exploited-vulnerabilities-catalog"),
            Source("nvd", "NVD", "Vulnerability database", "https://nvd.nist.gov/vuln"),
            Source("github-advisories", "GitHub Advisories", "Security advisories", "https://github.com/advisories"),
            Source("cloudflare-security", "Cloudflare Security", "Security research", "https://blog.cloudflare.com/tag/security/"),
            Source("project-zero", "Project Zero", "Vulnerability research", "https://googleprojectzero.blogspot.com/"),
            Source("openssf", "OpenSSF", "Open source security", "https://openssf.org/blog/"),
            Source("snyk-vulns", "Snyk Vuln DB", "Vulnerability database", "https://security.snyk.io"),
        ),
        bullets=(
            "Security monitoring needs source attribution, affected products, and fast public-page triage.",
            "Cheap broad coverage is useful before escalating hard pages into a full browser session.",
        ),
        prompt="security advisories CVE vulnerabilities CISA NVD GitHub",
        meta="CISA / NVD / GitHub / OpenSSF",
    ),
    "startups": Scenario(
        id="startups",
        label="Startup launches",
        description="Watch launch feeds and show where directories resist cheap retrieval.",
        sources=(
            Source("producthunt-live", "Product Hunt", "Launch directory", "https://www.producthunt.com", "blocked"),
            Source("hn-show", "HN Show", "Builder launches", "https://news.ycombinator.com/show"),
            Source("github-trending", "GitHub Trending", "Developer projects", "https://github.com/trending"),
            Source("techcrunch-startups", "TechCrunch Startups", "Startup news", "https://techcrunch.com/category/startups/"),
            Source("yc-companies", "YC Companies", "Startup directory", "https://www.ycombinator.com/companies"),
            Source("yc-launches", "YC Launches", "Startup launches", "https://www.ycombinator.com/launches"),
            Source("indiehackers-products", "Indie Hackers", "Indie products", "https://www.indiehackers.com/products"),
        ),
        bullets=(
            "Startup monitoring is easy to understand: scan public launch surfaces and summarize who shipped what.",
            "This scenario deliberately includes blocker-prone pages so the demo can show honest fallback behavior.",
        ),
        prompt="startup launches product hunt yc hacker news github trending",
        meta="Product Hunt / HN / YC / GitHub",
    ),
    "research": Scenario(
        id="research",
        label="Research papers",
        description="Scan preprint, paper index, and journal pages for research monitoring.",
        sources=(
            Source("arxiv-ai", "arXiv cs.AI", "Preprint feed", "https://arxiv.org/list/cs.AI/recent"),
            Source("arxiv-lg", "arXiv cs.LG", "ML preprints", "https://arxiv.org/list/cs.LG/recent"),
            Source("pubmed-ai", "PubMed AI", "Biomedical research", "https://pubmed.ncbi.nlm.nih.gov/?term=artificial+intelligence"),
            Source("nature-mi", "Nature Machine Intelligence", "Research journal", "https://www.nature.com/natmachintell/", "partial"),
            Source("paperswithcode", "Papers with Code", "ML papers", "https://paperswithcode.com/"),
            Source("openreview", "OpenReview", "Conference papers", "https://openreview.net/"),
        ),
        bullets=(
            "Research feeds are public, text-dense, and citation-friendly sources for cheap first-pass retrieval.",
            "A production research agent should deduplicate papers, classify methods, and remember prior scans.",
        ),
        prompt="research papers arxiv pubmed openreview machine learning",
        meta="arXiv / PubMed / OpenReview",
    ),
}

_SCAN_SEMAPHORE = asyncio.Semaphore(int(os.environ.get("UNBROWSER_DEMO_MAX_CONCURRENT", "2")))
_SOURCE_TIMEOUT_SECONDS = float(os.environ.get("UNBROWSER_DEMO_SOURCE_TIMEOUT", "25"))


async def handle_unbrowser_sources(request: web.Request) -> web.Response:
    """Return fixed demo source sets for the `/unbrowser` page."""
    del request
    return web.json_response([_scenario_payload(scenario) for scenario in SCENARIOS.values()])


async def handle_unbrowser_stream(request: web.Request) -> web.StreamResponse:
    """Stream one fixed-source live scan as Server-Sent Events."""
    scenario = SCENARIOS.get(request.query.get("scenario", "ai-agents"), SCENARIOS["ai-agents"])
    try:
        await asyncio.wait_for(_SCAN_SEMAPHORE.acquire(), timeout=0.1)
    except TimeoutError:
        return web.json_response({"error": "unbrowser demo is busy; try again shortly"}, status=429)

    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream; charset=utf-8",
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
    try:
        await response.prepare(request)
        await _run_scan(response, scenario)
    except (ConnectionResetError, asyncio.CancelledError):
        raise
    finally:
        _SCAN_SEMAPHORE.release()
    return response


async def _run_scan(response: web.StreamResponse, scenario: Scenario) -> None:
    sources = scenario.sources
    await _write_event(response, "run_started", {"total": len(sources), "message": f"Live scan accepted: {scenario.label}"})
    await _write_event(response, "plan_ready", _scenario_payload(scenario, mode="live"))
    await _write_event(response, "trace", {"message": f"Using fixed source list for {scenario.label}"})
    await _write_event(response, "trace", {"message": "Launching unbrowser fetches in parallel across selected public URLs"})
    for source in sources:
        await _write_event(response, "source_started", _source_payload(source))

    results: list[dict[str, Any]] = []
    facts: list[dict[str, Any]] = []
    tasks = [asyncio.create_task(_fetch_source(source)) for source in sources]
    try:
        for task in asyncio.as_completed(tasks):
            result = await task
            results.append(result)
            await _write_event(response, "source_result", {key: value for key, value in result.items() if key not in {"raw", "source"}})
            for fact in _extract_facts(result["source"], result.get("raw"))[:2]:
                facts.append(fact)
                await _write_event(response, "fact_extracted", fact)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()

    await _write_event(response, "trace", {"message": "Live fetches complete; summarizing results"})
    await _write_event(response, "brief_ready", _build_brief(results, facts, scenario))
    await _write_event(response, "done", {"message": "Live scan complete"})
    await response.write_eof()


async def _fetch_source(source: Source) -> dict[str, Any]:
    started = time.monotonic()
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "unbrowser",
            "navigate",
            source.url,
            "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_SOURCE_TIMEOUT_SECONDS)
    except FileNotFoundError:
        return _error_result(source, started, "unbrowser binary not found on this server")
    except TimeoutError:
        if proc and proc.returncode is None:
            proc.kill()
            await proc.communicate()
        return _error_result(source, started, f"timed out after {_SOURCE_TIMEOUT_SECONDS:.0f}s")

    ms = int((time.monotonic() - started) * 1000)
    if proc.returncode:
        return _error_result(source, started, (stderr or b"").decode("utf-8", "replace")[:220] or f"process exited {proc.returncode}")
    try:
        parsed = json.loads(stdout.decode("utf-8"))
    except json.JSONDecodeError:
        return _error_result(source, started, (stderr or b"").decode("utf-8", "replace")[:220] or "invalid JSON output")
    return _parsed_result(source, parsed, ms)


def _parsed_result(source: Source, parsed: dict[str, Any], ms: int) -> dict[str, Any]:
    blockmap = parsed.get("blockmap") or {}
    density = blockmap.get("density") or {}
    challenge = parsed.get("challenge")
    chars = density.get("body_text_chars") or parsed.get("bytes") or 0
    status = "ok"
    route = "unbrowser"
    if challenge:
        status = "blocked"
        route = challenge.get("provider") or challenge.get("vendor") or "bot wall"
    elif (parsed.get("status") or 0) >= 400:
        status = "error"
        route = f"HTTP {parsed.get('status')}"
    elif density.get("likely_js_filled") or density.get("thin_shell") or chars < 900:
        status = "partial"
        route = "JS-heavy"
    return {
        **_source_payload(source),
        "status": status,
        "route": route,
        "ms": ms,
        "chars": chars,
        "facts": 0,
        "preview": _preview(source, parsed),
        "raw": parsed,
        "source": source,
    }


def _error_result(source: Source, started: float, reason: str) -> dict[str, Any]:
    return {
        **_source_payload(source),
        "status": "error",
        "route": "error",
        "ms": int((time.monotonic() - started) * 1000),
        "chars": 0,
        "facts": 0,
        "preview": {
            "title": source.name,
            "url": source.url,
            "httpStatus": "error",
            "density": {"bodyTextChars": 0, "links": 0, "headings": 0, "jsonScripts": 0, "structure": 0},
            "headings": [],
            "links": [],
            "structure": [],
            "challenge": {"provider": "process_error", "reason": reason},
        },
        "raw": None,
        "source": source,
    }


def _preview(source: Source, parsed: dict[str, Any]) -> dict[str, Any]:
    blockmap = parsed.get("blockmap") or {}
    density = blockmap.get("density") or {}
    headings = [item.get("text") for item in blockmap.get("headings", []) if item.get("text")]
    link_samples = (blockmap.get("interactives") or {}).get("link_samples") or []
    structure = blockmap.get("structure") or []
    challenge = parsed.get("challenge")
    return {
        "title": blockmap.get("title") or parsed.get("title") or source.name,
        "url": parsed.get("url") or source.url,
        "httpStatus": parsed.get("status") or "--",
        "density": {
            "bodyTextChars": density.get("body_text_chars") or parsed.get("bytes") or 0,
            "links": (blockmap.get("interactives") or {}).get("links") or 0,
            "headings": len(headings),
            "jsonScripts": density.get("json_scripts") or 0,
            "structure": len(structure),
        },
        "headings": headings[:6],
        "links": [{"text": _compact(link.get("text") or link.get("href") or "Untitled", 110), "href": _compact(link.get("href") or "", 90)} for link in link_samples[:6]],
        "structure": [_compact(f"{item.get('ident', 'node')}: {item.get('summary', '')}", 130) for item in structure[:5]],
        "challenge": None
        if not challenge
        else {"provider": challenge.get("provider") or challenge.get("vendor") or "bot wall", "reason": challenge.get("reason") or challenge.get("hint") or "Challenge detected"},
    }


def _extract_facts(source: Source, raw: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not raw:
        return []
    blockmap = raw.get("blockmap") or {}
    headings = [item.get("text") for item in blockmap.get("headings", []) if _useful(item.get("text"))]
    links = [item.get("text") for item in (blockmap.get("interactives") or {}).get("link_samples", []) if _useful(item.get("text"))]
    unique = list(dict.fromkeys([*headings, *links]))[:2]
    return [
        {
            "source": source.name,
            "title": source.name,
            "fact": _compact(text, 170),
            "citations": [source.url.split("//", 1)[-1].split("/", 1)[0].removeprefix("www.")],
        }
        for text in unique
    ]


def _build_brief(results: list[dict[str, Any]], facts: list[dict[str, Any]], scenario: Scenario) -> dict[str, Any]:
    total = len(results)
    ok = sum(1 for item in results if item.get("status") == "ok")
    partial = sum(1 for item in results if item.get("status") == "partial")
    blocked = sum(1 for item in results if item.get("status") in {"blocked", "error", "fallback"})
    avoided = round((ok / total) * 100) if total else 0
    citations = list(dict.fromkeys(cite for fact in facts for cite in fact.get("citations", [])))[:12]
    return {
        "summary": f"Live {scenario.label} scan checked {total} fixed public sources. unbrowser fetched {ok} without a full browser, {partial} were partial or JS-heavy, and {blocked} needed blocker/error handling. Estimated full-browser sessions avoided: {avoided}%.",
        "bullets": list(scenario.bullets),
        "citations": citations,
    }


def _scenario_payload(scenario: Scenario, *, mode: str = "queued") -> dict[str, Any]:
    return {
        "id": scenario.id,
        "scenarioId": scenario.id,
        "label": scenario.label,
        "description": scenario.description,
        "prompt": scenario.prompt,
        "meta": scenario.meta,
        "mode": mode,
        "router": "fixed preset",
        "sourceCount": len(scenario.sources),
        "sources": [_source_payload(source) for source in scenario.sources],
    }


def _source_payload(source: Source) -> dict[str, Any]:
    return {"id": source.id, "name": source.name, "category": source.category, "url": source.url, "expected": source.expected}


async def _write_event(response: web.StreamResponse, event: str, payload: dict[str, Any]) -> None:
    await response.write(f"event: {event}\ndata: {json.dumps(payload, default=str)}\n\n".encode("utf-8"))


def _useful(text: str | None) -> bool:
    if not text or len(text.strip()) < 18:
        return False
    lowered = text.lower()
    return not any(term in lowered for term in ("privacy", "terms", "cookie", "linkedin", "facebook", "twitter", "youtube"))


def _compact(text: str, max_length: int) -> str:
    clean = " ".join(str(text or "").split())
    return clean if len(clean) <= max_length else clean[: max_length - 3] + "..."
