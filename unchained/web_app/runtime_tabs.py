"""Tab/session runtime helpers extracted from web.py."""

from __future__ import annotations

import asyncio
import os
import time


from web_app.core import get_core as _core


def session_cdp_url(agent_id: str) -> str:
    """Build the CDP relay URL for any agent."""
    core = _core()
    return core._relay_cdp_url(agent_id, "auto")


async def create_session_tab(
    session_id: str,
    agent_id: str,
    *,
    clean_cookies: bool = False,
) -> str:
    """Create a new Chrome tab via CDP Target.createTarget through the relay.

    Args:
        clean_cookies: If True, clear cookies and cache before creating the
            tab so anti-bot reputation scores (PerimeterX _pxvid/_pxhd etc.)
            don't carry over from previous sessions.  Only used for headless
            first-look guest sessions — not for logged-in users with saved
            browser profiles.
    """
    del session_id
    from cdp import CDP

    cdp = CDP(session_cdp_url(agent_id))
    try:
        await asyncio.wait_for(cdp.connect(), timeout=10)
        if clean_cookies:
            await cdp.send("Network.clearBrowserCookies")
            await cdp.send("Network.clearBrowserCache")
        result = await cdp.send("Target.createTarget", {"url": "about:blank"})
        return result["targetId"]
    finally:
        if cdp.ws:
            await cdp.ws.close()


async def close_session_tab(session_id: str):
    """Close the Chrome tab via CDP Target.closeTarget.

    On failure, queue the tab for retry instead of silently dropping it.
    Skips cleanup if an overlay copilot session is active on this tab.
    """
    core = _core()
    # Clear overlay state — the tab is being closed
    core._overlay_sessions.pop(session_id, None)
    tab_id = core._session_tabs.pop(session_id, None)
    if hasattr(core, "_session_allowed_tabs"):
        core._session_allowed_tabs.pop(session_id, None)
    agent_id = core._session_agent_map.pop(session_id, None)
    core._session_last_active.pop(session_id, None)
    if hasattr(core, "_session_profile_paths"):
        core._session_profile_paths.pop(session_id, None)
    if not tab_id or not agent_id:
        return

    if str(tab_id).startswith("prov-"):
        relay_host, relay_port = core._parse_relay()
        import cloud_tools
        from chrome_bridge import _extract_prov_slot
        slot = _extract_prov_slot(str(tab_id))

        try:
            await cloud_tools.provision_cleanup(agent_id, relay_host, relay_port, slot=slot)
            core._tabs_pending_close.pop(tab_id, None)
            print(f"[tabs] Cleaned provision browser for session {session_id}")
            return
        except Exception:
            core._tabs_pending_close[tab_id] = (agent_id, 0)
            return

    from cdp import CDP

    try:
        cdp = CDP(session_cdp_url(agent_id))
        await asyncio.wait_for(cdp.connect(), timeout=5)
        await cdp.send("Target.closeTarget", {"targetId": tab_id})
        if cdp.ws:
            await cdp.ws.close()
        print(f"[tabs] Closed tab {tab_id} for session {session_id}")
    except Exception:
        core._tabs_pending_close[tab_id] = (agent_id, 0)


async def ensure_session_tab(session_id: str, agent_id: str) -> str | None:
    """Create a per-session Chrome tab, enforcing the per-agent tab cap."""
    core = _core()
    agent_tab_count = sum(1 for aid in core._session_agent_map.values() if aid == agent_id)

    if agent_tab_count >= core._MAX_TABS_PER_AGENT:
        oldest_sid = None
        oldest_ts = float("inf")
        for sid, aid in list(core._session_agent_map.items()):
            if aid == agent_id:
                ts = core._session_last_active.get(sid, 0)
                if ts < oldest_ts:
                    oldest_ts = ts
                    oldest_sid = sid
        if oldest_sid:
            core.log.info(
                "[tabs] Evicting oldest session %s for agent %s (at %d tab limit)",
                oldest_sid,
                agent_id,
                core._MAX_TABS_PER_AGENT,
            )
            await close_session_tab(oldest_sid)

    # Headless agents (e.g. headless-9aaabaf7) get clean cookies per session
    # so anti-bot scores don't carry over between users.
    is_headless_agent = agent_id.startswith("headless-")
    try:
        tab_id = await create_session_tab(session_id, agent_id, clean_cookies=is_headless_agent)
        core._session_tabs[session_id] = tab_id
        if hasattr(core, "_session_allowed_tabs"):
            core._session_allowed_tabs[session_id] = {tab_id}
        core._session_agent_map[session_id] = agent_id
        core._session_last_active[session_id] = time.time()
        core.log.info("[tabs] Created tab %s for session %s (agent %s)", tab_id, session_id, agent_id)
        return tab_id
    except Exception as e:
        core.log.warning(
            "[tabs] Failed to create tab for session %s (agent %s): %s: %s",
            session_id, agent_id, type(e).__name__, e or "<empty>",
        )
        return None


async def stale_tab_cleanup_loop():
    """Periodically close stale tabs, retry failed closes, reconcile headless."""
    core = _core()
    while True:
        await asyncio.sleep(60)
        now = time.time()

        stale = [
            sid for sid, ts in core._session_last_active.items()
            if now - ts > (core._STALE_TAB_SECONDS if sid.startswith("s-guest") else core._STALE_TAB_SECONDS_AGENT)
        ]
        for sid in stale:
            print(f"[tabs] Closing stale tab for session {sid}")
            await close_session_tab(sid)

        for tab_id, (agent_id, retries) in list(core._tabs_pending_close.items()):
            if retries >= core._MAX_CLOSE_RETRIES:
                print(f"[tabs] Giving up on tab {tab_id} after {retries} retries")
                del core._tabs_pending_close[tab_id]
                continue
            if str(tab_id).startswith("prov-"):
                relay_host, relay_port = core._parse_relay()
                import cloud_tools
                from chrome_bridge import _extract_prov_slot
                slot = _extract_prov_slot(str(tab_id))

                try:
                    await cloud_tools.provision_cleanup(agent_id, relay_host, relay_port, slot=slot)
                    del core._tabs_pending_close[tab_id]
                    print("[tabs] Retry-cleaned provision browser")
                except Exception:
                    core._tabs_pending_close[tab_id] = (agent_id, retries + 1)
                continue
            try:
                from cdp import CDP

                cdp = CDP(session_cdp_url(agent_id))
                await asyncio.wait_for(cdp.connect(), timeout=5)
                await cdp.send("Target.closeTarget", {"targetId": tab_id})
                if cdp.ws:
                    await cdp.ws.close()
                del core._tabs_pending_close[tab_id]
                print(f"[tabs] Retry-closed tab {tab_id}")
            except Exception:
                core._tabs_pending_close[tab_id] = (agent_id, retries + 1)

        hkey = os.environ.get("HEADLESS_API_KEY", "")
        if not hkey:
            continue
        h_agent = core._agent_id("headless", hkey)
        try:
            from cdp import CDP

            cdp = CDP(session_cdp_url(h_agent))
            await asyncio.wait_for(cdp.connect(), timeout=10)
            result = await cdp.send("Target.getTargets")
            if cdp.ws:
                await cdp.ws.close()

            chrome_tabs = {
                t["targetId"] for t in result.get("targetInfos", []) if t.get("type") == "page"
            }
            tracked = set(core._session_tabs.values()) | set(core._tabs_pending_close.keys())
            orphans = chrome_tabs - tracked
            if orphans and len(chrome_tabs) > 1:
                for oid in list(orphans):
                    if len(chrome_tabs) <= 1:
                        break
                    try:
                        cdp2 = CDP(session_cdp_url(h_agent))
                        await asyncio.wait_for(cdp2.connect(), timeout=5)
                        await cdp2.send("Target.closeTarget", {"targetId": oid})
                        if cdp2.ws:
                            await cdp2.ws.close()
                        chrome_tabs.discard(oid)
                        print(f"[tabs] Reconciled orphan tab {oid}")
                    except Exception:
                        pass
        except Exception:
            pass


# --- Headless agent absence watchdog ---

# How often to poll the relay for headless agent presence.
_HEADLESS_WATCHDOG_POLL_INTERVAL_S = 60.0
# How long the headless agent can be missing before we emit a loud warning.
# Set to 2× poll interval so a single missed heartbeat doesn't page.
_HEADLESS_WATCHDOG_ABSENCE_THRESHOLD_S = 120.0


async def headless_agent_watchdog_loop():
    """Periodically verify HEADLESS_AGENT_ID is connected to the relay.

    When the headless worker crashes, wedges, or gets OOM-killed (as
    happened on 2026-04-10 when the EC2 host had to be hard-rebooted),
    the relay sees the agent disappear but nothing else notices until
    a guest tries to run /first-look-preview and gets an empty stream.
    By then the user has already had a bad experience.

    This loop polls the relay's /api/agents endpoint every 60s and
    emits a loud `[watchdog]` line to stdout/stderr when the headless
    agent has been absent for more than 2 minutes. On recovery it also
    emits a one-shot RECOVERED line. Both are easily greppable for
    alerting hooks.

    The watchdog is self-quiescing: if HEADLESS_AGENT_ID is empty (dev
    mode without a configured headless bridge), the loop returns
    immediately after the first poll. No spam on local dev.
    """
    core = _core()
    headless_id = getattr(core, "HEADLESS_AGENT_ID", "") or ""
    if not headless_id:
        print(
            "[watchdog] no HEADLESS_AGENT_ID configured; headless absence "
            "watchdog disabled",
            flush=True,
        )
        return

    absent_since: float | None = None
    warned = False
    print(
        f"[watchdog] headless absence watchdog armed for agent={headless_id} "
        f"(poll={_HEADLESS_WATCHDOG_POLL_INTERVAL_S:.0f}s, "
        f"threshold={_HEADLESS_WATCHDOG_ABSENCE_THRESHOLD_S:.0f}s)",
        flush=True,
    )
    while True:
        try:
            await asyncio.sleep(_HEADLESS_WATCHDOG_POLL_INTERVAL_S)
        except asyncio.CancelledError:
            return

        try:
            connected = await core._check_relay_agent(headless_id)
        except Exception as exc:
            # Don't let a relay network hiccup spam the logs. Degrade to
            # "unknown" and try again next cycle.
            print(
                f"[watchdog] headless relay check failed (ignored): {exc!r}",
                flush=True,
            )
            continue

        now = time.time()
        if connected:
            if warned and absent_since is not None:
                duration = int(now - absent_since)
                print(
                    f"[watchdog] headless agent {headless_id} RECOVERED "
                    f"after {duration}s absence",
                    flush=True,
                )
            absent_since = None
            warned = False
            continue

        # Agent is absent.
        if absent_since is None:
            absent_since = now
            # Don't warn on the first missed poll — wait for the
            # threshold so we don't spam on transient relay blips.
            continue
        elapsed = now - absent_since
        if elapsed >= _HEADLESS_WATCHDOG_ABSENCE_THRESHOLD_S and not warned:
            print(
                f"[watchdog] headless agent {headless_id} ABSENT for "
                f"{int(elapsed)}s (relay reports not connected) — "
                f"guest /first-look-preview runs will fail until the "
                f"headless worker reconnects",
                flush=True,
            )
            warned = True
