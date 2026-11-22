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

import logging
import os
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

from mcp_identity import GRANT_VALID, grant_state, revoke_if_detached
from mcp_oauth_store import (
    ACCESS_TOKEN_PREFIX,
    BudgetExceeded,
    OAuthStore,
)
from mcp_oauth_policy import (
    RedirectURIError,
    client_callback_status,
    match_policy,
    validate_redirect_uri,
)

logger = logging.getLogger("unchained.mcp_oauth")

SUPPORTED_SCOPE = "mcp:browser"
DEFAULT_SCOPES = [SUPPORTED_SCOPE]

# Dynamic registration is a public endpoint.  Bound it so a script cannot fill
# the database, while leaving room for real users behind shared NAT.
DCR_MAX_PER_HOUR = int(os.environ.get("MCP_OAUTH_DCR_MAX_PER_HOUR", "60"))
DCR_MAX_TOTAL = int(os.environ.get("MCP_OAUTH_DCR_MAX_TOTAL", "5000"))

# Authorization requests are allocated without a credential, so they need their
# own ceiling.  Both are counts of outstanding (pending, unexpired) rows.
AUTHORIZE_MAX_PENDING_PER_CLIENT = int(
    os.environ.get("MCP_OAUTH_MAX_PENDING_PER_CLIENT", "20")
)
AUTHORIZE_MAX_PENDING_GLOBAL = int(
    os.environ.get("MCP_OAUTH_MAX_PENDING_GLOBAL", "5000")
)


class _StoredCode(AuthorizationCode):
    """Authorization code plus the raw value needed to consume it."""

    raw_code: str


class _ReplayedRefreshToken(RefreshToken):
    """A refresh token that was already rotated.

    Returning it (rather than ``None``) lets the exchange step revoke the
    family; answering invalid_grant at load time would skip that.
    """

    family_id: str


class MCPOAuthProvider(OAuthProvider):
    """Authorization server backed by the Unchained SQLite database."""

    def __init__(
        self,
        store: OAuthStore,
        *,
        issuer: str,
        resource: str,
        dcr_enabled: bool = False,
    ):
        self.store = store
        self.resource = resource
        self.issuer = issuer.rstrip("/")
        self.consent_url = f"{self.issuer}/oauth/consent"
        self.complete_url = f"{self.issuer}/oauth/complete"
        self.dcr_enabled = bool(dcr_enabled)

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
        # A client whose callback is no longer admitted must look nonexistent,
        # otherwise the SDK can still redirect a code to it before authorize()
        # is ever called.
        if client_callback_status(row, self.store.list_policies()) == "blocked":
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

        policies = self.store.list_policies()
        for uri in redirect_uris:
            try:
                validate_redirect_uri(uri, issuer=self.issuer, allow_loopback=False)
            except RedirectURIError as exc:
                logger.warning(
                    "registration rejected: invalid redirect %r: %s", uri, exc
                )
                raise RegistrationError(
                    error="invalid_redirect_uri",
                    error_description=f"{exc}: {uri}",
                ) from None
            if not any(match_policy(uri, policy) for policy in policies):
                logger.warning(
                    "registration rejected: no policy for %r (client %r)",
                    uri,
                    client_info.client_name,
                )
                raise RegistrationError(
                    error="invalid_redirect_uri",
                    error_description=(
                        "this callback address is not approved for automatic "
                        "registration; ask the operator to add it, or use a "
                        "manually registered client"
                    ),
                )

        # Every Unchained client is public: PKCE binds the exchange and no
        # secret is ever checked.  Consoles still register with a confidential
        # method because the MCP SDK advertises client_secret_post/basic in
        # discovery, so accept those spellings and store the client as public.
        # The secret the console believes it has is ignored by get_client().
        method = client_info.token_endpoint_auth_method or "none"
        if method not in ("none", "client_secret_post", "client_secret_basic"):
            logger.warning("registration rejected: auth method %r", method)
            raise RegistrationError(
                error="invalid_client_metadata",
                error_description=f"unsupported token_endpoint_auth_method: {method}",
            )
        # The register handler echoes this object in its 201 response, so clear
        # the method and any generated secret here: a console must not walk away
        # believing it holds a secret that get_client() will never return.
        client_info.token_endpoint_auth_method = "none"
        client_info.client_secret = None
        client_info.client_secret_expires_at = None

        logger.info(
            "registration accepted: %s method=%s", redirect_uris, method
        )
        try:
            self.store.register_dynamic_client(
                client_id=client_info.client_id,
                client_name=(client_info.client_name or "Unnamed MCP client")[:120],
                redirect_uris=redirect_uris,
                max_per_hour=DCR_MAX_PER_HOUR,
                max_total=DCR_MAX_TOTAL,
            )
        except BudgetExceeded as exc:
            raise RegistrationError(
                error="invalid_client_metadata",
                error_description=str(exc),
            ) from None

    # -- authorization -----------------------------------------------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        redirect_uri = str(params.redirect_uri)

        # The SDK validates the callback against the client record before it
        # gets here. Re-check admission so a policy withdrawn while a request
        # was in flight cannot still receive a code. The failure target is our
        # own page: never redirect to a callback we no longer trust.
        row = self.store.get_client(client.client_id)
        if row is None or client_callback_status(row, self.store.list_policies()) == "blocked":
            return f"{self.issuer}/oauth/error?reason=client_not_approved"

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

        try:
            request_id = self.store.create_request(
                client_id=client.client_id,
                redirect_uri=redirect_uri,
                state=params.state,
                scopes=scopes,
                code_challenge=code_challenge,
                code_challenge_method="S256",
                resource=self.resource,
                max_per_client=AUTHORIZE_MAX_PENDING_PER_CLIENT,
                max_global=AUTHORIZE_MAX_PENDING_GLOBAL,
            )
        except BudgetExceeded:
            return _error_redirect(redirect_uri, "temporarily_unavailable", params.state)
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
        # The grant is only as valid as its account and its backing key.
        state = grant_state(_auth(), self.store, consumed.get("grant_id"))
        if state != GRANT_VALID:
            revoke_if_detached(self.store, consumed.get("grant_id"), state)
            raise TokenError(
                error="invalid_grant",
                error_description="the linked account or API key is no longer active",
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
        row = self.store.load_refresh_token(
            refresh_token, client_id=client.client_id
        )
        if row is None:
            return None
        if row["consumed"]:
            return _ReplayedRefreshToken(
                token=refresh_token,
                client_id=row["client_id"],
                scopes=row["scopes"],
                expires_at=int(row["expires_at"]),
                family_id=row["family_id"],
            )
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
        if isinstance(refresh_token, _ReplayedRefreshToken):
            # Same client presenting a spent token: treat the family as
            # compromised. No grace window; forked successors are worse.
            self.store.revoke_family(refresh_token.family_id)
            raise TokenError(
                error="invalid_grant",
                error_description="refresh token was already used; reconnect the app",
            )

        rotated = self.store.rotate_refresh_token(
            refresh_token.token,
            client_id=client.client_id,
            scopes=scopes or refresh_token.scopes,
        )
        if rotated is None or rotated.get("replay"):
            raise TokenError(
                error="invalid_grant",
                error_description="refresh token is invalid or expired",
            )
        granted = rotated.get("scopes") or scopes or refresh_token.scopes
        state = grant_state(_auth(), self.store, rotated["grant_id"])
        if state != GRANT_VALID:
            revoke_if_detached(self.store, rotated["grant_id"], state)
            raise TokenError(
                error="invalid_grant",
                error_description="the linked account or API key is no longer active",
            )
        return _token_response(rotated, granted)

    # -- verification ------------------------------------------------------

    async def load_access_token(self, token: str) -> AccessToken | None:
        if token.startswith(ACCESS_TOKEN_PREFIX):
            row = self.store.load_access_token(token)
            if row is None:
                return None
            # Transport-level check must match the tool-level one: a revoked
            # account or key has to fail here too, not only on tool dispatch,
            # and a confirmed detachment revokes the grant rather than merely
            # refusing this request.
            state = grant_state(_auth(), self.store, row["grant_id"])
            if state != GRANT_VALID:
                revoke_if_detached(self.store, row["grant_id"], state)
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

    # Grant eligibility lives in mcp_identity.grant_state, shared with the
    # tool layer so transport and dispatch cannot disagree.



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
    _ACTIVE_STORE.clear()
    _ACTIVE_STORE.append(store)
    return MCPOAuthProvider(
        store,
        issuer=issuer,
        resource=resource,
        dcr_enabled=dcr,
    )


def install_complete_route(mcp, provider: "MCPOAuthProvider") -> None:
    """Register ``GET /oauth/complete`` on a FastMCP server.

    The browser lands here after approving on the website.  The ticket is a
    single-use capability minted by the consent POST; consuming it here is what
    turns an approval into an authorization code, and the redirect target is
    always the URI that was validated at ``/authorize`` time.

    Completion re-checks the client because a policy can be withdrawn between
    approval and this redirect; a code must never reach a callback that is no
    longer admitted.
    """

    @mcp.custom_route("/oauth/complete", methods=["GET"])
    async def oauth_complete(request):  # pragma: no cover - exercised over HTTP
        from starlette.responses import PlainTextResponse, RedirectResponse
        from urllib.parse import urlencode

        ticket = (request.query_params.get("ticket") or "").strip()
        if not ticket:
            return PlainTextResponse("Missing approval ticket.", status_code=400)
        approval = provider.store.peek_approval(ticket)
        if approval is None:
            return PlainTextResponse("This approval is invalid or expired.", status_code=400)
        pending = provider.store.get_request(approval["request_id"])
        client = provider.store.get_client(pending["client_id"]) if pending else None
        if client is None or client_callback_status(
            client, provider.store.list_policies()
        ) == "blocked":
            return PlainTextResponse(
                "This app's callback address is no longer approved.", status_code=403
            )
        # The request was authorized against a callback on record at that time;
        # if the registration has since changed, do not send the code there.
        if pending is not None and pending["redirect_uri"] not in client["redirect_uris"]:
            return PlainTextResponse(
                "This app's callback address changed; reconnect it.", status_code=403
            )
        try:
            payload = provider.store.complete_request(ticket)
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
