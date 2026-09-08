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
req = Request(archive_url, headers={"User-Agent": "tst-ready-bot/3.2"})
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
    req = Request(url, headers={"User-Agent": "tst-ready-bot/3.2"})
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

export PYTHONPATH="/freqtrade:$NFI_DIR:${PYTHONPATH:-}"

if [[ -z "${TELEGRAM_BOT_TOKEN:-}" || -z "${TELEGRAM_CHAT_ID:-}" ]]; then
  echo "[ready-bot] TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required" >&2
  exit 3
fi

# Reclaim Telegram updates from an obsolete webhook without dropping pending
# messages, then prefer the latest real private user chat over a mistakenly
# configured bot id. This resolved id is inherited by every child process.
RESOLVED_CHAT_ID="$(python - <<'PY'
import json, os, sys, time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

token = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
configured = os.environ.get('TELEGRAM_CHAT_ID', '').strip()

def api(method, payload):
    req = Request(
        f'https://api.telegram.org/bot{token}/{method}',
        data=json.dumps(payload).encode(),
        headers={'Content-Type':'application/json','User-Agent':'tst-chat-resolver/2.0'},
    )
    try:
        with urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except HTTPError as exc:
        detail = exc.read().decode(errors='replace')[:600]
        raise RuntimeError(f'HTTP {exc.code}: {detail}') from exc

try:
    me = api('getMe', {})
    bot_id = str((me.get('result') or {}).get('id') or '')

    hook = api('getWebhookInfo', {})
    hook_url = str((hook.get('result') or {}).get('url') or '')
    if hook_url:
        print('[telegram-resolve] active webhook found; reclaiming updates without dropping pending messages', file=sys.stderr)
        api('deleteWebhook', {'drop_pending_updates': False})
        time.sleep(1.0)

    updates = None
    last_exc = None
    for _ in range(4):
        try:
            updates = api('getUpdates', {'limit':100, 'timeout':0, 'allowed_updates':['message']})
            break
        except Exception as exc:
            last_exc = exc
            time.sleep(1.0)
    if updates is None:
        raise last_exc or RuntimeError('getUpdates unavailable')

    candidates = []
    for upd in updates.get('result') or []:
        msg = upd.get('message') or {}
        chat = msg.get('chat') or {}
        sender = msg.get('from') or {}
        cid = chat.get('id')
        if cid is None or chat.get('type') != 'private' or sender.get('is_bot'):
            continue
        candidates.append((int(upd.get('update_id') or 0), str(cid)))
    candidates.sort()

    resolved = candidates[-1][1] if candidates else configured
    if resolved and resolved != configured:
        print(f'[telegram-resolve] RESOLVED_CHAT_ID={resolved}', file=sys.stderr)
    elif configured == bot_id:
        print('[telegram-resolve] configured chat id is still the bot id; no pending private message available', file=sys.stderr)
    print(resolved)
except Exception as exc:
    print(f'[telegram-resolve] warning: {type(exc).__name__}: {exc}', file=sys.stderr)
    print(configured)
PY
)"
if [[ -n "$RESOLVED_CHAT_ID" ]]; then
  export TELEGRAM_CHAT_ID="$RESOLVED_CHAT_ID"
fi

python -u "$BRIDGE_FILE" &
BRIDGE_PID=$!

SCANNER_URL="$SCANNER_URL" PAIRLIST_FILE="$PAIRLIST_FILE" python -u - <<'PY' &
import json, os, re, sys, time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

sys.path.insert(0, '/freqtrade')
from telegram_signal_bridge import send_prealert

url = os.environ['SCANNER_URL']
pairlist_file = Path(os.environ['PAIRLIST_FILE'])
known_universe = None
last_prealert = {}

EXCLUDE = {'USDCUSDT','FDUSDUSDT','TUSDUSDT','USDPUSDT','DAIUSDT','EURUSDT','AEURUSDT','BUSDUSDT'}
KLINE_BASES = ['https://data-api.binance.vision/api/v3', 'https://api.binance.com/api/v3']
PREALERT_COOLDOWN = 20 * 60
MIN_QUOTE_VOLUME_24H = 5_000_000.0
MIN_CHANGE_24H = 1.0
MAX_CHANGE_24H = 30.0
MIN_1H_RANGE = 0.012
MIN_15M_MOMENTUM = 0.001
MIN_VOLUME_RATIO = 1.15


def get_scan():
    with urlopen(Request(url, headers={'User-Agent':'tst-vercel-trigger/3.0','Accept':'application/json'}), timeout=25) as r:
        return json.loads(r.read())


def to_pair(symbol):
    if not symbol.endswith('USDT') or symbol in EXCLUDE:
        return None
    base = symbol[:-4]
    if not base or re.search(r'(UP|DOWN|BULL|BEAR)$', base):
        return None
    return f'{base}/USDT'


def get_klines(symbol):
    query = urlencode({'symbol': symbol, 'interval': '5m', 'limit': 13})
    last_error = None
    for base in KLINE_BASES:
        try:
            req = Request(f'{base}/klines?{query}', headers={'User-Agent':'tst-fast-prealert/1.0','Accept':'application/json'})
            with urlopen(req, timeout=12) as r:
                data = json.loads(r.read())
            if isinstance(data, list) and len(data) >= 13:
                return data
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f'klines unavailable: {last_error}')


def fast_metrics(symbol):
    k = get_klines(symbol)
    highs = [float(x[2]) for x in k]
    lows = [float(x[3]) for x in k]
    closes = [float(x[4]) for x in k]
    quote_volumes = [float(x[7]) for x in k]
    last = closes[-1]
    if last <= 0:
        return None
    range_1h = (max(highs[-12:]) - min(lows[-12:])) / last
    momentum_15m = (last / closes[-4]) - 1.0 if closes[-4] > 0 else -1.0
    recent = sum(quote_volumes[-3:]) / 3.0
    prior = sum(quote_volumes[-12:-3]) / 9.0
    volume_ratio = recent / prior if prior > 0 else 0.0
    return last, range_1h, momentum_15m, volume_ratio


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
            if known_universe is None:
                known_universe = all_symbols
            else:
                for sym in sorted(all_symbols - known_universe):
                    print(f'[universe-change] {sym} entered liquid/mover universe')
                known_universe = all_symbols

        movers = data.get('movers') or []
        if movers:
            print('[vercel-scan] top movers: ' + ', '.join(f"{x.get('symbol')}:{float(x.get('change',0)):+.1f}%" for x in movers[:8]))

        now = time.time()
        for m in movers[:12]:
            symbol = str(m.get('symbol') or '')
            pair = to_pair(symbol)
            if not pair:
                continue
            change = float(m.get('change') or 0.0)
            volume = float(m.get('volume') or 0.0)
            if volume < MIN_QUOTE_VOLUME_24H or change < MIN_CHANGE_24H or change > MAX_CHANGE_24H:
                continue
            if now - last_prealert.get(symbol, 0.0) < PREALERT_COOLDOWN:
                continue
            try:
                metrics = fast_metrics(symbol)
                if not metrics:
                    continue
                last, range_1h, momentum_15m, volume_ratio = metrics
                setup_ok = (
                    range_1h >= MIN_1H_RANGE
                    and momentum_15m >= MIN_15M_MOMENTUM
                    and volume_ratio >= MIN_VOLUME_RATIO
                )
                print(
                    f'[setup-gate] {symbol} range1h={range_1h*100:.2f}% '
                    f'mom15m={momentum_15m*100:+.2f}% volx={volume_ratio:.2f} ok={setup_ok}'
                )
                if setup_ok:
                    send_prealert(
                        pair=pair,
                        last=last,
                        change_24h=change,
                        volume_24h=volume,
                        range_1h=range_1h,
                        momentum_15m=momentum_15m,
                        volume_ratio=volume_ratio,
                    )
                    last_prealert[symbol] = now
            except Exception as exc:
                print(f'[setup-gate] {symbol} warning: {type(exc).__name__}: {exc}')

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
