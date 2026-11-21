"""OAuth 2.1 authorization server for the Unchained MCP endpoint.

Remote MCP connectors (Claude, ChatGPT, Gemini Apps, Gemini Enterprise) only
speak OAuth: they discover an authorization server, register a client, send the
user through a consent screen, and exchange a PKCE-protected authorization code
for tokens.  They cannot send a static ``Authorization: Bearer <api_key>``
header, so this provider makes ``https://unchainedsky.com/mcp`` reachable from
those products without changing how existing API-key clients authenticate.

The provider plugs into FastMCP's ``OAuthProvider``, which mounts
``/authorize``, ``/token``, ``/register``, ``/revoke`` and the
``/.well-known/oauth-*`` discovery documents.  Protocol mechanics stay in the
MCP SDK; this module owns policy and storage:

* only public clients (``token_endpoint_auth_method=none``) with mandatory
  PKCE S256, so there is no client secret to leak or hash-compare;
* registration is bounded and never authorizes anyone by itself;
* the consent screen lives on the website (it owns the login session), and the
  browser comes back through ``/oauth/complete`` with a single-use ticket;
* every grant is pinned to one exact agent id at consent time;
* access and refresh tokens are opaque and stored as hashes.
"""

from __future__ import annotations

import os
import time
from typing import Any
from urllib.parse import urlencode

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    RegistrationError,
    TokenError,
)
from pydantic import AnyUrl
from mcp.shared.auth import OAuthClientInformationFull

from fastmcp.server.auth import OAuthProvider

from mcp_oauth_store import (
    ACCESS_TOKEN_PREFIX,
    OAuthStore,
    fingerprint_key,
)

SUPPORTED_SCOPE = "mcp:browser"
DEFAULT_SCOPES = [SUPPORTED_SCOPE]

# Dynamic registration is a public endpoint.  Bound it so a script cannot fill
# the database, while leaving room for real users behind shared NAT.
DCR_MAX_PER_HOUR = int(os.environ.get("MCP_OAUTH_DCR_MAX_PER_HOUR", "60"))
DCR_MAX_TOTAL = int(os.environ.get("MCP_OAUTH_DCR_MAX_TOTAL", "5000"))


class _StoredCode(AuthorizationCode):
    """Authorization code plus the raw value needed to consume it."""

    raw_code: str


class MCPOAuthProvider(OAuthProvider):
    """Authorization server backed by the Unchained SQLite database."""

    def __init__(
        self,
        store: OAuthStore,
        *,
        issuer: str,
        resource: str,
        dcr_enabled: bool = False,
        allowed_redirect_hosts: list[str] | None = None,
    ):
        self.store = store
        self.resource = resource
        self.issuer = issuer.rstrip("/")
        self.consent_url = f"{self.issuer}/oauth/consent"
        self.complete_url = f"{self.issuer}/oauth/complete"
        self.dcr_enabled = bool(dcr_enabled)
        self.allowed_redirect_hosts = {h.lower() for h in (allowed_redirect_hosts or [])}

        from mcp.server.auth.settings import (
            ClientRegistrationOptions,
            RevocationOptions,
        )

        super().__init__(
            base_url=self.issuer,
            issuer_url=self.issuer,
            client_registration_options=ClientRegistrationOptions(
                enabled=self.dcr_enabled,
                valid_scopes=DEFAULT_SCOPES,
                default_scopes=DEFAULT_SCOPES,
            ),
            revocation_options=RevocationOptions(enabled=True),
            required_scopes=DEFAULT_SCOPES,
        )

    # -- client registration ----------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        row = self.store.get_client(client_id)
        if row is None:
            return None
        return OAuthClientInformationFull(
            client_id=row["client_id"],
            client_name=row["client_name"],
            redirect_uris=[AnyUrl(u) for u in row["redirect_uris"]],
            grant_types=row["grant_types"] or ["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
            scope=" ".join(DEFAULT_SCOPES),
        )

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not self.dcr_enabled:
            raise RegistrationError(
                error="invalid_client_metadata",
                error_description="dynamic client registration is disabled",
            )

        redirect_uris = [str(u) for u in (client_info.redirect_uris or [])]
        if not redirect_uris or len(redirect_uris) > 10:
            raise RegistrationError(
                error="invalid_redirect_uri",
                error_description="between 1 and 10 redirect URIs are required",
            )
        for uri in redirect_uris:
            if not _redirect_uri_allowed(uri, self.allowed_redirect_hosts):
                raise RegistrationError(
                    error="invalid_redirect_uri",
                    error_description=f"redirect URI not allowed: {uri}",
                )

        # Public clients only: a secret we never issue cannot be leaked.
        method = client_info.token_endpoint_auth_method or "none"
        if method != "none":
            raise RegistrationError(
                error="invalid_client_metadata",
                error_description="only public clients (token_endpoint_auth_method=none) are supported",
            )

        if self.store.count_clients(since=time.time() - 3600) >= DCR_MAX_PER_HOUR:
            raise RegistrationError(
                error="invalid_client_metadata",
                error_description="registration rate limit reached, try again later",
            )
        if self.store.count_clients() >= DCR_MAX_TOTAL:
            raise RegistrationError(
                error="invalid_client_metadata",
                error_description="registration capacity reached",
            )

        self.store.save_client(
            client_id=client_info.client_id,
            client_name=(client_info.client_name or "Unnamed MCP client")[:120],
            redirect_uris=redirect_uris,
            grant_types=["authorization_code", "refresh_token"],
            token_endpoint_auth_method="none",
            source="dcr",
        )

    # -- authorization -----------------------------------------------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        redirect_uri = str(params.redirect_uri)

        # RFC 8707: the token must be for this resource, and nothing else.
        resource = params.resource
        if resource and resource.rstrip("/") != self.resource.rstrip("/"):
            return _error_redirect(redirect_uri, "invalid_target", params.state)

        code_challenge = (params.code_challenge or "").strip()
        if not code_challenge or len(code_challenge) < 43:
            return _error_redirect(redirect_uri, "invalid_request", params.state)

        scopes = [s for s in (params.scopes or []) if s in DEFAULT_SCOPES] or list(DEFAULT_SCOPES)
        if not set(scopes).issubset(set(DEFAULT_SCOPES)):
            return _error_redirect(redirect_uri, "invalid_scope", params.state)

        request_id = self.store.create_request(
            client_id=client.client_id,
            redirect_uri=redirect_uri,
            state=params.state,
            scopes=scopes,
            code_challenge=code_challenge,
            code_challenge_method="S256",
            resource=self.resource,
        )
        return f"{self.consent_url}?{urlencode({'request': request_id})}"

    # -- authorization code ------------------------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        row = self.store.get_authorization_code(authorization_code)
        if row is None or row["client_id"] != client.client_id:
            return None
        return _StoredCode(
            raw_code=authorization_code,
            code=authorization_code,
            scopes=row["scopes"],
            expires_at=int(row["expires_at"]),
            client_id=row["client_id"],
            code_challenge=row["code_challenge"],
            redirect_uri=AnyUrl(row["redirect_uri"]),
            redirect_uri_provided_explicitly=True,
            resource=row["resource"],
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> Any:
        raw = getattr(authorization_code, "raw_code", authorization_code.code)
        consumed = self.store.consume_authorization_code(raw, client_id=client.client_id)
        if consumed is None:
            raise TokenError(
                error="invalid_grant",
                error_description="authorization code is invalid or already used",
            )
        # The pinned key must still exist; a revoked key invalidates the grant.
        if not self._grant_key_still_active(consumed.get("grant_id")):
            raise TokenError(
                error="invalid_grant",
                error_description="the linked API key was revoked",
            )
        issued = self.store.issue_tokens(
            grant_id=consumed["grant_id"],
            client_id=client.client_id,
            scopes=consumed["scopes"],
            resource=consumed["resource"],
        )
        return _token_response(issued, consumed["scopes"])

    # -- refresh -----------------------------------------------------------

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        row = self.store.load_refresh_token(refresh_token)
        if row is None or row["client_id"] != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=row["client_id"],
            scopes=row["scopes"],
            expires_at=int(row["expires_at"]),
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> Any:
        rotated = self.store.consume_refresh_token(
            refresh_token.token, client_id=client.client_id
        )
        if rotated is None:
            raise TokenError(
                error="invalid_grant",
                error_description="refresh token is invalid or expired",
            )
        if not self._grant_key_still_active(rotated["grant_id"]):
            raise TokenError(
                error="invalid_grant",
                error_description="the linked API key was revoked",
            )
        granted = [s for s in scopes if s in rotated["scopes"]] or rotated["scopes"]
        issued = self.store.issue_tokens(
            grant_id=rotated["grant_id"],
            client_id=client.client_id,
            scopes=granted,
            resource=rotated["resource"],
            family_id=rotated["family_id"],
        )
        return _token_response(issued, granted)

    # -- verification ------------------------------------------------------

    async def load_access_token(self, token: str) -> AccessToken | None:
        if token.startswith(ACCESS_TOKEN_PREFIX):
            row = self.store.load_access_token(token)
            if row is None:
                return None
            if row["resource"] and row["resource"].rstrip("/") != self.resource.rstrip("/"):
                return None
            return AccessToken(
                token=token,
                client_id=row["client_id"],
                scopes=row["scopes"],
                expires_at=int(row["expires_at"]),
                resource=row["resource"],
            )

        # Legacy API keys keep working through the same middleware.  Their
        # agent binding is resolved per tool call, not here.
        info = _auth().validate_key(token)
        if info is None:
            return None
        return AccessToken(
            token=token,
            client_id="api-key",
            scopes=list(DEFAULT_SCOPES),
            expires_at=None,
            resource=self.resource,
        )

    async def revoke_token(self, token: Any) -> None:
        raw = getattr(token, "token", None)
        if raw and isinstance(raw, str):
            self.store.revoke_token(raw)

    # -- helpers -----------------------------------------------------------

    def _grant_key_still_active(self, grant_id: str | None) -> bool:
        """True when the grant's backing API key still exists and is active."""
        if not grant_id:
            return False
        grant = self.store.get_grant(grant_id)
        if grant is None:
            return False
        auth = _auth()
        try:
            keys = auth.get_keys_for_user(grant["user_id"])
        except Exception:
            return False
        return any(fingerprint_key(k) == grant["key_fingerprint"] for k in keys)


_AUTH: list[Any] = []


def _auth() -> Any:
    """Cached ``Auth`` instance; constructing it re-runs schema setup."""
    if not _AUTH:
        from auth import Auth

        _AUTH.append(Auth())
    return _AUTH[0]


def _token_response(issued: dict, scopes: list[str]) -> Any:
    from mcp.shared.auth import OAuthToken

    return OAuthToken(
        access_token=issued["access_token"],
        token_type="Bearer",
        expires_in=int(issued.get("expires_in") or 900),
        scope=" ".join(scopes),
        refresh_token=issued.get("refresh_token"),
    )


def _error_redirect(redirect_uri: str, error: str, state: str | None) -> str:
    params = {"error": error}
    if state:
        params["state"] = state
    sep = "&" if "?" in redirect_uri else "?"
    return f"{redirect_uri}{sep}{urlencode(params)}"


def _redirect_uri_allowed(uri: str, extra_hosts: set[str]) -> bool:
    from urllib.parse import urlparse

    try:
        parsed = urlparse(uri)
    except ValueError:
        return False
    if not parsed.scheme or parsed.fragment:
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    if parsed.scheme == "https":
        return True
    # Native/desktop clients use loopback redirects (RFC 8252).
    if parsed.scheme == "http" and host in ("127.0.0.1", "localhost", "::1"):
        return True
    if extra_hosts and host in extra_hosts:
        return True
    return False


# Module-level handle so helpers can reach the store without threading it
# through every SDK callback.  Set by ``build_provider``.
_ACTIVE_STORE: list[OAuthStore] = []


def build_provider(store: OAuthStore) -> MCPOAuthProvider | None:
    """Build the provider from the environment, or ``None`` when disabled."""
    if os.environ.get("MCP_OAUTH_ENABLED", "").strip().lower() not in ("1", "true", "yes", "on"):
        return None
    issuer = os.environ.get("MCP_OAUTH_ISSUER", "https://unchainedsky.com").strip().rstrip("/")
    resource = os.environ.get(
        "MCP_OAUTH_RESOURCE", f"{issuer}/mcp"
    ).strip().rstrip("/")
    dcr = os.environ.get("MCP_OAUTH_DCR_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")
    hosts = [
        h.strip().lower()
        for h in os.environ.get("MCP_OAUTH_EXTRA_REDIRECT_HOSTS", "").split(",")
        if h.strip()
    ]
    _ACTIVE_STORE.clear()
    _ACTIVE_STORE.append(store)
    return MCPOAuthProvider(
        store,
        issuer=issuer,
        resource=resource,
        dcr_enabled=dcr,
        allowed_redirect_hosts=hosts,
    )


def install_complete_route(mcp, store: OAuthStore) -> None:
    """Register ``GET /oauth/complete`` on a FastMCP server.

    The browser lands here after approving on the website.  The ticket is a
    single-use capability minted by the consent POST; consuming it here is what
    turns an approval into an authorization code, and the redirect target is
    always the URI that was validated at ``/authorize`` time.
    """

    @mcp.custom_route("/oauth/complete", methods=["GET"])
    async def oauth_complete(request):  # pragma: no cover - exercised over HTTP
        from starlette.responses import PlainTextResponse, RedirectResponse
        from urllib.parse import urlencode

        ticket = (request.query_params.get("ticket") or "").strip()
        if not ticket:
            return PlainTextResponse("Missing approval ticket.", status_code=400)
        try:
            payload = store.complete_request(ticket)
        except ValueError as exc:
            return PlainTextResponse(str(exc), status_code=400)

        params = {"code": payload["code"]}
        if payload.get("state"):
            params["state"] = payload["state"]
        sep = "&" if "?" in payload["redirect_uri"] else "?"
        response = RedirectResponse(
            f"{payload['redirect_uri']}{sep}{urlencode(params)}", status_code=302
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response
