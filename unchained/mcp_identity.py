"""Who is calling the MCP server, and which browser may they drive?

Two credential kinds reach ``/mcp``:

``api_key``
    The original ``Authorization: Bearer uc_...`` header.  The bearer key *is*
    the identity: ``agent_id`` is derived from its hash, exactly as the relay
    and local agent derive it, so a caller can address any of their own
    profiles.

``oauth``
    A connector token issued by :mod:`mcp_oauth_provider` after the user picked
    one browser on the consent screen.  The token carries no agent identity of
    its own; the *grant* does, and it is pinned.  A disconnected or renamed
    browser must fail loudly rather than silently retarget another one, so the
    pinned id is enforced on every tool call.

Keeping this policy in one module means the tool handlers never have to reason
about which credential type they got.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from mcp_oauth_store import ACCESS_TOKEN_PREFIX, agent_id_for_key

KIND_API_KEY = "api_key"
KIND_OAUTH = "oauth"


class IdentityError(ValueError):
    """Raised when a caller is missing, invalid, or asking for a target it may not use."""


@dataclass(frozen=True)
class Principal:
    kind: str
    user_id: str
    caller_tag: str
    agent_id: str | None = None       # pinned target for OAuth grants
    grant_id: str | None = None
    scopes: tuple[str, ...] = field(default_factory=tuple)
    api_key: str | None = None        # only for KIND_API_KEY

    @property
    def is_oauth(self) -> bool:
        return self.kind == KIND_OAUTH

    def allows(self, scope: str) -> bool:
        return not self.scopes or scope in self.scopes


def resolve_principal(
    authorization_header: str,
    *,
    auth,
    store,
) -> Principal | None:
    """Resolve the ``Authorization`` header into a principal.

    Returns ``None`` when the credential is absent or invalid; callers turn
    that into a 401-style error message.  Never raises for bad credentials so
    the tool layer can keep its existing error text.
    """
    header = (authorization_header or "").strip()
    if not header.lower().startswith("bearer "):
        return None
    token = header[7:].strip()
    if not token:
        return None

    if token.startswith(ACCESS_TOKEN_PREFIX):
        if store is None:
            return None
        row = store.load_access_token(token)
        if row is None:
            return None
        # A grant is only as valid as the API key it was pinned to.  Checking
        # here (not just at refresh time) means revoking a key cuts connector
        # access immediately instead of waiting out the access-token TTL.
        if not _grant_key_active(auth, row["user_id"], row["key_fingerprint"]):
            return None
        # Cheap constant-time-ish tag so provisioning cleanup can never collide
        # with an API-key caller's tag.
        tag = "oauth-" + hashlib.sha256(row["grant_id"].encode()).hexdigest()[:8]
        return Principal(
            kind=KIND_OAUTH,
            user_id=row["user_id"],
            caller_tag=tag,
            agent_id=row["agent_id"],
            grant_id=row["grant_id"],
            scopes=tuple(row["scopes"]),
        )

    info = auth.validate_key(token)
    if info is None:
        return None
    return Principal(
        kind=KIND_API_KEY,
        user_id=info["user_id"],
        caller_tag=hashlib.sha256(token.encode()).hexdigest()[:12],
        api_key=token,
    )


def _grant_key_active(auth, user_id: str, key_fingerprint: str) -> bool:
    if not key_fingerprint:
        return False
    try:
        keys = auth.get_keys_for_user(user_id)
    except Exception:
        return False
    for key in keys:
        if hashlib.sha256(key.encode()).hexdigest() == key_fingerprint:
            return True
    return False


# Provisioned Chromes are temporary browsers launched with an arbitrary local
# profile. A connector approved for one browser must not reach them.
PROVISION_ONLY_TOOLS = frozenset(
    {
        "cdp_provision_launch",
        "cdp_provision_cleanup",
        "list_provisioned_tabs",
    }
)

OAUTH_SCOPE_DENIED = (
    "OAuth connectors can only drive the browser granted at consent. "
    "Provisioned Chrome instances require an API-key MCP client."
)


def oauth_tool_target_error(
    principal: Principal | None, tool_name: str, arguments: dict | None
) -> str | None:
    """Return a denial message when an OAuth caller reaches outside its grant.

    Applied to every tool call so a newly added tool that accepts ``tab_id`` is
    covered without another call-site check.
    """
    if principal is None or not principal.is_oauth:
        return None
    if tool_name in PROVISION_ONLY_TOOLS:
        return OAUTH_SCOPE_DENIED
    tab_id = str((arguments or {}).get("tab_id") or "")
    if tab_id.startswith("prov-"):
        return OAUTH_SCOPE_DENIED
    return None


def target_agent(principal: Principal, requested: str, *, auth=None) -> str:
    """Resolve the ``agent_id`` tool argument for this principal.

    API-key callers keep the historical behaviour (profile name, full agent id
    owned by the user, or empty for their default agent).  OAuth callers may
    only ever reach the agent pinned at consent time.
    """
    requested = (requested or "").strip()

    if not principal.is_oauth:
        if not requested:
            return agent_id_for_key(principal.api_key or "")
        if is_full_agent_id(requested):
            if auth is not None and _owned_by_user(auth, principal.user_id, requested):
                return requested
            raise IdentityError(f"Browser {agent_label(requested)} does not belong to you.")
        if not re.match(r"^[a-zA-Z0-9_-]{1,32}$", requested):
            raise IdentityError(f"Invalid browser name: {requested!r}")
        return agent_id_for_key(principal.api_key or "", requested)

    pinned = principal.agent_id or ""
    if not pinned:
        raise IdentityError("This connector has no pinned browser; reconnect it.")
    if not requested or requested in ("auto", pinned):
        return pinned
    # A bare profile name is accepted only when it names the pinned browser;
    # another profile of the same key is a different browser and must not be
    # reachable without a fresh consent.
    if requested == agent_profile(pinned):
        return pinned
    wanted = agent_label(requested) if is_full_agent_id(requested) else requested
    raise IdentityError(
        f"This connector is limited to {agent_label(pinned)}; "
        f"reconnect it to use {wanted}."
    )


# ``<lane>-<8 hex>[-<profile>]``.  The lane is an implementation detail (it
# distinguishes a user's local agent from a hosted worker, not a model vendor),
# so this recognises the shape rather than hardcoding today's names.
_FULL_AGENT_ID = re.compile(r"^[a-z][a-z0-9_-]*-[0-9a-f]{8}(?:-[A-Za-z0-9_-]{1,32})?$")


def is_full_agent_id(value: str) -> bool:
    return bool(_FULL_AGENT_ID.match(value or ""))


def agent_profile(agent_id: str) -> str:
    """Profile suffix of an agent id (``claude-abc12345-work`` -> ``work``)."""
    parts = agent_id.split("-", 2)
    return parts[2] if len(parts) > 2 else ""


def agent_label(agent_id: str) -> str:
    """Human-facing name for an agent id, without leaking the internal prefix."""
    if not agent_id:
        return ""
    if agent_id.startswith("headless-"):
        return "Unchained hosted browser"
    profile = agent_profile(agent_id)
    return profile or "Default browser"


def _owned_by_user(auth, user_id: str, agent_id: str) -> bool:
    """Original ownership rule: any active key of this user may address it."""
    try:
        keys = auth.get_keys_for_user(user_id)
    except Exception:
        return False
    for key in keys:
        key_hash = hashlib.sha256(key.encode()).hexdigest()[:8]
        if agent_id.startswith(f"claude-{key_hash}") or agent_id.startswith(f"headless-{key_hash}"):
            return True
    return False
