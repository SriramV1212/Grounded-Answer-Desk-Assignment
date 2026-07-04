#!/usr/bin/env bash
# One-time setup: install the fastapi systemd service on a fresh droplet.
#
# infra/systemd/fastapi.service is confirmed to match the real unit file
# (checked via `sudo systemctl cat fastapi` against the running droplet).
#
# Run this ONCE per droplet, by hand, after cloning the repo and before the
# first deploy. It is not part of any CI/CD workflow -- automated deploys
# only restart the already-installed service, they never install/enable it.
#
# Usage (from the repo root, on the droplet):
#   ./infra/scripts/setup-fastapi-service.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
UV_BIN="$(command -v uv)"

if [ -z "$UV_BIN" ]; then
  echo "ERROR: uv not found on PATH. Install uv first (see README)." >&2
  exit 1
fi

echo "Templating fastapi.service (WorkingDirectory=${REPO_ROOT}, uv=${UV_BIN}) ..."
sed "s|__WORKDIR__|${REPO_ROOT}|g; s|__UV_BIN__|${UV_BIN}|g" \
  "${REPO_ROOT}/infra/systemd/fastapi.service" | sudo tee /etc/systemd/system/fastapi.service > /dev/null

echo "Reloading systemd and enabling fastapi ..."
sudo systemctl daemon-reload
sudo systemctl enable fastapi
sudo systemctl restart fastapi

echo "Done. Check status with: sudo systemctl status fastapi"
