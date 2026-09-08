from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import telegram_signal_bridge as bridge

SCANNER_URL = os.getenv('VERCEL_SCANNER_URL', 'https://tst-spot-signal.vercel.app/api/market-scanner')
BINANCE_BASES = ['https://data-api.binance.vision/api/v3', 'https://api.binance.com/api/v3']

SCAN_INTERVAL_SEC = int(os.getenv('FAST_SCAN_INTERVAL_SEC', '60'))
PAIR_COOLDOWN_SEC = int(os.getenv('FAST_PAIR_COOLDOWN_SEC', str(45 * 60)))
GLOBAL_COOLDOWN_SEC = int(os.getenv('FAST_GLOBAL_COOLDOWN_SEC', str(8 * 60)))
MAX_SIGNALS_PER_DAY = int(os.getenv('FAST_MAX_SIGNALS_PER_DAY', '6'))
MIN_SCORE = float(os.getenv('FAST_MIN_SCORE', '82'))
MIN_QUOTE_VOLUME_24H = float(os.getenv('FAST_MIN_QUOTE_VOLUME_24H', '5000000'))
MAX_SPREAD_PCT = float(os.getenv('FAST_MAX_SPREAD_PCT', '0.20'))

last_signal_by_pair: dict[str, float] = {}
last_global_signal = 0.0
signal_day = ''
signals_today = 0

EXCLUDE = {
    'USDCUSDT', 'FDUSDUSDT', 'TUSDUSDT', 'USDPUSDT', 'DAIUSDT',
    'EURUSDT', 'AEURUSDT', 'BUSDUSDT', 'USD1USDT', 'RLUSDUSDT',
}


def validate_and_resolve_telegram_chat() -> bool:
    """Resolve a real private human chat and reject the bot's own id."""
    try:
        me = bridge.tg_api('getMe', {})
        bot_id = str((me.get('result') or {}).get('id') or '')
    except Exception as exc:
        print(f'[telegram-chat] getMe failed: {type(exc).__name__}: {exc}')
        return False

    candidates: list[str] = []
    current = bridge.get_chat_id().strip()
    if current and current != bot_id:
        candidates.append(current)

    try:
        updates = bridge.tg_api('getUpdates', {'limit': 100, 'timeout': 0, 'allowed_updates': ['message']})
        rows = []
        for upd in updates.get('result') or []:
            msg = upd.get('message') or {}
            chat = msg.get('chat') or {}
            sender = msg.get('from') or {}
            cid = chat.get('id')
            if cid is None or chat.get('type') != 'private' or sender.get('is_bot'):
                continue
            cid = str(cid)
            if cid == bot_id:
                continue
            rows.append((int(upd.get('update_id') or 0), cid))
        rows.sort(reverse=True)
        for _, cid in rows:
            if cid not in candidates:
                candidates.append(cid)
    except Exception as exc:
        print(f'[telegram-chat] getUpdates warning: {type(exc).__name__}: {exc}')

    for cid in candidates:
        try:
            # Silent validation: no user-visible message.
            bridge.tg_api('sendChatAction', {'chat_id': cid, 'action': 'typing'})
            bridge.CHAT_ID_FILE.write_text(cid, encoding='utf-8')
            print('[telegram-chat] VALID private chat resolved')
            return True
        except Exception:
            continue

    try:
        bridge.CHAT_ID_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    print('[telegram-chat] NO_VALID_PRIVATE_CHAT')
    return False


# Railway's current region receives HTTP 451 from Binance private /account.
# Trading remains manual, so use deterministic fallback sizing instead of
# repeatedly logging a blocked private-balance request.
bridge.get_free_usdt_balance = lambda: None


def get_json(url: str, timeout: int = 15):
    req = Request(url, headers={'User-Agent': 'tst-fast-entry/1.0', 'Accept': 'application/json'})
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
    rows = api('/klines', {'symbol': symbol, 'interval': '5m', 'limit': 60})
    if not isinstance(rows, list) or len(rows) < 40:
        raise RuntimeError('insufficient candles')

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

    ema9 = ema(closes[-30:], 9)
    ema21 = ema(closes[-50:], 21)
    rsi14 = rsi(closes, 14)
    mom5 = closes[-1] / closes[-2] - 1.0
    mom15 = closes[-1] / closes[-4] - 1.0
    mom30 = closes[-1] / closes[-7] - 1.0
    range1h = (max(highs[-12:]) - min(lows[-12:])) / last

    recent_qv = sum(quote_vol[-3:]) / 3.0
    prior_qv = sum(quote_vol[-15:-3]) / 12.0
    volume_ratio = recent_qv / prior_qv if prior_qv > 0 else 0.0

    recent_base = sum(base_vol[-3:])
    recent_taker = sum(taker_buy_base[-3:])
    taker_buy_ratio = recent_taker / recent_base if recent_base > 0 else 0.0

    trs = []
    for i in range(-14, 0):
        prev = closes[i - 1]
        tr = max(highs[i] - lows[i], abs(highs[i] - prev), abs(lows[i] - prev))
        trs.append(tr)
    atr_pct = (sum(trs) / len(trs)) / last if trs else 0.0

    book = api('/ticker/bookTicker', {'symbol': symbol})
    bid = float(book.get('bidPrice') or 0.0)
    ask = float(book.get('askPrice') or 0.0)
    mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else last
    spread_pct = ((ask - bid) / mid * 100.0) if mid > 0 and ask >= bid > 0 else 999.0

    distance_ema9 = (last / ema9 - 1.0) if ema9 > 0 else 9.0
    body = abs(closes[-1] - opens[-1])
    upper_wick = highs[-1] - max(opens[-1], closes[-1])
    wick_ratio = upper_wick / max(body, last * 0.0001)

    return {
        'last': last,
        'ema9': ema9,
        'ema21': ema21,
        'rsi': rsi14,
        'mom5': mom5,
        'mom15': mom15,
        'mom30': mom30,
        'range1h': range1h,
        'volume_ratio': volume_ratio,
        'taker_buy_ratio': taker_buy_ratio,
        'atr_pct': atr_pct,
        'spread_pct': spread_pct,
        'distance_ema9': distance_ema9,
        'wick_ratio': wick_ratio,
    }


def score_setup(m: dict) -> tuple[float, list[str]]:
    score = 0.0
    reasons = []

    if m['ema9'] > m['ema21'] and m['last'] > m['ema9']:
        score += 18; reasons.append('trend')
    if 0.0010 <= m['mom15'] <= 0.018:
        score += 15; reasons.append('mom15')
    elif 0.0004 <= m['mom15'] < 0.0010:
        score += 8
    if m['mom5'] >= -0.001 and m['mom30'] > 0.0015:
        score += 10; reasons.append('short-momentum')
    if m['range1h'] >= 0.012:
        score += 10; reasons.append('movement')
    if m['volume_ratio'] >= 1.25:
        score += 16; reasons.append('volume')
    elif m['volume_ratio'] >= 1.05:
        score += 8
    if m['taker_buy_ratio'] >= 0.56:
        score += 14; reasons.append('taker-buy')
    elif m['taker_buy_ratio'] >= 0.52:
        score += 7
    if 52.0 <= m['rsi'] <= 72.0:
        score += 8; reasons.append('rsi')
    if m['spread_pct'] <= MAX_SPREAD_PCT:
        score += 5; reasons.append('spread')
    if 0.0 <= m['distance_ema9'] <= 0.010:
        score += 4; reasons.append('not-chasing')

    if m['mom15'] > 0.025:
        score -= 18
    if m['distance_ema9'] > 0.018:
        score -= 16
    if m['rsi'] > 78:
        score -= 12
    if m['wick_ratio'] > 2.5:
        score -= 8
    if m['spread_pct'] > MAX_SPREAD_PCT:
        score -= 25

    return max(0.0, min(100.0, score)), reasons


def candidate_symbols(scan: dict) -> list[tuple[str, float, float]]:
    out = []
    seen = set()
    for item in scan.get('movers') or []:
        symbol = str(item.get('symbol') or '')
        if not symbol_ok(symbol) or symbol in seen:
            continue
        change = float(item.get('change') or 0.0)
        volume = float(item.get('volume') or 0.0)
        if volume >= MIN_QUOTE_VOLUME_24H and 0.5 <= change <= 25.0:
            out.append((symbol, change, volume)); seen.add(symbol)
    for symbol in scan.get('liquid') or []:
        symbol = str(symbol)
        if symbol_ok(symbol) and symbol not in seen:
            out.append((symbol, 0.0, MIN_QUOTE_VOLUME_24H)); seen.add(symbol)
        if len(out) >= 40:
            break
    return out[:40]


def maybe_signal(symbol: str, change24: float, volume24: float) -> bool:
    global last_global_signal, signals_today
    reset_day_counter()
    now = time.time()
    if signals_today >= MAX_SIGNALS_PER_DAY:
        return False
    if now - last_global_signal < GLOBAL_COOLDOWN_SEC:
        return False
    if now - last_signal_by_pair.get(symbol, 0.0) < PAIR_COOLDOWN_SEC:
        return False

    m = market_metrics(symbol)
    score, reasons = score_setup(m)
    print(
        f"[fast-score] {symbol} score={score:.0f} spread={m['spread_pct']:.3f}% "
        f"mom15={m['mom15']*100:+.2f}% volx={m['volume_ratio']:.2f} "
        f"taker={m['taker_buy_ratio']*100:.1f}% rsi={m['rsi']:.1f}"
    )
    if score < MIN_SCORE:
        return False

    tp_pct = clamp(max(0.009, m['atr_pct'] * 4.0), 0.009, 0.015)
    sl_pct = clamp(tp_pct / 1.45, 0.006, 0.010)
    entry = m['last']
    tp = entry * (1.0 + tp_pct)
    sl = entry * (1.0 - sl_pct)
    pair = f'{symbol[:-4]}/USDT'
    tag = (
        f"FAST30_60|score={score:.0f}|mom15={m['mom15']*100:.2f}%|"
        f"volx={m['volume_ratio']:.2f}|taker={m['taker_buy_ratio']*100:.1f}%|"
        f"rsi={m['rsi']:.1f}|spread={m['spread_pct']:.3f}%"
    )
    bridge.send_opportunity(pair=pair, stake_usdt=5.5, entry=entry, tp=tp, sl=sl, tag=tag)
    last_signal_by_pair[symbol] = now
    last_global_signal = now
    signals_today += 1
    print(f'[fast-buy] sent {pair} score={score:.0f} reasons={",".join(reasons)}')
    return True


def main() -> None:
    telegram_ok = validate_and_resolve_telegram_chat()
    print(
        f'[fast-engine] ONLINE min_score={MIN_SCORE:.0f} max/day={MAX_SIGNALS_PER_DAY} '
        f'pair_cd={PAIR_COOLDOWN_SEC//60}m global_cd={GLOBAL_COOLDOWN_SEC//60}m '
        f'telegram={"OK" if telegram_ok else "WAITING"}'
    )
    last_chat_retry = 0.0
    while True:
        try:
            if not telegram_ok and time.time() - last_chat_retry >= 60:
                telegram_ok = validate_and_resolve_telegram_chat()
                last_chat_retry = time.time()
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
            for score, symbol, change, volume, _ in ranked[:12]:
                if score < MIN_SCORE:
                    break
                if not telegram_ok:
                    print(f'[fast-engine] {symbol} score={score:.0f} ready but Telegram chat is unresolved')
                    break
                try:
                    if maybe_signal(symbol, change, volume):
                        break
                except Exception as exc:
                    print(f'[fast-engine] {symbol} signal failed: {type(exc).__name__}: {exc}')
                    telegram_ok = False
        except Exception as exc:
            print(f'[fast-engine] loop warning: {type(exc).__name__}: {exc}')
        time.sleep(SCAN_INTERVAL_SEC)


if __name__ == '__main__':
    main()
