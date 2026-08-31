#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/nurhannetarek13-design/tst-spot-signal.git"
APP_DIR="/opt/tst-spot-signal"

sudo apt-get update
sudo apt-get install -y ca-certificates curl git

if ! command -v node >/dev/null 2>&1 || [ "$(node -p 'Number(process.versions.node.split(".")[0])' 2>/dev/null || echo 0)" -lt 22 ]; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
  sudo apt-get install -y nodejs
fi

if [ -d "$APP_DIR/.git" ]; then
  sudo git -C "$APP_DIR" fetch origin main
  sudo git -C "$APP_DIR" reset --hard origin/main
else
  sudo git clone "$REPO_URL" "$APP_DIR"
fi

sudo chown -R ubuntu:ubuntu "$APP_DIR"
cd "$APP_DIR"
npm install --omit=dev

if [ ! -f /etc/tst-spot-signal.env ]; then
  sudo cp oracle/tst-spot-signal.env.example /etc/tst-spot-signal.env
  sudo chmod 600 /etc/tst-spot-signal.env
fi

sudo cp oracle/tst-spot-signal.service /etc/systemd/system/tst-spot-signal.service
sudo systemctl daemon-reload
sudo systemctl enable tst-spot-signal.service

# Oracle's cloud firewall/security list must also allow TCP 10000.
sudo ufw allow 10000/tcp >/dev/null 2>&1 || true

echo
echo "Installed."
echo "1) Edit /etc/tst-spot-signal.env and add Binance keys + Telegram bot token."
echo "2) Keep LIVE_TRADING=false until Binance Trusted IP is configured."
echo "3) Then run: sudo systemctl restart tst-spot-signal"
echo "4) Check: curl http://127.0.0.1:10000/executor/status"
