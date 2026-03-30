"""CDP helper for screenshots. Thin wrapper around cdp.py's CDP class."""
import asyncio, json, sys, random

# Reuse the CDP class and tab-aware connection from cdp.py
from cdp import CDP, DEBUG_PORT, _get_ws_url, _get_cdp


async def run(args):
    # Extract --tab <id> from anywhere in args
    tab_id = None
    filtered = []
    i = 0
    while i < len(args):
        if args[i] == "--tab" and i + 1 < len(args):
            tab_id = args[i + 1]
            i += 2
        else:
            filtered.append(args[i])
            i += 1
    args = filtered

    action = args[0] if args else "help"

    if action == "help":
        print("Usage: uv run engage_cdp.py <action> [args...]")
        print("  navigate <url>        — Navigate to URL, take screenshot")
        print("  screenshot [file]     — Take screenshot")
        print("  tweets                — List tweets on current X search page")
        print("  click <x> <y>         — Click at coordinates")
        print("  scroll [amount]       — Scroll down")
        print("  key <key>             — Press a key (e.g. ArrowDown)")
        print("  eval <js>             — Run JS and print result")
        print("  type <text>           — Type text char by char")
        print("  --tab <id>            — Target a specific tab (prefix match)")
        return

    cdp = await _get_cdp(tab_id=tab_id)

    if action == "navigate":
        url = args[1]
        await cdp.navigate(url)
        await asyncio.sleep(3)
        fname = "/tmp/engage_screenshot.png"
        await cdp.screenshot(fname)
        print(f"Navigated to {url}")
        print(f"Screenshot: {fname}")

    elif action == "screenshot":
        fname = args[1] if len(args) > 1 else "/tmp/engage_screenshot.png"
        await cdp.screenshot(fname)
        print(f"Screenshot: {fname}")

    elif action == "tweets":
        # Extract tweet info from X search results page
        js = '''
        (function() {
            const tweets = document.querySelectorAll('article[data-testid="tweet"]');
            let results = [];
            for (let i = 0; i < Math.min(tweets.length, 15); i++) {
                const tweet = tweets[i];
                // Get handle
                const links = tweet.querySelectorAll('a[role="link"]');
                let handle = '';
                for (const link of links) {
                    const href = link.getAttribute('href');
                    if (href && href.match(/^\\/[^/]+$/) && !href.startsWith('/search')) {
                        handle = href.substring(1);
                        break;
                    }
                }
                const textEl = tweet.querySelector('[data-testid="tweetText"]');
                const isLiked = tweet.querySelector('[data-testid="unlike"]') !== null;
                const likeBtn = tweet.querySelector('[data-testid="like"]');
                const followBtn = Array.from(tweet.querySelectorAll('[role="button"]')).find(
                    b => b.textContent.trim() === 'Follow'
                );

                let likePos = null;
                if (likeBtn) {
                    const rect = likeBtn.getBoundingClientRect();
                    likePos = [Math.round(rect.x + rect.width/2), Math.round(rect.y + rect.height/2)];
                }
                let followPos = null;
                if (followBtn) {
                    const rect = followBtn.getBoundingClientRect();
                    followPos = [Math.round(rect.x + rect.width/2), Math.round(rect.y + rect.height/2)];
                }

                results.push({
                    i: i,
                    handle: handle,
                    text: textEl ? textEl.textContent.substring(0, 100) : '',
                    liked: isLiked,
                    likePos: likePos,
                    followPos: followPos
                });
            }
            return JSON.stringify(results, null, 2);
        })()
        '''
        result = await cdp.send("Runtime.evaluate", {"expression": js})
        val = result.get("result", {}).get("value", "[]")
        print(val)

    elif action == "click":
        x, y = int(args[1]), int(args[2])
        await cdp.send("Input.dispatchMouseEvent", {
            "type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1
        })
        await asyncio.sleep(0.05)
        await cdp.send("Input.dispatchMouseEvent", {
            "type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1
        })
        print(f"Clicked at ({x}, {y})")

    elif action == "scroll":
        amount = int(args[1]) if len(args) > 1 else 3
        for _ in range(amount):
            await cdp.send("Input.dispatchMouseEvent", {
                "type": "mouseWheel", "x": 600, "y": 400, "deltaX": 0, "deltaY": 300
            })
            await asyncio.sleep(0.1)
        print(f"Scrolled down {amount} ticks")

    elif action == "key":
        key = args[1]
        await cdp.send("Input.dispatchKeyEvent", {"type": "keyDown", "key": key})
        await asyncio.sleep(0.05)
        await cdp.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": key})
        print(f"Pressed {key}")

    elif action == "eval":
        js = args[1]
        result = await cdp.send("Runtime.evaluate", {"expression": js})
        val = result.get("result", {}).get("value", result.get("result", {}))
        print(val)

    elif action == "type":
        text = args[1]
        for ch in text:
            await cdp.send("Input.insertText", {"text": ch})
            await asyncio.sleep(random.uniform(0.03, 0.08))
        print(f"Typed {len(text)} chars")

    await cdp.close()


if __name__ == "__main__":
    asyncio.run(run(sys.argv[1:]))
