#!/usr/bin/env bash
set -euo pipefail

STRATEGY_DIR="/freqtrade/user_data/strategies"
STRATEGY_FILE="$STRATEGY_DIR/NostalgiaForInfinityX7.py"
PROTECTED_FILE="$STRATEGY_DIR/NFIProtectedX7.py"
NFI_COMMIT="da2836bcf07b6c3e4507d8d73e705a7feb7e72bc"
NFI_URL="https://raw.githubusercontent.com/iterativv/NostalgiaForInfinity/${NFI_COMMIT}/NostalgiaForInfinityX7.py"
PROTECTED_URL="https://raw.githubusercontent.com/nurhannetarek13-design/tst-spot-signal/main/freqtrade/user_data/strategies/NFIProtectedX7.py"

mkdir -p "$STRATEGY_DIR" /freqtrade/user_data/logs

echo "[ready-bot] fetching pinned NostalgiaForInfinityX7 @ ${NFI_COMMIT}"
python - <<PY
from pathlib import Path
from urllib.request import Request, urlopen

def fetch(url: str, out: str, min_size: int, needle: bytes):
    req = Request(url, headers={"User-Agent": "tst-ready-bot/1.1"})
    with urlopen(req, timeout=60) as r:
        data = r.read()
    if len(data) < min_size or needle not in data:
        raise SystemExit(f"Strategy download failed validation: {url}")
    Path(out).write_bytes(data)
    print(f"[ready-bot] strategy ready: {out} ({len(data)} bytes)")

fetch("${NFI_URL}", "${STRATEGY_FILE}", 10000, b"class NostalgiaForInfinityX7")
fetch("${PROTECTED_URL}", "${PROTECTED_FILE}", 500, b"class NFIProtectedX7")
PY

exec freqtrade trade \
  --logfile /freqtrade/user_data/logs/freqtrade.log \
  --db-url sqlite:////freqtrade/user_data/tradesv3.sqlite \
  --config /freqtrade/user_data/config.json \
  --strategy NFIProtectedX7
