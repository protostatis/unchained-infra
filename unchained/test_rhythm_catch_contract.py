"""End-to-end navigation/extraction contract for the shipped core+rhythm pair.

Runs in the private-integrated-tests job, where the private core is overlaid and
the pinned rhythm checkout is on the path. Unit tests on either side can pass
while the pair is broken, because each side feature-detects the other: rhythm
treats an unrecognised navigate() return as unverified, and the core does not
care who reads its outcome. Only a test that drives the real ``cdp.CDP`` through
the real ``rhythm_catch`` proves the fields rhythm reads are the fields the core
actually returns.

The failure being pinned: ``caught: []`` reported with the *requested* URL while
the browser was still showing the previous page. Nothing in the payload said so.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))

# The integrated job checks rhythm out next to the repo; RHYTHM_SRC overrides.
_RHYTHM_SRC = os.environ.get("RHYTHM_SRC") or os.path.join(HERE, "..", "rhythm-src")
if os.path.isdir(_RHYTHM_SRC):
    _RHYTHM_SRC = os.path.abspath(_RHYTHM_SRC)
    if _RHYTHM_SRC not in sys.path:
        sys.path.insert(0, _RHYTHM_SRC)

# Both halves are imported defensively so the skip/fail decision happens in one
# place. The infra repo ships cdp.py as a stub that raises at import, and the
# rhythm checkout is absent locally.
_RHYTHM_AVAILABLE = os.path.isdir(_RHYTHM_SRC)

if _RHYTHM_AVAILABLE:
    from rhythm.tools import rhythm_catch

try:
    from cdp import CDP
    _CORE_AVAILABLE = True
except Exception:  # noqa: BLE001 - the stub raises RuntimeError by design
    _CORE_AVAILABLE = False


def setUpModule():
    """A missing half is a setup failure in CI, not a reason to skip.

    Skipping would let the integrated job go green while proving nothing: a
    wrong ref, a token failure, a missing overlay, or a transient checkout error
    would each disable the only test that the shipped core and rhythm work
    together. Locally, without either half, skipping is the honest answer.
    """
    missing = []
    if not _RHYTHM_AVAILABLE:
        missing.append(f"rhythm checkout at {_RHYTHM_SRC}")
    if not _CORE_AVAILABLE:
        missing.append("private core (cdp.py is the public stub)")
    if not missing:
        return
    detail = "cannot run the pair test: missing " + ", ".join(missing)
    if os.environ.get("RHYTHM_SRC") or os.environ.get("CI"):
        raise RuntimeError(detail)
    raise unittest.SkipTest(detail)

URL = "https://example.com/items"


class _CdpWebSocket:
    """CDP-shaped websocket with scripted replies for one rhythm_catch run."""

    def __init__(
        self,
        *,
        nav_result=None,
        acknowledge=True,
        frame=None,
        doc_ids=(),
        scan_payload=None,
        scan_reply=None,
        element_count=42,
    ):
        self._responses: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []
        self.nav_result = {} if nav_result is None else nav_result
        self.acknowledge = acknowledge
        self.frame = frame
        self._doc_ids = list(doc_ids)
        self.scan_payload = scan_payload
        self.scan_reply = scan_reply
        self.element_count = element_count

    def _next_doc_id(self):
        if not self._doc_ids:
            return None
        if len(self._doc_ids) == 1:
            return self._doc_ids[0]
        return self._doc_ids.pop(0)

    async def _put(self, payload):
        await self._responses.put(json.dumps(payload))

    async def send(self, raw: str):
        message = json.loads(raw)
        self.sent.append(message)
        method = message["method"]
        command_id = message["id"]

        if method == "Page.navigate":
            if not self.acknowledge:
                return
            await self._put({"id": command_id, "result": self.nav_result})
            await self._put({"method": "Page.loadEventFired"})
        elif method == "Page.getFrameTree":
            await self._put({
                "id": command_id,
                "result": {"frameTree": {"frame": self.frame}},
            })
        elif method == "Runtime.evaluate":
            expression = message.get("params", {}).get("expression", "")
            if "__ucDocId" in expression:
                value = json.dumps(
                    {"id": self._next_doc_id(), "rs": "complete"})
                await self._put({
                    "id": command_id,
                    "result": {"result": {"value": value}},
                })
            elif "querySelectorAll" in expression:
                await self._put({
                    "id": command_id,
                    "result": {"result": {"value": self.element_count}},
                })
            elif expression.startswith("JSON.stringify(window.__rc.catchNow("):
                if self.scan_reply is not None:
                    await self._put({"id": command_id, "result": self.scan_reply})
                else:
                    await self._put({
                        "id": command_id,
                        "result": {"result": {"value": json.dumps(self.scan_payload)}},
                    })
            else:
                # Scanner installation and the click/wait helpers.
                await self._put({
                    "id": command_id,
                    "result": {"result": {"value": "catcher installed"}},
                })
        else:
            await self._put({"id": command_id, "result": {}})

    async def recv(self) -> str:
        return await self._responses.get()

    def expressions(self) -> list[str]:
        return [
            message.get("params", {}).get("expression", "")
            for message in self.sent
            if message["method"] == "Runtime.evaluate"
        ]

    def scanned(self) -> bool:
        return any(
            expression.startswith("JSON.stringify(window.__rc.catchNow(")
            for expression in self.expressions()
        )


def _client(websocket) -> CDP:
    client = CDP("ws://relay/cdp/integration")
    client.ws = websocket
    client._relay = False
    return client


def _committed_frame(**overrides):
    frame = {"id": "frame-1", "loaderId": "loader-2", "url": URL}
    frame.update(overrides)
    return frame


def _payload(**overrides):
    payload = {
        "terms": ["4090"],
        "caught": [{"text": "RTX 4090", "tag": "LI"}],
        "count": 1,
        "grouped": False,
        "truncated": False,
        "limit": 20,
        "identity": {"docId": "doc-1", "href": URL, "readyState": "complete"},
        "title": "Items",
        "elements": [{"text": "Next", "type": "button"}],
        "elements_total": 1,
    }
    payload.update(overrides)
    return payload


class RhythmCatchPairTests(unittest.IsolatedAsyncioTestCase):
    async def test_committed_navigation_extracts_without_a_warning(self):
        websocket = _CdpWebSocket(
            nav_result={"frameId": "frame-1", "loaderId": "loader-2"},
            frame=_committed_frame(),
            doc_ids=["doc-1", "doc-1"],
            scan_payload=_payload(),
        )

        result = await rhythm_catch(
            _client(websocket), URL, "find a 4090", ["4090"])

        self.assertEqual(result["caught_count"], 1)
        self.assertEqual(result["url"], URL)
        self.assertEqual(result["observed_url"], URL)
        self.assertEqual(result["navigation"]["outcome"], "ok")
        self.assertEqual(result["navigation"]["scan_relation"], "same")
        self.assertNotIn("warning", result)

    async def test_navigation_failure_never_scans_the_previous_page(self):
        # This is the whole point of the contract: the browser is still on the
        # previous document, and its records are not results for this URL.
        websocket = _CdpWebSocket(
            nav_result={
                "frameId": "frame-1",
                "errorText": "net::ERR_NAME_NOT_RESOLVED",
            },
            frame=_committed_frame(url="https://previous/page"),
            doc_ids=["doc-0"],
            scan_payload=_payload(),
        )

        result = await rhythm_catch(_client(websocket), URL, "task", ["4090"])

        self.assertIn("error", result)
        self.assertNotIn("caught", result)
        self.assertFalse(
            websocket.scanned(),
            "a failed navigation must not produce records from the old page",
        )
        self.assertEqual(result["navigation"]["outcome"], "error")
        self.assertEqual(result["navigation"]["reason"], "network")

    async def test_unconfirmed_navigation_warns_even_when_matches_exist(self):
        websocket = _CdpWebSocket(
            acknowledge=False,
            frame=_committed_frame(url="https://previous/page"),
            doc_ids=["doc-0", "doc-0"],
            scan_payload=_payload(
                identity={
                    "docId": "doc-0",
                    "href": "https://previous/page",
                    "readyState": "complete",
                }),
        )

        result = await rhythm_catch(_client(websocket), URL, "task", ["4090"])

        self.assertEqual(result["navigation"]["outcome"], "unknown")
        self.assertEqual(result["navigation"]["reason"], "timeout")
        self.assertEqual(result["caught_count"], 1)
        self.assertIn("not evidence", result["warning"])
        self.assertEqual(result["observed_url"], "https://previous/page")

    async def test_document_swap_between_navigation_and_scan_is_flagged(self):
        websocket = _CdpWebSocket(
            nav_result={"frameId": "frame-1", "loaderId": "loader-2"},
            frame=_committed_frame(),
            doc_ids=["doc-1", "doc-1"],
            scan_payload=_payload(
                identity={
                    "docId": "doc-2",
                    "href": "https://elsewhere/",
                    "readyState": "complete",
                }),
        )

        result = await rhythm_catch(_client(websocket), URL, "task", ["4090"])

        self.assertEqual(result["navigation"]["outcome"], "ok")
        self.assertEqual(
            result["navigation"]["scan_relation"], "document-changed")
        self.assertEqual(result["observed_url"], "https://elsewhere/")
        self.assertIn("navigated again", result["warning"])

    async def test_broken_scan_is_an_error_not_an_empty_result(self):
        websocket = _CdpWebSocket(
            nav_result={"frameId": "frame-1", "loaderId": "loader-2"},
            frame=_committed_frame(),
            doc_ids=["doc-1", "doc-1"],
            scan_reply={"exceptionDetails": {"text": "Execution context was destroyed"}},
        )

        result = await rhythm_catch(_client(websocket), URL, "task", ["4090"])

        self.assertIn("error", result)
        self.assertNotIn("caught_count", result)
        self.assertNotIn("caught", result)


if __name__ == "__main__":
    unittest.main()
