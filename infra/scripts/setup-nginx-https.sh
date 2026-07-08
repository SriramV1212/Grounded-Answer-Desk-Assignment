#!/usr/bin/env bash
# Optional, Tier 3 setup: put nginx + Let's Encrypt HTTPS in front of the
# fastapi service (localhost:8000), so a browser-hosted HTTPS frontend
# (e.g. the Vercel-deployed Next.js app) can call this backend without
# hitting mixed-content blocking.
#
# This is NOT part of the required Tier 1 redeploy-from-scratch path -- the
# core RAG backend works fine over plain HTTP (curl http://<droplet-ip>:8000/ask).
# HTTPS is only needed to demo the backend behind a live HTTPS frontend.
#
# Usage (from the repo root, on the droplet):
#   ./infra/scripts/setup-nginx-https.sh [domain]
#
# - If you pass a domain you own and have already pointed at this droplet's
#   IP (A record), e.g.:
#     ./infra/scripts/setup-nginx-https.sh api.example.com
#   that domain is used directly.
#
# - If you don't pass anything, this script auto-detects the droplet's
#   public IP and uses a free sslip.io hostname (e.g. 143.198.51.2.sslip.io),
#   which resolves to that IP with zero DNS setup, no domain purchase, and
#   no registration required. Anyone can run this step, not just someone
#   who owns a domain.

set -euo pipefail

DOMAIN="${1:-}"

if [ -z "$DOMAIN" ]; then
  echo "No domain passed -- auto-detecting droplet public IP for a free sslip.io hostname ..."
  PUBLIC_IP="$(curl -s ifconfig.me)"
  if [ -z "$PUBLIC_IP" ]; then
    echo "ERROR: could not auto-detect public IP via ifconfig.me. Pass a domain explicitly instead:" >&2
    echo "  ./infra/scripts/setup-nginx-https.sh <your-domain>" >&2
    exit 1
  fi
  DOMAIN="${PUBLIC_IP}.sslip.io"
  echo
  echo "Using sslip.io fallback hostname: ${DOMAIN}"
  echo "sslip.io is a free public DNS service that resolves <ip>.sslip.io -> <ip>"
  echo "automatically, for any IP. No purchase, registration, or DNS configuration"
  echo "needed -- this hostname is usable immediately."
  echo
else
  echo "Using provided domain: ${DOMAIN}"
  echo "Make sure its A record already points at this droplet's public IP before continuing,"
  echo "or the certbot step below will fail."
  echo
fi

echo "Installing nginx and certbot ..."
sudo apt-get update -y
sudo apt-get install -y nginx certbot python3-certbot-nginx

# Filename is derived from the resolved hostname, not a fixed string -- a
# hardcoded name here ("grounded-answer-desk") previously caused a real
# duplicate `server_name` conflict on the droplet when a hand-written config
# at sites-available/api.sriramv.tech and this script's hardcoded-name output
# both declared `server_name api.sriramv.tech`. nginx didn't error on that,
# it just silently ignored one of the two configs (`nginx: [warn] conflicting
# server name ... ignored`), which would have caused confusing "my edits
# aren't taking effect" bugs later. Keying the filename off $DOMAIN makes one
# hostname map to exactly one config file, so re-running this script against
# the same hostname regenerates (not duplicates) the same file.
NGINX_CONF="/etc/nginx/sites-available/${DOMAIN}"

echo "Writing nginx server block for ${DOMAIN} -> localhost:8000 ..."
sudo tee "$NGINX_CONF" > /dev/null <<EOF
server {
    listen 80;
    server_name ${DOMAIN};

    location / {
        proxy_pass http://localhost:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF

sudo ln -sf "$NGINX_CONF" "/etc/nginx/sites-enabled/${DOMAIN}"

echo "Testing nginx config ..."
sudo nginx -t

echo "Reloading nginx ..."
sudo systemctl reload nginx

echo
echo "=== Interactive step ahead ==="
echo "certbot will now ask for an email address (for renewal notices) and whether"
echo "to redirect HTTP to HTTPS -- answer 'yes'/'2' to redirect when prompted."
echo
sudo certbot --nginx -d "$DOMAIN"

echo
echo "Done. Verify with:"
echo "  curl https://${DOMAIN}/health"
