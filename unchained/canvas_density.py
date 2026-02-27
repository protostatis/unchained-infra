"""Canvas Density Map — Extract text, lines, and shapes from canvas elements.

Connects to CDP Chrome (port 9515), injects draw interceptors on the
CanvasRenderingContext2D prototype, forces a re-render, and captures
all text labels, line paths, and filled shapes.

Usage:
    cd unchained/
    uv run canvas_density.py                     # Capture current page
    uv run canvas_density.py <url>               # Navigate + capture
    uv run canvas_density.py --summary           # Compact one-line summary
    uv run canvas_density.py --text              # All canvas text (default)
    uv run canvas_density.py --lines             # Line point data
    uv run canvas_density.py --all               # Everything
"""

import asyncio
import json
import sys

from cdp import CDP, _get_ws_url, _ensure_chrome


INJECT_JS = r"""
(function() {
    window.__cc = {text: [], lines: [], fills: [], strokes: [], installed: true};
    var proto = CanvasRenderingContext2D.prototype;

    // --- Text ---
    var _ft = proto.fillText;
    proto.fillText = function(t, x, y) {
        window.__cc.text.push({t: String(t), x: Math.round(x), y: Math.round(y),
                               f: this.font, c: this.fillStyle, m: 'fill'});
        return _ft.apply(this, arguments);
    };
    var _st = proto.strokeText;
    proto.strokeText = function(t, x, y) {
        window.__cc.text.push({t: String(t), x: Math.round(x), y: Math.round(y),
                               f: this.font, c: this.strokeStyle, m: 'stroke'});
        return _st.apply(this, arguments);
    };

    // --- Lines (sample every 3rd point) ---
    var _lt = proto.lineTo;
    var lc = 0;
    proto.lineTo = function(x, y) {
        lc++;
        if (lc % 3 === 0) {
            window.__cc.lines.push({x: Math.round(x), y: Math.round(y),
                                    c: this.strokeStyle, w: this.lineWidth});
        }
        return _lt.apply(this, arguments);
    };

    // --- Fill paths (for volume bars, area fills) ---
    var _fill = proto.fill;
    proto.fill = function() {
        window.__cc.fills.push({c: this.fillStyle});
        return _fill.apply(this, arguments);
    };

    // --- fillRect ---
    var _fr = proto.fillRect;
    proto.fillRect = function(x, y, w, h) {
        if (w > 0.5 && h > 2) {
            window.__cc.strokes.push({x: Math.round(x), y: Math.round(y),
                                      w: Math.round(w), h: Math.round(h), c: this.fillStyle});
        }
        return _fr.apply(this, arguments);
    };
})()
"""

EXTRACT_JS = r"""
(function() {
    var c = window.__cc;
    if (!c) return JSON.stringify({error: "no capture"});
    return JSON.stringify({
        tc: c.text.length, lc: c.lines.length,
        fc: c.fills.length, rc: c.strokes.length,
        text: c.text.slice(0, 500),
        lines: c.lines.slice(0, 200),
        rects: c.strokes.slice(0, 100)
    });
})()
"""

CLEANUP_JS = "delete window.__cc;"


def _parse_texts(texts):
    """Deduplicate and sort canvas text entries."""
    seen = set()
    unique = []
    for t in texts:
        key = (t['t'], t['x'], t['y'])
        if key not in seen:
            seen.add(key)
            unique.append(t)
    unique.sort(key=lambda t: (t['y'], t['x']))
    return unique


def _classify_axis(texts):
    """Separate texts into y-axis (prices), x-axis (times), and other."""
    y_axis = []    # right-aligned numbers (prices)
    x_axis = []    # bottom-aligned times/dates
    other = []

    if not texts:
        return y_axis, x_axis, other

    max_y = max(t['y'] for t in texts)
    max_x = max(t['x'] for t in texts)

    for t in texts:
        val = t['t'].strip()
        # Time labels: typically at the bottom (high y), contain AM/PM or :
        if t['y'] > max_y - 30 and ('AM' in val or 'PM' in val or '/' in val):
            x_axis.append(t)
        # Price labels: typically right-aligned (high x), are numbers
        elif t['x'] > max_x - 60:
            try:
                float(val.replace(',', '').replace('K', '000').replace('M', '000000'))
                y_axis.append(t)
            except ValueError:
                other.append(t)
        else:
            other.append(t)

    return y_axis, x_axis, other


def render_summary(data, title="", url=""):
    """Compact summary: price range, time range, current price."""
    texts = _parse_texts(data.get('text', []))
    y_axis, x_axis, other = _classify_axis(texts)

    lines = []
    lines.append("=== Canvas Chart Summary ===")
    if title:
        lines.append(f"Page: {title}")
    if url:
        lines.append(f"URL: {url}")

    # Price range from y-axis labels
    prices = []
    volume_label = None
    current_price = None
    for t in y_axis:
        val = t['t'].strip()
        if 'K' in val or 'M' in val:
            volume_label = val
            continue
        try:
            p = float(val.replace(',', ''))
            prices.append((p, t['y']))
        except ValueError:
            pass

    if prices:
        prices.sort(key=lambda x: x[0])
        low = prices[0][0]
        high = prices[-1][0]
        lines.append(f"Price range: ${low:.2f} - ${high:.2f}")

        # Current price = the non-round number (has decimals not .00)
        for p, y in prices:
            frac = p - int(p)
            if frac != 0 and abs(frac - round(frac, 0)) > 0.001:
                current_price = p
                break
        if current_price:
            lines.append(f"Current: ${current_price:.2f}")

    if volume_label:
        lines.append(f"Volume: {volume_label}")

    # Time range from x-axis labels
    times = [t['t'].strip() for t in x_axis if 'AM' in t['t'] or 'PM' in t['t']]
    if times:
        lines.append(f"Time: {times[0]} — {times[-1]}")

    # Dates
    dates = [t['t'].strip() for t in x_axis if '/' in t['t']]
    if dates:
        lines.append(f"Date: {', '.join(dates)}")

    # Line data stats
    line_pts = data.get('lines', [])
    if line_pts:
        ys = [l['y'] for l in line_pts]
        lines.append(f"Chart data: {data.get('lc', 0)} points, y-range {min(ys)}-{max(ys)}")

    lines.append(f"Captured: {data.get('tc', 0)} text, {data.get('lc', 0)} lines, {data.get('rc', 0)} rects")

    return '\n'.join(lines)


def render_text(data, title="", url=""):
    """All canvas text, classified by axis."""
    texts = _parse_texts(data.get('text', []))
    y_axis, x_axis, other = _classify_axis(texts)

    lines = []
    lines.append("=== Canvas Text ===")
    if title:
        lines.append(f"Page: {title}")
    if url:
        lines.append(f"URL: {url}")
    lines.append(f"Total: {len(texts)} unique text draws")
    lines.append("")

    if y_axis:
        lines.append("--- Y-Axis (prices/values) ---")
        for t in y_axis:
            lines.append(f"  y={t['y']:>4}  {t['t']}")

    if x_axis:
        lines.append("\n--- X-Axis (time/dates) ---")
        for t in x_axis:
            lines.append(f"  x={t['x']:>4}  {t['t']}")

    if other:
        lines.append("\n--- Other Text ---")
        for t in other:
            lines.append(f"  ({t['x']:>4},{t['y']:>4})  {t['t']}")

    return '\n'.join(lines)


def render_lines(data):
    """Line point data — for understanding chart shape."""
    pts = data.get('lines', [])
    lines = []
    lines.append(f"=== Canvas Lines ({data.get('lc', 0)} total, {len(pts)} sampled) ===")

    if not pts:
        lines.append("No line data captured.")
        return '\n'.join(lines)

    ys = [p['y'] for p in pts]
    xs = [p['x'] for p in pts]
    lines.append(f"X range: {min(xs)} - {max(xs)}")
    lines.append(f"Y range: {min(ys)} - {max(ys)}")

    # Group by color (different lines)
    by_color = {}
    for p in pts:
        c = p.get('c', '?')
        by_color.setdefault(c, []).append(p)

    lines.append(f"Distinct lines: {len(by_color)}")
    for color, points in by_color.items():
        ys = [p['y'] for p in points]
        lines.append(f"  {color}: {len(points)} pts, y={min(ys)}-{max(ys)}")

    return '\n'.join(lines)


def render_all(data, title="", url=""):
    """Everything."""
    parts = [
        render_summary(data, title, url),
        "",
        render_text(data, title, url),
        "",
        render_lines(data),
    ]
    return '\n'.join(parts)


async def run(args):
    url = None
    mode = "text"  # default

    i = 0
    while i < len(args):
        if args[i] == '--summary':
            mode = 'summary'
            i += 1
        elif args[i] == '--text':
            mode = 'text'
            i += 1
        elif args[i] == '--lines':
            mode = 'lines'
            i += 1
        elif args[i] == '--all':
            mode = 'all'
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
        # Get page info before reload
        if url:
            await cdp.navigate(url, wait=3)

        title_r = await cdp.execute_js("document.title")
        page_title = title_r.get("result", {}).get("value", "")
        url_r = await cdp.execute_js("window.location.href")
        page_url = url_r.get("result", {}).get("value", "")

        # Inject interceptors BEFORE page JS runs, then reload
        result = await cdp.send('Page.addScriptToEvaluateOnNewDocument',
                                {'source': INJECT_JS})
        script_id = result.get('identifier', '')

        await cdp.send('Page.reload', {})
        await asyncio.sleep(5)  # Wait for chart to fully render

        # Clean up the injection so future reloads aren't affected
        if script_id:
            await cdp.send('Page.removeScriptToEvaluateOnNewDocument',
                           {'identifier': script_id})

        # Extract captured data
        result = await cdp.execute_js(EXTRACT_JS)
        raw = result.get("result", {}).get("value", "{}")
        data = json.loads(raw)

        if 'error' in data:
            print(f"Error: {data['error']}")
            return

        # Render output
        if mode == 'summary':
            print(render_summary(data, page_title, page_url))
        elif mode == 'text':
            print(render_text(data, page_title, page_url))
        elif mode == 'lines':
            print(render_lines(data))
        elif mode == 'all':
            print(render_all(data, page_title, page_url))

        # Cleanup
        await cdp.execute_js(CLEANUP_JS)

    finally:
        if cdp.ws:
            await cdp.ws.close()


def main():
    asyncio.run(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
