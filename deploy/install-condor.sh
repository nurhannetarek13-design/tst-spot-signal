#!/usr/bin/env bash
set -euo pipefail

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required before installing Hummingbot/Condor."
  exit 1
fi

echo
echo "Installing the official Hummingbot API + Condor stack."
echo "The official installer will ask for API credentials, Telegram details,"
echo "and Tailscale. For production, answer yes to Tailscale."
echo

cd /opt
curl -fsSL https://raw.githubusercontent.com/hummingbot/deploy/main/setup.sh | bash

echo
echo "Hummingbot/Condor installer finished."
echo "TST custom controller source is stored at:"
echo "  /opt/tst-spot-signal/hummingbot/controllers/directional_trading/tst_fusion_signal.py"
echo
echo "Upload it to Hummingbot API via the /controllers endpoint before deploying a bot."
echo "Keep allow_executor_actions=false while TST_FUSION_V1 is PAPER_ONLY."
