#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/nurhannetarek13-design/tst-spot-signal.git"
APP_DIR="/opt/tst-spot-signal"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root: sudo bash deploy/bootstrap-oracle.sh"
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl git docker.io docker-compose-v2 openssl
systemctl enable --now docker

if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" fetch origin main
  git -C "$APP_DIR" reset --hard origin/main
else
  git clone "$REPO_URL" "$APP_DIR"
fi

cd "$APP_DIR"

if [ ! -f deploy/.env ]; then
  cp deploy/.env.example deploy/.env
  TOKEN="$(openssl rand -hex 32)"
  sed -i "s/^FUSION_INGEST_TOKEN=.*/FUSION_INGEST_TOKEN=$TOKEN/" deploy/.env
  chmod 600 deploy/.env
fi

docker compose --env-file deploy/.env -f deploy/docker-compose.yml up -d --build

echo
echo "Core TST Fusion services are up in PAPER_ONLY mode."
echo "Checking health..."
sleep 3
curl --fail --silent http://127.0.0.1:8787/health || true
echo
echo
echo "Next on this same server:"
echo "  sudo bash $APP_DIR/deploy/install-condor.sh"
echo
echo "No Binance live executor is enabled by this bootstrap."
