"""signup_agent.py — Script-driven CDP automation to provision AI API keys.

Automates the user's browser (via relay tunnel) to create API keys from
AI providers. No LLM involved — pure if/else logic with JS selectors.

Architecture:
    web.py POST /web/provision/start
      → signup_agent.provision_key("gemini", agent_id, user_id, ...)
        → cloud_tools.create_tab("about:blank")
        → cloud_tools.navigate("aistudio.google.com/apikey")
        → cloud_tools.run_js(...) to detect state, find buttons, extract key
        → store encrypted key in provider_keys table
        → cloud_tools close tab

Usage (testing):
    curl -X POST localhost:8080/web/provision/start \\
        -H "Authorization: Bearer <uc_key>" \\
        -d '{"provider":"gemini"}'
"""

import asyncio
import base64
import functools
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum

import httpx

import cloud_tools
from auth import Auth


# ---------------------------------------------------------------------------
# Status / Result types
# ---------------------------------------------------------------------------

class ProvisionStatus(Enum):
    SUCCESS = "success"
    ALREADY_EXISTS = "already_exists"
    NOT_SIGNED_IN = "not_signed_in"
    TOS_REQUIRED = "tos_required"
    FAILED = "failed"
    TIMEOUT = "timeout"


@dataclass
class ProvisionResult:
    status: ProvisionStatus
    provider: str
    api_key: str | None = None
    message: str = ""
    duration_ms: int = 0


# ---------------------------------------------------------------------------
# Encryption for provider keys (Fernet-like using PBKDF2 + AES via stdlib)
# ---------------------------------------------------------------------------

_JWT_SECRET = os.environ.get("JWT_SECRET", "").strip()
if not _JWT_SECRET:
    raise RuntimeError(
        "JWT_SECRET env var is required before provider keys can be encrypted."
    )

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes


def _derive_fernet_key(salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100_000,
    )
    return base64.urlsafe_b64encode(kdf.derive(_JWT_SECRET.encode()))


def _provider_key_salt() -> bytes:
    configured = os.environ.get("PROVIDER_KEY_SALT", "").strip()
    if configured:
        return configured.encode()
    # Avoid a public static salt. Tie the default to the deployment secret.
    return hashlib.sha256(f"{_JWT_SECRET}:provider-keys:v2".encode()).digest()[:16]


# Migration-only fallback for decrypting historical rows written before per-record salts.
_LEGACY_PROVIDER_KEY_SALT = b"unchained-provider-keys-v1"
_fallback_fernet = Fernet(_derive_fernet_key(_provider_key_salt()))
_legacy_fernet = Fernet(_derive_fernet_key(_LEGACY_PROVIDER_KEY_SALT))


def _encode_salt(salt: bytes) -> str:
    return base64.urlsafe_b64encode(salt).decode()


def _decode_salt(salt_text: str) -> bytes:
    return base64.urlsafe_b64decode(salt_text.encode())


def _encrypt(plaintext: str, salt: bytes | None = None) -> tuple[str, str]:
    salt = salt or os.urandom(16)
    fernet = Fernet(_derive_fernet_key(salt))
    return fernet.encrypt(plaintext.encode()).decode(), _encode_salt(salt)

def _decrypt(ciphertext: str, salt_text: str | None = None) -> str:
    token = ciphertext.encode()
    if salt_text:
        fernet = Fernet(_derive_fernet_key(_decode_salt(salt_text)))
        return fernet.decrypt(token).decode()
    try:
        return _fallback_fernet.decrypt(token).decode()
    except InvalidToken:
        # Backward compatibility for rows encrypted with the old static salt.
        return _legacy_fernet.decrypt(token).decode()


# ---------------------------------------------------------------------------
# Provider key storage (SQLite via auth.Auth)
# ---------------------------------------------------------------------------

_auth = Auth()


def _ensure_provider_keys_table():
    """Create the provider_keys table if it doesn't exist."""
    with _auth._conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS provider_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                encrypted_key TEXT NOT NULL,
                salt TEXT,
                created_at REAL NOT NULL,
                last_verified_at REAL,
                active INTEGER DEFAULT 1,
                UNIQUE(user_id, provider)
            )
        """)
        try:
            conn.execute("ALTER TABLE provider_keys ADD COLUMN salt TEXT")
        except Exception:
            pass


# Run on import
_ensure_provider_keys_table()


def store_provider_key(user_id: str, provider: str, api_key: str):
    """Store (or update) an encrypted provider API key."""
    encrypted, salt = _encrypt(api_key)
    now = time.time()
    with _auth._conn() as conn:
        conn.execute("""
            INSERT INTO provider_keys (user_id, provider, encrypted_key, salt, created_at, last_verified_at, active)
            VALUES (?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(user_id, provider)
            DO UPDATE SET encrypted_key = excluded.encrypted_key,
                          salt = excluded.salt,
                          last_verified_at = excluded.last_verified_at,
                          active = 1
        """, (user_id, provider, encrypted, salt, now, now))


def get_provider_key(user_id: str, provider: str) -> str | None:
    """Retrieve a decrypted provider API key, or None."""
    with _auth._conn() as conn:
        row = conn.execute(
            "SELECT id, encrypted_key, salt FROM provider_keys WHERE user_id = ? AND provider = ? AND active = 1",
            (user_id, provider),
        ).fetchone()
    if row is None:
        return None
    try:
        key = _decrypt(row[1], row[2])
        # Transparently migrate legacy rows onto a random per-record salt.
        if not row[2]:
            encrypted, salt = _encrypt(key)
            with _auth._conn() as conn:
                conn.execute(
                    "UPDATE provider_keys SET encrypted_key = ?, salt = ? WHERE id = ?",
                    (encrypted, salt, row[0]),
                )
        return key
    except Exception:
        return None


def has_provider_key(user_id: str, provider: str) -> bool:
    """Check if user has an active key for this provider."""
    with _auth._conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM provider_keys WHERE user_id = ? AND provider = ? AND active = 1",
            (user_id, provider),
        ).fetchone()
    return row is not None


def revoke_provider_key(user_id: str, provider: str) -> bool:
    """Deactivate a provider key. Returns True if existed."""
    with _auth._conn() as conn:
        cur = conn.execute(
            "UPDATE provider_keys SET active = 0 WHERE user_id = ? AND provider = ? AND active = 1",
            (user_id, provider),
        )
    return cur.rowcount > 0


def list_provider_keys(user_id: str) -> list[dict]:
    """List all active providers for a user (no raw keys exposed)."""
    with _auth._conn() as conn:
        rows = conn.execute(
            "SELECT provider, created_at, last_verified_at FROM provider_keys "
            "WHERE user_id = ? AND active = 1 ORDER BY created_at",
            (user_id,),
        ).fetchall()
    return [
        {"provider": r[0], "created_at": r[1], "last_verified_at": r[2]}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Chrome profile discovery (local provisioning)
# ---------------------------------------------------------------------------

DATA_DIR = os.environ.get("UNCHAINED_DATA_DIR",
                          os.path.join(os.path.expanduser("~"), ".unchained"))


def _chrome_user_data_dir() -> str | None:
    """Return the system Chrome user data directory, or None."""
    system = platform.system()
    if system == "Darwin":
        p = os.path.expanduser("~/Library/Application Support/Google/Chrome")
    elif system == "Linux":
        p = os.path.expanduser("~/.config/google-chrome")
    else:
        return None
    return p if os.path.isdir(p) else None


def list_chrome_profiles() -> list[dict]:
    """List Chrome profiles on this machine that are signed into Google."""
    chrome_dir = _chrome_user_data_dir()
    if not chrome_dir:
        return []

    profiles = []
    for entry in sorted(os.listdir(chrome_dir)):
        prefs_path = os.path.join(chrome_dir, entry, "Preferences")
        if not os.path.isfile(prefs_path):
            continue
        # Only look at Default and Profile N directories
        if entry != "Default" and not re.match(r"^Profile \d+$", entry):
            continue
        try:
            with open(prefs_path, "r") as f:
                prefs = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        account_info = prefs.get("account_info", [])
        if not account_info:
            continue

        email = account_info[0].get("email", "")
        full_name = account_info[0].get("full_name", "")
        profile_name = prefs.get("profile", {}).get("name", entry)

        if not email:
            continue

        profiles.append({
            "path": os.path.join(chrome_dir, entry),
            "dir_name": entry,
            "name": profile_name,
            "full_name": full_name,
            "email": email,
        })

    return profiles


_CACHE_DIRS = {"Cache", "Code Cache", "GPUCache", "ShaderCache",
               "Service Worker", "GrShaderCache", "DawnCache"}


def _copy_chrome_profile(src_profile: str, dest_user_data_dir: str):
    """Copy a Chrome profile into a new user-data-dir structure.

    Chrome expects --user-data-dir to be the *parent* containing profile dirs
    (Default, Profile 1, etc) plus Local State (cookie encryption keys).

    Args:
        src_profile: Path to the profile dir (e.g. .../Google/Chrome/Default)
        dest_user_data_dir: Destination user-data-dir (will be created)
    """
    chrome_parent = os.path.dirname(src_profile)
    profile_dir_name = os.path.basename(src_profile)

    os.makedirs(dest_user_data_dir, exist_ok=True)

    # Copy Local State (contains cookie encryption keys, required for sign-in)
    local_state = os.path.join(chrome_parent, "Local State")
    if os.path.isfile(local_state):
        shutil.copy2(local_state, os.path.join(dest_user_data_dir, "Local State"))

    # Copy the profile dir itself, skipping cache dirs
    def _ignore(directory, contents):
        return [c for c in contents if c in _CACHE_DIRS]
    shutil.copytree(
        src_profile,
        os.path.join(dest_user_data_dir, profile_dir_name),
        ignore=_ignore,
    )


def _find_free_port() -> int:
    """Find a free TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _find_chrome_binary() -> str | None:
    """Find the Chrome/Chromium binary on this machine."""
    paths = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
    ]
    for p in paths:
        if os.path.exists(p):
            return p
    return shutil.which("google-chrome") or shutil.which("chromium")


# ---------------------------------------------------------------------------
# Provider base class + registry
# ---------------------------------------------------------------------------

class AIProvider(ABC):
    """Base class for AI provider key provisioning."""

    name: str = ""

    @abstractmethod
    async def provision(self, agent_id: str, relay_host: str, relay_port: int,
                        profile_path: str = "") -> ProvisionResult:
        """Automate the browser to create an API key."""
        ...

    @abstractmethod
    async def check_existing(self, agent_id: str, relay_host: str, relay_port: int) -> str | None:
        """Check if a key already exists on the provider page. Returns key or None."""
        ...


_PROVIDERS: dict[str, AIProvider] = {}


def register_provider(provider: AIProvider):
    _PROVIDERS[provider.name] = provider


def get_provider(name: str) -> AIProvider | None:
    return _PROVIDERS.get(name)


def list_providers() -> list[str]:
    return list(_PROVIDERS.keys())


# ---------------------------------------------------------------------------
# Gemini provider
# ---------------------------------------------------------------------------

# Regex for Gemini API keys
_GEMINI_KEY_RE = re.compile(r"AIzaSy[A-Za-z0-9_-]{33}")
# Regex for OpenAI/Codex API keys
_OPENAI_KEY_RE = re.compile(r"sk-[A-Za-z0-9_-]{20,}")
# Regex for Anthropic API keys (sk-ant-api03-... or similar prefixes)
_ANTHROPIC_KEY_RE = re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")


class GeminiProvider(AIProvider):
    name = "gemini"

    def __init__(self):
        self._cdp = None  # Set for local (direct CDP) mode, None for relay mode
        self._local_port = None  # CDP port when in local mode

    async def check_existing(self, agent_id: str, relay_host: str, relay_port: int) -> str | None:
        """Navigate to AI Studio API key page and look for existing key."""
        result = await self._run_js(
            agent_id, "auto",
            "document.body.innerText",
            relay_host, relay_port,
        )
        match = _GEMINI_KEY_RE.search(result)
        return match.group(0) if match else None

    async def _run_js(self, agent_id, tab_id, js, relay_host, relay_port):
        """Helper: run JS and return result string. Uses direct CDP if available."""
        if self._cdp:
            r = await self._cdp.send("Runtime.evaluate", {
                "expression": js, "returnByValue": True,
            })
            # cdp.send() already unwraps the outer "result" envelope,
            # so r = {"result": {"type": "string", "value": "..."}, ...}
            res = r.get("result", {})
            val = res.get("value")
            if val is None and res.get("type") == "undefined":
                return "undefined"
            if isinstance(val, (dict, list)):
                return json.dumps(val, indent=2)
            return str(val) if val is not None else ""
        return await cloud_tools.run_js(agent_id, tab_id, js, relay_host, relay_port)

    async def _scan_keys_in_html(self, agent_id, tab_id, relay_host, relay_port) -> list[str]:
        """Scan visible key elements and copy buttons for Gemini API keys.

        Avoids scanning raw HTML which picks up Google-internal keys from JS bundles.
        """
        result = await self._run_js(
            agent_id, tab_id,
            r"""(function() {
                var keys = [];
                // Look for elements that display API keys (usually in copy-able containers)
                var candidates = document.querySelectorAll(
                    '[class*="key"], [class*="api"], [class*="secret"], [class*="token"], ' +
                    '[data-value], input[readonly], input[type="text"][value*="AIza"], ' +
                    '[aria-label*="key"], [aria-label*="API"]'
                );
                candidates.forEach(function(el) {
                    var text = (el.value || el.textContent || el.getAttribute('data-value') || '').trim();
                    var m = text.match(/AIzaSy[A-Za-z0-9_\-]{33}/);
                    if (m) keys.push(m[0]);
                });
                // Also check visible text in the main content area
                var main = document.querySelector('main, [role="main"], .main-content') || document.body;
                if (main) {
                    var walker = document.createTreeWalker(main, NodeFilter.SHOW_TEXT, null, false);
                    while (walker.nextNode()) {
                        var m = walker.currentNode.textContent.match(/AIzaSy[A-Za-z0-9_\-]{33}/);
                        if (m) keys.push(m[0]);
                    }
                }
                // Deduplicate
                return Array.from(new Set(keys));
            })()""",
            relay_host, relay_port,
        )
        if isinstance(result, str):
            return _GEMINI_KEY_RE.findall(result)
        if isinstance(result, list):
            return [k for k in result if _GEMINI_KEY_RE.match(k)]
        return []

    async def provision(self, agent_id: str, relay_host: str, relay_port: int,
                        profile_path: str = "") -> ProvisionResult:
        t0 = time.time()
        tab_id = None
        _used_prov_chrome = False  # track whether we launched a provision Chrome

        def _elapsed():
            return int((time.time() - t0) * 1000)

        def _result(status, **kw):
            return ProvisionResult(status=status, provider=self.name, duration_ms=_elapsed(), **kw)

        try:
            # Step 1: Create a new tab (don't disrupt user browsing)
            if self._cdp and self._local_port:
                # Local mode: use HTTP endpoint to create tab
                try:
                    async with httpx.AsyncClient() as client:
                        resp = await client.put(
                            f"http://127.0.0.1:{self._local_port}/json/new?about:blank",
                            timeout=5,
                        )
                    tab_info = resp.json()
                    tab_id = tab_info.get("id", "local")
                    # Connect CDP to the new tab
                    ws_url = tab_info.get("webSocketDebuggerUrl", "")
                    if ws_url:
                        from cdp import CDP
                        self._cdp = CDP(ws_url)
                        await self._cdp.connect()
                except Exception as e:
                    return _result(ProvisionStatus.FAILED, message=f"Failed to create tab: {e}")
            elif profile_path:
                # Relay mode with profile: launch a provision Chrome on the bridge
                print(f"[provision:gemini] Launching provision Chrome with profile: {profile_path}")
                launch_result = await cloud_tools.provision_launch(
                    agent_id, profile_path, relay_host, relay_port,
                )
                if "error" in launch_result:
                    return _result(ProvisionStatus.FAILED,
                                   message=f"Failed to launch provision Chrome: {launch_result['error']}")
                tab_id = launch_result.get("tab_id", "")
                _used_prov_chrome = True
                print(f"[provision:gemini] Provision Chrome launched, tab_id={tab_id}")
            else:
                tab_id = await cloud_tools.create_tab(
                    agent_id, "about:blank", relay_host, relay_port,
                )
            if not tab_id:
                return _result(ProvisionStatus.FAILED, message="Failed to create new tab")
            print(f"[provision:gemini] Created tab {tab_id}")

            # Step 2: Navigate to AI Studio API key page
            if self._cdp:
                await self._cdp.send("Page.navigate", {"url": "https://aistudio.google.com/apikey"})
            else:
                await cloud_tools.navigate(
                    agent_id, tab_id,
                    "https://aistudio.google.com/apikey",
                    relay_host, relay_port,
                )
            # Wait for page to actually load (AI Studio can be slow on first visit)
            print("[provision:gemini] Waiting for aistudio.google.com/apikey to load...")
            for attempt in range(20):  # up to ~30 seconds
                await asyncio.sleep(1.5)
                try:
                    ready = await self._run_js(
                        agent_id, tab_id,
                        r"""(function() {
                            if (document.readyState !== 'complete') return 'loading';
                            var loc = location.href;
                            if (loc.includes('accounts.google.com')) return 'signin';
                            if (document.querySelectorAll('mat-checkbox, [role="checkbox"]').length > 0) return 'ready';
                            var btns = Array.from(document.querySelectorAll('button, [role="button"]'));
                            if (btns.some(function(b) { return /create api key|continue/i.test(b.textContent.trim()); })) return 'ready';
                            if (document.querySelector('[class*="api-key"], [class*="apikey"], [class*="key-string"]')) return 'ready';
                            if (loc.includes('aistudio.google.com') && document.body && document.body.innerText.length > 200) return 'ready';
                            return 'waiting';
                        })()""",
                        relay_host, relay_port,
                    )
                except Exception:
                    ready = "waiting"
                print(f"[provision:gemini] Page load check ({attempt+1}/20): {ready}")
                if ready in ("ready", "signin"):
                    break
            else:
                print("[provision:gemini] Page did not fully load in time, proceeding anyway...")

            print("[provision:gemini] Navigated to aistudio.google.com/apikey")

            # Step 3: Check if redirected to sign-in
            location = await self._run_js(agent_id, tab_id, "location.href", relay_host, relay_port)
            if "accounts.google.com" in location or ready == "signin":
                if self._cdp or _used_prov_chrome:
                    # Visible Chrome (local mode or provision Chrome) — wait for user to sign in
                    print("[provision:gemini] Google sign-in required. Please sign in in the Chrome window...")
                    for _ in range(180):  # wait up to 6 minutes
                        await asyncio.sleep(2)
                        loc = await self._run_js(agent_id, tab_id, "location.href", relay_host, relay_port)
                        if "accounts.google.com" not in loc:
                            print(f"[provision:gemini] Sign-in complete, now at: {loc[:80]}")
                            # Wait for the destination page to load
                            await asyncio.sleep(3)
                            break
                    else:
                        return _result(
                            ProvisionStatus.NOT_SIGNED_IN,
                            message="Timed out waiting for Google sign-in.",
                        )
                else:
                    return _result(
                        ProvisionStatus.NOT_SIGNED_IN,
                        message="User is not signed into Google. Please sign in to Google in Chrome first.",
                    )

            # Log which Google account is active on this page
            account_info = await self._run_js(
                agent_id, tab_id,
                r"""(function() {
                    var imgs = document.querySelectorAll('img[data-src*="googleusercontent"], img[src*="googleusercontent"]');
                    var labels = document.querySelectorAll('[aria-label*="@"]');
                    var info = [];
                    labels.forEach(function(el) { info.push(el.getAttribute('aria-label')); });
                    if (info.length === 0) {
                        var all = document.body ? document.body.innerText : '';
                        var m = all.match(/[a-zA-Z0-9._%+-]+@gmail\.com/);
                        if (m) info.push(m[0]);
                    }
                    return info.join(', ') || 'unknown';
                })()""",
                relay_host, relay_port,
            )
            print(f"[provision:gemini] Active Google account: {account_info}")

            # Step 4: Check for existing API key in HTML (keys are in attributes, not always visible text)
            existing_keys = await self._scan_keys_in_html(agent_id, tab_id, relay_host, relay_port)
            if existing_keys:
                key = existing_keys[0]
                print(f"[provision:gemini] Found existing key: {key[:10]}...")
                return _result(ProvisionStatus.ALREADY_EXISTS, api_key=key, message="Found existing Gemini API key")

            # Step 5: Handle Terms of Service
            # Detect if ToS dialog is showing. If in local (visible) mode,
            # wait for the user to accept it manually. Otherwise try to click.
            tos_result = await self._run_js(
                agent_id, tab_id,
                r"""
                (function() {
                    var cbs = document.querySelectorAll('mat-checkbox, [role="checkbox"]');
                    var btns = Array.from(document.querySelectorAll('button, [role="button"]'));
                    var cont = btns.find(function(b) { return /^Continue$/i.test(b.textContent.trim()); });
                    if (cbs.length > 0 || cont) return 'tos_present';
                    return 'no_tos';
                })()
                """,
                relay_host, relay_port,
            )
            print(f"[provision:gemini] ToS check: {tos_result}")

            if "tos_present" in tos_result:
                if self._cdp or _used_prov_chrome:
                    # Visible Chrome (local mode or provision Chrome) — wait for user to accept ToS
                    print("[provision:gemini] ToS dialog detected. Please accept the terms in the Chrome window...")
                    for _ in range(120):  # wait up to 2 minutes
                        await asyncio.sleep(2)
                        # Check if ToS is gone (page now shows API keys UI)
                        check = await self._run_js(
                            agent_id, tab_id,
                            r"""
                            (function() {
                                var btns = Array.from(document.querySelectorAll('button, [role="button"]'));
                                var cont = btns.find(function(b) { return /^Continue$/i.test(b.textContent.trim()); });
                                var cbs = document.querySelectorAll('mat-checkbox, [role="checkbox"]');
                                if (cbs.length > 0 || cont) return 'still_tos';
                                return 'tos_done';
                            })()
                            """,
                            relay_host, relay_port,
                        )
                        if "tos_done" in check:
                            print("[provision:gemini] ToS accepted by user")
                            await asyncio.sleep(2)
                            break
                    else:
                        return _result(ProvisionStatus.TOS_REQUIRED, message="Timed out waiting for user to accept Terms of Service")
                else:
                    # Relay mode — user must accept ToS themselves
                    return _result(
                        ProvisionStatus.TOS_REQUIRED,
                        message="Google Terms of Service need your approval. Please open Chrome, accept the terms at aistudio.google.com/apikey, then click Provision again."
                    )

            # Step 6: Inject fetch/XHR interceptor BEFORE clicking create
            # This captures API responses containing the new key
            await self._run_js(
                agent_id, tab_id,
                r"""(function() {
                    window.__capturedKeys = [];
                    var origFetch = window.fetch;
                    window.fetch = function() {
                        return origFetch.apply(this, arguments).then(function(resp) {
                            var clone = resp.clone();
                            clone.text().then(function(body) {
                                var m = body.match(/AIzaSy[A-Za-z0-9_\-]{33}/g);
                                if (m) m.forEach(function(k) { window.__capturedKeys.push(k); });
                            }).catch(function(){});
                            return resp;
                        });
                    };
                    var origOpen = XMLHttpRequest.prototype.open;
                    var origSend = XMLHttpRequest.prototype.send;
                    XMLHttpRequest.prototype.open = function() {
                        this.addEventListener('load', function() {
                            try {
                                var m = this.responseText.match(/AIzaSy[A-Za-z0-9_\-]{33}/g);
                                if (m) m.forEach(function(k) { window.__capturedKeys.push(k); });
                            } catch(e) {}
                        });
                        return origOpen.apply(this, arguments);
                    };
                })()""",
                relay_host, relay_port,
            )
            print("[provision:gemini] Fetch/XHR interceptor injected")

            # Step 7: Find and click "Create API key" button
            create_result = await self._run_js(
                agent_id, tab_id,
                r"""
                (function() {
                    var btns = Array.from(document.querySelectorAll('button, [role="button"]'));
                    var createBtn = btns.find(function(b) { return /create api key/i.test(b.textContent.trim()); });
                    if (createBtn) { createBtn.click(); return 'clicked_create'; }
                    var allBtns = Array.from(document.querySelectorAll('[class*="button"]'));
                    createBtn = allBtns.find(function(b) { return /create.*key|get.*key/i.test(b.textContent.trim()); });
                    if (createBtn) { createBtn.click(); return 'clicked_fallback'; }
                    return 'not_found: ' + btns.map(function(b) { return b.textContent.trim().substring(0, 40); }).join(' | ');
                })()
                """,
                relay_host, relay_port,
            )
            print(f"[provision:gemini] Create button: {create_result}")
            if "not_found" in create_result:
                return _result(ProvisionStatus.FAILED, message=f"Could not find 'Create API key' button: {create_result[:200]}")

            await asyncio.sleep(3)

            # Step 8: Handle project selection dialog
            dialog_result = await self._run_js(
                agent_id, tab_id,
                r"""
                (function() {
                    var select = document.querySelector('mat-select[role="combobox"]');
                    if (select) { select.click(); return 'opened_dropdown'; }
                    var btns = Array.from(document.querySelectorAll('[role="dialog"] button, .cdk-overlay-pane button'));
                    var confirm = btns.find(function(b) { return /^create key$/i.test(b.textContent.trim()); });
                    if (confirm) { confirm.click(); return 'clicked_create_key'; }
                    return 'no_dialog';
                })()
                """,
                relay_host, relay_port,
            )
            print(f"[provision:gemini] Dialog step 1: {dialog_result}")

            if "opened_dropdown" in dialog_result:
                await asyncio.sleep(1)
                select_result = await self._run_js(
                    agent_id, tab_id,
                    r"""
                    (function() {
                        var options = document.querySelectorAll('mat-option, [role="option"]');
                        for (var i = 0; i < options.length; i++) {
                            if (/default gemini/i.test(options[i].textContent.trim())) {
                                options[i].click();
                                return 'selected_default';
                            }
                        }
                        for (var i = 0; i < options.length; i++) {
                            var t = options[i].textContent.trim();
                            if (!/import|create/i.test(t) && t.length > 0) {
                                options[i].click();
                                return 'selected:' + t.substring(0, 60);
                            }
                        }
                        return 'no_project_options';
                    })()
                    """,
                    relay_host, relay_port,
                )
                print(f"[provision:gemini] Project selection: {select_result}")

                if "no_project_options" in select_result:
                    return _result(ProvisionStatus.FAILED, message="No Cloud Projects available.")

                await asyncio.sleep(1)
                await self._run_js(
                    agent_id, tab_id,
                    r"""
                    (function() {
                        var btns = Array.from(document.querySelectorAll('button'));
                        var btn = btns.find(function(b) { return b.textContent.trim() === 'Create key'; });
                        if (btn) btn.click();
                    })()
                    """,
                    relay_host, relay_port,
                )
                print("[provision:gemini] Clicked 'Create key' in dialog")

            # Step 9: Extract the API key from intercepted fetch/XHR responses
            await asyncio.sleep(5)

            for attempt in range(8):
                intercepted = await self._run_js(
                    agent_id, tab_id,
                    "JSON.stringify(window.__capturedKeys || [])",
                    relay_host, relay_port,
                )
                print(f"[provision:gemini] Intercepted keys ({attempt+1}/8): {intercepted[:200]}")
                if intercepted and 'AIzaSy' in intercepted:
                    keys_found = _GEMINI_KEY_RE.findall(intercepted)
                    if keys_found:
                        key = keys_found[0]
                        print(f"[provision:gemini] Extracted key (intercepted): {key[:10]}...")
                        return _result(ProvisionStatus.SUCCESS, api_key=key, message="Successfully created Gemini API key")
                await asyncio.sleep(2)

            return _result(ProvisionStatus.FAILED, message="Could not extract API key. Check AI Studio manually.")

        except asyncio.TimeoutError:
            return _result(ProvisionStatus.TIMEOUT, message="Provisioning timed out")
        except Exception as e:
            return _result(ProvisionStatus.FAILED, message=f"Provisioning error: {e}")
        finally:
            # Always close the provisioning tab / provision Chrome
            if _used_prov_chrome:
                # Relay mode with provision Chrome: clean up the entire temp Chrome
                try:
                    from chrome_bridge import _extract_prov_slot
                    _slot = _extract_prov_slot(str(tab_id)) if tab_id else ""
                    await cloud_tools.provision_cleanup(agent_id, relay_host, relay_port, slot=_slot)
                    print("[provision:gemini] Provision Chrome cleaned up")
                except Exception:
                    pass
            elif tab_id:
                try:
                    if self._cdp and self._local_port:
                        # Local mode: close via HTTP endpoint
                        async with httpx.AsyncClient() as client:
                            await client.put(
                                f"http://127.0.0.1:{self._local_port}/json/close/{tab_id}",
                                timeout=3,
                            )
                    else:
                        await cloud_tools.close_tab(
                            agent_id, tab_id,
                            relay_host, relay_port,
                        )
                except Exception:
                    pass
            self._cdp = None
            self._local_port = None


class _CodexProviderBase(AIProvider):
    """Base provider for provisioning OpenAI/Codex API keys."""

    def __init__(self):
        self._cdp = None  # Set for local (direct CDP) mode, None for relay mode
        self._local_port = None  # CDP port when in local mode

    async def check_existing(self, agent_id: str, relay_host: str, relay_port: int) -> str | None:
        """OpenAI does not expose existing secret keys after creation."""
        del agent_id, relay_host, relay_port
        return None

    async def _run_js(self, agent_id, tab_id, js, relay_host, relay_port):
        """Helper: run JS and return result string. Uses direct CDP if available."""
        if self._cdp:
            r = await self._cdp.send("Runtime.evaluate", {
                "expression": js, "returnByValue": True,
            })
            res = r.get("result", {})
            val = res.get("value")
            if val is None and res.get("type") == "undefined":
                return "undefined"
            if isinstance(val, (dict, list)):
                return json.dumps(val, indent=2)
            return str(val) if val is not None else ""
        return await cloud_tools.run_js(agent_id, tab_id, js, relay_host, relay_port)

    async def provision(self, agent_id: str, relay_host: str, relay_port: int,
                        profile_path: str = "") -> ProvisionResult:
        t0 = time.time()
        tab_id = None
        _used_prov_chrome = False  # track whether we launched a provision Chrome

        def _elapsed():
            return int((time.time() - t0) * 1000)

        def _result(status, **kw):
            return ProvisionResult(status=status, provider=self.name, duration_ms=_elapsed(), **kw)

        try:
            # Step 1: Create a new tab (don't disrupt user browsing)
            if self._cdp and self._local_port:
                # Local mode: use HTTP endpoint to create tab
                try:
                    async with httpx.AsyncClient() as client:
                        resp = await client.put(
                            f"http://127.0.0.1:{self._local_port}/json/new?about:blank",
                            timeout=5,
                        )
                    tab_info = resp.json()
                    tab_id = tab_info.get("id", "local")
                    # Connect CDP to the new tab
                    ws_url = tab_info.get("webSocketDebuggerUrl", "")
                    if ws_url:
                        from cdp import CDP
                        self._cdp = CDP(ws_url)
                        await self._cdp.connect()
                except Exception as e:
                    return _result(ProvisionStatus.FAILED, message=f"Failed to create tab: {e}")
            elif profile_path:
                # Relay mode with profile: launch a provision Chrome on the bridge
                print(f"[provision:{self.name}] Launching provision Chrome with profile: {profile_path}")
                launch_result = await cloud_tools.provision_launch(
                    agent_id, profile_path, relay_host, relay_port,
                )
                if "error" in launch_result:
                    return _result(
                        ProvisionStatus.FAILED,
                        message=f"Failed to launch provision Chrome: {launch_result['error']}",
                    )
                tab_id = launch_result.get("tab_id", "")
                _used_prov_chrome = True
                print(f"[provision:{self.name}] Provision Chrome launched, tab_id={tab_id}")
            else:
                tab_id = await cloud_tools.create_tab(
                    agent_id, "about:blank", relay_host, relay_port,
                )
            if not tab_id:
                return _result(ProvisionStatus.FAILED, message="Failed to create new tab")
            print(f"[provision:{self.name}] Created tab {tab_id}")

            # Step 2: Navigate to OpenAI API key page
            if self._cdp:
                await self._cdp.send("Page.navigate", {"url": "https://platform.openai.com/api-keys"})
            else:
                await cloud_tools.navigate(
                    agent_id, tab_id,
                    "https://platform.openai.com/api-keys",
                    relay_host, relay_port,
                )
            print(f"[provision:{self.name}] Waiting for platform.openai.com/api-keys to load...")

            ready = "waiting"
            for attempt in range(20):  # up to ~30 seconds
                await asyncio.sleep(1.5)
                try:
                    ready = await self._run_js(
                        agent_id, tab_id,
                        r"""(function() {
                            if (document.readyState !== 'complete') return 'loading';
                            var loc = location.href || '';
                            if (/auth\.openai\.com|\/login|\/signin/i.test(loc)) return 'signin';
                            var btns = Array.from(document.querySelectorAll('button,[role="button"]'));
                            if (btns.some(function(b) {
                                return /create new secret key|create api key|new secret key|create key/i.test((b.textContent||'').trim());
                            })) return 'ready';
                            if (document.body && /api keys|secret key|project key/i.test(document.body.innerText || '')) return 'ready';
                            return 'waiting';
                        })()""",
                        relay_host, relay_port,
                    )
                except Exception:
                    ready = "waiting"
                print(f"[provision:{self.name}] Page load check ({attempt+1}/20): {ready}")
                if ready in ("ready", "signin"):
                    break
            else:
                print(f"[provision:{self.name}] Page did not fully load in time, proceeding anyway...")

            # Step 3: Check if redirected to sign-in
            location = await self._run_js(agent_id, tab_id, "location.href", relay_host, relay_port)
            if "auth.openai.com" in location or "/login" in location or ready == "signin":
                if self._cdp or _used_prov_chrome:
                    # Visible Chrome (local mode or provision Chrome) — wait for user to sign in
                    print(f"[provision:{self.name}] OpenAI sign-in required. Please sign in in the Chrome window...")
                    for _ in range(180):  # wait up to 6 minutes
                        await asyncio.sleep(2)
                        loc = await self._run_js(agent_id, tab_id, "location.href", relay_host, relay_port)
                        if "auth.openai.com" not in loc and "/login" not in loc:
                            print(f"[provision:{self.name}] Sign-in complete, now at: {loc[:80]}")
                            await asyncio.sleep(3)
                            break
                    else:
                        return _result(
                            ProvisionStatus.NOT_SIGNED_IN,
                            message="Timed out waiting for OpenAI sign-in.",
                        )
                else:
                    return _result(
                        ProvisionStatus.NOT_SIGNED_IN,
                        message="User is not signed into OpenAI. Please sign in in Chrome first.",
                    )

            # Step 4: Inject fetch/XHR + clipboard interceptor BEFORE clicking create
            await self._run_js(
                agent_id, tab_id,
                r"""(function() {
                    window.__capturedKeys = window.__capturedKeys || [];
                    function addKeys(text) {
                        if (!text) return;
                        var m = String(text).match(/sk-[A-Za-z0-9_\-]{20,}/g);
                        if (!m) return;
                        m.forEach(function(k) {
                            if (window.__capturedKeys.indexOf(k) === -1) {
                                window.__capturedKeys.push(k);
                            }
                        });
                    }
                    try {
                        var origFetch = window.fetch;
                        window.fetch = function() {
                            return origFetch.apply(this, arguments).then(function(resp) {
                                try {
                                    var clone = resp.clone();
                                    clone.text().then(addKeys).catch(function(){});
                                } catch (e) {}
                                return resp;
                            });
                        };
                    } catch (e) {}
                    try {
                        var origOpen = XMLHttpRequest.prototype.open;
                        XMLHttpRequest.prototype.open = function() {
                            this.addEventListener('load', function() {
                                try { addKeys(this.responseText); } catch (e) {}
                            });
                            return origOpen.apply(this, arguments);
                        };
                    } catch (e) {}
                    try {
                        if (navigator.clipboard && navigator.clipboard.writeText && !navigator.__uc_clip_wrapped) {
                            var origWrite = navigator.clipboard.writeText.bind(navigator.clipboard);
                            navigator.clipboard.writeText = function(t) {
                                addKeys(t);
                                return origWrite(t);
                            };
                            navigator.__uc_clip_wrapped = true;
                        }
                    } catch (e) {}
                    try {
                        addKeys(document.body ? document.body.innerText : '');
                    } catch (e) {}
                    return 'ok';
                })()""",
                relay_host, relay_port,
            )
            print(f"[provision:{self.name}] Key interceptor injected")

            # Step 5: Open "Create key" dialog.
            # OpenAI may show account onboarding (organization selection/creation)
            # before API keys are available. In visible mode, wait for the user.
            async def _try_open_create_dialog() -> str:
                return await self._run_js(
                    agent_id, tab_id,
                    r"""(function() {
                        var btns = Array.from(document.querySelectorAll('button,[role="button"],a[role="button"]'));
                        function txt(el) { return (el.textContent || '').trim().toLowerCase(); }
                        var createBtn = btns.find(function(b) {
                            var t = txt(b);
                            return t.includes('create new secret key') || t.includes('create api key') ||
                                   t === 'new secret key' || t.includes('create key');
                        });
                        if (createBtn) { createBtn.click(); return 'clicked_create'; }

                        var allText = (document.body && document.body.innerText || '').toLowerCase();
                        var onboardingHints = [
                            'create organization',
                            'select...',
                            'use another account',
                            'choose your organization',
                            'verify your email',
                            'complete your profile',
                            'welcome to openai',
                            'get started',
                            'set up your organization',
                        ];
                        var hit = onboardingHints.find(function(h) { return allText.indexOf(h) >= 0; });
                        if (hit) return 'onboarding:' + hit;

                        return 'not_found: ' + btns.slice(0, 20).map(function(b) { return txt(b).slice(0, 40); }).join(' | ');
                    })()""",
                    relay_host, relay_port,
                )

            create_result = await _try_open_create_dialog()
            print(f"[provision:{self.name}] Create button: {create_result}")
            if "not_found" in create_result or "onboarding:" in create_result:
                if self._cdp or _used_prov_chrome:
                    print(f"[provision:{self.name}] Waiting for user to finish OpenAI account setup...")
                    for _ in range(150):  # up to 5 minutes
                        await asyncio.sleep(2)
                        create_result = await _try_open_create_dialog()
                        if "clicked_create" in create_result:
                            print(f"[provision:{self.name}] Create button became available after setup")
                            break
                    else:
                        return _result(
                            ProvisionStatus.TOS_REQUIRED,
                            message=(
                                "OpenAI account setup is required (organization/profile selection). "
                                "Please complete it in Chrome, then click Provision again."
                            ),
                        )
                else:
                    return _result(
                        ProvisionStatus.TOS_REQUIRED,
                        message=(
                            "OpenAI account setup is required before creating API keys. "
                            "Please complete organization/profile setup in OpenAI, then retry."
                        ),
                    )

            await asyncio.sleep(2)

            # Step 6: Fill optional key name + click final create button in dialog
            dialog_result = await self._run_js(
                agent_id, tab_id,
                r"""(function() {
                    var keyName = 'unchained-' + Date.now().toString(36);
                    var input = document.querySelector(
                        'input[name*="name"], input[id*="name"], input[placeholder*="Name"], ' +
                        'input[placeholder*="name"], [role="dialog"] input[type="text"]'
                    );
                    if (input) {
                        input.focus();
                        input.value = keyName;
                        input.dispatchEvent(new Event('input', {bubbles:true}));
                        input.dispatchEvent(new Event('change', {bubbles:true}));
                    }
                    var btns = Array.from(document.querySelectorAll('[role="dialog"] button, [aria-modal="true"] button, button'));
                    function txt(el) { return (el.textContent || '').trim().toLowerCase(); }
                    var confirm = btns.find(function(b) {
                        var t = txt(b);
                        return t === 'create secret key' || t === 'create key' || t === 'create';
                    });
                    if (!confirm) {
                        confirm = btns.find(function(b) {
                            var t = txt(b);
                            return t.indexOf('create') >= 0 && t.indexOf('key') >= 0;
                        });
                    }
                    if (confirm) { confirm.click(); return input ? 'named_and_created' : 'created'; }
                    return input ? 'named_no_confirm' : 'no_dialog';
                })()""",
                relay_host, relay_port,
            )
            print(f"[provision:{self.name}] Dialog submit: {dialog_result}")

            # Step 7: Extract the API key (intercepted + visible page text fallback)
            await asyncio.sleep(3)
            for attempt in range(10):
                intercepted = await self._run_js(
                    agent_id, tab_id,
                    "JSON.stringify(window.__capturedKeys || [])",
                    relay_host, relay_port,
                )
                print(f"[provision:{self.name}] Intercepted keys ({attempt+1}/10): {intercepted[:200]}")
                if intercepted and "sk-" in intercepted:
                    keys_found = _OPENAI_KEY_RE.findall(intercepted)
                    if keys_found:
                        key = keys_found[0]
                        print(f"[provision:{self.name}] Extracted key (intercepted): {key[:10]}...")
                        return _result(
                            ProvisionStatus.SUCCESS,
                            api_key=key,
                            message="Successfully created Codex API key",
                        )

                visible = await self._run_js(
                    agent_id, tab_id,
                    "(document.body && document.body.innerText) || ''",
                    relay_host, relay_port,
                )
                if visible and "sk-" in visible:
                    keys_found = _OPENAI_KEY_RE.findall(visible)
                    if keys_found:
                        key = keys_found[0]
                        print(f"[provision:{self.name}] Extracted key (visible): {key[:10]}...")
                        return _result(
                            ProvisionStatus.SUCCESS,
                            api_key=key,
                            message="Successfully created Codex API key",
                        )
                await asyncio.sleep(2)

            return _result(
                ProvisionStatus.FAILED,
                message="Could not extract OpenAI/Codex API key. Please create one manually and paste it.",
            )

        except asyncio.TimeoutError:
            return _result(ProvisionStatus.TIMEOUT, message="Provisioning timed out")
        except Exception as e:
            return _result(ProvisionStatus.FAILED, message=f"Provisioning error: {e}")
        finally:
            # Always close the provisioning tab / provision Chrome
            if _used_prov_chrome:
                # Relay mode with provision Chrome: clean up the entire temp Chrome
                try:
                    from chrome_bridge import _extract_prov_slot
                    _slot = _extract_prov_slot(str(tab_id)) if tab_id else ""
                    await cloud_tools.provision_cleanup(agent_id, relay_host, relay_port, slot=_slot)
                    print(f"[provision:{self.name}] Provision Chrome cleaned up")
                except Exception:
                    pass
            elif tab_id:
                try:
                    if self._cdp and self._local_port:
                        # Local mode: close via HTTP endpoint
                        async with httpx.AsyncClient() as client:
                            await client.put(
                                f"http://127.0.0.1:{self._local_port}/json/close/{tab_id}",
                                timeout=3,
                            )
                    else:
                        await cloud_tools.close_tab(
                            agent_id, tab_id,
                            relay_host, relay_port,
                        )
                except Exception:
                    pass
            self._cdp = None
            self._local_port = None


class CodexSDKProvider(_CodexProviderBase):
    name = "codex-sdk"


class ClaudeSDKProvider(AIProvider):
    """Browser-automated provisioning for Anthropic Claude SDK API keys."""

    name = "claude-sdk"

    def __init__(self):
        self._cdp = None
        self._local_port = None

    async def check_existing(self, agent_id: str, relay_host: str, relay_port: int) -> str | None:
        """Anthropic does not expose existing secret keys after creation."""
        del agent_id, relay_host, relay_port
        return None

    async def _run_js(self, agent_id, tab_id, js, relay_host, relay_port):
        """Helper: run JS and return result string. Uses direct CDP if available."""
        if self._cdp:
            r = await self._cdp.send("Runtime.evaluate", {
                "expression": js, "returnByValue": True,
            })
            res = r.get("result", {})
            val = res.get("value")
            if val is None and res.get("type") == "undefined":
                return "undefined"
            if isinstance(val, (dict, list)):
                return json.dumps(val, indent=2)
            return str(val) if val is not None else ""
        return await cloud_tools.run_js(agent_id, tab_id, js, relay_host, relay_port)

    async def provision(
        self, agent_id: str, relay_host: str, relay_port: int, profile_path: str = ""
    ) -> ProvisionResult:
        t0 = time.time()
        tab_id = None
        _used_prov_chrome = False

        def _elapsed():
            return int((time.time() - t0) * 1000)

        def _result(status, **kw):
            return ProvisionResult(status=status, provider=self.name, duration_ms=_elapsed(), **kw)

        try:
            # Step 1: Create a new tab
            if self._cdp and self._local_port:
                try:
                    async with httpx.AsyncClient() as client:
                        resp = await client.put(
                            f"http://127.0.0.1:{self._local_port}/json/new?about:blank",
                            timeout=5,
                        )
                    tab_info = resp.json()
                    tab_id = tab_info.get("id", "local")
                    ws_url = tab_info.get("webSocketDebuggerUrl", "")
                    if ws_url:
                        from cdp import CDP
                        self._cdp = CDP(ws_url)
                        await self._cdp.connect()
                except Exception as e:
                    return _result(ProvisionStatus.FAILED, message=f"Failed to create tab: {e}")
            elif profile_path:
                print(f"[provision:{self.name}] Launching provision Chrome with profile: {profile_path}")
                launch_result = await cloud_tools.provision_launch(
                    agent_id, profile_path, relay_host, relay_port,
                )
                if "error" in launch_result:
                    return _result(
                        ProvisionStatus.FAILED,
                        message=f"Failed to launch provision Chrome: {launch_result['error']}",
                    )
                tab_id = launch_result.get("tab_id", "")
                _used_prov_chrome = True
                print(f"[provision:{self.name}] Provision Chrome launched, tab_id={tab_id}")
            else:
                tab_id = await cloud_tools.create_tab(
                    agent_id, "about:blank", relay_host, relay_port,
                )
            if not tab_id:
                return _result(ProvisionStatus.FAILED, message="Failed to create new tab")
            print(f"[provision:{self.name}] Created tab {tab_id}")

            # Step 2: Navigate to Anthropic console API keys page
            key_url = "https://console.anthropic.com/settings/keys"
            if self._cdp:
                await self._cdp.send("Page.navigate", {"url": key_url})
            else:
                await cloud_tools.navigate(
                    agent_id, tab_id, key_url, relay_host, relay_port,
                )
            print(f"[provision:{self.name}] Waiting for {key_url} to load...")

            ready = "waiting"
            for attempt in range(20):  # up to ~30 seconds
                await asyncio.sleep(1.5)
                try:
                    ready = await self._run_js(
                        agent_id, tab_id,
                        r"""(function() {
                            if (document.readyState !== 'complete') return 'loading';
                            var loc = location.href || '';
                            if (/login|signin|auth/i.test(loc)) return 'signin';
                            var btns = Array.from(document.querySelectorAll('button,[role="button"]'));
                            if (btns.some(function(b) {
                                return /create key/i.test((b.textContent||'').trim());
                            })) return 'ready';
                            if (document.body && /api key/i.test(document.body.innerText || '')) return 'ready';
                            return 'waiting';
                        })()""",
                        relay_host, relay_port,
                    )
                except Exception:
                    ready = "waiting"
                print(f"[provision:{self.name}] Page load check ({attempt+1}/20): {ready}")
                if ready in ("ready", "signin"):
                    break
            else:
                print(f"[provision:{self.name}] Page did not fully load in time, proceeding anyway...")

            # Step 3: Check if redirected to sign-in
            location = await self._run_js(agent_id, tab_id, "location.href", relay_host, relay_port)
            if ready == "signin" or "/login" in location:
                if self._cdp or _used_prov_chrome:
                    print(f"[provision:{self.name}] Anthropic sign-in required. Please sign in in the Chrome window...")
                    for _ in range(180):  # wait up to 6 minutes
                        await asyncio.sleep(2)
                        loc = await self._run_js(agent_id, tab_id, "location.href", relay_host, relay_port)
                        if "/login" not in loc and "signin" not in loc.lower():
                            print(f"[provision:{self.name}] Sign-in complete, now at: {loc[:80]}")
                            await asyncio.sleep(3)
                            break
                    else:
                        return _result(
                            ProvisionStatus.NOT_SIGNED_IN,
                            message="Timed out waiting for Anthropic sign-in.",
                        )
                else:
                    return _result(
                        ProvisionStatus.NOT_SIGNED_IN,
                        message="User is not signed into Anthropic. Please sign in in Chrome first.",
                    )

            # Step 4: Inject fetch/XHR + clipboard interceptor BEFORE clicking create
            await self._run_js(
                agent_id, tab_id,
                r"""(function() {
                    window.__capturedKeys = window.__capturedKeys || [];
                    function addKeys(text) {
                        if (!text) return;
                        var m = String(text).match(/sk-ant-[A-Za-z0-9_\-]{20,}/g);
                        if (!m) return;
                        m.forEach(function(k) {
                            if (window.__capturedKeys.indexOf(k) === -1) {
                                window.__capturedKeys.push(k);
                            }
                        });
                    }
                    try {
                        var origFetch = window.fetch;
                        window.fetch = function() {
                            return origFetch.apply(this, arguments).then(function(resp) {
                                try {
                                    var clone = resp.clone();
                                    clone.text().then(addKeys).catch(function(){});
                                } catch (e) {}
                                return resp;
                            });
                        };
                    } catch (e) {}
                    try {
                        var origOpen = XMLHttpRequest.prototype.open;
                        XMLHttpRequest.prototype.open = function() {
                            this.addEventListener('load', function() {
                                try { addKeys(this.responseText); } catch (e) {}
                            });
                            return origOpen.apply(this, arguments);
                        };
                    } catch (e) {}
                    try {
                        if (navigator.clipboard && navigator.clipboard.writeText && !navigator.__uc_clip_wrapped) {
                            var origWrite = navigator.clipboard.writeText.bind(navigator.clipboard);
                            navigator.clipboard.writeText = function(t) {
                                addKeys(t);
                                return origWrite(t);
                            };
                            navigator.__uc_clip_wrapped = true;
                        }
                    } catch (e) {}
                    try {
                        addKeys(document.body ? document.body.innerText : '');
                    } catch (e) {}
                    return 'ok';
                })()""",
                relay_host, relay_port,
            )
            print(f"[provision:{self.name}] Key interceptor injected")

            # Step 5: Click "Create Key" button
            create_result = await self._run_js(
                agent_id, tab_id,
                r"""(function() {
                    var btns = Array.from(document.querySelectorAll('button,[role="button"],a[role="button"]'));
                    var createBtn = btns.find(function(b) {
                        var t = (b.textContent || '').trim().toLowerCase();
                        return t === 'create key' || t.indexOf('create key') >= 0;
                    });
                    if (createBtn) { createBtn.click(); return 'clicked_create'; }
                    // Fallback: broader search for any button with "create" and "key"
                    createBtn = btns.find(function(b) {
                        var t = (b.textContent || '').trim().toLowerCase();
                        return t.indexOf('create') >= 0 && t.indexOf('key') >= 0;
                    });
                    if (createBtn) { createBtn.click(); return 'clicked_create_broad'; }
                    // Fallback: look for a "+" / add button near "API Keys" heading
                    createBtn = btns.find(function(b) {
                        var t = (b.textContent || '').trim();
                        return t === '+' || /^add/i.test(t);
                    });
                    if (createBtn) { createBtn.click(); return 'clicked_add'; }
                    return 'no_create_button';
                })()""",
                relay_host, relay_port,
            )
            print(f"[provision:{self.name}] Create key click: {create_result}")

            if "no_create_button" in str(create_result):
                return _result(
                    ProvisionStatus.FAILED,
                    message="Could not find 'Create Key' button on Anthropic console. The page layout may have changed.",
                )

            await asyncio.sleep(2)

            # Step 6: Fill key name in dialog + click confirm
            # Anthropic console uses "Add" as the confirm button (not "Create")
            dialog_result = await self._run_js(
                agent_id, tab_id,
                r"""(function() {
                    var keyName = 'unchained-' + Date.now().toString(36);
                    // Find name input (placeholder is "my-secret-key")
                    var input = document.querySelector(
                        'input[placeholder*="secret-key"], input[placeholder*="key" i], ' +
                        '[role="dialog"] input[type="text"], ' +
                        '[aria-modal="true"] input[type="text"], ' +
                        'input[name*="name"], input[id*="name"]'
                    );
                    if (input) {
                        input.focus();
                        var nativeSet = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'value'
                        ).set;
                        nativeSet.call(input, keyName);
                        input.dispatchEvent(new Event('input', {bubbles:true}));
                        input.dispatchEvent(new Event('change', {bubbles:true}));
                    }
                    // Find the confirm button — Anthropic uses "Add"
                    var btns = Array.from(document.querySelectorAll('button,[role="button"]'));
                    function txt(el) { return (el.textContent || '').trim().toLowerCase(); }
                    // Priority 1: "Add" button (Anthropic's confirm label)
                    var confirm = btns.find(function(b) { return txt(b) === 'add'; });
                    // Priority 2: "Create key" / "Create"
                    if (!confirm) confirm = btns.find(function(b) {
                        var t = txt(b); return t === 'create key' || t === 'create';
                    });
                    // Priority 3: submit button
                    if (!confirm) confirm = btns.find(function(b) { return b.type === 'submit'; });
                    if (confirm) { confirm.click(); return (input ? 'named_' : '') + 'clicked_' + txt(confirm); }
                    return input ? 'named_no_confirm' : 'no_dialog';
                })()""",
                relay_host, relay_port,
            )
            print(f"[provision:{self.name}] Dialog submit: {dialog_result}")

            await asyncio.sleep(3)

            # Step 7: Extract the API key via multiple methods
            # Anthropic console may display key in input/code elements, not just innerText
            for attempt in range(10):
                # Method 1: Check intercepted keys (fetch/XHR/clipboard)
                intercepted = await self._run_js(
                    agent_id, tab_id,
                    "JSON.stringify(window.__capturedKeys || [])",
                    relay_host, relay_port,
                )
                print(f"[provision:{self.name}] Intercepted keys ({attempt+1}/10): {intercepted[:200]}")
                if intercepted and "sk-ant-" in intercepted:
                    keys_found = _ANTHROPIC_KEY_RE.findall(intercepted)
                    if keys_found:
                        key = keys_found[0]
                        print(f"[provision:{self.name}] Extracted key (intercepted): {key[:12]}...")
                        return _result(
                            ProvisionStatus.SUCCESS,
                            api_key=key,
                            message="Successfully created Anthropic API key",
                        )

                # Method 2: Deep DOM scan — inputs, textareas, code, pre, data attrs, aria
                dom_scan = await self._run_js(
                    agent_id, tab_id,
                    r"""(function() {
                        var keys = [];
                        var re = /sk-ant-[A-Za-z0-9_\-]{20,}/g;
                        function scan(s) { if (!s) return; var m = s.match(re); if (m) m.forEach(function(k) { keys.push(k); }); }
                        // Scan input/textarea values
                        document.querySelectorAll('input, textarea').forEach(function(el) {
                            scan(el.value); scan(el.getAttribute('value'));
                        });
                        // Scan code/pre/span/div text (React renders keys in these)
                        document.querySelectorAll('code, pre, [class*="key"], [class*="secret"], [class*="token"], [data-testid*="key"]').forEach(function(el) {
                            scan(el.textContent);
                        });
                        // Scan data-* attributes
                        document.querySelectorAll('[data-value], [data-key], [data-secret]').forEach(function(el) {
                            scan(el.getAttribute('data-value'));
                            scan(el.getAttribute('data-key'));
                            scan(el.getAttribute('data-secret'));
                        });
                        // Scan dialog/modal content thoroughly
                        document.querySelectorAll('[role="dialog"], [aria-modal="true"], [class*="modal"], [class*="dialog"]').forEach(function(el) {
                            scan(el.textContent);
                            el.querySelectorAll('input, textarea').forEach(function(inp) { scan(inp.value); });
                        });
                        // Broad innerText fallback
                        scan(document.body ? document.body.innerText : '');
                        // Also innerHTML as last resort (key might be in an attribute)
                        if (keys.length === 0 && document.body) {
                            scan(document.body.innerHTML);
                        }
                        return JSON.stringify([...new Set(keys)]);
                    })()""",
                    relay_host, relay_port,
                )
                print(f"[provision:{self.name}] DOM scan ({attempt+1}/10): {dom_scan[:200]}")
                if dom_scan and "sk-ant-" in dom_scan:
                    keys_found = _ANTHROPIC_KEY_RE.findall(dom_scan)
                    if keys_found:
                        key = keys_found[0]
                        print(f"[provision:{self.name}] Extracted key (DOM scan): {key[:12]}...")
                        return _result(
                            ProvisionStatus.SUCCESS,
                            api_key=key,
                            message="Successfully created Anthropic API key",
                        )

                # Method 3: Try clicking "Copy" button to capture via clipboard interceptor
                if attempt == 1:
                    await self._run_js(
                        agent_id, tab_id,
                        r"""(function() {
                            var btns = Array.from(document.querySelectorAll('button,[role="button"]'));
                            var copyBtn = btns.find(function(b) {
                                var t = (b.textContent || '').trim().toLowerCase();
                                return t === 'copy' || t.indexOf('copy') >= 0;
                            });
                            if (copyBtn) { copyBtn.click(); return 'clicked_copy'; }
                            // Try SVG copy icon buttons (clipboard icon)
                            var iconBtns = document.querySelectorAll('button svg, [role="button"] svg');
                            if (iconBtns.length > 0) {
                                var parent = iconBtns[0].closest('button,[role="button"]');
                                if (parent) { parent.click(); return 'clicked_icon_btn'; }
                            }
                            return 'no_copy_btn';
                        })()""",
                        relay_host, relay_port,
                    )

                await asyncio.sleep(2)

            return _result(
                ProvisionStatus.FAILED,
                message="Could not extract Anthropic API key. Please create one manually and paste it.",
            )

        except asyncio.TimeoutError:
            return _result(ProvisionStatus.TIMEOUT, message="Provisioning timed out")
        except Exception as e:
            return _result(ProvisionStatus.FAILED, message=f"Provisioning error: {e}")
        finally:
            if _used_prov_chrome:
                try:
                    from chrome_bridge import _extract_prov_slot
                    _slot = _extract_prov_slot(str(tab_id)) if tab_id else ""
                    await cloud_tools.provision_cleanup(agent_id, relay_host, relay_port, slot=_slot)
                    print(f"[provision:{self.name}] Provision Chrome cleaned up")
                except Exception:
                    pass
            elif tab_id:
                try:
                    if self._cdp and self._local_port:
                        async with httpx.AsyncClient() as client:
                            await client.put(
                                f"http://127.0.0.1:{self._local_port}/json/close/{tab_id}",
                                timeout=3,
                            )
                    else:
                        await cloud_tools.close_tab(
                            agent_id, tab_id,
                            relay_host, relay_port,
                        )
                except Exception:
                    pass
            self._cdp = None
            self._local_port = None


class CodexCLIProvider(_CodexProviderBase):
    name = "codex-cli"


register_provider(GeminiProvider())
register_provider(ClaudeSDKProvider())
register_provider(CodexSDKProvider())
register_provider(CodexCLIProvider())


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

# Per-user-provider lock to prevent concurrent provisioning
_provision_locks: dict[str, asyncio.Lock] = {}

PROVISION_TIMEOUT = 300  # seconds (5 min — allows time for manual ToS acceptance)


def cleanup_provision_locks():
    """Remove provision locks that are not currently held."""
    for key in list(_provision_locks):
        if not _provision_locks[key].locked():
            _provision_locks.pop(key, None)


async def provision_key(
    provider_name: str,
    agent_id: str,
    relay_host: str,
    relay_port: int,
    user_id: str,
    store_key: bool = True,
    profile_path: str = "",
) -> ProvisionResult:
    """Provision an API key for the given provider.

    Idempotent: returns existing key if already provisioned.
    Thread-safe: per-user-provider lock prevents concurrent runs.
    """
    registered = get_provider(provider_name)
    if registered is None:
        return ProvisionResult(
            status=ProvisionStatus.FAILED,
            provider=provider_name,
            message=f"Unknown provider: {provider_name}. Available: {list_providers()}",
        )
    # Fresh instance so concurrent provisions don't share mutable state
    provider = type(registered)()

    # Check DB first (idempotent)
    existing = get_provider_key(user_id, provider_name)
    if existing:
        return ProvisionResult(
            status=ProvisionStatus.ALREADY_EXISTS,
            provider=provider_name,
            api_key=existing,
            message=f"{provider_name} key already provisioned",
        )

    # Per-user-provider lock
    lock_key = f"{user_id}:{provider_name}"
    if lock_key not in _provision_locks:
        _provision_locks[lock_key] = asyncio.Lock()
    lock = _provision_locks[lock_key]

    if lock.locked():
        return ProvisionResult(
            status=ProvisionStatus.FAILED,
            provider=provider_name,
            message="Provisioning already in progress for this provider",
        )

    async with lock:
        # Double-check after acquiring lock
        existing = get_provider_key(user_id, provider_name)
        if existing:
            return ProvisionResult(
                status=ProvisionStatus.ALREADY_EXISTS,
                provider=provider_name,
                api_key=existing,
                message=f"{provider_name} key already provisioned",
            )

        # Run provisioning with hard timeout
        try:
            result = await asyncio.wait_for(
                provider.provision(agent_id, relay_host, relay_port,
                                   profile_path=profile_path),
                timeout=PROVISION_TIMEOUT,
            )
        except asyncio.TimeoutError:
            result = ProvisionResult(
                status=ProvisionStatus.TIMEOUT,
                provider=provider_name,
                message=f"Provisioning timed out after {PROVISION_TIMEOUT}s",
            )

        # Store key on success (unless caller wants to defer)
        if store_key and result.api_key and result.status in (ProvisionStatus.SUCCESS, ProvisionStatus.ALREADY_EXISTS):
            store_provider_key(user_id, provider_name, result.api_key)
            print(f"[provision] Stored {provider_name} key for user {user_id}")

        return result


async def provision_key_local(
    provider_name: str,
    user_id: str,
    profile_path: str,
    headless: bool = False,
    store_key: bool = True,
) -> ProvisionResult:
    """Provision an API key using a local Chrome profile (direct CDP, no relay).

    Copies the profile to a temp dir, launches Chrome (visible so the user can
    accept ToS / CAPTCHAs), provisions via direct CDP, then cleans up.
    The original profile is never modified.
    """
    registered = get_provider(provider_name)
    if registered is None:
        return ProvisionResult(
            status=ProvisionStatus.FAILED,
            provider=provider_name,
            message=f"Unknown provider: {provider_name}. Available: {list_providers()}",
        )
    # Create a fresh instance so concurrent provisions don't share _cdp/_local_port state
    provider = type(registered)()

    if profile_path and not os.path.isdir(profile_path):
        return ProvisionResult(
            status=ProvisionStatus.FAILED,
            provider=provider_name,
            message=f"Profile path does not exist: {profile_path}",
        )

    # Check DB first (idempotent)
    existing = get_provider_key(user_id, provider_name)
    if existing:
        return ProvisionResult(
            status=ProvisionStatus.ALREADY_EXISTS,
            provider=provider_name,
            api_key=existing,
            message=f"{provider_name} key already provisioned",
        )

    # Per-user-provider lock
    lock_key = f"{user_id}:{provider_name}"
    if lock_key not in _provision_locks:
        _provision_locks[lock_key] = asyncio.Lock()
    lock = _provision_locks[lock_key]

    if lock.locked():
        return ProvisionResult(
            status=ProvisionStatus.FAILED,
            provider=provider_name,
            message="Provisioning already in progress for this provider",
        )

    chrome_bin = _find_chrome_binary()
    if not chrome_bin:
        return ProvisionResult(
            status=ProvisionStatus.FAILED,
            provider=provider_name,
            message="Chrome/Chromium not found on this machine",
        )

    temp_dir = os.path.join(DATA_DIR, f"provision_tmp_{uuid.uuid4().hex[:8]}")
    chrome_proc = None

    async with lock:
        # Double-check after acquiring lock
        existing = get_provider_key(user_id, provider_name)
        if existing:
            return ProvisionResult(
                status=ProvisionStatus.ALREADY_EXISTS,
                provider=provider_name,
                api_key=existing,
                message=f"{provider_name} key already provisioned",
            )

        try:
            # Step 1: Copy profile to temp user-data-dir (or create clean one)
            if profile_path:
                print(f"[provision:local] Source profile: {profile_path}")
                print(f"[provision:local] Copying profile to {temp_dir} ...")
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    None, functools.partial(_copy_chrome_profile, profile_path, temp_dir),
                )
                profile_dir_name = os.path.basename(profile_path)
            else:
                print(f"[provision:local] Using clean profile (no Google sign-in)")
                os.makedirs(temp_dir, exist_ok=True)
                profile_dir_name = "Default"

            # Step 2: Find a free port and launch Chrome
            port = _find_free_port()
            cmd = [
                chrome_bin,
                f"--user-data-dir={temp_dir}",
                f"--profile-directory={profile_dir_name}",
                f"--remote-debugging-port={port}",
                "--disable-sync",
                "--disable-background-networking",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-extensions",
                "--window-size=1280,900",
                "about:blank",
            ]
            if headless:
                cmd.insert(cmd.index("--disable-sync"), "--headless=new")
            print(f"[provision:local] Launching Chrome on port {port} ...")
            chrome_proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )

            # Step 3: Wait for Chrome CDP to be ready
            version_url = f"http://127.0.0.1:{port}/json/version"
            async with httpx.AsyncClient() as client:
                for _ in range(15):
                    await asyncio.sleep(1)
                    try:
                        resp = await client.get(version_url, timeout=2)
                        if resp.status_code == 200:
                            break
                    except Exception:
                        pass
                else:
                    return ProvisionResult(
                        status=ProvisionStatus.FAILED,
                        provider=provider_name,
                        message="Chrome did not start in time",
                    )

                # Step 4: Get the first page tab's WS URL
                tabs_url = f"http://127.0.0.1:{port}/json"
                resp = await client.get(tabs_url, timeout=3)
                tabs = resp.json()
            page_tabs = [t for t in tabs if t.get("type") == "page"]
            if not page_tabs:
                return ProvisionResult(
                    status=ProvisionStatus.FAILED,
                    provider=provider_name,
                    message="No page tabs found in Chrome",
                )
            ws_url = page_tabs[0].get("webSocketDebuggerUrl", "")
            if not ws_url:
                return ProvisionResult(
                    status=ProvisionStatus.FAILED,
                    provider=provider_name,
                    message="No WebSocket URL found for tab",
                )

            # Step 5: Connect CDP and run provisioning
            from cdp import CDP
            cdp = CDP(ws_url)
            await cdp.connect()

            provider._cdp = cdp
            provider._local_port = port

            result = await asyncio.wait_for(
                provider.provision("local", "127.0.0.1", port),
                timeout=PROVISION_TIMEOUT,
            )

            # Store key on success (unless caller wants to defer)
            if store_key and result.api_key and result.status in (ProvisionStatus.SUCCESS, ProvisionStatus.ALREADY_EXISTS):
                store_provider_key(user_id, provider_name, result.api_key)
                print(f"[provision:local] Stored {provider_name} key for user {user_id}")

            return result

        except asyncio.TimeoutError:
            return ProvisionResult(
                status=ProvisionStatus.TIMEOUT,
                provider=provider_name,
                message=f"Local provisioning timed out after {PROVISION_TIMEOUT}s",
            )
        except Exception as e:
            return ProvisionResult(
                status=ProvisionStatus.FAILED,
                provider=provider_name,
                message=f"Local provisioning error: {e}",
            )
        finally:
            # Reset provider state
            provider._cdp = None
            provider._local_port = None
            # Kill Chrome
            if chrome_proc:
                try:
                    chrome_proc.terminate()
                    chrome_proc.wait(timeout=5)
                except Exception:
                    try:
                        chrome_proc.kill()
                    except Exception:
                        pass
            # Clean up temp profile dir (in executor to avoid blocking the event loop)
            if os.path.isdir(temp_dir):
                try:
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(None, shutil.rmtree, temp_dir)
                    print(f"[provision:local] Cleaned up {temp_dir}")
                except Exception as e:
                    print(f"[provision:local] Warning: failed to clean up {temp_dir}: {e}")
