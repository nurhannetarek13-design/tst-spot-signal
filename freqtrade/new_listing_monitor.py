from __future__ import annotations

import os
import time

from fast_entry_engine import api, fast_ingest

SYMBOL = os.getenv('NEW_LISTING_SYMBOL', '牛来USDT')
START_MS = int(os.getenv('NEW_LISTING_START_MS', '1788964200000'))  # 2026-09-09 14:30 UTC
RANGE_MIN = int(os.getenv('NEW_LISTING_RANGE_MINUTES', '60'))
STAKE_USDT = float(os.getenv('NEW_LISTING_STAKE_USDT', '5.0'))
MAX_SPREAD_PCT = float(os.getenv('NEW_LISTING_MAX_SPREAD_PCT', '0.50'))
MAX_OPENING_GAIN = float(os.getenv('NEW_LISTING_MAX_OPENING_GAIN', '0.25'))
MAX_RANGE_EXTENSION = float(os.getenv('NEW_LISTING_MAX_RANGE_EXTENSION', '0.05'))
MAX_STOP_PCT = float(os.getenv('NEW_LISTING_MAX_STOP_PCT', '0.08'))
MIN_TAKER = float(os.getenv('NEW_LISTING_MIN_TAKER', '0.55'))
MIN_FIRST_HOUR_QV = float(os.getenv('NEW_LISTING_MIN_FIRST_HOUR_QV', '1000000'))
BREAKOUT_BUFFER = float(os.getenv('NEW_LISTING_BREAKOUT_BUFFER', '0.002'))
RETEST_TOL = float(os.getenv('NEW_LISTING_RETEST_TOLERANCE', '0.005'))
MONITOR_HOURS = int(os.getenv('NEW_LISTING_MONITOR_HOURS', '24'))
POLL_SEC = int(os.getenv('NEW_LISTING_POLL_SEC', '20'))

sent = False
last_status = ''


def min_notional(symbol: str) -> float:
    info = api('/exchangeInfo', {'symbol': symbol})
    symbols = info.get('symbols') or []
    if not symbols:
        raise RuntimeError('symbol-not-active')
    row = symbols[0]
    if row.get('status') != 'TRADING' or not row.get('isSpotTradingAllowed'):
        raise RuntimeError('spot-not-trading')
    for f in row.get('filters') or []:
        if f.get('filterType') in ('NOTIONAL', 'MIN_NOTIONAL'):
            value = float(f.get('minNotional') or 0.0)
            if value > 0:
                return value
    return 0.0


def setup() -> tuple[dict | None, str]:
    now_ms = int(time.time() * 1000)
    range_end = START_MS + RANGE_MIN * 60_000
    if now_ms < range_end:
        return None, 'OPENING_RANGE_ONLY'
    if now_ms > START_MS + MONITOR_HOURS * 3_600_000:
        return None, 'WINDOW_ENDED'

    rows = api('/klines', {'symbol': SYMBOL, 'interval': '1m', 'startTime': START_MS, 'limit': 360})
    rows = [x for x in rows if int(x[6]) < now_ms]
    if len(rows) < RANGE_MIN:
        return None, 'WAITING_60_CLOSED_1M'

    opening = rows[:RANGE_MIN]
    open_price = float(opening[0][1])
    high = max(float(x[2]) for x in opening)
    low = min(float(x[3]) for x in opening)
    qv = sum(float(x[7]) for x in opening)
    if open_price <= 0 or high <= 0:
        return None, 'BAD_OPENING_RANGE'
    if qv < MIN_FIRST_HOUR_QV:
        return None, 'LOW_FIRST_HOUR_LIQUIDITY'

    bars15 = api('/klines', {'symbol': SYMBOL, 'interval': '15m', 'startTime': range_end, 'limit': 32})
    bars15 = [x for x in bars15 if int(x[6]) < now_ms]
    breakout = next((b for b in bars15 if int(b[0]) >= range_end and float(b[4]) >= high * (1.0 + BREAKOUT_BUFFER)), None)
    if breakout is None:
        return None, 'WAITING_15M_BREAKOUT_CLOSE'

    after = [x for x in rows if int(x[0]) > int(breakout[6])]
    retest = next((r for r in after if float(r[3]) <= high * (1.0 + RETEST_TOL) and float(r[4]) >= high), None)
    if retest is None:
        return None, 'WAITING_SUCCESSFUL_RETEST'

    book = api('/ticker/bookTicker', {'symbol': SYMBOL})
    bid = float(book.get('bidPrice') or 0.0)
    ask = float(book.get('askPrice') or 0.0)
    if bid <= 0 or ask <= 0 or ask < bid:
        return None, 'BAD_ORDER_BOOK'
    mid = (bid + ask) / 2.0
    spread = (ask - bid) / mid * 100.0
    if spread > MAX_SPREAD_PCT:
        return None, 'SPREAD_TOO_WIDE'

    entry = ask
    if entry / open_price - 1.0 > MAX_OPENING_GAIN:
        return None, 'OVER_25PCT_FROM_OPEN'
    if entry / high - 1.0 > MAX_RANGE_EXTENSION:
        return None, 'TOO_EXTENDED_FROM_RANGE'
    if entry < high:
        return None, 'RETEST_LOST_SUPPORT'

    recent = rows[-5:]
    base = sum(float(x[5]) for x in recent)
    taker = sum(float(x[9]) for x in recent)
    taker_ratio = taker / base if base > 0 else 0.0
    if taker_ratio < MIN_TAKER:
        return None, 'BUY_PRESSURE_TOO_WEAK'

    retest_low = float(retest[3])
    stop = min(high * 0.99, retest_low * 0.995)
    if not (0 < stop < entry):
        return None, 'INVALID_STOP'
    stop_pct = (entry - stop) / entry
    if stop_pct > MAX_STOP_PCT:
        return None, 'STOP_OVER_8PCT'

    target = entry + 2.0 * (entry - stop)
    minimum = min_notional(SYMBOL)
    if STAKE_USDT + 1e-9 < minimum:
        return None, f'MIN_NOTIONAL_{minimum:.2f}_ABOVE_5'

    return {
        'entry': entry,
        'stop': stop,
        'target': target,
        'open': open_price,
        'range_high': high,
        'range_low': low,
        'first_hour_qv': qv,
        'spread': spread,
        'taker': taker_ratio,
        'stop_pct': stop_pct,
        'breakout_open_ms': int(breakout[0]),
    }, 'READY'


def main() -> None:
    global sent, last_status
    print(f'[new-listing] ONLINE symbol={SYMBOL} mode=FIRST_HOUR_RANGE_BREAKOUT_RETEST stake={STAKE_USDT:.2f} confirm_only=True', flush=True)
    while True:
        try:
            if sent:
                time.sleep(POLL_SEC)
                continue
            now_ms = int(time.time() * 1000)
            if now_ms < START_MS:
                status = 'WAITING_LISTING_OPEN'
                row = None
            else:
                row, status = setup()
            if status != last_status:
                print(f'[new-listing] {SYMBOL} {status}', flush=True)
                last_status = status
            if row is not None:
                payload = {
                    'id': f"newlisting-{int(row['breakout_open_ms'])}",
                    'symbol': SYMBOL,
                    'entry': row['entry'],
                    'stop': row['stop'],
                    'target': row['target'],
                    'stakeUSDT': STAKE_USDT,
                    'score': 95,
                    'strategy': (
                        f"NEW_LISTING_ORB_RETEST|rangeHigh={row['range_high']:.10g}|"
                        f"spread={row['spread']:.3f}%|taker={row['taker']*100:.1f}%|"
                        f"stop={row['stop_pct']*100:.2f}%|rr=2.00|firstHourQV={row['first_hour_qv']:.0f}"
                    ),
                    'dryRun': False,
                }
                result = fast_ingest(payload, timeout=30)
                if result.get('status') == 'FAST_SIGNAL_READY' and result.get('userConfirmationRequired') is True and result.get('autoBuy') is False:
                    sent = True
                    print(f"[new-listing] SIGNAL_READY {SYMBOL} entry={row['entry']:.10g} stop={row['stop']:.10g} target={row['target']:.10g} stake={STAKE_USDT:.2f}", flush=True)
                else:
                    raise RuntimeError(f'unexpected-ingest:{result}')
        except Exception as exc:
            print(f'[new-listing] waiting/error {type(exc).__name__}: {str(exc)[:240]}', flush=True)
        time.sleep(POLL_SEC)


if __name__ == '__main__':
    main()
