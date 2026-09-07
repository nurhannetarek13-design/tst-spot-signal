#!/usr/bin/env bash
set -euo pipefail

STRATEGY_DIR="/freqtrade/user_data/strategies"
STRATEGY_FILE="$STRATEGY_DIR/NostalgiaForInfinityX7.py"
NFI_COMMIT="da2836bcf07b6c3e4507d8d73e705a7feb7e72bc"
NFI_URL="https://raw.githubusercontent.com/iterativv/NostalgiaForInfinity/${NFI_COMMIT}/NostalgiaForInfinityX7.py"

mkdir -p "$STRATEGY_DIR" /freqtrade/user_data/logs

echo "[ready-bot] fetching pinned NostalgiaForInfinityX7 @ ${NFI_COMMIT}"
python - <<PY
from pathlib import Path
from urllib.request import Request, urlopen
url = "${NFI_URL}"
out = Path("${STRATEGY_FILE}")
req = Request(url, headers={"User-Agent": "tst-ready-bot/1.0"})
with urlopen(req, timeout=60) as r:
    data = r.read()
if len(data) < 10000 or b"class NostalgiaForInfinityX7" not in data:
    raise SystemExit("Pinned NFI strategy download failed validation")
out.write_bytes(data)
print(f"[ready-bot] strategy ready: {out} ({len(data)} bytes)")
PY

exec freqtrade trade \
  --logfile /freqtrade/user_data/logs/freqtrade.log \
  --db-url sqlite:////freqtrade/user_data/tradesv3.sqlite \
  --config /freqtrade/user_data/config.json \
  --strategy NostalgiaForInfinityX7
