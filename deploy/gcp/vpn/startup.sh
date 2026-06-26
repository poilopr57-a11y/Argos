#!/bin/bash
# Startup script for Argos VPN server on GCP
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y \
    apt-transport-https \
    ca-certificates \
    curl \
    gnupg \
    lsb-release \
    git \
    wireguard \
    wireguard-tools \
    linux-headers-$(uname -r) \
    sqlite3

# Install Docker
curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/debian $(lsb_release -cs) stable" > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin

# Load WireGuard module
modprobe wireguard || true

# Create data directory
mkdir -p /var/lib/argos

# Clone repo (or pull latest)
REPO_DIR=/opt/argos
if [ ! -d "$REPO_DIR/.git" ]; then
    git clone https://github.com/poilopr57-a11y/Argos.git "$REPO_DIR"
fi
cd "$REPO_DIR"
git fetch origin
git reset --hard origin/main

# Build VPN API image with real WireGuard
docker build -f Dockerfile.vpn_api -t argos-vpn-api:latest .

# Stop existing container if any
docker rm -f argos-vpn-api 2>/dev/null || true

# Run container privileged so it can manage wg0 and iptables
docker run -d \
    --name argos-vpn-api \
    --restart unless-stopped \
    --privileged \
    --cap-add NET_ADMIN \
    --cap-add SYS_MODULE \
    -e ARGOS_VPN_BOT_TOKEN="${ARGOS_VPN_BOT_TOKEN}" \
    -e ARGOS_VPN_DRY_RUN=false \
    -e ARGOS_VPN_DB_PATH=/app/data/vpn.db \
    -e ARGOS_VPN_SERVER_IP="${ARGOS_VPN_SERVER_IP}" \
    -e ARGOS_VPN_PORT=51820 \
    -e ARGOS_VPN_WEBAPP_URL="${ARGOS_VPN_WEBAPP_URL}" \
    -v /var/lib/argos:/app/data \
    -p 8004:8004 \
    -p 51820:51820/udp \
    argos-vpn-api:latest

# Install cloudflared if tunnel credentials provided
if [ -f /etc/cloudflared/cert.json ]; then
    curl -L --output /tmp/cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
    dpkg -i /tmp/cloudflared.deb || apt-get install -f -y
    mkdir -p /etc/cloudflared
    cat > /etc/cloudflared/config.yml <<EOF
tunnel: ${CLOUDFLARE_TUNNEL_ID}
credentials-file: /etc/cloudflared/cert.json
ingress:
  - hostname: ${CLOUDFLARE_TUNNEL_HOSTNAME}
    service: http://localhost:8004
  - service: http_status:404
EOF
    systemctl enable --now cloudflared
fi

echo "Argos VPN startup complete"
