#!/bin/bash
# WireGuard failover: when the VPN peer routing all traffic goes down,
# fall back to EC2 native internet. Restores VPN when peer comes back.
#
# Install via setup-wireguard.sh or manually:
#   sudo cp wg-failover.sh /usr/local/bin/
#   sudo cp wg-failover.{service,timer} /etc/systemd/system/
#   sudo systemctl enable --now wg-failover.timer
#
# Logs: journalctl -t wg-failover

set -euo pipefail

IFACE="${WG_IFACE:-wg0}"
STALE_THRESHOLD="${WG_STALE_THRESHOLD:-180}"  # seconds with no handshake = dead
STATE_FILE="/run/wg-failover-state"

# Detect default gateway (non-WG interface)
NET_IF=$(/sbin/ip route show table main default 2>/dev/null | awk '{print $5; exit}')
GW=$(/sbin/ip route show table main default dev "$NET_IF" 2>/dev/null | awk '{print $3; exit}')

if [ -z "$NET_IF" ] || [ -z "$GW" ]; then
    logger -t wg-failover "ERROR: cannot detect default gateway on main table"
    exit 1
fi

# Find the peer that owns 0.0.0.0/0 (the one routing all traffic)
VPN_PEER=$(wg show "$IFACE" allowed-ips 2>/dev/null \
    | awk '$2 ~ /0\.0\.0\.0\/0/ {print $1; exit}')

if [ -z "$VPN_PEER" ]; then
    # No peer with 0.0.0.0/0 — nothing to failover
    exit 0
fi

current_state=$(cat "$STATE_FILE" 2>/dev/null || echo "vpn")

# Get peer's latest handshake (epoch seconds, 0 if never)
latest=$(wg show "$IFACE" latest-handshakes 2>/dev/null \
    | awk -v pk="$VPN_PEER" '$1==pk {print $2}')

if [ -z "$latest" ] || [ "$latest" = "0" ]; then
    age=999999
else
    now=$(date +%s)
    age=$((now - latest))
fi

# Find the WG routing table (from ip rules, wg-quick uses fwmark)
WG_TABLE=$(ip rule list 2>/dev/null \
    | awk '/fwmark.*lookup/ {print $NF; exit}')
WG_TABLE="${WG_TABLE:-51820}"

if [ "$age" -gt "$STALE_THRESHOLD" ]; then
    if [ "$current_state" != "direct" ]; then
        logger -t wg-failover "VPN peer stale (${age}s), failing over to direct internet via $NET_IF"
        ip route replace default via "$GW" dev "$NET_IF" table "$WG_TABLE"
        # Disable WG catch-all DNS so system DNS takes over
        resolvectl domain "$IFACE" "" 2>/dev/null || true
        resolvectl default-route "$IFACE" false 2>/dev/null || true
        echo "direct" > "$STATE_FILE"
    fi
else
    if [ "$current_state" != "vpn" ]; then
        logger -t wg-failover "VPN peer alive (${age}s), restoring VPN routing"
        ip route replace default dev "$IFACE" table "$WG_TABLE"
        # Restore WG catch-all DNS
        resolvectl domain "$IFACE" "~." 2>/dev/null || true
        resolvectl default-route "$IFACE" true 2>/dev/null || true
        echo "vpn" > "$STATE_FILE"
    fi
fi
