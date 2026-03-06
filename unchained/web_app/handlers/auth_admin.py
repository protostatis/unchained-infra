"""Auth, admin, and scheduler handlers extracted from web.py."""

from __future__ import annotations

import time

from aiohttp import web


from web_app.core import get_core as _core


async def handle_google_auth(request: web.Request) -> web.Response:
    """POST /auth/google — verify Google ID token, create session."""
    core = _core()
    body = await request.json()
    source = str(body.get("source", "claude")).strip().lower() or "claude"
    if source not in {"trial", "claude"}:
        source = "claude"
    core._track_event(
        request,
        "auth_google_attempt",
        source=source,
        meta={"source": source},
        status_code=200,
    )
    id_token = body.get("credential", "")
    if not id_token:
        core._track_event(
            request,
            "auth_google_fail",
            source=source,
            error_code="missing_credential",
            meta={"source": source, "reason": "missing_credential"},
            status_code=400,
        )
        return web.json_response({"error": "Missing credential"}, status=400)

    payload = await core.verify_google_token(id_token)
    if payload is None:
        core._track_event(
            request,
            "auth_google_fail",
            source=source,
            error_code="invalid_google_token",
            meta={"source": source, "reason": "invalid_google_token"},
            status_code=401,
        )
        return web.json_response({"error": "Invalid Google token"}, status=401)

    email = payload.get("email", "").lower()
    name = payload.get("name", "")
    picture = payload.get("picture", "")
    user_type = "trial" if source == "trial" else "claude"

    existing = core._auth.find_user_by_email(email)

    if existing:
        status = existing.get("status", "approved")

        if status == "approved":
            user = core._auth.get_or_create_user(email, name, picture)
            api_key = user["api_key"]
            agent_id = f"claude-{core._key_hash(api_key)}"
            session_token = core.create_session_token(user["user_id"], email)
            resp = web.json_response(
                {
                    "ok": True,
                    "email": email,
                    "name": name,
                    "picture": picture,
                    "agent_id": agent_id,
                    "user_type": existing.get("user_type", "claude"),
                    "status": "approved",
                    "demo_prompt_count": core._auth.get_demo_count(email),
                    "demo_unlimited": core._is_demo_unlimited(existing),
                    "claude_access_requested": False,
                    "is_admin": email.lower() in core.ADMIN_EMAILS,
                }
            )
            core._set_session_cookie(resp, session_token, request)
            core._track_event(
                request,
                "auth_google_success",
                user_id=user["user_id"],
                user_type=existing.get("user_type", "claude"),
                source=source,
                meta={"source": source, "status": "approved", "existing": True},
                status_code=200,
            )
            return resp

        if status == "pending":
            if source == "trial":
                api_key = existing.get("api_key")
                if not api_key:
                    api_key = core._auth.create_key(existing["user_id"])
                now = time.time()
                with core._auth._conn() as conn:
                    conn.execute(
                        "UPDATE users SET api_key = COALESCE(api_key, ?), "
                        "last_login_at = ?, name = ?, picture = ? WHERE email = ?",
                        (
                            api_key,
                            now,
                            name or existing.get("name", ""),
                            picture or existing.get("picture", ""),
                            email,
                        ),
                    )
                refreshed = core._auth.find_user_by_email(email) or existing
                pending_user_type = refreshed.get("user_type", "trial")
                agent_id = f"claude-{core._key_hash(api_key)}"
                session_token = core.create_session_token(existing["user_id"], email)
                resp = web.json_response(
                    {
                        "ok": True,
                        "email": email,
                        "name": name,
                        "picture": picture,
                        "agent_id": agent_id,
                        "user_type": pending_user_type,
                        "status": "pending",
                        "demo_prompt_count": core._auth.get_demo_count(email),
                        "demo_unlimited": False,
                        "review_pending": True,
                        "claude_access_requested": pending_user_type == "claude",
                        "is_admin": email.lower() in core.ADMIN_EMAILS,
                    }
                )
                core._set_session_cookie(resp, session_token, request)
                core._track_event(
                    request,
                    "auth_google_success",
                    user_id=existing["user_id"],
                    user_type=pending_user_type,
                    source=source,
                    meta={"source": source, "status": "pending", "existing": True},
                    status_code=200,
                )
                return resp

            pending_user_type = existing.get("user_type", "claude")
            core._track_event(
                request,
                "auth_google_pending",
                user_id=existing["user_id"],
                user_type=pending_user_type,
                source=source,
                meta={"source": source, "status": "pending", "existing": True},
                status_code=200,
            )
            return web.json_response(
                {
                    "pending": True,
                    "status": "pending",
                    "user_type": pending_user_type,
                    "claude_access_requested": pending_user_type == "claude",
                    "message": "Your sign-up request is still being reviewed. We'll notify you by email once approved.",
                }
            )

        if status == "rejected":
            core._track_event(
                request,
                "auth_google_fail",
                user_id=existing["user_id"],
                user_type=existing.get("user_type", "claude"),
                source=source,
                error_code="rejected",
                meta={"source": source, "reason": "rejected"},
                status_code=403,
            )
            return web.json_response(
                {"error": "Your sign-up request was not approved."}, status=403
            )

    if source == "trial":
        user = core._auth.create_pending_user(email, name, picture, user_type="trial")
        api_key = core._auth.create_key(user["user_id"])
        with core._auth._conn() as conn:
            conn.execute(
                "UPDATE users SET api_key = ?, user_type = 'trial' WHERE email = ?",
                (api_key, email),
            )
        agent_id = f"claude-{core._key_hash(api_key)}"
        session_token = core.create_session_token(user["user_id"], email)
        resp = web.json_response(
            {
                "ok": True,
                "email": email,
                "name": name,
                "picture": picture,
                "agent_id": agent_id,
                "user_type": "trial",
                "status": "pending",
                "demo_prompt_count": core._auth.get_demo_count(email),
                "demo_unlimited": False,
                "review_pending": True,
                "claude_access_requested": False,
                "is_admin": email.lower() in core.ADMIN_EMAILS,
            }
        )
        core._set_session_cookie(resp, session_token, request)
        core._track_event(
            request,
            "signup_created",
            user_id=user["user_id"],
            user_type="trial",
            source=source,
            meta={"source": source, "status": "pending"},
            status_code=200,
        )
        core._track_event(
            request,
            "auth_google_success",
            user_id=user["user_id"],
            user_type="trial",
            source=source,
            meta={"source": source, "status": "pending", "new_user": True},
            status_code=200,
        )

        core.send_email(
            email,
            "Unchained — Trial access enabled (account review pending)",
            f"<p>Hi {name or email},</p>"
            "<p>Your account review is still pending, but you can start using Trial/Demo now.</p>"
            "<p>We'll notify you once your full account is approved.</p>"
            "<p>— The Unchained Team</p>",
        )
        for admin in core.ADMIN_EMAILS:
            core.send_email(
                admin,
                f"New trial sign-up (pending review): {email}",
                f"<p>New trial/demo user: <b>{name}</b> ({email}).</p>"
                "<p>Status: <b>pending review</b> (trial/demo access enabled).</p>",
            )
        return resp

    user = core._auth.create_pending_user(email, name, picture, user_type=user_type)
    session_token = core.create_session_token(user["user_id"], email)

    core.send_email(
        email,
        "Unchained — Sign-up request received",
        f"<p>Hi {name or email},</p>"
        "<p>We received your request to join Unchained. "
        "We're reviewing it now and will get back to you shortly.</p>"
        "<p>— The Unchained Team</p>",
    )

    for admin in core.ADMIN_EMAILS:
        core.send_email(
            admin,
            f"New Unchained sign-up: {email}",
            f"<p>New sign-up request from <b>{name}</b> ({email}).</p>"
            f"<p>Source: <b>{user_type}</b></p>"
            f"<p>Approve: <code>POST /admin/approve</code> with body "
            f'<code>{{"email": "{email}"}}</code></p>',
        )

    resp = web.json_response(
        {
            "pending": True,
            "message": "Your sign-up request has been submitted. We'll review it and notify you by email.",
        }
    )
    core._set_session_cookie(resp, session_token, request)
    core._track_event(
        request,
        "signup_created",
        user_id=user["user_id"],
        user_type=user_type,
        source=source,
        meta={"source": source, "status": "pending"},
        status_code=200,
    )
    core._track_event(
        request,
        "auth_google_pending",
        user_id=user["user_id"],
        user_type=user_type,
        source=source,
        meta={"source": source, "status": "pending", "new_user": True},
        status_code=200,
    )
    return resp


async def handle_request_claude_access(request: web.Request) -> web.Response:
    """POST /auth/request-claude-access — request full Claude access for pending account."""
    core = _core()
    auth_info = core._authenticate(request)
    if auth_info is None:
        return web.json_response({"error": "Not authenticated"}, status=401)

    email = str(auth_info.get("email", "")).strip().lower()
    if not email:
        return web.json_response({"error": "Missing account email"}, status=400)

    user = core._auth.find_user_by_email(email)
    if not user:
        return web.json_response({"error": "User not found"}, status=404)

    status = user.get("status", "approved")
    user_type = user.get("user_type", "claude")
    if status == "approved":
        return web.json_response(
            {
                "ok": True,
                "status": "approved",
                "user_type": user_type,
                "claude_access_requested": user_type == "claude",
                "already_approved": True,
            }
        )
    if status == "rejected":
        return web.json_response(
            {"error": "Your sign-up request was not approved."},
            status=403,
        )

    already_requested = user_type == "claude"
    if not already_requested:
        with core._auth._conn() as conn:
            conn.execute(
                "UPDATE users SET user_type = 'claude', last_login_at = ? "
                "WHERE email = ? AND status = 'pending'",
                (time.time(), email),
            )

        core.send_email(
            email,
            "Unchained — Claude access request received",
            f"<p>Hi {user.get('name') or email},</p>"
            "<p>We received your request for full Claude access.</p>"
            "<p>Your account is still pending review. You can continue using Trial while you wait.</p>"
            "<p>— The Unchained Team</p>",
        )
        for admin in core.ADMIN_EMAILS:
            core.send_email(
                admin,
                f"Claude access request (pending): {email}",
                f"<p>User requested full Claude access: <b>{user.get('name') or email}</b> ({email}).</p>"
                "<p>Status: <b>pending review</b>.</p>",
            )

    return web.json_response(
        {
            "ok": True,
            "status": "pending",
            "user_type": "claude",
            "claude_access_requested": True,
            "already_requested": already_requested,
            "message": "Request submitted. You can keep using Trial while your Claude access request is reviewed.",
        }
    )


async def handle_logout(request: web.Request) -> web.Response:
    """POST /auth/logout — clear session cookie."""
    core = _core()
    resp = web.json_response({"ok": True})
    core._clear_session_cookie(resp, request)
    return resp


async def handle_dev_auth(request: web.Request) -> web.Response:
    """POST /auth/dev — local dev login (no Google)."""
    core = _core()
    if core.GOOGLE_CLIENT_ID:
        return web.json_response(
            {"error": "Dev auth disabled (Google OAuth configured)"}, status=403
        )

    body = await request.json()
    email = body.get("email", "dev@localhost").strip().lower()
    name = body.get("name", "Dev User")

    core._auth.get_or_create_user(email, name, "")
    with core._auth._conn() as conn:
        conn.execute("UPDATE users SET status = 'approved' WHERE email = ?", (email,))

    user = core._auth.find_user_by_email(email)
    token = core.create_session_token(user["user_id"], email)
    agent_id = f"claude-{core._key_hash(user['api_key'])}"

    resp = web.json_response({"ok": True, "agent_id": agent_id, "email": email})
    core._set_session_cookie(resp, token, request)
    return resp


async def handle_auth_me(request: web.Request) -> web.Response:
    """GET /auth/me — return current user info if session is valid."""
    core = _core()
    auth_info = core._authenticate(request)
    if auth_info is not None:
        email = auth_info.get("email", "")
        user = core._auth.find_user_by_email(email)
        status = (
            user.get("status", auth_info.get("status", "approved"))
            if user
            else auth_info.get("status", "approved")
        )
        user_type = (
            user.get("user_type", auth_info.get("user_type", "claude"))
            if user
            else auth_info.get("user_type", "claude")
        )
        openrouter_usage = {}
        if user and (user_type == "trial" or status == "pending"):
            openrouter_usage = core._openrouter_budget_state_for_user(user["user_id"])
        return web.json_response(
            {
                "authenticated": True,
                "email": email,
                "agent_id": auth_info.get("agent_id", ""),
                "user_type": user_type,
                "status": status,
                "pending": status == "pending",
                "review_pending": status == "pending",
                "claude_access_requested": status == "pending" and user_type == "claude",
                "demo_prompt_count": core._auth.get_demo_count(email) if email else 0,
                "demo_unlimited": core._is_demo_unlimited(user) if user else False,
                "openrouter_usage": openrouter_usage,
                "is_admin": email.lower() in core.ADMIN_EMAILS,
                "name": user.get("name", "") if user else "",
                "picture": user.get("picture", "") if user else "",
            }
        )

    sessions: list[dict] = []
    for token in core._session_cookie_candidates(request):
        session = core.verify_session_token(token)
        if session:
            sessions.append(session)
    sessions.sort(key=lambda s: int(s.get("iat", 0)), reverse=True)
    for session in sessions:
        status = core._auth.get_user_status(session["email"])
        user = core._auth.find_user_by_email(session["email"])
        user_type = user.get("user_type", "claude") if user else "claude"
        if status == "pending":
            return web.json_response(
                {
                    "authenticated": False,
                    "pending": True,
                    "status": "pending",
                    "user_type": user_type,
                    "claude_access_requested": user_type == "claude",
                }
            )
        if status == "approved":
            if user and user.get("api_key"):
                api_key = user["api_key"]
                agent_id = f"claude-{core._key_hash(api_key)}"
                return web.json_response(
                    {
                        "authenticated": True,
                        "email": session["email"],
                        "agent_id": agent_id,
                        "user_type": user.get("user_type", "claude"),
                        "status": "approved",
                        "pending": False,
                        "review_pending": False,
                        "claude_access_requested": False,
                        "is_admin": session["email"].lower() in core.ADMIN_EMAILS,
                        "name": user.get("name", ""),
                        "picture": user.get("picture", ""),
                    }
                )

    return web.json_response({"authenticated": False}, status=401)


def is_admin(request: web.Request) -> dict | None:
    """Authenticate and check if user is an admin. Returns auth_info or None."""
    core = _core()
    auth_info = core._authenticate(request)
    if not auth_info:
        return None
    email = auth_info.get("email", "")
    if email not in core.ADMIN_EMAILS:
        return None
    return auth_info


async def handle_admin_pending(request: web.Request) -> web.Response:
    """GET /admin/pending — list all pending sign-up requests."""
    core = _core()
    if not is_admin(request):
        return web.json_response({"error": "Admin access required"}, status=403)
    pending = core._auth.list_pending_users()
    return web.json_response({"pending": pending})


async def handle_admin_approve(request: web.Request) -> web.Response:
    """POST /admin/approve — approve a pending user."""
    core = _core()
    if not is_admin(request):
        return web.json_response({"error": "Admin access required"}, status=403)
    body = await request.json()
    email = body.get("email", "").strip().lower()
    if not email:
        return web.json_response({"error": "email required"}, status=400)
    user = core._auth.approve_user(email)
    if not user:
        return web.json_response({"error": f"User {email} not found"}, status=404)

    core.send_email(
        email,
        "Unchained — You're in!",
        f"<p>Hi {user.get('name') or email},</p>"
        "<p>Your account has been approved! "
        '<a href="https://api.unchainedsky.com/chat">Visit unchainedsky.com/chat</a> to get started.</p>'
        "<p>— The Unchained Team</p>",
    )
    return web.json_response({"ok": True, "user": user})


async def handle_admin_reject(request: web.Request) -> web.Response:
    """POST /admin/reject — reject a pending user."""
    core = _core()
    if not is_admin(request):
        return web.json_response({"error": "Admin access required"}, status=403)
    body = await request.json()
    email = body.get("email", "").strip().lower()
    if not email:
        return web.json_response({"error": "email required"}, status=400)
    if core._auth.reject_user(email):
        return web.json_response({"ok": True})
    return web.json_response({"error": f"User {email} not found"}, status=404)


async def handle_admin_users(request: web.Request) -> web.Response:
    """GET /admin/users — list all users with their status."""
    core = _core()
    if not is_admin(request):
        return web.json_response({"error": "Admin access required"}, status=403)
    users = core._auth.list_all_users()
    return web.json_response({"users": users})


async def handle_setup_page(request: web.Request) -> web.Response:
    """GET /setup — serve the setup / provisioning UI."""
    core = _core()
    core._track_page_view(request)
    auth_info = core._authenticate(request)
    if core._is_pending_user(auth_info):
        core._track_redirect(request, "/trial", reason="pending_user_gate", auth_info=auth_info)
        raise web.HTTPFound("/trial")
    html = core.inject_google_client_id(core.SETUP_HTML, core.GOOGLE_CLIENT_ID)
    return web.Response(text=html, content_type="text/html")


async def handle_admin_page(request: web.Request) -> web.Response:
    """GET /admin — serve the admin UI."""
    core = _core()
    core._track_page_view(request)
    html = core.inject_google_client_id(core.ADMIN_HTML, core.GOOGLE_CLIENT_ID)
    return web.Response(text=html, content_type="text/html")


async def handle_scheduler_page(request: web.Request) -> web.Response:
    """GET /scheduler — authenticated scheduler editor UI."""
    core = _core()
    core._track_page_view(request)
    auth_info = core._authenticate(request)
    if auth_info is None:
        core._track_redirect(request, "/app", reason="scheduler_requires_auth")
        raise web.HTTPFound("/app")
    if core._is_pending_user(auth_info):
        core._track_redirect(request, "/trial", reason="pending_user_gate", auth_info=auth_info)
        raise web.HTTPFound("/trial")
    html = core.inject_google_client_id(core.SCHEDULER_HTML, core.GOOGLE_CLIENT_ID)
    return web.Response(text=html, content_type="text/html")


async def handle_scheduler_jobs(request: web.Request) -> web.Response:
    """GET/POST /web/scheduler/jobs — per-user scheduler config."""
    core = _core()
    auth_info = core._authenticate(request)
    if auth_info is None:
        return web.json_response({"error": "Not authenticated"}, status=401)
    user_id = auth_info["user_id"]

    import scheduled_tasks as st

    if request.method == "GET":
        payload = core._scheduler_read_jobs_payload(user_id)
        try:
            jobs = st.parse_jobs_payload(payload)
            preview = core._scheduler_preview_rows(user_id, jobs)
            return web.json_response({"jobs": st.jobs_to_payload(jobs)["jobs"], "preview": preview})
        except Exception:
            return web.json_response({"jobs": [], "preview": []})

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "Body must be a JSON object"}, status=400)

    try:
        jobs = st.parse_jobs_payload(body)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)

    if len(jobs) > 200:
        return web.json_response({"error": "Too many jobs (max 200)"}, status=400)

    canonical = st.jobs_to_payload(jobs)
    core._scheduler_write_jobs_payload(user_id, canonical)
    preview = core._scheduler_preview_rows(user_id, jobs)
    return web.json_response({"ok": True, "jobs": canonical["jobs"], "preview": preview})


async def handle_scheduler_preview(request: web.Request) -> web.Response:
    """POST /web/scheduler/preview — preview next run times for job payload."""
    core = _core()
    auth_info = core._authenticate(request)
    if auth_info is None:
        return web.json_response({"error": "Not authenticated"}, status=401)
    user_id = auth_info["user_id"]

    import scheduled_tasks as st

    try:
        body = await request.json() if request.can_read_body else {}
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)
    if body is None:
        body = {}
    if not isinstance(body, dict):
        return web.json_response({"error": "Body must be a JSON object"}, status=400)

    payload = body if isinstance(body.get("jobs"), list) else core._scheduler_read_jobs_payload(user_id)
    try:
        jobs = st.parse_jobs_payload(payload)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)

    preview = core._scheduler_preview_rows(user_id, jobs)
    return web.json_response({"preview": preview, "server_time": int(time.time())})


async def handle_scheduler_history(request: web.Request) -> web.Response:
    """GET /web/scheduler/history — recent persisted run records for one job."""
    core = _core()
    auth_info = core._authenticate(request)
    if auth_info is None:
        return web.json_response({"error": "Not authenticated"}, status=401)
    user_id = auth_info["user_id"]
    job_id = str(request.query.get("job_id", "") or "").strip()
    if not job_id:
        return web.json_response({"error": "job_id required"}, status=400)

    try:
        limit = int(request.query.get("limit", "20") or "20")
    except ValueError:
        return web.json_response({"error": "limit must be an integer"}, status=400)
    limit = max(1, min(limit, 50))

    import scheduled_tasks as st

    try:
        jobs = st.parse_jobs_payload(core._scheduler_read_jobs_payload(user_id))
    except Exception:
        jobs = []
    if job_id not in {job.id for job in jobs}:
        return web.json_response({"error": "job not found"}, status=404)

    records = st.load_run_history(core._scheduler_state_path(user_id), job_id, limit=limit)
    return web.json_response({"records": records, "job_id": job_id})
