"""DOM Density Stream — Live-updating terminal visualization of page DOM.

Connects to CDP Chrome and continuously renders the DOM density map
at ~15-30fps using ANSI escape codes for flicker-free updates.

Usage:
    cd unchained/
    uv run dom_stream.py                     # Stream current page, 30s
    uv run dom_stream.py <url>               # Navigate + stream
    uv run dom_stream.py --secs 60           # Custom duration
    uv run dom_stream.py --cols 80           # Custom width
    uv run dom_stream.py --fps 15            # Cap framerate
    uv run dom_stream.py --ascii             # ASCII instead of Unicode blocks
"""

import asyncio
import json
import sys
import time

from cdp import CDP, _get_ws_url, _ensure_chrome

# Same walker JS as ddm.py but inlined to avoid import overhead
DOM_WALKER_JS = r"""
(function() {
    var vw = window.innerWidth, vh = window.innerHeight;
    var all = document.querySelectorAll('*');
    var elems = [];
    var CAP = 2000;

    for (var i = 0; i < all.length && elems.length < CAP; i++) {
        var el = all[i];
        var st = window.getComputedStyle(el);
        if (st.display === 'none' || st.visibility === 'hidden') continue;
        if (parseFloat(st.opacity) === 0) continue;

        var r = el.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) continue;
        if (r.right < 0 || r.bottom < 0 || r.left > vw || r.top > vh) continue;

        var x = Math.max(0, r.left);
        var y = Math.max(0, r.top);
        var w = Math.min(r.right, vw) - x;
        var h = Math.min(r.bottom, vh) - y;
        if (w <= 0 || h <= 0) continue;

        var tag = el.tagName.toLowerCase();
        var role = el.getAttribute('role');
        var k = null;
        var isInteractive = false;

        if (tag === 'button' || role === 'button' ||
            (tag === 'input' && (el.type === 'button' || el.type === 'submit' || el.type === 'reset'))) {
            k = 'B'; isInteractive = true;
        } else if (tag === 'input' || tag === 'textarea' || tag === 'select' ||
                   el.contentEditable === 'true' || role === 'textbox') {
            k = 'F'; isInteractive = true;
        } else if (tag === 'a' && el.href) {
            k = 'L'; isInteractive = true;
        } else if (tag === 'img' || tag === 'video' || tag === 'canvas' || tag === 'svg') {
            k = 'I';
        } else {
            var directText = '';
            for (var c = 0; c < el.childNodes.length; c++) {
                if (el.childNodes[c].nodeType === 3) {
                    directText += el.childNodes[c].textContent;
                }
            }
            if (directText.trim().length > 20) k = 'T';
        }

        var entry = {x: Math.round(x), y: Math.round(y),
                     w: Math.round(w), h: Math.round(h)};
        if (k) entry.k = k;
        if (isInteractive) {
            entry.i = true;
            var label = el.getAttribute('aria-label') || '';
            if (!label) {
                var eid = el.id;
                if (eid) {
                    var labelEl = document.querySelector('label[for="' + eid + '"]');
                    if (labelEl) label = labelEl.textContent.trim();
                }
            }
            if (!label) label = (el.textContent || '').trim().substring(0, 40);
            if (!label) label = el.title || el.placeholder || el.name || '';
            label = label.replace(/\s+/g, ' ').trim();
            if (label.length > 40) label = label.substring(0, 37) + '...';
            if (label) entry.l = label;
        }

        elems.push(entry);
    }

    return {vw: vw, vh: vh, count: elems.length, elements: elems,
            title: document.title, url: window.location.href,
            scroll: window.scrollY};
})()
"""

_PRIORITY = {'T': 1, 'I': 2, 'L': 3, 'F': 4, 'B': 5}

# Unicode block chars — visually dense
_BLOCK = {
    ' ': ' ', '.': '░', ':': '▒', '#': '▓', '@': '█',
    'B': '▣', 'F': '▤', 'L': '▨', 'I': '▧', 'T': '▥',
}

# ANSI colors for element types
_COLORS = {
    'B': '\033[1;33m',   # bold yellow — buttons
    'F': '\033[1;36m',   # bold cyan — inputs
    'L': '\033[1;34m',   # bold blue — links
    'I': '\033[1;35m',   # bold magenta — images
    'T': '\033[0;37m',   # gray — text
}
_RESET = '\033[0m'
_DIM = '\033[2m'


def render_frame(data, cols, ascii_mode=False):
    """Render one frame as a list of strings (no newline joining)."""
    vw = data['vw']
    vh = data['vh']
    elements = data['elements']

    cell_px = vw / cols
    rows = max(1, round(vh / cell_px))
    if cols * rows > 16000:
        rows = 16000 // cols

    density = [[0] * cols for _ in range(rows)]
    kinds = [[None] * cols for _ in range(rows)]
    interactive = []

    for el in elements:
        ex, ey, ew, eh = el['x'], el['y'], el['w'], el['h']
        kind = el.get('k')

        c0 = max(0, min(int(ex / cell_px), cols - 1))
        c1 = max(0, min(int((ex + ew - 1) / cell_px), cols - 1))
        r0 = max(0, min(int(ey / cell_px), rows - 1))
        r1 = max(0, min(int((ey + eh - 1) / cell_px), rows - 1))

        for r in range(r0, r1 + 1):
            for c in range(c0, c1 + 1):
                density[r][c] += 1
                if kind:
                    cur = kinds[r][c]
                    if cur is None or _PRIORITY.get(kind, 0) > _PRIORITY.get(cur, 0):
                        kinds[r][c] = kind

        if el.get('i'):
            interactive.append({
                'kind': kind, 'label': el.get('l', ''),
                'px': int(ex + ew / 2), 'py': int(ey + eh / 2),
            })

    interactive.sort(key=lambda e: (e['py'], e['px']))
    interactive = interactive[:20]  # fewer for streaming — keep it compact

    lines = []

    # Grid with ANSI colors
    for r in range(rows):
        row_parts = []
        for c in range(cols):
            k = kinds[r][c]
            d = density[r][c]
            if k:
                color = _COLORS.get(k, '')
                ch = k if ascii_mode else _BLOCK.get(k, k)
                row_parts.append(f"{color}{ch}{_RESET}")
            elif d == 0:
                row_parts.append(' ')
            else:
                if d == 1:
                    ch = '.' if ascii_mode else '░'
                elif d <= 3:
                    ch = ':' if ascii_mode else '▒'
                elif d <= 7:
                    ch = '#' if ascii_mode else '▓'
                else:
                    ch = '@' if ascii_mode else '█'
                row_parts.append(f"{_DIM}{ch}{_RESET}")
        lines.append(''.join(row_parts))

    return lines, interactive, rows, cols


async def stream(args):
    url = None
    max_cols = 70
    duration = 30
    target_fps = 20
    ascii_mode = False

    i = 0
    while i < len(args):
        if args[i] == '--cols' and i + 1 < len(args):
            max_cols = int(args[i + 1])
            i += 2
        elif args[i] == '--secs' and i + 1 < len(args):
            duration = int(args[i + 1])
            i += 2
        elif args[i] == '--fps' and i + 1 < len(args):
            target_fps = int(args[i + 1])
            i += 2
        elif args[i] == '--ascii':
            ascii_mode = True
            i += 1
        elif not args[i].startswith('-'):
            url = args[i]
            i += 1
        else:
            i += 1

    _ensure_chrome()
    ws_url = _get_ws_url()
    cdp = CDP(ws_url)
    await cdp.connect()

    try:
        if url:
            await cdp.navigate(url, wait=2)

        frame_interval = 1.0 / target_fps
        start = time.perf_counter()
        frame_count = 0
        fps_samples = []

        # Hide cursor
        sys.stdout.write('\033[?25l')
        # Clear screen
        sys.stdout.write('\033[2J')

        while time.perf_counter() - start < duration:
            frame_start = time.perf_counter()

            # Fetch DOM
            result = await cdp.execute_js(DOM_WALKER_JS)
            data = result.get("result", {}).get("value")
            if not data:
                await asyncio.sleep(0.1)
                continue

            js_time = time.perf_counter() - frame_start

            # Render
            render_start = time.perf_counter()
            grid_lines, interactive, grid_rows, grid_cols = render_frame(
                data, max_cols, ascii_mode=ascii_mode
            )
            render_time = time.perf_counter() - render_start

            # Calculate FPS
            frame_count += 1
            elapsed = time.perf_counter() - start
            current_fps = frame_count / elapsed if elapsed > 0 else 0
            fps_samples.append(current_fps)

            # Build full frame
            title = data.get('title', '')[:50]
            page_url = data.get('url', '')[:60]
            scroll_y = data.get('scroll', 0)
            n_elems = data['count']

            # Cursor home (no clear — reduces flicker)
            sys.stdout.write('\033[H')

            # Header
            header = (
                f"\033[1;32m{'═' * max_cols}\033[0m\n"
                f"\033[1m DOM DENSITY STREAM \033[0m  "
                f"{_COLORS['B']}▣\033[0m btn  "
                f"{_COLORS['F']}▤\033[0m input  "
                f"{_COLORS['L']}▨\033[0m link  "
                f"{_COLORS['I']}▧\033[0m img  "
                f"{_DIM}░▒▓█\033[0m density\n"
                f" {title}  scroll:{scroll_y}px\n"
                f" {page_url}\n"
                f"\033[1;32m{'─' * max_cols}\033[0m\n"
            )
            sys.stdout.write(header)

            # Grid
            for line in grid_lines:
                sys.stdout.write(line + '\n')

            # Footer with stats
            remaining = max(0, duration - elapsed)
            footer = (
                f"\033[1;32m{'─' * max_cols}\033[0m\n"
                f" {n_elems} elements  "
                f"{len(interactive)} interactive  "
                f"js:{js_time*1000:.0f}ms  "
                f"render:{render_time*1000:.0f}ms  "
                f"\033[1m{current_fps:.0f}fps\033[0m  "
                f"{remaining:.0f}s left\n"
            )
            sys.stdout.write(footer)

            # Interactive elements (compact, side-scrolling feel)
            n_show = min(10, len(interactive))
            for idx in range(n_show):
                item = interactive[idx]
                k = item['kind'] or '?'
                color = _COLORS.get(k, '')
                label = item['label'][:30] if item['label'] else ''
                sys.stdout.write(
                    f" {color}{k}{_RESET} {label:30s} "
                    f"({item['px']:>4},{item['py']:>4})\n"
                )
            # Clear remaining lines (in case previous frame had more)
            for _ in range(10 - n_show):
                sys.stdout.write('\033[K\n')

            sys.stdout.flush()

            # Frame pacing
            frame_time = time.perf_counter() - frame_start
            sleep_time = frame_interval - frame_time
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)

        # Summary
        total = time.perf_counter() - start
        avg_fps = frame_count / total if total > 0 else 0
        sys.stdout.write(f"\n\033[1mDone: {frame_count} frames in {total:.1f}s ({avg_fps:.1f} avg fps)\033[0m\n")

    finally:
        # Show cursor
        sys.stdout.write('\033[?25l\033[?25h')
        sys.stdout.flush()
        if cdp.ws:
            await cdp.ws.close()


def main():
    asyncio.run(stream(sys.argv[1:]))


if __name__ == "__main__":
    main()
