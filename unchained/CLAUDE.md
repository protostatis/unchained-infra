# CLAUDE.md — Unchained Browser Agent

This directory contains the Unchained browser automation platform. When running as a chat agent (`chat_agent_cli.py`), Claude controls a remote Chrome browser through `cdp_tool.py` over a WSS relay.

## Critical CDP Rules

### #1: DDM First, Always
**ALWAYS use DDM for orientation and `--at` for element details. NEVER take a screenshot unless DDM can't answer (CAPTCHAs, visual state, images).** Screenshots cost ~2,100 tokens vs ~500 for DDM. Default to DDM for every navigation, every page check, every element lookup.

### #2: No More Than 1 Click Per Second
**NEVER click faster than 1 per second.** Rapid clicks trigger anti-bot detection and cause actions to silently fail.

### #3: Navigate and Click Return DDM — No Separate Call Needed
**`navigate` and `click` already return DDM page layout in their output (under "=== Page Layout ===").** Read that section to verify the page changed — do NOT call `ddm` separately after them. Only call `ddm` separately after `type`, or for `--text`, `--at x,y`, `--find`, `--js` flags. If DDM shows the same elements after an action, the action failed silently — try a different approach (JS `.click()`, URL params, different selector).

### #4: Click to Focus Before Typing
**Before typing, always click the target input field first.** Key events go to whichever element has focus — if nothing is focused, the event goes nowhere and silently fails.

### #5: Probe on First Visit to Every New Domain
**On the FIRST page load of any new domain, run `intel --probe` immediately after DDM.** Cost is negligible (~120ms, ~100 tokens). Then apply the result:
- If `js_global > 50%` → switch to store-based extraction: `intel --stores` → `intel --find-paths` → `js` to read
- If `host_attrs > 50%` → use `intel --extract` with `host_attrs` strategy
- If `data_testid > 40%` → use `intel --extract` with `data_testid` strategy
- Otherwise → stick with DDM-only (`--text`, `--at`, JS `querySelectorAll`)

Skip probe on subsequent pages within the same domain — the strategy won't change.

## Browser Tools

All browser tools are accessed via `cdp_tool.py`:

```bash
# DDM — DOM Density Map (use FIRST on every page)
uv run python cdp_tool.py ddm --llm-2pass --cols 60     # Map page layout + elements (~500 tok)
uv run python cdp_tool.py ddm --text                     # Extract page text (~3000 chars)
uv run python cdp_tool.py ddm --text --find keyword      # Search text on page
uv run python cdp_tool.py ddm --text --max 5000          # More text (custom limit)
uv run python cdp_tool.py ddm --at 694,584               # Element details at pixel coords
uv run python cdp_tool.py ddm --js "expression"          # Execute JS on page, return JSON

# Navigation & Interaction
uv run python cdp_tool.py navigate https://example.com   # Go to URL (returns page layout — no ddm needed)
uv run python cdp_tool.py click 500 300                  # Click at coordinates (returns page layout — no ddm needed)
uv run python cdp_tool.py type "search query"            # Type into focused input
uv run python cdp_tool.py js "document.title"            # Run JavaScript on page

# Page Intelligence
uv run python cdp_tool.py intel --probe                  # Page fingerprint + strategy (~100 tok)
uv run python cdp_tool.py intel --extract                # Extract structured data
uv run python cdp_tool.py intel --stores                 # List JS data store globals
uv run python cdp_tool.py intel --shape __NUXT__         # Map object tree of a global
uv run python cdp_tool.py intel --find-paths __NUXT__ deals  # Find data arrays in global

# Screenshot (last resort)
uv run python cdp_tool.py screenshot                     # Only for CAPTCHAs, visual verification

# Tab Management (all via ddm subcommand)
uv run python cdp_tool.py ddm --tabs                          # List all open tabs (ID, title, URL)
uv run python cdp_tool.py ddm --new https://example.com       # Open new tab at URL
uv run python cdp_tool.py ddm --close <tab_id>                # Close tab by ID prefix

# Add --tab <id> to ANY command to target a specific tab
uv run python cdp_tool.py navigate https://x.com --tab <tab_id>
uv run python cdp_tool.py ddm --text --tab <tab_id>
uv run python cdp_tool.py js "document.title" --tab <tab_id>
```

### When to Use Multiple Tabs

- **Parallel research**: Open each source in its own tab, switch between them to compare
- **Reference pages**: Keep docs/pricing open while working in another tab
- **Comparison**: Open multiple product pages side by side
- **Form workflows**: Keep a reference page open while filling a form in another tab

### Tab Workflow

1. `ddm --tabs` to see what's open
2. `ddm --new <url>` to open a new page in a new tab
3. Use `--tab <id>` on any command to target a specific tab
4. `ddm --close <id>` when done with a tab
5. Omit `--tab` to use the default first tab (backward compatible)

Tab IDs are Chrome-assigned strings. Use the first 6-12 characters as a prefix (prefix matching).

## DDM-First Methodology

Every browsing task follows this pipeline:

**Step 1: ORIENT** — `navigate` and `click` already return DDM page layout in their output (under "=== Page Layout ==="). Read that — do NOT call `ddm` separately after them. Only call `ddm` separately after `type`, or for `--text`, `--at x,y`, `--find`, `--js`.

**Step 2: IDENTIFY** — `ddm --at x,y` on targets from Step 1
Returns href, class, text, aria-* for elements you want to interact with.

**Step 3: CLASSIFY** — `intel --probe` on unknown SPAs
Fingerprints the page and ranks 8 extraction strategies. Reveals framework (Nuxt/Next/React), data stores, shadow DOM.

**Step 4: ACT** — Use coordinates from DDM to click, or navigate to URLs from --at
For SPA widgets, use `js` with `.click()` on the element.

**Step 5: VERIFY** — After `navigate` or `click`, check the "=== Page Layout ===" in their output. After `type` or other actions, run `ddm` to verify.

**Step 6: EXTRACT** — Choose method based on page type:
- Simple text (HN, Wikipedia, blogs): `ddm --text --max 5000`
- Shadow DOM (Reddit): `intel --extract` with host_attrs strategy
- SPA with data store (Nuxt, YouTube): `intel --stores` → `intel --find-paths` → `js` to read
- Structured data (lists, tables): `js` with querySelectorAll
- data-testid rich (GitHub, Weather): `intel --extract` with data_testid strategy

## DDM vs JS Decision Rules

| Task | Use DDM | Use JS |
|------|---------|--------|
| What's on this page? | `--llm-2pass` | — |
| Where is button/input X? | `--llm-2pass` → read interactive list | — |
| Get href for a link | `--at x,y` | — |
| Read text at a position | `--at x,y` | — |
| Read full page text | `--text` or `--text --max 5000` | — |
| Find specific text | `--text --find "keyword"` | — |
| Get all prices/titles/items | — | `js` with querySelectorAll().map() |
| Fill a form field | `--at` to find input IDs | `js` with .value = |
| Click a simple button | DDM coords → click tool | — |
| Click SPA widgets (dropdowns) | DDM to find it | `js` with .click() |
| Read a table | — | `js` with querySelectorAll('th, td') |
| Interact with web components | — | `js` into shadow DOM |
| Verify action worked | `--llm-2pass` | — |
| Extract structured data from many items | — | `intel --probe` then querySelectorAll |

## Per-Site-Type Quick Reference

| Site Type | Best Approach |
|-----------|---------------|
| Link-heavy (HN, Reddit, docs) | DDM-only. Reddit: `intel --extract` for post data |
| E-commerce (Amazon, eBay) | DDM orient + `intel --probe` + JS for price extraction |
| SPAs w/ data store (YouTube, Nuxt) | `intel --stores → --find-paths` then JS to extract |
| SPAs w/o data store (Flights, Gmail) | DDM for form mapping, JS for widget interaction |
| Text-heavy (Wikipedia, blogs) | `ddm --text --max 5000` |
| Simple pages (landing pages) | Single `ddm --llm-2pass` covers everything |
| data-testid rich (GitHub, Weather) | `intel --extract --strategy data_testid` |
| Property/real estate (Zillow, Redfin) | `ddm --text --max 8000` for data-dense pages |
| Job boards (Indeed, LinkedIn) | DDM shows filters, JS extracts all listings |
| Government sites (state portals) | DDM `--at` finds fields, JS fills + submits. CAPTCHAs may block |

## Key Gotchas

- **SPA widgets** (Google Flights, date pickers): CDP mouse clicks often fail on custom widgets. Use DDM to *find* elements, JS `.click()` to *interact*.
- **Viewport-bound**: DDM only sees current viewport. Scroll + remap for content below the fold.
- **Cookie/login walls**: DDM detects immediately (shows only banner elements). innerText may return misleading partial text.
- **ASP.NET/postback forms**: JS `.value` + `.click()` won't trigger postback. Use `__doPostBack()` or skip the site.
- **CAPTCHA**: Government sites often trigger after first submission. Do one search, extract everything, then use different data source.
- **PDF in Chrome**: DDM returns 0 elements. Use WebFetch or navigate to HTML version.
- **50-element cap**: DDM interactive list maxes at 50. Complex pages may cut off lower elements.

## Page Intelligence — 8 Strategies

| Strategy | Trigger signals | Example site |
|----------|----------------|-------------|
| `innerText` | No special signals | HN, Wikipedia, Craigslist |
| `host_attrs` | shadow>50 + rich attrs | Reddit |
| `js_global` | Known global >10KB | YouTube, Slickdeals |
| `react_fiber` | React fiber on #root | React SPAs |
| `data_testid` | testids >20 | Weather.com, GitHub |
| `heading_hier` | iframes >2, news/media | CNN |
| `img_alt` | Many images, no custom els | Amazon |
| `shadow_pierce` | Shadow roots, no rich attrs | Web components |
