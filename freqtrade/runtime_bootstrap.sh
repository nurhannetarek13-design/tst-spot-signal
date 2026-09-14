#!/usr/bin/env bash
set -euo pipefail

# Railway persistent volumes are mounted after the image is built and may be
# root-owned. Prepare the canonical state directory once, then drop privileges
# back to the normal ftuser for every trading process.
mkdir -p /data
chown -R ftuser:ftuser /data
chmod 0750 /data

echo "[runtime-bootstrap] persistent /data prepared; dropping to ftuser"
exec su -s /bin/bash ftuser -c '
if [[ "${ALLIGATOR_SMC_PAPER_ENABLED:-1}" == "1" ]]; then
  (
    while true; do
      echo "[runtime-bootstrap] starting TST_ALLIGATOR_SMC_V2 paper runner"
      python -u /freqtrade/alligator_smc_paper_runner.py
      rc=$?
      echo "[runtime-bootstrap] paper runner exited rc=${rc}; restarting in 5s" >&2
      sleep 5
    done
  ) &
  echo "[runtime-bootstrap] Alligator SMC paper runner enabled"
else
  echo "[runtime-bootstrap] Alligator SMC paper runner disabled"
fi
exec /bin/bash /freqtrade/entrypoint.sh
'
