#!/usr/bin/env bash
set -euo pipefail

STRATEGY_DIR="/freqtrade/user_data/strategies"
STRATEGY_FILE="$STRATEGY_DIR/NostalgiaForInfinityX7.py"
PROTECTED_FILE="$STRATEGY_DIR/NFIProtectedX7.py"
BRIDGE_FILE="/freqtrade/telegram_signal_bridge.py"
NFI_COMMIT="da2836bcf07b6c3e4507d8d73e705a7feb7e72bc"
NFI_URL="https://raw.githubusercontent.com/iterativv/NostalgiaForInfinity/${NFI_COMMIT}/NostalgiaForInfinityX7.py"
PROTECTED_URL="https://raw.githubusercontent.com/nurhannetarek13-design/tst-spot-signal/main/freqtrade/user_data/strategies/NFIProtectedX7.py"
BRIDGE_URL="https://raw.githubusercontent.com/nurhannetarek13-design/tst-spot-signal/main/freqtrade/telegram_signal_bridge.py"

mkdir -p "$STRATEGY_DIR" /freqtrade/user_data/logs /freqtrade/user_data/signals

echo "[ready-bot] fetching pinned NostalgiaForInfinityX7 @ ${NFI_COMMIT}"
python - <<PY
from pathlib import Path
from urllib.request import Request, urlopen

def fetch(url: str, out: str, min_size: int, needle: bytes):
    req = Request(url, headers={"User-Agent": "tst-ready-bot/1.3"})
    with urlopen(req, timeout=60) as r:
        data = r.read()
    if len(data) < min_size or needle not in data:
        raise SystemExit(f"Download failed validation: {url}")
    Path(out).write_bytes(data)
    print(f"[ready-bot] ready: {out} ({len(data)} bytes)")

fetch("${NFI_URL}", "${STRATEGY_FILE}", 10000, b"class NostalgiaForInfinityX7")
fetch("${PROTECTED_URL}", "${PROTECTED_FILE}", 500, b"class NFIProtectedX7")
fetch("${BRIDGE_URL}", "${BRIDGE_FILE}", 1000, b"def send_opportunity")
PY

# Telegram signal HTTP bridge. It only exposes opportunity confirmation links.
# No automatic Binance order is placed by Freqtrade.
python -u "$BRIDGE_FILE" &

# Background market-wide scanner. It scans every Binance Spot USDT symbol, logs
# pre-trading/newly-added markets, and prints the strongest liquid movers.
python -u - <<'PY' &
import json, time
from urllib.request import Request, urlopen

BASE = "https://data-api.binance.vision/api/v3"
HEADERS = {"User-Agent": "tst-all-usdt-scanner/1.0"}
known = None
stable_bases = {"USDC","FDUSD","TUSD","USDP","DAI","EUR"}

def get(path):
    req = Request(BASE + path, headers=HEADERS)
    with urlopen(req, timeout=20) as r:
        return json.loads(r.read())

while True:
    try:
        info = get("/exchangeInfo")
        usdt = {
            s["symbol"]: s for s in info.get("symbols", [])
            if s.get("quoteAsset") == "USDT" and s.get("isSpotTradingAllowed", True)
        }
        current = set(usdt)
        if known is None:
            known = current
            print(f"[market-scan] tracking {len(current)} Binance Spot USDT markets")
        else:
            for sym in sorted(current - known):
                s = usdt[sym]
                print(f"[new-listing] NEW MARKET {sym} status={s.get('status')}")
            known = current

        for sym, s in usdt.items():
            if s.get("status") != "TRADING":
                print(f"[new-listing] {sym} status={s.get('status')} (watch before trading opens)")

        tickers = get("/ticker/24hr")
        ranked = []
        for t in tickers:
            sym = t.get("symbol", "")
            s = usdt.get(sym)
            if not s or s.get("status") != "TRADING":
                continue
            base = s.get("baseAsset", "")
            if base in stable_bases or any(x in base for x in ("UP","DOWN","BULL","BEAR")):
                continue
            qv = float(t.get("quoteVolume") or 0)
            pct = float(t.get("priceChangePercent") or 0)
            if qv >= 1_000_000:
                ranked.append((abs(pct), pct, qv, sym))
        ranked.sort(reverse=True)
        top = ranked[:12]
        if top:
            summary = ", ".join(f"{s}:{p:+.1f}%" for _, p, _, s in top)
            print(f"[market-scan] top liquid movers: {summary}")
    except Exception as e:
        print(f"[market-scan] warning: {type(e).__name__}: {e}")
    time.sleep(60)
PY

exec freqtrade trade \
  --logfile /freqtrade/user_data/logs/freqtrade.log \
  --db-url sqlite:////freqtrade/user_data/tradesv3.sqlite \
  --config /freqtrade/user_data/config.json \
  --strategy NFIProtectedX7
