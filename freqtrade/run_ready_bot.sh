#!/usr/bin/env bash
set -euo pipefail

USER_DATA="/freqtrade/user_data"
STRATEGY_DIR="$USER_DATA/strategies"
NFI_DIR="$USER_DATA/nfi-pinned"
PROTECTED_FILE="$STRATEGY_DIR/NFIProtectedX7.py"
BRIDGE_FILE="/freqtrade/telegram_signal_bridge.py"
CONFIG_FILE="$USER_DATA/config_nfi_dryrun.json"
PAIRLIST_FILE="$USER_DATA/vercel_pairlist.json"
NFI_COMMIT="da50440bd5f8a829af9dc768822fa31cfe4b7867"
NFI_ARCHIVE="https://github.com/iterativv/NostalgiaForInfinity/archive/${NFI_COMMIT}.tar.gz"
PROTECTED_URL="https://raw.githubusercontent.com/nurhannetarek13-design/tst-spot-signal/main/freqtrade/user_data/strategies/NFIProtectedX7.py"
BRIDGE_URL="https://raw.githubusercontent.com/nurhannetarek13-design/tst-spot-signal/main/freqtrade/telegram_signal_bridge.py"
SCANNER_URL="${VERCEL_SCANNER_URL:-https://tst-spot-signal.vercel.app/api/market-scanner}"

mkdir -p "$STRATEGY_DIR" "$USER_DATA/logs" "$USER_DATA/signals"

echo "[ready-bot] fetching complete pinned NostalgiaForInfinity @ ${NFI_COMMIT}"
python - <<PY
from pathlib import Path
from urllib.request import Request, urlopen
import io, os, shutil, tarfile

nfi_dir = Path("${NFI_DIR}")
archive_url = "${NFI_ARCHIVE}"
req = Request(archive_url, headers={"User-Agent": "tst-ready-bot/2.0"})
with urlopen(req, timeout=120) as r:
    data = r.read()
if len(data) < 1_000_000:
    raise SystemExit(f"NFI archive download too small: {len(data)}")

tmp = Path("/tmp/nfi-ready-extract")
shutil.rmtree(tmp, ignore_errors=True)
tmp.mkdir(parents=True)
with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
    members = tf.getmembers()
    for m in members:
        target = (tmp / m.name).resolve()
        if not str(target).startswith(str(tmp.resolve()) + os.sep):
            raise SystemExit("Unsafe archive member")
    tf.extractall(tmp)
roots = [p for p in tmp.iterdir() if p.is_dir()]
if len(roots) != 1:
    raise SystemExit("Unexpected NFI archive layout")
root = roots[0]
x7 = root / "NostalgiaForInfinityX7.py"
legacy = root / "legacy" / "NostalgiaForInfinityNextGen.py"
if not x7.exists() or x7.stat().st_size < 1_000_000 or not legacy.exists():
    raise SystemExit("Pinned NFI package validation failed")
shutil.rmtree(nfi_dir, ignore_errors=True)
shutil.move(str(root), str(nfi_dir))
shutil.rmtree(tmp, ignore_errors=True)
print(f"[ready-bot] NFI package ready: {nfi_dir}")

def fetch(url: str, out: str, min_size: int, needle: bytes):
    req = Request(url, headers={"User-Agent": "tst-ready-bot/2.0"})
    with urlopen(req, timeout=60) as r:
        payload = r.read()
    if len(payload) < min_size or needle not in payload:
        raise SystemExit(f"Download failed validation: {url}")
    Path(out).write_bytes(payload)
    print(f"[ready-bot] ready: {out} ({len(payload)} bytes)")

fetch("${PROTECTED_URL}", "${PROTECTED_FILE}", 500, b"class NFIProtectedX7")
fetch("${BRIDGE_URL}", "${BRIDGE_FILE}", 1000, b"def send_opportunity")
PY

if [[ ! -f "$CONFIG_FILE" ]]; then
  echo "[ready-bot] missing config: $CONFIG_FILE" >&2
  exit 2
fi

export PYTHONPATH="$NFI_DIR:${PYTHONPATH:-}"

if [[ -z "${TELEGRAM_BOT_TOKEN:-}" || -z "${TELEGRAM_CHAT_ID:-}" ]]; then
  echo "[ready-bot] TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required" >&2
  exit 3
fi

# Canonical Telegram + BUY preview bridge. It owns all Telegram messages,
# including new-listing notifications, so the scanner never sends duplicates.
python -u "$BRIDGE_FILE" &
BRIDGE_PID=$!

# Keep a local Freqtrade-compatible pairlist synchronized from Vercel every minute.
SCANNER_URL="$SCANNER_URL" PAIRLIST_FILE="$PAIRLIST_FILE" python -u - <<'PY' &
import json, os, re, time
from pathlib import Path
from urllib.request import Request, urlopen

url = os.environ['SCANNER_URL']
pairlist_file = Path(os.environ['PAIRLIST_FILE'])
known = None

EXCLUDE = {'USDCUSDT','FDUSDUSDT','TUSDUSDT','USDPUSDT','DAIUSDT','EURUSDT','AEURUSDT','BUSDUSDT'}

def get_scan():
    with urlopen(Request(url, headers={'User-Agent':'tst-vercel-trigger/2.0','Accept':'application/json'}), timeout=25) as r:
        return json.loads(r.read())

def to_pair(symbol):
    if not symbol.endswith('USDT') or symbol in EXCLUDE:
        return None
    base = symbol[:-4]
    if not base or re.search(r'(UP|DOWN|BULL|BEAR)$', base):
        return None
    return f'{base}/USDT'

while True:
    try:
        data = get_scan()
        liquid_symbols = data.get('liquid') or []
        pairs = [p for p in (to_pair(s) for s in liquid_symbols) if p][:120]
        if pairs:
            tmp = pairlist_file.with_suffix('.tmp')
            tmp.write_text(json.dumps({'pairs': pairs, 'refresh_period': 60}))
            tmp.replace(pairlist_file)
            print(f'[vercel-pairlist] wrote {len(pairs)} pairs to {pairlist_file}')
        all_symbols = set(liquid_symbols) | {x.get('symbol') for x in (data.get('movers') or []) if x.get('symbol')}
        if all_symbols:
            if known is None:
                known = all_symbols
            else:
                for sym in sorted(all_symbols - known):
                    print(f'[new-listing] detected {sym}; canonical Telegram bridge handles notifications')
                known = all_symbols
        movers = data.get('movers') or []
        if movers:
            print('[vercel-scan] top movers: ' + ', '.join(f"{x.get('symbol')}:{float(x.get('change',0)):+.1f}%" for x in movers[:8]))
    except Exception as e:
        print(f'[vercel-scan] warning: {type(e).__name__}: {e}')
    time.sleep(60)
PY
WATCHER_PID=$!

for _ in $(seq 1 20); do
  [[ -s "$PAIRLIST_FILE" ]] && break
  sleep 1
done
if [[ ! -s "$PAIRLIST_FILE" ]]; then
  echo "[ready-bot] local pairlist was not created" >&2
  exit 4
fi

python -u - <<'PY' &
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

pairlist = Path('/freqtrade/user_data/vercel_pairlist.json')

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != '/pairs':
            self.send_response(404); self.end_headers(); return
        try:
            body = pairlist.read_bytes()
        except FileNotFoundError:
            self.send_response(503); self.end_headers(); return
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *_):
        pass

print('[pairlist-http] serving http://127.0.0.1:8765/pairs')
HTTPServer(('127.0.0.1', 8765), H).serve_forever()
PY
PAIRLIST_HTTP_PID=$!

for _ in $(seq 1 20); do
  if python - <<'PY'
from urllib.request import urlopen
with urlopen('http://127.0.0.1:8765/pairs', timeout=2) as r:
    raise SystemExit(0 if r.status == 200 else 1)
PY
  then
    break
  fi
  sleep 1
done

trap 'kill "$BRIDGE_PID" "$WATCHER_PID" "$PAIRLIST_HTTP_PID" 2>/dev/null || true' EXIT

exec freqtrade trade \
  --logfile "$USER_DATA/logs/freqtrade.log" \
  --db-url "sqlite:///$USER_DATA/tradesv3.sqlite" \
  --config "$CONFIG_FILE" \
  --strategy-path "$STRATEGY_DIR" \
  --strategy NFIProtectedX7
