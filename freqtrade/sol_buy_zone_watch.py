from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.request import Request, urlopen

import telegram_signal_bridge as bridge

BINANCE_PUBLIC = 'https://data-api.binance.vision/api/v3'
SYMBOL = 'SOLUSDT'
STATE_FILE = Path('/freqtrade/user_data/sol_buy_zone_state.json')
POLL_SECONDS = 60
ALERT_COOLDOWN_SECONDS = 3 * 60 * 60

PULLBACK_LOW = 101.70
PULLBACK_HIGH = 102.20
BREAKOUT_LEVEL = 104.50
MIN_TAKER_BUY_RATIO = 0.52


def _get(path: str):
    req = Request(BINANCE_PUBLIC + path, headers={'User-Agent': 'tst-sol-buy-zone/1.0'})
    with urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def _klines(interval: str, limit: int = 8):
    return _get(f'/klines?symbol={SYMBOL}&interval={interval}&limit={limit}')


def _price() -> float:
    data = _get(f'/ticker/price?symbol={SYMBOL}')
    return float(data['price'])


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding='utf-8'))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state), encoding='utf-8')
    except Exception:
        pass


def _candle(row) -> dict:
    volume = float(row[5])
    taker_buy = float(row[9])
    return {
        'open': float(row[1]),
        'high': float(row[2]),
        'low': float(row[3]),
        'close': float(row[4]),
        'volume': volume,
        'taker_buy_ratio': (taker_buy / volume) if volume > 0 else 0.0,
        'close_time': int(row[6]),
    }


def evaluate() -> dict:
    rows15 = _klines('15m', 8)
    rows1h = _klines('1h', 5)
    price = _price()

    # Last item is the still-open candle. Use only completed candles for confirmation.
    c15 = [_candle(x) for x in rows15[:-1]]
    c1h = [_candle(x) for x in rows1h[:-1]]
    if len(c15) < 3 or len(c1h) < 1:
        return {'trigger': None, 'price': price, 'reason': 'insufficient_data'}

    last = c15[-1]
    prev = c15[-2]
    last1h = c1h[-1]

    # Fail closed if the most recent completed 1H candle lost the key support.
    if last1h['close'] < PULLBACK_LOW:
        return {
            'trigger': None,
            'price': price,
            'reason': '1h_below_invalidation',
            'invalidation': PULLBACK_LOW,
        }

    # Setup A: pullback into 101.7-102.2 followed by a completed bullish rejection
    # and improving close-to-close momentum with buyers taking at least 52% of volume.
    touched_zone = last['low'] <= PULLBACK_HIGH and last['high'] >= PULLBACK_LOW
    bullish_rejection = last['close'] > last['open'] and last['close'] >= PULLBACK_LOW
    improving = last['close'] > prev['close']
    buyer_confirm = last['taker_buy_ratio'] >= MIN_TAKER_BUY_RATIO
    pullback_ok = touched_zone and bullish_rejection and improving and buyer_confirm and price >= PULLBACK_LOW

    if pullback_ok:
        return {
            'trigger': 'PULLBACK_REJECTION',
            'price': price,
            'close15': last['close'],
            'low15': last['low'],
            'taker_buy_ratio': last['taker_buy_ratio'],
            'invalidation': PULLBACK_LOW,
        }

    # Setup B: completed 15m candle confirms above 104.5 while current price still holds
    # the breakout and buyer flow remains positive.
    crossed = prev['close'] <= BREAKOUT_LEVEL < last['close']
    breakout_hold = last['close'] > BREAKOUT_LEVEL and price > BREAKOUT_LEVEL
    breakout_momentum = last['close'] > prev['close'] and last['taker_buy_ratio'] >= MIN_TAKER_BUY_RATIO
    breakout_ok = breakout_hold and breakout_momentum and (crossed or last['low'] <= BREAKOUT_LEVEL)

    if breakout_ok:
        return {
            'trigger': 'BREAKOUT_HOLD',
            'price': price,
            'close15': last['close'],
            'low15': last['low'],
            'taker_buy_ratio': last['taker_buy_ratio'],
            'invalidation': BREAKOUT_LEVEL,
        }

    return {
        'trigger': None,
        'price': price,
        'reason': 'no_confirmed_setup',
        'last15_close': last['close'],
        'taker_buy_ratio': last['taker_buy_ratio'],
    }


def should_alert(result: dict, state: dict) -> bool:
    trigger = result.get('trigger')
    if not trigger:
        return False
    now = time.time()
    last_trigger = state.get('last_trigger')
    last_alert_at = float(state.get('last_alert_at') or 0)
    if trigger == last_trigger and now - last_alert_at < ALERT_COOLDOWN_SECONDS:
        return False
    return True


def send_alert(result: dict) -> None:
    trigger = result['trigger']
    price = float(result['price'])
    ratio = float(result.get('taker_buy_ratio') or 0) * 100
    if trigger == 'PULLBACK_REJECTION':
        setup = 'Pullback rejection from 101.7–102.2'
        invalidation = '1H close below 101.70'
    else:
        setup = 'Breakout + hold above 104.50'
        invalidation = 'Loss of 104.50 after breakout'

    text = (
        '🟢 SOL BUY ZONE CONFIRMED\n'
        f'SOL/USDT: {price:.2f}\n'
        f'Setup: {setup}\n'
        f'15m close: {float(result.get("close15") or price):.2f}\n'
        f'15m Taker Buy: {ratio:.1f}%\n'
        f'Invalidation: {invalidation}\n\n'
        'تنبيه دخول فقط — لم يتم تنفيذ أي شراء تلقائي.'
    )
    bridge.tg_api('sendMessage', {'text': text, 'disable_web_page_preview': True})


def main() -> None:
    print('[sol-buy-zone] ONLINE poll=60s telegram=enabled', flush=True)
    while True:
        try:
            result = evaluate()
            state = _load_state()
            if should_alert(result, state):
                send_alert(result)
                state['last_trigger'] = result['trigger']
                state['last_alert_at'] = time.time()
                state['last_price'] = result['price']
                _save_state(state)
                print(f'[sol-buy-zone] ALERT trigger={result["trigger"]} price={result["price"]:.4f}', flush=True)
            else:
                print(
                    f'[sol-buy-zone] check price={result.get("price")} trigger={result.get("trigger")} reason={result.get("reason", "")}',
                    flush=True,
                )
        except Exception as exc:
            print(f'[sol-buy-zone] ERROR {type(exc).__name__}: {exc}', flush=True)
        time.sleep(POLL_SECONDS)


if __name__ == '__main__':
    main()
