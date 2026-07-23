"""Auth, admin, and scheduler handlers extracted from web.py."""

from __future__ import annotations

import hmac
import os
import secrets
import time
from urllib.parse import urlencode, urlparse

import httpx

from aiohttp import web


from web_app.core import get_core as _core


_FACEBOOK_OAUTH_STATE_COOKIE = "uc_fb_oauth_state"
_FACEBOOK_OAUTH_SOURCE_COOKIE = "uc_fb_oauth_source"
_FACEBOOK_OAUTH_NEXT_COOKIE = "uc_fb_oauth_next"
_FACEBOOK_OAUTH_MAX_AGE = 600
_FACEBOOK_DIALOG_URL_DEFAULT = "https://www.facebook.com/v22.0/dialog/oauth"
_FACEBOOK_GRAPH_BASE_DEFAULT = "https://graph.facebook.com/v22.0"
_GITHUB_OAUTH_STATE_COOKIE = "uc_gh_oauth_state"
_GITHUB_OAUTH_SOURCE_COOKIE = "uc_gh_oauth_source"
_GITHUB_OAUTH_NEXT_COOKIE = "uc_gh_oauth_next"
_GITHUB_OAUTH_MAX_AGE = 600
_GITHUB_AUTHORIZE_URL_DEFAULT = "https://github.com/login/oauth/authorize"
_GITHUB_TOKEN_URL_DEFAULT = "https://github.com/login/oauth/access_token"
_GITHUB_API_BASE_DEFAULT = "https://api.github.com"
_APPROVED_ACCOUNT_ACTIVATION_URL = (
    "https://unchainedsky.com/install?utm_source=lifecycle_email"
    "&utm_medium=email&utm_campaign=approved_account_activation"
    "&ref=welcome_install"
)


def _normalize_source(raw_source: str | None) -> str:
    source = str(raw_source or "claude").strip().lower() or "claude"
    if source not in {"trial", "claude"}:
        source = "claude"
    return source


def _safe_next_path(raw_next: str | None, *, source: str) -> str:
    fallback = "/trial" if source == "trial" else "/local"
    next_path = str(raw_next or "").strip()
    if not next_path:
        return fallback
    if not next_path.startswith("/"):
        return fallback
    if next_path.startswith("//"):
        return fallback
    return next_path


def _append_query_params(path: str, **params: str) -> str:
    clean = {k: v for k, v in params.items() if v}
    if not clean:
        return path
    sep = "&" if "?" in path else "?"
    return f"{path}{sep}{urlencode(clean)}"


def _normalized_email(value: object) -> str:
    if value is None:
        return ""
    email = str(value).strip().lower()
    if email in {"none", "null"}:
        return ""
    return email


def _user_status(user: dict) -> str:
    return user.get("status") or "approved"


def _user_type(user: dict, fallback: str = "claude") -> str:
    return user.get("user_type") or fallback


def _approved_account_email_body(display: str) -> str:
    return (
        f"<p>Hi {display},</p>"
        "<p>Your account is ready. Connect one computer to run browser tasks.</p>"
        f'<p><a href="{_APPROVED_ACCOUNT_ACTIVATION_URL}">Connect this computer</a></p>'
        "<p>— The Unchained Team</p>"
    )


def _ensure_user_api_key(core, user: dict, email: str) -> str:
    api_key = user.get("api_key") or ""
    if api_key:
        return api_key
    api_key = core._auth.create_key(user["user_id"])
    with core._auth._conn() as conn:
        conn.execute(
            "UPDATE users SET api_key = ? WHERE email = ? AND api_key IS NULL",
            (api_key, email),
        )
    user["api_key"] = api_key
    return api_key


def _ensure_trial_access(core, user: dict, email: str) -> tuple[dict, str]:
    api_key = _ensure_user_api_key(core, user, email)
    with core._auth._conn() as conn:
        conn.execute(
            "UPDATE users SET api_key = ?, user_type = 'trial' WHERE email = ?",
            (api_key, email),
        )
    refreshed = core._auth.find_user_by_email(email) or user
    refreshed["api_key"] = refreshed.get("api_key") or api_key
    return refreshed, api_key


def _send_signup_emails(
    core,
    *,
    user: dict,
    email: str,
    name: str,
    user_type: str,
    is_trial_branch: bool,
) -> None:
    # The auto_approve_pending_users trigger may have flipped the user to
    # 'approved' before we get here, so branch on the post-trigger status
    # rather than assuming pending.
    approved = user.get("status") == "approved"
    display = name or email

    if approved:
        core.send_email(
            email,
            "Unchained — You're in!",
            _approved_account_email_body(display),
        )
    elif is_trial_branch:
        core.send_email(
            email,
            "Unchained — Trial access enabled (account review pending)",
            f"<p>Hi {display},</p>"
            "<p>Your account review is still pending, but you can start using Trial/Demo now.</p>"
            "<p>We'll notify you once your full account is approved.</p>"
            "<p>— The Unchained Team</p>",
        )
    else:
        core.send_email(
            email,
            "Unchained — Sign-up request received",
            f"<p>Hi {display},</p>"
            "<p>We received your request to join Unchained. "
            "We're reviewing it now and will get back to you shortly.</p>"
            "<p>— The Unchained Team</p>",
        )

    if approved:
        admin_subject = f"New Unchained sign-up (auto-approved): {email}"
        admin_body = (
            f"<p>New {user_type} sign-up: <b>{display}</b> ({email}).</p>"
            "<p>Status: <b>auto-approved</b>.</p>"
        )
    elif is_trial_branch:
        admin_subject = f"New trial sign-up (pending review): {email}"
        admin_body = (
            f"<p>New trial/demo user: <b>{display}</b> ({email}).</p>"
            "<p>Status: <b>pending review</b> (trial/demo access enabled).</p>"
        )
    else:
        admin_subject = f"New Unchained sign-up: {email}"
        admin_body = (
            f"<p>New sign-up request from <b>{display}</b> ({email}).</p>"
            f"<p>Source: <b>{user_type}</b></p>"
            f"<p>Approve: <code>POST /admin/approve</code> with body "
            f'<code>{{"email": "{email}"}}</code></p>'
        )
    for admin in core.ADMIN_EMAILS:
        core.send_email(admin, admin_subject, admin_body)


def _facebook_oauth_config() -> tuple[str, str, str, str]:
    app_id = os.environ.get("FACEBOOK_APP_ID", "").strip()
    app_secret = os.environ.get("FACEBOOK_APP_SECRET", "").strip()
    dialog_url = (
        os.environ.get("FACEBOOK_OAUTH_DIALOG_URL", _FACEBOOK_DIALOG_URL_DEFAULT).strip()
        or _FACEBOOK_DIALOG_URL_DEFAULT
    )
    graph_base = (
        os.environ.get("FACEBOOK_GRAPH_API_BASE", _FACEBOOK_GRAPH_BASE_DEFAULT).strip()
        or _FACEBOOK_GRAPH_BASE_DEFAULT
    ).rstrip("/")
    return app_id, app_secret, dialog_url, graph_base


def _github_oauth_config() -> tuple[str, str, str, str, str]:
    client_id = os.environ.get("GITHUB_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GITHUB_CLIENT_SECRET", "").strip()
    authorize_url = (
        os.environ.get("GITHUB_OAUTH_AUTHORIZE_URL", _GITHUB_AUTHORIZE_URL_DEFAULT).strip()
        or _GITHUB_AUTHORIZE_URL_DEFAULT
    )
    token_url = (
        os.environ.get("GITHUB_OAUTH_TOKEN_URL", _GITHUB_TOKEN_URL_DEFAULT).strip()
        or _GITHUB_TOKEN_URL_DEFAULT
    )
    api_base = (
        os.environ.get("GITHUB_API_BASE", _GITHUB_API_BASE_DEFAULT).strip()
        or _GITHUB_API_BASE_DEFAULT
    ).rstrip("/")
    return client_id, client_secret, authorize_url, token_url, api_base


def _github_callback_base_url(core, request: web.Request) -> str:
    """Return deterministic callback base URL for GitHub OAuth."""
    explicit = os.environ.get("GITHUB_CALLBACK_BASE_URL", "").strip().rstrip("/")
    if explicit:
        return explicit
    canonical = str(getattr(core, "_PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
    if canonical:
        return canonical
    return str(core._public_base_url(request)).strip().rstrip("/")


def _set_facebook_oauth_cookie(
    core,
    response: web.StreamResponse,
    request: web.Request,
    name: str,
    value: str,
) -> None:
    kwargs = {
        "max_age": _FACEBOOK_OAUTH_MAX_AGE,
        "httponly": True,
        "secure": core._cookie_secure(request),
        "samesite": "Lax",
        "path": "/",
    }
    domain = core._cookie_domain(request)
    if domain:
        kwargs["domain"] = domain
    response.set_cookie(name, value, **kwargs)


def _clear_facebook_oauth_cookies(response: web.StreamResponse, request: web.Request | None = None) -> None:
    domain = _core()._cookie_domain(request) if request else None
    for cookie in (_FACEBOOK_OAUTH_STATE_COOKIE, _FACEBOOK_OAUTH_SOURCE_COOKIE, _FACEBOOK_OAUTH_NEXT_COOKIE):
        response.del_cookie(cookie, path="/")
        if domain:
            response.del_cookie(cookie, path="/", domain=domain)


def _set_github_oauth_cookie(
    core,
    response: web.StreamResponse,
    request: web.Request,
    name: str,
    value: str,
) -> None:
    kwargs = {
        "max_age": _GITHUB_OAUTH_MAX_AGE,
        "httponly": True,
        "secure": core._cookie_secure(request),
        "samesite": "Lax",
        "path": "/",
    }
    domain = core._cookie_domain(request)
    if domain:
        kwargs["domain"] = domain
    response.set_cookie(name, value, **kwargs)


def _clear_github_oauth_cookies(response: web.StreamResponse, request: web.Request | None = None) -> None:
    domain = _core()._cookie_domain(request) if request else None
    for cookie in (_GITHUB_OAUTH_STATE_COOKIE, _GITHUB_OAUTH_SOURCE_COOKIE, _GITHUB_OAUTH_NEXT_COOKIE):
        response.del_cookie(cookie, path="/")
        if domain:
            response.del_cookie(cookie, path="/", domain=domain)


async def handle_google_auth(request: web.Request) -> web.Response:
    """POST /auth/google — verify Google ID token, create session."""
    core = _core()
    body = await request.json()
    source = _normalize_source(body.get("source", "claude"))
    core._track_event(
        request,
        "google_signin_click",
        gate_type=core._analytics_gate_type_from_request(request) or "inline_gsi",
        cta_id="gsi_button",
        source=source,
        meta={"source": source, "inferred": True, "via": "auth_google_attempt"},
        status_code=200,
        dedupe_ttl_s=15.0,
    )
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
                    "review_pending": True,
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
        user, api_key = _ensure_trial_access(core, user, email)
        status = _user_status(user)
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
                "status": status,
                "demo_prompt_count": core._auth.get_demo_count(email),
                "demo_unlimited": core._is_demo_unlimited(user) if status == "approved" else False,
                "review_pending": status == "pending",
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
            meta={"source": source, "status": status},
            status_code=200,
        )
        core._track_event(
            request,
            "auth_google_success",
            user_id=user["user_id"],
            user_type="trial",
            source=source,
            meta={"source": source, "status": status, "new_user": True},
            status_code=200,
        )

        _send_signup_emails(
            core,
            user=user,
            email=email,
            name=name,
            user_type="trial",
            is_trial_branch=True,
        )
        return resp

    user = core._auth.create_pending_user(email, name, picture, user_type=user_type)
    status = _user_status(user)
    session_token = core.create_session_token(user["user_id"], email)

    _send_signup_emails(
        core,
        user=user,
        email=email,
        name=name,
        user_type=user_type,
        is_trial_branch=False,
    )

    core._track_event(
        request,
        "signup_created",
        user_id=user["user_id"],
        user_type=user_type,
        source=source,
        meta={"source": source, "status": status},
        status_code=200,
    )

    if status == "approved":
        api_key = _ensure_user_api_key(core, user, email)
        agent_id = f"claude-{core._key_hash(api_key)}"
        resp = web.json_response(
            {
                "ok": True,
                "email": email,
                "name": name,
                "picture": picture,
                "agent_id": agent_id,
                "user_type": _user_type(user, user_type),
                "status": "approved",
                "demo_prompt_count": core._auth.get_demo_count(email),
                "demo_unlimited": core._is_demo_unlimited(user),
                "review_pending": False,
                "claude_access_requested": False,
                "is_admin": email.lower() in core.ADMIN_EMAILS,
            }
        )
        core._set_session_cookie(resp, session_token, request)
        core._track_event(
            request,
            "auth_google_success",
            user_id=user["user_id"],
            user_type=_user_type(user, user_type),
            source=source,
            meta={"source": source, "status": "approved", "new_user": True},
            status_code=200,
        )
        return resp

    resp = web.json_response(
        {
            "pending": True,
            "status": "pending",
            "user_type": user_type,
            "review_pending": True,
            "claude_access_requested": user_type == "claude",
            "message": "Your sign-up request has been submitted. We'll review it and notify you by email.",
        }
    )
    core._set_session_cookie(resp, session_token, request)
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


async def handle_facebook_start(request: web.Request) -> web.Response:
    """GET /auth/facebook/start — begin Facebook OAuth redirect flow."""
    core = _core()
    source = _normalize_source(request.query.get("source", "claude"))
    next_path = _safe_next_path(request.query.get("next", ""), source=source)
    app_id, app_secret, dialog_url, _graph_base = _facebook_oauth_config()
    if not app_id or not app_secret:
        core._track_event(
            request,
            "auth_facebook_fail",
            source=source,
            error_code="missing_config",
            meta={"source": source, "reason": "missing_config"},
            status_code=503,
        )
        raise web.HTTPFound(_append_query_params(next_path, auth_error="facebook_not_configured"))

    state = secrets.token_urlsafe(24)
    redirect_uri = f"{core._public_base_url(request)}/auth/facebook/callback"
    oauth_params = {
        "client_id": app_id,
        "redirect_uri": redirect_uri,
        "scope": "email,public_profile",
        "response_type": "code",
        "state": state,
    }
    oauth_url = f"{dialog_url}?{urlencode(oauth_params)}"

    resp = web.HTTPFound(oauth_url)
    _set_facebook_oauth_cookie(core, resp, request, _FACEBOOK_OAUTH_STATE_COOKIE, state)
    _set_facebook_oauth_cookie(core, resp, request, _FACEBOOK_OAUTH_SOURCE_COOKIE, source)
    _set_facebook_oauth_cookie(core, resp, request, _FACEBOOK_OAUTH_NEXT_COOKIE, next_path)
    core._track_event(
        request,
        "auth_facebook_attempt",
        source=source,
        meta={"source": source, "stage": "oauth_start"},
        status_code=302,
    )
    return resp


async def handle_facebook_callback(request: web.Request) -> web.Response:
    """GET /auth/facebook/callback — exchange code and create session."""
    core = _core()
    app_id, app_secret, _dialog_url, graph_base = _facebook_oauth_config()
    source = _normalize_source(
        request.cookies.get(_FACEBOOK_OAUTH_SOURCE_COOKIE, request.query.get("source", "claude"))
    )
    next_path = _safe_next_path(
        request.cookies.get(_FACEBOOK_OAUTH_NEXT_COOKIE, request.query.get("next", "")),
        source=source,
    )

    def _redirect(*, auth_error: str = "", pending: bool = False) -> web.Response:
        location = _append_query_params(
            next_path,
            auth_error=auth_error,
            auth_pending="1" if pending else "",
        )
        resp = web.HTTPFound(location)
        _clear_facebook_oauth_cookies(resp, request)
        return resp

    if not app_id or not app_secret:
        core._track_event(
            request,
            "auth_facebook_fail",
            source=source,
            error_code="missing_config",
            meta={"source": source, "reason": "missing_config"},
            status_code=503,
        )
        return _redirect(auth_error="facebook_not_configured")

    if request.query.get("error"):
        core._track_event(
            request,
            "auth_facebook_fail",
            source=source,
            error_code="oauth_denied",
            meta={"source": source, "reason": "oauth_denied"},
            status_code=401,
        )
        return _redirect(auth_error="facebook_denied")

    expected_state = request.cookies.get(_FACEBOOK_OAUTH_STATE_COOKIE, "")
    got_state = str(request.query.get("state", "")).strip()
    if not expected_state or not got_state or not hmac.compare_digest(expected_state, got_state):
        core._track_event(
            request,
            "auth_facebook_fail",
            source=source,
            error_code="invalid_state",
            meta={"source": source, "reason": "invalid_state"},
            status_code=401,
        )
        return _redirect(auth_error="facebook_state_invalid")

    code = str(request.query.get("code", "")).strip()
    if not code:
        core._track_event(
            request,
            "auth_facebook_fail",
            source=source,
            error_code="missing_code",
            meta={"source": source, "reason": "missing_code"},
            status_code=400,
        )
        return _redirect(auth_error="facebook_exchange_failed")

    redirect_uri = f"{core._public_base_url(request)}/auth/facebook/callback"
    token_payload = {}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            token_resp = await client.get(
                f"{graph_base}/oauth/access_token",
                params={
                    "client_id": app_id,
                    "client_secret": app_secret,
                    "redirect_uri": redirect_uri,
                    "code": code,
                },
            )
        token_resp.raise_for_status()
        token_payload = token_resp.json() if token_resp.content else {}
        access_token = str(token_payload.get("access_token", "")).strip()
    except Exception:
        access_token = ""
    if not access_token:
        core._track_event(
            request,
            "auth_facebook_fail",
            source=source,
            error_code="token_exchange_failed",
            meta={"source": source, "reason": "token_exchange_failed"},
            status_code=401,
        )
        return _redirect(auth_error="facebook_exchange_failed")

    profile = {}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            profile_resp = await client.get(
                f"{graph_base}/me",
                params={
                    "fields": "id,name,email,picture.type(large)",
                    "access_token": access_token,
                },
            )
        profile_resp.raise_for_status()
        profile = profile_resp.json() if profile_resp.content else {}
    except Exception:
        core._track_event(
            request,
            "auth_facebook_fail",
            source=source,
            error_code="profile_fetch_failed",
            meta={"source": source, "reason": "profile_fetch_failed"},
            status_code=502,
        )
        return _redirect(auth_error="facebook_profile_failed")
    if not isinstance(profile, dict):
        core._track_event(
            request,
            "auth_facebook_fail",
            source=source,
            error_code="invalid_profile_payload",
            meta={"source": source, "reason": "invalid_profile_payload"},
            status_code=502,
        )
        return _redirect(auth_error="facebook_profile_failed")
    email = _normalized_email(profile.get("email", ""))
    name = str(profile.get("name", "")).strip()
    picture = ""
    picture_payload = profile.get("picture")
    if isinstance(picture_payload, dict):
        picture_data = picture_payload.get("data", {})
        if isinstance(picture_data, dict):
            picture = str(picture_data.get("url", "")).strip()
    if not email:
        core._track_event(
            request,
            "auth_facebook_fail",
            source=source,
            error_code="missing_email",
            meta={"source": source, "reason": "missing_email"},
            status_code=400,
        )
        return _redirect(auth_error="facebook_email_required")

    core._track_event(
        request,
        "auth_facebook_attempt",
        source=source,
        meta={"source": source, "stage": "oauth_callback"},
        status_code=200,
    )
    user_type = "trial" if source == "trial" else "claude"
    existing = core._auth.find_user_by_email(email)

    if existing:
        status = existing.get("status", "approved")
        if status == "approved":
            user = core._auth.get_or_create_user(email, name, picture)
            session_token = core.create_session_token(user["user_id"], email)
            resp = _redirect()
            core._set_session_cookie(resp, session_token, request)
            core._track_event(
                request,
                "auth_facebook_success",
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
                session_token = core.create_session_token(existing["user_id"], email)
                resp = _redirect(pending=True)
                core._set_session_cookie(resp, session_token, request)
                core._track_event(
                    request,
                    "auth_facebook_success",
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
                "auth_facebook_pending",
                user_id=existing["user_id"],
                user_type=pending_user_type,
                source=source,
                meta={"source": source, "status": "pending", "existing": True},
                status_code=200,
            )
            return _redirect(auth_error="pending_review")

        if status == "rejected":
            core._track_event(
                request,
                "auth_facebook_fail",
                user_id=existing["user_id"],
                user_type=existing.get("user_type", "claude"),
                source=source,
                error_code="rejected",
                meta={"source": source, "reason": "rejected"},
                status_code=403,
            )
            return _redirect(auth_error="rejected")

    if source == "trial":
        user = core._auth.create_pending_user(email, name, picture, user_type="trial")
        user, _api_key = _ensure_trial_access(core, user, email)
        status = _user_status(user)
        session_token = core.create_session_token(user["user_id"], email)
        resp = _redirect(pending=status == "pending")
        core._set_session_cookie(resp, session_token, request)
        core._track_event(
            request,
            "signup_created",
            user_id=user["user_id"],
            user_type="trial",
            source=source,
            meta={"source": source, "status": status},
            status_code=200,
        )
        core._track_event(
            request,
            "auth_facebook_success",
            user_id=user["user_id"],
            user_type="trial",
            source=source,
            meta={"source": source, "status": status, "new_user": True},
            status_code=200,
        )

        _send_signup_emails(
            core,
            user=user,
            email=email,
            name=name,
            user_type="trial",
            is_trial_branch=True,
        )
        return resp

    user = core._auth.create_pending_user(email, name, picture, user_type=user_type)
    status = _user_status(user)
    session_token = core.create_session_token(user["user_id"], email)
    _send_signup_emails(
        core,
        user=user,
        email=email,
        name=name,
        user_type=user_type,
        is_trial_branch=False,
    )
    resp = _redirect(pending=status == "pending")
    core._set_session_cookie(resp, session_token, request)
    core._track_event(
        request,
        "signup_created",
        user_id=user["user_id"],
        user_type=user_type,
        source=source,
        meta={"source": source, "status": status},
        status_code=200,
    )
    core._track_event(
        request,
        "auth_facebook_success" if status == "approved" else "auth_facebook_pending",
        user_id=user["user_id"],
        user_type=user_type,
        source=source,
        meta={"source": source, "status": status, "new_user": True},
        status_code=200,
    )
    return resp


async def handle_github_start(request: web.Request) -> web.Response:
    """GET /auth/github/start — begin GitHub OAuth redirect flow."""
    core = _core()
    source = _normalize_source(request.query.get("source", "claude"))
    next_path = _safe_next_path(request.query.get("next", ""), source=source)
    client_id, client_secret, authorize_url, _token_url, _api_base = _github_oauth_config()
    if not client_id or not client_secret:
        core._track_event(
            request,
            "auth_github_fail",
            source=source,
            error_code="missing_config",
            meta={"source": source, "reason": "missing_config"},
            status_code=503,
        )
        raise web.HTTPFound(_append_query_params(next_path, auth_error="github_not_configured"))

    state = secrets.token_urlsafe(24)
    redirect_uri = f"{_github_callback_base_url(core, request)}/auth/github/callback"
    oauth_params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": "read:user user:email",
        "state": state,
        "allow_signup": "true",
    }
    oauth_url = f"{authorize_url}?{urlencode(oauth_params)}"

    resp = web.HTTPFound(oauth_url)
    _set_github_oauth_cookie(core, resp, request, _GITHUB_OAUTH_STATE_COOKIE, state)
    _set_github_oauth_cookie(core, resp, request, _GITHUB_OAUTH_SOURCE_COOKIE, source)
    _set_github_oauth_cookie(core, resp, request, _GITHUB_OAUTH_NEXT_COOKIE, next_path)
    core._track_event(
        request,
        "auth_github_attempt",
        source=source,
        meta={"source": source, "stage": "oauth_start"},
        status_code=302,
    )
    return resp


async def handle_github_callback(request: web.Request) -> web.Response:
    """GET /auth/github/callback — exchange code and create session."""
    core = _core()
    client_id, client_secret, _authorize_url, token_url, api_base = _github_oauth_config()
    source = _normalize_source(
        request.cookies.get(_GITHUB_OAUTH_SOURCE_COOKIE, request.query.get("source", "claude"))
    )
    next_path = _safe_next_path(
        request.cookies.get(_GITHUB_OAUTH_NEXT_COOKIE, request.query.get("next", "")),
        source=source,
    )

    def _redirect(*, auth_error: str = "", pending: bool = False) -> web.Response:
        location = _append_query_params(
            next_path,
            auth_error=auth_error,
            auth_pending="1" if pending else "",
        )
        resp = web.HTTPFound(location)
        _clear_github_oauth_cookies(resp, request)
        return resp

    if not client_id or not client_secret:
        core._track_event(
            request,
            "auth_github_fail",
            source=source,
            error_code="missing_config",
            meta={"source": source, "reason": "missing_config"},
            status_code=503,
        )
        return _redirect(auth_error="github_not_configured")

    if request.query.get("error"):
        core._track_event(
            request,
            "auth_github_fail",
            source=source,
            error_code="oauth_denied",
            meta={"source": source, "reason": "oauth_denied"},
            status_code=401,
        )
        return _redirect(auth_error="github_denied")

    expected_state = request.cookies.get(_GITHUB_OAUTH_STATE_COOKIE, "")
    got_state = str(request.query.get("state", "")).strip()
    if not expected_state or not got_state or not hmac.compare_digest(expected_state, got_state):
        core._track_event(
            request,
            "auth_github_fail",
            source=source,
            error_code="invalid_state",
            meta={"source": source, "reason": "invalid_state"},
            status_code=401,
        )
        return _redirect(auth_error="github_state_invalid")

    code = str(request.query.get("code", "")).strip()
    if not code:
        core._track_event(
            request,
            "auth_github_fail",
            source=source,
            error_code="missing_code",
            meta={"source": source, "reason": "missing_code"},
            status_code=400,
        )
        return _redirect(auth_error="github_exchange_failed")

    redirect_uri = f"{_github_callback_base_url(core, request)}/auth/github/callback"
    token_payload = {}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            token_resp = await client.post(
                token_url,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": redirect_uri,
                    "code": code,
                },
                headers={"Accept": "application/json"},
            )
        token_resp.raise_for_status()
        token_payload = token_resp.json() if token_resp.content else {}
        access_token = str(token_payload.get("access_token", "")).strip()
    except Exception:
        access_token = ""
    if not access_token:
        core._track_event(
            request,
            "auth_github_fail",
            source=source,
            error_code="token_exchange_failed",
            meta={"source": source, "reason": "token_exchange_failed"},
            status_code=401,
        )
        return _redirect(auth_error="github_exchange_failed")

    api_headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "unchainedsky-auth",
    }
    profile = {}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            profile_resp = await client.get(
                f"{api_base}/user",
                headers=api_headers,
            )
        profile_resp.raise_for_status()
        profile = profile_resp.json() if profile_resp.content else {}
    except Exception:
        core._track_event(
            request,
            "auth_github_fail",
            source=source,
            error_code="profile_fetch_failed",
            meta={"source": source, "reason": "profile_fetch_failed"},
            status_code=502,
        )
        return _redirect(auth_error="github_profile_failed")
    if not isinstance(profile, dict):
        core._track_event(
            request,
            "auth_github_fail",
            source=source,
            error_code="invalid_profile_payload",
            meta={"source": source, "reason": "invalid_profile_payload"},
            status_code=502,
        )
        return _redirect(auth_error="github_profile_failed")

    email = _normalized_email(profile.get("email", ""))
    if not email:
        emails_payload = []
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                emails_resp = await client.get(
                    f"{api_base}/user/emails",
                    headers=api_headers,
                )
            emails_resp.raise_for_status()
            emails_payload = emails_resp.json() if emails_resp.content else []
        except Exception:
            core._track_event(
                request,
                "auth_github_fail",
                source=source,
                error_code="email_fetch_failed",
                meta={"source": source, "reason": "email_fetch_failed"},
                status_code=502,
            )
            return _redirect(auth_error="github_profile_failed")
        if not isinstance(emails_payload, list):
            core._track_event(
                request,
                "auth_github_fail",
                source=source,
                error_code="invalid_emails_payload",
                meta={"source": source, "reason": "invalid_emails_payload"},
                status_code=502,
            )
            return _redirect(auth_error="github_profile_failed")

        best_verified_primary = ""
        best_verified = ""
        best_primary = ""
        first = ""
        for row in emails_payload:
            if not isinstance(row, dict):
                continue
            raw = _normalized_email(row.get("email", ""))
            if not raw:
                continue
            if not first:
                first = raw
            is_primary = bool(row.get("primary"))
            is_verified = bool(row.get("verified"))
            if is_primary and is_verified:
                best_verified_primary = raw
                break
            if is_verified and not best_verified:
                best_verified = raw
            if is_primary and not best_primary:
                best_primary = raw
        email = best_verified_primary or best_verified or best_primary or first

    name = str(profile.get("name", "")).strip() or str(profile.get("login", "")).strip()
    picture = str(profile.get("avatar_url", "")).strip()
    if not email:
        core._track_event(
            request,
            "auth_github_fail",
            source=source,
            error_code="missing_email",
            meta={"source": source, "reason": "missing_email"},
            status_code=400,
        )
        return _redirect(auth_error="github_email_required")

    core._track_event(
        request,
        "auth_github_attempt",
        source=source,
        meta={"source": source, "stage": "oauth_callback"},
        status_code=200,
    )
    user_type = "trial" if source == "trial" else "claude"
    existing = core._auth.find_user_by_email(email)

    if existing:
        status = existing.get("status", "approved")
        if status == "approved":
            user = core._auth.get_or_create_user(email, name, picture)
            session_token = core.create_session_token(user["user_id"], email)
            resp = _redirect()
            core._set_session_cookie(resp, session_token, request)
            core._track_event(
                request,
                "auth_github_success",
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
                session_token = core.create_session_token(existing["user_id"], email)
                resp = _redirect(pending=True)
                core._set_session_cookie(resp, session_token, request)
                core._track_event(
                    request,
                    "auth_github_success",
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
                "auth_github_pending",
                user_id=existing["user_id"],
                user_type=pending_user_type,
                source=source,
                meta={"source": source, "status": "pending", "existing": True},
                status_code=200,
            )
            return _redirect(auth_error="pending_review")

        if status == "rejected":
            core._track_event(
                request,
                "auth_github_fail",
                user_id=existing["user_id"],
                user_type=existing.get("user_type", "claude"),
                source=source,
                error_code="rejected",
                meta={"source": source, "reason": "rejected"},
                status_code=403,
            )
            return _redirect(auth_error="rejected")

    if source == "trial":
        user = core._auth.create_pending_user(email, name, picture, user_type="trial")
        user, _api_key = _ensure_trial_access(core, user, email)
        status = _user_status(user)
        session_token = core.create_session_token(user["user_id"], email)
        resp = _redirect(pending=status == "pending")
        core._set_session_cookie(resp, session_token, request)
        core._track_event(
            request,
            "signup_created",
            user_id=user["user_id"],
            user_type="trial",
            source=source,
            meta={"source": source, "status": status},
            status_code=200,
        )
        core._track_event(
            request,
            "auth_github_success",
            user_id=user["user_id"],
            user_type="trial",
            source=source,
            meta={"source": source, "status": status, "new_user": True},
            status_code=200,
        )

        _send_signup_emails(
            core,
            user=user,
            email=email,
            name=name,
            user_type="trial",
            is_trial_branch=True,
        )
        return resp

    user = core._auth.create_pending_user(email, name, picture, user_type=user_type)
    status = _user_status(user)
    session_token = core.create_session_token(user["user_id"], email)
    _send_signup_emails(
        core,
        user=user,
        email=email,
        name=name,
        user_type=user_type,
        is_trial_branch=False,
    )
    resp = _redirect(pending=status == "pending")
    core._set_session_cookie(resp, session_token, request)
    core._track_event(
        request,
        "signup_created",
        user_id=user["user_id"],
        user_type=user_type,
        source=source,
        meta={"source": source, "status": status},
        status_code=200,
    )
    core._track_event(
        request,
        "auth_github_success" if status == "approved" else "auth_github_pending",
        user_id=user["user_id"],
        user_type=user_type,
        source=source,
        meta={"source": source, "status": status, "new_user": True},
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

    resp = web.json_response(
        {
            "ok": True,
            "user_id": user["user_id"],
            "agent_id": agent_id,
            "email": email,
        }
    )
    core._set_session_cookie(resp, token, request)
    return resp


# ---------------------------------------------------------------------------
# External auth redirect (cross-origin login for sky-search, etc.)
# ---------------------------------------------------------------------------

_AUTH_LOGIN_ALLOWED_ORIGINS = {
    "https://analytics.unchainedsky.com",
    "https://searchagentsky.com",
    "https://search.unchainedsky.com",
}
_AUTH_LOGIN_ALLOWED_ORIGINS_DEV = {
    "http://localhost:3000",
    "http://127.0.0.1:3000",
}
_AUTH_LOGIN_IDENTITY_TTL = 24 * 3600  # 24 hours


_AUTH_CODE_TTL = 120  # seconds — one-time codes expire after 2 minutes


def _validate_redirect_uri(uri: str, *, allow_dev: bool = False) -> bool:
    """Check redirect_uri against the allowlist (origin + /auth/callback path)."""
    if not uri:
        return False
    parsed = urlparse(uri)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    allowed = _AUTH_LOGIN_ALLOWED_ORIGINS
    if allow_dev:
        allowed = allowed | _AUTH_LOGIN_ALLOWED_ORIGINS_DEV
    return origin in allowed and parsed.path == "/auth/callback"


def _mint_identity_token(user: dict) -> str:
    """Mint a cross-origin identity JWT for external services."""
    import jwt as pyjwt

    core = _core()
    now = int(time.time())
    return pyjwt.encode(
        {
            "sub": user["user_id"],
            "name": user.get("name", ""),
            "email": user.get("email", ""),
            "picture": user.get("picture", ""),
            "aud": "sky-search",
            "iat": now,
            "exp": now + _AUTH_LOGIN_IDENTITY_TTL,
        },
        core.JWT_SECRET,
        algorithm="HS256",
    )


_AUTH_LOGIN_PAGE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in — Unchained</title>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  body{min-height:100vh;display:flex;align-items:center;justify-content:center;
       background:#0d1117;color:#c9d1d9;font-family:-apple-system,BlinkMacSystemFont,sans-serif}
  .card{text-align:center;padding:2rem}
  h1{font-size:1.25rem;margin-bottom:.5rem}
  p{font-size:.875rem;color:#8b949e;margin-bottom:1.5rem}
  .g_id_signin{display:flex;justify-content:center;width:320px;max-width:100%;margin:0 auto}
  #loginerr{color:#f85149;font-size:.8rem;margin-top:1rem;min-height:1.2em}
</style>
</head>
<body>
<div class="card">
  <h1>Sign in to continue</h1>
  <p>You'll be redirected back after signing in.</p>
  <div id="g_id_onload"
       data-client_id="__GOOGLE_CLIENT_ID__"
       data-callback="handleCredential"
       data-auto_prompt="false"
       data-context="signin"
       data-ux_mode="popup"></div>
  <div class="g_id_signin"
       data-type="standard"
       data-shape="rectangular"
       data-theme="outline"
       data-text="signin_with"
       data-size="large"
       data-logo_alignment="center"
       data-width="320"></div>
  <div id="loginerr"></div>
</div>
<script src="https://accounts.google.com/gsi/client" async defer></script>
<script>
async function handleCredential(response) {
  var errEl = document.getElementById('loginerr');
  errEl.textContent = '';
  try {
    var r = await fetch('/auth/google', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({credential: response.credential}),
    });
    if (!r.ok) {
      var d = await r.json().catch(function(){return {};});
      errEl.textContent = d.error || 'Sign-in failed';
      return;
    }
    // Session cookie is set — reload to trigger the redirect
    window.location.reload();
  } catch(e) {
    errEl.textContent = e.message || 'Network error';
  }
}
</script>
</body>
</html>
"""


def _issue_auth_code(user: dict, redirect_uri: str, scope: str) -> str:
    """Mint a single-use code bound to redirect_uri, persist it, and return it."""
    core = _core()
    code = secrets.token_hex(32)
    now = time.time()
    with core._auth._conn() as conn:
        conn.execute(
            "INSERT INTO auth_codes (code, user_id, redirect_uri, scope, created_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (code, user["user_id"], redirect_uri, scope, now, now + _AUTH_CODE_TTL),
        )
    return code


async def handle_auth_login(request: web.Request) -> web.Response:
    """GET /auth/login — external auth redirect for cross-origin login.

    If the user is already logged in, issues a short-lived one-time code and
    redirects back to redirect_uri with ?code=<code>&state=<state>.
    Otherwise serves a minimal Google Sign-In page; after login the page
    reloads and hits this handler again (now with a session cookie) to
    complete the redirect.
    """
    core = _core()
    redirect_uri = request.query.get("redirect_uri", "").strip()
    scope = request.query.get("scope", "").strip()
    state = request.query.get("state", "").strip()

    allow_dev = not core.GOOGLE_CLIENT_ID
    if not _validate_redirect_uri(redirect_uri, allow_dev=allow_dev):
        return web.Response(text="Invalid redirect_uri", status=400)

    # Check if user is already logged in
    auth_info = core._authenticate(request)
    if auth_info is not None:
        email = auth_info.get("email", "")
        user = core._auth.find_user_by_email(email) if email else None
        if user and user.get("status", "approved") == "approved":
            code = _issue_auth_code(user, redirect_uri, scope)
            sep = "&" if "?" in redirect_uri else "?"
            location = f"{redirect_uri}{sep}code={code}"
            if state:
                location += f"&state={state}"
            return web.HTTPFound(location)

    # Not logged in — serve login page
    html = core.inject_google_client_id(_AUTH_LOGIN_PAGE, core.GOOGLE_CLIENT_ID)
    return web.Response(text=html, content_type="text/html")


async def handle_auth_token(request: web.Request) -> web.Response:
    """POST /auth/token — exchange a one-time auth code for an identity JWT."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response(
            {"error": "invalid_request", "error_description": "Body must be JSON."},
            status=400,
        )

    grant_type = body.get("grant_type", "")
    code = body.get("code", "").strip()
    redirect_uri = body.get("redirect_uri", "").strip()

    if grant_type != "authorization_code":
        return web.json_response({"error": "unsupported_grant_type"}, status=400)
    if not code or not redirect_uri:
        return web.json_response(
            {"error": "invalid_request", "error_description": "code and redirect_uri are required."},
            status=400,
        )

    core = _core()
    now = time.time()
    with core._auth._conn() as conn:
        row = conn.execute(
            "SELECT user_id, redirect_uri, scope, expires_at, used FROM auth_codes WHERE code = ?",
            (code,),
        ).fetchone()

        if row is None:
            return web.json_response(
                {"error": "invalid_grant", "error_description": "Code not found."}, status=400
            )

        db_user_id, db_redirect_uri, db_scope, expires_at, used = row

        if used:
            return web.json_response(
                {"error": "invalid_grant", "error_description": "Code already used."}, status=400
            )
        if now > expires_at:
            return web.json_response(
                {"error": "invalid_grant", "error_description": "Code expired."}, status=400
            )
        if redirect_uri != db_redirect_uri:
            return web.json_response(
                {"error": "invalid_grant", "error_description": "redirect_uri mismatch."}, status=400
            )

        # Mark consumed before issuing token (prevent replay)
        conn.execute("UPDATE auth_codes SET used = 1 WHERE code = ?", (code,))

        user_row = conn.execute(
            "SELECT user_id, email, name, picture FROM users WHERE user_id = ?",
            (db_user_id,),
        ).fetchone()

    if user_row is None:
        return web.json_response(
            {"error": "invalid_grant", "error_description": "User not found."}, status=400
        )

    user = {
        "user_id": user_row[0],
        "email": user_row[1],
        "name": user_row[2] or "",
        "picture": user_row[3] or "",
    }
    token = _mint_identity_token(user)
    return web.json_response({
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": _AUTH_LOGIN_IDENTITY_TTL,
        "scope": db_scope,
    })


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

        # Credit state (backward-compatible, added for trial users)
        credit_state = {}
        if user:
            try:
                from credit import CreditLedger
                ledger = CreditLedger(db_path=core._auth.db_path)
                account = ledger.ensure_account(user["user_id"])
                # Grant initial trial credit from existing OpenRouter budget
                if user_type == "trial" or status == "pending":
                    budget_state = core._openrouter_budget_state_for_user(user["user_id"])
                    ledger.ensure_trial_grant_from_openrouter_budget(
                        user["user_id"],
                        current_spend_usd=budget_state.get("spent_usd", 0),
                        budget_usd=budget_state.get("budget_usd", 0),
                    )
                held = ledger.held_reservation_total(account["account_id"])
                available = account["balance_micro_usd"] - held
                credit_state = {
                    "balance_micro_usd": account["balance_micro_usd"],
                    "balance_usd": account["balance_usd"],
                    "held_micro_usd": held,
                    "available_micro_usd": max(0, available),
                    "available_usd": round(max(0, available) / 1_000_000, 6),
                    "total_granted_micro_usd": account["total_granted_micro_usd"],
                    "total_spent_micro_usd": account["total_spent_micro_usd"],
                }
            except Exception:
                pass

        return web.json_response(
            {
                "authenticated": True,
                "user_id": auth_info.get("user_id", ""),
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
                "credit": credit_state,
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
                        "user_id": user.get("user_id", ""),
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
        _approved_account_email_body(user.get("name") or email),
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


async def handle_landing_v2(request: web.Request) -> web.Response:
    """GET /landing-v2 — admin-only A/B test variant of the landing page."""
    if not is_admin(request):
        raise web.HTTPNotFound()
    core = _core()
    core._track_page_view(request)
    from web_app.templates import LANDING_V2_HTML
    html = LANDING_V2_HTML.replace("__CONTACT_EMAIL__", core.CONTACT_EMAIL)
    return web.Response(text=html, content_type="text/html")


def _scheduler_api_auth(core, request: web.Request) -> tuple[dict | None, web.Response | None]:
    auth_info = core._authenticate(request)
    if auth_info is None:
        return None, web.json_response({"error": "Not authenticated"}, status=401)
    if core._is_pending_user(auth_info):
        return None, core._pending_limited_response()
    return auth_info, None


def _scheduler_trial_agent_auth(core, request: web.Request, body: dict) -> dict | None:
    """Authenticate a hosted trial-agent scheduler call using the deployment service key.

    The hosted worker never receives a raw user API key.  Instead it sends the
    shared service token (``HOSTED_AGENT_SERVICE_TOKEN`` or fallback
    ``TRIAL_AGENT_KEY``) as a Bearer token together with a scoped scheduler
    grant id and the chat session id.  The grant (minted by
    :func:`_mint_scheduler_turn_grant`) binds the user id, chat session id,
    and expiry.  Deriving the user from the validated grant ensures the
    request body cannot spoof user ownership.

    Both the ``session_id`` body field and the ``scheduler_grant_id`` body
    field must match the grant's stored bindings — the session is validated
    here so that downstream checks see a consistent set of claims.
    """
    headers = getattr(request, "headers", {}) or {}
    auth_header = str(headers.get("Authorization", "") or "").strip()
    if not auth_header.lower().startswith("bearer "):
        return None
    token = auth_header[len("bearer "):].strip()

    # Prefer HOSTED_AGENT_SERVICE_TOKEN (scoped, separate from TRIAL_AGENT_KEY)
    # with a compatibility fallback so the credit-branch service-token change
    # can consolidate on one name.
    service_token = (
        str(getattr(core, "HOSTED_AGENT_SERVICE_TOKEN", "") or "")
        or str(getattr(core, "TRIAL_AGENT_KEY", "") or "")
    )
    if not token or not service_token:
        return None
    if not hmac.compare_digest(token, service_token):
        return None

    grant_id = str(body.get("scheduler_grant_id", "") or "").strip()
    if not grant_id:
        return None

    grants = getattr(core, "_scheduler_turn_grants", None)
    if not isinstance(grants, dict):
        return None
    grant_meta = grants.get(grant_id)
    if not isinstance(grant_meta, dict):
        return None

    now = time.time()
    expires_at = 0.0
    try:
        expires_at = float(grant_meta.get("expires_at", 0) or 0)
    except (TypeError, ValueError):
        return None
    if expires_at <= now:
        return None

    user_id = str(grant_meta.get("user_id", "") or "").strip()
    if not user_id:
        return None

    # Validate session_id binding in the service auth layer itself so a
    # mismatched session_id is caught before any read/mutation occurs.
    body_session_id = str(body.get("session_id", "") or "").strip()
    grant_session_id = str(grant_meta.get("session_id", "") or "").strip()
    if body_session_id and grant_session_id:
        if not hmac.compare_digest(body_session_id, grant_session_id):
            return None

    return {"user_id": user_id, "trial_agent_auth": True}


async def _scheduler_auth_with_trial_agent_fallback(
    core, request: web.Request
) -> tuple[dict | None, web.Response | None, dict | None]:
    """Authenticate scheduler agent endpoints with trial-agent fallback.

    Returns ``(auth_info, error_response, body)``.  When the caller is the
    hosted trial worker the *body* is parsed early so the grant id can be
    extracted for service-level auth.
    """
    auth_info, error = _scheduler_api_auth(core, request)
    if auth_info is not None:
        return auth_info, None, None

    # Normal auth failed — try trial-agent service auth with early body parse
    body, _body_error = await _scheduler_json_body(request)
    if body is not None:
        trial_auth_info = _scheduler_trial_agent_auth(core, request, body)
        if trial_auth_info is not None:
            return trial_auth_info, None, body

    return None, error, body


async def _scheduler_json_body(request: web.Request) -> tuple[dict | None, web.Response | None]:
    can_read = getattr(request, "can_read_body", True)
    try:
        body = await request.json() if can_read else {}
    except Exception:
        return None, web.json_response({"error": "Invalid JSON body"}, status=400)
    if body is None:
        body = {}
    if not isinstance(body, dict):
        return None, web.json_response({"error": "Body must be a JSON object"}, status=400)
    return body, None


def _scheduler_read_jobs(core, user_id: str, *, tolerate_invalid: bool = False):
    import scheduled_tasks as st

    payload = core._scheduler_read_jobs_payload(user_id)
    try:
        return st.parse_jobs_payload(payload)
    except Exception:
        if tolerate_invalid:
            return []
        raise


def _scheduler_jobs_response(core, user_id: str, jobs: list, *, write: bool = False, extra: dict | None = None) -> dict:
    import scheduled_tasks as st

    if len(jobs) > 200:
        raise ValueError("Too many jobs (max 200)")
    canonical = st.jobs_to_payload(jobs)
    if write:
        core._scheduler_write_jobs_payload(user_id, canonical)
        # Sync state: recalculate next_run_at for jobs whose schedule changed,
        # then initialize any brand-new jobs.
        state_path = core._scheduler_state_path(user_id)
        state = st.load_state(state_path)
        now = st._utcnow()
        st.recalculate_next_run(jobs, state, now)
        st.SchedulerEngine(jobs, state).initialize_missing(now)
        st.save_state(state_path, state)
    data = {
        "jobs": canonical["jobs"],
        "preview": core._scheduler_preview_rows(user_id, jobs),
    }
    if extra:
        data.update(extra)
    return data


def _scheduler_request_uses_bearer_auth(request: web.Request) -> bool:
    headers = getattr(request, "headers", {}) or {}
    auth_header = str(headers.get("Authorization", "") or "").strip()
    return auth_header.lower().startswith("bearer ")


def _scheduler_require_session_or_turn_grant(
    core,
    request: web.Request,
    auth_info: dict,
    payload: dict | None = None,
) -> web.Response | None:
    if not _scheduler_request_uses_bearer_auth(request):
        return None

    data = payload if isinstance(payload, dict) else {}
    session_id = str(data.get("session_id", "") or "").strip()
    grant_id = str(data.get("scheduler_grant_id", "") or "").strip()
    if session_id and grant_id and core._validate_scheduler_turn_grant(
        auth_info.get("user_id", ""),
        session_id,
        grant_id,
    ):
        return None
    return web.json_response(
        {"error": "scheduler JSON endpoints require a browser session or an active /schedule turn"},
        status=403,
    )


def _scheduler_merge_existing_raw_job(raw_job: dict, canonical_job: dict) -> dict:
    merged_job = dict(canonical_job)
    merged_job.update(raw_job)
    if isinstance(raw_job.get("schedule"), dict):
        merged_job["schedule"] = raw_job["schedule"]
    return merged_job


def _scheduler_parse_agent_job(
    core,
    user_id: str,
    body: dict,
    *,
    merge_existing: bool,
):
    import scheduled_tasks as st

    raw_job = body.get("job")
    if not isinstance(raw_job, dict):
        raise ValueError("job must be a JSON object")

    jobs = _scheduler_read_jobs(core, user_id)
    target_id = str(raw_job.get("id", "") or "").strip()
    if merge_existing and target_id:
        canonical_jobs = st.jobs_to_payload(jobs)["jobs"]
        for idx, existing in enumerate(jobs):
            if existing.id == target_id:
                merged_job = _scheduler_merge_existing_raw_job(raw_job, canonical_jobs[idx])
                return st.parse_jobs_payload({"jobs": [merged_job]})[0], jobs, idx, False

    return st.parse_jobs_payload({"jobs": [raw_job]})[0], jobs, None, True


def _scheduler_require_turn_grant(core, auth_info: dict, body: dict) -> web.Response | None:
    session_id = str(body.get("session_id", "") or "").strip()
    grant_id = str(body.get("scheduler_grant_id", "") or "").strip()
    if not session_id:
        return web.json_response({"error": "session_id required"}, status=400)
    if not grant_id:
        return web.json_response({"error": "scheduler_grant_id required"}, status=400)
    if not core._validate_scheduler_turn_grant(auth_info.get("user_id", ""), session_id, grant_id):
        return web.json_response(
            {"error": "scheduler trigger not active for this turn"},
            status=403,
        )
    return None


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
    auth_info, error = _scheduler_api_auth(core, request)
    if error is not None:
        return error
    user_id = auth_info["user_id"]

    import scheduled_tasks as st

    if request.method == "GET":
        grant_error = _scheduler_require_session_or_turn_grant(
            core,
            request,
            auth_info,
            dict(getattr(request, "query", {}) or {}),
        )
        if grant_error is not None:
            return grant_error
        try:
            jobs = _scheduler_read_jobs(core, user_id, tolerate_invalid=True)
            return web.json_response(_scheduler_jobs_response(core, user_id, jobs))
        except Exception:
            return web.json_response({"jobs": [], "preview": []})

    body, error = await _scheduler_json_body(request)
    if error is not None:
        return error
    grant_error = _scheduler_require_session_or_turn_grant(core, request, auth_info, body)
    if grant_error is not None:
        return grant_error

    try:
        jobs = st.parse_jobs_payload(body)
        payload = _scheduler_jobs_response(core, user_id, jobs, write=True, extra={"ok": True})
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    return web.json_response(payload)


async def handle_scheduler_preview(request: web.Request) -> web.Response:
    """POST /web/scheduler/preview — preview next run times for job payload."""
    core = _core()
    auth_info, error = _scheduler_api_auth(core, request)
    if error is not None:
        return error
    user_id = auth_info["user_id"]

    import scheduled_tasks as st

    body, error = await _scheduler_json_body(request)
    if error is not None:
        return error
    grant_error = _scheduler_require_session_or_turn_grant(core, request, auth_info, body)
    if grant_error is not None:
        return grant_error

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
    auth_info, error = _scheduler_api_auth(core, request)
    if error is not None:
        return error
    grant_error = _scheduler_require_session_or_turn_grant(
        core,
        request,
        auth_info,
        dict(getattr(request, "query", {}) or {}),
    )
    if grant_error is not None:
        return grant_error
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


async def handle_scheduler_agent_list(request: web.Request) -> web.Response:
    """POST /web/scheduler/agent/list — list scheduler jobs for the armed turn."""
    core = _core()
    auth_info, error, body = await _scheduler_auth_with_trial_agent_fallback(core, request)
    if error is not None:
        return error
    if body is None:
        body, error = await _scheduler_json_body(request)
        if error is not None:
            return error
    error = _scheduler_require_turn_grant(core, auth_info, body)
    if error is not None:
        return error
    jobs = _scheduler_read_jobs(core, auth_info["user_id"], tolerate_invalid=True)
    return web.json_response(_scheduler_jobs_response(core, auth_info["user_id"], jobs, extra={"ok": True}))


async def handle_scheduler_agent_preview(request: web.Request) -> web.Response:
    """POST /web/scheduler/agent/preview — preview one candidate job for the armed turn."""
    core = _core()
    auth_info, error, body = await _scheduler_auth_with_trial_agent_fallback(core, request)
    if error is not None:
        return error
    if body is None:
        body, error = await _scheduler_json_body(request)
        if error is not None:
            return error
    error = _scheduler_require_turn_grant(core, auth_info, body)
    if error is not None:
        return error
    try:
        job, _jobs, _idx, _created = _scheduler_parse_agent_job(
            core,
            auth_info["user_id"],
            body,
            merge_existing=True,
        )
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    except Exception as e:
        return web.json_response({"error": f"Failed to load current jobs: {e}"}, status=500)

    import scheduled_tasks as st

    canonical_job = st.jobs_to_payload([job])["jobs"][0]
    preview = core._scheduler_preview_rows(auth_info["user_id"], [job])
    return web.json_response({"ok": True, "job": canonical_job, "preview": preview[0] if preview else None})


async def handle_scheduler_agent_upsert(request: web.Request) -> web.Response:
    """POST /web/scheduler/agent/upsert — create or replace one job for the armed turn."""
    core = _core()
    auth_info, error, body = await _scheduler_auth_with_trial_agent_fallback(core, request)
    if error is not None:
        return error
    if body is None:
        body, error = await _scheduler_json_body(request)
        if error is not None:
            return error
    error = _scheduler_require_turn_grant(core, auth_info, body)
    if error is not None:
        return error
    try:
        job, jobs, existing_idx, created = _scheduler_parse_agent_job(
            core,
            auth_info["user_id"],
            body,
            merge_existing=True,
        )
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    except Exception as e:
        return web.json_response({"error": f"Failed to load current jobs: {e}"}, status=500)

    if existing_idx is None:
        jobs.append(job)
    else:
        jobs[existing_idx] = job

    try:
        payload = _scheduler_jobs_response(core, auth_info["user_id"], jobs, write=True, extra={"ok": True, "created": created})
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    payload["job"] = next((item for item in payload["jobs"] if item.get("id") == job.id), None)
    return web.json_response(payload)


async def handle_scheduler_agent_delete(request: web.Request) -> web.Response:
    """POST /web/scheduler/agent/delete — delete one job for the armed turn."""
    core = _core()
    auth_info, error, body = await _scheduler_auth_with_trial_agent_fallback(core, request)
    if error is not None:
        return error
    if body is None:
        body, error = await _scheduler_json_body(request)
        if error is not None:
            return error
    error = _scheduler_require_turn_grant(core, auth_info, body)
    if error is not None:
        return error
    job_id = str(body.get("job_id", "") or "").strip()
    if not job_id:
        return web.json_response({"error": "job_id required"}, status=400)
    try:
        jobs = _scheduler_read_jobs(core, auth_info["user_id"])
    except Exception as e:
        return web.json_response({"error": f"Failed to load current jobs: {e}"}, status=500)

    remaining = [job for job in jobs if job.id != job_id]
    if len(remaining) == len(jobs):
        return web.json_response({"error": "job not found"}, status=404)

    payload = _scheduler_jobs_response(
        core,
        auth_info["user_id"],
        remaining,
        write=True,
        extra={"ok": True, "deleted_id": job_id},
    )
    return web.json_response(payload)


# ---------------------------------------------------------------------------
# User-facing credit status/history
# ---------------------------------------------------------------------------


async def handle_credit_status(request: web.Request) -> web.Response:
    """GET /web/credit/status — return authenticated user's credit account state."""
    core = _core()
    auth_info = core._authenticate(request)
    if auth_info is None:
        return web.json_response({"error": "Not authenticated"}, status=401)

    user_id = auth_info.get("user_id", "")
    if not user_id:
        return web.json_response({"error": "No user ID"}, status=400)

    from credit import CreditLedger

    ledger = CreditLedger(db_path=core._auth.db_path)
    account = ledger.ensure_account(user_id)
    held = ledger.held_reservation_total(account["account_id"])
    available = account["balance_micro_usd"] - held

    return web.json_response({
        "user_id": user_id,
        "account_id": account["account_id"],
        "balance_micro_usd": account["balance_micro_usd"],
        "balance_usd": account["balance_usd"],
        "held_micro_usd": held,
        "held_usd": round(held / 1_000_000, 6),
        "available_micro_usd": max(0, available),
        "available_usd": round(max(0, available) / 1_000_000, 6),
        "total_granted_micro_usd": account["total_granted_micro_usd"],
        "total_granted_usd": account["total_granted_usd"],
        "total_spent_micro_usd": account["total_spent_micro_usd"],
        "total_spent_usd": account["total_spent_usd"],
    })


async def handle_credit_history(request: web.Request) -> web.Response:
    """GET /web/credit/status/history?limit=50 — return authenticated user's ledger."""
    core = _core()
    auth_info = core._authenticate(request)
    if auth_info is None:
        return web.json_response({"error": "Not authenticated"}, status=401)

    user_id = auth_info.get("user_id", "")
    if not user_id:
        return web.json_response({"error": "No user ID"}, status=400)

    try:
        limit = min(200, max(1, int(request.query.get("limit", "50"))))
    except ValueError:
        limit = 50

    from credit import CreditLedger

    ledger = CreditLedger(db_path=core._auth.db_path)
    entries = ledger.get_ledger_for_user(user_id, limit=limit)
    runs = ledger.get_runs_for_user(user_id, limit=limit)

    return web.json_response({
        "user_id": user_id,
        "ledger_entries": entries,
        "runs": runs,
    })


# ---------------------------------------------------------------------------
# Admin credit grant
# ---------------------------------------------------------------------------


async def handle_admin_credit_grant(request: web.Request) -> web.Response:
    """POST /admin/credit/grant — admin grants credit to a user.

    JSON body:
        {"user_id": "u-...", "amount_usd": 1.0, "reason": "..."}
    """
    core = _core()
    if not is_admin(request):
        return web.json_response({"error": "Admin access required"}, status=403)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    user_id = str(body.get("user_id", "")).strip()
    try:
        amount_usd = float(body.get("amount_usd", 0))
    except (TypeError, ValueError):
        return web.json_response({"error": "amount_usd must be a number"}, status=400)

    reason = str(body.get("reason", "admin_grant")).strip()

    if not user_id:
        return web.json_response({"error": "user_id required"}, status=400)
    if amount_usd <= 0:
        return web.json_response({"error": "amount_usd must be positive"}, status=400)

    from credit import CreditLedger, _usd_to_micro

    ledger = CreditLedger(db_path=core._auth.db_path)
    amount_micro = _usd_to_micro(amount_usd)

    import uuid
    idem_key = f"admin-grant-{user_id}-{uuid.uuid4().hex[:12]}"

    admin_auth = core._authenticate(request)
    admin_email = admin_auth.get("email", "") if admin_auth else ""

    try:
        result = ledger.grant(
            user_id=user_id,
            amount_micro_usd=amount_micro,
            idempotency_key=idem_key,
            metadata={"reason": reason, "admin_email": admin_email},
        )
        return web.json_response({
            "ok": True,
            "granted_micro_usd": amount_micro,
            "granted_usd": amount_usd,
            "new_balance_micro_usd": result.get("balance_micro_usd"),
            "new_balance_usd": result.get("balance_usd"),
            "idempotency_key": idem_key,
        })
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)
