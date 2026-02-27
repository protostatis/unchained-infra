#!/bin/bash
# stream_entry.sh — Docker entrypoint for 24/7 autonomous streaming
# Starts Xvfb, PulseAudio, Chromium, waits for CDP, then runs agent_stream.py

# Clean up stale lock files from previous runs (container restart)
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99

# Start D-Bus system bus (needed by PulseAudio and Chromium)
mkdir -p /run/dbus
dbus-daemon --system --nofork &
DBUS_PID=$!
sleep 1

echo "[entry] Starting Xvfb on :99 (1920x1080x24)…"
Xvfb :99 -screen 0 1920x1080x24 -ac +extension GLX +render -noreset &
XVFB_PID=$!
export DISPLAY=:99
sleep 2

# Verify Xvfb is actually running
if ! kill -0 $XVFB_PID 2>/dev/null; then
    echo "[entry] ERROR: Xvfb failed to start"
    exit 1
fi
echo "[entry] Xvfb running (PID $XVFB_PID)"

echo "[entry] Starting PulseAudio (virtual audio sink)…"
# Kill any stale PulseAudio
pulseaudio --kill 2>/dev/null || true
rm -rf /var/run/pulse/*.pid /run/pulse/*.pid /tmp/pulse-*
sleep 0.5

# Add root to pulse-access group (needed for --system mode)
groupadd -f pulse-access 2>/dev/null || true
usermod -aG pulse-access root 2>/dev/null || true
usermod -aG pulse pulse 2>/dev/null || true
mkdir -p /var/run/pulse
chown pulse:pulse-access /var/run/pulse
chmod 775 /var/run/pulse

# Write system-wide PulseAudio config
mkdir -p /etc/pulse
cat > /etc/pulse/system.pa << 'PAEOF'
load-module module-native-protocol-unix auth-anonymous=1 socket=/var/run/pulse/native
load-module module-null-sink sink_name=virtual_speaker sink_properties=device.description="VirtualSpeaker"
set-default-sink virtual_speaker
load-module module-always-sink
PAEOF

# Start PulseAudio in system mode
pulseaudio \
    --system \
    --disallow-exit \
    --exit-idle-time=-1 \
    --daemonize=yes 2>/dev/null || true
sleep 1

# Set env so all clients find the socket
export PULSE_SERVER=unix:/var/run/pulse/native

# Verify PulseAudio
if pactl info >/dev/null 2>&1; then
    echo "[entry] PulseAudio running"
    pactl set-default-sink virtual_speaker 2>/dev/null || true
else
    echo "[entry] WARNING: PulseAudio not responding (FFmpeg audio may fail)"
fi

echo "[entry] Starting Chromium (kiosk mode)…"
CHROME_USER="${CHROME_RUN_USER:-unchained}"
CHROME_HOME="/home/$CHROME_USER"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/10001}"
mkdir -p /tmp/chrome-profile "$XDG_RUNTIME_DIR"
chown -R "$CHROME_USER:$CHROME_USER" /tmp/chrome-profile "$XDG_RUNTIME_DIR"
su -s /bin/bash "$CHROME_USER" -c "
export DISPLAY=':99'
export PULSE_SERVER='$PULSE_SERVER'
export XDG_RUNTIME_DIR='$XDG_RUNTIME_DIR'
export HOME='$CHROME_HOME'
chromium \
    --test-type \
    --disable-dev-shm-usage \
    --disable-gpu \
    --disable-software-rasterizer \
    --remote-debugging-port=9222 \
    --remote-debugging-address=127.0.0.1 \
    --no-first-run \
    --no-default-browser-check \
    --disable-sync \
    --disable-background-networking \
    --disable-default-apps \
    --disable-extensions \
    --disable-infobars \
    --autoplay-policy=no-user-gesture-required \
    --kiosk \
    --window-size=1920,1080 \
    --window-position=0,0 \
    --disk-cache-size=52428800 \
    --user-data-dir=/tmp/chrome-profile \
    'https://www.google.com/maps'
" &
CHROME_PID=$!

# Wait for CDP to be ready (polls /json/version up to 30s)
echo "[entry] Waiting for CDP on :9222…"
for i in $(seq 1 30); do
    if curl -s http://127.0.0.1:9222/json/version > /dev/null 2>&1; then
        echo "[entry] CDP ready after ${i}s"
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo "[entry] ERROR: CDP not ready after 30s"
        # Show Chrome's stderr for debugging
        echo "[entry] Chrome PID $CHROME_PID alive: $(kill -0 $CHROME_PID 2>&1 && echo yes || echo no)"
        exit 1
    fi
    sleep 1
done

# Start x11vnc + noVNC for live preview only when explicitly enabled.
ENABLE_NOVNC="${ENABLE_NOVNC:-0}"
NOVNC_BIND="${NOVNC_BIND:-127.0.0.1}"
NOVNC_PORT="${NOVNC_PORT:-6080}"
if [ "$ENABLE_NOVNC" = "1" ]; then
    if [ -z "${NOVNC_PASSWORD:-}" ]; then
        echo "[entry] ERROR: ENABLE_NOVNC=1 requires NOVNC_PASSWORD"
        exit 1
    fi
    echo "[entry] Starting x11vnc + noVNC on ${NOVNC_BIND}:${NOVNC_PORT}…"
    PASSFILE=/tmp/x11vnc.pass
    x11vnc -storepasswd "$NOVNC_PASSWORD" "$PASSFILE" >/dev/null
    x11vnc -display :99 -rfbauth "$PASSFILE" -listen "$NOVNC_BIND" -rfbport 5900 -shared -forever -noxdamage -noxfixes -noxrecord -bg 2>/dev/null
    NOVNC_DIR=$(find /usr -path "*/novnc*" -name "vnc.html" 2>/dev/null | head -1 | xargs dirname 2>/dev/null)
    if [ -z "$NOVNC_DIR" ]; then
        NOVNC_DIR="/usr/share/novnc"
    fi
    websockify --web "$NOVNC_DIR" "${NOVNC_BIND}:${NOVNC_PORT}" 127.0.0.1:5900 &
    NOVNC_PID=$!
    echo "[entry] noVNC preview available on ${NOVNC_BIND}:${NOVNC_PORT}"
else
    echo "[entry] noVNC disabled (set ENABLE_NOVNC=1 to enable local preview)"
fi

# Give Chromium a moment to finish loading the Maps page
sleep 3

echo "[entry] Starting agent_stream.py --docker…"
exec python agent_stream.py --docker --no-ctl --no-chat "$@"
