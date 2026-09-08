from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import time
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import telegram_signal_bridge as bridge

SCANNER_URL = os.getenv('VERCEL_SCANNER_URL', 'https://tst-spot-signal.vercel.app/api/market-scanner')
FAST_INGEST_URL = os.getenv('FAST_INGEST_URL', 'https://tst-spot-signal.nurhanne-tarek13.workers.dev/fast-signal-ingest')
BINANCE_BASES = ['https://data-api.binance.vision/api/v3', 'https://api.binance.com/api/v3']

SCAN_INTERVAL_SEC = int(os.getenv('FAST_SCAN_INTERVAL_SEC', '60'))
PAIR_COOLDOWN_SEC = int(os.getenv('FAST_PAIR_COOLDOWN_SEC', str(45 * 60)))
GLOBAL_COOLDOWN_SEC = int(os.getenv('FAST_GLOBAL_COOLDOWN_SEC', str(8 * 60)))
MAX_SIGNALS_PER_DAY = int(os.getenv('FAST_MAX_SIGNALS_PER_DAY', '6'))
MIN_SCORE = float(os.getenv('FAST_MIN_SCORE', '82'))
MIN_QUOTE_VOLUME_24H = float(os.getenv('FAST_MIN_QUOTE_VOLUME_24H', '5000000'))
MAX_SPREAD_PCT = float(os.getenv('FAST_MAX_SPREAD_PCT', '0.20'))

# Pre-momentum rules: catch compression/pressure BEFORE the breakout, never chase it.
MIN_BREAKOUT_DISTANCE = float(os.getenv('FAST_MIN_BREAKOUT_DISTANCE', '0.0015'))
MAX_BREAKOUT_DISTANCE = float(os.getenv('FAST_MAX_BREAKOUT_DISTANCE', '0.0080'))
MAX_MOM5 = float(os.getenv('FAST_MAX_MOM5', '0.0060'))
MAX_MOM15 = float(os.getenv('FAST_MAX_MOM15', '0.0120'))
MAX_MOM30 = float(os.getenv('FAST_MAX_MOM30', '0.0250'))
MAX_RSI = float(os.getenv('FAST_MAX_RSI', '66'))

last_signal_by_pair: dict[str, float] = {}
last_global_signal = 0.0
signal_day = ''
signals_today = 0
execution_ready = False

EXCLUDE = {
    'USDCUSDT', 'FDUSDUSDT', 'TUSDUSDT', 'USDPUSDT', 'DAIUSDT',
    'EURUSDT', 'AEURUSDT', 'BUSDUSDT', 'USD1USDT', 'RLUSDUSDT',
}


def validate_and_resolve_telegram_chat() -> bool:
    """Validate only the already-configured private chat.

    Cloudflare owns the Telegram webhook. Railway must never call getUpdates,
    otherwise Telegram returns 409 while the webhook is active.
    """
    try:
        me = bridge.tg_api('getMe', {})
        bot_id = str((me.get('result') or {}).get('id') or '')
    except Exception as exc:
        print(f'[telegram-chat] getMe failed: {type(exc).__name__}: {exc}')
        return False

    current = bridge.get_chat_id().strip()
    if not current or current == bot_id:
        print('[telegram-chat] NO_VALID_PRIVATE_CHAT')
        return False

    try:
        bridge.tg_api('sendChatAction', {'chat_id': current, 'action': 'typing'})
        print('[telegram-chat] VALID configured private chat')
        return True
    except Exception as exc:
        print(f'[telegram-chat] configured chat invalid: {type(exc).__name__}: {exc}')
        return False


bridge.get_free_usdt_balance = lambda: None


def get_json(url: str, timeout: int = 15):
    req = Request(url, headers={'User-Agent': 'tst-fast-entry/3.0', 'Accept': 'application/json'})
    with urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def api(path: str, params: dict):
    query = urlencode(params)
    last_error = None
    for base in BINANCE_BASES:
        try:
            return get_json(f'{base}{path}?{query}')
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f'Binance API unavailable: {last_error}')


def fast_ingest(payload: dict, timeout: int = 25) -> dict:
    token = (os.getenv('TELEGRAM_BOT_TOKEN') or '').strip()
    if not token:
        raise RuntimeError('TELEGRAM_BOT_TOKEN missing for signed ingest')
    raw = json.dumps(payload, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    ts = str(int(time.time() * 1000))
    signature = hmac.new(token.encode('utf-8'), ts.encode('utf-8') + b'.' + raw, hashlib.sha256).hexdigest()
    req = Request(
        FAST_INGEST_URL,
        data=raw,
        method='POST',
        headers={
            'User-Agent': 'tst-fast-entry/3.0',
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'X-Fast-Timestamp': ts,
            'X-Fast-Signature': signature,
            'Cache-Control': 'no-store',
        },
    )
    try:
        with urlopen(req, timeout=timeout) as r:
            row = json.loads(r.read() or b'{}')
    except Exception as exc:
        detail = ''
        try:
            detail = exc.read().decode('utf-8', errors='replace')[:500]
        except Exception:
            pass
        raise RuntimeError(f'fast ingest HTTP failed: {exc}{" | " + detail if detail else ""}') from exc
    if not row.get('ok'):
        raise RuntimeError(f"fast ingest rejected: {row.get('status') or row}")
    return row


def execution_preflight() -> bool:
    global execution_ready
    probe = {
        'id': f'preflight-{int(time.time())}',
        'symbol': 'BTCUSDT',
        'entry': 100.0,
        'stop': 99.0,
        'target': 101.5,
        'stakeUSDT': 5.5,
        'score': 100,
        'strategy': 'FAST_EXECUTION_PREFLIGHT',
        'dryRun': True,
    }
    try:
        row = fast_ingest(probe, timeout=30)
        execution_ready = row.get('status') == 'FAST_SIGNAL_DRYRUN_OK' and row.get('userConfirmationRequired') is True and row.get('autoBuy') is False
        if execution_ready:
            print(f"[fast-ingest-preflight] OK canTrade={row.get('canTrade')} credentialMode={row.get('credentialMode')} recommended={row.get('recommendedUSDT')}")
        else:
            print(f'[fast-ingest-preflight] FAIL unexpected response={row}')
    except Exception as exc:
        execution_ready = False
        print(f'[fast-ingest-preflight] FAIL {type(exc).__name__}: {exc}')
    return execution_ready


def ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    alpha = 2.0 / (period + 1.0)
    out = values[0]
    for v in values[1:]:
        out = alpha * v + (1.0 - alpha) * out
    return out


def rsi(values: list[float], period: int = 14) -> float:
    if len(values) < period + 1:
        return 50.0
    gains = 0.0
    losses = 0.0
    for i in range(len(values) - period, len(values)):
        d = values[i] - values[i - 1]
        if d > 0:
            gains += d
        else:
            losses -= d
    if losses <= 0:
        return 100.0
    rs = gains / losses
    return 100.0 - (100.0 / (1.0 + rs))


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def reset_day_counter() -> None:
    global signal_day, signals_today
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    if today != signal_day:
        signal_day = today
        signals_today = 0


def symbol_ok(symbol: str) -> bool:
    if not symbol.endswith('USDT') or symbol in EXCLUDE:
        return False
    base = symbol[:-4]
    return bool(base) and not base.endswith(('UP', 'DOWN', 'BULL', 'BEAR'))


def market_metrics(symbol: str) -> dict:
    # 1m bars let us see pressure building earlier than the old 5m detector.
    rows = api('/klines', {'symbol': symbol, 'interval': '1m', 'limit': 120})
    now_ms = int(time.time() * 1000)
    rows = [x for x in rows if int(x[6]) < now_ms]
    if not isinstance(rows, list) or len(rows) < 90:
        raise RuntimeError('insufficient closed candles')

    opens = [float(x[1]) for x in rows]
    highs = [float(x[2]) for x in rows]
    lows = [float(x[3]) for x in rows]
    closes = [float(x[4]) for x in rows]
    base_vol = [float(x[5]) for x in rows]
    quote_vol = [float(x[7]) for x in rows]
    taker_buy_base = [float(x[9]) for x in rows]

    last = closes[-1]
    if last <= 0:
        raise RuntimeError('bad last price')

    ema9 = ema(closes[-40:], 9)
    ema21 = ema(closes[-60:], 21)
    rsi14 = rsi(closes, 14)
    mom1 = closes[-1] / closes[-2] - 1.0
    mom5 = closes[-1] / closes[-6] - 1.0
    mom15 = closes[-1] / closes[-16] - 1.0
    mom30 = closes[-1] / closes[-31] - 1.0
    range1h = (max(highs[-60:]) - min(lows[-60:])) / last

    recent_qv = sum(quote_vol[-5:]) / 5.0
    prior_qv = sum(quote_vol[-25:-5]) / 20.0
    volume_ratio = recent_qv / prior_qv if prior_qv > 0 else 0.0
    volume_accel = quote_vol[-3] < quote_vol[-2] < quote_vol[-1]
    volume_accel_ratio = quote_vol[-1] / max(quote_vol[-3], 1e-12)

    recent_base = sum(base_vol[-5:])
    recent_taker = sum(taker_buy_base[-5:])
    taker_buy_ratio = recent_taker / recent_base if recent_base > 0 else 0.0

    recent_ranges = [(highs[i] - lows[i]) / max(closes[i], 1e-12) for i in range(len(rows) - 5, len(rows))]
    prior_ranges = [(highs[i] - lows[i]) / max(closes[i], 1e-12) for i in range(len(rows) - 25, len(rows) - 5)]
    recent_range_avg = sum(recent_ranges) / len(recent_ranges)
    prior_range_avg = sum(prior_ranges) / len(prior_ranges)
    compression_ratio = recent_range_avg / prior_range_avg if prior_range_avg > 0 else 9.0

    # Resistance excludes the latest minute. We want price sitting just below it, not above it.
    breakout_level = max(highs[-31:-1])
    distance_to_breakout = breakout_level / last - 1.0 if last > 0 else 9.0
    touches = sum(1 for h in highs[-30:-1] if breakout_level > 0 and abs(h / breakout_level - 1.0) <= 0.0025)

    trs = []
    for i in range(-20, 0):
        prev = closes[i - 1]
        tr = max(highs[i] - lows[i], abs(highs[i] - prev), abs(lows[i] - prev))
        trs.append(tr)
    atr_pct = (sum(trs) / len(trs)) / last if trs else 0.0

    book = api('/ticker/bookTicker', {'symbol': symbol})
    bid = float(book.get('bidPrice') or 0.0)
    ask = float(book.get('askPrice') or 0.0)
    bid_qty = float(book.get('bidQty') or 0.0)
    ask_qty = float(book.get('askQty') or 0.0)
    mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else last
    spread_pct = ((ask - bid) / mid * 100.0) if mid > 0 and ask >= bid > 0 else 999.0
    top_book_ratio = bid_qty / ask_qty if ask_qty > 0 else 0.0

    distance_ema9 = (last / ema9 - 1.0) if ema9 > 0 else 9.0
    body = abs(closes[-1] - opens[-1])
    upper_wick = highs[-1] - max(opens[-1], closes[-1])
    wick_ratio = upper_wick / max(body, last * 0.0001)

    return {
        'last': last,
        'ema9': ema9,
        'ema21': ema21,
        'rsi': rsi14,
        'mom1': mom1,
        'mom5': mom5,
        'mom15': mom15,
        'mom30': mom30,
        'range1h': range1h,
        'volume_ratio': volume_ratio,
        'volume_accel': volume_accel,
        'volume_accel_ratio': volume_accel_ratio,
        'taker_buy_ratio': taker_buy_ratio,
        'compression_ratio': compression_ratio,
        'breakout_level': breakout_level,
        'distance_to_breakout': distance_to_breakout,
        'touches': touches,
        'atr_pct': atr_pct,
        'spread_pct': spread_pct,
        'top_book_ratio': top_book_ratio,
        'distance_ema9': distance_ema9,
        'wick_ratio': wick_ratio,
    }


def score_setup(m: dict) -> tuple[float, list[str]]:
    score = 0.0
    reasons = []

    # Hard anti-chase: a move already in progress is not a pre-momentum setup.
    momentum_spent = (
        m['last'] >= m['breakout_level'] or
        m['distance_to_breakout'] < MIN_BREAKOUT_DISTANCE or
        m['mom5'] > MAX_MOM5 or
        m['mom15'] > MAX_MOM15 or
        m['mom30'] > MAX_MOM30 or
        m['rsi'] > MAX_RSI
    )
    if momentum_spent:
        return 0.0, ['momentum-already-spent']

    if m['ema9'] > m['ema21'] and m['last'] >= m['ema9'] * 0.998:
        score += 14; reasons.append('early-trend')
    if MIN_BREAKOUT_DISTANCE <= m['distance_to_breakout'] <= MAX_BREAKOUT_DISTANCE:
        score += 18; reasons.append('near-breakout')
    if m['compression_ratio'] <= 0.75:
        score += 14; reasons.append('compression')
    elif m['compression_ratio'] <= 0.90:
        score += 8
    if 1.10 <= m['volume_ratio'] <= 1.80:
        score += 12; reasons.append('volume-building')
    elif 1.00 <= m['volume_ratio'] < 1.10:
        score += 6
    if m['volume_accel'] and m['volume_accel_ratio'] >= 1.10:
        score += 10; reasons.append('volume-acceleration')
    if m['taker_buy_ratio'] >= 0.58:
        score += 14; reasons.append('taker-buy-pressure')
    elif m['taker_buy_ratio'] >= 0.55:
        score += 8
    if 48.0 <= m['rsi'] <= 64.0:
        score += 8; reasons.append('rsi-not-extended')
    if m['spread_pct'] <= MAX_SPREAD_PCT:
        score += 5; reasons.append('tight-spread')
    if m['top_book_ratio'] >= 1.10:
        score += 3; reasons.append('bid-pressure')
    if m['touches'] >= 2:
        score += 2; reasons.append('repeated-resistance-pressure')

    # We want quiet positive drift, not the middle of the candle explosion.
    if -0.0015 <= m['mom5'] <= 0.0045 and -0.002 <= m['mom15'] <= 0.009:
        score += 5; reasons.append('early-drift')

    if m['distance_ema9'] > 0.012:
        score -= 15
    if m['wick_ratio'] > 2.5:
        score -= 8
    if m['spread_pct'] > MAX_SPREAD_PCT:
        score -= 30
    if m['volume_ratio'] > 2.2:
        score -= 12

    return max(0.0, min(100.0, score)), reasons


def candidate_symbols(scan: dict) -> list[tuple[str, float, float]]:
    # Liquidity first. 24h winners are not automatically better; large positive change
    # is often exactly what makes us late.
    out: list[tuple[str, float, float]] = []
    seen: set[str] = set()

    mover_map = {}
    for item in scan.get('movers') or []:
        symbol = str(item.get('symbol') or '')
        if symbol_ok(symbol):
            mover_map[symbol] = (float(item.get('change') or 0.0), float(item.get('volume') or 0.0))

    for symbol in scan.get('liquid') or []:
        symbol = str(symbol)
        if not symbol_ok(symbol) or symbol in seen:
            continue
        change, volume = mover_map.get(symbol, (0.0, MIN_QUOTE_VOLUME_24H))
        if volume >= MIN_QUOTE_VOLUME_24H and -4.0 <= change <= 10.0:
            out.append((symbol, change, volume)); seen.add(symbol)
        if len(out) >= 50:
            break

    for symbol, (change, volume) in mover_map.items():
        if symbol in seen or volume < MIN_QUOTE_VOLUME_24H:
            continue
        if -2.0 <= change <= 8.0:
            out.append((symbol, change, volume)); seen.add(symbol)
        if len(out) >= 50:
            break

    return out[:50]


def maybe_signal(symbol: str, change24: float, volume24: float) -> bool:
    global last_global_signal, signals_today, execution_ready
    reset_day_counter()
    now = time.time()
    if not execution_ready:
        print(f'[fast-engine] {symbol} blocked: one-tap execution preflight is not ready')
        return False
    if signals_today >= MAX_SIGNALS_PER_DAY:
        return False
    if now - last_global_signal < GLOBAL_COOLDOWN_SEC:
        return False
    if now - last_signal_by_pair.get(symbol, 0.0) < PAIR_COOLDOWN_SEC:
        return False

    m = market_metrics(symbol)
    score, reasons = score_setup(m)
    print(
        f"[pre-score] {symbol} score={score:.0f} dist={m['distance_to_breakout']*100:.2f}% "
        f"compress={m['compression_ratio']:.2f} mom5={m['mom5']*100:+.2f}% mom15={m['mom15']*100:+.2f}% "
        f"volx={m['volume_ratio']:.2f} accel={'Y' if m['volume_accel'] else 'N'} "
        f"taker={m['taker_buy_ratio']*100:.1f}% rsi={m['rsi']:.1f} spread={m['spread_pct']:.3f}%"
    )
    if score < MIN_SCORE:
        return False

    # Final fail-closed anti-chase check immediately before creating the confirm signal.
    if not (MIN_BREAKOUT_DISTANCE <= m['distance_to_breakout'] <= MAX_BREAKOUT_DISTANCE):
        return False
    if m['mom5'] > MAX_MOM5 or m['mom15'] > MAX_MOM15 or m['rsi'] > MAX_RSI:
        return False

    tp_pct = clamp(max(0.009, m['atr_pct'] * 5.0), 0.009, 0.014)
    sl_pct = clamp(tp_pct / 1.55, 0.0055, 0.0090)
    entry = m['last']
    tp = entry * (1.0 + tp_pct)
    sl = entry * (1.0 - sl_pct)
    pair = f'{symbol[:-4]}/USDT'
    tag = (
        f"PREMOMENTUM|score={score:.0f}|dist={m['distance_to_breakout']*100:.2f}%|"
        f"compress={m['compression_ratio']:.2f}|mom5={m['mom5']*100:.2f}%|mom15={m['mom15']*100:.2f}%|"
        f"volx={m['volume_ratio']:.2f}|accel={1 if m['volume_accel'] else 0}|"
        f"taker={m['taker_buy_ratio']*100:.1f}%|rsi={m['rsi']:.1f}|spread={m['spread_pct']:.3f}%"
    )
    payload = {
        'id': f'{symbol}-{int(now)}',
        'symbol': symbol,
        'entry': entry,
        'stop': sl,
        'target': tp,
        'stakeUSDT': 5.5,
        'score': round(score),
        'strategy': tag,
        'dryRun': False,
    }
    row = fast_ingest(payload, timeout=30)
    if row.get('status') != 'FAST_SIGNAL_READY' or row.get('userConfirmationRequired') is not True or row.get('autoBuy') is not False:
        raise RuntimeError(f'unexpected one-tap ingest response: {row}')
    last_signal_by_pair[symbol] = now
    last_global_signal = now
    signals_today += 1
    print(f'[pre-momentum] one-tap ready {pair} score={score:.0f} amount={row.get("recommendedUSDT")} reasons={",".join(reasons)}')
    return True


def main() -> None:
    global execution_ready
    telegram_ok = validate_and_resolve_telegram_chat()
    execution_preflight()
    print(
        f'[fast-engine] ONLINE mode=PRE_MOMENTUM min_score={MIN_SCORE:.0f} max/day={MAX_SIGNALS_PER_DAY} '
        f'pair_cd={PAIR_COOLDOWN_SEC//60}m global_cd={GLOBAL_COOLDOWN_SEC//60}m '
        f'telegram={"OK" if telegram_ok else "WAITING"} execution={"OK" if execution_ready else "BLOCKED"}'
    )
    last_chat_retry = 0.0
    last_execution_retry = 0.0
    while True:
        try:
            if not telegram_ok and time.time() - last_chat_retry >= 60:
                telegram_ok = validate_and_resolve_telegram_chat()
                last_chat_retry = time.time()
            if not execution_ready and time.time() - last_execution_retry >= 60:
                execution_preflight()
                last_execution_retry = time.time()
            scan = get_json(SCANNER_URL, timeout=25)
            ranked = []
            for symbol, change, volume in candidate_symbols(scan):
                try:
                    m = market_metrics(symbol)
                    score, _ = score_setup(m)
                    ranked.append((score, symbol, change, volume, m))
                except Exception as exc:
                    print(f'[fast-engine] {symbol} metrics failed: {type(exc).__name__}: {exc}')
            ranked.sort(reverse=True, key=lambda x: x[0])
            for score, symbol, change, volume, _ in ranked[:15]:
                if score < MIN_SCORE:
                    break
                if not telegram_ok:
                    print(f'[fast-engine] {symbol} score={score:.0f} ready but Telegram chat is unresolved')
                    break
                if not execution_ready:
                    print(f'[fast-engine] {symbol} score={score:.0f} ready but one-tap execution is blocked')
                    break
                try:
                    if maybe_signal(symbol, change, volume):
                        break
                except Exception as exc:
                    print(f'[fast-engine] {symbol} signal failed: {type(exc).__name__}: {exc}')
                    execution_ready = False
        except Exception as exc:
            print(f'[fast-engine] loop warning: {type(exc).__name__}: {exc}')
        time.sleep(SCAN_INTERVAL_SEC)


if __name__ == '__main__':
    main()
