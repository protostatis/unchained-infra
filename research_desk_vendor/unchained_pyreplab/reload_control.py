from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
RELOAD_STATE_PATH = Path(tempfile.gettempdir()) / "unchained_pyreplab" / "reload_state.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def reload_state() -> dict[str, Any]:
    if not RELOAD_STATE_PATH.exists():
        return {"paused": False, "updated_at": ""}
    try:
        payload = json.loads(RELOAD_STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {"paused": False, "updated_at": ""}
    return {
        "paused": bool(payload.get("paused", False)),
        "updated_at": str(payload.get("updated_at", "")),
    }


def is_reload_paused() -> bool:
    return bool(reload_state().get("paused", False))


def set_reload_paused(paused: bool) -> dict[str, Any]:
    payload = {
        "paused": bool(paused),
        "updated_at": _now_iso(),
    }
    RELOAD_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    RELOAD_STATE_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return payload
