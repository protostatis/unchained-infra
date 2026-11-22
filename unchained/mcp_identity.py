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
        state = grant_state(auth, store, row["grant_id"])
        if state != GRANT_VALID:
            revoke_if_detached(store, row["grant_id"], state)
            return None
        # Cheap constant-time-ish tag so provisioning cleanup can never collide
        # with an API-key caller's tag. 64 bits: this tag is the ownership
        # authority for provisioned browsers.
        tag = "oauth-" + hashlib.sha256(row["grant_id"].encode()).hexdigest()[:16]
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


GRANT_VALID = "valid"
GRANT_GONE = "gone"
GRANT_ACCOUNT = "account"
GRANT_KEY = "key"
GRANT_UNKNOWN = "unknown"


def user_is_eligible(auth, user_id: str) -> bool:
    """Whether an account may still act through a delegated connector.

    OAuth delegation requires an approved account.  A key-only identity (no
    ``users`` row) predates accounts and is preserved.  API-key callers are
    checked separately so this cannot silently change trial access.
    """
    try:
        user = auth.find_user_by_id(user_id)
    except Exception:
        return False
    if user is None:
        return True
    return (user.get("status") or "approved") == "approved"


def grant_state(auth, store, grant_id: str | None) -> str:
    """Classify a grant without side effects.

    ``GRANT_UNKNOWN`` exists so a transient database failure cannot be mistaken
    for a revoked account and trigger a destructive cleanup.
    """
    if not grant_id:
        return GRANT_GONE
    try:
        grant = store.get_grant(grant_id)
        if grant is None:
            return GRANT_GONE
        user = auth.find_user_by_id(grant["user_id"])
        if user is not None and (user.get("status") or "approved") != "approved":
            return GRANT_ACCOUNT
        keys = auth.get_keys_for_user(grant["user_id"])
        for key in keys:
            if hashlib.sha256(key.encode()).hexdigest() == grant["key_fingerprint"]:
                return GRANT_VALID
        return GRANT_KEY
    except Exception:
        return GRANT_UNKNOWN


def revoke_if_detached(store, grant_id: str | None, state: str) -> None:
    """Drop a grant once its account or key is confirmed to be gone."""
    if state in (GRANT_ACCOUNT, GRANT_KEY):
        try:
            store.revoke_grant(grant_id)
        except Exception:
            pass


# Provisioned Chromes are temporary browsers launched with an arbitrary local
# profile. A connector may launch its own, but it must never drive or enumerate
# another caller's slots. Ownership travels as the caller tag the bridge stored
# at launch time; provisioning tools pass ``principal.caller_tag`` and this
# module decides whether a ``prov-`` tab belongs to the grant.
OAUTH_PROV_TAB_DENIED = (
    "That provisioned browser belongs to another client. "
    "Launch your own with cdp_provision_launch."
)

OAUTH_PROV_TAB_UNVERIFIED = (
    "Could not verify that the provisioned browser belongs to this connector. "
    "Retry in a moment."
)

OAUTH_CONCRETE_TARGET_REQUIRED = (
    "OAuth connectors must target 'auto', a tab id, or one of their own "
    "provisioned tab ids. Tab aliases are not available to connectors."
)

# New consent grants this scope. Grants approved before profile access existed
# do not have it, so they must reconnect instead of silently gaining the
# ability to copy local Chrome profiles.
PROVISION_SCOPE = "mcp:provision"

PROVISION_RECONSENT_REQUIRED = (
    "This connector was approved before Chrome profile access existed. "
    "Reconnect it to review and approve the new permission."
)


def oauth_can_provision(principal: Principal | None) -> bool:
    """Return True when this caller may use the provisioning tools.

    API keys keep their existing access. OAuth grants need the scope added by
    the updated consent screen, which existing grants do not carry.
    """
    if principal is None or not principal.is_oauth:
        return True
    return principal.allows(PROVISION_SCOPE)


_PROV_TAB_RE = re.compile(r"^prov-([A-Za-z0-9]+)-([A-Za-z0-9]+)$")


def provision_slot_from_tab_id(tab_id: str) -> str:
    """Return the slot portion of a canonical ``prov-{slot}-{id}`` id, or "".

    Legacy ids such as ``prov-auto`` and ``prov-{id}`` do not name a slot.
    Control characters are rejected outright: downstream URL parsing
    normalizes them, which could turn a checked string into another target.
    """
    match = _PROV_TAB_RE.match(str(tab_id or ""))
    return match.group(1) if match else ""


def oauth_prov_tab_allowed(
    principal: Principal | None,
    tab_id: str,
    slots: dict | None,
) -> bool | None:
    """Decide whether an OAuth caller may drive ``tab_id``.

    ``slots`` is the ``provision_status`` payload for the browser pinned to the
    grant. Returns True when the caller is not OAuth (API keys keep their
    existing behaviour), False when the slot belongs to someone else, and None
    when the status payload cannot answer the question.
    """
    if principal is None or not principal.is_oauth:
        return True
    if not str(tab_id or "").startswith("prov-"):
        return True
    slot = provision_slot_from_tab_id(tab_id)
    if not slot:
        # Legacy ``prov-{id}`` and ``prov-auto`` reach the sole provisioned
        # browser without naming a slot, so ownership cannot be established.
        return False
    if not isinstance(slots, dict):
        return None
    info = slots.get(slot)
    if not isinstance(info, dict):
        return False
    return info.get("caller_tag") == principal.caller_tag


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
