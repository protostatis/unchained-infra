"""Callback admission policy for the MCP OAuth authorization server.

Remote connectors register their own callback URL, so the server has to decide
which destinations are legitimate.  A hostname check is not enough: an accepted
callback is where the authorization code lands, and a single permissive entry
turns the server into a code-delivery service for someone else's domain.

Two separate questions, deliberately answered in two places:

``validate_redirect_uri``
    Syntax and safety, applied to every registration, manual or automatic.
    Conservative: anything ambiguous is rejected rather than normalised.

``match_policy``
    Admission, applied only to automatic (DCR) registration.  A policy names an
    exact scheme, host, port and path — optionally a path prefix — and says
    whether a query string is allowed.  Manual registration is operated by a
    human who has decided to trust the destination, so it skips admission but
    never skips syntax validation.

Nothing here knows about storage; ``mcp_oauth_store`` persists policies and
``mcp_oauth_provider`` applies them.
"""

from __future__ import annotations

import re
from typing import Iterable, Mapping
from urllib.parse import urlsplit

MAX_REDIRECT_URI_LEN = 2048

# Static provider callbacks, seeded from code.  Each entry records the seed
# version it arrived in, so an existing database receives only the entries it
# has not seen: removing a policy stays removed across restarts, and a new
# provider can still be shipped without re-adding anything an operator dropped.
STATIC_POLICY_SET_VERSION = 2

DEFAULT_POLICIES: tuple[dict, ...] = (
    {
        "scheme": "https",
        "host": "claude.ai",
        "port": 443,
        "path": "/api/mcp/auth_callback",
        "match": "exact",
        "allow_query": 0,
        "note": "Claude custom connectors (Use Claude's published identity / own client)",
        "since": 1,
    },
    {
        "scheme": "https",
        "host": "vertexaisearch.cloud.google.com",
        "port": 443,
        "path": "/oauth-redirect",
        "match": "exact",
        "allow_query": 0,
        "note": "Gemini Enterprise custom MCP data store",
        "since": 1,
    },
    {
        "scheme": "https",
        "host": "chatgpt.com",
        "port": 443,
        "path": "/connector/oauth/",
        "match": "prefix",
        "allow_query": 0,
        "note": "ChatGPT custom MCP connector",
        "since": 2,
    },
    {
        "scheme": "https",
        "host": "agent.meta.ai",
        "port": 443,
        "path": "/api/hatch/oauth/callback",
        "match": "exact",
        "allow_query": 0,
        "note": "Meta Muse custom connector",
        "since": 2,
    },
)

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
# Encoded separators, dots and double-encoding: a browser decodes these before
# navigating, so a string compared literally here could land somewhere else.
_BAD_PERCENT = re.compile(r"%(?:2e|2f|25|5c|00|3f|23)", re.IGNORECASE)
_PUNYCODE = "xn--"


class RedirectURIError(ValueError):
    """A redirect URI that must not be stored or matched."""


def validate_redirect_uri(
    uri: str,
    *,
    issuer: str,
    allow_loopback: bool = False,
    allow_query: bool = True,
) -> str:
    """Return the URI unchanged if it is safe, else raise ``RedirectURIError``.

    ``allow_loopback`` is for native/desktop clients registered by an operator.
    Automatic registration never gets it: no cloud connector uses loopback.
    """
    raw = str(uri or "")
    if not raw.strip():
        raise RedirectURIError("redirect URI is empty")
    if raw != raw.strip():
        raise RedirectURIError("redirect URI must not contain leading or trailing whitespace")
    if len(raw) > MAX_REDIRECT_URI_LEN:
        raise RedirectURIError("redirect URI is too long")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
        raise RedirectURIError("redirect URI contains control characters")
    if "\\" in raw:
        raise RedirectURIError("redirect URI contains a backslash")
    if "#" in raw:
        raise RedirectURIError("redirect URI must not contain a fragment")

    try:
        parsed = urlsplit(raw)
    except ValueError:
        raise RedirectURIError("redirect URI is not a valid URL") from None

    if parsed.scheme not in ("https", "http"):
        raise RedirectURIError("redirect URI must use https")
    if parsed.username is not None or parsed.password is not None:
        raise RedirectURIError("redirect URI must not contain userinfo")
    if not parsed.hostname:
        raise RedirectURIError("redirect URI has no host")
    if not parsed.hostname.isascii():
        raise RedirectURIError("redirect URI host must be ASCII")
    host = parsed.hostname.lower()
    if _PUNYCODE in host:
        raise RedirectURIError("redirect URI host must not be punycode-encoded")
    if "%" in host:
        raise RedirectURIError("redirect URI host must not contain percent-encoding")
    if host.endswith("."):
        raise RedirectURIError("redirect URI host must not end with a dot")

    try:
        port = parsed.port
    except ValueError:
        raise RedirectURIError("redirect URI has an invalid port") from None
    if port is not None and not (1 <= port <= 65535):
        raise RedirectURIError("redirect URI has an invalid port")

    if parsed.path and not parsed.path.startswith("/"):
        raise RedirectURIError("redirect URI path must be absolute")
    if _BAD_PERCENT.search(parsed.path) or _BAD_PERCENT.search(parsed.query):
        raise RedirectURIError("redirect URI contains an encoded separator or control byte")
    if any(segment in (".", "..") for segment in parsed.path.split("/")):
        raise RedirectURIError("redirect URI path must not contain dot segments")
    if parsed.query and not allow_query:
        raise RedirectURIError("redirect URI must not contain a query string")

    if parsed.scheme == "http":
        if not (allow_loopback and host in _LOOPBACK_HOSTS):
            raise RedirectURIError("redirect URI must use https")

    if _is_issuer_host(host, issuer):
        raise RedirectURIError("redirect URI must not point at Unchained itself")

    return raw


def _is_issuer_host(host: str, issuer: str) -> bool:
    try:
        issuer_host = (urlsplit(issuer).hostname or "").lower()
    except ValueError:
        issuer_host = ""
    if not issuer_host:
        return False
    issuer_host = issuer_host.rstrip(".")
    return host == issuer_host or host.endswith("." + issuer_host)


def match_policy(uri: str, policy: Mapping) -> bool:
    """True when ``uri`` is admitted by one stored policy row.

    Assumes ``uri`` already passed :func:`validate_redirect_uri`; a malformed
    value simply fails the comparison.
    """
    try:
        parsed = urlsplit(str(uri))
        port = parsed.port
    except ValueError:
        return False
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower().rstrip(".")
    default_port = 443 if scheme == "https" else 80
    effective_port = port if port is not None else default_port
    policy_port = int(policy.get("port") or default_port)

    if scheme != str(policy.get("scheme") or "").lower():
        return False
    if host != str(policy.get("host") or "").lower().rstrip("."):
        return False
    if effective_port != policy_port:
        return False

    policy_path = str(policy.get("path") or "/")
    path = parsed.path or "/"
    if str(policy.get("match") or "exact") == "exact":
        if path != policy_path:
            return False
    else:
        if not path.startswith(policy_path):
            return False
        # A prefix must end on a path segment boundary. A policy path that
        # already ends in "/" is its own boundary.
        if (
            len(path) > len(policy_path)
            and policy_path != "/"
            and not policy_path.endswith("/")
            and path[len(policy_path)] != "/"
        ):
            return False

    if not int(policy.get("allow_query") or 0) and parsed.query:
        return False
    return True


def any_policy_matches(uri: str, policies: Iterable[Mapping]) -> bool:
    return any(match_policy(uri, policy) for policy in policies)


def all_policies_match(uris: Iterable[str], policies: Iterable[Mapping]) -> bool:
    policy_list = list(policies)
    uris = list(uris)
    if not uris:
        return False
    return all(any_policy_matches(uri, policy_list) for uri in uris)


def describe_policy(policy: Mapping) -> str:
    path = str(policy.get("path") or "/")
    suffix = "*" if str(policy.get("match") or "exact") == "prefix" else ""
    query = "?*" if int(policy.get("allow_query") or 0) else ""
    port = int(policy.get("port") or 443)
    port_part = "" if port == 443 else f":{port}"
    return f"{policy.get('scheme')}://{policy.get('host')}{port_part}{path}{suffix}{query}"


PERSONAL_CALLBACK_MARKER = "user_bound_custom-mcp-"
_BINDING_RE = re.compile(
    r"(" + re.escape(PERSONAL_CALLBACK_MARKER) + r"[A-Za-z0-9_-]+)"
)


def provider_for_binding(binding_id: str) -> str:
    """Provider a binding id belongs to."""
    return "google" if str(binding_id or "").startswith(PERSONAL_CALLBACK_MARKER) else "unknown"


def binding_id_from_uri(value: str) -> str | None:
    """The binding identifier embedded in a personal callback, if any."""
    match = _BINDING_RE.search(str(value or ""))
    return match.group(1) if match else None


def callback_binding(
    uri: str, policies: Iterable[Mapping], bindings: Mapping[str, Mapping]
) -> tuple[str, Mapping | None]:
    """Classify a callback against its policies and canonical binding.

    ``binding_id`` on the policy is authoritative; ``owner_user_id`` on the row
    is a denormalised cache.  A personal callback with no binding row is never
    treated as provider-wide, and overlapping bound/unbound policies fail
    closed rather than picking one.
    """
    matched = [p for p in policies if match_policy(uri, p)]
    if not matched:
        # Operator-registered (manual) clients bypass admission; a DCR client
        # without a matching policy never gets this far because get_client
        # reports it as blocked.  A personal-looking callback with no policy at
        # all still fails closed.
        return ("unbound_personal" if binding_id_from_uri(uri) else "static"), None
    bound = [p for p in matched if p.get("binding_id")]
    unbound = [p for p in matched if not p.get("binding_id")]
    personal = binding_id_from_uri(uri) is not None

    if bound:
        ids = {str(p["binding_id"]) for p in bound}
        if len(ids) != 1 or unbound:
            return "conflict", None
        binding = bindings.get(next(iter(ids)))
        if binding is None:
            return "missing", None
        if str(binding.get("status") or "") != "active":
            return "inactive", binding
        return "bound", binding

    if personal:
        return "unbound_personal", None
    return "static", None


def callback_owner_state(
    uri: str,
    policies: Iterable[Mapping],
    bindings: Mapping[str, Mapping],
    user_id: str,
) -> tuple[bool, str]:
    """Whether this account may authorize a callback, and why not when it may not."""
    state, binding = callback_binding(uri, policies, bindings)
    if state == "static":
        return True, "static"
    if state != "bound" or binding is None:
        return False, state
    owner = str(binding.get("owner_user_id") or "")
    if owner != str(user_id or ""):
        return False, "owner_mismatch"
    return True, "bound"


def client_callback_status(
    client: Mapping,
    policies: Iterable[Mapping],
    bindings: Mapping[str, Mapping] | None = None,
) -> str:
    """Classify a client's callbacks: ``manual``, ``policy`` or ``blocked``.

    Manual clients were registered by an operator, who is the approving party.
    Automatic clients stay admitted only while every stored callback still
    matches a current policy, so withdrawing a policy takes effect immediately.
    """
    if str(client.get("source") or "") == "manual":
        return "manual"
    uris = [str(u) for u in (client.get("redirect_uris") or [])]
    if not uris:
        return "blocked"
    for uri in uris:
        # DCR admission: every registered callback must match a policy first.
        if not any_policy_matches(uri, policies):
            return "blocked"
        state, _binding = callback_binding(uri, policies, bindings or {})
        if state not in ("static", "bound"):
            return "blocked"
    return "policy"
