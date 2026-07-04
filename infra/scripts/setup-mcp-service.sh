#!/usr/bin/env bash
# One-time setup: install the mcp-server systemd service on a fresh droplet.
#
# Run this ONCE per droplet, by hand, after cloning the repo and before the
# first deploy. It is not part of deploy.yml -- deploy.yml only restarts the
# already-installed service on every push. Re-running this script is safe
# (idempotent: re-templating an unchanged unit file and re-enabling an
# already-enabled service are both no-ops), but it is not meant to run
# automatically.
#
# Usage (from the repo root, on the droplet):
#   ./infra/scripts/setup-mcp-service.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
UV_BIN="$(command -v uv)"

if [ -z "$UV_BIN" ]; then
  echo "ERROR: uv not found on PATH. Install uv first (see README)." >&2
  exit 1
fi

echo "Templating mcp-server.service (WorkingDirectory=${REPO_ROOT}, uv=${UV_BIN}) ..."
sed "s|__WORKDIR__|${REPO_ROOT}|g; s|__UV_BIN__|${UV_BIN}|g" \
  "${REPO_ROOT}/infra/systemd/mcp-server.service" | sudo tee /etc/systemd/system/mcp-server.service > /dev/null

echo "Reloading systemd and enabling mcp-server ..."
sudo systemctl daemon-reload
sudo systemctl enable mcp-server
sudo systemctl restart mcp-server

echo "Done. Check status with: sudo systemctl status mcp-server"
