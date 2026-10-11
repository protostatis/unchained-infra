"""Helper functions for provider provisioning endpoints."""

from __future__ import annotations

import os
import re

import httpx


def is_profile_path_within(profile_path: str, chrome_dir: str) -> bool:
    """Return True if profile_path is inside chrome_dir."""
    resolved = os.path.realpath(profile_path)
    root = os.path.realpath(chrome_dir) + os.sep
    return resolved.startswith(root)


async def fetch_relay_profiles(
    agent_id: str,
    relay_host: str,
    relay_port: int,
    headers: dict[str, str] | None = None,
    timeout: float = 5.0,
) -> list[dict]:
    """Fetch profile list from the user's bridge via relay API.

    A failed relay request is not an empty profile inventory. Callers need to
    distinguish a temporary bridge outage from a user who has no profiles.
    """
    scheme = "https" if relay_port == 443 else "http"
    if relay_port in (443, 80):
        url = f"{scheme}://{relay_host}/api/agents/{agent_id}/profiles"
    else:
        url = f"{scheme}://{relay_host}:{relay_port}/api/agents/{agent_id}/profiles"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, timeout=timeout, headers=headers)
        resp.raise_for_status()
        payload = resp.json()
    profiles = payload.get("profiles") if isinstance(payload, dict) else None
    if not isinstance(profiles, list):
        raise ValueError("Relay returned an invalid Chrome profile inventory")
    return profiles


def validate_manual_api_key(api_key: str) -> str | None:
    """Validate manually supplied key and return an error message if invalid."""
    if len(api_key) > 256:
        return "api_key too long (max 256 chars)"
    if not re.match(r"^[A-Za-z0-9_-]+$", api_key):
        return "api_key contains invalid characters"
    return None
