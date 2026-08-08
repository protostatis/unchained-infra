#!/usr/bin/env python3
"""Automated fidelity lab checker — installs mirror, inspects snapshot fidelity.

For each test site:
  1. Set viewport and navigate
  2. Install the semantic mirror (same JS Browser Preview uses)
  3. Evaluate the snapshot via canonical chunked transport
  4. Check fidelity diagnostics + bodyAttrs.style paint survival

Usage:
    cd unchained-infra/unchained
    PRIVATE_CORE_URL=http://127.0.0.1:9770 \
    PRIVATE_CORE_MODE=http \
    PRIVATE_CORE_TOKEN="$(cat /tmp/unchained-dev/private-core-token)" \
    CDP_AGENT_ID=claude-3469b40d-dev \
    CDP_RELAY_HOST=127.0.0.1 \
    CDP_RELAY_PORT=8765 \
      uv run python fidelity_lab_checker.py
"""

import asyncio
import json
import os
import re
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cloud_tools
from web_app.semantic_mirror import (
    build_dispose_mirror_expression,
    build_install_mirror_expression,
    evaluate_mirror_payload,
)

# Config from env (mirrors cdp_tool.py)
AGENT_ID = os.environ.get("CDP_AGENT_ID", "claude-7fba49f4")
RELAY_HOST = os.environ.get("CDP_RELAY_HOST", "api.unchainedsky.com")
RELAY_PORT = int(os.environ.get("CDP_RELAY_PORT", "443"))

# ── Coverage Matrix ───────────────────────────────────────────────────────────

TEST_SITES = [
    # Google products (CSS-heavy, external stylesheets)
    ("google-search",   "https://www.google.com/search?q=weather+seattle",       1280, "google"),
    ("google-finance",  "https://www.google.com/finance",                        1280, "google"),
    ("google-maps",     "https://www.google.com/maps",                           1280, "google"),

    # React / CSS-in-JS
    ("stripe-docs",     "https://stripe.com/docs",                               1280, "react"),
    ("vercel",          "https://vercel.com",                                    1280, "react"),
    ("airbnb",          "https://www.airbnb.com",                                1280, "react"),

    # Dark mode
    ("github",          "https://github.com",                                    1280, "dark-mode"),
    ("tailwind-docs",   "https://tailwindcss.com/docs/installation",             1280, "dark-mode"),
    ("mdn",             "https://developer.mozilla.org/en-US/docs/Web/CSS",      1280, "dark-mode"),

    # Web Components / Shadow DOM
    ("reddit",          "https://www.reddit.com/r/all/top",                      1280, "web-components"),
    ("youtube",         "https://www.youtube.com/feed/trending",                 1280, "web-components"),

    # Sticky headers
    ("hacker-news",     "https://news.ycombinator.com",                          1280, "sticky"),
    ("stack-overflow",  "https://stackoverflow.com/questions",                   1280, "sticky"),

    # Virtualized tables
    ("npm-react",       "https://www.npmjs.com/package/react",                   1280, "virtualized"),

    # Responsive mobile
    ("google-search-mobile", "https://www.google.com/search?q=weather+seattle",  390,  "mobile"),
]

KEY_PAINT_PROPS = ["font-family", "color", "background-color", "display", "position"]
SALIENT_STYLE_RULE_RE = re.compile(
    r'\[data-ucm-cs~="([^"]+)"\]\{([^{}]*)\}'
)


def style_property_names(style: str) -> set[str]:
    """Extract CSS property names from a semicolon-separated style string."""
    names = set()
    for decl in style.split(";"):
        if ":" in decl:
            names.add(decl.split(":", 1)[0].strip().lower())
    return names


def resolved_style_for_attributes(attributes: dict, salient_styles: str) -> str:
    """Combine source inline style with projected computed-style tokens."""
    tokens = set(str(attributes.get("data-ucm-cs", "")).split())
    projected = [
        declarations
        for token, declarations in SALIENT_STYLE_RULE_RE.findall(salient_styles or "")
        if token in tokens
    ]
    return ";".join(filter(None, [str(attributes.get("style", "")), *projected]))


async def set_viewport(tab_id: str, width: int, height: int = 900):
    """Set device metrics via CDP."""
    await cloud_tools.run_cdp_command(
        AGENT_ID, tab_id, "Emulation.setDeviceMetricsOverride",
        {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": width <= 480},
        RELAY_HOST, RELAY_PORT,
    )


async def resolve_tab_id() -> str:
    """Get a concrete tab ID for the controlled Chrome."""
    tabs_raw = await cloud_tools.run_cdp_command(
        AGENT_ID, "auto", "Target.getTargets", {},
        RELAY_HOST, RELAY_PORT,
    )
    if isinstance(tabs_raw, dict):
        targets = tabs_raw.get("targetInfos", [])
        # Prefer the page tab (not DevTools, not extensions)
        for t in targets:
            if t.get("type") == "page":
                return t["targetId"]
        if targets:
            return targets[0]["targetId"]
    return "auto"


async def capture_snapshot(tab_id: str, timeout: float = 45.0) -> dict | None:
    """Install mirror, evaluate snapshot, dispose mirror. Returns snapshot dict."""
    mirror_key = f"unchained.fidelity.{uuid.uuid4().hex}"
    install_expr = build_install_mirror_expression(mirror_key)
    dispose_expr = build_dispose_mirror_expression(mirror_key)

    try:
        return await asyncio.wait_for(
            evaluate_mirror_payload(
                AGENT_ID, tab_id, install_expr,
                RELAY_HOST, RELAY_PORT, mirror_key=mirror_key,
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        return None
    except Exception:
        return None
    finally:
        # Best-effort cleanup
        try:
            await cloud_tools.run_js(
                AGENT_ID, tab_id, dispose_expr, RELAY_HOST, RELAY_PORT,
            )
        except Exception:
            pass


async def test_site(site_id: str, url: str, viewport_width: int, category: str) -> dict:
    """Navigate, capture, inspect fidelity."""
    result = {
        "site_id": site_id, "url": url, "category": category,
        "viewport_width": viewport_width,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status": "error", "title": "", "final_url": "",
        "snapshot_bytes": 0, "issues": [],
    }

    try:
        tab_id = await resolve_tab_id()
        if tab_id == "auto":
            result["issues"].append("no-page-tab-found")
            return result

        # Set viewport
        await set_viewport(tab_id, viewport_width)
        await asyncio.sleep(0.5)

        # Navigate
        await cloud_tools.navigate(AGENT_ID, tab_id, url, RELAY_HOST, RELAY_PORT)
        await asyncio.sleep(4)  # Let page settle

        # Get page info
        result["title"] = await cloud_tools.run_js(
            AGENT_ID, tab_id, "document.title", RELAY_HOST, RELAY_PORT,
        ) or ""
        result["final_url"] = await cloud_tools.run_js(
            AGENT_ID, tab_id, "location.href", RELAY_HOST, RELAY_PORT,
        ) or ""

        # Capture snapshot
        snapshot = await capture_snapshot(tab_id, timeout=30)
        if not snapshot:
            result["issues"].append("mirror-capture-failed")
            return result

        # Fidelity diagnostics
        fidelity = snapshot.get("fidelity", {})
        body_attrs = snapshot.get("bodyAttrs", {})
        body_style = resolved_style_for_attributes(
            body_attrs,
            snapshot.get("salientStyles", ""),
        )

        # Use rawBytes from the snapshot itself
        result["snapshot_bytes"] = snapshot.get("rawBytes", 0) or len(json.dumps(snapshot).encode("utf-8"))

        # Check paint properties in bodyAttrs.style
        props = style_property_names(body_style)

        paint_found = {}
        for prop in KEY_PAINT_PROPS:
            paint_found[prop] = prop in props

        diagnostics = {
            "criticalStylesTruncated": fidelity.get("criticalStylesTruncated", False),
            "criticalStyleBytes": fidelity.get("criticalStyleBytes", 0),
            "criticalStyleExpandedBytes": fidelity.get("criticalStyleExpandedBytes", 0),
            "criticalStyleRuleBytes": fidelity.get("criticalStyleRuleBytes", 0),
            "criticalStyleReferenceBytes": fidelity.get("criticalStyleReferenceBytes", 0),
            "bodyTruncated": fidelity.get("bodyTruncated", False),
            "bodyStyleString": body_style[:300] if body_style else "(empty)",
            "paintPropertiesInBodyStyle": paint_found,
            "omittedAdoptedStyleSheets": fidelity.get("omittedAdoptedStyleSheets", 0),
            "omittedInlineStyleBytes": fidelity.get("omittedInlineStyleBytes", 0),
            "inaccessibleStyleSheetLinks": fidelity.get("inaccessibleStyleSheetLinks", 0),
            "truncationStage": fidelity.get("truncationStage", ""),
            "viewport": snapshot.get("viewport", {}),
        }

        # Assess issues
        issues = []
        if fidelity.get("criticalStylesTruncated"):
            issues.append(f"critical-styles-truncated-({fidelity['criticalStyleBytes']}B)")
        if fidelity.get("bodyTruncated"):
            issues.append("body-truncated")
        if not paint_found.get("font-family"):
            issues.append("body-style-missing-font-family")
        if not paint_found.get("color"):
            issues.append("body-style-missing-color")
        if not paint_found.get("background-color"):
            issues.append("body-style-missing-background-color")
        if fidelity.get("omittedAdoptedStyleSheets", 0):
            issues.append(f"omitted-adopted-stylesheets-({fidelity['omittedAdoptedStyleSheets']})")
        if fidelity.get("omittedInlineStyleBytes", 0) > 4096:
            issues.append(f"omitted-inline-style-({fidelity['omittedInlineStyleBytes']}B)")
        if fidelity.get("truncated"):
            issues.append(f"snapshot-truncated-({fidelity.get('truncationStage', '?')})")

        result["status"] = "pass" if not issues else "fail"
        result["issues"] = issues
        result["diagnostics"] = diagnostics

    except Exception as e:
        result["status"] = "error"
        result["issues"].append(str(e))

    return result


def print_summary(results: list[dict]):
    """Print formatted summary."""
    print("\n" + "=" * 140)
    h = f"{'SITE':<24} {'CATEGORY':<16} {'STATUS':<8} {'FONT':<6} {'COLOR':<6} {'BG':<6} {'CRIT':<8} {'BODY':<8} {'SIZE':<8} {'ISSUES'}"
    print(h)
    print("=" * 140)
    for r in results:
        d = r.get("diagnostics", {})
        status = "PASS" if r["status"] == "pass" else "FAIL"
        pp = d.get("paintPropertiesInBodyStyle", {})
        font = "YES" if pp.get("font-family") else "NO"
        color = "YES" if pp.get("color") else "NO"
        bg = "YES" if pp.get("background-color") else "NO"
        crit = "TRUNC" if d.get("criticalStylesTruncated") else "ok"
        body = "TRUNC" if d.get("bodyTruncated") else "ok"
        size = f"{r.get('snapshot_bytes', 0)//1024}KB"
        issues = "; ".join(r.get("issues", []))[:60]
        print(f"{r['site_id']:<24} {r['category']:<16} {status:<8} {font:<6} {color:<6} {bg:<6} {crit:<8} {body:<8} {size:<8} {issues}")


async def main():
    print("Warming up connection...")
    tab_id = await resolve_tab_id()
    if tab_id == "auto":
        print("ERROR: Cannot resolve a page tab in the controlled Chrome.")
        sys.exit(1)

    # Navigate to blank to ensure clean state
    await cloud_tools.navigate(AGENT_ID, tab_id, "about:blank", RELAY_HOST, RELAY_PORT)
    await asyncio.sleep(1)

    all_results = []

    for site_id, url, vp_width, category in TEST_SITES:
        print(f"\n{'─' * 60}")
        print(f"[{site_id}] {category} @ {vp_width}px")
        print(f"  URL: {url}")
        print(f"{'─' * 60}")

        result = await test_site(site_id, url, vp_width, category)
        all_results.append(result)

        if result["status"] == "pass":
            print(f"  ✓ PASS")
        else:
            print(f"  ✗ {result['status'].upper()}")
            for issue in result.get("issues", []):
                print(f"    ⚠ {issue}")

        d = result.get("diagnostics", {})
        if d:
            bs = d.get("bodyStyleString", "")
            print(f"  body style ({len(bs)}B): {bs[:200]}")
            print(f"  snapshot: {result.get('snapshot_bytes', 0)//1024}KB, "
                  f"criticalStylesTruncated={d.get('criticalStylesTruncated')}, "
                  f"bodyTruncated={d.get('bodyTruncated')}")

    # Summary
    print_summary(all_results)

    report_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "fidelity_lab_report.json"
    )
    with open(report_path, "w") as f:
        serializable = [{k: v for k, v in r.items()} for r in all_results]
        json.dump(serializable, f, indent=2, default=str)
    print(f"\nReport saved to: {report_path}")

    passes = sum(1 for r in all_results if r["status"] == "pass")
    fails = sum(1 for r in all_results if r["status"] == "fail")
    errs = sum(1 for r in all_results if r["status"] == "error")
    print(f"\n{'=' * 60}")
    print(f"Results: {passes} PASS, {fails} FAIL, {errs} ERROR ({len(all_results)} total)")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    asyncio.run(main())
