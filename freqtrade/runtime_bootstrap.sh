#!/usr/bin/env bash
set -euo pipefail

# Railway persistent volumes are mounted after the image is built and may be
# root-owned. Prepare the canonical state directory once, then drop privileges
# back to the normal ftuser for every trading process.
mkdir -p /data
chown -R ftuser:ftuser /data
chmod 0750 /data

echo "[runtime-bootstrap] persistent /data prepared; dropping to ftuser"
exec su -s /bin/bash ftuser -c 'python -u /freqtrade/front_proxy.py & exec /bin/bash /freqtrade/entrypoint.sh'
