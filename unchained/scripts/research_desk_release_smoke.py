#!/usr/bin/env python3
"""Structured checks for the Research Desk release smoke script."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) != 7:
        raise SystemExit(
            "usage: research_desk_release_smoke.py "
            "<artifact_path> <hosted_base> <local_base> "
            "<hosted_html_path> <first_look_html_path> <local_status_path>"
        )

    artifact_path, hosted_base, local_base, hosted_html_path, first_look_html_path, local_status_path = argv[1:]

    hosted_html = Path(hosted_html_path).read_text(encoding="utf-8")
    first_look_html = Path(first_look_html_path).read_text(encoding="utf-8")
    status = json.loads(Path(local_status_path).read_text(encoding="utf-8"))

    checks = {
        "hosted_has_connect_button": 'id="connect-local-desk"' in hosted_html,
        "hosted_has_create_button": 'id="create-local-mission"' in hosted_html,
        "hosted_has_run_next_button": 'id="run-local-next-step"' in hosted_html,
        "hosted_has_lab_link": 'id="mission-watch-lab-link"' in hosted_html,
        "first_look_has_research_desk_handoff": "/labs/research-desk" in first_look_html,
        "local_status_ok": bool(status.get("ok")),
        "local_has_launch_url": bool(((status.get("local_urls") or {}).get("home") or "").strip()),
        "local_has_handshake_start": bool(((status.get("handshake") or {}).get("start_url") or "").strip()),
        "local_has_mission_create": bool(
            (((status.get("handshake") or {}).get("actions") or {}).get("mission_create_url") or "").strip()
        ),
        "local_has_mission_advance": bool(
            (((status.get("handshake") or {}).get("actions") or {}).get("mission_advance_url") or "").strip()
        ),
    }

    payload = {
        "ok": all(checks.values()),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "hosted_base": hosted_base,
        "local_base": local_base,
        "checks": checks,
        "local_summary": {
            "launch_ready": bool(status.get("launch_ready")),
            "configured_provider": str(((status.get("provider") or {}).get("configured_provider") or "")).strip(),
            "browser_client": str(((status.get("provider") or {}).get("browser_client") or "")).strip(),
            "agent_id": str(((status.get("bridge") or {}).get("agent_id") or "")).strip(),
            "capsule_count": int(((status.get("capsules") or {}).get("count") or 0)),
        },
    }

    artifact = Path(artifact_path)
    artifact.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
