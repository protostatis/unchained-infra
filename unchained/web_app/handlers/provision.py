"""Provider provisioning handlers extracted from web.py."""

from __future__ import annotations

import time

from aiohttp import web


from web_app.core import get_core as _core


async def handle_provision_profiles(request: web.Request) -> web.Response:
    """GET /web/provision/profiles — list Chrome profiles with Google sign-in."""
    core = _core()
    auth_info = core._authenticate(request)
    if auth_info is None:
        return web.json_response({"error": "Not authenticated"}, status=401)
    if core._is_pending_user(auth_info):
        return core._pending_limited_response()

    import signup_agent

    profiles = signup_agent.list_chrome_profiles()

    if not profiles:
        bridge_info = await core._resolve_bridge_agent(auth_info)
        agent_id = bridge_info.get("bridge_agent_id") or auth_info.get("agent_id", "")
        if agent_id:
            relay_host, relay_port = core._parse_relay()
            profiles = await core.provision_helpers.fetch_relay_profiles(
                agent_id=agent_id,
                relay_host=relay_host,
                relay_port=relay_port,
                headers=core._relay_auth_headers(),
            )

    return web.json_response({"profiles": profiles})


def spawn_provider_agent(
    provider: str,
    user_id: str,
    unchained_key: str,
    provider_key: str,
) -> str | None:
    """Spawn provider-specific chat agent and return destination chat URL."""
    core = _core()
    if provider == "gemini":
        core._spawn_gemini_agent(user_id, unchained_key, provider_key)
        return "/chat-gemini"
    if provider == "claude-sdk":
        core._spawn_claude_sdk_agent(user_id, unchained_key, provider_key)
        return "/chat-claude"
    if provider == "codex-sdk":
        core._spawn_codex_sdk_agent(user_id, unchained_key, provider_key)
        return "/local?provider=codex-sdk"
    if provider == "codex-cli":
        return "/local?provider=codex-cli"
    return None


def terminate_provider_agent(provider: str, key_hash: str) -> None:
    """Terminate a running provider agent process after key revoke."""
    core = _core()
    if provider == "gemini":
        agent_id = f"gemini-{key_hash}"
        proc = core._gemini_procs.pop(agent_id, None)
        if proc and proc.poll() is None:
            proc.terminate()
            print(f"[revoke] Killed Gemini agent {agent_id}")
        fh = core._gemini_log_fhs.pop(agent_id, None)
        if fh:
            fh.close()
        core._gemini_last_active.pop(agent_id, None)
        return

    if provider == "codex-sdk":
        agent_id = f"codexsdk-{key_hash}"
        proc = core._codex_sdk_procs.pop(agent_id, None)
        if proc and proc.poll() is None:
            proc.terminate()
            print(f"[revoke] Killed Codex SDK agent {agent_id}")
        fh = core._codex_sdk_log_fhs.pop(agent_id, None)
        if fh:
            fh.close()
        core._codex_sdk_last_active.pop(agent_id, None)
        return

    if provider == "claude-sdk":
        agent_id = f"claudesdk-{key_hash}"
        proc = core._claude_sdk_procs.pop(agent_id, None)
        if proc and proc.poll() is None:
            proc.terminate()
            print(f"[revoke] Killed Claude SDK agent {agent_id}")
        fh = core._claude_sdk_log_fhs.pop(agent_id, None)
        if fh:
            fh.close()
        core._claude_sdk_last_active.pop(agent_id, None)
        return

    if provider == "codex-cli":
        agent_id = f"codexcli-{key_hash}"
        proc = core._codex_cli_procs.pop(agent_id, None)
        if proc and proc.poll() is None:
            proc.terminate()
            print(f"[revoke] Killed Codex CLI agent {agent_id}")
        fh = core._codex_cli_log_fhs.pop(agent_id, None)
        if fh:
            fh.close()
        core._codex_cli_last_active.pop(agent_id, None)


async def handle_provision_start(request: web.Request) -> web.Response:
    """POST /web/provision/start — trigger API key provisioning for a provider."""
    core = _core()
    auth_info = core._authenticate(request)
    if auth_info is None:
        return web.json_response({"error": "Not authenticated"}, status=401)
    if core._is_pending_user(auth_info):
        return core._pending_limited_response()

    user_id = auth_info["user_id"]
    last = core._provision_cooldowns.get(user_id, 0)
    if time.time() - last < core._PROVISION_COOLDOWN_SECS:
        remaining = int(core._PROVISION_COOLDOWN_SECS - (time.time() - last))
        core._track_event(
            request,
            "provision_fail",
            route="/web/provision/start",
            route_intended="/setup",
            route_effective="/web/provision/start",
            user_id=user_id,
            user_type=auth_info.get("user_type", ""),
            source="web",
            error_code="cooldown_active",
            status_code=429,
            meta={"remaining_s": remaining},
        )
        return web.json_response(
            {
                "error": f"Please wait {remaining}s before starting another provision.",
                "status": "cooldown",
                "remaining_s": max(remaining, 1),
            },
            status=429,
        )

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    provider = body.get("provider", "").strip().lower()
    if not provider:
        core._track_event(
            request,
            "provision_fail",
            route="/web/provision/start",
            route_intended="/setup",
            route_effective="/web/provision/start",
            user_id=user_id,
            user_type=auth_info.get("user_type", ""),
            source="web",
            error_code="provider_required",
            status_code=400,
        )
        return web.json_response({"error": "provider required"}, status=400)

    import signup_agent

    if provider not in signup_agent.list_providers():
        core._track_event(
            request,
            "provision_fail",
            route="/web/provision/start",
            route_intended="/setup",
            route_effective="/web/provision/start",
            user_id=user_id,
            user_type=auth_info.get("user_type", ""),
            source="web",
            error_code="unknown_provider",
            status_code=400,
            meta={"provider": provider},
        )
        return web.json_response(
            {"error": f"Unknown provider: {provider}. Available: {signup_agent.list_providers()}"},
            status=400,
        )

    profile_path = body.get("profile_path")
    use_relay = body.get("use_relay", False)

    if profile_path and not use_relay:
        import signup_agent as _sa

        chrome_dir = _sa._chrome_user_data_dir()
        if not chrome_dir:
            core._track_event(
                request,
                "provision_fail",
                route="/web/provision/start",
                route_intended="/setup",
                route_effective="/web/provision/start",
                user_id=user_id,
                user_type=auth_info.get("user_type", ""),
                source="web",
                error_code="chrome_data_dir_missing",
                status_code=400,
            )
            return web.json_response({"error": "Chrome user data directory not found"}, status=400)
        if not core.provision_helpers.is_profile_path_within(profile_path, chrome_dir):
            core._track_event(
                request,
                "provision_fail",
                route="/web/provision/start",
                route_intended="/setup",
                route_effective="/web/provision/start",
                user_id=user_id,
                user_type=auth_info.get("user_type", ""),
                source="web",
                error_code="invalid_profile_path",
                status_code=403,
            )
            return web.json_response({"error": "Invalid profile path"}, status=403)

    core._track_event(
        request,
        "provision_start",
        route="/web/provision/start",
        route_intended="/setup",
        route_effective="/web/provision/start",
        cta_id=f"provision_{provider}",
        user_id=user_id,
        user_type=auth_info.get("user_type", ""),
        source="web",
        status_code=200,
        meta={"provider": provider, "use_relay": bool(use_relay)},
    )

    core._provision_cooldowns[user_id] = time.time()

    if use_relay:
        agent_id = auth_info.get("agent_id")
        if not agent_id:
            return web.json_response(
                {"error": "No agent_id resolved for relay provisioning"}, status=400
            )
        relay_host, relay_port = core._parse_relay()
        result = await signup_agent.provision_key(
            provider_name=provider,
            agent_id=agent_id,
            relay_host=relay_host,
            relay_port=relay_port,
            user_id=user_id,
            store_key=False,
            profile_path=profile_path or "",
        )
    else:
        result = await signup_agent.provision_key_local(
            provider_name=provider,
            user_id=user_id,
            profile_path=profile_path,
            store_key=False,
        )

    resp = {
        "status": result.status.value,
        "provider": result.provider,
        "message": result.message,
        "duration_ms": result.duration_ms,
        "has_key": result.api_key is not None,
    }

    if result.api_key and result.status == signup_agent.ProvisionStatus.SUCCESS:
        core._pending_provision[user_id] = (provider, result.api_key, time.time())
        key = result.api_key
        resp["key_preview"] = key[:8] + "..." + key[-4:] if len(key) > 12 else key[:4] + "..."

    if result.status == signup_agent.ProvisionStatus.ALREADY_EXISTS:
        existing_key = result.api_key or signup_agent.get_provider_key(user_id, provider)
        if existing_key:
            chat_url = core._spawn_provider_agent(provider, user_id, auth_info["key"], existing_key)
            if chat_url:
                resp["chat_url"] = chat_url

    event_name = "provision_fail"
    if result.status in (signup_agent.ProvisionStatus.SUCCESS, signup_agent.ProvisionStatus.ALREADY_EXISTS):
        event_name = "provision_success"
    core._track_event(
        request,
        event_name,
        route="/web/provision/start",
        route_intended="/setup",
        route_effective="/web/provision/start",
        cta_id=f"provision_{provider}",
        user_id=user_id,
        user_type=auth_info.get("user_type", ""),
        source="web",
        status_code=200,
        error_code="" if event_name == "provision_success" else result.status.value,
        latency_ms=int(result.duration_ms or 0),
        meta={
            "provider": provider,
            "status": result.status.value,
            "duration_ms": int(result.duration_ms or 0),
            "use_relay": bool(use_relay),
            "has_key": bool(result.api_key),
        },
    )
    return web.json_response(resp)


async def handle_provision_status(request: web.Request) -> web.Response:
    """GET /web/provision/status — check which providers have keys."""
    core = _core()
    auth_info = core._authenticate(request)
    if auth_info is None:
        return web.json_response({"error": "Not authenticated"}, status=401)
    if core._is_pending_user(auth_info):
        return core._pending_limited_response()

    import signup_agent

    user_id = auth_info["user_id"]
    providers = []
    visible_providers = [n for n in signup_agent.list_providers() if n != "codex-cli"]
    for name in visible_providers:
        entry = {"name": name, "provisioned": signup_agent.has_provider_key(user_id, name)}
        if entry["provisioned"]:
            key = signup_agent.get_provider_key(user_id, name)
            entry["key_preview"] = key[:10] + "..." if key else ""
        providers.append(entry)

    return web.json_response({"providers": providers})


async def handle_provision_confirm(request: web.Request) -> web.Response:
    """POST /web/provision/confirm — user confirms storing the provisioned key."""
    core = _core()
    auth_info = core._authenticate(request)
    if auth_info is None:
        return web.json_response({"error": "Not authenticated"}, status=401)
    if core._is_pending_user(auth_info):
        return core._pending_limited_response()

    user_id = auth_info["user_id"]
    pending = core._pending_provision.pop(user_id, None)
    if not pending:
        core._track_event(
            request,
            "provision_fail",
            route="/web/provision/confirm",
            route_intended="/setup",
            route_effective="/web/provision/confirm",
            cta_id="provision_confirm",
            user_id=user_id,
            user_type=auth_info.get("user_type", ""),
            source="web",
            error_code="no_pending_key",
            status_code=400,
        )
        return web.json_response(
            {"error": "No pending key to confirm (expired or already stored)"}, status=400
        )

    provider, api_key, ts = pending
    if time.time() - ts > core._PENDING_PROVISION_TTL:
        core._track_event(
            request,
            "provision_fail",
            route="/web/provision/confirm",
            route_intended="/setup",
            route_effective="/web/provision/confirm",
            cta_id=f"provision_confirm_{provider}",
            user_id=user_id,
            user_type=auth_info.get("user_type", ""),
            source="web",
            error_code="pending_key_expired",
            status_code=400,
            meta={"provider": provider},
        )
        return web.json_response({"error": "Pending key expired. Please provision again."}, status=400)

    import signup_agent

    signup_agent.store_provider_key(user_id, provider, api_key)
    core.log.info("[provision] User %s confirmed %s key storage", user_id, provider)

    resp = {"status": "success", "provider": provider, "message": f"{provider} key stored."}

    chat_url = core._spawn_provider_agent(provider, user_id, auth_info["key"], api_key)
    if chat_url:
        resp["chat_url"] = chat_url

    core._track_event(
        request,
        "provision_confirm",
        route="/web/provision/confirm",
        route_intended="/setup",
        route_effective="/web/provision/confirm",
        cta_id=f"provision_confirm_{provider}",
        user_id=user_id,
        user_type=auth_info.get("user_type", ""),
        source="web",
        status_code=200,
        meta={"provider": provider},
    )
    return web.json_response(resp)


async def handle_provision_save_manual(request: web.Request) -> web.Response:
    """POST /web/provision/save-manual — store a manually pasted API key."""
    core = _core()
    auth_info = core._authenticate(request)
    if auth_info is None:
        return web.json_response({"error": "Not authenticated"}, status=401)
    if core._is_pending_user(auth_info):
        return core._pending_limited_response()

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    provider = body.get("provider", "").strip().lower()
    api_key = body.get("api_key", "").strip()
    if not provider or not api_key:
        core._track_event(
            request,
            "provision_fail",
            route="/web/provision/save-manual",
            route_intended="/setup",
            route_effective="/web/provision/save-manual",
            cta_id="provision_manual_save",
            user_id=auth_info.get("user_id", ""),
            user_type=auth_info.get("user_type", ""),
            source="web",
            error_code="provider_or_key_missing",
            status_code=400,
        )
        return web.json_response({"error": "provider and api_key required"}, status=400)
    key_error = core.provision_helpers.validate_manual_api_key(api_key)
    if key_error:
        core._track_event(
            request,
            "provision_fail",
            route="/web/provision/save-manual",
            route_intended="/setup",
            route_effective="/web/provision/save-manual",
            cta_id=f"provision_manual_{provider}",
            user_id=auth_info.get("user_id", ""),
            user_type=auth_info.get("user_type", ""),
            source="web",
            error_code="manual_key_invalid",
            status_code=400,
            meta={"provider": provider},
        )
        return web.json_response({"error": key_error}, status=400)

    import signup_agent

    if provider not in signup_agent.list_providers():
        core._track_event(
            request,
            "provision_fail",
            route="/web/provision/save-manual",
            route_intended="/setup",
            route_effective="/web/provision/save-manual",
            cta_id=f"provision_manual_{provider}",
            user_id=auth_info.get("user_id", ""),
            user_type=auth_info.get("user_type", ""),
            source="web",
            error_code="unknown_provider",
            status_code=400,
            meta={"provider": provider},
        )
        return web.json_response(
            {"error": f"Unknown provider: {provider}. Available: {signup_agent.list_providers()}"},
            status=400,
        )

    user_id = auth_info["user_id"]
    signup_agent.store_provider_key(user_id, provider, api_key)
    core.log.info("[provision] User %s manually saved %s key", user_id, provider)

    resp = {"status": "success", "provider": provider, "message": f"{provider} key saved."}
    chat_url = core._spawn_provider_agent(provider, user_id, auth_info["key"], api_key)
    if chat_url:
        resp["chat_url"] = chat_url

    core._track_event(
        request,
        "provision_manual_save",
        route="/web/provision/save-manual",
        route_intended="/setup",
        route_effective="/web/provision/save-manual",
        cta_id=f"provision_manual_{provider}",
        user_id=user_id,
        user_type=auth_info.get("user_type", ""),
        source="web",
        status_code=200,
        meta={"provider": provider},
    )
    return web.json_response(resp)


async def handle_provision_revoke(request: web.Request) -> web.Response:
    """POST /web/provision/revoke — revoke a provisioned key."""
    core = _core()
    auth_info = core._authenticate(request)
    if auth_info is None:
        return web.json_response({"error": "Not authenticated"}, status=401)
    if core._is_pending_user(auth_info):
        return core._pending_limited_response()

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    provider = body.get("provider", "").strip().lower()
    if not provider:
        return web.json_response({"error": "provider required"}, status=400)

    import signup_agent

    user_id = auth_info["user_id"]
    revoked = signup_agent.revoke_provider_key(user_id, provider)

    core._terminate_provider_agent(provider, auth_info["key_hash"])
    core._provision_cooldowns.pop(user_id, None)

    core._track_event(
        request,
        "provision_revoke",
        route="/web/provision/revoke",
        route_intended="/setup",
        route_effective="/web/provision/revoke",
        cta_id=f"provision_revoke_{provider}",
        user_id=user_id,
        user_type=auth_info.get("user_type", ""),
        source="web",
        status_code=200,
        meta={"provider": provider, "revoked": bool(revoked)},
    )

    return web.json_response({"revoked": revoked, "provider": provider})
