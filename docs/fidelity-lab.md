# Agent View Fidelity Lab

Systematically catalog rendering defects in the semantic Agent View mirror.
Each entry captures the site, viewport, observed defect, and fidelity
diagnostics so we can triage and fix the root causes.

## Setup

```bash
# Start the full local stack
cd unchained-infra
PRIVATE_CORE_PORT=9770 ./dev.sh agent-view

# Open in your normal browser (not the controlled 'dev' Chrome window):
#   http://localhost:8080/local?provider=opencode-cli
#   → Dev Login → prompt agent to navigate → open Browser Preview
```

## Fidelity Diagnostics

Inspect the snapshot fidelity object in the controller browser's DevTools
console:

```js
agentViewSnapshot?.fidelity
```

Key warning signals:

| Signal | Meaning |
|--------|---------|
| `criticalStylesTruncated: true` | Not all computed styles fit in the per-node or global budget |
| `bodyTruncated: true` | Body capture hit the 2 MB total limit |
| `large omittedInlineStyleBytes` | Inline `<style>` blocks dropped due to budget |
| `omittedAdoptedStyleSheets > 0` | Constructable stylesheets dropped |
| `inaccessibleStyleSheetLinks > 0` | Cross-origin stylesheets (not actionable on its own) |

Also check the body style property:

```js
agentViewSnapshot?.bodyAttrs?.style
```

Expected: contains `font-family`, `color`, `background-color`, etc. from the
source page's body. Missing paint properties here means the computed-style
fallback is not reaching the body element (likely a `isInViewport` or priority
filtering issue).

## Test Sites

### Category 1: Google Products (CSS-heavy, external stylesheets)

The original Google Finance case. Focus on sites where author stylesheets are
large and the computed-style fallback is the primary styling mechanism.

| Site | URL | Why | Viewport |
|------|-----|-----|----------|
| Google Search | `https://www.google.com/search?q=weather` | Minimal page, should mirror perfectly | 1280×800 |
| Google Finance | `https://www.google.com/finance` | Regression test — body font/color/background must survive | 1280×800 |
| Google Calendar (week) | `https://calendar.google.com` | Virtualized grid + complex layout | 1280×800 |
| Google Maps | `https://www.google.com/maps` | Tile-based rendering, canvas overlays | 1280×800 |
| YouTube (trending) | `https://www.youtube.com/feed/trending` | Web components, custom elements, video cards | 1280×800 |
| Gmail | `https://mail.google.com` | Heavy CSS-in-JS, sticky header | 1280×800 |
| Google Docs (simple doc) | `https://docs.google.com` | Canvas-based rendering (placeholder expected) | 1280×800 |

### Category 2: React / CSS-in-JS

Sites where styles are injected via JS at runtime (emotion, styled-components).
External stylesheets may be small or absent; the computed-style fallback is
the main source of paint properties.

| Site | URL | Why | Viewport |
|------|-----|-----|----------|
| Stripe docs | `https://stripe.com/docs` | Styled-components, lots of inline styles | 1280×800 |
| Linear | `https://linear.app` | Heavy CSS-in-JS, dark mode | 1280×800 |
| Vercel | `https://vercel.com` | Next.js + Tailwind, dark mode | 1280×800 |
| Notion (public page) | `https://www.notion.so/help` | Rich content, block-based layout | 1280×800 |
| Airbnb | `https://www.airbnb.com` | Full SPA with complex card layout | 1280×800 |
| Tremor | `https://tremor.so` | Dashboard components, dark mode | 1280×800 |

### Category 3: Dark Mode

Sites that switch themes via CSS custom properties on `<html>` or via
`prefers-color-scheme`. Test both modes.

| Site | URL | Why | Viewport |
|------|-----|-----|----------|
| GitHub (logged out) | `https://github.com` | Dark mode via custom properties | 1280×800 |
| Tailwind docs | `https://tailwindcss.com/docs` | Dark mode toggle, Tailwind utility classes | 1280×800 |
| MDN | `https://developer.mozilla.org` | Dark mode, lots of text content | 1280×800 |
| read.cv | `https://read.cv` | Dark-first design, custom fonts | 1280×800 |

### Category 4: Shadow DOM / Web Components

Sites that use shadow DOM for component encapsulation. The mirror uses
`<template shadowrootmode="open">` declarative shadow DOM to represent these.

| Site | URL | Why | Viewport |
|------|-----|-----|----------|
| Reddit (post page) | `https://www.reddit.com/r/all/top` | Web components (`shreddit-*` elements) | 1280×800 |
| YouTube video | `https://www.youtube.com/watch?v=dQw4w9WgXcQ` | Custom `<ytd-*>` elements | 1280×800 |
| Play Store | `https://play.google.com` | Web components | 1280×800 |

### Category 5: Sticky / Fixed Headers

Verify sticky positioning survives the snapshot when the page is scrolled.

| Site | URL | Why | Viewport |
|------|-----|-----|----------|
| Hacker News | `https://news.ycombinator.com` | Sticky header, minimal CSS, regression test | 1280×800 |
| NY Times (article) | `https://www.nytimes.com` | Sticky header + ad slots | 1280×800 |
| Stack Overflow | `https://stackoverflow.com/questions` | Sticky nav + sidebar | 1280×800 |

### Category 6: Virtualized Tables / Lists

Sites where only visible rows are in the DOM. The mirror should capture what
is visible in the viewport.

| Site | URL | Why | Viewport |
|------|-----|-----|----------|
| Airtable (public base) | `https://airtable.com` | Virtualized spreadsheet | 1280×800 |
| NPM (package page) | `https://www.npmjs.com/package/react` | Tab-based content, lazy rendering | 1280×800 |

### Category 7: Responsive / Mobile

Test at narrow viewports to verify mobile layouts are mirrored correctly.

| Site | URL | Viewport |
|------|-----|----------|
| Google Search | `https://www.google.com/search?q=weather` | 390×844 |
| GitHub | `https://github.com` | 390×844 |
| Tailwind docs | `https://tailwindcss.com/docs/installation` | 390×844 |

## Test Procedure

For each site:

1. **Manually open the source page** in the controlled `dev` Chrome at the
   specified viewport. Note the visual appearance — fonts, colors, backgrounds,
   layout, sticky elements.

2. **Prompt the agent** in the controller browser to navigate to the same URL.
   Open **Browser Preview** to see the Agent View.

3. **Compare** the source page and the Agent View for:
   - Missing font-family (defaults to Times/serif)
   - Missing background-color/color (white on white, blue links)
   - Horizontal layout becoming vertical
   - Wrong widths, gaps, or positioning
   - Hidden/omitted elements becoming visible
   - Broken sticky/fixed headers
   - Missing icons or pseudo-elements
   - Dark mode vs light mode mismatch

4. **Capture diagnostics** from the console:
   ```js
   JSON.stringify(agentViewSnapshot?.fidelity, null, 2)
   ```

5. **Record the result** below. If the page looks correct, mark it PASS.
   If there's a visual defect, describe what's wrong and include the fidelity
   diagnostics.

## Results

### Automated Fidelity Scan (2026-07-13)

Full report: `unchained/fidelity_lab_report.json`

| Site | Category | Status | Font | Color | BG | Crit Trunc | Body Trunc | Snapshot | Key Issues |
|------|----------|--------|------|-------|----|------------|------------|----------|------------|
| google-search | google | PASS | YES | YES | YES | ok | ok | 1045KB | |
| google-finance | google | FAIL | YES | YES | YES | ok | TRUNC | 1242KB | body-truncated; 626KB inline styles omitted |
| google-maps | google | FAIL | YES | YES | YES | ok | ok | 336KB | head-budget truncated; 225KB inline styles omitted |
| stripe-docs | react | PASS | YES | YES | YES | ok | ok | 514KB | |
| vercel | react | PASS | YES | YES | YES | ok | ok | 362KB | |
| airbnb | react | FAIL | YES | YES | YES | TRUNC | TRUNC | 1161KB | 524KB critical styles; body-truncated |
| github | dark-mode | PASS | YES | YES | YES | ok | ok | 590KB | |
| tailwind-docs | dark-mode | PASS | YES | YES | YES | ok | ok | 390KB | |
| mdn | dark-mode | PASS | YES | YES | YES | ok | ok | 723KB | |
| reddit | web-components | FAIL | YES | YES | YES | ok | TRUNC | 1782KB | 1171 adopted stylesheets omitted; body-truncated |
| youtube | web-components | FAIL | NO | NO | NO | ok | ok | 381KB | empty bodyAttrs.style (web-component SPA) |
| hacker-news | sticky | PASS | YES | YES | YES | ok | ok | 521KB | |
| stack-overflow | sticky | PASS | YES | YES | YES | ok | ok | 798KB | |
| npm-react | virtualized | PASS | YES | YES | YES | ok | ok | 210KB | |
| google-search-mobile | mobile | FAIL | YES | YES | YES | TRUNC | TRUNC | 1371KB | 524KB critical styles; body-truncated |

### Manual Visual Checks

| Date | Site | URL | Viewport | Result | Fidelity Notes |
|------|------|-----|----------|--------|----------------|
| 2026-07-13 | Google Finance | finance.google.com | 1280×800 | FIXED in PR #408 | `criticalStylesTruncated: false` after fix; `font-family`/`color`/`background-color` present in body style |
| 2026-07-13 | YouTube | youtube.com | 1280×900 | PASS (visual) | Automated: empty bodyAttrs.style — visual inspection OK, web-component SPA doesn't rely on body styles |
| 2026-07-13 | CNN | cnn.com | 1280×900 | FAIL (visual) | 2.7MB inline CSS in 13 style blocks — only 330KB captured (128KB budget); 2.37MB dropped; bodyAttrs.style correct (`font-family: cnn_sans_display`, `color: black`, `bg: white`) but inner elements (nav, cards, grid) lose styling from omitted inline stylesheets |
| 2026-07-13 | Google Finance | finance.google.com | 1280×900 | FAIL (visual) | 664KB inline CSS in 50 style blocks (largest: 577KB) — only 103KB captured; 611KB omitted; body style correct (`font-family: Google Sans`, `color: rgb(10,10,10)`, `bg: white`); critical style fallback: 390KB applied to visible elements; bodyBudget truncated (1116KB body); layout/cards/grid lose class-based rules |
| 2026-07-13 | Yahoo Finance | finance.yahoo.com | 1280×900 | PASS (visual) | 1186KB snapshot; bodyTruncated=false; `font-family: GT America`; criticalStylesTruncated=false; 1 inaccessible stylesheet; inline styles fit within budget |

### Known Defect Patterns

1. **Inline style budget exhaustion** — Sites with 500KB+ inline CSS (CNN: 2.7MB, Google Finance: 664KB). The 128KB `styleLimit` captures only 5-19% of the author CSS. Inner elements lose their class-based layout rules; the computed-style fallback captures ~390KB of critical styles on visible elements but the 768-byte per-node budget (~14-15 properties) isn't enough for complex grid/card layouts. Candidate fixes:
   - Raise the `styleLimit` from 128KB to 512KB+ (budget comes from `bodyLimit - criticalStyleLimit`, currently ~1MB → 512KB would still leave ~500KB for content)
   - Increase per-node critical style budget from 768B to 1.5KB when critical styles are truncated
   - Implement viewport-scoped CSS rule filtering (only keep selectors that match visible elements)

2. **Empty body styles in web-component SPAs** — YouTube, possibly other sites where `<body>` has zero area and styles live on `<ytd-app>` or similar custom elements. Visually fine because the mirror still captures the element tree, but diagnostic flags it as missing paint properties.

3. **Budget truncation on large pages** — Airbnb, Reddit, Google Finance have 1MB+ pages. The 2MB total capture limit truncates the body. Paint properties survive (font-family/color/background-color in bodyAttrs.style) but inner elements may lose styling deeper in the tree.
