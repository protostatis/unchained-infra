#!/bin/bash
# Setup WireGuard VPN on EC2 for headless Chrome IP masking
# Reads configuration from environment variables or .env file
#
# Required env vars:
#   WG_PRIVATE_KEY  - WireGuard private key
#   WG_ADDRESS      - VPN address (e.g., 10.x.x.x/32)
#   WG_DNS          - DNS server (e.g., 10.64.0.1)
#   WG_PEER_PUBKEY  - Server public key
#   WG_ENDPOINT     - Server endpoint (e.g., server_ip:51820)

set -e

# Load WG_* vars from .env (only WG_ lines, to avoid breaking on unquoted values)
ENV_FILE="${1:-.env}"
if [ -f "$ENV_FILE" ]; then
    echo "Loading WG config from $ENV_FILE"
    while IFS= read -r line; do
        key="${line%%=*}"
        value="${line#*=}"
        export "$key=$value"
    done < <(grep '^WG_' "$ENV_FILE")
fi

# Validate required vars
for var in WG_PRIVATE_KEY WG_ADDRESS WG_DNS WG_PEER_PUBKEY WG_ENDPOINT; do
    if [ -z "${!var}" ]; then
        echo "ERROR: $var is not set"
        exit 1
    fi
done

# Install wireguard-tools if needed
if ! command -v wg &> /dev/null; then
    echo "Installing wireguard-tools..."
    sudo yum install -y wireguard-tools
fi

# Detect default network interface, EC2 private IP, and gateway
NET_IF=$(/sbin/ip route show default | awk '{print $5; exit}')
EC2_IP=$(hostname -I | awk '{print $1}')
EC2_GW=$(/sbin/ip route show default dev "$NET_IF" | awk '{print $3}')
echo "Network interface: $NET_IF"
echo "EC2 private IP: $EC2_IP"
echo "EC2 gateway: $EC2_GW"

# Write WireGuard config
echo "Writing /etc/wireguard/wg0.conf..."
sudo bash -c "cat > /etc/wireguard/wg0.conf << EOF
[Interface]
PrivateKey = $WG_PRIVATE_KEY
Address = $WG_ADDRESS
DNS = $WG_DNS
MTU = 1420
PostUp = /sbin/ip rule add from $EC2_IP table main priority 90; /sbin/iptables -t mangle -A PREROUTING -i $NET_IF -m conntrack --ctstate NEW -j CONNMARK --set-mark 0xca6c; /sbin/iptables -t mangle -A PREROUTING -j CONNMARK --restore-mark --nfmask 0xffffffff --ctmask 0xffffffff; /sbin/ip route add 140.82.112.0/20 via $EC2_GW dev $NET_IF; /sbin/ip route add 185.199.108.0/22 via $EC2_GW dev $NET_IF; /sbin/ip route add 169.254.169.254 dev $NET_IF
PostDown = /sbin/ip rule del from $EC2_IP table main priority 90; /sbin/iptables -t mangle -D PREROUTING -i $NET_IF -m conntrack --ctstate NEW -j CONNMARK --set-mark 0xca6c; /sbin/iptables -t mangle -D PREROUTING -j CONNMARK --restore-mark --nfmask 0xffffffff --ctmask 0xffffffff; /sbin/ip route del 140.82.112.0/20 dev $NET_IF; /sbin/ip route del 185.199.108.0/22 dev $NET_IF; /sbin/ip route del 169.254.169.254 dev $NET_IF

[Peer]
PublicKey = $WG_PEER_PUBKEY
Endpoint = $WG_ENDPOINT
AllowedIPs = 0.0.0.0/0
EOF"

sudo chmod 600 /etc/wireguard/wg0.conf

# Stop existing VPN if running
sudo wg-quick down wg0 2>/dev/null || true

# Start VPN
echo "Starting WireGuard..."
sudo wg-quick up wg0

# Enable on boot
sudo systemctl enable wg-quick@wg0 2>/dev/null || true

# Install failover script + systemd units
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$SCRIPT_DIR/wg-failover.sh" ]; then
    echo "Installing WireGuard failover..."
    sudo cp "$SCRIPT_DIR/wg-failover.sh" /usr/local/bin/wg-failover.sh
    sudo chmod +x /usr/local/bin/wg-failover.sh
    sudo cp "$SCRIPT_DIR/wg-failover.service" /etc/systemd/system/
    sudo cp "$SCRIPT_DIR/wg-failover.timer" /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now wg-failover.timer
    echo "Failover timer active (checks every 30s)."
fi

# Verify
echo ""
echo "=== Verification ==="
VPN_IP=$(curl -s --max-time 10 https://httpbin.org/ip | grep -oP '"origin": "\K[^"]+' 2>/dev/null || echo "failed")
echo "VPN IP: $VPN_IP"
echo "EC2 IP: $EC2_IP"

if [ "$VPN_IP" != "failed" ] && [ "$VPN_IP" != "$EC2_IP" ]; then
    echo "WireGuard VPN is active."
else
    echo "WARNING: VPN may not be working. Check 'sudo wg show'."
fi
