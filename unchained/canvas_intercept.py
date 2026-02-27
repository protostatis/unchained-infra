"""Canvas draw interception — capture all text rendered on canvas elements."""
import asyncio
import json
from cdp import CDP, _get_ws_url


async def run():
    ws_url = _get_ws_url()
    cdp = CDP(ws_url)
    await cdp.connect()

    # Step 1: Inject interceptors on canvas 2D context
    await cdp.execute_js(r"""
        (function() {
            // Storage for captured draws
            window.__canvasCapture = {text: [], lines: [], rects: []};

            var proto = CanvasRenderingContext2D.prototype;

            // Intercept fillText and strokeText
            var _fillText = proto.fillText;
            proto.fillText = function(text, x, y) {
                window.__canvasCapture.text.push({
                    text: String(text), x: Math.round(x), y: Math.round(y),
                    font: this.font, fill: this.fillStyle, type: 'fill'
                });
                return _fillText.apply(this, arguments);
            };

            var _strokeText = proto.strokeText;
            proto.strokeText = function(text, x, y) {
                window.__canvasCapture.text.push({
                    text: String(text), x: Math.round(x), y: Math.round(y),
                    font: this.font, stroke: this.strokeStyle, type: 'stroke'
                });
                return _strokeText.apply(this, arguments);
            };

            // Intercept lineTo to capture chart lines (sample every 5th point to keep size down)
            var _lineTo = proto.lineTo;
            var lineCount = 0;
            proto.lineTo = function(x, y) {
                lineCount++;
                if (lineCount % 5 === 0) {
                    window.__canvasCapture.lines.push({
                        x: Math.round(x), y: Math.round(y),
                        stroke: this.strokeStyle, width: this.lineWidth
                    });
                }
                return _lineTo.apply(this, arguments);
            };

            // Intercept fillRect for volume bars
            var _fillRect = proto.fillRect;
            proto.fillRect = function(x, y, w, h) {
                if (w > 0.5 && h > 2) {  // Filter out tiny fills
                    window.__canvasCapture.rects.push({
                        x: Math.round(x), y: Math.round(y),
                        w: Math.round(w), h: Math.round(h),
                        fill: this.fillStyle
                    });
                }
                return _fillRect.apply(this, arguments);
            };

            window.__canvasCapture.installed = true;
        })()
    """)
    print("Interceptors installed.")

    # Step 2: Force chart to re-render by resizing window slightly
    await cdp.execute_js("window.dispatchEvent(new Event('resize'))")
    await asyncio.sleep(2)
    print("Triggered re-render.")

    # Step 3: Extract captured data
    result = await cdp.execute_js(r"""
        (function() {
            var c = window.__canvasCapture;
            if (!c) return JSON.stringify({error: "no capture data"});
            return JSON.stringify({
                textCount: c.text.length,
                lineCount: c.lines.length,
                rectCount: c.rects.length,
                text: c.text.slice(0, 200),
                lines: c.lines.slice(0, 50),
                rects: c.rects.slice(0, 50)
            });
        })()
    """)
    raw = result.get('result', {}).get('value', '{}')
    data = json.loads(raw)

    print(f"\nCaptured: {data.get('textCount', 0)} text, {data.get('lineCount', 0)} line points, {data.get('rectCount', 0)} rects")

    # Print all captured text sorted by position
    texts = data.get('text', [])
    if texts:
        # Deduplicate by (text, x, y)
        seen = set()
        unique = []
        for t in texts:
            key = (t['text'], t['x'], t['y'])
            if key not in seen:
                seen.add(key)
                unique.append(t)

        # Sort by y then x (top-to-bottom, left-to-right)
        unique.sort(key=lambda t: (t['y'], t['x']))

        print(f"\n--- Canvas Text ({len(unique)} unique) ---")
        for t in unique:
            font_size = t.get('font', '').split('px')[0].split(' ')[-1] if 'px' in t.get('font', '') else '?'
            print(f"  ({t['x']:>4},{t['y']:>4}) [{font_size:>3}px] {t['text']}")

    # Show some line data
    lines = data.get('lines', [])
    if lines:
        ys = [l['y'] for l in lines]
        print(f"\n--- Chart Line Data (sampled) ---")
        print(f"  Y range: {min(ys)} - {max(ys)} (price axis)")
        print(f"  Points sampled: {data.get('lineCount', 0)}")

    # Show volume bars
    rects = data.get('rects', [])
    if rects:
        print(f"\n--- Volume Bars ({data.get('rectCount', 0)} total) ---")
        # Show a few
        for r in rects[:10]:
            print(f"  ({r['x']:>4},{r['y']:>4}) {r['w']}x{r['h']} fill={r['fill']}")

    # Clean up — restore original methods
    await cdp.execute_js(r"""
        delete window.__canvasCapture;
    """)

    await cdp.ws.close()

asyncio.run(run())
