"""Installer and trial onboarding handlers extracted from web.py."""

from __future__ import annotations

from aiohttp import web


from web_app.core import get_core as _core


async def handle_public_install_script(request: web.Request) -> web.Response:
    """GET /install.sh — public, no-auth install script with browser claim flow."""
    core = _core()
    from agent_package import _generate_public_install_script

    base_url = core._public_base_url(request)
    script = _generate_public_install_script(base_url)
    core._track_event(
        request,
        "public_install_script_served",
        route="/install.sh",
        route_intended="/install",
        route_effective="/install.sh",
        cta_id="public_install_sh",
        source="web",
        status_code=200,
    )
    return web.Response(text=script, content_type="text/plain")


async def handle_download_agent(request: web.Request) -> web.Response:
    """GET /web/download-agent — download agent ZIP package."""
    core = _core()
    install_token = core._request_install_token(request)
    auth_info = None
    token_info = None
    if install_token:
        token_info = core._auth.validate_install_token(install_token, consume=False)
        if not token_info:
            return web.json_response({"error": "Invalid or expired install token"}, status=401)
    else:
        auth_info = core._authenticate(request)
        if not auth_info:
            return web.json_response({"error": "Not authenticated"}, status=401)
        install_token = core._auth.create_install_token(auth_info["user_id"], auth_info["key"])

    from agent_package import build_agent_zip

    zip_bytes = build_agent_zip(
        api_key="",
        relay_host="api.unchainedsky.com",
        install_token=install_token,
    )
    user_id = token_info.get("user_id", "") if token_info else auth_info.get("user_id", "")
    user_type = auth_info.get("user_type", "") if auth_info else ""
    core._track_event(
        request,
        "agent_zip_download_start",
        route="/web/download-agent",
        route_intended="/install",
        route_effective="/web/download-agent",
        cta_id="download_agent_zip",
        user_id=user_id,
        user_type=user_type,
        source="web",
        status_code=200,
        meta={"channel": "install_token" if token_info else "session"},
    )
    return web.Response(
        body=zip_bytes,
        content_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=unchained-agent.zip"},
    )


async def handle_download_installer(request: web.Request) -> web.Response:
    """GET /web/download-installer — download native installer binary."""
    core = _core()
    platform_raw = request.query.get("os", "mac")
    platform = core._normalize_installer_platform(platform_raw)
    if not platform:
        return web.json_response({"error": "Unsupported os. Use mac or windows"}, status=400)

    install_token = core._request_install_token(request)
    auth_info = None
    token_info = None
    if install_token:
        token_info = core._auth.validate_install_token(install_token, consume=False)
        if not token_info:
            return web.json_response({"error": "Invalid or expired install token"}, status=401)
    else:
        auth_info = core._authenticate(request)
        if not auth_info:
            return web.json_response({"error": "Not authenticated"}, status=401)

    native_path = core._native_installer_path(platform)
    if native_path:
        user_id = token_info.get("user_id", "") if token_info else auth_info.get("user_id", "")
        user_type = auth_info.get("user_type", "") if auth_info else ""
        core._track_event(
            request,
            "installer_download_start",
            route="/web/download-installer",
            route_intended="/install",
            route_effective="/web/download-installer",
            cta_id=f"download_installer_{platform}",
            user_id=user_id,
            user_type=user_type,
            source="web",
            status_code=200,
            meta={
                "platform": platform,
                "artifact": "native",
                "filename": native_path.name,
                "channel": "install_token" if token_info else "session",
            },
        )
        return web.FileResponse(
            path=native_path,
            headers={"Content-Disposition": f'attachment; filename="{native_path.name}"'},
        )

    if not core._ALLOW_SCRIPT_INSTALLER_FALLBACK:
        expected_assets = core._native_installer_candidates(platform)
        user_id = token_info.get("user_id", "") if token_info else auth_info.get("user_id", "")
        core._track_event(
            request,
            "installer_download_fail",
            route="/web/download-installer",
            route_intended="/install",
            route_effective="/web/download-installer",
            cta_id=f"download_installer_{platform}",
            error_code="native_installer_missing",
            user_id=user_id,
            source="web",
            status_code=503,
            meta={"platform": platform, "expected_assets": expected_assets},
        )
        return web.json_response(
            {
                "error": "Native installer is not configured for this OS.",
                "os": platform,
                "expected_asset": expected_assets[0] if expected_assets else None,
                "expected_assets": expected_assets,
            },
            status=503,
        )

    # Optional compatibility fallback: return shell/PowerShell script installers
    # if native artifacts are not available and fallback is explicitly enabled.
    if not install_token:
        install_token = core._auth.create_install_token(auth_info["user_id"], auth_info["key"])
    from agent_package import generate_platform_installer_script

    base_url = core._public_base_url(request)
    script = generate_platform_installer_script(
        platform=platform,
        install_token=install_token,
        relay_host="api.unchainedsky.com",
        base_url=base_url,
    )
    filename = (
        "unchained-installer-windows.ps1"
        if platform == "windows"
        else "unchained-installer-mac.sh"
    )
    user_id = token_info.get("user_id", "") if token_info else auth_info.get("user_id", "")
    user_type = auth_info.get("user_type", "") if auth_info else ""
    core._track_event(
        request,
        "installer_download_start",
        route="/web/download-installer",
        route_intended="/install",
        route_effective="/web/download-installer",
        cta_id=f"download_installer_{platform}",
        user_id=user_id,
        user_type=user_type,
        source="web",
        status_code=200,
        meta={
            "platform": platform,
            "artifact": "script_fallback",
            "filename": filename,
            "channel": "install_token" if token_info else "session",
        },
    )
    return web.Response(
        text=script,
        content_type="text/plain",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


async def handle_install_token(request: web.Request) -> web.Response:
    """POST /web/install-token — create a short-lived install token for installers."""
    core = _core()
    auth_info = core._authenticate(request)
    if not auth_info:
        return web.json_response({"error": "Not authenticated"}, status=401)

    token = core._auth.create_install_token(auth_info["user_id"], auth_info["key"])
    base_url = core._public_base_url(request)
    curl_command = f'curl -sSL -H "X-Install-Token: {token}" "{base_url}/install/script" | bash'
    powershell_command = (
        "powershell -ExecutionPolicy Bypass -Command "
        f"\"$h=@{{'X-Install-Token'='{token}'}}; "
        f"Invoke-Expression ((Invoke-WebRequest -UseBasicParsing -Headers $h "
        f"'{base_url}/install/windows/script').Content)\""
    )
    mac_native = core._native_installer_path("mac") is not None
    windows_native = core._native_installer_path("windows") is not None
    core._track_event(
        request,
        "install_token_issued",
        route="/web/install-token",
        route_intended="/install",
        route_effective="/web/install-token",
        cta_id="install_token",
        user_id=auth_info.get("user_id", ""),
        user_type=auth_info.get("user_type", ""),
        source="web",
        status_code=200,
        meta={"native_available": {"mac": mac_native, "windows": windows_native}},
    )
    return web.json_response(
        {
            "curl_command": curl_command,
            "powershell_command": powershell_command,
            "mac_installer_url": f"{base_url}/web/download-installer?os=mac",
            "windows_installer_url": f"{base_url}/web/download-installer?os=windows",
            "zip_url": f"{base_url}/web/download-agent",
            "native_available": {"mac": mac_native, "windows": windows_native},
            "expires_in": 900,
        }
    )


async def handle_install_script(request: web.Request) -> web.Response:
    """GET /install/script or /install/{token} — serve personalized bash install script."""
    core = _core()
    token = core._request_install_token(request) or request.match_info.get("token", "")
    token = token.strip()
    token_info = core._auth.validate_install_token(token, consume=False)
    if not token_info:
        # Return a bash-friendly error message
        return web.Response(
            text='echo "ERROR: Install link expired or already used. '
            'Get a new one from https://api.unchainedsky.com/chat"\nexit 1\n',
            content_type="text/plain",
        )

    from agent_package import _generate_install_script

    base_url = core._public_base_url(request)
    script = _generate_install_script(
        install_token=token,
        relay_host="api.unchainedsky.com",
        base_url=base_url,
    )
    return web.Response(text=script, content_type="text/plain")


async def handle_install_script_windows(request: web.Request) -> web.Response:
    """GET /install/windows/script or /install/windows/{token} — PowerShell script."""
    core = _core()
    token = core._request_install_token(request) or request.match_info.get("token", "")
    token = token.strip()
    token_info = core._auth.validate_install_token(token, consume=False)
    if not token_info:
        return web.Response(
            text='Write-Error "Install link expired or already used. Get a new one from https://api.unchainedsky.com/chat"\nexit 1\n',
            content_type="text/plain",
        )

    from agent_package import _generate_windows_install_script

    base_url = core._public_base_url(request)
    script = _generate_windows_install_script(
        install_token=token,
        relay_host="api.unchainedsky.com",
        base_url=base_url,
    )
    return web.Response(text=script, content_type="text/plain")


async def handle_install_claim_page(request: web.Request) -> web.Response:
    """GET /install/claim/{claim_id} — approval page opened by native installer."""
    core = _core()
    claim_id = str(request.match_info.get("claim_id", "")).strip().lower()
    if not core._is_valid_claim_id(claim_id):
        return web.Response(text="Invalid install claim id.", status=400)
    html = core.INSTALL_CLAIM_HTML.replace("__CLAIM_ID__", claim_id)
    return web.Response(text=html, content_type="text/html")


async def handle_install_claim_start(request: web.Request) -> web.Response:
    """POST /web/install/claim/start — create a pending claim for installer auth."""
    core = _core()
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)
    claim_id = str(body.get("claim_id", "")).strip().lower()
    claim_secret = str(body.get("claim_secret", "")).strip()
    if not core._is_valid_claim_id(claim_id):
        return web.json_response({"error": "claim_id must be 32 hex chars"}, status=400)
    if len(claim_secret) < 24:
        return web.json_response({"error": "claim_secret too short"}, status=400)

    now = core.time.time()
    source = core._request_source_ip(request)
    with core._install_claims_lock:
        core._cleanup_install_claims(now)
        core._cleanup_install_claim_start_hits(now)
        if len(core._install_claims) >= core._INSTALL_CLAIM_MAX_PENDING:
            return web.json_response(
                {"error": "Too many pending install claims. Retry shortly."}, status=503
            )
        hits = core._install_claim_start_hits.get(source, [])
        if len(hits) >= core._INSTALL_CLAIM_START_MAX_PER_IP:
            return web.json_response({"error": "Too many claim attempts. Retry shortly."}, status=429)
        hits.append(now)
        core._install_claim_start_hits[source] = hits
        existing = core._install_claims.get(claim_id)
        if existing and not core.hmac.compare_digest(existing.get("secret", ""), claim_secret):
            return web.json_response({"error": "claim_id already exists"}, status=409)
        core._install_claims[claim_id] = {
            "secret": claim_secret,
            "created_at": now,
            "expires_at": now + core._INSTALL_CLAIM_TTL,
            "install_token": "",
        }
    core._track_event(
        request,
        "install_claim_start",
        route="/web/install/claim/start",
        route_intended="/install",
        route_effective="/web/install/claim/start",
        cta_id="install_claim_start",
        source="web",
        status_code=200,
        meta={"claim_id_prefix": claim_id[:6]},
    )
    return web.json_response(
        {"status": "pending", "claim_id": claim_id, "expires_in": core._INSTALL_CLAIM_TTL}
    )


async def handle_install_claim_approve(request: web.Request) -> web.Response:
    """POST /web/install/claim/approve — approve pending installer claim."""
    core = _core()
    auth_info = core._authenticate(request)
    if not auth_info:
        return web.json_response({"error": "Not authenticated"}, status=401)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)
    claim_id = str(body.get("claim_id", "")).strip().lower()
    if not core._is_valid_claim_id(claim_id):
        return web.json_response({"error": "claim_id must be 32 hex chars"}, status=400)

    now = core.time.time()
    with core._install_claims_lock:
        core._cleanup_install_claims(now)
        claim = core._install_claims.get(claim_id)
        if not claim:
            return web.json_response({"error": "Claim expired or not found"}, status=404)
        token = claim.get("install_token") or core._auth.create_install_token(
            auth_info["user_id"], auth_info["key"]
        )
        claim["install_token"] = token
        claim["approved_at"] = now
        claim["approved_user_id"] = auth_info["user_id"]
        claim["expires_at"] = min(
            claim.get("expires_at", now + core._INSTALL_CLAIM_TTL),
            now + core._INSTALL_CLAIM_TTL,
        )
    core._track_event(
        request,
        "install_claim_approve",
        route="/web/install/claim/approve",
        route_intended="/install",
        route_effective="/web/install/claim/approve",
        cta_id="install_claim_approve",
        user_id=auth_info.get("user_id", ""),
        user_type=auth_info.get("user_type", ""),
        source="web",
        status_code=200,
        meta={"claim_id_prefix": claim_id[:6]},
    )
    return web.json_response({"status": "approved"})


async def handle_install_claim_poll(request: web.Request) -> web.Response:
    """POST /web/install/claim/poll — poll claim status and retrieve token."""
    core = _core()
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)
    claim_id = str(body.get("claim_id", "")).strip().lower()
    claim_secret = str(body.get("claim_secret", "")).strip()
    if not core._is_valid_claim_id(claim_id):
        return web.json_response({"error": "claim_id must be 32 hex chars"}, status=400)
    if not claim_secret:
        return web.json_response({"error": "claim_secret required"}, status=400)

    now = core.time.time()
    with core._install_claims_lock:
        core._cleanup_install_claims(now)
        claim = core._install_claims.get(claim_id)
        if not claim:
            return web.json_response({"status": "expired"}, status=404)
        if not core.hmac.compare_digest(claim.get("secret", ""), claim_secret):
            return web.json_response({"error": "Invalid claim secret"}, status=401)
        install_token = str(claim.get("install_token", "")).strip()
        if install_token:
            core._install_claims.pop(claim_id, None)
            core._track_event(
                request,
                "install_claim_poll_approved",
                route="/web/install/claim/poll",
                route_intended="/install",
                route_effective="/web/install/claim/poll",
                cta_id="install_claim_poll",
                source="web",
                status_code=200,
                meta={"claim_id_prefix": claim_id[:6]},
            )
            return web.json_response({"status": "approved", "install_token": install_token})
        expires_at = float(claim.get("expires_at", now))
    return web.json_response({"status": "pending", "expires_in": max(0, int(expires_at - now))})


async def handle_install_bootstrap(request: web.Request) -> web.Response:
    """POST /web/install/bootstrap — exchange short-lived install token for API key."""
    core = _core()
    try:
        body = await request.json()
    except Exception:
        core._track_event(
            request,
            "install_bootstrap_fail",
            route="/web/install/bootstrap",
            route_intended="/install",
            route_effective="/web/install/bootstrap",
            error_code="invalid_json_body",
            source="web",
            status_code=400,
        )
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    token = str(body.get("token", "")).strip()
    if not token:
        core._track_event(
            request,
            "install_bootstrap_fail",
            route="/web/install/bootstrap",
            route_intended="/install",
            route_effective="/web/install/bootstrap",
            error_code="missing_token",
            source="web",
            status_code=400,
        )
        return web.json_response({"error": "token required"}, status=400)

    token_info = core._auth.validate_install_token(token, consume=True)
    if not token_info:
        core._track_event(
            request,
            "install_bootstrap_fail",
            route="/web/install/bootstrap",
            route_intended="/install",
            route_effective="/web/install/bootstrap",
            error_code="invalid_or_expired_token",
            source="web",
            status_code=401,
        )
        return web.json_response({"error": "Invalid or expired install token"}, status=401)

    core._track_event(
        request,
        "install_bootstrap_success",
        route="/web/install/bootstrap",
        route_intended="/install",
        route_effective="/web/install/bootstrap",
        user_id=token_info.get("user_id", ""),
        source="web",
        status_code=200,
    )
    return web.json_response({"api_key": token_info["api_key"]})


async def handle_trial_connector(request: web.Request) -> web.Response:
    """GET /trial/connector — serve chrome_bridge.py for trial users."""
    core = _core()
    bridge_path = core.os.path.join(
        core.os.path.dirname(core.os.path.abspath(core.__file__)), "chrome_bridge.py"
    )
    try:
        with open(bridge_path, "rb") as f:
            content = f.read()
    except FileNotFoundError:
        return web.Response(text="# chrome_bridge.py not found\n", content_type="text/plain")
    return web.Response(body=content, content_type="text/plain")


async def handle_trial_token(request: web.Request) -> web.Response:
    """POST /trial/token — create a short-lived trial connector install token."""
    core = _core()
    auth_info = core._authenticate(request)
    if not auth_info:
        return web.json_response({"error": "Not authenticated"}, status=401)
    token = core._auth.create_install_token(auth_info["user_id"], auth_info["key"])
    base_url = core._public_base_url(request)
    powershell_command = (
        "powershell -ExecutionPolicy Bypass -Command "
        f"\"$h=@{{'X-Install-Token'='{token}'}}; "
        f"Invoke-Expression ((Invoke-WebRequest -UseBasicParsing -Headers $h "
        f"'{base_url}/trial/windows/script').Content)\""
    )
    return web.json_response(
        {
            "curl_command": f'curl -sSL -H "X-Install-Token: {token}" "{base_url}/trial/script" | bash',
            "powershell_command": powershell_command,
        }
    )


async def handle_trial_script(request: web.Request) -> web.Response:
    """GET /trial/script or /trial/{token} — serve minimal bash trial connector script."""
    core = _core()
    token = core._request_install_token(request) or request.match_info.get("token", "")
    token = token.strip()
    token_info = core._auth.validate_install_token(token, consume=False)
    if not token_info:
        return web.Response(
            text='echo "ERROR: Link expired or already used. Get a new one from https://api.unchainedsky.com/chat"\nexit 1\n',
            content_type="text/plain",
        )
    base_url = core._public_base_url(request)
    relay_url = core._public_relay_url(request)
    script = f"""#!/bin/bash
# Unchained Trial — Browser Connector
# Connects your Chrome to the Unchained AI agent
# Only requires: Python 3 and curl
set -e

INSTALL_TOKEN="{token}"
RELAY="{relay_url}"
DIR="$HOME/.unchained"
BRIDGE="$DIR/chrome_bridge.py"
BOOTSTRAP_URL="{base_url}/web/install/bootstrap"

echo ""
echo "  Unchained — Connecting your browser..."
echo ""

# Check Python 3
if ! command -v python3 &>/dev/null; then
  echo "  Error: Python 3 not found. Install from https://python.org"
  exit 1
fi

# Stop any existing connector
if [ -f "$DIR/.agent_pid" ]; then
  OLD_PID=$(cat "$DIR/.agent_pid" 2>/dev/null)
  if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
    echo "  Stopping previous connector..."
    kill "$OLD_PID" 2>/dev/null || true
    sleep 1
  fi
fi

# Install websockets (the only dependency)
if ! python3 -c "import websockets" 2>/dev/null; then
  echo "  Installing websockets..."
  python3 -m pip install -q websockets
fi

# Download the connector
mkdir -p "$DIR"
echo "  Downloading connector..."
curl -sSL "{base_url}/trial/connector" -o "$BRIDGE"

# Exchange the short-lived install token for the real API key.
PAYLOAD=$(TOKEN="$INSTALL_TOKEN" python3 - <<'PY'
import json, os
print(json.dumps({{"token": os.environ["TOKEN"]}}))
PY
)
BOOTSTRAP=$(curl -sf -H "Content-Type: application/json" -d "$PAYLOAD" "$BOOTSTRAP_URL") || {{
  echo "  Error: install token exchange failed"
  exit 1
}}
API_KEY=$(printf '%s' "$BOOTSTRAP" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("api_key",""))' 2>/dev/null || true)
if [ -z "$API_KEY" ]; then
  echo "  Error: invalid install token response"
  exit 1
fi

# Launch Chrome + connector in background
echo "  Starting..."
UNCHAINED_API_KEY="$API_KEY" nohup python3 "$BRIDGE" start --relay "$RELAY" \\
  > "$DIR/connector.log" 2>&1 &
sleep 4

echo ""
echo "  Your browser is connected!"
echo "  An Unchained Chrome window will open — that's where the agent browses."
echo "  Screenshots of each page will appear in the chat so you can see what's happening."
echo ""
echo "  Open https://unchainedsky.com/chat, pick Trinity or StepFun 3.5 Flash, and start chatting."
echo ""
echo "  Stop:  python3 ~/.unchained/chrome_bridge.py stop"
echo "  Logs:  tail -f ~/.unchained/connector.log"
echo ""
"""
    return web.Response(text=script, content_type="text/plain")


async def handle_trial_script_windows(request: web.Request) -> web.Response:
    """GET /trial/windows/script — PowerShell trial connector script for Windows."""
    core = _core()
    token = core._request_install_token(request) or request.match_info.get("token", "")
    token = token.strip()
    token_info = core._auth.validate_install_token(token, consume=False)
    if not token_info:
        return web.Response(
            text='Write-Error "Install link expired or already used. Get a new one from https://api.unchainedsky.com/chat"\nexit 1\n',
            content_type="text/plain",
        )
    base_url = core._public_base_url(request)
    relay_url = core._public_relay_url(request)
    script = f"""# Unchained Trial - Browser Connector (Windows)
# Connects your Chrome to the Unchained AI agent
# Requires: Python 3.8+ and PowerShell
$ErrorActionPreference = "Stop"

$INSTALL_TOKEN = "{token}"
$RELAY = "{relay_url}"
$DIR = "$env:USERPROFILE\\.unchained"
$BRIDGE = "$DIR\\chrome_bridge.py"
$BOOTSTRAP_URL = "{base_url}/web/install/bootstrap"

Write-Host ""
Write-Host "  Unchained - Connecting your browser..."
Write-Host ""

# --- Robust Python 3.8+ detection (py -3, python, python3; skip WindowsApps shim) ---
function Test-PythonCommand([string]$Source, [string[]]$Prefix) {{
  if ([string]::IsNullOrWhiteSpace($Source)) {{ return $false }}
  $invokeArgs = @()
  if ($Prefix) {{ $invokeArgs += $Prefix }}
  $invokeArgs += @("-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)")
  try {{
    & $Source @invokeArgs *> $null
    return ($LASTEXITCODE -eq 0)
  }} catch {{
    return $false
  }}
}}

function Find-PythonCommand() {{
  $pyCmd = Get-Command py -ErrorAction SilentlyContinue
  if ($pyCmd) {{
    $pySource = [string]$pyCmd.Source
    if (Test-PythonCommand $pySource @("-3")) {{
      return @{{ Source = $pySource; Prefix = @("-3") }}
    }}
  }}
  foreach ($name in @("python", "python3")) {{
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if (-not $cmd) {{ continue }}
    $source = [string]$cmd.Source
    if ([string]::IsNullOrWhiteSpace($source)) {{ continue }}
    if ($source -like "*WindowsApps*") {{ continue }}
    if (Test-PythonCommand $source @()) {{
      return @{{ Source = $source; Prefix = @() }}
    }}
  }}
  return $null
}}

$pyInfo = Find-PythonCommand
if (-not $pyInfo) {{
    Write-Host "  Error: Python 3.8+ not found. Install from https://python.org"
    exit 1
}}
$pySrc = $pyInfo.Source
$pyPrefix = $pyInfo.Prefix

function Invoke-Python([string[]]$PyArgs) {{
    $all = @()
    if ($pyPrefix) {{ $all += $pyPrefix }}
    $all += $PyArgs
    & $pySrc @all
}}

# --- Stop any existing connector ---
$pidFile = Join-Path $DIR ".agent_pid"
if (Test-Path $pidFile) {{
    $oldPid = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1).Trim()
    if ($oldPid) {{
        try {{
            $proc = Get-Process -Id ([int]$oldPid) -ErrorAction SilentlyContinue
            if ($proc) {{
                Write-Host "  Stopping previous connector (PID $oldPid)..."
                Stop-Process -Id ([int]$oldPid) -Force -ErrorAction SilentlyContinue
                Start-Sleep -Seconds 1
            }}
        }} catch {{}}
    }}
}}

# Install websockets (the only dependency)
Invoke-Python @("-c", "import websockets") 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {{
    Write-Host "  Installing websockets..."
    Invoke-Python @("-m", "pip", "install", "-q", "websockets")
}}

# Create directory and download the connector
if (-not (Test-Path $DIR)) {{ New-Item -ItemType Directory -Path $DIR -Force | Out-Null }}
Write-Host "  Downloading connector..."
Invoke-WebRequest -UseBasicParsing -Uri "{base_url}/trial/connector" -OutFile $BRIDGE

# Exchange the short-lived install token for the real API key
$body = ConvertTo-Json @{{token = $INSTALL_TOKEN}}
try {{
    $resp = Invoke-RestMethod -Uri $BOOTSTRAP_URL -Method Post -ContentType "application/json" -Body $body
}} catch {{
    Write-Host "  Error: install token exchange failed"
    exit 1
}}
$API_KEY = $resp.api_key
if (-not $API_KEY) {{
    Write-Host "  Error: invalid install token response"
    exit 1
}}

# Launch Chrome + connector
Write-Host "  Starting..."
$env:UNCHAINED_API_KEY = $API_KEY
$pyArgs = @()
if ($pyPrefix) {{ $pyArgs += $pyPrefix }}
$pyArgs += @($BRIDGE, "start", "--relay", $RELAY)
Start-Process -NoNewWindow -FilePath $pySrc -ArgumentList $pyArgs -RedirectStandardOutput "$DIR\\connector.log" -RedirectStandardError "$DIR\\connector_err.log"
Start-Sleep -Seconds 4

Write-Host ""
Write-Host "  Your browser is connected!"
Write-Host "  An Unchained Chrome window will open - that's where the agent browses."
Write-Host "  Screenshots of each page will appear in the chat so you can see what's happening."
Write-Host ""
Write-Host "  Open https://unchainedsky.com/chat, pick Trinity or StepFun 3.5 Flash, and start chatting."
Write-Host ""
Write-Host "  Stop:  $pySrc $($pyPrefix -join ' ') $env:USERPROFILE\\.unchained\\chrome_bridge.py stop"
Write-Host "  Logs:  Get-Content -Wait $env:USERPROFILE\\.unchained\\connector.log"
Write-Host ""
"""
    return web.Response(text=script, content_type="text/plain")
