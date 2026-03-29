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
        core._session_agent_map[session_id] = agent_id
        core._session_last_active[session_id] = time.time()
        core.log.info("[tabs] Created tab %s for session %s (agent %s)", tab_id, session_id, agent_id)
        return tab_id
    except Exception as e:
        core.log.warning("[tabs] Failed to create tab for session %s: %s", session_id, e)
        return None


async def stale_tab_cleanup_loop():
    """Periodically close stale tabs, retry failed closes, reconcile headless."""
    core = _core()
    while True:
        await asyncio.sleep(60)
        now = time.time()

        stale = [sid for sid, ts in core._session_last_active.items() if now - ts > core._STALE_TAB_SECONDS]
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
